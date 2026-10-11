"""
AI-2 v2 Block 2A: structural reference safety (memory/reference.py) and its place in memory/v2.py.
Hand-written examples (not S3-S6 test data, devcal or hardneg); minimal pairs change only the relational phrase.
"""
import ast
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory.planner import Planner
from memory.reference import outside_scope
from memory.v2 import V2Planner, answer_for_session_v2


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s3 = _load("s3_generator_2a", ROOT / "scripts" / "s3_generator.py")
devcal = _load("v2_devcal_2a", ROOT / "scripts" / "v2_devcal.py")
hardneg = _load("v2_hardneg_2a", ROOT / "scripts" / "v2_hardneg.py")
PLANNER = Planner.train([(q["question"], q["frame"]) for q in s3.load("train")], 1, 13)
BLOCK1 = V2Planner(PLANNER, reference_check=False)
BLOCK2A = V2Planner(PLANNER, reference_check=True)

MINIMAL_PAIRS = [  # (target patient only, leaves the target patient's scope)
    ("What was the last prescription for this patient?", "What was the last prescription for the previous patient?"),
    ("Compare her Brahmi with the previous prescription.", "Compare her Brahmi with the patient seen before her."),
    ("What did the patient get before the latest visit?", "What did the patient before her get?"),
    ("What did this patient receive at the last visit?", "What did the next patient receive at the last visit?"),
    ("How many of her prescriptions contained Neem?", "How many of the clinic's prescriptions contained Neem?"),
    ("Show her prescriptions.", "Show all patients' prescriptions."),
    ("Compare her Neem with the first prescription.", "Compare her Neem with his."),
    ("Date of the patient's latest visit?", "Date of the other patient's latest visit?"),
    ("ee rogiyude avasanathe kurippadi enthaanu?", "kazhinja rogiyude avasanathe kurippadi enthaanu?"),
    ("ee rogikku ethra kurippadi undu?", "ellaa rogikalkkum ethra kurippadi undu?"),
    ("ഈ രോഗിയുടെ അവസാനത്തെ കുറിപ്പടി കാണിക്കൂ", "മറ്റൊരു രോഗിയുടെ അവസാനത്തെ കുറിപ്പടി കാണിക്കൂ"),
    ("ഇവർക്ക് എത്ര കുറിപ്പടി ഉണ്ട്?", "എല്ലാ രോഗികൾക്കും എത്ര കുറിപ്പടി ഉണ്ട്?"),
]
OUTSIDE = {
    "R1_relational_person": ["What did the previous patient receive?", "Show another patient's dosha.",
                             "What did the new patient get?", "What did the last patient get for Neem?",
                             "vere rogiyude marunnu kaanikku", "അടുത്ത രോഗിയുടെ കുറിപ്പടി കാണിക്കൂ"],
    "R2_relative_clause": ["Compare her Neem with the patient seen after her.", "What did the woman seen before her get?",
                           "What did the patient who came yesterday receive?", "Show the patient before this one."],
    "R3_group": ["How many prescriptions does each patient have?", "Did everyone get Brahmi?",
                 "Which patients got Kutki?", "How many people got Neem?", "rogikalkku ethra neem koduthu?",
                 "എല്ലാ രോഗികളുടെയും കുറിപ്പടികൾ കാണിക്കൂ"],
    "R4_clinic_wide": ["How many of the clinic's prescriptions had Brahmi?", "Total Neem across the clinic?",
                       "List the whole clinic's visits.", "clinicile ellaa kurippadikalum kaanikku",
                       "ക്ലിനിക്കിലെ എല്ലാ കുറിപ്പടികളും കാണിക്കൂ"],
    "R5_two_people": ["Compare her Neem with his.", "Is her Brahmi higher than their Brahmi?"],
    "R6_relative": ["ivarude makalkku enthu marunnu koduthu?", "ഇവരുടെ ഭർത്താവിന്റെ കുറിപ്പടി കാണിക്കൂ"],
}
CONTROLS = [  # mention before / after / previous / patient / clinic but stay with the target patient
    "What was her prescription before the last one?", "Show her prescriptions before the latest visit.",
    "How is the latest prescription different from the one before?", "Did Neem go up after the previous prescription?",
    "What changed from the one before?", "Has the dosha changed since the previous visit?",
    "Date of the patient's latest visit?", "How many times has she come to the clinic?",
    "How many prescriptions exist for him or her?", "Show his/her latest prescription.",
    "How many prescriptions are on their file?", "What did this patient get at her first visit?",
    "ee rogiyude avasanathe kurippadi enthaanu?", "ഈ രോഗിയുടെ അവസാനത്തെ കുറിപ്പടി കാണിക്കൂ",
    "ഇവർ ക്ലിനിക്കിൽ എത്ര തവണ വന്നു?", "kazhinja thavana enthu koduthu?",
]


