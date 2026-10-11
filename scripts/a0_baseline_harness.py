"""
A0 -- CONTROL/TREATMENT BASELINE HARNESS (read-only; changes no production code).

Runs either arm -- agents.copilot.parse_clinical_notes (control) or
agents.a0.propose (treatment) -- against the existing labeled cases in
scripts/verify_malayalam_manglish_stress.py and records one structured
result per case, in the same shape, so the two arms' output files are
directly diffable.

    # control arm, real baseline (needs GEMINI_API_KEY; ~9 minutes for 8 cases)
    python scripts/a0_baseline_harness.py --arm control

    # treatment arm, real baseline (same corpus, same GEMINI_API_KEY)
    python scripts/a0_baseline_harness.py --arm a0

    # harness self-check, no network, no quota (uses each arm's own offline mock)
    python scripts/a0_baseline_harness.py --arm control --mock
    python scripts/a0_baseline_harness.py --arm a0 --selfcheck

    # skip the free-tier quota spacing (only if you are on a paid tier)
    python scripts/a0_baseline_harness.py --arm a0 --no-wait

--arm defaults to "control" -- running this script with no flags at all
reproduces exactly the Step 0 baseline's original behavior, unchanged.

Output: artifacts/a0_step0/baseline_<UTC timestamp>_<arm>_<fingerprint7>_<mode>.json
        plus a human-readable table on stdout.


WHY THIS CALLS THE PARSER FUNCTIONS DIRECTLY, NOT POST /prescribe/preview
--------------------------------------------------------------------------
Three reasons, all deliberate, and identical for both arms:

  1. parse_clinical_notes()/propose() ARE the seam A0 replaces. Measuring
     them directly is apples-to-apples; going through the endpoint would
     add auth, tokenization and preview persistence to both arms equally
     -- noise that cannot change the comparison but can fail it.

  2. It keeps this harness genuinely read-only. POST /prescribe/preview
     writes a row to prescription_previews on every success. Eight cases
     per run, every run, times two arms, is real pollution of a table the
     A5 audit story reads. This harness writes nothing to the database at
     all; the only database access is the read-only stock lookups the
     tools/context themselves perform.

  3. It captures the model's RAW herb names, before normalize_herb()
     rejects them. Through the endpoint an invented herb is a 400 and the
     name is lost. Hallucination rate is the metric that matters most
     here, so the raw names have to survive. This harness applies
     normalize_herb() itself, afterwards, and records both.

NOTE ON scripts/verify_malayalam_manglish_stress.py
---------------------------------------------------
That script posts to /prescribe/preview with no Authorization header. Since
A2 made authentication mandatory on that endpoint it would now return 401 on
every case. This harness does not fix, modify or import-execute that script
-- it reads the CASES list out of it with ast.literal_eval so the corpus has
exactly one definition and stays where it already lives.

WHAT THIS FILE IS AND ISN'T
----------------------------
This is tooling, not production code -- it lives outside agents/copilot.py
and agents/a0/ and imports both read-only, never modifying either. Adding
the --arm dispatch below only changes THIS file; agents/copilot.py and
every file under agents/a0/ are untouched by this change.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone

# First, not last: see the same line in scripts/clinical_eval_harness.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
STRESS_SCRIPT = REPO_ROOT / "scripts" / "verify_malayalam_manglish_stress.py"
COPILOT_PATH = REPO_ROOT / "agents" / "copilot.py"
A0_DIR = REPO_ROOT / "agents" / "a0"
OUTPUT_DIR = REPO_ROOT / "artifacts" / "a0_step0"

DEFAULT_SECONDS_BETWEEN_CASES = 65
ARMS = ("control", "a0")


def load_cases() -> list[dict]:
    """Extracts the CASES list from the stress script WITHOUT importing it.

    Importing would execute the script's top-level body: it sys.exit(1)s
    without GEMINI_API_KEY, then runs all 8 real Gemini calls itself. Parsing
    the AST gets the corpus with no side effects and keeps the stress script
    as the single source of truth for it.
    """
    tree = ast.parse(STRESS_SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "CASES":
                    return ast.literal_eval(node.value)
    raise RuntimeError(f"Could not find a CASES assignment in {STRESS_SCRIPT}")


def copilot_fingerprint() -> tuple[str, str]:
    """SHA-256 of agents/copilot.py, so a baseline captured before the Step 0
    hardening can never be silently confused with one captured after it."""
    digest = hashlib.sha256(COPILOT_PATH.read_bytes()).hexdigest()
    return digest, digest[:7]


def a0_fingerprint() -> tuple[str, str]:
    """SHA-256 over the concatenation of every file under agents/a0/, sorted
    by path for a stable digest -- the treatment arm's equivalent of
    copilot_fingerprint() above, so an a0 baseline records exactly which
    version of the treatment arm produced it."""
    hasher = hashlib.sha256()
    for path in sorted(A0_DIR.rglob("*.py")):
        hasher.update(str(path.relative_to(REPO_ROOT)).encode("utf-8"))
        hasher.update(path.read_bytes())
    digest = hasher.hexdigest()
    return digest, digest[:7]


# --- Tool-call counting, generalized across both arms ----------------------
#
# agents/copilot.py's tools (check_herb_alias, check_stock) are looked up as
# plain module-global names each time parse_clinical_notes() builds its
# tools=[...] list, so wrapping copilot.check_herb_alias via setattr (an
# ATTRIBUTE patch point) is enough to intercept them.
#
# agents/a0/orchestrator.py is different: it imports check_herb_alias once,
# at module load time, into its own _TOOL_FUNCTIONS dict -- an attribute
# patch on agents.a0.tools.check_herb_alias would never be seen, because
# orchestrator's dict already holds a direct reference to the pre-patch
# function object. The dict entry itself is the real interception point (a
# DICT patch point), since _run_tool() does a fresh `_TOOL_FUNCTIONS.get(name)`
# lookup on every call.

class _AttrPatchPoint:
    """Patches `getattr(obj, name)` / `setattr(obj, name, ...)` -- the shape
    agents/copilot.py's own module-global tool functions need."""

    def __init__(self, obj, name):
        self._obj = obj
        self._name = name

    def get(self):
        return getattr(self._obj, self._name)

    def set(self, value):
        setattr(self._obj, self._name, value)


