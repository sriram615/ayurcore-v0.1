"""
RBAC + clinic isolation tests (Phase A, A3) -- covers backend/rbac.py's
require_role() and the clinic-scoping checks added to backend/main.py's
/prescribe/confirm and /audit/export.

This file is deliberately adversarial, per the explicit A3 instruction that
every permission be proven, not assumed. It is organized around the same
two independent questions backend/rbac.py's module docstring documents:

  1. ROLE -- can this ROLE call this ENDPOINT at all (independent of clinic)?
  2. CLINIC -- does this specific resource belong to THIS caller's clinic
     (independent of role)?

Specifically checks:
  - The documented PERMISSION_MATRIX dict matches what the code actually
    enforces at the HTTP layer, for every (role, endpoint) combination --
    not just the DOCTOR-can/ADMIN-can't positive cases, but that a 401
    (missing auth) always wins over a 403 (wrong role), and that an
    otherwise-valid-but-deactivated user is rejected before role is even
    considered.
  - A client-supplied clinic_id in a request body (PreviewRequest,
    ManualPreviewRequest) has ZERO effect on which clinic a preview is
    actually created under -- only the authenticated session's own,
    server-derived clinic_id does.
  - Cross-clinic /prescribe/confirm returns 404 (not 403, not 200).
  - Cross-clinic /audit/export (an explicit, mismatched clinic_id query
    param) returns 403; an absent clinic_id silently defaults to the
    caller's own clinic rather than erroring.
  - A role or clinic_id changed directly in the database takes effect on
    that user's very next request with their SAME, still-valid session
    token -- proving there is no cached/stale-privilege window, because
    backend/auth_service.py's resolve_session() re-joins users on every
    call.
  - The same live-reflection property survives a simulated app restart
    (a fresh app/TestClient instance, same raw token -- see
    tests/test_auth_endpoints.py's identical pattern).
  - Two authenticated DOCTORs in two DIFFERENT clinics operating
    concurrently never interfere with each other's stock or previews.

Like every other FastAPI-dependent test in this project, this file
REQUIRES MAC VERIFICATION (FastAPI + argon2-cffi installed) -- it is not
expected to run in the sandbox. See the A3 report's section G/I for which
parts of this milestone the sandbox actually exercised.

PYTEST COMPATIBILITY (fixed after a real Mac-verification incident): this
file's `check()` raises AssertionError immediately on a failed assertion,
rather than only printing a FAIL line and incrementing a counter. That
matters specifically because this file is security-critical and IS
collected by `pytest` (either directly -- `pytest tests/test_rbac.py` --
or as part of `pytest tests/`) as well as run standalone via
`python tests/test_rbac.py`. An earlier version of this helper never
raised anything; a failed check only ever showed up in the
`_passed`/`_failed` counters this file's own `__main__` block tallies and
exits non-zero on. pytest, when it collects and calls a `test_*` function
directly, never touches `__main__` at all -- it only reports a test as
FAILED if calling it raises. With the old non-raising `check()`, a
`test_*` function whose internal checks all failed would still RETURN
NORMALLY, and pytest would report it PASSED. This is exactly how a real
cross-clinic information-disclosure bug (an already-CONFIRMED
foreign-clinic preview returning 409 instead of 404 -- see
backend/preview_store.py's get_pending_preview_for_confirm docstring for
the full incident) was caught by `python tests/test_rbac.py` (49 passed,
1 failed) but silently reported as fully green by `pytest tests/ -v` (98
passed, 0 failed): pytest was never actually failing on this assertion --
the assertion itself was invisible to it. Fixed by making `check()` raise;
see this file's `__main__` block below for how direct execution now
handles that raise per-test-function, to keep running (and tallying) every
test function even after one of them raises, rather than aborting the
whole script partway through.

This is NOT a project-wide fix -- every other test file in this project
(test_golden.py, test_preview_lifecycle.py, test_audit_export.py,
test_auth_endpoints.py, test_manual_fallback.py, test_auth_service.py,
test_migrations.py, test_pii_sanitizer.py, test_pii_airgap.py,
test_copilot_tool_calling.py) still uses the OLD non-raising `check()`,
except tests/test_preview_store.py (fixed alongside this file, since it
now also carries a security-critical regression test for the same
cross-clinic bug at the storage layer). That means, for every other file,
`pytest tests/` genuinely is NOT a meaningful gate on any of their
individual assertions today -- it only proves each test FUNCTION ran to
completion without crashing for some unrelated reason (an import error, an
unhandled exception, etc.), never that its checks actually passed. That is
a real, project-wide gap, flagged here rather than silently left for
someone to discover the hard way again; fixing it everywhere is a
dedicated follow-up, out of scope for this security fix.

Run directly (no pytest required):
    python tests/test_rbac.py
Or under pytest (now a real, meaningful gate for THIS file specifically):
    pytest tests/test_rbac.py -v
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
    """Records AND enforces one assertion. See this file's module
    docstring ("PYTEST COMPATIBILITY") for exactly why this raises instead
    of only printing/counting -- a real cross-clinic information-
    disclosure bug was once invisible to `pytest tests/ -v` because an
    earlier version of this function never raised anything."""
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
    """Same convention as every other test file in this project (see
    tests/test_preview_lifecycle.py's identical helper) -- duplicated
    rather than shared, matching this project's existing convention of
    each test file being self-contained. `role` is the one thing this
    file's copy adds over the others', since A3 is the first milestone
    where role matters at all."""
    from backend.auth_service import create_user

    username = f"rbac_test_{secrets.token_hex(6)}"
    password = "rbac-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id, role=role)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _set_stock(herb_name: str, grams: int, clinic_id: str = "default_clinic"):
    conn = sqlite3.connect(DB_PATH)
    try:
        # UPDATE OR INSERT: most tests use the seeded default_clinic rows
        # (UPDATE hits them); the cross-clinic concurrency test below needs
        # a second clinic's inventory row, which the seed data has no entry
        # for at all.
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


def _get_preview_clinic_id(preview_id: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT clinic_id FROM prescription_previews WHERE preview_id = ?", (preview_id,)
        ).fetchone()
        return row["clinic_id"] if row else None
    finally:
        conn.close()


def _backdate_preview_expiry(preview_id: str, seconds_ago: int = 60):
    """Test-only helper (same pattern as tests/test_preview_store.py's
    _backdate_expiry): forces a preview into EXPIRED-by-live-comparison
    without waiting 30 real minutes -- used by
    test_cross_clinic_confirm_hides_every_foreign_lifecycle_state below to
    prove the EXPIRED case specifically, not just PENDING and CONFIRMED."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE prescription_previews SET expires_at = datetime('now', ?) WHERE preview_id = ?",
            (f"-{seconds_ago} seconds", preview_id),
        )
        conn.commit()
    finally:
        conn.close()


