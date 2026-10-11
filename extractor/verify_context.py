"""
T3 context checks R1-R5, called by extractor.verify.verify() after V1-V9.

    check_context(note, spans, parsed, report) -> {"violations": [...], "warnings": [...]}

T3 (extractor/verify.py) checks the output against the tagger's own labels, so a
wrong but self-consistent labelling passes. These checks read the note
independently: T3's own scan finds every formulary herb mention and every
stated gram amount, and attaches each amount to a mention (nearest preceding
mention in the sentence, else the next one with no other amount in between).
Sentences end at . ! ? and, because Malayalam/Manglish is verb-final and notes
are often unpunctuated, after a clause-final finite verb (closed list + suffixes).
Nothing is imported from extractor/assemble.py; only the low-level tokenize,
parse_grams and resolve_herb are reused, as in verify.py.

  R1  conflicting_quantities   an output herb has >1 distinct amount in the note and the
                               output is not the textually last one (QTY_OTHER-labelled
                               amounts excluded)
  R2  alternative_quantities   the output amount is joined to another amount by a disjunction
      alternative_herbs        the output herb's unit is joined to another herb's unit by one
  R3  historical_context       a past-time cue in the same clause is associated with the
                               output herb (nearest herb unit, no present-time anchor between)
  R4  contested_status         the same herb is also labelled HERB_NEG / HERB_REPL
      negated_context          a negation word directly before the herb or after the amount
  R5  unaccounted_quantity     WARNING ONLY: a gram amount no output fact or label accounts for

v3 additions (structural gaps found in the S1-OOD v2 failure analysis; closed-class lexicons):
  scan  non-formulary items: the word directly before a dose (unless a formulary herb directly
        follows the dose), the word after "<dose> of", T1 UNSTOCKED spans; "<dose> of X" attaches to X
  R1  foreign_quantity         the output's dose is attached to a non-formulary item
  R2  alternative_quantities   also: a disjunction with up to 8 words between two doses, no item between
      conditional_alternative  a sentence with a conditional ("if", "-enkil") and a substitution marker
                               or disjunction, and two or more items
  R3  historical_context       also: past-tense use verbs (not after "be"/"to"), "before this visit",
                               a past-medication section header ("History: ..."); a re-order verb
                               ("repeat", "continue", "restart") before the herb in its clause exempts it
  R4  contested_status         also: another mention of the same herb with adjacent negation (label-free)
      negated_context          also: a negator + prescription action later in the sentence with no item
                               after it ("do not dispense"); anaphoric cancellation ("cancel that",
                               "athu venda") of the preceding item

Violations (R1-R4) are hard rejections in verify(); R5 is a warning that never
changes the verdict. Facts whose provenance is malformed are skipped here (V1-V3
already reject them). The function never edits the parse. Lexicon entries
carry their source: the S1-train construction templates or a documented closed
grammatical class -- never the S1-OOD v1 notes.
SYNTHETIC TECHNICAL RESULT, NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

from extractor.labels import tokenize
from extractor.normalize import dosha_set, resolve_herb
from extractor.units import parse_grams

SENTENCE_END = frozenset({".", "!", "?"})
CLAUSE_END = SENTENCE_END | {";", ":", ","}
MAX_AMOUNT_TOKENS = 6
MAX_MENTION_TOKENS = 3
NEGATION_WINDOW = 2
KG_WORDS = frozenset({"kg", "kgs", "kilo", "kilos", "kilogram", "kilograms"})

S1 = "S1-train construction template"
DISJUNCTIONS = {
    "or": "closed-class: English disjunctive coordinator",
    "either": "closed-class: English disjunctive correlative",
    "otherwise": "closed-class: English disjunctive adverb",
    "alternatively": "closed-class: English disjunctive adverb",
    "/": "closed-class: slash between alternatives",
    "allenkil": "closed-class: Malayalam disjunction (romanised)",
    "allengil": "closed-class: Malayalam disjunction (romanised)",
    "അല്ലെങ്കിൽ": "closed-class: Malayalam disjunction",
}
PAST_WORDS = {
    "previously": f"{S1} (HISTORY_EN)",
    "earlier": f"{S1} (HISTORY_EN)",
    "munpu": f"{S1} (HISTORY_MANGLISH)",
    "kazhinja": f"{S1} (HISTORY_MANGLISH)",
    "previous": "closed-class: English past-time adjective",
    "prior": "closed-class: English past-time adjective",
    "formerly": "closed-class: English past-time adverb",
    "ago": "closed-class: English past-time postposition",
    "yesterday": "closed-class: English deictic time adverb",
    "innale": "closed-class: Malayalam deictic time adverb (romanised)",
    "മുമ്പ്": "closed-class: Malayalam past-time adverb",
    "കഴിഞ്ഞ": "closed-class: Malayalam past-time adjective",
    "ഇന്നലെ": "closed-class: Malayalam deictic time adverb",
}
PAST_DETERMINERS = {"last": f"{S1} (HISTORY_EN 'last month')", "past": "closed-class: English past-time determiner"}
TIME_NOUNS = {w: "closed-class: English time nouns" for w in (
    "day", "days", "night", "week", "weeks", "fortnight", "month", "months", "year", "years", "time", "visit",
    "visits", "course", "season", "spring", "summer", "monsoon", "autumn", "winter")}
TIME_NOUNS.update({"maasam": f"{S1} (HISTORY_MANGLISH 'kazhinja maasam')"})
TIME_NOUNS.update({w: "closed-class: Malayalam time nouns (romanised and script)" for w in (
    "divasam", "aazhcha", "azhcha", "varsham", "ദിവസം", "ആഴ്ച", "മാസം", "വർഷം")})
# Malayalam is verb-final: a finite verb ends its clause even when the note has no
# punctuation. A boundary is placed after such a verb unless punctuation follows
# (then the punctuation decides, so "40 g venda, 50 g mathi" stays one segment).
MANGLISH_FINITE_VERBS = {
    "venam": f"{S1} (RX_MANGLISH)", "kodukku": f"{S1} (RX_MANGLISH)", "mathi": f"{S1} (SELF_CORRECTION_MANGLISH)",
    "venda": f"{S1} (NEG_MANGLISH)", "paranju": f"{S1} (UNSTOCKED_MANGLISH)",
    "und": "closed-class: Malayalam existential copula (romanised)",
    "undu": "closed-class: Malayalam existential copula (romanised)",
    "aanu": "closed-class: Malayalam copula (romanised)",
    "കൊടുക്കണം": f"{S1} (RX_ML)", "നൽകുക": f"{S1} (RX_ML)",
    "വേണം": "closed-class: Malayalam modal verb", "വേണ്ട": "closed-class: Malayalam negative modal verb",
}
MANGLISH_FINITE_SUFFIXES = {
    "anam": "closed-class: Malayalam obligative suffix -anam (romanised; kodukkanam, edukkanam, nirthanam)",
    "unnu": "closed-class: Malayalam present-tense suffix -unnu (romanised)",
    "unnund": "closed-class: Malayalam present-progressive suffix -unnund (romanised)",
    "irunnu": "closed-class: Malayalam pluperfect suffix -irunnu (romanised)",
    "enda": "closed-class: Malayalam prohibitive suffix -enda (romanised; kodukkenda)",
}
PAST_SUFFIXES = {"irunnu": f"{S1} (HISTORY_MANGLISH pluperfect -irunnu)",
                 "ിരുന്നു": "closed-class: Malayalam pluperfect suffix"}
PRESENT_ANCHORS = {w: "closed-class: present-time deixis" for w in (
    "now", "today", "currently", "presently", "ippol", "innu", "ഇപ്പോൾ", "ഇന്ന്")}
NEGATIONS = {
    "not": f"{S1} (NEG_EN)", "stop": f"{S1} (NEG_EN)", "avoid": f"{S1} (NEG_EN)", "discontinue": f"{S1} (NEG_EN)",
    "venda": f"{S1} (NEG_MANGLISH)", "nirthanam": f"{S1} (NEG_MANGLISH)", "alla": f"{S1} (SELF_CORRECTION_MANGLISH)",
    "no": "closed-class: English negator", "never": "closed-class: English negator",
    "without": "closed-class: English negative preposition", "cease": "closed-class: English cessation verb",
    "don": "closed-class: English negative contraction (don't)", "വേണ്ട": "closed-class: Malayalam negative",
    "അല്ല": "closed-class: Malayalam negative",
}
NEGATION_SUFFIXES = {"enda": f"{S1} (NEG_MANGLISH 'kodukkenda')"}

# --- v3 structural additions (closed classes; chosen after the S1-OOD v2 failure analysis) ---------------
# Words that can stand directly before a dose without naming an item, so a non-formulary word there is
# read as an unknown item (an unstocked plant). Closed English function-word classes + S1 template verbs.
NON_ITEM_WORDS = {w: "closed-class: English determiners / pronouns" for w in (
    "a", "an", "the", "this", "that", "these", "those", "it", "its", "each", "every", "another", "same", "total")}
NON_ITEM_WORDS.update({w: "closed-class: English prepositions / conjunctions" for w in (
    "of", "to", "for", "in", "on", "at", "by", "with", "from", "into", "per", "and", "plus", "also", "then", "than",
    "but", "as", "about", "around", "approximately", "approx", "nearly", "only", "just", "up", "upto", "till", "until")})
NON_ITEM_WORDS.update({w: "closed-class: English auxiliaries" for w in (
    "is", "are", "was", "were", "be", "been", "being", "has", "have", "had", "will", "would", "should", "shall",
    "can", "could", "may", "might", "must", "do", "does", "did")})
NON_ITEM_WORDS.update({w: "closed-class: English quantity modifiers" for w in ("more", "less", "extra", "further")})
NON_ITEM_WORDS.update({w: "closed-class: English discourse / correction markers" for w in (
    "sorry", "correction", "actually", "rather", "instead", "wait", "oh", "ok", "okay", "well", "so", "final",
    "revised", "make")})
NON_ITEM_WORDS.update({w: f"{S1} (RX_EN / NEG_EN / SELF_CORRECTION_EN verbs)" for w in (
    "give", "prescribe", "start", "rx", "discontinue")})
NON_ITEM_WORDS.update({w: "closed-class: English prescription verbs" for w in (
    "dispense", "add", "use", "take", "continue", "repeat", "restart", "resume", "change", "changed", "increase",
    "increased", "reduce", "reduced")})
NON_ITEM_WORDS.update({w: "closed-class: Manglish function words" for w in (
    "um", "yum", "ennu", "ini", "pinne", "mathram", "veetham", "koode", "koodi")})
CONDITIONALS = {w: "closed-class: English conditional subordinators" for w in ("if", "unless")}
CONDITIONAL_SUFFIXES = {"enkil": "closed-class: Malayalam conditional suffix -enkil (romanised)",
                        "engil": "closed-class: Malayalam conditional suffix -engil (romanised)",
                        "െങ്കിൽ": "closed-class: Malayalam conditional suffix"}
SUBSTITUTION_MARKERS = {"instead": f"{S1} (INSTEAD_OF_EN)", "replace": f"{S1} (INSTEAD_OF_EN)",
                        "pakaram": f"{S1} (INSTEAD_OF_MANGLISH)", "maatti": f"{S1} (INSTEAD_OF_MANGLISH)",
                        "substitute": "closed-class: English substitution verb",
                        "rather": "closed-class: English substitution adverb"}
HISTORY_HEADERS = {w: "closed-class: clinical-note section headers for past medication" for w in (
    "history", "hx", "pmh", "previous", "past", "earlier", "old")}
PAST_VERBS = {"took": f"{S1} (HISTORY_EN 'Previously took')", "completed": f"{S1} (HISTORY_EN 'course ... completed')"}
PAST_VERBS.update({w: "closed-class: English past-tense medication-use verbs" for w in (
    "finished", "had", "received", "tried", "used")})
PASSIVE_OR_MODAL = {w: "closed-class: English auxiliaries (a following participle is not a past-use report)"
                    for w in ("be", "been", "being", "to", "is", "are", "will", "should", "must", "can", "may")}
ENCOUNTER_NOUNS = {w: "closed-class: clinical encounter nouns ('before this visit')" for w in (
    "visit", "consultation", "admission", "review", "appointment")}
REORDER_VERBS = {w: "closed-class: English re-order verbs (re-issue a past prescription now)" for w in (
    "repeat", "continue", "restart", "resume", "renew")}
ACTION_WORDS = {w: f"{S1} (NEG_EN 'Do not give', 'is not needed')" for w in ("give", "needed")}
ACTION_WORDS.update({w: "closed-class: English prescription actions" for w in (
    "given", "dispense", "dispensed", "prescribe", "prescribed", "need", "required", "use", "take", "start", "be")})
CANCEL_VERBS = {w: "closed-class: English cancellation verbs" for w in (
    "cancel", "cancelled", "scratch", "ignore", "disregard", "delete", "strike", "withdraw")}
ANAPHORS = {w: "closed-class: demonstrative / anaphoric pronouns" for w in (
    "that", "this", "it", "above", "athu", "ithu", "അത്", "ഇത്")}
NEGATED_ACTION_WINDOW = 2
ALTERNATIVE_GAP = 8

LEXICONS = {"disjunctions": DISJUNCTIONS, "past_words": PAST_WORDS, "past_determiners": PAST_DETERMINERS,
            "time_nouns": TIME_NOUNS, "past_suffixes": PAST_SUFFIXES, "present_anchors": PRESENT_ANCHORS,
            "negations": NEGATIONS, "negation_suffixes": NEGATION_SUFFIXES,
            "manglish_finite_verbs": MANGLISH_FINITE_VERBS, "manglish_finite_suffixes": MANGLISH_FINITE_SUFFIXES,
            "non_item_words": NON_ITEM_WORDS, "conditionals": CONDITIONALS,
            "conditional_suffixes": CONDITIONAL_SUFFIXES, "substitution_markers": SUBSTITUTION_MARKERS,
            "history_headers": HISTORY_HEADERS, "past_verbs": PAST_VERBS, "passive_or_modal": PASSIVE_OR_MODAL,
            "encounter_nouns": ENCOUNTER_NOUNS, "reorder_verbs": REORDER_VERBS, "action_words": ACTION_WORDS,
            "cancel_verbs": CANCEL_VERBS, "anaphors": ANAPHORS}

RULE_OF = {"conflicting_quantities": "R1", "foreign_quantity": "R1",
           "alternative_quantities": "R2", "alternative_herbs": "R2", "conditional_alternative": "R2",
           "historical_context": "R3", "contested_status": "R4", "negated_context": "R4",
           "unaccounted_quantity": "R5"}


def _is_word(tok: str) -> bool:
    return tok.isalpha() or any("ഀ" <= ch <= "ൿ" for ch in tok)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _is_finite_verb(tok: str) -> bool:
    return tok in MANGLISH_FINITE_VERBS or (tok.isascii() and tok.isalpha() and any(
        tok.endswith(s) and len(tok) >= len(s) + 3 for s in MANGLISH_FINITE_SUFFIXES))


class _Scan:
    """T3's own reading of the note: tokens, sentences, clauses, amounts, mentions, attachments."""

    def __init__(self, note: str, unstocked_spans=()):
        self.note = note
        self.tokens = tokenize(note)
        self.low = [t.lower() for t, _, _ in self.tokens]
        # sentence: split at . ! ? and after a clause-final Malayalam verb (segment for attachment)
        # clause:   additionally split at ; : ,                            (scope for R3 / R4 cues)
        self.sentence, self.clause, self.ends_segment = [], [], []
        s = c = 0
        for k, t in enumerate(self.low):
            self.sentence.append(s)
            self.clause.append(c)
            nxt = self.tokens[k + 1][0] if k + 1 < len(self.tokens) else ""
            verb_end = _is_finite_verb(t) and nxt and (_is_word(nxt) or nxt[:1].isdigit())
            self.ends_segment.append(bool(verb_end))
            if t in SENTENCE_END or verb_end:
                s += 1
            if t in CLAUSE_END or verb_end:
                c += 1
        self.amounts = self._amounts()
        self.mentions = self._mentions()
        self._unknown_mentions(unstocked_spans)
        self.attached: dict[int, int] = {}  # amount index -> mention index
        self._attach()

    def _text(self, i: int, j: int) -> str:
        return self.note[self.tokens[i][1]:self.tokens[j][2]]

    def _amounts(self) -> list[dict]:
        out, i, n = [], 0, len(self.tokens)
        while i < n:
            for j in range(min(n, i + MAX_AMOUNT_TOKENS) - 1, i - 1, -1):
                grams, why = parse_grams(self._text(i, j))
                if grams is not None and why == "ok":
                    out.append({"i": i, "j": j, "start": self.tokens[i][1], "end": self.tokens[j][2], "grams": grams,
                                "kg": any(self.low[k] in KG_WORDS for k in range(i, j + 1))})
                    i = j + 1
                    break
            else:
                i += 1
        return out

    def _mentions(self) -> list[dict]:
        out, i, n = [], 0, len(self.tokens)
        while i < n:
            found = None
            if _is_word(self.tokens[i][0]):
                for j in range(min(n, i + MAX_MENTION_TOKENS) - 1, i - 1, -1):
                    if all(_is_word(self.tokens[k][0]) for k in range(i, j + 1)):
                        canonical = resolve_herb(self._text(i, j))["canonical"]
                        if canonical:
                            found = {"i": i, "j": j, "start": self.tokens[i][1], "end": self.tokens[j][2],
                                     "canonical": canonical}
                            break
            if found:
                out.append(found)
                i = found["j"] + 1
            else:
                i += 1
        return out

    def _covered(self, k: int) -> bool:
        return any(x["i"] <= k <= x["j"] for x in self.mentions)

    def _item_word(self, k: int) -> bool:
        """Could token k name an item? A word that is no herb, no function word, verb, dosha or cue word."""
        t = self.low[k]
        return _is_word(self.tokens[k][0]) and not self._covered(k) and t not in NON_ITEM_WORDS \
            and not _is_finite_verb(t) and not _is_negation(t) and not dosha_set(t) \
            and not any(t in table for table in (DISJUNCTIONS, PAST_WORDS, PAST_DETERMINERS, TIME_NOUNS,
                                                  PRESENT_ANCHORS, CONDITIONALS, ANAPHORS, CANCEL_VERBS))

    def _unknown_mentions(self, unstocked_spans) -> None:
        """Non-formulary items: the word directly before a dose ("Tulsi 30 g"), unless a formulary herb
        directly follows the dose (Manglish "70 g X venam"); the word after "<dose> of" ("25 g of Chandana");
        and spans the tagger labelled UNSTOCKED. They carry canonical None."""
        found = set()
        starts = {x["i"] for x in self.mentions}
        for a in self.amounts:
            k = a["i"] - 1
            if k >= 0 and self._item_word(k) and self.sentence[k] == self.sentence[a["i"]] \
                    and not (a["j"] + 1 in starts):
                found.add((k, k))
            if a["j"] + 2 < len(self.tokens) and self.low[a["j"] + 1] == "of" and self._item_word(a["j"] + 2):
                found.add((a["j"] + 2, a["j"] + 2))
        for s, e in unstocked_spans:
            ks = [k for k, (_, ts, te) in enumerate(self.tokens) if ts >= s and te <= e]
            if ks and not any(self._covered(k) for k in ks):
                found.add((min(ks), max(ks)))
        for i, j in sorted(found):
            if not any(self._covered(k) for k in range(i, j + 1)):
                self.mentions.append({"i": i, "j": j, "start": self.tokens[i][1], "end": self.tokens[j][2],
                                      "canonical": None, "unknown": True})
        self.mentions.sort(key=lambda x: x["i"])

    def _attach(self) -> None:
        self.attached = {}
        for a_idx, a in enumerate(self.amounts):
            of = [m for m, x in enumerate(self.mentions) if x["i"] == a["j"] + 2] \
                if a["j"] + 1 < len(self.tokens) and self.low[a["j"] + 1] == "of" else []
            if of:  # "<dose> of X" names its own item
                self.attached[a_idx] = of[0]
                continue
            same = [m for m, x in enumerate(self.mentions) if self.sentence[x["i"]] == self.sentence[a["i"]]]
            before = [m for m in same if self.mentions[m]["j"] < a["i"]]
            if before:
                self.attached[a_idx] = before[-1]
                continue
            after = [m for m in same if self.mentions[m]["i"] > a["j"]]
            if after:
                m = after[0]
                if not any(a["j"] < b["i"] < self.mentions[m]["i"] for b in self.amounts):
                    self.attached[a_idx] = m

    def ensure_mention(self, start: int, end: int, canonical: str) -> int:
        for m, x in enumerate(self.mentions):
            if _overlaps((x["start"], x["end"]), (start, end)):
                if x.get("unknown"):  # the output names it as a formulary herb: T3 follows the span, not the guess
                    x.update(canonical=canonical, unknown=False)
                return m
        i = next(k for k, t in enumerate(self.tokens) if t[1] >= start)
        j = max(k for k, t in enumerate(self.tokens) if t[2] <= end)
        self.mentions.append({"i": i, "j": j, "start": start, "end": end, "canonical": canonical})
        self.mentions.sort(key=lambda x: x["i"])
        self._attach()
        return next(m for m, x in enumerate(self.mentions) if x["start"] == start)

    def ensure_amount(self, start: int, end: int, grams) -> int:
        for a, x in enumerate(self.amounts):
            if _overlaps((x["start"], x["end"]), (start, end)):
                return a
        i = next(k for k, t in enumerate(self.tokens) if t[1] >= start)
        j = max(k for k, t in enumerate(self.tokens) if t[2] <= end)
        self.amounts.append({"i": i, "j": j, "start": start, "end": end, "grams": grams,
                             "kg": any(self.low[k] in KG_WORDS for k in range(i, j + 1))})
        self.amounts.sort(key=lambda x: x["i"])
        self._attach()
        return next(a for a, x in enumerate(self.amounts) if x["start"] == start)

    def unit(self, m: int) -> tuple[int, int]:
        """Token extent of a mention plus the amounts attached to it."""
        x = self.mentions[m]
        idx = [x["i"], x["j"]] + [k for a, mm in self.attached.items() if mm == m
                                  for k in (self.amounts[a]["i"], self.amounts[a]["j"])]
        return min(idx), max(idx)


