"""
AyurCore v0.1 clinic-demo seed script.

Generates realistic synthetic volumes for a single small Ayurvedic clinic,
populating every table with the clinic-enrichment columns from migration 018:

  patients        demographics (prakriti, allergies, dob, address, ...)
  encounters      visit_type, chief_complaint, vitals, follow_up_date
  prescriptions   payment_status, payment_method, dispensed_at, invoice_number, ...
  inventory       expiry_date, supplier, reorder_level_grams, batch_code

All data is synthetic -- invented names, phones, UPI references, batch codes.
Nothing here is real patient data.

Usage:
    ./venv/bin/python db/seed.py          # seeds db/ayurcore.db
    ./venv/bin/python db/seed.py --db /path/to/demo.db   # seeds a demo database
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import secrets
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from db.database import get_connection, DB_PATH

DEFAULT_CLINIC_ID = "default_clinic"
CLINIC_2_ID = "clinic_north"

# Target volumes per clinic (smaller second clinic to show a different scale).
CLINIC_VOLUMES = {
    DEFAULT_CLINIC_ID: {
        "users": 6,
        "patients": 200,
        "encounters": 800,
        "prescriptions": 600,
    },
    CLINIC_2_ID: {
        "users": 4,
        "patients": 50,
        "encounters": 150,
        "prescriptions": 120,
    },
}

# --- Synthetic data pools (all invented, for demo only) ---
FIRST_NAMES_MALE = ["Arjun", "Ravi", "Suresh", "Manoj", "Vikram", "Ramesh", "Sanjay", "Deepak", "Rajesh", "Amit"]
FIRST_NAMES_FEMALE = ["Lakshmi", "Sita", "Kavita", "Anita", "Priya", "Geeta", "Sunita", "Meena", "Rekha", "Pooja"]
LAST_NAMES = ["Menon", "Iyer", "Nair", "Pillai", "Reddy", "Sharma", "Verma", "Rao", "Naidu", "Gowda"]

CHIEF_COMPLAINTS = [
    "Joint pain and stiffness",
    "Skin rash and itching",
    "Digestive discomfort and acidity",
    "Stress and anxiety",
    "Respiratory congestion",
    "General weakness and fatigue",
    "Headache and migraine",
    "Menstrual irregularity",
    "Weight management",
    "Sleep disturbance",
]

PRAKRITI_VALUES = ["Vata", "Pitta", "Kapha", "Tridosha"]
GENDER_VALUES = ["Male", "Female", "Other"]
VISIT_TYPE_VALUES = ["FIRST_VISIT", "FOLLOW_UP"]
PAYMENT_STATUS_VALUES = ["paid", "partial", "unpaid"]
PAYMENT_METHOD_VALUES = ["cash", "upi", "card"]

HERBS = [
    ("Ashwagandha", "Withania somnifera", 95),
    ("Brahmi", "Bacopa monnieri", 92),
    ("Dashamoola", "Dasamula (10 Roots)", 88),
    ("Guggulu", "Commiphora wightii", 96),
    ("Shatavari", "Asparagus racemosus", 94),
    ("Triphala", "Emblica officinalis, Terminalia chebula, Terminalia bellirica", 90),
    ("Arjuna", "Terminalia arjuna", 91),
    ("Haridra", "Curcuma longa", 98),
    ("Neem", "Azadirachta indica", 90),
    ("Yashtimadhu", "Glycyrrhiza glabra", 93),
    ("Punarnava", "Boerhavia diffusa", 89),
    ("Bhringraj", "Eclipta alba", 91),
    ("Manjistha", "Rubia cordifolia", 90),
    ("Vasaka", "Justicia adhatoda", 92),
    ("Shankhpushpi", "Convolvulus pluricaulis", 87),
    ("Kutki", "Picrorhiza kurroa", 85),
    ("Pippali", "Piper longum", 94),
    ("Haritaki", "Terminalia chebula", 93),
    ("Amalaki", "Phyllanthus emblica", 96),
    ("Bibhitaki", "Terminalia bellirica", 92),
]

# Target volumes per clinic (smaller second clinic to show a different scale).
CLINIC_VOLUMES = {
    DEFAULT_CLINIC_ID: {
        "users": 6,
        "patients": 200,
        "encounters": 800,
        "prescriptions": 600,
    },
    CLINIC_2_ID: {
        "users": 4,
        "patients": 50,
        "encounters": 150,
        "prescriptions": 120,
    },
}

SEED = 42


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _rand_dt(start: datetime, end: datetime) -> str:
    """Random datetime string between start and end."""
    delta = end - start
    random_seconds = random.randint(0, int(delta.total_seconds()))
    return (start + timedelta(seconds=random_seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _rand_phone() -> str:
    """Synthetic Indian-style mobile number."""
    return f"+91 {random.randint(70000, 99999)} {random.randint(10000, 99999)}"


def _rand_pincode() -> str:
    """Kerala-style synthetic pincode."""
    return f"6{random.randint(10000, 99999)}"


def _rand_upi_ref() -> str:
    """Synthetic UPI reference."""
    return f"UPI{random.randint(1000000000, 9999999999)}"


def _rand_invoice() -> str:
    """Synthetic invoice number."""
    return f"INV-{random.randint(1000, 9999)}"


def _rand_batch(herb: str) -> str:
    """Synthetic batch code."""
    prefix = herb[:3].upper()
    return f"{prefix}-2026-{random.randint(100, 999)}"


def _rand_expiry() -> str:
    """Expiry 6-18 months from now."""
    base = datetime.now(timezone.utc)
    offset = timedelta(days=random.randint(180, 540))
    return (base + offset).strftime("%Y-%m-%d")


def _rand_name() -> str:
    first_pool = FIRST_NAMES_MALE if random.random() < 0.5 else FIRST_NAMES_FEMALE
    return f"{random.choice(first_pool)} {random.choice(LAST_NAMES)}"


def _rand_dosha() -> str | None:
    """70% have a dosha recorded; 30% NULL (legacy-like fallback)."""
    if random.random() < 0.7:
        return random.choice(PRAKRITI_VALUES)
    return None


def _rand_herbs() -> list[tuple[str, int]]:
    """1-4 herbs per prescription with total grams."""
    n = random.randint(1, 4)
    selected = random.sample(HERBS, n)
    return [(name, random.randint(10, 500)) for name, _, _ in selected]


def _hash_password(plain: str) -> str:
    """Argon2id hash via backend.auth_service if available, else a placeholder."""
    try:
        from backend.auth_service import hash_password
        return hash_password(plain)
    except Exception:
        # Fallback keeps the seed runnable without argon2-cffi installed.
        # Never used for real accounts -- seed data only.
        return hashlib.sha256(plain.encode()).hexdigest()


def _clear_all_tables(conn: sqlite3.Connection) -> None:
    """Delete all seed-relevant tables in FK-safe order."""
    tables = [
        "prescription_items",
        "audit_events",
        "encounters",
        "prescriptions",
        "sessions",
        "login_throttle",
        "users",
        "patients",
        "inventory",
    ]
    for table in tables:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def _seed_users(conn: sqlite3.Connection) -> list[dict]:
    """Create clinic staff users with Argon2id-hashed passwords, per clinic."""
    all_users = []
    for clinic_id, vols in CLINIC_VOLUMES.items():
        n_users = vols["users"]
        if clinic_id == DEFAULT_CLINIC_ID:
            clinic_users = [
                ("dr_sharma", "DOCTOR"),
                ("dr_iyer", "DOCTOR"),
                ("admin_kumar", "ADMIN"),
                ("auditor_nair", "AUDITOR"),
                ("dispenser_reddy", "DOCTOR"),  # dispensing handled by DOCTOR role in v0.1
                ("receptionist_verma", "DOCTOR"),
            ]
        else:
            clinic_users = [
                ("dr_rao", "DOCTOR"),
                ("dr_nair", "DOCTOR"),
                ("admin_reddy", "ADMIN"),
                ("auditor_sharma", "AUDITOR"),
            ][:n_users]

        for username, role in clinic_users:
            password_hash = _hash_password("seed-demo-password-not-real")
            conn.execute(
                """
                INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (username, password_hash, clinic_id, role, _now(), _now()),
            )
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            all_users.append(dict(row))
    conn.commit()
    return all_users