def _update_user(username: str, **fields):
    """Directly mutates a user's row -- role and/or clinic_id -- to prove
    live-reflection: the CHANGE happens here, out-of-band from any session
    token, and the assertion in the calling test is always about what the
    user's EXISTING, already-issued token does on its NEXT request."""
    conn = sqlite3.connect(DB_PATH)
    try:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE users SET {set_clause} WHERE username = ?", (*fields.values(), username))
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


def _manual_preview_payload(patient_name="RBAC Test Patient", herb="Ashwagandha", grams=5, clinic_id=None):
    payload = {
        "patient_name": patient_name,
        "dosha_imbalance": "Vata",
        "prescribed_herbs": [{"name": herb, "grams": grams}],
    }
    if clinic_id is not None:
        payload["clinic_id"] = clinic_id
    return payload


# ---------------------------------------------------------------------------
# 1. Documented matrix vs. enforced code
# ---------------------------------------------------------------------------

def test_documented_permission_matrix_has_no_undocumented_roles():
    print("\nTest: PERMISSION_MATRIX only ever references the three real roles")
    from backend.rbac import PERMISSION_MATRIX, ALL_ROLES

    for action, allowed in PERMISSION_MATRIX.items():
        check(f"PERMISSION_MATRIX[{action!r}] only contains real roles", allowed.issubset(ALL_ROLES), allowed)


# ---------------------------------------------------------------------------
# 2. Role gating on the three prescribe endpoints -- DOCTOR-only
# ---------------------------------------------------------------------------

