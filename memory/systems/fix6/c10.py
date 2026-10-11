"""
C10 as a separate, clarification-only wrapper (AI-2 v3), usable on any planner with .interpret -- in particular
the frozen Block 2A, which has no C10 of its own. A plan for a bare amount ("Neem grams?": an amount word and a
herb, nothing saying total / count / list / change / which record) becomes a clarification between the total
and the latest grams. It only ever turns a plan into a clarification. Same rule as memory.v2._bare_amount.

Fix 6 (memory/systems/fix6, ambiguity handling; evaluation protocol v1.1, AM01 and AM10) keeps that wrapper and adds two things, both of
which still only turn a plan into a clarification:
  1. "how many grams" is a bare amount. In Manglish / Malayalam the interrogative is its own word ("Neem ethra gram?", "അര്യവേപ്പ് എത്ര ഗ്രാം?");
     it is not a blocker when it stands directly beside the gram word, exactly as the English "how many grams" (which has no such word).
  2. A bare count of a herb ("Neem count?", "How many Neem?", "Neem count ethra?") is the number of prescriptions containing it or the total
     grams (AM10 readings COUNT_HERB, SUM_HERB). It is bare only if nothing names the record being counted (prescription / visit / "given" /
     "times" / "often"), the quantity of a first / last / change / list, or a time. "How many prescriptions included Neem?" is a clear count.
"""
from __future__ import annotations

C10_BLOCKERS = {"TOTAL", "QUANT", "LIST", "COUNT", "CHANGE", "FIRST", "LAST", "PREVIOUS", "LASTPREV", "WHEN"}
C10_FRAMES = ("SUM_HERB", "LAST_HERB", "FIRST_HERB")
# fix 6 (2): anything that says WHAT is counted, or turns the count into another question
COUNT_BLOCKERS = {"RX_NOUN", "RX_VERB", "ENC", "HERB_NOUN", "DOSHA_NOUN", "LIST", "TOTAL", "AMOUNT", "CHANGE", "ADDREM", "ORDER", "FIRST",
                  "LAST", "PREVIOUS", "LASTPREV", "WHEN"}
BARE_COUNT_WORDS = {"count", "counts", "number", "how", "many", "എണ്ണം"}  # not "times" / "often" / "thavana" / "തവണ": those count occasions


def _beside(el: dict, a: str, b: str) -> bool:
    """Every token of cue category `a` stands directly next to a token of category `b`."""
    near = set(el["cues"].get(b, ()))
    return bool(el["cues"].get(a)) and all({i - 1, i + 1} & near for i in el["cues"][a])


def bare_amount(el: dict, frame: str) -> bool:
    cues = set(el["cues"])
    blockers = C10_BLOCKERS - ({"QUANT"} if _beside(el, "QUANT", "AMOUNT") else set())   # fix 6 (1)
    return "AMOUNT" in cues and not cues & blockers and bool(el["slots"]["herbs"]) and frame in C10_FRAMES


def bare_count(el: dict, frame: str) -> bool:
    cues = set(el["cues"])
    if frame != "COUNT_HERB" or "COUNT" not in cues or not el["slots"]["herbs"] or el["slots"]["numbers"]:
        return False
    blockers = COUNT_BLOCKERS - set()
    if cues & blockers or any(t in (",", ";") for t in el["tokens"]):
        return False
    if "QUANT" in cues and not _beside(el, "QUANT", "COUNT"):
        return False
    return all(el["tokens"][i] in BARE_COUNT_WORDS for i in el["cues"]["COUNT"])


class C10Arm:
    def __init__(self, base):
        self.base = base

    def interpret(self, question) -> dict:
        out = self.base.interpret(question)
        if out["outcome"] != "plan":
            return out
        frame = out["plan"]["frame"]
        from memory.interpret import _query, analyse
        from memory.v2 import describe
        el = analyse(out["interpretation"]["text"])
        if frame in C10_FRAMES and bare_amount(el, frame):
            frames = ("SUM_HERB", "LAST_HERB")
        elif frame == "COUNT_HERB" and bare_count(el, frame):
            frames = ("COUNT_HERB", "SUM_HERB")
        else:
            return out
        options = [(f, _query(f, el)) for f in frames]
        return {**out, "outcome": "clarify", "stage": "clarify", "reason": "UNDERSPECIFIED", "plan": None,
                "options": [{"query": q, "frame": f, "text": describe(q), "probability": None} for f, q in options]}
