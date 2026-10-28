#!/usr/bin/env python3
"""
Actor-aware evaluator for gate A6 (who may ask), run against the route that ships: POST /patients/{pk}/ask.

    ./venv/bin/python scripts/eval_actor_aware.py --policy v1_2 --out /tmp/actor_aware_report.json

Spec: eval/protocols/longitudinal_v1/actor_aware_evaluator_spec_v1.json. This script is QUALIFICATION TOOLING: it ships with synthetic
stand-in questions and never reads any benchmark file (a path containing "accept" is refused). It does not run the acceptance cases.

Environment: a throwaway database in a new temporary directory (never db/ayurcore.db), four users with random passwords that are never
printed or stored, tokens from POST /auth/login on the in-process TestClient, no network, no language model.

--policy chooses what the ADMIN-clinic-A control must do:
    v1_1  protocol v1.1 as registered: ADMIN asking about an own-clinic patient must NOT be denied
    v1_2  proposed v1.2 (documentation/docs/PROPOSED_PROTOCOL_v1_2_A6_ADMIN_CONTROL.md): ADMIN must be forbidden (/ask is DOCTOR-only)
Against the shipping route, v1_1 is expected to FAIL (ADMIN gets 403) until the owner decides D-02; the failure is reported, not hidden.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import tempfile
from unittest import mock

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

STATUS_OUTCOME = {200: "allowed", 401: "unauthenticated", 403: "forbidden", 404: "not_found"}
# Synthetic stand-in questions (NOT the registered wordings). Denial must not depend on the wording.
STAND_IN_QUESTIONS = ("What was the last prescription?", "When was Neem last prescribed?", "Avasanam enthu koduthu?", "Show me the history.")

# (case id, actor, patient owner, expected outcome) for the denials; controls are expected "allowed" unless the policy says otherwise.
DENIALS = (("no_token", "no_token", "A", "unauthenticated"), ("auditor", "auditor_a", "A", "forbidden"), ("doctor_a_asks_clinic_b_patient", "doctor_a", "B", "not_found"))


def controls(policy: str) -> tuple:
    if policy not in ("v1_1", "v1_2"):
        raise ValueError("policy must be v1_1 or v1_2")
    return (("doctor_a_own_patient", "doctor_a", "A", "allowed"),
            ("admin_a_own_patient", "admin_a", "A", "allowed" if policy == "v1_1" else "forbidden"))


def evaluate(call, questions, policy: str) -> dict:
    """`call(actor, owner, question)` -> {"status", "body", "snapshot_called"}; `call(actor, "MISSING", question)` asks for a non-existent patient.
    Pure scoring: the fakes in the tests exercise exactly this function."""
    rows, failures = [], []
    for kind, group in (("denial", DENIALS), ("control", controls(policy))):
        for case_id, actor, owner, expected in group:
            outcomes = set()
            for q in questions:
                r = call(actor, owner, q)
                got = STATUS_OUTCOME.get(r["status"], f"other_{r['status']}")
                outcomes.add(got)
                ok = got == expected
                if expected in ("unauthenticated", "forbidden") and r["snapshot_called"]:
                    ok = False
                    failures.append((case_id, "snapshot was read for a denied request"))
                if not ok:
                    failures.append((case_id, f"expected {expected}, got {got}"))
                rows.append({"case": case_id, "kind": kind, "actor": actor, "question_index": list(questions).index(q), "expected": expected, "outcome": got,
                             "snapshot_called": r["snapshot_called"], "pass": ok})
            if len(outcomes) > 1:
                failures.append((case_id, "outcome depends on the wording"))
    # A cross-clinic patient must look exactly like a missing one.
    leaks = []
    for q in questions:
        a, b = call("doctor_a", "B", q), call("doctor_a", "MISSING", q)
        if (a["status"], a["body"]) != (b["status"], b["body"]):
            leaks.append(q)
    if leaks:
        failures.append(("doctor_a_asks_clinic_b_patient", "response differs from a non-existent patient"))
    verdict = not failures and bool(rows)
    return {"policy": policy, "n_rows": len(rows), "failures": sorted(set(failures)), "cross_clinic_leak_wordings": len(leaks), "A6_pass": verdict, "rows": rows}


def run(policy: str, questions=STAND_IN_QUESTIONS) -> dict:
    import db.database as database

    tmp = tempfile.mkdtemp(prefix="ayurcore_actor_aware_")
    db_path = os.path.join(tmp, "eval.db")
    if os.path.realpath(db_path) == os.path.realpath(os.path.join(ROOT, "db", "ayurcore.db")):
        raise RuntimeError("refusing to use the application database")
    saved = database.DB_PATH
    database.DB_PATH = db_path
    try:
        database.init_db()
        from fastapi.testclient import TestClient

        import backend.main as main_module
        from ai2.adapters.clinic_stores import ClinicStoreRecords
        from backend.auth_service import create_user
        from backend.patient_store import create_patient

        tokens, user_ids = {}, {}
        specs = (("doctor_a", "A", "DOCTOR"), ("doctor_b", "B", "DOCTOR"), ("admin_a", "A", "ADMIN"), ("auditor_a", "A", "AUDITOR"))
        client = TestClient(main_module.app)
        for name, clinic, role in specs:
            username, password = f"aa_{name}_{secrets.token_hex(4)}", secrets.token_urlsafe(24)
            create_user(username, password, clinic_id=f"CLINIC_{clinic}", role=role)
            resp = client.post("/auth/login", json={"username": username, "password": password})
            assert resp.status_code == 200
            tokens[name] = resp.json()["token"]
            conn = database.get_connection()
            user_ids[name] = conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()[0]
            conn.close()
        pk = {}
        for owner, creator in (("A", "doctor_a"), ("B", "doctor_b")):
            p = create_patient(clinic_id=f"CLINIC_{owner}", created_by_user_id=user_ids[creator], clinic_patient_id=f"AA-{owner}-1", display_name=f"Stand-in {owner}", phone=None)
            pk[owner] = p["patient_pk"]
            conn = database.get_connection()
            conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status, created_at, patient_pk, dosha_imbalance) VALUES (?,?,?,?,?,?,?)",
                         (f"CLINIC_{owner}", f"Stand-in {owner}", "stand-in", "CONFIRMED", "2026-01-05 09:00:00", pk[owner], "Vata"))
            conn.commit()
            conn.close()
        pk["MISSING"] = max(pk.values()) + 1000

        calls = {"n": 0}
        real_load = ClinicStoreRecords.load_snapshot

        def counting(self, *a, **k):
            calls["n"] += 1
            return real_load(self, *a, **k)

        def call(actor, owner, question):
            headers = {} if actor == "no_token" else {"Authorization": f"Bearer {tokens[actor]}"}
            before = calls["n"]
            with mock.patch.object(ClinicStoreRecords, "load_snapshot", counting):
                resp = client.post(f"/patients/{pk[owner]}/ask", json={"question": question}, headers=headers)
            try:
                body = resp.json() if resp.status_code != 200 else "ok"
            except ValueError:
                body = resp.text
            return {"status": resp.status_code, "body": body, "snapshot_called": calls["n"] > before}

        report = evaluate(call, questions, policy)
        report["credentials_in_report"] = any(t in json.dumps(report) for t in tokens.values())
    finally:
        database.DB_PATH = saved
    cfg = {"script": hashlib.sha256(open(__file__, "rb").read()).hexdigest(), "policy": policy, "questions": list(questions)}
    report["configuration_fingerprint"] = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    report["scope"] = "SYNTHETIC STAND-IN CASES; not the registered accept cases; qualification only"
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--policy", choices=("v1_1", "v1_2"), required=True)
    ap.add_argument("--out", help="write the JSON report here")
    a = ap.parse_args(argv)
    if a.out and "accept" in a.out.lower():
        print("refused: output path names the acceptance set")
        return 2
    report = run(a.policy)
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(report, fh, indent=1, sort_keys=True)
    print(f"policy {report['policy']}: A6 {'PASS' if report['A6_pass'] else 'FAIL'} ({report['n_rows']} rows, {len(report['failures'])} failure kinds)")
    for case, why in report["failures"]:
        print(f"  {case}: {why}")
    return 0 if report["A6_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
