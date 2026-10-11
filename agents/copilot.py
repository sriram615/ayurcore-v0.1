import contextvars
import os
import json
import logging
import sys
import threading
import time
from typing import Optional

# Add project root to sys path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types
from pydantic import ValidationError

from backend.schemas import ClinicalParse
from backend.herb_resolver import normalize_herb, UnknownHerbError
from backend.inventory_service import get_stock
from db.database import DEFAULT_CLINIC_ID

logger = logging.getLogger(__name__)

# Temporary, explicit visibility into whether Gemini's automatic
# function-calling actually invokes our tools mid-conversation, versus
# just answering from the prompt text alone -- the one thing the existing
# test suite could NOT prove (it only proves our own code handles a tool
# call correctly IF one happens, using a fake client). Set
# AYURCORE_LOG_TOOL_CALLS=0 to silence this without another code change;
# defaults on while this is still an open question. Safe to leave on
# indefinitely if useful -- it never logs notes or patient identity, only
# herb names, which are not PII.
_LOG_TOOL_CALLS = os.environ.get("AYURCORE_LOG_TOOL_CALLS", "1") != "0"

# Which Gemini model to call. Configurable, not hardcoded, on purpose:
# Google has been deprecating Gemini model IDs at a real, fast pace lately
# (2.0 flash and 2.0 flash-lite, 3.1 flash-lite-preview, and 3-pro-preview
# are all already fully shut down as of this writing; a live API call
# during this project's own testing hit a 404 on gemini-2.5-flash with a
# message saying it's "no longer available to new users"). A hardcoded
# model name here means this exact outage recurs every few months. Set
# GEMINI_MODEL to override; gemini-3.6-flash is the current default,
# taken directly from Google's own error message recommending it as the
# replacement -- gemini-3.8-flash is newer and reportedly the better
# default for production tool-calling workloads, but hasn't been
# exercised against this project's real API key yet. Worth trying next
# and comparing accuracy/latency before committing to it as the default.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# A6.3 P1 fix (Sep 2026): bounded client-side timeout on the Gemini call.
#
# Before this, chat.send_message(prompt) below had NO timeout anywhere in
# this chain -- a hung network call could occupy the FastAPI worker thread
# handling /prescribe/preview indefinitely (confirmed in the A6.3 read-only
# review, Gate 11: every route in backend/main.py is a plain `def`, not
# `async def`, so FastAPI runs it via Starlette's shared, bounded default
# threadpool -- a hung Gemini call ties up one of those threads for as long
# as the hang lasts, which could eventually starve unrelated endpoints too).
#
# Deliberately LESS than ui/app.py's own 60s client-side timeout on its
# call to POST /prescribe/preview (the only UI call that reaches this code
# path -- see ui/app.py's call_preview, `timeout=60`) -- so this backend's
# own clean AI_SERVICE_UNAVAILABLE response has time to be generated and
# returned before the UI's OWN timeout gives up first and shows a generic
# "can't reach the backend" error instead of this endpoint's actual,
# documented failure mode. Configurable via env var for a clinic whose
# network conditions genuinely need a different value.
GEMINI_TIMEOUT_SECONDS = float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "45"))

# A0 Step 0, DEFECT B (restored Sep 2026): application-level TOTAL budget.
#
# GEMINI_TIMEOUT_SECONDS above bounds ONE HTTP request. The google-genai
# SDK's automatic function calling can make up to 10 round trips inside a
# single chat.send_message(), each getting that per-request budget afresh --
# worst case ~10 x 45s = 450s on one FastAPI threadpool thread. This caps
# the WHOLE call (every round trip plus every tool call) at 40s, measured
# with time.monotonic() so wall-clock adjustments (NTP steps, DST) can't
# stretch or shrink it. 40s deliberately sits UNDER the 45s per-request
# ceiling (left untouched) and under ui/app.py's 60s client timeout, so this
# endpoint's own AI_SERVICE_UNAVAILABLE answer always arrives first.
# Read at call time (module attribute), so it can be tuned via env var.
GEMINI_TOTAL_BUDGET_SECONDS = float(os.environ.get("GEMINI_TOTAL_BUDGET_SECONDS", "40"))

