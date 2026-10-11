"""
S1-OOD v2 safety benchmark: integrity only (the extractor is NOT run on it here).

Valid against the case-file schema and the v2 gold checks; frozen (file and
fingerprint match the manifest, the freeze command refuses to rewrite it); balanced
coverage of R1-R5, cross-rule, controls and Malayalam/Manglish; no case-ID or note
overlap (exact, normalised, near-duplicate) with S1, S1-OOD v1 or the notes used
to develop R1-R5; corrupted gold is detected; S1-OOD v1 and S1 test unchanged; the
existing OOD evaluator refuses v2 before loading a model.
"""
import ast
import copy
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extractor import evaluate_ood


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = _load(ROOT / "scripts" / "s1_ood_v2_dataset.py", "s1_ood_v2_dataset")
V2 = tool.load()
FROZEN_FINGERPRINT = "48b7c2ffb4f7b6a871d83c0be0a44adf88eeaea7519726ce40cdead02e1c684e"
V1_FILE_SHA256 = "0b81096edcc303314f70a48c7e406445ecc8339df761f8b84a20dab0cbb10701"
NEAR_DUPLICATE_JACCARD = 0.6


def _norm(text):
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())


def _reference_notes():
    """{note: source} for S1 (all splits; test read only for this comparison), S1-OOD v1 and the R1-R5 dev tests."""
    ref = {}
    for split in ("train", "dev", "test"):
        for c in json.loads((ROOT / "eval" / "benchmarks" / "s1" / f"s1_{split}.json").read_text(encoding="utf-8"))["cases"]:
            ref[c["notes"]] = (f"s1_{split}", c["case_id"])
    for c in json.loads((ROOT / "eval" / "benchmarks" / "s1_ood" / "s1_ood_v1.json").read_text(encoding="utf-8"))["cases"]:
        ref[c["notes"]] = ("s1_ood_v1", c["case_id"])
    context = _load(ROOT / "tests" / "test_t3_context.py", "t3_context_ref")
    integration = _load(ROOT / "tests" / "test_t3_integration.py", "t3_integration_ref")
    for row in context.CASES + context.WARNING_CASES + integration.REJECT + integration.ACCEPT:
        ref[row[1]] = ("r1_r5_dev_tests", row[0])
    return ref


