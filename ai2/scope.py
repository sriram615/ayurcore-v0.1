"""
Deterministic out-of-scope screen for AI-2 questions. Runs BEFORE the learned planner.

    screen(question) -> {"decision": "PASS" | "ABSTAIN", "reason": None | REASON, "cues": [...]}

Reasons (checked in this order; the first that fires wins):
  AMBIGUOUS     empty / injection-like / more than one herb / a quantity question with no herb /
                several selectors (first, last, previous) with no comparison word / pronoun with no
                subject / no memory-domain anchor
  OTHER_PATIENT another person or patient, other patients, the whole clinic
  DIAGNOSIS     diseases, conditions, diagnosis, prognosis, severity
  TREATMENT     advice or intent: modal and recommendation words, "next" dose / visit, present-tense
                clinical action verbs (increase, stop, start, add ...) without a past-tense auxiliary,
                Manglish / Malayalam obligation forms (kodukkanam, nirthano, ...)
  UNAVAILABLE   facts the memory does not record: vitals, symptoms, outcomes and effects ("did it
                work / help / improve"), allergies, frequency and duration, who / why / reasons,
                costs, notes text

It is conservative by construction: a question passes only if it contains a memory-domain
anchor and no cue fires. It never builds a plan and never reads patient data. Cue lists are
closed lexicons in English, Manglish and Malayalam.

fix1 (safety-layer parity; AI-2 evaluation protocol v1.1, first fix block) changes only three things, each a structural
distinction with a minimal-pair test (tests/test_safety_parity_fix1.py):
  1. Manglish / Malayalam clinical-action verbs in a non-past modal or imperative form ("nirthaamo" = can we stop,
     "thudangaam" = can start, "നിർത്താമോ") are TREATMENT, like the English "can we stop / start". Past forms
     (nirthi, thudangi, കൊടുത്തു, ചേർത്തത്) are history and are not matched.
  2. "give" is a record-display request, not an action, when its object is a record-display noun (history, dosha, list,
     record, timeline, summary, details) and no herb is named ("give the visit history", "give her dosha at each
     prescription"); "lower" is a comparative, not an action, in "higher or lower" / "lower than".
  3. The Malayalam copula "ആണോ" ("is it ...?", also "-ാണോ") is not the obligation ending "-ണോ" ("കൂട്ടണോ").

fix4 (Malayalam scope parity; evaluation protocol v1.1, fix block 4) changes one thing: the memory-anchor test. A Malayalam noun takes a case
ending that is part of the same token ("കുറിപ്പടിയിൽ" = in the prescription, "കുറിപ്പടിയെ"); the exact-word anchor list only holds the bare
"കുറിപ്പടി", so a plain record question about the latest prescription was refused as having no memory anchor. A token that STARTS WITH the
prescription noun now counts as an anchor. This check runs after every other-patient, diagnosis, treatment and unavailable cue and after
the herb / selector / pronoun checks, so no safety cue is skipped; it only lets such a question go on to the reference check, the
alignment and the coverage check (tests/test_scope_parity_fix4.py).
"""
from __future__ import annotations

import re

from ai2.planner import delexicalise, tokenize

# --- closed lexicons ---------------------------------------------------------------------------------
INJECTION = {"ignore", "system", "prompt", "sql", "select", "drop", "delete", "insert", "update", "database",
             "table", "script", "instructions", "jailbreak", "override", "admin", "password", "token"}
OTHER_PERSON = {"mother", "father", "husband", "wife", "son", "daughter", "brother", "sister", "family", "child",
                "children", "relative", "neighbour", "neighbor", "friend", "others", "everyone", "everybody",
                "amma", "achan", "bharthavu", "bharya", "makan", "makal", "chettan", "chechi", "aniyan", "aniyathi",
                "അമ്മ", "അച്ഛൻ", "ഭർത്താവ്", "ഭാര്യ", "മകൻ", "മകൾ"}
OTHER_PATIENT_PHRASES = (r"\bother patients?\b", r"\banother patient\b", r"\ball patients\b", r"\bevery patient\b",
                         r"\bpatient (id|number|no)\b", r"\bs2-p\d+", r"\bwhole clinic\b", r"\bin the clinic\b")
DIAGNOSIS = {"diagnosis", "diagnose", "diagnosed", "disease", "diseases", "illness", "condition", "conditions",
             "disorder", "syndrome", "infection", "cancer", "diabetes", "diabetic", "arthritis", "asthma",
             "hypertension", "prognosis", "serious", "severity", "severe", "suffer", "suffering", "sick",
             "rogam", "rogamaanu", "asukham", "asukhamaanu", "രോഗം", "രോഗമാണ്", "അസുഖം"}
