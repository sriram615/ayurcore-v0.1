"""
AyurCore MVP backup utility (Backup/Restore MVP phase).

Backs up the single production SQLite database (db/ayurcore.db) safely,
even while the application is running under WAL mode, using SQLite's own
VACUUM INTO mechanism -- never a raw filesystem copy. See the
Backup & Restore Read-Only Audit and docs/BACKUP_RESTORE_RUNBOOK.md for the
full reasoning; this module is the audit's "Exact implementation required"
section, now built.

WHY NOT A RAW `cp`: under WAL mode, recent commits can sit in a separate
`ayurcore.db-wal` file that hasn't been folded back into the main file yet.
A raw copy of just `ayurcore.db` while the app is live can miss committed
data, or (on some filesystems) catch a page mid-write. `VACUUM INTO` asks
SQLite itself for a transactionally consistent snapshot -- it is the
correct tool for a live WAL-mode database, and it needs no `-wal`/`-shm`
companion files at all: the output is always a single, flat, self-contained
file.

WHAT THIS SCRIPT DOES NOT DO (deliberately, per this phase's scope):
  - It never touches backend prescription logic, auth/RBAC, audit logic,
    migrations, API contracts, the UI, Gemini/AI logic, inventory logic,
    or DB transaction semantics. It only ever READS the live database
    (via VACUUM INTO, which SQLite guarantees is non-mutating on the
    source) and writes to an operator-supplied destination path that is
    verified, at every step, to never be the live database path.
  - It builds no cloud/S3/Drive/Dropbox integration, no in-app scheduler,
    and no database-encryption-at-rest for the LIVE database -- only the
    BACKUP artifact is optionally encrypted (see encrypt_backup() below),
    exactly as scoped.
  - It never prints patient data. Every printed line in main() is either
    a file path the operator already knows, a PASS/FAIL status word, or a
    bare integer row count -- never row content.

USAGE:
    python scripts/backup_db.py --dest /path/to/backups/ayurcore_2026-09-19.db
    python scripts/backup_db.py --dest /path/to/backups/ayurcore_2026-09-19.db --encrypt

Every function below is also directly importable and unit-testable against
an arbitrary (e.g. temporary, throwaway) SQLite file -- see
tests/test_backup.py, which does exactly that and never touches the real
db/ayurcore.db.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import DB_PATH as LIVE_DB_PATH  # noqa: E402  (see module docstring for path append)

# Every table this project's migrations/ directory currently defines (001-012),
# plus schema_migrations itself (created by db/migrations.py, not a numbered
# file). Used only to report row COUNTS after a successful backup -- never
# row content. Kept as an explicit, hand-maintained list (not introspected
# from sqlite_master at call time for THIS constant) so that a table being
# unexpectedly absent from a backup is visible by omission from the report,
# rather than this script silently trusting whatever happens to be in the
# file. verify_backup() below still uses sqlite_master to check which of
# these are ACTUALLY present before counting, so an intentionally-older
# schema (e.g. a pre-restore-migration database) doesn't crash this script
# on a table that doesn't exist yet.
KNOWN_TABLES = [
    "inventory",
    "prescriptions",
    "panchakarma_resources",
    "corrections",
    "prescription_previews",
    "users",
    "sessions",
    "idempotency_keys",
    "audit_events",
    "login_throttle",
    "patients",
    "encounters",
    "prescription_items",
    "schema_migrations",
]

# The migration that introduces each table (security review 4.1). A backup
# records its own schema version in schema_migrations, so verify_backup can
# REQUIRE exactly the tables that version must contain -- an older backup is
# still accepted (the runner upgrades it on startup), but a backup that
# claims a version and is missing one of that version's tables is rejected.
# Keep in sync with migrations/ when a migration adds a table.
TABLE_INTRODUCED_IN = {
    "inventory": 1,
    "prescriptions": 1,
    "panchakarma_resources": 1,
    "corrections": 1,
    "prescription_previews": 3,
    "users": 4,
    "sessions": 5,
    "idempotency_keys": 6,
    "audit_events": 7,
    "login_throttle": 8,
    "patients": 9,
    "encounters": 10,
    "prescription_items": 15,
}

PASSPHRASE_ENV_VAR = "AYURCORE_BACKUP_PASSPHRASE"


class BackupError(Exception):
    """Base class for every error this module raises. A BackupError always
    means the operation did NOT complete successfully -- nothing that
    raises this should ever be reported as a success by a caller."""


@dataclass
class VerifyResult:
    ok: bool
    reason: str
    table_row_counts: dict = field(default_factory=dict)


def _existing_tables(conn: sqlite3.Connection) -> set:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def backup_database(source_path: str, dest_path: str) -> str:
    """Creates a self-contained, transactionally consistent snapshot of the
    SQLite database at `source_path` into a brand-new file at `dest_path`,
    via `VACUUM INTO`. Safe to call against a live, concurrently-written
    WAL-mode database -- no application downtime required, and this
    function never modifies `source_path` (VACUUM INTO is a read operation
    against its source; see tests/test_backup.py's
    test_backup_does_not_modify_the_source_database for an executed proof,
    not just an assertion).

    Raises BackupError -- never returns a partial or silently-wrong result
    -- if:
      - `dest_path` resolves to the same file as `source_path` (refuses to
        ever treat the live database as a valid backup destination)
      - `dest_path`'s parent directory does not exist (this script does
        not create arbitrary directory structure on the operator's disk)
      - `dest_path` already exists (never silently overwrites a prior
        backup, or anything else already sitting at that path)
      - the VACUUM INTO statement itself fails for any reason (disk full,
        permissions, source database locked past the busy timeout, etc.)

    Returns the resolved `dest_path` on success.
    """
    source_real = os.path.realpath(source_path)
    dest_real = os.path.realpath(dest_path)

    if source_real == dest_real:
        raise BackupError(
            f"Refusing to back up: destination path resolves to the same file as the source "
            f"database ({dest_path}). A backup must never overwrite the live database."
        )

    dest_dir = os.path.dirname(dest_real) or "."
    if not os.path.isdir(dest_dir):
        raise BackupError(
            f"Refusing to back up: destination directory does not exist: {dest_dir}. "
            f"Create it first -- this script does not create directories on your behalf."
        )

    if os.path.exists(dest_real):
        raise BackupError(
            f"Refusing to back up: destination file already exists: {dest_path}. "
            f"Choose a new path (e.g. one that includes a timestamp) rather than overwriting it."
        )

    if not os.path.exists(source_path):
        raise BackupError(f"Source database does not exist: {source_path}")

    try:
        conn = sqlite3.connect(source_path, timeout=15)
    except sqlite3.Error as e:
        raise BackupError(f"Could not open source database {source_path}: {e}")

    try:
        try:
            conn.execute("VACUUM INTO ?", (dest_real,))
        except sqlite3.Error as e:
            raise BackupError(f"VACUUM INTO failed: {e}")
    finally:
        conn.close()

    return dest_real


def verify_backup(dest_path: str) -> VerifyResult:
    """The gate a backup must pass before it may ever be reported as
    successful or relied upon for a restore. Checks, in order:

      1. The file exists and can be opened as a SQLite database at all
         (catches a truncated/corrupted/non-SQLite file immediately,
         rather than failing confusingly on the next check).
      2. `PRAGMA integrity_check` returns exactly the single row `('ok',)`.
      3. `PRAGMA foreign_key_check` returns zero rows (no dangling
         foreign-key references anywhere in the backup).

    Only if all three pass does this function report ok=True and populate
    per-table row COUNTS (never row content) for every table from
    KNOWN_TABLES that is actually present in the file -- a table's absence
    (e.g. verifying a backup taken from an older schema) is not itself a
    failure here; that is exactly what the migration runner is for on the
    next startup (see docs/BACKUP_RESTORE_RUNBOOK.md).

    Security review 4.1 -- two further checks, after the three above:
      4. The backup is an AyurCore migrated database (schema_migrations
         present) and contains EVERY table its own recorded schema version
         requires (TABLE_INTRODUCED_IN). An older schema version is still
         accepted; a table missing for the version the backup claims is a
         failure, not a silently shorter row-count report.
      5. Every clinic's audit hash chain inside the backup verifies
         (backend.audit_store.verify_audit_integrity on the backup's own
         connection), so a backup of an already-tampered audit trail cannot
         pass as a good restore point.
    """
    if not os.path.exists(dest_path):
        return VerifyResult(False, f"backup file does not exist: {dest_path}")

    try:
        conn = sqlite3.connect(dest_path, timeout=15)
    except sqlite3.Error as e:
        return VerifyResult(False, f"backup file could not be opened as a SQLite database: {e}")

    try:
        try:
            integrity_rows = conn.execute("PRAGMA integrity_check;").fetchall()
        except sqlite3.DatabaseError as e:
            return VerifyResult(False, f"PRAGMA integrity_check could not run -- not a valid SQLite database: {e}")

        integrity_ok = len(integrity_rows) == 1 and integrity_rows[0][0] == "ok"
        if not integrity_ok:
            detail = "; ".join(str(r[0]) for r in integrity_rows) if integrity_rows else "no result returned"
            return VerifyResult(False, f"PRAGMA integrity_check reported problems: {detail}")

        conn.execute("PRAGMA foreign_keys = ON;")
        fk_violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_violations:
            return VerifyResult(False, f"PRAGMA foreign_key_check found {len(fk_violations)} violation(s)")

        present_tables = _existing_tables(conn)
        if "schema_migrations" not in present_tables:
            return VerifyResult(False, "not an AyurCore migrated database: schema_migrations is missing")
        schema_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
        missing = sorted(t for t, v in TABLE_INTRODUCED_IN.items() if v <= schema_version and t not in present_tables)
        if missing:
            return VerifyResult(False, f"backup at schema version {schema_version} is missing required table(s): {missing}")
        if "audit_events" in present_tables:
            from backend.audit_store import verify_audit_integrity
            report = verify_audit_integrity(None, conn=conn)
            if not report.valid:
                return VerifyResult(False, f"audit hash chain invalid in backup: {len(report.violations)} violation(s)")
        counts = {}
        for table in KNOWN_TABLES:
            if table in present_tables:
                counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec: table from fixed allow-list, never operator/user input

        return VerifyResult(True, "ok", counts)
    finally:
        conn.close()


def _find_openssl() -> str:
    path = shutil.which("openssl")
    if not path:
        raise BackupError(
            "openssl was not found on PATH. AyurCore's backup encryption deliberately uses the system "
            "`openssl` command-line tool (present by default on macOS and Linux) instead of adding a new "
            "Python/pip dependency -- see docs/BACKUP_RESTORE_RUNBOOK.md. Install or enable openssl before "
            "using --encrypt, or run this script without --encrypt and apply an already-approved "
            "encryption step separately before the backup leaves this machine."
        )
    return path


def encrypt_backup(plain_path: str, encrypted_path: str, passphrase_env_var: str = PASSPHRASE_ENV_VAR) -> str:
    """Encrypts `plain_path` into a new file at `encrypted_path` using the
    system `openssl` binary: AES-256-CBC, PBKDF2 key derivation (200,000
    iterations), random salt. No new Python/pip dependency is introduced.

    The passphrase is read ONLY from the `passphrase_env_var` environment
    variable -- never accepted as a CLI argument (which other processes on
    the same machine could observe via a process listing), never
    hard-coded anywhere in this file, and never logged or printed by this
    function, on success or failure.

    Raises BackupError if the environment variable is unset, if
    `encrypted_path` already exists, if openssl cannot be found, or if the
    openssl subprocess itself fails -- and never leaves a partial output
    file behind on failure.
    """
    passphrase = os.environ.get(passphrase_env_var)
    if not passphrase:
        raise BackupError(
            f"{passphrase_env_var} is not set. Backup encryption requires a passphrase supplied via this "
            f"environment variable only -- never a command-line argument, never a hard-coded value. Set it "
            f"in your shell/session (or from a secrets manager you already trust) before using --encrypt."
        )

    if os.path.exists(encrypted_path):
        raise BackupError(f"Refusing to encrypt: destination already exists: {encrypted_path}")

    openssl_path = _find_openssl()
    # -pass env:VAR tells openssl to read the passphrase directly from the
    # named environment variable inside its own process -- it is never
    # placed on the command line (unlike -pass pass:literal or -k, both of
    # which would be visible to other processes via `ps`), and this
    # function never writes it to stdout/stderr/a log itself.
    cmd = [
        openssl_path, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
        "-in", plain_path, "-out", encrypted_path,
        "-pass", f"env:{passphrase_env_var}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        raise BackupError(f"Failed to invoke openssl: {e}")

    if result.returncode != 0:
        if os.path.exists(encrypted_path):
            os.remove(encrypted_path)
        # openssl's own stderr here never contains the passphrase (it was
        # never passed as a literal argument), so it's safe to surface.
        raise BackupError(f"openssl encryption failed (exit {result.returncode}): {result.stderr.strip()}")

    return encrypted_path


def decrypt_backup(encrypted_path: str, plain_path: str, passphrase_env_var: str = PASSPHRASE_ENV_VAR) -> str:
    """The inverse of encrypt_backup() -- needed during an actual restore
    of an encrypted backup (see docs/BACKUP_RESTORE_RUNBOOK.md, step 2).
    Same passphrase-handling rules apply: env-var only, never logged.
    """
    passphrase = os.environ.get(passphrase_env_var)
    if not passphrase:
        raise BackupError(f"{passphrase_env_var} is not set; cannot decrypt.")

    if os.path.exists(plain_path):
        raise BackupError(f"Refusing to decrypt: destination already exists: {plain_path}")

    openssl_path = _find_openssl()
    cmd = [
        openssl_path, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
        "-in", encrypted_path, "-out", plain_path,
        "-pass", f"env:{passphrase_env_var}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        raise BackupError(f"Failed to invoke openssl: {e}")

    if result.returncode != 0:
        if os.path.exists(plain_path):
            os.remove(plain_path)
        raise BackupError(f"openssl decryption failed (exit {result.returncode}): {result.stderr.strip()}")

    return plain_path


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="AyurCore MVP backup utility -- creates a verified, self-contained snapshot of the "
                     "production database using SQLite's VACUUM INTO. Never overwrites the live database. "
                     "Prints no patient data.",
    )
    parser.add_argument(
        "--dest", required=True,
        help="Destination path for the backup file. Must not already exist and must not be the live "
             "database path.",
    )
    parser.add_argument(
        "--encrypt", action="store_true",
        help=f"Encrypt the backup with openssl (AES-256-CBC/PBKDF2) immediately after it passes "
             f"verification, using the passphrase in the {PASSPHRASE_ENV_VAR} environment variable. "
             f"The plaintext backup is removed after a successful encryption unless --keep-plaintext "
             f"is also given.",
    )
    parser.add_argument(
        "--keep-plaintext", action="store_true",
        help="With --encrypt, keep the plaintext backup file in addition to the encrypted one. Off by "
             "default -- an unencrypted, PII-bearing file should not linger on disk longer than needed.",
    )
    args = parser.parse_args(argv)

    print("AyurCore backup starting.")
    print(f"  source:      {LIVE_DB_PATH}")
    print(f"  destination: {args.dest}")

    try:
        backup_database(LIVE_DB_PATH, args.dest)
    except BackupError as e:
        print(f"BACKUP FAILED: {e}", file=sys.stderr)
        return 1

    result = verify_backup(args.dest)
    if not result.ok:
        print(f"BACKUP FAILED verification: {result.reason}", file=sys.stderr)
        if os.path.exists(args.dest):
            print(f"Removing the invalid backup file this run just created: {args.dest}", file=sys.stderr)
            os.remove(args.dest)
        return 1

    print("Backup created and verified successfully.")
    print("  PRAGMA integrity_check: ok")
    print("  PRAGMA foreign_key_check: no violations")
    print("  Table row counts (counts only -- never row content):")
    for table, count in result.table_row_counts.items():
        print(f"    {table}: {count}")

    final_path = args.dest
    if args.encrypt:
        encrypted_path = args.dest + ".enc"
        try:
            encrypt_backup(args.dest, encrypted_path)
        except BackupError as e:
            print(f"ENCRYPTION FAILED: {e}", file=sys.stderr)
            print(
                f"The verified PLAINTEXT backup at {args.dest} was NOT removed -- it still contains "
                f"unencrypted patient data. Handle it accordingly (encrypt it by another approved means "
                f"before it leaves this machine, or fix the issue above and re-run).",
                file=sys.stderr,
            )
            return 1
        print(f"Backup encrypted successfully: {encrypted_path}")
        if not args.keep_plaintext:
            os.remove(args.dest)
            print(f"Plaintext backup removed ({args.dest}) -- the encrypted file is the artifact to keep/move off this machine.")
        final_path = encrypted_path

    print(f"DONE. Final backup artifact: {final_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
