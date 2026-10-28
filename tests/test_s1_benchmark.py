"""
S1 synthetic Notes -> Structured Prescription benchmark (scripts/s1_generator.py)
and its deterministic baseline (scripts/s1_baseline.py).

Proves: reproducibility (same seed, same bytes; frozen files match a fresh
generation), gold-label correctness (re-derived from each case's construction
record and checked against the note text), no split leakage, baseline
correctness on hand-written notes, and that Baseline, Control and A0 run on the
same cases and grader. No Gemini call is made: Control and A0 run in the
harness's existing --selfcheck mode (scripted responses).

NOT CLINICAL VALIDATION -- every case here is synthetic.
"""
import ast
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.herb_resolver import UnknownHerbError, normalize_herb
from scripts import s1_baseline as base
from scripts import s1_generator as gen

BENCH = ROOT / "eval" / "benchmarks" / "s1"
SPLIT_NAMES = ("train", "dev", "test")
HARNESS = base._harness()


def _load(split):
    return json.loads((BENCH / f"s1_{split}.json").read_text(encoding="utf-8"))


SPLITS = {name: _load(name) for name in SPLIT_NAMES}
ALL_CASES = [c for name in SPLIT_NAMES for c in SPLITS[name]["cases"]]


def _resolves(text):
    try:
        return normalize_herb(text)
    except UnknownHerbError:
        return None


def _grams_from_spec(mention):
    spec, form = mention["quantity_spec"], mention["quantity_form"]
    if form == "kg":
        return round(spec["kg"] * 1000)
    if form == "packs":
        return spec["count"] * spec["each_grams"]
    return spec["grams"]


class ReproducibilityTests(unittest.TestCase):
    def test_same_seed_gives_identical_dataset(self):
        self.assertEqual(gen.generate(), gen.generate())

    def test_different_seed_gives_a_different_dataset(self):
        self.assertNotEqual(gen.dataset_fingerprint(gen.generate(1)), gen.dataset_fingerprint(gen.generate(2)))

    def test_frozen_files_match_a_fresh_generation(self):
        self.assertEqual(gen.verify(str(BENCH)), [])

    def test_build_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            gen.build(tmp)
            for name in ("manifest.json", *(f"s1_{s}.json" for s in SPLIT_NAMES)):
                self.assertEqual((pathlib.Path(tmp) / name).read_bytes(), (BENCH / name).read_bytes(), name)

    def test_manifest_fingerprint_and_file_hashes(self):
        manifest = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))
        loaded = {name: SPLITS[name]["cases"] for name in SPLIT_NAMES}
        self.assertEqual(manifest["dataset_fingerprint_sha256"], gen.dataset_fingerprint(loaded))
        for name, sha in manifest["split_file_sha256"].items():
            self.assertEqual(HARNESS.sha256_file(BENCH / name), sha, name)
        self.assertEqual(manifest["herb_resolver_sha256"], HARNESS.sha256_file(HARNESS.HERB_RESOLVER_PATH))
        self.assertIn("NOT CLINICAL VALIDATION", manifest["classification"])


