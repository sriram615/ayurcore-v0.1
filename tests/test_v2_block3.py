"""
AI-2 v2 Block 3: temperature-scaled candidate confidence (memory/confidence.py) and the opt-in
confidence gate of memory/v2.py. The gate is OFF by default (Block 3 recommended REJECT for it).
"""
import importlib.util
import math
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory.confidence import GRID, TEMPERATURE, THRESHOLD, calibrated, fit_temperature, nll
from memory.planner import Planner
from memory.v2 import V2Planner, answer_with_snapshot_v2


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s3 = _load("s3_generator_b3", ROOT / "scripts" / "s3_generator.py")
devcal = _load("v2_devcal_b3", ROOT / "scripts" / "v2_devcal.py")
evc = _load("evaluate_v2_confidence_b3", ROOT / "memory" / "evaluate_v2_confidence.py")
PLANNER = Planner.train([(q["question"], q["frame"]) for q in s3.load("train")], 1, 13)
DEFAULT = V2Planner(PLANNER)
GATED = V2Planner(PLANNER, confidence_gate="threshold")
LOW = "avasanathe kurippadiyil enthokke undaayirunnu?"  # single consistent reading, calibrated confidence ~0.002
# (the Block 3 example "On which date did she first come in?" is now read correctly by Block 4B C1)


class CalibrationTests(unittest.TestCase):
    def test_temperature_refits_on_dev_only(self):
        groups = evc.fit_groups(devcal.load("dev"), PLANNER)
        self.assertEqual(len(groups), 86)  # 84 under Block 2A; Block 4B lets two more dev questions be planned
        self.assertAlmostEqual(fit_temperature(groups), TEMPERATURE, places=3)
        self.assertLess(nll(groups, TEMPERATURE), nll(groups, 1.0))
        self.assertIn(round(TEMPERATURE, 4), {round(t, 4) for t in GRID})

    def test_calibrated_distribution(self):
        scores = [30.0, 12.0, -4.0]
        for t in (1.0, TEMPERATURE, 100.0):
            p = calibrated(scores, t)
            self.assertAlmostEqual(sum(p), 1.0)
            self.assertEqual(sorted(range(3), key=lambda i: -p[i]), [0, 1, 2])
        self.assertGreater(calibrated(scores, 1.0)[0], calibrated(scores, TEMPERATURE)[0])
        z = sum(math.exp(s - 30.0) for s in scores)
        self.assertAlmostEqual(calibrated(scores, 1.0)[0], 1 / z)
        self.assertEqual(calibrated([]), [])

    def test_candidates_carry_calibrated_probability(self):
        interp = DEFAULT.interpret("How many prescriptions does she have?")["interpretation"]
        probs = [c["calibrated_probability"] for c in interp["candidates"]]
        self.assertAlmostEqual(sum(probs), 1.0, places=4)
        self.assertIn("calibrated_confidence", interp)


class GateTests(unittest.TestCase):
    SAMPLE = [c["question"] for c in devcal.load("dev")][::2] + [LOW, "ethra Brahmi koduthu?", "Should we stop Neem?",
                                                                  "Compare her Brahmi with the patient seen before her."]

    def test_off_by_default(self):
        self.assertIsNone(DEFAULT.confidence_gate)
        self.assertEqual(DEFAULT.interpret(LOW)["outcome"], "plan")

    def test_low_confidence_reading_is_confirmed_not_answered(self):
        out = GATED.interpret(LOW)
        self.assertEqual((out["outcome"], out["stage"], out["reason"]), ("clarify", "confidence", "LOW_CONFIDENCE"))
        self.assertEqual([o["query"] for o in out["options"]], [DEFAULT.interpret(LOW)["plan"]["query"]])
        self.assertLess(out["interpretation"]["calibrated_confidence"], THRESHOLD)
        strict = V2Planner(PLANNER, confidence_gate="threshold", clarify=False)
        self.assertEqual(strict.interpret(LOW)["outcome"], "abstain")
        self.assertEqual(V2Planner(PLANNER, confidence_gate="top1").interpret(LOW)["outcome"], "clarify")

    def test_gate_only_withholds_single_readings(self):
        for gate in ("threshold", "top1"):
            v = V2Planner(PLANNER, confidence_gate=gate)
            for q in self.SAMPLE:
                a, b = DEFAULT.interpret(q), v.interpret(q)
                with self.subTest(gate=gate, q=q):
                    if a["outcome"] != "plan":
                        self.assertEqual((a["outcome"], a["stage"], a["reason"], a["options"]),
                                         (b["outcome"], b["stage"], b["reason"], b["options"]))
                    else:
                        self.assertIn(b["outcome"], ("plan", "clarify"))
                        if b["outcome"] == "plan":
                            self.assertEqual(b["plan"], a["plan"])

    def test_invalid_gate(self):
        with self.assertRaises(ValueError):
            V2Planner(PLANNER, confidence_gate="sometimes")

    def test_withheld_answer_reads_no_record_and_carries_no_claim(self):
        snaps = devcal.snapshots()
        out = answer_with_snapshot_v2(snaps["s2_clinic_a/P03"], LOW, GATED)
        self.assertEqual(out["outcome"], "clarify")
        self.assertIsNone(out["claim"])


class PreviousErrorProbeTests(unittest.TestCase):
    """Earlier failure patterns: no gate may turn them into a wrong answer."""

    def test_probes(self):
        for q, expected in evc.PROBES:
            for name, cfg in evc.ARMS.items():
                out = V2Planner(PLANNER, **cfg).interpret(q)
                with self.subTest(arm=name, q=q):
                    if expected.startswith("not an answer"):
                        self.assertNotEqual(out["outcome"], "plan")
                    elif out["outcome"] == "plan":
                        wanted = expected.split()[0] if expected.split()[0].isupper() else expected.split()[1]
                        self.assertEqual(out["plan"]["frame"], wanted)


if __name__ == "__main__":
    unittest.main()
