"""
Manual-fallback tests -- covers backend/main.py's POST /prescribe/manual-preview
and the two related changes that make it safe:

  1. /prescribe/preview sets an X-Error-Code: AI_SERVICE_UNAVAILABLE response
     header specifically (and only) when Gemini itself couldn't be reached
     (quota exhausted, outage, timeout) -- this is what ui/app.py's
     "Continue without AI" offer keys off of, instead of string-matching the
     human-readable `detail` text.
  2. /prescribe/manual-preview lets a doctor supply dosha_imbalance and
     prescribed_herbs directly, with NO call to Gemini at all, then hands
     off to the EXACT same /prescribe/confirm code path an AI-sourced
     preview uses -- same herb-name resolution, same stock check, same
     single-use preview/confirm split.
  3. /prescribe/confirm deliberately does NOT log a correction-table row
     for a manual-fallback preview, since there was no AI proposal to
     compare against -- logging one anyway would fabricate an "AI proposed
     this and it was unchanged" data point that never happened.

Specifically checks:
  - An unresolvable herb name is rejected by /prescribe/manual-preview with
    the same 400 /prescribe/preview would give the AI for the same name.
  - A valid manual preview, once confirmed, deducts stock correctly and
    returns a real prescription -- indistinguishable in outcome from an
    AI-sourced one.
  - That confirmed manual prescription does NOT show up in /audit/export --
    proving the correction-log skip actually happens, not just that it
    doesn't crash.
  - /prescribe/preview's real Gemini-call failure path (not the "no API key"
    mock-parser shortcut) sets X-Error-Code: AI_SERVICE_UNAVAILABLE on its
    502, which is the exact signal the UI's fallback offer depends on.

Assumes db/seed.py has already been run (same assumption as the other test
files in this directory). Run directly (no pytest required):
    python tests/test_manual_fallback.py
"""
import json
import os
import secrets
import sqlite3
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

_passed = 0
_failed = 0


