"""
Manglish / Malayalam case-ending splitter for AI-2 v2 (Block 4B). Deterministic, closed lists, text only.

    split_token(token) -> base word or None
    normalize_morphology(text) -> {"text": rewritten lower-case text, "splits": [(token, base), ...]}

A token is rewritten to its base only when
  - the whole token is NOT already a known word (herb alias, cue / neutral / unsupported / screen / person
    lexicon entry or stem), and
  - it ends with one suffix of the closed lists below (longest first), and
  - the remaining base (at least 3 characters) IS a known word.
So known English words are never touched, unknown bases are never invented, and the rewrite is recorded.
Runs after memory.normalize and before the scope screen, so every later layer sees base words.
"""
from __future__ import annotations

from functools import lru_cache

from backend.herb_resolver import HERB_ALIASES
from memory import coverage, reference, scope
from memory.interpret import CUES
from memory.planner import DOSHA_WORDS, NUMBER_WORDS, TOKEN

MANGLISH_SUFFIXES = ("yudeyum", "udeyum", "ileyum", "ilum", "ile", "il", "inte", "yude", "ude", "kkum", "kku", "yaa",
                     "aa", "um", "o")
MALAYALAM_SUFFIXES = ("യുടെയും", "ുടെയും", "യുള്ള", "ുള്ള", "ിലെയും", "ിലെ", "ിൽ", "ിന്റെ", "യുടെ", "ുടെ", "ക്ക്", "ും")
SUFFIXES = tuple(sorted(MANGLISH_SUFFIXES + MALAYALAM_SUFFIXES, key=len, reverse=True))


@lru_cache(maxsize=1)
def _lexicon() -> tuple[frozenset, tuple]:
    words = {a for a in HERB_ALIASES if " " not in a} | set(DOSHA_WORDS) | set(NUMBER_WORDS)
    stems: list[str] = []
    for w, prefixes, _ in CUES.values():
        words |= set(w)
        stems += list(prefixes)
    words |= coverage.NEUTRAL | coverage.UNSUPPORTED | reference.PERSON | reference.GROUP | reference.IDENTITY
    stems += list(reference.PERSON_STEMS) + list(reference.GROUP_STEMS) + list(reference.KIN_STEMS)
    for name in dir(scope):
        value = getattr(scope, name)
        if name.isupper() and isinstance(value, (set, frozenset)):
            words |= {v for v in value if isinstance(v, str)}
    return frozenset(words), tuple(s for s in stems if s)


def _known(token: str) -> bool:
    words, stems = _lexicon()
    return token in words or token.startswith(stems)


def split_token(token: str):
    if _known(token):
        return None
    for suffix in SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            base = token[: -len(suffix)]
            if _known(base):
                return base
    return None


def normalize_morphology(text: str) -> dict:
    low = text.lower()
    out, last, splits = [], 0, []
    for m in TOKEN.finditer(low):
        base = split_token(m.group())
        if base is not None:
            out.append(low[last:m.start()] + base)
            last = m.end()
            splits.append((m.group(), base))
    out.append(low[last:])
    return {"text": "".join(out), "splits": splits}
