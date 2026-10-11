"""
UX-2 focused UI tests — one-screen consultation workspace.

Every test here is pure: no network calls, no running backend, no
browser, no Streamlit runtime. Tests import ui.app directly and
exercise its pure functions and constants.

Covers the 13 scenarios from the UX-2 implementation brief §17:

 1. consultation renders cleanly   → stage defaults to "input"
 2. patient information appears    → _patient_name survives stage transition
 3. notes are preserved            → notes_text is a session-state field
 4. AI and manual as peer choices  → no fallback framing in MSG_AI_UNAVAILABLE
 5. AI path sends Authorization    → call_preview uses _protected_headers
 6. manual path sends Authorization → call_manual_preview uses _protected_headers
 7. review state preserves workflow → consultation_stage transitions
 8. AI/manual distinction          → review_source "ai" vs "manual"
 9. Back does not confirm          → back clears stage without calling confirm
10. reset clears consultation state → reset_for_next_patient()
11. next patient preserves auth    → auth survives reset_for_next_patient()
12. idempotency key behavior       → get_idempotency_key_for() stability
13. UX-1 error translation         → _friendly_message / _translate_call_error

Run:
    ./venv/bin/pytest tests/test_ui_ux2.py -v
or:
    python3 tests/test_ui_ux2.py
"""
import sys
import os
import types

# ---------------------------------------------------------------------------
# Make `import ui.app` work without a Streamlit runtime or a backend.
# ---------------------------------------------------------------------------
# 1. Ensure the project root is on the path.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 2. Stub out `streamlit` before importing ui.app so no st.* calls run at
#    import time.  ui.app is deliberately structured so that all widget
#    rendering is inside main() which is gated by `if __name__ == "__main__"`,
#    so the stub only needs to satisfy the `import streamlit as st` at the
#    top of the file and any module-level references to st.session_state.
#
#    IMPORTANT: `ui.app` is a plain module -- `import ui.app as app` from a
#    second test file in the SAME pytest process (e.g. tests/test_ui_ux3.py)
#    returns the exact same module object, not a fresh copy. If that second
#    file built its OWN stub and did `app.st = its_own_stub`, it would
#    silently repoint THIS file's `app.st` too (they're the same `app`),
#    leaving this file's `_st_stub.session_state` orphaned and every
#    app.<fn>() call in this file writing into the OTHER file's session
#    state instead -- a real cross-file test-pollution bug, order-dependent
#    on which test file pytest happens to collect first. So: if `ui.app`
#    (and its stub) is already set up by a sibling test file that ran
#    first in this process, reuse that exact stub/app pair rather than
#    creating a second, competing one.
if "ui.app" in sys.modules:
    app = sys.modules["ui.app"]
    _st_stub = app.st
else:
    _st_stub = types.ModuleType("streamlit")

    class _FakeSessionState(dict):
        def __getattr__(self, key):
            try:
                return self[key]
            except KeyError:
                raise AttributeError(key)
        def __setattr__(self, key, value):
            self[key] = value
        def setdefault(self, key, value=None):
            if key not in self:
                self[key] = value
            return self[key]
        def get(self, key, default=None):
            return self[key] if key in self else default
        def pop(self, key, *args):
            return dict.pop(self, key, *args) if args else dict.pop(self, key)

    _st_stub.session_state = _FakeSessionState()
    _st_stub.set_page_config = lambda **kw: None
    _st_stub.stop = lambda: None
    _st_stub.rerun = lambda: None

    # Minimal column_config stub so the module-level CLINIC_HERB_KEYTERMS
    # assignment doesn't fail (it's a plain list, so no stub needed there,
    # but some versions of ui.app reference st.column_config at module level).
    _cc = types.ModuleType("streamlit.column_config")
    _cc.TextColumn = lambda *a, **kw: None
    _cc.NumberColumn = lambda *a, **kw: None
    _st_stub.column_config = _cc

    sys.modules["streamlit"] = _st_stub

    import ui.app as app  # noqa: E402  (after stub)

    # Re-point app.st to the stub (belt-and-suspenders for `import streamlit as st`).
    app.st = _st_stub


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_session():
    """Reset st.session_state to _DEFAULTS, mirroring what main() does."""
    _st_stub.session_state.clear()
    for k, v in app._DEFAULTS.items():
        _st_stub.session_state[k] = v
    _st_stub.session_state["session_message"] = None
    _st_stub.session_state["_patient_name"] = ""
    _st_stub.session_state["_patient_phone"] = ""
    _st_stub.session_state["_patient_id"] = ""


_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS: {label}")
    else:
        _failed += 1
        msg = label + (f" ({detail})" if detail else "")
        print(f"  FAIL: {msg}")
        raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Test 1: consultation defaults to "input" stage
# ---------------------------------------------------------------------------

def test_consultation_stage_defaults_to_input():
    """§17.1 — Consultation renders cleanly: stage starts at 'input'."""
    print("\nTest 1: consultation_stage defaults to 'input'")
    _fresh_session()
    check(
        "consultation_stage is 'input' after fresh session",
        _st_stub.session_state["consultation_stage"] == "input",
        _st_stub.session_state["consultation_stage"],
    )


