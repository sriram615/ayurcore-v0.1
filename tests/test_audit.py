"""
Tamper-evident audit event HTTP-layer tests (Phase A5) -- covers
GET /audit/events and backend/main.py's /prescribe/confirm wiring of
backend/audit_store.py (see tests/test_audit_store.py for that module's
own, sandbox-runnable, non-FastAPI tests).

Like every other FastAPI-dependent test in this project, this file
REQUIRES MAC VERIFICATION (FastAPI + argon2-cffi installed) -- it is not
expected to run in the sandbox.

Specifically checks:
  1. AI-assisted, unchanged: a real /prescribe/preview -> /prescribe/confirm
     round trip (via a faked Gemini response, same pattern as
     tests/test_audit_export.py) produces exactly one PRESCRIPTION_CONFIRMED
     audit event, decision_source AI_ASSISTED, ai_proposal matching what
     the AI actually proposed, was_changed False.
  2. AI-assisted, corrected: was_changed True, and ai_proposal STILL shows
     the ORIGINAL AI proposal (not the doctor's correction).
  3. Manual fallback: decision_source MANUAL, ai_proposal is null -- never
     fabricated, per the approved A5 design and backend/audit_store.py's
     own module docstring.
  4. Authentication: GET /audit/events with no Authorization header -> 401.
  5. RBAC: a DOCTOR's valid session gets 403 on GET /audit/events -- same
     posture as the pre-existing GET /audit/export (tests/test_rbac.py
     covers the exhaustive role matrix; this file only re-confirms the
     ADMIN/AUDITOR-vs-DOCTOR boundary for THIS new endpoint specifically).
  6. Clinic isolation: an ADMIN only ever sees their OWN clinic's audit
     events -- proven by logging in as two DIFFERENT clinics' ADMINs, per
     tests/test_audit_export.py's established pattern -- and an explicit,
     mismatched `clinic_id` query parameter is rejected with 403, never
     silently substituted.
  7. chain_valid reflects a REAL SQLite tampering test run against the
     full stack: a real HTTP-confirmed prescription's audit_events row is
     located and directly modified via raw sqlite3 (bypassing this
     application's code paths entirely, exactly as a real attacker with
     file-level database access would), and GET /audit/events for that
     SAME clinic is shown to flip chain_valid from True to False and back
     to True after the row is restored. This is IN ADDITION to
     tests/test_audit_store.py's own, more exhaustive per-column
     tampering matrix -- this one exercises the real FastAPI endpoint's
     LIVE integrity check specifically, not just the underlying function.
  8. Concurrency: two genuinely concurrent /prescribe/confirm calls for
     two DIFFERENT previews in the SAME clinic (same pattern as
     tests/test_idempotency.py's concurrent test) both succeed, each
     produces exactly one audit event for its own prescription, and the
     resulting chain (as read back via GET /audit/events) is chain_valid.
  9. Audit-write failure integrity: create_audit_event is monkeypatched
     (same established mocking convention this project already uses for
     _with_fake_gemini above) to raise, for exactly one request, so
     backend/main.py's REAL exception-handling path -- not a simulation of
     it -- actually runs. Proves /prescribe/confirm returns 500, NO
     prescription is created, and the preview remains re-confirmable
     (still PENDING) when the audit write fails: audit failure is never
     silent and never leaves an orphaned, un-audited prescription.
     (A raw-sqlite3 forced-collision approach was considered and rejected
     for this specific case: any placeholder row inserted to satisfy
     audit_events' own foreign keys would itself consume the very
     AUTOINCREMENT id the real request needs to collide with, making a
     genuine collision impossible to set up from outside the request path
     -- monkeypatching the exact function backend/main.py calls is the
     more direct, reliable reproduction of "this call raises" here.)

Run directly (no pytest required):
    python tests/test_audit.py
Or under pytest:
    pytest tests/test_audit.py -v
"""
import json
import os
import secrets
import sqlite3
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Raises AssertionError immediately on failure -- see
    tests/test_rbac.py's module docstring, "PYTEST COMPATIBILITY", for why
    every security-relevant test file in this project uses a raising
    check(), not a printing-only one."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _login_new_user(client, clinic_id: str = "default_clinic", role: str = "DOCTOR") -> dict:
    from backend.auth_service import create_user

    username = f"audit_test_{secrets.token_hex(6)}"
    password = "audit-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id, role=role)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


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


