"""
Token features for the T1 tagger. Pure functions of the token sequence.

Besides the words themselves (lower-cased, character prefixes/suffixes, shape,
script) the features use three small, documented lexicons:
  - ALIAS_WORDS: every word of the production formulary alias table
    (backend/herb_resolver.HERB_ALIASES) -- the clinic's own vocabulary;
  - UNIT_WORDS / NUMBER_WORDS: units of mass and English number words.
They are knowledge about the domain, not about S1: no S1 template, cue list
or generator code is used here.
"""
from __future__ import annotations

from backend.herb_resolver import HERB_ALIASES

from extractor.labels import tokenize

ALIAS_WORDS = frozenset(word.lower() for alias in HERB_ALIASES for word, _, _ in tokenize(alias))
UNIT_WORDS = frozenset({"g", "gm", "gms", "gr", "gram", "grams", "kg", "kgs", "kilo", "kilos",
                        "kilogram", "kilograms", "ഗ്രാം"})
NUMBER_WORDS = frozenset({"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                          "eleven", "twelve", "fifteen", "twenty", "thirty", "forty", "fifty", "sixty",
                          "seventy", "eighty", "ninety", "hundred"})
WINDOW = 3


def _script(token: str) -> str:
    if any("ഀ" <= ch <= "ൿ" for ch in token):
        return "ml"
    if token[0].isdigit():
        return "num"
    if token.isalpha():
        return "latin"
    return "punct"


def _shape(token: str) -> str:
    out = []
    for ch in token:
        c = ("X" if ch.isupper() else "x") if ch.isascii() and ch.isalpha() else \
            "d" if ch.isdigit() else "m" if "ഀ" <= ch <= "ൿ" else ch
        if not out or out[-1] != c:
            out.append(c)
    return "".join(out)


def _word_class(token: str) -> str:
    low = token.lower()
    if low in UNIT_WORDS:
        return "unit"
    if low in NUMBER_WORDS:
        return "numword"
    if low in ALIAS_WORDS:
        return "alias"
    return _script(token)


def sentence_features(tokens: list[tuple[str, int, int]]) -> list[list[str]]:
    """One de-duplicated feature-string list per token."""
    words = [t for t, _, _ in tokens]
    lows = [w.lower() for w in words]
    classes = [_word_class(w) for w in words]
    feats = []
    for i, (w, low) in enumerate(zip(words, lows)):
        f = ["bias", f"w={low}", f"shape={_shape(w)}", f"script={_script(w)}", f"class={classes[i]}"]
        for n in (2, 3, 4):
            if len(low) > n:
                f += [f"pre{n}={low[:n]}", f"suf{n}={low[-n:]}"]
        if low in ALIAS_WORDS:
            f.append("in_alias_table")
        for off in range(-WINDOW, WINDOW + 1):
            if off == 0:
                continue
            j = i + off
            if 0 <= j < len(words):
                f += [f"w[{off}]={lows[j]}", f"class[{off}]={classes[j]}"]
            else:
                f.append(f"w[{off}]=<{'s' if j < 0 else '/s'}>")
        if i > 0:
            f.append(f"class[-1..0]={classes[i - 1]}|{classes[i]}")
        if i + 1 < len(words):
            f.append(f"class[0..1]={classes[i]}|{classes[i + 1]}")
        feats.append(sorted(set(f)))
    return feats
