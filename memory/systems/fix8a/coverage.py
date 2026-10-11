"""
Coverage check for AI-2 v2: is every meaningful word of the question accounted for? (deterministic)

    coverage(question) -> {"tokens": [...], "unexplained": [...], "roles": [role per token]}

Each word token gets exactly one role:
    slot      a herb name (any alias, 1-3 words), a dosha value or joiner inside one, a number
    cue       a word or phrase of the alignment lexicons (memory.interpret.CUES). The alignment step
              already requires every cue to be explained by the chosen plan
    neutral   a closed list of words that do not change what a memory question asks (articles,
              pronouns for the selected patient, auxiliaries, politeness, "recorded / noted / contain")
    context   a word that is neutral only next to another element: "since / than / between" with a
              change cue, "time" right after a first / last word
    UNSUPPORTED  a closed list of words that change the question into one the fixed plan space cannot
              express (negation, restriction, ordinals, extremes, averages, other people, calendar
              windows). No reading would be right, so memory.v2 abstains instead of clarifying
    UNKNOWN   anything else -- the word is not silently dropped: it is reported as unexplained
Punctuation is ignored. Deliberately NOT neutral (they change meaning and must be noticed): negation
("not", "no", "without"), restriction ("only", "except"), ordinals ("second", "third"), extremes
("largest", "most often"), other people ("patients", "others"), anything the lexicons have never seen.
Fix 2 (memory/systems/fix2, executor-native vocabulary) adds, and removes nothing: "across" is a neutral preposition; "ilekkal" (Manglish
"than / compared with") is a change-context word like "than"; a cue found by a phrase ("gone up or down") now registers its category, so
"since" next to it is context; "only / exactly / pure / maathram" next to a dosha in a COUNT question is the exact-dosha count the executor
already computes; "closed / ended / still / yet" (and "close / end" next to a WHEN word, "what time") next to a visit word asks for the
closed_at the encounter result already carries. Every other use of those words stays UNKNOWN / UNSUPPORTED.
Fix 3 (memory/systems/fix3, Manglish clitics) adds, and removes nothing: the case suffixes that tokenise as "-" + suffix are neutral ONLY in
the position where they carry no meaning of their own -- "-um" / "-ilum" ("also / in ... also") directly after a prescription or visit
noun ("prescriptions-um", "prescription-ilum"); "-umayi" ("with") after such a noun in a question that has a change cue; "cheyyumbol"
("when doing") directly after a change word ("compare cheyyumbol"). Everywhere else they stay UNKNOWN, and no word of the
other-patient / unsupported lists is touched.
Fix 5 (memory/systems/fix5, Malayalam vocabulary) adds, and removes nothing: three neutral Malayalam words ("എന്താണുള്ളത്" what is in it,
"രേഖപ്പെടുത്തിയത്" the recorded one, "നൽകിയിട്ടുണ്ട്" has been given -- the same meaning as the neutral words already listed), and "ചെയ്യുമ്പോൾ" (when doing)
only directly after "താരതമ്യം" (comparison). The cue words themselves are in memory.interpret.
Fix 7 (memory/systems/fix7, remaining Manglish coverage) adds, and removes nothing: Manglish spelling variants of neutral words already listed
("entha", "ullath", "rekhappeduthiyath", "rekhayil", "ethaayirunnu", "ezhuthiya"); "entenkilum" (any) only beside a change cue, "cheythath" only
directly after a visit-status word ("close cheythath"); "sharaashari" (average) among the UNSUPPORTED words, like "average"; and a person
word (rogi, രോഗി, aal, ആൾ) with "puthiya / ettavum / പുതിയ / ഏറ്റവും / munpathe / മുമ്പത്തെ / adutha / അടുത്ത" in the two words before it is UNSUPPORTED
("the newest patient's ..."), so the question is abstained, never clarified as one about this patient.
Fix 8a (memory/systems/fix8a, safe vocabulary) adds, and removes nothing: UNSUPPORTED words for questions the fixed plan space cannot express --
trend words ("trend / trending / rising / upward / downward", Manglish "koodi / kuranju varunnundo"), Manglish calendar windows ("maasam /
maasathil / maasathile", like the English "month"), and the nth-record selector ("prescription number three": the word "number" between a
record noun and a numeral). And four context-bound neutral words: "write" after "did", "whose" and "exist(s)" directly after a prescription /
visit noun, "anything" after an auxiliary in a question with a change cue.
The check never decides the answer; memory.v2 turns unexplained words into clarify / abstain.
"""
from __future__ import annotations