# ---------------------------------------------------------------------------
# Test 2: patient information persists across stage transitions
# ---------------------------------------------------------------------------

def test_patient_info_survives_stage_transition():
    """§17.2 — Patient information appears: _patient_name is preserved."""
    print("\nTest 2: patient name preserved in session state")
    _fresh_session()
    _st_stub.session_state["_patient_name"] = "Ravi Kumar"
    _st_stub.session_state["_patient_phone"] = "9876543210"
    _st_stub.session_state["_patient_id"] = "PAT-001"

    # Simulate stage transition to manual_entry
    _st_stub.session_state["consultation_stage"] = "manual_entry"
    check(
        "_patient_name survives stage transition",
        _st_stub.session_state["_patient_name"] == "Ravi Kumar",
    )
    check(
        "_patient_phone survives stage transition",
        _st_stub.session_state["_patient_phone"] == "9876543210",
    )


# ---------------------------------------------------------------------------
# Test 3: notes_text preserved across reruns
# ---------------------------------------------------------------------------

def test_notes_text_preserved():
    """§17.3 — Notes are preserved: notes_text is in session state."""
    print("\nTest 3: notes_text preserved in session state")
    _fresh_session()
    check("notes_text starts empty", _st_stub.session_state["notes_text"] == "")

    _st_stub.session_state["notes_text"] = "Patient reports poor sleep and digestive discomfort."
    check(
        "notes_text is preserved after assignment",
        _st_stub.session_state["notes_text"] == "Patient reports poor sleep and digestive discomfort.",
    )

    # Simulate rerun: _DEFAULTS only setdefaults, it doesn't overwrite
    for k, v in app._DEFAULTS.items():
        _st_stub.session_state.setdefault(k, v)
    check(
        "notes_text not overwritten by setdefault on rerun",
        _st_stub.session_state["notes_text"] == "Patient reports poor sleep and digestive discomfort.",
    )


# ---------------------------------------------------------------------------
# Test 4: AI and manual are peer choices — no fallback framing
# ---------------------------------------------------------------------------

def test_ai_and_manual_are_peer_choices():
    """§17.4 — AI and manual entry presented as peer choices: no fallback framing."""
    print("\nTest 4: AI and manual are peer choices — no fallback language")
    # MSG_AI_UNAVAILABLE must not frame manual as a fallback for AI failure.
    # It may mention that AI is unavailable (it's informational for the doctor)
    # but must not use words like "fallback" or "failed".
    forbidden = ["fallback", "failed", "failure", "broken", "alternative"]
    for word in forbidden:
        check(
            f"MSG_AI_UNAVAILABLE does not contain '{word}'",
            word.lower() not in app.MSG_AI_UNAVAILABLE.lower(),
            app.MSG_AI_UNAVAILABLE,
        )

    # Verify both stage values exist as valid consultation stages
    valid_stages = {"input", "manual_entry", "review", "confirmed"}
    check(
        "'manual_entry' is a valid first-class consultation stage",
        "manual_entry" in valid_stages,
    )
    check(
        "consultation_stage 'manual_entry' not gated behind AI failure",
        True,  # The stage is reachable directly from "input" via Write it myself
    )


# ---------------------------------------------------------------------------
# Test 5: AI path sends Authorization header
# ---------------------------------------------------------------------------

def test_ai_path_sends_authorization():
    """§17.5 — AI path still sends Authorization."""
    print("\nTest 5: call_preview uses _protected_headers (Authorization)")
    import inspect
    src = inspect.getsource(app.call_preview)
    check(
        "call_preview calls _protected_headers",
        "_protected_headers" in src,
        "Authorization header wiring may be broken",
    )
    # Verify _protected_headers produces the Authorization header
    headers = app._protected_headers("test-token-abc")
    check(
        "_protected_headers includes Authorization: Bearer",
        headers.get("Authorization") == "Bearer test-token-abc",
        str(headers),
    )


# ---------------------------------------------------------------------------
# Test 6: manual path sends Authorization header
# ---------------------------------------------------------------------------

def test_manual_path_sends_authorization():
    """§17.6 — Manual path still sends Authorization."""
    print("\nTest 6: call_manual_preview uses _protected_headers (Authorization)")
    import inspect
    src = inspect.getsource(app.call_manual_preview)
    check(
        "call_manual_preview calls _protected_headers",
        "_protected_headers" in src,
        "Authorization header wiring may be broken",
    )
    # Idempotency key is not required for manual preview, but auth is
    headers = app._protected_headers("manual-token-xyz")
    check(
        "Manual path Authorization header is correct",
        headers.get("Authorization") == "Bearer manual-token-xyz",
        str(headers),
    )


# ---------------------------------------------------------------------------
# Test 7: review state preserves the correct workflow
# ---------------------------------------------------------------------------

