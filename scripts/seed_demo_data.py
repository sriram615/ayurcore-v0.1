#!/usr/bin/env python3
"""
Deterministic, idempotent demo/test dataset seeder for AyurCore OS.

WHAT THIS IS
------------
Creates a small, clearly-synthetic dataset (demo users, demo inventory, and
a batch of real, backend-confirmed demo prescriptions/audit history) so the
existing Next.js + FastAPI application can be manually exercised end to end
-- for interactive Step-2 clinic simulation now, and for the final project
demonstration later. It invents NOTHING about clinical logic, auth, RBAC,
audit, idempotency, or inventory deduction: every prescription/audit record
this script produces is created by calling the REAL application code
(backend.auth_service.create_user, and the real HTTP endpoints via
FastAPI's own TestClient -- the same test harness every file in tests/
already uses) exactly the way the real Next.js frontend or a doctor at a
keyboard would.

WHY TestClient, NOT raw SQL, FOR PRESCRIPTIONS/AUDIT
-----------------------------------------------------
/prescribe/confirm's transaction (inventory deduction, prescription insert,
tamper-evident audit hash chaining, idempotency bookkeeping) lives entirely
inside backend/main.py's route handler -- there is no lower-level function
that performs "confirm a prescription" without also being that HTTP route.
Going through TestClient (in-process, no separate server needed) is the
only way to create demo history using the application's OWN, real
mechanisms rather than hand-rolling a parallel implementation of hash
chaining or inventory deduction, which the task explicitly forbids
("Do NOT manually fabricate audit hashes", "Do NOT bypass the existing
inventory deduction logic").

HOW DEMO RECORDS ARE MARKED (schema-safe, no migration)
---------------------------------------------------------
This schema has no dedicated "is_demo" column anywhere (prescriptions,
prescription_previews, users, inventory -- none of them). Rather than add
one (a schema change this task says to avoid unless "absolutely
required"), every demo patient name sent through the real API is prefixed
with the literal marker "(DEMO) " -- e.g. "(DEMO) Anil Kumar". Because
patient_name is copied verbatim into prescriptions.patient_name and
prescription_previews.patient_name by the application's own existing code,
this marker is exact-match filterable (`WHERE patient_name LIKE '(DEMO) %'`)
and is visible in the UI itself, satisfying "demo data must be clearly
synthetic" and "distinguishable from non-demo data" without touching the
schema. Demo inventory rows (test_clinic_b only -- see below) are marked
the same way via a "DEMO-" batch_code prefix. Demo users are identified by
their fixed, reserved usernames (demo_doctor, demo_admin, demo_auditor,
demo_doctor_b) -- verified unique against this project's real data before
relying on it (see module docstring history below / DEMO_DATA.md).

A companion manifest file (.demo_seed_manifest.json, next to this script)
additionally records exactly which rows this script created -- not as the
primary safety mechanism (the markers above are), but so --reset can find
them without any table scan, and so re-running this script is a fast,
correct no-op instead of re-deriving state from scratch every time.

WHY default_clinic'S PRESCRIPTIONS/AUDIT HISTORY CANNOT BE FULLY "RESET"
--------------------------------------------------------------------------
This task's own spec puts demo_doctor/demo_admin/demo_auditor in
"default_clinic" -- the same clinic_id this project's entire test suite
has been confirming real prescriptions against for months (1000+ rows,
observed directly in this database before this script ever ran). Two
independent, load-bearing safety properties of this application make a
literal "delete everything this script touched" reset impossible once a
demo prescription is actually confirmed, and this script does not attempt
to bypass either one:

  1. Foreign keys: audit_events.actor_user_id and prescriptions.id (via
     audit_events.prescription_id) are real FKs, enforced live
     (db/database.py sets `PRAGMA foreign_keys = ON`). Once demo_doctor has
     confirmed even one prescription, deleting the demo_doctor user row (or
     the prescription row) raises a real IntegrityError -- by design, the
     same protection that stops anyone else's data from being silently
     orphaned.
  2. Tamper-evident, per-clinic hash chain: backend/audit_store.py chains
     every clinic's audit_events by previous_event_hash, in insertion
     order, and default_clinic's chain already has hundreds of REAL
     (test-suite-generated) entries before this script's own entries join
     it. Deleting a demo audit_events row that isn't at the very end of
     that shared chain would break previous_event_hash linkage for every
     row appended after it -- corrupting audit integrity for entirely
     unrelated data. Corrupting audit integrity is explicitly forbidden by
     this task ("Do NOT modify audit integrity").

Given both constraints, --reset in this script is a best-effort, safety-first
cleanup: it removes everything that IS safely removable (demo sessions,
demo login-throttle rows, any of this script's own prescription_previews
that were never confirmed, and the dedicated test_clinic_b demo inventory
rows), and it ATTEMPTS to delete the four demo user rows, succeeding only
for ones that were truly never referenced by a confirmed prescription
(observed in practice: demo_admin/demo_auditor, which only ever read audit
data and are never an audit_events.actor_user_id). For demo_doctor and
demo_doctor_b, once real demo history exists, --reset reports (does not
hide) that their user rows and their confirmed prescription/audit history
are being kept in place, exactly as the application's own append-only
design intends for every clinic's data, demo or not. See docs/DEMO_DATA.md,
section "Restoring the clean demo state", for what this means in practice.

IDEMPOTENCY
-----------
Users: create_user() is called for each of the 4 fixed usernames; a
UsernameAlreadyExistsError is treated as "already there, nothing to do."
Clinic-B inventory: a row is inserted for a given (clinic_id, herb_name)
only if no row for that combination already exists there (test_clinic_b
has zero pre-existing inventory rows in this database, confirmed before
writing this script, so no ambiguity is possible for a fresh clinic; a
re-run naturally finds its own previously-inserted rows and skips them).
Demo prescription/audit HISTORY (the ~13-record scenario battery) is
gated by the manifest's "history_seeded" flag -- created exactly once;
every later run reports "already seeded" and creates nothing further,
regardless of how many times it is invoked. The three live REJECTION
verifications (insufficient stock, unknown herb, cross-clinic isolation)
are side-effect-free (nothing is ever committed on a rejection path) and
are re-run every invocation as a lightweight regression check.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import get_connection  # noqa: E402
from backend.auth_service import create_user, UsernameAlreadyExistsError  # noqa: E402

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".demo_seed_manifest.json")

# Documented, non-secret demo password -- same naming convention this
# project's own tests already use for synthetic credentials (see
# tests/test_ui_auth_integration.py's "...-not-a-real-credential" values).
# Argon2id-hashed exactly like every other account's password; nothing
# about password hashing is weakened for demo accounts.
DEMO_PASSWORD = "ayurcore-demo-password-not-a-real-credential"

DEMO_CLINIC_DEFAULT = "default_clinic"
DEMO_CLINIC_B = "test_clinic_b"

# The exact, reserved usernames this script owns. Verified before this
# script was written that none of these exist in this database already
# (SELECT ... WHERE username LIKE 'demo\_%' ESCAPE '\' returned zero rows).
DEMO_USERS = [
    {"username": "demo_doctor", "role": "DOCTOR", "clinic_id": DEMO_CLINIC_DEFAULT},
    {"username": "demo_admin", "role": "ADMIN", "clinic_id": DEMO_CLINIC_DEFAULT},
    {"username": "demo_auditor", "role": "AUDITOR", "clinic_id": DEMO_CLINIC_DEFAULT},
    {"username": "demo_doctor_b", "role": "DOCTOR", "clinic_id": DEMO_CLINIC_B},
]

DEMO_NAME_MARKER = "(DEMO) "

# test_clinic_b has ZERO pre-existing inventory rows in this database
# (confirmed by direct query before writing this script) -- there is no
# multi-row-per-herb ambiguity risk here the way there would be for
# default_clinic (whose 20 herbs each already have exactly one row, relied
# on by the rest of this project's own test suite; this script never
# inserts, updates, or deletes anything in default_clinic's inventory).
# batch_code carries the "DEMO-" marker; potency_score/botanical_name are
# cosmetic and mirror db/seed.py's own baseline values for the same herbs.
# Shatavari is deliberately given a small stock_grams value -- a real,
# persistent low-stock scenario (DEMO-004 can also be demonstrated live
# against it) that costs nothing to maintain since this clinic has no
# other consumer of these rows.
DEMO_CLINIC_B_INVENTORY = [
    {"herb_name": "Ashwagandha", "botanical_name": "Withania somnifera", "stock_grams": 5000, "batch_code": "DEMO-ASH-B01", "potency_score": 95},
    {"herb_name": "Brahmi", "botanical_name": "Bacopa monnieri", "stock_grams": 3500, "batch_code": "DEMO-BRA-B01", "potency_score": 92},
    {"herb_name": "Haridra", "botanical_name": "Curcuma longa", "stock_grams": 4000, "batch_code": "DEMO-HAR-B01", "potency_score": 98},
    {"herb_name": "Triphala", "botanical_name": "Emblica officinalis, Terminalia chebula, Terminalia bellirica", "stock_grams": 6000, "batch_code": "DEMO-TRI-B01", "potency_score": 90},
    {"herb_name": "Shatavari", "botanical_name": "Asparagus racemosus", "stock_grams": 40, "batch_code": "DEMO-SHA-B01-LOWSTOCK", "potency_score": 94},
]

# ---------------------------------------------------------------------------
# Demo "patients". This schema has NO patient table and NO age/sex fields
# anywhere (PreviewRequest/ManualPreviewRequest carry only patient_name,
# patient_phone, patient_id -- see backend/schemas.py) -- age/sex below are
# narrative-only, recorded in the manifest and docs/DEMO_DATA.md for the
# demo script's own storytelling, and are NEVER sent to the API (there is
# nowhere for the API to put them). See docs/DEMO_DATA.md's "Limitations"
# section and this script's own final report.
# ---------------------------------------------------------------------------
DEMO_PATIENTS_DEFAULT_CLINIC = [
    {"id": "DEMO-PT-001", "name": "Anil Kumar", "age": 34, "sex": "M"},
    {"id": "DEMO-PT-002", "name": "Meera Nair", "age": 27, "sex": "F"},
    {"id": "DEMO-PT-003", "name": "Ravi Menon", "age": 52, "sex": "M"},
    {"id": "DEMO-PT-004", "name": "Lakshmi Devi", "age": 61, "sex": "F"},
    {"id": "DEMO-PT-005", "name": "Arun Thomas", "age": 40, "sex": "M"},
    {"id": "DEMO-PT-006", "name": "Divya Pillai", "age": 29, "sex": "F"},
    {"id": "DEMO-PT-007", "name": "Suresh Nambiar", "age": 47, "sex": "M"},
    {"id": "DEMO-PT-008", "name": "Priya Warrier", "age": 33, "sex": "F"},
    {"id": "DEMO-PT-009", "name": "Rajesh Pillai", "age": 58, "sex": "M"},
    {"id": "DEMO-PT-010", "name": "Geetha Kurup", "age": 45, "sex": "F"},
    {"id": "DEMO-PT-011", "name": "Vinod Menon", "age": 38, "sex": "M"},  # DEMO-004 (insufficient stock)
    {"id": "DEMO-PT-012", "name": "Sunitha Joseph", "age": 31, "sex": "F"},  # DEMO-005 (unknown herb)
]

DEMO_PATIENTS_CLINIC_B = [
    {"id": "DEMO-PT-B01", "name": "Manoj Varma", "age": 44, "sex": "M"},
    {"id": "DEMO-PT-B02", "name": "Anjali Menon", "age": 26, "sex": "F"},
    {"id": "DEMO-PT-B03", "name": "Thomas George", "age": 55, "sex": "M"},
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_manifest() -> dict:
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "history_seeded": False, "users": {}, "clinic_b_inventory_ids": [], "history": []}


def _save_manifest(manifest: dict) -> None:
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# 1. Users
# ---------------------------------------------------------------------------

def ensure_users(manifest: dict) -> dict:
    """Creates the 4 demo users if they don't already exist. Uses the
    project's own create_user() (Argon2id hashing, unique-username
    enforcement) -- never a hand-rolled INSERT. Idempotent: a user that
    already exists (this run or a previous one) is left completely
    untouched, including its password."""
    results = {}
    for spec in DEMO_USERS:
        username = spec["username"]
        try:
            user_id = create_user(username, DEMO_PASSWORD, spec["clinic_id"], role=spec["role"])
            results[username] = {"user_id": user_id, "created": True}
            print(f"  created user: {username} (user_id={user_id}, role={spec['role']}, clinic={spec['clinic_id']})")
        except UsernameAlreadyExistsError:
            conn = get_connection()
            try:
                row = conn.execute(
                    "SELECT user_id, role, clinic_id FROM users WHERE username = ?", (username,)
                ).fetchone()
            finally:
                conn.close()
            results[username] = {"user_id": row["user_id"], "created": False}
            print(f"  already exists: {username} (user_id={row['user_id']}, role={row['role']}, clinic={row['clinic_id']})")
    manifest["users"] = {u: r["user_id"] for u, r in results.items()}
    return results


# ---------------------------------------------------------------------------
# 2. Inventory (test_clinic_b only -- default_clinic is never written to)
# ---------------------------------------------------------------------------

def ensure_clinic_b_inventory(manifest: dict) -> list:
    conn = get_connection()
    inserted_ids = list(manifest.get("clinic_b_inventory_ids", []))
    try:
        for herb in DEMO_CLINIC_B_INVENTORY:
            existing = conn.execute(
                "SELECT id FROM inventory WHERE clinic_id = ? AND herb_name = ?",
                (DEMO_CLINIC_B, herb["herb_name"]),
            ).fetchone()
            if existing is not None:
                print(f"  already present: {herb['herb_name']} @ {DEMO_CLINIC_B} (id={existing['id']})")
                if existing["id"] not in inserted_ids:
                    inserted_ids.append(existing["id"])
                continue
            cursor = conn.execute(
                """
                INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    DEMO_CLINIC_B, herb["herb_name"], herb["botanical_name"],
                    herb["stock_grams"], herb["batch_code"], herb["potency_score"],
                ),
            )
            conn.commit()
            inserted_ids.append(cursor.lastrowid)
            print(f"  inserted: {herb['herb_name']} @ {DEMO_CLINIC_B} = {herb['stock_grams']}g (id={cursor.lastrowid}, batch={herb['batch_code']})")
    finally:
        conn.close()
    manifest["clinic_b_inventory_ids"] = inserted_ids
    return inserted_ids


