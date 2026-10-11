"""
Real server-side authentication (Phase A, A2).

Replaces the placeholder "authentication" that existed before this: an
Authorization header whose value was never verified against anything --
literally any string, including no header at all, was accepted, and
backend/preview_store.py simply recorded whatever string a caller sent
under created_by_session. There was no real user identity, no password,
nothing that could be revoked, and nothing compared by anything other
than raw string equality against another arbitrary string on a later
call.

This module is the whole of that replacement:
  - Password hashing/verification via Argon2id (argon2-cffi), never a
    hand-rolled scheme. See _get_password_hasher()'s docstring for why the
    argon2 import happens lazily, inside functions, rather than at module
    import time.
  - Enumeration/timing-safe login (authenticate_user): a nonexistent
    username, a wrong password, and an inactive user's correct password
    all take exactly one Argon2id verify() call and return the identical
    `None`, so neither the response nor a timing side-channel reveals
    which of those three happened. This is the direct implementation of
    A2's explicit requirement: "Invalid credentials must not reveal
    whether the username exists."
  - Opaque, server-generated session tokens (secrets.token_urlsafe) --
    never a JWT, never anything the frontend decodes -- with only a
    SHA-256 hash of the token ever written to the sessions table. See
    migrations/005_sessions.sql's header comment for why SHA-256 (not
    Argon2id) is the correct hash for a token, as opposed to a password.
  - Session lifecycle: create_session, resolve_session (the single
    function every authenticated request path goes through), and
    revoke_session (logout). All three are ordinary, real SQL against the
    `sessions` table added by migration 005 -- there is no in-memory
    session state anywhere, so authentication survives a process restart
    the same way A1 made previews survive one.

Nothing in this module is Phase A3 (RBAC/clinic isolation) policy. It
persists `role` and `clinic_id` on the user record and hands both back on
a resolved session (via `resolve_session()`'s live JOIN, so a role or
clinic change is reflected on the very next call), but no function here
ever branches on role, and no function here decides whether a user is
ALLOWED to act on a given clinic_id. That policy layer now lives in
backend/rbac.py (built in A3), which imports `AuthenticatedSession` and
`resolve_session` from here rather than duplicating either.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from db.database import get_connection

# The locked-down default: sessions are valid for 12 hours from creation --
# a fixed, absolute expiry, not a sliding/refreshed one. There is no
# "touch to extend" behavior in A2's scope; re-authenticating after this
# window is the expected, correct behavior, not a bug.
SESSION_TTL_SECONDS = 12 * 60 * 60

# Bounded login-attempt throttling (Phase A, A6.1 -- Login Abuse
# Resistance). Deliberately NOT a permanent lockout, NOT IP-based, and NOT
# backed by any external infrastructure (Redis, a WAF, a distributed rate
# limiter) -- a small per-account counter and an absolute cooldown
# timestamp, persisted in SQLite via migrations/008_login_throttle.sql,
# exactly like every other piece of durable state in this project. See
# authenticate_user()'s docstring, section "THROTTLE + TIMING", for the
# exact state machine and for why the cooldown check is placed where it is
# relative to the real Argon2id verify call. Fixed, documented constants
# for this milestone, not per-deployment configuration -- see the A6.1
# report for why that's an intentionally small scope.
FAILURE_THRESHOLD = 5
COOLDOWN_SECONDS = 45

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

_dummy_hash: Optional[str] = None


class UsernameAlreadyExistsError(Exception):
    """Raised by create_user() when the requested username is already
    taken -- surfaces sqlite3's own UNIQUE constraint on users.username as
    a clear, specific exception rather than letting a raw
    sqlite3.IntegrityError escape."""


@dataclass(frozen=True)
class AuthenticatedSession:
    """What a validated Authorization header resolves to. Carries only
    what's needed to authorize an action and to log out -- never the raw
    password, and nothing read from a client-supplied request body.
    `raw_token` is kept only so /auth/logout can revoke the exact session
    that was just used to authenticate the logout call itself, without
    re-parsing the Authorization header a second time; it is never
    written anywhere and never appears in a response body."""
    session_id: int
    user_id: int
    username: str
    clinic_id: str
    role: str
    raw_token: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format(dt: datetime) -> str:
    return dt.strftime(_TIMESTAMP_FORMAT)


def _get_password_hasher():
    """Lazily imports argon2-cffi's PasswordHasher on first actual use,
    rather than at module import time.

    This is NOT a workaround to avoid using Argon2id, and it changes
    nothing about which algorithm is used in production -- every password
    this module hashes or verifies still goes through real Argon2id,
    every time, with no fallback path. The only thing deferred is WHEN the
    `argon2` module is imported: at first call, rather than the moment
    `backend.auth_service` itself is imported.

    Why that matters: this module also contains create_session /
    resolve_session / revoke_session, which are ordinary sqlite3 +
    hashlib code with no dependency on argon2-cffi at all. An eager
    module-level `from argon2 import PasswordHasher` would make THOSE
    functions impossible to import or unit-test anywhere argon2-cffi
    isn't installed -- this project's cloud sandbox has no PyPI access
    and cannot install argon2-cffi at all (confirmed directly: both
    `pip install argon2-cffi` and `import argon2` fail there). Deferring
    the import keeps that unrelated, argon2-independent session logic
    testable in that sandbox, while every code path that actually hashes
    or verifies a password still requires -- and uses -- real
    argon2-cffi, and raises a plain ImportError if it is genuinely
    missing at that point, rather than silently substituting anything
    weaker. See the A2 report's section G for exactly which tests this
    means require real-Mac verification.
    """
    from argon2 import PasswordHasher
    return PasswordHasher()


def _get_dummy_hash() -> str:
    """A real Argon2id hash of a fixed, non-secret placeholder value,
    computed once (lazily, on first use) and cached for the life of the
    process. authenticate_user() runs a real verify_password() against
    this exact hash whenever the supplied username does not exist, so a
    nonexistent-username attempt costs the same one Argon2id verify call
    as a real wrong-password attempt against a real user -- see
    authenticate_user()'s docstring. Cached rather than re-hashed on every
    miss purely so a flood of nonexistent-username attempts can't be used
    to burn CPU disproportionately; the hash itself carries no secret --
    knowing it grants no access to anything, since it is never compared
    against a real account.
    """
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("ayurcore-a2-dummy-hash-not-a-real-account-password")
    return _dummy_hash


def hash_password(plain: str) -> str:
    """Hashes `plain` with Argon2id via argon2-cffi. The returned string
    is self-contained -- algorithm, version, cost parameters, and a
    random salt are all encoded into it by the library itself -- so there
    is no separate salt column anywhere in this project's schema.
    Inventing one instead of using the library's own encoding would
    itself be exactly the "custom cryptographic password hashing" this
    project was told not to build."""
    return _get_password_hasher().hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """True iff `plain` matches `hashed` under Argon2id. Every failure
    mode (wrong password, a corrupted or foreign hash string, any other
    exception argon2-cffi's verify() can raise) is treated identically --
    returns False -- rather than ever propagating an exception that could
    let a caller distinguish failure reasons."""
    try:
        _get_password_hasher().verify(hashed, plain)
        return True
    except Exception:
        return False


def create_user(username: str, password: str, clinic_id: str, role: str = "DOCTOR") -> int:
    """Creates a new user with an Argon2id-hashed password. Raises
    UsernameAlreadyExistsError on a duplicate username rather than
    letting a raw sqlite3.IntegrityError escape.

    Test/seed-data use only in A2 -- there is no public /auth/register
    endpoint. Whether (and how) a clinic gets to create its own logins is
    a real product/security decision that belongs with A3's RBAC policy
    work, not smuggled into A2 as a side effect.
    """
    now = _format(_now())
    password_hash = hash_password(password)
    conn = get_connection()
    try:
        try:
            cursor = conn.execute(
                """
                INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (username, password_hash, clinic_id, role, now, now),
            )
            conn.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raise UsernameAlreadyExistsError(f"Username {username!r} is already taken.") from e
    finally:
        conn.close()


