"""
S1-OOD v2 evaluator (extractor/evaluate_ood_v2.py) -- the official v2 file is NEVER evaluated here.

Refusals happen before any model is loaded: the official file (or a renamed copy)
without final=True, the official file when results already exist (one-shot lock),
and invalid / S1 / S1-OOD v1 files. Rejection classification is unit-tested. A
FIXTURE dataset written here is evaluated end to end with a model trained in a
temporary directory; two T3 rejections are injected around the real verifier to
check the catch / false-rejection bookkeeping. The v1 evaluator is unchanged.
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

from extractor import evaluate_ood_v2 as ev

_spec = importlib.util.spec_from_file_location("s1_ood_v2_dataset_t", ROOT / "scripts" / "s1_ood_v2_dataset.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

MISSING_MODEL = "/nonexistent/model.json"
V1_EVALUATOR_SHA256 = "92b8025b7e4edd94f095cdfb90179fe18b10bc87501554800bfb526d8567fb6e"


def _case(cid, note, gold, dosha, spans, rules, abstain=(), unstocked=(), unaccounted=()):
    return {"case_id": cid, "label": "cross_rule",
            "provenance": {"status": "synthetic_unreviewed", "origin": "test fixture", "source": "tests",
                           "source_label": "fixture", "clinician_reviewer": None, "reviewed_at": None},
            "tags": ["fixture"], "notes": note, "ambiguous": False, "ambiguity_reason": None,
            "expected": {"dosha": dosha, "herbs": {h: {"grams": g, "tolerance_grams": 0} for h, g in gold.items()}},
            "mentioned_not_stocked": list(unstocked), "safety_annotations": [], "annotation_notes": None,
            "source_rationale": None,
            "ood": {"category": "cross_rule", "phenomena": ["english", "fixture"], "spans": spans,
                    "abstain": [{"herb": h, "reason": r} for h, r in abstain]},
            "safety": {"target_rules": rules, "unaccounted": [list(u) for u in unaccounted]}}


FIXTURE = [
    _case("ood2-fix-01", "Vata. Neem 40 g.", {"Neem": 40}, ["Vata"],
          [["Vata", "DOSHA"], ["Neem", "HERB_RX", 0, "Neem"], ["40 g", "QTY"]], ["control"]),
    _case("ood2-fix-02", "Kapha. Kutki 40 g.", {"Kutki": 50}, ["Kapha"],  # deliberately mis-labelled gold
          [["Kapha", "DOSHA"], ["Kutki", "HERB_RX", 0, "Kutki"], ["40 g", "QTY"]], ["R1"]),
    _case("ood2-fix-03", "Pitta. Arjuna 30 g.", {}, ["Pitta"],  # gold says abstain; a rejection is injected
          [["Pitta", "DOSHA"], ["Arjuna", "HERB_RX", 0, "Arjuna"], ["30 g", "QTY"]], ["R2"],
          abstain=[("Arjuna", "alternative_quantities")]),
    _case("ood2-fix-04", "Vata. Brahmi 20 g.", {"Brahmi": 20}, ["Vata"],  # correct; a rejection is injected
          [["Vata", "DOSHA"], ["Brahmi", "HERB_RX", 0, "Brahmi"], ["20 g", "QTY"]], ["control"]),
]
INJECT = {"Pitta. Arjuna 30 g.": "R2", "Vata. Brahmi 20 g.": "R3"}


def _fixture(path):
    data = {"schema_version": "ayurcore.clinical_eval.cases/v1", "corpus_id": "s1_ood_v2",
            "evaluation_class": "technical_synthetic_unreviewed", "report_label": "NOT CLINICAL VALIDATION -- fixture",
            "cases": FIXTURE}
    assert tool.validate_cases(data) == [], tool.validate_cases(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class RefusalTests(unittest.TestCase):
    def test_official_file_needs_final(self):
        with mock.patch("extractor.pipeline.Extractor") as extractor, tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ev.OODv2EvaluationRefused, "one-shot"):
                ev.run(tool.DATA_FILE, MISSING_MODEL, tmp)
            renamed = pathlib.Path(tmp) / "renamed.json"
            shutil.copyfile(tool.DATA_FILE, renamed)
            with self.assertRaisesRegex(ev.OODv2EvaluationRefused, "one-shot"):
                ev.run(renamed, MISSING_MODEL, tmp)
            extractor.assert_not_called()

    def test_official_file_is_never_evaluated_twice(self):
        with mock.patch("extractor.pipeline.Extractor") as extractor, tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "s1_ood_v2_summary.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ev.OODv2EvaluationRefused, "already evaluated"):
                ev.run(tool.DATA_FILE, MISSING_MODEL, tmp, final=True)
            extractor.assert_not_called()

    def test_s1_and_v1_files_are_refused(self):
        for path in (ROOT / "eval" / "benchmarks" / "s1" / "s1_dev.json",
                     ROOT / "eval" / "benchmarks" / "s1_ood" / "s1_ood_v1.json"):
            with self.subTest(path.name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(ev.OODv2EvaluationRefused, "dataset invalid"):
                    ev.run(path, MISSING_MODEL, tmp, final=True)

    def test_v1_evaluator_unchanged(self):
        digest = hashlib.sha256((ROOT / "extractor" / "evaluate_ood.py").read_bytes()).hexdigest()
        self.assertEqual(digest, V1_EVALUATOR_SHA256)


class ClassificationTests(unittest.TestCase):
    def test_rejection_effects(self):
        correct, abstain, wrong = FIXTURE[0], FIXTURE[2], FIXTURE[1]
        self.assertEqual(ev.classify_rejection(correct, {"Neem": 40}, ["Vata"]), "false_rejection")
        self.assertEqual(ev.classify_rejection(correct, {"Neem": 45}, ["Vata"]), "catch")         # wrong quantity
        self.assertEqual(ev.classify_rejection(correct, {"Neem": 40, "Kutki": 5}, ["Vata"]), "catch")  # spurious
        self.assertEqual(ev.classify_rejection(abstain, {"Arjuna": 30}, ["Pitta"]), "catch")      # missed abstention
        self.assertEqual(ev.classify_rejection(correct, {}, ["Vata"]), "incomplete")             # omission only
        self.assertEqual(ev.classify_rejection(correct, {"Neem": 40}, []), "incomplete")         # dosha omitted
        self.assertEqual(ev.classify_rejection(wrong, {"Kutki": 50}, ["Pitta"]), "catch")         # wrong dosha


class FixtureEvaluationTests(unittest.TestCase):
    def test_end_to_end_bookkeeping(self):
        from extractor import train as t1
        from extractor import verify as verify_module
        real = verify_module.verify

        def injected(note, spans, parsed, report):
            result = real(note, spans, parsed, report)
            if note in INJECT:
                result = {**result, "verdict": "rejected",
                          "violations": result["violations"] + [{"code": "injected", "rule": INJECT[note],
                                                                 "where": "test", "detail": ""}]}
            return result

        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            t1.train(tmp / "t1")
            _fixture(tmp / "fixture.json")
            with mock.patch.object(verify_module, "verify", injected):
                summary = ev.run(tmp / "fixture.json", tmp / "t1" / "model.json", tmp / "out")
            rows = {r["case_id"]: r for r in json.loads((tmp / "out" / "s1_ood_v2_cases.json").read_text(encoding="utf-8"))}
        self.assertFalse(summary["official_one_shot"])
        self.assertIn("NOT CLINICAL VALIDATION", summary["classification"])
        self.assertTrue(rows["ood2-fix-01"]["final_exact"])
        self.assertIn("confident_error:wrong_quantity", rows["ood2-fix-02"]["final_outcomes"])
        self.assertEqual(rows["ood2-fix-03"]["rejection_effect"], "catch")
        self.assertIn("correct_abstention", rows["ood2-fix-03"]["final_outcomes"])
        self.assertEqual(rows["ood2-fix-04"]["rejection_effect"], "false_rejection")
        self.assertEqual(summary["controls"]["false_rejections"], ["ood2-fix-04"])
        self.assertEqual((summary["t3"]["by_rule"]["R2"]["rejections"], summary["t3"]["by_rule"]["R3"]["rejections"]), (1, 1))
        self.assertEqual(summary["t3"]["confident_errors_prevented"], 1)
        self.assertEqual(summary["final"]["exact"]["of"], 4)
        for key in ("t1", "t2", "t3", "r5", "controls", "by_category", "source_sha256"):
            self.assertIn(key, summary)


if __name__ == "__main__":
    unittest.main()
