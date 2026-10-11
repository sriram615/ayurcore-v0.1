"""
Deterministic assembly of typed spans into a ClinicalParse (AI-1 T2).

Downstream of the T1 tagger and independent of it: input is the note text and
spans [(start, end, type)] -- from the tagger, or gold spans for an oracle
evaluation. No learning happens here. Rules (approved T2 design):

  L1  Roles come only from the spans. Only HERB_RX produces output; QTY_OLD,
      QTY_OTHER, HERB_NEG/HIST/REPL and UNSTOCKED never produce output and never
      take part in linking.
  L2  Sentences end only at "." "!" "?" tokens (";" and "," occur inside one
      prescription line). A note without punctuation is one sentence.
  L3  Candidate pairs: a HERB_RX and a QTY in the same sentence, adjacent in
      the ordered list of HERB_RX/QTY spans (none of either between them).
  L4  One-to-one greedy matching, smallest token gap first; ties go to the
      quantity after the herb, then to the earliest herb.
  L5  Shared quantity: a linked QTY followed within 2 tokens by a distributive
      word ("each", Manglish "veetham") also goes to every unlinked HERB_RX in
      the unbroken HERB_RX run just before it. No marker -> no sharing.
  L6  Same-line revision fallback -- ADDRESSES A KNOWN T1 DEV FAILURE
      (s1-negation-053, where the tagger labelled a superseded quantity as QTY):
      a QTY left unlinked that directly follows a herb's linked QTY (same
      sentence, nothing between in HERB_RX/QTY order) replaces it. Fitted with
      dev visible; every use is reported. Results that depend on it are not
      unbiased evidence.
  L7  Quantity -> grams (extractor/units.py). A quantity directly preceded by a
      number and "-" / "to" is a range and is rejected as ambiguous. A number
      without a unit is rejected. Grams are never inferred.
  L8  Herb -> canonical name (extractor/normalize.py): exact, suffix, unique
      fuzzy; otherwise unresolved and dropped (abstain).
  L9  Dosha: words of DOSHA spans -> canonical set -> "Vata-Pitta"; none -> "".
  L10 A herb without a usable quantity, and a quantity without a herb, are
      dropped and reported.
  L11 The same canonical herb twice: the first is kept, the conflict reported;
      quantities are never summed.
  L12 Output in note order; every fact keeps the spans it came from.
"""
from __future__ import annotations

import re

from backend.schemas import ClinicalParse

from extractor.labels import tokenize
from extractor.normalize import dosha_set, resolve_herb
from extractor.units import parse_grams

SENTENCE_END = {".", "!", "?"}
SHARED_MARKERS = {"each", "veetham"}
SHARED_MARKER_WINDOW = 2
RANGE_JOINERS = {"-", "to"}
RANGE_INSIDE = re.compile(r"\d\s*(?:-|to)\s*\d")
L6_NOTE = "L6 same-line revision: fallback addressing known T1 dev failure s1-negation-053"