def _ensure_throttle_row(conn: sqlite3.Connection, user_id: int) -> None:
    """Lazily creates this user's login_throttle row on first login
    attempt. INSERT OR IGNORE makes this safe to call unconditionally,
    concurrently, and repeatedly -- a race between two concurrent FIRST
    attempts for the same brand-new user resolves to exactly one row
    either way, SQLite's own PRIMARY KEY uniqueness enforcement handling
    the rest (the losing INSERT is silently ignored, not an error)."""
    conn.execute(
        "INSERT OR IGNORE INTO login_throttle (user_id, consecutive_failures, locked_until) VALUES (?, 0, NULL)",
        (user_id,),
    )


def _clear_expired_lock(conn: sqlite3.Connection, user_id: int, now_str: str) -> None:
    """Once a cooldown's locked_until has passed, resets the counter to a
    fresh window rather than leaving it primed to re-lock after a single
    subsequent failure. This is what actually makes the cooldown
    TEMPORARY (this task's explicit "cooldown must not become permanent"
    requirement) instead of a permanently degraded account that never
    again gets a full FAILURE_THRESHOLD worth of chances. A no-op (matches
    zero rows) for a user who was never locked, or whose lock hasn't
    expired yet."""
    conn.execute(
        """
        UPDATE login_throttle
        SET consecutive_failures = 0, locked_until = NULL
        WHERE user_id = ? AND locked_until IS NOT NULL AND locked_until <= ?
        """,
        (user_id, now_str),
    )


