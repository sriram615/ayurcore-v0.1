#!/usr/bin/env python3
"""
Run S7 unseen holdout through AI-2 executor and record pass/fail results.

Reads eval/benchmarks/s7/s7_unseen.json, runs each question through the
AI-2 executor on the demo database, and compares against gold answers.
Writes results to eval/reports/s7/s7_results.json.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.executor import Query, execute
from ai2.snapshot import PatientNotFound, SnapshotUnavailable, snapshot_from_records
from backend.patient_store import SnapshotReadError, get_patient_for_clinic, get_prescription_history, read_snapshot
from backend.encounter_store import list_encounters_for_patient
from scripts.s7_generator import query_from_plan

S7_FILE = ROOT / "eval" / "benchmarks" / "s7" / "s7_unseen.json"
RESULTS_FILE = ROOT / "eval" / "reports" / "s7" / "s7_results.json"


def build_snapshot(db_path: str, clinic_id: str, patient_pk: int):
    """Build a read-only snapshot from the demo database."""
    import db.database as db_database
    original_db_path = db_database.DB_PATH
    db_database.DB_PATH = db_path
    try:
        with read_snapshot() as conn:
            patient = get_patient_for_clinic(patient_pk, clinic_id, conn=conn)
            if patient is None:
                raise PatientNotFound(f"patient {patient_pk} not found")
            rx_records, enc_records, offset = [], [], 0
            while True:
                page, more = get_prescription_history(clinic_id, patient["patient_pk"], 50, offset, conn=conn)
                rx_records += page
                offset += 50
                if not more:
                    break
            offset = 0
            while True:
                page, more = list_encounters_for_patient(clinic_id, patient["patient_pk"], 50, offset, conn=conn)
                enc_records += page
                offset += 50
                if not more:
                    break
    except SnapshotReadError as exc:
        raise SnapshotUnavailable(str(exc)) from exc
    finally:
        db_database.DB_PATH = original_db_path
    return snapshot_from_records(clinic_id, patient["patient_pk"], rx_records, enc_records)


def run_question(snapshot, question_data: dict) -> dict:
    """Run a single question through the executor and compare with gold."""
    gold_plan = question_data["gold_plan"]
    gold_answer = question_data["gold_answer"]

    if gold_plan["kind"] == "abstain":
        return {
            "question_id": question_data["id"],
            "question": question_data["question"],
            "patient": question_data["patient"],
            "frame": question_data["frame"],
            "language": question_data["language"],
            "gold_plan": gold_plan,
            "gold_answer": gold_answer,
            "actual_status": "abstain",
            "actual_value": None,
            "actual_sources": [],
            "status_match": gold_answer.get("status") == "refused",
            "value_match": True,
            "passed": gold_answer.get("status") == "refused",
        }

    q = gold_plan["query"]
    try:
        query = query_from_plan(q)
        result = execute(snapshot, query)
        actual_status = result.status
        actual_value = result.value
        actual_sources = [e.label for e in result.evidence]
    except Exception as e:
        # A plan that cannot execute is a benchmark defect, never a pass.
        actual_status = "error"
        actual_value = str(e)
        actual_sources = []

    expected_status = gold_answer.get("status")
    status_match = actual_status == expected_status
    value_match = actual_value == gold_answer.get("value")
    invalid_gold = expected_status == "error"

    return {
        "question_id": question_data["id"],
        "question": question_data["question"],
        "patient": question_data["patient"],
        "frame": question_data["frame"],
        "language": question_data["language"],
        "gold_plan": gold_plan,
        "gold_answer": gold_answer,
        "actual_status": actual_status,
        "actual_value": actual_value,
        "actual_sources": actual_sources,
        "status_match": status_match,
        "value_match": value_match,
        "invalid_gold": invalid_gold,
        "passed": (not invalid_gold) and status_match and value_match,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run S7 unseen holdout through AI-2 executor")
    parser.add_argument("--db", default="db/ayurcore.db", help="Path to the demo database")
    args = parser.parse_args()
    db_path = args.db

    with open(S7_FILE) as f:
        data = json.load(f)

    questions = data["questions"]
    results = []
    passed = 0
    failed = 0
    errors = 0

    for q in questions:
        clinic_id = q["patient"].split("/")[0]
        clinic_patient_id = q["patient"].split("/")[1]

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT patient_pk FROM patients WHERE clinic_id = ? AND clinic_patient_id = ?",
            (clinic_id, clinic_patient_id),
        ).fetchone()
        conn.close()

        if row is None:
            errors += 1
            results.append({
                "question_id": q["id"],
                "question": q["question"],
                "patient": q["patient"],
                "frame": q["frame"],
                "language": q["language"],
                "error": f"Patient {clinic_patient_id} not found in clinic {clinic_id}",
                "passed": False,
                "skipped": True,
            })
            continue

        patient_pk = row["patient_pk"]

        try:
            snapshot = build_snapshot(db_path, clinic_id, patient_pk)
            result = run_question(snapshot, q)
            results.append(result)
            if result["passed"]:
                passed += 1
            else:
                failed += 1
        except (PatientNotFound, SnapshotUnavailable) as e:
            errors += 1
            results.append({
                "question_id": q["id"],
                "question": q["question"],
                "patient": q["patient"],
                "frame": q["frame"],
                "language": q["language"],
                "error": str(e),
                "passed": False,
                "skipped": True,
            })
        except Exception as e:
            errors += 1
            results.append({
                "question_id": q["id"],
                "question": q["question"],
                "patient": q["patient"],
                "frame": q["frame"],
                "language": q["language"],
                "error": str(e),
                "passed": False,
                "skipped": True,
            })

    total = len(results)
    invalid_gold = [r for r in results if r.get("invalid_gold")]
    by_frame: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_frame.setdefault(r["frame"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if r.get("passed") else 0

    report = {
        "dataset": "s7_v1",
        "total_questions": total,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "pass_rate": passed / total if total > 0 else 0.0,
        "invalid_gold_answers": len(invalid_gold),
        "dataset_valid": not invalid_gold,
        "by_frame": by_frame,
        "results": results,
    }

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(report, f, indent=2)

    print(f"S7 evaluation complete:")
    print(f"  Total questions: {total}")
    print(f"  Passed: {passed}")
    print(f"  Failed: {failed}")
    print(f"  Errors: {errors}")
    print(f"  Pass rate: {passed / total:.1%}" if total > 0 else "N/A")
    if invalid_gold:
        print(f"  INVALID GOLD: {len(invalid_gold)} questions have status='error' -- "
              "the dataset is not measuring anything for those; fix the gold first.")
    for frame in sorted(by_frame):
        b = by_frame[frame]
        print(f"  {frame:<16} {b['passed']:>3}/{b['total']:<3}")
    print(f"  Results written to {RESULTS_FILE}")


if __name__ == "__main__":
    main()
