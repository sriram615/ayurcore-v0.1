"""
scripts/demo_v01_walkthrough.py: the v0.1 journey end to end through the HTTP
API, on a disposable demo database made by scripts/setup_v01_demo.py in a
temporary directory (never the sandbox's db/ayurcore.db, which is checked to
be left exactly as it was).

After the walkthrough the demo database is read directly to confirm what the
API reported: one linked prescription with the corrected items, exact stock
deductions, one audit event, and nothing written by the rejected requests.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db.database as database
from scripts import setup_v01_demo as setup
from scripts.demo_v01_walkthrough import WalkthroughFailure, open_demo_database, run_walkthrough
from scripts.setup_v01_demo import UnsafeDemoPathError, setup_demo

APP_DB = os.path.realpath(database.DB_PATH)
INITIAL_STOCK = {name: grams for name, _, grams, _, _ in setup.DEMO_STOCK}


def _stat(path):
    if not os.path.exists(path):
        return None
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


class WalkthroughTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.db_path = os.path.join(self.root, "demo", "walkthrough.db")
        self.app_db_before = _stat(APP_DB)
        self.db_path_before = database.DB_PATH
        setup_demo(self.db_path, self.root)

    def tearDown(self):
        database.DB_PATH = self.db_path_before
        self._tmp.cleanup()

    def _query(self, sql, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _run(self):
        lines = []
        summary = run_walkthrough(self.db_path, self.root, log=lines.append)
        self.assertEqual(database.DB_PATH, self.db_path_before)
        self.assertEqual(_stat(APP_DB), self.app_db_before)
        return summary, "\n".join(lines)

    def test_journey_and_negative_cases_with_database_cross_check(self):
        summary, output = self._run()
        for marker in [f"[{n}]" for n in (1, 2, 3, 4, "5-6", 7, 8, 9, 10, 11)] + ["[N1]", "[N2]", "[N3]", "[N4]"]:
            self.assertIn(marker, output)
        self.assertIn(f"All {summary['checks']} checks passed.", output)
        for user in json.load(open(os.path.join(self.root, "demo", setup.CREDENTIALS_FILENAME)))["users"]:
            self.assertNotIn(user["password"], output)

        # Exactly one prescription: the rejected requests wrote none.
        prescriptions = self._query("SELECT * FROM prescriptions")
        self.assertEqual(len(prescriptions), 1)
        rx = prescriptions[0]
        self.assertEqual(rx["id"], summary["prescription_id"])
        self.assertEqual((rx["clinic_id"], rx["patient_pk"], rx["encounter_pk"], rx["dosha_imbalance"]),
                         (setup.DEMO_CLINIC_ID, summary["patient_pk"], summary["encounter_pk"], "Vata-Pitta"))
        items = {(r["herb_name"], r["grams"]) for r in self._query(
            "SELECT herb_name, grams FROM prescription_items WHERE prescription_id = ?", (rx["id"],))}
        self.assertEqual(items, {("Ashwagandha", 40), ("Shatavari", 25)})

        stock = {r["herb_name"]: r["stock_grams"] for r in self._query(
            "SELECT herb_name, stock_grams FROM inventory WHERE clinic_id = ?", (setup.DEMO_CLINIC_ID,))}
        expected = dict(INITIAL_STOCK, Ashwagandha=INITIAL_STOCK["Ashwagandha"] - 40,
                        Shatavari=INITIAL_STOCK["Shatavari"] - 25)
        self.assertEqual(stock, expected)
        self.assertEqual((summary["stock_before"], summary["stock_after"]), (INITIAL_STOCK, expected))

        events = self._query("SELECT event_type, prescription_id, preview_id FROM audit_events")
        confirmed = [e for e in events if e["event_type"] == "PRESCRIPTION_CONFIRMED"]
        self.assertEqual([(e["prescription_id"], e["preview_id"]) for e in confirmed],
                         [(rx["id"], summary["preview_id"])])
        self.assertEqual(sum(e["event_type"] == "ENCOUNTER_OPENED" for e in events), 3)
        self.assertEqual(sum(e["event_type"] == "ENCOUNTER_CLOSED" for e in events), 3)

        previews = {r["preview_id"]: r["status"] for r in self._query("SELECT preview_id, status FROM prescription_previews")}
        self.assertEqual(previews.pop(summary["preview_id"]), "CONFIRMED")
        self.assertEqual(sorted(previews.values()), ["PENDING", "PENDING"])  # closed-encounter, insufficient stock

        self.assertEqual(self._query("SELECT COUNT(*) FROM idempotency_keys")[0][0], 1)
        self.assertEqual(self._query("SELECT COUNT(*) FROM encounters WHERE status = 'OPEN'")[0][0], 0)
        self.assertEqual(self._query("SELECT COUNT(*) FROM patients")[0][0], 1)

    def test_walkthrough_can_be_repeated_on_the_same_demo_database(self):
        first, _ = self._run()
        second, _ = self._run()
        self.assertEqual(second["patient_pk"], first["patient_pk"])
        self.assertNotEqual(second["prescription_id"], first["prescription_id"])
        self.assertEqual(second["stock_before"], first["stock_after"])
        self.assertEqual(self._query("SELECT COUNT(*) FROM prescriptions")[0][0], 2)

    def test_history_check_stays_exact_beyond_one_page(self):
        # 55 earlier prescriptions for the demo patient (> the endpoint's
        # 50-per-page maximum), created through the real API.
        import backend.main as main_module
        from fastapi.testclient import TestClient

        with open(os.path.join(self.root, "demo", setup.CREDENTIALS_FILENAME)) as f:
            doctor = next(u for u in json.load(f)["users"] if u["role"] == "DOCTOR")
        patient = dict(self._query("SELECT patient_pk, display_name FROM patients")[0])
        database.DB_PATH = self.db_path
        try:
            with TestClient(main_module.app) as client:
                token = client.post("/auth/login", json={"username": doctor["username"],
                                                         "password": doctor["password"]}).json()["token"]
                headers = {"Authorization": f"Bearer {token}"}
                for _ in range(55):
                    preview = client.post("/prescribe/manual-preview", headers=headers, json={
                        "patient_name": patient["display_name"], "patient_pk": patient["patient_pk"],
                        "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Triphala", "grams": 1}]})
                    self.assertEqual(preview.status_code, 200, preview.text)
                    confirm = client.post("/prescribe/confirm", headers=headers,
                                          json={"preview_id": preview.json()["preview_id"]})
                    self.assertEqual(confirm.status_code, 200, confirm.text)
        finally:
            database.DB_PATH = self.db_path_before

        summary, output = self._run()
        self.assertIn("newest history entry is this prescription", output)
        self.assertEqual(self._query("SELECT COUNT(*) FROM prescriptions WHERE patient_pk = ?",
                                     (patient["patient_pk"],))[0][0], 56)
        newest = self._query("SELECT id FROM prescriptions ORDER BY created_at DESC, id DESC LIMIT 1")[0][0]
        self.assertEqual(newest, summary["prescription_id"])

    def test_a_failed_check_is_reported_not_hidden(self):
        # An encounter left open makes step 3's "encounter opened" check fail.
        import backend.main as main_module
        from fastapi.testclient import TestClient

        credentials = json.load(open(os.path.join(self.root, "demo", setup.CREDENTIALS_FILENAME)))
        doctor = next(u for u in credentials["users"] if u["role"] == "DOCTOR")
        database.DB_PATH = self.db_path
        try:
            with TestClient(main_module.app) as client:
                token = client.post("/auth/login", json={"username": doctor["username"],
                                                         "password": doctor["password"]}).json()["token"]
                patient_pk = self._query("SELECT patient_pk FROM patients")[0][0]
                resp = client.post(f"/patients/{patient_pk}/encounters", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(resp.status_code, 201)
        finally:
            database.DB_PATH = self.db_path_before
        with self.assertRaises(WalkthroughFailure):
            run_walkthrough(self.db_path, self.root, log=lambda _: None)
        self.assertEqual(database.DB_PATH, self.db_path_before)
        self.assertEqual(self._query("SELECT COUNT(*) FROM prescriptions")[0][0], 0)


class DatabaseRefusalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)
        self.app_db_before = _stat(APP_DB)

    def tearDown(self):
        self._tmp.cleanup()

    def test_refuses_the_application_database(self):
        for path in (APP_DB, os.path.join(setup.ROOT, "ayurcore.db")):
            with self.assertRaises(UnsafeDemoPathError):
                open_demo_database(path, setup.ROOT)
        self.assertEqual(_stat(APP_DB), self.app_db_before)

    def test_refuses_a_missing_database(self):
        with self.assertRaises(UnsafeDemoPathError):
            open_demo_database(os.path.join(self.root, "demo", "missing.db"), self.root)

    def test_refuses_a_database_without_matching_setup_credentials(self):
        demo_dir = os.path.join(self.root, "demo")
        os.makedirs(demo_dir)
        path = os.path.join(demo_dir, "plain.db")
        sqlite3.connect(path).close()
        with self.assertRaises(UnsafeDemoPathError):
            open_demo_database(path, self.root)  # no credentials file
        with open(os.path.join(demo_dir, setup.CREDENTIALS_FILENAME), "w") as f:
            json.dump({"database": os.path.join(demo_dir, "other.db"), "users": []}, f)
        with self.assertRaises(UnsafeDemoPathError):
            open_demo_database(path, self.root)  # credentials for another database

    def test_refuses_a_path_outside_the_root(self):
        with tempfile.TemporaryDirectory() as outside:
            path = os.path.join(outside, "x.db")
            sqlite3.connect(path).close()
            with self.assertRaises(UnsafeDemoPathError):
                open_demo_database(path, self.root)


if __name__ == "__main__":
    unittest.main()
