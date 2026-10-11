"""
AI-2 Block 5: candidate planning with deterministic intent / alignment checks (memory/interpret.py).

Hand-written development examples (not S3 test, not S4, not S5; S5 is checked not to overlap them):
  - a question whose elements fit one candidate is planned exactly, including cases the single-plan
    planner gets wrong (aggregation vs count, first vs previous, visits vs prescriptions)
  - legitimately ambiguous questions (two consistent readings, or none) abstain AMBIGUOUS
  - the alignment is deterministic and never touches patient data
  - retrieval with a CandidatePlanner keeps the existing RBAC, clinic isolation, evidence and verifier
S3 train / dev are used as development evidence only; the spent S3 test split is not used.
"""
import ast
import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory import retrieval
from memory.interpret import CandidatePlanner, align, analyse
from memory.planner import FRAMES, Planner
from memory.s2_reference import reference_snapshots

_spec = importlib.util.spec_from_file_location("s3_generator_i", ROOT / "scripts" / "s3_generator.py")
s3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s3)
_spec2 = importlib.util.spec_from_file_location("s2_generator_i", ROOT / "scripts" / "s2_generator.py")
s2 = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(s2)
PLANNER = Planner.train([(q["question"], q["frame"]) for q in s3.load("train")], 1, 13)
CANDIDATES = CandidatePlanner(PLANNER)

INTERPRET_PLANNED = [
    ("How many grams of Kutki in total?", "SUM_HERB"), ("How many times was Kutki given?", "COUNT_HERB"),
    ("Please list her visits.", "LIST_ENC"), ("Is the dosha now the same as at the beginning?", "CMP_DOSHA_FIRST"),
    ("Has the dosha changed since the previous visit?", "CMP_DOSHA_PREV"),
    ("Was there less Guggulu in the earliest prescription than now?", "CMP_HERB_FIRST"),
    ("How much Guggulu was in the latest prescription containing it?", "LAST_HERB"),
    ("Total grams of Neem across the latest 3 prescriptions?", "SUM_HERB_N"),
    ("How many visits has she had?", "COUNT_ENC"), ("How many prescriptions recorded Vata-Kapha?", "COUNT_DOSHA"),
    ("What did we give at the most recent visit?", "LAST_RX"), ("Which herbs were on her first prescription?", "FIRST_RX"),
    ("How many prescriptions altogether?", "COUNT_RX"), ("avasanathe dosham enthaanu?", "LAST_DOSHA"),
    ("Brahmi ethra gram aake koduthu?", "SUM_HERB"), ("What changed between the last two prescriptions?", "DIFF_PREV"),
    ("When did she last visit?", "LAST_ENC"),
]
INTERPRET_AMBIGUOUS = [
    "ethra Brahmi koduthu?",  # how much, or how many times
    "When was Brahmi given?",  # first or last
    "Compare the first and the previous prescription.",  # not a pair the vocabulary has
    "What was prescribed at the visit before the last one?",  # no "previous prescription" operation
]


