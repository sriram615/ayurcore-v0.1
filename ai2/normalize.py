"""
Input normalisation for AI-2 v2 questions (deterministic, no model).

    normalize(question) -> {"ok": bool, "text": str | None, "reason": None | REASON, "changes": [...]}

Steps, in order:
  1. type: only str is accepted (reason NOT_TEXT otherwise)
  2. ANSI / terminal escape sequences (ESC [ ... final byte) are removed whole, not left as "[2J"
  3. Unicode NFKC: compatibility forms become their plain equivalents (full-width letters and digits,
     ligatures); Malayalam text is unchanged by NFKC (checked in tests)
  4. invisible characters: zero-width space / word joiner / BOM / soft hyphen / bidi controls are
     removed (a word split by them is rejoined: "Bra<ZWSP>hmi" -> "Brahmi"). ZWJ and ZWNJ are KEPT
     between two Malayalam characters, where they select conjunct / chillu forms, and removed elsewhere
  4b. legacy Malayalam chillu sequences (consonant + virama + ZWJ, the pre-Unicode-5.1 encoding) become
     the atomic chillu letters the lexicons use; NFKC does not do this
  5. other control characters (Unicode category Cc) become a space; whitespace runs collapse to one space
  6. bounds: an empty result is rejected (EMPTY); more than MAX_CHARS characters or MAX_TOKENS
     whitespace tokens is rejected (TOO_LONG) -- long input is never truncated, because cutting it could
     change what is asked
Case is not changed here (the planner lower-cases); punctuation is kept.
"""
from __future__ import annotations

import re
import unicodedata

MAX_CHARS = 300
MAX_TOKENS = 60
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
INVISIBLE = {"\u200b", "\u2060", "\ufeff", "\u00ad", "\u180e", "\u200e", "\u200f", "\u061c",
             *map(chr, range(0x202a, 0x202f)), *map(chr, range(0x2066, 0x206a))}
JOINERS = {"\u200c", "\u200d"}  # ZWNJ, ZWJ
CHILLU = {"\u0d23\u0d4d\u200d": "\u0d7a", "\u0d28\u0d4d\u200d": "\u0d7b", "\u0d30\u0d4d\u200d": "\u0d7c",
          "\u0d32\u0d4d\u200d": "\u0d7d", "\u0d33\u0d4d\u200d": "\u0d7e", "\u0d15\u0d4d\u200d": "\u0d7f"}


def _malayalam(ch: str) -> bool:
    return "\u0d00" <= ch <= "\u0d7f"


def normalize(question) -> dict:
    if not isinstance(question, str):
        return {"ok": False, "text": None, "reason": "NOT_TEXT", "changes": ["not_text"]}
    changes = []
    text = ANSI.sub(" ", question)
    if text != question:
        changes.append("ansi_escape_removed")
    nfkc = unicodedata.normalize("NFKC", text)
    if nfkc != text:
        changes.append("nfkc")
    for legacy, atomic in CHILLU.items():
        if legacy in nfkc:
            nfkc = nfkc.replace(legacy, atomic)
            changes.append("legacy_chillu")
    out = []
    for i, ch in enumerate(nfkc):
        if ch in INVISIBLE:
            changes.append("invisible_removed")
            continue
        if ch in JOINERS:
            prev = nfkc[i - 1] if i else ""
            nxt = nfkc[i + 1] if i + 1 < len(nfkc) else ""
            if _malayalam(prev) and _malayalam(nxt):
                out.append(ch)
            else:
                changes.append("joiner_removed")
            continue
        if unicodedata.category(ch) == "Cc" and ch not in " ":
            if not ch.isspace():
                changes.append("control_removed")
            out.append(" ")
            continue
        out.append(ch)
    text = " ".join("".join(out).split())
    changes = sorted(set(changes))
    if not text:
        return {"ok": False, "text": None, "reason": "EMPTY", "changes": changes}
    if len(text) > MAX_CHARS or len(text.split()) > MAX_TOKENS:
        return {"ok": False, "text": None, "reason": "TOO_LONG", "changes": changes}
    return {"ok": True, "text": text, "reason": None, "changes": changes}
