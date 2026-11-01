"""
Unit tests for scripts/seed_demo_clinic.py.
"""
from __future__ import annotations

import os
import tempfile
import unittest

import db.database as database
from scripts.seed_demo_clinic import (
    DEMO_CLINIC_ID,
    DEMO_PASSWORD,
    DEMO_USERS,
    seed_demo_clinic,
    validate_db_path,
)


class SeedDemoClinicTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = os.path.realpath(self._tmp.name)
        self.db_path = os.path.join(self.tmp_dir, "test_demo.db")
        self.creds_path = os.path.join(self.tmp_dir, "credentials.v01-demo.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_validate_db_path_rejects_forbidden(self):
        root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        forbidden = os.path.join(root, "db", "ayurcore.db")
        with self.assertRaises(ValueError):
            validate_db_path(forbidden, root)

        with self.assertRaises(ValueError):
            validate_db_path(os.path.join(self.tmp_dir, "ayurcore.db"), root)

        with self.assertRaises(ValueError):
            validate_db_path(os.path.join(self.tmp_dir, "invalid.txt"), root)

    def test_validate_db_path_accepts_valid(self):
        accepted = validate_db_path(self.db_path)
        self.assertEqual(accepted, self.db_path)

    def test_seed_demo_clinic_populates_and_is_idempotent(self):
        # 1. First run: populate fresh database
        summary1 = seed_demo_clinic(db_path=self.db_path, credentials_path=self.creds_path)
        self.assertTrue(os.path.exists(self.db_path))
        self.assertTrue(os.path.exists(self.creds_path))
        self.assertTrue(summary1["audit_valid"])

        counts1 = summary1["counts"]
        self.assertEqual(counts1["users"], 3)
        self.assertEqual(counts1["patients"], 6)
        self.assertEqual(counts1["encounters"], 9)
        self.assertEqual(counts1["prescriptions"], 8)
        self.assertEqual(counts1["inventory"], 7)
        self.assertGreaterEqual(counts1["audit_events"], 23)

        # 2. Second run: idempotency check (no duplicate patients, encounters, prescriptions)
        summary2 = seed_demo_clinic(db_path=self.db_path, credentials_path=self.creds_path)
        self.assertTrue(summary2["audit_valid"])
        counts2 = summary2["counts"]
        self.assertEqual(counts1, counts2)

        # 3. Test API integration with the seeded DB
        saved_db_path = database.DB_PATH
        database.DB_PATH = self.db_path
        try:
            from fastapi.testclient import TestClient
            from backend.main import app

            client = TestClient(app)

            # Test login for all 3 users
            tokens = {}
            for u in DEMO_USERS:
                res = client.post("/auth/login", json={"username": u["username"], "password": DEMO_PASSWORD})
                self.assertEqual(res.status_code, 200, f"Login failed: {res.text}")
                data = res.json()
                self.assertEqual(data["username"], u["username"])
                self.assertEqual(data["role"], u["role"])
                self.assertEqual(data["clinic_id"], DEMO_CLINIC_ID)
                tokens[u["username"]] = data["token"]

            # Test GET /patients for dr_sharma
            dr_token = tokens["dr_sharma"]
            res_p = client.get("/patients?q=synthetic", headers={"Authorization": f"Bearer {dr_token}"})
            self.assertEqual(res_p.status_code, 200)
            patients = res_p.json()["results"]
            self.assertEqual(len(patients), 6)

            # Test GET /inventory for dr_sharma
            res_i = client.get("/inventory", headers={"Authorization": f"Bearer {dr_token}"})
            self.assertEqual(res_i.status_code, 200)
            items = res_i.json()["items"]
            self.assertEqual(len(items), 7)

        finally:
            database.DB_PATH = saved_db_path


if __name__ == "__main__":
    unittest.main()
