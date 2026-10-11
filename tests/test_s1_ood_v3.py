"""
S1-OOD v3 final safety benchmark: integrity only (the extractor is NOT run on it here).

Valid against the case-file schema and the v2-format gold checks; frozen (file and
fingerprint match the manifest, the freeze command refuses to rewrite it); balanced
coverage of R1-R5, cross-rule, controls and Malayalam/Manglish; no case-ID or note
overlap (exact, normalised, near-duplicate) with S1, S1-OOD v1, S1-OOD v2 or the
notes used to develop and test T3; corrupted gold is detected; v1, v2 and S1 test are
unchanged; the v1 and v2 evaluators refuse the v3 file before loading a model.
"""
import copy
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extractor import evaluate_ood, evaluate_ood_v2


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load(ROOT / "scripts" / "s1_ood_v3_dataset.py", "s1_ood_v3_dataset")
V3 = tool.load()
FROZEN_FINGERPRINT = "0d8e9f9ad2699c1ce4969bc6eef722f24c6036c2bb24b4b7a8c1fba9b38ed49e"
V1_FILE_SHA256 = "0b81096edcc303314f70a48c7e406445ecc8339df761f8b84a20dab0cbb10701"
V2_FILE_SHA256 = "0d8e782e08966b637c3bde1a10d8b74ab1390ec0957ac2e81bb4dcb60e69b55a"
NEAR_DUPLICATE_JACCARD = 0.6


def _norm(text):
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())


def _reference_notes():
    ref = {}
    for split in ("train", "dev", "test"):  # S1 test read only for this comparison
        for c in json.loads((ROOT / "eval" / "benchmarks" / "s1" / f"s1_{split}.json").read_text(encoding="utf-8"))["cases"]:
            ref[c["notes"]] = (f"s1_{split}", c["case_id"])
    for rel, tag in (("s1_ood/s1_ood_v1.json", "s1_ood_v1"), ("s1_ood_v2/s1_ood_v2.json", "s1_ood_v2")):
        for c in json.loads((ROOT / "eval" / "benchmarks" / rel).read_text(encoding="utf-8"))["cases"]:
            ref[c["notes"]] = (tag, c["case_id"])
    context = _load(ROOT / "tests" / "test_t3_context.py", "t3_context_ref_v3")
    integration = _load(ROOT / "tests" / "test_t3_integration.py", "t3_integration_ref_v3")
    for row in context.CASES + context.WARNING_CASES + integration.REJECT + integration.ACCEPT:
        ref[row[1]] = ("t3_dev_tests", row[0])
    return ref