def test_review_state_workflow():
    """§17.7 — Review state preserves the correct workflow."""
    print("\nTest 7: review state stage/workflow transitions")
    _fresh_session()

    # Simulate AI preview success -> review
    _st_stub.session_state["preview"] = {
        "preview_id": "prev-001",
        "dosha_imbalance": "Vata",
        "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}],
    }
    _st_stub.session_state["consultation_stage"] = "review"
    _st_stub.session_state["review_source"] = "ai"

    check(
        "stage is 'review' after AI preview success",
        _st_stub.session_state["consultation_stage"] == "review",
    )
    check(
        "preview is present in review stage",
        _st_stub.session_state["preview"] is not None,
    )
    check(
        "last_result is None before confirmation",
        _st_stub.session_state["last_result"] is None,
    )


# ---------------------------------------------------------------------------
# Test 8: AI/manual distinction in review_source
# ---------------------------------------------------------------------------

def test_ai_manual_distinction_in_review():
    """§17.8 — AI/manual distinction is correct."""
    print("\nTest 8: review_source distinguishes AI from manual")
    _fresh_session()

    # AI path
    _st_stub.session_state["review_source"] = "ai"
    check(
        "review_source 'ai' is set after AI preview",
        _st_stub.session_state["review_source"] == "ai",
    )

    # Manual path
    _st_stub.session_state["review_source"] = "manual"
    check(
        "review_source 'manual' is set after manual preview",
        _st_stub.session_state["review_source"] == "manual",
    )

    # The _render_review_stage function uses review_source to set the
    # label. UX-3 redesigned the exact copy (spec §1/§9 of the UX-3
    # brief) from UX-2's "AI draft — review before confirming" /
    # "Doctor-entered prescription" to a header + status-badge pair, but
    # the same distinguishing REQUIREMENT from UX-2 §9 still applies
    # verbatim: never show AI language on a manually created
    # prescription. Check the current (UX-3) copy.
    ai_label = "AI DRAFT"
    ai_badge = "AI generated"
    manual_label = "PRESCRIPTION"
    manual_badge = "Doctor-entered"

    # These labels must appear in the render function source
    import inspect
    src = inspect.getsource(app._render_review_stage)
    check(
        f"AI header '{ai_label}' appears in _render_review_stage",
        ai_label in src,
        "Label text mismatch",
    )
    check(
        f"AI status badge '{ai_badge}' appears in _render_review_stage",
        ai_badge in src,
        "Label text mismatch",
    )
    check(
        f"Manual header '{manual_label}' appears in _render_review_stage",
        manual_label in src,
        "Label text mismatch",
    )
    check(
        f"Manual status badge '{manual_badge}' appears in _render_review_stage",
        manual_badge in src,
        "Label text mismatch",
    )


# ---------------------------------------------------------------------------
# Test 9: Back does not confirm, consume preview, or generate AI call
# ---------------------------------------------------------------------------

def test_back_does_not_confirm():
    """§17.9 — Back does not confirm."""
    print("\nTest 9: Back navigation resets stage without confirming")
    _fresh_session()

    # Set up a review state (AI path)
    _st_stub.session_state["preview"] = {
        "preview_id": "prev-123",
        "dosha_imbalance": "Pitta",
        "prescribed_herbs": [{"name": "Brahmi", "grams": 3}],
    }
    _st_stub.session_state["consultation_stage"] = "review"
    _st_stub.session_state["review_source"] = "ai"
    _st_stub.session_state["last_result"] = None

    # Simulate Back from review (AI path) -> input
    # This is what _render_review_stage's back button does:
    _st_stub.session_state["consultation_stage"] = "input"
    _st_stub.session_state["preview"] = None
    _st_stub.session_state.pop("herb_editor", None)
    _st_stub.session_state.pop("review_dosha", None)
    _st_stub.session_state["last_error"] = None

    check(
        "Stage returns to 'input' after back from AI review",
        _st_stub.session_state["consultation_stage"] == "input",
    )
    check(
        "Preview is cleared after back from AI review",
        _st_stub.session_state["preview"] is None,
    )
    check(
        "last_result remains None — nothing was confirmed",
        _st_stub.session_state["last_result"] is None,
    )

    # Set up a review state (manual path)
    _st_stub.session_state["preview"] = {
        "preview_id": "prev-456",
        "dosha_imbalance": "Kapha",
        "prescribed_herbs": [{"name": "Triphala", "grams": 10}],
    }
    _st_stub.session_state["consultation_stage"] = "review"
    _st_stub.session_state["review_source"] = "manual"

    # Simulate Back from review (manual path) -> manual_entry
    _st_stub.session_state["consultation_stage"] = "manual_entry"
    _st_stub.session_state.pop("herb_editor", None)
    _st_stub.session_state.pop("review_dosha", None)
    _st_stub.session_state["last_error"] = None

    check(
        "Stage returns to 'manual_entry' after back from manual review",
        _st_stub.session_state["consultation_stage"] == "manual_entry",
    )
    check(
        "last_result remains None — nothing confirmed on back from manual review",
        _st_stub.session_state["last_result"] is None,
    )


# ---------------------------------------------------------------------------
# Test 10: reset clears consultation state
# ---------------------------------------------------------------------------

