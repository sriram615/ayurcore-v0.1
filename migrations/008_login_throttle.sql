-- Migration 008: login throttle state (Phase A, A6.1 -- Login Abuse
-- Resistance).
--
-- Bounds consecutive failed-login attempts per user with a short,
-- self-expiring cooldown -- explicitly NOT a permanent account lockout,
-- and NOT an IP-based or infrastructure-based rate limiter (no Redis, no
-- WAF, no external service -- see the A6.1 threat-model report for why
-- those are out of scope for this milestone). One row per user, created
-- lazily (INSERT OR IGNORE, in backend/auth_service.py's
-- _ensure_throttle_row()) the first time authenticate_user() sees a login
-- attempt for that user_id -- there is no bulk backfill for existing
-- users at migration time, since a user with no row yet here is exactly
-- equivalent to a user with consecutive_failures=0 and no active lock.
--
-- consecutive_failures: count of consecutive wrong-password attempts
-- since the last successful login, OR since the last time an active
-- cooldown was found to have expired (see _clear_expired_lock() in
-- backend/auth_service.py) -- a fresh window starts either way, which is
-- what keeps this a TEMPORARY cooldown rather than a permanently
-- degraded account.
--
-- locked_until: NULL, or an absolute timestamp (same "%Y-%m-%d %H:%M:%S"
-- UTC format migrations 004/005 already use for users/sessions). While
-- now <= locked_until, ALL login attempts for this user_id are rejected
-- with the exact same generic failure every other rejection reason in
-- authenticate_user() already produces -- this table is read from INSIDE
-- authenticate_user() only, and is never exposed as a distinguishable
-- status anywhere a client can observe it directly (see
-- backend/auth_service.py's authenticate_user() docstring, "THROTTLE +
-- TIMING", for the enumeration-resistance reasoning this depends on).
--
-- user_id is a real foreign key (db/database.py's get_connection() sets
-- PRAGMA foreign_keys = ON on every connection, same as migration 005's
-- sessions.user_id), so a login_throttle row can never outlive its user.
CREATE TABLE IF NOT EXISTS login_throttle (
    user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);
