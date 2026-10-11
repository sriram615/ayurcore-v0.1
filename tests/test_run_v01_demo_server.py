"""
scripts/run_v01_demo_server.py: unsafe databases are refused before anything is
served, and a real demo database is served on 127.0.0.1 with DB_PATH pointing
at it. The server is never started: `serve` is replaced by a recorder.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db.database as database
from scripts import run_v01_demo_server as launcher
from scripts.setup_v01_demo import CREDENTIALS_FILENAME, setup_demo

APP_DB = os.path.realpath(database.DB_PATH)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.db_path_before = database.DB_PATH

    def tearDown(self):
        database.DB_PATH = self.db_path_before

    def _serve(self, app, **kwargs):
        self.calls.append((app, kwargs, database.DB_PATH))

    def test_unsafe_databases_are_refused_and_nothing_is_served(self):
        with tempfile.TemporaryDirectory() as outside:
            outside_db = os.path.join(outside, "demo.db")
            open(outside_db, "w").close()
            for target in (APP_DB, "db/ayurcore.db", os.path.join(launcher.ROOT, "ayurcore.db"), outside_db,
                           os.path.join(launcher.ROOT, "demo", "never_set_up.db")):
                with self.subTest(target=target):
                    self.assertEqual(launcher.main(["--db", target], serve=self._serve), 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(database.DB_PATH, self.db_path_before)

    def test_a_setup_demo_database_is_served_on_localhost_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            demo_db = os.path.join(root, "demo", "served.db")
            setup_demo(demo_db, root)
            with mock.patch("builtins.print") as printed:
                self.assertEqual(launcher.main(["--db", demo_db, "--port", "8123"], serve=self._serve, root=root), 0)
            with open(os.path.join(root, "demo", CREDENTIALS_FILENAME)) as f:
                passwords = [u["password"] for u in json.load(f)["users"]]
        import backend.main as main_module
        self.assertEqual(len(self.calls), 1)
        app, kwargs, db_path_during_serve = self.calls[0]
        self.assertIs(app, main_module.app)
        self.assertEqual(kwargs, {"host": "127.0.0.1", "port": 8123})
        self.assertEqual(db_path_during_serve, demo_db)
        self.assertEqual(database.DB_PATH, self.db_path_before)
        output = " ".join(str(c.args) for c in printed.call_args_list)
        self.assertIn("DEMO ONLY", output)
        self.assertTrue(passwords and not any(p in output for p in passwords))


if __name__ == "__main__":
    unittest.main()