def _record_failed_login(conn: sqlite3.Connection, user_id: int, now_str: str) -> None:
    """Atomically increments consecutive_failures by exactly 1 -- the
    `consecutive_failures + 1` in the SQL below is evaluated by SQLite
    against the CURRENT row value at the moment this statement executes,
    never a value read earlier in Python -- and, only when that new count
    reaches FAILURE_THRESHOLD, sets locked_until to a fresh
    COOLDOWN_SECONDS-from-now timestamp.

    Concurrency: SQLite serializes writers (this project's
    db/database.py opens every connection with PRAGMA journal_mode = WAL
    and a 15s busy timeout), so two truly concurrent failed attempts for
    the same user_id are applied one after the other, never lost -- this
    is what keeps the counter itself immune to corruption under
    concurrent hammering, even though -- in a genuine simultaneous race
    right at the threshold boundary -- the exact attempt number that
    trips the lock can be off by one. That is an accepted, documented
    imprecision for an abuse-resistance BOUND, not a correctness
    invariant like A4's idempotency guarantee.

    The trailing `WHERE ... AND (locked_until IS NULL OR locked_until <=
    ?)` clause is defense-in-depth, not the primary mechanism (the caller
    already checks is_locked, from a fresh read, before ever calling this
    at all): it refuses to touch a row that is ALREADY in an active
    cooldown, so even a concurrent caller working from stale
    (not-yet-locked) state cannot extend or renew an already-active lock.
    This is exactly what keeps repeated hammering DURING a cooldown from
    turning it into an indefinitely-renewable, effectively permanent
    lock -- a real DoS this project would otherwise be handing an
    attacker against a legitimate doctor's own account.
    """
    new_locked_until = _format(_now() + timedelta(seconds=COOLDOWN_SECONDS))
    conn.execute(
        """
        UPDATE login_throttle
        SET consecutive_failures = consecutive_failures + 1,
            locked_until = CASE
                WHEN consecutive_failures + 1 >= ? THEN ?
                ELSE locked_until
            END
        WHERE user_id = ?
          AND (locked_until IS NULL OR locked_until <= ?)
        """,
        (FAILURE_THRESHOLD, new_locked_until, user_id, now_str),
    )


