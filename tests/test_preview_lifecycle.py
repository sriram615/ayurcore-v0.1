"""
Preview/confirm lifecycle tests -- written in response to an external
architecture review that caught a real bug: /confirm used to pop a preview
out of the pending store the moment it was looked up, BEFORE knowing
whether the transaction below would actually succeed. That meant a doctor
who hit a 409 (insufficient stock) or 400 (unknown herb correction) lost
the preview and had to re-run /preview (and pay for a fresh AI call) just
to retry. Fixed in backend/main.py: a preview is only ever consumed after
a SUCCESSFUL confirm.

This file specifically covers the six points from that review:
  1. preview_id is an opaque, server-side-only lookup key -- nothing about
     patient identity, clinic_id, or the AI's proposal is encoded in it.
     (Exercised implicitly by every test below reading real data back.)
  2. corrections logging is best-effort AFTER commit, never able to block
     or invalidate a prescription (see test_pii_airgap.py's corrections
     tests for direct coverage; not repeated here).
  3. the ORIGINAL AI proposal is what corrections.ai_* records, even when
     the doctor corrects it (see test_pii_airgap.py).
  4. AI-proposed and doctor-corrected herb names resolve through the same
     normalize_herb() boundary -- test_doctor_correction_uses_same_canonical_resolver.
  5. doctor corrections get the same Pydantic grams constraints as the AI's
     own output -- test_confirm_rejects_invalid_grams_same_as_preview.
  6. a preview is single-use after a SUCCESSFUL confirm (409 on replay),
     but stays usable after a FAILED one (retry without a fresh /preview) --
     test_single_use_confirm_returns_409_on_replay,
     test_failed_confirm_leaves_preview_retriable.
  Plus, since Phase A, A2 (Real Authentication) made session binding a real,
  enforced control instead of an inert placeholder: the full critical
  create/mismatch/confirm/logout flow (test_session_binding_full_flow),
  missing/malformed/forged Authorization handling
  (test_missing_or_malformed_authorization_is_rejected), and the
  defensive backward-compatibility path for a preview with no recorded
  session (test_preexisting_null_session_preview_accepts_any_authenticated_confirm).

Run directly (no pytest required):
    python tests/test_preview_lifecycle.py
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
    Each call creates a genuinely distinct user/session, which is exactly
    what this file's session-binding tests need."""
    from backend.auth_service import create_user

    username = f"lifecycle_test_{secrets.token_hex(6)}"
    password = "lifecycle-test-password-not-a-real-credential"
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


class _FakeModels:
    def __init__(self, canned_response: dict):
        self._canned_response = canned_response

    def generate_content(self, model, contents, config):
        class _FakeResponse:
            pass
        resp = _FakeResponse()
        resp.text = json.dumps(self._canned_response)
        return resp


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


def _preview(client, canned_response, headers, patient_name="Lifecycle Test Patient", notes="notes"):
    restore = _with_fake_gemini(canned_response)
    try:
        payload = {"patient_name": patient_name, "notes": notes}
        return client.post("/prescribe/preview", json=payload, headers=headers)
    finally:
        restore()