class GoldLabelTests(unittest.TestCase):
    def test_split_files_pass_the_harness_case_validation(self):
        for name in SPLIT_NAMES:
            data = HARNESS.load_case_file(BENCH / f"s1_{name}.json")
            self.assertEqual(data["evaluation_class"], "technical_synthetic_unreviewed")
            self.assertIn("NOT CLINICAL VALIDATION", data["report_label"])

    def test_every_case_is_marked_synthetic(self):
        for case in ALL_CASES:
            self.assertEqual(case["provenance"]["status"], "synthetic_unreviewed")
            self.assertIsNone(case["provenance"]["clinician_reviewer"])
            self.assertFalse(case["ambiguous"])
            self.assertEqual(case["safety_annotations"], [])

    def test_gold_label_is_rederived_from_the_construction_record_and_the_note(self):
        for case in ALL_CASES:
            with self.subTest(case=case["case_id"]):
                notes, meta, expected = case["notes"], case["s1_meta"], case["expected"]
                prescribed = [m for m in meta["mentions"] if m["role"] == "prescribed"]
                names = [m["herb"] for m in prescribed]
                self.assertEqual(len(names), len(set(names)), "a herb prescribed twice")
                self.assertEqual(set(expected["herbs"]), set(names))
                for m in prescribed:
                    self.assertIn(m["surface"], notes)
                    self.assertIn(m["quantity_text"], notes)
                    grams = _grams_from_spec(m)
                    self.assertEqual(expected["herbs"][m["herb"]], {"grams": grams, "tolerance_grams": 0})
                    self.assertTrue(isinstance(grams, int) and 1 <= grams <= 10000)
                    resolved = _resolves(m["surface"])
                    if resolved is None:  # only deliberate non-table spellings may fail to resolve
                        self.assertTrue({"misspelling", "inflection"} & set(case["tags"]), m["surface"])
                    else:
                        self.assertEqual(resolved, m["herb"])
                    if m.get("superseded_quantity_text"):
                        self.assertIn(m["superseded_quantity_text"], notes)
                for m in meta["mentions"]:
                    if m["role"] in ("negated", "history", "replaced"):
                        self.assertIn(m["herb"], gen.HERBS)
                        self.assertNotIn(m["herb"], expected["herbs"])
                        self.assertIn(m["surface"], notes)
                    if m["role"] == "unstocked":
                        self.assertIsNone(_resolves(m["herb"]))
                        self.assertIn(m["herb"], case["mentioned_not_stocked"])
                        self.assertIn(m["surface"], notes)
                self.assertEqual(sorted(case["mentioned_not_stocked"]),
                                 sorted({m["herb"] for m in meta["mentions"] if m["role"] == "unstocked"}))
                self.assertTrue(expected["dosha"])
                self.assertEqual(expected["dosha"], sorted(expected["dosha"], key=gen.DOSHAS.index))
                self.assertIn(meta["dosha_surface"].replace(".", ""), notes)

    def test_quantity_text_states_the_numbers_in_its_spec(self):
        for case in ALL_CASES:
            for m in case["s1_meta"]["mentions"]:
                if not m["quantity_text"]:
                    continue
                text, spec, form = m["quantity_text"], m["quantity_spec"], m["quantity_form"]
                if form == "kg":
                    self.assertTrue(text.startswith(f"{spec['kg']:g} "), text)
                elif form == "packs":
                    self.assertTrue(text.startswith(f"{spec['count']} ") and f" {spec['each_grams']}" in text, text)
                elif form == "words":
                    self.assertEqual(text, f"{gen.NUMBER_WORDS[spec['grams']]} grams")
                else:
                    self.assertTrue(text.startswith(str(spec["grams"])), text)

    def test_misspellings_are_not_in_the_production_alias_table(self):
        for herb, spellings in gen.MISSPELLINGS.items():
            for spelling in spellings:
                self.assertIsNone(_resolves(spelling), spelling)

    def test_categories_carry_their_phenomena(self):
        hard = {"negation", "history_mention", "unstocked_mention", "numeric_distractor", "misspelling",
                "inflection", "malayalam_script", "no_punctuation", "self_correction", "instead_of",
                "shared_quantity", "unit_kg", "unit_packs", "number_words", "unit_gm"}
        required = {
            "alias": {"alias"}, "malayalam_script": {"malayalam_script"}, "manglish": {"manglish"},
            "negation": {"negation", "self_correction", "instead_of"},
            "distractor": {"unstocked_mention", "history_mention", "numeric_distractor"},
            "units": {"unit_kg", "unit_packs", "number_words"},
        }
        for case in ALL_CASES:
            tags, category = set(case["tags"]), case["label"]
            with self.subTest(case=case["case_id"]):
                if category == "clean":
                    self.assertFalse(tags & hard)
                    self.assertIn("english", tags)
                elif category == "mixed_hard":
                    self.assertGreaterEqual(len(tags & hard), 3)
                elif category == "distractor":
                    self.assertGreaterEqual(len(tags & required["distractor"]), 2)
                else:
                    self.assertTrue(tags & required[category])

    def test_every_difficulty_tag_appears_in_every_split(self):
        for name in SPLIT_NAMES:
            tags = {t for c in SPLITS[name]["cases"] for t in c["tags"]}
            for tag in ("english", "alias", "malayalam_script", "manglish", "misspelling", "inflection",
                        "negation", "history_mention", "unstocked_mention", "numeric_distractor",
                        "unit_kg", "unit_packs", "number_words", "unit_gm", "self_correction",
                        "instead_of", "shared_quantity", "no_punctuation"):
                self.assertIn(tag, tags, f"{tag} missing from {name}")


