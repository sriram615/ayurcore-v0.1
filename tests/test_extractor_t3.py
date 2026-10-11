"""
AI-1 T3: deterministic verification of ClinicalParse output (extractor/verify.py).

Adversarial cases start from a correct (note, spans, parse, report) produced by
the T2 assembler on a hand-written note, change exactly one thing, and assert
the violation code. Every case also asserts that verification is pure: the
parse object comes back identical and unmodified. Positive controls: every T2
output on S1 train/dev verifies; v1 verdicts are only reported.
"""
import ast
import copy
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extractor import evaluate_t3
from extractor.assemble import assemble
from extractor.verify import verify

NOTE = "Pitta imbalance. Give Neem 40 g. Brahmi 20 grams. Kutki and Arjuna 30 g each. Stop Guggulu."


def mark(note, *marks):
    out = []
    for m in marks:
        text, kind, nth = m[0], m[1], (m[2] if len(m) > 2 else 0)
        start = -1
        for _ in range(nth + 1):
            start = note.index(text, start + 1)
        out.append((start, start + len(text), kind))
    return out


BASE_SPANS = mark(NOTE, ("Pitta", "DOSHA"), ("Neem", "HERB_RX"), ("40 g", "QTY"), ("Brahmi", "HERB_RX"),
                  ("20 grams", "QTY"), ("Kutki", "HERB_RX"), ("Arjuna", "HERB_RX"), ("30 g", "QTY"),
                  ("Guggulu", "HERB_NEG"))


def base():
    parsed, report = assemble(NOTE, BASE_SPANS)
    return NOTE, list(BASE_SPANS), parsed.model_dump(), report


def built(note, spans):
    parsed, report = assemble(note, spans)
    return note, spans, parsed.model_dump(), report


class VerifierCase(unittest.TestCase):
    def check(self, note, spans, parsed, report, *codes):
        frozen = copy.deepcopy((parsed, report, spans))
        result = verify(note, spans, parsed, report)
        self.assertIs(result["parse"], parsed)
        self.assertEqual((parsed, report, spans), frozen, "verification mutated its input")
        found = {v["code"] for v in result["violations"]}
        if codes:
            self.assertEqual(result["verdict"], "rejected", result["violations"])
            for code in codes:
                self.assertIn(code, found, result["violations"])
        else:
            self.assertEqual(result["verdict"], "verified", result["violations"])
        return result


class PositiveTests(VerifierCase):
    def test_correct_output_verifies(self):
        note, spans, parsed, report = base()
        self.assertEqual({h["name"]: h["grams"] for h in parsed["prescribed_herbs"]},
                         {"Neem": 40, "Brahmi": 20, "Kutki": 30, "Arjuna": 30})
        self.check(note, spans, parsed, report)

    def test_empty_but_consistent_output_verifies(self):
        note = "Pitta imbalance. Stop Guggulu."
        self.check(*built(note, mark(note, ("Pitta", "DOSHA"), ("Guggulu", "HERB_NEG"))))
        self.check("", [], {"dosha_imbalance": "", "prescribed_herbs": []}, {"facts": [], "dosha_spans": []})

    def test_legitimate_same_line_revision_verifies(self):
        note = "Kutki 25 g, sorry, 215 grams."
        self.check(*built(note, mark(note, ("Kutki", "HERB_RX"), ("25 g", "QTY"), ("215 grams", "QTY"))))

    def test_clinicalparse_object_is_returned_unchanged(self):
        parsed, report = assemble(NOTE, BASE_SPANS)
        before = parsed.model_dump()
        result = verify(NOTE, BASE_SPANS, parsed, report)
        self.assertIs(result["parse"], parsed)
        self.assertEqual(parsed.model_dump(), before)


