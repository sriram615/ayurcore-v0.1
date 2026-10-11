"""
Quantity text -> total grams (AI-1 T2, rule L7). Deterministic; no learning.

Accepted forms (whole text, case-insensitive):
  packs     "3 x 50 g", "3 x 50g", "2 packets of 25 g"   -> count * each
  kg        "1.5 kg", "2 kilo"                          -> * 1000, must be whole grams
  grams     "20 g", "20g", "20 gm", "20 gms", "20 grams", "20 ഗ്രാം"
  words     "eighty grams", "twenty five grams", "one hundred grams"
Result must be a whole number of grams in 1..GRAMS_MAX (backend PrescribedHerb.grams).

Never inferred: a number without a unit is rejected ("unitless"); ranges are
rejected by the assembler (the range context lies outside the quantity span);
frequency/duration words are not part of a quantity and never multiply it.
"""
from __future__ import annotations

import re

GRAMS_MAX = 10000  # mirrors backend/schemas.py PrescribedHerb.grams le=10000
_GRAM = r"(?:g|gm|gms|gr|gram|grams|ഗ്രാം)"
_KG = r"(?:kg|kgs|kilo|kilos|kilogram|kilograms)"
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
                 "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
                 "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
                 "eighty": 80, "ninety": 90}
_PACKS = re.compile(rf"(\d+)\s*(?:x|×|packets?\s+of|packs?\s+of)\s*(\d+)\s*{_GRAM}")
_KILOS = re.compile(rf"(\d+(?:\.\d+)?)\s*{_KG}")
_GRAMS = re.compile(rf"(\d+)\s*{_GRAM}")
_WORDS = re.compile(rf"([a-z]+(?:[\s-][a-z]+)*)\s+{_GRAM}")
_BARE_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _words_to_int(text: str) -> int | None:
    total = 0
    for word in text.replace("-", " ").split():
        if word == "hundred":
            total = (total or 1) * 100
        elif word in _NUMBER_WORDS:
            total += _NUMBER_WORDS[word]
        else:
            return None
    return total


def parse_grams(text: str) -> tuple[int | None, str]:
    """(grams, "ok") or (None, reason): unitless | unparseable | non_integer_grams | out_of_range."""
    t = " ".join(text.lower().split())
    grams: float | None = None
    if m := _PACKS.fullmatch(t):
        grams = int(m[1]) * int(m[2])
    elif m := _KILOS.fullmatch(t):
        grams = float(m[1]) * 1000
    elif m := _GRAMS.fullmatch(t):
        grams = int(m[1])
    elif m := _WORDS.fullmatch(t):
        grams = _words_to_int(m[1])
        if grams is None:
            return None, "unparseable"
    elif _BARE_NUMBER.fullmatch(t):
        return None, "unitless"
    else:
        return None, "unparseable"
    if grams != int(grams):
        return None, "non_integer_grams"
    if not 0 < grams <= GRAMS_MAX:
        return None, "out_of_range"
    return int(grams), "ok"