def test_doctor_can_use_all_three_prescribe_endpoints():
    print("\nTest: DOCTOR (positive baseline) can call preview, manual-preview, and confirm, all in their own clinic")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client, clinic_id="default_clinic", role="DOCTOR")
        _set_stock("Ashwagandha", 5000)

        restore = _with_fake_gemini({"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]})
        try:
            preview_resp = client.post("/prescribe/preview", json={"patient_name": "RBAC Doctor Test", "notes": "notes"}, headers=headers)
        finally:
            restore()
        check("DOCTOR can call /prescribe/preview", preview_resp.status_code == 200, preview_resp.text)

        manual_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(), headers=headers)
        check("DOCTOR can call /prescribe/manual-preview", manual_resp.status_code == 200, manual_resp.text)
        if manual_resp.status_code == 200:
            confirm_resp = client.post("/prescribe/confirm", json={"preview_id": manual_resp.json()["preview_id"]}, headers=headers)
            check("DOCTOR can call /prescribe/confirm on their own preview", confirm_resp.status_code == 200, confirm_resp.text)


def test_admin_forbidden_from_all_three_prescribe_endpoints():
    print("\nTest: ADMIN gets 403 (not 200, not 401) on all three prescribe endpoints")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client, clinic_id="default_clinic", role="ADMIN")

        restore = _with_fake_gemini({"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]})
        try:
            preview_resp = client.post("/prescribe/preview", json={"patient_name": "RBAC Admin Test", "notes": "notes"}, headers=headers)
        finally:
            restore()
        check("ADMIN is forbidden (403) from /prescribe/preview", preview_resp.status_code == 403, preview_resp.text)

        manual_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(), headers=headers)
        check("ADMIN is forbidden (403) from /prescribe/manual-preview", manual_resp.status_code == 403, manual_resp.text)

        confirm_resp = client.post("/prescribe/confirm", json={"preview_id": "does-not-matter-role-checked-first"}, headers=headers)
        check(
            "ADMIN is forbidden (403) from /prescribe/confirm -- rejected on ROLE before the (nonexistent) "
            "preview_id is even looked up",
            confirm_resp.status_code == 403, confirm_resp.text,
        )


def test_auditor_forbidden_from_all_three_prescribe_endpoints():
    print("\nTest: AUDITOR gets 403 on all three prescribe endpoints, same as ADMIN")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client, clinic_id="default_clinic", role="AUDITOR")

        preview_resp = client.post("/prescribe/preview", json={"patient_name": "RBAC Auditor Test", "notes": "notes"}, headers=headers)
        check("AUDITOR is forbidden (403) from /prescribe/preview", preview_resp.status_code == 403, preview_resp.text)

        manual_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(), headers=headers)
        check("AUDITOR is forbidden (403) from /prescribe/manual-preview", manual_resp.status_code == 403, manual_resp.text)

        confirm_resp = client.post("/prescribe/confirm", json={"preview_id": "does-not-matter-role-checked-first"}, headers=headers)
        check("AUDITOR is forbidden (403) from /prescribe/confirm", confirm_resp.status_code == 403, confirm_resp.text)


# ---------------------------------------------------------------------------
# 3. Role gating on /audit/export -- ADMIN + AUDITOR only
# ---------------------------------------------------------------------------

def test_admin_and_auditor_can_call_audit_export():
    print("\nTest: both ADMIN and AUDITOR can call /audit/export (200) for their own clinic")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        admin_headers = _login_new_user(client, clinic_id="default_clinic", role="ADMIN")
        auditor_headers = _login_new_user(client, clinic_id="default_clinic", role="AUDITOR")

        admin_resp = client.get("/audit/export", headers=admin_headers)
        check("ADMIN can call /audit/export", admin_resp.status_code == 200, admin_resp.text)
        check("ADMIN's export is scoped to their own clinic_id", admin_resp.json().get("clinic_id") == "default_clinic", admin_resp.json())

        auditor_resp = client.get("/audit/export", headers=auditor_headers)
        check("AUDITOR can call /audit/export", auditor_resp.status_code == 200, auditor_resp.text)


def test_doctor_forbidden_from_audit_export():
    print("\nTest: DOCTOR gets 403 on /audit/export -- auditing is not a clinical role's job")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client, clinic_id="default_clinic", role="DOCTOR")
        resp = client.get("/audit/export", headers=headers)
        check("DOCTOR is forbidden (403) from /audit/export", resp.status_code == 403, resp.text)


# ---------------------------------------------------------------------------
# 4. 401 (auth) always wins over 403 (role) -- and inactive users are
#    rejected before role is even considered.
# ---------------------------------------------------------------------------

def test_missing_auth_returns_401_not_403_on_role_gated_endpoints():
    print("\nTest: an unauthenticated caller gets 401 on role-gated endpoints, never a role-revealing 403")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        resp = client.get("/audit/export")
        check("no Authorization header on /audit/export returns 401, not 403", resp.status_code == 401, resp.text)

        resp2 = client.post("/prescribe/preview", json={"patient_name": "No Auth", "notes": "notes"})
        check("no Authorization header on /prescribe/preview returns 401, not 403", resp2.status_code == 401, resp2.text)


