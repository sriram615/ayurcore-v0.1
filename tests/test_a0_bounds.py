"""
A0 bounds -- focused regression tests for agents/a0/orchestrator.py's state
machine (A0 architecture report, section 7, and section 9's required test
"tests/test_a0_bounds.py": "Tool-call cap, reasoning cap, and deadline are
each enforced; each failure maps to the same ValueError strings main.py
already handles").

Every scenario here uses a FAKE google.genai.Client -- no network call, no
real API key, no quota spent -- exactly the pattern
tests/test_a0_step0_hardening.py and this project's other test files
already use for agents/copilot.py's genai.Client. Patching
google.genai.Client (not agents.a0.orchestrator.genai.Client) is
deliberate: orchestrator.py imports `from google import genai` LOCALLY,
inside propose(), specifically so this file's top-level imports stay
SDK-free; patching the shared google.genai module's own Client attribute
reaches that local import regardless of where in the function body it
happens, because Python caches the module object and the local `import`
statement is just a fresh attribute lookup on it.

Run directly (no pytest required):
    python tests/test_a0_bounds.py
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Raises AssertionError immediately on a failed assertion -- same
    convention as every other test file in this project (see
    tests/test_rbac.py's module docstring, "PYTEST COMPATIBILITY")."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


# --- Fakes: a scripted google.genai.Client, AFC disabled, no network ------

class _FakeFunctionCall:
    def __init__(self, name, args=None):
        self.name = name
        self.args = args or {}


class _FakePart:
    def __init__(self, function_call=None):
        self.function_call = function_call


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeCandidate:
    def __init__(self, parts):
        self.content = _FakeContent(parts)


class _FakeResponse:
    def __init__(self, parts, text=None):
        self.candidates = [_FakeCandidate(parts)]
        self.text = text


def _tool_call_response(name: str, args: dict) -> _FakeResponse:
    return _FakeResponse(parts=[_FakePart(function_call=_FakeFunctionCall(name, args))])


def _final_text_response(text: str) -> _FakeResponse:
    return _FakeResponse(parts=[], text=text)


class _FakeModels:
    """Scripted client.models.generate_content -- returns the next
    response off a fixed list per call, and counts calls so tests can
    assert the loop actually stopped rather than running away."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def generate_content(self, model, contents, config):
        self.calls += 1
        if not self._responses:
            raise RuntimeError("test bug: scripted response list exhausted")
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.models = _FakeModels(responses)


def _patch_genai_client(responses):
    """Patches the real google.genai module's Client attribute (not
    agents.a0.orchestrator's -- see module docstring) to return a fake
    client scripted with `responses`. Returns a restore function."""
    import google.genai as genai_real

    original = genai_real.Client
    fake = _FakeClient(responses)
    genai_real.Client = lambda api_key: fake

    def restore():
        genai_real.Client = original

    return fake, restore


def _with_api_key():
    """Context-manager-less helper: sets a fake GEMINI_API_KEY for the
    duration of a call (agents.a0.orchestrator.propose falls back to its
    offline mock entirely when this is unset) and returns a restore
    function."""
    previous = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "a0-bounds-test-not-a-real-key"

    def restore():
        if previous is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = previous

    return restore


# --- Tests -----------------------------------------------------------------

def test_tool_call_cap_is_enforced():
    """MAX_TOOL_CALLS = 6. A model that keeps asking for one more tool call
    forever must be cut off, not run away -- and the caller must see the
    same AI_SERVICE_UNAVAILABLE string every other Gemini failure mode
    already maps to, so backend/main.py's existing 502 + X-Error-Code
    handling covers this failure mode with no changes."""
    import agents.a0.orchestrator as orch
    from agents.a0.context import AgentContext

    # More scripted tool-call responses than the budget could ever allow,
    # so if the cap did NOT work, the loop would exhaust this list and
    # raise a test-harness RuntimeError instead of the expected ValueError.
    responses = [
        _tool_call_response("check_herb_alias", {"raw_name": f"herb_{i}"})
        for i in range(20)
    ]
    fake, restore_client = _patch_genai_client(responses)
    restore_key = _with_api_key()
    try:
        raised = None
        try:
            orch.propose("some notes", AgentContext(clinic_id="default_clinic", request_id="r1"))
        except ValueError as exc:
            raised = exc
        check("a runaway tool-calling model raises, rather than looping forever",
              raised is not None)
        check("the tool-call cap maps to AI_SERVICE_UNAVAILABLE, the existing failure string",
              str(raised) == "AI_SERVICE_UNAVAILABLE", str(raised))
        check("the loop actually stopped near the 6-call budget, not near the 20-response list",
              fake.models.calls <= 9, f"generate_content was called {fake.models.calls} times")
    finally:
        restore_client()
        restore_key()


