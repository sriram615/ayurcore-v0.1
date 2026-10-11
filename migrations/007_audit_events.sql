-- Migration 007: tamper-evident audit events (Phase A5).
--
-- A NEW, authoritative, append-only record of clinically meaningful
-- prescription decisions -- distinct from the pre-existing `corrections`
-- table (migration 001), which stays exactly as it is and continues to
-- back GET /audit/export unchanged. `corrections` was built as a
-- best-effort "data flywheel" (logged AFTER the prescribing transaction
-- commits, can silently fail without rolling anything back -- see
-- backend/corrections_service.py). `audit_events` is a different,
-- stronger guarantee: written INSIDE the same transaction as the
-- prescription it documents (both commit or both roll back -- see
-- backend/audit_store.py and backend/main.py's /prescribe/confirm), and
-- hash-chained per clinic so that a historical row edited directly in the
-- database (bypassing this application entirely) is detectable by
-- backend/audit_store.py's verify_audit_integrity(), not just prevented by
-- convention.
--
-- Column-by-column:
--   event_id             Row identity / insertion order. Deliberately NOT
--                         part of the hash (see audit_store.py) -- it is
--                         storage bookkeeping, not clinical content.
--   clinic_id             Always the ACTING doctor's server-derived
--                         auth.clinic_id (Phase A, A3) -- never anything
--                         client-supplied. Scopes both multi-tenancy and
--                         the hash chain (see below).
--   actor_user_id         Always the ACTING doctor's server-derived
--                         auth.user_id -- never anything client-supplied.
--                         FK to users(user_id) (migration 004).
--   event_type            Presently only 'PRESCRIPTION_CONFIRMED' -- see
--                         audit_store.py's module docstring for why this
--                         project starts with exactly one event type
--                         instead of an HTTP-access-log-style taxonomy.
--   prescription_id        The real, committed prescription this event
--                         documents. FK to prescriptions(id) (migration
--                         001). UNIQUE: exactly one audit event per
--                         prescription -- there is exactly one moment a
--                         prescription is confirmed, ever (single-use
--                         preview/confirm split, Phase A/A1).
--   preview_id             The prescription_previews row this decision
--                         came from. FK to prescription_previews(preview_id)
--                         (migration 003).
--   event_data_json        The clinically meaningful decision snapshot --
--                         AI proposal (or explicitly null for a manual
--                         entry -- never fabricated, see audit_store.py),
--                         doctor's final decision, and whether they
--                         differ. Canonical JSON (sort_keys, compact
--                         separators, ensure_ascii=False -- see
--                         audit_store.py's canonicalization rules).
--                         Deliberately NEVER contains patient name, phone,
--                         government ID, raw consultation notes, auth
--                         tokens, password hashes, API keys, or Gemini
--                         credentials -- see audit_store.py's
--                         build_prescription_confirmed_event_data().
--   created_at             Deterministic UTC timestamp, same TEXT format
--                         this project already uses in preview_store.py /
--                         idempotency_store.py ("%Y-%m-%d %H:%M:%S").
--   previous_event_hash    The event_hash of the immediately preceding
--                         event IN THE SAME CLINIC's chain (see idx below),
--                         or NULL for a clinic's very first event. There is
--                         deliberately no global, cross-clinic chain (see
--                         audit_store.py's module docstring) -- clinic A's
--                         chain and clinic B's chain are two entirely
--                         independent sequences that happen to live in the
--                         same table.
--   event_hash             SHA-256 of this row's own canonical content
--                         chained with previous_event_hash. See
--                         audit_store.py for the exact, documented
--                         algorithm and verify_audit_integrity() for how
--                         it is re-checked against direct database
--                         tampering.
--
-- No application code exposes an update or delete path for this table --
-- see audit_store.py's module docstring. This migration itself is the
-- only place its schema is defined; per every other migration in this
-- project, this file must never be edited after being applied (the
-- migration runner checksums it and refuses to proceed if it changes).
CREATE TABLE IF NOT EXISTS audit_events (
    event_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    clinic_id             TEXT    NOT NULL,
    actor_user_id          INTEGER NOT NULL REFERENCES users(user_id),
    event_type            TEXT    NOT NULL CHECK (event_type IN ('PRESCRIPTION_CONFIRMED')),
    prescription_id        INTEGER NOT NULL REFERENCES prescriptions(id),
    preview_id             TEXT    NOT NULL REFERENCES prescription_previews(preview_id),
    event_data_json        TEXT    NOT NULL,
    created_at             TEXT    NOT NULL,
    previous_event_hash    TEXT,
    event_hash             TEXT    NOT NULL
);

-- Exactly one audit event per prescription (see column note above).
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_events_prescription_id ON audit_events(prescription_id);

-- The chain-lookup index: "find the latest event for clinic X" is always
-- `ORDER BY event_id DESC LIMIT 1 WHERE clinic_id = ?` (see
-- audit_store.py's create_audit_event) -- this composite index makes that
-- a direct index scan instead of a full-table filter+sort, and (clinic_id,
-- event_id) together are also exactly what verify_audit_integrity() walks
-- to reconstruct and check each clinic's chain in order.
CREATE INDEX IF NOT EXISTS idx_audit_events_clinic_id_event_id ON audit_events(clinic_id, event_id);

CREATE INDEX IF NOT EXISTS idx_audit_events_actor_user_id ON audit_events(actor_user_id);
