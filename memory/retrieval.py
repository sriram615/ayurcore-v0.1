"""
AI-2 memory retrieval (evaluation module, not a production endpoint).

    answer_for_session(authorization, patient_pk, question, planner) -> dict
        authorization  the caller's "Bearer <token>" header value, resolved by the EXISTING
                       backend.rbac.require_auth (sessions, expiry, revocation); the clinic comes
                       only from that session, and the role must be in the existing patient-read
                       policy (PERMISSION_MATRIX["patients"]: DOCTOR, ADMIN). Nothing is taken
                       from the caller as clinic or patient scope except the patient key, which
                       is resolved inside the session's clinic (another clinic's patient = not found)
    answer_with_snapshot(snapshot, question, planner) -> dict
        the same pipeline on an already scoped snapshot (used by evaluation)

Pipeline, each stage able to stop with an abstention:
    scope screen (deterministic) -> local planner (plans only) -> deterministic executor
    -> evidence -> template claim -> deterministic verifier
An "answer" outcome always carries source evidence and a verified claim. "No evidence means
abstention" (master plan section 8): a not-recorded result is returned as ABSTAIN/NOT_RECORDED.

Outcome: {"outcome": answer | abstain | not_found | unauthenticated | forbidden,
          "stage": auth | screen | planner | executor | verifier | done, "reason", "screen",
          "plan", "claim", "verification"}
"""
from __future__ import annotations

from memory.answer import render
from memory.executor import Query, QueryError
from memory.planner import Planner
from memory.scope import screen
from memory.snapshot import PatientNotFound, PatientSnapshot
from memory.verifier import verify_claim


def _out(outcome: str, stage: str, reason=None, **extra) -> dict:
    base = {"outcome": outcome, "stage": stage, "reason": reason, "screen": None, "plan": None, "claim": None,
            "verification": None}
    base.update(extra)
    return base


def plan_question(question: str, planner: Planner) -> dict:
    """Screen, then plan. No patient data is touched here."""
    verdict = screen(question)
    if verdict["decision"] == "ABSTAIN":
        return _out("abstain", "screen", verdict["reason"], screen=verdict)
    plan = planner.plan(question)
    if plan["kind"] == "abstain":
        return _out("abstain", "planner", plan["reason"], screen=verdict, plan=plan)
    return _out("planned", "planner", None, screen=verdict, plan=plan)


def _answer(snapshot: PatientSnapshot, planned: dict) -> dict:
    plan = planned["plan"]
    try:
        claim = render(snapshot, Query.from_dict(plan["query"]))
    except QueryError as exc:
        return _out("abstain", "executor", "AMBIGUOUS", screen=planned["screen"], plan=plan,
                    verification={"verdict": "rejected", "violations": [{"code": "unsupported_query", "detail": str(exc)}]})
    verification = verify_claim(snapshot, claim)
    if verification["verdict"] != "verified":
        return _out("abstain", "verifier", "VERIFIER_REJECTED", screen=planned["screen"], plan=plan, claim=claim,
                    verification=verification)
    if claim["status"] == "not_recorded" or not claim["sources"]:
        return _out("abstain", "executor", "NOT_RECORDED", screen=planned["screen"], plan=plan, claim=claim,
                    verification=verification)
    return _out("answer", "done", None, screen=planned["screen"], plan=plan, claim=claim, verification=verification)


def answer_with_snapshot(snapshot: PatientSnapshot, question: str, planner: Planner) -> dict:
    planned = plan_question(question, planner)
    return planned if planned["outcome"] == "abstain" else _answer(snapshot, planned)


def answer_for_session(authorization: str, patient_pk: int, question: str, planner: Planner) -> dict:
    from fastapi import HTTPException

    from backend.rbac import PERMISSION_MATRIX, require_auth
    from memory.snapshot import build_snapshot

    try:
        session = require_auth(authorization)
    except HTTPException:
        return _out("unauthenticated", "auth")
    if session.role not in PERMISSION_MATRIX["patients"]:
        return _out("forbidden", "auth")
    planned = plan_question(question, planner)
    if planned["outcome"] == "abstain":
        return planned
    try:
        snapshot = build_snapshot(session.clinic_id, patient_pk)
    except PatientNotFound:
        return _out("not_found", "auth")
    return _answer(snapshot, planned)
