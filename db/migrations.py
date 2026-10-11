"""
AyurCore's migration runner.

Phase A finding, before this file existed: the project did NOT have real
migration infrastructure. `db/database.py`'s old `init_db()` ran
`CREATE TABLE IF NOT EXISTS` for everything plus a hand-rolled
`_ensure_column()` helper that additively `ALTER TABLE`d a missing column
in with a default. That is a reasonable pattern for a one-developer
prototype, but it is not versioned, not sequential, has no record of what
has been applied where, and cannot distinguish "bootstrap an empty
database" from "upgrade an existing one" as two deliberately different
paths -- it just hopes both happen to be safe for the same statement. This
module replaces it with an actual migration system, small enough to fit
this SQLite/FastAPI project without adding a dependency (no Alembic, no
ORM) but with the real properties a migration system needs:

  - Deterministic ordering: migrations are files named `NNN_description.sql`
    (or `.py` -- see below) under migrations/, applied in strictly
    ascending numeric order. Two files sharing a number is an error, not a
    silent pick-one.
  - Applied-migration tracking: a `schema_migrations` table records every
    migration's version, filename, a SHA-256 checksum of its content at
    apply time, and when it was applied.
  - Tamper detection on the migration files themselves: before applying
    anything new, every ALREADY-applied migration's current on-disk
    content is re-checksummed and compared to what was recorded when it
    was applied. A mismatch raises immediately rather than silently
    re-running or silently ignoring an edited historical migration -- this
    is what actually enforces "do not rewrite historical migrations",
    rather than just asking nicely in a code comment.
  - Idempotent and safe to rerun: running this against a database that is
    already fully up to date applies nothing and returns an empty list.
    This is what backend/main.py's startup hook relies on -- every process
    start calls run_migrations(), and on all but the very first start (or
    the start right after a new migration ships) that's a fast no-op.
  - Transactional per migration: each migration's statements run inside one
    manually-managed BEGIN/COMMIT. A failure partway through rolls back
    that ONE migration's changes and stops the whole run immediately --
    later migrations are never applied out of order past a failed one, and
    a half-applied migration is never left recorded as applied.
  - Bootstraps an empty database and upgrades an existing one with the
    SAME migration files -- see migrations/001_baseline_schema.sql's own
    header comment for exactly how migration 001 does both at once.

Migration file formats supported:
  - `.sql`  -- one or more semicolon-separated DDL statements, executed in
    order inside the migration's transaction. This is what every migration
    in this project uses today. NOTE: the statement splitter here is a
    simple split on `;` -- it is NOT a real SQL parser. It works correctly
    for the plain CREATE TABLE / CREATE INDEX / ALTER TABLE statements this
    project's migrations are made of, but a migration file must not put a
    literal `;` inside a string value or comment. Keep migration SQL
    boring; this is a deliberate, documented limitation, not an oversight.
  - `.py`   -- for the rare migration that genuinely needs procedural logic
    a static SQL file can't express (a conditional backfill, data
    reshaping, or similar). Must define a module-level `upgrade(conn)`
    function that performs its work using the given connection and does
    NOT call conn.commit()/rollback() itself -- the runner controls the
    transaction boundary the same way it does for `.sql` files. Not used
    by any migration in this project yet; documented here so a future
    migration that needs it doesn't have to invent this convention from
    scratch.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable

MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")

_FILENAME_RE = re.compile(r"^(\d{3,})_[a-zA-Z0-9_]+\.(sql|py)$")


class MigrationError(Exception):
    """Base class for every error this module raises."""


class MigrationChecksumMismatchError(MigrationError):
    """Raised when an already-applied migration's on-disk content no longer
    matches the checksum recorded at the time it was applied. This means
    the file was edited after being applied somewhere -- exactly what
    "do not rewrite historical migrations" exists to prevent. The fix is
    never to make this error go away by re-checksumming; it's to revert
    the historical file and add a new migration instead."""


class MigrationOrderingError(MigrationError):
    """Raised when migration files can't be deterministically ordered --
    e.g. two files claim the same version number."""


class MigrationApplyError(MigrationError):
    """Raised when applying a specific migration's statements fails. The
    triggering migration's transaction is rolled back before this is
    raised; no later migration is attempted."""


class MigrationIntegrityError(MigrationError):
    """Raised when the optional integrity guard (see run_migrations) finds
    that the pending migration batch would break an invariant that held
    before it -- e.g. a clinic's audit hash chain that verified before the
    batch no longer verifies after it. The ENTIRE batch is rolled back
    (nothing from this call is committed) before this is raised."""


@dataclass(frozen=True)
class MigrationFile:
    version: int
    filename: str
    path: str
    checksum: str
    content: str


def _checksum(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def discover_migrations(migrations_dir: str = MIGRATIONS_DIR) -> list[MigrationFile]:
    """Scans `migrations_dir` for files matching NNN_description.(sql|py),
    returns them sorted by version ascending. Raises MigrationOrderingError
    if two files share a version number -- there is no reasonable default
    for "which one wins" here, so this is a hard stop, not a warning."""
    if not os.path.isdir(migrations_dir):
        return []

    by_version: dict[int, MigrationFile] = {}
    for name in sorted(os.listdir(migrations_dir)):
        match = _FILENAME_RE.match(name)
        if not match:
            continue  # e.g. __pycache__, README.md -- not a migration file.
        version = int(match.group(1))
        path = os.path.join(migrations_dir, name)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if version in by_version:
            raise MigrationOrderingError(
                f"Two migration files claim version {version}: "
                f"{by_version[version].filename!r} and {name!r}. "
                "Migration versions must be unique -- rename one."
            )
        by_version[version] = MigrationFile(
            version=version, filename=name, path=path, checksum=_checksum(content), content=content,
        )

    return [by_version[v] for v in sorted(by_version)]


def _ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _applied_versions(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    rows = conn.execute("SELECT version, filename, checksum, applied_at FROM schema_migrations").fetchall()
    return {row["version"]: row for row in rows}


def _split_sql_statements(script: str) -> list[str]:
    """Naive but documented: splits on `;`, drops blank/comment-only
    fragments. See this module's docstring for the limitation this
    implies. Comments (`-- ...`) are stripped line-by-line before
    splitting so a `;` inside a comment can't be mistaken for a statement
    terminator."""
    lines = []
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        lines.append(line)
    cleaned = "\n".join(lines)
    statements = [s.strip() for s in cleaned.split(";")]
    return [s for s in statements if s]


def _load_py_upgrade(migration: MigrationFile):
    spec = importlib.util.spec_from_file_location(f"_migration_{migration.version}", migration.path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "upgrade"):
        raise MigrationApplyError(
            f"Migration {migration.filename!r} is a .py migration but defines no upgrade(conn) function."
        )
    return module.upgrade


def run_migrations(
    conn: sqlite3.Connection | None = None,
    migrations_dir: str = MIGRATIONS_DIR,
    integrity_guard: Callable[[sqlite3.Connection], dict] | None = None,
) -> list[int]:
    """Applies every pending migration in `migrations_dir` to `conn`, in
    ascending version order, and returns the list of versions actually
    applied by THIS call (empty on an already-up-to-date database -- this
    is what makes it safe to call unconditionally on every app startup).

    If `conn` is None, opens one against this project's real database via
    db.database.get_connection() and closes it before returning -- the
    normal way this is called (e.g. from backend/main.py's startup hook).
    Callers that already have a connection open (tests, mainly) pass it in
    directly; this function never closes a connection it didn't own.

    Raises MigrationChecksumMismatchError if any already-applied
    migration's on-disk content has changed since it was applied, and
    MigrationApplyError if a pending migration's statements fail. A
    migration that fails does not block one that already succeeded earlier
    in the SAME call (that earlier migration's work is kept), but no
    migration after the failing one is attempted.

    Concurrency note (the adversarial "what happens with two workers"
    case): the whole discover-pending / apply-pending body below runs
    inside one `BEGIN IMMEDIATE` -- SQLite's own write-lock, not a
    made-up one -- so if two processes (e.g. two uvicorn workers) both
    call this at startup at the same moment, one of them actually runs
    first while the other blocks (up to db.database.get_connection()'s
    15-second busy timeout) rather than both racing to insert the same
    schema_migrations row. Whichever runs second then re-reads
    schema_migrations AFTER acquiring the lock -- by which point the
    first has already committed -- finds nothing pending, and returns []
    having applied nothing, instead of crashing on a duplicate-version
    insert. Each individual migration still gets its own SAVEPOINT inside
    that outer transaction specifically so a later migration's failure
    can be undone without also undoing an earlier one that already
    succeeded in the same call -- see test_migrations.py's
    test_failed_migration_rolls_back_and_does_not_block_a_later_fix for
    exactly that scenario, and test_concurrent_startups_do_not_race for
    the two-workers scenario.
    """
    owns_connection = conn is None
    if conn is None:
        from db.database import get_connection
        conn = get_connection()

    try:
        _ensure_schema_migrations_table(conn)
        conn.commit()

        migrations = discover_migrations(migrations_dir)

        # BEGIN IMMEDIATE acquires SQLite's write lock right here, for the
        # whole remainder of this call -- a concurrent call from another
        # connection/process blocks on ITS OWN BEGIN IMMEDIATE until this
        # one commits or rolls back. Both the tamper-check read and the
        # apply loop below run after acquiring this lock, so they always
        # see the freshest possible state, never a snapshot that a
        # concurrent caller is about to invalidate.
        conn.execute("BEGIN IMMEDIATE")
        try:
            applied = _applied_versions(conn)

            # Tamper check, before applying anything new -- an edited
            # historical migration is a problem regardless of whether
            # there happen to be new migrations pending this run.
            for migration in migrations:
                recorded = applied.get(migration.version)
                if recorded is not None and recorded["checksum"] != migration.checksum:
                    raise MigrationChecksumMismatchError(
                        f"Migration {migration.filename!r} (version {migration.version}) was applied "
                        f"with checksum {recorded['checksum']}, but its on-disk content now checksums to "
                        f"{migration.checksum}. It was edited after being applied -- revert it and add a "
                        "new migration instead of changing history."
                    )

            newly_applied: list[int] = []
            apply_error: MigrationApplyError | None = None
            # Optional integrity guard: a snapshot {key: bool} of invariants
            # (backend/main.py passes backend.audit_store.audit_chain_validity,
            # {clinic_id: audit chain verifies}) taken BEFORE the batch and
            # again AFTER it, through this same connection and inside this same
            # transaction, so it sees exactly what would be committed. Anything
            # true before and not true after rolls the whole batch back. It
            # recomputes every audit hash from stored content, so it catches a
            # migration that alters event content, not only ids/hashes.
            pending = [m for m in migrations if m.version not in applied]
            guard_before = integrity_guard(conn) if (integrity_guard and pending) else None
            for migration in migrations:
                if migration.version in applied:
                    continue

                savepoint = f"migration_{migration.version}"
                conn.execute(f"SAVEPOINT {savepoint}")
                try:
                    if migration.filename.endswith(".sql"):
                        for statement in _split_sql_statements(migration.content):
                            conn.execute(statement)
                    else:
                        upgrade_fn = _load_py_upgrade(migration)
                        upgrade_fn(conn)

                    conn.execute(
                        "INSERT INTO schema_migrations (version, filename, checksum) VALUES (?, ?, ?)",
                        (migration.version, migration.filename, migration.checksum),
                    )
                    conn.execute(f"RELEASE {savepoint}")
                    newly_applied.append(migration.version)
                except Exception as e:
                    conn.execute(f"ROLLBACK TO {savepoint}")
                    conn.execute(f"RELEASE {savepoint}")
                    apply_error = MigrationApplyError(
                        f"Migration {migration.filename!r} (version {migration.version}) failed to apply: {e}"
                    )
                    break  # never attempt a later migration past a failed one

            if guard_before is not None:
                guard_after = integrity_guard(conn)
                broken = sorted(k for k, ok in guard_before.items() if ok and not guard_after.get(k, False))
                if broken:
                    raise MigrationIntegrityError(
                        f"Pending migrations {[m.version for m in pending]} would break integrity for "
                        f"{len(broken)} key(s) (first: {broken[:3]}); the whole batch was rolled back."
                    )

            # Commit unconditionally here -- this persists whatever
            # migrations DID succeed above (via their RELEASEd savepoints)
            # even when a later one failed and was rolled back to its own
            # savepoint. The failure itself is still raised to the caller
            # right after, exactly as before.
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        if apply_error is not None:
            raise apply_error

        return newly_applied
    finally:
        if owns_connection:
            conn.close()


if __name__ == "__main__":
    applied = run_migrations()
    if applied:
        print(f"Applied {len(applied)} migration(s): {applied}")
    else:
        print("Database already up to date -- no migrations applied.")