class ForgedOrMissingProvenanceTests(VerifierCase):
    def test_forged_span_text(self):
        note, spans, parsed, report = base()
        report["facts"][0]["herb_span"]["text"] = "Nexm"
        self.check(note, spans, parsed, report, "forged_span_text")

    def test_bad_offsets(self):
        for start, end in ((-1, 4), (30, 26), (0, 10_000), (True, 4)):
            note, spans, parsed, report = base()
            report["facts"][0]["herb_span"].update(start=start, end=end)
            self.check(note, spans, parsed, report, "bad_offsets")

    def test_provenance_pointing_at_another_herb(self):
        note, spans, parsed, report = base()
        report["facts"][0]["herb_span"] = copy.deepcopy(report["facts"][1]["herb_span"])  # Neem -> Brahmi's span
        self.check(note, spans, parsed, report, "herb_span_mismatch")

    def test_missing_extra_and_reordered_facts(self):
        note, spans, parsed, report = base()
        del report["facts"][2]
        self.check(note, spans, parsed, report, "missing_provenance")
        note, spans, parsed, report = base()
        report["facts"].append(copy.deepcopy(report["facts"][0]))
        self.check(note, spans, parsed, report, "orphan_provenance")
        note, spans, parsed, report = base()
        report["facts"][0], report["facts"][1] = report["facts"][1], report["facts"][0]
        self.check(note, spans, parsed, report, "provenance_mismatch")
        note, spans, parsed, report = base()
        del report["facts"][0]["quantity_span"]
        self.check(note, spans, parsed, report, "missing_provenance")
        note, spans, parsed, report = base()
        report["facts"] = []
        self.check(note, spans, parsed, report, "missing_provenance")


class AlteredQuantityTests(VerifierCase):
    def test_output_grams_altered(self):
        note, spans, parsed, report = base()
        parsed["prescribed_herbs"][0]["grams"] = 45
        self.check(note, spans, parsed, report, "provenance_mismatch", "quantity_span_mismatch")

    def test_fact_and_output_altered_together(self):
        note, spans, parsed, report = base()
        parsed["prescribed_herbs"][0]["grams"] = 45
        report["facts"][0]["grams"] = 45
        self.check(note, spans, parsed, report, "quantity_span_mismatch")

    def test_quantity_span_moved_to_another_number(self):
        note, spans, parsed, report = base()
        report["facts"][0]["quantity_span"] = copy.deepcopy(report["facts"][1]["quantity_span"])  # "20 grams"
        self.check(note, spans, parsed, report, "quantity_span_mismatch")


class UnknownAndDuplicateHerbTests(VerifierCase):
    def test_unknown_herb(self):
        note, spans, parsed, report = base()
        parsed["prescribed_herbs"][0]["name"] = report["facts"][0]["name"] = "Tulsi"
        self.check(note, spans, parsed, report, "unknown_herb", "herb_span_mismatch")

    def test_real_herb_on_the_wrong_span(self):
        note, spans, parsed, report = base()
        parsed["prescribed_herbs"][0]["name"] = report["facts"][0]["name"] = "Shatavari"
        self.check(note, spans, parsed, report, "herb_span_mismatch")

    def test_ambiguous_resolution_is_rejected(self):
        note, spans, parsed, report = base()
        ambiguous = {"canonical": None, "method": None, "reason": "ambiguous", "candidates": ["Alpha", "Beta"]}
        with mock.patch("extractor.verify.resolve_herb", return_value=ambiguous):
            self.check(note, spans, parsed, report, "ambiguous_resolution")

    def test_unresolvable_span(self):
        note = "Give Zzzzzz 40 g."
        spans = mark(note, ("Zzzzzz", "HERB_RX"), ("40 g", "QTY"))
        parsed = {"dosha_imbalance": "", "prescribed_herbs": [{"name": "Neem", "grams": 40}]}
        report = {"facts": [{"name": "Neem", "grams": 40, "herb_span": {"start": 5, "end": 11, "text": "Zzzzzz"},
                             "quantity_span": {"start": 12, "end": 16, "text": "40 g"}}], "dosha_spans": []}
        self.check(note, spans, parsed, report, "unresolvable_herb_span")

    def test_duplicate_herb(self):
        note = "Give Neem 40 g. Neem 60 g."
        spans = mark(note, ("Neem", "HERB_RX"), ("40 g", "QTY"), ("Neem", "HERB_RX", 1), ("60 g", "QTY"))
        parsed, report = assemble(note, spans)
        parsed, report = parsed.model_dump(), report
        self.assertEqual(len(parsed["prescribed_herbs"]), 1)  # T2 already refuses; forge the duplicate
        second = {"name": "Neem", "grams": 60, "herb_span": {"start": 16, "end": 20, "text": "Neem"},
                  "quantity_span": {"start": 21, "end": 25, "text": "60 g"}}
        parsed["prescribed_herbs"].append({"name": "Neem", "grams": 60})
        report["facts"].append(second)
        self.check(note, spans, parsed, report, "duplicate_herb")


