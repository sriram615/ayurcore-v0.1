"""
Migration-runner tests -- covers db/migrations.py, the Phase A
migration-infrastructure milestone.

Everything here runs against throwaway SQLite files (tempfile), never the
real db/ayurcore.db, EXCEPT where a test explicitly says otherwise. None of
this needs FastAPI -- the migration runner is pure sqlite3 + hashlib + file
I/O, so every test in this file is something that was genuinely executed,
not something that merely "should work" once FastAPI is available.

Specifically checks:
  1. Bootstrap: running the runner against a brand new, empty database file
     creates the full expected schema from nothing.
  2. Idempotency: running it again immediately applies nothing further and
     leaves schema_migrations unchanged.
  3. Upgrade-without-data-loss: running it against a database that already
     has the OLD pre-migration schema (created the way db/database.py used
     to, by hand, before migrations existed) -- including a real row of
     data -- does not touch, alter, or lose that row, and still correctly
     adds the one genuinely-new thing (clinic_id on panchakarma_resources).
  4. Deterministic ordering is enforced: two migration files claiming the
     same version number is a hard error, not a silent pick-one.
  5. Tamper detection: editing an already-applied migration's on-disk
     content is caught on the next run, before anything else happens.
  6. Fail-fast: a broken migration's failure rolls back cleanly, is
     recorded as NOT applied, and does not block a later successful run
     once fixed -- later migrations are never skipped ahead to.
  7. "Restart" simulation: applying migrations, closing the connection, and
     reopening a fresh connection to the same file (simulating a process
     restart that calls run_migrations() again on boot, per
     backend/main.py's startup hook) is a safe no-op.
  8. Integration: this project's REAL migrations/ directory, applied to a
     fresh temp database, produces the exact schema the application
     expects (all four tables, panchakarma_resources carrying clinic_id).
  9. The adversarial "what happens with two workers" case: two threads,
     each with their own sqlite3 connection to the SAME fresh database
     file, both calling run_migrations() at effectively the same moment.
     Neither may crash, and the database must end up with each migration
     applied exactly once -- not duplicated, not skipped.

Run directly (no pytest required):
    python tests/test_migrations.py
"""
import os
import sqlite3
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrations import (
    run_migrations,
    discover_migrations,
    MigrationChecksumMismatchError,
    MigrationOrderingError,
    MigrationApplyError,
    MIGRATIONS_DIR,
)

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Records AND enforces one assertion -- raises AssertionError
    immediately on failure instead of only printing a FAIL line and
    incrementing a counter, so that `pytest tests/` genuinely fails when a
    check fails, not just this file's own __main__ block. See
    tests/test_rbac.py's module docstring, "PYTEST COMPATIBILITY", for the
    real incident (a cross-clinic information-disclosure bug that was
    invisible to `pytest tests/ -v`) this fixes across every test file in
    this project that still had the old, non-raising version."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {r[0] for r in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _temp_db_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # run_migrations should be able to bootstrap from truly nothing
    return path