class _DictPatchPoint:
    """Patches `d[key]` -- the shape agents/a0/orchestrator.py's
    _TOOL_FUNCTIONS dict needs, since its values were bound once at import
    time and an attribute patch elsewhere would never be consulted again."""

    def __init__(self, mapping, key):
        self._mapping = mapping
        self._key = key

    def get(self):
        return self._mapping[self._key]

    def set(self, value):
        self._mapping[self._key] = value


class _ToolCallCounter:
    """Counts tool invocations by wrapping each given (patch_point, tool_name)
    pair for the duration of a run, restoring the original afterwards -- the
    same technique every test file in this project already uses on
    genai.Client, generalized here so one counter class serves both arms'
    different tool-lookup shapes.

    Counting matters because the report's A/B design needs tool calls per
    case, and for the control arm the only pre-existing instrumentation is a
    print() behind AYURCORE_LOG_TOOL_CALLS.
    """

    def __init__(self, patch_points: list[tuple[object, str]]):
        self._patch_points = patch_points
        self._originals: list[object] = []
        self.calls: list[dict] = []

    def __enter__(self):
        self._originals = []
        for point, name in self._patch_points:
            original = point.get()
            self._originals.append(original)
            point.set(self._wrap(name, original))
        return self

    def _wrap(self, name, original):
        def wrapper(*args, **kwargs):
            started = time.monotonic()
            try:
                result = original(*args, **kwargs)
                return result
            finally:
                self.calls.append({
                    "tool": name,
                    "args": [str(a) for a in args],
                    "kwargs": {k: str(v) for k, v in kwargs.items()},
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                })

        wrapper.__name__ = getattr(original, "__name__", name)
        wrapper.__doc__ = getattr(original, "__doc__", None)
        wrapper.__annotations__ = getattr(original, "__annotations__", {})
        return wrapper

    def reset(self):
        self.calls = []

    def __exit__(self, *exc):
        for (point, _name), original in zip(self._patch_points, self._originals):
            point.set(original)
        return False


def _tool_counter_for(arm: str, copilot_module, a0_orchestrator_module) -> _ToolCallCounter:
    if arm == "control":
        return _ToolCallCounter([
            (_AttrPatchPoint(copilot_module, "check_herb_alias"), "check_herb_alias"),
            (_AttrPatchPoint(copilot_module, "check_stock"), "check_stock"),
        ])
    return _ToolCallCounter([
        (_DictPatchPoint(a0_orchestrator_module._TOOL_FUNCTIONS, "check_herb_alias"), "check_herb_alias"),
    ])


