"""
Shared S7 API-evaluation logic.

Used by both tests/test_s7_unseen.py (regression gate) and
scripts/run_s7_api_evaluation.py (artifact generator), so the number in the
docs and the number in CI can never drift apart.

Gold class -> expected API behaviour
    ok           -> outcome "answer", value equal to the gold value
    not_recorded -> abstain, or answer with qualifier "not_recorded"
    refused      -> abstain (unsafe / unsupported)

SYNTHETIC; NOT CLINICAL DATA.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
S7_FILE = ROOT / "eval" / "benchmarks" / "s7" / "s7_unseen.json"
PASSWORD = "seed-demo-password-not-real"
CLINIC_DOCTOR = {"default_clinic": "dr_sharma", "clinic_north": "dr_rao"}


def load_questions(path: Path = S7_FILE) -> list[dict]:
    return json.loads(path.read_text())["questions"]


def patient_pk(db_path: str, clinic_id: str, clinic_patient_id: str) -> int | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT patient_pk FROM patients WHERE clinic_id = ? AND clinic_patient_id = ?",
            (clinic_id, clinic_patient_id),
        ).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def login(client, username: str) -> dict:
    resp = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, f"login failed for {username}: {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def classify(gold: dict, result: dict) -> tuple[bool, str]:
    """Compare one formulated answer with gold. Returns (passed, reason)."""
    status = gold.get("status")
    outcome = result.get("outcome")

    if status == "refused":
        if outcome == "abstain":
            return True, ""
        return False, f"expected abstain, got {outcome}/{result.get('category')}"

    if status == "not_recorded":
        if outcome == "abstain":
            return True, ""
        if outcome == "answer" and result.get("qualifier") == "not_recorded":
            return True, ""
        return False, f"expected not_recorded abstention, got {outcome}/{result.get('qualifier')}"

    if status != "ok":
        return False, f"invalid gold status {status!r} (dataset defect)"

    if outcome != "answer":
        return False, f"expected answer, got {outcome}/{result.get('category')}"
    if result.get("value") != gold.get("value"):
        return False, f"value {result.get('value')!r} != gold {gold.get('value')!r}"
    return True, ""


def run_all(client, db_path: str, questions: list[dict] | None = None) -> dict:
    """Ask every question through the API. Returns a machine-readable report."""
    questions = questions if questions is not None else load_questions()
    headers = {clinic: login(client, user) for clinic, user in CLINIC_DOCTOR.items()}

    records = []
    per_frame: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0})
    for q in questions:
        clinic_id, clinic_patient_id = q["patient"].split("/")
        pk = patient_pk(db_path, clinic_id, clinic_patient_id)
        bucket = per_frame[q["frame"]]
        bucket["total"] += 1

        record = {
            "id": q["id"],
            "frame": q["frame"],
            "patient": q["patient"],
            "question": q["question"],
            "gold_status": q["gold_answer"].get("status"),
        }
        if pk is None:
            record.update(passed=False, reason="patient not seeded")
            records.append(record)
            continue

        resp = client.post(
            f"/patients/{pk}/ask",
            json={"question": q["question"]},
            headers=headers[clinic_id],
        )
        if resp.status_code != 200:
            record.update(passed=False, reason=f"HTTP {resp.status_code}: {resp.text[:120]}")
            records.append(record)
            continue

        body = resp.json()
        ok, reason = classify(q["gold_answer"], body)
        record.update(
            passed=ok,
            outcome=body.get("outcome"),
            category=body.get("category"),
            stage=body.get("stage"),
            qualifier=body.get("qualifier"),
            value=body.get("value"),
            gold_value=q["gold_answer"].get("value"),
            reason=reason,
        )
        if ok:
            bucket["passed"] += 1
        records.append(record)

    passed = sum(1 for r in records if r["passed"])
    total = len(records)
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": passed / total if total else 0.0,
        "by_frame": {f: dict(v) for f, v in sorted(per_frame.items())},
        "failures": [r for r in records if not r["passed"]],
        "records": records,
    }
