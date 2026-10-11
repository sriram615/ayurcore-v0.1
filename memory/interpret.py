"""
Candidate planning with deterministic intent / alignment checks (AI-2 Block 5).

    CandidatePlanner(planner, k=5).plan(question) -> the same plan dict as Planner.plan, plus
        "candidates": [{frame, query, score, rank, consistent, unexplained, unsupported}, ...]
        "decision":   "top_candidate" | "repaired" | "no_aligned_plan" | "ambiguous" | "planner_abstain"

It replaces "take the planner's single best frame" with:
  1. candidates (learned): the existing planner's factor scores (op + target + selector) rank every
     frame of the closed vocabulary whose content slots match the question exactly (one herb if the
     frame needs one, ...); the top K are kept (k=None: all of them). If the best operation is DIAGNOSIS,
     TREATMENT or UNAVAILABLE, the plan abstains as before. A learned AMBIGUOUS operation is not
     final: ambiguity is decided by the deterministic checks below.
  2. alignment (deterministic): closed cue lexicons (English / Manglish / Malayalam) find the
     question's semantic elements -- operation words (count / amount / total / list / change / when),
     selector words (first / last / previous), target words (prescription / herb / dosha / visit),
     units. A candidate is CONSISTENT only if
       - every element is explained by it (e.g. "grams" needs sum, compare-herb or first/last-herb;
         "first" needs op first or selector first; "visits" as head noun needs the encounter target), and
       - every part of it is supported by some element (op count needs a count word; selector first
         needs a first word; target encounter needs a visit word ...).
     A candidate that leaves an element unexplained, or has an unsupported part, is rejected.
  3. decision: exactly one consistent candidate (one distinct query) -> that plan; none -> ABSTAIN /
     AMBIGUOUS; two or more materially different consistent candidates -> ABSTAIN / AMBIGUOUS.
     Candidate scores never break a tie between consistent candidates.

Like the planner, this never touches patient data: it does not import the executor, the snapshot,
the database or any network client. Retrieval, RBAC, clinic isolation, evidence and the verifier are
the unchanged memory.retrieval functions (pass a CandidatePlanner where a Planner is expected).
"""
from __future__ import annotations

import re

from memory.planner import ABSTAIN, FRAMES, NUMBER_WORDS, Planner, delexicalise, features, tokenize

K = 5