def _seed_patients(
    conn: sqlite3.Connection,
    user_ids_by_clinic: dict[str, list[int]],
) -> dict[str, list[dict]]:
    """Create synthetic patients per clinic with full demographics (migration 018 columns)."""
    patients_by_clinic: dict[str, list[dict]] = {}
    start = datetime(2026, 4, 1)
    end = datetime(2026, 10, 1)
    for clinic_id, vols in CLINIC_VOLUMES.items():
        n_patients = vols["patients"]
        clinic_user_ids = user_ids_by_clinic[clinic_id]
        clinic_patients = []
        for i in range(n_patients):
            name = _rand_name()
            clinic_patient_id = f"{clinic_id[:2].upper()}-{i + 1:04d}"
            gender = random.choice(GENDER_VALUES)
            dob = _rand_dt(datetime(1940, 1, 1), datetime(2008, 1, 1))
            registered_at = _rand_dt(start, end)
            conn.execute(
                """
                INSERT INTO patients
                (clinic_id, clinic_patient_id, display_name, phone, date_of_birth, gender,
                 address_line, city, state, pincode, prakriti, allergies,
                 created_by_user_id, created_at, registered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clinic_id, clinic_patient_id, name, _rand_phone(),
                    dob, gender,
                    f"{random.randint(1, 999)} Main Road", "Kochi", "Kerala", _rand_pincode(),
                    random.choice(PRAKRITI_VALUES),
                    random.choice(["", "Dust allergy", "Pollen allergy", None, None, None]),
                    random.choice(clinic_user_ids),
                    registered_at,
                    registered_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM patients WHERE clinic_patient_id = ? AND clinic_id = ?",
                (clinic_patient_id, clinic_id),
            ).fetchone()
            clinic_patients.append(dict(row))
        conn.commit()
        patients_by_clinic[clinic_id] = clinic_patients
        print(f"  {clinic_id}: {len(clinic_patients)} patients")
    return patients_by_clinic


def _seed_inventory(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Seed herbs with batch, expiry, supplier, reorder level per clinic (migration 018 columns)."""
    inventory_by_clinic: dict[str, list[dict]] = {}
    for clinic_id in CLINIC_VOLUMES:
        clinic_inventory = []
        for name, botanical, potency in HERBS:
            stock = random.randint(50, 5000)
            batch = _rand_batch(name)
            expiry = _rand_expiry()
            supplier = random.choice(
                ["Ayurvedic Herbs Co.", "Kerala Botanicals", "Southern Roots", "Himalayan Herbs", None]
            )
            reorder = random.randint(100, 500)
            conn.execute(
                """
                INSERT INTO inventory
                (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score,
                 expiry_date, supplier, reorder_level_grams)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (clinic_id, name, botanical, stock, batch, potency,
                 expiry, supplier, reorder),
            )
            row = conn.execute(
                "SELECT * FROM inventory WHERE herb_name = ? AND clinic_id = ?",
                (name, clinic_id),
            ).fetchone()
            clinic_inventory.append(dict(row))
        conn.commit()
        inventory_by_clinic[clinic_id] = clinic_inventory
        print(f"  {clinic_id}: {len(clinic_inventory)} inventory items")
    return inventory_by_clinic


def _seed_encounters(
    conn: sqlite3.Connection,
    patients_by_clinic: dict[str, list[dict]],
    doctor_user_ids_by_clinic: dict[str, list[int]],
) -> dict[str, dict[int, list[dict]]]:
    """Create encounters per patient per clinic with visit_type, chief_complaint, vitals (migration 018)."""
    patient_encounters_by_clinic: dict[str, dict[int, list[dict]]] = {}
    start = datetime(2026, 4, 1)
    end = datetime(2026, 10, 28)

    for clinic_id, patients in patients_by_clinic.items():
        doctor_user_ids = doctor_user_ids_by_clinic[clinic_id]
        patient_encounters: dict[int, list[dict]] = {}
        for patient in patients:
            patient_encounters[patient["patient_pk"]] = []
            n_encounters = random.randint(1, 6)
            for i in range(n_encounters):
                encounter_created_at = _rand_dt(start, end)
                visit_type = random.choice(VISIT_TYPE_VALUES)
                # Only one OPEN encounter per patient (database constraint).
                status = "OPEN" if (i == 0 and random.random() < 0.3) else "CLOSED"
                closed_at = None
                if status == "CLOSED":
                    created_dt = datetime.strptime(encounter_created_at, "%Y-%m-%d %H:%M:%S")
                    closed_at = _rand_dt(created_dt, end)
                encounter_ref = secrets.token_hex(8)
                conn.execute(
                    """
                    INSERT INTO encounters
                    (clinic_id, patient_pk, created_by_user_id, status, created_at, closed_at,
                     encounter_ref, visit_type, chief_complaint, vitals_bp, vitals_weight_kg,
                     vitals_temperature_c, follow_up_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clinic_id, patient["patient_pk"], random.choice(doctor_user_ids),
                        status, encounter_created_at, closed_at,
                        encounter_ref, visit_type,
                        random.choice(CHIEF_COMPLAINTS),
                        f"{random.randint(110, 140)}/{random.randint(70, 90)}",
                        round(random.uniform(45.0, 95.0), 1),
                        round(random.uniform(36.0, 39.0), 1),
                        _rand_dt(datetime(2026, 10, 1), datetime(2027, 3, 1)) if random.random() < 0.3 else None,
                    ),
                )
                encounter_pk = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                row = conn.execute("SELECT * FROM encounters WHERE encounter_pk = ?", (encounter_pk,)).fetchone()
                patient_encounters[patient["patient_pk"]].append(dict(row))

        conn.commit()
        total = sum(len(v) for v in patient_encounters.values())
        patient_encounters_by_clinic[clinic_id] = patient_encounters
        print(f"  {clinic_id}: {total} encounters")

    return patient_encounters_by_clinic


def _seed_prescriptions(
    conn: sqlite3.Connection,
    patients_by_clinic: dict[str, list[dict]],
    patient_encounters_by_clinic: dict[str, dict[int, list[dict]]],
    doctor_user_ids_by_clinic: dict[str, list[int]],
    inventory_by_clinic: dict[str, list[dict]],
) -> dict[str, int]:
    """Create prescriptions with payment/dispense fields and prescription_items (migration 018), per clinic."""
    counts_by_clinic: dict[str, int] = {}
    start = datetime(2026, 4, 1)
    end = datetime(2026, 10, 28)

    for clinic_id, patients in patients_by_clinic.items():
        herb_map = {h["herb_name"]: h for h in inventory_by_clinic[clinic_id]}
        patient_encounters = patient_encounters_by_clinic[clinic_id]
        doctor_user_ids = doctor_user_ids_by_clinic[clinic_id]
        count = 0

        for patient in patients:
            encounters_for_patient = patient_encounters.get(patient["patient_pk"], [])
            n_prescriptions = random.randint(0, 4)
            for _ in range(n_prescriptions):
                # Encounter link must be OPEN (migration 012 trigger).
                open_encounters = [e for e in encounters_for_patient if e["status"] == "OPEN"]
                encounter = random.choice(open_encounters) if open_encounters and random.random() < 0.7 else None
                prescribed_at = _rand_dt(start, end)
                dosha = _rand_dosha()
                herb_lines = _rand_herbs()
                herbs_json = json.dumps([{"name": name, "grams": g} for name, g in herb_lines])
                description = (
                    f"Patient: {patient['display_name']} | Dosha: {dosha or 'Not recorded'} "
                    f"| Formulation: {herbs_json}"
                )

                payment_status = random.choice(PAYMENT_STATUS_VALUES)
                payment_amount = random.randint(150, 2500) if payment_status != "unpaid" else None
                payment_method = random.choice(PAYMENT_METHOD_VALUES) if payment_status != "unpaid" else None
                if payment_method == "upi":
                    payment_reference = _rand_upi_ref()
                elif payment_method == "card":
                    payment_reference = _rand_invoice()
                else:
                    payment_reference = None

                dispensed = random.random() < 0.8
                if dispensed:
                    prescribed_dt = datetime.strptime(prescribed_at, "%Y-%m-%d %H:%M:%S")
                    dispensed_at = _rand_dt(prescribed_dt, end)
                    dispensed_by = random.choice(doctor_user_ids)
                else:
                    dispensed_at = None
                    dispensed_by = None

                invoice_number = _rand_invoice() if payment_status == "paid" else None

                conn.execute(
                    """
                    INSERT INTO prescriptions
                    (clinic_id, patient_name, description, status, created_at,
                     patient_pk, encounter_pk, dosha_imbalance,
                     payment_status, payment_amount_cents, payment_method, payment_reference,
                     dispensed_at, dispensed_by_user_id, invoice_number)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clinic_id, patient["display_name"], description, "CONFIRMED",
                        prescribed_at,
                        patient["patient_pk"], encounter["encounter_pk"] if encounter else None,
                        dosha,
                        payment_status, payment_amount, payment_method, payment_reference,
                        dispensed_at, dispensed_by, invoice_number,
                    ),
                )
                prescription_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                for line_no, (herb_name, grams) in enumerate(herb_lines, 1):
                    conn.execute(
                        """
                        INSERT INTO prescription_items
                        (prescription_id, clinic_id, line_no, herb_name, grams, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (prescription_id, clinic_id, line_no, herb_name, grams, prescribed_at),
                    )
                count += 1

        conn.commit()
        counts_by_clinic[clinic_id] = count
        print(f"  {clinic_id}: {count} prescriptions with items")

    return counts_by_clinic


def _seed_sessions(conn: sqlite3.Connection, users: list[dict]) -> None:
    """Create active sessions so demo logins work immediately."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=12)
    for user in users:
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at, revoked_at, last_seen_at)
            VALUES (?, ?, ?, ?, NULL, NULL)
            """,
            (token_hash, user["user_id"], _now(), expires_at.strftime("%Y-%m-%d %H:%M:%S")),
        )
    conn.commit()


def _seed_audit_events(conn: sqlite3.Connection) -> int:
    """Create PRESCRIPTION_CONFIRMED audit events for every confirmed prescription,
    maintaining the per-clinic SHA-256 hash chain.

    Runs inside a single BEGIN IMMEDIATE transaction so the previous-event hash
    lookup (inside create_audit_event) sees a consistent chain. All seed
    prescriptions are treated as manual entries (is_manual=True) since they were
    entered directly by the seed script, not proposed by an AI.

    Each audit event requires a prescription_previews row (FK on preview_id),
    so one is created per prescription first, mirroring the real preview ->
    confirm flow.
    """
    from backend.audit_store import (
        AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
        build_prescription_confirmed_event_data,
        create_audit_event,
    )

    prescriptions = conn.execute(
        """
        SELECT p.id, p.clinic_id, p.dosha_imbalance, p.dispensed_by_user_id,
               p.created_at, p.patient_name
        FROM prescriptions p
        WHERE p.status = 'CONFIRMED'
        ORDER BY p.created_at ASC, p.id ASC
        """
    ).fetchall()

    if not prescriptions:
        print("No confirmed prescriptions -- skipping audit events.")
        return 0

    count = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in prescriptions:
            prescription_id = row["id"]
            clinic_id = row["clinic_id"]
            actor_user_id = row["dispensed_by_user_id"]

            # Fallback to first doctor from the same clinic if no dispenser recorded.
            if actor_user_id is None:
                actor_user_id = conn.execute(
                    "SELECT user_id FROM users WHERE role = 'DOCTOR' AND clinic_id = ? ORDER BY user_id LIMIT 1",
                    (clinic_id,),
                ).fetchone()[0]

            # Read structured herb items for this prescription.
            items = conn.execute(
                """
                SELECT herb_name, grams FROM prescription_items
                WHERE prescription_id = ? AND clinic_id = ?
                ORDER BY line_no
                """,
                (prescription_id, clinic_id),
            ).fetchall()
            confirmed_herbs = [
                {"name": item["herb_name"], "grams": item["grams"]} for item in items
            ]
            confirmed_herbs_json = json.dumps(confirmed_herbs)

            preview_id = f"preview_seed_{prescription_id}"

            # Create the preview row (FK required by audit_events.preview_id).
            # For manual entries, ai_* columns hold the doctor's own values.
            conn.execute(
                """
                INSERT INTO prescription_previews
                (preview_id, clinic_id, patient_name, is_manual,
                 ai_dosha_imbalance, ai_prescribed_herbs_json,
                 confirmed_dosha_imbalance, confirmed_prescribed_herbs_json,
                 status, created_by_session, created_at, expires_at, confirmed_at)
                VALUES (?, ?, ?, 1, ?, ?, ?, ?, 'CONFIRMED', NULL, ?, ?, ?)
                """,
                (
                    preview_id,
                    clinic_id,
                    row["patient_name"],
                    row["dosha_imbalance"] or "Not recorded",
                    confirmed_herbs_json,
                    row["dosha_imbalance"] or "Not recorded",
                    confirmed_herbs_json,
                    row["created_at"],
                    _rand_expiry(),
                    row["created_at"],
                ),
            )

            event_data = build_prescription_confirmed_event_data(
                is_manual=True,
                ai_dosha_imbalance=None,
                ai_prescribed_herbs=None,
                confirmed_dosha_imbalance=row["dosha_imbalance"] or "Not recorded",
                confirmed_prescribed_herbs=confirmed_herbs,
            )

            create_audit_event(
                conn,
                clinic_id=clinic_id,
                actor_user_id=actor_user_id,
                event_type=AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
                prescription_id=prescription_id,
                preview_id=preview_id,
                event_data=event_data,
            )
            count += 1

        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    print(f"Created {count} preview rows and {count} audit events (prescription confirmations).")
    return count


def _seed_encounter_audit_events(conn: sqlite3.Connection) -> int:
    """Create ENCOUNTER_OPENED / ENCOUNTER_CLOSED audit events for every encounter,
    maintaining the per-clinic SHA-256 hash chain.

    Runs inside a single BEGIN IMMEDIATE transaction so the previous-event hash
    lookup (inside create_audit_event) sees a consistent chain. Encounter events
    carry no prescription_id or preview_id (migration 011's CHECK constraint).
    """
    from backend.audit_store import (
        AUDIT_EVENT_TYPE_ENCOUNTER_CLOSED,
        AUDIT_EVENT_TYPE_ENCOUNTER_OPENED,
        build_encounter_event_data,
        create_audit_event,
    )

    encounters = conn.execute(
        """
        SELECT encounter_pk, clinic_id, created_by_user_id, status, created_at,
               encounter_ref
        FROM encounters
        ORDER BY created_at ASC, encounter_pk ASC
        """
    ).fetchall()

    if not encounters:
        print("No encounters -- skipping encounter audit events.")
        return 0

    count = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in encounters:
            encounter_pk = row["encounter_pk"]
            clinic_id = row["clinic_id"]
            actor_user_id = row["created_by_user_id"]
            status = row["status"]
            encounter_ref = row["encounter_ref"]

            event_data = build_encounter_event_data(encounter_ref)

            # Every encounter gets an OPENED event.
            create_audit_event(
                conn,
                clinic_id=clinic_id,
                actor_user_id=actor_user_id,
                event_type=AUDIT_EVENT_TYPE_ENCOUNTER_OPENED,
                prescription_id=None,
                preview_id=None,
                event_data=event_data,
            )
            count += 1

            # CLOSED encounters also get a CLOSED event.
            if status == "CLOSED":
                create_audit_event(
                    conn,
                    clinic_id=clinic_id,
                    actor_user_id=actor_user_id,
                    event_type=AUDIT_EVENT_TYPE_ENCOUNTER_CLOSED,
                    prescription_id=None,
                    preview_id=None,
                    event_data=event_data,
                )
                count += 1

        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    print(f"Created {count} encounter audit events (opened/closed).")
    return count


def seed_db(db_path: str = DB_PATH) -> None:
    """Full clinic-demo seed. Idempotent: clears and regenerates all tables."""
    random.seed(SEED)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")

    try:
        _clear_all_tables(conn)
        users = _seed_users(conn)

        # Group user IDs by clinic for downstream functions.
        user_ids_by_clinic: dict[str, list[int]] = {}
        doctor_user_ids_by_clinic: dict[str, list[int]] = {}
        for user in users:
            cid = user["clinic_id"]
            user_ids_by_clinic.setdefault(cid, []).append(user["user_id"])
            if user["role"] == "DOCTOR":
                doctor_user_ids_by_clinic.setdefault(cid, []).append(user["user_id"])

        patients_by_clinic = _seed_patients(conn, user_ids_by_clinic)
        inventory_by_clinic = _seed_inventory(conn)
        patient_encounters_by_clinic = _seed_encounters(
            conn, patients_by_clinic, doctor_user_ids_by_clinic
        )
        counts_by_clinic = _seed_prescriptions(
            conn, patients_by_clinic, patient_encounters_by_clinic,
            doctor_user_ids_by_clinic, inventory_by_clinic,
        )
        _seed_audit_events(conn)
        _seed_encounter_audit_events(conn)
        _seed_sessions(conn, users)
    finally:
        conn.close()

    total_patients = sum(len(p) for p in patients_by_clinic.values())
    total_prescriptions = sum(counts_by_clinic.values())
    print(f"Database seeded successfully at {db_path}.")
    for clinic_id, vols in CLINIC_VOLUMES.items():
        print(f"  {clinic_id}: {len(patients_by_clinic[clinic_id])} patients, "
              f"{counts_by_clinic[clinic_id]} prescriptions")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed AyurCore demo database with clinic-like synthetic data.")
    parser.add_argument("--db", default=DB_PATH, help="Path to the database to seed")
    args = parser.parse_args()
    seed_db(args.db)