import re

from backend.herb_resolver import HERB_ALIASES
from memory.interpret import CUES, _canon_numbers
from memory.planner import DOSHA_JOINERS, DOSHA_WORDS, MAX_HERB_TOKENS, NUMBER_WORDS, tokenize

NEUTRAL = {
    # English function words and auxiliaries
    "a", "an", "the", "of", "in", "at", "for", "to", "with", "by", "from", "and", "or", "as", "into", "within",
    "is", "are", "was", "were", "be", "been", "being", "has", "have", "had", "do", "does", "did", "will",
    "what", "which", "how", "there", "here", "any", "s", "so", "far", "ever", "just", "please", "pls",
    # the selected patient and the asker
    "her", "his", "she", "he", "him", "they", "them", "their", "this", "that", "it", "its", "patient", "i", "me",
    "we", "us", "you", "my", "our", "can", "could",
    # display / record verbs and nouns
    "show", "tell", "display", "see", "view", "record", "records", "file", "recorded", "noted", "note", "assessed",
    "assessment", "entered", "documented", "logged", "written", "wrote", "issued", "dispensed", "given", "listed",
    # containment
    "contain", "contains", "contained", "containing", "include", "includes", "included", "including", "had",
    "having", "appear", "appears", "appeared", "featured", "part", "present", "were",
    # Manglish
    "ee", "ivalude", "avalude", "ivarude", "avarude", "ivanu", "ivalkku", "avalkku", "patientinu", "patientinte",
    "und", "undu", "undo", "undaayirunnu", "undaayi", "aayirunnu", "aanu", "aano", "enthu", "enthaanu",
    "enthaayirunnu", "ethaanu", "ethu", "ethokke", "ulla", "okke", "kaanikku", "kaanikkuka", "parayu", "parayuka",
    "tharu", "il", "ninnu", "muthal", "vare", "ithuvare", "ennu", "ittund", "cheythu", "rekhappeduthi",
    "rekhappeduthiya",
    # Malayalam
    "ഈ", "ഇവർ", "ഇവരുടെ", "ഇവളുടെ", "അവളുടെ", "അവരുടെ", "ഇദ്ദേഹം", "ഇദ്ദേഹത്തിന്റെ", "ഉണ്ട്", "ഉണ്ടോ",
    "ഉണ്ടായിരുന്നു", "ഉണ്ടായി", "ആയിരുന്നു", "ആണ്", "ആണോ", "എന്ത്", "എന്താണ്", "എന്തായിരുന്നു", "ഏത്",
    "ഏതാണ്", "ഏതായിരുന്നു", "കാണിക്കൂ", "കാണിക്കുക", "പറയൂ", "പറയുക", "രേഖപ്പെടുത്തി", "രേഖപ്പെടുത്തിയ",
    "നൽകി", "നൽകിയ", "മുതൽ", "വരെ", "ഇതുവരെ", "നിന്ന്", "ഉള്ള", "എഴുതി", "എഴുതിയ", "ക്ലിനിക്കിൽ",
    # added on the dev split (meaning-neutral for a memory question)
    "very", "read", "open", "made", "clinic", "clinicil", "enthokke",
    # fix 2: a preposition, as "within"
    "across",
    # fix 5: Malayalam, meaning-neutral for a memory question
    "എന്താണുള്ളത്", "രേഖപ്പെടുത്തിയത്", "നൽകിയിട്ടുണ്ട്",
    # fix 7: Manglish variants of words listed above
    "entha", "ullath", "rekhappeduthiyath", "rekhayil", "ethaayirunnu", "ezhuthiya",
}
RECORD_NOUN_PREFIXES = ("prescription", "visit", "rx")
TREND_PARTICLES = {"koodi", "kuranju"}  # fix 8a: "koodi varunnundo" / "kuranju varunnundo" (is it rising / falling)
PERSON_STEMS = ("rogi", "രോഗി", "aal", "ആൾ")
PERSON_MODIFIERS = {"puthiya", "ettavum", "പുതിയ", "ഏറ്റവും", "munpathe", "മുമ്പത്തെ", "adutha", "അടുത്ത"}
ONLY_WORDS = {"only", "exactly", "pure", "maathram", "മാത്രം"}  # exact-dosha count: next to a dosha slot, in a COUNT question
STATUS_WORDS = {"closed", "ended", "still", "yet", "ഓപ്പൺ"}  # closed_at of a visit: next to a visit word
RECORD_NOUN_STEMS = ("prescription", "visit")  # the nouns a Manglish case clitic may hang on, as "prescriptions-um"
CLITIC_PLAIN = {"um", "ilum"}
CLITIC_WITH_CHANGE = {"umayi"}
STATUS_WITH_WHEN = {"close", "end"}  # imperative-looking forms: only with a WHEN word ("when did the visit end")
UNSUPPORTED = {
    "not", "no", "never", "without", "except", "excluding", "only", "but", "second", "third", "fourth", "fifth",
    "penultimate", "smallest", "largest", "biggest", "highest", "lowest", "maximum", "minimum", "max", "min",
    "most", "least", "average", "mean", "median", "patients", "others", "other", "people", "everyone",
    "anyone", "somebody", "month", "months", "week", "weeks", "year", "years", "yesterday",
    "rising", "trend", "trends", "trending", "upward", "upwards", "downward", "downwards", "maasam", "maasathil", "maasathile",
    "sharaashari", "vere", "randaamathe", "moonnaamathe", "mattu", "rogikal", "rogikalkku", "rogikalude",
    "രണ്ടാമത്തെ", "മൂന്നാമത്തെ", "മറ്റ്", "മറ്റു", "രോഗികൾ", "രോഗികളുടെ", "രോഗികൾക്ക്",
}
CONTEXT_WITH_CHANGE = {"since", "than", "between", "against", "with", "ninnu", "ekkal", "ekkaal", "ilekkal"}
TIME_AFTER = {"first", "last", "latest", "most", "recent", "previous", "earliest"}
# "patient" is the selected patient ("this patient", "the patient's"), unless a selector names another one
OTHER_PATIENT_BEFORE = {"previous", "last", "next", "other", "another", "first", "earlier", "prior", "new", "kazhinja"}


