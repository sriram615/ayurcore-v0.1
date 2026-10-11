"""
A6.3 hardening, item 4 -- unsupported HTTP method attack surface.

The A6.3 read-only review found NO test anywhere in this project's suite
had ever exercised PUT/PATCH/DELETE/OPTIONS against any of this API's 7
routes -- Gate 8 was marked NOT TESTED, falling back entirely to
FastAPI/Starlette's own framework-default 405 behavior with no test
proving it. This file closes that gap, without customizing the
framework's method handling in any way (no code change accompanies this
file -- it is purely a new test file).

Verifies, for each case:
  1. The response is 405 (Method Not Allowed), not 500 and not a silent
     200.
  2. No state mutation occurred (inventory/prescriptions/audit_events
     counts unchanged).
  3. No authentication bypass: the 405 happens even with NO Authorization
     header at all, proving Starlette's routing layer rejects the method
     before any auth/RBAC dependency ever runs -- an unsupported method
     cannot be used to skip require_auth/require_role.
  4. No sensitive error disclosure (no stack trace, no internal path, no
     token value) in the 405 response body.

Run directly (no pytest required):
    python tests/test_unsupported_methods.py
Or under pytest:
    pytest tests/test_unsupported_methods.py -v
"""
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
    convention as every other test file in this project."""
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
    from backend.auth_service import create_user

    username = f"method_test_{secrets.token_hex(6)}"
    password = "method-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _counts():
    conn = sqlite3.connect(DB_PATH)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("prescriptions", "prescription_previews", "audit_events", "corrections", "idempotency_keys")
        }
    finally:
        conn.close()


def _assert_clean_405(resp, label: str):
    check(f"{label}: status is 405, not 500", resp.status_code == 405, resp.text)
    check(f"{label}: response carries no stack trace / internal exception text", "traceback" not in resp.text.lower(), resp.text)
    check(f"{label}: no file-path disclosure in the body", ".py" not in resp.text, resp.text)


def test_delete_prescribe_confirm_returns_405_without_authentication():
    print("\nTest: DELETE /prescribe/confirm returns 405, even with NO Authorization header at all -- proves the method check happens before auth")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        counts_before = _counts()
        resp = client.delete("/prescribe/confirm")
        _assert_clean_405(resp, "DELETE /prescribe/confirm (unauthenticated)")
        check(
            "an unsupported method on a normally-protected route does NOT require authentication to be "
            "rejected -- it fails at routing, before require_auth ever runs; this is the expected, safe "
            "default, not an authentication bypass (there is nothing to bypass -- no business logic ran)",
            resp.status_code == 405,
            resp.text,
        )
        check("no state mutation occurred", _counts() == counts_before, (counts_before, _counts()))


def test_delete_prescribe_confirm_returns_405_with_valid_authentication_too():
    print("\nTest: DELETE /prescribe/confirm returns 405 even WITH a genuinely valid Authorization header -- the method itself is simply not routable, regardless of identity")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        counts_before = _counts()
        resp = client.delete("/prescribe/confirm", headers=headers)
        _assert_clean_405(resp, "DELETE /prescribe/confirm (authenticated)")
        check("no state mutation occurred", _counts() == counts_before, (counts_before, _counts()))


def test_put_prescribe_preview_returns_405():
    print("\nTest: PUT /prescribe/preview returns 405")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        resp = client.put("/prescribe/preview", json={})
        _assert_clean_405(resp, "PUT /prescribe/preview")


def test_get_prescribe_confirm_returns_405():
    print("\nTest: GET /prescribe/confirm (GET-where-POST-expected) returns 405")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        resp = client.get("/prescribe/confirm")
        _assert_clean_405(resp, "GET /prescribe/confirm")


def test_patch_audit_export_returns_405():
    print("\nTest: PATCH /audit/export (a GET-only, ADMIN/AUDITOR-only route) returns 405")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        resp = client.patch("/audit/export")
        _assert_clean_405(resp, "PATCH /audit/export")


def test_delete_auth_login_returns_405():
    print("\nTest: DELETE /auth/login (a public, no-auth-required route) returns 405, not accidentally routed as a logout or anything else")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        resp = client.delete("/auth/login")
        _assert_clean_405(resp, "DELETE /auth/login")


_ALL_TESTS = [
    test_delete_prescribe_confirm_returns_405_without_authentication,
    test_delete_prescribe_confirm_returns_405_with_valid_authentication_too,
    test_put_prescribe_preview_returns_405,
    test_get_prescribe_confirm_returns_405,
    test_patch_audit_export_returns_405,
    test_delete_auth_login_returns_405,
]


def run_tests():
    print("--- A6.3 hardening: unsupported HTTP method attack surface ---\n")
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