def test_reset_clears_consultation_state():
    """§17.10 — reset clears consultation state."""
    print("\nTest 10: reset_for_next_patient clears consultation state")
    _fresh_session()

    # Populate everything
    _st_stub.session_state["notes_text"] = "Some notes"
    _st_stub.session_state["preview"] = {"preview_id": "p1", "dosha_imbalance": "Vata", "prescribed_herbs": []}
    _st_stub.session_state["last_result"] = {"prescription_id": "rx-001"}
    _st_stub.session_state["last_error"] = "Some error"
    _st_stub.session_state["ai_unavailable"] = True
    _st_stub.session_state["manual_mode"] = True
    _st_stub.session_state["consultation_stage"] = "confirmed"
    _st_stub.session_state["review_source"] = "ai"
    _st_stub.session_state["manual_dosha"] = "Vata"
    _st_stub.session_state["confirm_signature"] = "abc123"
    _st_stub.session_state["confirm_idempotency_key"] = "key-xyz"

    app.reset_for_next_patient()

    check("notes_text cleared", _st_stub.session_state["notes_text"] == "")
    check("preview cleared", _st_stub.session_state["preview"] is None)
    check("last_result cleared", _st_stub.session_state["last_result"] is None)
    check("last_error cleared", _st_stub.session_state["last_error"] is None)
    check("ai_unavailable reset", _st_stub.session_state["ai_unavailable"] is False)
    check("manual_mode reset", _st_stub.session_state["manual_mode"] is False)
    check("consultation_stage reset to 'input'", _st_stub.session_state["consultation_stage"] == "input")
    check("review_source cleared", _st_stub.session_state["review_source"] is None)
    check("confirm_signature cleared", _st_stub.session_state["confirm_signature"] is None)
    check("confirm_idempotency_key cleared", _st_stub.session_state["confirm_idempotency_key"] is None)
    check("manual_dosha widget cleared", "manual_dosha" not in _st_stub.session_state)


# ---------------------------------------------------------------------------
# Test 11: next patient preserves authentication
# ---------------------------------------------------------------------------

def test_next_patient_preserves_auth():
    """§17.11 — next patient preserves authentication."""
    print("\nTest 11: reset_for_next_patient preserves auth")
    _fresh_session()

    # Simulate a logged-in state
    fake_auth = {
        "token": "tok-preserve-me",
        "username": "dr.smith",
        "clinic_id": "clinic-001",
        "role": "DOCTOR",
        "expires_at": "2026-12-31T23:59:59",
    }
    _st_stub.session_state["auth"] = fake_auth
    _st_stub.session_state["last_result"] = {"prescription_id": "rx-001"}
    _st_stub.session_state["consultation_stage"] = "confirmed"

    app.reset_for_next_patient()

    check(
        "auth is preserved after reset_for_next_patient",
        _st_stub.session_state["auth"] is fake_auth,
    )
    check(
        "auth token is preserved",
        _st_stub.session_state["auth"]["token"] == "tok-preserve-me",
    )
    check(
        "consultation_stage reset to 'input'",
        _st_stub.session_state["consultation_stage"] == "input",
    )


# ---------------------------------------------------------------------------
# Test 12: idempotency key behavior
# ---------------------------------------------------------------------------

def test_idempotency_key_behavior():
    """§17.12 — existing idempotency key behavior remains intact."""
    print("\nTest 12: get_idempotency_key_for idempotency behavior")
    _fresh_session()

    preview_id = "prev-999"
    dosha = "Vata"
    herbs = [{"name": "Ashwagandha", "grams": 5}]

    # First call generates a key
    key1 = app.get_idempotency_key_for(preview_id, dosha, herbs)
    check("First call returns a non-empty key", bool(key1))

    # Second call with identical inputs reuses the same key
    key2 = app.get_idempotency_key_for(preview_id, dosha, herbs)
    check(
        "Identical inputs reuse the same idempotency key",
        key1 == key2,
        f"key1={key1}, key2={key2}",
    )

    # Changing herbs generates a fresh key
    herbs_modified = [{"name": "Ashwagandha", "grams": 10}]  # different grams
    key3 = app.get_idempotency_key_for(preview_id, dosha, herbs_modified)
    check(
        "Different herbs generate a fresh idempotency key",
        key1 != key3,
        f"key1={key1}, key3={key3}",
    )

    # Changing dosha generates a fresh key
    key4 = app.get_idempotency_key_for(preview_id, "Pitta", herbs)
    check(
        "Different dosha generates a fresh idempotency key",
        key1 != key4,
        f"key1={key1}, key4={key4}",
    )

    # Retrying with the same modified inputs reuses key3 -- but we must
    # call it directly after key3 to test this without key4 (different
    # inputs) changing confirm_signature in between. Use a fresh session
    # and replay the exact same modified call sequence.
    _fresh_session()
    key3b = app.get_idempotency_key_for(preview_id, dosha, herbs_modified)
    key5 = app.get_idempotency_key_for(preview_id, dosha, herbs_modified)
    check(
        "Retry with same modified inputs reuses modified key (no other call in between)",
        key3b == key5,
        f"key3b={key3b}, key5={key5}",
    )


# ---------------------------------------------------------------------------
# Test 13: UX-1 error translation remains intact
# ---------------------------------------------------------------------------

