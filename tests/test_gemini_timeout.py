"""
A6.3 P1 fix (Sep 2026) -- bounded client-side timeout on the Gemini call.

Prior to this fix, agents/copilot.py's parse_clinical_notes() had NO
client-side timeout anywhere around the Gemini network operation (chat =
client.chats.create(...); chat.send_message(prompt)) -- a hung network call
could occupy the FastAPI worker thread handling /prescribe/preview
indefinitely (see the A6.3 read-only review, Gate 11).

The fix adds http_options=types.HttpOptions(timeout=<ms>) to the
GenerateContentConfig already passed to client.chats.create(...) -- a real
HTTP-transport-level timeout enforced by the google-genai SDK's own
underlying client, not a Python-side thread wrapper that would leave the
underlying network call running after this process stops waiting on it.

Deliberately attached to GenerateContentConfig, NOT to genai.Client(...)
itself -- so every existing `lambda api_key: _FakeGenAIClient(...)` fake
across this project's other test files (test_audit.py, test_audit_export.py,
test_copilot_tool_calling.py, test_golden.py, test_manual_fallback.py,
test_pii_airgap.py, test_preview_lifecycle.py, test_rbac.py) keeps working
completely unmodified: none of them inspect config's fields, and none of
them intercept anything but genai.Client(api_key=...) itself. This file
does not modify any of them.

What this file proves:
  1. The configured timeout value is genuinely attached to the config
     object handed to chats.create() (captured the same way
     tests/test_copilot_tool_calling.py already captures configs).
  2. Normal Gemini success is completely unaffected.
  3. A Gemini-side timeout maps to the EXISTING AI_SERVICE_UNAVAILABLE
     contract -- no new error path was added; the existing blanket
     `except Exception` in agents/copilot.py already covers this.
  4. A timeout during /prescribe/preview leaves NO side effects: no
     preview persisted, no inventory deducted, no prescription created, no
     audit_events/corrections row written -- proven by querying the real
     database directly, the same way tests/test_pii_airgap.py does.
  5. The PII air-gap still holds in the timeout scenario specifically: the
     outgoing (about-to-fail) prompt is captured and asserted to contain a
     [[PAT_...]] token and NOT the real patient name/phone, and the real
     name is asserted absent from the HTTP error response body too.
  6. /prescribe/confirm makes ZERO Gemini calls -- proven with a poisoned
     fake client that fails the test immediately if genai.Client is ever
     constructed during that call, not just absent from a captured list.

This file does NOT call the real Gemini service (per the A6.3 P1
authorization's explicit instruction) -- every scenario uses a local,
deterministic fake exactly like the rest of this project's test suite.

Run directly (no pytest required):
    python tests/test_gemini_timeout.py
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


def _login_new_user(client, clinic_id: str = "default_clinic") -> dict:
    """Same self-contained helper every other test file in this project
    defines independently -- see tests/test_pii_airgap.py's identical
    copy for the full rationale."""
    from backend.auth_service import create_user

    username = f"gemini_timeout_test_{secrets.token_hex(6)}"
    password = "gemini-timeout-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _set_stock(herb_name: str, grams: int):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("UPDATE inventory SET stock_grams = ? WHERE herb_name = ?", (grams, herb_name))
        conn.commit()
    finally:
        conn.close()


def _get_stock(herb_name: str):
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute("SELECT stock_grams FROM inventory WHERE herb_name = ?", (herb_name,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _count(table: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# --- Fakes ---------------------------------------------------------------

class _FakeChat:
    """Records the outgoing send_message prompt and returns a canned
    successful response -- mirrors agents/copilot.py's real call shape."""

    def __init__(self, canned_response: dict, captured_prompts: list):
        self._canned_response = canned_response
        self._captured_prompts = captured_prompts

    def send_message(self, prompt):
        self._captured_prompts.append(prompt)

        class _FakeResponse:
            pass

        resp = _FakeResponse()
        resp.text = json.dumps(self._canned_response)
        return resp


