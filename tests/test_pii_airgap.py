"""
The PII Air-Gap Golden Test.

Proves, not just asserts, the whole chain end to end and in-process (no
live uvicorn server needed -- uses FastAPI's TestClient):

  real PII -> local tokenization -> "Gemini" -> local rehydration -> SQLite

Specifically:
  1. Network-boundary test: intercepts the exact payload that would leave
     the local server for Gemini (by monkeypatching the google-genai
     client boundary in agents/copilot.py) and asserts the real patient
     name/phone are NOT in it, and the token IS. Then confirms via
     /prescribe/confirm and asserts the database ends up with the REAL
     patient name.
  2. Abort-before-call test: if the pre-flight PII check would fail, the
     AI boundary must never be invoked at all.
  3. Corrections-logging test: /prescribe/confirm always writes a
     corrections row (the data flywheel), and correctly flags whether the
     doctor changed anything relative to what the AI proposed.

Run directly (no pytest required):
    python tests/test_pii_airgap.py

Requires GEMINI_API_KEY to be unset in the real environment (the fake
key set here only exists to route agents.copilot.parse_clinical_notes down
its real code path -- the actual genai.Client is replaced, so no real
network call is ever made).
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
    """Phase A, A2: /prescribe/preview and /prescribe/confirm now require
    real authentication. See tests/test_golden.py's identical helper for
    the full rationale -- duplicated here rather than shared, matching
    this project's existing convention of each test file being
    self-contained."""
    from backend.auth_service import create_user

    username = f"pii_airgap_test_{secrets.token_hex(6)}"
    password = "pii-airgap-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id)
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


def _last_prescription() -> tuple:
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, patient_name FROM prescriptions ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        return (row[0], row[1]) if row else (None, None)
    finally:
        conn.close()


def _last_correction_for(prescription_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM corrections WHERE prescription_id = ? ORDER BY id DESC LIMIT 1",
            (prescription_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


class _FakeChat:
    """Stands in for the chat session object `client.chats.create(...)`
    returns. Records every outgoing `send_message` prompt so the test can
    inspect exactly what would have been sent over the network, and
    returns a canned final JSON reply -- mirrors agents/copilot.py's real
    call shape (chat = client.chats.create(...); chat.send_message(prompt)),
    which is what the agentic tool-calling loop actually uses."""

    def __init__(self, captured_payloads: list, canned_response: dict):
        self._captured_payloads = captured_payloads
        self._canned_response = canned_response

    def send_message(self, prompt):
        self._captured_payloads.append(prompt)

        class _FakeResponse:
            pass

        resp = _FakeResponse()
        resp.text = json.dumps(self._canned_response)
        return resp


class _FakeChats:
    def __init__(self, captured_payloads: list, canned_response: dict):
        self._captured_payloads = captured_payloads
        self._canned_response = canned_response

    def create(self, model, config):
        return _FakeChat(self._captured_payloads, self._canned_response)


class _FakeGenAIClient:
    def __init__(self, captured_payloads: list, canned_response: dict):
        self.chats = _FakeChats(captured_payloads, canned_response)


def _with_fake_gemini(canned_response: dict):
    """Context-manager-less helper: patches genai.Client, returns
    (client, captured_payloads, restore_fn)."""
    import agents.copilot as copilot_module

    captured_payloads: list = []
    real_client_cls = copilot_module.genai.Client
    original_api_key = os.environ.get("GEMINI_API_KEY")

    os.environ["GEMINI_API_KEY"] = "fake-test-key-not-a-real-credential"
    copilot_module.genai.Client = lambda api_key: _FakeGenAIClient(captured_payloads, canned_response)

    def restore():
        copilot_module.genai.Client = real_client_cls
        if original_api_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_api_key

    return captured_payloads, restore


def test_network_boundary_and_rehydration_and_db():
    print("\nTest: network boundary + rehydration + DB reverse-verification (preview/confirm split)")

    _set_stock("Ashwagandha", 5000)

    import backend.main as main_module

    captured_payloads, restore = _with_fake_gemini({
        "dosha_imbalance": "Vata",
        "prescribed_herbs": [{"name": "Ashwagandha", "grams": 20}],
    })
    try:
        from fastapi.testclient import TestClient
        with TestClient(main_module.app) as client:
            headers = _login_new_user(client)

            real_name = "John Mathew"
            real_phone = "9876543210"
            payload = {
                "patient_name": real_name,
                "patient_phone": real_phone,
                "patient_id": None,
                "notes": "Patient John Mathew (phone 9876543210) has fatigue and poor sleep. Vata imbalance. Prescribe 20g Ashwagandha.",
            }

            preview_response = client.post("/prescribe/preview", json=payload, headers=headers)

            check("/preview succeeded (200)", preview_response.status_code == 200, preview_response.text)
            check("exactly one AI call was made", len(captured_payloads) == 1, len(captured_payloads))

            outgoing_payload = captured_payloads[0] if captured_payloads else ""
            check("real patient name NOT in outgoing AI payload", real_name not in outgoing_payload)
            check("real phone NOT in outgoing AI payload", real_phone not in outgoing_payload)
            check("a [[PAT_...]] token IS in the outgoing AI payload", "[[PAT_" in outgoing_payload, outgoing_payload)

            if preview_response.status_code != 200:
                return

            preview_body = preview_response.json()
            check("preview response has the REAL patient name", preview_body["patient_name"] == real_name, preview_body)
            preview_id = preview_body["preview_id"]

            confirm_response = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
            check("/confirm succeeded (200)", confirm_response.status_code == 200, confirm_response.text)
            check("exactly one AI call was made overall (confirm makes none)", len(captured_payloads) == 1, len(captured_payloads))

            if confirm_response.status_code == 200:
                confirm_body = confirm_response.json()
                check("confirm response has the REAL patient name", confirm_body["patient_name"] == real_name, confirm_body)

            prescription_id, db_patient_name = _last_prescription()
            check("database row has the REAL patient name", db_patient_name == real_name, db_patient_name)
            check(
                "that same real name was proven absent from the network payload above",
                real_name not in outgoing_payload and db_patient_name == real_name,
            )
    finally:
        restore()


def test_ai_boundary_never_called_when_preflight_check_fails():
    print("\nTest: pre-flight PII check failing aborts BEFORE the AI boundary is reached")

    import backend.main as main_module
    import backend.pii_sanitizer as sanitizer_module

    captured_payloads, restore = _with_fake_gemini({"dosha_imbalance": "Vata", "prescribed_herbs": []})
    real_assert_fn = main_module.assert_no_pii_leak
    try:
        def _always_fail(sanitized_notes, pii_values):
            raise sanitizer_module.PIIViolationError("forced failure for this test")

        main_module.assert_no_pii_leak = _always_fail

        from fastapi.testclient import TestClient
        with TestClient(main_module.app) as client:
            headers = _login_new_user(client)

            response = client.post("/prescribe/preview", json={
                "patient_name": "Someone",
                "notes": "Irrelevant notes.",
            }, headers=headers)

            check("/preview was rejected (500) rather than proceeding", response.status_code == 500, response.text)
            check("the AI boundary was NEVER invoked", len(captured_payloads) == 0, len(captured_payloads))
    finally:
        main_module.assert_no_pii_leak = real_assert_fn
        restore()


def test_corrections_logging_captures_ai_vs_confirmed():
    print("\nTest: /prescribe/confirm always logs AI-proposed vs doctor-confirmed values")

    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)

        # --- Case A: doctor accepts the AI's proposal unchanged ---
        _set_stock("Ashwagandha", 5000)
        captured_payloads, restore = _with_fake_gemini({
            "dosha_imbalance": "Pitta",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}],
        })
        try:
            payload = {"patient_name": "Correction Test Patient A", "notes": "10g Ashwagandha for Pitta imbalance."}
            preview_response = client.post("/prescribe/preview", json=payload, headers=headers)
            check("Case A: /preview succeeded", preview_response.status_code == 200, preview_response.text)
            if preview_response.status_code != 200:
                return
            preview_id = preview_response.json()["preview_id"]

            confirm_response = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
            check("Case A: /confirm succeeded", confirm_response.status_code == 200, confirm_response.text)
            if confirm_response.status_code != 200:
                return

            prescription_id = confirm_response.json()["prescription_id"]
            correction = _last_correction_for(prescription_id)
            check("Case A: a corrections row was written", correction is not None)
            if correction:
                check("Case A: was_changed is 0 (doctor accepted AI as-is)", correction["was_changed"] == 0, correction)
                check("Case A: ai_dosha_imbalance matches confirmed_dosha_imbalance", correction["ai_dosha_imbalance"] == correction["confirmed_dosha_imbalance"])
        finally:
            restore()

        # --- Case B: doctor corrects the AI's proposal ---
        _set_stock("Ashwagandha", 5000)
        _set_stock("Brahmi", 3500)
        captured_payloads, restore = _with_fake_gemini({
            "dosha_imbalance": "Pitta",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}],
        })
        try:
            payload = {"patient_name": "Correction Test Patient B", "notes": "10g Ashwagandha for Pitta imbalance."}
            preview_response = client.post("/prescribe/preview", json=payload, headers=headers)
            check("Case B: /preview succeeded", preview_response.status_code == 200, preview_response.text)
            if preview_response.status_code != 200:
                return
            preview_id = preview_response.json()["preview_id"]

            # Doctor overrides both the dosha and the herb amount.
            confirm_response = client.post("/prescribe/confirm", json={
                "preview_id": preview_id,
                "dosha_imbalance": "Vata-Pitta",
                "prescribed_herbs": [{"name": "Brahmi", "grams": 25}],
            }, headers=headers)
            check("Case B: /confirm succeeded", confirm_response.status_code == 200, confirm_response.text)
            if confirm_response.status_code != 200:
                return

            confirm_body = confirm_response.json()
            check("Case B: confirmed dosha reflects the doctor's correction", confirm_body["dosha_imbalance"] == "Vata-Pitta", confirm_body)
            check("Case B: confirmed herbs reflect the doctor's correction", confirm_body["prescribed_herbs"] == [{"name": "Brahmi", "grams": 25}], confirm_body)

            prescription_id = confirm_body["prescription_id"]
            correction = _last_correction_for(prescription_id)
            check("Case B: a corrections row was written", correction is not None)
            if correction:
                check("Case B: was_changed is 1 (doctor corrected the AI)", correction["was_changed"] == 1, correction)
                check("Case B: ai_dosha_imbalance still records the ORIGINAL AI proposal", correction["ai_dosha_imbalance"] == "Pitta", correction)
                check("Case B: confirmed_dosha_imbalance records the doctor's correction", correction["confirmed_dosha_imbalance"] == "Vata-Pitta", correction)
        finally:
            restore()


_ALL_TESTS = [
    test_network_boundary_and_rehydration_and_db,
    test_ai_boundary_never_called_when_preflight_check_fails,
    test_corrections_logging_captures_ai_vs_confirmed,
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
    print("--- PII Air-Gap Golden Test (network boundary + DB, in-process) ---")
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