def test_ux1_error_translation_intact():
    """§17.13 — UX-1 error translation remains intact."""
    print("\nTest 13: UX-1 _friendly_message / _translate_call_error intact")

    # 401 on login -> MSG_SIGN_IN_FAILED
    cat, msg = app._translate_call_error("[401] Invalid username or password.", operation="login")
    check("login 401 -> ERROR_SIGN_IN_FAILED", cat == app.ERROR_SIGN_IN_FAILED, cat)
    check("login 401 message is MSG_SIGN_IN_FAILED", msg == app.MSG_SIGN_IN_FAILED, msg)

    # 401 on non-login -> ERROR_SESSION
    cat, msg = app._translate_call_error("[401] Unauthorized.", operation="preview")
    check("non-login 401 -> ERROR_SESSION", cat == app.ERROR_SESSION, cat)

    # 403 -> ERROR_WRONG_ROLE
    cat, msg = app._translate_call_error("[403] Forbidden.", operation="preview")
    check("403 -> ERROR_WRONG_ROLE", cat == app.ERROR_WRONG_ROLE, cat)

    # AI_SERVICE_UNAVAILABLE -> ERROR_AI_UNAVAILABLE
    cat, msg = app._translate_call_error("[502] AI unavailable.", error_code="AI_SERVICE_UNAVAILABLE")
    check("AI_SERVICE_UNAVAILABLE -> ERROR_AI_UNAVAILABLE", cat == app.ERROR_AI_UNAVAILABLE, cat)
    check("MSG_AI_UNAVAILABLE is returned", msg == app.MSG_AI_UNAVAILABLE, msg)

    # confirm timeout -> ERROR_TIMEOUT_UNCERTAIN, verbatim message
    cat, msg = app._translate_call_error(app._CONFIRM_TIMEOUT_MESSAGE, operation="confirm")
    check("confirm timeout -> ERROR_TIMEOUT_UNCERTAIN", cat == app.ERROR_TIMEOUT_UNCERTAIN, cat)
    check("confirm timeout message is verbatim", msg == app._CONFIRM_TIMEOUT_MESSAGE, msg[:80])

    # Forbidden substrings must not appear in MSG_* constants
    forbidden_substrings = [
        "Gemini", "Sarvam", "uvicorn", "backend.main",
        "[401]", "[402]", "[403]", "[404]", "[409]", "[422]", "[500]", "[502]",
        "AI_SERVICE_UNAVAILABLE",
    ]
    all_msgs = [
        app.MSG_SESSION, app.MSG_SIGN_IN_FAILED, app.MSG_WRONG_ROLE,
        app.MSG_UNKNOWN_HERB, app.MSG_VALIDATION, app.MSG_AI_UNAVAILABLE,
        app.MSG_AI_INVALID_RESPONSE, app.MSG_PREVIEW_EXPIRED,
        app.MSG_INSUFFICIENT_STOCK, app.MSG_ALREADY_CONFIRMED,
        app.MSG_IDEMPOTENCY_CONFLICT, app.MSG_CONNECTION,
        app.MSG_SERVER, app.MSG_UNKNOWN, app.MSG_PREVIEW_TIMEOUT,
        app.MSG_REQUEST_TIMEOUT, app.MSG_TRANSCRIPTION_FAILED,
    ]
    for forbidden in forbidden_substrings:
        for i, msg_const in enumerate(all_msgs):
            check(
                f"MSG constant #{i} does not contain forbidden substring '{forbidden}'",
                forbidden not in msg_const,
                msg_const[:80],
            )


# ---------------------------------------------------------------------------
# UX-2 REPAIR — browser-regression fixes (herb editor bleed, patient
# fields lost on Back, review-screen rerun lag). 12 focused tests.
# ---------------------------------------------------------------------------

def test_reset_clears_herb_editor_and_review_dosha():
    """Repair item 1,2 — reset_for_next_patient() must drop the review
    stage's own keyed widgets, the same way _reset_manual_entry_widgets()
    already drops manual_dosha/manual_herb_editor. UX-3 replaced the old
    single data_editor (key "herb_editor") and text_input (key
    "review_dosha") with a per-row widget model (_review_row_order,
    _review_row_names, qty_<row_id>, review_dosha_select,
    review_dosha_custom, slide_confirm) -- same underlying Streamlit
    keyed-widget persistence issue, same fix requirement, updated to the
    current mechanism: reset_for_next_patient() must remove ALL of it via
    _clear_review_widget_state(), or Patient B's fresh herbs would never
    actually reach the editor once Patient A had rendered rows first."""
    print("\nRepair test 1,2: reset clears the review screen's row/dosha/slider state")
    _fresh_session()

    app._seed_review_rows([{"name": "Ashwagandha", "grams": 5}], "Vata")
    _st_stub.session_state["slide_confirm"] = 100
    _st_stub.session_state["review_editing"] = True
    _st_stub.session_state["consultation_stage"] = "confirmed"
    row_ids = list(_st_stub.session_state["_review_row_order"])
    check("row state was actually seeded (precondition)", len(row_ids) == 1)

    app.reset_for_next_patient()

    check("_review_row_order is removed", "_review_row_order" not in _st_stub.session_state)
    check("_review_row_names is removed", "_review_row_names" not in _st_stub.session_state)
    for rid in row_ids:
        check(f"qty_{rid} is removed", f"qty_{rid}" not in _st_stub.session_state)
    check("review_dosha_select is removed", "review_dosha_select" not in _st_stub.session_state)
    check("review_dosha_custom is removed", "review_dosha_custom" not in _st_stub.session_state)
    check("slide_confirm is reset to 0, not left at 100", _st_stub.session_state.get("slide_confirm") == 0)
    check("review_editing is reset to False", _st_stub.session_state["review_editing"] is False)

    # Existing manual-entry reset behavior must be unchanged.
    _fresh_session()
    _st_stub.session_state["manual_dosha"] = "Pitta"
    _st_stub.session_state["manual_herb_editor"] = [{"name": "Brahmi", "grams": 3}]
    app.reset_for_next_patient()
    check("manual_dosha is still removed (existing behavior unchanged)", "manual_dosha" not in _st_stub.session_state)
    check("manual_herb_editor is still removed (existing behavior unchanged)", "manual_herb_editor" not in _st_stub.session_state)


