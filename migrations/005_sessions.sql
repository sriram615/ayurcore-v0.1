-- Migration 005: server-side sessions (Phase A, A2 -- Real Authentication).
--
-- This table -- together with migration 004's users table and
-- backend/auth_service.py -- replaces the placeholder "session" that used
-- to be nothing more than an arbitrary, unverified Authorization header
-- string recorded on a preview and string-compared at confirm time. A real
-- session here is a row: a specific user, a specific issue time, a
-- specific expiry, and a specific (nullable) revocation time.
--
-- token_hash, not the raw token: the raw bearer token is 256 bits of
-- secrets.token_urlsafe() entropy handed to the client once, at login, and
-- never written anywhere in plaintext after that -- what's persisted here
-- is only hashlib.sha256(raw_token).hexdigest(). Unlike a password, a
-- session token is already high-entropy and machine-generated, so a fast
-- cryptographic hash (SHA-256) is the right tool for "detect this exact
-- token again without storing it in plaintext" -- Argon2id's deliberate
-- slowness exists to blunt offline guessing of LOW-entropy human-chosen
-- secrets, which does not apply here and would just make every
-- authenticated request needlessly slower for no security benefit.
--
-- user_id is a real foreign key (db/database.py's get_connection() already
-- sets PRAGMA foreign_keys = ON on every connection, so this is enforced
-- live, not just documented). expires_at is a fixed, absolute
-- creation-time + TTL value -- see backend/auth_service.py's
-- SESSION_TTL_SECONDS -- there is no sliding-expiration/refresh mechanism
-- in A2's scope. revoked_at is set once, by /auth/logout, and a session
-- with a non-NULL revoked_at must never authenticate again regardless of
-- expires_at. last_seen_at is informational only (best-effort, updated on
-- successful resolution) and nothing about authentication correctness
-- depends on it.
CREATE TABLE IF NOT EXISTS sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_seen_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
