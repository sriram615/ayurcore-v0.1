"""
S5 v1 interpretation holdout (integrity only) and its one-shot evaluator (memory/evaluate_s5.py).
The official S5 file is NEVER evaluated here: the evaluator is exercised on a small fixture.
"""
import copy
import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory import evaluate_s5 as ev
from memory.planner import Planner

_spec = importlib.util.spec_from_file_location("s5_dataset_t", ROOT / "scripts" / "s5_dataset.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)
_spec3 = importlib.util.spec_from_file_location("s3_generator_t5", ROOT / "scripts" / "s3_generator.py")
s3 = importlib.util.module_from_spec(_spec3)
_spec3.loader.exec_module(s3)
S5_FINGERPRINT = "540b922258d89f79319dcf2b3b1cea0d537f6c51298550a22cce6882bef2b71b"
DATA = tool.load()


def _query(op, target, herb=None, a="previous"):
    return {"kind": "query", "query": {"op": op, "target": target, "herb": herb, "dosha": None, "last_n": None,
                                       "a": a, "b": "last"}}


class S5IntegrityTests(unittest.TestCase):
    def test_frozen(self):
        self.assertEqual(tool.verify(), [])
        self.assertEqual(tool.fingerprint(DATA), S5_FINGERPRINT)
        self.assertEqual(tool.main(["freeze"]), 1)

    def test_coverage(self):
        self.assertEqual({c["category"] for c in DATA["cases"]}, set(tool.CATEGORIES))
        self.assertEqual({c["language"] for c in DATA["cases"]}, set(tool.LANGUAGES))
        self.assertGreaterEqual(sum(c["gold_plan"]["kind"] == "abstain" for c in DATA["cases"]), 12)
        for cat in tool.CATEGORIES:
            if cat != "legit_ambiguous":
                self.assertTrue(any(c["category"] == cat and c["gold_plan"]["kind"] == "query" for c in DATA["cases"]), cat)

    def test_corruption_detected(self):
        for label, mutate in {
            "gold answer changed": lambda d: next(c for c in d["cases"] if c["gold_answer"])["gold_answer"].update(value=-1),
            "S4 question copied": lambda d: d["cases"][0].update(question=tool.S4.load()["cases"][0]["question"]),
            "S3 question copied": lambda d: d["cases"][0].update(question=s3.load("dev")[0]["question"]),
            "non-ambiguity abstain": lambda d: next(c for c in d["cases"] if c["gold_plan"]["kind"] == "abstain")[
                "gold_plan"].update(reason="TREATMENT"),
            "duplicate id": lambda d: d["cases"][1].update(id=d["cases"][0]["id"]),
        }.items():
            with self.subTest(label):
                data = copy.deepcopy(DATA)
                mutate(data)
                self.assertTrue(tool.validate(data))


class EvaluatorTests(unittest.TestCase):
    def test_one_shot_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ev.S5EvaluationRefused, "one-shot"):
                ev.run(final=False, model_file="/nonexistent", out_dir=tmp)
            (pathlib.Path(tmp) / "s5_summary.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ev.S5EvaluationRefused, "already evaluated"):
                ev.run(final=True, model_file="/nonexistent", out_dir=tmp)

    def test_mcnemar(self):
        self.assertEqual(ev.mcnemar_exact(0, 0), 1.0)
        self.assertAlmostEqual(ev.mcnemar_exact(0, 6), 2 / 64)
        self.assertEqual(ev.mcnemar_exact(3, 3), 1.0)

    def test_fixture_bookkeeping(self):
        """Fixture cases (not S5): one the single planner gets wrong, one ambiguous, one plain."""
        planner = Planner.train([(q["question"], q["frame"]) for q in s3.load("train")], 1, 13)
        snaps = tool.snapshots()
        fixture = [
            {"id": "fx-1", "category": "target_confusion", "language": "en", "question": "Please list her visits.",
             "patient": "s2_clinic_a/P03", "gold_plan": _query("list", "encounter")},
            {"id": "fx-2", "category": "legit_ambiguous", "language": "manglish", "question": "ethra Brahmi koduthu?",
             "patient": "s2_clinic_a/P03", "gold_plan": {"kind": "abstain", "reason": "AMBIGUOUS"}},
            {"id": "fx-3", "category": "control", "language": "en", "question": "How many prescriptions does she have?",
             "patient": "s2_clinic_a/P03", "gold_plan": _query("count", "prescription")},
        ]
        for c in fixture:
            c["gold_answer"] = tool.gold_answer(snaps, c)
        summary, rows = ev.evaluate(fixture, snaps, planner)
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["fx-1"]["single"]["wrong_but_valid"])
        self.assertTrue(by_id["fx-1"]["candidate"]["correct"])
        self.assertEqual(by_id["fx-1"]["candidate"]["decision"], "repaired")
        self.assertTrue(by_id["fx-2"]["single"]["wrong_but_valid"])
        self.assertEqual(by_id["fx-2"]["candidate"]["outcome"], "abstain")
        self.assertTrue(by_id["fx-3"]["single"]["correct"] and by_id["fx-3"]["candidate"]["correct"])
        cand, single = summary["arms"]["candidate"], summary["arms"]["single"]
        self.assertEqual((single["wrong_but_valid"]["count"], cand["wrong_but_valid"]["count"]), (2, 0))
        self.assertEqual(summary["paired"]["wrong_but_valid"], {"single_only": 2, "candidate_only": 0,
                                                                "mcnemar_exact_p": 0.5})
        self.assertEqual(cand["ambiguity_detection"]["rate"], 1.0)
        self.assertEqual(cand["verifier"]["tamper_caught"]["rate"], 1.0)
        self.assertEqual(cand["verifier"]["answers_without_evidence"], 0)


if __name__ == "__main__":
    unittest.main()
