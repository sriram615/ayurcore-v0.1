"""
AI-2 v2 interpretation pipeline -- Block 1 deterministic foundations (evaluation module, not production).

    V2Planner(planner).interpret(question)           -> interpretation, NO patient data touched
    answer_with_snapshot_v2(snapshot, question, v2)  -> outcome on an already scoped snapshot
    answer_for_session_v2(authorization, patient_pk, question, v2)
                                                     -> the same through the EXISTING require_auth /
                                                        PERMISSION_MATRIX["patients"] / clinic-scoped
                                                        snapshot, as memory.retrieval

Pipeline:
    normalize (memory.normalize)      not text / empty / too long            -> abstain
    scope screen (memory.scope)       out of scope                           -> abstain (its reason)
    reference check (memory.reference, Block 2A; on by default, reference_check=False turns it off)
                                      another patient / group / clinic-wide  -> abstain OTHER_PATIENT
    candidates (memory.interpret)     EVERY slot-compatible plan of the fixed plan space (no top-K cut),
                                      scored by the unchanged planner; DIAGNOSIS / TREATMENT /
                                      UNAVAILABLE as the planner's best operation -> abstain
    alignment (memory.interpret)      consistent candidates only
    coverage (memory.coverage)        words no component accounts for
    decision (Block 1):
        a word that no fixed plan can express (coverage UNSUPPORTED: negation, ordinal, extreme,
        other people, calendar window)                         -> abstain AMBIGUOUS
        one consistent reading, every word accounted for      -> plan (executed below), unless the optional
            confidence gate (Block 3, confidence_gate="threshold": calibrated confidence below
            memory.confidence.THRESHOLD; "top1": not the planner's best-scored plan) withholds it
            -> CLARIFY (confirm that reading)
        one consistent reading, unexplained words             -> CLARIFY (confirm that reading)
        2..MAX_OPTIONS consistent readings                    -> CLARIFY (choose one)
        no consistent reading, but 1..MAX_OPTIONS readings that are only UNDER-specified (an
        operation or selector the question does not name, nothing contradicting)  -> CLARIFY
        otherwise                                             -> abstain AMBIGUOUS
    learned detector (memory.oos, Block 2, optional): may turn a plan / clarification into an abstention
        when it fires; it never creates a plan, never runs on refused input, and reads only the text
    executor -> evidence -> claim -> verifier (memory.retrieval._answer, unchanged); a not-recorded
    result abstains NOT_RECORDED.
A clarification carries only plain-sentence descriptions of the candidate plans: it is built from the
question alone, before any patient data is read, and is never an answer.

Outcome: {"outcome": answer | clarify | abstain | not_found | unauthenticated | forbidden, "stage",
          "reason", "normalized", "screen", "interpretation", "plan", "options", "claim", "verification"}
"""
from __future__ import annotations

import math

from memory.confidence import THRESHOLD, calibrated
from memory.coverage import coverage
from memory.interpret import CandidatePlanner
from memory.normalize import normalize
from memory.reference import outside_scope
from memory.scope import screen

MAX_OPTIONS = 3
CLINICAL = ("DIAGNOSIS", "TREATMENT", "UNAVAILABLE")


def describe(query: dict) -> str:
    """A plain sentence for a closed-vocabulary query (used for clarification options)."""
    op, target, herb, dosha, n, a = (query[k] for k in ("op", "target", "herb", "dosha", "last_n", "a"))
    scope = f" in the latest {n} prescriptions" if n else ""
    sel = {"first": "first", "previous": "previous"}.get(a, a)
    text = {
        ("first", "prescription"): "The first prescription",
        ("last", "prescription"): "The latest prescription",
        ("first", "herb"): f"When {herb} was first prescribed, and the grams then",
        ("last", "herb"): f"When {herb} was last prescribed, and the grams then",
        ("first", "dosha"): "The dosha recorded on the first prescription",
        ("last", "dosha"): "The dosha recorded on the latest prescription",
        ("first", "encounter"): "The date of the first visit",
        ("last", "encounter"): "The date of the latest visit",
        ("count", "prescription"): "The number of prescriptions" + scope,
        ("count", "encounter"): "The number of visits",
        ("count", "herb"): f"The number of prescriptions containing {herb}" + scope,
        ("count", "dosha"): f"The number of prescriptions recorded as {dosha}" + scope,
        ("sum", "herb"): f"The total grams of {herb}" + (scope or " across all prescriptions"),
        ("list", "prescription"): "All prescriptions, oldest first" + scope,
        ("list", "encounter"): "All visits, oldest first",
        ("list", "herb"): "Every herb prescribed" + (scope or " so far"),
        ("list", "dosha"): "The dosha of each prescription, oldest first" + scope,
        ("diff", "prescription"): f"What changed from the {sel} prescription to the latest one",
        ("compare", "herb"): f"{herb} grams on the {sel} prescription compared with the latest one",
        ("compare", "dosha"): f"The dosha on the {sel} prescription compared with the latest one",
    }.get((op, target))
    return text + "." if text else f"{op} {target}."