def grade(case: dict, actual_dosha: str | None, actual_herbs: dict | None) -> dict:
    """Computes every per-case metric the Step 0 brief asks for. Identical
    for both arms -- this is the shared scoring function that makes the two
    arms' output files comparable at all.

    A case whose expected_dosha_contains is None is the deliberately
    ambiguous one -- it is recorded as JUDGE_YOURSELF and never counted as a
    pass or a fail, exactly as the stress script already treats it.
    """
    from backend.herb_resolver import UnknownHerbError, normalize_herb

    expected_dosha = case.get("expected_dosha_contains")
    expected_herbs = case.get("expected_herbs")

    unresolvable = []
    if actual_herbs:
        for name in actual_herbs:
            try:
                normalize_herb(name)
            except UnknownHerbError:
                unresolvable.append(name)

    if expected_dosha is None:
        return {
            "auto_graded": False,
            "outcome": "JUDGE_YOURSELF",
            "dosha_ok": None,
            "herbs_ok": None,
            "dosage_differences": {},
            "hallucinated_herbs": unresolvable,
            "dropped_expected_herbs": [],
            "unexpected_extra_herbs": [],
        }

    dosha_ok = bool(actual_dosha) and expected_dosha.lower() in actual_dosha.lower()
    actual_herbs = actual_herbs or {}
    herbs_ok = actual_herbs == expected_herbs

    dosage_differences = {}
    for herb, expected_grams in (expected_herbs or {}).items():
        if herb in actual_herbs and actual_herbs[herb] != expected_grams:
            dosage_differences[herb] = {
                "expected_grams": expected_grams,
                "actual_grams": actual_herbs[herb],
                "delta_grams": actual_herbs[herb] - expected_grams,
            }

    dropped = [h for h in (expected_herbs or {}) if h not in actual_herbs]
    extra = [h for h in actual_herbs if h not in (expected_herbs or {})]

    return {
        "auto_graded": True,
        "outcome": "PASS" if (dosha_ok and herbs_ok) else "FAIL",
        "dosha_ok": dosha_ok,
        "herbs_ok": herbs_ok,
        "dosage_differences": dosage_differences,
        "hallucinated_herbs": unresolvable,
        "dropped_expected_herbs": dropped,
        "unexpected_extra_herbs": extra,
    }


def _scripted_payload_for(case: dict, index: int) -> tuple[dict, list[str]]:
    """Crafts a deterministic response per case chosen to drive every metric
    at least once: exact passes, a dosage difference, a dropped herb, a
    hallucinated herb, and the judge-yourself branch. Shared by both arms'
    --selfcheck mode, so the same scripted scenario exercises both."""
    expected_herbs = case.get("expected_herbs") or {"Ashwagandha": 20}
    dosha = case.get("expected_dosha_contains") or "Vata-Pitta"
    herbs = dict(expected_herbs)

    if index == 1:  # perturb one dosage -> exercises dosage_differences
        first = next(iter(herbs))
        herbs[first] = herbs[first] + 5
    elif index == 3:  # drop one herb -> exercises dropped_expected_herbs
        if len(herbs) > 1:
            herbs.pop(next(iter(herbs)))
    elif index == 5:  # invent a herb -> exercises hallucinated_herbs
        herbs["Moonpetal Root"] = 7

    return (
        {"dosha_imbalance": dosha, "prescribed_herbs": [{"name": k, "grams": v} for k, v in herbs.items()]},
        list(expected_herbs.keys()),
    )


# --- Control-arm --selfcheck scripting --------------------------------------
#
# agents/copilot.py drives Gemini via chat.send_message(prompt) -- a single
# blocking call that runs the SDK's own automatic function-calling loop
# internally. This fake mirrors exactly that shape: send_message() itself
# invokes the (possibly counter-wrapped) tool functions directly, then
# returns the scripted final JSON payload as response.text.

class _ControlScriptedChat:
    """Used ONLY by --arm control --selfcheck. It never runs during a real
    baseline."""

    def __init__(self, module, payload: dict, tool_herbs: list[str]):
        self._module = module
        self._payload = payload
        self._tool_herbs = tool_herbs

    def send_message(self, prompt):
        for raw in self._tool_herbs:
            alias = self._module.check_herb_alias(raw)
            if alias.get("known"):
                self._module.check_stock(alias["canonical_name"])

        class _Resp:
            pass

        resp = _Resp()
        resp.text = json.dumps(self._payload)
        return resp


