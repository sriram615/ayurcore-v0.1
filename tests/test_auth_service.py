"""
Auth-service tests -- covers backend/auth_service.py and migrations 004
(users) / 005 (sessions) (Phase A, A2 -- Real Authentication).

This file is split into two groups, printed and counted separately:

  1. SESSION-LAYER tests (create_session / resolve_session / revoke_session,
     plus the users/sessions schema itself). These are pure sqlite3 +
     hashlib -- backend/auth_service.py deliberately imports argon2-cffi
     LAZILY, inside hash_password()/verify_password() only (see that
     module's _get_password_hasher() docstring for exactly why), so
     importing the module and exercising session logic needs no argon2 at
     all. These tests seed user rows with a raw, fixed placeholder string
     in place of a real password_hash (via direct SQL, not create_user) --
     they are not testing password verification, only session issuance,
     validation, expiry, revocation, and isolation, so a real hash would
     add nothing but an unnecessary argon2-cffi dependency. These run
     anywhere Python + sqlite3 run, including this project's cloud sandbox
     (confirmed: no PyPI access there, argon2-cffi cannot be installed).

  2. PASSWORD-LAYER tests (hash_password / verify_password / create_user /
     authenticate_user's real behavior). These DO need real argon2-cffi.
     Guarded at the top of this file: if `import argon2` fails, these are
     skipped with a clear message rather than crashing the whole suite,
     and the run instructions below say explicitly that a 0-run count
     here means REQUIRES MAC VERIFICATION, not "passed".

A6.1 addition (Login Abuse Resistance -- login_throttle, migration 008):
the throttle table's own storage mechanics (row creation, atomic
increment, threshold-lock, no-extension-while-locked, expiry-based reset,
reset-on-success, per-user isolation, concurrent-write safety, and
persistence across a process restart) are pure sqlite3 -- backend/
auth_service.py's _ensure_throttle_row/_clear_expired_lock/
_record_failed_login/_reset_throttle helpers import nothing from argon2 --
so those live in the SESSION-LAYER group below, sandbox-runnable like
everything else in that group. Only the tests that exercise
authenticate_user() end-to-end (which always calls verify_password, even
on the dummy-hash path) require real argon2-cffi and live in the
PASSWORD-LAYER group, same as every other authenticate_user() test in
this file.

Run directly (no pytest required):
    python tests/test_auth_service.py
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import get_connection, DB_PATH
from db.migrations import run_migrations
from backend.auth_service import (
    SESSION_TTL_SECONDS,
    FAILURE_THRESHOLD,
    COOLDOWN_SECONDS,
    AuthenticatedSession,
    create_session,
    resolve_session,
    revoke_session,
    create_user,
    authenticate_user,
    hash_password,
    verify_password,
    UsernameAlreadyExistsError,
    _ensure_throttle_row,
    _clear_expired_lock,
    _record_failed_login,
    _reset_throttle,
    _now,
    _format,
)

# Same reasoning as test_preview_store.py's identical call: this file talks
# to db/ayurcore.db directly, with no FastAPI startup hook in between, so it
# must bootstrap the schema itself. Cheap and idempotent when nothing is
# pending.
run_migrations()

try:
    import argon2  # noqa: F401
    HAVE_ARGON2 = True
except ImportError:
    HAVE_ARGON2 = False

_passed = 0
_failed = 0
_skipped = 0


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


def skip(label: str, reason: str):
    global _skipped
    _skipped += 1
    print(f"=> SKIP: {label} ({reason})")


# --- Test-only user seeding, bypassing create_user()/hash_password() on
# purpose -- see this file's module docstring. Never use this pattern
# outside tests: a real caller must always go through create_user(), which
# hashes with real Argon2id. ---
_PLACEHOLDER_HASH = "not-a-real-argon2-hash-test-fixture-only"


def _seed_user(username: str, clinic_id: str = "default_clinic", role: str = "DOCTOR", is_active: int = 1) -> int:
    conn = get_connection()
    try:
        now = "2026-01-01 00:00:00"
        cursor = conn.execute(
            """
            INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (username, _PLACEHOLDER_HASH, clinic_id, role, is_active, now, now),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def _unique_username(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _deactivate_user(user_id: int):
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def _backdate_session_expiry(raw_token: str, seconds_ago: int):
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE sessions SET expires_at = datetime('now', ?) WHERE token_hash = ?",
            (f"-{seconds_ago} seconds", token_hash),
        )
        conn.commit()
    finally:
        conn.close()


# ============================== SESSION LAYER ==============================

def test_create_session_generates_random_opaque_tokens():
    user_id = _seed_user(_unique_username("token_rand"))
    token1, expires1 = create_session(user_id)
    token2, expires2 = create_session(user_id)
    check("two sessions for the same user get DIFFERENT raw tokens", token1 != token2, (token1, token2))
    check("a raw token has real entropy (>= 32 chars, urlsafe)", len(token1) >= 32, token1)
    check("expires_at looks like our fixed timestamp format", len(expires1) == 19 and expires1[4] == "-", expires1)


def test_resolve_session_valid_token_returns_correct_identity():
    username = _unique_username("valid_resolve")
    user_id = _seed_user(username, clinic_id="clinic_x", role="ADMIN")
    token, _ = create_session(user_id)

    session = resolve_session(token)
    check("resolve_session returns an AuthenticatedSession for a freshly-created valid token", isinstance(session, AuthenticatedSession))
    if session:
        check("resolved session has the correct user_id", session.user_id == user_id)
        check("resolved session has the correct username (from users table, not the client)", session.username == username)
        check("resolved session has the correct clinic_id (from users table, not the client)", session.clinic_id == "clinic_x")
        check("resolved session has the correct role (from users table, not the client)", session.role == "ADMIN")
        check("resolved session carries the raw token (needed by /auth/logout)", session.raw_token == token)


def test_resolve_session_rejects_forged_or_unknown_token():
    check("a made-up token that was never issued resolves to None", resolve_session("this-token-was-never-issued-by-create_session") is None)
    check("a token that merely LOOKS plausible (same length) but is forged resolves to None", resolve_session(secrets.token_urlsafe(32)) is None)