def _softmax(scores: list[float]) -> list[float]:
    if not scores:
        return []
    top = max(scores)
    exp = [math.exp(s - top) for s in scores]
    total = sum(exp)
    return [e / total for e in exp]


class V2Planner:
    """Defaults are the v2 foundations. The switches exist so the harness can measure each step:
    V2Planner(p, k=5, normalize_input=False, coverage_check=False, clarify=False, reference_check=False) is the
    Block 5 pipeline; reference_check=False alone gives Block 1."""

    def __init__(self, planner, k: int | None = None, normalize_input: bool = True, coverage_check: bool = True,
                 clarify: bool = True, detector=None, detector_mode: str = "gate", reference_check: bool = True,
                 confidence_gate: str | None = None, confidence_threshold: float = THRESHOLD):
        if detector_mode not in ("gate", "clarify_only"):
            raise ValueError("detector_mode must be gate or clarify_only")
        self.detector, self.detector_mode = detector, detector_mode
        self.reference_check = reference_check
        if confidence_gate not in (None, "threshold", "top1"):
            raise ValueError("confidence_gate must be None, threshold or top1")
        self.confidence_gate, self.confidence_threshold = confidence_gate, confidence_threshold
        self.planner, self.k = planner, k
        self.normalize_input, self.coverage_check, self.clarify = normalize_input, coverage_check, clarify
        self.cand = CandidatePlanner(planner, k=None)

    def _low_confidence(self, win: dict, cands: list[dict]) -> bool:
        return _low_confidence_rule(self.confidence_gate, self.confidence_threshold, win, cands)

    def interpret(self, question) -> dict:
        """Block 1 decision, then the optional learned detector, which may only turn a plan or a
        clarification into an abstention (never the reverse)."""
        out = self._interpret_block1(question)
        if self.detector is None or out["outcome"] not in ("plan", "clarify"):
            return out
        if self.detector_mode == "clarify_only" and out["outcome"] != "clarify":
            return out
        verdict = self.detector.predict(out["interpretation"]["text"])
        out["detector"] = verdict
        if not verdict["fires"]:
            return out
        return {**out, "outcome": "abstain", "stage": "detector", "reason": verdict["reason"], "plan": None,
                "options": []}

    def _interpret_block1(self, question) -> dict:
        base = {"normalized": None, "screen": None, "plan": None, "options": [], "interpretation": None}
        if self.normalize_input:
            norm = normalize(question)
            base["normalized"] = {k: norm[k] for k in ("ok", "reason", "changes")}
            if not norm["ok"]:
                return {**base, "outcome": "abstain", "stage": "normalize", "reason": "AMBIGUOUS"}
            text = norm["text"]
        else:
            text = question
        verdict = screen(text)
        base["screen"] = verdict
        if verdict["decision"] == "ABSTAIN":
            return {**base, "outcome": "abstain", "stage": "screen", "reason": verdict["reason"]}
        if self.reference_check:
            ref = outside_scope(text)
            base["reference"] = ref
            if ref["outside"]:
                return {**base, "outcome": "abstain", "stage": "reference", "reason": "OTHER_PATIENT"}
        el, cands, best_op = self.cand.candidates(text)
        cov = coverage(text) if self.coverage_check else {"unexplained": [], "unsupported": []}
        probs = _softmax([c["score"] for c in cands])  # over EVERY compatible plan, whatever k is
        for c, p, q in zip(cands, probs, calibrated([c["score"] for c in cands])):
            c["probability"] = round(p, 6)
            c["calibrated_probability"] = round(q, 6)  # temperature-scaled (memory.confidence)
        if self.k is not None:
            cands = cands[: self.k]
        consistent, seen = [], set()
        for c in cands:  # rank order
            key = tuple(sorted(c["query"].items()))
            if c["consistent"] and key not in seen:
                seen.add(key)
                consistent.append(c)
        top = max(probs) if probs else None
        info = {"text": text, "candidates": [{k: c[k] for k in ("frame", "query", "rank", "score", "probability",
                                                                 "calibrated_probability", "consistent", "unexplained",
                                                                 "unsupported")} for c in cands],
                "consistent": [c["frame"] for c in consistent], "unexplained_words": cov["unexplained"],
                "best_op": best_op, "top_probability": top}
        base["interpretation"] = info
        if best_op in CLINICAL:
            return {**base, "outcome": "abstain", "stage": "planner", "reason": best_op}
        if cov["unsupported"]:
            return {**base, "outcome": "abstain", "stage": "coverage", "reason": "AMBIGUOUS"}
        if not consistent and self.clarify:
            under, seen = [], set()
            for c in cands:
                key = tuple(sorted(c["query"].items()))
                if not c["unexplained"] and c["unsupported"] and key not in seen and \
                        all(u.startswith(("op:", "selector:")) for u in c["unsupported"]):
                    seen.add(key)
                    under.append(c)
            if 1 <= len(under) <= MAX_OPTIONS:
                return {**base, "outcome": "clarify", "stage": "clarify", "reason": "UNDERSPECIFIED",
                        "options": [{"query": c["query"], "frame": c["frame"], "text": describe(c["query"]),
                                     "probability": c["probability"]} for c in under]}
        if not consistent:
            return {**base, "outcome": "abstain", "stage": "alignment", "reason": "AMBIGUOUS"}
        if not self.clarify and (len(consistent) > 1 or cov["unexplained"]):
            return {**base, "outcome": "abstain", "stage": "alignment", "reason": "AMBIGUOUS"}
        if len(consistent) > MAX_OPTIONS:
            return {**base, "outcome": "abstain", "stage": "alignment", "reason": "AMBIGUOUS"}
        if len(consistent) == 1 and not cov["unexplained"]:
            win = consistent[0]
            info["confidence"] = win["probability"]
            info["calibrated_confidence"] = win["calibrated_probability"]
            if self._low_confidence(win, cands):
                if not self.clarify:
                    return {**base, "outcome": "abstain", "stage": "confidence", "reason": "AMBIGUOUS"}
                return {**base, "outcome": "clarify", "stage": "confidence", "reason": "LOW_CONFIDENCE",
                        "options": [{"query": win["query"], "frame": win["frame"], "text": describe(win["query"]),
                                     "probability": win["probability"]}]}
            plan = {"kind": "query", "query": win["query"], "frame": win["frame"]}
            return {**base, "outcome": "plan", "stage": "planner", "reason": None, "plan": plan}
        options = [{"query": c["query"], "frame": c["frame"], "text": describe(c["query"]),
                    "probability": c["probability"]} for c in consistent]
        reason = "UNEXPLAINED_WORDS" if len(consistent) == 1 else "SEVERAL_READINGS"
        return {**base, "outcome": "clarify", "stage": "clarify", "reason": reason, "options": options}


