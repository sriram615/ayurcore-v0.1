"""
Deterministic template claims for executor results (no model).

A Claim is the unit the verifier checks:
    {"query": <Query dict>, "status": <result status>, "value": <answer value>,
     "qualifier": "exact" | "at_least" | "partial" | "unknown" | "not_recorded",
     "sources": [<source labels>], "text": <phrasing>}

qualifier per status: ok -> exact; incomplete -> at_least for count/sum, partial otherwise;
unknown -> unknown; not_recorded -> not_recorded. Sources are exactly the result's labels
(evidence plus the records whose unknown values limit the answer). A future planner or
phraser must produce the same structure and pass the same verifier.
"""
from __future__ import annotations

from ai2.executor import Query, Result, execute
from ai2.snapshot import PatientSnapshot


def qualifier_for(query: Query, result: Result) -> str:
    if result.status == "ok":
        return "exact"
    if result.status == "incomplete":
        return "at_least" if query.op in ("count", "sum") else "partial"
    return result.status  # unknown / not_recorded


def _fmt(value) -> str:
    if isinstance(value, list):
        return ", ".join(_fmt(v) for v in value) if value else "none"
    if isinstance(value, dict):
        return "; ".join(f"{k} {_fmt(v)}" for k, v in value.items())
    return "unknown" if value is None else str(value)


def _text(query: Query, result: Result, qualifier: str) -> str:
    subject = f"{query.op} {query.target}" + (f" {query.herb}" if query.herb else "") + \
        (f" {query.dosha}" if query.dosha else "")
    if qualifier == "not_recorded":
        return f"Not recorded: {subject}."
    if qualifier == "unknown":
        return f"Unknown from the record: {subject} depends on records with unknown values."
    prefix = {"exact": "", "at_least": "At least ", "partial": "From the known records only: "}[qualifier]
    return f"{prefix}{subject}: {_fmt(result.value)}."


def render(snapshot: PatientSnapshot, query: Query) -> dict:
    result = execute(snapshot, query)
    qualifier = qualifier_for(query, result)
    return {"query": query.to_dict(), "status": result.status, "value": result.value, "qualifier": qualifier,
            "sources": sorted(result.labels), "text": _text(query, result, qualifier)}
