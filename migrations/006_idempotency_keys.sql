-- Migration 006: idempotency keys (Phase B, A4 -- exactly-once confirmation).
--
-- Backs backend/idempotency_store.py. Scoped by (user_id, operation,
-- idempotency_key) rather than session_id, deliberately: a doctor who gets
-- logged out and re-authenticates mid-retry must still be able to safely
-- replay their own in-flight confirmation, and session_id would break that.
-- `operation` future-proofs against a second idempotent endpoint someday
-- colliding with this one's key space ("prescribe_confirm" is the only
-- value used today).
--
-- clinic_id is captured at reservation time from the caller's own
-- server-derived auth.clinic_id (never anything client-supplied) and is
-- re-checked live on every replay -- see idempotency_store.py's
-- reserve_idempotent_operation() docstring for why, and why this is
-- currently unreachable-but-defended-anyway (no endpoint in this project
-- reassigns a user's clinic_id today).
--
-- response_status_code / response_body_json are NULLable at the schema
-- level, but this is NOT a durable third state. The two-state model is:
-- absent (never durably committed) or SUCCEEDED (a row with BOTH fields
-- populated). The reservation is INSERTed with these two columns NULL
-- near the top of /prescribe/confirm's existing BEGIN IMMEDIATE
-- transaction, purely to atomically claim the composite key before the
-- (possibly slow, possibly failing) business logic runs; they are UPDATEd
-- to their real, final values in that SAME transaction once the response
-- is known, and only then does the transaction commit. Because SQLite
-- never exposes one connection's uncommitted writes to another, no other
-- connection can ever observe a row with NULL response fields -- it is
-- either invisible (transaction still open, or rolled back and gone
-- entirely) or fully populated (committed). A transaction that fails
-- anywhere between the INSERT and the UPDATE rolls back in full,
-- reservation included, leaving the key retryable exactly as if never
-- attempted. See idempotency_store.py for the code that enforces this and
-- tests/test_idempotency.py for the tests that prove it (including a
-- direct "no row may have NULL response fields" invariant scan).
--
-- response_body_json necessarily contains the same patient-identifying
-- fields PrescriptionResponse already returns to the doctor who made the
-- original request (patient_name) -- this is required for the replay
-- guarantee itself (a replayed response must be byte-identical to the
-- original), not a new PII exposure: only the SAME authenticated user who
-- owns the (user_id, operation, key) row can ever cause it to be read back
-- (see idempotency_store.py), and it is never joined into corrections,
-- audit export, or any other read path. Unlike the corrections table,
-- this table is explicitly NOT part of the PII-minimized audit dataset --
-- it is operational/technical state for request deduplication, with its
-- own, separate retention question (a future TTL/cleanup pass is A6
-- territory, not decided here).
CREATE TABLE IF NOT EXISTS idempotency_keys (
    user_id               INTEGER NOT NULL REFERENCES users(user_id),
    operation             TEXT    NOT NULL,
    idempotency_key       TEXT    NOT NULL,
    clinic_id             TEXT    NOT NULL,
    request_fingerprint   TEXT    NOT NULL,
    response_status_code  INTEGER,
    response_body_json    TEXT,
    created_at            TEXT    NOT NULL,
    completed_at          TEXT,
    PRIMARY KEY (user_id, operation, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_keys_clinic_id ON idempotency_keys(clinic_id);