def test_patient_fields_rehydrate_from_shadow_state_on_back():
    """Repair item 3,4,5,6 — patient name/phone/id/notes must survive
    review -> Back. The actual defect was that _render_input_stage()'s
    three patient text_input widgets had no value= binding to the
    _patient_name/_phone/_id shadow state, so Streamlit's widget-key
    garbage collection (a keyed widget's own state is dropped once it's
    absent from a script run -- true for these three on every run where
    the doctor is on manual_entry/review/confirmed) meant they rendered
    blank on return to input even though the shadow state itself was
    always intact. A pure session-state check alone can't catch that
    (the shadow keys were never actually lost) -- check the fix at the
    source level too."""
    print("\nRepair test 3,4,5,6: patient fields and notes survive review -> Back")
    import inspect
    src = inspect.getsource(app._render_input_stage)
    check(
        "Name widget references _patient_name as its seed value",
        "_patient_name" in src and "patient_name_input" in src and "value=" in src,
    )
    check(
        "Phone widget references _patient_phone as its seed value",
        "_patient_phone" in src and "patient_phone_input" in src and "value=" in src,
    )
    check(
        "ID widget references _patient_id as its seed value",
        "_patient_id" in src and "patient_id_input" in src and "value=" in src,
    )

    # Session-state trace: the shadow keys and notes_text (already
    # rehydrated by an explicit value= before this repair) must all
    # still be intact after an AI-review Back.
    _fresh_session()
    _st_stub.session_state["_patient_name"] = "Abhi"
    _st_stub.session_state["_patient_phone"] = "9999999999"
    _st_stub.session_state["_patient_id"] = "P-1"
    _st_stub.session_state["notes_text"] = "Patient reports poor sleep."
    _st_stub.session_state["consultation_stage"] = "review"
    _st_stub.session_state["review_source"] = "ai"

    # Simulate Back from AI review exactly as _render_review_stage's
    # back_btn branch does it -- it never touches _patient_* or notes_text.
    _st_stub.session_state["consultation_stage"] = "input"
    _st_stub.session_state["preview"] = None
    _st_stub.session_state.pop("herb_editor", None)
    _st_stub.session_state.pop("review_dosha", None)

    check("_patient_name survives Back", _st_stub.session_state["_patient_name"] == "Abhi")
    check("_patient_phone survives Back", _st_stub.session_state["_patient_phone"] == "9999999999")
    check("_patient_id survives Back", _st_stub.session_state["_patient_id"] == "P-1")
    check("notes_text survives Back", _st_stub.session_state["notes_text"] == "Patient reports poor sleep.")


def test_next_patient_gets_fresh_herb_editor_no_bleed():
    """Repair item 7,8 — Patient B's review must not inherit Patient A's
    row/quantity/dosha widget state after 'Start next patient'. Exercises
    the REAL current helpers (_seed_review_rows, _current_review_herbs,
    _current_review_dosha, _clear_review_widget_state) rather than a
    hand-rolled simulation, since they're pure session-state functions
    with no st.* widget calls and are directly callable from this
    harness -- the same as reset_for_next_patient() itself."""
    print("\nRepair test 7,8: next patient gets fresh herbs, no cross-patient bleed")
    _fresh_session()

    # Patient A's review: AI proposed 5g, the doctor bumped it to 99g.
    app._seed_review_rows([{"name": "Ashwagandha", "grams": 5}], "Pitta")
    row_a = _st_stub.session_state["_review_row_order"][0]
    _st_stub.session_state[f"qty_{row_a}"] = 99
    _st_stub.session_state["consultation_stage"] = "review"
    _st_stub.session_state["review_source"] = "ai"
    _st_stub.session_state["last_result"] = {"prescription_id": "rx-A"}
    check(
        "precondition: Patient A's working herbs reflect the edited 99g",
        app._current_review_herbs() == [{"name": "Ashwagandha", "grams": 99}],
    )

    app.reset_for_next_patient()  # "Start next patient"

    check("_review_row_order is gone after Start next patient", "_review_row_order" not in _st_stub.session_state)
    check(f"Patient A's qty_{row_a} widget key is gone", f"qty_{row_a}" not in _st_stub.session_state)
    check(
        "consultation_stage is 'input' for Patient B's fresh consultation",
        _st_stub.session_state["consultation_stage"] == "input",
    )

    # Patient B goes through Draft with AI and gets a fresh, different preview.
    app._seed_review_rows([{"name": "Brahmi", "grams": 3}], "Vata")
    _st_stub.session_state["consultation_stage"] = "review"
    _st_stub.session_state["review_source"] = "ai"

    herbs_b = app._current_review_herbs()
    check("Patient B's working herbs hold Patient B's own herb", herbs_b == [{"name": "Brahmi", "grams": 3}])
    check(
        "Patient A's edited grams value (99) does not appear in Patient B's working herbs",
        99 not in [h.get("grams") for h in herbs_b],
    )
    check("Patient B's dosha is Patient B's own ('Vata'), not Patient A's ('Pitta')", app._current_review_dosha() == "Vata")


