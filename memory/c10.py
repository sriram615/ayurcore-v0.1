"""
C10 as a separate, clarification-only wrapper (AI-2 v3), usable on any planner with .interpret -- in particular
the frozen Block 2A, which has no C10 of its own. A plan for a bare amount ("Neem grams?": an amount word and a
herb, nothing saying total / count / list / change / which record) becomes a clarification between the total
and the latest grams. It only ever turns a plan into a clarification. Same rule as memory.v2._bare_amount.
"""
from __future__ import annotations

C10_BLOCKERS = {"TOTAL", "QUANT", "LIST", "COUNT", "CHANGE", "FIRST", "LAST", "PREVIOUS", "LASTPREV", "WHEN"}
C10_FRAMES = ("SUM_HERB", "LAST_HERB", "FIRST_HERB")


def bare_amount(el: dict, frame: str) -> bool:
    cues = set(el["cues"])
    return "AMOUNT" in cues and not cues & C10_BLOCKERS and bool(el["slots"]["herbs"]) and frame in C10_FRAMES


class C10Arm:
    def __init__(self, base):
        self.base = base

    def interpret(self, question) -> dict:
        out = self.base.interpret(question)
        if out["outcome"] != "plan" or out["plan"]["frame"] not in C10_FRAMES:
            return out
        from memory.interpret import _query, analyse
        from memory.v2 import describe
        el = analyse(out["interpretation"]["text"])
        if not bare_amount(el, out["plan"]["frame"]):
            return out
        options = [(f, _query(f, el)) for f in ("SUM_HERB", "LAST_HERB")]
        return {**out, "outcome": "clarify", "stage": "clarify", "reason": "UNDERSPECIFIED", "plan": None,
                "options": [{"query": q, "frame": f, "text": describe(q), "probability": None} for f, q in options]}
