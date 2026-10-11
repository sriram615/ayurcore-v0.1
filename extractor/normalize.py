"""
Herb and dosha normalisation (AI-1 T2, rules L8-L9). Deterministic; no learning.

resolve_herb tries, in order, and stops at the first step that applies:
  1. exact     the production alias table (backend/herb_resolver.HERB_ALIASES)
  2. suffix    a Manglish case/coordination suffix removed (-yum, -vum, -um,
               -inu, -nu), and the remainder is an exact alias
  3. fuzzy     edit distance <= 1 (one insert/delete/substitute, or one swap of
               adjacent letters) to single-word ASCII aliases of 5+ letters --
               accepted ONLY if exactly one canonical herb is reachable;
               two or more -> "ambiguous", none -> "unknown"
It never guesses: anything else is unresolved, and the caller abstains.
"""
from __future__ import annotations

import re

from backend.herb_resolver import HERB_ALIASES

MANGLISH_SUFFIXES = ("yum", "vum", "inu", "um", "nu")
FUZZY_MIN_LENGTH = 5
CANONICAL_DOSHAS = ("Vata", "Pitta", "Kapha")
DOSHA_WORDS = {
    "vata": "Vata", "vaata": "Vata", "vatha": "Vata", "vaatha": "Vata", "vatham": "Vata", "vaatham": "Vata",
    "വാതം": "Vata",
    "pitta": "Pitta", "pita": "Pitta", "pitha": "Pitta", "pittha": "Pitta", "pitham": "Pitta", "pittham": "Pitta",
    "പിത്തം": "Pitta",
    "kapha": "Kapha", "kafa": "Kapha", "kapham": "Kapha", "kafam": "Kapha", "കഫം": "Kapha",
}


def _within_one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        return len(diffs) == 1 or (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                                   and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def resolve_herb(text: str, aliases: dict[str, str] = HERB_ALIASES) -> dict:
    """{"canonical": str | None, "method": exact|suffix|fuzzy|None,
        "reason": ok|ambiguous|unknown, "candidates": [...]}."""
    t = " ".join(text.lower().split())
    if t in aliases:
        return {"canonical": aliases[t], "method": "exact", "reason": "ok", "candidates": [aliases[t]]}
    stripped = sorted({aliases[t[:-len(s)]] for s in MANGLISH_SUFFIXES
                       if t.endswith(s) and t[:-len(s)] in aliases})
    if len(stripped) == 1:
        return {"canonical": stripped[0], "method": "suffix", "reason": "ok", "candidates": stripped}
    if len(stripped) > 1:
        return {"canonical": None, "method": None, "reason": "ambiguous", "candidates": stripped}
    if t.isascii() and t.isalpha() and len(t) >= FUZZY_MIN_LENGTH:
        near = sorted({c for a, c in aliases.items()
                       if a.isascii() and a.isalpha() and len(a) >= FUZZY_MIN_LENGTH and _within_one_edit(t, a)})
        if len(near) == 1:
            return {"canonical": near[0], "method": "fuzzy", "reason": "ok", "candidates": near}
        if len(near) > 1:
            return {"canonical": None, "method": None, "reason": "ambiguous", "candidates": near}
    return {"canonical": None, "method": None, "reason": "unknown", "candidates": []}


def dosha_set(text: str) -> list[str]:
    """Canonical doshas named in a DOSHA span, in canonical order."""
    found = {DOSHA_WORDS[w] for w in re.findall(r"[a-zഀ-ൿ]+", text.lower()) if w in DOSHA_WORDS}
    return [d for d in CANONICAL_DOSHAS if d in found]