# Request-scoped context for the tools below (A0 Step 0, restored).
#
# _ACTIVE_CLINIC_ID -- DEFECT A: which clinic's inventory check_stock may
# read. Set by parse_clinical_notes() from the AUTHENTICATED session's
# clinic_id that backend/main.py passes in -- an authorization fact resolved
# server-side (backend/rbac.py's doctrine), never a tool argument: the SDK
# builds the tool declaration Gemini sees from check_stock's signature, so a
# clinic parameter there would let text in the clinical notes choose which
# tenant's stock to read.
#
# _DEADLINE_MONOTONIC -- DEFECT B: the time.monotonic() instant the total
# budget runs out. Once it passes, check_stock stops touching the database,
# so a worker abandoned by the bounded wait in parse_clinical_notes() can't
# keep querying after its caller has already been answered.
#
# Both are ContextVars, not module globals: every request (FastAPI runs each
# sync route on its own threadpool thread) and the worker thread that runs
# its Gemini call carry their own values, so two concurrent previews from
# different clinics can never see each other's clinic or deadline.
_ACTIVE_CLINIC_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "ayurcore_active_clinic_id", default=None
)
_DEADLINE_MONOTONIC: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "ayurcore_deadline_monotonic", default=None
)


def _remaining_seconds() -> Optional[float]:
    """Seconds left in the current call's total budget, or None when no
    budget is active (e.g. a tool called directly, outside
    parse_clinical_notes)."""
    deadline = _DEADLINE_MONOTONIC.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _deadline_exceeded() -> bool:
    remaining = _remaining_seconds()
    return remaining is not None and remaining <= 0


class _TotalBudgetExceeded(Exception):
    """Internal signal only: the bounded wait in parse_clinical_notes()
    ran out of total budget. Always mapped to AI_SERVICE_UNAVAILABLE."""