def _past_cues(scan: _Scan) -> list[tuple[int, int]]:
    """Token extents of past-time cues."""
    cues, low = [], scan.low
    for k, t in enumerate(low):
        if t in PAST_WORDS or any(t.endswith(s) and len(t) > len(s) for s in PAST_SUFFIXES):
            cues.append((k, k + 1) if k + 1 < len(low) and low[k + 1] in TIME_NOUNS else (k, k))  # "kazhinja maasam"
        elif t in PAST_DETERMINERS:
            if k + 1 < len(low) and low[k + 1] in TIME_NOUNS:
                cues.append((k, k + 1))
            elif k > 0 and low[k - 1] == "the":  # "the last 2 weeks", "the past few days"
                nxt = [n for n in range(k + 1, min(len(low), k + 4)) if low[n] in TIME_NOUNS]
                if nxt:
                    cues.append((k, nxt[0]))
        elif t in PAST_VERBS and not (k > 0 and low[k - 1] in PASSIVE_OR_MODAL):
            cues.append((k, k))  # "took", "finished", "had" -- not "to be used"
        elif t == "before":  # "before this visit", "before today" -- never "before food"
            if k + 1 < len(low) and low[k + 1] in PRESENT_ANCHORS:
                cues.append((k, k + 1))
            elif k + 2 < len(low) and low[k + 1] in ("this", "the", "that") and low[k + 2] in ENCOUNTER_NOUNS:
                cues.append((k, k + 2))
    return cues