def _unique_patient_name(label: str) -> str:
    """Same isolation strategy as tests/test_idempotency.py's identical
    helper -- see that file's module docstring for the full incident this
    fixes. Every prescription this suite creates goes into the real,
    persistent db/ayurcore.db with no teardown."""
    return f"{label} {secrets.token_hex(6)}"


def _manual_preview_payload(patient_name, herb="Ashwagandha", grams=5):
    return {
        "patient_name": patient_name,
        "dosha_imbalance": "Vata",
        "prescribed_herbs": [{"name": herb, "grams": grams}],
    }


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
    """Same helper as tests/test_audit_export.py -- see that file for the
    full rationale."""
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


def _get_audit_event_for_prescription(admin_headers, client, prescription_id: int):
    body = client.get("/audit/events", headers=admin_headers).json()
    matching = [e for e in body["events"] if e["prescription_id"] == prescription_id]
    return body, matching


def _audit_row_for_prescription(prescription_id: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM audit_events WHERE prescription_id = ?", (prescription_id,)).fetchone()
    finally:
        conn.close()


def _raw_update_audit_event(event_id: int, column: str, value) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        # Migration 014 made audit_events append-only with triggers. A real
        # attacker with raw access to the .db file can drop them first, so
        # this simulation does exactly that (and restores the trigger from
        # its exact stored SQL afterwards) -- the point of this test is that
        # the hash chain still DETECTS such an edit.
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'audit_events_no_update'"
        ).fetchone()
        if trigger:
            conn.execute("DROP TRIGGER audit_events_no_update")
        conn.execute(f"UPDATE audit_events SET {column} = ? WHERE event_id = ?", (value, event_id))
        if trigger:
            conn.execute(trigger[0])
        conn.commit()
    finally:
        conn.close()


