"""
T3 integration of the context checks: verify() runs R1-R4 (V10-V13) after V1-V9
as hard rejections and returns R5 separately as warnings.

Proves: every R1-R4 rejection reaches the final verdict; R5 never rejects; the
checks run after V1-V9; the parse is never modified; valid S1 train/dev outputs
(gold and T1-predicted spans) are still all accepted, with R1-R5 counts printed;
and mutations of the integration and of the context rules are caught.
All notes are hand-written for the tests; S1-OOD is not used.
"""
import copy
import importlib.util
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.schemas import ClinicalParse
from extractor import verify as verify_module
from extractor.verify import verify

_spec = importlib.util.spec_from_file_location("t3_context_cases", ROOT / "tests" / "test_t3_context.py")
context_cases = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(context_cases)
build = context_cases.build

REJECT = [
    # (label, note, facts, labels, exact violation codes, rule)
    ("R1", "Kapha. Give Kutki 30 g. Correction for Kutki: 35 g.",
     [("Kutki", 0, "30 g", 0, "Kutki", 30)], (), {"conflicting_quantities"}, "R1"),
    ("R2 amounts", "Vata. Arjuna either 30 g or 45 g.",
     [("Arjuna", 0, "45 g", 0, "Arjuna", 45)], (), {"alternative_quantities"}, "R2"),
    ("R2 herbs", "Pitta. Neem or Haridra 40 g.",
     [("Haridra", 0, "40 g", 0, "Haridra", 40)], (), {"alternative_herbs"}, "R2"),
    ("R3", "Took Vasaka 50 g two weeks ago.",
     [("Vasaka", 0, "50 g", 0, "Vasaka", 50)], (), {"historical_context"}, "R3"),
    ("R4 contested", "Stop Manjistha. Manjistha 40 g.",
     [("Manjistha", 1, "40 g", 0, "Manjistha", 40)], [("Manjistha", 0, "HERB_NEG")], {"contested_status"}, "R4"),
    ("R4 negated", "Do not give Neem 40 g.",
     [("Neem", 0, "40 g", 0, "Neem", 40)], (), {"negated_context"}, "R4"),
]
ACCEPT = [
    # (label, note, facts, labels, expected warning codes)
    ("correction, last amount", "Give Punarnava 40 g, no, make it 45 g.",
     [("Punarnava", 0, "45 g", 0, "Punarnava", 45)], [("40 g", 0, "QTY_OLD")], set()),
    ("history in another sentence", "Neem was used last month. Brahmi 30 g today.",
     [("Brahmi", 0, "30 g", 0, "Brahmi", 30)], [("Neem", 0, "HERB_HIST")], set()),
    ("R5 unknown plant dose", "Neem 40 g. Lodhra 20 g.", [("Neem", 0, "40 g", 0, "Neem", 40)], (),
     {"unaccounted_quantity"}),
    ("R5 empty output with a dose", "Kapha. Pippali 25 g.", [], (), {"unaccounted_quantity"}),
]


def _run(fn, note, facts, labels):
    spans, parsed, report = build(note, facts, labels)
    before = copy.deepcopy(parsed), copy.deepcopy(report)
    result = fn(note, spans, parsed, report)
    assert (parsed, report) == before, "verify modified its input"
    assert result["parse"] is parsed
    return result


