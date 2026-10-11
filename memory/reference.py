"""
Structural reference safety for AI-2 v2 (Block 2A): does the question refer to someone OTHER than the
authenticated target patient? Deterministic, text only, no patient data, no identity resolution.

    outside_scope(question) -> {"outside": bool, "rules": [rule, ...], "cues": [token, ...]}

A memory question is about exactly one person: the patient the session selected. The question leaves
that scope -- and must be refused -- when it contains one of these shapes (English / Manglish /
Malayalam; Manglish and Malayalam words are matched by stem, so case endings are covered):

  R1 selector inside the person's phrase (Block 4B structural rule)
                         a PERSON noun (patient, person, woman, man, lady, case, rogi, aal, രോഗി, ആൾ ...)
                         whose own noun phrase -- the up-to-4 tokens immediately to its left, up to a
                         BOUNDARY (determiner, possessive 's, preposition, question word / verb,
                         punctuation) -- contains a SELECTOR: a first / last / previous word of the
                         vocabulary layer (memory.interpret.CUES, so new recency synonyms extend this rule
                         automatically) or a relational word (other, another, next, new, vere, munpu,
                         മറ്റ്, പുതിയ ...). An IDENTITY word (this, current, present, same, selected, ee,
                         ഈ, ippozhathe ...) marks the target patient. "the newest patient", "avasanathe
                         rogi" fire; "the patient's newest prescription", "the last time the patient came"
                         do not (the selector modifies prescription / time, not the person)
  R2 relative clause     a PERSON noun followed by a relative / participle word (seen, who, that,
                         treated, registered, admitted, came ...) or by before / after + a person
                         word (her, him, them, this one, this patient): "the patient seen before her",
                         "the patient before this one", "the patient after her"
  R3 group of people     patients, people, everyone, anyone, others, all / each / every / any + PERSON,
                         rogikal, ellaarum, രോഗികൾ, എല്ലാവരും ...; also ororo / oro / each / ഓരോ right before a
                         PERSON noun ("ororo rogiyudeyum" = each patient's) [Block 4B repair]
  R4 clinic-wide         clinic's, of / across / in the clinic, whole / entire / all clinic, clinic-wide,
                         clinicinte, clinicile, ക്ലിനിക്കിലെ, ക്ലിനിക്കിന്റെ ...; also the clinic as the SUBJECT of
                         an issuing verb ("the clinic issued / gave / prescribed ...", optionally after
                         has / had / did) unless the verb names the target patient as recipient within
                         3 tokens ("the clinic gave her ...") [Block 4B repair]
  R5 two people          personal pronouns of two different people in one question (her ... his,
                         her ... their); a gender-neutral pair joined by or / and / slash ("him or
                         her", "his/her") names one person and is ignored
  R6 relatives           kinship nouns with case endings (makalkku, മകൾക്ക്, ഭർത്താവിന്റെ ...);
                         uninflected English kinship words are already refused by the scope screen
A PERSON noun directly preceded by a target determiner (this, same, current, ee, ഈ) is the target
patient and starts no rule; "the patient" alone is the target too ("the patient's latest visit").
The rule only refuses; it never resolves who the other person is and never changes the session scope.
"""
from __future__ import annotations

from memory.interpret import CUES
from memory.planner import tokenize

PERSON = {"patient", "patients", "person", "persons", "people", "woman", "women", "man", "men", "lady", "ladies",
          "gentleman", "gentlemen", "case", "individual", "client", "clients"}  # not "one": "the previous one" is
# usually the previous prescription; "the patient before this one" is caught by R2
PERSON_STEMS = ("rogi", "aal", "രോഗി", "ആൾ", "ആള", "വ്യക്തി")
TARGET_DET = {"this", "same", "current", "ee", "ഈ", "ഇതേ"}
RELATIONAL = {"previous", "prior", "preceding", "next", "other", "another", "different", "earlier", "later", "new",
              "first", "second", "third", "last", "following", "subsequent", "waiting", "remaining",
              "kazhinja", "munpu", "munpe", "munne", "adutha", "aduthulla", "vere", "mattu", "mattoru",
              "avasanathe", "aadyathe"}
