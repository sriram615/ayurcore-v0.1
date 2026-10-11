#!/usr/bin/env python3
"""
AyurCore v0.1 demo: add SYNTHETIC patient histories to the disposable demo database, for the AI-2 questions and the AI-3 brief.

    ./venv/bin/python scripts/setup_v01_demo.py                    # once: creates demo/ayurcore_v01_demo.db and its logins
    ./venv/bin/python scripts/seed_v01_demo_histories.py           # adds the five demo patients (refuses to run twice)
    ./venv/bin/python scripts/demo_v01_ai.py                       # asks questions and prints briefs, no server needed

The database must be one made by scripts/setup_v01_demo.py: the same path checks apply (inside this checkout, never db/ayurcore.db, never
outside it), and the demo credentials file must sit next to it. Nothing here touches the application's database.

The histories are written straight into the demo database with dated rows (the real confirm path cannot back-date a prescription, and a
prescription whose herbs or dosha are unknown only exists as a legacy-style row). They are invented; nobody is real:

    DEMO-0101  complete history   4 dated prescriptions; the latest changes the dosha (Pitta to Kapha) and removes a herb; 3 closed visits and 1 open visit
    DEMO-0102  mixed history      3 prescriptions, the middle one legacy-style (herbs and dosha not recorded)
    DEMO-0103  legacy only        3 prescriptions, none with recorded herbs or dosha
    DEMO-0104  new patient        no prescriptions, no visits
    DEMO-0105  stable regimen     5 prescriptions with the same herbs and dosha, last visit closed
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db.database as database  # noqa: E402
from scripts.demo_v01_walkthrough import open_demo_database  # noqa: E402
from scripts.setup_v01_demo import DEFAULT_DB_PATH, DEMO_CLINIC_ID  # noqa: E402

EXTRA_STOCK = (("Neem", "Azadirachta indica", 3000, "DEMO-NEE-01", 90), ("Guggulu", "Commiphora wightii", 3000, "DEMO-GUG-01", 90),
               ("Haridra", "Curcuma longa", 3000, "DEMO-HAR-01", 92))

# (clinic_patient_id, display_name, prescriptions [(date, dosha|None, herbs|None)], visits [(opened, closed|None)])
HISTORIES = (
    ("DEMO-0101", "Meera Menon (synthetic)",
     [("2026-03-02", "Vata", [("Ashwagandha", 30), ("Neem", 10)]), ("2026-05-04", "Vata", [("Ashwagandha", 30), ("Neem", 20)]),
      ("2026-07-06", "Pitta", [("Brahmi", 15), ("Neem", 20), ("Guggulu", 12)]), ("2026-09-07", "Kapha", [("Brahmi", 15), ("Guggulu", 12)])],
     [("2026-03-02", "2026-03-02"), ("2026-05-04", "2026-05-04"), ("2026-07-06", "2026-07-06"), ("2026-09-28", None)]),
    ("DEMO-0102", "Ravi Pillai (synthetic)",
     [("2026-04-10", "Kapha", [("Triphala", 20), ("Haridra", 10)]), ("2026-06-12", None, None), ("2026-08-14", "Kapha", [("Triphala", 25), ("Haridra", 10)])],
     [("2026-04-10", "2026-04-10"), ("2026-08-14", "2026-08-14")]),
    ("DEMO-0103", "Lakshmi Iyer (synthetic)",
     [("2026-02-03", None, None), ("2026-03-05", None, None), ("2026-04-07", None, None)], [("2026-04-07", "2026-04-07")]),
    ("DEMO-0104", "Arun Das (synthetic)", [], []),
    ("DEMO-0105", "Sita Varma (synthetic)",
     [(d, "Vata", [("Ashwagandha", 30), ("Shatavari", 20)]) for d in ("2026-04-01", "2026-05-01", "2026-06-01", "2026-07-01", "2026-08-03")],
     [("2026-08-03", "2026-08-03")]),
)


class AlreadySeeded(RuntimeError):
    pass


def seed(db_path: str = DEFAULT_DB_PATH, root: str = ROOT) -> dict:
    resolved, credentials = open_demo_database(db_path, root)
    doctor = next(u for u in credentials["users"] if u["role"] == "DOCTOR")
    saved = database.DB_PATH
    database.DB_PATH = resolved
    try:
        import secrets

        from backend.auth_service import create_user  # noqa: F401  (credentials already exist; only the DOCTOR's id is needed)
        from backend.patient_store import create_patient

        conn = database.get_connection()
        try:
            doctor_id = conn.execute("SELECT user_id FROM users WHERE username = ? AND clinic_id = ?", (doctor["username"], DEMO_CLINIC_ID)).fetchone()[0]
            if conn.execute("SELECT 1 FROM patients WHERE clinic_id = ? AND clinic_patient_id = 'DEMO-0101'", (DEMO_CLINIC_ID,)).fetchone():
                raise AlreadySeeded("the demo histories are already in this database")
            have = {r[0] for r in conn.execute("SELECT herb_name FROM inventory WHERE clinic_id = ?", (DEMO_CLINIC_ID,))}
            conn.executemany("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) VALUES (?, ?, ?, ?, ?, ?)",
                             [(DEMO_CLINIC_ID, *row) for row in EXTRA_STOCK if row[0] not in have])
            conn.commit()
        finally:
            conn.close()
        out = {}
        for cpid, name, prescriptions, visits in HISTORIES:
            patient = create_patient(clinic_id=DEMO_CLINIC_ID, created_by_user_id=doctor_id, clinic_patient_id=cpid, display_name=name, phone=None)
            pk = patient["patient_pk"]
            conn = database.get_connection()
            try:
                for day, dosha, herbs in prescriptions:
                    rid = conn.execute(
                        "INSERT INTO prescriptions (clinic_id, patient_name, description, status, created_at, patient_pk, dosha_imbalance) VALUES (?,?,?,?,?,?,?)",
                        (DEMO_CLINIC_ID, name, "demo history" if herbs is None else f"Patient: {name} | Dosha: {dosha}", "CONFIRMED", f"{day} 09:00:00", pk, dosha)).lastrowid
                    for line, (herb, grams) in enumerate(herbs or [], 1):
                        conn.execute("INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) VALUES (?,?,?,?,?)", (rid, DEMO_CLINIC_ID, line, herb, grams))
                for opened, closed in visits:
                    conn.execute("INSERT INTO encounters (clinic_id, patient_pk, created_by_user_id, status, created_at, closed_at, encounter_ref) VALUES (?,?,?,?,?,?,?)",
                                 (DEMO_CLINIC_ID, pk, doctor_id, "OPEN" if closed is None else "CLOSED", f"{opened} 09:00:00", None if closed is None else f"{closed} 10:00:00", secrets.token_hex(8)))
                conn.commit()
            finally:
                conn.close()
            out[cpid] = {"patient_pk": pk, "display_name": name, "prescriptions": len(prescriptions), "visits": len(visits)}
        return out
    finally:
        database.DB_PATH = saved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    a = ap.parse_args(argv)
    try:
        result = seed(a.db)
    except AlreadySeeded as exc:
        print("refused:", exc)
        return 2
    for cpid, info in result.items():
        print(f"{cpid}  patient_pk={info['patient_pk']:<3} {info['display_name']}: {info['prescriptions']} prescriptions, {info['visits']} visits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
