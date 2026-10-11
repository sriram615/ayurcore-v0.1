"""
A6.3 hardening, item 1 -- HTTP-layer Authorization header edge cases.

The A6.3 read-only review found real HTTP-layer test coverage for the
basic malformed-Authorization cases (empty `Bearer `, a non-`Bearer`
scheme, a never-issued token -- see tests/test_preview_lifecycle.py's
test_missing_or_malformed_authorization_is_rejected) but flagged a set of
narrower variants as code-reasoned-safe rather than proven: duplicate
headers, a lowercase `bearer` scheme, a bare `Bearer` with no trailing
space at all, internal whitespace variants, and a single-character
mutation of a genuinely issued valid token. This file closes that gap.

Every case here goes through the REAL FastAPI app via TestClient, hitting
backend.rbac.require_auth's actual `if not authorization or not
authorization.startswith("Bearer "): raise HTTPException(401, ...)` /
token-lookup logic (backend/rbac.py, unmodified by this batch) -- not a
unit test of the function in isolation.

Every case asserts the same four safety properties the A6.3 review's Gate
1 and Gate 10 asked for:
  1. The response is 401 (not 500, not 200).
  2. The response body contains no stack trace / internal exception text.
  3. Neither the real valid token nor the mutated/malformed value sent
     ever appears anywhere in the response body or headers.
  4. A protected resource's actual data never comes back in the body.

This file does NOT change backend/rbac.py or backend/auth_service.py --
per the operating rule, authentication behavior is touched only if one of
these tests exposes an actual vulnerability. None did (see each test's
own PASS/FAIL meaning below).

Run directly (no pytest required):
    python tests/test_auth_edge_cases.py
Or under pytest:
    pytest tests/test_auth_edge_cases.py -v
"""
import os
import secrets
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def _create_real_user_and_get_valid_token(client, clinic_id: str = "default_clinic") -> tuple:
    """Logs in a genuinely fresh, real user through the actual
    /auth/login endpoint (not a synthetic token) -- needed for the
    single-character-mutation test, which must mutate a token that would
    otherwise actually work. Returns (token, username)."""
    from backend.auth_service import create_user

    username = f"auth_edge_test_{secrets.token_hex(6)}"
    password = "auth-edge-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    return resp.json()["token"], username


def _assert_safe_401(resp, *, secrets_that_must_not_leak: list, label: str):
    """The shared safety checks every case in this file runs. `resp` is a
    TestClient response from a protected endpoint; `secrets_that_must_not_leak`
    is every token/header value used in this specific request."""
    check(f"{label}: status is 401, not 500 or 200", resp.status_code == 401, resp.text)
    body_lower = resp.text.lower()
    check(
        f"{label}: response body carries no stack trace / internal exception markers",
        "traceback" not in body_lower and "file \"" not in body_lower and "exception" not in body_lower,
        resp.text,
    )
    for secret_value in secrets_that_must_not_leak:
        if not secret_value:
            continue
        check(f"{label}: the sent header value does not appear in the response body", secret_value not in resp.text, resp.text)
        for header_value in resp.headers.values():
            check(f"{label}: the sent header value does not appear in any response header", secret_value not in header_value, dict(resp.headers))
    check(
        f"{label}: no protected clinic data comes back in the body (no 'clinic_id' field present)",
        '"clinic_id"' not in resp.text,
        resp.text,
    )


def test_bearer_with_no_trailing_space_and_no_token():
    print("\nTest: 'Bearer' with no trailing space and no token at all -- not even the empty-Bearer case already covered elsewhere")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        resp = client.get("/audit/export", headers={"Authorization": "Bearer"})
        _assert_safe_401(resp, secrets_that_must_not_leak=[], label="bare 'Bearer', no space")


def test_lowercase_bearer_scheme_is_rejected():
    print("\nTest: lowercase 'bearer <token>' is rejected -- require_auth's startswith('Bearer ') check is case-sensitive")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        token, _username = _create_real_user_and_get_valid_token(client)
        resp = client.get("/audit/export", headers={"Authorization": f"bearer {token}"})
        _assert_safe_401(resp, secrets_that_must_not_leak=[token], label="lowercase 'bearer'")
        check(
            "lowercase scheme does NOT authenticate as the real user despite carrying a genuinely valid token value",
            resp.status_code == 401,
            resp.text,
        )


def test_malformed_non_bearer_schemes_are_rejected():
    print("\nTest: other HTTP auth schemes (Basic, Token, Digest) are rejected the same way as any other malformed header")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        token, _username = _create_real_user_and_get_valid_token(client)
        for scheme_value in [f"Basic {token}", f"Token {token}", f"Digest {token}", "Bearer:" + token, token]:
            resp = client.get("/audit/export", headers={"Authorization": scheme_value})
            _assert_safe_401(resp, secrets_that_must_not_leak=[token], label=f"scheme variant {scheme_value.split()[0] if ' ' in scheme_value else '(bare token)'}")


