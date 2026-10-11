"""
AI-2 v2 Block 1 pipeline (memory/v2.py): normalise -> screen -> every compatible candidate -> alignment
-> coverage -> answer / clarify / abstain, plus the deterministic defect fixes of this block.
Hand-written examples (not S3-S6, not the devcal set).
"""
import ast
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory import retrieval
from memory.interpret import CandidatePlanner, analyse
from memory.planner import FRAMES, Planner
from memory.s2_reference import reference_snapshots
from memory.scope import screen
from memory.v2 import MAX_OPTIONS, V2Planner, answer_for_session_v2, answer_with_snapshot_v2, describe

_spec = importlib.util.spec_from_file_location("s3_generator_v2", ROOT / "scripts" / "s3_generator.py")
s3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s3)
_spec2 = importlib.util.spec_from_file_location("s2_generator_v2", ROOT / "scripts" / "s2_generator.py")
s2 = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(s2)
PLANNER = Planner.train([(q["question"], q["frame"]) for q in s3.load("train")], 1, 13)
V2 = V2Planner(PLANNER)
SNAPS = reference_snapshots(s2.generate())
ZWSP = chr(0x200B)


class InterpretTests(unittest.TestCase):
    def test_plain_question_is_planned(self):
        out = V2.interpret("How many prescriptions does she have?")
        self.assertEqual((out["outcome"], out["plan"]["frame"]), ("plan", "COUNT_RX"))
        self.assertEqual(out["interpretation"]["unexplained_words"], [])

    def test_two_readings_clarify(self):
        out = V2.interpret("ethra Brahmi koduthu?")
        self.assertEqual((out["outcome"], out["reason"]), ("clarify", "SEVERAL_READINGS"))
        self.assertEqual({o["frame"] for o in out["options"]}, {"SUM_HERB", "COUNT_HERB"})
        self.assertTrue(all(o["text"].endswith(".") for o in out["options"]))

    def test_unexplained_word_confirms_instead_of_answering(self):
        out = V2.interpret("Total grams of Neem shipped to her?")
        self.assertEqual((out["outcome"], out["reason"]), ("clarify", "UNEXPLAINED_WORDS"))
        self.assertEqual([o["frame"] for o in out["options"]], ["SUM_HERB"])
        self.assertEqual(out["interpretation"]["unexplained_words"], ["shipped"])

    def test_underspecified_question_clarifies(self):
        out = V2.interpret("Did the dosha change?")
        self.assertEqual((out["outcome"], out["reason"]), ("clarify", "UNDERSPECIFIED"))
        self.assertEqual({o["frame"] for o in out["options"]}, {"CMP_DOSHA_PREV", "CMP_DOSHA_FIRST"})

    def test_unsupported_words_abstain(self):
        for q in ["Show the third prescription.", "How many patients got Neem?", "Which visits had no prescription?",
                  "What did the previous patient receive?"]:
            with self.subTest(q):
                out = V2.interpret(q)
                self.assertEqual(out["outcome"], "abstain", out)
                self.assertNotEqual(out["outcome"], "clarify")

    def test_screen_and_normaliser_still_first(self):
        self.assertEqual(V2.interpret("Should we stop Neem?")["stage"], "screen")
        for q in (None, 42, "", ZWSP * 2, "x " * 200):
            self.assertEqual(V2.interpret(q)[("stage")], "normalize")

    def test_never_more_than_max_options(self):
        for q in ["Brahmi?", "dosha", "Neem changed?", "prescriptions"]:
            out = V2.interpret(q)
            self.assertLessEqual(len(out["options"]), MAX_OPTIONS)

    def test_all_compatible_candidates_are_considered(self):
        q = "How many grams of Neem in the latest 3 prescriptions?"
        el = analyse(q)
        compatible = [f for f in FRAMES if "h" in FRAMES[f][4] and "n" in FRAMES[f][4]]
        self.assertEqual(len(V2.interpret(q)["interpretation"]["candidates"]), len(compatible))
        self.assertGreater(len(CandidatePlanner(PLANNER, k=None).candidates("List all prescriptions.")[1]), 5)
        self.assertTrue(el["slots"]["herbs"])

    def test_describe_covers_the_whole_plan_space(self):
        for frame, (op, target, a, b, needs) in FRAMES.items():
            query = {"op": op, "target": target, "herb": "Neem" if "h" in needs else None,
                     "dosha": "Vata" if "d" in needs else None, "last_n": 3 if "n" in needs else None,
                     "a": a or "previous", "b": b or "last"}
            text = describe(query)
            self.assertFalse(text.startswith(op + " "), frame)  # not the fallback
            if "h" in needs:
                self.assertIn("Neem", text)
            if "n" in needs:
                self.assertIn("3", text)

    def test_interpretation_reads_no_data(self):
        tree = ast.parse((ROOT / "memory" / "v2.py").read_text(encoding="utf-8"))
        top = {n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
        self.assertEqual(top - {"__future__"}, {"memory.confidence", "memory.coverage", "memory.interpret", "memory.morph", "memory.normalize", "memory.reference",
                                                 "memory.scope"})


class DefectFixTests(unittest.TestCase):
    def test_same_herb_twice_is_one_herb(self):
        q = "How many grams of Pippali were on the latest prescription with Pippali?"
        self.assertEqual(screen(q)["decision"], "PASS")
        self.assertEqual(analyse(q)["slots"]["herbs"], ["Pippali"])
        self.assertEqual(screen("Neem or Brahmi?")["reason"], "AMBIGUOUS")  # two different herbs still ambiguous

    def test_every_one_is_not_a_number(self):
        self.assertEqual(analyse("Show every one of the prescriptions.")["slots"]["numbers"], [])
        self.assertEqual(analyse("How many of the last 3 prescriptions had Neem?")["slots"]["numbers"], [3])


class PipelineTests(unittest.TestCase):
    def test_answer_carries_evidence_and_verification(self):
        snap = SNAPS["s2_clinic_a/P03"]
        for q in ["How many prescriptions does she have?", "How many pre" + ZWSP + "scriptions does she have?"]:
            out = answer_with_snapshot_v2(snap, q, V2)
            self.assertEqual(out["outcome"], "answer", out)
            self.assertEqual(out["verification"]["verdict"], "verified")
            self.assertLessEqual(set(out["claim"]["sources"]), snap.labels())

    def test_clarify_and_abstain_carry_no_claim(self):
        snap = SNAPS["s2_clinic_a/P03"]
        for q in ["ethra Brahmi koduthu?", "Show the third prescription."]:
            out = answer_with_snapshot_v2(snap, q, V2)
            self.assertIsNone(out["claim"])

    def test_v1_configuration_matches_the_block5_pipeline(self):
        v1 = V2Planner(PLANNER, k=5, normalize_input=False, coverage_check=False, clarify=False,
                       reference_check=False)
        cand = CandidatePlanner(PLANNER)
        snap = SNAPS["s2_clinic_b/P05"]
        for q in [x["question"] for x in s3.load("dev")][::7] + ["ethra Brahmi koduthu?", "Please list her visits."]:
            with self.subTest(q):
                a = answer_with_snapshot_v2(snap, q, v1)
                b = retrieval.answer_with_snapshot(snap, q, cand)
                self.assertEqual(a["outcome"], b["outcome"])
                self.assertEqual((a.get("plan") or {}).get("query"),
                                 (b["plan"] or {}).get("query") if b["outcome"] == "answer" else (a.get("plan") or {}).get("query"))


_spec_r = importlib.util.spec_from_file_location("test_memory_retrieval_v2", ROOT / "tests" / "test_memory_retrieval.py")
_rt = importlib.util.module_from_spec(_spec_r)
_spec_r.loader.exec_module(_rt)


class SessionTests(unittest.TestCase):
    """answer_for_session_v2 on S2 built through the API: the existing auth, RBAC and clinic isolation."""
    setUpClass = classmethod(_rt.SessionRetrievalTests.setUpClass.__func__)
    tearDownClass = classmethod(_rt.SessionRetrievalTests.tearDownClass.__func__)
    pk = _rt.SessionRetrievalTests.pk

    def test_access_outcomes(self):
        q = "How many prescriptions does she have?"
        own = answer_for_session_v2(self.tokens["s2_clinic_a"], self.pk("s2_clinic_a", 3), q, V2)
        self.assertEqual(own["outcome"], "answer")
        rx = {f"rx:{i}" for i in self.mapping["clinics"]["s2_clinic_a"]["patients"]["s2_clinic_a/P03"]["prescription_ids"]}
        self.assertLessEqual(set(own["claim"]["sources"]), rx)
        self.assertEqual(answer_for_session_v2(self.tokens["s2_clinic_a"], self.pk("s2_clinic_b", 3), q, V2)["outcome"],
                         "not_found")
        self.assertEqual(answer_for_session_v2(self.tokens["auditor"], self.pk("s2_clinic_a", 3), q, V2)["outcome"],
                         "forbidden")
        for bad in (None, "", "Bearer not-a-token"):
            self.assertEqual(answer_for_session_v2(bad, self.pk("s2_clinic_a", 3), q, V2)["outcome"], "unauthenticated")

    def test_clarify_and_abstain_read_no_patient_data(self):
        with mock.patch("memory.snapshot.build_snapshot") as build:
            for q in ["ethra Brahmi koduthu?", "Show the third prescription.", None]:
                out = answer_for_session_v2(self.tokens["s2_clinic_a"], self.pk("s2_clinic_a", 3), q, V2)
                self.assertIn(out["outcome"], ("clarify", "abstain"))
        build.assert_not_called()

    def test_unauthenticated_caller_gets_no_clarification(self):
        out = answer_for_session_v2(None, self.pk("s2_clinic_a", 3), "ethra Brahmi koduthu?", V2)
        self.assertEqual((out["outcome"], out["options"]), ("unauthenticated", []))


if __name__ == "__main__":
    unittest.main()