class DoshaTests(VerifierCase):
    def test_invalid_dosha_values(self):
        for value in ("Vata-Fire", "pitta", "Kapha-Pitta", "Pitta-Pitta", "Pitta-"):
            note, spans, parsed, report = base()
            parsed["dosha_imbalance"] = value
            self.check(note, spans, parsed, report, "invalid_dosha")

    def test_dosha_without_provenance(self):
        note, spans, parsed, report = base()
        parsed["dosha_imbalance"] = "Vata"
        self.check(note, spans, parsed, report, "dosha_without_provenance")
        note, spans, parsed, report = base()
        report["dosha_spans"] = []
        self.check(note, spans, parsed, report, "dosha_without_provenance")

    def test_dosha_span_forged_or_wrong_role(self):
        note, spans, parsed, report = base()
        report["dosha_spans"][0]["text"] = "Vata"
        self.check(note, spans, parsed, report, "forged_span_text")
        note, spans, parsed, report = base()
        spans = [(s, e, "UNSTOCKED" if t == "DOSHA" else t) for s, e, t in spans]
        self.check(note, spans, parsed, report, "role_mismatch")


class ImpossibleQuantityTests(VerifierCase):
    def test_impossible_gram_values(self):
        for grams in (0, -5, 10_001, 2.5, True, "40"):
            note, spans, parsed, report = base()
            parsed["prescribed_herbs"][0]["grams"] = report["facts"][0]["grams"] = grams
            self.check(note, spans, parsed, report, "invalid_grams")

    def test_body_weight_claimed_as_dose(self):
        note = "Give neem weight 84 kg."
        spans = mark(note, ("neem", "HERB_RX"), ("84 kg", "QTY"))
        for grams in (84, 84000):
            parsed = {"dosha_imbalance": "", "prescribed_herbs": [{"name": "Neem", "grams": grams}]}
            report = {"facts": [{"name": "Neem", "grams": grams, "herb_span": {"start": 5, "end": 9, "text": "neem"},
                                 "quantity_span": {"start": 17, "end": 22, "text": "84 kg"}}], "dosha_spans": []}
            self.check(note, spans, parsed, report, "quantity_span_mismatch")

    def test_range_claimed_as_dose(self):
        for note, qty in (("Give Neem 20-25 g.", "25 g"), ("Give Neem 20 to 25 g.", "25 g"), ("Give Neem 20-25 g.", "20-25 g")):
            spans = mark(note, ("Neem", "HERB_RX"), (qty, "QTY"))
            q0 = note.index(qty)
            parsed = {"dosha_imbalance": "", "prescribed_herbs": [{"name": "Neem", "grams": 25}]}
            report = {"facts": [{"name": "Neem", "grams": 25, "herb_span": {"start": 5, "end": 9, "text": "Neem"},
                                 "quantity_span": {"start": q0, "end": q0 + len(qty), "text": qty}}], "dosha_spans": []}
            self.check(note, spans, parsed, report, "range_quantity")