def report_default_clinic_inventory() -> dict:
    """Read-only: confirms default_clinic already stocks the usable target
    herbs (never written to by this script -- see module docstring)."""
    conn = get_connection()
    try:
        rows = {
            r["herb_name"]: r["stock_grams"]
            for r in conn.execute(
                "SELECT herb_name, stock_grams FROM inventory WHERE clinic_id = ?", (DEMO_CLINIC_DEFAULT,)
            ).fetchall()
        }
    finally:
        conn.close()
    for herb in ("Ashwagandha", "Brahmi", "Haridra", "Triphala", "Shatavari"):
        stock = rows.get(herb)
        print(f"  default_clinic already stocks {herb}: {stock}g (untouched by this script)")
    return rows


# ---------------------------------------------------------------------------
# 3. Demo prescription/audit history -- real HTTP calls via TestClient
# ---------------------------------------------------------------------------

def _login(client, username: str, password: str = DEMO_PASSWORD) -> dict:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    if resp.status_code != 200:
        raise RuntimeError(f"login failed for {username!r}: {resp.status_code} {resp.text}")
    body = resp.json()
    return {"token": body["token"], "headers": {"Authorization": f"Bearer {body['token']}"}}


def _audit_event_for_prescription(prescription_id: int) -> int | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT event_id FROM audit_events WHERE prescription_id = ?", (prescription_id,)
        ).fetchone()
        return row["event_id"] if row else None
    finally:
        conn.close()