def _history_scopes(scan: "_Scan") -> list[tuple[int, int]]:
    """Token ranges under a past-medication section header ("History: ...", "Past hx: ..."), up to the
    sentence end or the next header (a word followed by ':')."""
    scopes, low, n = [], scan.low, len(scan.low)
    for k in range(n - 1):
        if low[k + 1] == ":" and (low[k] in HISTORY_HEADERS or (k > 0 and low[k - 1] in HISTORY_HEADERS)):
            end = k + 2
            while end < n and scan.sentence[end] == scan.sentence[k] \
                    and not (end + 1 < n and low[end + 1] == ":" and _is_word(scan.tokens[end][0])):
                end += 1
            scopes.append((k + 2, end - 1))
    return scopes


def _is_negation(tok: str) -> bool:
    return tok in NEGATIONS or (tok.isascii() and len(tok) > 5 and any(tok.endswith(s) for s in NEGATION_SUFFIXES))


def check_context(note: str, spans, parsed, report) -> dict:
    data = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
    herbs = data.get("prescribed_herbs", []) if isinstance(data, dict) else []
    facts = report.get("facts", []) if isinstance(report, dict) else []
    labelled: dict[str, list[tuple[int, int]]] = {}
    for s, e, kind in spans:
        labelled.setdefault(kind, []).append((s, e))
    scan = _Scan(note, labelled.get("UNSTOCKED", []))
    violations, warnings = [], []

    def flag(out, code, where, detail):
        out.append({"code": code, "rule": RULE_OF[code], "where": where, "detail": detail})

    starts = {s for _, s, _ in scan.tokens}
    ends = {e for _, _, e in scan.tokens}

    def aligned(span) -> bool:
        return isinstance(span, dict) and all(type(span.get(k)) is int for k in ("start", "end")) \
            and span["start"] in starts and span["end"] in ends and span["start"] < span["end"]

    fact_refs = []  # (index, name, grams, herb span key, quantity span key)
    for i, (herb, fact) in enumerate(zip(herbs, facts)):
        if not (isinstance(herb, dict) and isinstance(fact, dict) and isinstance(herb.get("name"), str)):
            continue  # malformed output / provenance is verify()'s job (V1-V3)
        hs, qs = fact.get("herb_span"), fact.get("quantity_span")
        if not (aligned(hs) and aligned(qs)) or _overlaps((hs["start"], hs["end"]), (qs["start"], qs["end"])):
            continue
        fact_refs.append((i, herb["name"], herb["grams"], (hs["start"], hs["end"]), (qs["start"], qs["end"])))
    for i, name, grams, hkey, qkey in fact_refs:  # the output's own spans are always part of the scan
        scan.ensure_mention(*hkey, name)
        scan.ensure_amount(*qkey, grams)
    refs = []  # indices are looked up once the scan is final (ensure_* may insert and re-sort)
    for i, name, grams, hkey, qkey in fact_refs:
        m = next(k for k, x in enumerate(scan.mentions) if _overlaps((x["start"], x["end"]), hkey))
        a = next(k for k, x in enumerate(scan.amounts) if _overlaps((x["start"], x["end"]), qkey))
        refs.append((i, name, grams, m, a))
    other = labelled.get("QTY_OTHER", [])

    # R1 -- conflicting quantities for the same herb
    for i, name, grams, m, a in refs:
        pool = {a} | {b for b, mm in scan.attached.items() if scan.mentions[mm]["canonical"] == name
                      and not any(_overlaps((scan.amounts[b]["start"], scan.amounts[b]["end"]), o) for o in other)}
        if len({scan.amounts[b]["grams"] for b in pool}) > 1:
            last = max(pool, key=lambda b: scan.amounts[b]["start"])
            if scan.amounts[last]["grams"] != grams:
                values = [scan.amounts[b]["grams"] for b in sorted(pool, key=lambda b: scan.amounts[b]["start"])]
                flag(violations, "conflicting_quantities", f"herb[{i}]",
                     f"{name!r} has amounts {values} in the note; output {grams} is not the last")
        owner = scan.attached.get(a)
        if owner is not None and scan.mentions[owner].get("unknown"):
            flag(violations, "foreign_quantity", f"herb[{i}]",
                 f"{note[scan.amounts[a]['start']:scan.amounts[a]['end']]!r} belongs to "
                 f"{note[scan.mentions[owner]['start']:scan.mentions[owner]['end']]!r}, not to {name!r}")

    # R2 -- alternatives joined by a disjunction
    def joined(lo: int, hi: int) -> bool:
        between = scan.low[lo + 1:hi]
        return any(t in DISJUNCTIONS for t in between) and all(t in DISJUNCTIONS or t == "," for t in between)

    def joined_loosely(lo: int, hi: int) -> bool:
        """Two doses with a disjunction between them and no item mention: "50 g or, if low, 30 g"."""
        return 0 < hi - lo - 1 <= ALTERNATIVE_GAP and any(t in DISJUNCTIONS for t in scan.low[lo + 1:hi]) \
            and not any(lo < x["i"] < hi for x in scan.mentions)

    alt_amounts = set()
    for p in range(len(scan.amounts) - 1):
        x, y = scan.amounts[p], scan.amounts[p + 1]
        if scan.sentence[x["i"]] == scan.sentence[y["i"]] and (joined(x["j"], y["i"]) or joined_loosely(x["j"], y["i"])):
            alt_amounts |= {p, p + 1}
    for p, x in enumerate(scan.amounts):
        k = x["i"]
        if k >= 2 and scan.low[k - 1] in DISJUNCTIONS and scan.tokens[k - 2][0][:1].isdigit():
            alt_amounts.add(p)  # "40 or 60 g", "40/60 g"
    units = sorted((scan.unit(m), m) for m in range(len(scan.mentions)))
    alt_mentions = set()
    for (u1, m1), (u2, m2) in zip(units, units[1:]):
        if scan.sentence[u1[0]] == scan.sentence[u2[0]] and u1[1] < u2[0] and joined(u1[1], u2[0]) \
                and scan.mentions[m1]["canonical"] != scan.mentions[m2]["canonical"]:
            alt_mentions |= {m1, m2}
    conditional_sentences = set()
    for sent in set(scan.sentence):
        ks = [k for k, s_ in enumerate(scan.sentence) if s_ == sent]
        words = [scan.low[k] for k in ks]
        conditional = any(w in CONDITIONALS or (w not in DISJUNCTIONS and any(
            w.endswith(x) and len(w) > len(x) for x in CONDITIONAL_SUFFIXES)) for w in words)
        offers = any(w in SUBSTITUTION_MARKERS or w in DISJUNCTIONS for w in words)
        items = sum(1 for x in scan.mentions if scan.sentence[x["i"]] == sent)
        if conditional and offers and items >= 2:  # "may use Punarnava 100 g instead if out of stock"
            conditional_sentences.add(sent)
    for i, name, grams, m, a in refs:
        if scan.sentence[scan.mentions[m]["i"]] in conditional_sentences:
            flag(violations, "conditional_alternative", f"herb[{i}]",
                 f"{name!r} is in a sentence offering a conditional substitute or alternative")
        if a in alt_amounts:
            flag(violations, "alternative_quantities", f"herb[{i}]",
                 f"{note[scan.amounts[a]['start']:scan.amounts[a]['end']]!r} is one of alternative amounts")
        if m in alt_mentions:
            flag(violations, "alternative_herbs", f"herb[{i}]", f"{name!r} is one of alternative herbs")

    # R3 -- historical context associated with the output herb
    def reordered(m: int) -> bool:
        """A re-order verb ("repeat", "continue", "restart") before the herb in its clause makes it current."""
        i0 = scan.mentions[m]["i"]
        return any(scan.low[k] in REORDER_VERBS for k in range(i0) if scan.clause[k] == scan.clause[i0])

    for c0, c1 in _past_cues(scan):
        clause = scan.clause[c0]
        candidates = []
        for m in range(len(scan.mentions)):
            lo, hi = scan.unit(m)
            ks = [k for k in range(lo, hi + 1) if scan.clause[k] == clause]
            if not ks:
                continue
            lo, hi = min(ks), max(ks)
            between = range(hi + 1, c0) if hi < c0 else range(c1 + 1, lo) if lo > c1 else range(0)
            if any(scan.low[k] in PRESENT_ANCHORS for k in between):
                continue
            distance = 0 if lo <= c1 and c0 <= hi else (c0 - hi if hi < c0 else lo - c1)
            candidates.append((distance, m))
        if not candidates:
            continue
        best = min(d for d, _ in candidates)
        associated = {m for d, m in candidates if d == best}
        cue = scan._text(c0, c1)
        for i, name, grams, m, a in refs:
            if m in associated and not reordered(m):
                flag(violations, "historical_context", f"herb[{i}]", f"past-time cue {cue!r} is associated with {name!r}")
    for lo, hi in _history_scopes(scan):
        for i, name, grams, m, a in refs:
            if lo <= scan.mentions[m]["i"] <= hi:
                flag(violations, "historical_context", f"herb[{i}]", f"{name!r} is under a past-medication header")

    # R4 -- unresolved status: contested labels, adjacent negation, negated action, anaphoric cancellation
    def negation_near(first: int, last: int) -> list[str]:
        window = []
        for k in range(first - 1, max(-1, first - 1 - NEGATION_WINDOW), -1):
            if (not _is_word(scan.tokens[k][0]) and scan.tokens[k][0] != "'") or scan.ends_segment[k]:
                break  # punctuation, or the verb that ends the previous clause
            window.append(k)
        for k in range(last + 1, min(len(scan.tokens), last + 1 + NEGATION_WINDOW)):
            if not _is_word(scan.tokens[k][0]) and scan.tokens[k][0] != "'":
                break
            window.append(k)
            if scan.ends_segment[k]:
                break  # this fact's own clause-final verb is included, nothing beyond it
        return [scan.tokens[k][0] for k in window if _is_negation(scan.low[k])]

    def negated_action_after(last: int) -> str | None:
        """Later in the same sentence, before any other item: a negator followed by a prescription action
        ("... - patient refused, do not dispense") that is not itself followed by an item it could target."""
        sent = scan.sentence[last]
        k = last + 1
        while k < len(scan.tokens) and scan.sentence[k] == sent and not scan._covered(k):
            if _is_negation(scan.low[k]) and any(scan.low[n] in ACTION_WORDS for n in
                                                 range(k + 1, min(len(scan.tokens), k + 1 + NEGATED_ACTION_WINDOW))):
                ahead = range(k + 1, min(len(scan.tokens), k + 5))
                if not any(scan._covered(n) for n in ahead):
                    return scan._text(k, min(len(scan.tokens) - 1, k + NEGATED_ACTION_WINDOW))
            k += 1
        return None

    def negative_need(n: int) -> bool:
        """"venda" / "kodukkenda" / "വേണ്ട" on their own, or a negator followed by an action ("not needed")."""
        t = scan.low[n]
        return t in ("venda", "വേണ്ട") or (t.isascii() and len(t) > 5 and t.endswith("enda")) or \
            (_is_negation(t) and any(scan.low[q] in ACTION_WORDS
                                     for q in range(n + 1, min(len(scan.tokens), n + 1 + NEGATED_ACTION_WINDOW))))

    cancelled = set()  # mention index -> cancelled by "cancel that" / "athu venda" / "that is not needed"
    for k, t in enumerate(scan.low):
        near = range(k + 1, min(len(scan.tokens), k + 3))
        if (t in CANCEL_VERBS and any(scan.low[n] in ANAPHORS for n in near)) or \
                (t in ANAPHORS and any(negative_need(n) for n in near)):
            before = [m for m, x in enumerate(scan.mentions) if x["j"] < k and scan.sentence[k] - scan.sentence[x["i"]] <= 1]
            if before:
                cancelled.add(before[-1])

    for i, name, grams, m, a in refs:
        for kind in ("HERB_NEG", "HERB_REPL"):
            for s, e in labelled.get(kind, []):
                if resolve_herb(note[s:e])["canonical"] == name:
                    flag(violations, "contested_status", f"herb[{i}]",
                         f"{name!r} is also labelled {kind} at {note[s:e]!r}")
        for m2, x2 in enumerate(scan.mentions):  # the same herb negated elsewhere, whatever the labels say
            if m2 != m and x2["canonical"] == name:
                lo2, hi2 = scan.unit(m2)
                hits2 = negation_near(lo2, hi2)
                if hits2:
                    flag(violations, "contested_status", f"herb[{i}]",
                         f"{name!r} is also mentioned with negation {hits2} at {note[x2['start']:x2['end']]!r}")
        x, y = scan.mentions[m], scan.amounts[a]
        first, last = min(x["i"], y["i"]), max(x["j"], y["j"])
        hits = negation_near(first, last)
        if hits:
            flag(violations, "negated_context", f"herb[{i}]", f"negation {hits} next to {name!r}")
        action = negated_action_after(last)
        if action:
            flag(violations, "negated_context", f"herb[{i}]", f"negated action {action!r} later in the sentence")
        if m in cancelled:
            flag(violations, "negated_context", f"herb[{i}]", f"{name!r} is cancelled by a following reference")

    # R5 -- unaccounted gram amounts (warning only)
    used = {a for _, _, _, _, a in refs}
    exempt_q = labelled.get("QTY_OLD", []) + other
    exempt_h = [x for kind in ("HERB_NEG", "HERB_HIST", "HERB_REPL", "UNSTOCKED") for x in labelled.get(kind, [])]
    for b, x in enumerate(scan.amounts):
        if b in used or x["kg"] or any(_overlaps((x["start"], x["end"]), q) for q in exempt_q):
            continue
        mm = scan.attached.get(b)
        if mm is not None and any(_overlaps((scan.mentions[mm]["start"], scan.mentions[mm]["end"]), h) for h in exempt_h):
            continue
        flag(warnings, "unaccounted_quantity", f"amount[{x['start']},{x['end']})",
             f"{note[x['start']:x['end']]!r} is not used by any output fact or accounted for by a label")
    return {"violations": violations, "warnings": warnings}
