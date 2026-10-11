"""
Migration 010 -- clinic-scoped encounters (Encounter foundation, Phase 1A).

An encounter is one consultation episode for one registered patient in one
clinic. It is deliberately minimal: identity, who opened it, and a two-state
lifecycle. Nothing links previews or prescriptions to it yet.

    OPEN  --close-->  CLOSED        (CLOSED is terminal)

Enforced here, at WRITE time, independently of application code and of
whether a connection enables foreign keys (triggers always fire):

  - encounters_patient_clinic_insert: the patient must exist AND belong to the
    encounter's own clinic, and the creating user must belong to that clinic
    too. Together with migration 009's patients_identity_immutable (a
    patient's clinic can never change), encounter.clinic_id always equals its
    patient's clinic_id.
  - encounters_identity_immutable: encounter_pk, clinic_id, patient_pk,
    created_by_user_id and created_at can never change.
  - encounters_closed_is_final: a CLOSED encounter can never be updated again
    (no reopening, no changing closed_at).
  - encounters_no_delete: encounters are never deleted.
  - CHECK: status is OPEN or CLOSED, and closed_at is set exactly when CLOSED.
  - idx_encounters_one_open_per_patient: at most one OPEN encounter per
    patient, so a retried "open encounter" request can never create a
    duplicate.

Why a .py migration: db/migrations.py's .sql splitter splits on every `;`,
which would cut a trigger's BEGIN ... END body apart (same reason as 009).
"""

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS encounters (
        encounter_pk        INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id           TEXT    NOT NULL,
        patient_pk          INTEGER NOT NULL REFERENCES patients(patient_pk),
        created_by_user_id  INTEGER NOT NULL REFERENCES users(user_id),
        status              TEXT    NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
        created_at          TEXT    NOT NULL,
        closed_at           TEXT,
        CHECK ((status = 'OPEN' AND closed_at IS NULL) OR (status = 'CLOSED' AND closed_at IS NOT NULL))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_encounters_clinic_patient ON encounters(clinic_id, patient_pk, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_encounters_one_open_per_patient ON encounters(patient_pk) WHERE status = 'OPEN'",
    """
    CREATE TRIGGER IF NOT EXISTS encounters_patient_clinic_insert
    BEFORE INSERT ON encounters
    WHEN NOT EXISTS (
            SELECT 1 FROM patients p WHERE p.patient_pk = NEW.patient_pk AND p.clinic_id = NEW.clinic_id
         )
      OR NOT EXISTS (
            SELECT 1 FROM users u WHERE u.user_id = NEW.created_by_user_id AND u.clinic_id = NEW.clinic_id
         )
    BEGIN
        SELECT RAISE(ABORT, 'encounter_clinic_mismatch');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS encounters_identity_immutable
    BEFORE UPDATE OF encounter_pk, clinic_id, patient_pk, created_by_user_id, created_at ON encounters
    WHEN OLD.encounter_pk IS NOT NEW.encounter_pk
      OR OLD.clinic_id IS NOT NEW.clinic_id
      OR OLD.patient_pk IS NOT NEW.patient_pk
      OR OLD.created_by_user_id IS NOT NEW.created_by_user_id
      OR OLD.created_at IS NOT NEW.created_at
    BEGIN
        SELECT RAISE(ABORT, 'encounter_identity_immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS encounters_closed_is_final
    BEFORE UPDATE ON encounters
    WHEN OLD.status = 'CLOSED'
    BEGIN
        SELECT RAISE(ABORT, 'encounter_closed_is_final');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS encounters_no_delete
    BEFORE DELETE ON encounters
    BEGIN
        SELECT RAISE(ABORT, 'encounter_delete_forbidden');
    END
    """,
]


def upgrade(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