def test_inactive_user_rejected_even_with_a_permitted_role():
    print("\nTest: a session whose user is deactivated mid-session is rejected (401), even though their role would otherwise be permitted")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        username = f"rbac_inactive_test_{secrets.token_hex(6)}"
        password = "rbac-inactive-test-password"
        from backend.auth_service import create_user
        create_user(username, password, clinic_id="default_clinic", role="ADMIN")
        token = client.post("/auth/login", json={"username": username, "password": password}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        before = client.get("/audit/export", headers=headers)
        check("the ADMIN's token works before deactivation", before.status_code == 200, before.text)

        _update_user(username, is_active=0)

        after = client.get("/audit/export", headers=headers)
        check(
            "the SAME token, after the user is deactivated directly in the database, is rejected with 401 -- "
            "not 403 (role is never even reached; require_auth's resolve_session() already rejects a "
            "deactivated user's session outright)",
            after.status_code == 401, after.text,
        )


# ---------------------------------------------------------------------------
# 5. A client-supplied clinic_id in a request body has zero effect.
# ---------------------------------------------------------------------------

def test_forged_clinic_id_in_preview_request_body_is_ignored():
    print("\nTest: a client-supplied clinic_id in the /prescribe/preview and /manual-preview request body has NO effect on which clinic the preview is created under")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client, clinic_id="forge_test_real_clinic", role="DOCTOR")
        _set_stock("Ashwagandha", 5000)

        restore = _with_fake_gemini({"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]})
        try:
            preview_resp = client.post(
                "/prescribe/preview",
                json={"patient_name": "Forged Clinic Test", "notes": "notes", "clinic_id": "forge_test_attacker_clinic"},
                headers=headers,
            )
        finally:
            restore()
        check("preview with a forged clinic_id in the body still succeeds (the field isn't rejected, just ignored)", preview_resp.status_code == 200, preview_resp.text)
        if preview_resp.status_code == 200:
            actual_clinic = _get_preview_clinic_id(preview_resp.json()["preview_id"])
            check(
                "the preview's REAL persisted clinic_id is the authenticated user's own clinic, NOT the forged one from the request body",
                actual_clinic == "forge_test_real_clinic", actual_clinic,
            )

        manual_resp = client.post(
            "/prescribe/manual-preview",
            json=_manual_preview_payload(clinic_id="forge_test_attacker_clinic"),
            headers=headers,
        )
        check("manual-preview with a forged clinic_id in the body still succeeds", manual_resp.status_code == 200, manual_resp.text)
        if manual_resp.status_code == 200:
            actual_clinic = _get_preview_clinic_id(manual_resp.json()["preview_id"])
            check(
                "the manual preview's REAL persisted clinic_id is likewise the authenticated user's own clinic",
                actual_clinic == "forge_test_real_clinic", actual_clinic,
            )


# ---------------------------------------------------------------------------
# 6. Cross-clinic access -- /prescribe/confirm (404) and /audit/export (403)
# ---------------------------------------------------------------------------

def test_cross_clinic_confirm_returns_404():
    print("\nTest: a DOCTOR in clinic B attempting to confirm clinic A's preview gets 404 (not 403, not 200)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers_clinic_a = _login_new_user(client, clinic_id="rbac_cross_clinic_a", role="DOCTOR")
        headers_clinic_b = _login_new_user(client, clinic_id="rbac_cross_clinic_b", role="DOCTOR")
        _set_stock("Ashwagandha", 5000, clinic_id="rbac_cross_clinic_a")

        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(), headers=headers_clinic_a)
        check("clinic A's DOCTOR can create a preview", preview_resp.status_code == 200, preview_resp.text)
        if preview_resp.status_code != 200:
            return
        preview_id = preview_resp.json()["preview_id"]

        cross_confirm = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers_clinic_b)
        check(
            "a DIFFERENT clinic's DOCTOR confirming clinic A's preview gets 404 -- treated as "
            "'does not exist' from clinic B's point of view, distinct from the SAME-clinic "
            "different-session 403 tests/test_preview_lifecycle.py already covers",
            cross_confirm.status_code == 404, cross_confirm.text,
        )

        same_clinic_confirm = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers_clinic_a)
        check("clinic A's own DOCTOR can still confirm it normally after the cross-clinic attempt was rejected", same_clinic_confirm.status_code == 200, same_clinic_confirm.text)