def _reset_throttle(conn: sqlite3.Connection, user_id: int) -> None:
    """Called only on a genuinely successful login (correct password,
    account active, not currently locked) -- clears both the counter and
    any lock, so the doctor's next mistake starts a fresh
    FAILURE_THRESHOLD-attempt window rather than carrying over a partial
    count from before."""
    conn.execute(
        "UPDATE login_throttle SET consecutive_failures = 0, locked_until = NULL WHERE user_id = ?",
        (user_id,),
    )


def authenticate_user(username: str, password: str) -> Optional[sqlite3.Row]:
    """Verifies a login attempt. Returns the user's row on success, or
    None on ANY failure -- nonexistent username, wrong password, an
    inactive account, or (A6.1, new) an account currently inside a
    login-throttle cooldown -- with the identical return value across all
    four, and comparable timing across all four (see "THROTTLE + TIMING"
    below).

    ENUMERATION RESISTANCE (A2, preserved unchanged): exactly one Argon2id
    verify() call happens on every path that reaches this far.
      - Username not found: verify against a fixed dummy hash (see
        _get_dummy_hash) and discard the result -- always False, but the
        CPU cost paid is the same as a real verify. This path never
        touches login_throttle at all -- there is no user_id to key a
        throttle row on for a username that doesn't exist. See "THROTTLE
        + TIMING" below for why that asymmetry does not reopen an
        enumeration channel.
      - Username found: verify against that user's REAL stored hash,
        regardless of is_active OR whether the account is currently
        throttled. Only AFTER that real verify do we check the throttle
        lock, then is_active -- checking either one first (before
        verifying) would let that case return faster than a real
        wrong-password attempt, reopening exactly the timing side-channel
        A2 closed.

    THROTTLE + TIMING (A6.1, new): a currently-locked account is rejected
    with the SAME None as every other failure reason, and only AFTER
    paying the SAME real Argon2id verify cost as every other
    existing-username path -- never an early return before that call.
    Returning early for a locked account (i.e. checking the lock BEFORE
    calling verify_password) would create exactly the oracle this
    milestone's spec warns against: an attacker sweeping usernames would
    see near-instant responses cluster on exactly the accounts that (a)
    exist and (b) have recently failed enough attempts to be locked,
    disclosing both existence and recent-attack-history for those
    specific accounts via timing alone, even though the response body and
    status code stay byte-identical. Paying the Argon2id cost
    unconditionally for every existing username -- locked or not -- keeps
    every known-username response at the same order of magnitude,
    matching the unknown-username (dummy-hash) case, exactly as A2
    already established.

    One deliberate, documented timing asymmetry remains, and is
    considered acceptable rather than fixed: an existing-username attempt
    now runs a handful of extra local SQLite statements against the
    throttle table (ensure the row exists, clear an expired lock, read
    throttle state, and on failure/success one more UPDATE) that an
    unknown-username attempt does not. These are microsecond-scale local
    operations against the same on-disk SQLite file already being
    queried for the users row, dwarfed by Argon2id's own deliberately
    expensive cost (tens to hundreds of milliseconds, by design). See
    tests/test_auth_service.py's existing
    test_login_failure_is_indistinguishable_across_failure_reasons for
    the timing tolerance (ratio < 5.0) this project already considered
    acceptable for the ORIGINAL A2 enumeration-timing property -- this
    change does not rely on any tighter precision than that test already
    exercises.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

        if row is None:
            verify_password(password, _get_dummy_hash())
            return None

        user_id = row["user_id"]
        now_str = _format(_now())

        _ensure_throttle_row(conn, user_id)
        _clear_expired_lock(conn, user_id, now_str)
        conn.commit()

        throttle_row = conn.execute(
            "SELECT locked_until FROM login_throttle WHERE user_id = ?", (user_id,)
        ).fetchone()
        locked_until = throttle_row["locked_until"] if throttle_row else None
        is_locked = locked_until is not None and locked_until > now_str

        # See "THROTTLE + TIMING" above: this call happens unconditionally,
        # locked or not, before either outcome below is decided.
        password_ok = verify_password(password, row["password_hash"])

        if is_locked:
            return None

        if not password_ok:
            _record_failed_login(conn, user_id, now_str)
            conn.commit()
            return None

        if not row["is_active"]:
            return None

        _reset_throttle(conn, user_id)
        conn.commit()
        return row
    finally:
        conn.close()


def create_session(user_id: int, ttl_seconds: int = SESSION_TTL_SECONDS) -> tuple[str, str]:
    """Creates a new session row for `user_id` and returns
    (raw_token, expires_at_str). The raw token -- secrets.token_urlsafe(32),
    256 bits of CSPRNG entropy -- is returned to the caller exactly once,
    here, and is never written to the database or logged anywhere; only
    its SHA-256 hash is persisted (see migrations/005_sessions.sql's
    header comment for why SHA-256, not Argon2id, is correct here)."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    created_at = _now()
    expires_at = created_at + timedelta(seconds=ttl_seconds)
    expires_at_str = _format(expires_at)

    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at, revoked_at, last_seen_at)
            VALUES (?, ?, ?, ?, NULL, NULL)
            """,
            (token_hash, user_id, _format(created_at), expires_at_str),
        )
        conn.commit()
    finally:
        conn.close()

    return raw_token, expires_at_str


def resolve_session(raw_token: Optional[str]) -> Optional[AuthenticatedSession]:
    """The single function every authenticated request path goes
    through. Returns None -- deliberately never raising -- for every way
    a presented token can fail to authenticate: empty/missing, doesn't
    hash-match anything in the table (forged, garbled, or simply made
    up), matches a row that has been revoked, matches a row that has
    expired, or matches a row whose backing user has since been
    deactivated. A caller (backend/main.py's require_auth dependency)
    turns a None here into a 401 -- this function itself has no notion of
    HTTP status codes.

    Never trusts anything the client claims about itself beyond the raw
    token string -- clinic_id, role, and username all come back from the
    users table via a JOIN, keyed off the session row's user_id, never
    from anything the caller supplied on this call.
    """
    if not raw_token:
        return None

    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now_str = _format(_now())

    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT s.session_id, s.user_id, s.expires_at, s.revoked_at,
                   u.username, u.clinic_id, u.role, u.is_active
            FROM sessions s
            JOIN users u ON u.user_id = s.user_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()

        if row is None:
            return None
        if row["revoked_at"] is not None:
            return None
        if row["expires_at"] <= now_str:
            return None
        if not row["is_active"]:
            return None

        # Best-effort only. Unlike backend/preview_store.py's writes, a
        # last_seen_at update failure here is purely informational -- a
        # session that already resolved correctly above must still be
        # allowed to authenticate THIS request even if this one
        # housekeeping write fails for some unrelated reason.
        try:
            # Security review 2.2: never wait for the write lock just to
            # record last_seen_at. With the connection's normal 15s busy
            # timeout, every authenticated request -- read-only ones
            # included -- stalled up to 15s whenever another writer held the
            # lock. busy_timeout=0 makes this housekeeping write fail fast
            # (caught below, request proceeds); the connection is closed at
            # the end of this function, so nothing else inherits it.
            conn.execute("PRAGMA busy_timeout = 0")
            conn.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE session_id = ?",
                (now_str, row["session_id"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()

        return AuthenticatedSession(
            session_id=row["session_id"],
            user_id=row["user_id"],
            username=row["username"],
            clinic_id=row["clinic_id"],
            role=row["role"],
            raw_token=raw_token,
        )
    finally:
        conn.close()


def revoke_session(raw_token: str) -> bool:
    """Logout. Marks the session matching `raw_token` revoked, if it
    exists and isn't already revoked. Returns whether a row was actually
    changed. Once revoked, resolve_session() above rejects this token
    immediately and permanently -- there is no un-revoke."""
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    now_str = _format(_now())

    conn = get_connection()
    try:
        cursor = conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (now_str, token_hash),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