class _FakeChats:
    """Also records the config it was created with -- this is what makes
    the 'timeout value is actually passed' test possible without any new
    fake infrastructure: production code's GenerateContentConfig object
    (with whatever http_options it carries) is captured verbatim."""

    def __init__(self, canned_response: dict, captured_configs: list, captured_prompts: list):
        self._canned_response = canned_response
        self._captured_configs = captured_configs
        self._captured_prompts = captured_prompts

    def create(self, model, config):
        self._captured_configs.append(config)
        return _FakeChat(self._canned_response, self._captured_prompts)


class _FakeGenAIClient:
    def __init__(self, canned_response: dict, captured_configs: list, captured_prompts: list):
        self.chats = _FakeChats(canned_response, captured_configs, captured_prompts)


def _with_fake_success(canned_response: dict):
    """Patches genai.Client so a call succeeds with a canned response.
    Returns (captured_configs, captured_prompts, restore_fn)."""
    import agents.copilot as copilot_module

    captured_configs: list = []
    captured_prompts: list = []
    real_client_cls = copilot_module.genai.Client
    original_api_key = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "fake-test-key-not-a-real-credential"
    copilot_module.genai.Client = lambda api_key: _FakeGenAIClient(canned_response, captured_configs, captured_prompts)

    def restore():
        copilot_module.genai.Client = real_client_cls
        if original_api_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_api_key

    return captured_configs, captured_prompts, restore


class _TimingOutFakeChat:
    """Simulates a Gemini call that did not respond in time -- the exact
    scenario this fix exists for. Records the prompt it was about to send
    (so the PII-boundary test can prove tokenization already happened
    before this failure) and then raises TimeoutError, mirroring what a
    real HTTP-transport-level timeout raises -- this exercises the SAME
    `except Exception: raise ValueError("AI_SERVICE_UNAVAILABLE")` block
    every other Gemini failure mode already goes through. See
    tests/test_manual_fallback.py's _FailingFakeChat for the established
    pattern this mirrors (that one raises RuntimeError to simulate a
    429/503; this one raises TimeoutError to simulate the timeout
    specifically)."""

    def __init__(self, captured_prompts: list):
        self._captured_prompts = captured_prompts

    def send_message(self, prompt):
        self._captured_prompts.append(prompt)
        raise TimeoutError("simulated Gemini request timeout (http_options.timeout expired)")


class _TimingOutFakeChats:
    def __init__(self, captured_prompts: list):
        self._captured_prompts = captured_prompts

    def create(self, model, config):
        return _TimingOutFakeChat(self._captured_prompts)


class _TimingOutFakeGenAIClient:
    def __init__(self, captured_prompts: list):
        self.chats = _TimingOutFakeChats(captured_prompts)


def _with_fake_timeout():
    """Patches genai.Client so the call simulates a timeout. Returns
    (captured_prompts, restore_fn)."""
    import agents.copilot as copilot_module

    captured_prompts: list = []
    real_client_cls = copilot_module.genai.Client
    original_api_key = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "fake-test-key-not-a-real-credential"
    copilot_module.genai.Client = lambda api_key: _TimingOutFakeGenAIClient(captured_prompts)

    def restore():
        copilot_module.genai.Client = real_client_cls
        if original_api_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = original_api_key

    return captured_prompts, restore


class _PoisonedGenAIClient:
    """Fails the test immediately if genai.Client is EVER constructed --
    used to prove /prescribe/confirm makes zero Gemini calls by making any
    accidental call impossible to miss, rather than merely absent from a
    captured-calls list."""

    def __init__(self, api_key):
        raise AssertionError(
            "genai.Client() was constructed during a call that must make ZERO Gemini "
            "calls (this poisoned fake is only installed around /prescribe/confirm)."
        )


def _with_poisoned_gemini():
    import agents.copilot as copilot_module

    real_client_cls = copilot_module.genai.Client
    copilot_module.genai.Client = _PoisonedGenAIClient

    def restore():
        copilot_module.genai.Client = real_client_cls

    return restore


