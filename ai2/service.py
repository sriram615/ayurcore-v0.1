"""
Public AI-2 service: ask one question about one patient's recorded history.

    service = Ai2Service.default()                       # packaged planner, integrity-checked, loaded once
    answer  = service.ask(snapshot, "What was her last dose of Neem?")                       # snapshot supplied by the application
    answer  = service.ask_patient(records, clinic_id, patient_pk, "...")                     # or a PatientRecords port

Guarantees:
    local and deterministic  no network, no model server, no randomness at ask time, no cost; the same snapshot and question
                             always give the same answer
    read-only                the only data it sees is the PatientSnapshot; it has no database handle and no write path
    authority stays outside  authentication, role checks and the choice of clinic are the application's; AI-2 receives a
                             snapshot (or a port already bound to the caller's clinic) and nothing else
    never an unverified answer  an "answer" always carries evidence and passed the deterministic verifier
Thread-safety: the planner is read-only after loading and the pipeline keeps no per-request state, so one service can be shared.
Not a clinical validation: it retrieves what was recorded, and refuses advice, diagnosis and anything it cannot ground.
"""
from __future__ import annotations

import hashlib
import pathlib
from typing import Optional

from ai2.c10 import C10Arm
from ai2.interpretation import V2Planner, _finish, answer_with_snapshot_v2
from ai2.planner import Planner
from ai2.ports import PatientRecords
from ai2.result import Ai2Answer, from_pipeline
from ai2.snapshot import PatientSnapshot, require_unique_records

PLANNER_FILE = pathlib.Path(__file__).resolve().parent / "data" / "planner_model.json"
# the planner evaluated in every dev run of the accepted composition (dev-run-09 config: planner_model_sha256)
PLANNER_SHA256 = "8e8161b56d60d8823d21f5ef588abb0b03c52183076c2eb1977085914f650b77"


class PlannerIntegrityError(RuntimeError):
    """The packaged planner file is not the evaluated one."""


class InvalidChoice(ValueError):
    """The submitted reading is not one of the readings AI-2 itself offers for that question."""


def load_planner(path: Optional[pathlib.Path] = None) -> Planner:
    path = pathlib.Path(path) if path is not None else PLANNER_FILE
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != PLANNER_SHA256:
        raise PlannerIntegrityError(f"planner file {path.name} has sha256 {digest}, expected {PLANNER_SHA256}")
    return Planner.load(path)


def _require_snapshot(snapshot) -> None:
    """The verifier re-executes a claim on the very snapshot it is given, so it cannot notice a snapshot that is itself inconsistent. Refuse one
    that lists a record twice (ai2.snapshot.SnapshotUnavailable) before anything is executed or verified."""
    if not isinstance(snapshot, PatientSnapshot):
        raise TypeError("snapshot must be an ai2.snapshot.PatientSnapshot")
    require_unique_records(snapshot.prescriptions, snapshot.encounters)


class Ai2Service:
    def __init__(self, planner: Planner):
        self._arm = C10Arm(V2Planner(planner))

    @classmethod
    def default(cls) -> "Ai2Service":
        return cls(load_planner())

    def ask(self, snapshot: PatientSnapshot, question: str) -> Ai2Answer:
        _require_snapshot(snapshot)
        return from_pipeline(snapshot, answer_with_snapshot_v2(snapshot, question, self._arm))

    def choose(self, snapshot: PatientSnapshot, question: str, frame: str, query: dict) -> Ai2Answer:
        """Answer the reading the clinician picked from a clarification.

        Nothing the caller sends is trusted as a plan: the question is interpreted again (deterministically, from the text alone),
        and (frame, query) must equal one of the readings that interpretation offers -- otherwise InvalidChoice. The chosen reading
        then goes through the same executor, evidence and verifier as any other answer."""
        _require_snapshot(snapshot)
        interp = self._arm.interpret(question)
        offered = interp["options"] if interp["outcome"] == "clarify" else []
        if not (isinstance(frame, str) and isinstance(query, dict)) or not any(o["frame"] == frame and o["query"] == query for o in offered):
            raise InvalidChoice("the submitted reading is not one of the readings offered for this question")
        plan = {"kind": "query", "query": dict(query), "frame": frame}
        return from_pipeline(snapshot, _finish(snapshot, {**interp, "plan": plan, "options": []}))

    def ask_patient(self, records: PatientRecords, clinic_id: str, patient_pk: int, question: str) -> Ai2Answer:
        """Raises ai2.snapshot.PatientNotFound for a patient that is not in `clinic_id` (the caller maps it to its own 404)."""
        return self.ask(records.load_snapshot(clinic_id, patient_pk), question)
