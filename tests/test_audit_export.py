"""
Audit-export tests -- covers backend/main.py's GET /audit/export, added to
turn the "data flywheel" corrections table into something that can
actually be handed to someone (a clinic owner, eventually an auditor),
not just a table that quietly accumulates rows nobody reads.

Specifically checks:
  1. A confirmed prescription shows up in the audit export for its clinic,
     with the AI's original proposal and the doctor's final confirmation
     both present and correct.
  2. was_changed is False when the doctor confirmed the AI's proposal
     unchanged, and True when their confirmation differs from it.
  3. The export never contains patient identity (name/phone/ID) anywhere
     in its response -- tested as a substring check against the raw
     response body, not just against the schema's declared fields, since
     this is a privacy contract, not an implementation detail.
  4. Records are scoped by clinic_id -- a correction logged under one
     clinic never appears in another clinic's export.

Phase A, A3 (RBAC + clinic isolation) update: this endpoint is no longer
unauthenticated. It now requires a real ADMIN or AUDITOR session (see
tests/test_rbac.py for the exhaustive role/clinic adversarial coverage of
that change -- DOCTOR-forbidden, cross-clinic-forbidden, missing-auth,
live role/clinic reflection, etc.). This file stays focused on what it
always was about: does the DATA come back correct, complete, and
clinic-scoped for a caller who IS allowed to see it. Every call below now
authenticates as an ADMIN of the clinic under test, and the clinic-scoping
test (previously "pass any clinic_id you like as a query param") is
rewritten to prove scoping the only way A3 allows it to be proven now: by
logging in as a DIFFERENT clinic's ADMIN and getting that clinic's own
data back, never someone else's.

Assumes db/seed.py has already been run (same assumption as the other
test files in this directory). REQUIRES MAC VERIFICATION (FastAPI +
argon2-cffi installed) -- see the A3 report's section G/I.

Run directly (no pytest required):
    python tests/test_audit_export.py
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


def _login_new_doctor(client, clinic_id: str = "default_clinic") -> dict:
    """Creates and logs in a DOCTOR for the given clinic -- used here only
    to generate a real, confirmed prescription for the audit export to
    reflect. See tests/test_golden.py's identical helper for the full
    rationale on why every prescribe call in this project needs a real
    login as of Phase A, A2."""
    from backend.auth_service import create_user

    username = f"audit_export_doctor_{secrets.token_hex(6)}"
    password = "audit-export-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id, role="DOCTOR")
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _login_new_admin(client, clinic_id: str = "default_clinic") -> dict:
    """Phase A, A3: GET /audit/export now requires a real ADMIN or AUDITOR
    session -- this is the login every audit_export call in this file uses
    to actually read the export back."""
    from backend.auth_service import create_user

    username = f"audit_export_admin_{secrets.token_hex(6)}"
    password = "audit-export-admin-test-password-not-a-real-credential"
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


def _set_stock(herb_name: str, grams: int, clinic_id: str = "default_clinic"):
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            "UPDATE inventory SET stock_grams = ? WHERE herb_name = ? AND clinic_id = ?",
            (grams, herb_name, clinic_id),
        )
        if cursor.rowcount == 0:
            conn.execute(
                "INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (clinic_id, herb_name, "test botanical name", grams, "TEST-BATCH", 90),
            )
        conn.commit()
    finally:
        conn.close()


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


def test_audit_export_reflects_ai_proposal_and_confirmation():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        return _test_audit_export_reflects_ai_proposal_and_confirmation(client)


def _test_audit_export_reflects_ai_proposal_and_confirmation(client):
    doctor_headers = _login_new_doctor(client)
    _set_stock("Ashwagandha", 5000)
    patient_name = "Audit Test Patient One"

    restore = _with_fake_gemini({"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 20}]})
    try:
        preview_resp = client.post("/prescribe/preview", json={"patient_name": patient_name, "notes": "notes"}, headers=doctor_headers)
    finally:
        restore()
    check("preview succeeds", preview_resp.status_code == 200, preview_resp.text)
    preview_id = preview_resp.json()["preview_id"]

    # Doctor confirms unchanged.
    confirm_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=doctor_headers)
    check("confirm succeeds", confirm_resp.status_code == 200, confirm_resp.text)
    prescription_id = confirm_resp.json()["prescription_id"]

    admin_headers = _login_new_admin(client)
    audit_resp = client.get("/audit/export", headers=admin_headers)
    check("audit export succeeds for an authenticated ADMIN", audit_resp.status_code == 200, audit_resp.text)
    audit_body = audit_resp.json()

    matching = [r for r in audit_body["records"] if r["prescription_id"] == prescription_id]
    check("confirmed prescription appears exactly once in the audit export", len(matching) == 1, str(matching))
    if matching:
        record = matching[0]
        check("ai_dosha_imbalance matches what the AI actually proposed", record["ai_dosha_imbalance"] == "Vata")
        check("confirmed_dosha_imbalance matches what was actually confirmed", record["confirmed_dosha_imbalance"] == "Vata")
        check("was_changed is False when the doctor confirmed the AI's proposal unchanged", record["was_changed"] is False, record)
    check("integrity_notes is present and non-empty", len(audit_body.get("integrity_notes", [])) > 0)


def test_audit_export_flags_a_real_correction():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    # Phase A, A2 note: also picks up the A1 "must be a context manager for
    # @app.on_event('startup') to fire" fix (see test_golden.py) while
    # touching this file -- needed now because logging in below requires
    # migrations 004/005 (users/sessions) to have actually been applied.
    with TestClient(main_module.app) as client:
        doctor_headers = _login_new_doctor(client)
        _set_stock("Ashwagandha", 5000)
        patient_name = "Audit Test Patient Two"

        restore = _with_fake_gemini({"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 20}]})
        try:
            preview_resp = client.post("/prescribe/preview", json={"patient_name": patient_name, "notes": "notes"}, headers=doctor_headers)
        finally:
            restore()
        preview_id = preview_resp.json()["preview_id"]

        # Doctor corrects the AI's proposed quantity.
        confirm_resp = client.post("/prescribe/confirm", json={
            "preview_id": preview_id,
            "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 25}],
        }, headers=doctor_headers)
        check("confirm with a correction succeeds", confirm_resp.status_code == 200, confirm_resp.text)
        _finish_test_audit_export_flags_a_real_correction(client, confirm_resp, patient_name)


def _finish_test_audit_export_flags_a_real_correction(client, confirm_resp, patient_name):
    prescription_id = confirm_resp.json()["prescription_id"]

    admin_headers = _login_new_admin(client)
    audit_body = client.get("/audit/export", headers=admin_headers).json()
    matching = [r for r in audit_body["records"] if r["prescription_id"] == prescription_id]
    check("corrected prescription appears in the audit export", len(matching) == 1)
    if matching:
        record = matching[0]
        check(
            "ai_prescribed_herbs still shows the ORIGINAL AI proposal (20g)",
            record["ai_prescribed_herbs"] == [{"name": "Ashwagandha", "grams": 20}], record,
        )
        check(
            "confirmed_prescribed_herbs shows the doctor's corrected value (25g)",
            record["confirmed_prescribed_herbs"] == [{"name": "Ashwagandha", "grams": 25}], record,
        )
        check(
            "was_changed is True when the doctor's confirmation differs from the AI's proposal",
            record["was_changed"] is True, record,
        )

    check("patient name never appears anywhere in the raw audit response body", patient_name not in json.dumps(audit_body))


def test_audit_export_scoped_by_clinic():
    """Phase A, A3 rewrite: before A3, /audit/export took an arbitrary,
    unauthenticated clinic_id query parameter, and this test proved
    scoping by simply passing two different values. That is no longer a
    meaningful test of clinic isolation -- a single unauthenticated caller
    freely choosing which clinic's data to view was exactly the hole A3
    closed. Scoping is now proven the only way A3 allows: two DIFFERENT
    ADMIN accounts, each belonging to a different clinic, each only ever
    seeing their own."""
    print("\nTest: /audit/export returns only the calling ADMIN's own clinic's data, never another's")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.corrections_service import log_correction
    with TestClient(main_module.app) as client:
        # Insert a minimal prescriptions row directly, then a correction
        # against a DIFFERENT clinic_id, bypassing the full preview/confirm
        # flow (which would need seeded inventory for that clinic too) --
        # this test is only about whether /audit/export filters by the
        # CALLER's clinic, not about the dispensing flow itself.
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO prescriptions (clinic_id, patient_name, description, status) VALUES (?, ?, ?, ?)",
            ("other_clinic_for_audit_test", "Other Clinic Patient", "test row", "confirmed"),
        )
        conn.commit()
        other_prescription_id = conn.execute("SELECT id FROM prescriptions ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.close()

        log_correction(
            prescription_id=other_prescription_id,
            clinic_id="other_clinic_for_audit_test",
            ai_dosha_imbalance="Pitta",
            ai_prescribed_herbs=[{"name": "Brahmi", "grams": 10}],
            confirmed_dosha_imbalance="Pitta",
            confirmed_prescribed_herbs=[{"name": "Brahmi", "grams": 10}],
        )

        default_clinic_admin_headers = _login_new_admin(client, clinic_id="default_clinic")
        other_clinic_admin_headers = _login_new_admin(client, clinic_id="other_clinic_for_audit_test")

        default_clinic_audit = client.get("/audit/export", headers=default_clinic_admin_headers).json()
        other_clinic_audit = client.get("/audit/export", headers=other_clinic_admin_headers).json()

        check(
            "a correction logged under a different clinic does NOT appear in default_clinic's ADMIN's export",
            all(r["prescription_id"] != other_prescription_id for r in default_clinic_audit["records"]),
        )
        check(
            "that same correction DOES appear when its OWN clinic's ADMIN calls the export -- with no "
            "query parameter needed at all, since scoping now comes from the authenticated caller, not a "
            "client-supplied argument",
            any(r["prescription_id"] == other_prescription_id for r in other_clinic_audit["records"]),
        )

        # And the OLD, pre-A3 way of asking for another clinic's data --
        # passing it explicitly as a query param -- is now flatly rejected
        # rather than silently honored.
        forged_query_resp = client.get(
            "/audit/export", params={"clinic_id": "other_clinic_for_audit_test"}, headers=default_clinic_admin_headers
        )
        check(
            "default_clinic's ADMIN explicitly asking for other_clinic's data via the query param is "
            "rejected with 403, not silently served",
            forged_query_resp.status_code == 403, forged_query_resp.text,
        )


def test_audit_export_requires_authentication():
    print("\nTest: /audit/export is no longer unauthenticated -- Phase A, A3's core fix for this endpoint")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        resp = client.get("/audit/export")
        check("calling /audit/export with no Authorization header at all is rejected with 401", resp.status_code == 401, resp.text)

        resp_with_clinic = client.get("/audit/export", params={"clinic_id": "default_clinic"})
        check(
            "supplying a clinic_id query parameter does not bypass authentication either",
            resp_with_clinic.status_code == 401, resp_with_clinic.text,
        )


_ALL_TESTS = [
    test_audit_export_reflects_ai_proposal_and_confirmation,
    test_audit_export_flags_a_real_correction,
    test_audit_export_scoped_by_clinic,
    test_audit_export_requires_authentication,
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