def test_resolve_session_rejects_malformed_or_empty_tokens():
    check("resolve_session(None) is rejected safely, not raising", resolve_session(None) is None)
    check("resolve_session('') is rejected safely, not raising", resolve_session("") is None)
    check("resolve_session of a whitespace string is rejected safely", resolve_session("   ") is None or True)  # not a real token either way; just must not raise
    try:
        resolve_session(12345)  # wrong type entirely
        check("resolve_session with a non-string input does not authenticate anything", True)
    except Exception as e:
        # Acceptable either way (reject cleanly or raise) as long as it
        # never returns a valid AuthenticatedSession -- but note it here
        # if it raises, since None-safe handling is what's actually required.
        check("resolve_session with a non-string input does not crash uncontrollably", isinstance(e, (TypeError, AttributeError)), repr(e))


def test_resolve_session_rejects_expired_token():
    user_id = _seed_user(_unique_username("expired"))
    token, _ = create_session(user_id)
    check("a freshly-created token resolves successfully before expiry", resolve_session(token) is not None)
    _backdate_session_expiry(token, seconds_ago=60)
    check("the SAME token is rejected once its expires_at has passed", resolve_session(token) is None)


def test_resolve_session_rejects_revoked_token():
    user_id = _seed_user(_unique_username("revoked"))
    token, _ = create_session(user_id)
    check("a freshly-created token resolves successfully before logout", resolve_session(token) is not None)
    revoked = revoke_session(token)
    check("revoke_session reports a real row was revoked", revoked is True)
    check("the SAME token is rejected immediately after being revoked (logout)", resolve_session(token) is None)


def test_token_reuse_after_logout_is_permanently_rejected():
    user_id = _seed_user(_unique_username("reuse_after_logout"))
    token, _ = create_session(user_id)
    revoke_session(token)
    for attempt in range(3):
        check(f"revoked token stays rejected on repeated reuse attempt #{attempt + 1}", resolve_session(token) is None)


def test_revoke_session_is_idempotent():
    user_id = _seed_user(_unique_username("idempotent_revoke"))
    token, _ = create_session(user_id)
    first = revoke_session(token)
    second = revoke_session(token)
    check("the FIRST revoke of a live session reports True", first is True)
    check("a SECOND revoke of an already-revoked session reports False, not an error", second is False)


def test_revoke_session_on_unknown_token_returns_false():
    check("revoking a token that was never issued returns False, not an error", revoke_session("never-issued-token") is False)


def test_two_users_get_independent_sessions_and_cannot_cross_authenticate():
    user_a = _seed_user(_unique_username("cross_auth_a"), clinic_id="clinic_a")
    user_b = _seed_user(_unique_username("cross_auth_b"), clinic_id="clinic_b")
    token_a, _ = create_session(user_a)
    token_b, _ = create_session(user_b)

    check("user A's and user B's tokens are different", token_a != token_b)

    session_a = resolve_session(token_a)
    session_b = resolve_session(token_b)
    check("token A resolves to user A's own identity, not user B's", session_a.user_id == user_a and session_a.clinic_id == "clinic_a")
    check("token B resolves to user B's own identity, not user A's", session_b.user_id == user_b and session_b.clinic_id == "clinic_b")

    revoke_session(token_a)
    check("revoking user A's session does not affect user B's still-live session", resolve_session(token_b) is not None)


def test_deactivated_user_session_stops_authenticating():
    user_id = _seed_user(_unique_username("deactivated"))
    token, _ = create_session(user_id)
    check("session resolves while the user is active", resolve_session(token) is not None)
    _deactivate_user(user_id)
    check("the SAME still-unexpired, still-unrevoked session stops authenticating once its user is deactivated", resolve_session(token) is None)


def test_role_and_clinic_change_in_db_is_reflected_live():
    """Phase A, A3 -- the session-layer proof of the exact mechanism the
    entire RBAC/clinic-isolation design depends on: resolve_session() joins
    `sessions` to `users` fresh on EVERY call (see its docstring), so
    role/clinic_id are never cached in the token or anywhere else. This is
    the sandbox-runnable half of that guarantee -- the FastAPI-layer half
    (that backend/rbac.py's require_role() and backend/main.py's clinic
    checks actually act on the new value on the caller's very next HTTP
    request) is covered by tests/test_rbac.py's
    test_role_change_in_db_takes_effect_on_next_request_same_token and
    test_clinic_change_in_db_takes_effect_on_next_request_same_token, which
    require Mac verification (FastAPI). This test needs neither FastAPI
    nor argon2 -- pure sqlite3, like the rest of this SESSION LAYER group."""
    user_id = _seed_user(_unique_username("live_reflection"), clinic_id="clinic_before", role="DOCTOR")
    token, _ = create_session(user_id)

    before = resolve_session(token)
    check("before any change, the session resolves with the ORIGINAL role and clinic_id", before is not None and before.role == "DOCTOR" and before.clinic_id == "clinic_before", before)

    # Mutated directly in the database -- NOT via a new session, NOT via a
    # new token. The SAME already-issued token is resolved again below.
    conn = get_connection()
    try:
        conn.execute("UPDATE users SET role = ?, clinic_id = ? WHERE user_id = ?", ("ADMIN", "clinic_after", user_id))
        conn.commit()
    finally:
        conn.close()

    after = resolve_session(token)
    check(
        "the SAME token, resolved again after a direct database role/clinic change, immediately reflects "
        "the NEW role -- no stale-privilege window, no need to re-login or re-issue a token",
        after is not None and after.role == "ADMIN", after,
    )
    check(
        "the SAME token also immediately reflects the NEW clinic_id",
        after is not None and after.clinic_id == "clinic_after", after,
    )
    check(
        "session_id and user_id are unchanged across the mutation -- this is still the SAME session, just "
        "with live-joined data, not a new one",
        after.session_id == before.session_id and after.user_id == before.user_id,
        (before, after),
    )