class ReferenceRuleTests(unittest.TestCase):
    def test_rules(self):
        for rule, questions in OUTSIDE.items():
            for q in questions:
                with self.subTest(rule=rule, q=q):
                    r = outside_scope(q)
                    self.assertTrue(r["outside"], r)
                    self.assertIn(rule, r["rules"])

    def test_controls_stay_in_scope(self):
        for q in CONTROLS:
            with self.subTest(q):
                self.assertEqual(outside_scope(q), {"outside": False, "rules": [], "cues": []})

    def test_minimal_pairs(self):
        for good, bad in MINIMAL_PAIRS:
            with self.subTest(good):
                self.assertFalse(outside_scope(good)["outside"], good)
                self.assertTrue(outside_scope(bad)["outside"], bad)
                self.assertNotEqual(BLOCK2A.interpret(good)["stage"], "reference")
                bad_out = BLOCK2A.interpret(bad)
                self.assertEqual(bad_out["outcome"], "abstain", bad)
                self.assertIn(bad_out["stage"], ("screen", "reference"))

    def test_no_false_fire_on_development_questions(self):
        legit = [q["question"] for split in ("train", "dev") for q in s3.load(split) if q["gold_plan"]["kind"] == "query"]
        legit += [c["question"] for c in devcal.load("dev") if c["category"] in ("answerable", "paraphrase", "ambiguous")]
        legit += [c["question"] for c in hardneg.load() if c["label"] == "IN_SCOPE_MEMORY"]
        fired = [q for q in legit if outside_scope(q)["outside"]]
        self.assertEqual(fired, [])

    def test_hardneg_other_patient_questions_are_never_planned(self):
        planned = [c["question"] for c in hardneg.load() if c["label"] != "IN_SCOPE_MEMORY"
                   and BLOCK2A.interpret(c["question"])["outcome"] == "plan"]
        self.assertEqual(planned, [])

    def test_non_text_and_no_data_imports(self):
        self.assertFalse(outside_scope(None)["outside"])
        tree = ast.parse((ROOT / "memory" / "reference.py").read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(imported - {"__future__"}, {"memory.interpret", "memory.planner"})  # 4B: shared selectors


class PipelineTests(unittest.TestCase):
    def test_default_is_block1(self):
        q = "Compare her Brahmi with the patient seen before her."
        self.assertEqual(BLOCK1.interpret(q)["outcome"], "plan")  # the Block 1 gap this block closes
        out = BLOCK2A.interpret(q)
        self.assertEqual((out["outcome"], out["stage"], out["reason"]), ("abstain", "reference", "OTHER_PATIENT"))
        self.assertIsNone(out["plan"])

    def test_reference_check_is_on_by_default(self):
        default = V2Planner(PLANNER)
        self.assertTrue(default.reference_check)
        for q in [b for _, b in MINIMAL_PAIRS] + [g for g, _ in MINIMAL_PAIRS] + CONTROLS:
            a, b = default.interpret(q), BLOCK2A.interpret(q)
            self.assertEqual((a["outcome"], a["stage"], a["reason"], a["plan"], a["options"]),
                             (b["outcome"], b["stage"], b["reason"], b["plan"], b["options"]), q)

    def test_screen_still_first(self):
        out = BLOCK2A.interpret("Should we stop Neem for the previous patient?")
        self.assertEqual(out["stage"], "screen")

    def test_valid_questions_unchanged(self):
        for q in CONTROLS + [g for g, _ in MINIMAL_PAIRS]:
            a, b = BLOCK1.interpret(q), BLOCK2A.interpret(q)
            self.assertEqual((a["outcome"], a["plan"], a["options"]), (b["outcome"], b["plan"], b["options"]), q)


_rt = _load("test_memory_retrieval_2a", ROOT / "tests" / "test_memory_retrieval.py")


class NoDataReadTests(unittest.TestCase):
    setUpClass = classmethod(_rt.SessionRetrievalTests.setUpClass.__func__)
    tearDownClass = classmethod(_rt.SessionRetrievalTests.tearDownClass.__func__)
    pk = _rt.SessionRetrievalTests.pk

    def test_structural_references_read_no_patient_data(self):
        questions = [b for _, b in MINIMAL_PAIRS] + [q for qs in OUTSIDE.values() for q in qs]
        with mock.patch("memory.snapshot.build_snapshot") as build:
            for q in questions:
                out = answer_for_session_v2(self.tokens["s2_clinic_a"], self.pk("s2_clinic_a", 3), q, BLOCK2A)
                self.assertEqual(out["outcome"], "abstain", q)
                self.assertIsNone(out["claim"])
        build.assert_not_called()

    def test_valid_question_still_answered_in_scope(self):
        out = answer_for_session_v2(self.tokens["s2_clinic_a"], self.pk("s2_clinic_a", 3),
                                    "What was the last prescription for this patient?", BLOCK2A)
        self.assertEqual(out["outcome"], "answer", out)
        rx = {f"rx:{i}" for i in self.mapping["clinics"]["s2_clinic_a"]["patients"]["s2_clinic_a/P03"]["prescription_ids"]}
        self.assertLessEqual(set(out["claim"]["sources"]), rx)


if __name__ == "__main__":
    unittest.main()
