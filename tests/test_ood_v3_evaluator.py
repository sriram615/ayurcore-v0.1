"""
S1-OOD v3 evaluator (extractor/evaluate_ood_v3.py) -- the official v3 file is NEVER evaluated here.

It wraps a private copy of the v2 evaluator (unchanged) pointed at the v3 tool. Refusals
happen before any model loads: official v3 without final=True (also a renamed copy),
official v3 when results exist (one-shot lock), S1 / v1 / v2 files. A two-case v3-format
FIXTURE is evaluated end to end with a model trained in a temporary directory.
"""
import hashlib
import importlib.util
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extractor import evaluate_ood_v3 as ev

_spec = importlib.util.spec_from_file_location("s1_ood_v3_dataset_t", ROOT / "scripts" / "s1_ood_v3_dataset.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

MISSING_MODEL = "/nonexistent/model.json"
V2_EVALUATOR_SHA256 = "ad66cc45"  # prefix recorded at the official v2 run


def _case(cid, note, gold, spans, rules):
    return {"case_id": cid, "label": "clean_control",
            "provenance": {"status": "synthetic_unreviewed", "origin": "test fixture", "source": "tests",
                           "source_label": "fixture", "clinician_reviewer": None, "reviewed_at": None},
            "tags": ["fixture"], "notes": note, "ambiguous": False, "ambiguity_reason": None,
            "expected": {"dosha": ["Vata"], "herbs": {h: {"grams": g, "tolerance_grams": 0} for h, g in gold.items()}},
            "mentioned_not_stocked": [], "safety_annotations": [], "annotation_notes": None, "source_rationale": None,
            "ood": {"category": "clean_control", "phenomena": ["english", "fixture"], "spans": spans, "abstain": []},
            "safety": {"target_rules": rules, "unaccounted": []}}


class RefusalTests(unittest.TestCase):
    def test_official_file_needs_final_and_runs_once(self):
        with mock.patch("extractor.pipeline.Extractor") as extractor, tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ev.OODv3EvaluationRefused, "one-shot"):
                ev.run(tool.DATA_FILE, MISSING_MODEL, tmp)
            renamed = pathlib.Path(tmp) / "renamed.json"
            shutil.copyfile(tool.DATA_FILE, renamed)
            with self.assertRaisesRegex(ev.OODv3EvaluationRefused, "one-shot"):
                ev.run(renamed, MISSING_MODEL, tmp)
            (pathlib.Path(tmp) / "s1_ood_v3_summary.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ev.OODv3EvaluationRefused, "S1-OOD v3 was already evaluated") as ctx:
                ev.run(tool.DATA_FILE, MISSING_MODEL, tmp, final=True)
            self.assertNotIn("v2", str(ctx.exception))
            extractor.assert_not_called()

    def test_command_line_refusal_names_v3(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stderr") as err:
            self.assertEqual(ev.main(["--out", tmp]), 2)
        written = "".join(call.args[0] for call in err.write.call_args_list)
        self.assertIn("S1-OOD v3 is a one-shot holdout", written)
        self.assertNotIn("S1-OOD v2", written)

    def test_other_files_are_refused(self):
        for rel in ("s1/s1_dev.json", "s1_ood/s1_ood_v1.json", "s1_ood_v2/s1_ood_v2.json"):
            with self.subTest(rel), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ev.OODv3EvaluationRefused, "dataset invalid"):
                    ev.run(ROOT / "eval" / "benchmarks" / rel, MISSING_MODEL, tmp, final=True)

    def test_v2_evaluator_unchanged_and_points_at_v2(self):
        digest = hashlib.sha256((ROOT / "extractor" / "evaluate_ood_v2.py").read_bytes()).hexdigest()
        self.assertTrue(digest.startswith(V2_EVALUATOR_SHA256))
        from extractor import evaluate_ood_v2
        self.assertEqual(evaluate_ood_v2.DATASET_TOOL_PATH.name, "s1_ood_v2_dataset.py")
        self.assertEqual(ev.DEFAULT_OUT.name, "ood_v3_eval")


class FixtureTests(unittest.TestCase):
    def test_end_to_end_on_a_fixture(self):
        from extractor import train as t1
        cases = [_case("ood3-fix-01", "Vata. Neem 40 g.", {"Neem": 40},
                       [["Vata", "DOSHA"], ["Neem", "HERB_RX", 0, "Neem"], ["40 g", "QTY"]], ["control"]),
                 _case("ood3-fix-02", "Vata. Brahmi 20 g.", {"Brahmi": 20},
                       [["Vata", "DOSHA"], ["Brahmi", "HERB_RX", 0, "Brahmi"], ["20 g", "QTY"]], ["control"])]
        data = {"schema_version": "ayurcore.clinical_eval.cases/v1", "corpus_id": "s1_ood_v3",
                "evaluation_class": "technical_synthetic_unreviewed", "report_label": "NOT CLINICAL VALIDATION -- fixture",
                "cases": cases}
        self.assertEqual(tool.validate_cases(data), [])
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            t1.train(tmp / "t1")
            (tmp / "fixture.json").write_text(json.dumps(data), encoding="utf-8")
            summary = ev.run(tmp / "fixture.json", tmp / "t1" / "model.json", tmp / "out")
            self.assertTrue((tmp / "out" / "s1_ood_v3_cases.json").exists())
        self.assertEqual(summary["dataset"], "s1_ood_v3")
        self.assertFalse(summary["official_one_shot"])
        self.assertIn("scripts/s1_ood_v3_dataset.py", summary["source_sha256"])
        self.assertEqual(summary["final"]["exact"]["of"], 2)


if __name__ == "__main__":
    unittest.main()