def _preview_status(preview_id: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status FROM prescription_previews WHERE preview_id = ?", (preview_id,)).fetchone()
        return row["status"] if row else None
    finally:
        conn.close()


def _count_prescriptions_for_patient(patient_name: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM prescriptions WHERE patient_name = ?", (patient_name,)).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1-2. AI-assisted: unchanged and corrected
# ---------------------------------------------------------------------------

def test_ai_assisted_unchanged_produces_correct_audit_event():
    print("\nTest: AI-assisted, doctor confirms unchanged -> AI_ASSISTED event, was_changed False")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        clinic_id = f"audit_http_clinic_{secrets.token_hex(4)}"
        doctor_headers = _login_new_user(client, clinic_id=clinic_id, role="DOCTOR")
        admin_headers = _login_new_user(client, clinic_id=clinic_id, role="ADMIN")
        _set_stock("Ashwagandha", 5000, clinic_id=clinic_id)
        patient_name = _unique_patient_name("Audit HTTP AI Unchanged")

        restore = _with_fake_gemini({"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 20}]})
        try:
            preview_resp = client.post("/prescribe/preview", json={"patient_name": patient_name, "notes": "notes"}, headers=doctor_headers)
        finally:
            restore()
        check("preview succeeds", preview_resp.status_code == 200, preview_resp.text)
        preview_id = preview_resp.json()["preview_id"]

        confirm_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=doctor_headers)
        check("confirm succeeds", confirm_resp.status_code == 200, confirm_resp.text)
        prescription_id = confirm_resp.json()["prescription_id"]

        body, matching = _get_audit_event_for_prescription(admin_headers, client, prescription_id)
        check("GET /audit/events succeeds for an authenticated ADMIN", "events" in body, body)
        check("exactly one audit event exists for this prescription", len(matching) == 1, matching)
        event = matching[0]
        check("event_type is PRESCRIPTION_CONFIRMED", event["event_type"] == "PRESCRIPTION_CONFIRMED", event)
        check("decision_source is AI_ASSISTED", event["event_data"]["decision_source"] == "AI_ASSISTED", event)
        check("ai_proposal matches what the AI actually proposed", event["event_data"]["ai_proposal"] == {"dosha": "Vata", "herbs": [{"name": "Ashwagandha", "grams": 20}]}, event)
        check("doctor_decision matches the confirmed values", event["event_data"]["doctor_decision"] == {"dosha": "Vata", "herbs": [{"name": "Ashwagandha", "grams": 20}]}, event)
        check("was_changed is False", event["event_data"]["was_changed"] is False, event)
        check("actor_user_id is a real integer (the confirming doctor's user_id)", isinstance(event["actor_user_id"], int), event)
        check("preview_id matches the preview this event came from", event["preview_id"] == preview_id, event)
        check("chain_valid is True", body["chain_valid"] is True, body)
        check("no integrity_violations are reported", body["integrity_violations"] == [], body)


def test_ai_assisted_corrected_preserves_original_proposal():
    print("\nTest: AI-assisted, doctor corrects -> was_changed True, ORIGINAL ai_proposal preserved")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        clinic_id = f"audit_http_clinic_{secrets.token_hex(4)}"
        doctor_headers = _login_new_user(client, clinic_id=clinic_id, role="DOCTOR")
        admin_headers = _login_new_user(client, clinic_id=clinic_id, role="ADMIN")
        _set_stock("Ashwagandha", 5000, clinic_id=clinic_id)
        patient_name = _unique_patient_name("Audit HTTP AI Corrected")

        restore = _with_fake_gemini({"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 20}]})
        try:
            preview_resp = client.post("/prescribe/preview", json={"patient_name": patient_name, "notes": "notes"}, headers=doctor_headers)
        finally:
            restore()
        preview_id = preview_resp.json()["preview_id"]

        confirm_resp = client.post("/prescribe/confirm", json={
            "preview_id": preview_id,
            "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 25}],
        }, headers=doctor_headers)
        check("confirm with a correction succeeds", confirm_resp.status_code == 200, confirm_resp.text)
        prescription_id = confirm_resp.json()["prescription_id"]

        _, matching = _get_audit_event_for_prescription(admin_headers, client, prescription_id)
        check("exactly one audit event exists", len(matching) == 1, matching)
        event = matching[0]
        check("was_changed is True", event["event_data"]["was_changed"] is True, event)
        check("ai_proposal STILL shows the ORIGINAL AI proposal (20g), not the correction", event["event_data"]["ai_proposal"]["herbs"] == [{"name": "Ashwagandha", "grams": 20}], event)
        check("doctor_decision shows the doctor's corrected value (25g)", event["event_data"]["doctor_decision"]["herbs"] == [{"name": "Ashwagandha", "grams": 25}], event)


# ---------------------------------------------------------------------------
# 3. Manual fallback -- never fabricates an AI proposal
# ---------------------------------------------------------------------------

def test_manual_preview_never_fabricates_ai_proposal():
    print("\nTest: manual-preview confirmation -> MANUAL event, ai_proposal is null, never fabricated")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        clinic_id = f"audit_http_clinic_{secrets.token_hex(4)}"
        doctor_headers = _login_new_user(client, clinic_id=clinic_id, role="DOCTOR")
        admin_headers = _login_new_user(client, clinic_id=clinic_id, role="ADMIN")
        _set_stock("Ashwagandha", 5000, clinic_id=clinic_id)
        patient_name = _unique_patient_name("Audit HTTP Manual")

        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name), headers=doctor_headers)
        check("manual preview succeeds", preview_resp.status_code == 200, preview_resp.text)
        preview_id = preview_resp.json()["preview_id"]

        confirm_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=doctor_headers)
        check("confirm succeeds", confirm_resp.status_code == 200, confirm_resp.text)
        prescription_id = confirm_resp.json()["prescription_id"]

        _, matching = _get_audit_event_for_prescription(admin_headers, client, prescription_id)
        check("exactly one audit event exists for the manual prescription", len(matching) == 1, matching)
        event = matching[0]
        check("decision_source is MANUAL", event["event_data"]["decision_source"] == "MANUAL", event)
        check("ai_proposal is exactly null -- never fabricated for a manual entry", event["event_data"]["ai_proposal"] is None, event)
        check("was_changed is null (not false) -- there is no proposal to compare against", event["event_data"]["was_changed"] is None, event)
        check("doctor_decision reflects what the doctor actually entered", event["event_data"]["doctor_decision"] == {"dosha": "Vata", "herbs": [{"name": "Ashwagandha", "grams": 5}]}, event)


# ---------------------------------------------------------------------------
# 4-5. Authentication and RBAC
# ---------------------------------------------------------------------------

def test_audit_events_requires_authentication():
    print("\nTest: GET /audit/events is not accessible without a valid session")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        resp = client.get("/audit/events")
        check("no Authorization header at all -> 401", resp.status_code == 401, resp.text)


def test_audit_events_forbidden_for_doctor():
    print("\nTest: a DOCTOR's valid session is forbidden from GET /audit/events (ADMIN/AUDITOR only)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        doctor_headers = _login_new_user(client, role="DOCTOR")
        resp = client.get("/audit/events", headers=doctor_headers)
        check("DOCTOR gets 403, not 200 or 401", resp.status_code == 403, resp.text)


def test_audit_events_allowed_for_auditor():
    print("\nTest: an AUDITOR's valid session is allowed to call GET /audit/events")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        auditor_headers = _login_new_user(client, role="AUDITOR")
        resp = client.get("/audit/events", headers=auditor_headers)
        check("AUDITOR gets 200", resp.status_code == 200, resp.text)


# ---------------------------------------------------------------------------
# 6. Clinic isolation
# ---------------------------------------------------------------------------

def test_audit_events_scoped_by_clinic():
    print("\nTest: GET /audit/events returns only the calling ADMIN's own clinic's events, never another's")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        clinic_a = f"audit_http_isolation_a_{secrets.token_hex(4)}"
        clinic_b = f"audit_http_isolation_b_{secrets.token_hex(4)}"

        doctor_a = _login_new_user(client, clinic_id=clinic_a, role="DOCTOR")
        doctor_b = _login_new_user(client, clinic_id=clinic_b, role="DOCTOR")
        admin_a = _login_new_user(client, clinic_id=clinic_a, role="ADMIN")
        admin_b = _login_new_user(client, clinic_id=clinic_b, role="ADMIN")

        _set_stock("Ashwagandha", 5000, clinic_id=clinic_a)
        _set_stock("Ashwagandha", 5000, clinic_id=clinic_b)

        preview_b = client.post("/prescribe/manual-preview", json=_manual_preview_payload(_unique_patient_name("Audit Isolation B")), headers=doctor_b).json()["preview_id"]
        confirm_b = client.post("/prescribe/confirm", json={"preview_id": preview_b}, headers=doctor_b)
        prescription_b = confirm_b.json()["prescription_id"]

        events_seen_by_a = client.get("/audit/events", headers=admin_a).json()
        check(
            "clinic B's confirmed prescription does NOT appear in clinic A's ADMIN's audit events",
            all(e["prescription_id"] != prescription_b for e in events_seen_by_a["events"]), events_seen_by_a,
        )

        events_seen_by_b = client.get("/audit/events", headers=admin_b).json()
        check(
            "clinic B's confirmed prescription DOES appear in clinic B's own ADMIN's audit events",
            any(e["prescription_id"] == prescription_b for e in events_seen_by_b["events"]), events_seen_by_b,
        )

        forged_query_resp = client.get("/audit/events", params={"clinic_id": clinic_b}, headers=admin_a)
        check(
            "clinic A's ADMIN explicitly asking for clinic B's data via the query param is rejected with 403, "
            "never silently served",
            forged_query_resp.status_code == 403, forged_query_resp.text,
        )


# ---------------------------------------------------------------------------
# 7. Real SQLite tampering, against the full stack's LIVE integrity check
# ---------------------------------------------------------------------------

def test_chain_valid_reflects_real_sqlite_tampering():
    print("\nTest: GET /audit/events' chain_valid flips False->True after a REAL raw-sqlite3 tamper->restore cycle")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        clinic_id = f"audit_http_tamper_{secrets.token_hex(4)}"
        doctor_headers = _login_new_user(client, clinic_id=clinic_id, role="DOCTOR")
        admin_headers = _login_new_user(client, clinic_id=clinic_id, role="ADMIN")
        _set_stock("Ashwagandha", 5000, clinic_id=clinic_id)

        preview_id = client.post(
            "/prescribe/manual-preview", json=_manual_preview_payload(_unique_patient_name("Audit HTTP Tamper")), headers=doctor_headers
        ).json()["preview_id"]
        confirm_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=doctor_headers)
        check("confirm succeeds", confirm_resp.status_code == 200, confirm_resp.text)
        prescription_id = confirm_resp.json()["prescription_id"]

        before = client.get("/audit/events", headers=admin_headers).json()
        check("chain_valid is True before any tampering", before["chain_valid"] is True, before)

        audit_row = _audit_row_for_prescription(prescription_id)
        check("a real audit_events row exists for this prescription (raw sqlite3 read)", audit_row is not None)
        original_event_hash = audit_row["event_hash"]

        # Direct database mutation, bypassing this application entirely --
        # exactly the "attacker with raw SQLite file access" scenario A5
        # exists to make detectable. tests/test_audit_store.py already
        # exercises a full column-by-column matrix against
        # verify_audit_integrity() directly; THIS test proves the same
        # thing is true through the REAL, deployed HTTP endpoint's LIVE
        # check, run fresh on this exact request.
        _raw_update_audit_event(audit_row["event_id"], "event_hash", "0" * 64)

        after_tamper = client.get("/audit/events", headers=admin_headers).json()
        check("chain_valid flips to False after the direct database edit", after_tamper["chain_valid"] is False, after_tamper)
        check("integrity_violations is non-empty and names the tampered event", len(after_tamper["integrity_violations"]) > 0 and any(str(audit_row["event_id"]) in v for v in after_tamper["integrity_violations"]), after_tamper)

        # Restore, per the A5 spec's explicit instruction.
        _raw_update_audit_event(audit_row["event_id"], "event_hash", original_event_hash)
        after_restore = client.get("/audit/events", headers=admin_headers).json()
        check("chain_valid flips back to True after restoring the original value", after_restore["chain_valid"] is True, after_restore)
        check("integrity_violations is empty again after restoring", after_restore["integrity_violations"] == [], after_restore)


# ---------------------------------------------------------------------------
# 8. Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_confirms_each_get_exactly_one_valid_audit_event():
    print("\nTest: two concurrent /prescribe/confirm calls (different previews, same clinic) each get exactly one audit event")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        clinic_id = f"audit_http_concurrent_{secrets.token_hex(4)}"
        doctor_headers = _login_new_user(client, clinic_id=clinic_id, role="DOCTOR")
        admin_headers = _login_new_user(client, clinic_id=clinic_id, role="ADMIN")
        _set_stock("Ashwagandha", 5000, clinic_id=clinic_id)

        preview_1 = client.post(
            "/prescribe/manual-preview", json=_manual_preview_payload(_unique_patient_name("Audit Concurrent 1")), headers=doctor_headers
        ).json()["preview_id"]
        preview_2 = client.post(
            "/prescribe/manual-preview", json=_manual_preview_payload(_unique_patient_name("Audit Concurrent 2")), headers=doctor_headers
        ).json()["preview_id"]

        results = {}
        errors = {}
        barrier = threading.Barrier(2)

        def attempt(name: str, preview_id: str):
            try:
                barrier.wait()
                results[name] = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=doctor_headers)
            except Exception as e:
                errors[name] = e

        t1 = threading.Thread(target=attempt, args=("t1", preview_1))
        t2 = threading.Thread(target=attempt, args=("t2", preview_2))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        check("both threads completed without raising", not errors, errors)
        check("both concurrent confirms succeeded", all(r.status_code == 200 for r in results.values()), {n: r.text for n, r in results.items()})

        prescription_ids = {name: r.json()["prescription_id"] for name, r in results.items()}
        check("the two confirms produced two DIFFERENT prescriptions", len(set(prescription_ids.values())) == 2, prescription_ids)

        body = client.get("/audit/events", headers=admin_headers).json()
        for name, pid in prescription_ids.items():
            matching = [e for e in body["events"] if e["prescription_id"] == pid]
            check(f"[{name}] exactly one audit event exists for prescription_id={pid}", len(matching) == 1, matching)
        check("the resulting chain is valid after two concurrent inserts", body["chain_valid"] is True, body)


# ---------------------------------------------------------------------------
# 9. Audit-write failure rolls back the ENTIRE transaction -- never silent
# ---------------------------------------------------------------------------

def test_audit_write_failure_rolls_back_the_whole_confirm():
    print("\nTest: a forced audit-event write failure rolls back the prescription too -- never a silent loss")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        clinic_id = f"audit_http_write_failure_{secrets.token_hex(4)}"
        doctor_headers = _login_new_user(client, clinic_id=clinic_id, role="DOCTOR")
        _set_stock("Ashwagandha", 5000, clinic_id=clinic_id)
        patient_name = _unique_patient_name("Audit Write Failure")

        preview_id = client.post(
            "/prescribe/manual-preview", json=_manual_preview_payload(patient_name), headers=doctor_headers
        ).json()["preview_id"]

        # Monkeypatch the EXACT name backend/main.py's /prescribe/confirm
        # calls (create_audit_event, imported into main_module's own
        # namespace) to raise -- same established mocking convention this
        # file already uses for _with_fake_gemini above. This exercises
        # main.py's REAL exception-handling path for real, rather than
        # simulating what it "should" do.
        real_create_audit_event = main_module.create_audit_event

        def _failing_create_audit_event(*args, **kwargs):
            raise RuntimeError("simulated audit_event write failure (forced by test, not a real bug)")

        main_module.create_audit_event = _failing_create_audit_event
        try:
            confirm_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=doctor_headers)
        finally:
            main_module.create_audit_event = real_create_audit_event

        check("the confirm attempt fails with 500 when audit-event creation fails", confirm_resp.status_code == 500, confirm_resp.text)

        check(
            "NO prescription was created for this patient -- the whole transaction rolled back, "
            "not just the audit write",
            _count_prescriptions_for_patient(patient_name) == 0,
        )
        check(
            "the preview remains PENDING (re-confirmable), not silently consumed",
            _preview_status(preview_id) == "PENDING", _preview_status(preview_id),
        )

        # And, now that the patch is restored, the SAME preview really is
        # re-confirmable -- proving this isn't just "the response said 500"
        # but that the system is left in a genuinely retryable state.
        retry_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=doctor_headers)
        check("after the patch is restored, the SAME preview confirms successfully", retry_resp.status_code == 200, retry_resp.text)
        check("exactly one prescription now exists for this patient", _count_prescriptions_for_patient(patient_name) == 1)


_ALL_TESTS = [
    test_ai_assisted_unchanged_produces_correct_audit_event,
    test_ai_assisted_corrected_preserves_original_proposal,
    test_manual_preview_never_fabricates_ai_proposal,
    test_audit_events_requires_authentication,
    test_audit_events_forbidden_for_doctor,
    test_audit_events_allowed_for_auditor,
    test_audit_events_scoped_by_clinic,
    test_chain_valid_reflects_real_sqlite_tampering,
    test_concurrent_confirms_each_get_exactly_one_valid_audit_event,
    test_audit_write_failure_rolls_back_the_whole_confirm,
]


if __name__ == "__main__":
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
