-- Migration 004: persistent users (Phase A, A2 -- Real Authentication).
--
-- Replaces the placeholder authentication model, where `Authorization`
-- was an arbitrary, unverified string a caller could set to anything --
-- there was no real user identity behind it, just a label recorded on a
-- preview and compared for equality at confirm time. Migration 003's
-- prescription_previews.created_by_session column, and the locked API
-- contract's "session binding" note, both explicitly said this activates
-- for real "the day real auth exists, with no code change needed" to the
-- comparison itself -- this migration, plus migration 005 and
-- backend/auth_service.py, is that day.
--
-- role is reserved for Phase A, A3 (RBAC + clinic isolation). A2 does
-- NOT gate any behavior on this column's value anywhere in the
-- application -- it only persists the identity primitive A3 will consume.
-- The CHECK constraint exists purely for data integrity (only these three
-- values are ever meaningful in this system going forward), not as an
-- RBAC enforcement mechanism -- no code path reads a user's role and
-- makes a decision based on it yet.
--
-- password_hash stores an Argon2id hash (via argon2-cffi) -- the encoded
-- hash string Argon2id produces already embeds its own salt and
-- parameters, so there is no separate salt column: inventing one instead
-- of using the library's own encoding would be exactly the kind of
-- "custom cryptographic password hashing" this project was told not to
-- build.
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    clinic_id TEXT NOT NULL DEFAULT 'default_clinic',
    role TEXT NOT NULL DEFAULT 'DOCTOR' CHECK (role IN ('DOCTOR', 'ADMIN', 'AUDITOR')),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_clinic_id ON users(clinic_id);
