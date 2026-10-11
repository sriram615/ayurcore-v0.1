"""
Migration 015 -- structured prescription items.

Adds prescription_items: one row per canonical herb of a confirmed
prescription, written by backend/inventory_service.deduct_and_prescribe in
the SAME transaction as the prescription row, the inventory deduction and
(on /prescribe/confirm) the PRESCRIPTION_CONFIRMED audit event. This is the
structured source of truth for what was dispensed; prescriptions.description
is kept, unchanged, for backward compatibility.

  prescription_id  the prescription this line belongs to
  clinic_id        must equal the prescription's clinic_id
  line_no          1, 2, 3, ... in prescription order, no gaps
  herb_name        canonical herb name (backend/herb_resolver.normalize_herb),
                   which must be stocked in that clinic's inventory
  grams            positive integer, TOTAL grams dispensed for this herb
                   (duplicate lines are merged before insert, so one row
                   per herb)

Enforced by the database (triggers fire regardless of PRAGMA foreign_keys):

  - prescription_items_insert_check: the prescription exists and is in the
    same clinic; line_no is exactly the next line; the herb is in that
    clinic's inventory.
  - prescription_items_sealed: no line may be added to a prescription that
    already has its PRESCRIPTION_CONFIRMED audit event. Confirm writes the
    items BEFORE that event, in one transaction, so the item set is final
    the moment the confirmation commits.
  - prescription_items_no_update / prescription_items_no_delete: confirmed
    items are immutable.
  - UNIQUE (prescription_id, line_no) and UNIQUE (prescription_id, herb_name).

Nothing is backfilled. Prescriptions confirmed before this migration have no
items; readers fall back to parsing their description (see
backend/patient_store.get_prescription_history). Deriving items for them
from free text here would present a parse as a structured record.
"""

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS prescription_items (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        prescription_id INTEGER NOT NULL REFERENCES prescriptions(id),
        clinic_id TEXT NOT NULL,
        line_no INTEGER NOT NULL CHECK (typeof(line_no) = 'integer' AND line_no >= 1),
        herb_name TEXT NOT NULL CHECK (length(herb_name) BETWEEN 1 AND 200),
        grams INTEGER NOT NULL CHECK (typeof(grams) = 'integer' AND grams > 0),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (prescription_id, line_no),
        UNIQUE (prescription_id, herb_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_prescription_items_clinic_herb ON prescription_items(clinic_id, herb_name)",
    """
    CREATE TRIGGER IF NOT EXISTS prescription_items_insert_check
    BEFORE INSERT ON prescription_items
    BEGIN
        SELECT RAISE(ABORT, 'prescription_item_prescription_invalid')
        WHERE NOT EXISTS (
            SELECT 1 FROM prescriptions p WHERE p.id = NEW.prescription_id AND p.clinic_id = NEW.clinic_id
        );
        SELECT RAISE(ABORT, 'prescription_item_line_invalid')
        WHERE NEW.line_no IS NOT (
            SELECT COUNT(*) + 1 FROM prescription_items i WHERE i.prescription_id = NEW.prescription_id
        );
        SELECT RAISE(ABORT, 'prescription_item_unknown_herb')
        WHERE NOT EXISTS (
            SELECT 1 FROM inventory v WHERE v.clinic_id = NEW.clinic_id AND v.herb_name = NEW.herb_name
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS prescription_items_sealed
    BEFORE INSERT ON prescription_items
    WHEN EXISTS (SELECT 1 FROM audit_events a WHERE a.prescription_id = NEW.prescription_id)
    BEGIN
        SELECT RAISE(ABORT, 'prescription_items_immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS prescription_items_no_update
    BEFORE UPDATE ON prescription_items
    BEGIN
        SELECT RAISE(ABORT, 'prescription_items_immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS prescription_items_no_delete
    BEFORE DELETE ON prescription_items
    BEGIN
        SELECT RAISE(ABORT, 'prescription_items_immutable');
    END
    """,
]


def upgrade(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
