#!/usr/bin/env python3
"""
AyurCore v0.1 -- One-command demo clinic seeder.

Populates a disposable SQLite database with a fully functional synthetic Ayurvedic clinic:
  - Runs all migrations 001–022
  - Seeds 3 demo staff users (DOCTOR, ADMIN, AUDITOR) with known credentials
  - Seeds 6 synthetic patients with complete demographics
  - Seeds clinical encounters (mix of OPEN and CLOSED)
  - Seeds prescriptions linked to encounters with herbal formulations, dosha imbalances,
    AI proposal sources (local_extractor, gemini, manual), payment states, and dispensing status
  - Seeds inventory stock with batch numbers, expiry dates, and supplier details
  - Records tamper-evident audit events for encounters and prescriptions (SHA-256 hash chain)
  - Writes demo/credentials.v01-demo.json (git-ignored) with login credentials

Usage:
    ./venv/bin/python scripts/seed_demo_clinic.py
    ./venv/bin/python scripts/seed_demo_clinic.py --db db/demo_ayurcore.db
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db.database as database
from backend.auth_service import hash_password
from backend.audit_store import (
    AUDIT_EVENT_TYPE_ENCOUNTER_CLOSED,
    AUDIT_EVENT_TYPE_ENCOUNTER_OPENED,
    AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
    build_encounter_event_data,
    build_prescription_confirmed_event_data,
    create_audit_event,
    verify_audit_integrity,
)
from db.migrations import run_migrations

DEFAULT_DB_PATH = os.path.join(ROOT, "db", "demo_ayurcore.db")
DEFAULT_CREDENTIALS_PATH = os.path.join(ROOT, "demo", "credentials.v01-demo.json")
FORBIDDEN_BASENAME = "ayurcore.db"

DEMO_CLINIC_ID = "default_clinic"
DEMO_PASSWORD = "seed-demo-password-not-real"

DEMO_USERS = [
    {"username": "dr_sharma", "role": "DOCTOR", "clinic_id": DEMO_CLINIC_ID},
    {"username": "admin_kumar", "role": "ADMIN", "clinic_id": DEMO_CLINIC_ID},
    {"username": "auditor_nair", "role": "AUDITOR", "clinic_id": DEMO_CLINIC_ID},
]

DEMO_STOCK = [
    {
        "herb_name": "Ashwagandha",
        "botanical_name": "Withania somnifera",
        "stock_grams": 2500,
        "batch_code": "ASH-2026-001",
        "potency_score": 95,
        "expiry_date": "2027-06-30",
        "supplier": "Kerala Botanicals",
        "reorder_level_grams": 300,
    },
    {
        "herb_name": "Brahmi",
        "botanical_name": "Bacopa monnieri",
        "stock_grams": 1800,
        "batch_code": "BRA-2026-002",
        "potency_score": 92,
        "expiry_date": "2027-05-15",
        "supplier": "Southern Roots",
        "reorder_level_grams": 250,
    },
    {
        "herb_name": "Triphala",
        "botanical_name": "Emblica officinalis, Terminalia chebula, Terminalia bellirica",
        "stock_grams": 3200,
        "batch_code": "TRI-2026-003",
        "potency_score": 90,
        "expiry_date": "2027-08-20",
        "supplier": "Ayurvedic Herbs Co.",
        "reorder_level_grams": 400,
    },
    {
        "herb_name": "Neem",
        "botanical_name": "Azadirachta indica",
        "stock_grams": 1500,
        "batch_code": "NEE-2026-004",
        "potency_score": 90,
        "expiry_date": "2027-04-10",
        "supplier": "Himalayan Herbs",
        "reorder_level_grams": 200,
    },
    {
        "herb_name": "Vasaka",
        "botanical_name": "Justicia adhatoda",
        "stock_grams": 1200,
        "batch_code": "VAS-2026-005",
        "potency_score": 92,
        "expiry_date": "2027-07-01",
        "supplier": "Kerala Botanicals",
        "reorder_level_grams": 200,
    },
    {
        "herb_name": "Shatavari",
        "botanical_name": "Asparagus racemosus",
        "stock_grams": 1600,
        "batch_code": "SHA-2026-006",
        "potency_score": 94,
        "expiry_date": "2027-09-15",
        "supplier": "Southern Roots",
        "reorder_level_grams": 250,
    },
    {
        "herb_name": "Guggulu",
        "botanical_name": "Commiphora wightii",
        "stock_grams": 1400,
        "batch_code": "GUG-2026-007",
        "potency_score": 96,
        "expiry_date": "2027-10-30",
        "supplier": "Himalayan Herbs",
        "reorder_level_grams": 200,
    },
]

DEMO_PATIENTS = [
    {
        "clinic_patient_id": "DE-0157",
        "display_name": "Aarav Sharma (synthetic)",
        "phone": "+91 98450 12345",
        "gender": "Male",
        "date_of_birth": "1985-06-15",
        "prakriti": "Vata-Pitta",
        "address_line": "12 Temple Road",
        "city": "Kochi",
        "state": "Kerala",
        "pincode": "682001",
        "allergies": "Dust",
        "registered_at": "2026-08-01 09:00:00",
        "encounters": [
            {
                "status": "CLOSED",
                "visit_date": "2026-08-10 10:15:00",
                "closed_date": "2026-08-10 10:45:00",
                "visit_type": "FIRST_VISIT",
                "chief_complaint": "Persistent dry cough and throat irritation",
                "vitals_bp": "120/80",
                "vitals_weight_kg": 68.0,
                "vitals_temperature_c": 37.0,
                "follow_up_date": "2026-09-10",
                "prescription": {
                    "dosha_imbalance": "Vata",
                    "herbs": [("Vasaka", 10), ("Ashwagandha", 5)],
                    "proposal_source": "local_extractor",
                    "payment_status": "paid",
                    "payment_amount_cents": 45000,
                    "payment_method": "upi",
                    "payment_reference": "UPI9845012345",
                    "invoice_number": "INV-2026-0101",
                    "dispensed": True,
                    "dispensed_at": "2026-08-10 10:50:00",
                },
            },
            {
                "status": "OPEN",
                "visit_date": "2026-10-25 11:00:00",
                "closed_date": None,
                "visit_type": "FOLLOW_UP",
                "chief_complaint": "Cough reduced, mild fatigue remains",
                "vitals_bp": "118/78",
                "vitals_weight_kg": 68.2,
                "vitals_temperature_c": 36.8,
                "follow_up_date": "2026-11-25",
                "prescription": {
                    "dosha_imbalance": "Vata",
                    "herbs": [("Ashwagandha", 10), ("Brahmi", 5)],
                    "proposal_source": "gemini",
                    "payment_status": "unpaid",
                    "payment_amount_cents": None,
                    "payment_method": None,
                    "payment_reference": None,
                    "invoice_number": None,
                    "dispensed": False,
                    "dispensed_at": None,
                },
            },
        ],
    },
    {
        "clinic_patient_id": "DE-0158",
        "display_name": "Devika Menon (synthetic)",
        "phone": "+91 98450 23456",
        "gender": "Female",
        "date_of_birth": "1992-03-22",
        "prakriti": "Pitta",
        "address_line": "45 Palace Cross",
        "city": "Kochi",
        "state": "Kerala",
        "pincode": "682002",
        "allergies": None,
        "registered_at": "2026-09-01 10:00:00",
        "encounters": [
            {
                "status": "CLOSED",
                "visit_date": "2026-09-05 14:00:00",
                "closed_date": "2026-09-05 14:30:00",
                "visit_type": "FIRST_VISIT",
                "chief_complaint": "Hyperacidity and burning sensation in epigastrium",
                "vitals_bp": "115/75",
                "vitals_weight_kg": 54.0,
                "vitals_temperature_c": 36.9,
                "follow_up_date": "2026-10-05",
                "prescription": {
                    "dosha_imbalance": "Pitta",
                    "herbs": [("Shatavari", 10), ("Brahmi", 5)],
                    "proposal_source": "gemini",
                    "payment_status": "paid",
                    "payment_amount_cents": 48000,
                    "payment_method": "upi",
                    "payment_reference": "UPI9845023456",
                    "invoice_number": "INV-2026-0103",
                    "dispensed": True,
                    "dispensed_at": "2026-09-05 14:40:00",
                },
            },
            {
                "status": "CLOSED",
                "visit_date": "2026-10-12 15:15:00",
                "closed_date": "2026-10-12 15:45:00",
                "visit_type": "FOLLOW_UP",
                "chief_complaint": "Acid reflux improved, follow-up review",
                "vitals_bp": "116/76",
                "vitals_weight_kg": 54.5,
                "vitals_temperature_c": 36.7,
                "follow_up_date": "2026-11-12",
                "prescription": {
                    "dosha_imbalance": "Pitta",
                    "herbs": [("Shatavari", 5), ("Triphala", 5)],
                    "proposal_source": "manual",
                    "payment_status": "partial",
                    "payment_amount_cents": 30000,
                    "payment_method": "cash",
                    "payment_reference": None,
                    "invoice_number": "INV-2026-0104",
                    "dispensed": False,
                    "dispensed_at": None,
                },
            },
        ],
    },
    {
        "clinic_patient_id": "DE-0159",
        "display_name": "Rohan Verma (synthetic)",
        "phone": "+91 98450 34567",
        "gender": "Male",
        "date_of_birth": "1978-11-04",
        "prakriti": "Kapha",
        "address_line": "89 Hill View",
        "city": "Thrissur",
        "state": "Kerala",
        "pincode": "680001",
        "allergies": "Pollen",
        "registered_at": "2026-09-15 11:30:00",
        "encounters": [
            {
                "status": "CLOSED",
                "visit_date": "2026-09-18 09:30:00",
                "closed_date": "2026-09-18 10:00:00",
                "visit_type": "FIRST_VISIT",
                "chief_complaint": "Weight gain and sluggish digestion (Agni mandya)",
                "vitals_bp": "130/85",
                "vitals_weight_kg": 84.0,
                "vitals_temperature_c": 36.8,
                "follow_up_date": "2026-10-18",
                "prescription": {
                    "dosha_imbalance": "Kapha",
                    "herbs": [("Triphala", 10), ("Guggulu", 5)],
                    "proposal_source": "local_extractor",
                    "payment_status": "paid",
                    "payment_amount_cents": 52000,
                    "payment_method": "card",
                    "payment_reference": "CARD-9845034",
                    "invoice_number": "INV-2026-0105",
                    "dispensed": True,
                    "dispensed_at": "2026-09-18 10:10:00",
                },
            },
            {
                "status": "OPEN",
                "visit_date": "2026-10-28 16:00:00",
                "closed_date": None,
                "visit_type": "FOLLOW_UP",
                "chief_complaint": "Digestion active, joint stiffness in morning",
                "vitals_bp": "126/82",
                "vitals_weight_kg": 82.5,
                "vitals_temperature_c": 36.7,
                "follow_up_date": "2026-11-28",
                "prescription": {
                    "dosha_imbalance": "Kapha",
                    "herbs": [("Guggulu", 10), ("Neem", 5)],
                    "proposal_source": "manual",
                    "payment_status": "unpaid",
                    "payment_amount_cents": None,
                    "payment_method": None,
                    "payment_reference": None,
                    "invoice_number": None,
                    "dispensed": False,
                    "dispensed_at": None,
                },
            },
        ],
    },
    {
        "clinic_patient_id": "PT-8834",
        "display_name": "Meera Nair (synthetic)",
        "phone": "+91 98450 45678",
        "gender": "Female",
        "date_of_birth": "1988-09-12",
        "prakriti": "Vata",
        "address_line": "14 Marine Drive",
        "city": "Kochi",
        "state": "Kerala",
        "pincode": "682011",
        "allergies": None,
        "registered_at": "2026-10-01 11:00:00",
        "encounters": [
            {
                "status": "CLOSED",
                "visit_date": "2026-10-02 11:30:00",
                "closed_date": "2026-10-02 12:00:00",
                "visit_type": "FIRST_VISIT",
                "chief_complaint": "Insomnia and anxiety, restless leg sensation",
                "vitals_bp": "122/80",
                "vitals_weight_kg": 58.0,
                "vitals_temperature_c": 36.8,
                "follow_up_date": "2026-11-02",
                "prescription": {
                    "dosha_imbalance": "Vata",
                    "herbs": [("Brahmi", 10), ("Ashwagandha", 10)],
                    "proposal_source": "gemini",
                    "payment_status": "paid",
                    "payment_amount_cents": 60000,
                    "payment_method": "upi",
                    "payment_reference": "UPI9845045678",
                    "invoice_number": "INV-2026-0106",
                    "dispensed": True,
                    "dispensed_at": "2026-10-02 12:15:00",
                },
            },
        ],
    },
    {
        "clinic_patient_id": "XY-9902",
        "display_name": "Kavita Pillai (synthetic)",
        "phone": "+91 98450 56789",
        "gender": "Female",
        "date_of_birth": "1995-12-30",
        "prakriti": "Tridosha",
        "address_line": "77 Lake Road",
        "city": "Alappuzha",
        "state": "Kerala",
        "pincode": "688001",
        "allergies": None,
        "registered_at": "2026-09-18 14:00:00",
        "encounters": [
            {
                "status": "CLOSED",
                "visit_date": "2026-09-20 10:00:00",
                "closed_date": "2026-09-20 10:30:00",
                "visit_type": "FIRST_VISIT",
                "chief_complaint": "Skin flare-up and generalized rash",
                "vitals_bp": "118/76",
                "vitals_weight_kg": 62.0,
                "vitals_temperature_c": 37.1,
                "follow_up_date": "2026-10-20",
                "prescription": {
                    "dosha_imbalance": "Not recorded",
                    "herbs": [("Neem", 10), ("Triphala", 5)],
                    "proposal_source": "local_extractor",
                    "payment_status": "paid",
                    "payment_amount_cents": 42000,
                    "payment_method": "cash",
                    "payment_reference": None,
                    "invoice_number": "INV-2026-0107",
                    "dispensed": True,
                    "dispensed_at": "2026-09-20 10:45:00",
                },
            },
            {
                "status": "OPEN",
                "visit_date": "2026-10-29 14:00:00",
                "closed_date": None,
                "visit_type": "FOLLOW_UP",
                "chief_complaint": "Skin clearing, routine follow-up check",
                "vitals_bp": "116/74",
                "vitals_weight_kg": 62.1,
                "vitals_temperature_c": 36.8,
                "follow_up_date": "2026-11-29",
                "prescription": None,
            },
        ],
    },
    {
        "clinic_patient_id": "OP-1001",
        "display_name": "Anjali Nair (synthetic)",
        "phone": "+91 98450 67890",
        "gender": "Female",
        "date_of_birth": "1990-05-14",
        "prakriti": "Vata-Pitta",
        "address_line": "56 MG Road",
        "city": "Ernakulam",
        "state": "Kerala",
        "pincode": "682016",
        "allergies": None,
        "registered_at": "2026-10-30 09:30:00",
        "encounters": [],
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def validate_db_path(path: str, root: str = ROOT) -> str:
    resolved = os.path.realpath(os.path.abspath(path))
    forbidden_live = os.path.realpath(os.path.join(root, "db", "ayurcore.db"))

    if resolved == forbidden_live:
        raise ValueError(
            f"Refusing to operate on forbidden path: {resolved}. "
            "db/ayurcore.db ships empty and must not be modified."
        )
    if os.path.basename(resolved).lower() == FORBIDDEN_BASENAME:
        raise ValueError(f"Refusing to operate on database named {FORBIDDEN_BASENAME}.")
    if not resolved.endswith(".db"):
        raise ValueError(f"Database path must end in .db: {resolved}")
    return resolved


def seed_demo_clinic(
    db_path: str = DEFAULT_DB_PATH,
    credentials_path: str = DEFAULT_CREDENTIALS_PATH,
    root: str = ROOT,
) -> dict:
    resolved_db = validate_db_path(db_path, root)
    os.makedirs(os.path.dirname(resolved_db), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(credentials_path)), exist_ok=True)

    saved_db_path = database.DB_PATH
    database.DB_PATH = resolved_db

    conn = sqlite3.connect(resolved_db, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")

    try:
        # Step 1: Run migrations
        run_migrations(conn=conn)

        # Step 2: Seed staff users
        users_by_name = {}
        for u in DEMO_USERS:
            row = conn.execute("SELECT user_id, username, role FROM users WHERE username = ?", (u["username"],)).fetchone()
            if row is None:
                pwd_hash = hash_password(DEMO_PASSWORD)
                cursor = conn.execute(
                    """
                    INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (u["username"], pwd_hash, u["clinic_id"], u["role"], _now(), _now()),
                )
                user_id = cursor.lastrowid
            else:
                user_id = row["user_id"]
            users_by_name[u["username"]] = user_id
        conn.commit()

        dr_user_id = users_by_name["dr_sharma"]

        # Step 3: Seed inventory stock
        for item in DEMO_STOCK:
            inv_row = conn.execute(
                "SELECT id FROM inventory WHERE clinic_id = ? AND herb_name = ?",
                (DEMO_CLINIC_ID, item["herb_name"]),
            ).fetchone()
            if inv_row is None:
                conn.execute(
                    """
                    INSERT INTO inventory (
                        clinic_id, herb_name, botanical_name, stock_grams, batch_code,
                        potency_score, expiry_date, supplier, reorder_level_grams
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        DEMO_CLINIC_ID,
                        item["herb_name"],
                        item["botanical_name"],
                        item["stock_grams"],
                        item["batch_code"],
                        item["potency_score"],
                        item["expiry_date"],
                        item["supplier"],
                        item["reorder_level_grams"],
                    ),
                )
        conn.commit()

        # Step 4: Seed patients, encounters, prescriptions, and audit events
        for p in DEMO_PATIENTS:
            p_row = conn.execute(
                "SELECT patient_pk FROM patients WHERE clinic_patient_id = ? AND clinic_id = ?",
                (p["clinic_patient_id"], DEMO_CLINIC_ID),
            ).fetchone()

            if p_row is None:
                cursor = conn.execute(
                    """
                    INSERT INTO patients (
                        clinic_id, clinic_patient_id, display_name, phone, created_by_user_id,
                        created_at, date_of_birth, gender, address_line, city, state, pincode,
                        prakriti, allergies, registered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        DEMO_CLINIC_ID,
                        p["clinic_patient_id"],
                        p["display_name"],
                        p["phone"],
                        dr_user_id,
                        p["registered_at"],
                        p["date_of_birth"],
                        p["gender"],
                        p["address_line"],
                        p["city"],
                        p["state"],
                        p["pincode"],
                        p["prakriti"],
                        p["allergies"],
                        p["registered_at"],
                    ),
                )
                patient_pk = cursor.lastrowid
                conn.commit()
            else:
                patient_pk = p_row["patient_pk"]

            # Check if encounters already exist for this patient (idempotency)
            enc_count = conn.execute(
                "SELECT COUNT(*) FROM encounters WHERE patient_pk = ?", (patient_pk,)
            ).fetchone()[0]

            if enc_count == 0:
                for enc_data in p["encounters"]:
                    encounter_ref = secrets.token_hex(8)
                    visit_date = enc_data["visit_date"]

                    # Begin transaction for atomic encounter + prescription + audit writes
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        # 4a. Insert encounter as OPEN
                        cursor = conn.execute(
                            """
                            INSERT INTO encounters (
                                clinic_id, patient_pk, created_by_user_id, status, created_at,
                                closed_at, encounter_ref, visit_type, chief_complaint, vitals_bp,
                                vitals_weight_kg, vitals_temperature_c, follow_up_date
                            ) VALUES (?, ?, ?, 'OPEN', ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                DEMO_CLINIC_ID,
                                patient_pk,
                                dr_user_id,
                                visit_date,
                                encounter_ref,
                                enc_data["visit_type"],
                                enc_data["chief_complaint"],
                                enc_data["vitals_bp"],
                                enc_data["vitals_weight_kg"],
                                enc_data["vitals_temperature_c"],
                                enc_data["follow_up_date"],
                            ),
                        )
                        encounter_pk = cursor.lastrowid

                        # Emit ENCOUNTER_OPENED audit event
                        open_event_data = build_encounter_event_data(encounter_ref)
                        create_audit_event(
                            conn,
                            clinic_id=DEMO_CLINIC_ID,
                            actor_user_id=dr_user_id,
                            event_type=AUDIT_EVENT_TYPE_ENCOUNTER_OPENED,
                            prescription_id=None,
                            preview_id=None,
                            event_data=open_event_data,
                        )

                        # 4b. If prescription exists, insert it while encounter is still OPEN
                        rx_data = enc_data.get("prescription")
                        if rx_data:
                            herb_lines = rx_data["herbs"]
                            herbs_json = json.dumps([{"name": name, "grams": g} for name, g in herb_lines])
                            description = (
                                f"Patient: {p['display_name']} | "
                                f"Dosha: {rx_data['dosha_imbalance']} | "
                                f"Formulation: {herbs_json}"
                            )

                            dispensed_by = dr_user_id if rx_data["dispensed"] else None
                            dispensed_at = rx_data["dispensed_at"] if rx_data["dispensed"] else None

                            cursor = conn.execute(
                                """
                                INSERT INTO prescriptions (
                                    clinic_id, patient_name, description, status, created_at,
                                    patient_pk, encounter_pk, dosha_imbalance, payment_status,
                                    payment_amount_cents, payment_method, payment_reference,
                                    dispensed_at, dispensed_by_user_id, invoice_number, proposal_source
                                ) VALUES (?, ?, ?, 'CONFIRMED', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    DEMO_CLINIC_ID,
                                    p["display_name"],
                                    description,
                                    visit_date,
                                    patient_pk,
                                    encounter_pk,
                                    rx_data["dosha_imbalance"],
                                    rx_data["payment_status"],
                                    rx_data["payment_amount_cents"],
                                    rx_data["payment_method"],
                                    rx_data["payment_reference"],
                                    dispensed_at,
                                    dispensed_by,
                                    rx_data["invoice_number"],
                                    rx_data["proposal_source"],
                                ),
                            )
                            prescription_id = cursor.lastrowid

                            # Immediately insert prescription_items (migration 016 requires newest row)
                            for line_no, (h_name, grams) in enumerate(herb_lines, 1):
                                conn.execute(
                                    """
                                    INSERT INTO prescription_items (
                                        prescription_id, clinic_id, line_no, herb_name, grams, created_at
                                    ) VALUES (?, ?, ?, ?, ?, ?)
                                    """,
                                    (prescription_id, DEMO_CLINIC_ID, line_no, h_name, grams, visit_date),
                                )

                            # Insert prescription_previews row
                            preview_id = f"preview_demo_{prescription_id}"
                            is_manual = 1 if rx_data["proposal_source"] == "manual" else 0
                            confirmed_herbs = [{"name": name, "grams": g} for name, g in herb_lines]
                            confirmed_herbs_json = json.dumps(confirmed_herbs)

                            conn.execute(
                                """
                                INSERT INTO prescription_previews (
                                    preview_id, clinic_id, patient_name, is_manual,
                                    ai_dosha_imbalance, ai_prescribed_herbs_json,
                                    confirmed_dosha_imbalance, confirmed_prescribed_herbs_json,
                                    status, created_by_session, created_at, expires_at, confirmed_at,
                                    encounter_pk, patient_pk, proposal_source
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CONFIRMED', NULL, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    preview_id,
                                    DEMO_CLINIC_ID,
                                    p["display_name"],
                                    is_manual,
                                    rx_data["dosha_imbalance"],
                                    confirmed_herbs_json,
                                    rx_data["dosha_imbalance"],
                                    confirmed_herbs_json,
                                    visit_date,
                                    visit_date,
                                    visit_date,
                                    encounter_pk,
                                    patient_pk,
                                    rx_data["proposal_source"],
                                ),
                            )

                            # Audit event for prescription confirmation
                            if is_manual:
                                rx_event_data = build_prescription_confirmed_event_data(
                                    is_manual=True,
                                    ai_dosha_imbalance=None,
                                    ai_prescribed_herbs=None,
                                    confirmed_dosha_imbalance=rx_data["dosha_imbalance"],
                                    confirmed_prescribed_herbs=confirmed_herbs,
                                )
                            else:
                                rx_event_data = build_prescription_confirmed_event_data(
                                    is_manual=False,
                                    ai_dosha_imbalance=rx_data["dosha_imbalance"],
                                    ai_prescribed_herbs=confirmed_herbs,
                                    confirmed_dosha_imbalance=rx_data["dosha_imbalance"],
                                    confirmed_prescribed_herbs=confirmed_herbs,
                                )

                            create_audit_event(
                                conn,
                                clinic_id=DEMO_CLINIC_ID,
                                actor_user_id=dr_user_id,
                                event_type=AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
                                prescription_id=prescription_id,
                                preview_id=preview_id,
                                event_data=rx_event_data,
                            )

                        # 4c. If encounter is closed, close it and emit ENCOUNTER_CLOSED
                        if enc_data["status"] == "CLOSED":
                            closed_at = enc_data["closed_date"] or visit_date
                            conn.execute(
                                "UPDATE encounters SET status = 'CLOSED', closed_at = ? WHERE encounter_pk = ?",
                                (closed_at, encounter_pk),
                            )
                            close_event_data = build_encounter_event_data(encounter_ref)
                            create_audit_event(
                                conn,
                                clinic_id=DEMO_CLINIC_ID,
                                actor_user_id=dr_user_id,
                                event_type=AUDIT_EVENT_TYPE_ENCOUNTER_CLOSED,
                                prescription_id=None,
                                preview_id=None,
                                event_data=close_event_data,
                            )

                        conn.commit()
                    except BaseException:
                        conn.rollback()
                        raise

        # Step 5: Verify SHA-256 audit integrity
        report = verify_audit_integrity(clinic_id=DEMO_CLINIC_ID, conn=conn)
        if not report.valid:
            raise RuntimeError(f"Audit chain verification failed: {report.violations}")

        # Step 6: Query final stats
        counts = {
            "users": conn.execute("SELECT COUNT(*) FROM users WHERE clinic_id = ?", (DEMO_CLINIC_ID,)).fetchone()[0],
            "patients": conn.execute("SELECT COUNT(*) FROM patients WHERE clinic_id = ?", (DEMO_CLINIC_ID,)).fetchone()[0],
            "encounters": conn.execute("SELECT COUNT(*) FROM encounters WHERE clinic_id = ?", (DEMO_CLINIC_ID,)).fetchone()[0],
            "prescriptions": conn.execute("SELECT COUNT(*) FROM prescriptions WHERE clinic_id = ?", (DEMO_CLINIC_ID,)).fetchone()[0],
            "inventory": conn.execute("SELECT COUNT(*) FROM inventory WHERE clinic_id = ?", (DEMO_CLINIC_ID,)).fetchone()[0],
            "audit_events": report.checked_count,
        }

        # Step 7: Write credentials file (supports both 'users' and 'credentials' keys)
        user_list = [
            {
                "username": u["username"],
                "password": DEMO_PASSWORD,
                "role": u["role"],
                "clinic_id": u["clinic_id"],
            }
            for u in DEMO_USERS
        ]
        creds_content = {
            "database": os.path.relpath(resolved_db, root),
            "clinic_id": DEMO_CLINIC_ID,
            "users": user_list,
            "credentials": user_list,
            "created_at": _now(),
        }
        with open(credentials_path, "w", encoding="utf-8") as f:
            json.dump(creds_content, f, indent=2)
        try:
            os.chmod(credentials_path, 0o600)
        except OSError:
            pass

        return {
            "database_path": resolved_db,
            "credentials_path": credentials_path,
            "counts": counts,
            "audit_valid": report.valid,
            "credentials": [
                (u["role"], u["username"], DEMO_PASSWORD) for u in DEMO_USERS
            ],
        }

    finally:
        conn.close()
        database.DB_PATH = saved_db_path


def main():
    parser = argparse.ArgumentParser(description="Seed AyurCore v0.1 demo clinic database.")
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"Target SQLite database path (default: {os.path.relpath(DEFAULT_DB_PATH, ROOT)})",
    )
    parser.add_argument(
        "--credentials",
        default=DEFAULT_CREDENTIALS_PATH,
        help=f"Path to write credentials JSON (default: {os.path.relpath(DEFAULT_CREDENTIALS_PATH, ROOT)})",
    )
    args = parser.parse_args()

    try:
        summary = seed_demo_clinic(db_path=args.db, credentials_path=args.credentials)
    except Exception as e:
        print(f"\n[ERROR] Seeding failed: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 72)
    print("                 AYURCORE DEMO CLINIC SEEDED SUCCESSFULLY")
    print("=" * 72)
    print(f"  Database:         {summary['database_path']}")
    print(f"  Credentials file: {summary['credentials_path']}")
    print(f"  Clinic ID:        {DEMO_CLINIC_ID}")
    print("-" * 72)
    print("  Populated Volumes:")
    for entity, cnt in summary["counts"].items():
        print(f"    - {entity:<16}: {cnt}")
    print(f"    - audit_chain_valid : {summary['audit_valid']}")
    print("-" * 72)
    print("  Demo Login Credentials:")
    print("    Role        Username        Password")
    print("    " + "-" * 56)
    for role, username, pwd in summary["credentials"]:
        print(f"    {role:<11} {username:<15} {pwd}")
    print("=" * 72 + "\n")


if __name__ == "__main__":
    main()
