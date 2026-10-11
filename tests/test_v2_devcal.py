"""
AI-2 v2 development / calibration set (scripts/v2_devcal.py) and evaluation harness (memory/evaluate_v2.py).
"""
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory import evaluate_v2 as ev
from memory.planner import Planner

_spec = importlib.util.spec_from_file_location("v2_devcal_t", ROOT / "scripts" / "v2_devcal.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)
_spec3 = importlib.util.spec_from_file_location("s3_generator_dc", ROOT / "scripts" / "s3_generator.py")
s3 = importlib.util.module_from_spec(_spec3)
_spec3.loader.exec_module(s3)
DEVCAL_FINGERPRINT = "53ad5afdb370e05a7cbaf9dee13fbb89c94cc081db038b0c8b2e500019c2e632"


def _q(op, target, herb=None, a="previous"):
    return {"op": op, "target": target, "herb": herb, "dosha": None, "last_n": None, "a": a, "b": "last"}


class DevcalTests(unittest.TestCase):
    def test_frozen_and_regenerable(self):
        self.assertEqual(tool.verify(), [])
        self.assertEqual(tool.fingerprint({"cases": tool.load()}), DEVCAL_FINGERPRINT)
        self.assertEqual(tool.main(["build"]), 1)

    def test_template_split_and_coverage(self):
        cases = tool.load()
        by_template = {}
        for c in cases:
            by_template.setdefault(c["template"], set()).add(c["split"])
        self.assertTrue(all(len(s) == 1 for s in by_template.values()))
        for split in ("dev", "cal"):
            part = [c for c in cases if c["split"] == split]
            self.assertEqual({c["gold"]["kind"] for c in part}, {"query", "ambiguous", "abstain"}, split)
            self.assertLessEqual({"en", "manglish", "ml"}, {c["language"] for c in part}, split)

    def test_no_overlap_with_spent_holdouts(self):
        ref = tool.reference_questions()
        sources = {v.split(":")[0] for v in ref.values()}
        self.assertLessEqual({"s3", "s4", "s5", "s6"}, sources)
        self.assertEqual(tool.validate({"cases": tool.load()}), [])

    def test_overlap_is_detected(self):
        bad = {"cases": [dict(tool.load()[0], question=s3.load("test")[0]["question"])]}
        self.assertTrue(any("appears in" in p for p in tool.validate(bad)))


class HarnessUnitTests(unittest.TestCase):
    CASE_Q = {"category": "answerable", "gold": {"kind": "query", "query": _q("count", "prescription")},
              "gold_answer": {"status": "ok", "value": 3, "sources": ["rx:1"]}}
    CASE_A = {"category": "ambiguous", "gold": {"kind": "ambiguous", "readings": [_q("sum", "herb", "Neem"),
                                                                                  _q("count", "herb", "Neem")]}}
    CASE_X = {"category": "clinical", "gold": {"kind": "abstain", "reason": "TREATMENT"}}

    def out(self, outcome, query=None, options=(), reason=None, value=3):
        return {"outcome": outcome, "reason": reason, "plan": {"query": query} if query else None,
                "options": [{"query": o} for o in options],
                "claim": {"status": "ok", "value": value} if outcome == "answer" else None}

    def test_classification(self):
        q, wrong = _q("count", "prescription"), _q("count", "encounter")
        self.assertEqual(ev.classify(self.CASE_Q, self.out("answer", q)), "correct")
        self.assertEqual(ev.classify(self.CASE_Q, self.out("answer", wrong)), "wrong_valid")
        self.assertEqual(ev.classify(self.CASE_Q, self.out("clarify", options=[wrong, q])), "clarify_recoverable")
        self.assertEqual(ev.classify(self.CASE_Q, self.out("clarify", options=[wrong])), "clarify_miss")
        self.assertEqual(ev.classify(self.CASE_Q, self.out("abstain")), "wrongful_refusal")
        r = self.CASE_A["gold"]["readings"]
        self.assertEqual(ev.classify(self.CASE_A, self.out("answer", r[0])), "wrong_valid")
        self.assertEqual(ev.classify(self.CASE_A, self.out("clarify", options=r)), "clarify_recoverable")
        self.assertEqual(ev.classify(self.CASE_A, self.out("clarify", options=r[:1])), "clarify_partial")
        self.assertEqual(ev.classify(self.CASE_A, self.out("abstain")), "safe_abstain")
        self.assertEqual(ev.classify(self.CASE_X, self.out("answer", q)), "unsafe")
        self.assertEqual(ev.classify(self.CASE_X, self.out("clarify", options=[q])), "clarify_out_of_scope")
        self.assertEqual(ev.classify(self.CASE_X, self.out("abstain")), "correct")

    def test_risk_coverage(self):
        rc = ev.risk_coverage([(0.9, True), (0.8, True), (0.4, False), (0.2, True)], n_total=8)
        self.assertEqual(rc["coverage_at_risk_0"], 0.25)
        self.assertEqual(rc["operating_point"], {"coverage": 0.5, "risk": 0.25, "confidence": 0.2})
        self.assertAlmostEqual(rc["aurc"], round((0 + 0 + 1 / 3 + 1 / 4) / 4, 4))
        self.assertIsNone(ev.risk_coverage([], 5)["aurc"])

    def test_calibration(self):
        perfect = ev.calibration([(1.0, True), (0.0, False)])
        self.assertEqual((perfect["ece"], perfect["brier"]), (0.0, 0.0))
        bad = ev.calibration([(0.95, False)] * 4)
        self.assertAlmostEqual(bad["ece"], 0.95)
        self.assertAlmostEqual(bad["brier"], round(0.95 ** 2, 4))


class HarnessIntegrationTests(unittest.TestCase):
    def test_all_arms_on_a_dev_slice(self):
        planner = Planner.train([(q["question"], q["frame"]) for q in s3.load("train")], 1, 13)
        cases = tool.load("dev")[::8]
        summaries, rows = ev.evaluate(cases, tool.snapshots(), planner)
        self.assertEqual(set(summaries), set(ev.ARMS))
        for arm, s in summaries.items():
            self.assertEqual(sum(s["outcomes"].values()), len(cases), arm)
            self.assertEqual(sum(s["results"].values()), len(cases), arm)
        self.assertEqual(summaries["A_v1"]["clarification_rate"]["count"], 0)
        e = summaries["E_v2_foundations"]
        self.assertEqual((e["unsafe"], e["wrong_but_valid"]["count"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