DIAGNOSIS_PHRASES = (r"\bwhat is wrong\b", r"\bwhat'?s wrong\b")
ADVICE = {"should", "shall", "recommend", "recommended", "recommendation", "suggest", "suggestion", "advise", "advice",
          "ought", "safe", "safer", "unsafe", "best", "ideal", "optimal", "appropriate", "enough", "sufficient",
          "need", "needs", "needed", "next", "ok", "okay", "alright", "worth", "plan",
          "nallathu", "nallatho", "venamo", "ini", "അടുത്തതായി", "ഇനി", "നല്ലത്", "വേണോ"}
ADVICE_PHRASES = (r"\b(can|could|may|must) (i|we|she|he)\b", r"\bis it (ok|okay|safe|fine|alright)\b",
                  r"\bwhat (dose|amount) (to|should)\b", r"\bgood (for|idea)\b", r"\bwould you\b", r"\bdo i\b")
# base-form clinical action verbs; nouns that share the form ("change", "start", "use") are left out
ACTION_VERBS = {"increase", "decrease", "reduce", "raise", "lower", "stop", "continue", "switch", "add",
                "remove", "swap", "replace", "give", "prescribe", "dispense", "try", "resume",
                "restart", "discontinue", "taper", "double", "halve", "repeat"}
PAST_AUX = {"did", "was", "were", "has", "have", "had", "been", "got", "ago", "earlier", "previously"}
SYSTEM_REQUEST = {"me", "us", "up"}  # "give me", "add up" ask the system, not the patient's treatment
MANGLISH_OBLIGATION = ("kkanam", "kkano", "ttano", "thano", "kkatte", "ttatte", "kkanamo", "ttanamo")
# fix1 (1): clinical-action stems (stop, start, increase, reduce, give, add, omit, continue, change, double) + non-past endings
MANGLISH_ACTION_STEMS = ("nirth", "thudang", "koott", "kurakk", "kodukk", "cherkk", "ozhivakk", "thudar", "maatt", "irattipp")
MANGLISH_MODAL_ENDINGS = ("aamo", "aam", "anam", "ano", "atte", "u")
ML_ACTION_STEMS = ("നിർത്ത", "തുടങ്ങ", "കൂട്ട", "കുറയ്ക്ക", "കുറക്ക", "കൊടുക്ക", "ചേർക്ക", "ഒഴിവാക്ക", "തുടർ", "മാറ്റ", "ഇരട്ടി")
ML_MODAL_ENDINGS = ("ാമോ", "ാം", "ണം", "ണോ", "ട്ടെ")
# fix1 (2): objects that make "give" a request to display records
DISPLAY_NOUNS = {"history", "dosha", "doshas", "record", "records", "list", "timeline", "summary", "details"}
NP_SKIP = {"the", "a", "an", "all", "her", "his", "their", "its", "this", "that", "these", "those", "each", "every"}
UNAVAILABLE = {"bp", "blood", "pressure", "pulse", "temperature", "fever", "weight", "height", "bmi", "sugar",
               "glucose", "lab", "labs", "test", "tests", "report", "reports", "scan", "xray", "symptom", "symptoms",
               "complaint", "complaints", "complain", "complained", "pain", "ache", "aches", "sleep", "appetite",
               "diet", "food", "eat", "ate", "eating", "allergy", "allergies", "allergic", "side", "effect", "effects",
               "reaction", "reactions", "improve", "improved", "improvement", "better", "worse", "worsen", "worsened",
               "work", "worked", "working", "help", "helped", "helps", "effective", "effectiveness", "response",
               "responded", "relief", "cured", "cure", "outcome", "outcomes", "result", "results", "progress",
               "frequency", "daily", "twice", "thrice", "morning", "evening", "night", "duration", "days", "weeks",
               "months", "doctor", "doctors", "physician", "who", "whom", "phone", "address", "age", "cost", "price",
               "bill", "stock", "inventory", "notes", "why", "reason", "reasons", "because", "cause", "caused",
               "causes", "vedana", "pani", "urakkam", "bhakshanam", "divasam", "neram", "enthukondu", "aaru",
               "രക്തസമ്മർദ്ദം", "വേദന", "പനി", "ഉറക്കം", "ഭക്ഷണം", "ഡോക്ടർ", "എന്തുകൊണ്ട്", "ദിവസം"}
