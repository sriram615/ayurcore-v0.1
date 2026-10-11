"""
AI-2 v2 input normalisation (memory/normalize.py). Invisible characters are built with chr() so the
test source itself stays visible.
"""
import pathlib
import sys
import unicodedata
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory.normalize import MAX_CHARS, MAX_TOKENS, normalize

ZWSP, ZWJ, ZWNJ, BOM, RLO, ESC = chr(0x200B), chr(0x200D), chr(0x200C), chr(0xFEFF), chr(0x202E), chr(0x1B)
MALAYALAM = ["അവസാനത്തെ സന്ദർശനം എപ്പോഴായിരുന്നു?", "ഗുൽഗുലു എത്ര കുറിപ്പടികളിൽ ഉണ്ടായിരുന്നു?",
             "ഇരട്ടിമധുരം ആകെ എത്ര ഗ്രാം?", "കയ്യോന്നി കൊടുത്തിട്ടുണ്ട്", "ൺ ൻ ർ ൽ ൾ ൿ"]


class NormalizeTests(unittest.TestCase):
    def text(self, q):
        out = normalize(q)
        self.assertTrue(out["ok"], out)
        return out["text"]

    def test_plain_text_is_unchanged(self):
        for q in ["How many prescriptions does she have?", "brahmi ethra thavana koduthu?", *MALAYALAM]:
            with self.subTest(q):
                self.assertEqual(normalize(q), {"ok": True, "text": q, "reason": None, "changes": []})

    def test_malayalam_is_preserved_by_nfkc(self):
        for q in MALAYALAM:
            self.assertEqual(unicodedata.normalize("NFKC", q), q)

    def test_compatibility_forms(self):
        full = "".join(chr(ord(c) + 0xFEE0) if c != " " else chr(0x3000) for c in "HOW MANY VISITS")
        self.assertEqual(self.text(full + chr(0xFF1F)), "HOW MANY VISITS?")
        self.assertEqual(self.text("ﬁrst prescription"), "first prescription")

    def test_invisible_characters_are_removed_and_words_rejoined(self):
        self.assertEqual(self.text("Total grams of Bra" + ZWSP + "hmi?"), "Total grams of Brahmi?")
        self.assertEqual(self.text(BOM + "List all visits" + RLO), "List all visits")
        self.assertIn("invisible_removed", normalize("a" + ZWSP + "b")["changes"])

    def test_joiners_kept_inside_malayalam_only(self):
        inside = "ക്" + ZWNJ + "ക"
        self.assertEqual(self.text(inside), inside)
        self.assertEqual(self.text("Neem" + ZWJ + "total"), "Neemtotal")

    def test_legacy_chillu_becomes_atomic(self):
        legacy = "സന്ദ" + "ര്" + ZWJ + "ശനം"
        out = normalize(legacy)
        self.assertEqual(out["text"], "സന്ദർശനം")
        self.assertIn("legacy_chillu", out["changes"])

    def test_escape_and_control_characters(self):
        self.assertEqual(self.text(ESC + "[2J" + chr(0) + " clear screen"), "clear screen")
        self.assertEqual(self.text("When was the\tlatest\nvisit?"), "When was the latest visit?")

    def test_rejections(self):
        for q, reason in [(None, "NOT_TEXT"), (42, "NOT_TEXT"), (["x"], "NOT_TEXT"), ("", "EMPTY"), ("  \n\t ", "EMPTY"),
                          (ZWSP * 3, "EMPTY"), (ESC + "[31m" + ESC + "[0m", "EMPTY"),
                          ("a" * (MAX_CHARS + 1), "TOO_LONG"), ("visit " * (MAX_TOKENS + 1), "TOO_LONG")]:
            with self.subTest(repr(q)[:30]):
                out = normalize(q)
                self.assertEqual((out["ok"], out["text"], out["reason"]), (False, None, reason))

    def test_long_input_is_never_truncated(self):
        self.assertTrue(normalize("a" * MAX_CHARS)["ok"])
        self.assertEqual(normalize("x" * (MAX_CHARS + 50))["text"], None)

    def test_idempotent_and_keeps_case_and_punctuation(self):
        for q in ["  What  was the LAST dosha?! ", "Bra" + ZWSP + "hmi, total?", *MALAYALAM]:
            once = self.text(q)
            self.assertEqual(self.text(once), once)
        self.assertEqual(self.text("What was the LAST dosha?!"), "What was the LAST dosha?!")


if __name__ == "__main__":
    unittest.main()