def assemble(note: str, spans: list[tuple[int, int, str]]) -> tuple[ClinicalParse, dict]:
    """(ClinicalParse, report). The report holds per-fact provenance and every
    dropped item with its reason."""
    tokens = tokenize(note)
    first_tok = {s: i for i, (_, s, _) in enumerate(tokens)}
    last_tok = {e: i for i, (_, _, e) in enumerate(tokens)}
    ends = [i for i, (t, _, _) in enumerate(tokens) if t in SENTENCE_END]

    def sentence(i: int) -> int:
        return sum(b < i for b in ends)

    items = []
    for s, e, kind in sorted(spans):
        if kind in ("HERB_RX", "QTY"):
            items.append({"start": s, "end": e, "type": kind, "text": note[s:e],
                          "t0": first_tok[s], "t1": last_tok[e]})
    for k, item in enumerate(items):
        item["k"] = k
    herbs = [x for x in items if x["type"] == "HERB_RX"]
    qtys = [x for x in items if x["type"] == "QTY"]

    # L3 + L4: adjacent candidate pairs, greedy by (gap, following-first, herb position)
    pairs = []
    for h in herbs:
        for q in qtys:
            if abs(h["k"] - q["k"]) != 1 or sentence(h["t0"]) != sentence(q["t0"]):
                continue
            first, second = (h, q) if h["t0"] < q["t0"] else (q, h)
            gap = second["t0"] - first["t1"] - 1
            pairs.append((gap, 0 if q["t0"] > h["t0"] else 1, h["t0"], h["k"], q["k"]))
    pairs.sort()
    link: dict[int, int] = {}   # herb k -> qty k
    rule: dict[int, str] = {}
    used_q = set()
    for _, _, _, hk, qk in pairs:
        if hk in link or qk in used_q:
            continue
        link[hk], rule[hk] = qk, "L4 adjacent"
        used_q.add(qk)

    # L5: shared quantity
    for q in qtys:
        if q["k"] not in used_q:
            continue
        after = {tokens[j][0].lower() for j in range(q["t1"] + 1, min(q["t1"] + 1 + SHARED_MARKER_WINDOW, len(tokens)))}
        if not after & SHARED_MARKERS:
            continue
        for x in reversed(items[:q["k"]]):
            if x["type"] != "HERB_RX" or sentence(x["t0"]) != sentence(q["t0"]):
                break
            if x["k"] not in link:
                link[x["k"]], rule[x["k"]] = q["k"], "L5 shared quantity"

    # L6: same-line revision fallback
    revisions = []
    for k in range(len(items) - 2):
        h, q1, q2 = items[k], items[k + 1], items[k + 2]
        if (h["type"], q1["type"], q2["type"]) == ("HERB_RX", "QTY", "QTY") and link.get(h["k"]) == q1["k"] \
                and q2["k"] not in used_q and sentence(h["t0"]) == sentence(q2["t0"]):
            link[h["k"]], rule[h["k"]] = q2["k"], "L6 same-line revision"
            used_q.add(q2["k"])
            revisions.append({"herb": h["text"], "superseded": q1["text"], "final": q2["text"], "note": L6_NOTE})

    report = {"facts": [], "dropped_herbs": [], "dropped_quantities": [], "conflicts": [],
              "l6_revisions": revisions, "dosha_spans": []}
    out, kept = [], {}
    for h in herbs:
        resolution = resolve_herb(h["text"])
        herb_span = {"start": h["start"], "end": h["end"], "text": h["text"]}
        if resolution["canonical"] is None:
            report["dropped_herbs"].append({**herb_span, "reason": f"unresolved:{resolution['reason']}",
                                            "candidates": resolution["candidates"]})
            continue
        if h["k"] not in link:
            report["dropped_herbs"].append({**herb_span, "reason": "no_quantity"})
            continue
        q = items[link[h["k"]]]
        qty_span = {"start": q["start"], "end": q["end"], "text": q["text"]}
        before = [tokens[j][0].lower() for j in range(max(0, q["t0"] - 2), q["t0"])]
        if (len(before) == 2 and before[1] in RANGE_JOINERS and before[0][:1].isdigit()) \
                or RANGE_INSIDE.search(q["text"].lower()):
            grams, why = None, "range"
        else:
            grams, why = parse_grams(q["text"])
        if grams is None:
            report["dropped_herbs"].append({**herb_span, "reason": f"quantity:{why}", "quantity": qty_span})
            continue
        canonical = resolution["canonical"]
        if canonical in kept:
            report["conflicts"].append({"herb": canonical, "kept": kept[canonical], "dropped": herb_span})
            continue
        kept[canonical] = herb_span
        out.append({"name": canonical, "grams": grams})
        report["facts"].append({"name": canonical, "grams": grams, "herb_span": herb_span, "quantity_span": qty_span,
                                "normalisation": resolution["method"], "link_rule": rule[h["k"]]})
    report["dropped_quantities"] = [{"start": q["start"], "end": q["end"], "text": q["text"], "reason": "no_herb"}
                                    for q in qtys if q["k"] not in used_q]

    doshas = []
    for s, e, kind in sorted(spans):
        if kind == "DOSHA":
            found = dosha_set(note[s:e])
            report["dosha_spans"].append({"start": s, "end": e, "text": note[s:e], "doshas": found})
            doshas += [d for d in found if d not in doshas]
    dosha = "-".join(d for d in ("Vata", "Pitta", "Kapha") if d in doshas)
    return ClinicalParse(dosha_imbalance=dosha, prescribed_herbs=out), report
