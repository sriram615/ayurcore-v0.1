#!/usr/bin/env python3
"""
AyurCore v0.1 backend walkthrough: the whole clinical journey through the real
HTTP API, against the disposable demo database made by scripts/setup_v01_demo.py.

    ./venv/bin/python scripts/setup_v01_demo.py          # once
    ./venv/bin/python scripts/demo_v01_walkthrough.py    # any number of times

Every step is an HTTP request to backend.main.app, sent in-process with
FastAPI's TestClient (routing, authentication, RBAC, validation and handlers
exactly as served; no server is started). Nothing is written to the database
except through those requests, apart from one throwaway user in a second
clinic (created with backend.auth_service.create_user, as setup does) for
the wrong-clinic checks.

Journey:
   1. log in as the demo doctor
   2. find (or register) the synthetic demo patient
   3. open an encounter
   4. manual prescription preview linked to the patient and encounter
   5-6. confirm with doctor corrections and an Idempotency-Key
   7. stock decreased by exactly the corrected amounts
   8. as the demo admin: the PRESCRIPTION_CONFIRMED audit event, chain valid
   9. the prescription is in the patient's history
  10. close the encounter
  11. replay the same confirmation (same key): same response, nothing duplicated
Negative cases: idempotency key reused with a different request; wrong-clinic
access; confirming against a closed encounter; insufficient stock. Each must
be rejected and leave stock, prescriptions and audit events unchanged.

The database must pass the same location rules as setup (inside this checkout,
not under db/ or .git, not named ayurcore.db, ends in .db), must exist, and
must have setup's credentials file next to it naming it. Exit code 0 when
every check passes, 1 when one fails, 2 when the database is refused.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import uuid

ROOT = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db.database as database  # noqa: E402
from scripts.setup_v01_demo import (  # noqa: E402
    CREDENTIALS_FILENAME, DEFAULT_DB_PATH, DEMO_PATIENT, UnsafeDemoPathError, check_demo_location,
)

OTHER_CLINIC_ID = "demo_clinic_other"


class WalkthroughFailure(AssertionError):
    """A walkthrough check did not hold."""


def open_demo_database(db_path: str, root: str = ROOT) -> tuple[str, dict]:
    """(resolved path, credentials) for an existing demo database, or
    UnsafeDemoPathError."""
    resolved = check_demo_location(db_path, root)
    if not os.path.isfile(resolved):
        raise UnsafeDemoPathError(f"{resolved} does not exist; run scripts/setup_v01_demo.py first.")
    credentials_path = os.path.join(os.path.dirname(resolved), CREDENTIALS_FILENAME)
    if not os.path.isfile(credentials_path):
        raise UnsafeDemoPathError(f"{credentials_path} is missing; this is not a demo database made by setup.")
    with open(credentials_path) as f:
        credentials = json.load(f)
    if credentials.get("database") != resolved:
        raise UnsafeDemoPathError(f"{credentials_path} belongs to a different database.")
    return resolved, credentials


def run_walkthrough(db_path: str = DEFAULT_DB_PATH, root: str = ROOT, log=print) -> dict:
    resolved, credentials = open_demo_database(db_path, root)
    saved_db_path = database.DB_PATH
    database.DB_PATH = resolved
    try:
        import backend.main as main_module
        from fastapi.testclient import TestClient

        with TestClient(main_module.app) as client:
            return _Walkthrough(client, credentials, log).run()
    finally:
        database.DB_PATH = saved_db_path


def _herbs(items) -> list[tuple[str, int]]:
    """Order-independent comparison form: merged (name, grams) pairs."""
    totals: dict[str, int] = {}
    for item in items:
        totals[item["name"]] = totals.get(item["name"], 0) + item["grams"]
    return sorted(totals.items())


class _Walkthrough:
    def __init__(self, client, credentials: dict, log):
        self.client = client
        self.log = log
        self.users = {u["role"]: u for u in credentials["users"]}
        self.checks = 0

    # --- helpers ---------------------------------------------------------------

    def expect(self, condition: bool, label: str, response=None):
        if not condition:
            detail = f" [HTTP {response.status_code}: {response.text[:300]}]" if response is not None else ""
            raise WalkthroughFailure(f"{label}{detail}")
        self.checks += 1
        self.log(f"      ok  {label}")

    @staticmethod
    def require(condition: bool, label: str, response=None):
        """Like expect, for helper-level preconditions: not logged or counted."""
        if not condition:
            detail = f" [HTTP {response.status_code}: {response.text[:300]}]" if response is not None else ""
            raise WalkthroughFailure(f"{label}{detail}")

    def step(self, title: str):
        self.log(f"\n{title}")

    def login(self, role: str) -> dict:
        user = self.users[role]
        resp = self.client.post("/auth/login", json={"username": user["username"], "password": user["password"]})
        self.expect(resp.status_code == 200 and resp.json()["role"] == role, f"{role} logged in", resp)
        return {"Authorization": f"Bearer {resp.json()['token']}"}

    def inventory(self, headers) -> dict:
        resp = self.client.get("/inventory", headers=headers)
        self.require(resp.status_code == 200, "GET /inventory", resp)
        return {i["herb_name"]: i["stock_grams"] for i in resp.json()["items"]}

    def history(self, headers, patient_pk) -> list:
        """The patient's COMPLETE history, newest first, read page by page
        (the endpoint returns at most 50 per page), so counts stay exact
        however many prescriptions the patient has."""
        records, offset = [], 0
        while True:
            resp = self.client.get(f"/patients/{patient_pk}/prescriptions",
                                   params={"limit": 50, "offset": offset}, headers=headers)
            self.require(resp.status_code == 200, "GET patient history", resp)
            page = resp.json()
            records += page["prescriptions"]
            if not page["has_more"]:
                return records
            self.require(bool(page["prescriptions"]), "history page with has_more is not empty", resp)
            offset += len(page["prescriptions"])

    def audit_events(self, headers) -> dict:
        resp = self.client.get("/audit/events", headers=headers)
        self.require(resp.status_code == 200, "GET /audit/events", resp)
        return resp.json()

    def open_encounter(self, headers, patient_pk) -> dict:
        resp = self.client.post(f"/patients/{patient_pk}/encounters", headers=headers)
        self.expect(resp.status_code == 201 and resp.json()["status"] == "OPEN",
                    "encounter opened (an earlier run may have left one open)", resp)
        return resp.json()

    def close_encounter(self, headers, encounter_pk):
        resp = self.client.post(f"/encounters/{encounter_pk}/close", headers=headers)
        self.expect(resp.status_code == 200 and resp.json()["status"] == "CLOSED", "encounter closed", resp)

    def manual_preview(self, headers, patient, encounter_pk, dosha, herbs) -> str:
        resp = self.client.post("/prescribe/manual-preview", headers=headers, json={
            "patient_name": patient["display_name"], "patient_pk": patient["patient_pk"],
            "encounter_pk": encounter_pk, "dosha_imbalance": dosha, "prescribed_herbs": herbs,
        })
        self.expect(resp.status_code == 200, "manual preview created", resp)
        return resp.json()["preview_id"]

    def confirm(self, headers, body, key=None):
        extra = {"Idempotency-Key": key} if key else {}
        return self.client.post("/prescribe/confirm", json=body, headers={**headers, **extra})

    def state(self, doctor, admin, patient_pk) -> tuple:
        """(stock, prescription ids in history, PRESCRIPTION_CONFIRMED event count)"""
        events = self.audit_events(admin)["events"]
        return (self.inventory(doctor),
                [p["prescription_id"] for p in self.history(doctor, patient_pk)],
                sum(e["event_type"] == "PRESCRIPTION_CONFIRMED" for e in events))

    # --- journey ---------------------------------------------------------------

    def run(self) -> dict:
        self.step("[1] Log in as the demo doctor")
        doctor = self.login("DOCTOR")

        self.step("[2] Find the synthetic demo patient")
        patient = self._demo_patient(doctor)
        patient_pk = patient["patient_pk"]

        self.step("[3] Open an encounter")
        encounter = self.open_encounter(doctor, patient_pk)
        stock_before = self.inventory(doctor)
        history_before = self.history(doctor, patient_pk)

        self.step("[4] Manual preview linked to the patient and encounter")
        proposed = [{"name": "Ashwagandha", "grams": 50}, {"name": "Brahmi", "grams": 30}]
        preview_id = self.manual_preview(doctor, patient, encounter["encounter_pk"], "Vata", proposed)

        self.step("[5-6] Confirm with doctor corrections and an Idempotency-Key")
        corrected_dosha = "Vata-Pitta"
        corrected = [{"name": "Ashwagandha", "grams": 40}, {"name": "Shatavari", "grams": 25}]
        confirm_body = {"preview_id": preview_id, "dosha_imbalance": corrected_dosha, "prescribed_herbs": corrected}
        key = str(uuid.uuid4())
        resp = self.confirm(doctor, confirm_body, key)
        self.expect(resp.status_code == 200, "confirmation accepted", resp)
        confirmed = resp.json()
        prescription_id = confirmed["prescription_id"]
        self.expect(confirmed["dosha_imbalance"] == corrected_dosha and _herbs(confirmed["prescribed_herbs"])
                    == _herbs(corrected), "confirmed values are the doctor's corrections, not the preview", resp)

        self.step("[7] Stock decreased by exactly the corrected amounts")
        stock_after = self.inventory(doctor)
        expected_stock = dict(stock_before)
        for name, grams in _herbs(corrected):
            expected_stock[name] -= grams
        self.expect(stock_after == expected_stock,
                    f"stock {_changes(stock_before, stock_after)}; uncorrected Brahmi untouched")

        self.step("[8] As the demo admin: the audit event")
        admin = self.login("ADMIN")
        self.expect(self.client.get("/audit/events", headers=doctor).status_code == 403,
                    "the doctor cannot read the audit trail (403)")
        audit = self.audit_events(admin)
        self.expect(audit["chain_valid"] is True and not audit["integrity_violations"], "audit hash chain valid")
        matches = [e for e in audit["events"]
                   if e["event_type"] == "PRESCRIPTION_CONFIRMED" and e["prescription_id"] == prescription_id]
        self.expect(len(matches) == 1 and matches[0]["preview_id"] == preview_id,
                    "exactly one PRESCRIPTION_CONFIRMED event for this prescription and preview")
        decision = matches[0]["event_data"]
        self.expect(decision["decision_source"] == "MANUAL"
                    and decision["doctor_decision"]["dosha"] == corrected_dosha
                    and _herbs(decision["doctor_decision"]["herbs"]) == _herbs(corrected),
                    "audit records the corrected decision")
        self.expect(any(e["event_type"] == "ENCOUNTER_OPENED"
                        and e["event_data"].get("encounter_ref") == encounter["encounter_ref"]
                        for e in audit["events"]), "ENCOUNTER_OPENED event for this encounter")

        self.step("[9] The prescription is in the patient's history")
        history = self.history(doctor, patient_pk)
        self.expect(len(history) == len(history_before) + 1 and history[0]["prescription_id"] == prescription_id,
                    "newest history entry is this prescription")
        self.expect(history[0]["dosha_imbalance"] == corrected_dosha
                    and _herbs(history[0]["prescribed_herbs"]) == _herbs(corrected),
                    "history shows the corrected values")

        self.step("[10] Close the encounter")
        self.close_encounter(doctor, encounter["encounter_pk"])
        self.expect(any(e["event_type"] == "ENCOUNTER_CLOSED"
                        and e["event_data"].get("encounter_ref") == encounter["encounter_ref"]
                        for e in self.audit_events(admin)["events"]), "ENCOUNTER_CLOSED event for this encounter")

        self.step("[11] Replay the same confirmation with the same Idempotency-Key")
        before = self.state(doctor, admin, patient_pk)
        replay = self.confirm(doctor, confirm_body, key)
        self.expect(replay.status_code == 200 and replay.json() == confirmed,
                    "replay returns the original response (even though the encounter is now closed)", replay)
        self.expect(self.state(doctor, admin, patient_pk) == before,
                    "no duplicate prescription, deduction or audit event")

        self.log("\nNegative cases")
        self._key_reused_with_a_different_request(doctor, admin, patient_pk, confirm_body, key)
        self._wrong_clinic(doctor, admin, patient, encounter, preview_id)
        self._closed_encounter(doctor, admin, patient)
        self._insufficient_stock(doctor, admin, patient)

        self.log(f"\nAll {self.checks} checks passed.")
        return {"checks": self.checks, "patient_pk": patient_pk, "encounter_pk": encounter["encounter_pk"],
                "preview_id": preview_id, "prescription_id": prescription_id,
                "stock_before": stock_before, "stock_after": stock_after}

    def _demo_patient(self, doctor) -> dict:
        wanted = DEMO_PATIENT["clinic_patient_id"]
        resp = self.client.get("/patients", params={"q": wanted}, headers=doctor)
        self.require(resp.status_code == 200, "patient search", resp)
        found = [p for p in resp.json()["results"] if p["clinic_patient_id"] == wanted]
        if found:
            self.expect(len(found) == 1, f"demo patient {wanted} found")
            return found[0]
        resp = self.client.post("/patients", headers=doctor, json={
            "clinic_patient_id": wanted, "display_name": DEMO_PATIENT["display_name"]})
        self.expect(resp.status_code == 201, f"demo patient {wanted} registered", resp)
        return resp.json()

    # --- negative cases ------------------------------------------------------------

    def _key_reused_with_a_different_request(self, doctor, admin, patient_pk, confirm_body, key):
        self.step("[N1] Idempotency key reused with a different request")
        before = self.state(doctor, admin, patient_pk)
        different = {**confirm_body, "prescribed_herbs": [{"name": "Ashwagandha", "grams": 41}]}
        resp = self.confirm(doctor, different, key)
        self.expect(resp.status_code == 409, "same key, different body -> 409", resp)
        self.expect(self.state(doctor, admin, patient_pk) == before, "nothing changed")

    def _wrong_clinic(self, doctor, admin, patient, encounter, preview_id):
        self.step("[N2] Wrong-clinic access")
        from backend.auth_service import create_user
        username, password = f"v01_other_{secrets.token_hex(4)}", secrets.token_urlsafe(18)
        create_user(username, password, OTHER_CLINIC_ID, role="DOCTOR")
        resp = self.client.post("/auth/login", json={"username": username, "password": password})
        self.expect(resp.status_code == 200, "doctor of another clinic logged in", resp)
        other = {"Authorization": f"Bearer {resp.json()['token']}"}
        patient_pk, encounter_pk = patient["patient_pk"], encounter["encounter_pk"]
        before = self.state(doctor, admin, patient_pk)

        resp = self.client.get("/patients", params={"q": DEMO_PATIENT["clinic_patient_id"]}, headers=other)
        self.expect(resp.status_code == 200 and resp.json()["results"] == [], "search finds nothing", resp)
        for label, resp in (
            ("patient history", self.client.get(f"/patients/{patient_pk}/prescriptions", headers=other)),
            ("patient encounters", self.client.get(f"/patients/{patient_pk}/encounters", headers=other)),
            ("encounter", self.client.get(f"/encounters/{encounter_pk}", headers=other)),
            ("open an encounter", self.client.post(f"/patients/{patient_pk}/encounters", headers=other)),
            ("preview for the patient", self.client.post("/prescribe/manual-preview", headers=other, json={
                "patient_name": "x", "patient_pk": patient_pk, "dosha_imbalance": "Vata",
                "prescribed_herbs": [{"name": "Brahmi", "grams": 1}]})),
            ("confirm the preview", self.confirm(other, {"preview_id": preview_id})),
        ):
            self.expect(resp.status_code == 404, f"{label} -> 404", resp)
        resp = self.client.get("/inventory", headers=other)
        self.expect(resp.status_code == 200 and resp.json()["items"] == [], "inventory shows none of this clinic's stock")
        self.expect(self.state(doctor, admin, patient_pk) == before, "nothing changed")

    def _closed_encounter(self, doctor, admin, patient):
        self.step("[N3] Confirming against a closed encounter")
        encounter = self.open_encounter(doctor, patient["patient_pk"])
        preview_id = self.manual_preview(doctor, patient, encounter["encounter_pk"], "Kapha",
                                         [{"name": "Triphala", "grams": 20}])
        self.close_encounter(doctor, encounter["encounter_pk"])
        before = self.state(doctor, admin, patient["patient_pk"])
        resp = self.confirm(doctor, {"preview_id": preview_id}, str(uuid.uuid4()))
        self.expect(resp.status_code == 409 and "closed" in resp.json()["detail"], "confirm -> 409 (closed)", resp)
        self.expect(self.state(doctor, admin, patient["patient_pk"]) == before, "nothing changed")

    def _insufficient_stock(self, doctor, admin, patient):
        self.step("[N4] Insufficient stock")
        encounter = self.open_encounter(doctor, patient["patient_pk"])
        available = self.inventory(doctor)["Brahmi"]
        preview_id = self.manual_preview(doctor, patient, encounter["encounter_pk"], "Pitta",
                                         [{"name": "Brahmi", "grams": available + 1}])
        before = self.state(doctor, admin, patient["patient_pk"])
        resp = self.confirm(doctor, {"preview_id": preview_id}, str(uuid.uuid4()))
        self.expect(resp.status_code == 409, f"confirm {available + 1} g with {available} g in stock -> 409", resp)
        self.expect(self.state(doctor, admin, patient["patient_pk"]) == before, "nothing changed")
        self.close_encounter(doctor, encounter["encounter_pk"])


def _changes(before: dict, after: dict) -> str:
    return ", ".join(f"{name} {before[name]}->{after[name]}" for name in sorted(before) if before[name] != after.get(name))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="existing demo database made by setup_v01_demo.py")
    args = parser.parse_args(argv)
    try:
        run_walkthrough(args.db)
    except UnsafeDemoPathError as e:
        print(f"Refused: {e}", file=sys.stderr)
        return 2
    except WalkthroughFailure as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