def test_bootstrap_from_empty_database():
    # Not hardcoded: this project's real migrations/ directory grows with
    # every Phase A milestone (2 files as of Migration Infrastructure, 3 as
    # of A1, ...). See test_real_project_migrations_directory_bootstraps_cleanly's
    # docstring for why a literal count here would be the wrong kind of
    # "strict" -- it would fail on every legitimate new migration.
    expected_versions = [m.version for m in discover_migrations()]
    db_path = _temp_db_path()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        applied = run_migrations(conn=conn)
        check("bootstrap applies every real project migration", applied == expected_versions, applied)
        check(
            "all four application tables exist after bootstrap",
            {"inventory", "prescriptions", "panchakarma_resources", "corrections"} <= _table_names(conn),
            _table_names(conn),
        )
        check(
            "panchakarma_resources has clinic_id after bootstrap (migration 002)",
            "clinic_id" in _columns(conn, "panchakarma_resources"),
        )
        check(
            f"schema_migrations recorded exactly {len(expected_versions)} applied migrations",
            conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(expected_versions),
        )
        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_rerun_is_idempotent():
    expected_versions = [m.version for m in discover_migrations()]
    db_path = _temp_db_path()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        run_migrations(conn=conn)

        applied_second_run = run_migrations(conn=conn)
        check("a second run against an up-to-date database applies nothing", applied_second_run == [])
        check(
            f"schema_migrations still has exactly {len(expected_versions)} rows, no duplicates",
            conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(expected_versions),
        )
        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_upgrade_of_pre_migration_database_preserves_data():
    """Simulates exactly the database this project actually has: created by
    the OLD hand-written init_db() before migrations existed (so no
    schema_migrations table, and panchakarma_resources has no clinic_id
    column), with a real row of data already in it."""
    db_path = _temp_db_path()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Recreate the OLD pre-migration schema by hand, matching what
        # db/database.py used to create directly, before this milestone.
        conn.execute('''
            CREATE TABLE inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clinic_id TEXT NOT NULL DEFAULT 'default_clinic',
                herb_name TEXT NOT NULL,
                botanical_name TEXT NOT NULL,
                stock_grams INTEGER NOT NULL CHECK (stock_grams >= 0),
                batch_code TEXT NOT NULL,
                potency_score INTEGER NOT NULL CHECK (potency_score >= 0 AND potency_score <= 100)
            )
        ''')
        conn.execute('''
            CREATE TABLE prescriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clinic_id TEXT NOT NULL DEFAULT 'default_clinic',
                patient_name TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.execute('''
            CREATE TABLE panchakarma_resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                resource_type TEXT NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prescription_id INTEGER NOT NULL REFERENCES prescriptions(id),
                clinic_id TEXT NOT NULL DEFAULT 'default_clinic',
                ai_dosha_imbalance TEXT NOT NULL,
                ai_prescribed_herbs_json TEXT NOT NULL,
                confirmed_dosha_imbalance TEXT NOT NULL,
                confirmed_prescribed_herbs_json TEXT NOT NULL,
                was_changed INTEGER NOT NULL CHECK (was_changed IN (0, 1)),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # A real pre-existing row -- this is the row that must NOT be lost
        # or altered by applying migrations on top of this database.
        conn.execute('''
            INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score)
            VALUES ('default_clinic', 'Ashwagandha', 'Withania somnifera', 4321, 'PRE-EXISTING-BATCH', 95)
        ''')
        conn.execute('''
            INSERT INTO panchakarma_resources (name, quantity, resource_type) VALUES ('Sample Resource', 5, 'oil')
        ''')
        conn.commit()

        applied = run_migrations(conn=conn)
        expected_versions = [m.version for m in discover_migrations()]
        check("upgrading a pre-migration database applies every real migration", applied == expected_versions, applied)

        row = conn.execute("SELECT stock_grams, batch_code FROM inventory WHERE herb_name = 'Ashwagandha'").fetchone()
        check(
            "the pre-existing inventory row survives untouched",
            row is not None and row["stock_grams"] == 4321 and row["batch_code"] == "PRE-EXISTING-BATCH",
            dict(row) if row else None,
        )
        pk_row = conn.execute("SELECT clinic_id FROM panchakarma_resources WHERE name = 'Sample Resource'").fetchone()
        check(
            "the pre-existing panchakarma_resources row is backfilled with the default clinic_id, not dropped",
            pk_row is not None and pk_row["clinic_id"] == "default_clinic",
            dict(pk_row) if pk_row else None,
        )
        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_duplicate_version_numbers_are_rejected():
    with tempfile.TemporaryDirectory() as tmp_migrations_dir:
        with open(os.path.join(tmp_migrations_dir, "001_first.sql"), "w") as f:
            f.write("CREATE TABLE a (id INTEGER);")
        with open(os.path.join(tmp_migrations_dir, "001_second.sql"), "w") as f:
            f.write("CREATE TABLE b (id INTEGER);")

        try:
            discover_migrations(tmp_migrations_dir)
            check("duplicate version numbers raise MigrationOrderingError", False, "no exception raised")
        except MigrationOrderingError:
            check("duplicate version numbers raise MigrationOrderingError", True)


def test_tampering_with_an_applied_migration_is_detected():
    with tempfile.TemporaryDirectory() as tmp_migrations_dir:
        migration_path = os.path.join(tmp_migrations_dir, "001_create_widgets.sql")
        with open(migration_path, "w") as f:
            f.write("CREATE TABLE widgets (id INTEGER PRIMARY KEY);")

        db_path = _temp_db_path()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            run_migrations(conn=conn, migrations_dir=tmp_migrations_dir)

            # Edit the migration AFTER it has been applied -- exactly the
            # scenario "do not rewrite historical migrations" warns against.
            with open(migration_path, "a") as f:
                f.write("\n-- an edit made after this migration was already applied")

            try:
                run_migrations(conn=conn, migrations_dir=tmp_migrations_dir)
                check("editing an applied migration is detected on the next run", False, "no exception raised")
            except MigrationChecksumMismatchError:
                check("editing an applied migration is detected on the next run", True)
            conn.close()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)