def test_single_use_confirm_returns_409_on_replay():
    print("\nTest: a preview can only be confirmed once (409 on replay)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    # Phase A, A1 fix: must be a context manager for @app.on_event("startup")
    # (which runs pending migrations) to actually fire -- see test_golden.py's
    # run_tests() for the full explanation.
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        preview_resp = _preview(client, {"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}]}, headers)
        check("preview succeeded", preview_resp.status_code == 200, preview_resp.text)
        if preview_resp.status_code != 200:
            return
        preview_id = preview_resp.json()["preview_id"]

        first_confirm = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check("first confirm succeeds (200)", first_confirm.status_code == 200, first_confirm.text)

        second_confirm = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check("replaying the same preview_id returns 409, not a second prescription", second_confirm.status_code == 409, second_confirm.text)


def test_failed_confirm_leaves_preview_retriable():
    print("\nTest: a FAILED confirm (insufficient stock) leaves the preview usable for a retry")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Brahmi", 50)  # deliberately too little for the AI's proposed 9000g
        preview_resp = _preview(client, {"dosha_imbalance": "Pitta", "prescribed_herbs": [{"name": "Brahmi", "grams": 9000}]}, headers)
        check("preview succeeded", preview_resp.status_code == 200, preview_resp.text)
        if preview_resp.status_code != 200:
            return
        preview_id = preview_resp.json()["preview_id"]

        failed_confirm = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check("first confirm fails with 409 (insufficient stock)", failed_confirm.status_code == 409, failed_confirm.text)

        # Doctor corrects the amount down to something in stock, using the SAME preview_id --
        # this must still work; the failed attempt above must not have consumed it.
        _set_stock("Brahmi", 50)
        retry_confirm = client.post("/prescribe/confirm", json={
            "preview_id": preview_id,
            "prescribed_herbs": [{"name": "Brahmi", "grams": 20}],
        }, headers=headers)
        check("retrying the SAME preview_id with a corrected amount succeeds (200)", retry_confirm.status_code == 200, retry_confirm.text)

        # And now that it succeeded, the preview is finally consumed.
        third_confirm = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check("after the retry succeeds, the preview is consumed (409 on further replay)", third_confirm.status_code == 409, third_confirm.text)


def test_session_binding_full_flow():
    """Phase A, A2 -- the "critical" preview/session-binding flow, exactly
    as specified: User A logs in, creates a preview; User B (a genuinely
    different authenticated user, not a guessed/forged header string)
    attempts to confirm it and is rejected (403); User A confirms it
    successfully (200); User A then logs out; the SAME now-revoked
    token attempting to act again is rejected -- and rejected with 401
    (require_auth's dependency catches a revoked session before the
    handler's own 403 session-mismatch check is ever reached), not a
    silent success.

    This supersedes the old (pre-A2) test of the same shape, which used
    two arbitrary, never-validated header strings ("Bearer doctor-A-session"
    / "Bearer doctor-B-session") to stand in for "two different sessions" --
    correct for testing the inert placeholder that existed before real
    auth, but not a meaningful test of a REAL enforcement layer, where an
    arbitrary unverified string is now rejected outright by require_auth
    with 401 before the session-binding check in /prescribe/confirm is
    ever reached. Two genuinely distinct logged-in users is what actually
    exercises that check now.
    """
    print("\nTest: the full A2 session-binding + logout flow (User A creates+confirms, User B is rejected, logout revokes)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers_a = _login_new_user(client)
        headers_b = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)

        preview_resp = _preview(client, {"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}]}, headers_a)
        check("User A's preview succeeded", preview_resp.status_code == 200, preview_resp.text)
        if preview_resp.status_code != 200:
            return
        preview_id = preview_resp.json()["preview_id"]

        mismatched = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers_b)
        check("User B (a genuinely different authenticated session) confirming User A's preview is rejected (403)", mismatched.status_code == 403, mismatched.text)

        matched = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers_a)
        check("User A confirming their OWN preview succeeds (200)", matched.status_code == 200, matched.text)

        # A second preview, so there's something live to attempt against
        # after logout below (the first one is already consumed).
        _set_stock("Ashwagandha", 5000)
        preview_resp_2 = _preview(client, {"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]}, headers_a)
        check("User A's second preview succeeded", preview_resp_2.status_code == 200, preview_resp_2.text)
        preview_id_2 = preview_resp_2.json()["preview_id"]

        logout_resp = client.post("/auth/logout", headers=headers_a)
        check("User A logging out succeeds", logout_resp.status_code == 200, logout_resp.text)

        reuse_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id_2}, headers=headers_a)
        check(
            "the SAME token, reused after logout, is rejected with 401 (revoked session) -- "
            "require_auth rejects it before the handler's own session-binding check is ever reached",
            reuse_resp.status_code == 401, reuse_resp.text,
        )

        # And the preview itself is untouched by that rejected attempt --
        # still PENDING, not silently consumed by the rejected request.
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status FROM prescription_previews WHERE preview_id = ?", (preview_id_2,)).fetchone()
        conn.close()
        check("the second preview is still PENDING after the rejected post-logout attempt, not silently consumed", row is not None and row["status"] == "PENDING", dict(row) if row else None)


def test_missing_or_malformed_authorization_is_rejected():
    print("\nTest: missing, empty, and malformed Authorization headers are all rejected with 401, not treated as 'no session'")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        no_auth = client.post("/prescribe/manual-preview", json={
            "patient_name": "No Auth Test Patient", "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}],
        })
        check("a request with NO Authorization header at all is rejected with 401", no_auth.status_code == 401, no_auth.text)

        empty_bearer = client.post("/prescribe/manual-preview", json={
            "patient_name": "Empty Bearer Test Patient", "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}],
        }, headers={"Authorization": "Bearer "})
        check("an Authorization header of 'Bearer ' with no token is rejected with 401", empty_bearer.status_code == 401, empty_bearer.text)

        not_bearer = client.post("/prescribe/manual-preview", json={
            "patient_name": "Not Bearer Test Patient", "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}],
        }, headers={"Authorization": "some-arbitrary-string-not-a-bearer-token"})
        check("a malformed (non-'Bearer ') Authorization header is rejected with 401", not_bearer.status_code == 401, not_bearer.text)

        forged = client.post("/prescribe/manual-preview", json={
            "patient_name": "Forged Token Test Patient", "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}],
        }, headers={"Authorization": "Bearer this-token-was-never-issued-by-any-login"})
        check("a well-formed but forged/never-issued bearer token is rejected with 401", forged.status_code == 401, forged.text)

        # And /prescribe/confirm requires the exact same thing -- not just /preview endpoints.
        confirm_no_auth = client.post("/prescribe/confirm", json={"preview_id": "does-not-matter-auth-checked-first"})
        check(
            "/prescribe/confirm without any Authorization header is also rejected with 401 "
            "(auth is checked before the preview even needs to exist)",
            confirm_no_auth.status_code == 401, confirm_no_auth.text,
        )


def test_preexisting_null_session_preview_accepts_any_authenticated_confirm():
    """Defensive-compatibility regression test: backend/main.py's
    session-binding check in /prescribe/confirm keeps an `is not None`
    guard specifically for a preview row whose created_by_session is NULL
    -- which can no longer happen via the HTTP API now that A2 makes auth
    mandatory on preview creation, but could exist from data written
    directly at the storage layer (as every pre-A2 preview in this
    project's test suite, and this exact scenario, does). Proves that
    such a row is still confirmable by ANY authenticated session, rather
    than being permanently stuck (which would be a worse failure mode
    than "too permissive") or crashing.
    """
    print("\nTest: a preview with no recorded session (created_by_session IS NULL) accepts a confirm from any authenticated session")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.preview_store import create_preview
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)

        preview_id = create_preview(
            patient_name="Null Session Compat Test Patient", patient_phone=None, patient_id=None,
            clinic_id="default_clinic", dosha_imbalance="Vata",
            prescribed_herbs=[{"name": "Ashwagandha", "grams": 5}], is_manual=True, created_by_session=None,
        )
        resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check(
            "an authenticated confirm succeeds against a preview with created_by_session IS NULL",
            resp.status_code == 200, resp.text,
        )


def test_concurrent_confirms_at_the_real_http_layer():
    """Phase A, A1: the adversarial "what happens if the same confirmation
    arrives twice, at the same time" case -- but unlike
    tests/test_preview_store.py's test_concurrent_confirms_of_the_same_preview_id
    (which calls the transaction logic directly, reproducing main.py's
    sequence by hand so it can run without a real server), this drives two
    real concurrent HTTP requests through TestClient at the actual
    /prescribe/confirm endpoint -- the literal code path a browser double-
    click or retry would hit. Both tests need to pass; they're checking the
    same guarantee from two different layers.
    """
    print("\nTest: two concurrent HTTP confirms of the SAME preview_id -- exactly one 200, one 409, one deduction")
    import threading
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        preview_resp = _preview(client, {"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 50}]}, headers)
        check("preview succeeded", preview_resp.status_code == 200, preview_resp.text)
        if preview_resp.status_code != 200:
            return
        preview_id = preview_resp.json()["preview_id"]

        responses = {}

        def confirm(name):
            responses[name] = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)

        t1 = threading.Thread(target=confirm, args=("t1",))
        t2 = threading.Thread(target=confirm, args=("t2",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        statuses = sorted(r.status_code for r in responses.values())
        check(
            "exactly one request got 200 and the other got 409 -- never two 200s, never two 409s",
            statuses == [200, 409],
            {name: (r.status_code, r.text) for name, r in responses.items()},
        )

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        stock = conn.execute("SELECT stock_grams FROM inventory WHERE herb_name = ?", ("Ashwagandha",)).fetchone()
        conn.close()
        check(
            "stock reflects exactly ONE deduction of 50g, never a double deduction or a torn one",
            stock is not None and stock["stock_grams"] == 5000 - 50,
            dict(stock) if stock else None,
        )


def test_unknown_preview_id_returns_404():
    print("\nTest: confirming an unknown/never-issued preview_id returns 404, not 409 (for an AUTHENTICATED caller)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        resp = client.post("/prescribe/confirm", json={"preview_id": "this-was-never-issued"}, headers=headers)
        check("unknown preview_id returns 404", resp.status_code == 404, resp.text)


def test_doctor_correction_uses_same_canonical_resolver():
    print("\nTest: a doctor's corrected herb name resolves through the SAME normalize_herb() boundary the AI's does")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Haridra", 5000)
        preview_resp = _preview(client, {"dosha_imbalance": "Kapha", "prescribed_herbs": [{"name": "Turmeric", "grams": 10}]}, headers)
        check("preview succeeded (AI's 'Turmeric' resolved to canonical Haridra)", preview_resp.status_code == 200, preview_resp.text)
        if preview_resp.status_code != 200:
            return
        check("preview shows the canonical name, not the AI's raw alias", preview_resp.json()["prescribed_herbs"] == [{"name": "Haridra", "grams": 10}], preview_resp.json())
        preview_id = preview_resp.json()["preview_id"]

        # Doctor corrects using a DIFFERENT alias for the same herb.
        confirm_resp = client.post("/prescribe/confirm", json={
            "preview_id": preview_id,
            "prescribed_herbs": [{"name": "Curcuma longa", "grams": 8}],
        }, headers=headers)
        check("doctor's alternate alias ('Curcuma longa') also resolves to Haridra", confirm_resp.status_code == 200 and confirm_resp.json().get("prescribed_herbs") == [{"name": "Haridra", "grams": 8}], confirm_resp.text)

        # A genuinely unknown herb in a doctor's correction must still 400, exactly like /preview does.
        preview_resp2 = _preview(client, {"dosha_imbalance": "Kapha", "prescribed_herbs": [{"name": "Haridra", "grams": 5}]}, headers)
        preview_id2 = preview_resp2.json()["preview_id"]
        bad_correction = client.post("/prescribe/confirm", json={
            "preview_id": preview_id2,
            "prescribed_herbs": [{"name": "unicorn root", "grams": 5}],
        }, headers=headers)
        check("an unknown herb in a doctor's correction returns 400, same as /preview would", bad_correction.status_code == 400, bad_correction.text)


def test_confirm_rejects_invalid_grams_same_as_preview():
    print("\nTest: doctor corrections get the same grams validation (>0, <=10000) as AI output")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        preview_resp = _preview(client, {"dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}]}, headers)
        preview_id = preview_resp.json()["preview_id"]

        zero_grams = client.post("/prescribe/confirm", json={"preview_id": preview_id, "prescribed_herbs": [{"name": "Ashwagandha", "grams": 0}]}, headers=headers)
        check("grams=0 in a doctor correction is rejected by validation (422)", zero_grams.status_code == 422, zero_grams.text)

        negative_grams = client.post("/prescribe/confirm", json={"preview_id": preview_id, "prescribed_herbs": [{"name": "Ashwagandha", "grams": -5}]}, headers=headers)
        check("negative grams in a doctor correction is rejected by validation (422)", negative_grams.status_code == 422, negative_grams.text)

        too_much = client.post("/prescribe/confirm", json={"preview_id": preview_id, "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10001}]}, headers=headers)
        check("grams > 10000 in a doctor correction is rejected by validation (422)", too_much.status_code == 422, too_much.text)

        # The preview must still be untouched/usable after all those rejections (they never even reached our handler).
        valid = client.post("/prescribe/confirm", json={"preview_id": preview_id, "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}]}, headers=headers)
        check("a subsequently valid confirm on the same preview_id still succeeds", valid.status_code == 200, valid.text)


def test_fresh_database_gets_migrated_by_real_app_startup():
    """Regression test for the hard A1 Mac verification failure: a real
    Mac db that had never had run_migrations() invoked against it (not
    even for migrations 001/002) produced "no such table:
    prescription_previews" the moment any test touched the database,
    because every test in this file constructed `TestClient(app)` WITHOUT
    the `with` block -- and FastAPI/Starlette only run
    @app.on_event("startup") (which calls run_migrations()) when TestClient
    is used as a context manager. This test proves the fix at the actual
    application-startup layer: point db.database.DB_PATH at a BRAND NEW,
    completely empty file (no schema_migrations, no tables at all -- more
    hostile than the reported bug, which at least had migrations 001/002's
    tables already present from the pre-migration codebase), start the app
    the same way `with TestClient(app) as client:` always has, and confirm
    the app serves a real request successfully with no manual bootstrap
    step -- i.e. that a genuinely fresh Mac/application database gets
    migration 003 (and 001, 002) applied automatically by the real startup
    event, exactly as production (uvicorn) already does.
    """
    print("\nTest: a completely fresh database file gets fully migrated by the real FastAPI startup event")
    import tempfile
    import db.database as database_module
    import db.migrations as migrations_module
    import backend.main as main_module
    from fastapi.testclient import TestClient

    fd, fresh_db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(fresh_db_path)  # the app must be able to bootstrap from truly nothing

    original_db_path = database_module.DB_PATH
    database_module.DB_PATH = fresh_db_path
    try:
        # Sanity check: genuinely nothing here yet -- this is stricter than
        # the real bug report, which had 001/002's tables pre-existing.
        check(
            "the fresh database file does not exist before startup",
            not os.path.exists(fresh_db_path),
        )

        with TestClient(main_module.app) as client:
            # Phase A, A2: also proves migrations 004 (users) and 005
            # (sessions) get applied by the same real startup event, and
            # that a genuinely fresh database can bootstrap a real user
            # and log in with zero manual steps -- not just that the
            # OLDER (A1) tables come up correctly.
            headers = _login_new_user(client)

            # If @app.on_event("startup") didn't run (the original bug),
            # this fails with the exact "no such table: prescription_previews"
            # error the Mac verification reported.
            resp = client.post("/prescribe/manual-preview", json={
                "patient_name": "Fresh DB Startup Test Patient",
                "dosha_imbalance": "Vata",
                "prescribed_herbs": [{"name": "Ashwagandha", "grams": 10}],
            }, headers=headers)
            check(
                "a request succeeds against a totally fresh database with zero manual bootstrap steps",
                resp.status_code == 200,
                f"got {resp.status_code}: {resp.text}",
            )

        verify_conn = sqlite3.connect(fresh_db_path)
        verify_conn.row_factory = sqlite3.Row
        applied_versions = sorted(
            row["version"] for row in verify_conn.execute("SELECT version FROM schema_migrations")
        )
        expected_versions = [m.version for m in migrations_module.discover_migrations()]
        check(
            "the real startup event applied every real migration, not just enough to limp through one request",
            applied_versions == expected_versions,
            applied_versions,
        )
        check(
            "prescription_previews exists in the freshly-migrated database",
            verify_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='prescription_previews'"
            ).fetchone() is not None,
        )
        verify_conn.close()
    finally:
        database_module.DB_PATH = original_db_path
        if os.path.exists(fresh_db_path):
            os.remove(fresh_db_path)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(fresh_db_path + suffix):
                os.remove(fresh_db_path + suffix)


_ALL_TESTS = [
    test_single_use_confirm_returns_409_on_replay,
    test_failed_confirm_leaves_preview_retriable,
    test_concurrent_confirms_at_the_real_http_layer,
    test_session_binding_full_flow,
    test_missing_or_malformed_authorization_is_rejected,
    test_preexisting_null_session_preview_accepts_any_authenticated_confirm,
    test_unknown_preview_id_returns_404,
    test_doctor_correction_uses_same_canonical_resolver,
    test_confirm_rejects_invalid_grams_same_as_preview,
    test_fresh_database_gets_migrated_by_real_app_startup,
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
    print("--- Preview/Confirm Lifecycle Tests ---")
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