def test_cross_clinic_confirm_hides_every_foreign_lifecycle_state():
    """SECURITY REGRESSION TEST (Phase A, A3 Mac verification finding):
    a foreign-clinic preview_id must return the SAME 404 regardless of
    whether that preview is PENDING, EXPIRED, or CONFIRMED -- the caller
    must not be able to learn a foreign preview's existence OR its
    lifecycle state via the response's status code or body.

    An earlier version of /prescribe/confirm checked clinic ownership only
    AFTER calling backend.preview_store.get_pending_preview_for_confirm()
    with no clinic argument -- which meant an already-CONFIRMED
    foreign-clinic preview raised PreviewAlreadyConfirmedError (-> 409,
    "has already been confirmed") before clinic_id was ever compared,
    leaking both the foreign preview's existence and its state. Fixed by
    moving the clinic-ownership check inside
    get_pending_preview_for_confirm() itself, BEFORE it examines status at
    all -- see that function's docstring in backend/preview_store.py for
    the full incident. This test proves the fix at the real HTTP layer,
    across all three lifecycle states, in one place; the storage layer
    (no FastAPI needed, sandbox-runnable) has its own dedicated regression
    test in tests/test_preview_store.py.
    """
    print("\nTest: a foreign-clinic preview returns 404 whether it is PENDING, EXPIRED, or CONFIRMED -- never the state-revealing 409")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers_home = _login_new_user(client, clinic_id="rbac_lifecycle_home", role="DOCTOR")
        headers_foreign = _login_new_user(client, clinic_id="rbac_lifecycle_foreign", role="DOCTOR")
        _set_stock("Ashwagandha", 5000, clinic_id="rbac_lifecycle_foreign")

        # --- Foreign-clinic PENDING ---
        pending_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name="Lifecycle PENDING"), headers=headers_foreign)
        check("setup: foreign clinic's PENDING preview is created", pending_resp.status_code == 200, pending_resp.text)
        pending_id = pending_resp.json()["preview_id"]
        cross_pending = client.post("/prescribe/confirm", json={"preview_id": pending_id}, headers=headers_home)
        check("foreign-clinic PENDING preview: home clinic gets 404", cross_pending.status_code == 404, cross_pending.text)

        # --- Foreign-clinic EXPIRED ---
        expired_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name="Lifecycle EXPIRED"), headers=headers_foreign)
        expired_id = expired_resp.json()["preview_id"]
        _backdate_preview_expiry(expired_id, seconds_ago=60)
        cross_expired = client.post("/prescribe/confirm", json={"preview_id": expired_id}, headers=headers_home)
        check(
            "foreign-clinic EXPIRED preview: home clinic gets 404 (NOT distinguishable from the PENDING case above -- "
            "same status, same message)",
            cross_expired.status_code == 404, cross_expired.text,
        )
        check(
            "the EXPIRED and PENDING cross-clinic responses are byte-for-byte identical -- no signal leaks "
            "about which lifecycle state the foreign preview was actually in",
            cross_expired.json() == cross_pending.json(), (cross_expired.json(), cross_pending.json()),
        )

        # --- Foreign-clinic CONFIRMED -- the exact case the Mac run caught. ---
        confirmed_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name="Lifecycle CONFIRMED"), headers=headers_foreign)
        confirmed_id = confirmed_resp.json()["preview_id"]
        own_confirm = client.post("/prescribe/confirm", json={"preview_id": confirmed_id}, headers=headers_foreign)
        check("setup: foreign clinic confirms its own preview successfully first", own_confirm.status_code == 200, own_confirm.text)

        cross_confirmed = client.post("/prescribe/confirm", json={"preview_id": confirmed_id}, headers=headers_home)
        check(
            "THE REGRESSION CASE: foreign-clinic CONFIRMED preview: home clinic gets 404, NOT 409 -- the "
            "caller must not learn this preview exists and was already confirmed",
            cross_confirmed.status_code == 404, cross_confirmed.text,
        )
        check(
            "the CONFIRMED cross-clinic response is ALSO byte-for-byte identical to the PENDING one -- no "
            "'has already been confirmed' text, no distinguishing detail at all",
            cross_confirmed.json() == cross_pending.json(), (cross_confirmed.json(), cross_pending.json()),
        )

        # --- Contrast: the SAME preview, confirmed a SECOND time by its
        # OWN clinic, must still correctly get 409 -- the fix must not
        # have overcorrected into hiding a legitimate same-clinic
        # already-confirmed response behind 404. ---
        same_clinic_replay = client.post("/prescribe/confirm", json={"preview_id": confirmed_id}, headers=headers_foreign)
        check(
            "CONTRAST: the SAME preview, re-confirmed by its OWN (correct) clinic, still correctly gets 409 "
            "-- the fix did not overcorrect into a blanket 404 for every already-confirmed preview",
            same_clinic_replay.status_code == 409, same_clinic_replay.text,
        )

        # --- Baseline: a genuinely unknown preview_id also gets the SAME 404. ---
        unknown_resp = client.post("/prescribe/confirm", json={"preview_id": "never-issued-lifecycle-test-id"}, headers=headers_home)
        check("a genuinely unknown preview_id also gets 404", unknown_resp.status_code == 404, unknown_resp.text)
        check(
            "the unknown-preview_id response is byte-for-byte identical to every cross-clinic response above -- "
            "'foreign clinic' and 'never existed' are provably indistinguishable to the caller",
            unknown_resp.json() == cross_pending.json(), (unknown_resp.json(), cross_pending.json()),
        )