def test_failed_migration_rolls_back_and_does_not_block_a_later_fix():
    with tempfile.TemporaryDirectory() as tmp_migrations_dir:
        with open(os.path.join(tmp_migrations_dir, "001_good.sql"), "w") as f:
            f.write("CREATE TABLE good_table (id INTEGER PRIMARY KEY);")
        broken_path = os.path.join(tmp_migrations_dir, "002_broken.sql")
        with open(broken_path, "w") as f:
            f.write("THIS IS NOT VALID SQL AT ALL;")

        db_path = _temp_db_path()
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row

            try:
                run_migrations(conn=conn, migrations_dir=tmp_migrations_dir)
                check("a broken migration raises MigrationApplyError", False, "no exception raised")
            except MigrationApplyError:
                check("a broken migration raises MigrationApplyError", True)

            check(
                "the good migration before it was still applied and recorded",
                conn.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = 1"
                ).fetchone()[0] == 1,
            )
            check(
                "the broken migration is NOT recorded as applied",
                conn.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = 2"
                ).fetchone()[0] == 0,
            )

            # Fix the broken migration and confirm a later run picks up
            # exactly the fix, without needing to touch migration 001 again.
            with open(broken_path, "w") as f:
                f.write("CREATE TABLE fixed_table (id INTEGER PRIMARY KEY);")

            applied = run_migrations(conn=conn, migrations_dir=tmp_migrations_dir)
            check("fixing the broken migration and rerunning applies just it", applied == [2], applied)
            check("the fixed table now exists", "fixed_table" in _table_names(conn))
            conn.close()
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)