# --- Tests -----------------------------------------------------------------

def test_configured_timeout_is_actually_passed_to_the_chat_config():
    print("\nTest: the configured Gemini timeout is genuinely attached to the GenerateContentConfig passed to chats.create()")
    import agents.copilot as copilot_module

    captured_configs, _prompts, restore = _with_fake_success({
        "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}],
    })
    try:
        copilot_module.parse_clinical_notes("[[PAT_ABCDEF03]] needs 10g Ashwagandha for Vata imbalance.")
        check("exactly one chat config was captured", len(captured_configs) == 1, len(captured_configs))
        if not captured_configs:
            return
        config = captured_configs[0]
        check("the captured config carries a non-None http_options value", getattr(config, "http_options", None) is not None, config)
        configured_timeout_ms = int(copilot_module.GEMINI_TIMEOUT_SECONDS * 1000)
        check(
            f"http_options.timeout equals GEMINI_TIMEOUT_SECONDS ({copilot_module.GEMINI_TIMEOUT_SECONDS}s = {configured_timeout_ms}ms) "
            "-- proves the fix actually applies a bounded value, not the SDK's own unbounded default",
            getattr(getattr(config, "http_options", None), "timeout", None) == configured_timeout_ms,
            getattr(config, "http_options", None),
        )
    finally:
        restore()


def test_normal_gemini_success_is_unaffected():
    print("\nTest: a normal, fast Gemini success still works exactly as before")
    import agents.copilot as copilot_module

    captured_configs, prompts, restore = _with_fake_success({
        "dosha_imbalance": "Pitta", "prescribed_herbs": [{"name": "Brahmi", "grams": 5}],
    })
    try:
        result = copilot_module.parse_clinical_notes("[[PAT_ABCDEF04]] needs 5g Brahmi for Pitta imbalance.")
        check("dosha_imbalance parsed correctly", result.dosha_imbalance == "Pitta", result)
        check(
            "prescribed_herbs parsed correctly",
            result.prescribed_herbs[0].name == "Brahmi" and result.prescribed_herbs[0].grams == 5,
            result,
        )
        check("exactly one prompt was sent", len(prompts) == 1, prompts)
    finally:
        restore()


def test_gemini_timeout_maps_to_ai_service_unavailable():
    print("\nTest: a Gemini-side timeout raises the SAME ValueError('AI_SERVICE_UNAVAILABLE') as every other Gemini failure mode")
    import agents.copilot as copilot_module

    _prompts, restore = _with_fake_timeout()
    try:
        try:
            copilot_module.parse_clinical_notes("[[PAT_ABCDEF05]] needs 10g Ashwagandha for Vata imbalance.")
            check("parse_clinical_notes raised on a simulated timeout", False, "no exception was raised")
        except ValueError as e:
            check("the raised ValueError's message is exactly AI_SERVICE_UNAVAILABLE", str(e) == "AI_SERVICE_UNAVAILABLE", str(e))
        except Exception as e:
            check("a timeout raised ValueError('AI_SERVICE_UNAVAILABLE'), not some other exception type", False, f"{type(e).__name__}: {e}")
    finally:
        restore()