def _confirm_manual(client, auth: dict, patient: dict, dosha: str, herbs: list) -> dict:
    """Runs the manual fallback flow end to end: manual-preview -> confirm.
    Returns the confirmed PrescriptionResponse body."""
    preview_resp = client.post(
        "/prescribe/manual-preview",
        headers=auth["headers"],
        json={
            "patient_name": DEMO_NAME_MARKER + patient["name"],
            "patient_id": patient["id"],
            "dosha_imbalance": dosha,
            "prescribed_herbs": herbs,
        },
    )
    if preview_resp.status_code != 200:
        raise RuntimeError(f"manual-preview failed for {patient['name']!r}: {preview_resp.status_code} {preview_resp.text}")
    preview_id = preview_resp.json()["preview_id"]

    confirm_resp = client.post(
        "/prescribe/confirm",
        headers=auth["headers"],
        json={"preview_id": preview_id},
    )
    if confirm_resp.status_code != 200:
        raise RuntimeError(f"confirm failed for {patient['name']!r}: {confirm_resp.status_code} {confirm_resp.text}")
    return confirm_resp.json()


def _confirm_ai_as_is(client, auth: dict, patient: dict, notes: str) -> dict:
    """DEMO-001: AI preview (mock parser, GEMINI_API_KEY unset in this
    environment -- see agents/copilot.py's deterministic fixtures)
    confirmed unchanged."""
    preview_resp = client.post(
        "/prescribe/preview",
        headers=auth["headers"],
        json={
            "patient_name": DEMO_NAME_MARKER + patient["name"],
            "patient_id": patient["id"],
            "notes": notes,
        },
    )
    if preview_resp.status_code != 200:
        raise RuntimeError(f"AI preview failed for {patient['name']!r}: {preview_resp.status_code} {preview_resp.text}")
    preview_id = preview_resp.json()["preview_id"]

    confirm_resp = client.post(
        "/prescribe/confirm",
        headers=auth["headers"],
        json={"preview_id": preview_id},
    )
    if confirm_resp.status_code != 200:
        raise RuntimeError(f"confirm failed for {patient['name']!r}: {confirm_resp.status_code} {confirm_resp.text}")
    return confirm_resp.json()


