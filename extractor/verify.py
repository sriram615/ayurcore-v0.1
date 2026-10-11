"""
Deterministic verification of an extracted ClinicalParse (AI-1 T3).

verify(note, spans, parsed, report) checks the output against the note, the
tagger's typed spans and the per-fact provenance, and returns
    {"verdict": "verified" | "rejected", "violations": [...], "checked_facts": n, "parse": parsed,
     "warnings": [...]}
where "parse" is the very object passed in and "warnings" (R5) never affect the verdict. It never edits, completes or
repairs anything: any violation rejects the whole output, and a rejected output
means ABSTAIN. An empty output whose provenance is consistent is verified.

Independent of T2: nothing here is imported from extractor/assemble.py and no
assembly decision (link rules, labels in the report) is trusted. The verifier
re-establishes everything from the note and the spans with its own rules,
reusing only low-level functions: the tokeniser, units.parse_grams,
normalize.resolve_herb / dosha_set, and the production formulary table.

Checks (violation codes):
  V1  output shape                                   malformed_output, malformed_provenance
  V2  exactly one fact per output herb, same order   missing_provenance, orphan_provenance, provenance_mismatch
  V3  span offsets, note substring, token bounds     bad_offsets, forged_span_text, partial_token_span, overlapping_spans
  V4  span roles in the tagger output                role_mismatch
  V5  herb in formulary, span resolves to it         unknown_herb, herb_span_mismatch, unresolvable_herb_span, ambiguous_resolution
  V6  whole grams 1..10000, span parses to it        invalid_grams, quantity_span_mismatch, range_quantity
  V7  linkage permissible                            cross_sentence_link, non_adjacent_link, unjustified_shared_quantity, herb_span_reused
  V8  canonical herbs unique                         duplicate_herb
  V9  dosha canonical and with provenance            invalid_dosha, dosha_without_provenance

Context checks (extractor/verify_context.py), run after V1-V9 against T3's own
reading of the note; each of these violations also carries "rule" R1-R4:
  V10 / R1  conflicting amounts for one herb;        conflicting_quantities, foreign_quantity
            the output's dose belongs to another item
  V11 / R2  alternative amounts or herbs             alternative_quantities, alternative_herbs,
                                                     conditional_alternative
  V12 / R3  past-use context of the output herb      historical_context
  V13 / R4  unresolved status (labels, negation)     contested_status, negated_context
  R5        WARNING ONLY, in "warnings"              unaccounted_quantity
"""
from __future__ import annotations

import re

from backend.herb_resolver import HERB_ALIASES

from extractor.labels import tokenize
from extractor.normalize import dosha_set, resolve_herb
from extractor.units import parse_grams
from extractor.verify_context import check_context