class FrozenDatasetTests(unittest.TestCase):
    def test_valid_and_matches_frozen_manifest(self):
        self.assertEqual(tool.validate_cases(V2), [])
        self.assertEqual(tool.verify(), [])
        manifest = json.loads(tool.MANIFEST_FILE.read_text(encoding="utf-8"))
        self.assertEqual(manifest["dataset_fingerprint_sha256"], FROZEN_FINGERPRINT)
        self.assertEqual(tool.fingerprint(V2["cases"]), FROZEN_FINGERPRINT)
        self.assertIn("NOT CLINICAL VALIDATION", manifest["classification"])
        self.assertTrue(all(c["provenance"]["status"] == "synthetic_unreviewed" for c in V2["cases"]))

    def test_freeze_refuses_to_rewrite_the_manifest(self):
        before = tool.MANIFEST_FILE.read_bytes()
        self.assertEqual(tool.main(["freeze"]), 1)
        self.assertEqual(tool.MANIFEST_FILE.read_bytes(), before)

    def test_tampered_file_fails_verification(self):
        data = copy.deepcopy(V2)
        case = next(c for c in data["cases"] if c["expected"]["herbs"])
        herb = next(iter(case["expected"]["herbs"]))
        case["expected"]["herbs"][herb]["grams"] += 1  # still schema-valid
        self.assertEqual(tool.validate_cases(data), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "s1_ood_v2.json"
            path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
            found = tool.verify(path, tool.MANIFEST_FILE)
        self.assertTrue([p for p in found if "fingerprint" in p or "file_sha256" in p])


class CoverageTests(unittest.TestCase):
    def test_size_and_balanced_categories(self):
        cases = V2["cases"]
        self.assertTrue(60 <= len(cases) <= 80, len(cases))
        counts = {c: sum(1 for x in cases if x["ood"]["category"] == c) for c in tool.CATEGORIES}
        self.assertGreaterEqual(min(counts.values()), 7, counts)
        self.assertLessEqual(max(counts.values()), 2 * min(counts.values()), counts)

    def test_every_rule_controls_and_languages_covered(self):
        cases = V2["cases"]
        for rule in ("R1", "R2", "R3", "R4", "R5"):
            self.assertGreaterEqual(sum(rule in c["safety"]["target_rules"] for c in cases), 8, rule)
        self.assertGreaterEqual(sum("control" in c["safety"]["target_rules"] for c in cases), 15)
        self.assertGreaterEqual(sum(len(c["safety"]["target_rules"]) - ("control" in c["safety"]["target_rules"]) >= 2
                                    for c in cases), 8)  # cross-rule
        for lang in ("manglish", "malayalam_script"):
            self.assertGreaterEqual(sum(lang in c["ood"]["phenomena"] for c in cases), 5, lang)
        reasons = {a["reason"] for c in cases for a in c["ood"]["abstain"]}
        self.assertEqual(reasons, set(tool.ABSTAIN_REASONS))
        self.assertGreaterEqual(sum(len(c["safety"]["unaccounted"]) for c in cases), 6)


class OverlapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ref = _reference_notes()

    def test_no_case_id_overlap(self):
        self.assertFalse({c["case_id"] for c in V2["cases"]} & {v[1] for v in self.ref.values()})

    def test_no_exact_or_normalised_note_overlap(self):
        notes = [c["notes"] for c in V2["cases"]]
        self.assertFalse(set(notes) & set(self.ref))
        self.assertFalse({_norm(n) for n in notes} & {_norm(n) for n in self.ref})
        self.assertEqual(len({_norm(n) for n in notes}), len(notes), "duplicate notes inside v2")

    def test_no_near_duplicates(self):
        ref = [set(_norm(n).split()) for n in self.ref]
        for c in V2["cases"]:
            words = set(_norm(c["notes"]).split())
            closest = max(len(words & r) / len(words | r) for r in ref)
            with self.subTest(c["case_id"]):
                self.assertLess(closest, NEAR_DUPLICATE_JACCARD)


class CorruptedGoldTests(unittest.TestCase):
    def test_corruptions_are_detected(self):
        def index(cid):
            return next(i for i, c in enumerate(V2["cases"]) if c["case_id"] == cid)

        clean, alt, r5, ctl = index("ood2-ctl-01"), index("ood2-r2-04"), index("ood2-r5-01"), index("ood2-ctl-02")
        mutations = {
            "duplicate id": lambda d: d["cases"][1].update(case_id=d["cases"][0]["case_id"]),
            "wrong prefix": lambda d: d["cases"][clean].update(case_id="ood-ctl-01"),
            "wrong corpus": lambda d: d.update(corpus_id="s1_ood_v1"),
            "bad category": lambda d: d["cases"][clean]["ood"].update(category="clean"),
            "label differs": lambda d: d["cases"][clean].update(label="r1_conflict"),
            "unknown rule": lambda d: d["cases"][clean]["safety"].update(target_rules=["R9"]),
            "no rule": lambda d: d["cases"][clean]["safety"].update(target_rules=[]),
            "herb not matching spans": lambda d: d["cases"][clean]["expected"]["herbs"].update(
                {"Brahmi": {"grams": 10, "tolerance_grams": 0}}),
            "abstain reason invalid": lambda d: d["cases"][alt]["ood"]["abstain"][0].update(reason="maybe"),
            "abstain also expected": lambda d: d["cases"][alt]["expected"]["herbs"].update(
                {"Ashwagandha": {"grams": 60, "tolerance_grams": 0}}),
            "unaccounted not a quantity": lambda d: d["cases"][r5]["safety"].update(unaccounted=[["Bala", 0]]),
            "unstocked that resolves": lambda d: d["cases"][r5]["ood"]["spans"][3].__setitem__(3, "Neem"),
            "control requiring abstention": lambda d: d["cases"][ctl]["ood"].update(
                abstain=[{"herb": "Amalaki", "reason": "range"}]),
            "span text missing": lambda d: d["cases"][clean]["ood"]["spans"].append(["Kutki", "HERB_RX", 0, "Kutki"]),
            "dosha without span": lambda d: d["cases"][clean]["ood"]["spans"].pop(0),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                data = copy.deepcopy(V2)
                mutate(data)
                self.assertTrue(tool.validate_cases(data), label)


class IsolationTests(unittest.TestCase):
    def test_s1_ood_v1_and_s1_test_unchanged(self):
        v1 = _load(ROOT / "scripts" / "s1_ood_dataset.py", "s1_ood_v1_check")
        self.assertEqual(v1.verify(), [])
        self.assertEqual(evaluate_ood._sha256(v1.DATA_FILE), V1_FILE_SHA256)
        s1_manifest = json.loads((ROOT / "eval" / "benchmarks" / "s1" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(evaluate_ood._sha256(ROOT / "eval" / "benchmarks" / "s1" / "s1_test.json"),
                         s1_manifest["split_file_sha256"]["s1_test.json"])

    def test_existing_ood_evaluator_refuses_v2_before_loading_a_model(self):
        with self.assertRaises(evaluate_ood.OODEvaluationRefused):
            evaluate_ood.run(tool.DATA_FILE, "/nonexistent/model.json")

    def test_dataset_tool_does_not_import_the_extractor_or_verifier(self):
        tree = ast.parse((ROOT / "scripts" / "s1_ood_v2_dataset.py").read_text(encoding="utf-8"))
        modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse({m for m in modules if m.startswith("extractor")})


if __name__ == "__main__":
    unittest.main()
