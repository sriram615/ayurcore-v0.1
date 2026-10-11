"""
AyurCore — minimal Streamlit UI.

Deliberately minimal: enough to demo the real pipeline end to end (voice
or typed notes -> AI preview -> doctor review/correction -> confirm),
without deploy polish or styling. Talks to the FastAPI backend over plain
HTTP -- it never touches the database, Gemini, or Sarvam directly, only
backend/main.py's endpoints (per AyurCore_API_Contract_v1.md and its
addenda) and Sarvam's own API for transcription.

Authentication (Sep 2026): every protected backend call now requires a
real, server-issued session -- see AyurCore_API_Contract_v1.2_addendum.md
(A2) and v1.3 (A3, RBAC). This file used to send no Authorization header
at all (documented as a known, unfixed gap in v1.3 addendum section
"Known gaps", and again in the A2/A3 reports) -- every prescribe call
would 401. This UI now logs in via POST /auth/login, keeps the resulting
opaque session token ONLY in Streamlit's own server-side
st.session_state (never a URL/query string/cookie/log line), and attaches
`Authorization: Bearer <token>` to every protected call. The backend
remains the sole source of truth for identity, role, and clinic -- this
file never decodes the token, never invents a clinic_id, and never
second-guesses a 401/403 by retrying.

Run with:
    streamlit run ui/app.py
(with the backend already running separately: uvicorn backend.main:app)

Requires: pip install streamlit requests

Structure note for testability: every Streamlit widget-rendering
statement below lives inside main(), guarded by `if __name__ ==
"__main__": main()` at the bottom of this file -- `streamlit run
ui/app.py` executes this file as __main__ (same as invoking it directly
with `python ui/app.py` would), so this is transparent to normal use, but
it also means `import ui.app` (from a test) only defines the functions
and constants below and does NOT touch st.session_state, render any
widget, or make any network call -- st.set_page_config and the
session-state defaults loop are Streamlit-runtime-only calls that used to
run unconditionally at import time. All of the API-calling functions
(call_login, call_logout, call_preview, call_manual_preview, call_confirm)
were already plain functions with no st.* calls inside them; they still
are, so they're directly callable from a test with an explicit API_BASE
and token, with no Streamlit runtime involved at all.
"""
import hashlib
import html
import json
import os
import uuid
from datetime import datetime

import requests
import streamlit as st

API_BASE = os.environ.get("AYURCORE_API_BASE", "http://127.0.0.1:8000")
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY")
SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"

# The one role this UI's prescribing workflow supports, per
# backend/rbac.py's PERMISSION_MATRIX (prescribe: DOCTOR-only). Deliberately
# a local string constant, not an import of backend.rbac.ROLE_DOCTOR -- this
# file is a plain HTTP client and never imports backend/db code directly
# (see the module docstring); the backend's own 403 on prescribe/* is what
# actually enforces this; the check below is purely a friendlier, faster UX
# short-circuit for a role the backend would reject anyway (defense in
# depth, not a second authority).
REQUIRED_ROLE = "DOCTOR"

# The clinic's actual stocked herbs (kept in sync with backend/herb_resolver.py's
# HERB_ALIASES canonical names) -- passed to Sarvam as `keyterms` so its
# transcription is biased toward recognizing these exact words correctly,
# rather than mishearing a herb name as something similar-sounding.
CLINIC_HERB_KEYTERMS = [
    "Ashwagandha", "Brahmi", "Dashamoola", "Guggulu",
    "Shatavari", "Triphala", "Arjuna", "Haridra",
    # Grew from 8 to 20 herbs (Sep 2026) -- keep in sync with
    # backend/herb_resolver.py's canonical names and agents/copilot.py's
    # SEED_HERBS. Still well under Sarvam's 50-keyterm cap.
    "Neem", "Yashtimadhu", "Punarnava", "Bhringraj",
    "Manjistha", "Vasaka", "Shankhpushpi", "Kutki",
    "Pippali", "Haritaki", "Amalaki", "Bibhitaki",
]

# UX-3: the review screen's "Add medicine" control is a controlled
# dropdown, not free text (spec §6: "Do NOT create arbitrary free-text
# medicine entry... do not invent a second medicine naming system").
# Deliberately reuses CLINIC_HERB_KEYTERMS above -- the SAME canonical
# name list this file already treats as its one source of truth for herb
# names (used for Sarvam's keyterms) -- rather than a second list.
#
# KNOWN LIMITATION, reported rather than silently worked around (per this
# authorization's own "stop and report a backend dependency rather than
# inventing an endpoint" instruction): this list is a static mirror of
# backend/herb_resolver.py's canonical names, not a live, per-clinic
# stock-aware read. There is currently no HTTP-reachable endpoint for the
# UI to ask "what is this clinic's available inventory right now" --
# backend/inventory_read.py's list_stock() exists only as an in-process
# helper for agents/a0, and this file talks to the backend exclusively
# over HTTP (see the module docstring), so it cannot import that
# function directly without breaking that architectural boundary. A
# medicine chosen here that the clinic happens to be out of stock on
# still surfaces the existing, correct "not enough stock" error (UX-1's
# MSG_INSUFFICIENT_STOCK) from the backend at confirm time -- nothing is
# bypassed -- but the dropdown itself can't yet reflect real-time
# availability. The real fix is a new, read-only, authenticated endpoint
# (e.g. GET /inventory) wrapping list_stock(auth.clinic_id); adding one
# is a backend change outside this UI-only authorization's scope.
REVIEW_DOSHA_OPTIONS = [
    "Vata", "Pitta", "Kapha",
    "Vata-Pitta", "Pitta-Kapha", "Vata-Kapha", "Tridosha",
]
_DOSHA_OTHER = "Other (type manually)"

# ---- session state defaults (applied inside main(), see the loop below) --
_DEFAULTS = {
    "auth": None,           # dict: {"token","username","clinic_id","role","expires_at"} from /auth/login,
                             # or None when not logged in. The token lives ONLY here (Streamlit's own
                             # server-side session_state) -- never in a URL, query string, cookie, or log.
    "notes_text": "",
    "transcript_draft": "",
    "preview": None,       # dict: the last /prescribe/preview (or /prescribe/manual-preview) response, or None
    "last_result": None,   # dict: the last successful /prescribe/confirm response
    "last_error": None,    # str: a message to show above the preview/confirm area
    "ai_unavailable": False,  # True only when the last /preview failure was specifically an AI-service
                               # outage (quota/down/timeout) per the X-Error-Code response header -- gates
                               # the "continue without AI" offer so it never appears for an error manual
                               # entry wouldn't fix anyway (e.g. an unresolvable herb name -> 400).
    "manual_mode": False,      # True once the doctor has chosen to fall through to manual entry.
    "confirm_signature": None,      # str: fingerprint of the (preview_id, dosha, herbs) last sent to
                                     # /prescribe/confirm -- see get_idempotency_key_for()'s docstring.
    "confirm_idempotency_key": None,  # str: the Idempotency-Key reused as long as confirm_signature matches.
    # UX-2: consultation stage tracking
    # Stages: "input" -> "manual_entry" | "review" -> "confirmed"
    # "input":        patient + notes + action choice
    # "manual_entry": doctor is filling dosha/herbs manually ("Write it myself")
    # "review":       reviewing a preview (AI or manual)
    # "confirmed":    prescription confirmed, show result
    "consultation_stage": "input",
    "review_source": None,   # "ai" or "manual" -- determines review label (spec §9)
    # UX-3: whether the review screen's prescription editor is in its
    # explicit editing state (steppers/remove/add/dosha-select visible,
    # Save/Cancel offered) or its calm read-only summary state (spec §9).
    "review_editing": False,
}


# ---- UX-1: clinician-facing error translation ---------------------------
#
# Every message in this section is what a DOCTOR actually reads. The rule
# behind all of them: say what happened to the patient's prescription, then
# what to do next. Never the transport, never the component, never a vendor.
#
# IMPORTANT STRUCTURAL NOTE. The call_* functions below are deliberately
# left byte-for-byte unchanged by UX-1: they still return the raw
# "[<status>] <detail>" strings they always have. That is not an oversight
# -- tests/test_ui_auth_integration.py asserts on those exact raw strings
# ("401" in error for call_login, "[404]" in error for a cross-clinic
# call_confirm), and those assertions are a real part of the locked
# networking contract. Translation therefore happens at RENDER time, on the
# way to st.error(), which is also what the UX-1 brief prefers ("keep the
# underlying networking functions unchanged wherever possible; the UI can
# use the translated category/message at render time"). The raw string
# never reaches the screen; it stays inside this module.

ERROR_SESSION = "SESSION"
ERROR_SIGN_IN_FAILED = "SIGN_IN_FAILED"
ERROR_WRONG_ROLE = "WRONG_ROLE"
ERROR_UNKNOWN_HERB = "UNKNOWN_HERB"
ERROR_VALIDATION = "VALIDATION"
ERROR_AI_UNAVAILABLE = "AI_UNAVAILABLE"
ERROR_AI_INVALID_RESPONSE = "AI_INVALID_RESPONSE"
ERROR_PREVIEW_EXPIRED = "PREVIEW_EXPIRED"
ERROR_INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
ERROR_ALREADY_CONFIRMED = "ALREADY_CONFIRMED"
ERROR_IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
ERROR_CONNECTION = "CONNECTION"
ERROR_TIMEOUT_RETRYABLE = "TIMEOUT_RETRYABLE"
ERROR_TIMEOUT_UNCERTAIN = "TIMEOUT_UNCERTAIN"
ERROR_SERVER = "SERVER"
ERROR_UNKNOWN = "UNKNOWN"

MSG_SESSION = "Your session has ended. Sign in again to carry on — your notes are kept."
MSG_SIGN_IN_FAILED = "That username and password don't match."
MSG_WRONG_ROLE = "This account can't write prescriptions. Sign in with a doctor's account."
MSG_UNKNOWN_HERB = "Not a herb this clinic stocks. Pick one from the list."
MSG_VALIDATION = "Some of the prescription details couldn't be accepted. Check the dosha and the herb amounts, then try again."
MSG_AI_UNAVAILABLE = (
    "The assistant isn't responding right now. You can write this prescription "
    "yourself — the same herb and stock checks still apply."
)
MSG_AI_INVALID_RESPONSE = "The assistant's reply couldn't be read. Try again, or write it yourself."
MSG_PREVIEW_EXPIRED = "This draft is over 30 minutes old and has lapsed. Draft it again from your notes?"
MSG_INSUFFICIENT_STOCK = "Not enough stock to dispense this amount."
MSG_ALREADY_CONFIRMED = "This prescription has already been issued."
MSG_IDEMPOTENCY_CONFLICT = "Something changed while confirming. Check the herbs below and confirm again."
MSG_CONNECTION = "AyurCore can't be reached. Check the clinic's connection, or ask whoever set this up."
MSG_SERVER = "Something went wrong on our side and nothing was saved. Try once more."
MSG_UNKNOWN = "Something went wrong and nothing was saved. Try once more."
MSG_PREVIEW_TIMEOUT = (
    "The assistant took too long to respond. Nothing was saved — you can try again, "
    "or write this prescription yourself."
)
MSG_REQUEST_TIMEOUT = "That took too long and nothing was saved. Please try again."
MSG_TRANSCRIPTION_FAILED = (
    "That recording couldn't be turned into text. Type the notes instead, or record again."
)

