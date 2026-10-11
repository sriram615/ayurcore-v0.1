"""
Migration 016 -- structured prescription data as the authoritative record
(Phase 2B), and the database-level gaps left open by migration 015.

1. prescriptions.dosha_imbalance (TEXT, nullable). The confirmed dosha as a
   structured value, written by backend/inventory_service.deduct_and_prescribe
   alongside prescription_items. NULL means a prescription confirmed before
   this migration, whose dosha exists only inside `description`; nothing is
   backfilled (a parse of free text is not a structured record).

2. prescriptions_content_immutable: a confirmed prescription's content --
   id, patient_name, description, created_at, dosha_imbalance, and clinic_id
   of an unlinked row -- can never change. (A linked row's clinic_id and its
   patient/encounter links are already guarded by migrations 009 and 012,
   which keep their own error messages.) `status` is left updatable for a
   future lifecycle; nothing updates it today.

3. prescriptions_no_delete: a prescription can never be deleted, so its
   items, audit event and history can never be orphaned or silently lost.

4. prescription_items_grams_limit: an item's grams may not exceed 10000 --
   the per-herb bound /prescribe/confirm enforces on the merged total
   (backend/schemas.PrescribedHerb), now held by the database as well.

5. prescription_items_newest_only: items can be added only to the most
   recently inserted prescription. deduct_and_prescribe inserts the
   prescription and its items back to back inside one BEGIN IMMEDIATE
   transaction, so that is always the row being written. Every older
   prescription -- including legacy rows that have no audit event and so
   were not sealed by migration 015 -- can no longer gain items. The
   trigger fires only for an EXISTING prescription, so a nonexistent one
   still reports migration 015's 'prescription_item_prescription_invalid'.

Scope: anyone with write access to the database FILE can drop triggers;
these stop application-level and accidental modification.
"""

STATEMENTS = [
    "ALTER TABLE prescriptions ADD COLUMN dosha_imbalance TEXT",
    """
    CREATE TRIGGER IF NOT EXISTS prescriptions_content_immutable
    BEFORE UPDATE ON prescriptions
    WHEN OLD.id IS NOT NEW.id
      OR OLD.patient_name IS NOT NEW.patient_name
      OR OLD.description IS NOT NEW.description
      OR OLD.created_at IS NOT NEW.created_at
      OR OLD.dosha_imbalance IS NOT NEW.dosha_imbalance
      OR (OLD.clinic_id IS NOT NEW.clinic_id AND NEW.patient_pk IS NULL)
    BEGIN
        SELECT RAISE(ABORT, 'prescription_content_immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS prescriptions_no_delete
    BEFORE DELETE ON prescriptions
    BEGIN
        SELECT RAISE(ABORT, 'prescription_content_immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS prescription_items_grams_limit
    BEFORE INSERT ON prescription_items
    WHEN NEW.grams > 10000
    BEGIN
        SELECT RAISE(ABORT, 'prescription_item_grams_limit');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS prescription_items_newest_only
    BEFORE INSERT ON prescription_items
    WHEN EXISTS (SELECT 1 FROM prescriptions p WHERE p.id = NEW.prescription_id)
     AND NEW.prescription_id < (SELECT MAX(id) FROM prescriptions)
    BEGIN
        SELECT RAISE(ABORT, 'prescription_items_immutable');
    END
    """,
]


def upgrade(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
