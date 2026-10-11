"""
Usage/cost recording for Gemini evaluations (scripts/clinical_eval_harness.py
and scripts/eval_cost.py) -- recording only.

No request ever leaves the process: the SDK's per-request method is replaced by
a stub that returns a constructed response, and the harness itself runs in
--selfcheck mode (scripted responses). Proves: every request through the
public SDK paths both arms use (models.generate_content, chats send_message,
including from a worker thread under a copied context) is recorded once into
the current case; responses are returned unchanged; the original method is
restored; usage totals never invent zeros; cost is computed only from a
documented price entry and consistent usage, and is otherwise "unavailable";
grading is unaffected and cost lives in its own artifact.
"""
import contextvars
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from google import genai
from google.genai import models as genai_models
from google.genai import types


def _load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HARNESS = _load("clinical_eval_harness_usage", ROOT / "scripts" / "clinical_eval_harness.py")
COST = _load("eval_cost_usage", ROOT / "scripts" / "eval_cost.py")


def _response(prompt=100, candidates=20, thoughts=None, tool_use=None, total=None, model="gemini-test-1"):
    total = total if total is not None else prompt + candidates + (thoughts or 0) + (tool_use or 0)
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text="ok")]))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt, candidates_token_count=candidates, thoughts_token_count=thoughts,
            tool_use_prompt_token_count=tool_use, total_token_count=total),
        model_version=model, response_id="resp-1")


class RecorderTests(unittest.TestCase):
    def test_public_sdk_paths_are_recorded_and_responses_unchanged(self):
        canned = _response(prompt=120, candidates=30, thoughts=50)
        stub = mock.Mock(return_value=canned)
        original = genai_models.Models.__dict__["_generate_content"]
        client = genai.Client(api_key="test-key-not-a-real-credential")
        with mock.patch.object(genai_models.Models, "_generate_content", lambda self, **kw: stub(**kw)):
            with HARNESS._record_usage():
                sink = []
                token = HARNESS._USAGE_SINK.set(sink)
                try:
                    r1 = client.models.generate_content(model="gemini-test", contents="hi")
                    chat = client.chats.create(model="gemini-test")
                    r2 = chat.send_message("hello")
                    # Control's pattern: send_message in a worker thread under a copied context.
                    ctx, out = contextvars.copy_context(), {}
                    t = threading.Thread(target=lambda: out.setdefault("r", ctx.run(chat.send_message, "again")))
                    t.start(); t.join()
                finally:
                    HARNESS._USAGE_SINK.reset(token)
                outside = client.models.generate_content(model="gemini-test", contents="no sink")
        self.assertIs(genai_models.Models.__dict__["_generate_content"], original)  # restored
        self.assertEqual(stub.call_count, 4)
        self.assertEqual(len(sink), 3)  # the call made with no sink is not recorded anywhere
        for response in (r1, r2, out["r"], outside):
            self.assertEqual(response.text, "ok")
        self.assertEqual(sink[0]["prompt_token_count"], 120)
        self.assertEqual(sink[0]["candidates_token_count"], 30)
        self.assertEqual(sink[0]["thoughts_token_count"], 50)
        self.assertEqual(sink[0]["total_token_count"], 200)
        self.assertEqual((sink[0]["model_version"], sink[0]["response_id"]), ("gemini-test-1", "resp-1"))

    def test_failed_request_is_recorded_and_the_error_propagates(self):
        class FakeModels:
            def _generate_content(self, **kwargs):
                raise ConnectionError("boom")
        sink = []
        with HARNESS._record_usage(FakeModels):
            token = HARNESS._USAGE_SINK.set(sink)
            try:
                with self.assertRaises(ConnectionError):
                    FakeModels()._generate_content(model="m", contents="x")
            finally:
                HARNESS._USAGE_SINK.reset(token)
        self.assertEqual(len(sink), 1)
        self.assertTrue(sink[0]["failed"])
        self.assertEqual(sink[0]["error_type"], "ConnectionError")

    def test_summary_never_invents_counts(self):
        calls = [HARNESS._usage_entry(_response(prompt=10, candidates=5)),
                 HARNESS._usage_entry(_response(prompt=7, candidates=3, thoughts=4))]
        s = HARNESS.summarize_usage(calls, "live")
        self.assertEqual(s["status"], "recorded")
        self.assertEqual(s["totals"]["prompt_token_count"], 17)
        self.assertEqual(s["totals"]["thoughts_token_count"], 4)
        self.assertIsNone(s["totals"]["tool_use_prompt_token_count"])  # never reported -> None, not 0
        missing = HARNESS._usage_entry(types.GenerateContentResponse())
        self.assertEqual(HARNESS.summarize_usage(calls + [missing], "live")["status"], "partial")
        self.assertEqual(HARNESS.summarize_usage([], "live")["status"], "no_api_calls_recorded")
        self.assertEqual(HARNESS.summarize_usage([], "selfcheck")["status"], "not_applicable_selfcheck")


PRICE_ENTRY = {"input_usd_per_1m_tokens": 1.0, "output_usd_per_1m_tokens": 10.0,
               "cached_input_usd_per_1m_tokens": None, "thoughts_billed_as_output": True,
               "tool_use_prompt_billed_as_input": True, "source_url": "https://example.invalid/pricing",
               "retrieved_on": "2026-10-04"}


def _raw(calls, arm="control", mode="live", model="gemini-test"):
    usage = HARNESS.summarize_usage(calls, mode)
    return {"metadata": {"arm": arm, "mode": mode, "provider": HARNESS.PROVIDER,
                         "settings": {arm: {"gemini_model": model}}},
            "records": [{"case_id": "c1", "repeat": 0, "latency_ms": 5, "usage": usage}]}