RELATIONAL_STEMS = ("കഴിഞ്ഞ", "മുമ്പ", "മുൻപ", "അടുത്ത", "മറ്റ", "വേറെ", "അവസാന", "ആദ്യ")
CLAUSE = {"seen", "who", "that", "whom", "whose", "treated", "registered", "admitted", "consulted", "examined",
          "came", "coming", "visiting", "booked", "before", "after", "ahead", "behind", "vanna", "kanda"}
PERSON_AFTER_RELATION = {"her", "him", "them", "me", "us", "you", "this", "hers", "his", "theirs"}
GROUP = {"patients", "people", "persons", "everyone", "everybody", "anyone", "anybody", "others", "someone",
         "somebody", "rogikal", "ellaarum", "ellaarudeyum", "ellaarkkum", "ellavarudeyum"}
GROUP_STEMS = ("രോഗികൾ", "രോഗികള", "എല്ലാവ", "rogikal", "ellaavar")
QUANTIFIER = {"all", "each", "every", "any", "both", "most", "many", "several", "ella", "ellaa", "oro", "എല്ലാ", "ഓരോ",
              "ororo"}  # Block 4B repair: "ororo rogi" = each patient
CLINIC_STEMS = ("clinicinte", "clinicile", "ക്ലിനിക്കിലെ", "ക്ലിനിക്കിന്റെ", "ക്ലിനിക്കിലുള്ള")
CLINIC_BEFORE = {"of", "across", "in", "whole", "entire", "all", "every", "throughout"}
# Block 4B repair: the clinic as the subject of an issuing verb ("the clinic issued ...")
CLINIC_AUX = {"has", "had", "have", "did", "does", "do", "will"}
CLINIC_VERBS = {"issued", "issue", "issues", "gave", "give", "gives", "given", "prescribed", "prescribe", "prescribes",
                "dispensed", "dispense", "dispenses", "wrote", "written", "writes", "saw", "sees", "seen", "treated",
                "treats", "registered", "recorded", "handed"}
CLINIC_RECIPIENT = {"her", "him", "ivarkku", "ivalkku", "ivanu", "ഇവർക്ക്"}  # or "this patient"
FEMININE, MASCULINE, PLURAL = {"her", "hers", "she", "herself"}, {"his", "him", "he", "himself"}, \
    {"their", "theirs", "them", "they", "themselves"}
KIN_STEMS = ("makal", "makan", "bharthav", "bharya", "chettan", "chechi", "aniyan", "aniyath", "ammayude", "achante",
             "മകൾ", "മകന", "മകൻ", "ഭർത്താവ", "ഭാര്യ", "അമ്മയ", "അച്ഛ", "സഹോദര", "ചേട്ട", "ചേച്ചി", "അനിയ")


SELECTOR_WORDS = set().union(*(CUES[c][0] for c in ("FIRST", "LAST", "PREVIOUS", "LASTPREV"))) | RELATIONAL | \
    {"penultimate", "fourth", "puthiya", "പുതിയ"}
SELECTOR_STEMS = tuple(p for c in ("FIRST", "LAST", "LASTPREV") for p in CUES[c][1]) + RELATIONAL_STEMS
IDENTITY = {"this", "current", "currently", "present", "same", "selected", "now", "today", "ee", "ഈ", "ഇതേ"}
IDENTITY_STEMS = ("ippozh", "ഇപ്പോഴ", "ഇപ്പോൾ")
BOUNDARY = {"the", "a", "an", "that", "these", "those", "her", "his", "their", "my", "our", "your", "its",
            "for", "of", "with", "at", "to", "from", "in", "on", "by", "about", "and", "or", "than", "when", "what",
            "which", "who", "was", "is", "did", "does", "show", "list", "give", "tell", "compare", "s", "'", "?", ",",
            ".", ":", ";", "-", "ivarude", "avalude", "ഇവരുടെ"}


def _identity(t: str) -> bool:
    return t in IDENTITY or t.startswith(IDENTITY_STEMS)