class AlignmentTests(unittest.TestCase):
    def test_planned_exactly(self):
        for question, frame in INTERPRET_PLANNED:
            with self.subTest(question):
                plan = CANDIDATES.plan(question)
                self.assertEqual((plan["kind"], plan["frame"]), ("query", frame), plan)

    def test_ambiguous_abstains(self):
        for question in INTERPRET_AMBIGUOUS:
            with self.subTest(question):
                plan = CANDIDATES.plan(question)
                self.assertEqual((plan["kind"], plan["reason"]), ("abstain", "AMBIGUOUS"), plan)

    def test_unexplained_elements_reject_a_candidate(self):
        el = analyse("How many grams of Pippali altogether?")
        self.assertIn("AMOUNT", align("COUNT_HERB", el)[0])
        self.assertEqual(align("SUM_HERB", el), ([], []))
        el = analyse("Is the dosha unchanged from the beginning?")
        self.assertIn("FIRST", align("CMP_DOSHA_PREV", el)[0])
        self.assertIn("selector:first", align("CMP_DOSHA_FIRST", analyse("Has the dosha changed since the previous visit?"))[1])
        el = analyse("Can you list the visits?")
        self.assertIn("target:prescription", align("LIST_RX", el)[1])
        self.assertEqual(align("LIST_ENC", el), ([], []))

    def test_two_consistent_readings_abstain(self):
        plan = CANDIDATES.plan("ethra Brahmi koduthu?")
        consistent = {c["frame"] for c in plan["candidates"] if c["consistent"]}
        self.assertLessEqual({"SUM_HERB", "COUNT_HERB"}, consistent)
        self.assertEqual(plan["decision"], "ambiguous")

    def test_clinical_abstain_classes_of_the_planner_are_kept(self):
        class Fixed(Planner):
            def _scores(self, name, feats):
                scores = super()._scores(name, feats)
                if name == "op":
                    scores["TREATMENT"] = 1e9
                return scores
        plan = CandidatePlanner(Fixed(PLANNER.weights)).plan("How many prescriptions does she have?")
        self.assertEqual((plan["kind"], plan["reason"], plan["decision"]), ("abstain", "TREATMENT", "planner_abstain"))

    def test_s3_train_and_dev_development_evidence(self):
        """No wrong plan and no gold-abstain question planned on S3 train / dev (development data)."""
        for split in ("train", "dev"):
            for q in s3.load(split):
                out = retrieval.plan_question(q["question"], CANDIDATES)
                if q["gold_plan"]["kind"] == "abstain":
                    self.assertEqual(out["outcome"], "abstain", q)
                elif out["outcome"] != "abstain":
                    self.assertEqual(out["plan"]["query"], q["gold_plan"]["query"], q)

    def test_candidates_are_top_k_and_slot_compatible(self):
        el, cands, _ = CANDIDATES.candidates("Total grams of Neem across the latest 3 prescriptions?")
        self.assertLessEqual(len(cands), CANDIDATES.k)
        self.assertTrue(all("h" in FRAMES[c["frame"]][4] and "n" in FRAMES[c["frame"]][4] for c in cands))
        self.assertEqual([c["rank"] for c in cands], list(range(len(cands))))

    def test_deterministic(self):
        for question, _ in INTERPRET_PLANNED:
            self.assertEqual(CANDIDATES.plan(question), CANDIDATES.plan(question))

    def test_no_data_or_executor_access(self):
        tree = ast.parse((ROOT / "memory" / "interpret.py").read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
                   {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertEqual(imported - {"__future__", "re"}, {"memory.planner"})


class CandidateRetrievalTests(unittest.TestCase):
    SNAPS = reference_snapshots(s2.generate())

    def test_answers_carry_evidence_and_verified_claims(self):
        snap = self.SNAPS["s2_clinic_a/P03"]
        out = retrieval.answer_with_snapshot(snap, "How many grams of Kutki in total?", CANDIDATES)
        self.assertIn(out["outcome"], ("answer", "abstain"))
        out = retrieval.answer_with_snapshot(snap, "How many prescriptions does she have?", CANDIDATES)
        self.assertEqual(out["outcome"], "answer", out)
        self.assertEqual(out["verification"]["verdict"], "verified")
        self.assertTrue(out["claim"]["sources"])
        self.assertLessEqual(set(out["claim"]["sources"]), snap.labels())

    def test_screen_still_runs_first(self):
        out = retrieval.plan_question("Should we stop Neem?", CANDIDATES)
        self.assertEqual((out["outcome"], out["stage"], out["plan"]), ("abstain", "screen", None))



_spec_r = importlib.util.spec_from_file_location("test_memory_retrieval_i", ROOT / "tests" / "test_memory_retrieval.py")
_retrieval_tests = importlib.util.module_from_spec(_spec_r)
_spec_r.loader.exec_module(_retrieval_tests)


class CandidateSessionTests(unittest.TestCase):
    """The unchanged memory.retrieval.answer_for_session with a CandidatePlanner, on S2 built through the API."""
    setUpClass = classmethod(_retrieval_tests.SessionRetrievalTests.setUpClass.__func__)
    tearDownClass = classmethod(_retrieval_tests.SessionRetrievalTests.tearDownClass.__func__)
    pk = _retrieval_tests.SessionRetrievalTests.pk
    Q = "Total grams of Neem across the latest 3 prescriptions?"

    def test_rbac_isolation_and_evidence_are_unchanged(self):
        own = retrieval.answer_for_session(self.tokens["s2_clinic_a"], self.pk("s2_clinic_a", 3),
                                           "How many prescriptions does she have?", CANDIDATES)
        self.assertEqual(own["outcome"], "answer", own)
        rx = {f"rx:{i}" for i in self.mapping["clinics"]["s2_clinic_a"]["patients"]["s2_clinic_a/P03"]["prescription_ids"]}
        self.assertTrue(own["claim"]["sources"])
        self.assertLessEqual(set(own["claim"]["sources"]), rx)
        other = retrieval.answer_for_session(self.tokens["s2_clinic_a"], self.pk("s2_clinic_b", 3), self.Q, CANDIDATES)
        self.assertEqual((other["outcome"], other["claim"]), ("not_found", None))
        self.assertEqual(retrieval.answer_for_session(self.tokens["auditor"], self.pk("s2_clinic_a", 3), self.Q,
                                                      CANDIDATES)["outcome"], "forbidden")
        for bad in (None, "", "Bearer not-a-token"):
            self.assertEqual(retrieval.answer_for_session(bad, self.pk("s2_clinic_a", 3), self.Q, CANDIDATES)["outcome"],
                             "unauthenticated")

    def test_ambiguous_question_does_not_read_patient_data(self):
        from unittest import mock
        with mock.patch("memory.snapshot.build_snapshot") as build:
            out = retrieval.answer_for_session(self.tokens["s2_clinic_a"], self.pk("s2_clinic_a", 3),
                                               "ethra Brahmi koduthu?", CANDIDATES)
        self.assertEqual((out["outcome"], out["stage"], out["reason"]), ("abstain", "planner", "AMBIGUOUS"))
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
