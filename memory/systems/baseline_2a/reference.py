"""
Structural reference safety for AI-2 v2 (Block 2A): does the question refer to someone OTHER than the
authenticated target patient? Deterministic, text only, no patient data, no identity resolution.

    outside_scope(question) -> {"outside": bool, "rules": [rule, ...], "cues": [token, ...]}

A memory question is about exactly one person: the patient the session selected. The question leaves
that scope -- and must be refused -- when it contains one of these shapes (English / Manglish /
Malayalam; Manglish and Malayalam words are matched by stem, so case endings are covered):

  R1 relational person   a PERSON noun (patient, person, woman, man, lady, case, rogi, aal, രോഗി, ആൾ ...)
                         with a relational word among the 3 tokens before it: previous, next, other,
                         another, last, first, earlier, new, kazhinja, munpu, adutha, vere, കഴിഞ്ഞ,
                         മുമ്പ്, അടുത്ത, മറ്റ്, വേറെ, അവസാന ... ("the previous patient", "vere rogi")
  R2 relative clause     a PERSON noun followed by a relative / participle word (seen, who, that,
                         treated, registered, admitted, came ...) or by before / after + a person
                         word (her, him, them, this one, this patient): "the patient seen before her",
                         "the patient before this one", "the patient after her"
  R3 group of people     patients, people, everyone, anyone, others, all / each / every / any + PERSON,
                         rogikal, ellaarum, രോഗികൾ, എല്ലാവരും ...
  R4 clinic-wide         clinic's, of / across / in the clinic, whole / entire / all clinic, clinic-wide,
                         clinicinte, clinicile, ക്ലിനിക്കിലെ, ക്ലിനിക്കിന്റെ ...
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
QUANTIFIER = {"all", "each", "every", "any", "both", "most", "many", "several", "ella", "ellaa", "oro", "എല്ലാ", "ഓരോ"}
CLINIC_STEMS = ("clinicinte", "clinicile", "ക്ലിനിക്കിലെ", "ക്ലിനിക്കിന്റെ", "ക്ലിനിക്കിലുള്ള")
CLINIC_BEFORE = {"of", "across", "in", "whole", "entire", "all", "every", "throughout"}
FEMININE, MASCULINE, PLURAL = {"her", "hers", "she", "herself"}, {"his", "him", "he", "himself"}, \
    {"their", "theirs", "them", "they", "themselves"}
KIN_STEMS = ("makal", "makan", "bharthav", "bharya", "chettan", "chechi", "aniyan", "aniyath", "ammayude", "achante",
             "മകൾ", "മകന", "മകൻ", "ഭർത്താവ", "ഭാര്യ", "അമ്മയ", "അച്ഛ", "സഹോദര", "ചേട്ട", "ചേച്ചി", "അനിയ")


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
                rel = [w for w in before if _relational(w)]
                if rel:
                    hit("R1_relational_person", *rel, t)
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
        if t in ("clinics",) or t.startswith(CLINIC_STEMS):
            hit("R4_clinic_wide", t)
        if t.startswith(KIN_STEMS):
            hit("R6_relative", t)
    pronoun = FEMININE | MASCULINE | PLURAL
    paired = {i + d for i in range(1, len(toks) - 1) if toks[i] in ("or", "and", "/")
              and toks[i - 1] in pronoun and toks[i + 1] in pronoun for d in (-1, 1)}
    words = {t for i, t in enumerate(toks) if i not in paired}
    classes = [c for c in (FEMININE, MASCULINE, PLURAL) if words & c]
    if len(classes) > 1:
        hit("R5_two_people", *sorted(words & (FEMININE | MASCULINE | PLURAL)))
    return {"outside": bool(rules), "rules": sorted(set(rules)), "cues": list(dict.fromkeys(cues))}
