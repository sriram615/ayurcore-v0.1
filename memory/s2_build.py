"""
Materialise the S2 specification into an ISOLATED database through the real API.

    build(spec, db_path) -> mapping

For every clinic: one DOCTOR is created with backend.auth_service.create_user (random
password, never printed) and synthetic stock is inserted. Then, through FastAPI's
TestClient on the real app (routing, authentication, RBAC, validation, handlers):
    POST /patients                      register each patient in the doctor's clinic
    POST /patients/{pk}/encounters      open an encounter per visit
    POST /prescribe/manual-preview      the visit's dosha + herbs (no AI call)
    POST /prescribe/confirm             confirm (writes prescription, items, audit, stock)
    POST /encounters/{pk}/close         close the encounter
Legacy visits are inserted directly as prescription rows without structured values,
exactly as pre-structured data would exist (no encounter, no items, no dosha column).

Safety: the target must be a new file that is not named ayurcore.db; db.database.DB_PATH
is pointed at it only for the duration of the build and restored afterwards.
SYNTHETIC DATA ONLY.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import uuid

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

STOCK_GRAMS = 1_000_000
LEGACY_UNKNOWN_DESCRIPTION = "LEGACY-UNSTRUCTURED (synthetic S2 record)"


class UnsafeBuildTarget(ValueError):
    """The requested database path may not be used."""


def _legacy_readable_description(patient_name: str, dosha: str, herbs: list) -> str:
    # The exact legacy shape that backend/patient_store's compatibility reader understands.
    return f"Patient: {patient_name} | Dosha: {dosha} | Formulation: " + \
        json.dumps([{"name": n, "grams": g} for n, g in herbs])


def build(spec: dict, db_path: str) -> dict:
    target = os.path.realpath(db_path)
    if os.path.basename(target) == "ayurcore.db" or os.path.exists(target):
        raise UnsafeBuildTarget(f"refusing {target}: must be a new file not named ayurcore.db")
    import db.database as database
    saved = database.DB_PATH
    database.DB_PATH = target
    try:
        return _build(spec)
    finally:
        database.DB_PATH = saved


def _build(spec: dict) -> dict:
    import db.database as database
    from backend.audit_store import audit_chain_validity
    from backend.auth_service import create_user
    from backend.herb_resolver import HERB_ALIASES
    from db.migrations import run_migrations
    from fastapi.testclient import TestClient
    import backend.main as main_module

    run_migrations(integrity_guard=audit_chain_validity)
    doctors = {}
    for clinic in spec["clinics"]:  # users first: create_user commits on its own connection
        cid = clinic["clinic_id"]
        password = secrets.token_urlsafe(18)
        username = f"{cid}_doctor"
        create_user(username, password, cid, role="DOCTOR")
        doctors[cid] = {"username": username, "password": password}
    conn = database.get_connection()
    try:
        for clinic in spec["clinics"]:
            cid = clinic["clinic_id"]
            conn.executemany(
                "INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(cid, h, "synthetic", STOCK_GRAMS, f"S2-{h[:3].upper()}", 90) for h in sorted(set(HERB_ALIASES.values()))])
        conn.commit()
    finally:
        conn.close()

    mapping = {"clinics": {}}
    with TestClient(main_module.app) as client:
        for clinic in spec["clinics"]:
            cid = clinic["clinic_id"]
            resp = client.post("/auth/login", json=doctors[cid])
            _ok(resp, 200, "login")
            headers = {"Authorization": f"Bearer {resp.json()['token']}"}
            patients = {}
            for patient in clinic["patients"]:
                resp = client.post("/patients", headers=headers, json={
                    "clinic_patient_id": patient["clinic_patient_id"], "display_name": patient["display_name"]})
                _ok(resp, 201, "register patient")
                pk = resp.json()["patient_pk"]
                record = {"patient_pk": pk, "prescription_ids": [], "encounter_pks": []}
                for visit in patient["visits"]:
                    kind = visit["kind"]
                    if kind in ("legacy_unknown", "legacy_readable"):
                        record["prescription_ids"].append(_legacy(cid, pk, patient["display_name"], visit))
                        continue
                    resp = client.post(f"/patients/{pk}/encounters", headers=headers)
                    _ok(resp, 201, "open encounter")
                    enc = resp.json()["encounter_pk"]
                    record["encounter_pks"].append(enc)
                    if kind == "rx":
                        resp = client.post("/prescribe/manual-preview", headers=headers, json={
                            "patient_name": patient["display_name"], "patient_pk": pk, "encounter_pk": enc,
                            "dosha_imbalance": visit["dosha"],
                            "prescribed_herbs": [{"name": n, "grams": g} for n, g in visit["herbs"]]})
                        _ok(resp, 200, "manual preview")
                        resp = client.post("/prescribe/confirm", json={"preview_id": resp.json()["preview_id"]},
                                           headers={**headers, "Idempotency-Key": str(uuid.uuid4())})
                        _ok(resp, 200, "confirm")
                        record["prescription_ids"].append(resp.json()["prescription_id"])
                    resp = client.post(f"/encounters/{enc}/close", headers=headers)
                    _ok(resp, 200, "close encounter")
                patients[patient["key"]] = record
            mapping["clinics"][cid] = {"patients": patients}
    return mapping


def _legacy(clinic_id: str, patient_pk: int, patient_name: str, visit: dict) -> int:
    import db.database as database
    description = LEGACY_UNKNOWN_DESCRIPTION if visit["kind"] == "legacy_unknown" else \
        _legacy_readable_description(patient_name, visit["dosha"], visit["herbs"])
    conn = database.get_connection()
    try:
        cur = conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status, patient_pk) "
                           "VALUES (?, ?, ?, ?, ?)", (clinic_id, patient_name, description, "Active", patient_pk))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _ok(resp, status: int, step: str) -> None:
    if resp.status_code != status:
        raise RuntimeError(f"S2 build failed at {step}: HTTP {resp.status_code} {resp.text[:300]}")
