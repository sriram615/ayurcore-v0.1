"""
A0 parity -- the A0 architecture report's section 9 third required test:
"A0 output validates as ClinicalParse and survives normalize_herb."

This file does not re-test the bounds (see tests/test_a0_bounds.py) or the
import surface (see tests/test_a0_no_write_imports.py). It tests the one
thing that actually lets A0 stand in for agents.copilot.parse_clinical_notes
at backend/main.py's single call site: that whatever comes out of
agents.a0.propose is the SAME type, and survives the SAME downstream rails
(normalize_herb, grams bounds) that already run unconditionally on the
control arm's output at backend/main.py:288 and again at /prescribe/confirm.

Run directly (no pytest required):
    python tests/test_a0_parity.py
"""
import dataclasses
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


def test_agent_context_is_frozen_and_two_fields_only():
    """Report section 5's two rules about AgentContext: it is frozen (can't
    grow new fields at a call site) and carries exactly clinic_id and
    request_id -- no user_id, username, session_id, preview_id, or
    anything patient-identity-derived."""
    from agents.a0.context import AgentContext

    ctx = AgentContext(clinic_id="default_clinic", request_id="r1")
    field_names = {f.name for f in dataclasses.fields(ctx)}
    check("AgentContext has exactly {clinic_id, request_id}",
          field_names == {"clinic_id", "request_id"}, str(field_names))

    raised = None
    try:
        ctx.clinic_id = "other_clinic"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError as exc:
        raised = exc
    check("AgentContext is frozen -- a call site cannot mutate it", raised is not None)


def test_formulary_is_derived_from_herb_aliases_not_a_new_copy():
    """Report section 3/6: the formulary must come from
    backend.herb_resolver.HERB_ALIASES, not become a fifth hand-synced
    copy alongside SEED_HERBS, db/seed.py, and CLINIC_HERB_KEYTERMS."""
    from backend.herb_resolver import HERB_ALIASES
    from agents.a0.context import AgentContext, build_clinical_context

    ctx = AgentContext(clinic_id="default_clinic", request_id="r2")
    clinical_context = build_clinical_context(ctx)
    expected = sorted(set(HERB_ALIASES.values()))
    check("build_clinical_context's formulary is exactly HERB_ALIASES' canonical names",
          clinical_context.formulary == expected,
          f"got {clinical_context.formulary[:3]}..., expected {expected[:3]}...")
    check("stock_by_herb is a plain dict (from one bulk read, not per-herb tool calls)",
          isinstance(clinical_context.stock_by_herb, dict))


def test_offline_mock_output_is_a_valid_clinical_parse():
    """Every branch of the offline mock (agents.a0.orchestrator._mock_propose)
    must produce a ClinicalParse whose herbs all survive normalize_herb --
    exactly the same downstream check backend/main.py:288 runs
    unconditionally on either arm's output."""
    from backend.schemas import ClinicalParse
    from backend.herb_resolver import normalize_herb, UnknownHerbError
    from agents.a0.context import AgentContext
    import agents.a0.orchestrator as orch

    previous_key = os.environ.pop("GEMINI_API_KEY", None)
    try:
        ctx = AgentContext(clinic_id="default_clinic", request_id="r3")
        cases = [
            "notes mentioning Tulsi and Ashwagandha together",
            "notes with 9000g of Brahmi in them",
            "notes with 400g of Ashwagandha in them",
        ]
        for notes in cases:
            result = orch.propose(notes, ctx)
            check(f"mock branch for {notes!r} returns a ClinicalParse",
                  isinstance(result, ClinicalParse))
            for herb in result.prescribed_herbs:
                check(f"grams for {herb.name!r} is a positive integer",
                      isinstance(herb.grams, int) and herb.grams > 0, str(herb.grams))
                # Tulsi is deliberately unstocked (see
                # scripts/verify_malayalam_manglish_stress.py's own case 5
                # docstring) -- it is EXPECTED to fail normalize_herb here;
                # every other mock-branch herb must succeed.
                if herb.name == "Tulsi":
                    continue
                try:
                    canonical = normalize_herb(herb.name)
                    check(f"{herb.name!r} survives normalize_herb", True)
                    check(f"normalize_herb({herb.name!r}) is idempotent on its own output",
                          normalize_herb(canonical) == canonical)
                except UnknownHerbError:
                    check(f"{herb.name!r} survives normalize_herb", False,
                          f"UnknownHerbError for a herb the mock branch itself named")

        raised = None
        try:
            orch.propose("notes matching none of the three mock branches", ctx)
        except ValueError as exc:
            raised = exc
        check("an unmatched offline case raises AI_SERVICE_UNAVAILABLE, not a degraded proposal",
              raised is not None and str(raised) == "AI_SERVICE_UNAVAILABLE")
    finally:
        if previous_key is not None:
            os.environ["GEMINI_API_KEY"] = previous_key


def test_live_shaped_output_survives_normalize_herb():
    """A scripted (fake-client) run through the real tool-calling path --
    not the offline mock -- to prove the parity property holds for the
    live code path too, not just the three hardcoded stub branches."""
    from backend.schemas import ClinicalParse
    from backend.herb_resolver import normalize_herb
    from agents.a0.context import AgentContext
    import agents.a0.orchestrator as orch
    import google.genai as genai_real

    class _FakeFunctionCall:
        def __init__(self, name, args):
            self.name = name
            self.args = args

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

    valid_json = (
        '{"dosha_imbalance": "Vata-Pitta", "prescribed_herbs": '
        '[{"name": "Brahmi", "grams": 15}, {"name": "Shatavari", "grams": 10}]}'
    )
    responses = [
        _FakeResponse(parts=[_FakePart(function_call=_FakeFunctionCall(
            "check_herb_alias", {"raw_name": "brahmi"}))]),
        _FakeResponse(parts=[_FakePart(function_call=_FakeFunctionCall(
            "check_herb_alias", {"raw_name": "shatavari"}))]),
        _FakeResponse(parts=[], text=valid_json),
    ]

    class _FakeModels:
        def __init__(self, responses):
            self._responses = list(responses)

        def generate_content(self, model, contents, config):
            return self._responses.pop(0)

    class _FakeClient:
        def __init__(self, api_key):
            self.models = _FakeModels(responses)

    original_client = genai_real.Client
    genai_real.Client = _FakeClient
    previous_key = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "a0-parity-test-not-a-real-key"
    try:
        result = orch.propose("some tokenized notes", AgentContext(clinic_id="default_clinic", request_id="r4"))
        check("the scripted live-shaped run returns a ClinicalParse", isinstance(result, ClinicalParse))
        check("dosha_imbalance round-trips exactly", result.dosha_imbalance == "Vata-Pitta")
        names = {h.name for h in result.prescribed_herbs}
        check("both prescribed herbs round-trip", names == {"Brahmi", "Shatavari"})
        for herb in result.prescribed_herbs:
            canonical = normalize_herb(herb.name)
            check(f"{herb.name!r} survives normalize_herb and is already canonical",
                  canonical == herb.name)
    finally:
        genai_real.Client = original_client
        if previous_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = previous_key


_ALL_TESTS = [
    test_agent_context_is_frozen_and_two_fields_only,
    test_formulary_is_derived_from_herb_aliases_not_a_new_copy,
    test_offline_mock_output_is_a_valid_clinical_parse,
    test_live_shaped_output_survives_normalize_herb,
]


def run_tests():
    print("--- A0 parity: ClinicalParse shape + normalize_herb survival -- focused verification ---\n")
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
