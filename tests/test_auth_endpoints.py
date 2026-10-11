"""
HTTP-layer tests for POST /auth/login and POST /auth/logout (Phase A, A2 --
new, additive endpoints; see API Contract Addendum v1.2), plus the
require_auth dependency they and the three prescribe endpoints all share.

This file complements tests/test_auth_service.py (which tests
backend/auth_service.py's functions directly, without FastAPI) by proving
the same guarantees hold through the real HTTP layer: real request/response
JSON shapes, real status codes, and -- for
test_valid_session_survives_a_simulated_app_restart -- a genuinely separate
FastAPI app instance/TestClient standing in for a fresh process, at the
actual HTTP layer rather than the sqlite layer test_auth_service.py already
covers more literally (via a real subprocess).

Like every other FastAPI-dependent test in this project, this file needs
FastAPI installed to run at all, and needs argon2-cffi installed for the
real login calls inside it to succeed (create_user() hashes with real
Argon2id) -- REQUIRES MAC VERIFICATION. See the A2 report's section G.

A6.1 addition (Login Abuse Resistance): the HTTP-layer half of the new
login-throttle behavior -- enumeration resistance extended to a FOURTH
failure class (a temporarily-throttled account, alongside the existing
nonexistent-username/wrong-password/inactive-account trio), the throttle
actually engaging and blocking further attempts through the real
POST /auth/login endpoint, a cooldown expiring and login working again
afterward, and the new `event=login_failed` log line never leaking a
password or token and never varying by internal failure reason. The
storage-level mechanics (atomicity, concurrency, persistence) are already
covered, without needing FastAPI or argon2-cffi at all, by
tests/test_auth_service.py's SESSION-LAYER throttle tests -- this file
only adds what genuinely needs the real HTTP layer.

Run directly (no pytest required):
    python tests/test_auth_endpoints.py
"""
import logging
import os
import secrets
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_passed = 0
_failed = 0


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