def _low_confidence_rule(gate, threshold, win, cands) -> bool:
    if gate == "threshold":
        return win["calibrated_probability"] < threshold
    if gate == "top1":
        return win["rank"] != 0
    return False



def _finish(snapshot, interp: dict) -> dict:
    from memory.retrieval import _answer
    out = _answer(snapshot, {"screen": interp["screen"], "plan": interp["plan"]})
    return {**interp, "outcome": out["outcome"], "stage": out["stage"], "reason": out["reason"],
            "claim": out["claim"], "verification": out["verification"]}


def answer_with_snapshot_v2(snapshot, question, v2: V2Planner) -> dict:
    interp = v2.interpret(question)
    if interp["outcome"] != "plan":
        return {**interp, "claim": None, "verification": None}
    return _finish(snapshot, interp)


def answer_for_session_v2(authorization, patient_pk, question, v2: V2Planner) -> dict:
    from fastapi import HTTPException

    from backend.rbac import PERMISSION_MATRIX, require_auth
    from memory.snapshot import PatientNotFound, build_snapshot

    empty = {"stage": "auth", "reason": None, "normalized": None, "screen": None, "plan": None, "options": [],
             "interpretation": None, "claim": None, "verification": None}
    try:
        session = require_auth(authorization)
    except HTTPException:
        return {**empty, "outcome": "unauthenticated"}
    if session.role not in PERMISSION_MATRIX["patients"]:
        return {**empty, "outcome": "forbidden"}
    interp = v2.interpret(question)
    if interp["outcome"] != "plan":
        return {**interp, "claim": None, "verification": None}
    try:
        snapshot = build_snapshot(session.clinic_id, patient_pk)
    except PatientNotFound:
        return {**empty, "outcome": "not_found"}
    return _finish(snapshot, interp)
