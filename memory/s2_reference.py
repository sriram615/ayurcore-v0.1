"""
Deterministic reference snapshots of the S2 specification (no database).

reference_snapshots(spec) -> {patient key: PatientSnapshot}

Used to compute S3 gold answers by deterministic execution. The records carry
synthetic, stable identifiers and timestamps derived from visit order only:
    prescription ids    1, 2, 3, ... across the dataset, in spec order
    encounter refs      s2e1, s2e2, ...
    visit i of a patient: encounter opened at day i 09:00, prescription confirmed at
                        09:10, encounter closed at 09:20 (2026-01-01 = day 0)
These are reference values for evaluation, not the identifiers a materialised
database would assign; the clinical content (order, dosha, herbs, unknowns) is the
same as memory/s2_build.py produces (checked by tests).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from memory.snapshot import PatientSnapshot, snapshot_from_records

BASE = datetime(2026, 1, 1, 9, 0, 0)


def _at(day: int, minutes: int) -> str:
    return (BASE + timedelta(days=day, minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def reference_snapshots(spec: dict) -> dict[str, PatientSnapshot]:
    out, rx_id, enc_pk = {}, 0, 0
    for clinic in spec["clinics"]:
        for patient in clinic["patients"]:
            rx_records, enc_records = [], []
            for day, visit in enumerate(patient["visits"]):
                kind = visit["kind"]
                if kind in ("rx", "encounter_only"):
                    enc_pk += 1
                    enc_records.append({"encounter_pk": enc_pk, "encounter_ref": f"s2e{enc_pk}", "status": "CLOSED",
                                        "created_at": _at(day, 0), "closed_at": _at(day, 20)})
                if kind == "encounter_only":
                    continue
                rx_id += 1
                known = kind != "legacy_unknown"
                rx_records.append({
                    "prescription_id": rx_id, "created_at": _at(day, 10), "status": "Active",
                    "dosha_imbalance": visit["dosha"] if known else None,
                    "prescribed_herbs": [{"name": n, "grams": g} for n, g in visit["herbs"]] if known else None})
            out[patient["key"]] = snapshot_from_records(clinic["clinic_id"], len(out) + 1, rx_records, enc_records)
    return out
