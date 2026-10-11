-- Migration 003: persistent prescription previews (Phase A, A1).
--
-- Replaces backend/main.py's old in-memory _PENDING_PREVIEWS /
-- _CONSUMED_PREVIEWS module-level dicts, which did not survive a process
-- restart and were not safe across multiple worker processes (two workers
-- each had their OWN dicts, so a preview created on worker A could never
-- be confirmed via worker B). Everything those dicts held now lives here
-- instead, in one shared table every worker reads and writes.
--
-- status is the lifecycle: PENDING -> CONFIRMED, or PENDING -> EXPIRED.
-- Enforcement of the 30-minute expiry at /prescribe/confirm (see
-- backend/preview_store.py) is always a LIVE comparison of expires_at
-- against the current time, done fresh on every confirm attempt inside
-- that request's own transaction -- never something that depends on a
-- separate cleanup job having already flipped a row to EXPIRED first. A
-- background cleanup (mark_stale_previews_expired() in preview_store.py)
-- does opportunistically flip stale PENDING rows to EXPIRED, but purely
-- for admin/audit readability -- correctness never depends on it having
-- run.
--
-- confirmed_dosha_imbalance / confirmed_prescribed_herbs_json hold the
-- doctor's final, actually-confirmed values, written only at the moment a
-- preview transitions to CONFIRMED -- kept as separate columns from
-- ai_dosha_imbalance / ai_prescribed_herbs_json (the original proposal,
-- which for a manual preview is simply what the doctor entered up front)
-- specifically so the two can never be conflated the way a single
-- overwritten field would risk.
CREATE TABLE IF NOT EXISTS prescription_previews (
    preview_id TEXT PRIMARY KEY,
    clinic_id TEXT NOT NULL,
    patient_name TEXT NOT NULL,
    patient_phone TEXT,
    patient_id TEXT,
    is_manual INTEGER NOT NULL CHECK (is_manual IN (0, 1)),
    ai_dosha_imbalance TEXT NOT NULL,
    ai_prescribed_herbs_json TEXT NOT NULL,
    confirmed_dosha_imbalance TEXT,
    confirmed_prescribed_herbs_json TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONFIRMED', 'EXPIRED')),
    created_by_session TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_prescription_previews_status ON prescription_previews(status);
CREATE INDEX IF NOT EXISTS idx_prescription_previews_clinic_id ON prescription_previews(clinic_id);
CREATE INDEX IF NOT EXISTS idx_prescription_previews_expires_at ON prescription_previews(expires_at);