class FrozenDatasetTests(unittest.TestCase):
    def test_valid_and_matches_frozen_manifest(self):
        self.assertEqual(tool.validate_cases(V3), [])
        self.assertEqual(tool.verify(), [])
        manifest = json.loads(tool.MANIFEST_FILE.read_text(encoding="utf-8"))
        self.assertEqual(manifest["dataset"], "s1_ood_v3")
        self.assertEqual(manifest["dataset_fingerprint_sha256"], FROZEN_FINGERPRINT)
        self.assertEqual(tool.fingerprint(V3["cases"]), FROZEN_FINGERPRINT)
        self.assertIn("NOT CLINICAL VALIDATION", manifest["classification"])

    def test_freeze_refuses_to_rewrite_the_manifest(self):
        before = tool.MANIFEST_FILE.read_bytes()
        self.assertEqual(tool.main(["freeze"]), 1)
        self.assertEqual(tool.MANIFEST_FILE.read_bytes(), before)

    def test_tool_reads_v3_not_v2(self):
        self.assertEqual(V3["corpus_id"], "s1_ood_v3")
        self.assertTrue(all(c["case_id"].startswith("ood3-") for c in V3["cases"]))

    def test_tampered_file_fails_verification(self):
        data = copy.deepcopy(V3)
        case = next(c for c in data["cases"] if c["expected"]["herbs"])
        herb = next(iter(case["expected"]["herbs"]))
        case["expected"]["herbs"][herb]["grams"] += 1
        self.assertEqual(tool.validate_cases(data), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "s1_ood_v3.json"
            path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            found = tool.verify(path, tool.MANIFEST_FILE)
        self.assertTrue([p for p in found if "fingerprint" in p or "file_sha256" in p])


class CoverageTests(unittest.TestCase):
    def test_size_and_balanced_categories(self):
        cases = V3["cases"]
        self.assertTrue(55 <= len(cases) <= 70, len(cases))
        counts = {c: sum(1 for x in cases if x["ood"]["category"] == c) for c in tool.CATEGORIES}
        self.assertGreaterEqual(min(counts.values()), 6, counts)
        self.assertLessEqual(max(counts.values()), 2 * min(counts.values()), counts)

    def test_rules_controls_languages(self):
        cases = V3["cases"]
        for rule in ("R1", "R2", "R3", "R4", "R5"):
            self.assertGreaterEqual(sum(rule in c["safety"]["target_rules"] for c in cases), 8, rule)
        self.assertGreaterEqual(sum("control" in c["safety"]["target_rules"] for c in cases), 15)
        for lang in ("manglish", "malayalam_script"):
            self.assertGreaterEqual(sum(lang in c["ood"]["phenomena"] for c in cases), 3, lang)
        self.assertEqual({a["reason"] for c in cases for a in c["ood"]["abstain"]}, set(tool.ABSTAIN_REASONS))


class OverlapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = _reference_notes()

    def test_no_case_id_overlap(self):
        self.assertFalse({c["case_id"] for c in V3["cases"]} & {v[1] for v in self.ref.values()})

    def test_no_exact_or_normalised_overlap(self):
        notes = [c["notes"] for c in V3["cases"]]
        self.assertFalse(set(notes) & set(self.ref))
        self.assertFalse({_norm(n) for n in notes} & {_norm(n) for n in self.ref})
        self.assertEqual(len({_norm(n) for n in notes}), len(notes))

    def test_no_near_duplicates(self):
        ref = [set(_norm(n).split()) for n in self.ref]
        for c in V3["cases"]:
            words = set(_norm(c["notes"]).split())
            with self.subTest(c["case_id"]):
                self.assertLess(max(len(words & r) / len(words | r) for r in ref), NEAR_DUPLICATE_JACCARD)


class CorruptedGoldTests(unittest.TestCase):
    def test_corruptions_are_detected(self):
        idx = {c["case_id"]: i for i, c in enumerate(V3["cases"])}
        clean, alt = idx["ood3-ctl-01"], idx["ood3-r2-01"]
        mutations = {
            "duplicate id": lambda d: d["cases"][1].update(case_id=d["cases"][0]["case_id"]),
            "v2 prefix": lambda d: d["cases"][clean].update(case_id="ood2-ctl-99"),
            "v2 corpus": lambda d: d.update(corpus_id="s1_ood_v2"),
            "bad category": lambda d: d["cases"][clean]["ood"].update(category="clean"),
            "unknown rule": lambda d: d["cases"][clean]["safety"].update(target_rules=["R7"]),
            "abstain reason invalid": lambda d: d["cases"][alt]["ood"]["abstain"][0].update(reason="perhaps"),
            "herb not matching spans": lambda d: d["cases"][clean]["expected"]["herbs"].update(
                {"Neem": {"grams": 10, "tolerance_grams": 0}}),
            "span text missing": lambda d: d["cases"][clean]["ood"]["spans"].append(["Kutki", "HERB_RX", 0, "Kutki"]),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                data = copy.deepcopy(V3)
                mutate(data)
                self.assertTrue(tool.validate_cases(data), label)


class IsolationTests(unittest.TestCase):
    def test_earlier_sets_unchanged(self):
        self.assertEqual(evaluate_ood._sha256(ROOT / "eval" / "benchmarks" / "s1_ood" / "s1_ood_v1.json"), V1_FILE_SHA256)
        self.assertEqual(evaluate_ood._sha256(ROOT / "eval" / "benchmarks" / "s1_ood_v2" / "s1_ood_v2.json"), V2_FILE_SHA256)
        s1 = json.loads((ROOT / "eval" / "benchmarks" / "s1" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(evaluate_ood._sha256(ROOT / "eval" / "benchmarks" / "s1" / "s1_test.json"),
                         s1["split_file_sha256"]["s1_test.json"])

    def test_v1_and_v2_evaluators_refuse_v3(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(evaluate_ood.OODEvaluationRefused):
                evaluate_ood.run(tool.DATA_FILE, "/nonexistent/model.json", tmp)
            with self.assertRaises(evaluate_ood_v2.OODv2EvaluationRefused):
                evaluate_ood_v2.run(tool.DATA_FILE, "/nonexistent/model.json", tmp, final=True)


if __name__ == "__main__":
    unittest.main()