def _confirm_ai_edited(client, auth: dict, patient: dict, notes: str, edited_dosha: str, edited_herbs: list) -> dict:
    """DEMO-002: AI preview, doctor edits dosha/quantities/herbs at confirm
    time -- exactly the doctor-correction path /prescribe/confirm already
    supports (ConfirmRequest.dosha_imbalance / prescribed_herbs override)."""
    preview_resp = client.post(
        "/prescribe/preview",
        headers=auth["headers"],
        json={
            "patient_name": DEMO_NAME_MARKER + patient["name"],
            "patient_id": patient["id"],
            "notes": notes,
        },
    )
    if preview_resp.status_code != 200:
        raise RuntimeError(f"AI preview failed for {patient['name']!r}: {preview_resp.status_code} {preview_resp.text}")
    preview_id = preview_resp.json()["preview_id"]

    confirm_resp = client.post(
        "/prescribe/confirm",
        headers=auth["headers"],
        json={"preview_id": preview_id, "dosha_imbalance": edited_dosha, "prescribed_herbs": edited_herbs},
    )
    if confirm_resp.status_code != 200:
        raise RuntimeError(f"edited confirm failed for {patient['name']!r}: {confirm_resp.status_code} {confirm_resp.text}")
    return confirm_resp.json()


def seed_history(manifest: dict) -> None:
    if manifest.get("history_seeded"):
        print(f"  demo prescription/audit history already seeded on {manifest.get('history_seeded_at', '?')} "
              f"({len(manifest.get('history', []))} records) -- skipping (idempotent).")
        return

    from fastapi.testclient import TestClient
    import backend.main as main_module

    history = []
    with TestClient(main_module.app) as client:
        doctor_a = _login(client, "demo_doctor")
        doctor_b = _login(client, "demo_doctor_b")

        pts = DEMO_PATIENTS_DEFAULT_CLINIC
        scenarios = [
            ("DEMO-001", pts[0], lambda: _confirm_ai_as_is(client, doctor_a, pts[0], "Patient presents with sluggish digestion and fatigue. Prescribing 400g of Ashwagandha for Kapha imbalance.")),
            ("DEMO-002", pts[1], lambda: _confirm_ai_edited(client, doctor_a, pts[1], "Prescribing 400g of Ashwagandha for Kapha imbalance.", "Vata-Kapha", [{"name": "Ashwagandha", "grams": 350}, {"name": "Shatavari", "grams": 50}])),
            ("DEMO-003", pts[2], lambda: _confirm_manual(client, doctor_a, pts[2], "Vata", [{"name": "Brahmi", "grams": 30}])),
            ("DEMO-003", pts[3], lambda: _confirm_manual(client, doctor_a, pts[3], "Pitta", [{"name": "Triphala", "grams": 40}, {"name": "Haridra", "grams": 10}])),
            ("DEMO-003", pts[4], lambda: _confirm_manual(client, doctor_a, pts[4], "Kapha", [{"name": "Shatavari", "grams": 25}])),
            ("DEMO-003", pts[5], lambda: _confirm_manual(client, doctor_a, pts[5], "Vata-Pitta", [{"name": "Ashwagandha", "grams": 20}, {"name": "Brahmi", "grams": 15}])),
            ("DEMO-003", pts[6], lambda: _confirm_manual(client, doctor_a, pts[6], "Pitta-Kapha", [{"name": "Triphala", "grams": 60}])),
            ("DEMO-003", pts[7], lambda: _confirm_manual(client, doctor_a, pts[7], "Vata", [{"name": "Haridra", "grams": 20}, {"name": "Shatavari", "grams": 30}])),
            ("DEMO-003", pts[8], lambda: _confirm_manual(client, doctor_a, pts[8], "Kapha", [{"name": "Ashwagandha", "grams": 45}])),
            ("DEMO-003", pts[9], lambda: _confirm_manual(client, doctor_a, pts[9], "Vata-Kapha", [{"name": "Brahmi", "grams": 25}, {"name": "Triphala", "grams": 35}])),
        ]

        for scenario_id, patient, action in scenarios:
            result = action()
            audit_event_id = _audit_event_for_prescription(result["prescription_id"])
            record = {
                "scenario": scenario_id,
                "clinic_id": DEMO_CLINIC_DEFAULT,
                "actor": "demo_doctor",
                "patient_id": patient["id"],
                "patient_name": DEMO_NAME_MARKER + patient["name"],
                "prescription_id": result["prescription_id"],
                "audit_event_id": audit_event_id,
                "dosha_imbalance": result["dosha_imbalance"],
                "prescribed_herbs": result["prescribed_herbs"],
            }
            history.append(record)
            print(f"  [{scenario_id}] {record['patient_name']}: prescription_id={record['prescription_id']} "
                  f"audit_event_id={audit_event_id} dosha={record['dosha_imbalance']}")

        pts_b = DEMO_PATIENTS_CLINIC_B
        scenarios_b = [
            ("DEMO-003", pts_b[0], lambda: _confirm_manual(client, doctor_b, pts_b[0], "Vata", [{"name": "Ashwagandha", "grams": 20}])),
            ("DEMO-003", pts_b[1], lambda: _confirm_manual(client, doctor_b, pts_b[1], "Pitta", [{"name": "Brahmi", "grams": 15}, {"name": "Haridra", "grams": 10}])),
            ("DEMO-003", pts_b[2], lambda: _confirm_manual(client, doctor_b, pts_b[2], "Kapha", [{"name": "Triphala", "grams": 30}])),
        ]
        for scenario_id, patient, action in scenarios_b:
            result = action()
            audit_event_id = _audit_event_for_prescription(result["prescription_id"])
            record = {
                "scenario": scenario_id,
                "clinic_id": DEMO_CLINIC_B,
                "actor": "demo_doctor_b",
                "patient_id": patient["id"],
                "patient_name": DEMO_NAME_MARKER + patient["name"],
                "prescription_id": result["prescription_id"],
                "audit_event_id": audit_event_id,
                "dosha_imbalance": result["dosha_imbalance"],
                "prescribed_herbs": result["prescribed_herbs"],
            }
            history.append(record)
            print(f"  [{scenario_id}] {record['patient_name']} @ {DEMO_CLINIC_B}: prescription_id={record['prescription_id']} "
                  f"audit_event_id={audit_event_id} dosha={record['dosha_imbalance']}")

        # Log out both demo doctors' seeding sessions -- the demo dataset
        # should hand a caller fresh logins, not sessions this script
        # happens to leave dangling.
        client.post("/auth/logout", headers=doctor_a["headers"])
        client.post("/auth/logout", headers=doctor_b["headers"])

    manifest["history"] = history
    manifest["history_seeded"] = True
    manifest["history_seeded_at"] = _now_iso()
    print(f"  seeded {len(history)} confirmed demo prescriptions with valid audit events.")