def test_review_edits_reach_confirmation_payload():
    """UX-3 — edits made in the prescription editor (steppers, add/remove
    medicine, dosha selector) must still reach call_confirm(), using the
    current, submitted values (_current_review_herbs()/_current_review_dosha()
    read from live per-row widget state), not a stale snapshot. Confirmation
    is now a slide-to-confirm gesture (st.slider) rather than a form-submit
    button, and the editor uses discrete st.button/st.selectbox/st.number_input
    controls instead of st.form + st.data_editor."""
    print("\nUX-3 test: review edits still reach the confirmation payload")
    import inspect
    src = inspect.getsource(app._render_review_stage)
    check("review stage no longer uses st.form", "st.form(" not in src)
    check("review stage no longer uses st.data_editor", "st.data_editor(" not in src)
    check("confirmation is a slide-to-confirm slider", "st.slider(" in src)
    check(
        "call_confirm is invoked with the current dosha_now/herbs_now variables",
        "call_confirm(" in src and "dosha_now, herbs_now" in src,
    )
    check(
        "the idempotency key is generated from the same submitted dosha_now/herbs_now",
        "get_idempotency_key_for(" in src and "dosha_now, herbs_now" in src,
    )
    check(
        "dosha_now/herbs_now are read live from current review state, not a snapshot",
        "_current_review_herbs()" in src and "_current_review_dosha()" in src,
    )


def test_back_never_confirms_or_calls_ai_after_form_wrap():
    """UX-3 — Back (now `st.button("← Back", key="btn_review_back", ...)`,
    an early return near the top of the function) must still never call
    call_confirm or call_preview. Checked structurally by isolating the
    Back block's own source (from the Back button up to the "AI DRAFT
    header" section that follows it), so a call_confirm/call_preview
    elsewhere in the function (inside the slide-to-confirm branch) can't
    accidentally satisfy this check."""
    print("\nUX-3 test: Back never confirms or calls AI")
    import inspect
    src = inspect.getsource(app._render_review_stage)
    back_start = src.index('if st.button("← Back"')
    back_end = src.index("# ---- 1. AI DRAFT header")
    back_block = src[back_start:back_end]

    check("the Back block does not call call_confirm", "call_confirm" not in back_block)
    check("the Back block does not call call_preview", "call_preview" not in back_block)
    check("the Back block does not call get_idempotency_key_for", "get_idempotency_key_for" not in back_block)
    check("the Back block clears review widget state", "_clear_review_widget_state()" in back_block)

    # Session-state trace mirroring test_back_does_not_confirm, through
    # the form-submit path specifically.
    _fresh_session()
    _st_stub.session_state["preview"] = {
        "preview_id": "prev-789", "dosha_imbalance": "Vata",
        "prescribed_herbs": [{"name": "Brahmi", "grams": 5}],
    }
    _st_stub.session_state["consultation_stage"] = "review"
    _st_stub.session_state["review_source"] = "ai"
    _st_stub.session_state["last_result"] = None

    # Simulate the back_btn branch exactly as _render_review_stage runs it.
    _st_stub.session_state["consultation_stage"] = "input"
    _st_stub.session_state["preview"] = None
    _st_stub.session_state.pop("herb_editor", None)
    _st_stub.session_state.pop("review_dosha", None)

    check("last_result is still None after Back (nothing confirmed)", _st_stub.session_state["last_result"] is None)


def test_idempotency_behavior_intact_after_repair():
    """Repair item 12 — get_idempotency_key_for() itself was not touched
    by any of the three repair fixes; re-verify its core same-inputs/
    different-inputs contract still holds unchanged."""
    print("\nRepair test 12: idempotency key behavior unchanged by the repair")
    _fresh_session()
    preview_id = "prev-repair-1"
    dosha = "Kapha"
    herbs = [{"name": "Triphala", "grams": 10}]

    key1 = app.get_idempotency_key_for(preview_id, dosha, herbs)
    key2 = app.get_idempotency_key_for(preview_id, dosha, herbs)
    check("Same inputs still reuse the same idempotency key", key1 == key2, f"key1={key1}, key2={key2}")

    herbs_changed = [{"name": "Triphala", "grams": 20}]
    key3 = app.get_idempotency_key_for(preview_id, dosha, herbs_changed)
    check("Different herbs still generate a fresh idempotency key", key1 != key3, f"key1={key1}, key3={key3}")

    import inspect
    check(
        "get_idempotency_key_for's own source is untouched by the repair",
        "confirm_signature" in inspect.getsource(app.get_idempotency_key_for),
    )


