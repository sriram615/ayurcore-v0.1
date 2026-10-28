"""
Migration 018 -- clinic-enriched schema for a realistic single-clinic workflow.

Adds columns (no new tables, no existing-column changes) so the demo data and
API surface resemble an actual Ayurvedic clinic visit:

  patients:        date_of_birth, gender, address_line, city, state, pincode,
                   prakriti, allergies, registered_at
  encounters:      visit_type, chief_complaint, vitals_bp, vitals_weight_kg,
                   vitals_temperature_c, follow_up_date
  prescriptions:   payment_status, payment_amount_cents, payment_method,
                   payment_reference, dispensed_at, dispensed_by_user_id,
                   invoice_number
  inventory:       expiry_date, supplier, reorder_level_grams   (batch_code
                   already exists from migration 001)

Nothing is backfilled: rows confirmed before this migration keep NULL for the
new columns, exactly as migration 015 leaves older prescriptions without
structured items. Readers already fall back gracefully to NULL.

All new columns are additive and nullable; no existing trigger, response
shape or write path is altered. Application-layer validation for the new
fields lives in backend/schemas.py.
"""

STATEMENTS = [
    # --- patients: demographics ------------------------------------------------
    "ALTER TABLE patients ADD COLUMN date_of_birth TEXT",
    "ALTER TABLE patients ADD COLUMN gender TEXT",
    "ALTER TABLE patients ADD COLUMN address_line TEXT",
    "ALTER TABLE patients ADD COLUMN city TEXT",
    "ALTER TABLE patients ADD COLUMN state TEXT",
    "ALTER TABLE patients ADD COLUMN pincode TEXT",
    "ALTER TABLE patients ADD COLUMN prakriti TEXT",
    "ALTER TABLE patients ADD COLUMN allergies TEXT",
    "ALTER TABLE patients ADD COLUMN registered_at TEXT",

    # --- encounters: consultation realism --------------------------------------
    "ALTER TABLE encounters ADD COLUMN visit_type TEXT",
    "ALTER TABLE encounters ADD COLUMN chief_complaint TEXT",
    "ALTER TABLE encounters ADD COLUMN vitals_bp TEXT",
    "ALTER TABLE encounters ADD COLUMN vitals_weight_kg REAL",
    "ALTER TABLE encounters ADD COLUMN vitals_temperature_c REAL",
    "ALTER TABLE encounters ADD COLUMN follow_up_date TEXT",

    # --- prescriptions: dispensing + payment ----------------------------------
    "ALTER TABLE prescriptions ADD COLUMN payment_status TEXT DEFAULT 'unpaid'",
    "ALTER TABLE prescriptions ADD COLUMN payment_amount_cents INTEGER",
    "ALTER TABLE prescriptions ADD COLUMN payment_method TEXT",
    "ALTER TABLE prescriptions ADD COLUMN payment_reference TEXT",
    "ALTER TABLE prescriptions ADD COLUMN dispensed_at TEXT",
    "ALTER TABLE prescriptions ADD COLUMN dispensed_by_user_id INTEGER REFERENCES users(user_id)",
    "ALTER TABLE prescriptions ADD COLUMN invoice_number TEXT",

    # --- inventory: batch / expiry realism -------------------------------------
    # batch_code already exists (migration 001)
    "ALTER TABLE inventory ADD COLUMN expiry_date TEXT",
    "ALTER TABLE inventory ADD COLUMN supplier TEXT",
    "ALTER TABLE inventory ADD COLUMN reorder_level_grams INTEGER",
]


def upgrade(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