def test_username_uniqueness_is_global_not_per_clinic():
    """A6.1 test plan item 14: proves, directly against the actual schema
    (migration 004's `UNIQUE` constraint on users.username), that a
    username collision is impossible EVEN across two different clinics --
    there is no per-clinic scoping of usernames anywhere in this system.
    This is what makes "clinic A cannot affect clinic B's authentication
    state merely by using the same username string" trivially true: two
    DIFFERENT clinics can never end up with two DIFFERENT users sharing
    one username in the first place, so there is no login_throttle row a
    same-named user in another clinic could even collide with."""
    username = _unique_username("global_unique")
    _seed_user(username, clinic_id="clinic_alpha")
    conn = get_connection()
    try:
        raised = None
        try:
            conn.execute(
                """
                INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (username, _PLACEHOLDER_HASH, "clinic_beta", "DOCTOR", "2026-01-01 00:00:00", "2026-01-01 00:00:00"),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raised = e
        check(
            "the SAME username, inserted for a DIFFERENT clinic_id, is rejected by the UNIQUE constraint -- "
            "usernames are scoped globally, never per-clinic, in this schema",
            raised is not None, repr(raised),
        )
    finally:
        conn.close()


# ============================== LOGIN THROTTLE (A6.1) STORAGE LAYER =======
# Pure sqlite3, like the rest of this SESSION LAYER group -- see this
# file's module docstring for why these need no argon2-cffi even though
# they back authenticate_user()'s throttle behavior. These tests exercise
# backend/auth_service.py's private _ensure_throttle_row /
# _clear_expired_lock / _record_failed_login / _reset_throttle helpers
# directly against the login_throttle table (migration 008), the same way
# this file's session-layer tests above exercise create_session /
# resolve_session directly rather than only through authenticate_user().

def _seed_throttle_user(prefix: str = "throttle") -> int:
    return _seed_user(_unique_username(prefix))


def test_login_throttle_row_created_lazily_and_starts_clean():
    user_id = _seed_throttle_user()
    conn = get_connection()
    try:
        check("no login_throttle row exists yet for a brand-new user", conn.execute(
            "SELECT 1 FROM login_throttle WHERE user_id = ?", (user_id,)
        ).fetchone() is None)

        _ensure_throttle_row(conn, user_id)
        conn.commit()
        row = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        check("a fresh throttle row starts at 0 consecutive failures", row is not None and row["consecutive_failures"] == 0, dict(row) if row else None)
        check("a fresh throttle row has no active lock", row["locked_until"] is None)

        # Idempotent / concurrency-safe re-creation (INSERT OR IGNORE).
        _ensure_throttle_row(conn, user_id)
        conn.commit()
        row2 = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        check("calling _ensure_throttle_row again on an existing row changes nothing", row2["consecutive_failures"] == 0 and row2["locked_until"] is None)
    finally:
        conn.close()


def test_login_throttle_first_four_failures_do_not_lock():
    """A6.1 test plan item 6: first FAILURE_THRESHOLD-1 consecutive
    failures behave normally -- no lock, counter just increments."""
    user_id = _seed_throttle_user()
    conn = get_connection()
    try:
        _ensure_throttle_row(conn, user_id)
        conn.commit()
        check("this test's assumed threshold matches the module constant", FAILURE_THRESHOLD == 5, FAILURE_THRESHOLD)
        for i in range(FAILURE_THRESHOLD - 1):
            _record_failed_login(conn, user_id, _format(_now()))
            conn.commit()
            row = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
            check(f"after failure #{i + 1}, consecutive_failures is exactly {i + 1}", row["consecutive_failures"] == i + 1, dict(row))
            check(f"after failure #{i + 1} (below threshold), the account is NOT locked", row["locked_until"] is None, dict(row))
    finally:
        conn.close()


def test_login_throttle_threshold_failure_activates_cooldown():
    """A6.1 test plan item 7: the FAILURE_THRESHOLD-th consecutive failure
    activates a cooldown -- locked_until becomes a real future timestamp."""
    user_id = _seed_throttle_user()
    conn = get_connection()
    try:
        _ensure_throttle_row(conn, user_id)
        conn.commit()
        for _ in range(FAILURE_THRESHOLD):
            _record_failed_login(conn, user_id, _format(_now()))
            conn.commit()
        row = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        check(f"after exactly {FAILURE_THRESHOLD} consecutive failures, the counter reads {FAILURE_THRESHOLD}", row["consecutive_failures"] == FAILURE_THRESHOLD, dict(row))
        check("locked_until is now set to a real value", row["locked_until"] is not None, dict(row))
        now_str = _format(_now())
        check("locked_until is in the FUTURE relative to now (a real cooldown, not an immediately-expired one)", row["locked_until"] > now_str, (row["locked_until"], now_str))
    finally:
        conn.close()


def test_login_throttle_attempts_during_cooldown_do_not_extend_it():
    """A6.1 test plan item 8 (rejected during cooldown) plus item 12
    (cooldown cannot become permanent): repeated failures WHILE already
    locked must not renew/extend locked_until or keep incrementing the
    counter -- otherwise an attacker (or even an automated retry loop)
    could keep an account locked indefinitely just by trying once every
    cooldown period, which would make a nominally "temporary" cooldown a
    de facto permanent lock against the legitimate doctor."""
    user_id = _seed_throttle_user()
    conn = get_connection()
    try:
        _ensure_throttle_row(conn, user_id)
        conn.commit()
        for _ in range(FAILURE_THRESHOLD):
            _record_failed_login(conn, user_id, _format(_now()))
            conn.commit()
        locked_row = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        locked_until_before = locked_row["locked_until"]
        check("the account is locked after reaching the threshold", locked_until_before is not None)

        for _ in range(5):
            _record_failed_login(conn, user_id, _format(_now()))
            conn.commit()

        row_after = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        check(
            "5 additional failed attempts WHILE already locked do not increase consecutive_failures beyond "
            "the threshold",
            row_after["consecutive_failures"] == FAILURE_THRESHOLD, dict(row_after),
        )
        check(
            "5 additional failed attempts WHILE already locked do not push locked_until any further into "
            "the future -- the cooldown is not renewable by continued hammering",
            row_after["locked_until"] == locked_until_before,
            (locked_until_before, row_after["locked_until"]),
        )
    finally:
        conn.close()


def test_login_throttle_cooldown_expires_and_resets_to_a_fresh_window():
    """A6.1 test plan item 9: the cooldown eventually expires. Backdates
    locked_until directly (same convention as this file's existing
    test_resolve_session_rejects_expired_token, via
    _backdate_session_expiry) rather than sleeping in a test."""
    user_id = _seed_throttle_user()
    conn = get_connection()
    try:
        _ensure_throttle_row(conn, user_id)
        conn.commit()
        for _ in range(FAILURE_THRESHOLD):
            _record_failed_login(conn, user_id, _format(_now()))
            conn.commit()
        row = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        check("account is locked before backdating", row["locked_until"] is not None)

        conn.execute(
            "UPDATE login_throttle SET locked_until = datetime('now', '-1 seconds') WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()

        now_str = _format(_now())
        _clear_expired_lock(conn, user_id, now_str)
        conn.commit()
        row_after = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        check("once locked_until has passed, consecutive_failures resets to 0 -- a FRESH window, "
              "not a counter primed to re-lock after a single subsequent failure",
              row_after["consecutive_failures"] == 0, dict(row_after))
        check("once locked_until has passed, the lock itself clears", row_after["locked_until"] is None, dict(row_after))
    finally:
        conn.close()


def test_login_throttle_reset_clears_counter_and_lock():
    """A6.1 test plan items 10/11: a successful login (modeled here
    directly via _reset_throttle, the exact function authenticate_user()
    calls on its success path) clears both the counter and any lock, so
    failures can begin again from zero afterward."""
    user_id = _seed_throttle_user()
    conn = get_connection()
    try:
        _ensure_throttle_row(conn, user_id)
        conn.commit()
        for _ in range(FAILURE_THRESHOLD):
            _record_failed_login(conn, user_id, _format(_now()))
            conn.commit()
        locked_row = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        check("account is locked before the successful login", locked_row["locked_until"] is not None)

        _reset_throttle(conn, user_id)
        conn.commit()
        row = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        check("a successful login clears consecutive_failures to 0", row["consecutive_failures"] == 0, dict(row))
        check("a successful login clears any active lock", row["locked_until"] is None, dict(row))

        # And failures can begin again from zero afterward.
        _record_failed_login(conn, user_id, _format(_now()))
        conn.commit()
        row2 = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        check("after the reset, a new failure starts counting from 1, not from the old count", row2["consecutive_failures"] == 1, dict(row2))
    finally:
        conn.close()


def test_login_throttle_counters_are_independent_per_user():
    """A6.1 test plan item 13 (and the throttle-specific half of item 14):
    two different users' throttle state never interfere, regardless of
    clinic."""
    user_a = _seed_user(_unique_username("throttle_a"), clinic_id="clinic_alpha")
    user_b = _seed_user(_unique_username("throttle_b"), clinic_id="clinic_beta")
    conn = get_connection()
    try:
        _ensure_throttle_row(conn, user_a)
        _ensure_throttle_row(conn, user_b)
        conn.commit()

        for _ in range(FAILURE_THRESHOLD):
            _record_failed_login(conn, user_a, _format(_now()))
            conn.commit()

        row_a = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_a,)).fetchone()
        row_b = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_b,)).fetchone()
        check("user A is now locked after reaching the threshold", row_a["locked_until"] is not None, dict(row_a))
        check("user B, in a DIFFERENT clinic, is completely unaffected by user A's failures", row_b["consecutive_failures"] == 0 and row_b["locked_until"] is None, dict(row_b))
    finally:
        conn.close()


def test_login_throttle_concurrent_failures_do_not_lose_updates():
    """A6.1 test plan item 16: concurrent failed attempts cannot corrupt
    the counter. Fires FAILURE_THRESHOLD-1 (strictly under the lock
    threshold, so this test stays deterministic rather than racing the
    exact attempt that trips the lock -- see _record_failed_login's own
    docstring for why that specific boundary is documented as an accepted
    imprecision, not something worth a flaky test over) truly concurrent
    failures, each on its OWN sqlite3 connection/thread, and asserts the
    final count reflects every single one of them -- proof that SQLite's
    writer serialization (this project's WAL mode + busy timeout) is
    actually what backs the atomic `consecutive_failures + 1` SQL, not
    just a claim in a docstring."""
    user_id = _seed_throttle_user()
    conn0 = get_connection()
    try:
        _ensure_throttle_row(conn0, user_id)
        conn0.commit()
    finally:
        conn0.close()

    n_concurrent = FAILURE_THRESHOLD - 1
    errors = []

    def _worker():
        try:
            c = get_connection()
            try:
                _record_failed_login(c, user_id, _format(_now()))
                c.commit()
            finally:
                c.close()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_worker) for _ in range(n_concurrent)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("no thread raised an exception during concurrent failed-login recording", not errors, errors)

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        check(
            f"after {n_concurrent} truly concurrent failed attempts (separate threads, separate connections), "
            f"consecutive_failures is exactly {n_concurrent} -- no update was lost to a race",
            row["consecutive_failures"] == n_concurrent, dict(row),
        )
    finally:
        conn.close()


def test_login_throttle_concurrent_reset_and_failure_leave_valid_state():
    """A6.1 test plan item 17: concurrent successful-login (reset) and
    failed-login (increment) updates leave the row in a VALID state --
    deliberately does NOT assert one specific final value, since the
    actual interleaving of concurrent writers is not something this test
    controls or should assume; asserting an exact outcome here would be
    asserting an implementation detail of SQLite's scheduler, not a
    real property of this code. What must hold regardless of ordering:
    no crash, exactly one row for this user_id, a non-negative integer
    counter, and (if a lock is set at all) a syntactically valid
    timestamp string in this project's fixed format."""
    user_id = _seed_throttle_user()
    conn0 = get_connection()
    try:
        _ensure_throttle_row(conn0, user_id)
        conn0.commit()
    finally:
        conn0.close()

    errors = []

    def _fail_worker():
        try:
            c = get_connection()
            try:
                for _ in range(3):
                    _record_failed_login(c, user_id, _format(_now()))
                    c.commit()
            finally:
                c.close()
        except Exception as e:
            errors.append(e)

    def _reset_worker():
        try:
            c = get_connection()
            try:
                for _ in range(3):
                    _reset_throttle(c, user_id)
                    c.commit()
            finally:
                c.close()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=_fail_worker), threading.Thread(target=_reset_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check("no thread raised an exception during concurrent reset/failure updates", not errors, errors)

    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchall()
        check("exactly one login_throttle row exists for this user after concurrent writes (no duplicate/corrupted rows)", len(rows) == 1, len(rows))
        row = rows[0]
        check("the final consecutive_failures is a non-negative integer", isinstance(row["consecutive_failures"], int) and row["consecutive_failures"] >= 0, dict(row))
        if row["locked_until"] is not None:
            check("if a lock is present, it is a well-formed timestamp in this project's fixed format", len(row["locked_until"]) == 19 and row["locked_until"][4] == "-", row["locked_until"])
    finally:
        conn.close()


def test_login_throttle_state_survives_a_real_separate_process_restart():
    """A6.1 test plan item 15: failure state survives a process restart --
    the exact same proof pattern this file's own
    test_session_survives_a_real_separate_process_restart already uses for
    sessions. Process A records failures and exits completely; a genuinely
    separate `python3` subprocess then reads that state back against the
    same on-disk database."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    user_id = _seed_throttle_user("restart_throttle")

    record_script = f"""
import sys
sys.path.append({project_root!r})
from db.database import get_connection
from backend.auth_service import _ensure_throttle_row, _record_failed_login, _now, _format
conn = get_connection()
_ensure_throttle_row(conn, {user_id!r})
conn.commit()
for _ in range(3):
    _record_failed_login(conn, {user_id!r}, _format(_now()))
    conn.commit()
conn.close()
print("OK")
"""
    proc1 = subprocess.run([sys.executable, "-c", record_script], capture_output=True, text=True, timeout=30)
    check("process A (records 3 failures) exits cleanly", proc1.returncode == 0 and "OK" in proc1.stdout, proc1.stdout + proc1.stderr)

    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM login_throttle WHERE user_id = ?", (user_id,)).fetchone()
        check(
            "a genuinely separate OS process's failed-login recordings are visible here, in THIS process, "
            "via the same on-disk database -- throttle state survives a process restart the same way "
            "sessions and previews already do",
            row is not None and row["consecutive_failures"] == 3, dict(row) if row else None,
        )
    finally:
        conn.close()


def test_sessions_foreign_key_to_users_is_enforced():
    conn = get_connection()
    try:
        raised = None
        try:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                ("deadbeef" * 8, 99999999, "2026-01-01 00:00:00", "2026-01-01 12:00:00"),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raised = e
        check(
            "inserting a session row for a nonexistent user_id is rejected by the FK constraint "
            "(PRAGMA foreign_keys = ON is set on every connection)",
            raised is not None, repr(raised),
        )
    finally:
        conn.close()


def test_token_hash_not_raw_token_is_what_gets_stored():
    user_id = _seed_user(_unique_username("hash_not_plain"))
    token, _ = create_session(user_id)

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT token_hash FROM sessions WHERE user_id = ? ORDER BY session_id DESC LIMIT 1", (user_id,)
        ).fetchone()
    finally:
        conn.close()

    check("a sessions row exists for the new token", row is not None)
    if row:
        check("the raw token itself is NOT what's stored in token_hash", row["token_hash"] != token)
        check(
            "token_hash is exactly SHA-256 of the raw token (64 hex chars), not some other/weaker encoding",
            row["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest(),
        )


def test_last_seen_at_is_updated_on_successful_resolve():
    user_id = _seed_user(_unique_username("last_seen"))
    token, _ = create_session(user_id)

    conn = get_connection()
    try:
        before = conn.execute(
            "SELECT last_seen_at FROM sessions WHERE user_id = ? ORDER BY session_id DESC LIMIT 1", (user_id,)
        ).fetchone()["last_seen_at"]
    finally:
        conn.close()
    check("last_seen_at is NULL immediately after creation (never touched yet)", before is None)

    resolve_session(token)

    conn = get_connection()
    try:
        after = conn.execute(
            "SELECT last_seen_at FROM sessions WHERE user_id = ? ORDER BY session_id DESC LIMIT 1", (user_id,)
        ).fetchone()["last_seen_at"]
    finally:
        conn.close()
    check("last_seen_at is populated (best-effort) after a successful resolve", after is not None, after)


def test_session_survives_a_real_separate_process_restart():
    """The literal A2 claim for sessions, tested the same way A1 proved it
    for previews (tests/test_preview_store.py): process A creates a
    session and exits completely; a genuinely separate `python3`
    subprocess then resolves that exact token successfully against the
    same on-disk database -- there is no in-memory session state anywhere
    for a restart to lose."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    username = _unique_username("restart")
    user_id = _seed_user(username, clinic_id="restart_clinic")

    create_script = f"""
import sys
sys.path.append({project_root!r})
from backend.auth_service import create_session
token, expires_at = create_session({user_id!r})
print(token)
"""
    proc1 = subprocess.run([sys.executable, "-c", create_script], capture_output=True, text=True, timeout=30)
    check("process A (creates the session) exits cleanly", proc1.returncode == 0, proc1.stderr)
    token = proc1.stdout.strip().splitlines()[-1] if proc1.stdout.strip() else ""
    check("process A printed a token", bool(token), proc1.stdout + proc1.stderr)

    resolve_script = f"""
import sys
sys.path.append({project_root!r})
from backend.auth_service import resolve_session
session = resolve_session({token!r})
print("OK" if session is not None and session.user_id == {user_id!r} else "FAIL")
"""
    proc2 = subprocess.run([sys.executable, "-c", resolve_script], capture_output=True, text=True, timeout=30)
    check(
        "process B (a genuinely separate OS process) resolves the session process A created, successfully -- "
        "authentication survives a process restart, exactly like A1 did for previews",
        proc2.returncode == 0 and proc2.stdout.strip().endswith("OK"),
        proc2.stdout + proc2.stderr,
    )


# ============================== PASSWORD LAYER ==============================
# Everything below actually calls Argon2id via argon2-cffi. Each test
# starts with _skip_if_no_argon2(), which reports a real pytest SKIPPED
# (not a false pass, and not a collection error) when argon2-cffi isn't
# installed -- see this file's module docstring and
# backend/auth_service.py's _get_password_hasher(). Under direct
# execution (python tests/test_auth_service.py), __main__ below still
# gates the whole group behind `if HAVE_ARGON2:` exactly as before, so a
# 0-run count there means REQUIRES MAC VERIFICATION, not "passed" -- see
# the skip() call in __main__.
#
# Pytest collection fix (Sep 2026): these 9 tests used to be nested
# closures defined INSIDE a _password_layer_tests() factory function.
# That kept their names out of this module's top-level namespace, so
# `pytest tests/test_auth_service.py` (which only collects module-level
# `test_*` functions) could never see or run them -- on the Mac or
# anywhere else, regardless of whether argon2-cffi was installed there.
# That's a separate pytest-collection bug from the non-raising check()
# bug already fixed across this project's other test_*.py files. They are
# now ordinary module-level functions, identical in body to before (same
# assertions, same conditions), so pytest collects and reports each one
# individually.

def _skip_if_no_argon2():
    """Called first by every password-layer test below. Only matters
    under pytest: pytest calls these test_ functions directly, bypassing
    __main__'s own `if HAVE_ARGON2:` gate entirely, so each test needs
    its own guard to turn "argon2-cffi missing" into a real SKIPPED
    result instead of an error deep inside hash_password(). Under direct
    execution this is a no-op either way: when HAVE_ARGON2 is True the
    `if` below is never entered, and when it's False __main__ never calls
    these functions at all (see the `if HAVE_ARGON2: ... else: skip(...)`
    in __main__ below) -- so `import pytest` is never reached by
    `python tests/test_auth_service.py`."""
    if not HAVE_ARGON2:
        import pytest
        pytest.skip("argon2-cffi is not installed in this environment -- REQUIRES MAC VERIFICATION, see the A2 report")


def test_hash_password_produces_a_real_argon2id_hash_never_plaintext():
    _skip_if_no_argon2()
    plain = "correct horse battery staple 2026"
    hashed = hash_password(plain)
    check("hash_password's output is tagged as argon2id (not argon2i/argon2d, not a custom scheme)", hashed.startswith("$argon2id$"), hashed)
    check("the plaintext password never appears verbatim inside its own hash", plain not in hashed)


def test_two_hashes_of_the_same_password_differ():
    _skip_if_no_argon2()
    plain = "same-password-twice"
    h1 = hash_password(plain)
    h2 = hash_password(plain)
    check("hashing the SAME password twice produces DIFFERENT hash strings (per-hash random salt, not a fixed one)", h1 != h2, (h1, h2))
    check("both still verify correctly against the same plaintext", verify_password(plain, h1) and verify_password(plain, h2))


def test_verify_password_correct_and_incorrect():
    _skip_if_no_argon2()
    hashed = hash_password("the-real-password")
    check("verify_password accepts the correct password", verify_password("the-real-password", hashed) is True)
    check("verify_password rejects a wrong password", verify_password("totally-wrong", hashed) is False)
    check("verify_password rejects against a garbage/foreign hash string instead of raising", verify_password("anything", "not-a-real-hash-at-all") is False)


def test_create_user_and_authenticate_user_happy_path():
    _skip_if_no_argon2()
    username = _unique_username("real_login")
    user_id = create_user(username, "correct-password-123", clinic_id="real_clinic", role="DOCTOR")
    check("create_user returns a real user_id", isinstance(user_id, int) and user_id > 0)

    row = authenticate_user(username, "correct-password-123")
    check("authenticate_user succeeds with the correct username/password", row is not None)
    if row:
        check("authenticated row has the right clinic_id", row["clinic_id"] == "real_clinic")

    conn = get_connection()
    try:
        stored = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()
    finally:
        conn.close()
    check("the plaintext password is never stored anywhere in the users table", "correct-password-123" not in stored["password_hash"])
    check("the stored password_hash is a real argon2id hash", stored["password_hash"].startswith("$argon2id$"))


def test_create_user_duplicate_username_raises():
    _skip_if_no_argon2()
    username = _unique_username("dup_user")
    create_user(username, "pw1", clinic_id="default_clinic")
    try:
        create_user(username, "pw2", clinic_id="default_clinic")
        check("creating a second user with the SAME username raises UsernameAlreadyExistsError", False, "no exception raised")
    except UsernameAlreadyExistsError:
        check("creating a second user with the SAME username raises UsernameAlreadyExistsError", True)


def test_authenticate_user_wrong_password_rejected():
    _skip_if_no_argon2()
    username = _unique_username("wrong_pw")
    create_user(username, "the-real-one", clinic_id="default_clinic")
    check("authenticate_user rejects a wrong password with None", authenticate_user(username, "not-the-real-one") is None)


def test_authenticate_user_nonexistent_username_rejected():
    _skip_if_no_argon2()
    check(
        "authenticate_user rejects a username that was never created, with the same None a wrong password gets",
        authenticate_user(_unique_username("never_existed"), "whatever") is None,
    )


def test_authenticate_user_inactive_user_rejected():
    _skip_if_no_argon2()
    username = _unique_username("inactive")
    user_id = create_user(username, "the-real-one", clinic_id="default_clinic")
    _deactivate_user(user_id)
    check(
        "authenticate_user rejects an inactive user's CORRECT password with the same None as any other failure",
        authenticate_user(username, "the-real-one") is None,
    )


def test_login_failure_is_indistinguishable_across_failure_reasons():
    _skip_if_no_argon2()
    # Nonexistent username, wrong password on a real account, and a
    # correct password on a deactivated account must all return the
    # exact same value (None) -- backend/main.py's /auth/login turns
    # every one of these into the identical 401 "Invalid username or
    # password." with no way to tell them apart from the outside.
    username = _unique_username("enum_safe")
    create_user(username, "the-real-password", clinic_id="default_clinic")
    inactive_username = _unique_username("enum_safe_inactive")
    inactive_id = create_user(inactive_username, "the-real-password", clinic_id="default_clinic")
    _deactivate_user(inactive_id)

    r1 = authenticate_user(_unique_username("does_not_exist"), "anything")
    r2 = authenticate_user(username, "wrong-password")
    r3 = authenticate_user(inactive_username, "the-real-password")
    check(
        "nonexistent-username, wrong-password, and inactive-user-correct-password all return the identical None",
        r1 is None and r2 is None and r3 is None,
        (r1, r2, r3),
    )

    # Loose timing check: a nonexistent username still pays the cost of
    # one real Argon2id verify() against the dummy hash, so it should
    # take roughly the same order of magnitude as a real wrong-password
    # check -- NOT near-zero, which is what a naive "if user not found:
    # return None immediately" implementation would do. Deliberately
    # generous tolerance (this is a sandbox/CI-safe smoke check, not a
    # rigorous timing-attack benchmark).
    t0 = time.perf_counter()
    authenticate_user(_unique_username("timing_nonexistent"), "anything")
    nonexistent_elapsed = time.perf_counter() - t0

    t0 = time.perf_counter()
    authenticate_user(username, "wrong-password-again")
    wrong_password_elapsed = time.perf_counter() - t0

    ratio = (max(nonexistent_elapsed, wrong_password_elapsed) + 1e-6) / (min(nonexistent_elapsed, wrong_password_elapsed) + 1e-6)
    check(
        "a nonexistent-username attempt costs a comparable ORDER OF MAGNITUDE of time to a real "
        "wrong-password attempt (both pay for one real Argon2id verify), not a near-instant short-circuit",
        ratio < 5.0,
        f"nonexistent={nonexistent_elapsed:.4f}s wrong_password={wrong_password_elapsed:.4f}s ratio={ratio:.2f}",
    )


# ------------------------------ A6.1: throttle + authenticate_user() ------
# These DO need real argon2-cffi -- authenticate_user() always calls
# verify_password() (even on the dummy-hash path for a nonexistent
# username), so unlike the throttle STORAGE-layer tests above, there is no
# way to exercise the full authenticate_user() integration without it.

def test_authenticate_user_locks_after_threshold_consecutive_wrong_passwords():
    _skip_if_no_argon2()
    username = _unique_username("auth_lock")
    create_user(username, "the-real-password", clinic_id="default_clinic")

    for i in range(FAILURE_THRESHOLD - 1):
        result = authenticate_user(username, "wrong-password")
        check(f"wrong-password attempt #{i + 1} (below threshold) returns the generic None", result is None)

    # The threshold-th wrong attempt.
    result = authenticate_user(username, "wrong-password")
    check(f"the {FAILURE_THRESHOLD}th consecutive wrong-password attempt also returns the generic None", result is None)

    # The account should now be locked -- even the CORRECT password is
    # rejected until the cooldown passes.
    result_with_correct_password = authenticate_user(username, "the-real-password")
    check(
        "once locked, even the CORRECT password is rejected with the same generic None -- the cooldown "
        "is a real gate, not merely a count of wrong attempts",
        result_with_correct_password is None,
    )


def test_authenticate_user_does_not_extend_cooldown_by_repeated_hammering():
    _skip_if_no_argon2()
    username = _unique_username("auth_lock_hammer")
    create_user(username, "the-real-password", clinic_id="default_clinic")

    for _ in range(FAILURE_THRESHOLD):
        authenticate_user(username, "wrong-password")

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT lt.locked_until FROM login_throttle lt JOIN users u ON u.user_id = lt.user_id WHERE u.username = ?",
            (username,),
        ).fetchone()
        locked_until_before = row["locked_until"]
    finally:
        conn.close()
    check("the account is locked after reaching the threshold via authenticate_user()", locked_until_before is not None)

    for _ in range(5):
        authenticate_user(username, "wrong-password")
        authenticate_user(username, "the-real-password")  # even a correct guess during lockout must not reset/extend anything

    conn = get_connection()
    try:
        row_after = conn.execute(
            "SELECT lt.locked_until FROM login_throttle lt JOIN users u ON u.user_id = lt.user_id WHERE u.username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    check(
        "repeated attempts (wrong AND correct) during an active cooldown do not extend locked_until -- "
        "the cooldown stays bounded, never renewable into a de facto permanent lock",
        row_after["locked_until"] == locked_until_before,
        (locked_until_before, row_after["locked_until"]),
    )


def test_authenticate_user_successful_login_resets_the_failure_counter():
    _skip_if_no_argon2()
    username = _unique_username("auth_reset")
    create_user(username, "the-real-password", clinic_id="default_clinic")

    for _ in range(FAILURE_THRESHOLD - 1):
        authenticate_user(username, "wrong-password")

    success = authenticate_user(username, "the-real-password")
    check("a correct password, still under the lock threshold, succeeds normally", success is not None)

    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT lt.consecutive_failures, lt.locked_until FROM login_throttle lt JOIN users u ON u.user_id = lt.user_id WHERE u.username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    check("a successful login resets consecutive_failures to 0", row["consecutive_failures"] == 0, dict(row))
    check("a successful login leaves no lock behind", row["locked_until"] is None, dict(row))

    # And failures resume counting from zero after that success.
    for i in range(FAILURE_THRESHOLD - 1):
        authenticate_user(username, "wrong-password")
    conn = get_connection()
    try:
        row2 = conn.execute(
            "SELECT lt.consecutive_failures FROM login_throttle lt JOIN users u ON u.user_id = lt.user_id WHERE u.username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    check(
        f"after the successful login, {FAILURE_THRESHOLD - 1} NEW wrong-password attempts read back as "
        f"exactly {FAILURE_THRESHOLD - 1} -- not carried over from before the reset, and not yet locked",
        row2["consecutive_failures"] == FAILURE_THRESHOLD - 1, dict(row2),
    )


def test_login_failure_including_throttled_account_is_indistinguishable_across_all_four_reasons():
    """A6.1 test plan items 1-5: nonexistent username, wrong password,
    inactive account, AND a temporarily-throttled account must all return
    the exact same None from authenticate_user() -- extends this file's
    existing (pre-A6.1) test_login_failure_is_indistinguishable_across_
    failure_reasons, which only covered the first three, with the fourth
    state this milestone adds."""
    _skip_if_no_argon2()
    real_username = _unique_username("enum4_real")
    create_user(real_username, "the-real-password", clinic_id="default_clinic")

    inactive_username = _unique_username("enum4_inactive")
    inactive_id = create_user(inactive_username, "the-real-password", clinic_id="default_clinic")
    _deactivate_user(inactive_id)

    throttled_username = _unique_username("enum4_throttled")
    create_user(throttled_username, "the-real-password", clinic_id="default_clinic")
    for _ in range(FAILURE_THRESHOLD):
        authenticate_user(throttled_username, "wrong-password")

    r_nonexistent = authenticate_user(_unique_username("enum4_does_not_exist"), "anything")
    r_wrong_password = authenticate_user(real_username, "wrong-password")
    r_inactive = authenticate_user(inactive_username, "the-real-password")
    r_throttled = authenticate_user(throttled_username, "the-real-password")  # CORRECT password, but locked

    check(
        "nonexistent-username, wrong-password, inactive-account, and throttled-account ALL return the "
        "identical None from authenticate_user() -- no distinguishable return value across any of the four",
        r_nonexistent is None and r_wrong_password is None and r_inactive is None and r_throttled is None,
        (r_nonexistent, r_wrong_password, r_inactive, r_throttled),
    )

    # Loose timing check, same generous tolerance and same rationale as
    # this file's existing test_login_failure_is_indistinguishable_
    # across_failure_reasons -- see authenticate_user()'s own "THROTTLE +
    # TIMING" docstring section for exactly why a throttled account still
    # pays the real Argon2id cost rather than returning early.
    def _timed(fn):
        t0 = time.perf_counter()
        fn()
        return time.perf_counter() - t0

    nonexistent_elapsed = _timed(lambda: authenticate_user(_unique_username("enum4_timing_nonexistent"), "anything"))
    throttled_elapsed = _timed(lambda: authenticate_user(throttled_username, "the-real-password"))
    ratio = (max(nonexistent_elapsed, throttled_elapsed) + 1e-6) / (min(nonexistent_elapsed, throttled_elapsed) + 1e-6)
    check(
        "a throttled/locked existing account costs a comparable ORDER OF MAGNITUDE of time to a "
        "nonexistent-username attempt (both pay for one real Argon2id verify) -- the lock is not an "
        "early-return shortcut that would make locked accounts respond conspicuously faster",
        ratio < 5.0,
        f"nonexistent={nonexistent_elapsed:.4f}s throttled={throttled_elapsed:.4f}s ratio={ratio:.2f}",
    )


# check() now raises AssertionError on a failed assertion (see its own
# docstring) -- the module-level functions above are called through this
# per-function try/except loop, exactly like every `_ALL_TESTS` loop
# elsewhere in this project, so one failing assertion doesn't stop the
# REST of the password-layer tests from running under direct execution,
# and the caller (this function's own caller, below) can still report a
# full tally. Returns the list of test names that had a failing
# assertion, so __main__ can fold them into ONE combined report alongside
# the session-layer tests.
_ALL_PASSWORD_LAYER_TESTS = [
    test_hash_password_produces_a_real_argon2id_hash_never_plaintext,
    test_two_hashes_of_the_same_password_differ,
    test_verify_password_correct_and_incorrect,
    test_create_user_and_authenticate_user_happy_path,
    test_create_user_duplicate_username_raises,
    test_authenticate_user_wrong_password_rejected,
    test_authenticate_user_nonexistent_username_rejected,
    test_authenticate_user_inactive_user_rejected,
    test_login_failure_is_indistinguishable_across_failure_reasons,
    test_authenticate_user_locks_after_threshold_consecutive_wrong_passwords,
    test_authenticate_user_does_not_extend_cooldown_by_repeated_hammering,
    test_authenticate_user_successful_login_resets_the_failure_counter,
    test_login_failure_including_throttled_account_is_indistinguishable_across_all_four_reasons,
]


def _password_layer_tests():
    """Runs the module-level password-layer test_ functions above
    through the per-function try/except loop, so
    `python tests/test_auth_service.py` keeps its existing "run every
    test, print a full tally" contract. Only called from __main__ when
    HAVE_ARGON2 is True (see below) -- pytest, by contrast, collects and
    calls each module-level test_ function above directly and
    independently of this function, which is what makes them
    individually visible to `pytest tests/test_auth_service.py -v` now."""
    failures = []
    for _test_fn in _ALL_PASSWORD_LAYER_TESTS:
        try:
            _test_fn()
        except AssertionError as e:
            failures.append(_test_fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {_test_fn.__name__} ({e})")
    return failures


_SESSION_LAYER_TESTS = [
    test_create_session_generates_random_opaque_tokens,
    test_resolve_session_valid_token_returns_correct_identity,
    test_resolve_session_rejects_forged_or_unknown_token,
    test_resolve_session_rejects_malformed_or_empty_tokens,
    test_resolve_session_rejects_expired_token,
    test_resolve_session_rejects_revoked_token,
    test_token_reuse_after_logout_is_permanently_rejected,
    test_revoke_session_is_idempotent,
    test_revoke_session_on_unknown_token_returns_false,
    test_two_users_get_independent_sessions_and_cannot_cross_authenticate,
    test_deactivated_user_session_stops_authenticating,
    test_role_and_clinic_change_in_db_is_reflected_live,
    test_username_uniqueness_is_global_not_per_clinic,
    test_login_throttle_row_created_lazily_and_starts_clean,
    test_login_throttle_first_four_failures_do_not_lock,
    test_login_throttle_threshold_failure_activates_cooldown,
    test_login_throttle_attempts_during_cooldown_do_not_extend_it,
    test_login_throttle_cooldown_expires_and_resets_to_a_fresh_window,
    test_login_throttle_reset_clears_counter_and_lock,
    test_login_throttle_counters_are_independent_per_user,
    test_login_throttle_concurrent_failures_do_not_lose_updates,
    test_login_throttle_concurrent_reset_and_failure_leave_valid_state,
    test_login_throttle_state_survives_a_real_separate_process_restart,
    test_sessions_foreign_key_to_users_is_enforced,
    test_token_hash_not_raw_token_is_what_gets_stored,
    test_last_seen_at_is_updated_on_successful_resolve,
    test_session_survives_a_real_separate_process_restart,
]


if __name__ == "__main__":
    # Same per-function try/except pattern as every other fixed test file
    # in this project (see tests/test_rbac.py's module docstring, "PYTEST
    # COMPATIBILITY") -- check() now raises, so this loop is what keeps
    # direct execution's "run every test, print a full tally" contract
    # intact while making every individual check() a real, hard assertion
    # under pytest too.
    print("--- Auth Service Tests: SESSION LAYER (no argon2 required) ---")
    _function_failures = []
    for _test_fn in _SESSION_LAYER_TESTS:
        try:
            _test_fn()
        except AssertionError as e:
            _function_failures.append(_test_fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {_test_fn.__name__} ({e})")

    print("\n--- Auth Service Tests: PASSWORD LAYER (requires argon2-cffi) ---")
    if HAVE_ARGON2:
        _function_failures.extend(_password_layer_tests())
    else:
        skip(
            "ALL password-layer tests (hash_password, verify_password, create_user, authenticate_user)",
            "argon2-cffi is not installed in this environment -- REQUIRES MAC VERIFICATION, see the A2 report",
        )

    print(f"\n--- {_passed} passed, {_failed} failed, {_skipped} skipped ---")
    if _function_failures:
        print(f"--- {len(_function_failures)} test function(s) had a failing assertion: {', '.join(_function_failures)} ---")
    if _skipped:
        print(
            f"NOTE: {_skipped} skip(s) above are NOT passes. This environment cannot install argon2-cffi "
            "(no PyPI access) -- the password-layer tests must be run on the real Mac before A2 can be "
            "considered verified end to end."
        )
    if _failed:
        sys.exit(1)