class IntegrationTests(unittest.TestCase):
    def test_each_context_rejection_reaches_the_verdict(self):
        for label, note, facts, labels, codes, rule in REJECT:
            with self.subTest(label):
                result = _run(verify, note, facts, labels)
                self.assertEqual(result["verdict"], "rejected")
                self.assertEqual({v["code"] for v in result["violations"]}, codes)  # no V1-V9 code: only the rule
                self.assertTrue(all(v.get("rule") == rule for v in result["violations"]))
                self.assertEqual(result["checked_facts"], len(facts))

    def test_valid_outputs_verify_and_r5_only_warns(self):
        for label, note, facts, labels, warnings in ACCEPT:
            with self.subTest(label):
                result = _run(verify, note, facts, labels)
                self.assertEqual(result["verdict"], "verified", result["violations"])
                self.assertEqual(result["violations"], [])
                self.assertEqual({w["code"] for w in result["warnings"]}, warnings)
                self.assertTrue(all(w["rule"] == "R5" for w in result["warnings"]))

    def test_r5_warnings_alone_never_reject(self):
        fake = {"violations": [], "warnings": [{"code": "unaccounted_quantity", "rule": "R5", "where": "x", "detail": ""}]}
        with mock.patch.object(verify_module, "check_context", return_value=fake):
            result = _run(verify, "Neem 40 g.", [("Neem", 0, "40 g", 0, "Neem", 40)], ())
        self.assertEqual(result["verdict"], "verified")
        self.assertEqual(len(result["warnings"]), 1)

    def test_context_checks_run_after_v1_v9(self):
        note = "Kapha. Give Kutki 30 g. Correction for Kutki: 35 g."
        spans, parsed, report = build(note, [("Kutki", 0, "30 g", 0, "Kutki", 30)])
        parsed["dosha_imbalance"] = "Vata"  # no DOSHA span -> V9
        codes = [v["code"] for v in verify(note, spans, parsed, report)["violations"]]
        self.assertEqual(codes, ["dosha_without_provenance", "conflicting_quantities"])

    def test_malformed_input_still_handled_by_v1_v3(self):
        note = "Do not give Neem 40 g."
        spans, parsed, report = build(note, [("Neem", 0, "40 g", 0, "Neem", 40)])
        report["facts"][0]["herb_span"] = {"start": 1, "end": 300, "text": "x"}
        result = verify(note, spans, parsed, report)
        self.assertIn("bad_offsets", {v["code"] for v in result["violations"]})
        self.assertEqual(result["verdict"], "rejected")
        early = verify(note, spans, None, report)  # V1 early return keeps the result shape
        self.assertEqual((early["verdict"], early["warnings"]), ("rejected", []))

    def test_clinicalparse_returned_unchanged(self):
        note = "Took Vasaka 50 g two weeks ago."
        spans, parsed, report = build(note, [("Vasaka", 0, "50 g", 0, "Vasaka", 50)])
        model = ClinicalParse(dosha_imbalance="", prescribed_herbs=parsed["prescribed_herbs"])
        dump = model.model_dump()
        result = verify(note, spans, model, report)
        self.assertIs(result["parse"], model)
        self.assertEqual(model.model_dump(), dump)
        self.assertEqual(result["verdict"], "rejected")

    def test_result_keys_are_backward_compatible(self):
        result = _run(verify, "Neem 40 g.", [("Neem", 0, "40 g", 0, "Neem", 40)], ())
        self.assertEqual(set(result), {"verdict", "violations", "checked_facts", "parse", "warnings"})


class S1PositiveControlTests(unittest.TestCase):
    def test_s1_train_dev_outputs_all_accepted_and_counts(self):
        from extractor import evaluate_t2, train as t1
        from extractor.labels import case_spans
        from extractor.pipeline import Extractor
        with tempfile.TemporaryDirectory() as tmp:
            t1.train(pathlib.Path(tmp) / "t1")
            extractor = Extractor(t1.load_model(pathlib.Path(tmp) / "t1" / "model.json"))
            print(f"\nT1 weights sha256 {extractor.model.weights_sha256()}")
            for mode in ("oracle", "predicted"):
                spans_for = (lambda c: case_spans(c)) if mode == "oracle" else (lambda c: extractor.spans(c["notes"]))
                for split, path in (("train", t1.TRAIN_FILE), ("dev", t1.DEV_FILE)):
                    cases = t1.load_cases(path)
                    counts = {r: 0 for r in ("R1", "R2", "R3", "R4", "R5")}
                    verified = 0
                    for case, rec in zip(cases, evaluate_t2._records("t3_integration", cases, spans_for)):
                        spans = [(s["start"], s["end"], s["type"]) for s in rec["spans"]]
                        result = verify(case["notes"], spans, rec["parsed"], rec["assembly"])
                        verified += result["verdict"] == "verified"
                        for item in result["violations"] + result["warnings"]:
                            if "rule" in item:
                                counts[item["rule"]] += 1
                    print(f"{mode}_{split}: verified {verified}/{len(cases)}  {counts}")
                    with self.subTest(f"{mode}_{split}"):
                        self.assertEqual(verified, len(cases))
                        self.assertEqual([counts[r] for r in ("R1", "R2", "R3", "R4")], [0, 0, 0, 0])


