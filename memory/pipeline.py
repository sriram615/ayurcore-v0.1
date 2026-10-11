"""
AI-2 question pipeline (evaluation only): question -> plan -> deterministic answer -> verification.

    answer_question(snapshot, question, planner) -> dict

The planner only plans. The executor answers from the snapshot, the template renderer
phrases the claim, and the verifier re-checks it; a rejected claim or an abstaining plan
yields an abstention. Nothing here answers from the question text itself.
"""
from __future__ import annotations

from memory.answer import render
from memory.executor import Query, QueryError
from memory.planner import Planner
from memory.snapshot import PatientSnapshot
from memory.verifier import verify_claim


def answer_question(snapshot: PatientSnapshot, question: str, planner: Planner) -> dict:
    plan = planner.plan(question)
    if plan["kind"] == "abstain":
        return {"plan": plan, "outcome": "abstain", "reason": plan["reason"], "claim": None, "verification": None}
    try:
        claim = render(snapshot, Query.from_dict(plan["query"]))
    except QueryError as exc:
        return {"plan": plan, "outcome": "abstain", "reason": "AMBIGUOUS", "claim": None,
                "verification": {"verdict": "rejected", "violations": [{"code": "unsupported_query", "detail": str(exc)}]}}
    verification = verify_claim(snapshot, claim)
    if verification["verdict"] != "verified":
        return {"plan": plan, "outcome": "abstain", "reason": "VERIFIER_REJECTED", "claim": claim,
                "verification": verification}
    return {"plan": plan, "outcome": "answer", "reason": None, "claim": claim, "verification": verification}
