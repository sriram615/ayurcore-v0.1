"""
AI-2 S3 benchmark and local question planner (memory/planner.py, memory/pipeline.py,
memory/evaluate_planner.py) -- the S3 TEST split is never evaluated here.

S3: deterministic and fingerprinted; splits are disjoint by template and by question text;
every supported operation pair, every abstain class and all three languages are in test;
gold answers equal the deterministic executor on the S2 reference snapshots.
Planner: delexicalisation, slot rules (missing / extra / unused content -> AMBIGUOUS), abstain
classes never yield a query, deterministic training, save / load; it imports nothing that can
answer. Pipeline: a planned query is answered by the executor and verified. Evaluator: the test
split needs final=True and is refused once results exist.
"""
import ast
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory import evaluate_planner as ev
from memory.executor import SUPPORTED, Query, execute
from memory.pipeline import answer_question
from memory.planner import ABSTAIN, FRAMES, Planner, delexicalise
from memory.s2_reference import reference_snapshots

_spec = importlib.util.spec_from_file_location("s3_generator_t", ROOT / "scripts" / "s3_generator.py")
s3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s3)
S3_FINGERPRINT = "75efd5ab2b2221251cedb906b0e7c17e84c15c34d21f7ddaa3cc845d05316bd8"
SPLITS = {s: s3.load(s) for s in ("train", "dev", "test")}
SNAPSHOTS = reference_snapshots(json.loads((ROOT / "eval" / "benchmarks" / "s2" / "s2_v1.json").read_text(encoding="utf-8")))


def _norm(text):
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())


class S3DatasetTests(unittest.TestCase):
    def test_frozen_and_deterministic(self):
        self.assertEqual(s3.verify(), [])
        manifest = json.loads(s3.MANIFEST_FILE.read_text(encoding="utf-8"))
        self.assertEqual(manifest["dataset_fingerprint_sha256"], S3_FINGERPRINT)
        self.assertEqual(s3.fingerprint(s3.generate(s3.SEED)), S3_FINGERPRINT)
        self.assertEqual(manifest["split_sizes"], {s: len(v) for s, v in SPLITS.items()})

    def test_splits_disjoint_by_template_and_text(self):
        templates = {s: {q["template"] for q in v} for s, v in SPLITS.items()}
        texts = {s: {_norm(q["question"]) for q in v} for s, v in SPLITS.items()}
        for a, b in (("train", "dev"), ("train", "test"), ("dev", "test")):
            with self.subTest(f"{a}/{b}"):
                self.assertFalse(templates[a] & templates[b])
                self.assertFalse(texts[a] & texts[b])
        ids = [q["id"] for v in SPLITS.values() for q in v]
        self.assertEqual(len(ids), len(set(ids)))

    def test_test_split_coverage(self):
        test = SPLITS["test"]
        pairs = {(q["gold_plan"]["query"]["op"], q["gold_plan"]["query"]["target"]) for q in test
                 if q["gold_plan"]["kind"] == "query"}
        self.assertEqual(pairs, SUPPORTED)
        self.assertEqual({q["frame"] for q in test}, set(FRAMES) | set(ABSTAIN))
        self.assertEqual({q["language"] for q in test}, {"en", "manglish", "ml"})
        self.assertTrue(any(q["gold_plan"]["kind"] == "query" and q["gold_plan"]["query"]["last_n"] for q in test))
        self.assertTrue(any(q["gold_plan"]["kind"] == "query" and q["gold_plan"]["query"]["a"] == "first"
                            and q["gold_plan"]["query"]["op"] in ("diff", "compare") for q in test))

    def test_gold_answers_are_deterministic_execution(self):
        for split, questions in SPLITS.items():
            for q in questions:
                if q["gold_plan"]["kind"] != "query":
                    self.assertIsNone(q["gold_answer"])
                    continue
                r = execute(SNAPSHOTS[q["patient"]], Query.from_dict(q["gold_plan"]["query"]))
                self.assertEqual((r.status, json.loads(json.dumps(r.value)), sorted(r.labels)),
                                 (q["gold_answer"]["status"], q["gold_answer"]["value"], q["gold_answer"]["sources"]),
                                 q["id"])

    def test_reference_snapshots_follow_the_s2_spec(self):
        s2 = json.loads((ROOT / "eval" / "benchmarks" / "s2" / "s2_v1.json").read_text(encoding="utf-8"))
        for clinic in s2["clinics"]:
            for p in clinic["patients"]:
                snap = SNAPSHOTS[p["key"]]
                rx_visits = [v for v in p["visits"] if v["kind"] != "encounter_only"]
                self.assertEqual(len(snap.prescriptions), len(rx_visits))
                self.assertEqual(len(snap.encounters), sum(v["kind"] in ("rx", "encounter_only") for v in p["visits"]))
                for v, got in zip(rx_visits, snap.prescriptions):
                    if v["kind"] == "legacy_unknown":
                        self.assertEqual((got.dosha, got.herbs), (None, None))
                    else:
                        self.assertEqual((got.dosha, [list(h) for h in got.herbs]), (v["dosha"], v["herbs"]))


class PlannerUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.planner = Planner.train([(q["question"], q["frame"]) for q in SPLITS["train"]], 1, ev.SEED)

    def test_delexicalisation(self):
        tokens, slots = delexicalise("When was Indian gooseberry first given, and how much Vata-Pitta in the last 3?")
        self.assertEqual(slots, {"herbs": ["Amalaki"], "doshas": ["Vata-Pitta"], "numbers": [3]})
        self.assertIn("HERB", tokens)
        _, slots = delexicalise("ബ്രഹ്മി എത്ര തവണ കൊടുത്തു? vatham um pitham, moonu")
        self.assertEqual(slots, {"herbs": ["Brahmi"], "doshas": ["Vata-Pitta"], "numbers": [3]})

    def test_slot_rules_force_abstention(self):
        for question in ("Compare Neem and Brahmi.", "How much did she get?", "", "   "):
            with self.subTest(question):
                plan = self.planner.plan(question)
                if plan["kind"] == "query":  # if the classifier picked a frame, its slots must be exactly satisfied
                    q = plan["query"]
                    self.assertEqual(sum(bool(q[k]) for k in ("herb", "dosha", "last_n")),
                                     len(plan["slots"]["herbs"]) + len(plan["slots"]["doshas"]) + len(plan["slots"]["numbers"]))
        self.assertEqual(self.planner.plan("")["reason"], "AMBIGUOUS")
        two_herbs = self.planner.plan("When was Neem and Brahmi first prescribed?")
        self.assertEqual((two_herbs["kind"], two_herbs["reason"]), ("abstain", "AMBIGUOUS"))

    def test_plans_are_valid_queries_or_abstentions(self):
        for q in SPLITS["dev"]:
            plan = self.planner.plan(q["question"])
            self.assertIn(plan["kind"], ("query", "abstain"))
            if plan["kind"] == "query":
                self.assertEqual(set(plan), {"kind", "query", "frame", "slots"})
                execute(SNAPSHOTS[q["patient"]], Query.from_dict(plan["query"]))  # raises if outside the vocabulary
            else:
                self.assertIn(plan["reason"], ABSTAIN)
                self.assertNotIn("query", plan)

    def test_deterministic_training_and_round_trip(self):
        examples = [(q["question"], q["frame"]) for q in SPLITS["train"][:300]]
        a, b = Planner.train(examples, 2, 7), Planner.train(examples, 2, 7)
        self.assertEqual(a.weights_sha256(), b.weights_sha256())
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "m.json"
            a.save(path)
            loaded = Planner.load(path)
        self.assertEqual(loaded.weights_sha256(), a.weights_sha256())
        self.assertEqual([loaded.plan(q["question"]) for q in SPLITS["dev"][:40]],
                         [a.plan(q["question"]) for q in SPLITS["dev"][:40]])

    def test_pipeline_answers_only_through_executor_and_verifier(self):
        q = next(q for q in SPLITS["dev"] if q["gold_plan"]["kind"] == "query"
                 and self.planner.plan(q["question"])["kind"] == "query")
        out = answer_question(SNAPSHOTS[q["patient"]], q["question"], self.planner)
        self.assertEqual(out["outcome"], "answer")
        self.assertEqual(out["verification"]["verdict"], "verified")
        expected = execute(SNAPSHOTS[q["patient"]], Query.from_dict(out["plan"]["query"]))
        self.assertEqual((out["claim"]["status"], out["claim"]["value"]), (expected.status, expected.value))
        abstain = answer_question(SNAPSHOTS[q["patient"]], "", self.planner)
        self.assertEqual((abstain["outcome"], abstain["claim"]), ("abstain", None))


class BoundaryTests(unittest.TestCase):
    def test_planner_cannot_answer(self):
        tree = ast.parse((ROOT / "memory" / "planner.py").read_text(encoding="utf-8"))
        modules = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | \
                  {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse({m for m in modules if m.startswith("memory")}, "planner must not import executor / snapshot / data")
        self.assertFalse({m for m in modules if m.split(".")[0] in ("google", "openai", "anthropic", "langchain", "agents",
                                                                 "db", "sqlite3", "requests", "urllib", "http")})


class EvaluatorTests(unittest.TestCase):
    def test_test_split_is_one_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ev.PlannerEvaluationRefused, "one-shot"):
                ev.run_eval("test", final=False, model_file="/nonexistent", out_dir=tmp)
            (pathlib.Path(tmp) / "s3_test_summary.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ev.PlannerEvaluationRefused, "already evaluated"):
                ev.run_eval("test", final=True, model_file="/nonexistent", out_dir=tmp)

    def test_dev_evaluation_runs_and_reports(self):
        planner = Planner.train([(q["question"], q["frame"]) for q in SPLITS["train"]], 1, ev.SEED)
        with tempfile.TemporaryDirectory() as tmp:
            model = pathlib.Path(tmp) / "model.json"
            planner.save(model)
            summary = ev.run_eval("dev", model_file=model, out_dir=tmp)
        self.assertEqual(summary["questions"], len(SPLITS["dev"]))
        for key in ("exact_plan", "answer_accuracy_answerable", "abstention", "by_operation", "by_language", "errors"):
            self.assertIn(key, summary)
        self.assertEqual(summary["verifier_rejections_of_answers"], 0)


if __name__ == "__main__":
    unittest.main()
