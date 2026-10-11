"""
AI-1 T2: deterministic units / normalisation / assembly downstream of the T1 tagger.

Rule-level tests use hand-written notes and hand-placed spans (no tagger), so
each rule is checked on its own. Then: gold spans reproduce every S1 train/dev
gold ClinicalParse; the full pipeline evaluation runs through the existing
grader without opening s1_test.json; the deterministic modules import nothing
from the trained side.
"""
import ast
import builtins
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extractor import evaluate_t2
from extractor.assemble import L6_NOTE, assemble
from extractor.labels import case_spans
from extractor.normalize import dosha_set, resolve_herb
from extractor.units import parse_grams

S1 = ROOT / "eval" / "benchmarks" / "s1"


def spans(note, *marks):
    """marks: (substring, type[, occurrence index]) -> [(start, end, type)]"""
    out = []
    for mark in marks:
        text, kind = mark[0], mark[1]
        nth = mark[2] if len(mark) > 2 else 0
        start = -1
        for _ in range(nth + 1):
            start = note.index(text, start + 1)
        out.append((start, start + len(text), kind))
    return out


def herbs(parsed):
    return {h.name: h.grams for h in parsed.prescribed_herbs}


class UnitTests(unittest.TestCase):
    def test_accepted_forms(self):
        for text, grams in (("20 g", 20), ("20g", 20), ("20 gm", 20), ("20 gms", 20), ("20 grams", 20),
                            ("25 ഗ്രാം", 25), ("1.5 kg", 1500), ("2 kilo", 2000), ("3 x 50 g", 150),
                            ("2 packets of 25 g", 50), ("eighty grams", 80), ("twenty five grams", 25),
                            ("one hundred grams", 100), ("10000 g", 10000)):
            self.assertEqual(parse_grams(text), (grams, "ok"), text)

    def test_rejections_never_infer(self):
        for text, reason in (("20", "unitless"), ("2.5", "unitless"), ("84 kg", "out_of_range"),
                             ("0 g", "out_of_range"), ("1.0005 kg", "non_integer_grams"),
                             ("20 tablets", "unparseable"), ("blue grams", "unparseable")):
            self.assertEqual(parse_grams(text), (None, reason), text)


class NormaliseTests(unittest.TestCase):
    def test_exact_suffix_fuzzy(self):
        self.assertEqual(resolve_herb("Withania Somnifera")["canonical"], "Ashwagandha")
        self.assertEqual((resolve_herb("brahmiyum")["canonical"], resolve_herb("brahmiyum")["method"]), ("Brahmi", "suffix"))
        self.assertEqual(resolve_herb("arjunanu")["canonical"], "Arjuna")
        self.assertEqual((resolve_herb("ashwaganda")["canonical"], resolve_herb("ashwaganda")["method"]), ("Ashwagandha", "fuzzy"))
        self.assertEqual(resolve_herb("ബ്രഹ്മി")["canonical"], "Brahmi")

    def test_fuzzy_abstains_unless_exactly_one_candidate(self):
        toy = {"abcde": "Alpha", "abcdf": "Beta"}
        r = resolve_herb("abcdx", aliases=toy)
        self.assertEqual((r["canonical"], r["reason"], r["candidates"]), (None, "ambiguous", ["Alpha", "Beta"]))
        self.assertEqual(resolve_herb("zzzzzz")["reason"], "unknown")
        self.assertIsNone(resolve_herb("neam")["canonical"])  # 4 letters: too short for fuzzy

    def test_dosha(self):
        self.assertEqual(dosha_set("vatham-pitham"), ["Vata", "Pitta"])
        self.assertEqual(dosha_set("Pitta and Kapha"), ["Pitta", "Kapha"])
        self.assertEqual(dosha_set("വാതം കഫം"), ["Vata", "Kapha"])


