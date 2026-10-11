"""
The public result of one AI-2 question: a typed, JSON-able view of the pipeline output, with a deterministic category and resolved
evidence. Nothing here changes what the pipeline decided; it only names it and looks up the records it cites.

Categories (a pure function of outcome / stage / reason / claim qualifier):
    answer         verified exact answer
    incomplete     verified answer limited by unknown or partial records (qualifier at_least / partial / unknown)
    clarify        1..3 readings to choose from; never an answer
    missing        the record holds nothing for it (NOT_RECORDED)
    unsupported    outside what the memory can express or record (ambiguous wording, trend, calendar window, vitals, ...)
    unsafe         advice / treatment / diagnosis, or an instruction-injection attempt
    access_denied  asks about another patient, other patients or the whole clinic
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ai2.snapshot import PatientSnapshot

CATEGORIES = ("answer", "incomplete", "clarify", "missing", "unsupported", "unsafe", "access_denied")


def categorize(raw: dict) -> str:
    outcome, reason = raw["outcome"], raw.get("reason")
    if outcome == "answer":
        return "answer" if (raw.get("claim") or {}).get("qualifier") == "exact" else "incomplete"
    if outcome == "clarify":
        return "clarify"
    if reason == "NOT_RECORDED":
        return "missing"
    if reason == "OTHER_PATIENT":
        return "access_denied"
    if reason in ("TREATMENT", "DIAGNOSIS"):
        return "unsafe"
    if any(str(c).startswith("injection:") for c in (raw.get("screen") or {}).get("cues", [])):
        return "unsafe"
    return "unsupported"


def resolve_evidence(snapshot: PatientSnapshot, sources) -> list[dict]:
    """The records a claim cites ("rx:7", "enc:abc..."), as plain dicts. Sequential encounter keys are not exposed."""
    out = []
    for label in sources or []:
        rx = snapshot.prescription(label)
        if rx is not None:
            out.append({"kind": "prescription", "label": label, "prescription_id": rx.prescription_id, "created_at": rx.created_at,
                        "status": rx.status, "dosha": rx.dosha,
                        "herbs": None if rx.herbs is None else [{"name": n, "grams": g} for n, g in rx.herbs]})
            continue
        enc = next((e for e in snapshot.encounters if e.label == label), None)
        if enc is not None:
            out.append({"kind": "encounter", "label": label, "encounter_ref": enc.encounter_ref, "status": enc.status,
                        "created_at": enc.created_at, "closed_at": enc.closed_at})
            continue
        out.append({"kind": "unknown", "label": label})   # cannot happen for a verified claim; kept visible rather than dropped
    return out


@dataclass(frozen=True)
class Ai2Answer:
    outcome: str                      # answer | clarify | abstain
    stage: str                        # normalize | screen | reference | planner | coverage | alignment | clarify | executor | verifier | done
    reason: Optional[str]
    category: str                     # one of CATEGORIES
    text: Optional[str]               # the claim's template sentence (English), if there is a claim
    value: object = None              # the claim's structured value
    qualifier: Optional[str] = None   # exact | at_least | partial | unknown | not_recorded
    verified: bool = False
    options: tuple = ()               # ({"frame", "text", "query"}, ...) for a clarification
    evidence: tuple = ()              # resolved records
    plan_frame: Optional[str] = None
    plan_query: Optional[dict] = None
    pipeline: dict = field(default_factory=dict, compare=False, repr=False)   # the full pipeline output, for audit and tests

    def to_dict(self, include_pipeline: bool = False) -> dict:
        d = {"outcome": self.outcome, "stage": self.stage, "reason": self.reason, "category": self.category, "text": self.text,
             "value": self.value, "qualifier": self.qualifier, "verified": self.verified, "options": [dict(o) for o in self.options],
             "evidence": [dict(e) for e in self.evidence], "plan_frame": self.plan_frame, "plan_query": self.plan_query}
        if include_pipeline:
            d["pipeline"] = self.pipeline
        return d


def from_pipeline(snapshot: PatientSnapshot, raw: dict) -> Ai2Answer:
    claim = raw.get("claim")
    plan = raw.get("plan") or {}
    return Ai2Answer(
        outcome=raw["outcome"], stage=raw["stage"], reason=raw.get("reason"), category=categorize(raw),
        text=claim["text"] if claim else None, value=claim["value"] if claim else None,
        qualifier=claim["qualifier"] if claim else None,
        verified=bool(raw.get("verification") and raw["verification"].get("verdict") == "verified"),
        options=tuple({"frame": o["frame"], "text": o["text"], "query": o["query"]} for o in raw.get("options") or []),
        evidence=tuple(resolve_evidence(snapshot, claim["sources"])) if claim else (),
        plan_frame=plan.get("frame"), plan_query=plan.get("query"), pipeline=raw)
