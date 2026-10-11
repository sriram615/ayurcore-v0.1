"""
Stress-test verification: messier, more realistic Malayalam/Manglish
clinical notes than scripts/verify_malayalam_manglish.py used.

WHY THIS EXISTS: the clean, one-sentence-per-language notes in the other
script are not representative of what a doctor actually scribbles during a
live consultation -- no punctuation, abbreviations, mixed scripts mid
sentence, medical shorthand (Rx, OD, TDS, c/o, f/u), distractor patient
history, and herbs that aren't in this clinic's inventory at all. These
notes are SYNTHETIC -- written by an AI, not a real doctor -- as an
explicit stopgap while real notes are collected from the clinic's actual
doctor. They are a stress test of robustness, not a substitute for that.
Do not treat a clean pass here as proof the system handles this doctor's
real handwriting/shorthand; it only proves it handles THESE particular
kinds of messiness.

THIS SCRIPT MAKES REAL, BILLED CALLS TO THE GEMINI API. Requires
GEMINI_API_KEY to be set (a .env file works too).

Some cases below have a fixed expected answer and are auto-graded
(PASS/FAIL). Others are marked "JUDGE-YOURSELF" -- these are genuinely
ambiguous (an imprecise quantity, no explicit unit) and there isn't one
single correct answer to grade against; the script just prints what Gemini
returned so you can read it and decide if it's reasonable, per the same
"read the actual output, don't just trust a PASS/FAIL label" rule as the
other verification scripts in this repo.

Run with:
    venv/bin/python scripts/verify_malayalam_manglish_stress.py
"""
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

if not os.environ.get("GEMINI_API_KEY"):
    print("GEMINI_API_KEY is not set. This script makes real Gemini calls and needs it. Aborting.")
    sys.exit(1)

from fastapi.testclient import TestClient
import backend.main as main_module

client = TestClient(main_module.app)

# Same lesson as verify_malayalam_manglish.py: one logical parse can cost
# several Gemini requests once tool-calling is involved, and the free tier
# is 5 requests/minute. Spacing cases out avoids one case's quota usage
# starving the next.
SECONDS_BETWEEN_CASES = 65

CASES = [
    {
        "label": "Clinical shorthand (English)",
        "notes": "c/o fatigue, poor sleep x 2wks. vata++. Rx: Ashwagandha 20g OD. f/u 2wks.",
        "why": "Real doctor-shorthand abbreviations (c/o, Rx, OD, f/u) and a dose-frequency "
               "marker (OD = once daily) that must NOT be confused with the gram quantity.",
        "expected_dosha_contains": "Vata",
        "expected_herbs": {"Ashwagandha": 20},
    },
    {
        "label": "Manglish, no punctuation, 2 herbs",
        "notes": "kore adhika vishamam ravile ninnu vayya vata kooduthal ashwagandha 20gm brahmi 15gm "
                 "randum kodukkanam rathri kazhikkanam",
        "why": "Run-on sentence, no punctuation, distractor symptom description before the "
               "prescription, unusual gram abbreviation ('gm').",
        "expected_dosha_contains": "Vata",
        "expected_herbs": {"Ashwagandha": 20, "Brahmi": 15},
    },
    {
        "label": "Malayalam script, terse, 2 herbs",
        "notes": "വാതദോഷം. അശ്വഗന്ധ 15ഗ്രാം, ശതാവരി 10ഗ്രാം. ഒരുമിച്ച് നൽകുക.",
        "why": "Tests the newly added അശ്വഗന്ധ alias plus an existing one (ശതാവരി), "
               "number+unit glued together with no space (15ഗ്രാം).",
        "expected_dosha_contains": "Vata",
        "expected_herbs": {"Ashwagandha": 15, "Shatavari": 10},
    },
    {
        "label": "Mixed script mid-sentence, 3 herbs, Pitta-Kapha",
        "notes": "Pitta-Kapha aanu condition. Triphala 10g cheyyണം, koode Guggulu 12ഗ്രാം, Haridra 8g. "
                 "TDS kazhikkanam.",
        "why": "Malayalam and English words interleaved within the same clause (not just "
               "alternating sentences), plus a dosing-frequency abbreviation (TDS).",
        "expected_dosha_contains": "Pitta",
        "expected_herbs": {"Triphala": 10, "Guggulu": 12, "Haridra": 8},
    },
    {
        "label": "Non-stocked herb mentioned alongside a real one",
        "notes": "vata kൂടുതൽ. thulasi ela kurachu kodukkanam, koode ashwagandha 20 gram.",
        "why": "Thulasi (Tulsi) is deliberately NOT one of this clinic's stocked herbs (the "
               "formulary grew from 8 to 20 herbs in Sep 2026, and Tulsi was a candidate that "
               "was left out on purpose, specifically so this test would stay meaningful) and "
               "has no alias entry at all. Per the tightened prompt, Gemini should leave it out "
               "entirely rather than guess a canonical name for it -- expected output has ONLY "
               "Ashwagandha. If Tulsi shows up in the output under any name, that's the "
               "'guessing past an unknown herb' failure mode this test exists to catch.",
        "expected_dosha_contains": "Vata",
        "expected_herbs": {"Ashwagandha": 20},
    },
    {
        "label": "Telegraphic Malayalam, no unit words at all",
        "notes": "വാതം. അശ്വഗന്ധ 20. ബ്രഹ്മി 10.",
        "why": "Bare numbers with no explicit 'gram'/ഗ്രാം unit -- tests whether Gemini still "
               "infers grams (the only unit this system ever uses) rather than refusing or "
               "guessing a different unit.",
        "expected_dosha_contains": "Vata",
        "expected_herbs": {"Ashwagandha": 20, "Brahmi": 10},
    },
    {
        "label": "Long note, distractor history, 4 herbs, Vata-Kapha",
        "notes": "Patient 45yrs, diabetic. Ration vishamam undu kore divasamayi. Vata-Kapha dosham. "
                 "Dashamoola 25g, Triphala 20g, Arjuna 15g, Guggulu 10g okke kodukkanam. Follow up "
                 "1 month kazhinjond varanam.",
        "why": "Longest/noisiest case -- irrelevant patient history (age, diabetes) mixed in "
               "with 4 herbs and a follow-up instruction that must NOT be mistaken for a 5th herb.",
        "expected_dosha_contains": "Vata",
        "expected_herbs": {"Dashamoola": 25, "Triphala": 20, "Arjuna": 15, "Guggulu": 10},
    },
    {
        "label": "Imprecise quantity (genuinely ambiguous)",
        "notes": "vata pitta mix. ashwagandha kurachu koodutham venam, 20-25 g range il. brahmi original dose 15.",
        "why": "'20-25 g range' has no single correct integer -- this system requires a strict "
               "integer gram count, so there is no objectively right answer here. Read the "
               "actual output and judge whether Gemini's choice (or refusal) is reasonable; "
               "this case is NOT auto-graded.",
        "expected_dosha_contains": None,
        "expected_herbs": None,
    },
]

