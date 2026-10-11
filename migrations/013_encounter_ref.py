"""
Migration 013 -- random, non-sequential encounter reference for auditing.

Audit events for encounter open/close used to carry encounter_pk, a GLOBAL
autoincrement shared by every clinic, so gaps between an auditor's own
clinic's numbers revealed how many encounters OTHER clinics opened in
between. From this migration on, encounter audit events carry encounter_ref
instead: 16 random hex characters, unique, meaningless outside this system.

  - encounters.encounter_ref TEXT, backfilled for every existing encounter
    with a fresh random value, then required (non-NULL, 16 lowercase hex
    chars) for every new row, unique, and immutable.
  - Backfilling a CLOSED encounter is impossible while
    encounters_closed_is_final (migration 010) exists -- it blocks every
    update of a closed row, by design. So that trigger and
    encounters_identity_immutable are dropped, the backfill runs, and both
    are recreated in the SAME transaction (the runner's savepoint): the
    closed-is-final trigger from its exact stored SQL, the identity trigger
    extended to also freeze encounter_ref. If anything fails, the whole
    migration -- including the trigger drops -- rolls back.

Existing audit events are NOT changed (they are hashed; migration 011's
rule stands): events written before this migration keep encounter_pk.
"""

_IDENTITY_TRIGGER = """
CREATE TRIGGER encounters_identity_immutable
BEFORE UPDATE OF encounter_pk, clinic_id, patient_pk, created_by_user_id, created_at, encounter_ref ON encounters
WHEN OLD.encounter_pk IS NOT NEW.encounter_pk
  OR OLD.clinic_id IS NOT NEW.clinic_id
  OR OLD.patient_pk IS NOT NEW.patient_pk
  OR OLD.created_by_user_id IS NOT NEW.created_by_user_id
  OR OLD.created_at IS NOT NEW.created_at
  OR OLD.encounter_ref IS NOT NEW.encounter_ref
BEGIN
    SELECT RAISE(ABORT, 'encounter_identity_immutable');
END
"""

_REF_REQUIRED_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS encounters_ref_required
BEFORE INSERT ON encounters
WHEN NEW.encounter_ref IS NULL
  OR length(NEW.encounter_ref) != 16
  OR NEW.encounter_ref GLOB '*[^0-9a-f]*'
BEGIN
    SELECT RAISE(ABORT, 'encounter_ref_invalid');
END
"""


def upgrade(conn):
    closed_final_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'encounters_closed_is_final'"
    ).fetchone()[0]
    conn.execute("ALTER TABLE encounters ADD COLUMN encounter_ref TEXT")
    conn.execute("DROP TRIGGER encounters_closed_is_final")
    conn.execute("DROP TRIGGER encounters_identity_immutable")
    conn.execute("UPDATE encounters SET encounter_ref = lower(hex(randomblob(8))) WHERE encounter_ref IS NULL")
    conn.execute(closed_final_sql)
    conn.execute(_IDENTITY_TRIGGER)
    conn.execute(_REF_REQUIRED_TRIGGER)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_encounters_encounter_ref ON encounters(encounter_ref)")
    missing = conn.execute("SELECT COUNT(*) FROM encounters WHERE encounter_ref IS NULL").fetchone()[0]
    if missing:
        raise RuntimeError(f"{missing} encounters left without encounter_ref; migration aborted")