VERIFY_MUTATIONS = {
    "context call removed": ("    violations.extend(context[\"violations\"])\n", ""),
    "warnings become violations": ("    warnings.extend(context[\"warnings\"])\n",
                                   "    violations.extend(context[\"warnings\"])\n"),
    "context before V1-V9 only on success": ("    context = check_context(note, spans, parsed, report)\n",
                                             "    context = check_context(note, spans, parsed, report) if not violations "
                                             "else {\"violations\": [], \"warnings\": []}\n"),
}


class MutationTests(unittest.TestCase):
    def _mutated_verify(self, old, new):
        src = (ROOT / "extractor" / "verify.py").read_text(encoding="utf-8")
        self.assertEqual(src.count(old), 1, old)
        module = types.ModuleType("verify_mutant")
        exec(compile(src.replace(old, new), "verify_mutant", "exec"), module.__dict__)
        return module.verify

    def _outcomes_ok(self, fn) -> bool:
        for label, note, facts, labels, codes, rule in REJECT:
            result = fn(note, *build(note, facts, labels))
            if result["verdict"] != "rejected" or {v["code"] for v in result["violations"]} != codes:
                return False
        for label, note, facts, labels, warnings in ACCEPT:
            result = fn(note, *build(note, facts, labels))
            if result["verdict"] != "verified" or {w["code"] for w in result["warnings"]} != warnings:
                return False
        note = "Kapha. Give Kutki 30 g. Correction for Kutki: 35 g."
        spans, parsed, report = build(note, [("Kutki", 0, "30 g", 0, "Kutki", 30)])
        parsed["dosha_imbalance"] = "Vata"
        return [v["code"] for v in fn(note, spans, parsed, report)["violations"]] == \
            ["dosha_without_provenance", "conflicting_quantities"]

    def test_unmutated_integration_passes_the_oracle(self):
        self.assertTrue(self._outcomes_ok(verify))

    def test_integration_mutations_are_caught(self):
        for label, (old, new) in VERIFY_MUTATIONS.items():
            with self.subTest(label):
                self.assertFalse(self._outcomes_ok(self._mutated_verify(old, new)), f"mutation survived: {label}")

    def test_context_rule_mutations_are_caught(self):
        src = (ROOT / "extractor" / "verify_context.py").read_text(encoding="utf-8")
        mutations = {
            "R1 off": ('if scan.amounts[last]["grams"] != grams:', "if False:"),
            "R1 QTY_OTHER exclusion off": ('and not any(_overlaps((scan.amounts[b]["start"], scan.amounts[b]["end"]), o) '
                                           'for o in other)', ""),
            "R2 amounts off": ("alt_amounts |= {p, p + 1}", "pass"),
            "R2 bare number off": ("alt_amounts.add(p)  #", "pass  #"),
            "R2 herbs off": ("alt_mentions |= {m1, m2}", "pass"),
            "R3 present anchor off": ("if any(scan.low[k] in PRESENT_ANCHORS for k in between):", "if False:"),
            "R3 clause scope off": ("ks = [k for k in range(lo, hi + 1) if scan.clause[k] == clause]",
                                    "ks = list(range(lo, hi + 1))"),
            "R3 nearest unit off": ("associated = {m for d, m in candidates if d == best}",
                                    "associated = {m for d, m in candidates}"),
            "R3 time-word phrase off": ("cues.append((k, k + 1) if k + 1 < len(low) and low[k + 1] in TIME_NOUNS "
                                        "else (k, k))", "cues.append((k, k))"),
            "R4 contested off": ('if resolve_herb(note[s:e])["canonical"] == name:', "if False:"),
            "R4 backward stop off": ("""if (not _is_word(scan.tokens[k][0]) and scan.tokens[k][0] != "'") """
                                     """or scan.ends_segment[k]:""", "if False:"),
            "R4 backward clause stop off": ("""!= "'") or scan.ends_segment[k]:""", """!= "'"):"""),
            "R4 forward punctuation stop off": ("""            if not _is_word(scan.tokens[k][0]) and scan.tokens[k][0] != "'":\n"""
                                                """                break\n            window.append(k)\n            if scan.ends_segment""",
                                                "            window.append(k)\n            if scan.ends_segment"),
            "R4 forward clause stop off": ("            if scan.ends_segment[k]:\n                break  # this",
                                           "            if False:\n                break  # this"),
            "Manglish verb boundary off": ("verb_end = _is_finite_verb(t) and", "verb_end = False and"),
            "verb boundary punctuation guard off": ("and nxt and (_is_word(nxt) or nxt[:1].isdigit())", ""),
            "R5 stopped-herb exemption off": ("if mm is not None and any(", "if False and any("),
            "G1 unknown items off": ("        self._unknown_mentions(unstocked_spans)\n", ""),
            "G1 formulary-after guard off": ('and not (a["j"] + 1 in starts):', ":"),
            "G1 dose-of attachment off": ('if of:  # "<dose> of X" names its own item', "if False:"),
            "G1 foreign quantity off": ('if owner is not None and scan.mentions[owner].get("unknown"):', "if False:"),
            "G2 loose alternatives off": ('(joined(x["j"], y["i"]) or joined_loosely(x["j"], y["i"]))',
                                          'joined(x["j"], y["i"])'),
            "G2 conditional off": ("if conditional and offers and items >= 2:", "if False:"),
            "G2 conditional two-item guard off": ("if conditional and offers and items >= 2:", "if conditional and offers:"),
            "G3 past verbs off": ("elif t in PAST_VERBS and not (k > 0 and low[k - 1] in PASSIVE_OR_MODAL):", "elif False:"),
            "G3 passive guard off": (" and not (k > 0 and low[k - 1] in PASSIVE_OR_MODAL):", ":"),
            "G3 before-encounter off": ('elif t == "before":', "elif False:"),
            "G3 history header off": ("for lo, hi in _history_scopes(scan):", "for lo, hi in []:"),
            "G3 re-order exemption off": ("if m in associated and not reordered(m):", "if m in associated:"),
            "G4 same-herb negation off": ("                if hits2:", "                if False:"),
            "G4 negated action off": ("        if action:", "        if False:"),
            "G4 negated-action target guard off": ("if not any(scan._covered(n) for n in ahead):", "if True:"),
            "G4 cancellation off": ("        if m in cancelled:", "        if False:"),
            "G4 negative-need restriction off": ("(t in ANAPHORS and any(negative_need(n) for n in near))",
                                                 "(t in ANAPHORS and any(_is_negation(scan.low[n]) for n in near))"),
        }
        for label, (old, new) in mutations.items():
            with self.subTest(label):
                self.assertEqual(src.count(old), 1, label)
                module = types.ModuleType("context_mutant")
                exec(compile(src.replace(old, new), "context_mutant", "exec"), module.__dict__)
                failures = 0
                for _, note, facts, labels, expected in context_cases.CASES:
                    codes = {v["code"] for v in module.check_context(note, *build(note, facts, labels))["violations"]}
                    failures += (bool(expected) and not expected <= codes) or (not expected and bool(codes))
                for _, note, facts, labels, warn in context_cases.WARNING_CASES:
                    out = module.check_context(note, *build(note, facts, labels))
                    failures += bool(out["violations"]) or (any(w["code"] == "unaccounted_quantity"
                                                                for w in out["warnings"]) != warn)
                self.assertGreater(failures, 0, f"mutation survived: {label}")


if __name__ == "__main__":
    unittest.main()