results = []

for i, case in enumerate(CASES):
    if i > 0:
        print(f"(waiting {SECONDS_BETWEEN_CASES}s for the free-tier quota window to reset...)")
        time.sleep(SECONDS_BETWEEN_CASES)

    print()
    print("=" * 100)
    print(f"[{i + 1}/{len(CASES)}] {case['label']}")
    print(f"Notes:  {case['notes']}")
    print(f"Why:    {case['why']}")

    payload = {
        "patient_name": "Test Patient",
        "notes": case["notes"],
        "clinic_id": "default_clinic",
    }
    resp = client.post("/prescribe/preview", json=payload)

    judge_yourself = case["expected_dosha_contains"] is None

    if resp.status_code != 200:
        detail = f"{resp.status_code}: {resp.text}"
        print(f"Result: FAIL  {detail}")
        results.append((case["label"], "FAIL", detail))
        continue

    body = resp.json()
    got_herbs = {h["name"]: h["grams"] for h in body["prescribed_herbs"]}
    print(f"Actual: dosha={body['dosha_imbalance']!r}, herbs={got_herbs}")

    if judge_yourself:
        results.append((case["label"], "JUDGE-YOURSELF", f"dosha={body['dosha_imbalance']!r}, herbs={got_herbs}"))
        print("Result: JUDGE-YOURSELF (no fixed correct answer -- read the actual output above)")
        continue

    dosha_ok = case["expected_dosha_contains"].lower() in body["dosha_imbalance"].lower()
    herbs_ok = got_herbs == case["expected_herbs"]

    if dosha_ok and herbs_ok:
        results.append((case["label"], "PASS", f"dosha={body['dosha_imbalance']!r}, herbs={got_herbs}"))
        print("Result: PASS")
    else:
        detail = (
            f"dosha={body['dosha_imbalance']!r} (expected to contain {case['expected_dosha_contains']!r}), "
            f"herbs={got_herbs} (expected {case['expected_herbs']})"
        )
        results.append((case["label"], "FAIL", detail))
        print(f"Result: FAIL  {detail}")

print()
print("=" * 100)
print(f"{'Input':<45} {'Result':<16} Detail")
print("-" * 100)
for label, result, detail in results:
    print(f"{label:<45} {result:<16} {detail}")

print()
n_pass = sum(1 for _, r, _ in results if r == "PASS")
n_graded = sum(1 for _, r, _ in results if r in ("PASS", "FAIL"))
print(f"{n_pass}/{n_graded} auto-graded cases passed. "
      f"{sum(1 for _, r, _ in results if r == 'JUDGE-YOURSELF')} case(s) need your own judgment (see above).")
print()
print(
    "Reminder: every note above was written by an AI as a stress test, not sourced from this "
    "clinic's actual doctor. A clean pass here is evidence of robustness to messiness in "
    "general -- it is still not evidence that this matches how YOUR doctor actually writes "
    "notes. Get real samples from them before treating this as settled."
)