# ---------------------------------------------------------------------------
# 4. Live verification scenarios (DEMO-004 / DEMO-005 / DEMO-008) --
#    side-effect-free (every one of these is a REJECTION; nothing is ever
#    committed), so these are safe to re-run on every invocation as a
#    lightweight regression check, independent of history_seeded.
# ---------------------------------------------------------------------------

def run_verifications() -> dict:
    from fastapi.testclient import TestClient
    import backend.main as main_module

    results = {}
    with TestClient(main_module.app) as client:
        doctor_a = _login(client, "demo_doctor")
        doctor_b = _login(client, "demo_doctor_b")

        # DEMO-004: insufficient inventory -> safe rejection (409). Reads
        # CURRENT stock live rather than assuming a fixed number, so this
        # stays correct no matter how much prior test/demo activity has
        # already drawn down default_clinic's Brahmi -- guarantees the
        # requested amount exceeds stock without ever writing to inventory.
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT stock_grams FROM inventory WHERE clinic_id = ? AND herb_name = 'Brahmi'",
                (DEMO_CLINIC_DEFAULT,),
            ).fetchone()
        finally:
            conn.close()
        current_brahmi_stock = row["stock_grams"] if row else 0
        over_request = current_brahmi_stock + 5000
        preview = client.post(
            "/prescribe/manual-preview",
            headers=doctor_a["headers"],
            json={
                "patient_name": DEMO_NAME_MARKER + "Vinod Menon",
                "patient_id": "DEMO-PT-011",
                "dosha_imbalance": "Pitta",
                "prescribed_herbs": [{"name": "Brahmi", "grams": over_request}],
            },
        )
        confirm = client.post(
            "/prescribe/confirm",
            headers=doctor_a["headers"],
            json={"preview_id": preview.json()["preview_id"]},
        ) if preview.status_code == 200 else None
        results["DEMO-004_insufficient_stock"] = {
            "requested_grams": over_request,
            "current_stock_grams": current_brahmi_stock,
            "preview_status": preview.status_code,
            "confirm_status": confirm.status_code if confirm is not None else None,
            "expected": "confirm -> 409 (InsufficientStockError)",
            "passed": bool(confirm is not None and confirm.status_code == 409),
        }

        # DEMO-005: unknown herb -> safe rejection (400) through the AI
        # path. "Tulsi" is not in backend/herb_resolver.py's HERB_ALIASES
        # (confirmed by inspection) -- deliberately used here as a live,
        # real example of an unresolvable herb, rather than a fictional
        # name invented just for this test. This is the mock AI parser's
        # own "Tulsi + Ashwagandha" fixture (agents/copilot.py, used
        # because GEMINI_API_KEY is unset in this environment) -- it
        # proposes Tulsi, which then fails real canonical resolution at
        # /prescribe/preview, exactly the rejection a genuinely unknown
        # herb (AI-proposed or hand-typed) would hit in production.
        preview2 = client.post(
            "/prescribe/preview",
            headers=doctor_a["headers"],
            json={
                "patient_name": DEMO_NAME_MARKER + "Sunitha Joseph",
                "patient_id": "DEMO-PT-012",
                "notes": "Patient reports Vata imbalance. Recommending Tulsi and Ashwagandha for daily use.",
            },
        )
        results["DEMO-005_unknown_herb"] = {
            "preview_status": preview2.status_code,
            "expected": "preview -> 400 (UnknownHerbError, herb='Tulsi')",
            "passed": preview2.status_code == 400,
            "detail": preview2.json().get("detail") if preview2.headers.get("content-type", "").startswith("application/json") else None,
        }

        # DEMO-008: cross-clinic isolation -- demo_doctor (default_clinic)
        # must not be able to confirm demo_doctor_b's (test_clinic_b)
        # pending preview. Per backend/rbac.py's documented posture, a
        # foreign-clinic preview_id is indistinguishable from one that was
        # never issued -- expect 404, not 403 and not success. The preview
        # created here for the check is left PENDING and will expire on
        # its own in 30 minutes (backend/preview_store.py) -- it is never
        # confirmed by anyone, so it never becomes persisted history and
        # needs no cleanup.
        clinic_b_preview = client.post(
            "/prescribe/manual-preview",
            headers=doctor_b["headers"],
            json={
                "patient_name": DEMO_NAME_MARKER + "Cross-Clinic Probe",
                "dosha_imbalance": "Vata",
                "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}],
            },
        )
        cross_confirm = client.post(
            "/prescribe/confirm",
            headers=doctor_a["headers"],  # demo_doctor, wrong clinic on purpose
            json={"preview_id": clinic_b_preview.json()["preview_id"]},
        )
        results["DEMO-008_cross_clinic_isolation"] = {
            "confirm_status": cross_confirm.status_code,
            "expected": "404 (foreign-clinic preview treated as not found)",
            "passed": cross_confirm.status_code == 404,
        }

        client.post("/auth/logout", headers=doctor_a["headers"])
        client.post("/auth/logout", headers=doctor_b["headers"])

    return results


