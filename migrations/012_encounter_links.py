"""
Migration 012 -- link previews and prescriptions to an encounter
(Encounter Phase 1B).

Adds a nullable encounter_pk to prescription_previews and prescriptions.
NULL means "not part of an encounter", exactly the pre-1B behavior, so every
existing row and every existing client stays valid. Nothing is backfilled.

Enforced at WRITE time (triggers fire regardless of PRAGMA foreign_keys):

  - previews_encounter_link_insert / prescriptions_encounter_link_insert:
    a non-NULL encounter_pk must name an encounter that
      * belongs to the row's own clinic_id,
      * belongs to the row's own patient_pk (so a row with no patient can
        never carry an encounter), and
      * is still OPEN.
    For prescriptions this is the database backstop for "cannot confirm into
    an encounter that was closed after the draft was created".
  - previews_encounter_link_immutable / prescriptions_encounter_link_immutable:
    encounter_pk can never change once written -- no later linking,
    relinking or unlinking.

A row's clinic_id and patient_pk are already protected by migration 009's
triggers (a linked row cannot move clinic or change patient), and an
encounter's clinic_id / patient_pk are immutable (migration 010), so the
clinic/patient match checked at insert stays true afterwards.
"""

_LINK_CHECK = """
    WHEN NEW.encounter_pk IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM encounters e
        WHERE e.encounter_pk = NEW.encounter_pk
          AND e.clinic_id = NEW.clinic_id
          AND e.patient_pk IS NEW.patient_pk
          AND e.status = 'OPEN'
    )
    BEGIN
        SELECT RAISE(ABORT, 'encounter_link_invalid');
    END
"""

STATEMENTS = [
    "ALTER TABLE prescription_previews ADD COLUMN encounter_pk INTEGER REFERENCES encounters(encounter_pk)",
    "ALTER TABLE prescriptions ADD COLUMN encounter_pk INTEGER REFERENCES encounters(encounter_pk)",
    "CREATE INDEX IF NOT EXISTS idx_prescriptions_clinic_encounter ON prescriptions(clinic_id, encounter_pk)",
    "CREATE TRIGGER IF NOT EXISTS previews_encounter_link_insert BEFORE INSERT ON prescription_previews" + _LINK_CHECK,
    "CREATE TRIGGER IF NOT EXISTS prescriptions_encounter_link_insert BEFORE INSERT ON prescriptions" + _LINK_CHECK,
    """
    CREATE TRIGGER IF NOT EXISTS previews_encounter_link_immutable
    BEFORE UPDATE OF encounter_pk ON prescription_previews
    WHEN OLD.encounter_pk IS NOT NEW.encounter_pk
    BEGIN
        SELECT RAISE(ABORT, 'encounter_link_immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS prescriptions_encounter_link_immutable
    BEFORE UPDATE OF encounter_pk ON prescriptions
    WHEN OLD.encounter_pk IS NOT NEW.encounter_pk
    BEGIN
        SELECT RAISE(ABORT, 'encounter_link_immutable');
    END
    """,
]


def upgrade(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
