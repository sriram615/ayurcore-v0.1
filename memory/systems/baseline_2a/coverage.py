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
The check never decides the answer; memory.v2 turns unexplained words into clarify / abstain.
"""
from __future__ import annotations

import re

from backend.herb_resolver import HERB_ALIASES
from memory.interpret import CUES
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
}
UNSUPPORTED = {
    "not", "no", "never", "without", "except", "excluding", "only", "but", "second", "third", "fourth", "fifth",
    "penultimate", "smallest", "largest", "biggest", "highest", "lowest", "maximum", "minimum", "max", "min",
    "most", "least", "average", "mean", "median", "patients", "others", "other", "people", "everyone",
    "anyone", "somebody", "month", "months", "week", "weeks", "year", "years", "yesterday",
    "vere", "randaamathe", "moonnaamathe", "mattu", "rogikal", "rogikalkku", "rogikalude",
    "രണ്ടാമത്തെ", "മൂന്നാമത്തെ", "മറ്റ്", "മറ്റു", "രോഗികൾ", "രോഗികളുടെ", "രോഗികൾക്ക്",
}
CONTEXT_WITH_CHANGE = {"since", "than", "between", "against", "with", "ninnu", "ekkal", "ekkaal"}
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


def coverage(question: str) -> dict:
    tokens = tokenize(question)
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
    cue_cats = set()
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
        elif t in NEUTRAL:
            roles[idx] = "neutral"
        elif t in CONTEXT_WITH_CHANGE and "CHANGE" in cue_cats:
            roles[idx] = "context"
        elif t == "time" and idx and tokens[idx - 1] in TIME_AFTER:
            roles[idx] = "context"
        elif t in UNSUPPORTED:
            roles[idx] = "UNSUPPORTED"
        else:
            roles[idx] = "UNKNOWN"
    return {"tokens": tokens, "roles": roles,
            "unexplained": [t for t, r in zip(tokens, roles) if r in ("UNKNOWN", "UNSUPPORTED")],
            "unsupported": [t for t, r in zip(tokens, roles) if r == "UNSUPPORTED"]}