class AssembleRuleTests(unittest.TestCase):
    def test_l4_adjacent_pairs_before_and_after(self):
        note = "Give Neem 40 g. 30 grams Brahmi venam."
        parsed, report = assemble(note, spans(note, ("Neem", "HERB_RX"), ("40 g", "QTY"),
                                              ("30 grams", "QTY"), ("Brahmi", "HERB_RX")))
        self.assertEqual(herbs(parsed), {"Neem": 40, "Brahmi": 30})
        fact = report["facts"][0]
        self.assertEqual((note[fact["herb_span"]["start"]:fact["herb_span"]["end"]],
                          note[fact["quantity_span"]["start"]:fact["quantity_span"]["end"]]), ("Neem", "40 g"))

    def test_l4_no_punctuation_gap_and_tie(self):
        note = "neem 40 g kodukkanam 30 grams brahmi venam"
        parsed, _ = assemble(note, spans(note, ("neem", "HERB_RX"), ("40 g", "QTY"),
                                         ("30 grams", "QTY"), ("brahmi", "HERB_RX")))
        self.assertEqual(herbs(parsed), {"Neem": 40, "Brahmi": 30})

    def test_l2_no_linking_across_sentences(self):
        note = "Neem. 40 g of something."
        parsed, report = assemble(note, spans(note, ("Neem", "HERB_RX"), ("40 g", "QTY")))
        self.assertEqual(herbs(parsed), {})
        self.assertEqual(report["dropped_herbs"][0]["reason"], "no_quantity")
        self.assertEqual(report["dropped_quantities"][0]["reason"], "no_herb")

    def test_l5_shared_only_with_marker(self):
        note = "Give Kutki and Neem, 50 g each."
        parsed, report = assemble(note, spans(note, ("Kutki", "HERB_RX"), ("Neem", "HERB_RX"), ("50 g", "QTY")))
        self.assertEqual(herbs(parsed), {"Kutki": 50, "Neem": 50})
        self.assertIn("L5 shared quantity", [f["link_rule"] for f in report["facts"]])
        note = "Give Kutki and Neem 50 g."
        parsed, report = assemble(note, spans(note, ("Kutki", "HERB_RX"), ("Neem", "HERB_RX"), ("50 g", "QTY")))
        self.assertEqual(herbs(parsed), {"Neem": 50})
        self.assertEqual(report["dropped_herbs"][0]["reason"], "no_quantity")

    def test_l6_revision_is_applied_and_flagged(self):
        note = "Kutki 25 g, sorry, 215 grams."
        parsed, report = assemble(note, spans(note, ("Kutki", "HERB_RX"), ("25 g", "QTY"), ("215 grams", "QTY")))
        self.assertEqual(herbs(parsed), {"Kutki": 215})
        self.assertEqual(report["l6_revisions"][0]["note"], L6_NOTE)
        # with the tagger's QTY_OLD the normal path applies and L6 is not used
        parsed, report = assemble(note, spans(note, ("Kutki", "HERB_RX"), ("25 g", "QTY_OLD"), ("215 grams", "QTY")))
        self.assertEqual((herbs(parsed), report["l6_revisions"]), ({"Kutki": 215}, []))

    def test_l1_roles_never_output_or_link(self):
        note = "Stop Guggulu 20 g. Was on Arjuna 15 g. Give Neem 40 g. Takes Tulsi."
        parsed, _ = assemble(note, spans(note, ("Guggulu", "HERB_NEG"), ("20 g", "QTY_OTHER"), ("Arjuna", "HERB_HIST"),
                                         ("15 g", "QTY_OTHER"), ("Neem", "HERB_RX"), ("40 g", "QTY"), ("Tulsi", "UNSTOCKED")))
        self.assertEqual(herbs(parsed), {"Neem": 40})

    def test_body_weight_unitless_and_range_are_rejected(self):
        note = "neem weight 84 kg"
        parsed, report = assemble(note, spans(note, ("neem", "HERB_RX"), ("84 kg", "QTY")))
        self.assertEqual((herbs(parsed), report["dropped_herbs"][0]["reason"]), ({}, "quantity:out_of_range"))
        note = "Ashwagandha 20."
        parsed, report = assemble(note, spans(note, ("Ashwagandha", "HERB_RX"), ("20", "QTY")))
        self.assertEqual((herbs(parsed), report["dropped_herbs"][0]["reason"]), ({}, "quantity:unitless"))
        for note, qty in (("ashwagandha 20-25 g venam", "25 g"), ("ashwagandha 20 to 25 g venam", "25 g"),
                          ("ashwagandha 20-25 g venam", "20-25 g")):
            parsed, report = assemble(note, spans(note, ("ashwagandha", "HERB_RX"), (qty, "QTY")))
            self.assertEqual((herbs(parsed), report["dropped_herbs"][0]["reason"]), ({}, "quantity:range"), note)

    def test_l11_duplicate_keeps_first_and_reports(self):
        note = "Neem 40 g. Neem 60 g."
        parsed, report = assemble(note, spans(note, ("Neem", "HERB_RX"), ("40 g", "QTY"), ("Neem", "HERB_RX", 1), ("60 g", "QTY")))
        self.assertEqual(herbs(parsed), {"Neem": 40})
        self.assertEqual(report["conflicts"][0]["herb"], "Neem")

    def test_l8_unresolved_herb_abstains(self):
        note = "Give Zzzzzz 40 g."
        parsed, report = assemble(note, spans(note, ("Zzzzzz", "HERB_RX"), ("40 g", "QTY")))
        self.assertEqual(herbs(parsed), {})
        self.assertEqual(report["dropped_herbs"][0]["reason"], "unresolved:unknown")

    def test_l9_dosha(self):
        note = "vatham-pitham dosham. Neem 40 g."
        parsed, _ = assemble(note, spans(note, ("vatham-pitham", "DOSHA"), ("Neem", "HERB_RX"), ("40 g", "QTY")))
        self.assertEqual(parsed.dosha_imbalance, "Vata-Pitta")
        parsed, _ = assemble("Neem 40 g.", spans("Neem 40 g.", ("Neem", "HERB_RX"), ("40 g", "QTY")))
        self.assertEqual(parsed.dosha_imbalance, "")