# --- closed cue lexicons: exact tokens, token prefixes (Manglish / Malayalam inflection), phrases ---
CUES = {
    "COUNT": ({"count", "counts", "often", "times", "number", "എണ്ണം", "thavana", "തവണ",
               "pravashyam", "പ്രാവശ്യം", "ennam"}, (), (r"\bhow many\b", r"\bin how many\b")),  # 4B C9
    "AMOUNT": ({"grams", "gram", "g", "gm", "gms", "amount", "quantity", "much", "dose", "dosage", "ഗ്രാം", "alavu", "അളവ്"},
               ("ഗ്രാമ",), (r"\bhow many (grams?|g|gms?)\b",)),
    "TOTAL": ({"total", "sum", "cumulative", "altogether", "combined", "overall", "aake", "ആകെ", "motham", "മൊത്തം",
               "summed"},  # 4B C9
              (), (r"\badd up\b", r"\bin all\b")),
    "QUANT": ({"ethra", "എത്ര"}, (), ()),
    "LIST": ({"list", "all", "every", "each", "ever", "history", "ella", "ellam", "ethokke", "oro", "എല്ലാ", "ഏതൊക്കെ",
              "ഓരോ", "ororo"},  # 4B C9: ororo
             ("ellaa", "orooro", "എല്ലാ", "ഓരോ"), (r"\bover time\b", r"\bso far\b", r"\bto date\b")),
    "CHANGE": ({"compare", "compared", "comparison", "versus", "vs", "change", "changed", "changes", "differ", "differs",
                "different", "difference", "differences", "same", "unchanged", "shifted", "shift", "more", "less",
                "higher", "lower", "increased", "decreased", "reduced", "raised", "lowered", "maari", "maariyo",
                "maattam", "kooduthalo", "kuravo", "koodiyo", "kuranjo", "vyathyasam", "മാറ്റം", "മാറിയോ", "മാറി",
                "കൂടിയോ", "കുറഞ്ഞോ", "വ്യത്യാസം", "climbed", "climb", "drop", "dropped"},  # 4B C9
               ("മാറ്റ", "മാറി", "vyathyas", "compare"), (r"\b(go|gone|went|goes) (up|down)\b", r"\bup or down\b")),
    "WHEN": ({"when", "date", "dates", "day", "eppol", "eppozhaanu", "eppozhaayirunnu", "eppozha", "എപ്പോൾ"},
             ("eppozh", "എപ്പോ", "തീയതി"), (r"\bethu divasam\b",)),  # 4B C4: "which day" is a date
    "FIRST": ({"first", "earliest", "initial", "initially", "original", "originally", "beginning", "start", "started",
               "starting", "began", "begin", "aadyam", "aadyathe", "aadyamaayi", "തുടക്കം"},
              ("aadya", "ആദ്യ", "തുടക്ക"), (r"\bat the start\b",)),
    "LAST": ({"last", "latest", "recent", "recently", "current", "currently", "now", "today", "present", "newest",
              "avasanam", "avasanathe", "ippol", "ippozhathe", "ippo", "ഇപ്പോൾ", "avasaanam"},
             ("avasana", "അവസാന", "ഇപ്പോഴ", "ippozh"),
             (r"\bmost recent(ly)?\b", r"\bthese days\b", r"\bright now\b", r"\bat present\b")),  # 4B C3
    "PREVIOUS": ({"previous", "previously", "prior", "before", "preceding", "earlier"}, (), (r"\blast time\b", r"\bone before\b")),
    "LASTPREV": ({"kazhinja", "കഴിഞ്ഞ"}, ("kazhinja", "കഴിഞ്ഞ"), ()),
    "RX_NOUN": ({"prescription", "prescriptions", "rx", "kurippadi", "script", "scripts"},  # 4B C9
                ("prescription", "കുറിപ്പടി", "kurippadi"), ()),
    "RX_VERB": ({"prescribed", "prescribe", "given", "give", "gave", "received", "receive", "got", "get", "koduthu",
                 "koduthathu", "kodutha", "koduthittund", "on", "taking", "kittiya", "കിട്ടിയ"},  # 4B C9
                ("കൊടുത്ത", "koduth"), ()),
    "HERB_NOUN": ({"herb", "herbs", "medicine", "medicines", "drug", "drugs", "marunnu", "marunnukal"},
                  ("മരുന്ന", "marunn"), ()),
    "DOSHA_NOUN": ({"dosha", "doshas", "dosham", "ദോഷം"}, ("ദോഷ", "dosham"), ()),
    "ENC": ({"visit", "visits", "visited", "encounter", "encounters", "consultation", "consultations", "appointment",
             "appointments", "came", "come", "comes", "seen", "vannu", "vannathu", "വന്നു", "vannittund", "ethi"},  # C9
            ("visit", "സന്ദർശന", "consultation", "encounter"), ()),
}
# words that make a following visit word a time anchor ("at the first visit") rather than the head noun
ANCHOR_BEFORE = {"first", "last", "latest", "previous", "earliest", "recent", "current", "initial", "each", "every",
                 "that", "this", "same", "NUM", "aadyathe", "avasanathe", "kazhinja", "oro", "ആദ്യ", "അവസാന", "കഴിഞ്ഞ"}
UNIT_AFTER_HOW_MANY = {"grams", "gram", "g", "gm", "gms"}
PAIR_WORDS = {"last", "latest", "recent", "avasanathe"}  # "last two" = previous and last
PRONOUN_ONE_BEFORE = {"the", "first", "last", "previous", "latest", "that", "this", "original", "earlier", "current",
                      "every", "each", "any", "which"}  # "every one of": a pronoun, not a quantity


def _find(tokens: list[str], fills: dict | None = None) -> dict:
    """{cue category: [token index, ...]} -- phrases are mapped to the index of their first token.
    fills: optional {token index: category} from the Block 5 cue tagger, for words no lexicon knows; they are
    merged AFTER the lexicons, so a lexicon match is never changed."""
    text = " ".join(tokens)
    starts, pos = [], 0
    for t in tokens:
        starts.append(pos)
        pos += len(t) + 1
    found = {}
    for cat, (words, prefixes, phrases) in CUES.items():
        idx = [i for i, t in enumerate(tokens) if t in words or (prefixes and t.startswith(prefixes))]
        for p in phrases:
            for m in re.finditer(p, text):
                if m.start() in starts:
                    idx.append(starts.index(m.start()))
        if idx:
            found[cat] = sorted(set(idx))
    # "give me / give us" asks the system, not about what was given
    if "RX_VERB" in found:
        last_word = max((i for i, t in enumerate(tokens) if any(ch.isalnum() for ch in t)), default=-1)
        found["RX_VERB"] = [i for i in found["RX_VERB"] if not (tokens[i] == "give" and i + 1 < len(tokens)
                                                                 and tokens[i + 1] in ("me", "us"))
                            and not (tokens[i] == "on" and i != last_word)]  # 4B C1: only "currently on?"
        if not found["RX_VERB"]:
            del found["RX_VERB"]
    # "how many grams" is an amount, not a count
    if "COUNT" in found:
        found["COUNT"] = [i for i in found["COUNT"]
                          if not (tokens[i] == "how" and i + 2 < len(tokens) and tokens[i + 2] in UNIT_AFTER_HOW_MANY)
                          and not (tokens[i] == "many" and i + 1 < len(tokens) and tokens[i + 1] in UNIT_AFTER_HOW_MANY)]
        if not found["COUNT"]:
            del found["COUNT"]
    known = {i for idx in found.values() for i in idx}
    for i, cat in (fills or {}).items():
        if i not in known:
            found[cat] = sorted(set(found.get(cat, [])) | {i})
    return found