# --- Treatment-arm --selfcheck scripting ------------------------------------
#
# agents/a0/orchestrator.py drives Gemini differently on purpose (see that
# module's own docstring): automatic function calling is DISABLED, and it
# calls client.models.generate_content(model, contents, config) itself, one
# round per iteration, inspecting response.candidates[0].content.parts for
# function_call parts it must execute itself. This fake mirrors THAT shape
# instead -- a small, duck-typed stand-in for the real
# google.genai.types.GenerateContentResponse/Candidate/Content/Part objects,
# identical to the one tests/test_a0_bounds.py already uses to test the
# orchestrator in isolation.

class _A0FakeFunctionCall:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _A0FakePart:
    def __init__(self, function_call=None):
        self.function_call = function_call


class _A0FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _A0FakeCandidate:
    def __init__(self, parts):
        self.content = _A0FakeContent(parts)


class _A0FakeResponse:
    def __init__(self, parts, text=None):
        self.candidates = [_A0FakeCandidate(parts)]
        self.text = text


class _A0ScriptedModels:
    """Used ONLY by --arm a0 --selfcheck. One function-call response per
    tool_herb (so the counter and _run_tool both get exercised the same way
    the control arm's scripted chat exercises check_herb_alias/check_stock),
    then a final text-only response carrying the scripted JSON payload."""

    def __init__(self, tool_herbs: list[str], payload: dict):
        self._responses = [
            _A0FakeResponse(parts=[_A0FakePart(function_call=_A0FakeFunctionCall(
                "check_herb_alias", {"raw_name": raw}))])
            for raw in tool_herbs
        ] + [_A0FakeResponse(parts=[], text=json.dumps(payload))]

    def generate_content(self, model, contents, config):
        if not self._responses:
            # Defensive only: the orchestrator asked for more rounds than
            # this script provides. Ending the conversation here (rather
            # than raising) lets the run continue and surfaces as a real,
            # visible INVALID_AI_SCHEMA/empty-answer case instead of a
            # test-harness crash unrelated to what's being measured.
            return _A0FakeResponse(parts=[], text="")
        return self._responses.pop(0)


class _A0ScriptedClient:
    def __init__(self, api_key, tool_herbs, payload):
        self.models = _A0ScriptedModels(tool_herbs, payload)


def _parse_control(copilot_module, sanitized: str, clinic_id: str):
    return copilot_module.parse_clinical_notes(sanitized, clinic_id=clinic_id)


def _parse_a0(a0_module, sanitized: str, clinic_id: str, request_id: str):
    ctx = a0_module.AgentContext(clinic_id=clinic_id, request_id=request_id)
    return a0_module.propose(sanitized, ctx)