FORMULARY = frozenset(HERB_ALIASES.values())
GRAMS_MIN, GRAMS_MAX = 1, 10000
CANONICAL_DOSHAS = ("Vata", "Pitta", "Kapha")
SENTENCE_END = frozenset({".", "!", "?"})
DISTRIBUTIVE_WORDS = frozenset({"each", "veetham"})
DISTRIBUTIVE_WINDOW = 2
RANGE_IN_TEXT = re.compile(r"\d\s*(?:-|to)\s*\d")
RANGE_JOINERS = frozenset({"-", "to"})
OUTPUT_KEYS = frozenset({"dosha_imbalance", "prescribed_herbs"})
HERB_KEYS = frozenset({"name", "grams"})


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def verify(note: str, spans, parsed, report) -> dict:
    data = parsed.model_dump() if hasattr(parsed, "model_dump") else parsed
    violations: list[dict] = []
    warnings: list[dict] = []

    def flag(code: str, where: str, detail: str = "") -> None:
        violations.append({"code": code, "where": where, "detail": detail})

    def result(checked: int) -> dict:
        return {"verdict": "rejected" if violations else "verified", "violations": violations,
                "checked_facts": checked, "parse": parsed, "warnings": warnings}

    # V1 -- output and provenance shape
    if not isinstance(data, dict) or set(data) != OUTPUT_KEYS or not isinstance(data.get("dosha_imbalance"), str) \
            or not isinstance(data.get("prescribed_herbs"), list) \
            or any(not isinstance(h, dict) or set(h) != HERB_KEYS or not isinstance(h.get("name"), str)
                   for h in data["prescribed_herbs"]):
        flag("malformed_output", "parse", "not {dosha_imbalance: str, prescribed_herbs: [{name, grams}]}")
        return result(0)
    if not isinstance(report, dict) or not isinstance(report.get("facts", []), list) \
            or not isinstance(report.get("dosha_spans", []), list) or not isinstance(spans, (list, tuple)):
        flag("malformed_provenance", "report", "facts / dosha_spans / spans must be lists")
        return result(0)
    herbs, facts = data["prescribed_herbs"], report.get("facts", [])

    tokens = tokenize(note)
    starts = {s for _, s, _ in tokens}
    ends = {e for _, _, e in tokens}
    boundary_tokens = [i for i, (t, _, _) in enumerate(tokens) if t in SENTENCE_END]
    token_index = {s: i for i, (_, s, _) in enumerate(tokens)}
    end_index = {e: i for i, (_, _, e) in enumerate(tokens)}
    typed: dict[tuple[int, int], set] = {}
    for item in spans:
        try:
            s, e, kind = item
        except (TypeError, ValueError):
            flag("malformed_provenance", "spans", repr(item))
            return result(0)
        typed.setdefault((s, e), set()).add(kind)

    def sentence(char_pos: int) -> int:
        i = token_index[char_pos]
        return sum(b < i for b in boundary_tokens)

    def span_ok(span, where: str) -> bool:
        if not isinstance(span, dict) or not {"start", "end", "text"} <= set(span):
            flag("missing_provenance", where, "span with start/end/text required")
            return False
        s, e, text = span["start"], span["end"], span["text"]
        if not (_is_int(s) and _is_int(e) and 0 <= s < e <= len(note)):
            flag("bad_offsets", where, f"[{s},{e}) for a note of length {len(note)}")
            return False
        if note[s:e] != text:
            flag("forged_span_text", where, f"note has {note[s:e]!r}, provenance says {text!r}")
            return False
        if s not in starts or e not in ends:
            flag("partial_token_span", where, f"{text!r} does not start and end on token boundaries")
            return False
        return True

    # V2 -- one fact per output herb, in order, same values
    for i in range(len(herbs), len(facts)):
        flag("orphan_provenance", f"fact[{i}]", "provenance for a herb that is not in the output")
    for i in range(len(facts), len(herbs)):
        flag("missing_provenance", f"herb[{i}]", f"{herbs[i].get('name')!r} has no provenance")

    linked = []  # (fact index, herb span key, qty span key) for facts whose spans are valid
    for i, (herb, fact) in enumerate(zip(herbs, facts)):
        where = f"herb[{i}]"
        if not isinstance(fact, dict):
            flag("missing_provenance", where, "fact is not an object")
            continue
        if fact.get("name") != herb["name"] or fact.get("grams") != herb["grams"] \
                or type(fact.get("grams")) is not type(herb["grams"]):
            flag("provenance_mismatch", where, f"output {herb} vs fact {fact.get('name')!r}/{fact.get('grams')!r}")

        # V6 (value part) and V5 (membership) -- independent of spans
        if not (_is_int(herb["grams"]) and GRAMS_MIN <= herb["grams"] <= GRAMS_MAX):
            flag("invalid_grams", where, f"{herb['grams']!r} is not a whole number of grams in {GRAMS_MIN}..{GRAMS_MAX}")
        if herb["name"] not in FORMULARY:
            flag("unknown_herb", where, f"{herb['name']!r} is not in the formulary")

        h_ok = span_ok(fact.get("herb_span"), f"{where}.herb_span")
        q_ok = span_ok(fact.get("quantity_span"), f"{where}.quantity_span")
        if not (h_ok and q_ok):
            continue
        hs, qs = fact["herb_span"], fact["quantity_span"]
        hkey, qkey = (hs["start"], hs["end"]), (qs["start"], qs["end"])
        if hkey[0] < qkey[1] and qkey[0] < hkey[1]:
            flag("overlapping_spans", where, f"{hs['text']!r} overlaps {qs['text']!r}")
            continue

        # V4 -- roles as the tagger labelled them
        if "HERB_RX" not in typed.get(hkey, set()):
            flag("role_mismatch", f"{where}.herb_span", f"{hs['text']!r} is {sorted(typed.get(hkey, set())) or 'untyped'}, not HERB_RX")
        if "QTY" not in typed.get(qkey, set()):
            flag("role_mismatch", f"{where}.quantity_span", f"{qs['text']!r} is {sorted(typed.get(qkey, set())) or 'untyped'}, not QTY")

        # V5 -- the span text resolves to exactly this herb
        resolution = resolve_herb(hs["text"])
        if resolution["canonical"] is None:
            code = "ambiguous_resolution" if resolution["reason"] == "ambiguous" else "unresolvable_herb_span"
            flag(code, f"{where}.herb_span", f"{hs['text']!r}: {resolution['reason']} {resolution['candidates']}")
        elif resolution["canonical"] != herb["name"]:
            flag("herb_span_mismatch", f"{where}.herb_span", f"{hs['text']!r} resolves to {resolution['canonical']!r}, output says {herb['name']!r}")

        # V6 -- the quantity span says exactly these grams, and is not a range
        q_first = token_index[qkey[0]]
        before = [tokens[j][0].lower() for j in range(max(0, q_first - 2), q_first)]
        if RANGE_IN_TEXT.search(qs["text"].lower()) or (len(before) == 2 and before[1] in RANGE_JOINERS
                                                       and before[0][:1].isdigit()):
            flag("range_quantity", f"{where}.quantity_span", f"{qs['text']!r} is part of a range")
        else:
            grams, reason = parse_grams(qs["text"])
            if (grams, reason) != (herb["grams"], "ok") or not _is_int(herb["grams"]):
                flag("quantity_span_mismatch", f"{where}.quantity_span",
                     f"{qs['text']!r} parses to {grams!r} ({reason}), output says {herb['grams']!r}")
        linked.append((i, hkey, qkey))

    # V7 -- linkage permissibility, from the spans alone
    order = sorted(k for k, kinds in typed.items() if kinds & {"HERB_RX", "QTY"})
    position = {k: n for n, k in enumerate(order)}
    users: dict[tuple[int, int], list] = {}
    herb_uses: dict[tuple[int, int], int] = {}
    for i, hkey, qkey in linked:
        users.setdefault(qkey, []).append((i, hkey))
        herb_uses[hkey] = herb_uses.get(hkey, 0) + 1
    used_qty = set(users)
    for hkey, n in herb_uses.items():
        if n > 1:
            flag("herb_span_reused", f"span[{hkey[0]},{hkey[1]})", f"one herb span supports {n} facts")
    for qkey, fact_users in users.items():
        for i, hkey in fact_users:
            where = f"herb[{i}]"
            if hkey not in position or qkey not in position:
                continue  # already a role_mismatch
            if sentence(hkey[0]) != sentence(qkey[0]):
                flag("cross_sentence_link", where, "herb and quantity are in different sentences")
                continue
            lo, hi = sorted((position[hkey], position[qkey]))
            between = order[lo + 1:hi]
            if len(fact_users) > 1:
                after = {tokens[j][0].lower() for j in range(end_index[qkey[1]] + 1,
                                                             min(end_index[qkey[1]] + 1 + DISTRIBUTIVE_WINDOW, len(tokens)))}
                user_herbs = {h for _, h in fact_users}
                if not (after & DISTRIBUTIVE_WORDS) or hkey[0] > qkey[0] \
                        or any(k not in user_herbs for k in between):
                    flag("unjustified_shared_quantity", where,
                         f"quantity serves {len(fact_users)} herbs without a contiguous run and a distributive word")
            elif between:
                revision = (hkey[0] < qkey[0] and len(between) == 1 and "QTY" in typed[between[0]]
                            and between[0] not in used_qty and sentence(between[0][0]) == sentence(hkey[0]))
                if not revision:
                    flag("non_adjacent_link", where, f"{len(between)} herb/quantity span(s) between herb and quantity")

    # V8 -- unique canonical herbs
    names = [h["name"] for h in herbs]
    for name in sorted({n for n in names if names.count(n) > 1}):
        flag("duplicate_herb", "parse", f"{name!r} appears {names.count(name)} times")

    # V9 -- dosha
    dosha = data["dosha_imbalance"]
    if dosha:
        parts = dosha.split("-")
        if any(p not in CANONICAL_DOSHAS for p in parts) or len(set(parts)) != len(parts) \
                or parts != sorted(parts, key=CANONICAL_DOSHAS.index):
            flag("invalid_dosha", "dosha_imbalance", f"{dosha!r} is not an ordered set of {CANONICAL_DOSHAS}")
        else:
            named = set()
            for n, span in enumerate(report.get("dosha_spans", [])):
                where = f"dosha_spans[{n}]"
                if span_ok(span, where):
                    if "DOSHA" not in typed.get((span["start"], span["end"]), set()):
                        flag("role_mismatch", where, f"{span['text']!r} is not a DOSHA span")
                    else:
                        named |= set(dosha_set(span["text"]))
            for p in parts:
                if p not in named:
                    flag("dosha_without_provenance", "dosha_imbalance", f"{p!r} is named by no DOSHA span")

    # V10-V13 (R1-R4) context checks -- after V1-V9; R5 only warns
    context = check_context(note, spans, parsed, report)
    violations.extend(context["violations"])
    warnings.extend(context["warnings"])
    return result(len(linked))
