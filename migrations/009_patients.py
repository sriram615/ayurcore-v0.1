"""
Migration 009 -- clinic-scoped patient registry (Patient Registry Phase 1).

Adds:
  - patients: one row per (clinic_id, clinic_patient_id). That pair IS the
    patient's identity -- unique per clinic, and immutable once created.
    display_name and phone are attributes, not identity.
  - prescription_previews.patient_pk and prescriptions.patient_pk: nullable
    links to patients. NULL means "not linked" -- every row that exists
    before this migration stays NULL; nothing is backfilled or matched.

Clinic isolation is enforced at WRITE time by triggers, not only in
application code: a preview or prescription can only reference a patient
of its own clinic_id. SQLite cannot express this as a composite foreign
key on an existing table (ALTER TABLE ADD COLUMN only allows a single-
column REFERENCES), and a plain REFERENCES patients(patient_pk) would only
prove the patient exists somewhere -- so the clinic match is a BEFORE
INSERT/UPDATE trigger on each table. Triggers fire on every connection
regardless of PRAGMA foreign_keys, so this does not depend on how a
connection was opened.

Also enforced here:
  - a patient's patient_pk / clinic_id / clinic_patient_id can never be
    changed, and patients cannot be deleted (Phase 1 has no delete, and a
    delete would orphan the links);
  - a preview's or prescription's patient_pk can never be changed once
    written -- so old, unlinked prescriptions cannot be linked by a later
    UPDATE; a future, audited linking path would need its own migration.

Why a .py migration: db/migrations.py's .sql splitter splits on every `;`,
which would cut a trigger's BEGIN ... END body apart. Each statement below
is executed whole via conn.execute(). The runner owns the transaction.
"""

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS patients (
        patient_pk          INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id           TEXT    NOT NULL,
        clinic_patient_id   TEXT    NOT NULL
            CHECK (length(clinic_patient_id) BETWEEN 1 AND 50 AND clinic_patient_id = trim(clinic_patient_id)),
        display_name        TEXT    NOT NULL CHECK (length(trim(display_name)) >= 1),
        phone               TEXT,
        created_by_user_id  INTEGER NOT NULL REFERENCES users(user_id),
        created_at          TEXT    NOT NULL,
        UNIQUE (clinic_id, clinic_patient_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_patients_clinic_id ON patients(clinic_id)",
    "ALTER TABLE prescription_previews ADD COLUMN patient_pk INTEGER REFERENCES patients(patient_pk)",
    "ALTER TABLE prescriptions ADD COLUMN patient_pk INTEGER REFERENCES patients(patient_pk)",
    "CREATE INDEX IF NOT EXISTS idx_prescriptions_clinic_patient ON prescriptions(clinic_id, patient_pk)",

    # --- patients: identity is immutable, rows are never deleted ----------
    """
    CREATE TRIGGER IF NOT EXISTS patients_identity_immutable
    BEFORE UPDATE OF patient_pk, clinic_id, clinic_patient_id ON patients
    WHEN OLD.patient_pk IS NOT NEW.patient_pk
      OR OLD.clinic_id IS NOT NEW.clinic_id
      OR OLD.clinic_patient_id IS NOT NEW.clinic_patient_id
    BEGIN
        SELECT RAISE(ABORT, 'patient_identity_immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS patients_no_delete
    BEFORE DELETE ON patients
    BEGIN
        SELECT RAISE(ABORT, 'patient_delete_forbidden');
    END
    """,

    # --- prescription_previews: same-clinic patient only, link immutable ---
    """
    CREATE TRIGGER IF NOT EXISTS previews_patient_clinic_insert
    BEFORE INSERT ON prescription_previews
    WHEN NEW.patient_pk IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM patients p WHERE p.patient_pk = NEW.patient_pk AND p.clinic_id = NEW.clinic_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'patient_clinic_mismatch');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS previews_patient_clinic_update
    BEFORE UPDATE OF patient_pk, clinic_id ON prescription_previews
    WHEN NEW.patient_pk IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM patients p WHERE p.patient_pk = NEW.patient_pk AND p.clinic_id = NEW.clinic_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'patient_clinic_mismatch');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS previews_patient_link_immutable
    BEFORE UPDATE OF patient_pk ON prescription_previews
    WHEN OLD.patient_pk IS NOT NEW.patient_pk
    BEGIN
        SELECT RAISE(ABORT, 'patient_link_immutable');
    END
    """,

    # --- prescriptions: same-clinic patient only, link immutable -----------
    """
    CREATE TRIGGER IF NOT EXISTS prescriptions_patient_clinic_insert
    BEFORE INSERT ON prescriptions
    WHEN NEW.patient_pk IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM patients p WHERE p.patient_pk = NEW.patient_pk AND p.clinic_id = NEW.clinic_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'patient_clinic_mismatch');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS prescriptions_patient_clinic_update
    BEFORE UPDATE OF patient_pk, clinic_id ON prescriptions
    WHEN NEW.patient_pk IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM patients p WHERE p.patient_pk = NEW.patient_pk AND p.clinic_id = NEW.clinic_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'patient_clinic_mismatch');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS prescriptions_patient_link_immutable
    BEFORE UPDATE OF patient_pk ON prescriptions
    WHEN OLD.patient_pk IS NOT NEW.patient_pk
    BEGIN
        SELECT RAISE(ABORT, 'patient_link_immutable');
    END
    """,
]


def upgrade(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