def test_cross_clinic_audit_export_returns_403():
    print("\nTest: an ADMIN explicitly requesting a DIFFERENT clinic's audit export gets 403")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        admin_headers = _login_new_user(client, clinic_id="rbac_audit_clinic_a", role="ADMIN")

        cross_resp = client.get("/audit/export", params={"clinic_id": "rbac_audit_clinic_b"}, headers=admin_headers)
        check(
            "requesting another clinic's audit data via the clinic_id query param is rejected with 403, "
            "not silently substituted or silently allowed",
            cross_resp.status_code == 403, cross_resp.text,
        )

        own_resp = client.get("/audit/export", params={"clinic_id": "rbac_audit_clinic_a"}, headers=admin_headers)
        check("explicitly requesting one's OWN clinic_id (matching auth.clinic_id) still succeeds", own_resp.status_code == 200, own_resp.text)

        omitted_resp = client.get("/audit/export", headers=admin_headers)
        check("omitting clinic_id entirely also succeeds and defaults to the caller's own clinic", omitted_resp.status_code == 200, omitted_resp.text)
        check("the omitted-clinic_id response is scoped to the caller's own clinic", omitted_resp.json().get("clinic_id") == "rbac_audit_clinic_a", omitted_resp.json())


# ---------------------------------------------------------------------------
# 7. Role escalation attempts
# ---------------------------------------------------------------------------

def test_role_escalation_attempt_via_prescribe_is_rejected():
    print("\nTest: a non-DOCTOR role attempting to prescribe is rejected (covered above, re-asserted here as the explicit 'role escalation' case)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        for role in ("ADMIN", "AUDITOR"):
            headers = _login_new_user(client, clinic_id="default_clinic", role=role)
            resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(), headers=headers)
            check(f"{role} attempting to prescribe (a DOCTOR-only action) is rejected with 403", resp.status_code == 403, resp.text)