def _unique_username(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def test_login_success_returns_a_usable_token():
    print("\nTest: a correct username/password returns a token that authenticates a protected endpoint")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user
    with TestClient(main_module.app) as client:
        username = _unique_username("login_success")
        password = "a-real-password-123"
        create_user(username, password, clinic_id="login_test_clinic")

        resp = client.post("/auth/login", json={"username": username, "password": password})
        check("login with correct credentials returns 200", resp.status_code == 200, resp.text)
        body = resp.json()
        check("login response includes a non-empty opaque token", isinstance(body.get("token"), str) and len(body["token"]) >= 32, body)
        check("login response echoes the username", body.get("username") == username, body)
        check("login response echoes the account's real clinic_id", body.get("clinic_id") == "login_test_clinic", body)
        check("login response includes expires_at", "expires_at" in body, body)

        # The returned token must actually work against a protected endpoint.
        manual_preview_resp = client.post(
            "/prescribe/manual-preview",
            json={"patient_name": "Login Success Test Patient", "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]},
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        check("the token returned by /auth/login authenticates a protected endpoint", manual_preview_resp.status_code == 200, manual_preview_resp.text)


def test_login_wrong_password_returns_generic_401():
    print("\nTest: a wrong password returns a generic 401, not a distinguishing error")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user
    with TestClient(main_module.app) as client:
        username = _unique_username("wrong_pw")
        create_user(username, "the-real-password", clinic_id="default_clinic")

        resp = client.post("/auth/login", json={"username": username, "password": "not-the-real-password"})
        check("wrong password returns 401", resp.status_code == 401, resp.text)
        check("wrong password's detail is the generic message, not something like 'wrong password'", resp.json().get("detail") == "Invalid username or password.", resp.json())


def test_login_nonexistent_username_returns_identical_401():
    print("\nTest: a nonexistent username returns the SAME 401 + message a wrong password does (no enumeration)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user
    with TestClient(main_module.app) as client:
        real_username = _unique_username("enum_real")
        create_user(real_username, "the-real-password", clinic_id="default_clinic")

        wrong_password_resp = client.post("/auth/login", json={"username": real_username, "password": "wrong"})
        nonexistent_resp = client.post("/auth/login", json={"username": _unique_username("does_not_exist"), "password": "anything"})

        check("nonexistent username returns 401", nonexistent_resp.status_code == 401, nonexistent_resp.text)
        check(
            "nonexistent-username and wrong-password responses are BYTE-FOR-BYTE identical (status + body) -- "
            "no way to tell 'no such user' from 'wrong password' from the response",
            wrong_password_resp.status_code == nonexistent_resp.status_code and wrong_password_resp.json() == nonexistent_resp.json(),
            (wrong_password_resp.status_code, wrong_password_resp.json(), nonexistent_resp.status_code, nonexistent_resp.json()),
        )


def test_login_inactive_user_returns_identical_401():
    print("\nTest: an inactive (deactivated) user's correct password also returns the SAME generic 401")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user
    from db.database import get_connection
    with TestClient(main_module.app) as client:
        username = _unique_username("inactive_http")
        create_user(username, "the-real-password", clinic_id="default_clinic")
        conn = get_connection()
        try:
            conn.execute("UPDATE users SET is_active = 0 WHERE username = ?", (username,))
            conn.commit()
        finally:
            conn.close()

        resp = client.post("/auth/login", json={"username": username, "password": "the-real-password"})
        check("an inactive user's CORRECT password still returns 401", resp.status_code == 401, resp.text)
        check("the message is the same generic one, not 'account deactivated'", resp.json().get("detail") == "Invalid username or password.", resp.json())


def test_login_malformed_request_returns_422():
    print("\nTest: a malformed login request (missing password) is rejected with 422, before authenticate_user is ever called")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        resp = client.post("/auth/login", json={"username": "someone"})
        check("a login request missing 'password' returns 422", resp.status_code == 422, resp.text)

        empty_body_resp = client.post("/auth/login", json={})
        check("a completely empty login request body returns 422", empty_body_resp.status_code == 422, empty_body_resp.text)


def test_logout_revokes_the_session_immediately():
    print("\nTest: /auth/logout revokes the session, which then immediately stops authenticating anything")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user
    with TestClient(main_module.app) as client:
        username = _unique_username("logout_http")
        password = "logout-test-password"
        create_user(username, password, clinic_id="default_clinic")
        token = client.post("/auth/login", json={"username": username, "password": password}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        before = client.post(
            "/prescribe/manual-preview",
            json={"patient_name": "Logout Test Patient", "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]},
            headers=headers,
        )
        check("the token works before logout", before.status_code == 200, before.text)

        logout_resp = client.post("/auth/logout", headers=headers)
        check("logout succeeds (200) for a valid session", logout_resp.status_code == 200, logout_resp.text)

        after = client.post(
            "/prescribe/manual-preview",
            json={"patient_name": "Logout Test Patient 2", "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]},
            headers=headers,
        )
        check("the SAME token is rejected with 401 immediately after logout", after.status_code == 401, after.text)

        second_logout = client.post("/auth/logout", headers=headers)
        check("logging out an already-revoked session returns 401 (require_auth rejects it), not a crash", second_logout.status_code == 401, second_logout.text)


def test_logout_requires_authentication():
    print("\nTest: /auth/logout itself requires a valid Authorization header -- it isn't an unauthenticated endpoint")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        resp = client.post("/auth/logout")
        check("logging out with no Authorization header at all is rejected with 401", resp.status_code == 401, resp.text)


def test_valid_session_survives_a_simulated_app_restart():
    """Authentication must survive an application restart -- there is no
    in-memory session state anywhere for a restart to lose (see
    backend/auth_service.py). tests/test_auth_service.py already proves
    this the most literally possible way (a genuinely separate `python3`
    subprocess). This test proves the same thing one layer up: log in
    against ONE FastAPI app/TestClient instance, tear it down completely,
    then use the SAME raw token against a BRAND NEW app/TestClient
    instance (a fresh @app.on_event("startup") run, a fresh in-process
    FastAPI app object -- as close to "the process restarted" as this
    layer allows without literally spawning a second uvicorn) -- the
    token must still authenticate successfully.
    """
    print("\nTest: a valid session, once created, authenticates successfully against a freshly-started app instance")
    import importlib
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user

    username = _unique_username("restart_http")
    password = "restart-test-password"

    with TestClient(main_module.app) as client1:
        create_user(username, password, clinic_id="default_clinic")
        token = client1.post("/auth/login", json={"username": username, "password": password}).json()["token"]

    # client1 (and its `with` block) is now fully torn down. Re-import
    # backend.main fresh and start a SECOND, independent TestClient/app
    # lifecycle -- as close to "the application restarted" as this layer
    # allows.
    main_module_reloaded = importlib.reload(main_module)
    with TestClient(main_module_reloaded.app) as client2:
        resp = client2.post(
            "/prescribe/manual-preview",
            json={"patient_name": "Restart Survival Test Patient", "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]},
            headers={"Authorization": f"Bearer {token}"},
        )
        check(
            "the token created under the FIRST app instance still authenticates successfully against a "
            "freshly-restarted SECOND app instance",
            resp.status_code == 200, resp.text,
        )


def test_credentials_are_not_present_in_application_logs():
    print("\nTest: neither the plaintext password nor the raw session token appear anywhere in application logs around login")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user
    with TestClient(main_module.app) as client:
        username = _unique_username("log_safety")
        password = "super-secret-password-must-never-be-logged"

        create_user(username, password, clinic_id="default_clinic")

        log_capture = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                log_capture.append(self.format(record))

        handler = _CaptureHandler()
        ayurcore_logger = logging.getLogger("ayurcore")
        ayurcore_logger.addHandler(handler)
        try:
            resp = client.post("/auth/login", json={"username": username, "password": password})
        finally:
            ayurcore_logger.removeHandler(handler)

        token = resp.json().get("token", "")
        all_log_text = "\n".join(log_capture)
        check("the plaintext password never appears in any captured log line", password not in all_log_text, log_capture)
        check("the raw session token never appears in any captured log line", bool(token) and token not in all_log_text, log_capture)


# =============================== A6.1: LOGIN THROTTLE (HTTP layer) ========

def _lock_out_via_http(client, username: str, password: str):
    """Drives FAILURE_THRESHOLD wrong-password attempts through the real
    POST /auth/login endpoint -- not a shortcut through auth_service.py
    directly -- so the throttle-engaging tests below prove the real HTTP
    path, matching this file's own stated purpose (see module docstring)."""
    from backend.auth_service import FAILURE_THRESHOLD
    for _ in range(FAILURE_THRESHOLD):
        resp = client.post("/auth/login", json={"username": username, "password": "wrong-password"})
        check("each lockout-inducing attempt still returns 401", resp.status_code == 401, resp.text)


def test_login_enumeration_resistance_across_four_failure_classes():
    print("\nTest: nonexistent username, wrong password, inactive account, and a throttled account all return byte-identical 401s")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user
    from db.database import get_connection
    with TestClient(main_module.app) as client:
        real_username = _unique_username("enum4_real")
        real_password = "the-real-password-enum4"
        create_user(real_username, real_password, clinic_id="default_clinic")

        inactive_username = _unique_username("enum4_inactive")
        create_user(inactive_username, real_password, clinic_id="default_clinic")
        conn = get_connection()
        try:
            conn.execute("UPDATE users SET is_active = 0 WHERE username = ?", (inactive_username,))
            conn.commit()
        finally:
            conn.close()

        throttled_username = _unique_username("enum4_throttled")
        create_user(throttled_username, real_password, clinic_id="default_clinic")
        _lock_out_via_http(client, throttled_username, real_password)

        nonexistent_resp = client.post("/auth/login", json={"username": _unique_username("enum4_missing"), "password": "anything"})
        wrong_password_resp = client.post("/auth/login", json={"username": real_username, "password": "wrong"})
        inactive_resp = client.post("/auth/login", json={"username": inactive_username, "password": real_password})
        # The throttled account's CORRECT password, presented while still locked.
        throttled_resp = client.post("/auth/login", json={"username": throttled_username, "password": real_password})

        responses = {
            "nonexistent": nonexistent_resp,
            "wrong_password": wrong_password_resp,
            "inactive": inactive_resp,
            "throttled": throttled_resp,
        }
        for name, resp in responses.items():
            check(f"the {name} case returns 401", resp.status_code == 401, (name, resp.text))

        bodies = {name: resp.json() for name, resp in responses.items()}
        check(
            "all FOUR failure classes -- nonexistent username, wrong password, inactive account, and a "
            "throttled account presenting its CORRECT password -- return the exact same status code and "
            "byte-identical JSON body; none of them is distinguishable from any other from the outside",
            len({resp.status_code for resp in responses.values()}) == 1
            and len({tuple(sorted(b.items())) for b in bodies.values()}) == 1,
            bodies,
        )


def test_login_throttle_activates_via_http_and_blocks_further_attempts():
    print("\nTest: FAILURE_THRESHOLD wrong-password attempts through the real endpoint activate a cooldown that then blocks a subsequent attempt")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user
    with TestClient(main_module.app) as client:
        username = _unique_username("http_throttle")
        password = "http-throttle-test-password"
        create_user(username, password, clinic_id="default_clinic")

        _lock_out_via_http(client, username, password)

        # Even the CORRECT password is now rejected -- the same generic
        # 401 as every other failure reason on this endpoint.
        locked_resp = client.post("/auth/login", json={"username": username, "password": password})
        check("once locked via real HTTP attempts, even the correct password is rejected with 401", locked_resp.status_code == 401, locked_resp.text)
        check(
            "the locked-out response body is the exact same generic message every other login failure uses",
            locked_resp.json().get("detail") == "Invalid username or password.", locked_resp.json(),
        )


def test_login_throttle_cooldown_expiry_allows_login_again():
    print("\nTest: once a cooldown's locked_until has passed, the legitimate doctor can log in again")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user
    from db.database import get_connection
    with TestClient(main_module.app) as client:
        username = _unique_username("http_throttle_expiry")
        password = "http-throttle-expiry-test-password"
        create_user(username, password, clinic_id="default_clinic")

        _lock_out_via_http(client, username, password)

        still_locked = client.post("/auth/login", json={"username": username, "password": password})
        check("immediately after lockout, even the correct password is still rejected", still_locked.status_code == 401, still_locked.text)

        # Simulate the cooldown having elapsed -- same backdating
        # convention tests/test_auth_service.py already uses for session
        # expiry, applied here to login_throttle.locked_until instead of
        # sleeping in a test for real wall-clock seconds.
        conn = get_connection()
        try:
            conn.execute(
                """
                UPDATE login_throttle SET locked_until = datetime('now', '-1 seconds')
                WHERE user_id = (SELECT user_id FROM users WHERE username = ?)
                """,
                (username,),
            )
            conn.commit()
        finally:
            conn.close()

        after_expiry = client.post("/auth/login", json={"username": username, "password": password})
        check("once the cooldown has expired, the legitimate doctor's correct password succeeds again", after_expiry.status_code == 200, after_expiry.text)
        check("a successful post-cooldown login returns a real, usable token", isinstance(after_expiry.json().get("token"), str) and len(after_expiry.json()["token"]) >= 32, after_expiry.json())


def test_failed_login_logs_event_login_failed_without_leaking_secrets():
    print("\nTest: a failed login produces an 'event=login_failed' log line that never contains the password, a token, or the Authorization header")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user
    with TestClient(main_module.app) as client:
        username = _unique_username("log_failed")
        password = "super-secret-password-must-never-be-logged-either"

        create_user(username, password, clinic_id="default_clinic")

        log_capture = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                log_capture.append(self.format(record))

        handler = _CaptureHandler()
        ayurcore_logger = logging.getLogger("ayurcore")
        ayurcore_logger.addHandler(handler)
        try:
            resp = client.post("/auth/login", json={"username": username, "password": "definitely-wrong"})
        finally:
            ayurcore_logger.removeHandler(handler)

        check("the failed login itself still returns 401", resp.status_code == 401, resp.text)
        all_log_text = "\n".join(log_capture)
        check("a captured log line contains 'event=login_failed'", "event=login_failed" in all_log_text, log_capture)
        check("the failed-login log line includes the SUPPLIED username (an operational identifier, already known to the caller)", username in all_log_text, log_capture)
        check("the real password never appears in any captured log line", password not in all_log_text, log_capture)
        check("the WRONG password submitted in this attempt never appears in any captured log line either", "definitely-wrong" not in all_log_text, log_capture)
        check("no 'Authorization' or 'Bearer' text appears anywhere in the captured logs for this failed login", "Authorization" not in all_log_text and "Bearer" not in all_log_text, log_capture)


def test_failed_login_log_line_does_not_disclose_the_internal_failure_reason():
    print("\nTest: the event=login_failed log line has the identical shape regardless of WHY the login failed -- nonexistent user, wrong password, or throttled")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.auth_service import create_user
    with TestClient(main_module.app) as client:
        real_username = _unique_username("log_reason_real")
        real_password = "log-reason-real-password"
        create_user(real_username, real_password, clinic_id="default_clinic")

        throttled_username = _unique_username("log_reason_throttled")
        create_user(throttled_username, real_password, clinic_id="default_clinic")
        _lock_out_via_http(client, throttled_username, real_password)

        def _capture_one_failed_login(username, password):
            """Captures every 'ayurcore'-logger record emitted by exactly
            one POST /auth/login call. Returns (joined_text, line, raw):

            `joined_text` -- ALL captured records joined into one string
            -- is what decides whether an 'event=login_failed' record was
            emitted at all, using the SAME robust "join everything, then
            substring-search" pattern already proven to work just above in
            test_failed_login_logs_event_login_failed_without_leaking_secrets
            -- deliberately NOT the fragile "filter to matching lines,
            return the first one or None" pattern this helper used before
            (root-caused, Sep 2026 A6.1 Mac verification: a Mac run
            reported this case's log line as "not captured", and a bare
            None gave no way to tell a genuine missing-record from a
            filtering artifact -- see this segment's investigation notes).

            `raw` is the full list of captured record strings, always
            returned (even when empty) so a failed check has real
            evidence attached instead of a bare pass/fail label.

            `line` is the one line containing 'event=login_failed', for
            the shape-comparison check below, which needs one specific
            line's text to diff -- looked up only as a convenience once
            presence has already been decided from `joined_text`."""
            captured = []

            class _H(logging.Handler):
                def emit(self, record):
                    captured.append(self.format(record))

            handler = _H()
            ayurcore_logger = logging.getLogger("ayurcore")
            ayurcore_logger.addHandler(handler)
            try:
                client.post("/auth/login", json={"username": username, "password": password})
            finally:
                ayurcore_logger.removeHandler(handler)
            joined_text = "\n".join(captured)
            matching_lines = [line for line in captured if "event=login_failed" in line]
            line = matching_lines[0] if matching_lines else None
            return joined_text, line, captured

        nonexistent_username = _unique_username("log_reason_missing")
        nonexistent_text, nonexistent_line, nonexistent_raw = _capture_one_failed_login(nonexistent_username, "anything")
        wrong_password_text, wrong_password_line, wrong_password_raw = _capture_one_failed_login(real_username, "wrong-password")
        throttled_text, throttled_line, throttled_raw = _capture_one_failed_login(throttled_username, real_password)  # correct password, but locked

        for label, text, raw in (
            ("nonexistent", nonexistent_text, nonexistent_raw),
            ("wrong_password", wrong_password_text, wrong_password_raw),
            ("throttled", throttled_text, throttled_raw),
        ):
            # `raw` (the actual captured records, or [] if truly none were
            # captured) is passed as the failure detail here -- NOT the
            # case label -- so a failure shows real evidence instead of a
            # diagnostic string that can be mistaken for captured content.
            check(f"a captured log line contains 'event=login_failed' for the {label} case", "event=login_failed" in text, raw)

        def _template(line, username):
            # Replace the one part of the line that's expected to legitimately
            # differ (the username itself) so the remaining shape can be
            # compared for equality across all three causes.
            return line.replace(username, "<username>") if line else line

        templates = {
            "nonexistent": _template(nonexistent_line, nonexistent_username),
            "wrong_password": _template(wrong_password_line, real_username),
            "throttled": _template(throttled_line, throttled_username),
        }
        check(
            "the login_failed log line's shape (everything except the username itself) is IDENTICAL "
            "whether the cause was a nonexistent username, a wrong password, or an active throttle "
            "cooldown -- nothing in the log line itself discloses which of those actually happened",
            len(set(templates.values())) == 1,
            templates,
        )


_ALL_TESTS = [
    test_login_success_returns_a_usable_token,
    test_login_wrong_password_returns_generic_401,
    test_login_nonexistent_username_returns_identical_401,
    test_login_inactive_user_returns_identical_401,
    test_login_malformed_request_returns_422,
    test_logout_revokes_the_session_immediately,
    test_logout_requires_authentication,
    test_valid_session_survives_a_simulated_app_restart,
    test_credentials_are_not_present_in_application_logs,
    test_login_enumeration_resistance_across_four_failure_classes,
    test_login_throttle_activates_via_http_and_blocks_further_attempts,
    test_login_throttle_cooldown_expiry_allows_login_again,
    test_failed_login_logs_event_login_failed_without_leaking_secrets,
    test_failed_login_log_line_does_not_disclose_the_internal_failure_reason,
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