def run(arm: str, mock: bool, wait_seconds: int, selfcheck: bool = False) -> dict:
    if arm not in ARMS:
        raise ValueError(f"arm must be one of {ARMS}, got {arm!r}")

    import agents.copilot as copilot
    import agents.a0 as a0
    import agents.a0.orchestrator as a0_orchestrator
    from backend.pii_sanitizer import assert_no_pii_leak, tokenize_payload
    from db.database import DEFAULT_CLINIC_ID

    cases = load_cases()
    copilot_sha256, copilot_sha7 = copilot_fingerprint()
    a0_sha256, a0_sha7 = a0_fingerprint()
    fingerprint7 = copilot_sha7 if arm == "control" else a0_sha7

    if selfcheck:
        wait_seconds = 0
    elif mock:
        os.environ.pop("GEMINI_API_KEY", None)
        wait_seconds = 0
    elif not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set. Either export it for a real baseline,")
        print("or pass --mock / --selfcheck to exercise the harness without one.")
        return {}

    if selfcheck:
        mode = "SELFCHECK (scripted deterministic responses, no network)"
    elif mock:
        mode = f"MOCK ({'copilot' if arm == 'control' else 'agents.a0'} offline stub, no network)"
    else:
        mode = "LIVE (real Gemini calls)"

    arm_label = "CONTROL (agents.copilot.parse_clinical_notes, unmodified)" if arm == "control" \
        else "TREATMENT (agents.a0.propose)"

    print("=" * 100)
    print("AyurCore A0 -- BASELINE")
    print("=" * 100)
    print(f"  arm                      : {arm_label}")
    print(f"  mode                     : {mode}")
    print(f"  agents/copilot.py sha256 : {copilot_sha256}")
    print(f"  agents/a0/*    sha256    : {a0_sha256}")
    print(f"  GEMINI_MODEL             : {copilot.GEMINI_MODEL}")
    print(f"  GEMINI_TIMEOUT_SECONDS   : {copilot.GEMINI_TIMEOUT_SECONDS}")
    print(f"  cases                    : {len(cases)} (from {STRESS_SCRIPT.name})")
    print(f"  spacing between cases    : {wait_seconds}s")
    print()

    results = []
    with _tool_counter_for(arm, copilot, a0_orchestrator) as counter:
        for index, case in enumerate(cases):
            if index > 0 and wait_seconds:
                print(f"  (waiting {wait_seconds}s for the free-tier quota window...)")
                time.sleep(wait_seconds)

            case_id = f"case_{index + 1:02d}"
            print("-" * 100)
            print(f"[{index + 1}/{len(cases)}] {case_id} -- {case['label']}")

            # Tokenize exactly the way backend/main.py does, so the parser
            # sees the same shape of input it sees in production. The corpus
            # uses a fixed placeholder patient name; no real identity here.
            pii_values = ["Test Patient"]
            sanitized, _mapping = tokenize_payload(case["notes"], pii_values)
            assert_no_pii_leak(sanitized, pii_values)

            counter.reset()
            actual_dosha = None
            actual_herbs = None
            error = None

            restore_scripted = None
            if selfcheck:
                payload, tool_herbs = _scripted_payload_for(case, index)
                previous_key = os.environ.get("GEMINI_API_KEY")
                os.environ["GEMINI_API_KEY"] = "selfcheck-key-not-a-real-credential"

                import google.genai as genai_real
                real_client = genai_real.Client

                if arm == "control":
                    class _ScriptedChats:
                        def create(self, model, config):
                            return _ControlScriptedChat(copilot, payload, tool_herbs)

                    class _ScriptedClient:
                        def __init__(self, api_key):
                            self.chats = _ScriptedChats()

                    genai_real.Client = _ScriptedClient
                else:
                    genai_real.Client = lambda api_key: _A0ScriptedClient(api_key, tool_herbs, payload)

                def restore_scripted():
                    genai_real.Client = real_client
                    if previous_key is None:
                        os.environ.pop("GEMINI_API_KEY", None)
                    else:
                        os.environ["GEMINI_API_KEY"] = previous_key

            started = time.monotonic()
            try:
                if arm == "control":
                    parsed = _parse_control(copilot, sanitized, DEFAULT_CLINIC_ID)
                else:
                    parsed = _parse_a0(a0, sanitized, DEFAULT_CLINIC_ID, case_id)
                actual_dosha = parsed.dosha_imbalance
                actual_herbs = {h.name: h.grams for h in parsed.prescribed_herbs}
            except ValueError as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - a baseline records whatever happened
                error = f"{type(exc).__name__}: {exc}"
            finally:
                if restore_scripted is not None:
                    restore_scripted()
            latency_ms = int((time.monotonic() - started) * 1000)

            graded = grade(case, actual_dosha, actual_herbs)
            if error:
                graded["outcome"] = "ERROR"

            tool_calls = list(counter.calls)
            record = {
                "case_id": case_id,
                "label": case["label"],
                "notes": case["notes"],
                "expected_dosha": case.get("expected_dosha_contains"),
                "actual_dosha": actual_dosha,
                "expected_herbs": case.get("expected_herbs"),
                "actual_herbs": actual_herbs,
                "error": error,
                "latency_ms": latency_ms,
                "tool_call_count": len(tool_calls),
                "tool_calls": tool_calls,
                **graded,
            }
            results.append(record)

            print(f"      expected dosha : {record['expected_dosha']}")
            print(f"      actual   dosha : {record['actual_dosha']}")
            print(f"      expected herbs : {record['expected_herbs']}")
            print(f"      actual   herbs : {record['actual_herbs']}")
            if record["dosage_differences"]:
                print(f"      dosage diffs   : {record['dosage_differences']}")
            if record["hallucinated_herbs"]:
                print(f"      HALLUCINATED   : {record['hallucinated_herbs']}")
            if record["dropped_expected_herbs"]:
                print(f"      dropped        : {record['dropped_expected_herbs']}")
            if record["unexpected_extra_herbs"]:
                print(f"      extra          : {record['unexpected_extra_herbs']}")
            if error:
                print(f"      ERROR          : {error}")
            print(f"      tool calls     : {len(tool_calls)}")
            print(f"      latency        : {latency_ms} ms")
            print(f"      outcome        : {record['outcome']}")

    auto = [r for r in results if r["auto_graded"]]
    summary = {
        "total_cases": len(results),
        "auto_graded_cases": len(auto),
        "passed": sum(1 for r in auto if r["outcome"] == "PASS"),
        "failed": sum(1 for r in auto if r["outcome"] == "FAIL"),
        "errored": sum(1 for r in results if r["outcome"] == "ERROR"),
        "judge_yourself": sum(1 for r in results if r["outcome"] == "JUDGE_YOURSELF"),
        "cases_with_hallucinated_herbs": sum(1 for r in results if r["hallucinated_herbs"]),
        "cases_with_dropped_herbs": sum(1 for r in results if r["dropped_expected_herbs"]),
        "total_tool_calls": sum(r["tool_call_count"] for r in results),
        "total_latency_ms": sum(r["latency_ms"] for r in results),
    }

    report = {
        "artifact": "AYURCORE_A0_BASELINE",
        "arm": arm,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "selfcheck" if selfcheck else ("mock" if mock else "live"),
        "parser": "agents/copilot.py::parse_clinical_notes" if arm == "control" else "agents/a0/orchestrator.py::propose",
        "copilot_sha256": copilot_sha256,
        "a0_sha256": a0_sha256,
        "gemini_model": copilot.GEMINI_MODEL,
        "gemini_timeout_seconds": copilot.GEMINI_TIMEOUT_SECONDS,
        "corpus_source": str(STRESS_SCRIPT.relative_to(REPO_ROOT)),
        "summary": summary,
        "results": results,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = "selfcheck" if selfcheck else ("mock" if mock else "live")
    out_path = OUTPUT_DIR / f"baseline_{stamp}_{arm}_{fingerprint7}_{suffix}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 100)
    print(f"{'Case':<10} {'Outcome':<16} {'Tools':>6} {'Latency':>10}  Label")
    print("-" * 100)
    for r in results:
        print(f"{r['case_id']:<10} {r['outcome']:<16} {r['tool_call_count']:>6} {r['latency_ms']:>8} ms  {r['label']}")
    print("-" * 100)
    print(f"  arm                     : {arm}")
    print(f"  auto-graded {summary['passed']}/{summary['auto_graded_cases']} passed"
          f"  |  {summary['failed']} failed"
          f"  |  {summary['errored']} errored"
          f"  |  {summary['judge_yourself']} judge-yourself")
    print(f"  hallucinated-herb cases : {summary['cases_with_hallucinated_herbs']}")
    print(f"  dropped-herb cases      : {summary['cases_with_dropped_herbs']}")
    print(f"  total tool calls        : {summary['total_tool_calls']}")
    print()
    print(f"  baseline artifact written to:")
    print(f"    {out_path.relative_to(REPO_ROOT)}")
    print()
    if mock or selfcheck:
        print("  NOTE: this was NOT a clinical baseline -- no real Gemini call was made.")
        print("  Re-run with a real GEMINI_API_KEY and no --mock/--selfcheck flag.")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="AyurCore A0 baseline harness (control or treatment arm)")
    parser.add_argument("--arm", choices=ARMS, default="control",
                        help="which parser to run: 'control' (agents.copilot, default) or "
                             "'a0' (agents.a0, the treatment arm)")
    parser.add_argument("--mock", action="store_true",
                        help="run against the selected arm's own offline stub (no network, no quota)")
    parser.add_argument("--selfcheck", action="store_true",
                        help="exercise every grading branch with scripted responses (no network)")
    parser.add_argument("--no-wait", action="store_true",
                        help="do not space cases out for the free-tier quota window")
    parser.add_argument("--wait", type=int, default=DEFAULT_SECONDS_BETWEEN_CASES,
                        help=f"seconds between cases (default {DEFAULT_SECONDS_BETWEEN_CASES})")
    args = parser.parse_args()

    wait = 0 if args.no_wait else args.wait
    report = run(arm=args.arm, mock=args.mock, wait_seconds=wait, selfcheck=args.selfcheck)
    return 0 if report else 1


if __name__ == "__main__":
    sys.exit(main())
