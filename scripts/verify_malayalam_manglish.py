"""
Step 4 verification: does /prescribe/preview correctly parse Malayalam,
Manglish, and mixed-language clinical notes? This is the real-world gate --
none of the existing 87 checks in the test suite use anything but English.

THIS SCRIPT MAKES REAL, BILLED CALLS TO THE GEMINI API. Requires
GEMINI_API_KEY to be set (a .env file works too).

Uses FastAPI's TestClient (in-process, real routing/validation, no
separate server/port to manage) against the real backend.main.app, with
the real Gemini client untouched -- these are genuine API responses.

Run with:
    venv/bin/python scripts/verify_malayalam_manglish.py

IMPORTANT: read the printed table literally, not optimistically. One PASS
on one sentence per language is one data point, not proof "Malayalam
works." Before drawing any real conclusion, run several notes per
language -- ideally actual sentences a doctor produced, in their own
phrasing, not ones written for a test script.
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

CASES = [
    {
        "label": "English",
        "notes": "Patient has Vata imbalance. Prescribe 20 grams of Ashwagandha.",
        "expected_dosha_contains": "Vata",
        "expected_herbs": {"Ashwagandha": 20},
    },
    {
        "label": "Malayalam",
        "notes": "രോഗിക്ക് വാത ദോഷം കൂടുതലാണ്. അശ്വഗന്ധ 20 ഗ്രാം നൽകുക.",
        "expected_dosha_contains": "Vata",
        "expected_herbs": {"Ashwagandha": 20},
    },
    {
        "label": "Manglish",
        "notes": "Patientinu Vata dosham kooduthal aanu. Ashwagandha 20 gram kodukkuka.",
        "expected_dosha_contains": "Vata",
        "expected_herbs": {"Ashwagandha": 20},
    },
    {
        "label": "Mixed (2 herbs)",
        "notes": "Patientinu Vata dosham kooduthal aanu. Ashwagandha 20 gramum Brahmi 15 gramum prescribe cheyyuka.",
        "expected_dosha_contains": "Vata",
        "expected_herbs": {"Ashwagandha": 20, "Brahmi": 15},
    },
]

results = []

# Free-tier Gemini quota is 5 generate_content calls PER MINUTE per model --
# and, discovered the hard way running this script, ONE call to
# parse_clinical_notes is not one generate_content call. Automatic
# function-calling means every tool call the model makes (check_herb_alias,
# check_stock) is its own round trip to Gemini, so a single test case that
# calls tools 5 times costs ~6 requests (5 tool round trips + the final
# answer) -- enough to exhaust the entire per-minute budget by itself. This
# sleep is a blunt fix so the 4 cases below don't cannibalize each other's
# quota; it does not fix the deeper issue (see the comment added to the
# prompt in agents/copilot.py about calling each tool at most once per herb).
SECONDS_BETWEEN_CASES = 65

for i, case in enumerate(CASES):
    if i > 0:
        print(f"(waiting {SECONDS_BETWEEN_CASES}s for the free-tier quota window to reset...)")
        time.sleep(SECONDS_BETWEEN_CASES)

    payload = {
        "patient_name": "Test Patient",
        "notes": case["notes"],
        "clinic_id": "default_clinic",
    }
    resp = client.post("/prescribe/preview", json=payload)

    if resp.status_code != 200:
        results.append((case["label"], "FAIL", f"{resp.status_code}: {resp.text}"))
        continue

    body = resp.json()
    dosha_ok = case["expected_dosha_contains"].lower() in body["dosha_imbalance"].lower()
    got_herbs = {h["name"]: h["grams"] for h in body["prescribed_herbs"]}
    herbs_ok = got_herbs == case["expected_herbs"]

    if dosha_ok and herbs_ok:
        results.append((case["label"], "PASS", f"dosha={body['dosha_imbalance']!r}, herbs={got_herbs}"))
    else:
        results.append((
            case["label"], "FAIL",
            f"dosha={body['dosha_imbalance']!r} (expected to contain {case['expected_dosha_contains']!r}), "
            f"herbs={got_herbs} (expected {case['expected_herbs']})",
        ))

print(f"{'Input':<20} {'Result':<6} Detail")
print("-" * 100)
for label, result, detail in results:
    print(f"{label:<20} {result:<6} {detail}")

print()
n_pass = sum(1 for _, r, _ in results if r == "PASS")
print(f"{n_pass}/{len(results)} passed.")
print()
print(
    "A FAIL here can mean either 'Gemini didn't understand the note' or "
    "'the note itself was ambiguous/unnatural' -- read the actual dosha/herbs "
    "output above before concluding which. And again: this is 4 sentences, "
    "not a language-support claim."
)