class SplitTests(unittest.TestCase):
    def test_sizes_and_stratification(self):
        self.assertEqual({n: len(SPLITS[n]["cases"]) for n in SPLIT_NAMES}, {"train": 360, "dev": 120, "test": 120})
        for name, per in (("train", 45), ("dev", 15), ("test", 15)):
            for category in gen.CATEGORIES:
                self.assertEqual(sum(1 for c in SPLITS[name]["cases"] if c["label"] == category), per)
            self.assertTrue(all(c["split"] == name for c in SPLITS[name]["cases"]))

    def test_no_leakage_between_splits(self):
        def note_key(text):
            return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())

        def gold_key(case):
            return json.dumps([case["expected"]["dosha"],
                               sorted((h, s["grams"]) for h, s in case["expected"]["herbs"].items())])

        for key in (lambda c: c["case_id"], lambda c: note_key(c["notes"]), gold_key):
            seen = {}
            for name in SPLIT_NAMES:
                for case in SPLITS[name]["cases"]:
                    k = key(case)
                    self.assertNotIn(k, seen, f"{case['case_id']} shares a key with {seen.get(k)}")
                    seen[k] = case["case_id"]


class BaselineTests(unittest.TestCase):
    CASES = [
        ("Patient has Vata imbalance. Prescribe 20 grams of Ashwagandha.", "Vata", {"Ashwagandha": 20}),
        ("Vata-Pitta. Brahmi: 15g. Shatavari 2 x 50 g.", "Vata-Pitta", {"Brahmi": 15, "Shatavari": 100}),
        ("Kapha. Give Haridra 1.5 kg.", "Kapha", {"Haridra": 1500}),
        ("Turmeric 30 gm and withania somnifera 45 gms", "", {"Haridra": 30, "Ashwagandha": 45}),
        ("ബ്രഹ്മി 25 ഗ്രാം കൊടുക്കണം. വാതം കൂടുതൽ.", "Vata", {"Brahmi": 25}),
        ("Stop Guggulu. Give Neem 40 g.", "", {"Neem": 40}),
        ("ashwaganda 20gm kodukkanam", "", {"Ashwagandha": 20}),
        ("brahmiyum 15 grams venam", "", {"Brahmi": 15}),
        ("Symptoms for 10 days. Age 45. Triphala fifty grams.", "", {"Triphala": 50}),
        ("Takes Tulsi tea at home. Arjuna 60 g.", "", {"Arjuna": 60}),
        ("Weight 70 kg. Kutki 30 g.", "", {"Kutki": 30}),
        ("Previously took Vasaka 20 g. Give Pippali 35 g.", "", {"Pippali": 35}),
        ("Brahmi, dose to be decided.", "", {}),
        ("pitham kapham kooduthal", "Pitta-Kapha", {}),
    ]

    def test_hand_written_notes(self):
        for notes, dosha, herbs in self.CASES:
            with self.subTest(notes=notes):
                parsed = base.extract(notes)
                self.assertEqual(parsed.dosha_imbalance, dosha)
                self.assertEqual({h.name: h.grams for h in parsed.prescribed_herbs}, herbs)

    def test_extract_is_deterministic(self):
        for case in SPLITS["dev"]["cases"]:
            self.assertEqual(base.extract(case["notes"]).model_dump(), base.extract(case["notes"]).model_dump())

    def test_baseline_never_imports_the_generator(self):
        tree = ast.parse((ROOT / "scripts" / "s1_baseline.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        self.assertFalse({m for m in imported if "s1_generator" in m})
        self.assertNotIn("s1_meta", (ROOT / "scripts" / "s1_baseline.py").read_text(encoding="utf-8"))

    def test_artifacts_use_the_harness_schema_and_regrade_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            case_file = BENCH / "s1_dev.json"
            graded = base.run(case_file, tmp)
            raw = json.loads(pathlib.Path(graded["_paths"]["raw"]).read_text(encoding="utf-8"))
            self.assertEqual(raw["schema_version"], HARNESS.RAW_SCHEMA_VERSION)
            self.assertEqual(raw["metadata"]["case_file_sha256"], HARNESS.sha256_file(case_file))
            self.assertEqual(len(raw["records"]), 120)
            regraded = HARNESS.regrade(pathlib.Path(graded["_paths"]["raw"]), case_file)
            self.assertEqual(regraded["summary"], graded["summary"])
            self.assertIn("NOT CLINICAL VALIDATION", graded["summary"]["disclaimer"])

    def test_easy_slices_are_fully_solved(self):
        # Sanity check of baseline and grader together: clean/alias/Malayalam/distractor cases
        # contain nothing the rules cannot handle. Uses dev: no test reads results on the
        # s1_test.json holdout (documentation/eval/benchmarks/s1/README.md).
        with tempfile.TemporaryDirectory() as tmp:
            graded = base.run(BENCH / "s1_dev.json", tmp)
            table = base.compare(BENCH / "s1_dev.json", [graded["_paths"]["graded"]])["baseline"]
        for category in ("clean", "alias", "malayalam_script", "distractor"):
            self.assertEqual(table["by_category"][category]["pass_rate"], 1.0, category)


class ThreeArmIntegrationTests(unittest.TestCase):
    def test_baseline_control_and_a0_on_the_same_cases_without_gemini(self):
        key_before = os.environ.get("GEMINI_API_KEY")
        dev = SPLITS["dev"]
        subset = [next(c for c in dev["cases"] if c["label"] == cat) for cat in gen.CATEGORIES]
        with tempfile.TemporaryDirectory() as tmp:
            case_file = pathlib.Path(tmp) / "s1_subset.json"
            case_file.write_text(json.dumps({**dev, "cases": subset}, ensure_ascii=False), encoding="utf-8")
            reports = [base.run(case_file, tmp)]
            for arm in ("control", "a0"):
                reports.append(HARNESS.run(arm, "selfcheck", case_file=case_file, output_dir=pathlib.Path(tmp)))
            for report, arm, mode in zip(reports, ("baseline", "control", "a0"),
                                         ("deterministic", "selfcheck", "selfcheck")):
                self.assertEqual((report["metadata"]["arm"], report["metadata"]["mode"]), (arm, mode))
                self.assertEqual(len(report["results"]), len(subset))
                self.assertEqual(report["metadata"]["case_file_sha256"], HARNESS.sha256_file(case_file))
            table = base.compare(case_file, [r["_paths"]["graded"] for r in reports])
        self.assertEqual(list(table), ["baseline", "control", "a0"])
        for arm in table:
            self.assertEqual(set(table[arm]["by_category"]), set(gen.CATEGORIES))
        self.assertEqual(os.environ.get("GEMINI_API_KEY"), key_before)

    def test_compare_refuses_reports_from_another_case_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            graded = base.run(BENCH / "s1_dev.json", tmp)
            with self.assertRaises(ValueError):
                base.compare(BENCH / "s1_test.json", [graded["_paths"]["graded"]])


if __name__ == "__main__":
    unittest.main()