# Conservative, lowercase substring markers used ONLY to disambiguate
# responses the backend cannot yet distinguish structurally.
#
# The backend sets X-Error-Code on exactly ONE branch today
# (AI_SERVICE_UNAVAILABLE, backend/main.py). Everything else -- and in
# particular the THREE unrelated failures that all return 409 (preview
# already confirmed, insufficient stock, idempotency conflict) -- is
# separable only by its `detail` prose. backend/main.py's own comment on
# why X-Error-Code exists warns against matching that prose, and it is
# right to.
#
# So this matching is deliberately contained: it lives in exactly one pure
# function (_friendly_error, below), it is never a security or
# authorization decision (every such decision is the backend's and is
# already made by the time a status code reaches this file), and each
# marker falls back to a safe generic message rather than guessing when it
# does not match. Nothing matched here is ever shown to the clinician.
#
# BACKEND DEPENDENCY B1 (see the Master UX/UI Specification) replaces all
# of this with structured X-Error-Code values on the 400/404/409 branches.
# When B1 lands, delete these markers and read error_code instead; the
# public behaviour of _friendly_error should not change.
_MARK_UNKNOWN_HERB = "is not in inventory"
_MARK_PREVIEW_MISSING = "not found or expired"
_MARK_ALREADY_CONFIRMED = "has already been confirmed"
_MARK_INSUFFICIENT_STOCK = "insufficient stock for herb"
_MARK_IDEMPOTENCY = "idempotency key"
_MARK_AI_SCHEMA = "invalid ai schema"

# The exact network-failure strings the call_* functions return. Extracted
# to constants ONLY so that _translate_call_error() above can recognise
# them by equality rather than by guessing at their wording -- the values
# are byte-for-byte what those functions produced before UX-1, so every
# call_* return value is unchanged, including the two that
# tests/test_ui_auth_integration.py asserts on.
#
# These strings embed API_BASE and, in one case, a shell command. They are
# internal diagnostics: none of them is ever displayed, because
# _translate_call_error() maps every one of them to a MSG_* constant first.
_LOGIN_TIMEOUT_MESSAGE = "The login request to {api_base} timed out. The backend may be slow or unreachable -- please try again."
_CONNECTION_ERROR_MESSAGE = "Can't reach the backend at {api_base}. Is `uvicorn backend.main:app` running?"
_UNEXPECTED_ERROR_MESSAGE = "Unexpected error talking to the backend at {api_base}: {error}"
_LOGOUT_TIMEOUT_MESSAGE = "The logout request to {api_base} timed out; could not confirm the session was revoked server-side."
_LOGOUT_CONNECTION_ERROR_MESSAGE = "Can't reach the backend at {api_base} to revoke the session."
_PREVIEW_TIMEOUT_MESSAGE = (
    "The AI request to {api_base} took too long and timed out. Nothing was saved -- "
    "you can try again, or continue without AI below."
)
_REQUEST_TIMEOUT_MESSAGE = "The request to {api_base} timed out. Nothing was saved -- please try again."
_CONFIRM_TIMEOUT_MESSAGE = (
    "The confirmation request timed out -- we could NOT confirm whether the backend received "
    "and completed it. Do not assume it failed. If you retry with the same preview and the same "
    "dosha/herbs shown below, this is safe: the same request identity is reused, so if the first "
    "attempt already went through, retrying will return that original result rather than create "
    "a second prescription or deduct inventory twice."
)


def _friendly_error(status_code, detail, error_code=None):
    """Pure translator: (status_code, detail, error_code) -> (category, message).

    Makes no network call, reads no Streamlit state, and returns no value
    derived from `detail` -- every message it can return is one of the
    MSG_* constants above, so a backend detail string can never reach a
    clinician through this function, however it is worded.

    `status_code` is None for a failure that never produced an HTTP
    response at all (connection refused, DNS failure); callers that need
    to distinguish a timeout from a connection error use
    _translate_call_error() below, which knows which operation was in
    flight.

    Returns a (category, message) tuple. The category is for the UI and
    for tests; only the message is ever displayed.
    """
    text = (detail or "").lower()

    # The one structured signal the backend actually gives us today. It is
    # checked first and independently of the status code, exactly as
    # call_preview already checks it, so this stays correct if the status
    # attached to that branch ever changes.
    if error_code == "AI_SERVICE_UNAVAILABLE":
        return ERROR_AI_UNAVAILABLE, MSG_AI_UNAVAILABLE

    if status_code is None:
        return ERROR_CONNECTION, MSG_CONNECTION

    if status_code == 401:
        return ERROR_SESSION, MSG_SESSION

    if status_code == 403:
        return ERROR_WRONG_ROLE, MSG_WRONG_ROLE

    if status_code == 400:
        if _MARK_UNKNOWN_HERB in text:
            return ERROR_UNKNOWN_HERB, MSG_UNKNOWN_HERB
        return ERROR_VALIDATION, MSG_VALIDATION

    if status_code == 404:
        # /prescribe/confirm is the only endpoint in this API that returns
        # 404 for a resource reason, and it does so for a preview that is
        # missing, expired, or belongs to another clinic -- all three are
        # "this draft is gone, draft it again" from the doctor's side.
        if _MARK_PREVIEW_MISSING in text:
            return ERROR_PREVIEW_EXPIRED, MSG_PREVIEW_EXPIRED
        return ERROR_UNKNOWN, MSG_UNKNOWN

    if status_code == 409:
        if _MARK_INSUFFICIENT_STOCK in text:
            return ERROR_INSUFFICIENT_STOCK, MSG_INSUFFICIENT_STOCK
        if _MARK_ALREADY_CONFIRMED in text:
            return ERROR_ALREADY_CONFIRMED, MSG_ALREADY_CONFIRMED
        if _MARK_IDEMPOTENCY in text:
            return ERROR_IDEMPOTENCY_CONFLICT, MSG_IDEMPOTENCY_CONFLICT
        # Unrecognised 409: say the safest true thing rather than guess
        # which of the three it was.
        return ERROR_IDEMPOTENCY_CONFLICT, MSG_IDEMPOTENCY_CONFLICT

    if status_code == 422:
        # 422 arrives from two unrelated places: the AI returning something
        # that doesn't validate (backend/main.py's INVALID_AI_SCHEMA), and
        # FastAPI's own request validation (an oversized Idempotency-Key,
        # a herb name over 200 characters). Only the first is the
        # assistant's fault, so only the first gets the assistant's
        # message -- telling a doctor "the assistant's reply couldn't be
        # read" when they actually typed too long a herb name would send
        # them looking in the wrong place.
        if _MARK_AI_SCHEMA in text:
            return ERROR_AI_INVALID_RESPONSE, MSG_AI_INVALID_RESPONSE
        return ERROR_VALIDATION, MSG_VALIDATION

    if status_code >= 500:
        return ERROR_SERVER, MSG_SERVER

    return ERROR_UNKNOWN, MSG_UNKNOWN


def _split_call_error(raw_error):
    """Pure: splits a call_* error string into (status_code, detail).

    The call_* functions below format every HTTP failure as
    "[<status>] <detail>". That format is this module's own convention,
    produced a few dozen lines below and consumed only here -- parsing it
    is not a new trust boundary, and no authorization decision is made
    from it.

    Returns (None, raw_error) for anything that isn't in that shape, which
    is how the network-level failures (connection refused, timeout) fall
    through to _translate_call_error()'s operation-aware handling.
    """
    if not raw_error:
        return None, ""
    text = str(raw_error)
    if not text.startswith("["):
        return None, text
    closing = text.find("]")
    if closing == -1:
        return None, text
    try:
        status_code = int(text[1:closing])
    except ValueError:
        return None, text
    return status_code, text[closing + 1:].strip()


def _translate_call_error(raw_error, error_code=None, operation=None, ai_unavailable=False):
    """Pure: turns whatever a call_* function returned into a
    (category, message) pair fit for st.error().

    `operation` is one of "login", "logout", "preview", "manual_preview",
    "confirm", or None. It exists for exactly one reason: a timeout means
    something different for /prescribe/confirm than for every other call
    in this file, and only the caller knows which call it made.

    /prescribe/confirm is the only call with a real side effect. A timeout
    there is an UNCERTAIN OUTCOME, not a failure -- the backend may well
    have committed the prescription and deducted inventory after this
    client stopped waiting. That distinction is the whole reason
    call_confirm's timeout copy is worded the way it is, and UX-1
    preserves it verbatim rather than collapsing it into an ordinary
    error.
    """
    status_code, detail = _split_call_error(raw_error)

    if status_code is not None:
        # A 401 from /auth/login is the one 401 in this file that does NOT
        # mean "your session ended" -- no session existed yet. The backend
        # returns a single generic 401 for a wrong username, a wrong
        # password and an inactive account alike (backend/main.py's
        # login() docstring explains why those three are deliberately
        # indistinguishable), and this message keeps that property.
        if operation == "login" and status_code == 401:
            return ERROR_SIGN_IN_FAILED, MSG_SIGN_IN_FAILED
        return _friendly_error(status_code, detail, error_code)

    # No HTTP response: connection refused, DNS failure, or a timeout.
    if operation == "confirm" and detail == _CONFIRM_TIMEOUT_MESSAGE:
        # Preserved verbatim, deliberately. See this function's docstring.
        return ERROR_TIMEOUT_UNCERTAIN, _CONFIRM_TIMEOUT_MESSAGE

    if detail == _PREVIEW_TIMEOUT_MESSAGE.format(api_base=API_BASE):
        # A preview timeout looks exactly like the AI being unavailable
        # from the doctor's side, and call_preview already flags it as
        # such (ai_unavailable=True) so the same "continue without AI"
        # offer appears. Only the wording changes here; that behaviour is
        # untouched.
        return ERROR_TIMEOUT_RETRYABLE, MSG_PREVIEW_TIMEOUT

    if detail == _REQUEST_TIMEOUT_MESSAGE.format(api_base=API_BASE):
        return ERROR_TIMEOUT_RETRYABLE, MSG_REQUEST_TIMEOUT

    if detail in (
        _LOGIN_TIMEOUT_MESSAGE.format(api_base=API_BASE),
        _LOGOUT_TIMEOUT_MESSAGE.format(api_base=API_BASE),
    ):
        return ERROR_TIMEOUT_RETRYABLE, MSG_REQUEST_TIMEOUT

    if detail in (
        _CONNECTION_ERROR_MESSAGE.format(api_base=API_BASE),
        _LOGOUT_CONNECTION_ERROR_MESSAGE.format(api_base=API_BASE),
    ):
        return ERROR_CONNECTION, MSG_CONNECTION

    if ai_unavailable:
        return ERROR_AI_UNAVAILABLE, MSG_AI_UNAVAILABLE

    # Anything else that never reached the backend -- including the
    # catch-all RequestException branches, whose text embeds a raw
    # exception and the API base URL and so must never be shown.
    return ERROR_CONNECTION, MSG_CONNECTION


def _friendly_message(raw_error, error_code=None, operation=None, ai_unavailable=False) -> str:
    """Convenience wrapper: the message alone, for a call site that has
    nothing to do with the category."""
    return _translate_call_error(raw_error, error_code, operation, ai_unavailable)[1]


class SessionExpiredError(Exception):
    """Raised by every protected call_* function below when the backend
    responds 401 Unauthorized -- the session is missing, expired, revoked,
    or was never valid. Callers (main(), below) MUST treat this as "the
    doctor needs to log in again": clear the local session and show the
    login screen. Never caught-and-retried, never used to silently mint a
    new session, and never treated like a network timeout that might
    succeed on a second try -- a 401 here means the credential itself is
    no longer good, not that the request failed transiently."""