class S1OracleTests(unittest.TestCase):
    def test_gold_spans_reproduce_every_train_and_dev_gold_parse(self):
        for split in ("train", "dev"):
            with open(S1 / f"s1_{split}.json", encoding="utf-8") as f:
                cases = json.load(f)["cases"]
            for case in cases:
                with self.subTest(case=case["case_id"]):
                    parsed, report = assemble(case["notes"], case_spans(case))
                    self.assertEqual(herbs(parsed), {h: s["grams"] for h, s in case["expected"]["herbs"].items()})
                    self.assertEqual(parsed.dosha_imbalance.split("-"), case["expected"]["dosha"])
                    self.assertEqual(report["l6_revisions"], [])
                    for fact in report["facts"]:  # provenance points at the note text
                        self.assertEqual(case["notes"][fact["herb_span"]["start"]:fact["herb_span"]["end"]],
                                         fact["herb_span"]["text"])


class PipelineEvaluationTests(unittest.TestCase):
    def test_evaluation_runs_through_the_grader_without_the_holdout(self):
        opened = []
        real_open = builtins.open

        def spy(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            from extractor import train as t1
            t1.train(pathlib.Path(tmp) / "t1")
            with mock.patch("builtins.open", spy):
                summary = evaluate_t2.run(pathlib.Path(tmp) / "t1" / "model.json", pathlib.Path(tmp) / "t2")
            files = sorted(p.name for p in (pathlib.Path(tmp) / "t2").iterdir())
        self.assertFalse([p for p in opened if "s1_test" in p])
        self.assertEqual(set(summary["runs"]), {"oracle_train", "oracle_dev", "predicted_train", "predicted_dev", "predicted_v1"})
        self.assertIn("summary.json", files)
        for name in ("oracle_train", "oracle_dev"):
            self.assertEqual(summary["runs"][name]["pass"], summary["runs"][name]["cases"])
        self.assertIn("NOT CLINICAL VALIDATION", summary["classification"])
        self.assertIn("never used for tuning", summary["v1_note"])


class SeparationTests(unittest.TestCase):
    def test_deterministic_modules_import_nothing_from_the_trained_side(self):
        for name in ("units.py", "normalize.py", "assemble.py"):
            tree = ast.parse((ROOT / "extractor" / name).read_text(encoding="utf-8"))
            modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            modules |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            self.assertFalse({m for m in modules if m.endswith(("tagger", "features", "train", "pipeline"))}, name)


if __name__ == "__main__":
    unittest.main()