def _selector(t: str) -> bool:
    return (t in SELECTOR_WORDS or t.startswith(SELECTOR_STEMS)) and not _identity(t)


def _r1(raw: list[str]) -> list[list[str]]:
    """Selectors inside the noun phrase of a PERSON noun (raw tokens, punctuation kept as boundaries)."""
    hits = []
    for i, t in enumerate(raw):
        if not _is_person(t):
            continue
        span, j = [], i - 1
        while j >= 0 and len(span) < 4 and raw[j] not in BOUNDARY:
            span.append(raw[j])
            j -= 1
        if (j >= 0 and _identity(raw[j])) or any(_identity(w) for w in span):
            continue
        selectors = [w for w in reversed(span) if _selector(w)]
        if selectors:
            hits.append(selectors + [t])
    return hits


def _is_person(t: str) -> bool:
    return t in PERSON or t.startswith(PERSON_STEMS)


def _relational(t: str) -> bool:
    return t in RELATIONAL or t.startswith(RELATIONAL_STEMS)


def outside_scope(question) -> dict:
    if not isinstance(question, str):
        return {"outside": False, "rules": [], "cues": []}
    toks = [t for t in tokenize(question) if any(ch.isalnum() for ch in t) or t in ("-", "/")]
    rules, cues = [], []

    def hit(rule, *words):
        rules.append(rule)
        cues.extend(words)

    for i, t in enumerate(toks):
        before = toks[max(0, i - 3):i]
        after = toks[i + 1:i + 5]
        if _is_person(t):
            targeted = bool(before) and before[-1] in TARGET_DET
            if not targeted:
                if after and after[0] in CLAUSE - {"before", "after", "ahead", "behind"}:
                    hit("R2_relative_clause", t, after[0])
                for j, w in enumerate(after):
                    if w in ("before", "after", "ahead", "behind") and j + 1 < len(after) and \
                            after[j + 1] in PERSON_AFTER_RELATION:
                        hit("R2_relative_clause", t, w, after[j + 1])
                        break
                if any(w in QUANTIFIER for w in before[-2:]):
                    hit("R3_group", *[w for w in before[-2:] if w in QUANTIFIER], t)
        if t in GROUP or t.startswith(GROUP_STEMS):
            hit("R3_group", t)
        if t == "clinic" and ((i + 1 < len(toks) and toks[i + 1] in ("s", "-", "wide")) or
                              (before and before[-1] in CLINIC_BEFORE) or
                              (len(before) > 1 and before[-1] == "the" and before[-2] in CLINIC_BEFORE)):
            hit("R4_clinic_wide", *before[-2:], t)
        if t == "clinic":
            rest = toks[i + 1:i + 6]
            k = 1 if rest and rest[0] in CLINIC_AUX else 0
            after_verb = rest[k + 1:k + 4]
            recipient = bool(set(after_verb) & CLINIC_RECIPIENT) or any(
                a == "this" and b == "patient" for a, b in zip(after_verb, after_verb[1:]))
            if len(rest) > k and rest[k] in CLINIC_VERBS and not recipient:
                hit("R4_clinic_wide", t, *rest[:k + 1])
        if t in ("clinics",) or t.startswith(CLINIC_STEMS):
            hit("R4_clinic_wide", t)
        if t.startswith(KIN_STEMS):
            hit("R6_relative", t)
    for words in _r1(tokenize(question)):
        hit("R1_relational_person", *words)
    pronoun = FEMININE | MASCULINE | PLURAL
    paired = {i + d for i in range(1, len(toks) - 1) if toks[i] in ("or", "and", "/")
              and toks[i - 1] in pronoun and toks[i + 1] in pronoun for d in (-1, 1)}
    words = {t for i, t in enumerate(toks) if i not in paired}
    classes = [c for c in (FEMININE, MASCULINE, PLURAL) if words & c]
    if len(classes) > 1:
        hit("R5_two_people", *sorted(words & (FEMININE | MASCULINE | PLURAL)))
    return {"outside": bool(rules), "rules": sorted(set(rules)), "cues": list(dict.fromkeys(cues))}
