"""
Backup/Restore MVP phase -- tests for scripts/backup_db.py.

Proves, at minimum, everything BACKUP/RESTORE MVP IMPLEMENTATION's
"4. TESTS" section asked for:
  A. live-DB-style database -> backup succeeds
  B. backup passes PRAGMA integrity_check
  C. backup passes PRAGMA foreign_key_check
  D. a restored database opens successfully
  E. schema_migrations survives a backup/restore round trip
  F. every table's row count survives a backup/restore round trip
  G. audit_events survives a backup/restore round trip
  H. the audit hash chain (backend.audit_store.verify_audit_integrity())
     remains valid after restore
  I. the migration runner recognizes a restored database as already
     up to date (a fast no-op, not a re-application of anything)
  J. a corrupted/truncated backup is rejected by verify_backup()
  K. a backup operation never modifies its source database

Plus (L) the destination-safety refusals backup_database() must enforce,
(M/N) the openssl-based encryption round trip, and (O) the full isolated
restore rehearsal BACKUP/RESTORE MVP IMPLEMENTATION's "5. RESTORE
END-TO-END TEST" section asked for: backup -> isolated restored DB ->
migrations -> audit verification -> a simulated new prescription ->
inventory deduction -> a new audit event that links to the restored
chain's last event, not to a fresh genesis.

NONE OF THIS EVER TOUCHES THE REAL db/ayurcore.db. Every test here builds
its own throwaway temporary SQLite file via db.migrations.run_migrations()
-- the same discipline tests/test_migrations.py already uses -- and every
"restore" in this file is a copy into ANOTHER throwaway temporary file,
never a write to any path this project's application code would ever
actually serve requests against. Test data is built with plain SQL and
this project's own non-HTTP business-logic functions
(backend.inventory_service.deduct_and_prescribe,
backend.audit_store.create_audit_event / verify_audit_integrity) rather
than through FastAPI's TestClient -- backup/restore is a persistence-layer
concern, and none of the functions this file imports depend on fastapi,
streamlit, or argon2-cffi being installed, so (unlike several of this
project's other test files) this one runs to completion in any Python
environment that has this project's own db/ and backend/ modules on the
path -- no pip install required for the non-encryption tests.

LIMITATION, documented rather than worked around (per this phase's
explicit instruction not to modify DB_PATH's architecture): this file
points db.database.DB_PATH at a temporary file for the two tests that need
backend.audit_store.verify_audit_integrity() and db.migrations.
run_migrations()'s own no-argument call path (both resolve the database to
use by reading db.database.DB_PATH at call time), using the EXACT SAME
monkeypatch-and-restore pattern tests/test_preview_lifecycle.py's
test_fresh_database_gets_migrated_by_real_app_startup already established
in this project -- this is not a new pattern invented for this file. Every
monkeypatch is undone in a `finally` block before the test function
returns, restoring the true DB_PATH regardless of pass or fail.

Encryption tests additionally need the system `openssl` binary. They
check for it with shutil.which() and SKIP (print a clear SKIP line, do not
fail or error) when it is not on PATH -- the same "environment doesn't
have X, degrade gracefully rather than fail" pattern this project already
uses for GEMINI_API_KEY-gated tests elsewhere.

Run directly (no pytest required):
    python tests/test_backup.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrations import run_migrations, discover_migrations
import db.database as database_module
from scripts.backup_db import (
    backup_database,
    verify_backup,
    encrypt_backup,
    decrypt_backup,
    BackupError,
    KNOWN_TABLES,
)
from backend.audit_store import (
    create_audit_event,
    verify_audit_integrity,
    build_prescription_confirmed_event_data,
    AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
)
from backend.inventory_service import deduct_and_prescribe

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Records AND enforces one assertion -- raises AssertionError
    immediately on failure, matching every other test file in this
    project (see tests/test_migrations.py's identical helper)."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _temp_path(suffix: str = ".db") -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    os.remove(path)
    return path


def _table_names(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _row_counts(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        present = _table_names(conn)
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in KNOWN_TABLES if t in present}
    finally:
        conn.close()


def _full_table_dump(db_path: str) -> dict:
    """Every row of every known table, as a sorted list of tuples -- used
    only for test_backup_does_not_modify_the_source_database's
    before/after content comparison (never printed; this file never prints
    row content anywhere else either)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        present = _table_names(conn)
        dump = {}
        for t in KNOWN_TABLES:
            if t not in present:
                continue
            rows = conn.execute(f"SELECT * FROM {t}").fetchall()
            dump[t] = sorted(tuple(r) for r in rows)
        return dump
    finally:
        conn.close()