class InconsistentSpanTests(VerifierCase):
    def test_overlapping_and_partial_token_spans(self):
        note, spans, parsed, report = base()
        f = report["facts"][0]
        f["quantity_span"] = {"start": f["herb_span"]["start"], "end": f["herb_span"]["end"] + 3,
                              "text": note[f["herb_span"]["start"]:f["herb_span"]["end"] + 3]}
        self.check(note, spans, parsed, report, "overlapping_spans")
        note, spans, parsed, report = base()
        f = report["facts"][0]
        f["herb_span"] = {"start": f["herb_span"]["start"] + 1, "end": f["herb_span"]["end"] - 1, "text": "ee"}
        self.check(note, spans, parsed, report, "partial_token_span")

    def test_cross_sentence_link(self):
        note = "Give Neem. 40 g later."
        spans = mark(note, ("Neem", "HERB_RX"), ("40 g", "QTY"))
        parsed = {"dosha_imbalance": "", "prescribed_herbs": [{"name": "Neem", "grams": 40}]}
        report = {"facts": [{"name": "Neem", "grams": 40, "herb_span": {"start": 5, "end": 9, "text": "Neem"},
                             "quantity_span": {"start": 11, "end": 15, "text": "40 g"}}], "dosha_spans": []}
        self.check(note, spans, parsed, report, "cross_sentence_link")

    def test_non_adjacent_link(self):
        note = "Give Neem 40 g and Brahmi 20 g."
        spans = mark(note, ("Neem", "HERB_RX"), ("40 g", "QTY"), ("Brahmi", "HERB_RX"), ("20 g", "QTY"))
        parsed = {"dosha_imbalance": "", "prescribed_herbs": [{"name": "Neem", "grams": 20}]}
        report = {"facts": [{"name": "Neem", "grams": 20, "herb_span": {"start": 5, "end": 9, "text": "Neem"},
                             "quantity_span": {"start": 26, "end": 30, "text": "20 g"}}], "dosha_spans": []}
        self.check(note, spans, parsed, report, "non_adjacent_link")

    def test_shared_quantity_without_distributive_word(self):
        note = "Give Kutki and Neem 50 g."
        spans = mark(note, ("Kutki", "HERB_RX"), ("Neem", "HERB_RX"), ("50 g", "QTY"))
        q = {"start": 20, "end": 24, "text": "50 g"}
        parsed = {"dosha_imbalance": "", "prescribed_herbs": [{"name": "Kutki", "grams": 50}, {"name": "Neem", "grams": 50}]}
        report = {"facts": [{"name": "Kutki", "grams": 50, "herb_span": {"start": 5, "end": 10, "text": "Kutki"}, "quantity_span": q},
                            {"name": "Neem", "grams": 50, "herb_span": {"start": 15, "end": 19, "text": "Neem"}, "quantity_span": q}],
                  "dosha_spans": []}
        self.check(note, spans, parsed, report, "unjustified_shared_quantity")

    def test_herb_span_reused(self):
        note, spans, parsed, report = base()
        parsed["prescribed_herbs"].append(dict(parsed["prescribed_herbs"][0]))
        report["facts"].append(copy.deepcopy(report["facts"][0]))
        self.check(note, spans, parsed, report, "herb_span_reused", "duplicate_herb")

    def test_role_mismatch(self):
        note, spans, parsed, report = base()
        spans = [(s, e, "HERB_NEG" if NOTE[s:e] == "Neem" else t) for s, e, t in spans]
        self.check(note, spans, parsed, report, "role_mismatch")
        note, spans, parsed, report = base()
        spans = [(s, e, "QTY_OLD" if NOTE[s:e] == "40 g" else t) for s, e, t in spans]
        self.check(note, spans, parsed, report, "role_mismatch")

    def test_revision_quantity_cannot_also_serve_another_herb(self):
        note = "Kutki 25 g Neem 215 grams."
        spans = mark(note, ("Kutki", "HERB_RX"), ("25 g", "QTY"), ("Neem", "HERB_RX"), ("215 grams", "QTY"))
        parsed = {"dosha_imbalance": "", "prescribed_herbs": [{"name": "Kutki", "grams": 215}]}
        report = {"facts": [{"name": "Kutki", "grams": 215, "herb_span": {"start": 0, "end": 5, "text": "Kutki"},
                             "quantity_span": {"start": 16, "end": 25, "text": "215 grams"}}], "dosha_spans": []}
        self.check(note, spans, parsed, report, "non_adjacent_link")


class MalformedInputTests(VerifierCase):
    def test_malformed_output_and_provenance(self):
        note, spans, parsed, report = base()
        for bad in ({"prescribed_herbs": []}, {**parsed, "extra": 1},
                    {**parsed, "prescribed_herbs": [{"name": "Neem", "grams": 40, "why": "x"}]}, None, "text"):
            self.check(note, spans, bad, report, "malformed_output")
        self.check(note, spans, parsed, {**report, "facts": "not a list"}, "malformed_provenance")
        self.check(note, [("bad",)], parsed, report, "malformed_provenance")


class IndependenceTests(unittest.TestCase):
    def test_verifier_does_not_import_or_trust_assembly_decisions(self):
        source = (ROOT / "extractor" / "verify.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse({m for m in modules if "assemble" in m or "pipeline" in m or "tagger" in m})
        strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertFalse({"link_rule", "normalisation", "l6_revisions", "dropped_herbs"} & strings)


class PositiveControlTests(unittest.TestCase):
    def test_all_s1_train_dev_outputs_verify_and_v1_verdicts_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            from extractor import train as t1
            t1.train(pathlib.Path(tmp) / "t1")
            summary = evaluate_t3.run(pathlib.Path(tmp) / "t1" / "model.json", pathlib.Path(tmp) / "t3")
        for name in ("oracle_train", "oracle_dev", "predicted_train", "predicted_dev"):
            run = summary["runs"][name]
            self.assertEqual((run["verified"], run["rejected"]), (run["cases"], 0), (name, run["violations_by_code"]))
        self.assertEqual(summary["runs"]["predicted_v1"]["cases"], 12)  # reported, not asserted on


if __name__ == "__main__":
    unittest.main()
