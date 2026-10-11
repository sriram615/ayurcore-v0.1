"""
A0's bounded agent state machine (A0 architecture report, sections 5, 7, 9).

propose(notes_text, ctx) -> ClinicalParse is this package's entire public
surface -- a second implementation of agents/copilot.parse_clinical_notes's
exact signature-and-return-type contract, selected by backend/main.py
behind AYURCORE_A0_ENABLED (default off, see report section 9/13 step 4).
agents/copilot.py is not imported, modified, or shared with: it is the
control arm, and the report's section 10 requires it to stay
byte-identical for the A/B design in section 11 to measure two independent
implementations rather than one implementation against itself.

Unlike agents/copilot.py, this module does NOT hand tools=[...] to
chat.send_message() and let the google-genai SDK run automatic function
calling. It disables AFC (automatic_function_calling.disable=True) and
drives the REASON <-> TOOLS loop itself, one client.models.generate_content()
call at a time, so every bound in the report's section 7 table is a real
counter in this file, never prompt text the model could ignore. That is
the entire honest scope of A0, stated plainly in the report's own
executive summary: "moving that loop out of Google's SDK and into code
you can bound, count and measure."

State machine (report section 7):

    BUILD_CONTEXT -> REASON -> TOOLS (model asks)
                             -> REASON (budget left)
                             -> VALIDATE (JSON returned)
                                  -> REASON (repair, once)
                                  -> PROPOSE (schema ok)
    REASON  -> FAIL (deadline or transport)
    VALIDATE -> FAIL (repair exhausted)
    TOOLS   -> FAIL (budget exhausted)

No re-planning, no self-critique beyond the single repair, no autonomous
continuation.

Bounds (section 7's table), enforced by counters in code below, never by
prompt text:

    MAX_TOOL_CALLS          6    total check_herb_alias calls, one preview
    MAX_REASONING_CALLS     2    one initial attempt, one repair
    TOTAL_DEADLINE_SECONDS  40   monotonic, checked before every model and
                                 tool call
    per-request timeout     45s  unchanged, via the existing HttpOptions

Every failure path maps to an error the system already has (section 7):
  - deadline exceeded, tool budget exhausted, or any transport/SDK
    failure -> ValueError("AI_SERVICE_UNAVAILABLE") (the same string
    agents/copilot.py raises; backend/main.py already turns this into its
    documented 502 + X-Error-Code response, unconditionally, whichever arm
    produced it).
  - unparseable after the one repair -> ValueError("INVALID_AI_SCHEMA")
    (same string, same existing 422).

A0 never emits a degraded proposal on a partial failure: it either returns
a fully-validated ClinicalParse or raises one of the two strings above.
There is no third outcome.
"""
import json
import logging
import os
import time

from pydantic import ValidationError

from backend.schemas import ClinicalParse

from agents.a0.context import AgentContext, build_clinical_context
from agents.a0.prompt import build_prompt, build_repair_prompt
from agents.a0.tools import check_herb_alias

logger = logging.getLogger(__name__)

# Same defaults as agents/copilot.py, for the same reasons documented in
# that file's own comments. Kept as independently-read env vars, not
# imported constants, so retuning one arm can never silently retune the
# other -- the two arms must be free to diverge in tuning even though
# they currently share numbers.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_TIMEOUT_SECONDS = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "45"))
GEMINI_TOTAL_BUDGET_SECONDS = float(os.environ.get("GEMINI_TOTAL_BUDGET_SECONDS", "40"))

# Section 7's bounds table.
MAX_TOOL_CALLS = 6
MAX_REASONING_CALLS = 2

_TOOL_FUNCTIONS = {"check_herb_alias": check_herb_alias}


class _ToolBudgetExhausted(Exception):
    """Internal signal only: the tool-call cap (MAX_TOOL_CALLS) was hit
    mid-loop. Always caught inside propose() and mapped to
    AI_SERVICE_UNAVAILABLE -- never surfaced past this module."""


def _deadline_exceeded(deadline: float) -> bool:
    """Monotonic, like agents/copilot.py's own deadline check -- immune to
    wall-clock adjustments (NTP steps, DST) during a long call."""
    return time.monotonic() >= deadline