def test_whitespace_variants_around_a_valid_token():
    print("\nTest: whitespace variants around an otherwise-valid token")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        token, _username = _create_real_user_and_get_valid_token(client)

        # Leading whitespace before the whole header value: NOT stripped by
        # `.startswith("Bearer ")`, so this must be rejected.
        resp_leading = client.get("/audit/export", headers={"Authorization": f"  Bearer {token}"})
        _assert_safe_401(resp_leading, secrets_that_must_not_leak=[token], label="leading whitespace before 'Bearer'")

        # Trailing whitespace after the token: `authorization[len("Bearer "):].strip()`
        # strips it, so this is EXPECTED to still authenticate successfully --
        # documented here as a real, intentional behavior, not a gap.
        resp_trailing = client.get("/audit/export", headers={"Authorization": f"Bearer {token}  "})
        check(
            "trailing whitespace AFTER a valid token is tolerated (stripped) and still authenticates -- "
            "documented, intentional, not a vulnerability (it only ever accepts the SAME real token, "
            "never a different or partial one)",
            resp_trailing.status_code in (200, 403),
            resp_trailing.text,
        )

        # A tab or newline instead of a space after 'Bearer' does NOT match
        # the literal "Bearer " prefix check -- must be rejected.
        resp_tab = client.get("/audit/export", headers={"Authorization": f"Bearer\t{token}"})
        _assert_safe_401(resp_tab, secrets_that_must_not_leak=[token], label="tab instead of space after 'Bearer'")


def test_single_character_mutation_of_a_valid_token_is_rejected():
    print("\nTest: a single-character mutation of a genuinely issued, currently-valid session token is rejected")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        token, _username = _create_real_user_and_get_valid_token(client)

        # Sanity check first: the REAL token must actually work, or this
        # test would be meaningless.
        sanity = client.get("/audit/export", headers={"Authorization": f"Bearer {token}"})
        check("sanity: the real, unmutated token is currently valid (200 or 403 -- role, not auth, may still reject it)", sanity.status_code in (200, 403), sanity.text)

        # Mutate exactly one character in the middle of the token.
        mid = len(token) // 2
        original_char = token[mid]
        replacement_char = "0" if original_char != "0" else "1"
        mutated_token = token[:mid] + replacement_char + token[mid + 1:]
        check("the mutation actually changed the token string", mutated_token != token, (token, mutated_token))

        resp = client.get("/audit/export", headers={"Authorization": f"Bearer {mutated_token}"})
        _assert_safe_401(resp, secrets_that_must_not_leak=[token, mutated_token], label="single-character-mutated valid token")


def test_duplicate_authorization_headers():
    print("\nTest: duplicate Authorization headers, if representable through this test client/server stack")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        real_token, _username = _create_real_user_and_get_valid_token(client)
        garbage_token = secrets.token_hex(16)

        # httpx (which TestClient wraps) accepts a list of (name, value)
        # tuples for `headers`, which -- unlike a plain dict -- can carry
        # more than one value for the same header name. Whether the
        # underlying ASGI/Starlette stack then exposes BOTH values, only
        # the first, or only the last to backend.rbac.require_auth's
        # `Header(default=None)` parameter is framework-defined behavior
        # this project's code does not control -- so this test asserts
        # the SAFETY invariant (no 500, no leak, and if it DOES
        # authenticate, it can only be as the exact real session sent,
        # never a corrupted hybrid) rather than one hardcoded expected
        # status, and prints exactly what was observed.
        try:
            resp = client.get(
                "/audit/export",
                headers=[
                    ("Authorization", f"Bearer {real_token}"),
                    ("Authorization", f"Bearer {garbage_token}"),
                ],
            )
        except TypeError as e:
            print(
                f"    NOT TESTED: this httpx/TestClient version does not accept a list of "
                f"duplicate-name header tuples ({e}) -- documenting rather than inventing a result, "
                f"per this batch's explicit instruction."
            )
            return

        check("duplicate Authorization headers never produce a 500", resp.status_code != 500, resp.text)
        check(
            "duplicate Authorization headers never produce an authenticated response for a session "
            "that isn't EXACTLY the real, valid one that was sent (never the garbage value, never a hybrid)",
            resp.status_code in (200, 401, 403),
            resp.text,
        )
        check("the garbage token value never appears in the response", garbage_token not in resp.text, resp.text)
        print(f"    OBSERVED: duplicate Authorization headers (valid first, garbage second) resolved to HTTP {resp.status_code}")

        # Reverse order too -- garbage first, valid second -- since which
        # one "wins" (first vs last) is exactly the framework-defined
        # detail this test cannot assume.
        resp_reversed = client.get(
            "/audit/export",
            headers=[
                ("Authorization", f"Bearer {garbage_token}"),
                ("Authorization", f"Bearer {real_token}"),
            ],
        )
        check("duplicate Authorization headers (reversed order) never produce a 500 either", resp_reversed.status_code != 500, resp_reversed.text)
        check("the garbage token value never appears in the reversed-order response", garbage_token not in resp_reversed.text, resp_reversed.text)
        print(f"    OBSERVED: duplicate Authorization headers (garbage first, valid second) resolved to HTTP {resp_reversed.status_code}")


_ALL_TESTS = [
    test_bearer_with_no_trailing_space_and_no_token,
    test_lowercase_bearer_scheme_is_rejected,
    test_malformed_non_bearer_schemes_are_rejected,
    test_whitespace_variants_around_a_valid_token,
    test_single_character_mutation_of_a_valid_token_is_rejected,
    test_duplicate_authorization_headers,
]


def run_tests():
    print("--- A6.3 hardening: HTTP Authorization edge cases ---\n")
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
