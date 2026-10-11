"""
AI-2 v2 coverage check (memory/coverage.py): every word gets a role; nothing is silently dropped.
"""
import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory.coverage import NEUTRAL, UNSUPPORTED, coverage

ROLES = {"slot", "cue", "neutral", "context", "punct", "UNKNOWN", "UNSUPPORTED"}


def roles(q):
    c = coverage(q)
    return dict(zip(c["tokens"], c["roles"])), c


class CoverageTests(unittest.TestCase):
    def test_every_token_gets_exactly_one_role(self):
        for q in ["How many grams of bacopa monnieri in the latest 3 prescriptions?", "Has the Vata-Pitta changed?",
                  "zzz qqq", "ഗുൽഗുലു എത്ര കുറിപ്പടികളിൽ ഉണ്ടായിരുന്നു?", "ee patientinte ethra kurippadi undu?"]:
            c = coverage(q)
            self.assertEqual(len(c["tokens"]), len(c["roles"]))
            self.assertLessEqual(set(c["roles"]), ROLES)

    def test_fully_accounted_questions(self):
        for q in ["How many prescriptions does she have?", "What was the first prescription?",
                  "How many grams of bacopa monnieri in the latest 3 prescriptions?",
                  "Has the dosha changed since the first visit?", "When was Brahmi last prescribed for this patient?",
                  "How many times was Vata-Pitta recorded?"]:
            with self.subTest(q):
                self.assertEqual(coverage(q)["unexplained"], [])

    def test_slots_cues_and_neutral_words(self):
        r, _ = roles("How many grams of bacopa monnieri in the latest 3 prescriptions?")
        self.assertEqual((r["bacopa"], r["monnieri"], r["3"]), ("slot", "slot", "slot"))
        self.assertEqual((r["how"], r["many"], r["latest"], r["prescriptions"]), ("cue", "cue", "cue", "cue"))
        self.assertEqual((r["of"], r["the"], r["in"]), ("neutral", "neutral", "neutral"))
        r, _ = roles("Has the Vata-Pitta changed?")
        self.assertEqual((r["vata"], r["-"], r["pitta"]), ("slot", "slot", "slot"))

    def test_unknown_words_are_reported_not_dropped(self):
        _, c = roles("Overall how much Neem has gone out to her?")
        self.assertEqual(c["unexplained"], ["gone", "out"])  # "overall" is a TOTAL cue
        self.assertEqual(c["unsupported"], [])
        self.assertEqual(coverage("zzz qqq")["unexplained"], ["zzz", "qqq"])

    def test_meaning_changers_are_unsupported(self):
        for q, word in [("Which visits had no prescription?", "no"), ("What was the third prescription?", "third"),
                        ("How many patients got Neem?", "patients"), ("Total Neem this month?", "month"),
                        ("What was the smallest Neem dose?", "smallest"), ("Only the Brahmi grams?", "only"),
                        ("What did the previous patient receive?", "patient")]:
            with self.subTest(q):
                c = coverage(q)
                self.assertIn(word, c["unsupported"])
                self.assertIn(word, c["unexplained"])

    def test_selected_patient_is_neutral(self):
        for q in ["How many prescriptions does this patient have?", "Date of the patient's latest visit?"]:
            self.assertEqual(coverage(q)["unsupported"], [])

    def test_context_words_need_their_context(self):
        self.assertEqual(roles("Has Neem changed since the first prescription?")[0]["since"], "context")
        self.assertEqual(roles("How many visits since the first prescription?")[0]["since"], "UNKNOWN")
        self.assertEqual(roles("What was given the last time?")[0]["time"], "cue")
        self.assertEqual(roles("What time was Neem given?")[0]["time"], "UNKNOWN")

    def test_lexicons_are_disjoint_and_never_neutralise_negation(self):
        self.assertEqual(NEUTRAL & UNSUPPORTED, set())
        self.assertFalse({"not", "no", "only", "second", "patients", "other"} & NEUTRAL)

    def test_no_data_access(self):
        tree = ast.parse((ROOT / "memory" / "coverage.py").read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(imported - {"__future__"}, {"backend.herb_resolver", "memory.interpret", "memory.planner"})


if __name__ == "__main__":
    unittest.main()