def _run_tool(name: str, args: dict) -> dict:
    """Executes exactly one tool call by name, against this module's own
    fixed _TOOL_FUNCTIONS map -- never a name or a callable the model
    supplied. A tool failure is returned to the model as data ({"error":
    ...}), not raised, matching the same reasoning agents/copilot.py's
    check_stock docstring gives for the google-genai SDK's own behavior:
    a raised exception here would have nowhere sane to go, since this
    function is one step inside the REASON<->TOOLS loop, not the loop's
    caller."""
    func = _TOOL_FUNCTIONS.get(name)
    if func is None:
        return {"error": f"unknown tool: {name}"}
    try:
        return func(**args)
    except Exception as exc:  # noqa: BLE001 - fed back to the model as data, never raised into the caller
        return {"error": str(exc)}


def _reason_until_text(client, model, config, contents: list, deadline: float, tool_calls_made: list) -> str:
    """Drives one REASON<->TOOLS cycle to completion for a single reasoning
    attempt: repeatedly calls generate_content, and for any function calls
    the model asks for, executes them itself via _run_tool (counting each
    one against tool_calls_made, a list shared across repair attempts so
    MAX_TOOL_CALLS is a total for the whole preview, not per attempt) --
    until a response comes back with no further function calls, whose text
    is returned.

    Raises TimeoutError once the deadline has passed and
    _ToolBudgetExhausted once MAX_TOOL_CALLS is spent; propose() catches
    both and maps them to AI_SERVICE_UNAVAILABLE.
    """
    from google.genai import types  # local: this pure-orchestration file's top-level imports stay SDK-free otherwise

    while True:
        if _deadline_exceeded(deadline):
            raise TimeoutError(
                f"A0 exceeded its total budget of {GEMINI_TOTAL_BUDGET_SECONDS}s "
                "(GEMINI_TOTAL_BUDGET_SECONDS)."
            )

        response = client.models.generate_content(model=model, contents=contents, config=config)

        candidate = response.candidates[0] if response.candidates else None
        parts = candidate.content.parts if (candidate and candidate.content) else None
        function_calls = [p for p in (parts or []) if getattr(p, "function_call", None)]

        if not function_calls:
            return (response.text or "").strip()

        if len(tool_calls_made) >= MAX_TOOL_CALLS:
            raise _ToolBudgetExhausted(
                f"A0 exceeded its tool-call budget of {MAX_TOOL_CALLS} calls."
            )

        contents.append(candidate.content)
        response_parts = []
        for part in function_calls:
            if len(tool_calls_made) >= MAX_TOOL_CALLS:
                # Budget spent partway through a batch of parallel tool
                # calls: answer the rest with a real (negative) result
                # instead of leaving a dangling function call the model
                # never gets an answer to. The deadline/budget check at
                # the top of the next iteration is what actually ends the
                # loop; this just keeps the conversation well-formed.
                result = {"error": "tool_budget_exhausted"}
            else:
                tool_calls_made.append(part.function_call.name)
                result = _run_tool(part.function_call.name, dict(part.function_call.args or {}))
            response_parts.append(
                types.Part.from_function_response(name=part.function_call.name, response=result)
            )
        contents.append(types.Content(role="user", parts=response_parts))