def analyse(question: str, fills: dict | None = None) -> dict:
    """Deterministic question elements: delexicalised tokens, slots, cues, and number roles."""
    raw = tokenize(question)
    masked, slots = delexicalise(question)
    cues = _find(raw, fills)
    # number roles, on the delexicalised tokens: "the one before" is a pronoun; "last two" a selector pair
    numbers, pair_two, values = [], False, iter(slots["numbers"])
    for i, t in enumerate(masked):
        if t != "NUM":
            continue
        value = next(values)
        prev = masked[i - 1] if i else ""
        nxt = masked[i + 1] if i + 1 < len(masked) else ""
        if value == 1 and (prev in PRONOUN_ONE_BEFORE or nxt == "before"):
            continue  # "the one before", "the first one": not a quantity
        if value == 2 and (prev in PAIR_WORDS or nxt in PAIR_WORDS or nxt == "most"):  # 4B C8: "two latest"
            pair_two = True
        numbers.append(value)
    # visit words: anchors ("at the first visit", "latest three visits") vs head nouns ("list the visits")
    anchored = []
    for i in cues.get("ENC", []):
        before = {("NUM" if (w.isdigit() or w in NUMBER_WORDS) else w) for w in raw[max(0, i - 2):i]}
        anchored.append(bool(before & ANCHOR_BEFORE) or raw[i].startswith(("visitil", "സന്ദർശനത്തി"))
                        or (i + 1 < len(raw) and raw[i + 1] == "before"))
    # the same herb named twice ("Pippali ... with Pippali") is one herb slot, not two
    herbs = list(dict.fromkeys(slots["herbs"]))
    return {"tokens": masked, "slots": {"herbs": herbs, "doshas": slots["doshas"], "numbers": numbers},
            "all_numbers": slots["numbers"], "cues": cues, "pair_two": pair_two,
            "enc_all_anchored": bool(anchored) and all(anchored)}


def _slots_fit(frame: str, el: dict) -> bool:
    op, _, a, _, needs = FRAMES[frame]
    s = el["slots"]
    numbers = len(s["numbers"])
    if op in ("diff", "compare") and a == "previous" and el["pair_two"] and s["numbers"] == [2]:
        numbers = 0  # "the last two" names the previous / last pair
    want = {"h": len(s["herbs"]), "d": len(s["doshas"]), "n": numbers}
    return all(want[x] == (1 if x in needs else 0) for x in "hdn")