class CostTests(unittest.TestCase):
    def _prices(self, tmp, models):
        path = pathlib.Path(tmp) / "prices.json"
        path.write_text(json.dumps({"models": models}))
        return path

    def test_shipped_price_file_has_no_prices_so_cost_is_unavailable(self):
        report = COST.cost_report_for(_raw([HARNESS._usage_entry(_response())]))
        self.assertEqual(report["summary"]["cost_status"], "unavailable")
        self.assertIsNone(report["summary"]["total_cost_usd"])
        self.assertEqual(report["summary"]["usage_totals"]["prompt_token_count"], 100)
        self.assertEqual(json.loads((ROOT / "eval" / "pricing" / "gemini_prices.json").read_text())["models"], {})

    def test_cost_from_documented_prices(self):
        calls = [HARNESS._usage_entry(_response(prompt=1000, candidates=200, thoughts=300, tool_use=50,
                                                model="gemini-test-001"))]
        with tempfile.TemporaryDirectory() as tmp:
            report = COST.cost_report_for(_raw(calls), prices_path=self._prices(tmp, {"gemini-test": PRICE_ENTRY}))
        # input (1000 + 50 tool-use) * $1/M + output (200 + 300 thoughts) * $10/M
        self.assertEqual(report["summary"]["cost_status"], "computed")
        self.assertAlmostEqual(report["summary"]["total_cost_usd"], (1050 * 1.0 + 500 * 10.0) / 1_000_000)
        self.assertEqual(report["price_entry"]["source_url"], "https://example.invalid/pricing")

    def test_cost_is_unavailable_rather_than_guessed(self):
        good = HARNESS._usage_entry(_response())
        cases = {
            "unpriced model": ([good], {"other-model": PRICE_ENTRY}),
            "entry without source": ([good], {"gemini-test": {**PRICE_ENTRY, "source_url": ""}}),
            "inconsistent counts": ([HARNESS._usage_entry(_response(prompt=10, candidates=5, total=99))],
                                    {"gemini-test": PRICE_ENTRY}),
            "different model version": ([HARNESS._usage_entry(_response(model="gemini-other"))],
                                        {"gemini-test": PRICE_ENTRY}),
            "no usage metadata": ([HARNESS._usage_entry(types.GenerateContentResponse())],
                                  {"gemini-test": PRICE_ENTRY}),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, (calls, models) in cases.items():
                with self.subTest(label):
                    report = COST.cost_report_for(_raw(calls), prices_path=self._prices(tmp, models))
                    self.assertEqual(report["summary"]["cost_status"], "unavailable")
                    self.assertIsNone(report["summary"]["total_cost_usd"])
                    self.assertTrue(report["summary"]["reason"])

    def test_selfcheck_and_deterministic_runs_are_not_applicable(self):
        self.assertEqual(COST.cost_report_for(_raw([], mode="selfcheck"))["summary"]["cost_status"], "not_applicable")
        baseline_raw = {"metadata": {"arm": "baseline", "mode": "deterministic"},
                        "records": [{"case_id": "c1", "repeat": 0, "latency_ms": 1}]}
        self.assertEqual(COST.cost_report_for(baseline_raw)["summary"]["cost_status"], "not_applicable")


class HarnessRunTests(unittest.TestCase):
    def test_selfcheck_run_records_usage_and_writes_cost_separately_from_grading(self):
        dev = json.loads((ROOT / "eval" / "benchmarks" / "s1" / "s1_dev.json").read_text(encoding="utf-8"))
        original = genai_models.Models.__dict__["_generate_content"]
        key_before = os.environ.get("GEMINI_API_KEY")
        with tempfile.TemporaryDirectory() as tmp:
            case_file = pathlib.Path(tmp) / "cases.json"
            case_file.write_text(json.dumps({**dev, "cases": dev["cases"][:3]}, ensure_ascii=False), encoding="utf-8")
            for arm in ("control", "a0"):
                with self.subTest(arm=arm):
                    graded = HARNESS.run(arm, "selfcheck", case_file=case_file, output_dir=pathlib.Path(tmp) / arm)
                    raw = json.loads(pathlib.Path(graded["_paths"]["raw"]).read_text(encoding="utf-8"))
                    cost = json.loads(pathlib.Path(graded["_paths"]["cost"]).read_text(encoding="utf-8"))
                    self.assertEqual(raw["metadata"]["provider"], HARNESS.PROVIDER)
                    self.assertEqual(raw["metadata"]["usage_recording"]["hook"], HARNESS.USAGE_HOOK)
                    self.assertTrue(all(r["usage"]["status"] == "not_applicable_selfcheck" for r in raw["records"]))
                    self.assertTrue(all(isinstance(r["latency_ms"], int) for r in raw["records"]))
                    self.assertEqual(cost["summary"]["cost_status"], "not_applicable")
                    self.assertNotIn("cost", json.dumps(graded["summary"]).lower())
                    # grading is unaffected by the new raw fields: a regrade reproduces it exactly
                    regraded = HARNESS.build_graded_report(raw, HARNESS.load_case_file(case_file), None)
                    self.assertEqual(regraded["summary"], graded["summary"])
        self.assertIs(genai_models.Models.__dict__["_generate_content"], original)
        self.assertEqual(os.environ.get("GEMINI_API_KEY"), key_before)


if __name__ == "__main__":
    unittest.main()