def _parse_json_response(text: str) -> dict:
    """Identical fence-stripping to agents/copilot.py's own parsing --
    some models wrap JSON in a ```json ... ``` fence even when explicitly
    told not to."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    return json.loads(cleaned)


def _mock_propose(notes_text: str) -> ClinicalParse:
    """Reproduces agents/copilot.py's offline stub: same three hardcoded
    substring-matched branches, same outputs, same failure for anything
    else. The A0 architecture report (section 9) is explicit that this
    must be reproduced -- "or offline tests that currently pass will start
    making real network calls," the exact bug tests/test_golden.py's own
    docstring already documents this project having been burned by once.
    Kept as an independent copy rather than imported from copilot.py, for
    the same byte-identical-control-arm reason every other file in
    agents/a0/ duplicates rather than shares."""
    logger.warning("GEMINI_API_KEY not set. Using mock parser for tests (A0 arm).")
    if "Tulsi" in notes_text and "Ashwagandha" in notes_text:
        return ClinicalParse(dosha_imbalance="Vata", prescribed_herbs=[{"name": "Tulsi", "grams": 15}, {"name": "Ashwagandha", "grams": 10}])
    elif "9000g of Brahmi" in notes_text:
        return ClinicalParse(dosha_imbalance="Pitta", prescribed_herbs=[{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 9000}])
    elif "400g of Ashwagandha" in notes_text:
        return ClinicalParse(dosha_imbalance="Kapha", prescribed_herbs=[{"name": "Ashwagandha", "grams": 400}])
    else:
        raise ValueError("AI_SERVICE_UNAVAILABLE")


def propose(notes_text: str, ctx: AgentContext) -> ClinicalParse:
    """A0's entry point -- the treatment arm.

    Same PII air-gap contract as agents/copilot.parse_clinical_notes:
    `notes_text` MUST already be sanitized by
    backend.pii_sanitizer.tokenize_payload and cleared by
    assert_no_pii_leak before this is ever called. This function has no
    knowledge of, and must never receive, raw patient identity.

    `ctx` is an AgentContext built in backend/main.py from the
    authenticated session -- never inside agents/ (see
    agents/a0/context.py's docstring for why that boundary matters).
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _mock_propose(notes_text)

    from google import genai
    from google.genai import types

    try:
        client = genai.Client(api_key=api_key)
        clinical_context = build_clinical_context(ctx)
        prompt = build_prompt(notes_text, clinical_context)

        # from_callable_with_api_option (not from_callable) deliberately --
        # from_callable additionally requires a live client just to read
        # client.vertexai and pick between 'VERTEX_AI'/'GEMINI_API'; this
        # project only ever talks to the public Gemini API (see
        # agents/copilot.py, which uses the same genai.Client(api_key=...)
        # shape, never Vertex), so 'GEMINI_API' is hardcoded rather than
        # threading a client reference through just to ask it a question
        # whose answer is always the same here.
        tool = types.Tool(function_declarations=[
            types.FunctionDeclaration.from_callable_with_api_option(
                callable=check_herb_alias, api_option="GEMINI_API"
            ),
        ])
        config = types.GenerateContentConfig(
            tools=[tool],
            temperature=0.1,
            # Unchanged per-request ceiling -- same reasoning and same
            # value as agents/copilot.py's own GEMINI_TIMEOUT_SECONDS.
            http_options=types.HttpOptions(timeout=int(GEMINI_TIMEOUT_SECONDS * 1000)),
            # Automatic function calling is deliberately OFF -- see this
            # module's docstring. A0 executes every tool call itself, in
            # _run_tool, so MAX_TOOL_CALLS and the deadline are real code
            # this file enforces, not an SDK loop it could only watch from
            # the outside the way agents/copilot.py's total-budget fix
            # has to.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
    except Exception as exc:  # noqa: BLE001 - client/context/prompt setup failure maps the same way a transport failure does
        logger.error(f"A0 setup failure: {exc}", exc_info=True)
        raise ValueError("AI_SERVICE_UNAVAILABLE")

    deadline = time.monotonic() + GEMINI_TOTAL_BUDGET_SECONDS
    tool_calls_made: list = []
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]

    last_error: Exception | None = None
    for attempt in range(MAX_REASONING_CALLS):
        try:
            text = _reason_until_text(client, GEMINI_MODEL, config, contents, deadline, tool_calls_made)
        except (TimeoutError, _ToolBudgetExhausted) as exc:
            logger.error(f"A0 budget exhausted: {exc}")
            raise ValueError("AI_SERVICE_UNAVAILABLE")
        except Exception as exc:  # noqa: BLE001 - any transport/SDK failure maps the same way agents/copilot.py's does
            logger.error(f"A0 Gemini API failure: {exc}", exc_info=True)
            raise ValueError("AI_SERVICE_UNAVAILABLE")

        try:
            json_data = _parse_json_response(text)
            return ClinicalParse.model_validate(json_data)
        except (json.JSONDecodeError, ValidationError, AttributeError) as exc:
            last_error = exc
            logger.error(
                f"A0 schema validation failed (attempt {attempt + 1}/{MAX_REASONING_CALLS}). "
                f"Raw response: {text} | Error: {exc}"
            )
            if attempt + 1 < MAX_REASONING_CALLS:
                # The one allowed repair turn: fed back as a follow-up
                # message in the same conversation so the model still has
                # the original notes and any tool results in context.
                contents.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
                contents.append(types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=build_repair_prompt(text, str(exc)))],
                ))

    raise ValueError("INVALID_AI_SCHEMA")
