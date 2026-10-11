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

Fix 2 (memory/systems/fix2, executor-native vocabulary): additions only -- no new operation, frame or slot kind. Each new cue
maps to a concept the executor already computes: ADDREM (added / removed / dropped / newly / "is new" and their Manglish forms)
is a diff of the previous and the latest prescription (diff prescription; compare herb with the previous selector); ORDER
(oldest first / chronological / in order and their Manglish and Malayalam forms) is the order every list operation already
returns; "most recently" is the LAST selector; "ethra gram" is the amount of a first / last herb; the Manglish spelling
"moonnu" is the number word the planner spells "moonu". Everything else is unchanged.

Fix 5 (memory/systems/fix5, Malayalam vocabulary): additions only, each mapped to an existing operation. "ഏറ്റവും പുതിയ" (most recent) is the
LAST selector; "മുമ്പത്തെ" (previous) the PREVIOUS selector; "അപേക്ഷിച്ച്" / "താരതമ്യം" (compared with / comparison) the CHANGE cue of diff and
compare; "പുതുതായി" / "ചേർത്തത്" / "ഒഴിവാക്കിയത്" (newly / added / omitted) the ADDREM cue of diff; a herb name carrying the Malayalam
possessive ending ("അശ്വഗന്ധയുടെ", "അര്യവേപ്പിന്റെ") is the herb slot, as in "Neem's dose". No frame, operation or slot kind is added.

Fix 7 (memory/systems/fix7, remaining Manglish coverage): additions only, each a spelling variant of a word already in a lexicon:
"ethellaam" (which all) beside "ethokke" in LIST; "ennam" (number) and "ethrayennathil" (in how many) beside "thavana" / "number" in COUNT;
"vyathyaasam" beside "vyathyasam" in CHANGE; "ettavum puthiya" (most recent) beside "most recent" in LAST. No frame or operation is added.
The Malayalam "ഏറ്റവും പുതിയ" (fix 5) and the Manglish "ettavum puthiya" are not the LAST selector when a person noun follows ("the newest patient's ..."):
that is a question about another patient, so the phrase is left unrecognised and the question is not answered or clarified as one about this patient.

Fix 8a (memory/systems/fix8a, safe vocabulary): two LIST phrases that ask for the existing list operation -- "walk (me) through" and
"prescription by prescription" / "visit by visit" (X by X over a record noun). Nothing else in this module changes.

Fix 8b (memory/systems/fix8b, ambiguity candidate filter): a bare question such as "Her dosha?" has five under-specified readings (first, last,
list, and two compare readings the question never asks for), which is above the limit of three, so it was abstained. When the question has no
change cue, the compare / diff readings are dropped, but ONLY if that brings the count into 1..3 and every word of the question is accounted
for (see _drop_change_readings); in every other case the candidates are unchanged. No cue, lexicon, frame or decision rule changes.