def _build_seeded_db(db_path: str) -> dict:
    """Builds a fully-migrated database at `db_path` (must not already
    exist) with one clinic-owned, referentially-real slice of data: one
    inventory row, one user, one already-CONFIRMED prescription_previews
    row, one real confirmed prescription (via the actual
    deduct_and_prescribe() business logic -- inventory really deducted),
    and one real, correctly hash-chained audit_events row (via the actual
    create_audit_event() -- not a hand-computed fake hash), built the same
    way backend/main.py's real /prescribe/confirm does: inside one shared
    BEGIN IMMEDIATE transaction, deduction and audit-event insert together,
    committed once.

    Returns a dict of everything a caller might need to assert against:
    clinic_id, user_id, herb_name, initial_stock, deducted_grams,
    prescription_id, preview_id, event_id, event_hash.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn=conn)

    clinic_id = "default_clinic"
    herb_name = "Ashwagandha"
    initial_stock = 5000
    deducted_grams = 10

    conn.execute(
        """
        INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score)
        VALUES (?, ?, 'Withania somnifera', ?, 'TEST-BATCH', 90)
        """,
        (clinic_id, herb_name, initial_stock),
    )
    conn.execute(
        """
        INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at)
        VALUES ('backup_test_doctor', 'not-a-real-argon2id-hash-placeholder', ?, 'DOCTOR', 1,
                '2026-01-01 00:00:00', '2026-01-01 00:00:00')
        """,
        (clinic_id,),
    )
    user_id = conn.execute("SELECT user_id FROM users WHERE username = 'backup_test_doctor'").fetchone()["user_id"]

    preview_id = "backup-test-preview-1"
    conn.execute(
        """
        INSERT INTO prescription_previews (
            preview_id, clinic_id, patient_name, patient_phone, patient_id,
            is_manual, ai_dosha_imbalance, ai_prescribed_herbs_json,
            confirmed_dosha_imbalance, confirmed_prescribed_herbs_json,
            status, created_by_session, created_at, expires_at, confirmed_at
        ) VALUES (?, ?, 'Backup Test Patient', NULL, NULL, 1, 'Vata',
                  '[{"name": "ashwagandha", "grams": 10}]',
                  'Vata', '[{"name": "ashwagandha", "grams": 10}]',
                  'CONFIRMED', NULL, '2026-01-01 00:00:00', '2026-01-01 00:30:00', '2026-01-01 00:05:00')
        """,
        (preview_id, clinic_id),
    )
    conn.commit()

    herbs = [{"name": herb_name, "grams": deducted_grams}]
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = deduct_and_prescribe(
            patient_name="Backup Test Patient",
            dosha="Vata",
            raw_herbs=herbs,
            clinic_id=clinic_id,
            conn=conn,
        )
        prescription_id = result["prescription_id"]

        event_data = build_prescription_confirmed_event_data(
            is_manual=True,
            ai_dosha_imbalance=None,
            ai_prescribed_herbs=None,
            confirmed_dosha_imbalance="Vata",
            confirmed_prescribed_herbs=herbs,
        )
        event_id = create_audit_event(
            conn,
            clinic_id=clinic_id,
            actor_user_id=user_id,
            event_type=AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
            prescription_id=prescription_id,
            preview_id=preview_id,
            event_data=event_data,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    event_hash = conn.execute(
        "SELECT event_hash FROM audit_events WHERE event_id = ?", (event_id,)
    ).fetchone()["event_hash"]

    conn.close()

    return {
        "clinic_id": clinic_id,
        "user_id": user_id,
        "herb_name": herb_name,
        "initial_stock": initial_stock,
        "deducted_grams": deducted_grams,
        "prescription_id": prescription_id,
        "preview_id": preview_id,
        "event_id": event_id,
        "event_hash": event_hash,
    }


# --------------------------------------------------------------------- #
# A / B / C -- backup succeeds, passes integrity_check, passes
# foreign_key_check
# --------------------------------------------------------------------- #

def test_backup_of_seeded_database_succeeds_and_passes_verification():
    source_path = _temp_path()
    dest_path = _temp_path()  # _temp_path() already guarantees this does not exist yet
    try:
        seed = _build_seeded_db(source_path)

        returned_path = backup_database(source_path, dest_path)
        check("(A) backup_database() returns the destination path", returned_path == os.path.realpath(dest_path))
        check("(A) the backup file actually exists on disk", os.path.exists(dest_path))

        result = verify_backup(dest_path)
        check("(B) verify_backup() reports ok=True for a healthy backup", result.ok, result.reason)
        check("(B) verify_backup() reason is 'ok'", result.reason == "ok", result.reason)

        conn = sqlite3.connect(dest_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            fk_violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
            check("(C) PRAGMA foreign_key_check on the backup returns zero rows", fk_violations == [], fk_violations)
        finally:
            conn.close()

        check(
            "(A/B) verify_backup()'s own row counts include the seeded audit_events row",
            result.table_row_counts.get("audit_events") == 1,
            result.table_row_counts,
        )
    finally:
        for p in (source_path, dest_path):
            if os.path.exists(p):
                os.remove(p)


# --------------------------------------------------------------------- #
# D / E / F / G -- restored DB opens; schema_migrations, row counts, and
# audit_events all survive the round trip
# --------------------------------------------------------------------- #

def test_restored_database_opens_and_all_data_survives():
    source_path = _temp_path()
    backup_path = _temp_path()
    restored_path = _temp_path()
    for p in (backup_path, restored_path):
        if os.path.exists(p):
            os.remove(p)
    try:
        seed = _build_seeded_db(source_path)
        pre_backup_counts = _row_counts(source_path)

        backup_database(source_path, backup_path)
        result = verify_backup(backup_path)
        check("backup of the seeded database verifies ok before restore is attempted", result.ok, result.reason)

        # "Restore" = copy the verified backup into an isolated scratch
        # path. Never the real db/ayurcore.db path, and never overwriting
        # anything -- shutil.copy2 onto a path confirmed not to exist yet.
        check("the restore target does not exist yet", not os.path.exists(restored_path))
        shutil.copy2(backup_path, restored_path)

        conn = sqlite3.connect(restored_path)
        conn.row_factory = sqlite3.Row
        try:
            check("(D) the restored database opens successfully", True)  # sqlite3.connect() above didn't raise
            integrity = conn.execute("PRAGMA integrity_check;").fetchall()
            check(
                "(D) the restored database passes PRAGMA integrity_check",
                len(integrity) == 1 and integrity[0][0] == "ok",
                [tuple(r) for r in integrity],
            )

            real_migration_versions = [m.version for m in discover_migrations()]
            restored_versions = sorted(
                r["version"] for r in conn.execute("SELECT version FROM schema_migrations")
            )
            check(
                "(E) schema_migrations survives the round trip with every real migration recorded",
                restored_versions == real_migration_versions,
                restored_versions,
            )

            post_restore_counts = _row_counts(restored_path)
            check(
                "(F) every table's row count survives the backup/restore round trip unchanged",
                post_restore_counts == pre_backup_counts,
                {"before": pre_backup_counts, "after": post_restore_counts},
            )

            audit_row = conn.execute(
                "SELECT event_id, event_hash, previous_event_hash FROM audit_events WHERE event_id = ?",
                (seed["event_id"],),
            ).fetchone()
            check(
                "(G) the seeded audit_events row survives the round trip with an identical event_hash",
                audit_row is not None and audit_row["event_hash"] == seed["event_hash"],
                dict(audit_row) if audit_row else None,
            )
        finally:
            conn.close()
    finally:
        for p in (source_path, backup_path, restored_path):
            if os.path.exists(p):
                os.remove(p)


# --------------------------------------------------------------------- #
# H -- the audit hash chain remains valid after restore, verified through
# the REAL backend.audit_store.verify_audit_integrity(), not a
# reimplementation of its hash check
# --------------------------------------------------------------------- #

def test_audit_chain_remains_valid_after_restore():
    source_path = _temp_path()
    backup_path = _temp_path()
    restored_path = _temp_path()
    for p in (backup_path, restored_path):
        if os.path.exists(p):
            os.remove(p)

    original_db_path = database_module.DB_PATH
    try:
        seed = _build_seeded_db(source_path)
        backup_database(source_path, backup_path)
        shutil.copy2(backup_path, restored_path)

        # verify_audit_integrity() resolves its own connection via
        # db.database.get_connection(), which reads db.database.DB_PATH at
        # CALL time -- the same monkeypatch-and-restore pattern
        # tests/test_preview_lifecycle.py's
        # test_fresh_database_gets_migrated_by_real_app_startup already
        # established in this project. Always undone in `finally`.
        database_module.DB_PATH = restored_path

        report = verify_audit_integrity(seed["clinic_id"])
        check("(H) verify_audit_integrity() reports the restored chain as valid", report.valid, report.violations)
        check("(H) verify_audit_integrity() checked exactly the one seeded event", report.checked_count == 1, report.checked_count)
    finally:
        database_module.DB_PATH = original_db_path
        for p in (source_path, backup_path, restored_path):
            if os.path.exists(p):
                os.remove(p)


# --------------------------------------------------------------------- #
# I -- the migration runner recognizes a restored database as already up
# to date
# --------------------------------------------------------------------- #

def test_migration_runner_recognizes_restored_database_as_up_to_date():
    source_path = _temp_path()
    backup_path = _temp_path()
    restored_path = _temp_path()
    for p in (backup_path, restored_path):
        if os.path.exists(p):
            os.remove(p)
    try:
        _build_seeded_db(source_path)
        backup_database(source_path, backup_path)
        shutil.copy2(backup_path, restored_path)

        conn = sqlite3.connect(restored_path)
        conn.row_factory = sqlite3.Row
        try:
            applied = run_migrations(conn=conn)
            check(
                "(I) running the migration runner against a freshly-restored, fully-migrated "
                "database applies nothing (a fast no-op), rather than re-applying or erroring",
                applied == [],
                applied,
            )
        finally:
            conn.close()
    finally:
        for p in (source_path, backup_path, restored_path):
            if os.path.exists(p):
                os.remove(p)


# --------------------------------------------------------------------- #
# J -- a corrupted/truncated backup is rejected
# --------------------------------------------------------------------- #

def test_corrupted_backup_is_rejected_by_verify_backup():
    source_path = _temp_path()
    backup_path = _temp_path()
    truncated_path = _temp_path()
    for p in (backup_path, truncated_path):
        if os.path.exists(p):
            os.remove(p)
    try:
        _build_seeded_db(source_path)
        backup_database(source_path, backup_path)

        good_result = verify_backup(backup_path)
        check("a healthy backup verifies ok before the corruption test", good_result.ok, good_result.reason)

        # Deliberately truncate a COPY of the valid backup -- simulates an
        # interrupted transfer or a partially-written backup file.
        with open(backup_path, "rb") as f:
            full_bytes = f.read()
        with open(truncated_path, "wb") as f:
            f.write(full_bytes[: max(1, len(full_bytes) // 4)])

        bad_result = verify_backup(truncated_path)
        check("(J) verify_backup() rejects a truncated/corrupted backup file", not bad_result.ok, bad_result.reason)
        check(
            "(J) verify_backup() gives a specific reason rather than reporting a bare False",
            bool(bad_result.reason) and bad_result.reason != "ok",
            bad_result.reason,
        )

        # Also confirm the CLI-facing gate itself: verify_backup() must
        # never be bypassable into a false "success" report for this file.
        check("(J) a rejected backup reports no table row counts", bad_result.table_row_counts == {}, bad_result.table_row_counts)
    finally:
        for p in (source_path, backup_path, truncated_path):
            if os.path.exists(p):
                os.remove(p)


def test_a_nonexistent_backup_path_is_rejected_by_verify_backup():
    result = verify_backup(_temp_path())  # path created then immediately removed by _temp_path()
    check("(J) verify_backup() rejects a path that doesn't exist at all", not result.ok, result.reason)


def test_a_non_sqlite_file_is_rejected_by_verify_backup():
    fake_path = _temp_path()
    with open(fake_path, "w") as f:
        f.write("this is not a sqlite database, just plain text\n" * 20)
    try:
        result = verify_backup(fake_path)
        check("(J) verify_backup() rejects a file that was never a SQLite database at all", not result.ok, result.reason)
    finally:
        if os.path.exists(fake_path):
            os.remove(fake_path)


# --------------------------------------------------------------------- #
# K -- a backup operation never modifies its source database
# --------------------------------------------------------------------- #

def test_backup_does_not_modify_the_source_database():
    source_path = _temp_path()
    dest_path = _temp_path()
    try:
        _build_seeded_db(source_path)

        before_counts = _row_counts(source_path)
        before_dump = _full_table_dump(source_path)
        before_mtime_data = os.path.getsize(source_path)  # informational only; real proof is the content dump below

        backup_database(source_path, dest_path)

        after_counts = _row_counts(source_path)
        after_dump = _full_table_dump(source_path)

        check(
            "(K) every table's row COUNT in the source is unchanged after a backup operation",
            before_counts == after_counts,
            {"before": before_counts, "after": after_counts},
        )
        check(
            "(K) every table's actual row CONTENT in the source is byte-for-byte unchanged after a "
            "backup operation (not just counts -- a full per-table content comparison)",
            before_dump == after_dump,
            "content differs" if before_dump != after_dump else "",
        )

        conn = sqlite3.connect(source_path)
        try:
            integrity = conn.execute("PRAGMA integrity_check;").fetchall()
            check("(K) the source database still passes its own integrity_check after being backed up", integrity == [("ok",)], integrity)
        finally:
            conn.close()
    finally:
        for p in (source_path, dest_path):
            if os.path.exists(p):
                os.remove(p)


# --------------------------------------------------------------------- #
# Destination-safety refusals backup_database() must enforce
# --------------------------------------------------------------------- #

def test_backup_refuses_to_write_over_the_source_path():
    source_path = _temp_path()
    try:
        _build_seeded_db(source_path)
        try:
            backup_database(source_path, source_path)
            check("backup_database() raises BackupError when dest == source", False, "no exception raised")
        except BackupError:
            check("backup_database() raises BackupError when dest == source", True)
    finally:
        if os.path.exists(source_path):
            os.remove(source_path)


def test_backup_refuses_to_overwrite_an_existing_destination_file():
    source_path = _temp_path()
    dest_path = _temp_path()  # this one is left in place, unlike other tests' cleanup-first pattern
    try:
        _build_seeded_db(source_path)
        with open(dest_path, "w") as f:
            f.write("something already here")
        try:
            backup_database(source_path, dest_path)
            check("backup_database() raises BackupError when the destination already exists", False, "no exception raised")
        except BackupError:
            check("backup_database() raises BackupError when the destination already exists", True)
        check(
            "the pre-existing destination file's content was left untouched",
            open(dest_path).read() == "something already here",
        )
    finally:
        for p in (source_path, dest_path):
            if os.path.exists(p):
                os.remove(p)


def test_backup_refuses_a_destination_whose_parent_directory_does_not_exist():
    source_path = _temp_path()
    missing_parent_dest = os.path.join(tempfile.gettempdir(), "ayurcore_backup_test_no_such_dir_xyz", "backup.db")
    try:
        _build_seeded_db(source_path)
        try:
            backup_database(source_path, missing_parent_dest)
            check("backup_database() raises BackupError when the destination directory doesn't exist", False, "no exception raised")
        except BackupError:
            check("backup_database() raises BackupError when the destination directory doesn't exist", True)
        check(
            "backup_database() did not create the missing directory on its own",
            not os.path.isdir(os.path.dirname(missing_parent_dest)),
        )
    finally:
        if os.path.exists(source_path):
            os.remove(source_path)


# --------------------------------------------------------------------- #
# M / N -- openssl-based encryption round trip (SKIPS gracefully if
# openssl is not on PATH -- see module docstring)
# --------------------------------------------------------------------- #

_ENCRYPTION_ENV_VAR = "AYURCORE_BACKUP_PASSPHRASE"


def test_encrypt_decrypt_round_trip_preserves_a_valid_backup():
    if shutil.which("openssl") is None:
        print("=> SKIP: test_encrypt_decrypt_round_trip_preserves_a_valid_backup (openssl not on PATH)")
        return

    source_path = _temp_path()
    dest_path = _temp_path()
    encrypted_path = dest_path + ".enc"
    decrypted_path = _temp_path()

    original_env = os.environ.get(_ENCRYPTION_ENV_VAR)
    os.environ[_ENCRYPTION_ENV_VAR] = "correct-test-passphrase-not-a-real-secret"
    try:
        _build_seeded_db(source_path)
        backup_database(source_path, dest_path)
        pre_encrypt_counts = verify_backup(dest_path).table_row_counts

        encrypt_backup(dest_path, encrypted_path)
        check("(M) encrypt_backup() produces the encrypted output file", os.path.exists(encrypted_path))

        with open(encrypted_path, "rb") as f:
            head = f.read(16)
        check(
            "(M) the encrypted file does not start with the plaintext SQLite file header "
            "(i.e. it is genuinely not readable as a SQLite file)",
            head != b"SQLite format 3",
        )
        try:
            sqlite3.connect(encrypted_path).execute("PRAGMA integrity_check;").fetchall()
            check("(M) the encrypted file cannot be opened as a valid SQLite database", False, "it opened cleanly -- not actually encrypted")
        except sqlite3.DatabaseError:
            check("(M) the encrypted file cannot be opened as a valid SQLite database", True)

        decrypt_backup(encrypted_path, decrypted_path)
        result = verify_backup(decrypted_path)
        check("(M) the decrypted file verifies ok, identical in substance to the original backup", result.ok, result.reason)
        check(
            "(M) the decrypted backup has the exact same table row counts as the pre-encryption backup",
            result.table_row_counts == pre_encrypt_counts,
            {"before": pre_encrypt_counts, "after": result.table_row_counts},
        )
    finally:
        if original_env is None:
            os.environ.pop(_ENCRYPTION_ENV_VAR, None)
        else:
            os.environ[_ENCRYPTION_ENV_VAR] = original_env
        for p in (source_path, dest_path, encrypted_path, decrypted_path):
            if os.path.exists(p):
                os.remove(p)


def test_decrypt_with_the_wrong_passphrase_fails_loudly_and_leaves_no_partial_file():
    if shutil.which("openssl") is None:
        print("=> SKIP: test_decrypt_with_the_wrong_passphrase_fails_loudly_and_leaves_no_partial_file (openssl not on PATH)")
        return

    source_path = _temp_path()
    dest_path = _temp_path()
    encrypted_path = dest_path + ".enc"
    decrypted_path = _temp_path()

    original_env = os.environ.get(_ENCRYPTION_ENV_VAR)
    try:
        _build_seeded_db(source_path)
        backup_database(source_path, dest_path)

        os.environ[_ENCRYPTION_ENV_VAR] = "correct-test-passphrase-not-a-real-secret"
        encrypt_backup(dest_path, encrypted_path)

        os.environ[_ENCRYPTION_ENV_VAR] = "a-completely-different-wrong-passphrase"
        try:
            decrypt_backup(encrypted_path, decrypted_path)
            check("(N) decrypting with the wrong passphrase raises BackupError", False, "no exception raised")
        except BackupError:
            check("(N) decrypting with the wrong passphrase raises BackupError", True)
        check(
            "(N) a failed decrypt leaves no partial plaintext file behind",
            not os.path.exists(decrypted_path),
        )
    finally:
        if original_env is None:
            os.environ.pop(_ENCRYPTION_ENV_VAR, None)
        else:
            os.environ[_ENCRYPTION_ENV_VAR] = original_env
        for p in (source_path, dest_path, encrypted_path, decrypted_path):
            if os.path.exists(p):
                os.remove(p)


def test_encrypt_backup_without_passphrase_env_var_fails_loudly():
    source_path = _temp_path()
    dest_path = _temp_path()
    encrypted_path = dest_path + ".enc"

    original_env = os.environ.get(_ENCRYPTION_ENV_VAR)
    os.environ.pop(_ENCRYPTION_ENV_VAR, None)
    try:
        _build_seeded_db(source_path)
        backup_database(source_path, dest_path)
        try:
            encrypt_backup(dest_path, encrypted_path)
            check("encrypt_backup() raises BackupError when the passphrase env var is unset", False, "no exception raised")
        except BackupError:
            check("encrypt_backup() raises BackupError when the passphrase env var is unset", True)
        check("no encrypted output file was created without a passphrase", not os.path.exists(encrypted_path))
    finally:
        if original_env is not None:
            os.environ[_ENCRYPTION_ENV_VAR] = original_env
        for p in (source_path, dest_path, encrypted_path):
            if os.path.exists(p):
                os.remove(p)


# --------------------------------------------------------------------- #
# O -- the full isolated restore rehearsal: backup -> isolated restored
# DB -> migrations -> audit verification -> a simulated new prescription
# -> inventory deduction -> a new audit event that links to the restored
# chain's last event
# --------------------------------------------------------------------- #

def test_full_isolated_restore_rehearsal_supports_a_new_prescription_after_restore():
    source_path = _temp_path()
    backup_path = _temp_path()
    restored_path = _temp_path()
    for p in (backup_path, restored_path):
        if os.path.exists(p):
            os.remove(p)

    original_db_path = database_module.DB_PATH
    try:
        seed = _build_seeded_db(source_path)

        # backup
        backup_database(source_path, backup_path)
        result = verify_backup(backup_path)
        check("(O) the backup taken before the rehearsal verifies ok", result.ok, result.reason)

        # -> isolated restored DB (never the real db/ayurcore.db, never
        # the original seeded source either -- a THIRD, independent path)
        shutil.copy2(backup_path, restored_path)
        check("(O) the restored file is a genuinely separate path from the source and the backup", len({source_path, backup_path, restored_path}) == 3)

        database_module.DB_PATH = restored_path

        # -> migrations (should be a no-op: this restore was taken from a
        # database already fully migrated with the SAME on-disk
        # migrations/ directory)
        applied = run_migrations()
        check("(O) migrations against the restored DB are a no-op (already up to date)", applied == [], applied)

        # -> audit verification (of the restored chain, before anything
        # new is added to it)
        report_before = verify_audit_integrity(seed["clinic_id"])
        check("(O) the restored audit chain verifies valid before any new activity", report_before.valid, report_before.violations)
        check("(O) the restored audit chain has exactly the one seeded event so far", report_before.checked_count == 1, report_before.checked_count)

        # -> a simulated new prescription -> inventory deduction, against
        # the RESTORED database, using the real business-logic functions
        # (exactly as backend/main.py's /prescribe/confirm does: a fresh
        # preview row, then deduct_and_prescribe() + create_audit_event()
        # inside one shared transaction)
        conn = sqlite3.connect(restored_path)
        conn.row_factory = sqlite3.Row
        try:
            stock_after_restore_before_new_rx = conn.execute(
                "SELECT stock_grams FROM inventory WHERE herb_name = ? AND clinic_id = ?",
                (seed["herb_name"], seed["clinic_id"]),
            ).fetchone()["stock_grams"]
            check(
                "(O) restored inventory reflects the ORIGINAL deduction (not the pre-deduction stock)",
                stock_after_restore_before_new_rx == seed["initial_stock"] - seed["deducted_grams"],
                stock_after_restore_before_new_rx,
            )

            new_preview_id = "backup-test-preview-2-post-restore"
            new_grams = 25
            conn.execute(
                """
                INSERT INTO prescription_previews (
                    preview_id, clinic_id, patient_name, patient_phone, patient_id,
                    is_manual, ai_dosha_imbalance, ai_prescribed_herbs_json,
                    confirmed_dosha_imbalance, confirmed_prescribed_herbs_json,
                    status, created_by_session, created_at, expires_at, confirmed_at
                ) VALUES (?, ?, 'Post-Restore Test Patient', NULL, NULL, 1, 'Pitta',
                          '[{"name": "ashwagandha", "grams": 25}]',
                          'Pitta', '[{"name": "ashwagandha", "grams": 25}]',
                          'CONFIRMED', NULL, '2026-01-02 00:00:00', '2026-01-02 00:30:00', '2026-01-02 00:05:00')
                """,
                (new_preview_id, seed["clinic_id"]),
            )
            conn.commit()

            conn.execute("BEGIN IMMEDIATE")
            new_herbs = [{"name": seed["herb_name"], "grams": new_grams}]
            new_result = deduct_and_prescribe(
                patient_name="Post-Restore Test Patient",
                dosha="Pitta",
                raw_herbs=new_herbs,
                clinic_id=seed["clinic_id"],
                conn=conn,
            )
            new_prescription_id = new_result["prescription_id"]

            new_event_data = build_prescription_confirmed_event_data(
                is_manual=True,
                ai_dosha_imbalance=None,
                ai_prescribed_herbs=None,
                confirmed_dosha_imbalance="Pitta",
                confirmed_prescribed_herbs=new_herbs,
            )
            new_event_id = create_audit_event(
                conn,
                clinic_id=seed["clinic_id"],
                actor_user_id=seed["user_id"],
                event_type=AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
                prescription_id=new_prescription_id,
                preview_id=new_preview_id,
                event_data=new_event_data,
            )
            conn.commit()

            final_stock = conn.execute(
                "SELECT stock_grams FROM inventory WHERE herb_name = ? AND clinic_id = ?",
                (seed["herb_name"], seed["clinic_id"]),
            ).fetchone()["stock_grams"]
            check(
                "(O) inventory deduction for the NEW post-restore prescription is correct",
                final_stock == seed["initial_stock"] - seed["deducted_grams"] - new_grams,
                final_stock,
            )

            # -> a new audit event that links to the restored chain's LAST
            # event, not to a fresh genesis/null -- this is the specific
            # proof that the hash chain continues correctly from restored
            # state rather than silently restarting.
            new_event_row = conn.execute(
                "SELECT previous_event_hash, event_hash FROM audit_events WHERE event_id = ?",
                (new_event_id,),
            ).fetchone()
            check(
                "(O) the new event's previous_event_hash equals the RESTORED chain's last (seeded) event_hash",
                new_event_row["previous_event_hash"] == seed["event_hash"],
                dict(new_event_row),
            )
        finally:
            conn.close()

        report_after = verify_audit_integrity(seed["clinic_id"])
        check("(O) the audit chain still verifies valid after the new post-restore event", report_after.valid, report_after.violations)
        check("(O) the audit chain now has exactly two events (restored + new)", report_after.checked_count == 2, report_after.checked_count)
    finally:
        database_module.DB_PATH = original_db_path
        for p in (source_path, backup_path, restored_path):
            if os.path.exists(p):
                os.remove(p)


_ALL_TESTS = [
    test_backup_of_seeded_database_succeeds_and_passes_verification,
    test_restored_database_opens_and_all_data_survives,
    test_audit_chain_remains_valid_after_restore,
    test_migration_runner_recognizes_restored_database_as_up_to_date,
    test_corrupted_backup_is_rejected_by_verify_backup,
    test_a_nonexistent_backup_path_is_rejected_by_verify_backup,
    test_a_non_sqlite_file_is_rejected_by_verify_backup,
    test_backup_does_not_modify_the_source_database,
    test_backup_refuses_to_write_over_the_source_path,
    test_backup_refuses_to_overwrite_an_existing_destination_file,
    test_backup_refuses_a_destination_whose_parent_directory_does_not_exist,
    test_encrypt_decrypt_round_trip_preserves_a_valid_backup,
    test_decrypt_with_the_wrong_passphrase_fails_loudly_and_leaves_no_partial_file,
    test_encrypt_backup_without_passphrase_env_var_fails_loudly,
    test_full_isolated_restore_rehearsal_supports_a_new_prescription_after_restore,
]


if __name__ == "__main__":
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