def test_preview_timeout_leaves_no_database_side_effects_and_preserves_pii_boundary():
    print("\nTest: a /prescribe/preview call that times out leaves NO preview/inventory/prescription/audit side effects, and the PII boundary still held before the failure")
    import backend.main as main_module
    from fastapi.testclient import TestClient

    _set_stock("Ashwagandha", 5000)
    stock_before = _get_stock("Ashwagandha")
    previews_before = _count("prescription_previews")
    prescriptions_before = _count("prescriptions")
    audit_events_before = _count("audit_events")
    corrections_before = _count("corrections")

    captured_prompts, restore = _with_fake_timeout()
    try:
        with TestClient(main_module.app) as client:
            headers = _login_new_user(client)
            real_name = "Timeout Test Patient"
            real_phone = "9998887776"
            resp = client.post("/prescribe/preview", json={
                "patient_name": real_name,
                "patient_phone": real_phone,
                "patient_id": None,
                "notes": f"Patient {real_name} (phone {real_phone}) needs 10g Ashwagandha for Vata imbalance.",
            }, headers=headers)

            check("/prescribe/preview returns 502 on a simulated Gemini timeout", resp.status_code == 502, resp.text)
            check(
                "the response carries the existing X-Error-Code: AI_SERVICE_UNAVAILABLE contract -- unchanged by this fix",
                resp.headers.get("X-Error-Code") == "AI_SERVICE_UNAVAILABLE",
                dict(resp.headers),
            )
            check("the real patient name does not appear anywhere in the HTTP error response body", real_name not in resp.text, resp.text)

            outgoing_prompt = captured_prompts[0] if captured_prompts else ""
            check("tokenization still ran before the (about-to-fail) AI call: real name NOT in the outgoing prompt", real_name not in outgoing_prompt, outgoing_prompt)
            check("tokenization still ran before the (about-to-fail) AI call: real phone NOT in the outgoing prompt", real_phone not in outgoing_prompt, outgoing_prompt)
            check("a [[PAT_...]] token IS in the outgoing prompt", "[[PAT_" in outgoing_prompt, outgoing_prompt)

        check("inventory stock is completely unchanged after the timeout", _get_stock("Ashwagandha") == stock_before, (_get_stock("Ashwagandha"), stock_before))
        check("no new prescription_previews row was created", _count("prescription_previews") == previews_before, (_count("prescription_previews"), previews_before))
        check("no new prescriptions row was created", _count("prescriptions") == prescriptions_before, (_count("prescriptions"), prescriptions_before))
        check("no new audit_events row was created", _count("audit_events") == audit_events_before, (_count("audit_events"), audit_events_before))
        check("no new corrections row was created", _count("corrections") == corrections_before, (_count("corrections"), corrections_before))
    finally:
        restore()


def test_confirm_makes_zero_gemini_calls():
    print("\nTest: /prescribe/confirm makes ZERO Gemini calls (manual-preview -> confirm flow, with genai.Client poisoned to fail loudly if ever touched)")
    import backend.main as main_module
    from fastapi.testclient import TestClient

    _set_stock("Ashwagandha", 5000)
    restore = _with_poisoned_gemini()
    try:
        with TestClient(main_module.app) as client:
            headers = _login_new_user(client)
            preview_resp = client.post("/prescribe/manual-preview", json={
                "patient_name": "Zero AI Call Test Patient",
                "dosha_imbalance": "Vata",
                "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}],
            }, headers=headers)
            check("manual-preview succeeded without touching Gemini", preview_resp.status_code == 200, preview_resp.text)
            if preview_resp.status_code != 200:
                return
            preview_id = preview_resp.json()["preview_id"]

            confirm_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
            check("confirm succeeded without ever constructing genai.Client", confirm_resp.status_code == 200, confirm_resp.text)
    finally:
        restore()


_ALL_TESTS = [
    test_configured_timeout_is_actually_passed_to_the_chat_config,
    test_normal_gemini_success_is_unaffected,
    test_gemini_timeout_maps_to_ai_service_unavailable,
    test_preview_timeout_leaves_no_database_side_effects_and_preserves_pii_boundary,
    test_confirm_makes_zero_gemini_calls,
]


def run_tests():
    print("--- A6.3 P1: Gemini call timeout -- focused verification ---\n")
    _function_failures = []
    for _test_fn in _ALL_TESTS:
        try:
            _test_fn()
        except AssertionError as e:
            _function_failures.append(_test_fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {_test_fn.__name__} ({e})")
        print()
    print(f"--- {_passed} passed, {_failed} failed ---")
    if _function_failures:
        print(f"--- {len(_function_failures)} test function(s) had a failing assertion: {', '.join(_function_failures)} ---")
    return _function_failures


if __name__ == "__main__":
    _failures = run_tests()
    if _failures:
        sys.exit(1)