def test_reasoning_cap_allows_exactly_one_repair():
    """MAX_REASONING_CALLS = 2: one initial attempt, one repair on
    malformed JSON. A model that returns unparseable text twice in a row
    must fail with INVALID_AI_SCHEMA -- the same string
    parse_clinical_notes already raises for the identical situation, and
    the same existing 422 backend/main.py already returns for it."""
    import agents.a0.orchestrator as orch
    from agents.a0.context import AgentContext

    responses = [
        _final_text_response("this is not json"),
        _final_text_response("still not json, even after the repair prompt"),
    ]
    fake, restore_client = _patch_genai_client(responses)
    restore_key = _with_api_key()
    try:
        raised = None
        try:
            orch.propose("some notes", AgentContext(clinic_id="default_clinic", request_id="r2"))
        except ValueError as exc:
            raised = exc
        check("two consecutive malformed replies raise", raised is not None)
        check("repair-exhausted maps to INVALID_AI_SCHEMA, the existing failure string",
              str(raised) == "INVALID_AI_SCHEMA", str(raised))
        check("exactly two reasoning calls were made (one initial, one repair)",
              fake.models.calls == 2, f"generate_content was called {fake.models.calls} times")
    finally:
        restore_client()
        restore_key()


def test_repair_succeeds_when_the_second_reply_is_valid():
    """The repair path isn't just a failure mode -- a model that fixes its
    own malformed JSON on the one allowed retry must still succeed."""
    import agents.a0.orchestrator as orch
    from agents.a0.context import AgentContext

    valid_json = '{"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}]}'
    responses = [
        _final_text_response("not valid json the first time"),
        _final_text_response(valid_json),
    ]
    fake, restore_client = _patch_genai_client(responses)
    restore_key = _with_api_key()
    try:
        result = orch.propose("some notes", AgentContext(clinic_id="default_clinic", request_id="r3"))
        check("the repaired reply is accepted", result.dosha_imbalance == "Vata")
        check("the repaired reply's herb survives", result.prescribed_herbs[0].name == "Ashwagandha")
        check("exactly two reasoning calls were made", fake.models.calls == 2, str(fake.models.calls))
    finally:
        restore_client()
        restore_key()


def test_total_deadline_is_enforced():
    """TOTAL_DEADLINE_SECONDS = 40, monotonic, checked before every model
    and tool call (report section 7). An already-expired budget must fail
    fast, before spending a single generate_content call -- not merely
    get flagged after the fact."""
    import agents.a0.orchestrator as orch
    from agents.a0.context import AgentContext

    original_budget = orch.GEMINI_TOTAL_BUDGET_SECONDS
    # A negative budget means time.monotonic() + budget is already in the
    # past the instant propose() computes its deadline.
    orch.GEMINI_TOTAL_BUDGET_SECONDS = -1.0
    responses = [_tool_call_response("check_herb_alias", {"raw_name": "ashwagandha"})]
    fake, restore_client = _patch_genai_client(responses)
    restore_key = _with_api_key()
    try:
        raised = None
        try:
            orch.propose("some notes", AgentContext(clinic_id="default_clinic", request_id="r4"))
        except ValueError as exc:
            raised = exc
        check("an already-expired deadline raises", raised is not None)
        check("deadline exhaustion maps to AI_SERVICE_UNAVAILABLE",
              str(raised) == "AI_SERVICE_UNAVAILABLE", str(raised))
        check("no generate_content call was made once the deadline had already passed",
              fake.models.calls == 0, f"generate_content was called {fake.models.calls} times")
    finally:
        orch.GEMINI_TOTAL_BUDGET_SECONDS = original_budget
        restore_client()
        restore_key()


