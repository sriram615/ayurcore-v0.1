"""
Tokenisation and gold span labels for S1 (deterministic, read-only on S1).

Each S1 case carries its construction record in `s1_meta`: the exact surface
text of every herb mention and its role, every quantity text, and the dosha
surface. `case_spans` locates those strings in the note at token boundaries
and turns them into typed spans; `bio_tags` turns spans into BIO tags.
Anything that cannot be located exactly raises AlignmentError -- it is
reported, never guessed.

Span types:
  HERB_RX     herb prescribed in this note
  HERB_NEG    herb stopped / avoided / not given
  HERB_HIST   herb from past medication
  HERB_REPL   herb replaced ("X instead of Y": Y)
  UNSTOCKED   plant the clinic does not stock
  QTY         the final quantity of a prescribed herb (one span for a shared "... each")
  QTY_OLD     a quantity the doctor revised in the same line ("20 g, no, 30 g": 20 g)
  QTY_OTHER   a quantity attached to a negated or historical herb
  DOSHA       the dosha phrase
"""
from __future__ import annotations

import re

TOKEN_RE = re.compile(r"[ഀ-ൿ‌‍]+|[A-Za-z]+|\d+(?:\.\d+)?|\S")

SPAN_TYPES = ("HERB_RX", "HERB_NEG", "HERB_HIST", "HERB_REPL", "UNSTOCKED", "QTY", "QTY_OLD", "QTY_OTHER", "DOSHA")
TAGS = ("O",) + tuple(f"{prefix}-{t}" for t in SPAN_TYPES for prefix in ("B", "I"))
ROLE_TO_TYPE = {"prescribed": "HERB_RX", "negated": "HERB_NEG", "history": "HERB_HIST",
                "replaced": "HERB_REPL", "unstocked": "UNSTOCKED"}


class AlignmentError(ValueError):
    """A construction-record string could not be located exactly in the note."""


def tokenize(text: str) -> list[tuple[str, int, int]]:
    """[(token, start, end)]. Malayalam runs, Latin words and numbers (with a
    decimal part) are tokens; every other non-space character is its own token,
    so "270g" -> "270", "g" and "3 x 50g" -> "3", "x", "50", "g"."""
    return [(m.group(0), m.start(), m.end()) for m in TOKEN_RE.finditer(text)]


def _occurrences(text: str, needle: str, tokens) -> list[tuple[int, int]]:
    """Occurrences of `needle` that start and end on token boundaries
    (so "20 g" is not found inside "120 g")."""
    starts = {s for _, s, _ in tokens}
    ends = {e for _, _, e in tokens}
    found, i = [], text.find(needle)
    while i != -1:
        j = i + len(needle)
        if i in starts and j in ends:
            found.append((i, j))
        i = text.find(needle, i + 1)
    return found


def _herb_occurrences(text: str, surface: str, tokens) -> list[tuple[int, int]]:
    """Token-aligned occurrences; if there are none, occurrences that start a
    token and end inside it, extended to the end of that token. That is a
    Manglish case suffix fused to the name ("arjunanu pakaram" = "instead of
    arjuna"): the whole inflected token is the mention."""
    exact = _occurrences(text, surface, tokens)
    if exact:
        return exact
    found = []
    for _, s, e in tokens:
        if text.startswith(surface, s) and s + len(surface) < e:
            found.append((s, e))
    return found


def case_spans(case: dict) -> list[tuple[int, int, str]]:
    """Gold spans [(start, end, type)] for one S1 case, sorted by start."""
    note, meta, cid = case["notes"], case["s1_meta"], case["case_id"]
    tokens = tokenize(note)
    spans = []

    for m in meta["mentions"]:
        occ = _herb_occurrences(note, m["surface"], tokens)
        if len(occ) != 1:
            raise AlignmentError(f"{cid}: herb surface {m['surface']!r} found {len(occ)} times")
        spans.append((*occ[0], ROLE_TO_TYPE[m["role"]]))

    # Quantities, in mention order (= note order). A shared "... each" quantity is one span.
    needed: list[tuple[str, str]] = []
    shared_seen = set()
    for m in meta["mentions"]:
        if m.get("superseded_quantity_text"):
            needed.append((m["superseded_quantity_text"], "QTY_OLD"))
        if m.get("quantity_text"):
            if m.get("shared_quantity"):
                if m["quantity_text"] in shared_seen:
                    continue
                shared_seen.add(m["quantity_text"])
            needed.append((m["quantity_text"], "QTY" if m["role"] == "prescribed" else "QTY_OTHER"))
    by_text: dict[str, list[str]] = {}
    for text, kind in needed:
        by_text.setdefault(text, []).append(kind)
    # Longest first, and never inside an already-placed quantity: "25 g" is not
    # the "25 g" at the end of "2 x 25 g".
    placed: list[tuple[int, int]] = []
    for text in sorted(by_text, key=lambda t: (-len(t), t)):
        kinds = by_text[text]
        occ = [(s, e) for s, e in _occurrences(note, text, tokens)
               if not any(s < pe and ps < e for ps, pe in placed)]
        if len(occ) != len(kinds):
            raise AlignmentError(f"{cid}: quantity {text!r} needed {len(kinds)} times, found {len(occ)}")
        spans.extend((s, e, kind) for (s, e), kind in zip(occ, kinds))
        placed.extend(occ)

    occ = _occurrences(note, meta["dosha_surface"], tokens)
    if not occ:
        raise AlignmentError(f"{cid}: dosha surface {meta['dosha_surface']!r} not found")
    spans.append((*occ[0], "DOSHA"))

    spans.sort()
    for (s1, e1, t1), (s2, e2, t2) in zip(spans, spans[1:]):
        if s2 < e1:
            raise AlignmentError(f"{cid}: overlapping spans {t1} [{s1},{e1}) and {t2} [{s2},{e2})")
    return spans


def bio_tags(tokens, spans) -> list[str]:
    tags = ["O"] * len(tokens)
    for start, end, kind in spans:
        inside = [i for i, (_, s, e) in enumerate(tokens) if s >= start and e <= end]
        if not inside:
            raise AlignmentError(f"span {kind} [{start},{end}) covers no token")
        tags[inside[0]] = f"B-{kind}"
        for i in inside[1:]:
            tags[i] = f"I-{kind}"
    return tags


def tags_to_spans(tokens, tags) -> list[tuple[int, int, str]]:
    """BIO -> [(start, end, type)]. An I- tag that does not continue a span of
    the same type starts a new one (lenient decoding of model output)."""
    spans, current = [], None
    for (_, s, e), tag in zip(tokens, tags):
        if tag == "O":
            if current:
                spans.append(tuple(current))
            current = None
            continue
        prefix, kind = tag.split("-", 1)
        if prefix == "B" or current is None or current[2] != kind:
            if current:
                spans.append(tuple(current))
            current = [s, e, kind]
        else:
            current[1] = e
    if current:
        spans.append(tuple(current))
    return spans
