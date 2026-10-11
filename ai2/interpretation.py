"""
AI-2 question interpretation and answering pipeline (production; deterministic, local, read-only).

    V2Planner(planner).interpret(question)           -> interpretation, NO patient data touched
    answer_with_snapshot_v2(snapshot, question, v2)  -> outcome on an already scoped snapshot

Pipeline:
    normalize (ai2.normalize)      not text / empty / too long            -> abstain
    scope screen (ai2.scope)       out of scope                           -> abstain (its reason)
    reference check (ai2.reference; on by default)
                                      another patient / group / clinic-wide  -> abstain OTHER_PATIENT
    candidates (ai2.interpret)     EVERY slot-compatible plan of the fixed plan space (no top-K cut),
                                      scored by the planner; DIAGNOSIS / TREATMENT /
                                      UNAVAILABLE as the planner's best operation -> abstain
    alignment (ai2.interpret)      consistent candidates only
    coverage (ai2.coverage)        words no component accounts for
    decision:
        a word that no fixed plan can express (coverage UNSUPPORTED: negation, ordinal, extreme,
        other people, calendar window, trend)                  -> abstain AMBIGUOUS
        one consistent reading, every word accounted for      -> plan (executed below)
        one consistent reading, unexplained words             -> CLARIFY (confirm that reading)
        2..MAX_OPTIONS consistent readings                    -> CLARIFY (choose one)
        no consistent reading, but 1..MAX_OPTIONS readings that are only UNDER-specified (an
        operation or selector the question does not name, nothing contradicting)  -> CLARIFY
        otherwise                                             -> abstain AMBIGUOUS
    executor -> evidence -> claim -> verifier (ai2.answering._answer); a not-recorded result abstains
    NOT_RECORDED.
A clarification carries only plain-sentence descriptions of the candidate plans: it is built from the
question alone, before any patient data is read, and is never an answer.

This module has no authentication, session or database code: who may ask, and which clinic's patient is
read, is decided by the application that hands it a PatientSnapshot (see ai2.service). Production
packaging removed, unchanged in behaviour for the default configuration, the optional learned out-of-scope
detector hook, the optional confidence gate and the session entry point of the evaluation module.

Outcome: {"outcome": answer | clarify | abstain, "stage",
          "reason", "normalized", "screen", "interpretation", "plan", "options", "claim", "verification"}
"""
from __future__ import annotations

import math

from ai2.confidence import calibrated
from ai2.coverage import coverage
from ai2.interpret import CandidatePlanner
from ai2.normalize import normalize
from ai2.reference import outside_scope
from ai2.scope import screen

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
    """The production configuration is the default. The switches exist only so tests can ablate one stage at a time
    (for example reference_check=False); the service never changes them."""

    def __init__(self, planner, k: int | None = None, normalize_input: bool = True, coverage_check: bool = True,
                 clarify: bool = True, reference_check: bool = True):
        self.reference_check = reference_check
        self.planner, self.k = planner, k
        self.normalize_input, self.coverage_check, self.clarify = normalize_input, coverage_check, clarify
        self.cand = CandidatePlanner(planner, k=None)

    def interpret(self, question) -> dict:
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
            c["calibrated_probability"] = round(q, 6)  # temperature-scaled (ai2.confidence)
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
            plan = {"kind": "query", "query": win["query"], "frame": win["frame"]}
            return {**base, "outcome": "plan", "stage": "planner", "reason": None, "plan": plan}
        options = [{"query": c["query"], "frame": c["frame"], "text": describe(c["query"]),
                    "probability": c["probability"]} for c in consistent]
        reason = "UNEXPLAINED_WORDS" if len(consistent) == 1 else "SEVERAL_READINGS"
        return {**base, "outcome": "clarify", "stage": "clarify", "reason": reason, "options": options}


def _finish(snapshot, interp: dict) -> dict:
    from ai2.answering import _answer
    out = _answer(snapshot, {"screen": interp["screen"], "plan": interp["plan"]})
    return {**interp, "outcome": out["outcome"], "stage": out["stage"], "reason": out["reason"],
            "claim": out["claim"], "verification": out["verification"]}


def answer_with_snapshot_v2(snapshot, question, v2: V2Planner) -> dict:
    interp = v2.interpret(question)
    if interp["outcome"] != "plan":
        return {**interp, "claim": None, "verification": None}
    return _finish(snapshot, interp)