def _login_new_user(client, clinic_id: str = "default_clinic") -> dict:
    """Phase A, A2: every prescribe endpoint now requires real
    authentication. See tests/test_golden.py's identical helper for the
    full rationale -- duplicated here rather than shared, matching this
    project's existing convention of each test file being self-contained.
    Defaults to role="DOCTOR" (create_user()'s own default), which is
    exactly what Phase A, A3 requires to call any of the three prescribe
    endpoints -- unchanged by A3 for that reason."""
    from backend.auth_service import create_user

    username = f"manual_fallback_test_{secrets.token_hex(6)}"
    password = "manual-fallback-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _login_new_admin(client, clinic_id: str = "default_clinic") -> dict:
    """Phase A, A3: GET /audit/export now requires a real ADMIN or AUDITOR
    session (see tests/test_rbac.py for the exhaustive role/clinic
    coverage of that change) -- this is the login test_manual_fallback's
    one audit_export call below now needs to read the export back at
    all."""
    from backend.auth_service import create_user

    username = f"manual_fallback_admin_{secrets.token_hex(6)}"
    password = "manual-fallback-admin-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id, role="ADMIN")
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def check(label: str, condition: bool, detail: str = ""):
    """Records AND enforces one assertion -- raises AssertionError
    immediately on failure instead of only printing a FAIL line and
    incrementing a counter, so that `pytest tests/` genuinely fails when a
    check fails, not just this file's own __main__ block. See
    tests/test_rbac.py's module docstring, "PYTEST COMPATIBILITY", for the
    real incident (a cross-clinic information-disclosure bug that was
    invisible to `pytest tests/ -v`) this fixes across every test file in
    this project that still had the old, non-raising version."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _set_stock(herb_name: str, grams: int):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("UPDATE inventory SET stock_grams = ? WHERE herb_name = ?", (grams, herb_name))
        conn.commit()
    finally:
        conn.close()


def _get_stock(herb_name: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT stock_grams FROM inventory WHERE herb_name = ?", (herb_name,)).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


# --- Fakes for the "Gemini returns a canned answer" tests (unused by the
# outage test below, which needs Gemini to FAIL instead -- see _FakeChatThatFails). ---

class _FakeChat:
    def __init__(self, canned_response: dict):
        self._canned_response = canned_response

    def send_message(self, prompt):
        class _FakeResponse:
            pass
        resp = _FakeResponse()
        resp.text = json.dumps(self._canned_response)
        return resp


class _FakeChats:
    def __init__(self, canned_response: dict):
        self._canned_response = canned_response

    def create(self, model, config):
        return _FakeChat(self._canned_response)


class _FakeGenAIClient:
    def __init__(self, canned_response: dict):
        self.chats = _FakeChats(canned_response)


def _with_fake_gemini(canned_response: dict):
    import agents.copilot as copilot_module

    real_client_cls = copilot_module.genai.Client
    original_api_key = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "fake-test-key-not-a-real-credential"
    copilot_module.genai.Client = lambda api_key: _FakeGenAIClient(canned_response)

    def restore():
        copilot_module.genai.Client = real_client_cls
        if original_api_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_api_key

    return restore


# --- Fakes for the "Gemini itself is unreachable" test. This exercises the
# REAL code path in agents/copilot.py (the `try: chat.send_message(...)
# except Exception: raise ValueError("AI_SERVICE_UNAVAILABLE")` block around
# line 227), not the separate "GEMINI_API_KEY is unset" mock-parser shortcut
# -- a 429/503/timeout from the real API raises inside that same try block,
# so simulating it here means send_message() must raise, not return. ---

class _FailingFakeChat:
    def send_message(self, prompt):
        raise RuntimeError("simulated Gemini outage (e.g. 429 RESOURCE_EXHAUSTED)")


class _FailingFakeChats:
    def create(self, model, config):
        return _FailingFakeChat()


class _FailingFakeGenAIClient:
    def __init__(self):
        self.chats = _FailingFakeChats()


def _with_failing_gemini():
    import agents.copilot as copilot_module

    real_client_cls = copilot_module.genai.Client
    original_api_key = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "fake-test-key-not-a-real-credential"
    copilot_module.genai.Client = lambda api_key: _FailingFakeGenAIClient()

    def restore():
        copilot_module.genai.Client = real_client_cls
        if original_api_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_api_key

    return restore


def test_manual_preview_rejects_unknown_herb():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    # Phase A, A1 fix: must use TestClient as a context manager, or
    # @app.on_event("startup") (which runs pending migrations) never fires
    # -- see test_golden.py's run_tests() for the full explanation. Every
    # function in this file was hitting "no such table: prescription_previews"
    # for exactly this reason.
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        resp = client.post("/prescribe/manual-preview", json={
            "patient_name": "Manual Fallback Test Patient A",
            "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Tulsi", "grams": 15}],
        }, headers=headers)
        check(
            "manual-preview rejects an unresolvable herb name with 400, same as the AI path would",
            resp.status_code == 400, f"got {resp.status_code}: {resp.text}",
        )


def test_manual_preview_requires_at_least_one_herb():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        resp = client.post("/prescribe/manual-preview", json={
            "patient_name": "Manual Fallback Test Patient B",
            "dosha_imbalance": "Vata",
            "prescribed_herbs": [],
        }, headers=headers)
        check(
            "manual-preview rejects an empty herb list at the schema level (422)",
            resp.status_code == 422, f"got {resp.status_code}: {resp.text}",
        )


def test_manual_preview_then_confirm_deducts_stock():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        initial = _get_stock("Ashwagandha")

        preview_resp = client.post("/prescribe/manual-preview", json={
            "patient_name": "Manual Fallback Test Patient C",
            "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "ashwagandha", "grams": 30}],  # lowercase, to prove alias resolution runs
        }, headers=headers)
        check("manual-preview succeeds for a known herb", preview_resp.status_code == 200, preview_resp.text)
        body = preview_resp.json()
        check(
            "manual-preview returns the herb under its canonical (resolved) name",
            body.get("prescribed_herbs") == [{"name": "Ashwagandha", "grams": 30}], body,
        )
        preview_id = body["preview_id"]

        confirm_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check("confirming a manual preview succeeds", confirm_resp.status_code == 200, confirm_resp.text)

        final = _get_stock("Ashwagandha")
        check(
            "stock is deducted by exactly the manually-entered amount",
            final == initial - 30, f"expected {initial - 30}, got {final}",
        )

        # Preview is single-use exactly like an AI-sourced one.
        replay_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check("re-confirming the same manual preview_id is rejected with 409", replay_resp.status_code == 409, replay_resp.text)

        _set_stock("Ashwagandha", 5000)  # restore for other tests/runs


def test_manual_confirm_does_not_pollute_audit_trail():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Brahmi", 3500)

        preview_resp = client.post("/prescribe/manual-preview", json={
            "patient_name": "Manual Fallback Test Patient D",
            "dosha_imbalance": "Pitta",
            "prescribed_herbs": [{"name": "Brahmi", "grams": 12}],
        }, headers=headers)
        preview_id = preview_resp.json()["preview_id"]

        confirm_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check("manual confirm succeeds", confirm_resp.status_code == 200, confirm_resp.text)
        prescription_id = confirm_resp.json()["prescription_id"]

        # Phase A, A3: /audit/export now requires a real ADMIN/AUDITOR
        # session -- previously called here unauthenticated.
        admin_headers = _login_new_admin(client)
        audit_body = client.get("/audit/export", headers=admin_headers).json()
        matching = [r for r in audit_body["records"] if r["prescription_id"] == prescription_id]
        check(
            "a manually-confirmed prescription does NOT appear in the AI-proposal-vs-confirmation audit "
            "export -- there was no AI proposal to log against it",
            len(matching) == 0, str(matching),
        )

        _set_stock("Brahmi", 3500)  # restore


def test_ai_outage_sets_error_code_header():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        restore = _with_failing_gemini()
        try:
            resp = client.post("/prescribe/preview", json={
                "patient_name": "Manual Fallback Test Patient E",
                "notes": "Patient needs 10g of Ashwagandha.",
            }, headers=headers)
        finally:
            restore()

        check("a real Gemini failure still returns 502", resp.status_code == 502, f"got {resp.status_code}: {resp.text}")
        check(
            "the 502 carries X-Error-Code: AI_SERVICE_UNAVAILABLE -- the exact signal "
            "ui/app.py's manual-fallback offer depends on",
            resp.headers.get("X-Error-Code") == "AI_SERVICE_UNAVAILABLE",
            dict(resp.headers),
        )


_ALL_TESTS = [
    test_manual_preview_rejects_unknown_herb,
    test_manual_preview_requires_at_least_one_herb,
    test_manual_preview_then_confirm_deducts_stock,
    test_manual_confirm_does_not_pollute_audit_trail,
    test_ai_outage_sets_error_code_header,
]


if __name__ == "__main__":
    # check() now raises AssertionError on a failed assertion (see its own
    # docstring, and tests/test_rbac.py's module docstring "PYTEST
    # COMPATIBILITY") -- so each test function is called through this
    # loop, which catches that raise per-function and moves on to the
    # next one, rather than a flat list of bare calls that would abort
    # the whole script at the FIRST failing assertion anywhere. This
    # keeps direct execution's long-standing contract ("run every test
    # function, print a full tally at the end") intact while making every
    # individual check() a real, hard assertion under pytest too.
    _function_failures = []
    for _test_fn in _ALL_TESTS:
        try:
            _test_fn()
        except AssertionError as e:
            _function_failures.append(_test_fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {_test_fn.__name__} ({e})")

    print(f"\n--- {_passed} passed, {_failed} failed ---")
    if _function_failures:
        print(f"--- {len(_function_failures)} test function(s) had a failing assertion: {', '.join(_function_failures)} ---")
    if _failed:
        sys.exit(1)