def align(frame: str, el: dict) -> tuple[list[str], list[str]]:
    """(unexplained question elements, unsupported candidate parts) for one candidate frame."""
    op, target, a, _, needs = FRAMES[frame]
    c = set(el["cues"])
    s = el["slots"]
    window = "n" in needs
    unexplained, unsupported = [], []

    def explain(cat, ok):
        if cat in c and not ok:
            unexplained.append(cat)

    explain("COUNT", op == "count")
    explain("AMOUNT", op == "sum" or (target == "herb" and op in ("compare", "first", "last")))
    explain("TOTAL", op == "sum" or (op == "count" and "COUNT" in c))
    explain("QUANT", op in ("count", "sum") or (op == "compare" and target == "herb"))
    explain("LIST", op in ("list", "count", "sum"))
    explain("CHANGE", op in ("diff", "compare"))
    explain("WHEN", op in ("first", "last") or (op == "list" and target == "encounter"))
    explain("FIRST", op == "first" or a == "first")
    explain("LAST", op == "last" or op in ("diff", "compare") or window)
    explain("PREVIOUS", a == "previous")
    explain("LASTPREV", op == "last" or a == "previous" or window)
    explain("RX_NOUN", target in ("prescription", "herb", "dosha"))
    explain("RX_VERB", target in ("prescription", "herb", "dosha"))
    explain("HERB_NOUN", target == "herb" or (target == "prescription" and (
        "RX_NOUN" in c or ("RX_VERB" in c and op in ("first", "last")))))  # 4B C2: "which herbs is she on?"
    explain("DOSHA_NOUN", target == "dosha")
    explain("ENC", target == "encounter" or target in ("herb", "dosha")
            or (target == "prescription" and (el["enc_all_anchored"] or op == "diff")))

    need = {
        "first": "FIRST" in c, "last": bool(c & {"LAST", "LASTPREV"}),
        "count": bool(c & {"COUNT", "QUANT"}), "sum": bool(c & {"AMOUNT", "TOTAL", "QUANT"}),
        "list": "LIST" in c or _plural_head(el), "diff": "CHANGE" in c, "compare": "CHANGE" in c,
    }
    if not need[op]:
        unsupported.append("op:" + op)
    has = {
        "herb": bool(s["herbs"]) or "HERB_NOUN" in c,
        "dosha": bool(s["doshas"]) or "DOSHA_NOUN" in c,
        "encounter": "ENC" in c,
        "prescription": bool(c & {"RX_NOUN", "RX_VERB"}) or op == "diff",
    }
    if not has[target]:
        unsupported.append("target:" + target)
    if a == "first" and "FIRST" not in c:
        unsupported.append("selector:first")
    if a == "previous" and not (c & {"PREVIOUS", "LASTPREV", "LAST"} or el["pair_two"]):
        unsupported.append("selector:previous")
    return unexplained, unsupported


def _plural_head(el: dict) -> bool:
    return any(t in ("herbs", "doshas", "visits", "prescriptions", "encounters", "consultations", "medicines",
                     "marunnukal") for t in el["tokens"])


def _query(frame: str, el: dict) -> dict:
    op, target, a, b, needs = FRAMES[frame]
    s = el["slots"]
    return {"op": op, "target": target, "herb": s["herbs"][0] if "h" in needs else None,
            "dosha": s["doshas"][0] if "d" in needs else None, "last_n": s["numbers"][0] if "n" in needs else None,
            "a": a or "previous", "b": b or "last"}


class CandidatePlanner:
    """Top-K candidate plans from the existing planner, filtered by deterministic alignment."""

    def __init__(self, planner: Planner, k: int | None = K):  # k=None: every slot-compatible frame
        self.planner, self.k = planner, k

    def candidates(self, question: str, fills: dict | None = None) -> tuple[dict, list[dict], str]:
        el = analyse(question, fills)
        feats = features(el["tokens"])
        sc = {name: self.planner._scores(name, feats) for name in ("op", "target", "selector")}
        best_op = max(sc["op"], key=lambda o: (sc["op"][o], -list(sc["op"]).index(o)))
        ranked = []
        for frame, (op, target, a, _, _) in FRAMES.items():
            if not _slots_fit(frame, el):
                continue
            score = sc["op"][op] + sc["target"][target] + (sc["selector"][a] if a else 0.0)
            ranked.append((-score, frame))
        ranked.sort()
        out = []
        for rank, (neg, frame) in enumerate(ranked if self.k is None else ranked[: self.k]):
            unexplained, unsupported = align(frame, el)
            out.append({"frame": frame, "query": _query(frame, el), "score": round(-neg, 6), "rank": rank,
                        "consistent": not unexplained and not unsupported, "unexplained": unexplained,
                        "unsupported": unsupported})
        return el, out, best_op

    def plan(self, question: str) -> dict:
        if not isinstance(question, str) or not question.strip():
            return {"kind": "abstain", "reason": "AMBIGUOUS", "frame": None, "slots": {}, "candidates": [],
                    "decision": "no_aligned_plan"}
        el, cands, best_op = self.candidates(question)
        base = {"slots": el["slots"], "cues": {k: len(v) for k, v in el["cues"].items()}, "candidates": cands}
        if best_op in ABSTAIN and best_op != "AMBIGUOUS":
            return {"kind": "abstain", "reason": best_op, "frame": best_op, "decision": "planner_abstain", **base}
        consistent = [c for c in cands if c["consistent"]]
        distinct = {tuple(sorted(c["query"].items())) for c in consistent}
        if len(distinct) == 1:
            win = consistent[0]
            decision = "top_candidate" if win["rank"] == 0 else "repaired"
            return {"kind": "query", "query": win["query"], "frame": win["frame"], "decision": decision, **base}
        decision = "no_aligned_plan" if not consistent else "ambiguous"
        return {"kind": "abstain", "reason": "AMBIGUOUS", "frame": consistent[0]["frame"] if consistent else None,
                "decision": decision, **base}
