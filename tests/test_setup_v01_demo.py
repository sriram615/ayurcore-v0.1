"""
scripts/setup_v01_demo.py: path-safety checks and a complete setup.

Every setup here runs against a temporary directory used as the checkout
root, so nothing is written into the test sandbox itself; the sandbox's own
db/ayurcore.db is checked to be left exactly as it was.
"""
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db.database as database
from scripts import setup_v01_demo as demo
from scripts.setup_v01_demo import UnsafeDemoPathError, check_demo_location, setup_demo, validate_demo_db_path

APP_DB = os.path.realpath(database.DB_PATH)

# Refusal messages, so each test proves WHICH rule rejected the path.
OUTSIDE = "is not inside this checkout"
PROTECTED_DIR = "is under (db|\\.git)/"
APP_DB_NAME = "is named ayurcore.db"
NOT_DB = "must end in .db"
EXISTS = "already exists"


def _stat(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


class PathSafetyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self._outside = tempfile.TemporaryDirectory()
        self.outside = os.path.realpath(self._outside.name)

    def tearDown(self):
        self._tmp.cleanup()
        self._outside.cleanup()

    def assertRefused(self, path, reason, root=None):
        with self.assertRaisesRegex(UnsafeDemoPathError, reason):
            validate_demo_db_path(path, root or self.root)

    def assertLocationRefused(self, path, reason, root=None):
        """The location rules alone (no existence check), for paths that exist."""
        with self.assertRaisesRegex(UnsafeDemoPathError, reason):
            check_demo_location(path, root or self.root)

    def test_accepts_a_new_db_file_inside_the_root(self):
        path = os.path.join(self.root, "demo", "x.db")
        self.assertEqual(validate_demo_db_path(path, self.root), path)

    def test_default_path_is_inside_this_checkout_and_allowed(self):
        self.assertTrue(demo.DEFAULT_DB_PATH.startswith(demo.ROOT + os.sep))
        if not os.path.lexists(demo.DEFAULT_DB_PATH):
            self.assertEqual(validate_demo_db_path(demo.DEFAULT_DB_PATH), demo.DEFAULT_DB_PATH)

    def test_location_rules_refuse_the_existing_application_database(self):
        # check_demo_location has no existence check, so this proves the
        # location rule itself rejects the real db/ayurcore.db.
        self.assertTrue(os.path.exists(APP_DB))
        self.assertLocationRefused(APP_DB, PROTECTED_DIR, demo.ROOT)
        self.assertLocationRefused(database.DB_PATH, PROTECTED_DIR, demo.ROOT)

    def test_refuses_a_new_file_in_this_checkouts_db_directory(self):
        path = os.path.join(demo.ROOT, "db", "not_yet_created.db")
        self.assertFalse(os.path.lexists(path))
        self.assertRefused(path, PROTECTED_DIR, demo.ROOT)
        self.assertFalse(os.path.lexists(path))

    def test_refuses_the_root_ayurcore_db_by_name(self):
        self.assertFalse(os.path.lexists(os.path.join(demo.ROOT, "ayurcore.db")))
        self.assertRefused(os.path.join(demo.ROOT, "ayurcore.db"), APP_DB_NAME, demo.ROOT)
        self.assertRefused(os.path.join(self.root, "ayurcore.db"), APP_DB_NAME)

    def test_refuses_ayurcore_db_name_anywhere_any_case(self):
        self.assertRefused(os.path.join(self.root, "demo", "ayurcore.db"), APP_DB_NAME)
        self.assertRefused(os.path.join(self.root, "demo", "AyurCore.DB"), APP_DB_NAME)

    def test_refuses_anything_under_db_or_git(self):
        self.assertRefused(os.path.join(self.root, "db", "demo.db"), PROTECTED_DIR)
        self.assertRefused(os.path.join(self.root, "db", "sub", "demo.db"), PROTECTED_DIR)
        self.assertRefused(os.path.join(self.root, ".git", "demo.db"), PROTECTED_DIR)

    def test_protected_directories_are_matched_case_insensitively(self):
        variants = [("DB", "demo.db"), ("Db", "sub", "demo.db"), (".GIT", "demo.db"), (".Git", "x", "demo.db")]
        for parts in variants:
            self.assertRefused(os.path.join(self.root, *parts), PROTECTED_DIR)
        # The same with real db/ and .git/ directories present, as in a checkout.
        os.makedirs(os.path.join(self.root, "db"))
        os.makedirs(os.path.join(self.root, ".git"))
        for parts in variants:
            self.assertRefused(os.path.join(self.root, *parts), PROTECTED_DIR)
        self.assertRefused(os.path.join(demo.ROOT, "DB", "not_yet_created.db"), PROTECTED_DIR, demo.ROOT)
        self.assertEqual(os.listdir(os.path.join(self.root, "db")), [])

    def test_refuses_paths_outside_the_root(self):
        self.assertRefused(os.path.join(self.outside, "demo.db"), OUTSIDE)
        self.assertRefused(os.path.join(self.root, "..", os.path.basename(self.outside), "demo.db"), OUTSIDE)
        self.assertRefused(os.path.join(self.root, "demo", "..", "..", "demo.db"), OUTSIDE)

    def test_refuses_the_root_itself_and_non_db_names(self):
        self.assertLocationRefused(self.root, OUTSIDE)
        self.assertRefused(os.path.join(self.root, "demo", "demo.sqlite"), NOT_DB)
        self.assertRefused(os.path.join(self.root, "demo", "demo"), NOT_DB)

    def test_refuses_a_symlinked_directory_that_escapes_the_root(self):
        os.symlink(self.outside, os.path.join(self.root, "escape"))
        self.assertRefused(os.path.join(self.root, "escape", "demo.db"), OUTSIDE)

    def test_location_rules_follow_a_symlink_into_db(self):
        os.makedirs(os.path.join(self.root, "db"))
        os.symlink(os.path.join(self.root, "db", "target.db"), os.path.join(self.root, "link.db"))
        self.assertLocationRefused(os.path.join(self.root, "link.db"), PROTECTED_DIR)

    def test_refuses_existing_files_and_companions(self):
        demo_dir = os.path.join(self.root, "demo")
        os.makedirs(demo_dir)
        existing = os.path.join(demo_dir, "old.db")
        open(existing, "w").close()
        self.assertRefused(existing, EXISTS)
        for suffix in ("-wal", "-shm", "-journal"):
            path = os.path.join(demo_dir, f"new{suffix[1:]}.db")
            open(path + suffix, "w").close()
            self.assertRefused(path, EXISTS)

    def test_refuses_a_dangling_symlink(self):
        os.symlink(os.path.join(self.root, "nowhere.db"), os.path.join(self.root, "dangling.db"))
        self.assertRefused(os.path.join(self.root, "dangling.db"), EXISTS)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.db_path = os.path.join(self.root, "demo", "v01.db")
        self.app_db_before = _stat(APP_DB) if os.path.exists(APP_DB) else None
        self.db_path_before = database.DB_PATH

    def tearDown(self):
        database.DB_PATH = self.db_path_before
        self._tmp.cleanup()

    def assertAppDatabaseUntouched(self):
        self.assertEqual(database.DB_PATH, self.db_path_before)
        self.assertEqual(_stat(APP_DB) if os.path.exists(APP_DB) else None, self.app_db_before)

    def test_complete_setup(self):
        summary = setup_demo(self.db_path, self.root)
        self.assertAppDatabaseUntouched()
        self.assertEqual(summary["database"], self.db_path)
        self.assertNotIn("password", json.dumps(summary))

        from db.migrations import discover_migrations
        expected_versions = [m.version for m in discover_migrations()]
        self.assertIn(17, expected_versions)
        self.assertEqual(summary["migrations_applied"], expected_versions)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
            self.assertEqual(versions, expected_versions)
            users = {r["username"]: r for r in conn.execute("SELECT * FROM users")}
            self.assertEqual({u: users[u]["role"] for u in users}, dict(demo.DEMO_USERS))
            self.assertTrue(all(r["clinic_id"] == demo.DEMO_CLINIC_ID and r["is_active"] == 1 for r in users.values()))
            self.assertTrue(all(r["password_hash"].startswith("$argon2id$") for r in users.values()))
            patients = conn.execute("SELECT * FROM patients").fetchall()
            self.assertEqual(len(patients), 1)
            self.assertEqual(patients[0]["clinic_id"], demo.DEMO_CLINIC_ID)
            self.assertEqual(patients[0]["clinic_patient_id"], demo.DEMO_PATIENT["clinic_patient_id"])
            self.assertEqual(patients[0]["created_by_user_id"], users["v01_doctor"]["user_id"])
            self.assertEqual(summary["patient"]["patient_pk"], patients[0]["patient_pk"])
            stock = {r["herb_name"]: r["stock_grams"]
                     for r in conn.execute("SELECT * FROM inventory WHERE clinic_id = ?", (demo.DEMO_CLINIC_ID,))}
            self.assertEqual(stock, {name: grams for name, _, grams, _, _ in demo.DEMO_STOCK})
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inventory").fetchone()[0], len(demo.DEMO_STOCK))
            for table in ("prescriptions", "prescription_previews", "encounters", "guardrail_shadow_activations"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        finally:
            conn.close()

        from backend.auth_service import verify_password
        credentials_path = summary["credentials_file"]
        self.assertEqual(os.path.dirname(credentials_path), os.path.dirname(self.db_path))
        self.assertEqual(stat.S_IMODE(os.stat(credentials_path).st_mode), 0o600)
        with open(credentials_path) as f:
            credentials = json.load(f)
        self.assertEqual(credentials["clinic_id"], demo.DEMO_CLINIC_ID)
        for entry in credentials["users"]:
            self.assertTrue(verify_password(entry["password"], users[entry["username"]]["password_hash"]))
        self.assertEqual(len({e["password"] for e in credentials["users"]}), len(demo.DEMO_USERS))

    def test_herbs_in_stock_resolve_to_themselves(self):
        from backend.herb_resolver import normalize_herb
        for name, *_ in demo.DEMO_STOCK:
            self.assertEqual(normalize_herb(name), name)

    def test_second_run_is_refused_and_changes_nothing(self):
        setup_demo(self.db_path, self.root)
        before = _stat(self.db_path)
        with self.assertRaises(UnsafeDemoPathError):
            setup_demo(self.db_path, self.root)
        self.assertEqual(_stat(self.db_path), before)
        self.assertAppDatabaseUntouched()

    def test_existing_credentials_file_is_refused_before_any_database_is_created(self):
        os.makedirs(os.path.dirname(self.db_path))
        with open(os.path.join(os.path.dirname(self.db_path), demo.CREDENTIALS_FILENAME), "w") as f:
            f.write("{}")
        with self.assertRaises(UnsafeDemoPathError):
            setup_demo(self.db_path, self.root)
        self.assertFalse(os.path.lexists(self.db_path))

    def test_failure_removes_created_files_and_restores_db_path(self):
        with mock.patch("backend.patient_store.create_patient", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                setup_demo(self.db_path, self.root)
        demo_dir = os.path.dirname(self.db_path)
        self.assertEqual(os.listdir(demo_dir), [])
        self.assertAppDatabaseUntouched()

    def test_unsafe_path_is_refused_before_anything_is_created(self):
        with self.assertRaisesRegex(UnsafeDemoPathError, PROTECTED_DIR):
            setup_demo(os.path.join(self.root, "db", "ayurcore.db"), self.root)
        self.assertFalse(os.path.exists(os.path.join(self.root, "db")))
        self.assertAppDatabaseUntouched()


class CommandLineTests(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, os.path.join(demo.ROOT, "scripts", "setup_v01_demo.py"), *args],
                              capture_output=True, text=True, timeout=120, cwd=demo.ROOT)

    def test_cli_refuses_the_application_database_and_leaves_it_untouched(self):
        before = _stat(APP_DB) if os.path.exists(APP_DB) else None
        for target in (APP_DB, os.path.join(demo.ROOT, "ayurcore.db"), "db/ayurcore.db"):
            result = self._run("--db", target)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("Refused", result.stderr)
        self.assertEqual(_stat(APP_DB) if os.path.exists(APP_DB) else None, before)
        self.assertFalse(os.path.exists(os.path.join(demo.ROOT, "ayurcore.db")))

    def test_cli_refuses_new_files_under_db_in_any_case(self):
        for target in ("db/not_yet_created.db", "DB/not_yet_created.db"):
            result = self._run("--db", target)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertRegex(result.stderr, PROTECTED_DIR)
            self.assertFalse(os.path.lexists(os.path.join(demo.ROOT, "db", "not_yet_created.db")))

    def test_cli_refuses_a_path_outside_the_checkout(self):
        with tempfile.TemporaryDirectory() as outside:
            target = os.path.join(outside, "demo.db")
            result = self._run("--db", target)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
