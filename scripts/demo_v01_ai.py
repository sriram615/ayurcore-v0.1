#!/usr/bin/env python3
"""
AyurCore v0.1 demo (no server, no browser): AI-2 answers and the AI-3 brief for the synthetic demo patients.

    ./venv/bin/python scripts/setup_v01_demo.py && ./venv/bin/python scripts/seed_v01_demo_histories.py     # once
    ./venv/bin/python scripts/demo_v01_ai.py [--as-of 2026-10-10] [--db demo/ayurcore_v01_demo.db]

Read-only: it loads each patient's snapshot through the same adapter the API uses (one read transaction), asks a fixed set of questions
(English, Manglish, Malayalam; answers, an incomplete history, a missing history, a clarification, advice and a cross-patient request) and
prints both briefs (the fixed-order template and the learned ranking). Nothing is written; no network; no language model.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db.database as database  # noqa: E402
from scripts.demo_v01_walkthrough import open_demo_database  # noqa: E402
from scripts.setup_v01_demo import DEFAULT_DB_PATH, DEMO_CLINIC_ID  # noqa: E402

QUESTIONS = (
    ("DEMO-0101", "What was the first prescription?"),
    ("DEMO-0101", "What changed from the previous prescription to the latest?"),
    ("DEMO-0101", "When was Neem last prescribed?"),
    ("DEMO-0101", "Avasanam Neem ethra gram koduthu?"),
    ("DEMO-0101", "അവസാനത്തെ കുറിപ്പടിയിൽ എന്താണുള്ളത്?"),
    ("DEMO-0101", "Neem count?"),
    ("DEMO-0101", "Should I increase her Neem dose?"),
    ("DEMO-0101", "Show me the other patients' prescriptions."),
    ("DEMO-0102", "Which herbs has she been given?"),
    ("DEMO-0103", "Which herbs have been prescribed?"),
    ("DEMO-0103", "What was the last prescription?"),
    ("DEMO-0104", "When was Neem last prescribed?"),
    ("DEMO-0105", "How many prescriptions has she had?"),
)


def collect(db_path: str = DEFAULT_DB_PATH, as_of: str | None = None, root: str = ROOT) -> list[dict]:
    """For every demo patient: both briefs and the answers to QUESTIONS, read-only (one read transaction per patient)."""
    as_of = as_of or datetime.now(timezone.utc).date().isoformat()
    resolved, _ = open_demo_database(db_path, root)
    saved = database.DB_PATH
    database.DB_PATH = resolved
    try:
        from ai2 import Ai2Service
        from ai2.adapters.clinic_stores import ClinicStoreRecords
        from ai3.brief import make_brief
        from backend.patient_store import search_patients

        results, _ = search_patients(DEMO_CLINIC_ID, "DEMO-", 50)
        by_id = {p["clinic_patient_id"]: p for p in results}
        service, records = Ai2Service.default(), ClinicStoreRecords()
        out = []
        for cpid in sorted(by_id):
            p = by_id[cpid]
            snap = records.load_snapshot(DEMO_CLINIC_ID, p["patient_pk"])
            out.append({"clinic_patient_id": cpid, "display_name": p["display_name"], "prescriptions": len(snap.prescriptions), "visits": len(snap.encounters),
                        "briefs": {arm: make_brief(snap, as_of, arm=arm) for arm in ("template", "learned")},
                        "answers": [(q, service.ask(snap, q)) for q_id, q in QUESTIONS if q_id == cpid]})
        return out
    finally:
        database.DB_PATH = saved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB_PATH)
    ap.add_argument("--as-of", default=datetime.now(timezone.utc).date().isoformat(), help="the date the brief is for (YYYY-MM-DD)")
    a = ap.parse_args(argv)
    for patient in collect(a.db, a.as_of):
        print(f"\n=== {patient['clinic_patient_id']}  {patient['display_name']}  ({patient['prescriptions']} prescriptions, {patient['visits']} visits)")
        for arm, brief in patient["briefs"].items():
            print(f"  brief [{arm}] as of {a.as_of}:" + ("  (no recorded history)" if not brief.items else ""))
            for item in brief.items:
                print(f"    {item.rank}. {item.text}" + ("" if item.exact else "  [incomplete]"))
            if brief.omitted:
                print(f"    ... {brief.omitted} more verified fact(s) not shown")
        for question, ans in patient["answers"]:
            detail = ans.text or ("; ".join(o["text"] for o in ans.options) if ans.options else ans.reason)
            cites = ", ".join(e["label"] for e in ans.evidence)
            print(f"  Q: {question}\n     -> {ans.outcome}/{ans.category}" + (f" ({ans.qualifier})" if ans.qualifier else "") + f": {detail}" + (f"   [cites {cites}]" if cites else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