def _protected_headers(token: str, idempotency_key: str = None) -> dict:
    """Builds the header dict for a call that requires
    `Authorization: Bearer <token>`. `token` is treated as a fully opaque
    bearer credential -- never parsed, decoded, or inspected here, exactly
    as AyurCore_API_Contract_v1.2_addendum.md (A2) requires. Adds
    `Idempotency-Key` too when the caller supplies one (A4, see
    AyurCore_API_Contract_v1.4_addendum.md)."""
    headers = {"Authorization": f"Bearer {token}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _raise_if_session_expired(resp):
    """Shared first check for every protected call below. A 401 here means
    require_auth (backend/rbac.py) rejected the Authorization header
    outright -- missing, malformed, forged, expired, or revoked; backend/
    rbac.py's require_auth() deliberately makes all of those identical.
    Must run BEFORE the generic error-detail extraction in each call_*
    function, so a 401 never gets displayed as an ordinary inline error
    message -- it has to interrupt the flow and force a fresh login."""
    if resp.status_code == 401:
        raise SessionExpiredError("Session expired, revoked, or invalid. Please log in again.")


def transcribe_audio(audio_bytes: bytes) -> str:
    """
    Sends recorded audio (WAV, from st.audio_input) to Sarvam AI's
    speech-to-text API and returns the transcript.

    Uses `model=saaras:v3` with `mode=codemix`, since v3's codemix mode is
    specifically built for exactly the code-mixed Malayalam/English speech
    this clinic's doctors actually use -- `saaras:v4`'s newer `transcribe`
    mode is generally more accurate but wasn't documented as supporting
    codemix specifically. Try both against real recordings and see which
    performs better in practice; this isn't something to guess at from
    documentation alone.

    Verified against Sarvam's docs (docs.sarvam.ai) as of Sep 2026:
    POST https://api.sarvam.ai/speech-to-text, header
    `api-subscription-key: <key>`, multipart form fields `file`/`model`/
    `mode`/`language_code`/`keyterms`. NOT exercised against a live key in
    this environment -- the very first real recording should be checked
    carefully against what the doctor actually said.

    Note: this is a call to a THIRD-PARTY service (Sarvam), not our own
    backend -- it deliberately never receives our own Authorization
    bearer token (that would leak our session credential to an unrelated
    external API). It uses its own separate SARVAM_API_KEY credential.
    """
    if not SARVAM_API_KEY:
        raise RuntimeError(
            "SARVAM_API_KEY is not set, so voice transcription is unavailable right now. "
            "Type the notes instead, or set SARVAM_API_KEY and restart."
        )

    response = requests.post(
        SARVAM_STT_URL,
        headers={"api-subscription-key": SARVAM_API_KEY},
        files={"file": ("recording.wav", audio_bytes, "audio/wav")},
        data={
            "model": "saaras:v3",
            "mode": "codemix",
            "language_code": "unknown",
            "keyterms": json.dumps(CLINIC_HERB_KEYTERMS),
        },
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    return result.get("transcript", "")


def call_login(username: str, password: str):
    """POST /auth/login (public -- no Authorization header sent or
    expected; see backend/rbac.py's PERMISSION_MATRIX docstring: "public,
    public, public -- no identity exists yet"). Sends only the exact
    existing request shape (backend/schemas.py's LoginRequest:
    username/password, nothing else -- no clinic_id, the doctor never
    asserts their own clinic). On success, returns the parsed
    LoginResponse dict (token/expires_at/username/clinic_id/role) --
    everything the UI needs to display or act on, per that model's own
    docstring, without ever decoding the opaque token itself. On a wrong
    username/password/inactive-account, the backend returns a single
    generic 401 "Invalid username or password." (deliberately
    indistinguishable across all three reasons -- see backend/main.py's
    login() docstring) which is returned here unmodified, not
    reinterpreted or narrowed."""
    try:
        resp = requests.post(
            f"{API_BASE}/auth/login",
            json={"username": username, "password": password},
            timeout=30,
        )
    except requests.exceptions.Timeout:
        # Login has no side effect to worry about (nothing is created or
        # changed by a login attempt) -- a timeout here is just an
        # ordinary, safely-retryable failure, same as a connection error.
        return None, _LOGIN_TIMEOUT_MESSAGE.format(api_base=API_BASE)
    except requests.exceptions.ConnectionError:
        return None, _CONNECTION_ERROR_MESSAGE.format(api_base=API_BASE)
    except requests.exceptions.RequestException as e:
        # Catch-all for any other expected HTTP/network failure (bad URL,
        # SSL error, malformed response, etc.) -- never let one of these
        # surface to the doctor as a raw Python traceback.
        return None, _UNEXPECTED_ERROR_MESSAGE.format(api_base=API_BASE, error=e)
    if resp.status_code == 200:
        return resp.json(), None
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    return None, f"[{resp.status_code}] {detail}"


def call_logout(token: str):
    """POST /auth/logout with the current session's Authorization header
    -- this is what actually revokes the session server-side (see
    backend/main.py's logout(), which calls revoke_session()). Returns
    (ok, error): ok is True only on a real 200 from the backend. Calling
    this and THEN clearing local session state (never the other way
    around, and never skipping this call) is what makes logout a real
    security action instead of the UI merely forgetting its own copy of
    a token that would otherwise keep working against the backend."""
    try:
        resp = requests.post(
            f"{API_BASE}/auth/logout",
            headers=_protected_headers(token),
            timeout=30,
        )
    except requests.exceptions.Timeout:
        # Same posture as the 401 branch just below: whether or not the
        # backend actually revoked the session before this client gave up
        # waiting, there's nothing more the UI can do here except drop its
        # own copy of the token -- the caller (_clear_session) does that
        # regardless of this function's return value. Reported as an error
        # (not silently swallowed) so the doctor knows revocation wasn't
        # confirmed, without this crashing the app.
        return False, _LOGOUT_TIMEOUT_MESSAGE.format(api_base=API_BASE)
    except requests.exceptions.ConnectionError:
        return False, _LOGOUT_CONNECTION_ERROR_MESSAGE.format(api_base=API_BASE)
    except requests.exceptions.RequestException as e:
        return False, _UNEXPECTED_ERROR_MESSAGE.format(api_base=API_BASE, error=e)
    if resp.status_code == 200:
        return True, None
    # A 401 here just means the session was already invalid (expired/
    # revoked already) -- nothing left to revoke, so from the doctor's
    # point of view this is still an effective logout, not a failure.
    if resp.status_code == 401:
        return True, None
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    return False, f"[{resp.status_code}] {detail}"


def call_preview(patient_name, patient_phone, patient_id, notes, token: str):
    payload = {
        "patient_name": patient_name,
        "patient_phone": patient_phone or None,
        "patient_id": patient_id or None,
        "notes": notes,
    }
    try:
        resp = requests.post(
            f"{API_BASE}/prescribe/preview",
            json=payload,
            headers=_protected_headers(token),
            timeout=60,
        )
    except requests.exceptions.Timeout:
        # This call makes no database write (see the module/endpoint
        # docstrings -- /prescribe/preview only tokenizes, calls Gemini,
        # and resolves herb names; nothing is persisted until /confirm).
        # A timeout here is therefore safely retryable, not an uncertain
        # outcome -- and from the doctor's point of view it looks exactly
        # like the AI being unavailable, so it is treated the same way:
        # ai_unavailable=True reuses the SAME "continue without AI" offer
        # the backend's own AI_SERVICE_UNAVAILABLE response triggers,
        # rather than introducing a second, separate fallback path.
        return None, _PREVIEW_TIMEOUT_MESSAGE.format(api_base=API_BASE), True
    except requests.exceptions.ConnectionError:
        return None, _CONNECTION_ERROR_MESSAGE.format(api_base=API_BASE), False
    except requests.exceptions.RequestException as e:
        return None, _UNEXPECTED_ERROR_MESSAGE.format(api_base=API_BASE, error=e), False
    _raise_if_session_expired(resp)
    if resp.status_code == 200:
        return resp.json(), None, False
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    # backend/main.py sets this header ONLY on its AI_SERVICE_UNAVAILABLE
    # branch (Gemini quota exhausted, outage, or timeout) -- never for a
    # validation error like an unresolvable herb name, where manual entry
    # wouldn't route around anything since the same herb still needs
    # resolving. Checked via the header, not by matching the `detail`
    # text, so this stays correct even if that message's wording changes.
    ai_unavailable = resp.headers.get("X-Error-Code") == "AI_SERVICE_UNAVAILABLE"
    return None, f"[{resp.status_code}] {detail}", ai_unavailable


def call_manual_preview(patient_name, patient_phone, patient_id, dosha_imbalance, prescribed_herbs, token: str):
    """AI-outage fallback: same shape of call as call_preview, but hits
    /prescribe/manual-preview (no notes text, no AI call) with a
    doctor-entered dosha + herb list instead. Still goes through the same
    herb-name resolution as the AI path -- an unresolvable herb name here
    gets the same 400 it would from /prescribe/preview."""
    payload = {
        "patient_name": patient_name,
        "patient_phone": patient_phone or None,
        "patient_id": patient_id or None,
        "dosha_imbalance": dosha_imbalance,
        "prescribed_herbs": prescribed_herbs,
    }
    try:
        resp = requests.post(
            f"{API_BASE}/prescribe/manual-preview",
            json=payload,
            headers=_protected_headers(token),
            timeout=30,
        )
    except requests.exceptions.Timeout:
        # Same as call_preview: no database write happens on this path
        # either, so a timeout here is an ordinary, safely-retryable
        # failure, not an uncertain one.
        return None, _REQUEST_TIMEOUT_MESSAGE.format(api_base=API_BASE)
    except requests.exceptions.ConnectionError:
        return None, _CONNECTION_ERROR_MESSAGE.format(api_base=API_BASE)
    except requests.exceptions.RequestException as e:
        return None, _UNEXPECTED_ERROR_MESSAGE.format(api_base=API_BASE, error=e)
    _raise_if_session_expired(resp)
    if resp.status_code == 200:
        return resp.json(), None
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    return None, f"[{resp.status_code}] {detail}"


def get_idempotency_key_for(preview_id, dosha_imbalance, prescribed_herbs) -> str:
    """Returns a stable Idempotency-Key for the confirmation currently
    being attempted, generating a fresh one (via uuid4, the same
    approach AyurCore_API_Contract_v1.4_addendum.md's own D1 example --
    `crypto.randomUUID()` -- describes for this project's UI) only when
    the (preview_id, dosha_imbalance, prescribed_herbs) about to be sent
    actually differ from whatever was last sent under the currently
    stored key.

    This is what makes retrying SAFE without either half of the two ways
    this could go wrong:
      - A doctor who clicks "Confirm & dispense" again after a timeout or
        dropped connection, WITHOUT changing anything, reuses the SAME
        key -- the backend recognizes the retry and returns the original
        result (D2), never double-deducting inventory or double-writing
        a prescription.
      - A doctor who edits the herbs/dosha after a failed attempt and
        confirms again is issuing a genuinely DIFFERENT request -- this
        gets a FRESH key, so the backend treats it as a new operation
        instead of rejecting it with D1's "already used with a different
        request" 409 (which would otherwise incorrectly block a
        legitimate correction-and-retry).

    Keyed on session state, not a plain module-level variable, so it is
    naturally scoped per Streamlit session (per doctor, per browser tab)
    exactly like every other piece of this UI's state."""
    signature = hashlib.sha256(
        json.dumps(
            {"preview_id": preview_id, "dosha_imbalance": dosha_imbalance, "prescribed_herbs": prescribed_herbs},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if st.session_state.get("confirm_signature") != signature:
        st.session_state.confirm_signature = signature
        st.session_state.confirm_idempotency_key = str(uuid.uuid4())
    return st.session_state.confirm_idempotency_key


def call_confirm(preview_id, dosha_imbalance, prescribed_herbs, token: str, idempotency_key: str = None):
    payload = {
        "preview_id": preview_id,
        "dosha_imbalance": dosha_imbalance,
        "prescribed_herbs": prescribed_herbs,
    }
    try:
        resp = requests.post(
            f"{API_BASE}/prescribe/confirm",
            json=payload,
            headers=_protected_headers(token, idempotency_key),
            timeout=30,
        )
    except requests.exceptions.Timeout:
        # UNCERTAIN OUTCOME, not a known failure -- unlike every other
        # call_* function in this file, /prescribe/confirm has a real side
        # effect (it can create a prescription and deduct inventory), and
        # that work happens inside the backend's own database transaction
        # regardless of whether THIS client is still waiting for the
        # response. A timeout here only tells us the response never
        # arrived in time -- it does NOT tell us whether the backend ever
        # received the request, or whether it committed before or after
        # this client gave up. So this is deliberately NOT treated the
        # same as a clean 4xx/5xx rejection (which does mean "this attempt
        # did not go through"), and this function does not retry
        # automatically -- only the doctor clicking "Confirm & dispense"
        # again decides that. `idempotency_key` (already attached above)
        # is what makes that manual retry safe either way: if the first
        # attempt already committed, the backend recognizes the SAME key
        # on a retry and replays that exact original result instead of
        # creating a second prescription or a second inventory deduction
        # (see get_idempotency_key_for's docstring) -- so the message
        # below says this explicitly rather than leaving the doctor to
        # guess whether it's safe to try again.
        return None, _CONFIRM_TIMEOUT_MESSAGE
    except requests.exceptions.ConnectionError:
        return None, _CONNECTION_ERROR_MESSAGE.format(api_base=API_BASE)
    except requests.exceptions.RequestException as e:
        return None, _UNEXPECTED_ERROR_MESSAGE.format(api_base=API_BASE, error=e)
    _raise_if_session_expired(resp)
    if resp.status_code == 200:
        return resp.json(), None
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    # A 400/409 here is deliberately NOT fatal to the preview -- the
    # backend leaves it usable for a retry (see AyurCore_API_Contract_v1.md,
    # "single-use" note). The UI just needs to let the doctor fix and
    # resubmit against the same preview_id, which happens naturally here
    # since st.session_state.preview isn't cleared on failure.
    return None, f"[{resp.status_code}] {detail}"


def _reset_manual_entry_widgets():
    """Drops the manual-entry screen's two KEYED widgets
    (`manual_dosha`, `manual_herb_editor`) from st.session_state.

    Streamlit only uses a keyed widget's initial value/data argument
    (e.g. this file's `st.text_input("Dosha imbalance", key="manual_dosha",
    ...)` with no `value=`, or `st.data_editor([{"name": "", "grams": 1}],
    ..., key="manual_herb_editor")`) the FIRST time that key is ever
    created in a session. Every render after that restores the key's OWN
    persisted state instead -- for manual_dosha, whatever text was last
    typed; for manual_herb_editor, the row add/edit/delete history --
    regardless of what's passed in code on that render. Since nothing
    used to clear these two keys, a doctor who had been through manual
    entry once already in the session (e.g. deleted the seeded blank
    herb row, or typed a dosha, then clicked "Back" or moved to the next
    patient) would find the "fresh" manual-entry screen silently starting
    from that stale state -- including, in the herb editor's case, zero
    rows and no visible way to add one. See the A6.3 MVP UI bug
    diagnosis for the full mechanism.

    Called ONLY at genuine workflow boundaries -- entering a fresh manual
    workflow (the "Write it myself" button), or explicitly leaving/
    resetting it (the manual-entry screen's own "Back" button, and
    reset_for_next_patient() below) -- never on an ordinary rerun while
    the doctor is still on the manual-entry screen itself, so in-progress
    edits there are preserved for exactly as long as they should be."""
    st.session_state.pop("manual_dosha", None)
    st.session_state.pop("manual_herb_editor", None)


def reset_for_next_patient():
    st.session_state.notes_text = ""
    st.session_state.transcript_draft = ""
    st.session_state.preview = None
    st.session_state.last_result = None
    st.session_state.last_error = None
    st.session_state.ai_unavailable = False
    st.session_state.manual_mode = False
    st.session_state.confirm_signature = None
    st.session_state.confirm_idempotency_key = None
    st.session_state.consultation_stage = "input"
    st.session_state.review_source = None
    _reset_manual_entry_widgets()
    # Same class of stale-keyed-widget issue _reset_manual_entry_widgets()
    # already guards against (see its docstring, "A6.3"), but for the
    # UX-3 review stage's own per-row/dosha/slider widgets. Without this,
    # a doctor who confirms Patient A's prescription and clicks "Start
    # next patient" would find Patient B's review screen silently
    # restoring Patient A's row/quantity/dosha/slider widget state
    # instead of Patient B's fresh preview data -- only cleared here, at
    # the genuine next-patient workflow boundary, never on an ordinary
    # rerun while a doctor is still on the review screen itself.
    _clear_review_widget_state()
    st.session_state.pop("_review_started_at", None)
    # Necessary companion to the input-stage patient-field rehydration
    # (_render_input_stage now seeds patient_name_input/_phone_input/
    # _id_input from _patient_name/_patient_phone/_patient_id so those
    # fields survive Back). Without also clearing the shadow keys here,
    # that same rehydration would carry Patient A's name/phone/id into
    # Patient B's "clean" consultation screen after "Start next patient"
    # -- the same cross-patient bleed-through this repair exists to
    # eliminate, just for patient identity instead of herbs. Spec §14
    # already requires "Start next patient" to return to a clean
    # consultation; this keeps that true now that the fields are wired
    # to persist across Back.
    st.session_state["_patient_name"] = ""
    st.session_state["_patient_phone"] = ""
    st.session_state["_patient_id"] = ""


def _clear_session(message: str = None):
    """Logs the UI out locally: drops the token and every piece of
    in-progress consultation state. Used both by the explicit Logout
    button (after call_logout has already revoked the session
    server-side) and by the SessionExpiredError handler (where the
    session is already invalid/revoked server-side, so there is nothing
    left to revoke -- only local cleanup is needed)."""
    st.session_state.auth = None
    reset_for_next_patient()
    if message:
        st.session_state.session_message = message


def _format_prescription_datetime(created_at) -> str:
    """Formats PrescriptionResponse.created_at (an ISO-8601 string, as
    every JSON HTTP response serializes a Pydantic `datetime` field) into
    something a doctor reads at a glance -- "16 Sep 2026 - 10:23 AM"
    rather than a raw ISO timestamp. Never raises: any value this can't
    parse (a format change, a stray None) falls back to showing whatever
    string was there, so a display-formatting edge case can never break
    the confirmation screen."""
    if not created_at:
        return ""
    try:
        parsed = datetime.fromisoformat(str(created_at))
    except ValueError:
        return str(created_at)
    # Built from portable strftime codes only (no "%-d"/"%-I", which are
    # non-standard platform-specific extensions), with the zero-padded day
    # trimmed manually -- this way the same code behaves identically on
    # every platform this app might run on, not just the one it happens to
    # be developed/tested on.
    day = str(parsed.day)
    return parsed.strftime(f"{day} %b %Y - %I:%M %p")


def _format_prescription_result_html(result: dict) -> str:
    """Pure string-building function (no st.* calls, like this file's
    call_* functions) that renders the post-confirmation prescription as
    a calm, minimal clinical summary -- deliberately not a raw st.json()
    dump of the PrescriptionResponse. Uses ONLY fields already on that
    response (patient_name, dosha_imbalance, prescribed_herbs,
    prescription_id, created_at); introduces no new data and makes no
    call of its own. Kept separate from main() specifically so it's
    directly callable and inspectable from a test with a plain dict, no
    Streamlit runtime involved -- see this file's module docstring,
    "Structure note for testability".

    Returns raw HTML for st.markdown(..., unsafe_allow_html=True). Every
    dynamic value is passed through html.escape() before interpolation,
    since this is doctor-entered clinical data (patient name, dosha
    imbalance, herb names), not a fixed template -- never trusted as
    pre-sanitized HTML.

    Styling uses Streamlit's own CSS theme variables (--text-color, etc.)
    rather than hardcoded colors, so this reads correctly in both light
    and dark Streamlit themes, and neutral rgba() hairlines instead of
    solid borders/card backgrounds, so it doesn't add the boxy "dashboard
    widget" look the rest of this file deliberately avoids.
    """
    patient_name = html.escape(str(result.get("patient_name") or ""))
    dosha_imbalance = html.escape(str(result.get("dosha_imbalance") or ""))
    prescription_id = html.escape(str(result.get("prescription_id") or ""))
    when = html.escape(_format_prescription_datetime(result.get("created_at")))
    herbs = result.get("prescribed_herbs") or []

    herb_rows = "".join(
        f'<div class="ayc-herb-row">'
        f'<span class="ayc-herb-name">{html.escape(str(h.get("name", "")))}</span>'
        f'<span class="ayc-herb-grams">{html.escape(str(h.get("grams", "")))} g</span>'
        f'</div>'
        for h in herbs
    )

    # UX-3 item 11: centered, calm result -- checkmark, "Prescription
    # confirmed", Patient/Date/Dosha, a divider, the herb list, and the
    # prescription id. "No excessive celebration animation. This is
    # clinical software." -- no animation is used anywhere here.
    return f"""
<style>
.ayc-result {{ max-width: 480px; margin: 2rem auto 0.5rem auto; text-align: center; }}
.ayc-result .ayc-check {{
    width: 44px; height: 44px; border-radius: 50%;
    background: var(--ayc-success, #3F7659); color: #FFFFFF;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.35rem; margin: 0 auto 1rem auto;
}}
.ayc-result .ayc-status {{
    font-size: 1.05rem; font-weight: 600; color: var(--text-color, inherit);
    margin-bottom: 1.75rem;
}}
.ayc-result .ayc-fact-row {{
    display: flex; justify-content: space-between; font-size: 0.95rem;
    padding: 0.4rem 0; color: var(--text-color, inherit);
}}
.ayc-result .ayc-fact-label {{ opacity: 0.55; }}
.ayc-result .ayc-fact-value {{ font-weight: 600; text-align: right; }}
.ayc-result .ayc-divider {{
    border: none; border-top: 1px solid var(--ayc-border, rgba(128, 128, 128, 0.25));
    margin: 1.25rem 0;
}}
.ayc-result .ayc-herb-row {{
    display: flex; justify-content: space-between; font-size: 0.95rem;
    padding: 0.35rem 0; color: var(--text-color, inherit);
}}
.ayc-result .ayc-herb-row .ayc-herb-grams {{ font-variant-numeric: tabular-nums; opacity: 0.75; }}
.ayc-result .ayc-rx-id {{
    margin-top: 1.5rem; font-size: 0.85rem; opacity: 0.55;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    color: var(--text-color, inherit);
}}
</style>
<div class="ayc-result">
  <div class="ayc-check">&#10003;</div>
  <div class="ayc-status">Prescription confirmed</div>
  <div class="ayc-fact-row"><span class="ayc-fact-label">Patient</span><span class="ayc-fact-value">{patient_name or "&mdash;"}</span></div>
  <div class="ayc-fact-row"><span class="ayc-fact-label">Date</span><span class="ayc-fact-value">{when or "&mdash;"}</span></div>
  <div class="ayc-fact-row"><span class="ayc-fact-label">Dosha</span><span class="ayc-fact-value">{dosha_imbalance or "&mdash;"}</span></div>
  <hr class="ayc-divider">
  {herb_rows or '<div class="ayc-herb-row"><span>No herbs recorded.</span></div>'}
  <div class="ayc-rx-id">Prescription #{prescription_id}</div>
</div>
"""


# ---- UX-2: workspace CSS ------------------------------------------------
# Injected once at the top of every authenticated render. Uses only
# Streamlit's own CSS variables and avoids any override that could break
# the data_editor, form, or other Streamlit-managed components.
# No [theme] block -- test_streamlit_config.py locks the .streamlit/config.toml
# structure to exactly [browser] gatherUsageStats=false; a [theme] section
# would fail that test.

_WORKSPACE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:wght@600;700&family=Inter:wght@400;500;600&display=swap');

html, body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}

input, textarea, select, button,
[data-testid="stTextInputField"],
[data-testid="stTextAreaRootElement"] textarea,
[data-testid="stNumberInputField"],
[data-testid="stSelectbox"] input {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}

.stApp h1, .stApp h2, .stApp h3 {
    font-family: 'Playfair Display', Georgia, serif !important;
    color: var(--ayc-text);
}

.stApp h4, .stApp h5, .stApp h6, .stApp p, .stApp label {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: var(--ayc-text);
}

:root {
    --ayc-bg: #F7F5EF;
    --ayc-surface: #ffffff;
    --ayc-green-deep: #315C4A;
    --ayc-green-light: #e6edea;
    --ayc-green-hover: #1f4a3d;
    --ayc-text: #24312B;
    --ayc-text-muted: #6D7771;
    --ayc-border: #e0e0e0;
    --ayc-warning: #B77932;
    --ayc-danger: #B95750;
    --ayc-success: #3F7659;
    --ayc-radius: 8px;
}

/* Base styles and overrides */
.stApp, [data-testid="stAppViewContainer"] {
    background-color: var(--ayc-bg);
}
[data-testid="stSidebar"] {
    background-color: var(--ayc-surface);
    border-right: 1px solid var(--ayc-border);
}
[data-testid="stSidebar"] * {
    color: var(--ayc-text) !important;
}

[data-testid="stHeader"],
[data-testid="stToolbar"],
[data-testid="stMainMenu"],
[data-testid="stAppDeployButton"],
[data-testid="stStatusWidget"],
[data-testid="InputInstructions"] {
    display: none !important;
}

[data-testid="stMainBlockContainer"] {
    max-width: 720px;
    padding-top: 3.5rem;
    padding-bottom: 4rem;
}

[data-testid="stMainBlockContainer"]:has([data-testid="stForm"]),
[data-testid="stMainBlockContainer"]:has(.ayc-hero-wrapper) {
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-height: 85vh;
}
[data-testid="stMainBlockContainer"]:has([data-testid="stForm"]) > [data-testid="stVerticalBlock"],
[data-testid="stMainBlockContainer"]:has(.ayc-hero-wrapper) > [data-testid="stVerticalBlock"] {
    flex-grow: 0;
}

/* Button styles - specifically targeting the p tag inside to fix contrast */
.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] button[kind="primaryFormSubmit"] {
    background-color: var(--ayc-green-deep);
    border-color: var(--ayc-green-deep);
    border-radius: var(--ayc-radius);
    min-height: 48px;
}
.stButton > button[kind="primary"] *,
[data-testid="stFormSubmitButton"] button[kind="primaryFormSubmit"] * {
    color: #FFFFFF !important;
}

.stButton > button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] button[kind="primaryFormSubmit"]:hover {
    background-color: var(--ayc-green-hover);
    border-color: var(--ayc-green-hover);
}

.stButton > button[kind="secondary"],
[data-testid="stFormSubmitButton"] button[kind="secondaryFormSubmit"] {
    background-color: var(--ayc-surface);
    border-color: var(--ayc-border);
    border-radius: var(--ayc-radius);
    min-height: 48px;
}
.stButton > button[kind="secondary"] *,
[data-testid="stFormSubmitButton"] button[kind="secondaryFormSubmit"] * {
    color: var(--ayc-text) !important;
}
.stButton > button[kind="secondary"]:hover,
[data-testid="stFormSubmitButton"] button[kind="secondaryFormSubmit"]:hover {
    border-color: var(--ayc-green-deep);
}
.stButton > button[kind="secondary"]:hover *,
[data-testid="stFormSubmitButton"] button[kind="secondaryFormSubmit"]:hover * {
    color: var(--ayc-green-deep) !important;
}

[data-testid="stRadioOption"][data-selected="true"] div div:not([data-testid]) {
    background-color: var(--ayc-green-deep) !important;
}

/* Form inputs */
[data-testid="stTextInputRootElement"],
[data-testid="stTextAreaRootElement"],
[data-testid="stNumberInputContainer"],
[data-testid="stSelectbox"] [role="group"] {
    background-color: var(--ayc-surface) !important;
    border: 1px solid var(--ayc-border) !important;
    border-radius: var(--ayc-radius) !important;
    min-height: 48px;
}
[data-testid="stTextAreaRootElement"] {
    min-height: 96px;
}
[data-testid="stTextInputRootElement"]:focus-within,
[data-testid="stTextAreaRootElement"]:focus-within,
[data-testid="stNumberInputContainer"]:focus-within,
[data-testid="stSelectbox"] [role="group"]:focus-within {
    border-color: var(--ayc-green-deep) !important;
    box-shadow: 0 0 0 3px rgba(41, 95, 78, 0.15);
}
[data-testid="stTextInputField"],
[data-testid="stTextAreaRootElement"] textarea,
[data-testid="stNumberInputField"],
[data-testid="stSelectbox"] input {
    color: var(--ayc-text) !important;
}

[data-testid="stForm"] {
    background: var(--ayc-surface);
    border: 1px solid var(--ayc-border);
    border-radius: 12px;
    padding: 2.5rem;
    margin-top: 0.5rem;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.03);
}