# common symptom nouns and outcome verbs: outcomes and symptoms are never recorded in memory
UNAVAILABLE |= {"cough", "cold", "headache", "constipation", "diarrhoea", "diarrhea", "itching", "itch", "rash",
                "insomnia", "swelling", "nausea", "vomiting", "fatigue", "tired", "stress", "anxiety", "dizziness",
                "acidity", "gas", "bloating", "joint", "joints", "stiffness", "relieve", "relieved", "relieves",
                "resolved", "recover", "recovered", "recovery", "heal", "healed", "feel", "feels", "feeling", "felt",
                "tolerate", "tolerated", "benefit", "benefited", "benefits", "chuma", "kshinam", "ksheenam"}
UNAVAILABLE_PHRASES = (r"\btimes (a|per) day\b", r"\bper day\b", r"\bhow long\b", r"\bhow often (should|must|do)\b",
                       r"\bside effects?\b", r"\bwho (prescribed|gave|saw)\b")
ANCHORS = {"prescription", "prescriptions", "prescribed", "rx", "herb", "herbs", "medicine", "medicines", "dose",
           "doses", "dosage", "gram", "grams", "g", "total", "amount", "much", "many", "count", "number", "list",
           "all", "every", "show", "first", "last", "latest", "recent", "earliest", "previous", "original", "initial",
           "current", "currently", "dosha", "doshas", "visit", "visits", "encounter", "encounters", "consultation",
           "consultations", "came", "come", "given", "gave", "give", "got", "change", "changed", "changes",
           "different", "difference", "differences", "differ", "compare", "comparison", "versus", "vs", "increased",
           "decreased", "reduced", "more", "less", "higher", "lower", "since", "start", "started", "cumulative",
           "sum", "times", "often", "history", "record", "recorded", "noted", "assessed",
           "marunnu", "marunnukal", "visitil", "visitukalum", "vannu", "vannathu", "koduthu", "koduthathu",
           "aadyam", "aadyathe", "avasanam", "avasanathe", "ethra", "aake", "ella", "ellam", "maattam", "maari",
           "maariyo", "dosham", "kazhinja", "ippol", "ippozhathe", "prescriptionil", "prescriptionum", "thavana",
           "compare", "kooduthalo", "kuravo", "oro",
           "കുറിപ്പടി", "കുറിപ്പടികൾ", "കുറിപ്പടികളും", "മരുന്ന്", "മരുന്നുകൾ", "സന്ദർശനം", "സന്ദർശനങ്ങളും",
           "ദോഷം", "ആദ്യ", "ആദ്യം", "ആദ്യത്തെ", "അവസാന", "അവസാനം", "അവസാനത്തെ", "എത്ര", "ആകെ", "എല്ലാ",
           "മാറ്റം", "മാറിയോ", "കൊടുത്തു", "കൊടുത്തത്", "കഴിഞ്ഞ", "ഗ്രാം", "വന്നു", "തവണ", "ഇപ്പോഴത്തെ"}
ML_ANCHOR_STEMS = ("കുറിപ്പടി",)  # fix4: the prescription noun with any case ending
QUANTITY_WORDS = {"much", "grams", "gram", "amount", "cumulative", "aake", "ആകെ", "ഗ്രാം"}
TOTAL_AMOUNT = r"\btotal\b(?! (number|count|of (visits|prescriptions|encounters)))"  # "total number of ..." is a count
SELECTOR_WORDS = {"first", "last", "latest", "previous", "earliest", "original", "initial", "current", "recent",
                  "aadyam", "aadyathe", "avasanam", "avasanathe", "kazhinja", "ippol", "ആദ്യ", "അവസാന", "കഴിഞ്ഞ"}
COMPARISON_WORDS = {"compare", "comparison", "versus", "vs", "since", "between", "same", "change", "changed", "changes",
                    "differ", "different", "difference", "differences", "from", "than", "maattam", "maari",
                    "maariyo", "muthal", "മാറ്റം", "മാറിയോ"}
PRONOUNS = {"it", "that", "this", "they", "them", "those", "these", "one", "athu", "ithu", "അത്", "ഇത്"}
RECORD_NOUNS = {"prescription", "prescriptions", "herb", "herbs", "medicine", "medicines", "dosha", "doshas", "visit",
                "visits", "encounter", "encounters", "consultation", "consultations", "dose", "doses", "dosage",
                "marunnu", "dosham", "കുറിപ്പടി", "മരുന്ന്", "സന്ദർശനം", "ദോഷം"}


def _modal_action(w: str) -> bool:
    """fix1 (1): clinical-action stem + modal / imperative ending, Manglish or Malayalam ("nirthaamo", "തുടങ്ങാം")."""
    if w.isascii():
        return any(w.startswith(s) and w[len(s):] in MANGLISH_MODAL_ENDINGS for s in MANGLISH_ACTION_STEMS)
    return any(w.startswith(s) and w[len(s):] in ML_MODAL_ENDINGS for s in ML_ACTION_STEMS)