Like the planner, this never touches patient data: it does not import the executor, the snapshot,
the database or any network client. Retrieval, RBAC, clinic isolation, evidence and the verifier are
the unchanged memory.retrieval functions (pass a CandidatePlanner where a Planner is expected).
"""
from __future__ import annotations

import re

from backend.herb_resolver import HERB_ALIASES
from memory.planner import ABSTAIN, FRAMES, NUMBER_WORDS, Planner, delexicalise, features, tokenize

K = 5

# --- closed cue lexicons: exact tokens, token prefixes (Manglish / Malayalam inflection), phrases ---
CUES = {
    "COUNT": ({"count", "counts", "often", "times", "number", "എണ്ണം", "thavana", "തവണ", "ennam", "ethrayennathil"}, (), (r"\bhow many\b", r"\bin how many\b")),
    "AMOUNT": ({"grams", "gram", "g", "gm", "gms", "amount", "quantity", "much", "dose", "dosage", "ഗ്രാം", "alavu", "അളവ്"},
               ("ഗ്രാമ",), (r"\bhow many (grams?|g|gms?)\b",)),
    "TOTAL": ({"total", "sum", "cumulative", "altogether", "combined", "overall", "aake", "ആകെ", "motham", "മൊത്തം"},
              (), (r"\badd up\b", r"\bin all\b")),
    "QUANT": ({"ethra", "എത്ര"}, (), ()),
    "LIST": ({"list", "all", "every", "each", "ever", "history", "ella", "ellam", "ethokke", "ethellaam", "oro", "എല്ലാ", "ഏതൊക്കെ",
              "ഓരോ"}, ("ellaa", "orooro", "എല്ലാ", "ഓരോ"), (r"\bover time\b", r"\bso far\b", r"\bto date\b", r"\bwalk (me |us )?through\b",
                                                                       r"\b(prescription|visit|encounter|herb)s? by (prescription|visit|encounter|herb)s?\b")),
    "CHANGE": ({"compare", "compared", "comparison", "versus", "vs", "change", "changed", "changes", "differ", "differs",
                "different", "difference", "differences", "same", "unchanged", "shifted", "shift", "more", "less",
                "higher", "lower", "increased", "decreased", "reduced", "raised", "lowered", "maari", "maariyo",
                "maattam", "kooduthalo", "kuravo", "koodiyo", "kuranjo", "vyathyasam", "vyathyaasam", "മാറ്റം", "മാറിയോ", "മാറി",
                "കൂടിയോ", "കുറഞ്ഞോ", "വ്യത്യാസം", "അപേക്ഷിച്ച്", "താരതമ്യം"},
               ("മാറ്റ", "മാറി", "vyathyas", "compare"), (r"\b(go|gone|went|goes) (up|down)\b", r"\bup or down\b")),
    "WHEN": ({"when", "date", "dates", "day", "eppol", "eppozhaanu", "eppozhaayirunnu", "eppozha", "എപ്പോൾ"},
             ("eppozh", "എപ്പോ", "തീയതി"), ()),
    "FIRST": ({"first", "earliest", "initial", "initially", "original", "originally", "beginning", "start", "started",
               "starting", "began", "begin", "aadyam", "aadyathe", "aadyamaayi", "തുടക്കം"},
              ("aadya", "ആദ്യ", "തുടക്ക"), (r"\bat the start\b",)),
    "LAST": ({"last", "latest", "recent", "recently", "current", "currently", "now", "today", "present", "newest",
              "avasanam", "avasanathe", "ippol", "ippozhathe", "ippo", "ഇപ്പോൾ"},
             ("avasana", "അവസാന", "ഇപ്പോഴ", "ippozh"), (r"\bmost recent\b", r"\bmost recently\b",
                                                                              "ഏറ്റവും പുതിയ(?! (രോഗി|ആൾ|ആള|വ്യക്തി))", r"\bettavum puthiya\b(?! (rogi|aal|vyakthi|patient))")),
    "PREVIOUS": ({"previous", "previously", "prior", "before", "preceding", "earlier", "munpathe", "മുമ്പത്തെ"}, (), (r"\blast time\b", r"\bone before\b")),
    "LASTPREV": ({"kazhinja", "കഴിഞ്ഞ"}, ("kazhinja", "കഴിഞ്ഞ"), ()),
    "RX_NOUN": ({"prescription", "prescriptions", "rx", "kurippadi"}, ("prescription", "കുറിപ്പടി", "kurippadi"), ()),
    "RX_VERB": ({"prescribed", "prescribe", "given", "give", "gave", "received", "receive", "got", "get", "koduthu",
                 "koduthathu", "kodutha", "koduthittund", "on"}, ("കൊടുത്ത", "koduth"), ()),
    "HERB_NOUN": ({"herb", "herbs", "medicine", "medicines", "drug", "drugs", "marunnu", "marunnukal"},
                  ("മരുന്ന", "marunn"), ()),
    "DOSHA_NOUN": ({"dosha", "doshas", "dosham", "ദോഷം"}, ("ദോഷ", "dosham"), ()),
    "ADDREM": ({"added", "removed", "dropped", "newly", "cherthath", "cherthathu", "ozhivaakki", "ozhivaakkiyath",
                "puthuthayi", "പുതുതായി", "ചേർത്തത്", "ഒഴിവാക്കിയത്"}, (), (r"\bdropped off\b", r"\b(is|are|was|were) new\b")),
    "ORDER": (set(), (), (r"\bchronolog\w*( order)?\b", r"\bin (the )?order\b", r"\boldest (first|to (newest|latest|last|most recent))\b",
                           r"\bfrom (the )?oldest\b", r"\bpazhayathu muthal\b", r"\b(aadyam muthal )?order ?- ?il\b",
                           "പഴയത് മുതൽ", "ക്രമത്തിൽ")),
    "ENC": ({"visit", "visits", "visited", "encounter", "encounters", "consultation", "consultations", "appointment",
             "appointments", "came", "come", "comes", "seen", "vannu", "vannathu", "വന്നു", "vannittund"},
            ("visit", "സന്ദർശന", "consultation", "encounter"), ()),
}
# words that make a following visit word a time anchor ("at the first visit") rather than the head noun
ANCHOR_BEFORE = {"first", "last", "latest", "previous", "earliest", "recent", "current", "initial", "each", "every",
                 "that", "this", "same", "NUM", "aadyathe", "avasanathe", "kazhinja", "oro", "ആദ്യ", "അവസാന", "കഴിഞ്ഞ"}
UNIT_AFTER_HOW_MANY = {"grams", "gram", "g", "gm", "gms"}
PAIR_WORDS = {"last", "latest", "recent", "avasanathe"}  # "last two" = previous and last
PRONOUN_ONE_BEFORE = {"the", "first", "last", "previous", "latest", "that", "this", "original", "earlier", "current",
                      "every", "each", "any", "which"}  # "every one of": a pronoun, not a quantity


DISPLAY_OBJECTS = {"visit", "visits", "history", "dosha", "doshas", "record", "records", "list", "timeline", "summary",
                   "details"}
GIVE_SKIP = {"the", "a", "an", "all", "her", "his", "their", "its", "this", "that", "each", "every"}
NUMBER_SPELLING = {"moonnu": "moonu"}  # Manglish spelling of a number word the planner already knows


ML_POSSESSIVE = ("യുടെ", "ിന്റെ")  # fix 5: "അശ്വഗന്ധയുടെ" (of Ashwagandha), "അര്യവേപ്പിന്റെ" (of Neem)
ML_WORD = re.compile("[ഀ-ൿ\u200c\u200d]+")


def _herb_base(word: str) -> str:
    """The herb alias a possessive Malayalam token is built on ("അര്യവേപ്പിന്റെ" -> "അര്യവേപ്പ്"); any other token is returned unchanged."""
    if word in HERB_ALIASES:
        return word
    for suffix in ML_POSSESSIVE:
        if word.endswith(suffix) and len(word) > len(suffix):
            stem = word[: -len(suffix)]
            for candidate in (stem, stem + "്"):
                if candidate in HERB_ALIASES:
                    return candidate
    return word


def _canon_numbers(question: str) -> str:
    text = re.sub(r"\b(" + "|".join(NUMBER_SPELLING) + r")\b", lambda m: NUMBER_SPELLING[m.group(1)], question.lower())
    return ML_WORD.sub(lambda m: _herb_base(m.group(0)), text)


def _give_displays(tokens: list[str], i: int) -> bool:
    """"give the visit history", "give her dosha at each ...": a request to show the record, not a dispensing."""
    j = i + 1
    while j < len(tokens) and tokens[j] in GIVE_SKIP:
        j += 1
    return j < len(tokens) and tokens[j] in DISPLAY_OBJECTS


def _find(tokens: list[str]) -> dict:
    """{cue category: [token index, ...]} -- phrases are mapped to the index of their first token."""
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
    # an ordering phrase ("oldest first", "from oldest to newest", "aadyam muthal ... order-il") explains the first / last
    # words inside it: the list operations already return oldest first
    if "ORDER" in found:
        covered = set()
        for p in CUES["ORDER"][2]:
            for m in re.finditer(p, text):
                covered.update(i for i, s in enumerate(starts) if m.start() <= s < m.end())
        for cat in ("FIRST", "LAST"):
            if cat in found:
                found[cat] = [i for i in found[cat] if i not in covered]
                if not found[cat]:
                    del found[cat]
    # "give me / give us" asks the system, not about what was given
    if "RX_VERB" in found:
        found["RX_VERB"] = [i for i in found["RX_VERB"] if not (tokens[i] == "give" and i + 1 < len(tokens)
                                                                 and (tokens[i + 1] in ("me", "us") or _give_displays(tokens, i)))]
        if not found["RX_VERB"]:
            del found["RX_VERB"]
    # "how many grams" is an amount, not a count
    if "COUNT" in found:
        found["COUNT"] = [i for i in found["COUNT"]
                          if not (tokens[i] == "how" and i + 2 < len(tokens) and tokens[i + 2] in UNIT_AFTER_HOW_MANY)
                          and not (tokens[i] == "many" and i + 1 < len(tokens) and tokens[i + 1] in UNIT_AFTER_HOW_MANY)]
        if not found["COUNT"]:
            del found["COUNT"]
    return found


def analyse(question: str) -> dict:
    """Deterministic question elements: delexicalised tokens, slots, cues, and number roles."""
    question = _canon_numbers(question)
    raw = tokenize(question)
    masked, slots = delexicalise(question)
    cues = _find(raw)
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
        if value == 2 and prev in PAIR_WORDS:
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
    explain("QUANT", op in ("count", "sum") or (op == "compare" and target == "herb")
            or ("AMOUNT" in c and _amount_quant(el) and target == "herb" and op in ("first", "last")))  # "ethra gram"
    explain("ADDREM", op in ("diff", "compare") and target in ("prescription", "herb") and a == "previous"
            and bool(c & {"LAST", "LASTPREV", "PREVIOUS"}))  # "what was added" needs the latest / previous prescription named
    explain("ORDER", op == "list")
    explain("LIST", op in ("list", "count", "sum"))
    explain("CHANGE", op in ("diff", "compare"))
    explain("WHEN", op in ("first", "last") or (op == "list" and target == "encounter"))
    explain("FIRST", op == "first" or a == "first")
    explain("LAST", op == "last" or op in ("diff", "compare") or window)
    explain("PREVIOUS", a == "previous")
    explain("LASTPREV", op == "last" or a == "previous" or window)
    explain("RX_NOUN", target in ("prescription", "herb", "dosha"))
    explain("RX_VERB", target in ("prescription", "herb", "dosha"))
    explain("HERB_NOUN", target == "herb" or (target == "prescription" and "RX_NOUN" in c))
    explain("DOSHA_NOUN", target == "dosha")
    explain("ENC", target == "encounter" or target in ("herb", "dosha")
            or (target == "prescription" and (el["enc_all_anchored"] or op == "diff")))

    if "AMOUNT" in c and _count_reading(el):  # fix 5: "ethra prescriptions ... ethra gram" is two questions; no single plan answers both
        unexplained.append("COMPOUND")

    need = {
        "first": "FIRST" in c, "last": bool(c & {"LAST", "LASTPREV"}),
        "count": bool(c & {"COUNT", "QUANT"}), "sum": bool(c & {"AMOUNT", "TOTAL", "QUANT"}),
        "list": "LIST" in c or _plural_head(el), "diff": bool(c & {"CHANGE", "ADDREM"}), "compare": bool(c & {"CHANGE", "ADDREM"}),
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


def _amount_quant(el: dict) -> bool:
    """fix 5 tightening of the fix 2 rule: "ethra" asks how many grams only when it is the single quantity word, it does not sit next to a
    prescription / visit noun ("ethra prescriptions" is a count), and the question is one clause (no comma / semicolon)."""
    quant = el["cues"].get("QUANT", ())
    if len(quant) != 1 or any(t in (",", ";") for t in el["tokens"]):
        return False
    near = set(el["cues"].get("RX_NOUN", ())) | set(el["cues"].get("ENC", ()))
    return not ({quant[0] - 1, quant[0] + 1} & near)


def _count_reading(el: dict) -> bool:
    """A quantity word next to a prescription / visit noun ("ethra prescriptions", "എത്ര കുറിപ്പടികൾ") asks for a count."""
    near = set(el["cues"].get("RX_NOUN", ())) | set(el["cues"].get("ENC", ()))
    return any({q - 1, q + 1} & near for q in el["cues"].get("QUANT", ()))


def _plural_head(el: dict) -> bool:
    return any(t in ("herbs", "doshas", "visits", "prescriptions", "encounters", "consultations", "medicines",
                     "marunnukal") for t in el["tokens"])


def _query(frame: str, el: dict) -> dict:
    op, target, a, b, needs = FRAMES[frame]
    s = el["slots"]
    return {"op": op, "target": target, "herb": s["herbs"][0] if "h" in needs else None,
            "dosha": s["doshas"][0] if "d" in needs else None, "last_n": s["numbers"][0] if "n" in needs else None,
            "a": a or "previous", "b": b or "last"}


def _under_specified(c: dict) -> bool:
    """The test memory.v2 applies for a clarification: nothing unexplained, and only an operation or a selector the question does not name."""
    return not c["unexplained"] and bool(c["unsupported"]) and all(u.startswith(("op:", "selector:")) for u in c["unsupported"])


def _drop_change_readings(question: str, el: dict, cands: list[dict]) -> None:
    """fix 8b: rescue a bare question ("Her dosha?") whose readings overflow the clarification limit only because compare / diff readings were counted.

    Applies only if ALL of these hold, otherwise the candidates are left exactly as they were:
      - the question has no change cue (no CHANGE / ADDREM word), so it is not about a change between records;
      - more than MAX_OPTIONS distinct under-specified readings exist now (the question would be abstained);
      - removing the compare / diff readings leaves between 1 and MAX_OPTIONS readings;
      - every word of the question is accounted for (memory.coverage reports nothing unexplained), so an unknown or unsupported word
        ("each patient's", "why") keeps the abstention that the overflow gave.
    The compare / diff candidates then carry the extra unsupported part "no-change-cue:<op>", which the clarification step does not accept."""
    from memory.coverage import coverage
    from memory.v2 import MAX_OPTIONS
    if set(el["cues"]) & {"CHANGE", "ADDREM"}:
        return
    under, seen = [], set()
    for c in cands:
        key = tuple(sorted(c["query"].items()))
        if _under_specified(c) and key not in seen:
            seen.add(key)
            under.append(c)
    if len(under) <= MAX_OPTIONS:
        return
    change = [c for c in under if FRAMES[c["frame"]][0] in ("diff", "compare")]
    if not 1 <= len(under) - len(change) <= MAX_OPTIONS or coverage(question)["unexplained"]:
        return
    for c in change:
        c["unsupported"].append("no-change-cue:" + FRAMES[c["frame"]][0])


class CandidatePlanner:
    """Top-K candidate plans from the existing planner, filtered by deterministic alignment."""

    def __init__(self, planner: Planner, k: int | None = K):  # k=None: every slot-compatible frame
        self.planner, self.k = planner, k

    def candidates(self, question: str) -> tuple[dict, list[dict], str]:
        el = analyse(question)
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
        _drop_change_readings(question, el, out)
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