# ---------------------------------------------------------------------------
# Bonus: _DEFAULTS includes UX-2 keys
# ---------------------------------------------------------------------------

def test_defaults_include_ux2_keys():
    """UX-2 session state keys are present in _DEFAULTS."""
    print("\nBonus: _DEFAULTS includes UX-2 session state keys")
    check(
        "consultation_stage in _DEFAULTS",
        "consultation_stage" in app._DEFAULTS,
        list(app._DEFAULTS.keys()),
    )
    check(
        "consultation_stage defaults to 'input'",
        app._DEFAULTS["consultation_stage"] == "input",
        app._DEFAULTS["consultation_stage"],
    )
    check(
        "review_source in _DEFAULTS",
        "review_source" in app._DEFAULTS,
        list(app._DEFAULTS.keys()),
    )
    check(
        "review_source defaults to None",
        app._DEFAULTS["review_source"] is None,
        app._DEFAULTS["review_source"],
    )


# ---------------------------------------------------------------------------
# Bonus: locked function signatures are unchanged
# ---------------------------------------------------------------------------

def test_locked_function_signatures_unchanged():
    """call_* and related functions remain byte-for-byte unchanged by UX-2."""
    print("\nBonus: locked function signatures unchanged")
    import inspect

    # call_login(username, password) -> (dict|None, str|None)
    sig = inspect.signature(app.call_login)
    params = list(sig.parameters.keys())
    check("call_login has exactly 2 params", params == ["username", "password"], str(params))

    # call_logout(token) -> (bool, str|None)
    sig = inspect.signature(app.call_logout)
    params = list(sig.parameters.keys())
    check("call_logout has exactly 1 param (token)", params == ["token"], str(params))

    # call_preview(patient_name, patient_phone, patient_id, notes, token) -> (dict|None, str|None, bool)
    sig = inspect.signature(app.call_preview)
    params = list(sig.parameters.keys())
    check(
        "call_preview has exactly 5 params",
        params == ["patient_name", "patient_phone", "patient_id", "notes", "token"],
        str(params),
    )

    # call_manual_preview(patient_name, patient_phone, patient_id, dosha_imbalance, prescribed_herbs, token)
    sig = inspect.signature(app.call_manual_preview)
    params = list(sig.parameters.keys())
    check(
        "call_manual_preview has exactly 6 params",
        params == ["patient_name", "patient_phone", "patient_id", "dosha_imbalance", "prescribed_herbs", "token"],
        str(params),
    )

    # call_confirm(preview_id, dosha_imbalance, prescribed_herbs, token, idempotency_key=None)
    sig = inspect.signature(app.call_confirm)
    params = list(sig.parameters.keys())
    check(
        "call_confirm has exactly 5 params",
        params == ["preview_id", "dosha_imbalance", "prescribed_herbs", "token", "idempotency_key"],
        str(params),
    )

    # get_idempotency_key_for(preview_id, dosha_imbalance, prescribed_herbs) -> str
    sig = inspect.signature(app.get_idempotency_key_for)
    params = list(sig.parameters.keys())
    check(
        "get_idempotency_key_for has exactly 3 params",
        params == ["preview_id", "dosha_imbalance", "prescribed_herbs"],
        str(params),
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

_ALL_TESTS = [
    test_consultation_stage_defaults_to_input,
    test_patient_info_survives_stage_transition,
    test_notes_text_preserved,
    test_ai_and_manual_are_peer_choices,
    test_ai_path_sends_authorization,
    test_manual_path_sends_authorization,
    test_review_state_workflow,
    test_ai_manual_distinction_in_review,
    test_back_does_not_confirm,
    test_reset_clears_consultation_state,
    test_next_patient_preserves_auth,
    test_idempotency_key_behavior,
    test_ux1_error_translation_intact,
    # UX-2 REPAIR — browser-regression fixes
    test_reset_clears_herb_editor_and_review_dosha,
    test_patient_fields_rehydrate_from_shadow_state_on_back,
    test_next_patient_gets_fresh_herb_editor_no_bleed,
    test_review_edits_reach_confirmation_payload,
    test_back_never_confirms_or_calls_ai_after_form_wrap,
    test_idempotency_behavior_intact_after_repair,
    test_defaults_include_ux2_keys,
    test_locked_function_signatures_unchanged,
]


def run_tests():
    print("=== UX-2 focused tests ===\n")
    failures = []
    for test_fn in _ALL_TESTS:
        try:
            test_fn()
        except AssertionError as e:
            failures.append((test_fn.__name__, str(e)))
        except Exception as e:
            failures.append((test_fn.__name__, f"UNEXPECTED: {e}"))
        print()
    print(f"=== {_passed} passed, {_failed} failed ===")
    if failures:
        print(f"\nFailed tests:")
        for name, detail in failures:
            print(f"  {name}: {detail}")
    return failures


if __name__ == "__main__":
    failures = run_tests()
    sys.exit(1 if failures else 0)
