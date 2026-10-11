"""
UX-1 verification: clinician-facing error translation (ui/app.py).

Scope. This file tests ONE thing: that every backend or network outcome
the UI can encounter is turned into language a doctor can act on, and that
nothing internal ever reaches the screen. It touches no database, starts no
server, makes no network call, and needs no running backend -- every test
below calls pure functions with literal arguments.

Why these functions are pure. `_friendly_error`, `_split_call_error` and
`_translate_call_error` were written with no `st.*` access and no `requests`
access precisely so they could be tested like this. That mirrors the
structure note in ui/app.py's own module docstring: `import ui.app` defines
functions and constants without touching the Streamlit runtime.

What this file deliberately does NOT test. The call_* networking functions
are unchanged by UX-1 and keep returning their raw "[<status>] <detail>"
strings -- tests/test_ui_auth_integration.py asserts on exactly those raw
strings and remains the owner of that contract. Translation happens at
render time instead, so this file tests the translator, not the caller.

Run with:
    ./venv/bin/pytest tests/test_ui_error_translation.py -v
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ui.app as ui_app  # noqa: E402  (sys.path must be set up first, above)


# ---------------------------------------------------------------------------
# Anything that must never reach a clinician.
#
# Checked against EVERY message this module can produce, not just the ones
# a given test is about -- a leak introduced in one branch should fail the
# whole file, not only the test that happens to exercise that branch.
# ---------------------------------------------------------------------------
FORBIDDEN_SUBSTRINGS = [
    "[400]", "[401]", "[403]", "[404]", "[409]", "[422]", "[500]", "[502]",
    "preview_id",
    "Idempotency-Key",
    "idempotency_key",
    "session_id",
    "clinic_id",
    "uvicorn",
    "backend.main",
    "endpoint",
    "http://",
    "https://",
    "Gemini",
    "Sarvam",
    "Traceback",
    "Exception",
]

# The raw backend `detail` strings this UI can actually receive, taken
# verbatim from the code that raises them. Kept here as literals on
# purpose: if a backend message is reworded, a test here should fail and
# force a deliberate decision, rather than silently tracking the change.
DETAIL_UNKNOWN_HERB = "Herb 'Aswagandaa' is not in inventory."
DETAIL_PREVIEW_MISSING = "Preview not found or expired. Please re-run /prescribe/preview."
DETAIL_ALREADY_CONFIRMED = "Preview 'a3f9c1e28b4d5f60' has already been confirmed."
DETAIL_INSUFFICIENT_STOCK = "Insufficient stock for herb 'Brahmi'. Requested: 400g."
DETAIL_IDEMPOTENCY_CONFLICT = (
    "Idempotency key 'b1e4c7a2-9f33-4d18-8c55-2a7e6d90f412' was already used "
    "with a different request."
)
DETAIL_IDEMPOTENCY_CLINIC_MISMATCH = (
    "Idempotency key 'b1e4c7a2-9f33-4d18-8c55-2a7e6d90f412' is no longer valid "
    "for the current authorization context."
)
DETAIL_INVALID_AI_SCHEMA = "Invalid AI schema returned"
DETAIL_SESSION_INVALID = "Invalid, expired, or revoked session."
DETAIL_WRONG_ROLE = "Your role is not permitted to perform this action."
DETAIL_BAD_LOGIN = "Invalid username or password."
DETAIL_SERVER = "Transaction failed due to an internal error."
DETAIL_SESSION_MISMATCH = "This preview belongs to a different session."


def assert_clinician_safe(message):
    """Every assertion in this file funnels through here."""
    assert isinstance(message, str) and message.strip(), f"empty message: {message!r}"
    for banned in FORBIDDEN_SUBSTRINGS:
        assert banned.lower() not in message.lower(), (
            f"clinician-facing message leaked {banned!r}: {message!r}"
        )


# ---------------------------------------------------------------------------
# 1-10: the required status-code cases, through the pure helper.
# ---------------------------------------------------------------------------

def test_401_is_a_session_message():
    category, message = ui_app._friendly_error(401, DETAIL_SESSION_INVALID, None)
    assert category == ui_app.ERROR_SESSION
    assert message == ui_app.MSG_SESSION
    assert_clinician_safe(message)


def test_403_is_a_wrong_role_message():
    category, message = ui_app._friendly_error(403, DETAIL_WRONG_ROLE, None)
    assert category == ui_app.ERROR_WRONG_ROLE
    assert message == ui_app.MSG_WRONG_ROLE
    assert_clinician_safe(message)


def test_400_unknown_herb_does_not_echo_the_spelling():
    category, message = ui_app._friendly_error(400, DETAIL_UNKNOWN_HERB, None)
    assert category == ui_app.ERROR_UNKNOWN_HERB
    assert message == ui_app.MSG_UNKNOWN_HERB
    # The misspelling the doctor typed must not be read back at them, and
    # neither must the backend's misleading "not in inventory" wording --
    # the herb was not recognised, which is a different problem from being
    # out of stock.
    assert "Aswagandaa" not in message
    assert "inventory" not in message.lower()
    assert_clinician_safe(message)


def test_502_ai_unavailable_uses_the_structured_error_code():
    category, message = ui_app._friendly_error(502, "AI parsing failed", "AI_SERVICE_UNAVAILABLE")
    assert category == ui_app.ERROR_AI_UNAVAILABLE
    assert message == ui_app.MSG_AI_UNAVAILABLE
    # The fallback must be offered in the words, not just in the UI state.
    assert "yourself" in message
    assert_clinician_safe(message)


def test_422_invalid_ai_schema():
    category, message = ui_app._friendly_error(422, DETAIL_INVALID_AI_SCHEMA, None)
    assert category == ui_app.ERROR_AI_INVALID_RESPONSE
    assert message == ui_app.MSG_AI_INVALID_RESPONSE
    assert_clinician_safe(message)


def test_422_from_request_validation_is_not_blamed_on_the_assistant():
    """A 422 also comes from FastAPI's own validation (an oversized
    Idempotency-Key, a herb name over 200 characters). Telling a doctor
    "the assistant's reply couldn't be read" in that case would send them
    looking in the wrong place."""
    category, message = ui_app._friendly_error(
        422, "Input should be a valid integer", None,
    )
    assert category == ui_app.ERROR_VALIDATION
    assert message != ui_app.MSG_AI_INVALID_RESPONSE
    assert_clinician_safe(message)


def test_404_expired_preview_does_not_expose_the_preview_id():
    category, message = ui_app._friendly_error(404, DETAIL_PREVIEW_MISSING, None)
    assert category == ui_app.ERROR_PREVIEW_EXPIRED
    assert message == ui_app.MSG_PREVIEW_EXPIRED
    assert "/prescribe/preview" not in message
    assert_clinician_safe(message)


def test_409_insufficient_stock():
    category, message = ui_app._friendly_error(409, DETAIL_INSUFFICIENT_STOCK, None)
    assert category == ui_app.ERROR_INSUFFICIENT_STOCK
    assert message == ui_app.MSG_INSUFFICIENT_STOCK
    assert_clinician_safe(message)


def test_409_already_confirmed_does_not_expose_the_preview_id():
    category, message = ui_app._friendly_error(409, DETAIL_ALREADY_CONFIRMED, None)
    assert category == ui_app.ERROR_ALREADY_CONFIRMED
    assert message == ui_app.MSG_ALREADY_CONFIRMED
    assert "a3f9c1e28b4d5f60" not in message
    assert_clinician_safe(message)


def test_409_idempotency_conflict_does_not_expose_the_key():
    for detail in (DETAIL_IDEMPOTENCY_CONFLICT, DETAIL_IDEMPOTENCY_CLINIC_MISMATCH):
        category, message = ui_app._friendly_error(409, detail, None)
        assert category == ui_app.ERROR_IDEMPOTENCY_CONFLICT
        assert message == ui_app.MSG_IDEMPOTENCY_CONFLICT
        assert "b1e4c7a2" not in message
        assert_clinician_safe(message)


def test_the_three_409_branches_are_actually_distinguished():
    """The whole point of the detail matching in _friendly_error: these
    three unrelated failures share a status code and must not share a
    message. When BACKEND DEPENDENCY B1 lands and gives them structured
    X-Error-Code values, this test should keep passing unchanged."""
    messages = {
        ui_app._friendly_error(409, DETAIL_INSUFFICIENT_STOCK, None)[1],
        ui_app._friendly_error(409, DETAIL_ALREADY_CONFIRMED, None)[1],
        ui_app._friendly_error(409, DETAIL_IDEMPOTENCY_CONFLICT, None)[1],
    }
    assert len(messages) == 3, f"409 branches collapsed into: {messages}"


def test_unrecognised_409_falls_back_safely_rather_than_guessing():
    category, message = ui_app._friendly_error(409, "Some new conflict we've never seen", None)
    assert category in (
        ui_app.ERROR_IDEMPOTENCY_CONFLICT,
        ui_app.ERROR_UNKNOWN,
    )
    assert "Some new conflict" not in message
    assert_clinician_safe(message)


def test_generic_500():
    category, message = ui_app._friendly_error(500, DETAIL_SERVER, None)
    assert category == ui_app.ERROR_SERVER
    assert message == ui_app.MSG_SERVER
    assert "Transaction" not in message
    assert_clinician_safe(message)


def test_403_session_mismatch_still_reads_as_an_account_problem():
    """/prescribe/confirm returns 403 when a preview belongs to a different
    session. The doctor cannot act on the distinction, so it gets the same
    message as any other 403 -- and must not leak the wording."""
    _, message = ui_app._friendly_error(403, DETAIL_SESSION_MISMATCH, None)
    assert "preview" not in message.lower()
    assert_clinician_safe(message)


# ---------------------------------------------------------------------------
# 11-12: network-level outcomes, through the operation-aware wrapper.
# ---------------------------------------------------------------------------

def test_connection_error_is_translated_and_hides_the_host():
    raw = ui_app._CONNECTION_ERROR_MESSAGE.format(api_base=ui_app.API_BASE)
    # Guard: the raw string really does contain what we claim to be hiding.
    assert "uvicorn" in raw and "http" in raw

    category, message = ui_app._translate_call_error(raw, operation="preview")
    assert category == ui_app.ERROR_CONNECTION
    assert message == ui_app.MSG_CONNECTION
    assert_clinician_safe(message)


def test_confirm_timeout_remains_an_uncertain_outcome():
    """The single most safety-critical message in the application. A
    /prescribe/confirm timeout does NOT mean the prescription failed -- the
    backend may have committed it after this client stopped waiting. UX-1
    preserves this wording verbatim rather than collapsing it into an
    ordinary error."""
    raw = ui_app._CONFIRM_TIMEOUT_MESSAGE
    category, message = ui_app._translate_call_error(raw, operation="confirm")

    assert category == ui_app.ERROR_TIMEOUT_UNCERTAIN
    assert message == ui_app._CONFIRM_TIMEOUT_MESSAGE, "confirm timeout copy was altered"

    # It must not have been turned into a plain failure.
    assert message != ui_app.MSG_SERVER
    assert message != ui_app.MSG_CONNECTION
    # And it must still say the three things that make a retry safe.
    assert "Do not assume it failed" in message
    assert "safe" in message
    assert "second prescription" in message
    assert_clinician_safe(message)


def test_preview_timeout_keeps_the_write_it_yourself_offer():
    raw = ui_app._PREVIEW_TIMEOUT_MESSAGE.format(api_base=ui_app.API_BASE)
    assert "http" in raw  # guard: the raw string leaks a URL

    category, message = ui_app._translate_call_error(
        raw, operation="preview", ai_unavailable=True,
    )
    assert category == ui_app.ERROR_TIMEOUT_RETRYABLE
    assert "Nothing was saved" in message
    assert "yourself" in message
    assert_clinician_safe(message)


def test_manual_preview_timeout_is_a_plain_retry():
    raw = ui_app._REQUEST_TIMEOUT_MESSAGE.format(api_base=ui_app.API_BASE)
    category, message = ui_app._translate_call_error(raw, operation="manual_preview")
    assert category == ui_app.ERROR_TIMEOUT_RETRYABLE
    assert message == ui_app.MSG_REQUEST_TIMEOUT
    assert_clinician_safe(message)


def test_unexpected_request_exception_never_shows_the_raw_exception():
    raw = ui_app._UNEXPECTED_ERROR_MESSAGE.format(
        api_base=ui_app.API_BASE,
        error="SSLError(1, '[SSL] record layer failure')",
    )
    _, message = ui_app._translate_call_error(raw, operation="confirm")
    assert "SSLError" not in message
    assert_clinician_safe(message)


# ---------------------------------------------------------------------------
# The raw -> friendly seam.
# ---------------------------------------------------------------------------

def test_split_call_error_parses_the_modules_own_format():
    assert ui_app._split_call_error("[409] Preview 'x' has already been confirmed.") == (
        409, "Preview 'x' has already been confirmed.",
    )
    assert ui_app._split_call_error("[401] Invalid username or password.") == (
        401, "Invalid username or password.",
    )


def test_split_call_error_passes_non_http_text_through_untouched():
    for raw in ("Can't reach the backend at http://x. Is `uvicorn` running?", "", None, "[oops] x"):
        status_code, _ = ui_app._split_call_error(raw)
        assert status_code is None


def test_a_wrong_password_reads_as_a_wrong_password_not_an_expired_session():
    """call_login's 401 is the one 401 in this UI that is not a lapsed
    session -- no session existed yet."""
    raw = f"[401] {DETAIL_BAD_LOGIN}"
    category, message = ui_app._translate_call_error(raw, operation="login")
    assert category == ui_app.ERROR_SIGN_IN_FAILED
    assert message == ui_app.MSG_SIGN_IN_FAILED
    assert message != ui_app.MSG_SESSION
    assert_clinician_safe(message)


def test_the_same_401_elsewhere_still_reads_as_an_expired_session():
    raw = f"[401] {DETAIL_SESSION_INVALID}"
    _, message = ui_app._translate_call_error(raw, operation="confirm")
    assert message == ui_app.MSG_SESSION
    assert_clinician_safe(message)


def test_a_wrong_password_message_does_not_distinguish_the_three_reasons():
    """The backend deliberately returns one generic 401 for a wrong
    username, a wrong password and a deactivated account alike. The
    translation must not accidentally narrow that."""
    message = ui_app._friendly_message(f"[401] {DETAIL_BAD_LOGIN}", operation="login")
    for leaky in ("username doesn't exist", "no such user", "deactivated", "inactive", "locked"):
        assert leaky not in message.lower()


# ---------------------------------------------------------------------------
# Sweep: every message the module can emit, checked in one place.
# ---------------------------------------------------------------------------

def test_every_public_message_constant_is_clinician_safe():
    names = [n for n in dir(ui_app) if n.startswith("MSG_")]
    assert len(names) >= 12, f"expected the full MSG_* set, found {names}"
    for name in names:
        assert_clinician_safe(getattr(ui_app, name))


def test_no_status_code_in_the_matrix_ever_produces_a_leaky_message():
    """Brute force over every status this API can return, crossed with
    every real detail string, plus a few the backend has never sent."""
    details = [
        DETAIL_UNKNOWN_HERB, DETAIL_PREVIEW_MISSING, DETAIL_ALREADY_CONFIRMED,
        DETAIL_INSUFFICIENT_STOCK, DETAIL_IDEMPOTENCY_CONFLICT,
        DETAIL_IDEMPOTENCY_CLINIC_MISMATCH, DETAIL_INVALID_AI_SCHEMA,
        DETAIL_SESSION_INVALID, DETAIL_WRONG_ROLE, DETAIL_BAD_LOGIN,
        DETAIL_SERVER, DETAIL_SESSION_MISMATCH,
        "", None,
        "Preview 'deadbeef' belongs to clinic_id 'other_clinic'",
        "Traceback (most recent call last): File 'backend/main.py', line 1",
        "http://127.0.0.1:8000/prescribe/confirm failed",
    ]
    statuses = [400, 401, 403, 404, 409, 413, 422, 429, 500, 502, 503, 504, 418]
    for status in statuses:
        for detail in details:
            _, message = ui_app._friendly_error(status, detail, None)
            assert_clinician_safe(message)


def test_the_ai_unavailable_code_wins_regardless_of_status():
    for status in (500, 502, 503, 504):
        category, message = ui_app._friendly_error(status, "anything at all", "AI_SERVICE_UNAVAILABLE")
        assert category == ui_app.ERROR_AI_UNAVAILABLE
        assert message == ui_app.MSG_AI_UNAVAILABLE


def test_friendly_error_is_pure():
    """Same inputs, same outputs, no reliance on Streamlit state or the
    network. Called twice with identical arguments and compared."""
    first = ui_app._friendly_error(409, DETAIL_INSUFFICIENT_STOCK, None)
    second = ui_app._friendly_error(409, DETAIL_INSUFFICIENT_STOCK, None)
    assert first == second

    import inspect
    source = inspect.getsource(ui_app._friendly_error)
    for forbidden in ("st.", "requests.", "session_state"):
        assert forbidden not in source, f"_friendly_error is not pure: found {forbidden!r}"


def test_networking_functions_were_left_intact():
    """UX-1 must not have changed the locked networking layer."""
    for name in (
        "call_login", "call_logout", "call_preview", "call_manual_preview",
        "call_confirm", "get_idempotency_key_for", "_protected_headers",
        "_raise_if_session_expired", "SessionExpiredError", "API_BASE",
    ):
        assert hasattr(ui_app, name), f"{name} is missing from ui/app.py"

    import inspect
    # Arity and parameter names, exactly as tests/test_ui_auth_integration.py
    # calls them.
    assert list(inspect.signature(ui_app.call_preview).parameters) == [
        "patient_name", "patient_phone", "patient_id", "notes", "token",
    ]
    assert list(inspect.signature(ui_app.call_confirm).parameters) == [
        "preview_id", "dosha_imbalance", "prescribed_herbs", "token", "idempotency_key",
    ]
    assert list(inspect.signature(ui_app.call_login).parameters) == ["username", "password"]
    assert list(inspect.signature(ui_app.call_logout).parameters) == ["token"]