[data-testid="stSlider"] [role="slider"] {
    background-color: var(--ayc-green-deep);
}

/* Landing Page Styles */
.ayc-hero-wrapper {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: flex-start;
    margin-top: -3rem; /* shift up */
    width: 100%;
}
.ayc-hero-content {
    max-width: 640px;
    width: 100%;
    text-align: center;
}
.ayc-hero-title {
    font-size: 3.4rem;
    margin-bottom: 1.5rem;
    color: var(--ayc-green-deep);
    letter-spacing: -0.01em;
}
.ayc-hero-tagline {
    font-size: 1.05rem;
    color: var(--ayc-text-muted);
    line-height: 1.6;
    margin: 0 auto 3.5rem auto;
    font-weight: 400;
    max-width: 90%;
}

/* Process Diagram */
.ayc-process-diagram {
    display: flex;
    flex-direction: column;
    align-items: center;
    margin-bottom: 4rem;
    font-size: 0.9rem;
    font-weight: 500;
    color: var(--ayc-text);
}
.ayc-process-main {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
}
.ayc-proc-step {
    letter-spacing: 0.02em;
    white-space: nowrap;
}
.ayc-proc-line {
    flex-grow: 1;
    height: 1px;
    background-color: var(--ayc-green-deep);
    opacity: 0.25;
    margin: 0 16px;
    min-width: 20px;
}
.ayc-proc-outcome {
    display: flex;
    flex-direction: column;
    align-items: center;
    margin-top: 0.25rem;
}
.ayc-proc-outcome-line {
    width: 1px;
    height: 20px;
    background-color: var(--ayc-green-deep);
    opacity: 0.25;
}
.ayc-proc-outcome-text {
    font-size: 0.75rem;
    color: var(--ayc-text-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-top: 0.5rem;
}

@media (max-width: 600px) {
    .ayc-process-main {
        flex-direction: column;
        gap: 0.5rem;
    }
    .ayc-proc-line {
        width: 1px;
        height: 16px;
        margin: 0;
    }
}

/* Scoped Landing Button Styling */
[data-testid="stMainBlockContainer"]:has(.ayc-hero-wrapper) .stButton > button[kind="primary"] {
    min-height: 40px !important;
    height: 40px !important;
    padding: 0 1.5rem !important;
    border-radius: 4px !important;
    font-size: 0.9rem !important;
    letter-spacing: 0.02em;
    font-weight: 500;
    box-shadow: 0 2px 8px rgba(41, 95, 78, 0.15);
}

/* Other clinical styles (kept mostly same as original UX-3) */
.ayc-section {
    font-size: 0.78rem; font-weight: 600; letter-spacing: 0.09em; text-transform: uppercase;
    color: var(--ayc-text-muted); margin: 2rem 0 0.5rem 0; padding: 0;
}
.ayc-section:first-child { margin-top: 0.75rem; }
.ayc-patient-name { font-size: 1.25rem; font-weight: 600; color: var(--ayc-text); margin: 0 0 1.5rem 0; line-height: 1.3; }
.ayc-divider { border: none; border-top: 1px solid var(--ayc-border); margin: 2rem 0; }
.ayc-rx-eyebrow { font-size: 0.75rem; font-weight: 600; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ayc-text-muted); margin-bottom: 0.4rem; }
.ayc-rx-section-gap { margin-top: 2rem; }
.ayc-rx-subtext { font-size: 0.92rem; color: var(--ayc-text-muted); margin-bottom: 0.6rem; }
.ayc-rx-status { display: inline-flex; align-items: center; gap: 0.4em; font-size: 0.82rem; color: var(--ayc-green-deep); margin-bottom: 1.5rem; }
.ayc-rx-status.ayc-rx-status-manual { color: var(--ayc-text-muted); }
.ayc-rx-dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; display: inline-block; }
.ayc-patient-card { background: var(--ayc-surface); border: 1px solid var(--ayc-border); border-radius: var(--ayc-radius); padding: 16px 20px; margin-bottom: 8px; }
.ayc-patient-card-name { font-size: 1.2rem; font-weight: 600; color: var(--ayc-text); margin: 0.15rem 0 0.2rem 0; }
.ayc-patient-card-meta { font-size: 0.85rem; color: var(--ayc-text-muted); }
.ayc-rx-table { background: var(--ayc-surface); border: 1px solid var(--ayc-border); border-radius: var(--ayc-radius); overflow: hidden; margin-bottom: 12px; }
.ayc-rx-row-view { display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; border-bottom: 1px solid var(--ayc-border); color: var(--ayc-text); font-size: 0.98rem; }
.ayc-rx-row-view:last-child { border-bottom: none; }
.ayc-rx-row-view.ayc-rx-empty { color: var(--ayc-text-muted); font-style: normal; justify-content: center; }
.ayc-rx-row-name { font-weight: 500; }
.ayc-rx-row-qty { font-variant-numeric: tabular-nums; color: var(--ayc-text-muted); }
.ayc-rx-dosha-view { background: var(--ayc-green-light); border-radius: var(--ayc-radius); padding: 12px 20px; margin-bottom: 16px; color: var(--ayc-text); font-size: 1rem; }
.ayc-rx-row-edit { border: 1px solid var(--ayc-border); border-radius: var(--ayc-radius); padding: 8px 12px; margin-bottom: 8px; background: var(--ayc-surface); }
.ayc-rx-row-edit .ayc-rx-row-name { padding-top: 8px; }
.ayc-rx-row-highlight { border-color: var(--ayc-warning); background: #FBF3EA; }
.ayc-rx-add-row { margin: 12px 0 8px 0; }
</style>
"""


# ---- UI helpers ---------------------------------------------------------


def _render_landing_page():
    st.markdown('''
    <div class="ayc-hero-wrapper">
        <div class="ayc-hero-content">
            <h1 class="ayc-hero-title">AyurCore</h1>
            <p class="ayc-hero-tagline">AI-assisted prescribing &mdash; the AI proposes, you confirm. Nothing is ever final until you say so.</p>
            
            <div class="ayc-process-diagram">
                <div class="ayc-process-main">
                    <span class="ayc-proc-step">Patient</span>
                    <span class="ayc-proc-line"></span>
                    <span class="ayc-proc-step">AI proposes</span>
                    <span class="ayc-proc-line"></span>
                    <span class="ayc-proc-step">Doctor reviews</span>
                    <span class="ayc-proc-line"></span>
                    <span class="ayc-proc-step">Doctor confirms</span>
                </div>
                <div class="ayc-proc-outcome">
                    <span class="ayc-proc-outcome-line"></span>
                    <span class="ayc-proc-outcome-text">Audit &amp; Inventory</span>
                </div>
            </div>
        </div>
    </div>
    ''', unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([3, 2, 3])
    with col2:
        if st.button("Sign in", type="primary", use_container_width=True, key="landing_signin"):
            st.session_state.landing_viewed = True
            st.rerun()

def _render_login_screen():

    if st.session_state.get("session_message"):
        st.warning(st.session_state.session_message)
        st.session_state.session_message = None

    st.subheader("Sign in")
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary")

    if submitted:
        if not username or not password:
            st.error("Enter both a username and a password.")
        else:
            with st.spinner("Signing in…"):
                auth, error = call_login(username, password)
            if error:
                # UX-1: the raw "[401] Invalid username or password." that
                # call_login returns (and that
                # tests/test_ui_auth_integration.py asserts on) is
                # translated here, on the way to the screen, rather than
                # inside call_login itself.
                st.error(_friendly_message(error, operation="login"))
            else:
                st.session_state.auth = auth
                st.rerun()


def _render_sidebar_auth(auth):
    """Development/session indicator -- moved to sidebar per spec §4.
    Clinically irrelevant: not in the primary consultation hierarchy."""
    with st.sidebar:
        st.caption(f"Dr {auth['username']}")
        st.caption(f"Clinic {auth['clinic_id']}")
        if st.button("Sign out", key="sidebar_logout"):
            with st.spinner("Signing out…"):
                ok, error = call_logout(auth["token"])
            if not ok and error:
                st.warning(
                    "Signed out on this device. Couldn't confirm with AyurCore — "
                    "mention it to whoever set this up if this is a shared computer."
                )
            _clear_session()
            st.rerun()


def _render_input_stage(auth):
    """Stage: patient + clinical notes + action choice.

    This is the central consultation entry point. The doctor enters
    patient details and notes, then chooses one of two peer paths:
    Draft with AI or Write it myself.

    Neither path is framed as a fallback. Both are peer clinical workflows.
    """
    token = auth["token"]

    st.markdown('<div class="ayc-section">Patient</div>', unsafe_allow_html=True)

    # value= seeds these keyed widgets from the existing _patient_* shadow
    # state (set below, and by the review/manual-entry stages before they
    # navigate back here). Streamlit only honors a keyed widget's value=
    # the first time that key exists in a session -- and progressive
    # disclosure means these keys are absent (garbage-collected by
    # Streamlit) on every run where the doctor is on a different stage,
    # so returning here after Back counts as "first time" again and the
    # seed is honored; while the doctor keeps typing on this same stage,
    # the key persists across reruns and value= is correctly ignored in
    # favor of their live input. Same shadow-state mechanism as before,
    # unchanged; this only feeds it back into the widgets it already fed.
    col1, col2, col3 = st.columns([3, 2, 2])
    with col1:
        patient_name = st.text_input(
            "Name", value=st.session_state.get("_patient_name", ""),
            placeholder="Patient name", key="patient_name_input", label_visibility="collapsed",
        )
    with col2:
        patient_phone = st.text_input(
            "Phone", value=st.session_state.get("_patient_phone", ""),
            placeholder="Phone (optional)", key="patient_phone_input", label_visibility="collapsed",
        )
    with col3:
        patient_id = st.text_input(
            "ID", value=st.session_state.get("_patient_id", ""),
            placeholder="Patient ID (optional)", key="patient_id_input", label_visibility="collapsed",
        )

    # Persist across reruns so the doctor doesn't lose their patient info
    # when the action buttons cause a rerun.
    if patient_name:
        st.session_state["_patient_name"] = patient_name
    if patient_phone:
        st.session_state["_patient_phone"] = patient_phone
    if patient_id:
        st.session_state["_patient_id"] = patient_id

    st.markdown('<div class="ayc-section">Consultation</div>', unsafe_allow_html=True)

    input_mode = st.radio("Notes input", ["Type", "Record voice"], horizontal=True, label_visibility="collapsed")

    if input_mode == "Type":
        st.session_state.notes_text = st.text_area(
            "Clinical notes",
            value=st.session_state.notes_text,
            height=160,
            placeholder="Patient reports poor sleep, digestive discomfort and increased stress…",
            label_visibility="visible",
        )
    else:
        if not SARVAM_API_KEY:
            st.info("Voice transcription isn't configured. Type the notes instead, or set SARVAM_API_KEY and restart.")
        audio_value = st.audio_input("Record clinical notes", label_visibility="collapsed")
        if audio_value is not None and st.button("Transcribe recording"):
            with st.spinner("Transcribing…"):
                try:
                    st.session_state.transcript_draft = transcribe_audio(audio_value.read())
                except Exception:
                    st.error(MSG_TRANSCRIPTION_FAILED)
        if st.session_state.transcript_draft:
            st.session_state.notes_text = st.text_area(
                "Transcript — review and correct before continuing",
                value=st.session_state.transcript_draft,
                height=160,
            )
            st.session_state.transcript_draft = st.session_state.notes_text

    if st.session_state.last_error:
        st.error(st.session_state.last_error)

    st.markdown("<hr class='ayc-divider'>", unsafe_allow_html=True)

    # The two peer actions. Both are at the same visual level.
    # "Draft with AI" is type="primary" only when we have all required
    # inputs, making it the obvious default when notes are ready.
    # "Write it myself" is always available regardless of AI state.
    notes_ready = bool(patient_name and st.session_state.notes_text)
    name_ready = bool(patient_name)

    col_a, col_b = st.columns(2)
    with col_a:
        draft_ai = st.button(
            "Draft with AI",
            type="primary",
            disabled=not notes_ready,
            use_container_width=True,
            key="btn_draft_ai",
        )
    with col_b:
        write_myself = st.button(
            "Write it myself",
            type="secondary",
            disabled=not name_ready,
            use_container_width=True,
            key="btn_write_myself",
        )

    if draft_ai:
        # Preserve patient inputs for the review stage
        st.session_state["_patient_name"] = patient_name
        st.session_state["_patient_phone"] = patient_phone
        st.session_state["_patient_id"] = patient_id
        with st.spinner("Preparing clinical draft…"):
            try:
                preview, error, ai_unavailable = call_preview(
                    patient_name, patient_phone, patient_id,
                    st.session_state.notes_text, token,
                )
            except SessionExpiredError:
                _clear_session(MSG_SESSION)
                st.rerun()
                return
        if error:
            st.session_state.last_error = _friendly_message(
                error, operation="preview", ai_unavailable=ai_unavailable,
            )
            st.session_state.ai_unavailable = ai_unavailable
        else:
            st.session_state.preview = preview
            st.session_state.last_error = None
            st.session_state.ai_unavailable = False
            st.session_state.consultation_stage = "review"
            st.session_state.review_source = "ai"
            st.session_state.manual_mode = False
            # UX-3: seed the review screen's own per-row/dosha widget
            # state from this FRESH preview now, at the moment it's set
            # -- not lazily on first render -- so it can never pick up a
            # previous preview's leftover row state (spec §18).
            _seed_review_rows(preview["prescribed_herbs"], preview["dosha_imbalance"])
            st.session_state["_review_started_at"] = datetime.now()
        st.rerun()

    if write_myself:
        # Preserve patient inputs for the manual entry stage
        st.session_state["_patient_name"] = patient_name
        st.session_state["_patient_phone"] = patient_phone
        st.session_state["_patient_id"] = patient_id
        st.session_state.last_error = None
        st.session_state.ai_unavailable = False
        st.session_state.manual_mode = True
        _reset_manual_entry_widgets()
        st.session_state.consultation_stage = "manual_entry"
        st.rerun()


def _render_manual_entry_stage(auth):
    """Stage: doctor enters dosha and herbs directly.

    This is a first-class clinical workflow, not a fallback.
    No mention of AI unavailability; no fallback framing.
    Same herb validation applies when the doctor continues to review.
    """
    token = auth["token"]

    patient_name = st.session_state.get("_patient_name", "")
    patient_phone = st.session_state.get("_patient_phone", "")
    patient_id = st.session_state.get("_patient_id", "")

    if patient_name:
        st.markdown(
            f'<div class="ayc-patient-name">{html.escape(patient_name)}</div>',
            unsafe_allow_html=True,
        )

    if st.session_state.last_error:
        st.error(st.session_state.last_error)

    st.markdown('<div class="ayc-section">Dosha imbalance</div>', unsafe_allow_html=True)
    manual_dosha = st.text_input(
        "Dosha imbalance",
        key="manual_dosha",
        placeholder="e.g. Vata",
        label_visibility="collapsed",
    )

    st.markdown('<div class="ayc-section">Prescribed herbs</div>', unsafe_allow_html=True)
    manual_herbs_raw = st.data_editor(
        [{"name": "", "grams": 1}],
        column_config={
            "name": st.column_config.TextColumn("Herb", help="Canonical herb name from the clinic's inventory"),
            "grams": st.column_config.NumberColumn("Grams", min_value=1, max_value=10000, step=1),
        },
        num_rows="dynamic",
        use_container_width=True,
        key="manual_herb_editor",
    )
    if hasattr(manual_herbs_raw, "to_dict"):
        manual_herbs_raw = manual_herbs_raw.to_dict("records")
    # Drop blank rows the data_editor's own "add row" control leaves behind
    # so an empty trailing row never gets sent as a herb with no name.
    manual_herbs = [h for h in manual_herbs_raw if h.get("name")]

    st.markdown("<div style='margin-top: 1.5rem;'></div>", unsafe_allow_html=True)
    col_a, col_b = st.columns(2)
    with col_a:
        continue_btn = st.button(
            "Continue to review",
            type="primary",
            disabled=not (manual_dosha and manual_herbs),
            use_container_width=True,
            key="btn_manual_continue",
        )
    with col_b:
        back_btn = st.button(
            "Back",
            type="secondary",
            use_container_width=True,
            key="btn_manual_back",
        )

    if continue_btn:
        with st.spinner("Checking herb names and stock…"):
            try:
                preview, error = call_manual_preview(
                    patient_name, patient_phone, patient_id,
                    manual_dosha, manual_herbs, token,
                )
            except SessionExpiredError:
                _clear_session(MSG_SESSION)
                st.rerun()
                return
        if error:
            st.session_state.last_error = _friendly_message(
                error, operation="manual_preview",
            )
            st.session_state.ai_unavailable = False
        else:
            st.session_state.preview = preview
            st.session_state.last_error = None
            st.session_state.consultation_stage = "review"
            st.session_state.review_source = "manual"
            # UX-3: seed fresh, same reasoning as the AI path above.
            _seed_review_rows(preview["prescribed_herbs"], preview["dosha_imbalance"])
            st.session_state["_review_started_at"] = datetime.now()
        st.rerun()

    if back_btn:
        # Back from manual entry -> back to input stage.
        # Clears manual widget state so next visit starts fresh.
        # Does NOT call the AI or confirm anything.
        st.session_state.consultation_stage = "input"
        st.session_state.manual_mode = False
        st.session_state.last_error = None
        _reset_manual_entry_widgets()
        st.rerun()


def _seed_review_rows(herbs, dosha):
    """(Re)initializes the UX-3 review screen's per-row prescription
    state from a (herbs, dosha) source -- either a fresh preview (AI or
    manual draft) or an edit-session snapshot (Cancel). Assigns each herb
    a fresh row id that is NEVER reused within a session, so a removed
    row's old widget key is simply orphaned/inert afterward rather than
    silently reused for a different herb later -- the same class of
    stale-keyed-widget bug _reset_manual_entry_widgets() already guards
    against for the manual-entry screen (see its own docstring, "A6.3"),
    applied here to UX-3's per-row model. Must be the only place that
    creates row ids.

    Writes each row's quantity directly into st.session_state[f"qty_{rid}"]
    rather than passing it as a widget value= -- Streamlit only honors a
    keyed widget's value= the FIRST time that key exists in a session;
    every render after that uses whatever is already in session_state
    for that key regardless of what's passed in code. Writing directly
    here (and from the +/- stepper handlers, and from Cancel) is the
    documented, correct way to programmatically set a keyed widget's
    displayed value on the next render.
    """
    next_id = st.session_state.get("_review_next_row_id", 0)
    row_order = []
    row_names = {}
    for h in herbs:
        rid = f"r{next_id}"
        next_id += 1
        row_order.append(rid)
        row_names[rid] = h.get("name", "")
        st.session_state[f"qty_{rid}"] = int(h.get("grams") or 1)
    st.session_state["_review_next_row_id"] = next_id
    st.session_state["_review_row_order"] = row_order
    st.session_state["_review_row_names"] = row_names

    if dosha in REVIEW_DOSHA_OPTIONS:
        st.session_state["review_dosha_select"] = dosha
        st.session_state["review_dosha_custom"] = ""
    else:
        st.session_state["review_dosha_select"] = _DOSHA_OTHER
        st.session_state["review_dosha_custom"] = dosha or ""


def _current_review_herbs() -> list:
    """Assembles the review screen's CURRENT working herb list from its
    own per-row widget state -- always the final, as-of-now values
    (spec §19: idempotency key generation must receive the submitted
    payload, never a stale pre-edit snapshot). Rows with no name (should
    not normally occur, since rows are only created via _seed_review_rows
    or the canonical-medicine dropdown) are defensively skipped."""
    row_order = st.session_state.get("_review_row_order", [])
    row_names = st.session_state.get("_review_row_names", {})
    herbs = []
    for rid in row_order:
        name = row_names.get(rid, "")
        if not name:
            continue
        herbs.append({"name": name, "grams": st.session_state.get(f"qty_{rid}", 0)})
    return herbs


def _current_review_dosha() -> str:
    """Current working dosha value -- the curated selectbox's choice, or
    the free-text "Other" entry when that's selected (spec §8: a
    controlled UI that never silently blocks a value the backend would
    otherwise accept)."""
    selected = st.session_state.get("review_dosha_select", "")
    if selected == _DOSHA_OTHER:
        return (st.session_state.get("review_dosha_custom") or "").strip()
    return selected or ""


def _clear_review_widget_state():
    """Drops every one of the UX-3 review screen's own keyed widgets --
    per-row quantities, row bookkeeping, the dosha selector/custom text,
    the add-medicine dropdown, the edit-mode snapshot, and the slide-to-
    confirm slider -- so the next preview to reach this screen (a new
    AI/manual draft for the same patient after Back, or a new patient
    after "Start next patient") always seeds fresh from ITS OWN data,
    never a previous preview's leftover widget state (spec §18). Same
    fix class as _reset_manual_entry_widgets(); called from both the
    review screen's own Back handler and reset_for_next_patient()."""
    for rid in st.session_state.get("_review_row_order", []):
        st.session_state.pop(f"qty_{rid}", None)
    st.session_state.pop("_review_row_order", None)
    st.session_state.pop("_review_row_names", None)
    st.session_state.pop("_review_next_row_id", None)
    st.session_state.pop("review_dosha_select", None)
    st.session_state.pop("review_dosha_custom", None)
    st.session_state.pop("review_add_medicine_select", None)
    st.session_state.pop("_review_edit_snapshot", None)
    st.session_state.pop("_review_highlight", None)
    # Reset, not pop -- slide_confirm is read with a session_state.get(...,
    # 0) default either way, but explicitly zeroing it here (rather than
    # leaving it absent) makes the "always starts at 0 for a new preview"
    # guarantee readable at the call site without relying on the default.
    st.session_state["slide_confirm"] = 0
    st.session_state["review_editing"] = False