def test_deadline_uses_a_monotonic_clock():
    """Same reasoning as agents/copilot.py's own deadline (see
    tests/test_a0_step0_hardening.py::test_deadline_uses_a_monotonic_clock):
    the check must be time.monotonic()-based, immune to wall-clock
    adjustments, not time.time()-based."""
    import inspect
    import agents.a0.orchestrator as orch

    source = inspect.getsource(orch._deadline_exceeded)
    check("the deadline check uses time.monotonic()", "time.monotonic()" in source)
    check("the deadline check does not use time.time()", "time.time()" not in source)


def test_offline_mock_behaviour_is_unchanged():
    """With GEMINI_API_KEY unset, A0 must fall back to a hardcoded stub --
    identical in shape to agents/copilot.py's -- and must never construct
    a genai.Client at all (see this module's own docstring: the exact
    'offline tests silently start hitting the network' failure mode
    tests/test_golden.py already documents this project having been
    burned by)."""
    import agents.a0.orchestrator as orch
    from agents.a0.context import AgentContext

    previous_key = os.environ.pop("GEMINI_API_KEY", None)

    def _client_should_not_be_constructed(api_key):
        raise AssertionError("genai.Client was constructed despite GEMINI_API_KEY being unset")

    import google.genai as genai_real
    original_client = genai_real.Client
    genai_real.Client = _client_should_not_be_constructed
    try:
        ctx = AgentContext(clinic_id="default_clinic", request_id="r5")
        result = orch.propose("Patient has Tulsi and Ashwagandha mentioned.", ctx)
        check("the mock stub's Vata branch fires for the same substrings copilot.py's does",
              result.dosha_imbalance == "Vata")

        raised = None
        try:
            orch.propose("notes matching none of the three mock branches", ctx)
        except ValueError as exc:
            raised = exc
        check("an unmatched mock case still raises AI_SERVICE_UNAVAILABLE",
              raised is not None and str(raised) == "AI_SERVICE_UNAVAILABLE")
    finally:
        genai_real.Client = original_client
        if previous_key is not None:
            os.environ["GEMINI_API_KEY"] = previous_key


def test_automatic_function_calling_is_disabled():
    """The whole point of this orchestrator (module docstring) is taking
    the tool-calling loop back from the SDK. If a future edit re-enabled
    AFC, MAX_TOOL_CALLS/MAX_REASONING_CALLS would stop being real bounds
    and silently become dead code the SDK's own loop never consults."""
    import inspect
    import agents.a0.orchestrator as orch

    source = inspect.getsource(orch.propose)
    check("propose() explicitly disables automatic function calling",
          "AutomaticFunctionCallingConfig(disable=True)" in source)


_ALL_TESTS = [
    test_tool_call_cap_is_enforced,
    test_reasoning_cap_allows_exactly_one_repair,
    test_repair_succeeds_when_the_second_reply_is_valid,
    test_total_deadline_is_enforced,
    test_deadline_uses_a_monotonic_clock,
    test_offline_mock_behaviour_is_unchanged,
    test_automatic_function_calling_is_disabled,
]


def run_tests():
    print("--- A0 bounds: tool-call cap, reasoning cap, deadline -- focused verification ---\n")
    failures = []
    for fn in _ALL_TESTS:
        try:
            fn()
        except AssertionError as e:
            failures.append(fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {fn.__name__} ({e})")
        print()
    print(f"--- {_passed} passed, {_failed} failed ---")
    if failures:
        print(f"--- {len(failures)} test function(s) had a failing assertion: {', '.join(failures)} ---")
    return failures


if __name__ == "__main__":
    if run_tests():
        sys.exit(1)
