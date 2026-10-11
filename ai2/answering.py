"""
Execution stage of the AI-2 pipeline: plan -> deterministic executor -> evidence -> template claim -> deterministic verifier.

Moved without change in behaviour from the evaluation module memory.retrieval (_out, _answer); the session and planning
entry points of that module are not part of the production package. Every "answer" outcome carries source evidence and a
verified claim; "no evidence means abstention": a not-recorded result is returned as ABSTAIN/NOT_RECORDED, and a claim the
verifier rejects is never shown (VERIFIER_REJECTED).
"""
from __future__ import annotations

from ai2.answer import render
from ai2.executor import Query, QueryError
from ai2.snapshot import PatientSnapshot
from ai2.verifier import verify_claim


def _out(outcome: str, stage: str, reason=None, **extra) -> dict:
    base = {"outcome": outcome, "stage": stage, "reason": reason, "screen": None, "plan": None, "claim": None,
            "verification": None}
    base.update(extra)
    return base


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