def _render_review_stage(auth):
    """Stage: review a preview (AI or manual) before confirming -- UX-3
    clinical prescription editor.

    Clearly labels the source (spec §9, unchanged from UX-2):
    - AI:     "AI DRAFT" / "AI generated" / "Review the proposed
      prescription before confirming."
    - Manual: "PRESCRIPTION" / "Doctor-entered" / "Review your
      prescription before confirming."
    Never shows the AI label on a manually created prescription.

    The prescription editor has two states: a calm read-only summary
    (default) and an explicit editing state entered via "Edit
    prescription" (steppers, remove, add-medicine dropdown, dosha
    selector; Save changes / Cancel). Confirming is a slide-to-confirm
    gesture (st.slider -- natively mouse/touch-drag AND keyboard
    operable via arrow keys/Home/End, satisfying spec §17's
    accessibility requirement without a separate control) that, on
    reaching 100, calls the SAME call_confirm() flow UX-1/UX-2 already
    established: no new confirmation endpoint, no bypass of auth, RBAC,
    idempotency, inventory, or audit -- the backend remains the sole
    authority for committing the prescription.

    Back returns to the appropriate previous stage without triggering
    any AI call, confirmation, or inventory deduction.
    """
    token = auth["token"]
    preview = st.session_state.preview
    review_source = st.session_state.get("review_source", "ai")

    patient_name = st.session_state.get("_patient_name", preview.get("patient_name", ""))
    patient_phone = st.session_state.get("_patient_phone", "")
    patient_id = st.session_state.get("_patient_id", "")

    # Defensive seed: normally already done by _render_input_stage's
    # draft_ai branch or _render_manual_entry_stage's continue_btn branch
    # the moment this preview was set. Guards against a preview reaching
    # this stage some other way (e.g. the legacy-state reconciliation in
    # main()) with no row state yet.
    if "_review_row_order" not in st.session_state:
        _seed_review_rows(preview["prescribed_herbs"], preview["dosha_imbalance"])
    if "_review_started_at" not in st.session_state:
        st.session_state["_review_started_at"] = datetime.now()

    # ---- Back -- quiet, secondary, always available on this screen -----
    if st.button("← Back", key="btn_review_back", type="secondary"):
        # Does NOT confirm, does NOT consume a preview, does NOT deduct
        # inventory, does NOT generate another AI call.
        if review_source == "manual":
            st.session_state.consultation_stage = "manual_entry"
        else:
            # Clear the preview so re-running "Draft with AI" gets a
            # fresh request; do not silently reuse the existing preview.
            st.session_state.consultation_stage = "input"
            st.session_state.preview = None
        _clear_review_widget_state()
        st.session_state.pop("_review_started_at", None)
        st.session_state.last_error = None
        st.rerun()
        return

    # ---- 1. AI DRAFT header (spec §1, §9) -------------------------------
    if review_source == "ai":
        st.markdown('<div class="ayc-rx-eyebrow">AI DRAFT</div>', unsafe_allow_html=True)
        st.markdown('<div class="ayc-rx-subtext">Review the proposed prescription before confirming.</div>', unsafe_allow_html=True)
        st.markdown('<div class="ayc-rx-status"><span class="ayc-rx-dot"></span>AI generated</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="ayc-rx-eyebrow">PRESCRIPTION</div>', unsafe_allow_html=True)
        st.markdown('<div class="ayc-rx-subtext">Review your prescription before confirming.</div>', unsafe_allow_html=True)
        st.markdown('<div class="ayc-rx-status ayc-rx-status-manual"><span class="ayc-rx-dot"></span>Doctor-entered</div>', unsafe_allow_html=True)

    if st.session_state.last_error:
        st.error(st.session_state.last_error)

    # ---- 2. Patient context (spec §2) -----------------------------------
    started_at = st.session_state["_review_started_at"]
    when_str = started_at.strftime(f"{started_at.day} %B %Y · %I:%M %p")
    meta_bits = [html.escape(b) for b in (patient_phone, patient_id) if b]
    meta_bits.append(html.escape(when_str))
    st.markdown(
        f'<div class="ayc-patient-card">'
        f'<div class="ayc-rx-eyebrow">PATIENT</div>'
        f'<div class="ayc-patient-card-name">{html.escape(patient_name) if patient_name else "&mdash;"}</div>'
        f'<div class="ayc-patient-card-meta">{" &middot; ".join(meta_bits)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ---- 3/8/9. Prescription editor (view state or edit state) ---------
    editing = st.session_state.get("review_editing", False)
    st.markdown('<div class="ayc-rx-eyebrow ayc-rx-section-gap">PRESCRIPTION</div>', unsafe_allow_html=True)

    if not editing:
        herbs_now = _current_review_herbs()
        dosha_now = _current_review_dosha()

        if herbs_now:
            rows_html = "".join(
                f'<div class="ayc-rx-row-view">'
                f'<span class="ayc-rx-row-name">{html.escape(h["name"])}</span>'
                f'<span class="ayc-rx-row-qty">{html.escape(str(h["grams"]))} g</span>'
                f'</div>'
                for h in herbs_now
            )
        else:
            rows_html = '<div class="ayc-rx-row-view ayc-rx-empty">No medicines in this prescription yet.</div>'
        st.markdown(f'<div class="ayc-rx-table">{rows_html}</div>', unsafe_allow_html=True)

        st.markdown(
            f'<div class="ayc-rx-dosha-view">'
            f'<div class="ayc-rx-eyebrow">DOSHA IMBALANCE</div>'
            f'{html.escape(dosha_now) if dosha_now else "&mdash;"}'
            f'</div>',
            unsafe_allow_html=True,
        )

        if st.button("Edit prescription", key="btn_edit_prescription", type="secondary"):
            st.session_state["_review_edit_snapshot"] = {"herbs": list(herbs_now), "dosha": dosha_now}
            st.session_state["review_editing"] = True
            st.rerun()
            return

        # ---- 10. Slide to confirm (spec §10, §17) -----------------------
        st.markdown('<div class="ayc-rx-eyebrow ayc-rx-section-gap">CONFIRM</div>', unsafe_allow_html=True)
        can_confirm = bool(dosha_now and herbs_now)
        if not can_confirm:
            st.caption("Add at least one medicine and a dosha imbalance before confirming.")
        slide_value = st.slider(
            "Slide to confirm prescription",
            min_value=0, max_value=100,
            value=st.session_state.get("slide_confirm", 0),
            key="slide_confirm",
            disabled=not can_confirm,
            help="Drag to the end (or press End/arrow keys with this control focused) to confirm.",
        )
        if can_confirm and slide_value >= 100:
            with st.spinner("Confirming prescription…"):
                idempotency_key = get_idempotency_key_for(preview["preview_id"], dosha_now, herbs_now)
                try:
                    result, error = call_confirm(
                        preview["preview_id"], dosha_now, herbs_now, token, idempotency_key,
                    )
                except SessionExpiredError:
                    _clear_session(MSG_SESSION)
                    st.rerun()
                    return
            # Always reset the slider back to 0 before the next render.
            # Without this, a value stuck at 100 would either re-trigger
            # confirm on the very next unrelated rerun (if an error kept
            # the doctor on this stage) or, before reset_for_next_patient
            # runs, show as already-slid the next time this key is used
            # (defense in depth alongside that reset).
            st.session_state["slide_confirm"] = 0
            if error:
                # operation="confirm" preserves the "outcome unknown"
                # timeout wording verbatim -- see _translate_call_error().
                st.session_state.last_error = _friendly_message(error, operation="confirm")
            else:
                st.session_state.last_result = result
                st.session_state.last_error = None
                st.session_state.consultation_stage = "confirmed"
            st.rerun()
            return
    else:
        # ---- 4/5/6/7. Steppers, direct edit, remove, add medicine -------
        for rid in list(st.session_state.get("_review_row_order", [])):
            name = st.session_state.get("_review_row_names", {}).get(rid, "")
            qty_key = f"qty_{rid}"
            highlighted = name and name == st.session_state.get("_review_highlight")
            row_class = "ayc-rx-row-edit ayc-rx-row-highlight" if highlighted else "ayc-rx-row-edit"
            st.markdown(f'<div class="{row_class}">', unsafe_allow_html=True)
            col_name, col_minus, col_qty, col_plus, col_remove = st.columns([4, 1, 2, 1, 1])
            with col_name:
                st.markdown(f'<div class="ayc-rx-row-name">{html.escape(name)}</div>', unsafe_allow_html=True)
            with col_minus:
                if st.button("−", key=f"minus_{rid}", help=f"Decrease {name} by 1g"):
                    st.session_state[qty_key] = max(1, st.session_state.get(qty_key, 1) - 1)
                    st.rerun()
            with col_qty:
                st.number_input(
                    f"{name} grams", min_value=1, max_value=10000, step=1,
                    key=qty_key, label_visibility="collapsed",
                )
            with col_plus:
                if st.button("+", key=f"plus_{rid}", help=f"Increase {name} by 1g"):
                    st.session_state[qty_key] = min(10000, st.session_state.get(qty_key, 1) + 1)
                    st.rerun()
            with col_remove:
                if st.button("✕", key=f"remove_{rid}", help=f"Remove {name}"):
                    st.session_state["_review_row_order"].remove(rid)
                    st.session_state["_review_row_names"].pop(rid, None)
                    st.session_state.pop(qty_key, None)
                    st.rerun()
            st.markdown("</div>", unsafe_allow_html=True)
        st.session_state.pop("_review_highlight", None)

        st.markdown('<div class="ayc-rx-add-row">', unsafe_allow_html=True)
        add_col1, add_col2 = st.columns([4, 1])
        with add_col1:
            existing_names = set(st.session_state.get("_review_row_names", {}).values())
            # Controlled dropdown only -- CLINIC_HERB_KEYTERMS is this
            # file's single canonical herb name list (spec §6: no second
            # naming system). See that constant's own comment for the
            # known static-list-vs-live-stock limitation.
            options = ["— Select medicine —"] + [h for h in CLINIC_HERB_KEYTERMS if h not in existing_names]
            chosen = st.selectbox(
                "Add medicine", options=options,
                key="review_add_medicine_select", label_visibility="collapsed",
            )
        with add_col2:
            add_clicked = st.button("+ Add", key="btn_add_medicine", use_container_width=True)
        st.markdown("</div>", unsafe_allow_html=True)

        if add_clicked and chosen and chosen != "— Select medicine —":
            if chosen in st.session_state.get("_review_row_names", {}).values():
                # Spec §7: friendly message, focus/highlight the existing
                # row, never a silent duplicate.
                st.warning(f'"{chosen}" is already in this prescription.')
                st.session_state["_review_highlight"] = chosen
            else:
                next_id = st.session_state.get("_review_next_row_id", 0)
                rid = f"r{next_id}"
                st.session_state["_review_next_row_id"] = next_id + 1
                st.session_state["_review_row_order"].append(rid)
                st.session_state["_review_row_names"][rid] = chosen
                st.session_state[f"qty_{rid}"] = 1
                st.session_state["review_add_medicine_select"] = "— Select medicine —"
                st.rerun()

        # ---- 8. Dosha (controlled selector, spec §8) --------------------
        st.markdown('<div class="ayc-rx-eyebrow ayc-rx-section-gap">DOSHA IMBALANCE</div>', unsafe_allow_html=True)
        dosha_select = st.selectbox(
            "Dosha imbalance", options=REVIEW_DOSHA_OPTIONS + [_DOSHA_OTHER],
            key="review_dosha_select", label_visibility="collapsed",
        )
        if dosha_select == _DOSHA_OTHER:
            st.text_input(
                "Custom dosha imbalance", key="review_dosha_custom",
                placeholder="Type the dosha imbalance", label_visibility="collapsed",
            )

        # ---- 9. Save / Cancel -------------------------------------------
        st.markdown('<div class="ayc-rx-section-gap"></div>', unsafe_allow_html=True)
        save_col, cancel_col = st.columns(2)
        with save_col:
            save_clicked = st.button(
                "Save changes", key="btn_save_prescription",
                type="primary", use_container_width=True,
            )
        with cancel_col:
            cancel_clicked = st.button(
                "Cancel", key="btn_cancel_prescription",
                type="secondary", use_container_width=True,
            )

        if save_clicked:
            # Working state already IS the new state going forward --
            # nothing to write back, just leave editing.
            st.session_state["review_editing"] = False
            st.session_state["_review_edit_snapshot"] = None
            st.rerun()
            return
        if cancel_clicked:
            snapshot = st.session_state.get("_review_edit_snapshot") or {"herbs": [], "dosha": ""}
            _seed_review_rows(snapshot["herbs"], snapshot["dosha"])
            st.session_state["review_editing"] = False
            st.session_state["_review_edit_snapshot"] = None
            st.rerun()
            return


def _render_confirmed_stage():
    """Stage: prescription confirmed. Show the result and offer next patient."""
    result = st.session_state.last_result
    st.markdown(_format_prescription_result_html(result), unsafe_allow_html=True)
    st.markdown("<div style='margin-top: 1.5rem;'></div>", unsafe_allow_html=True)
    if st.button("Start next patient", type="primary", key="btn_next_patient"):
        reset_for_next_patient()
        st.rerun()


def main():
    st.set_page_config(page_title="AyurCore", page_icon="🌿", layout="centered")

    # Inject the workspace's warm off-white/muted-green visual system FIRST,
    # before the auth gate below. It previously ran only after a successful
    # login (right before the sidebar/page heading), which meant the login
    # screen itself -- rendered by the auth-gate branch immediately below,
    # which returns via st.stop() before ever reaching that later line --
    # never received it and fell back to Streamlit's own default theme
    # resolution. With no [theme] block in .streamlit/config.toml (locked by
    # test_streamlit_config.py), that default follows the visitor's browser/
    # OS dark-mode preference, which is what produced the "old dark
    # Streamlit theme" on the login screen while the post-login workspace
    # already showed the correct palette. _WORKSPACE_CSS is a global
    # stylesheet (`.stApp`, `[data-testid="stAppViewContainer"]`,
    # `[data-testid="stSidebar"]`, button/input styling) with nothing in it
    # that depends on being authenticated, so moving the injection earlier
    # is a pure visual fix -- no stage, auth, or RBAC logic below is
    # reordered or touched.
    st.markdown(_WORKSPACE_CSS, unsafe_allow_html=True)

    for _k, _v in _DEFAULTS.items():
        st.session_state.setdefault(_k, _v)
    st.session_state.setdefault("session_message", None)
    # Patient-identity scratch keys (not in _DEFAULTS -- they exist only to
    # carry patient data from input stage to review/manual-entry stages
    # across reruns without the widget values being lost).
    st.session_state.setdefault("_patient_name", "")
    st.session_state.setdefault("_patient_phone", "")
    st.session_state.setdefault("_patient_id", "")

    st.session_state.setdefault("landing_viewed", False)
    
    # --- auth gate: nothing below this point renders until logged in ---
    if not st.session_state.auth:
        if not st.session_state.landing_viewed:
            _render_landing_page()
        else:
            _render_login_screen()
        st.stop()

    auth = st.session_state.auth
    token = auth["token"]

    # Backend-authoritative role check happens on every prescribe/* call
    # regardless (require_role(ROLE_DOCTOR) in backend/main.py, via
    # backend/rbac.py) -- this is a UI-side short-circuit for a role that
    # call would reject anyway with 403, so a non-DOCTOR account gets a
    # clear explanation instead of a confusing failed button.
    if auth.get("role") != REQUIRED_ROLE:
        _render_sidebar_auth(auth)
        st.error(MSG_WRONG_ROLE)
        st.stop()

    # (_WORKSPACE_CSS is now injected once, at the very top of main(), before
    # the auth gate -- see the comment there. No longer duplicated here.)

    # Dev/session indicator in sidebar, out of the clinical hierarchy (spec §4).
    _render_sidebar_auth(auth)

    # Page heading — small and secondary, not a dominant dashboard title (spec §3).
    st.markdown("### AyurCore")

    # Reconcile legacy state with UX-2 stage. Pre-UX-2 sessions may have
    # `preview` set or `last_result` set but `consultation_stage` at the
    # wrong value. This brings them into alignment on first load after upgrade.
    if st.session_state.last_result and st.session_state.consultation_stage != "confirmed":
        st.session_state.consultation_stage = "confirmed"
    elif st.session_state.preview is not None and st.session_state.consultation_stage == "input":
        st.session_state.consultation_stage = "review"
        st.session_state.review_source = "ai" if not st.session_state.manual_mode else "manual"
    elif st.session_state.manual_mode and st.session_state.consultation_stage == "input":
        st.session_state.consultation_stage = "manual_entry"

    # --- Progressive disclosure: render exactly one stage at a time ---
    stage = st.session_state.consultation_stage

    if stage == "confirmed":
        _render_confirmed_stage()
    elif stage == "review":
        _render_review_stage(auth)
    elif stage == "manual_entry":
        _render_manual_entry_stage(auth)
    else:
        _render_input_stage(auth)


if __name__ == "__main__":
    main()
