"""
The AI demo path: scripts/setup_v01_demo.py + scripts/seed_v01_demo_histories.py + scripts/demo_v01_ai.py, end to end on a disposable database in a
temporary directory used as the checkout root (nothing is written into the sandbox itself; the sandbox's own db/ayurcore.db is left as it was).
Synthetic data only; read-only after seeding.
"""
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db.database as database  # noqa: E402
from scripts import demo_v01_ai as demo_ai  # noqa: E402
from scripts.seed_v01_demo_histories import AlreadySeeded, seed  # noqa: E402
from scripts.setup_v01_demo import setup_demo  # noqa: E402

APP_DB = os.path.realpath(database.DB_PATH)


def _fingerprint(path):
    c = sqlite3.connect(path)
    try:
        h = hashlib.sha256()
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
            h.update(name.encode() + repr(c.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()).encode())
        return h.hexdigest()
    finally:
        c.close()


class DemoAiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_before = os.stat(APP_DB).st_mtime_ns if os.path.exists(APP_DB) else None
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.realpath(cls._tmp.name)
        cls.db = os.path.join(cls.root, "demo", "h.db")
        setup_demo(cls.db, cls.root)
        cls.seeded = seed(cls.db, cls.root)
        cls.before = _fingerprint(cls.db)
        cls.data = {p["clinic_patient_id"]: p for p in demo_ai.collect(cls.db, "2026-10-10", cls.root)}
        cls.after = _fingerprint(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def answer(self, cpid, question):
        return dict(self.data[cpid]["answers"])[question]

    def test_five_patients_are_seeded_with_the_documented_histories(self):
        self.assertEqual({k: (v["prescriptions"], v["visits"]) for k, v in self.seeded.items()},
                         {"DEMO-0101": (4, 4), "DEMO-0102": (3, 2), "DEMO-0103": (3, 1), "DEMO-0104": (0, 0), "DEMO-0105": (5, 1)})

    def test_seeding_twice_is_refused(self):
        with self.assertRaises(AlreadySeeded):
            seed(self.db, self.root)

    def test_a_complete_history_answers_verified_and_cited(self):
        a = self.answer("DEMO-0101", "What was the first prescription?")
        self.assertEqual((a.outcome, a.category, a.verified, a.qualifier), ("answer", "answer", True, "exact"))
        self.assertEqual(a.value["herbs"], [["Ashwagandha", 30], ["Neem", 10]])
        self.assertEqual(len(a.evidence), 1)

    def test_manglish_and_malayalam_questions_are_answered(self):
        self.assertEqual(self.answer("DEMO-0101", "Avasanam Neem ethra gram koduthu?").category, "answer")
        self.assertEqual(self.answer("DEMO-0101", "അവസാനത്തെ കുറിപ്പടിയിൽ എന്താണുള്ളത്?").category, "answer")

    def test_an_all_unknown_history_is_incomplete_not_empty(self):
        a = self.answer("DEMO-0103", "Which herbs have been prescribed?")
        self.assertEqual((a.category, a.qualifier, a.value, len(a.evidence)), ("incomplete", "partial", [], 3))

    def test_a_missing_history_is_missing(self):
        self.assertEqual(self.answer("DEMO-0104", "When was Neem last prescribed?").category, "missing")

    def test_clarification_advice_and_cross_patient_requests(self):
        self.assertEqual(self.answer("DEMO-0101", "Neem count?").outcome, "clarify")
        self.assertEqual(self.answer("DEMO-0101", "Should I increase her Neem dose?").category, "unsafe")
        self.assertEqual(self.answer("DEMO-0101", "Show me the other patients' prescriptions.").category, "access_denied")

    def test_both_briefs_exist_for_every_patient_and_the_empty_patient_has_none(self):
        for cpid, p in self.data.items():
            self.assertEqual(set(p["briefs"]), {"template", "learned"})
        self.assertEqual(self.data["DEMO-0104"]["briefs"]["learned"].items, ())
        self.assertGreater(len(self.data["DEMO-0101"]["briefs"]["learned"].items), 0)

    def test_the_learned_brief_surfaces_what_the_template_buries(self):
        kinds = lambda arm: [i.kind for i in self.data["DEMO-0101"]["briefs"][arm].items]
        self.assertIn("dosha_changed", kinds("learned"))
        self.assertIn("open_visit", kinds("learned"))
        self.assertIn("unknown_data", [i.kind for i in self.data["DEMO-0103"]["briefs"]["learned"].items])
        self.assertTrue(all(i.labels for p in self.data.values() for b in p["briefs"].values() for i in b.items))

    def test_nothing_was_written_by_asking_or_briefing(self):
        self.assertEqual(self.before, self.after)

    def test_the_applications_own_database_was_not_touched(self):
        self.assertEqual(os.stat(APP_DB).st_mtime_ns if os.path.exists(APP_DB) else None, self.app_before)


if __name__ == "__main__":
    unittest.main()