def _record_display_give(words: list, i: int, slots: dict) -> bool:
    """fix1 (2): 'give the visit history ...' / 'give her dosha ...' display records; 'give her Neem' / 'give her a higher dose' do not."""
    if words[i] != "give" or slots["herbs"]:
        return False
    phrase = []
    for w in words[i + 1:i + 6]:
        if w in NP_SKIP or w in RECORD_NOUNS or w in DISPLAY_NOUNS:
            phrase.append(w)
        else:
            break
    content = [w for w in phrase if w not in NP_SKIP]
    return bool(content) and content[-1] in DISPLAY_NOUNS


def _comparative_lower(words: list, i: int) -> bool:
    """fix1 (2): 'higher or lower', 'lower or higher', 'lower than' compare records; 'lower the dose' is an action."""
    if words[i] != "lower":
        return False
    return (i >= 2 and words[i - 2:i] == ["higher", "or"]) or words[i + 1:i + 3] == ["or", "higher"] or words[i + 1:i + 2] == ["than"]


def _copula(t: str) -> bool:
    """fix1 (3): 'ആണോ' / '-ാണോ' ('is it ...?') ends in ണോ but is not the obligation ending of 'കൂട്ടണോ' ('should (we) increase?')."""
    return t == "ആണോ" or t.endswith("ാണോ")


def _abstain(reason: str, cues) -> dict:
    return {"decision": "ABSTAIN", "reason": reason, "cues": sorted(set(cues))}


def screen(question) -> dict:
    if not isinstance(question, str) or not question.strip():
        return _abstain("AMBIGUOUS", ["empty"])
    text = " ".join(question.lower().split())
    words = [t for t in tokenize(text) if any(ch.isalnum() for ch in t)]
    wordset = set(words)
    masked, slots = delexicalise(question)

    cues = sorted(wordset & INJECTION)
    if cues:
        return _abstain("AMBIGUOUS", ["injection:" + c for c in cues])
    cues = sorted(wordset & OTHER_PERSON) + [p for p in OTHER_PATIENT_PHRASES if re.search(p, text)]
    if cues:
        return _abstain("OTHER_PATIENT", cues)
    cues = sorted(wordset & DIAGNOSIS) + [p for p in DIAGNOSIS_PHRASES if re.search(p, text)]
    if cues:
        return _abstain("DIAGNOSIS", cues)

    cues = sorted(wordset & ADVICE) + [p for p in ADVICE_PHRASES if re.search(p, text)]
    past = bool(wordset & PAST_AUX)
    for i, w in enumerate(words):
        if w in ACTION_VERBS and not past and not (i + 1 < len(words) and words[i + 1] in SYSTEM_REQUEST) \
                and not _record_display_give(words, i, slots) and not _comparative_lower(words, i):
            cues.append("action:" + w)
        if w.isascii() and len(w) > 5 and w.endswith(MANGLISH_OBLIGATION):
            cues.append("obligation:" + w)
        if _modal_action(w):
            cues.append("modal-action:" + w)
    if any(t.endswith(("ണം", "ണോ")) and not _copula(t) for t in words if not t.isascii()):
        cues.append("obligation:ml")
    if cues:
        return _abstain("TREATMENT", cues)

    cues = sorted(wordset & UNAVAILABLE) + [p for p in UNAVAILABLE_PHRASES if re.search(p, text)]
    if cues:
        return _abstain("UNAVAILABLE", cues)

    if len(set(slots["herbs"])) > 1:  # the same herb named twice is one herb
        return _abstain("AMBIGUOUS", ["herbs:" + ",".join(dict.fromkeys(slots["herbs"]))])
    if (wordset & QUANTITY_WORDS or re.search(TOTAL_AMOUNT, text)) and not slots["herbs"]:
        return _abstain("AMBIGUOUS", ["quantity-without-herb"])
    if len(wordset & SELECTOR_WORDS) >= 2 and not wordset & COMPARISON_WORDS:
        return _abstain("AMBIGUOUS", ["several-selectors-without-comparison"])
    has_content = bool(slots["herbs"] or slots["doshas"]) or bool(wordset & RECORD_NOUNS)
    if wordset & PRONOUNS and not has_content:
        return _abstain("AMBIGUOUS", ["pronoun-without-subject"])
    if not (wordset & ANCHORS or slots["herbs"] or slots["doshas"] or any(w.startswith(ML_ANCHOR_STEMS) for w in words)):
        return _abstain("AMBIGUOUS", ["no-memory-anchor"])
    return {"decision": "PASS", "reason": None, "cues": []}