def _phrase_tokens(tokens: list[str]) -> set[int]:
    """Indices of tokens inside a matched cue phrase ("how many", "most recent", "last time" ...)."""
    text = " ".join(tokens)
    starts, pos = [], 0
    for t in tokens:
        starts.append(pos)
        pos += len(t) + 1
    covered = set()
    for _, (_, _, phrases) in CUES.items():
        for p in phrases:
            for m in re.finditer(p, text):
                covered.update(i for i, s in enumerate(starts) if m.start() <= s < m.end())
    return covered


def _on_record_noun(tokens: list[str], idx: int) -> bool:
    """"prescriptions-um": the clitic directly follows "-" which directly follows a prescription / visit noun."""
    return idx >= 2 and tokens[idx - 1] == "-" and tokens[idx - 2].startswith(RECORD_NOUN_STEMS)


def _phrase_categories(tokens: list[str]) -> set[str]:
    text = " ".join(tokens)
    return {cat for cat, (_, _, phrases) in CUES.items() if any(re.search(p, text) for p in phrases)}


def coverage(question: str) -> dict:
    tokens = tokenize(_canon_numbers(question))
    roles = [None] * len(tokens)
    # slots: herbs (longest alias first), doshas with their joiners, numbers -- same rules as delexicalise
    i = 0
    while i < len(tokens):
        for j in range(min(len(tokens), i + MAX_HERB_TOKENS), i, -1):
            name = " ".join(tokens[i:j])
            if name in HERB_ALIASES and not name.isdigit():
                for k in range(i, j):
                    roles[k] = "slot"
                i = j
                break
        else:
            t = tokens[i]
            if t in DOSHA_WORDS:
                roles[i] = "slot"
                j = i + 1
                while j + 1 < len(tokens) and tokens[j] in DOSHA_JOINERS and tokens[j + 1] in DOSHA_WORDS:
                    roles[j] = roles[j + 1] = "slot"
                    j += 2
                i = j
                continue
            if t.isdigit() or t in NUMBER_WORDS:
                roles[i] = "slot"
            i += 1
    phrase = _phrase_tokens(tokens)
    cue_cats = _phrase_categories(tokens)
    for idx, t in enumerate(tokens):
        if roles[idx]:
            continue
        for cat, (words, prefixes, _) in CUES.items():
            if t in words or (prefixes and t.startswith(prefixes)):
                roles[idx] = "cue"
                cue_cats.add(cat)
                break
        if roles[idx] is None and idx in phrase:
            roles[idx] = "cue"
    for idx, t in enumerate(tokens):
        if roles[idx]:
            continue
        if not any(ch.isalnum() for ch in t):
            roles[idx] = "punct"
        elif t in ("patient", "patients", "patientinte", "patientinu") and \
                set(tokens[max(0, idx - 2):idx]) & OTHER_PATIENT_BEFORE:
            roles[idx] = "UNSUPPORTED"
        elif t.startswith(PERSON_STEMS) and set(tokens[max(0, idx - 2):idx]) & PERSON_MODIFIERS:
            roles[idx] = "UNSUPPORTED"      # fix 7: "ettavum puthiya rogiyude ..." / "ഏറ്റവും പുതിയ രോഗിയുടെ ...": another patient
        elif t in NEUTRAL:
            roles[idx] = "neutral"
        elif t in ONLY_WORDS and cue_cats & {"COUNT", "QUANT"} and any(
                roles[j] == "slot" and tokens[j] in DOSHA_WORDS for j in range(max(0, idx - 2), min(len(tokens), idx + 3))):
            roles[idx] = "context"
        elif t in CLITIC_PLAIN | CLITIC_WITH_CHANGE and _on_record_noun(tokens, idx) and (t in CLITIC_PLAIN or "CHANGE" in cue_cats):
            roles[idx] = "context"
        elif t == "varunnundo" and idx and tokens[idx - 1] in TREND_PARTICLES:
            roles[idx] = "UNSUPPORTED"
        elif t == "write" and "did" in tokens[max(0, idx - 3):idx]:
            roles[idx] = "context"
        elif t in ("whose", "exist", "exists") and idx and tokens[idx - 1].startswith(RECORD_NOUN_PREFIXES):
            roles[idx] = "context"
        elif t == "anything" and "CHANGE" in cue_cats and idx and tokens[idx - 1] in ("did", "does", "do", "has", "have", "was", "is"):
            roles[idx] = "context"
        elif t == "entenkilum" and "CHANGE" in cue_cats:
            roles[idx] = "context"
        elif t == "cheythath" and idx and tokens[idx - 1] in STATUS_WITH_WHEN | STATUS_WORDS and "ENC" in cue_cats:
            roles[idx] = "context"
        elif t == "ചെയ്യുമ്പോൾ" and idx and tokens[idx - 1] == "താരതമ്യം":
            roles[idx] = "context"
        elif t == "cheyyumbol" and idx and tokens[idx - 1] in CUES["CHANGE"][0] and tokens[idx - 1].startswith("compar"):
            roles[idx] = "context"
        elif "ENC" in cue_cats and (t in STATUS_WORDS or (t in STATUS_WITH_WHEN and "WHEN" in cue_cats)
                                    or (t == "time" and idx and tokens[idx - 1] == "what")):
            roles[idx] = "context"
        elif t in CONTEXT_WITH_CHANGE and "CHANGE" in cue_cats:
            roles[idx] = "context"
        elif t == "time" and idx and tokens[idx - 1] in TIME_AFTER:
            roles[idx] = "context"
        elif t in UNSUPPORTED:
            roles[idx] = "UNSUPPORTED"
        else:
            roles[idx] = "UNKNOWN"
    for idx, t in enumerate(tokens):   # fix 8a: "prescription number three" / "number 3 prescription" selects the nth record: unsupported
        if t == "number" and idx + 1 < len(tokens) and (tokens[idx + 1].isdigit() or tokens[idx + 1] in NUMBER_WORDS) and (
                (idx and tokens[idx - 1].startswith(RECORD_NOUN_PREFIXES)) or
                (idx + 2 < len(tokens) and tokens[idx + 2].startswith(RECORD_NOUN_PREFIXES))):
            roles[idx] = "UNSUPPORTED"
    return {"tokens": tokens, "roles": roles,
            "unexplained": [t for t, r in zip(tokens, roles) if r in ("UNKNOWN", "UNSUPPORTED")],
            "unsupported": [t for t, r in zip(tokens, roles) if r == "UNSUPPORTED"]}