# ---------------------------------------------------------------------------
# 5. Reset -- see module docstring for exactly what is and isn't removable.
# ---------------------------------------------------------------------------

def reset(manifest: dict) -> dict:
    """Deliberately does NOT rely solely on the manifest to find what to
    remove -- it re-derives demo user ids and demo rows directly from the
    database by the SAME markers used to create them (the 4 fixed
    usernames, the "(DEMO) " patient_name prefix, the "DEMO-" batch_code
    prefix), so --reset is correct even if the manifest file is missing,
    stale, or was hand-edited. The manifest is still updated afterward so
    a later normal run's idempotency checks stay accurate."""
    conn = get_connection()
    report = {"deleted": {}, "kept": {}}
    try:
        usernames = [u["username"] for u in DEMO_USERS]
        user_id_map = {}
        for username in usernames:
            row = conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()
            if row is not None:
                user_id_map[username] = row["user_id"]
        user_ids = list(user_id_map.values())

        if user_ids:
            qmarks = ",".join("?" for _ in user_ids)

            cur = conn.execute(f"DELETE FROM sessions WHERE user_id IN ({qmarks})", user_ids)
            conn.commit()
            report["deleted"]["sessions"] = cur.rowcount

            cur = conn.execute(f"DELETE FROM login_throttle WHERE user_id IN ({qmarks})", user_ids)
            conn.commit()
            report["deleted"]["login_throttle_rows"] = cur.rowcount
        else:
            report["deleted"]["sessions"] = 0
            report["deleted"]["login_throttle_rows"] = 0

        # Only ever-unconfirmed previews of ours are safe to remove --
        # a CONFIRMED one is FK-referenced by audit_events.preview_id.
        cur = conn.execute(
            "DELETE FROM prescription_previews WHERE patient_name LIKE ? AND status != 'CONFIRMED'",
            (DEMO_NAME_MARKER + "%",),
        )
        conn.commit()
        report["deleted"]["unconfirmed_demo_previews"] = cur.rowcount

        # test_clinic_b demo inventory rows -- found directly by the
        # "DEMO-" batch_code marker, not by trusting the manifest's row
        # ids alone (defense in depth; also makes this correct even if
        # the manifest was lost).
        cur = conn.execute(
            "DELETE FROM inventory WHERE clinic_id = ? AND batch_code LIKE 'DEMO-%'",
            (DEMO_CLINIC_B,),
        )
        conn.commit()
        report["deleted"]["clinic_b_inventory_rows"] = cur.rowcount

        # Best-effort user deletion -- succeeds only for a demo user never
        # referenced by a confirmed prescription's audit trail (in
        # practice: demo_admin / demo_auditor, which only ever read audit
        # data). demo_doctor / demo_doctor_b, once real history exists,
        # fail this with a real FK IntegrityError -- reported, not hidden,
        # never forced through.
        kept_users = []
        deleted_users = []
        for username, user_id in user_id_map.items():
            try:
                conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
                conn.commit()
                deleted_users.append(username)
            except sqlite3.IntegrityError:
                conn.rollback()
                ref_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM audit_events WHERE actor_user_id = ?", (user_id,)
                ).fetchone()["n"]
                kept_users.append({"username": username, "reason": f"referenced by {ref_count} confirmed audit event(s); cannot be deleted without corrupting audit integrity"})
        report["deleted"]["users"] = deleted_users
        report["kept"]["users"] = kept_users

        # If every demo user was actually removed, the dataset is
        # genuinely back to a clean slate -- allow a future normal run to
        # recreate everything, including fresh history.
        if user_ids and set(usernames) == set(deleted_users):
            manifest["history_seeded"] = False
            manifest["history"] = []
            manifest["users"] = {}
            manifest["clinic_b_inventory_ids"] = []
        else:
            manifest["clinic_b_inventory_ids"] = [
                rid for rid in manifest.get("clinic_b_inventory_ids", [])
                if conn.execute("SELECT 1 FROM inventory WHERE id = ?", (rid,)).fetchone() is not None
            ]
    finally:
        conn.close()
    return report


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="Remove everything about the demo dataset that is safely removable (see module docstring).")
    args = parser.parse_args()

    manifest = _load_manifest()

    if args.reset:
        print("== Resetting demo dataset ==")
        report = reset(manifest)
        _save_manifest(manifest)
        print(json.dumps(report, indent=2))
        print(f"\nManifest updated: {MANIFEST_PATH}")
        return

    print("== Ensuring demo users ==")
    ensure_users(manifest)
    _save_manifest(manifest)

    print("\n== Ensuring demo inventory (test_clinic_b only) ==")
    ensure_clinic_b_inventory(manifest)
    _save_manifest(manifest)

    print("\n== Confirming default_clinic inventory is already usable (read-only) ==")
    report_default_clinic_inventory()

    print("\n== Seeding demo prescription/audit history (idempotent, once) ==")
    seed_history(manifest)
    _save_manifest(manifest)

    print("\n== Running live rejection-path verifications (DEMO-004 / DEMO-005 / DEMO-008) ==")
    verification_results = run_verifications()
    for name, result in verification_results.items():
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  [{status}] {name}: {result}")
    manifest["last_verification"] = {"at": _now_iso(), "results": verification_results}
    _save_manifest(manifest)

    any_failed = any(not r["passed"] for r in verification_results.values())
    print("\n== Done ==")
    print(f"Manifest: {MANIFEST_PATH}")
    if any_failed:
        print("WARNING: one or more live verifications did not produce the expected result -- see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
