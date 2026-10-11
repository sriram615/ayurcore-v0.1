"""
S1-OOD v1 dataset and evaluator (no final OOD evaluation is run here).

Dataset: valid against the grader's schema and the OOD gold checks; manifest and
fingerprint match; category and phenomenon coverage; no case-ID or note overlap
with any S1 split; S1 test hash unchanged; duplicate IDs and corrupted gold are
detected. Evaluator: refuses S1 files (accidental substitution) and the official
OOD file without final=True, before loading a model; on a small FIXTURE dataset
written here (never the OOD file) it reports T1 / T2 / T3 and classifies
confident errors and correct abstentions correctly.
"""
import copy
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extractor import evaluate_ood

_spec = importlib.util.spec_from_file_location("s1_ood_dataset", ROOT / "scripts" / "s1_ood_dataset.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

S1 = ROOT / "eval" / "benchmarks" / "s1"
OOD = tool.load()


def _norm(text):
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())


def _s1_cases(split):
    with open(S1 / f"s1_{split}.json", encoding="utf-8") as f:
        return json.load(f)["cases"]


class DatasetTests(unittest.TestCase):
    def test_valid_and_matches_manifest(self):
        self.assertEqual(tool.validate_cases(OOD), [])
        self.assertEqual(tool.verify(), [])
        manifest = json.loads(tool.MANIFEST_FILE.read_text(encoding="utf-8"))
        self.assertEqual(manifest["case_count"], len(OOD["cases"]))
        self.assertEqual(manifest["dataset_fingerprint_sha256"], tool.fingerprint(OOD["cases"]))
        self.assertIn("NOT CLINICAL VALIDATION", manifest["classification"])
        self.assertTrue(all(c["provenance"]["status"] == "synthetic_unreviewed" for c in OOD["cases"]))

    def test_coverage(self):
        cats = [c["ood"]["category"] for c in OOD["cases"]]
        for category in tool.CATEGORIES:
            self.assertGreaterEqual(cats.count(category), 5, category)
        phenomena = {p for c in OOD["cases"] for p in c["ood"]["phenomena"]}
        for needed in ("common_name_alias", "botanical_alias", "malayalam_script", "manglish", "replacement_swap",
                       "negation_withhold", "unstocked_mention", "history_with_quantity", "decimal_kg", "number_words",
                       "shared_three_herbs", "self_correction", "body_weight_kg", "newline_list", "no_spaces",
                       "quantity_before_herb", "range", "unitless", "no_quantity", "unknown_herb"):
            self.assertIn(needed, phenomena)
        reasons = {a["reason"] for c in OOD["cases"] for a in c["ood"]["abstain"]}
        self.assertGreaterEqual(len(reasons), 5)

    def test_no_overlap_with_any_s1_split(self):
        # Reads S1 test only to compare ids/notes for leakage -- no model is run on it.
        s1 = [c for split in ("train", "dev", "test") for c in _s1_cases(split)]
        self.assertFalse({c["case_id"] for c in OOD["cases"]} & {c["case_id"] for c in s1})
        self.assertFalse({c["notes"] for c in OOD["cases"]} & {c["notes"] for c in s1})
        self.assertFalse({_norm(c["notes"]) for c in OOD["cases"]} & {_norm(c["notes"]) for c in s1})

    def test_s1_test_hash_unchanged(self):
        manifest = json.loads((S1 / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(evaluate_ood._sha256(S1 / "s1_test.json"), manifest["split_file_sha256"]["s1_test.json"])

    def test_duplicate_ids_detected(self):
        data = copy.deepcopy(OOD)
        data["cases"][1]["case_id"] = data["cases"][0]["case_id"]
        self.assertTrue([p for p in tool.validate_cases(data) if "duplicate" in p])

    def test_corrupted_gold_detected(self):
        def problems(mutate):
            data = copy.deepcopy(OOD)
            mutate(data["cases"])
            return tool.validate_cases(data)

        clean = next(i for i, c in enumerate(OOD["cases"]) if c["case_id"] == "ood-clean-03")
        abstain = next(i for i, c in enumerate(OOD["cases"]) if c["case_id"] == "ood-abstain-01")
        unstocked = next(i for i, c in enumerate(OOD["cases"]) if c["case_id"] == "ood-distractor-01")
        mutations = {
            "herb not matching spans": lambda cs: cs[clean]["expected"]["herbs"].update({"Brahmi": {"grams": 10, "tolerance_grams": 0}}),
            "non-canonical herb": lambda cs: cs[clean]["expected"]["herbs"].update({"neem leaf": {"grams": 10, "tolerance_grams": 0}}),
            "grams out of range": lambda cs: cs[clean]["expected"]["herbs"]["Neem"].update(grams=0),
            "span text missing": lambda cs: cs[clean]["ood"]["spans"].append(["Kutki", "HERB_RX", 0, "Kutki"]),
            "unknown span type": lambda cs: cs[clean]["ood"]["spans"][0].__setitem__(1, "HERB"),
            "abstain herb also expected": lambda cs: cs[abstain]["expected"]["herbs"].update({"Ashwagandha": {"grams": 25, "tolerance_grams": 0}}),
            "dosha without span": lambda cs: cs[clean]["ood"]["spans"].pop(0),
            "unstocked that resolves": lambda cs: cs[unstocked]["ood"]["spans"][1].__setitem__(3, "Neem"),
            "bad category": lambda cs: cs[clean]["ood"].update(category="other"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                self.assertTrue(problems(mutate), label)

    def test_tampered_file_fails_manifest_verification(self):
        data = copy.deepcopy(OOD)
        data["cases"][0]["expected"]["herbs"]["Ashwagandha"]["grams"] = 101  # still schema-valid
        self.assertEqual(tool.validate_cases(data), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "s1_ood_v1.json"
            path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            found = tool.verify(path, tool.MANIFEST_FILE)
        self.assertTrue([p for p in found if "fingerprint" in p or "file_sha256" in p])


FIXTURE_CASES = [
    # (id, note, gold herbs, abstain [(herb, reason)], spans)
    ("ood-fixture-01", "Give Neem 40 g.", {"Neem": 40}, [],
     [["Neem", "HERB_RX", 0, "Neem"], ["40 g", "QTY"]]),
    ("ood-fixture-02", "Give Brahmi 40.", {}, [("Brahmi", "unitless")],
     [["Brahmi", "HERB_RX", 0, "Brahmi"], ["40", "QTY"]]),
    ("ood-fixture-03", "Give Kutki 40 g.", {"Kutki": 50}, [],   # deliberately mis-labelled: forces a wrong-quantity error
     [["Kutki", "HERB_RX", 0, "Kutki"], ["40 g", "QTY"]]),
    ("ood-fixture-04", "Give Arjuna 30 g.", {}, [("Arjuna", "fixture")],  # emitted -> missed abstention
     [["Arjuna", "HERB_RX", 0, "Arjuna"], ["30 g", "QTY"]]),
]


def _fixture_dataset(path):
    cases = [{"case_id": cid, "label": "abstention" if abst else "clean",
              "provenance": {"status": "synthetic_unreviewed", "origin": "test fixture", "source": "tests",
                             "source_label": "fixture", "clinician_reviewer": None, "reviewed_at": None},
              "tags": ["fixture"], "notes": note, "ambiguous": False, "ambiguity_reason": None,
              "expected": {"dosha": [], "herbs": {h: {"grams": g, "tolerance_grams": 0} for h, g in gold.items()}},
              "mentioned_not_stocked": [], "safety_annotations": [], "annotation_notes": None, "source_rationale": None,
              "ood": {"category": "abstention" if abst else "clean", "phenomena": ["fixture"], "spans": spans,
                      "abstain": [{"herb": h, "reason": r} for h, r in abst]}}
             for cid, note, gold, abst, spans in FIXTURE_CASES]
    data = {"schema_version": "ayurcore.clinical_eval.cases/v1", "corpus_id": "s1_ood_fixture",
            "evaluation_class": "technical_synthetic_unreviewed", "report_label": "NOT CLINICAL VALIDATION -- fixture",
            "cases": cases}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class EvaluatorTests(unittest.TestCase):
    MISSING_MODEL = "/nonexistent/model.json"  # refusals must happen before any model is loaded

    def test_refuses_s1_splits_and_s1_content(self):
        # The substitution guard itself must fire (not just the later OOD validation).
        for split in ("dev", "test", "train"):
            with self.assertRaisesRegex(evaluate_ood.OODEvaluationRefused, "is an S1 split"):
                evaluate_ood.run(S1 / f"s1_{split}.json", self.MISSING_MODEL)
        with tempfile.TemporaryDirectory() as tmp:
            renamed = pathlib.Path(tmp) / "my_ood_cases.json"
            renamed.write_bytes((S1 / "s1_dev.json").read_bytes())
            with self.assertRaisesRegex(evaluate_ood.OODEvaluationRefused, "same content as an S1 split"):
                evaluate_ood.run(renamed, self.MISSING_MODEL)
            relabelled = pathlib.Path(tmp) / "relabelled.json"  # S1 notes dressed up as OOD -> fails OOD validation
            data = json.loads((S1 / "s1_dev.json").read_text(encoding="utf-8"))
            data["corpus_id"] = "s1_ood_fake"
            relabelled.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(evaluate_ood.OODEvaluationRefused, "dataset invalid"):
                evaluate_ood.run(relabelled, self.MISSING_MODEL)

    def test_refuses_the_official_set_without_final(self):
        with mock.patch("extractor.pipeline.Extractor") as extractor:
            with self.assertRaises(evaluate_ood.OODEvaluationRefused):
                evaluate_ood.run(tool.DATA_FILE, self.MISSING_MODEL)
            extractor.assert_not_called()

    def test_fixture_evaluation_reports_all_layers_and_classifies_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            from extractor import train as t1
            t1.train(tmp / "t1")
            _fixture_dataset(tmp / "fixture.json")
            summary = evaluate_ood.run(tmp / "fixture.json", tmp / "t1" / "model.json", tmp / "out")
            cases = json.loads((tmp / "out" / "s1_ood_fixture_cases.json").read_text(encoding="utf-8"))
        self.assertFalse(summary["final_run"])
        self.assertIn("NOT CLINICAL VALIDATION", summary["classification"])
        for key in ("t1", "t2", "t3", "outcomes"):
            self.assertIn(key, summary)
        self.assertEqual(summary["t3"]["verified"] + summary["t3"]["rejected_abstained"], 4)
        by_id = {c["case_id"]: c["outcomes"] for c in cases}
        self.assertEqual(by_id["ood-fixture-01"], {"correct_fact": 1})
        self.assertEqual(by_id["ood-fixture-02"], {"correct_abstention": 1})
        self.assertEqual(by_id["ood-fixture-03"], {"confident_error:wrong_quantity": 1})
        self.assertEqual(by_id["ood-fixture-04"], {"confident_error:missed_abstention": 1})
        self.assertEqual(summary["outcomes"]["confident_errors"], 2)
        self.assertEqual(summary["t1"]["spans_micro"]["tp"] + summary["t1"]["spans_micro"]["fn"], 8)


if __name__ == "__main__":
    unittest.main()