def test_role_escalation_attempt_via_audit_export_is_rejected():
    print("\nTest: DOCTOR attempting audit export (an ADMIN/AUDITOR-only action) is rejected (re-asserted as the explicit 'role escalation' case)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client, clinic_id="default_clinic", role="DOCTOR")
        resp = client.get("/audit/export", headers=headers)
        check("DOCTOR attempting audit export is rejected with 403", resp.status_code == 403, resp.text)


# ---------------------------------------------------------------------------
# 8. Live-reflection: a role or clinic change made directly in the database
#    takes effect on the user's very NEXT request with their SAME,
#    still-valid session token -- no stale-privilege window.
# ---------------------------------------------------------------------------

def test_role_change_in_db_takes_effect_on_next_request_same_token():
    print("\nTest: a role change made directly in the database takes effect on the SAME session token's very next request")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        username = f"rbac_role_escalation_test_{secrets.token_hex(6)}"
        password = "rbac-role-escalation-test-password"
        from backend.auth_service import create_user
        create_user(username, password, clinic_id="default_clinic", role="DOCTOR")
        token = client.post("/auth/login", json={"username": username, "password": password}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        _set_stock("Ashwagandha", 5000)

        before = client.post("/prescribe/manual-preview", json=_manual_preview_payload(), headers=headers)
        check("before the role change, this DOCTOR can prescribe", before.status_code == 200, before.text)

        before_audit = client.get("/audit/export", headers=headers)
        check("before the role change, this DOCTOR is forbidden from audit export", before_audit.status_code == 403, before_audit.text)

        # The user's role is changed directly in the database -- NOT via a
        # new login, NOT via a new token. The SAME already-issued,
        # still-valid session is reused below.
        _update_user(username, role="ADMIN")

        after = client.post("/prescribe/manual-preview", json=_manual_preview_payload(), headers=headers)
        check(
            "the SAME token, on its very next request after the DB-side role change, is now REJECTED "
            "from prescribing (403) -- the privilege change took effect immediately, with no re-login needed",
            after.status_code == 403, after.text,
        )

        after_audit = client.get("/audit/export", headers=headers)
        check(
            "the SAME token can now call audit export (200) -- the NEW role is live immediately, proving "
            "resolve_session()'s live JOIN, not a cached/stale role from token creation time",
            after_audit.status_code == 200, after_audit.text,
        )


def test_clinic_change_in_db_takes_effect_on_next_request_same_token():
    print("\nTest: a clinic_id change made directly in the database takes effect on the SAME session token's very next request")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        username = f"rbac_clinic_change_test_{secrets.token_hex(6)}"
        password = "rbac-clinic-change-test-password"
        from backend.auth_service import create_user
        create_user(username, password, clinic_id="rbac_clinic_change_old", role="DOCTOR")
        token = client.post("/auth/login", json={"username": username, "password": password}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        _set_stock("Ashwagandha", 5000, clinic_id="rbac_clinic_change_old")

        old_clinic_preview = client.post("/prescribe/manual-preview", json=_manual_preview_payload(), headers=headers)
        check("preview created while still in the OLD clinic succeeds", old_clinic_preview.status_code == 200, old_clinic_preview.text)
        old_preview_id = old_clinic_preview.json()["preview_id"]

        # Clinic changed directly in the database -- same token reused below.
        _update_user(username, clinic_id="rbac_clinic_change_new")

        stale_confirm = client.post("/prescribe/confirm", json={"preview_id": old_preview_id}, headers=headers)
        check(
            "the SAME token, now resolved to a DIFFERENT clinic, can no longer confirm the preview it "
            "created under its OLD clinic -- 404, treated as belonging to a clinic it no longer has access to",
            stale_confirm.status_code == 404, stale_confirm.text,
        )

        _set_stock("Ashwagandha", 5000, clinic_id="rbac_clinic_change_new")
        new_clinic_preview = client.post("/prescribe/manual-preview", json=_manual_preview_payload(), headers=headers)
        check("the SAME token creates its NEXT preview under the NEW clinic automatically", new_clinic_preview.status_code == 200, new_clinic_preview.text)
        if new_clinic_preview.status_code == 200:
            actual_clinic = _get_preview_clinic_id(new_clinic_preview.json()["preview_id"])
            check("the new preview's persisted clinic_id is the NEW clinic, live-reflected with no re-login", actual_clinic == "rbac_clinic_change_new", actual_clinic)


# ---------------------------------------------------------------------------
# 9. Session/role/clinic persistence across a simulated app restart.
# ---------------------------------------------------------------------------

def test_role_and_clinic_survive_a_simulated_app_restart():
    print("\nTest: role and clinic-scoped authorization both survive a simulated app restart, same token")
    import importlib
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user

    username = f"rbac_restart_test_{secrets.token_hex(6)}"
    password = "rbac-restart-test-password"

    with TestClient(main_module.app) as client1:
        create_user(username, password, clinic_id="rbac_restart_clinic", role="AUDITOR")
        token = client1.post("/auth/login", json={"username": username, "password": password}).json()["token"]

    # client1's lifecycle is fully torn down -- reload backend.main and
    # start a second, independent TestClient/app lifecycle, exactly like
    # tests/test_auth_endpoints.py's test_valid_session_survives_a_simulated_app_restart.
    main_module_reloaded = importlib.reload(main_module)
    with TestClient(main_module_reloaded.app) as client2:
        headers = {"Authorization": f"Bearer {token}"}
        resp = client2.get("/audit/export", params={"clinic_id": "rbac_restart_clinic"}, headers=headers)
        check(
            "an AUDITOR's token, created before a simulated restart, still authorizes correctly "
            "(200, correct role AND correct clinic scope) against a freshly-started app instance",
            resp.status_code == 200, resp.text,
        )

        wrong_clinic_resp = client2.get("/audit/export", params={"clinic_id": "some_other_clinic_entirely"}, headers=headers)
        check(
            "the same post-restart token is still correctly REJECTED (403) from a clinic that isn't its own",
            wrong_clinic_resp.status_code == 403, wrong_clinic_resp.text,
        )


# ---------------------------------------------------------------------------
# 10. Concurrent operations across two different clinics -- no interference.
# ---------------------------------------------------------------------------

def test_concurrent_doctors_in_different_clinics_do_not_interfere():
    print("\nTest: two DOCTORs in two DIFFERENT clinics, confirming concurrently, never cross-contaminate stock or previews")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers_a = _login_new_user(client, clinic_id="rbac_concurrent_clinic_a", role="DOCTOR")
        headers_b = _login_new_user(client, clinic_id="rbac_concurrent_clinic_b", role="DOCTOR")
        _set_stock("Ashwagandha", 1000, clinic_id="rbac_concurrent_clinic_a")
        _set_stock("Ashwagandha", 2000, clinic_id="rbac_concurrent_clinic_b")

        preview_a = client.post("/prescribe/manual-preview", json=_manual_preview_payload(grams=100), headers=headers_a)
        preview_b = client.post("/prescribe/manual-preview", json=_manual_preview_payload(grams=200), headers=headers_b)
        check("clinic A's preview succeeds", preview_a.status_code == 200, preview_a.text)
        check("clinic B's preview succeeds", preview_b.status_code == 200, preview_b.text)
        if preview_a.status_code != 200 or preview_b.status_code != 200:
            return

        responses = {}

        def confirm(name, headers, preview_id):
            responses[name] = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)

        t_a = threading.Thread(target=confirm, args=("a", headers_a, preview_a.json()["preview_id"]))
        t_b = threading.Thread(target=confirm, args=("b", headers_b, preview_b.json()["preview_id"]))
        t_a.start(); t_b.start()
        t_a.join(); t_b.join()

        check("clinic A's concurrent confirm succeeded", responses["a"].status_code == 200, responses["a"].text)
        check("clinic B's concurrent confirm succeeded", responses["b"].status_code == 200, responses["b"].text)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        stock_a = conn.execute(
            "SELECT stock_grams FROM inventory WHERE herb_name = ? AND clinic_id = ?", ("Ashwagandha", "rbac_concurrent_clinic_a")
        ).fetchone()
        stock_b = conn.execute(
            "SELECT stock_grams FROM inventory WHERE herb_name = ? AND clinic_id = ?", ("Ashwagandha", "rbac_concurrent_clinic_b")
        ).fetchone()
        conn.close()
        check("clinic A's stock reflects ONLY its own 100g deduction (1000 -> 900), unaffected by clinic B's concurrent confirm", stock_a is not None and stock_a["stock_grams"] == 900, dict(stock_a) if stock_a else None)
        check("clinic B's stock reflects ONLY its own 200g deduction (2000 -> 1800), unaffected by clinic A's concurrent confirm", stock_b is not None and stock_b["stock_grams"] == 1800, dict(stock_b) if stock_b else None)

        # And clinic A cannot see/confirm clinic B's preview, or vice versa.
        cross = client.post("/prescribe/confirm", json={"preview_id": preview_b.json()["preview_id"]}, headers=headers_a)
        check("clinic A's DOCTOR cannot touch clinic B's (already-confirmed) preview either -- 404, not 409", cross.status_code == 404, cross.text)


_ALL_TESTS = [
    test_documented_permission_matrix_has_no_undocumented_roles,
    test_doctor_can_use_all_three_prescribe_endpoints,
    test_admin_forbidden_from_all_three_prescribe_endpoints,
    test_auditor_forbidden_from_all_three_prescribe_endpoints,
    test_admin_and_auditor_can_call_audit_export,
    test_doctor_forbidden_from_audit_export,
    test_missing_auth_returns_401_not_403_on_role_gated_endpoints,
    test_inactive_user_rejected_even_with_a_permitted_role,
    test_forged_clinic_id_in_preview_request_body_is_ignored,
    test_cross_clinic_confirm_returns_404,
    test_cross_clinic_confirm_hides_every_foreign_lifecycle_state,
    test_cross_clinic_audit_export_returns_403,
    test_role_escalation_attempt_via_prescribe_is_rejected,
    test_role_escalation_attempt_via_audit_export_is_rejected,
    test_role_change_in_db_takes_effect_on_next_request_same_token,
    test_clinic_change_in_db_takes_effect_on_next_request_same_token,
    test_role_and_clinic_survive_a_simulated_app_restart,
    test_concurrent_doctors_in_different_clinics_do_not_interfere,
]


if __name__ == "__main__":
    print("--- RBAC + Clinic Isolation Tests (Phase A, A3) ---")
    # check() now raises AssertionError on a failed assertion (see this
    # file's module docstring, "PYTEST COMPATIBILITY", for why) -- so each
    # test function is called through this loop, which catches that raise
    # per-function and moves on to the next one, rather than a flat list
    # of bare calls that would abort the whole script at the FIRST failing
    # assertion anywhere. This keeps direct execution's long-standing
    # contract ("run every test function, print a full tally at the end")
    # intact while making every individual check() a real, hard assertion
    # under pytest too.
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