def test_restart_simulation_reopening_connection_is_safe():
    """Simulates backend/main.py's startup hook firing again after a
    process restart: a brand new sqlite3 connection to the SAME database
    file, with run_migrations() called again."""
    db_path = _temp_db_path()
    try:
        conn1 = sqlite3.connect(db_path)
        conn1.row_factory = sqlite3.Row
        run_migrations(conn=conn1)
        conn1.close()

        # A fresh connection, as a fresh process would open on restart.
        conn2 = sqlite3.connect(db_path)
        conn2.row_factory = sqlite3.Row
        applied_after_restart = run_migrations(conn=conn2)
        check(
            "re-running migrations against the same file via a new connection (simulated restart) is a no-op",
            applied_after_restart == [],
        )
        check(
            "schema survived the simulated restart intact",
            {"inventory", "prescriptions", "panchakarma_resources", "corrections"} <= _table_names(conn2),
        )
        conn2.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_real_project_migrations_directory_bootstraps_cleanly():
    """Integration check against this project's ACTUAL migrations/
    directory (not a synthetic temp one) -- proves every shipped migration
    file applies cleanly together to a fresh database.

    Deliberately does NOT hardcode a migration count: this project's
    migrations/ directory grows with every Phase A milestone (it had 2
    files as of Migration Infrastructure, 3 as of A1, and will keep
    growing through A2-A5), so pinning a literal count here would make
    this test fail every time a new migration ships for a completely
    unrelated, non-broken reason. What this test actually guarantees --
    the versions are gap-free starting at 1, and every one of them applies
    cleanly to a fresh database -- would be weakened by hardcoding a count
    just as much as by deleting the check outright, so the count is
    derived from discover_migrations() itself instead."""
    check("this project's migrations/ directory exists", os.path.isdir(MIGRATIONS_DIR), MIGRATIONS_DIR)
    migrations = discover_migrations()
    expected_versions = [m.version for m in migrations]
    check("at least one real migration is discovered", len(migrations) >= 1, [m.filename for m in migrations])
    check(
        "discovered migration versions are gap-free starting at 1",
        expected_versions == list(range(1, len(expected_versions) + 1)),
        expected_versions,
    )

    db_path = _temp_db_path()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        applied = run_migrations(conn=conn)
        check(
            f"all {len(expected_versions)} real migrations apply to a fresh database with no errors",
            applied == expected_versions,
            applied,
        )
        check(
            "resulting schema has all 4 tables plus schema_migrations",
            {"inventory", "prescriptions", "panchakarma_resources", "corrections", "schema_migrations"}
            <= _table_names(conn),
        )
        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_upgrade_from_migration_002_to_003_adds_prescription_previews():
    """Regression test for the hard A1 Mac verification failure.

    Simulates exactly the "existing database sitting at an earlier
    migration" scenario the real bug turned out to be a variant of: a
    database that already has migrations 001 and 002 applied (real
    schema_migrations rows, not synthetic ones -- built by running the
    REAL migration files 001 and 002 against a fresh temp db) but has
    never seen migration 003. Proves that calling the real run_migrations()
    -- the ONLY sanctioned way this project ever changes schema -- takes
    that database the rest of the way to migration 003 cleanly: adds
    prescription_previews (immediately usable, not just present), leaves
    pre-existing data completely untouched, and does not re-apply 001/002.

    (The actual Mac incident was more severe than this -- schema_migrations
    didn't exist there AT ALL, meaning run_migrations() had never been
    invoked by any path, not even for 001/002. That total-bootstrap case is
    covered by test_bootstrap_from_empty_database above and by
    tests/test_preview_lifecycle.py's
    test_fresh_database_gets_migrated_by_real_app_startup, which exercises
    the real FastAPI startup event end-to-end. This test covers the
    specific 002->003 upgrade path the user asked to see proven.)
    """
    real_migrations = discover_migrations()
    check(
        "the real migrations/ directory has migration 003 to upgrade to",
        any(m.version == 3 for m in real_migrations),
        [m.filename for m in real_migrations],
    )
    migrations_up_to_002 = [m for m in real_migrations if m.version <= 2]

    db_path = _temp_db_path()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Build a database sitting at exactly migration 002 -- using a
        # synthetic migrations directory containing copies of ONLY the
        # real 001/002 files, so this is genuinely "the real schema as of
        # migration 002," not a hand-written approximation of it.
        with tempfile.TemporaryDirectory() as up_to_002_dir:
            for m in migrations_up_to_002:
                with open(os.path.join(up_to_002_dir, m.filename), "w") as f:
                    f.write(m.content)
            applied_to_002 = run_migrations(conn=conn, migrations_dir=up_to_002_dir)
            check(
                "database is bootstrapped to exactly migrations 001+002",
                applied_to_002 == [m.version for m in migrations_up_to_002],
                applied_to_002,
            )

        check(
            "prescription_previews does NOT exist yet at migration 002",
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='prescription_previews'"
            ).fetchone() is None,
        )

        # A real pre-existing row, matching the shape of the actual Mac
        # incident's data -- this must survive the 002 -> 003 upgrade
        # completely untouched (003 only ADDS a new table; it must never
        # touch inventory).
        conn.execute('''
            INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score)
            VALUES ('default_clinic', 'Ashwagandha', 'Withania somnifera', 4321, 'PRE-003-BATCH', 95)
        ''')
        conn.commit()

        # Now run the REAL run_migrations() against the REAL migrations/
        # directory (discovers every real migration currently on disk, not
        # just 001-003) -- 001/002 must be no-ops (already applied), and
        # every OTHER real migration beyond 002 should apply. Deliberately
        # derived from `real_migrations` (a real discover_migrations()
        # call, made at the top of this test) rather than hardcoded as
        # [3]: this project has since grown migrations 004/005 (Phase A,
        # A2), and a hardcoded [3] here would go stale in exactly the way
        # this file's OTHER migration-count assertions already did once
        # before this test was even added -- the thing that fix was
        # supposed to stop happening again.
        expected_beyond_002 = [m.version for m in real_migrations if m.version > 2]
        applied_to_003 = run_migrations(conn=conn)
        check(
            "upgrading from 002 applies every migration beyond it, not a re-application of 001/002",
            applied_to_003 == expected_beyond_002,
            applied_to_003,
        )

        check(
            "prescription_previews exists after upgrading to migration 003",
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='prescription_previews'"
            ).fetchone() is not None,
        )

        # Not just "the table exists" -- prove it's actually usable with
        # the exact INSERT shape backend/preview_store.py's create_preview()
        # uses, so this test would have caught the real incident.
        conn.execute(
            """
            INSERT INTO prescription_previews (
                preview_id, clinic_id, patient_name, patient_phone, patient_id,
                is_manual, ai_dosha_imbalance, ai_prescribed_herbs_json,
                status, created_by_session, created_at, expires_at
            ) VALUES ('test-preview-id', 'default_clinic', 'Upgrade Test Patient', NULL, NULL,
                      1, 'Vata', '[]', 'PENDING', NULL, '2026-01-01 00:00:00', '2026-01-01 00:30:00')
            """
        )
        conn.commit()
        check(
            "a real INSERT matching create_preview()'s exact shape succeeds against the upgraded table",
            conn.execute(
                "SELECT COUNT(*) FROM prescription_previews WHERE preview_id = 'test-preview-id'"
            ).fetchone()[0] == 1,
        )

        row = conn.execute("SELECT stock_grams, batch_code FROM inventory WHERE herb_name = 'Ashwagandha'").fetchone()
        check(
            "the pre-existing inventory row survives the 002 -> 003 upgrade completely untouched",
            row is not None and row["stock_grams"] == 4321 and row["batch_code"] == "PRE-003-BATCH",
            dict(row) if row else None,
        )

        schema_versions = sorted(
            r["version"] for r in conn.execute("SELECT version FROM schema_migrations")
        )
        expected_all_versions = [m.version for m in real_migrations]
        check(
            "schema_migrations records exactly every real migration version once -- no duplicates "
            "from the two run_migrations() calls",
            schema_versions == expected_all_versions,
            schema_versions,
        )
        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_migration_008_adds_login_throttle_fresh_bootstrap_and_upgrade():
    """A6.1 (Login Abuse Resistance) migration test -- covers both halves
    this task's spec explicitly asks for: "Verify upgrade from the current
    latest schema as well as fresh bootstrap."

    FRESH BOOTSTRAP: running the real migrations/ directory against a
    brand-new empty database produces a login_throttle table with exactly
    the columns migrations/008_login_throttle.sql defines.

    UPGRADE: mirrors test_upgrade_from_migration_002_to_003_adds_
    prescription_previews's exact pattern (a synthetic migrations dir
    containing copies of only the REAL 001-007 files, so this is genuinely
    "the real schema as of migration 007," not a hand-written
    approximation) -- but additionally inserts a REAL users row and a REAL
    sessions row before upgrading to 008, and asserts both are completely
    untouched afterward (008 only ADDS a new table; it must never touch
    users or sessions)."""
    real_migrations = discover_migrations()
    check(
        "the real migrations/ directory has migration 8 to upgrade to",
        any(m.version == 8 for m in real_migrations),
        [m.filename for m in real_migrations],
    )

    # --- FRESH BOOTSTRAP ---
    fresh_db_path = _temp_db_path()
    try:
        fresh_conn = sqlite3.connect(fresh_db_path)
        fresh_conn.row_factory = sqlite3.Row
        applied_fresh = run_migrations(conn=fresh_conn)
        check(
            "a fresh bootstrap against the real migrations/ directory applies every real migration, 008 included",
            applied_fresh == [m.version for m in real_migrations],
            applied_fresh,
        )
        check("login_throttle exists after a fresh bootstrap", "login_throttle" in _table_names(fresh_conn), _table_names(fresh_conn))
        check(
            "login_throttle has exactly the columns migration 008 defines",
            _columns(fresh_conn, "login_throttle") == {"user_id", "consecutive_failures", "locked_until"},
            _columns(fresh_conn, "login_throttle"),
        )
        fresh_conn.close()
    finally:
        if os.path.exists(fresh_db_path):
            os.remove(fresh_db_path)

    # --- UPGRADE FROM 007, WITH REAL PRE-EXISTING DATA ---
    migrations_up_to_007 = [m for m in real_migrations if m.version <= 7]
    db_path = _temp_db_path()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        with tempfile.TemporaryDirectory() as up_to_007_dir:
            for m in migrations_up_to_007:
                with open(os.path.join(up_to_007_dir, m.filename), "w") as f:
                    f.write(m.content)
            applied_to_007 = run_migrations(conn=conn, migrations_dir=up_to_007_dir)
            check(
                "database is bootstrapped to exactly migrations 001-007",
                applied_to_007 == [m.version for m in migrations_up_to_007],
                applied_to_007,
            )

        check(
            "login_throttle does NOT exist yet at migration 007",
            conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='login_throttle'").fetchone() is None,
        )

        # Real pre-existing data that migration 008 must leave completely
        # untouched -- a users row and a sessions row, matching the actual
        # shape backend/auth_service.py's create_user()/create_session()
        # write.
        conn.execute(
            """
            INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at)
            VALUES ('migration_008_test_user', 'a-real-looking-argon2id-hash-placeholder', 'default_clinic', 'DOCTOR', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
            """
        )
        user_id = conn.execute("SELECT user_id FROM users WHERE username = 'migration_008_test_user'").fetchone()["user_id"]
        conn.execute(
            """
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at, revoked_at, last_seen_at)
            VALUES (?, ?, '2026-01-01 00:00:00', '2026-01-01 12:00:00', NULL, NULL)
            """,
            ("deadbeef" * 8, user_id),
        )
        conn.commit()

        expected_beyond_007 = [m.version for m in real_migrations if m.version > 7]
        applied_to_008 = run_migrations(conn=conn)
        check(
            "upgrading from 007 applies every migration beyond it (008, and any later than that this "
            "project has since grown), not a re-application of 001-007",
            applied_to_008 == expected_beyond_007,
            applied_to_008,
        )

        check("login_throttle exists after upgrading to 008", "login_throttle" in _table_names(conn), _table_names(conn))
        check(
            "login_throttle has exactly the columns migration 008 defines",
            _columns(conn, "login_throttle") == {"user_id", "consecutive_failures", "locked_until"},
            _columns(conn, "login_throttle"),
        )

        user_row = conn.execute("SELECT * FROM users WHERE username = 'migration_008_test_user'").fetchone()
        check(
            "the pre-existing users row survives the 007 -> 008 upgrade completely untouched",
            user_row is not None and user_row["password_hash"] == "a-real-looking-argon2id-hash-placeholder" and user_row["clinic_id"] == "default_clinic",
            dict(user_row) if user_row else None,
        )
        session_row = conn.execute("SELECT * FROM sessions WHERE user_id = ?", (user_id,)).fetchone()
        check(
            "the pre-existing sessions row survives the 007 -> 008 upgrade completely untouched",
            session_row is not None and session_row["token_hash"] == "deadbeef" * 8,
            dict(session_row) if session_row else None,
        )

        # And the new table is actually usable with the exact shape
        # backend/auth_service.py's throttle helpers write.
        conn.execute(
            "INSERT INTO login_throttle (user_id, consecutive_failures, locked_until) VALUES (?, 3, NULL)",
            (user_id,),
        )
        conn.commit()
        check(
            "a real INSERT matching the throttle helpers' exact shape succeeds against the upgraded table",
            conn.execute("SELECT consecutive_failures FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()["consecutive_failures"] == 3,
        )

        conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


def test_concurrent_startups_do_not_race():
    """Simulates two uvicorn workers both starting up and calling
    run_migrations() against the same database file at effectively the
    same moment -- the adversarial "what happens with two workers" case.
    Each thread gets its OWN sqlite3 connection (a real second connection,
    not a shared one -- sharing one connection across threads would not
    test anything about SQLite's actual locking).

    Like test_real_project_migrations_directory_bootstraps_cleanly, this
    runs against the real migrations/ directory rather than a synthetic
    one (the real directory is what actually needs to survive a two-worker
    race), so the expected version list is derived from
    discover_migrations() rather than hardcoded -- this project's
    migration count grows with every Phase A milestone."""
    import threading

    expected_versions = [m.version for m in discover_migrations()]
    db_path = _temp_db_path()
    results = {}
    errors = {}
    start_barrier = threading.Barrier(2)

    def worker(name: str):
        conn = sqlite3.connect(db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            start_barrier.wait()  # maximize the chance both threads actually overlap
            results[name] = run_migrations(conn=conn)
        except Exception as e:
            errors[name] = e
        finally:
            conn.close()

    try:
        t1 = threading.Thread(target=worker, args=("worker_a",))
        t2 = threading.Thread(target=worker, args=("worker_b",))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        check("neither concurrent worker raised an exception", errors == {}, errors)

        combined_applied = sorted((results.get("worker_a") or []) + (results.get("worker_b") or []))
        check(
            "each migration was applied exactly once across both workers combined, not duplicated",
            combined_applied == expected_versions,
            f"worker_a={results.get('worker_a')} worker_b={results.get('worker_b')} expected={expected_versions}",
        )

        verify_conn = sqlite3.connect(db_path)
        verify_conn.row_factory = sqlite3.Row
        check(
            "schema_migrations has exactly one row per migration after the race, not two",
            verify_conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == len(expected_versions),
        )
        verify_conn.close()
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)


_ALL_TESTS = [
    test_bootstrap_from_empty_database,
    test_rerun_is_idempotent,
    test_upgrade_of_pre_migration_database_preserves_data,
    test_duplicate_version_numbers_are_rejected,
    test_tampering_with_an_applied_migration_is_detected,
    test_failed_migration_rolls_back_and_does_not_block_a_later_fix,
    test_restart_simulation_reopening_connection_is_safe,
    test_real_project_migrations_directory_bootstraps_cleanly,
    test_upgrade_from_migration_002_to_003_adds_prescription_previews,
    test_migration_008_adds_login_throttle_fresh_bootstrap_and_upgrade,
    test_concurrent_startups_do_not_race,
]


if __name__ == "__main__":
    # check() now raises AssertionError on a failed assertion (see its own
    # docstring, and tests/test_rbac.py's module docstring "PYTEST
    # COMPATIBILITY") -- so each test function is called through this
    # loop, which catches that raise per-function and moves on to the
    # next one, rather than a flat list of bare calls that would abort
    # the whole script at the FIRST failing assertion anywhere. This
    # keeps direct execution's long-standing contract ("run every test
    # function, print a full tally at the end") intact while making every
    # individual check() a real, hard assertion under pytest too.
    _function_failures = []
    for _test_fn in _ALL_TESTS:
        try:
            _test_fn()
        except AssertionError as e:
            _function_failures.append(_test_fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {_test_fn.__name__} ({e})")

    print(f"\n--- {_passed} passed, {_failed} failed ---")
    if _function_failures:
        print(f"--- {len(_function_failures)} test function(s) had a failing assertion: {', '.join(_function_failures)} ---")
    if _failed:
        sys.exit(1)