def _send_within_budget(chat, prompt):
    """Runs chat.send_message(prompt) -- the SDK's whole blocking
    function-calling loop -- on a worker thread and waits at most the
    remaining total budget for it. A bound on the blocking call itself, not
    an after-the-fact elapsed-time check that would report an overrun
    without preventing one.

    The worker runs inside a COPY of the caller's context, so the tools the
    SDK invokes on that thread see this request's clinic and deadline. If the
    wait gives up, the caller is released immediately; the abandoned worker
    may still be blocked in network I/O (Python can't kill a thread), but its
    copied deadline has passed, so check_stock returns without touching the
    database. Daemon thread: it never blocks process shutdown.
    """
    remaining = _remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise _TotalBudgetExceeded("total budget already spent before the Gemini call started")

    ctx = contextvars.copy_context()
    outcome: dict = {}
    done = threading.Event()

    def _worker():
        try:
            outcome["response"] = ctx.run(chat.send_message, prompt)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            outcome["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_worker, name="ayurcore-gemini-call", daemon=True).start()
    if not done.wait(timeout=remaining):
        raise _TotalBudgetExceeded(f"exceeded GEMINI_TOTAL_BUDGET_SECONDS={GEMINI_TOTAL_BUDGET_SECONDS}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["response"]

# The 20 seed herbs from our inventory (grew from 8, Sep 2026 -- keep this
# list in sync with backend/herb_resolver.py's canonical names AND with
# db/seed.py's actual inventory rows; ui/app.py's CLINIC_HERB_KEYTERMS
# should also stay in sync with this same list, for the same reason).
SEED_HERBS = [
    "Ashwagandha",
    "Brahmi",
    "Dashamoola",
    "Guggulu",
    "Shatavari",
    "Triphala",
    "Arjuna",
    "Haridra",
    "Neem",
    "Yashtimadhu",
    "Punarnava",
    "Bhringraj",
    "Manjistha",
    "Vasaka",
    "Shankhpushpi",
    "Kutki",
    "Pippali",
    "Haritaki",
    "Amalaki",
    "Bibhitaki",
]


# --- Agentic tools -----------------------------------------------------
#
# These are real Python functions, given to Gemini as tools it can call
# mid-answer (function/tool calling) rather than guessing. Both are
# strictly READ-ONLY -- they look things up and return information; they
# never write to the database or decide anything on the AI's behalf. The
# actual clinical/inventory decision still only happens when a human
# confirms via /prescribe/confirm (see backend/main.py). Docstrings here
# double as the tool descriptions Gemini sees, so keep them accurate.

def check_herb_alias(raw_name: str) -> dict:
    """Check whether a herb name matches one this clinic actually stocks.

    Args:
        raw_name: the herb name as it appeared in the clinical notes (any
            local name, spelling, or botanical name).

    Returns:
        A dict with `known` (bool) and, if known, `canonical_name` (the
        exact name to use in the final answer).
    """
    if _LOG_TOOL_CALLS:
        print(f"[GEMINI TOOL] check_herb_alias({raw_name!r})")
    try:
        canonical = normalize_herb(raw_name)
        return {"known": True, "canonical_name": canonical}
    except UnknownHerbError:
        return {"known": False, "canonical_name": None}


def check_stock(canonical_herb_name: str) -> dict:
    """Look up current stock, in grams, for a herb this clinic stocks.

    Always pass the CANONICAL name (as returned by check_herb_alias), not
    a raw or local name. This is informational only -- if stock looks low
    or insufficient, still report the prescription exactly as the doctor
    described it; the confirm step (a separate, later action a human
    takes) is what actually enforces stock limits, not this lookup.

    Args:
        canonical_herb_name: the clinic's exact canonical name for a herb.

    Returns:
        A dict with `herb_name` and current `stock_grams`. If the lookup is
        no longer possible (time ran out), `stock_grams` is null and
        `available` is false -- report the prescription as described anyway.
    """
    if _LOG_TOOL_CALLS:
        print(f"[GEMINI TOOL] check_stock({canonical_herb_name!r})")
    # DEFECT B: once the total budget is spent, no further database access --
    # returned to the model as data (not raised), since this runs inside the
    # SDK's function-calling loop where an exception has nowhere sane to go.
    if _deadline_exceeded():
        return {"herb_name": canonical_herb_name, "stock_grams": None, "available": False, "reason": "deadline_exceeded"}
    # DEFECT A: the caller's clinic, from request-scoped context. DEFAULT_CLINIC_ID
    # is only the fallback for callers outside an authenticated request (no
    # clinic set at all); /prescribe/preview always sets the doctor's own clinic.
    clinic_id = _ACTIVE_CLINIC_ID.get()
    if clinic_id is None:
        clinic_id = DEFAULT_CLINIC_ID
    stock_grams = get_stock(canonical_herb_name, clinic_id=clinic_id)
    return {"herb_name": canonical_herb_name, "stock_grams": stock_grams}


def parse_clinical_notes(notes_text: str, clinic_id: Optional[str] = None) -> ClinicalParse:
    """
    Parses clinical notes using Gemini to extract the dosha imbalance and
    prescribed botanical herbs with exact gram counts. Gemini is given two
    read-only tools (check_herb_alias, check_stock) it can call to verify
    its own understanding of herb names and stock before answering, rather
    than guessing and hoping it's right.

    IMPORTANT -- PII air-gap contract: `notes_text` MUST already be
    PII-sanitized by backend.pii_sanitizer.tokenize_payload (and verified
    with assert_no_pii_leak) before it is passed here. This function has no
    knowledge of, and must never receive, raw patient identity -- it treats
    any `[[PAT_xxxxxxxx]]` tokens it sees purely as opaque text. Callers
    are responsible for tokenizing before calling this and rehydrating the
    result afterward; this function does neither.

    `clinic_id` (A0 Step 0): the AUTHENTICATED caller's clinic, passed by
    backend/main.py from its server-derived session -- never from request
    input. It scopes check_stock for this call only (see _ACTIVE_CLINIC_ID).
    The whole real-Gemini path is bounded by GEMINI_TOTAL_BUDGET_SECONDS;
    exceeding it raises ValueError("AI_SERVICE_UNAVAILABLE"), the same
    contract as any other Gemini failure.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set. Using mock parser for tests.")
        if "Tulsi" in notes_text and "Ashwagandha" in notes_text:
            return ClinicalParse(dosha_imbalance="Vata", prescribed_herbs=[{"name": "Tulsi", "grams": 15}, {"name": "Ashwagandha", "grams": 10}])
        elif "9000g of Brahmi" in notes_text:
            return ClinicalParse(dosha_imbalance="Pitta", prescribed_herbs=[{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 9000}])
        elif "400g of Ashwagandha" in notes_text:
            return ClinicalParse(dosha_imbalance="Kapha", prescribed_herbs=[{"name": "Ashwagandha", "grams": 400}])
        else:
            raise ValueError("AI_SERVICE_UNAVAILABLE")

    deadline_token = _DEADLINE_MONOTONIC.set(time.monotonic() + GEMINI_TOTAL_BUDGET_SECONDS)
    clinic_token = _ACTIVE_CLINIC_ID.set(clinic_id) if clinic_id is not None else None
    try:
        return _parse_with_gemini(notes_text, api_key)
    finally:
        if clinic_token is not None:
            _ACTIVE_CLINIC_ID.reset(clinic_token)
        _DEADLINE_MONOTONIC.reset(deadline_token)


def _parse_with_gemini(notes_text: str, api_key: str) -> ClinicalParse:
    """The real-Gemini half of parse_clinical_notes(), unchanged apart from
    the bounded send. Runs with _ACTIVE_CLINIC_ID / _DEADLINE_MONOTONIC
    already set by its caller."""
    client = genai.Client(api_key=api_key)

    prompt = f"""
    You are an expert Ayurvedic clinical assistant AI.
    Extract the following structured information from the clinical consultation notes provided below:
    1. Dosha imbalance (e.g., Vata, Pitta, Kapha, or combinations like Vata-Pitta)
    2. Prescribed herbs and their quantities. All quantities MUST be converted to strictly integer grams.

    Language Support:
    The notes might be written in Malayalam, Manglish (Malayalam written in English script), or English.
    Translate and interpret the context accurately.

    Herb Matching constraint:
    We ONLY stock the following herbs in our inventory:
    {', '.join(SEED_HERBS)}

    Instructions for herbs:
    - Before including any herb in your final answer, call check_herb_alias with the name exactly as it appears
      in the notes, and use the canonical_name it returns instead of guessing a mapping yourself.
    - Call check_herb_alias for a given herb AT MOST TWICE: once with the name as it appears in the notes, and,
      only if that call returns known: false, at most one more time with a different plausible spelling or
      transliteration of the SAME herb. Do not keep retrying beyond that -- every call costs real API quota.
    - CRITICAL: never include a herb in your final answer under a name of your own guessing. Only use a
      canonical_name that some call to check_herb_alias actually returned with known: true. If every attempt
      for a herb returns known: false, that herb is not something this clinic stocks under any name you tried --
      leave it out of prescribed_herbs entirely rather than including your best guess. A doctor reviewing a
      shorter-than-expected list will notice and can add it back manually; a wrong herb slipping through because
      it sounded plausible is the failure mode this whole tool exists to prevent.
    - If a herb is mentioned in two languages/scripts in the same note (e.g. an English name and its Malayalam
      name together), that is still one herb: check whichever form you see first, and only try the other form if
      the first attempt returns known: false.
    - You may also call check_stock on a canonical name out of curiosity about current levels, but a low or
      insufficient stock result must NOT change your answer -- report the prescription exactly as the doctor
      described it. Enforcing stock limits happens later, as a separate step a human confirms; that is not your
      job here. Calling check_stock is optional -- skip it if you're already confident in the prescription.
    - Include all prescribed herbs.
    - Ensure quantities are strictly integers representing grams.

    Privacy notice:
    The patient's identity has already been anonymized before reaching you. Any text
    matching the pattern [[PAT_xxxxxxxx]] is an opaque placeholder standing in for a
    real name, phone number, or ID -- it is NOT clinical information. Ignore such
    placeholders for the purposes of dosha/herb extraction; do not attempt to guess,
    translate, or reconstruct what they stand for, and do not include them in your
    output.

    Output format:
    Once you are done using tools to verify herb names, respond with ONLY a single valid JSON object, and
    nothing else (no markdown fences, no commentary before or after it), matching exactly this shape:
    {{"dosha_imbalance": "<string>", "prescribed_herbs": [{{"name": "<canonical clinic herb name>", "grams": <integer>}}]}}

    Clinical Notes:
    \"\"\"
    {notes_text}
    \"\"\"
    """

    try:
        # `tools=[check_herb_alias, check_stock]` -- passing plain Python
        # functions (with type hints + docstrings) enables the google-genai
        # SDK's automatic function calling: it inspects the callables,
        # builds the tool declarations Gemini sees, executes them itself
        # whenever Gemini asks for a tool call, feeds the result back, and
        # only returns once Gemini has stopped calling tools and produced
        # a final answer. We never call check_herb_alias/check_stock
        # ourselves in this path -- the SDK does, using the real functions
        # against the real database.
        chat = client.chats.create(
            model=GEMINI_MODEL,
            config=types.GenerateContentConfig(
                tools=[check_herb_alias, check_stock],
                temperature=0.1,
                # A6.3 P1 fix: bounds every underlying HTTP request this
                # chat session's send_message() makes -- including each
                # round trip inside the automatic function-calling loop
                # above (check_herb_alias / check_stock) -- to
                # GEMINI_TIMEOUT_SECONDS. This is enforced by the
                # google-genai SDK's own HTTP transport (http_options is a
                # real request-level timeout the SDK passes down), not a
                # Python-side thread wrapper that would leave the
                # underlying network call running in the background after
                # this process stops waiting on it. See
                # GEMINI_TIMEOUT_SECONDS's own comment above for why 45s
                # and why it must stay under ui/app.py's 60s UI-side
                # timeout on this same call path. Unchanged by A0 Step 0:
                # it remains the per-request ceiling; the total budget
                # (_send_within_budget) sits on top of it.
                http_options=types.HttpOptions(timeout=int(GEMINI_TIMEOUT_SECONDS * 1000)),
            ),
        )
        response = _send_within_budget(chat, prompt)
    except _TotalBudgetExceeded as e:
        logger.error(f"Gemini total budget exceeded: {e}")
        raise ValueError("AI_SERVICE_UNAVAILABLE")
    except Exception as e:
        # Catches a Gemini-side timeout exactly the same way it already
        # catches every other Gemini failure mode (network error, quota,
        # outage) -- no change was needed here for the timeout case
        # specifically; it was already an unconditional `except Exception`.
        logger.error(f"Gemini API failure: {e}", exc_info=True)
        raise ValueError("AI_SERVICE_UNAVAILABLE")

    try:
        json_text = (response.text or "").strip()
        # Defensive: some models wrap JSON in a ```json ... ``` fence even
        # when explicitly told not to. Strip it if present.
        if json_text.startswith("```"):
            json_text = json_text.strip("`").strip()
            if json_text.lower().startswith("json"):
                json_text = json_text[4:].strip()
        json_data = json.loads(json_text)
        return ClinicalParse.model_validate(json_data)
    except (json.JSONDecodeError, ValidationError, AttributeError) as e:
        # Safe to log response.text here: it is the AI's reply, built only
        # from the tokenized notes -- it should never contain raw PII, only
        # tokens (and if it somehow did, that's an air-gap violation the
        # caller's assert_no_pii_leak boundary check was meant to prevent
        # on the way out, not something this log statement introduces on
        # the way back).
        logger.error(f"Schema validation failed. Raw response: {getattr(response, 'text', '')} | Error: {e}")
        raise ValueError("INVALID_AI_SCHEMA")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Demonstrates the full air-gap: tokenize -> parse (with tool calls) -> rehydrate.
    # Never call parse_clinical_notes with raw notes containing PII.
    from backend.pii_sanitizer import tokenize_payload, assert_no_pii_leak, rehydrate_payload

    raw_notes = "Patient Rahul varma vannirunnu. pullikku vata-pitta imbalance aanu. 500g ashwagandha and 200 grams brahmi kodukkanam. pinne kurachu thulasiyum (100g) venam."
    raw_patient_name = "Rahul Varma"

    sanitized_notes, mapping = tokenize_payload(raw_notes, [raw_patient_name])
    assert_no_pii_leak(sanitized_notes, [raw_patient_name])

    print("Sanitized notes sent to Gemini:")
    print(sanitized_notes)
    print()

    try:
        result = parse_clinical_notes(sanitized_notes)
        rehydrated = rehydrate_payload(result.model_dump(), mapping)
        print("Rehydrated result (patient name restored locally, never sent):")
        print(json.dumps({"patient_name": raw_patient_name, **rehydrated}, indent=2, default=str))
    except Exception as e:
        print(f"Error: {e}")
