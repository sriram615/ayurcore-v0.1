"""
Live verification of the three things the existing 87-check test suite
explicitly could NOT prove (see documentation/docs/archive/AyurCore_Test_Verification_Report.md,
"What 87/87 does not prove"):

  Step 1: does Gemini's real automatic function-calling actually invoke
          check_herb_alias/check_stock mid-conversation, not just answer
          from the prompt text? -- watch for [GEMINI TOOL] lines below,
          printed by agents/copilot.py itself.
  Step 2: is the real outgoing prompt to Gemini actually free of the
          patient's real name/phone/ID, with only a [[PAT_...]] token
          present -- verified against the REAL Gemini call, not a faked
          one this time.
  Step 3: does /prescribe/confirm make ZERO further Gemini calls, exactly
          as the API contract promises?

THIS SCRIPT MAKES REAL, BILLED CALLS TO THE GEMINI API. Requires
GEMINI_API_KEY to be set (a .env file works too, since backend/main.py
already calls load_dotenv()).

Design note: this uses FastAPI's TestClient (in-process) rather than a
second `uvicorn` process + curl. That's a deliberate choice, not a
shortcut -- it exercises the exact same real Starlette routing and real
Pydantic validation as a live server, without a second terminal/port to
manage, which is what caused the false-alarm 404s during the
test_golden.py run (a stale process silently squatting on port 8000).
It does NOT fake or replace the Gemini client: a thin "spy" wrapper sits
in front of the REAL genai.Client, forwarding every call through
unchanged while recording what was sent. The response you see is
genuinely Gemini's.

Run with:
    venv/bin/python scripts/verify_live_integration.py
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

if not os.environ.get("GEMINI_API_KEY"):
    print("GEMINI_API_KEY is not set. This script makes real Gemini calls and needs it. Aborting.")
    sys.exit(1)

import agents.copilot as copilot_module

captured_prompts: list = []
call_count = {"n": 0}

_real_client_cls = copilot_module.genai.Client


class _SpyChat:
    def __init__(self, real_chat):
        self._real_chat = real_chat

    def send_message(self, prompt):
        captured_prompts.append(prompt)
        call_count["n"] += 1
        return self._real_chat.send_message(prompt)


class _SpyChats:
    def __init__(self, real_chats):
        self._real_chats = real_chats

    def create(self, model, config):
        real_chat = self._real_chats.create(model=model, config=config)
        return _SpyChat(real_chat)


class _SpyClient:
    """Wraps the REAL genai.Client. Every call underneath is genuinely
    made to Gemini -- this only observes, never fakes, a response."""

    def __init__(self, api_key):
        self._real_client = _real_client_cls(api_key=api_key)
        self.chats = _SpyChats(self._real_client.chats)


copilot_module.genai.Client = _SpyClient

from fastapi.testclient import TestClient
import backend.main as main_module

client = TestClient(main_module.app)

REAL_NAME = "Sriram Testpatient"
REAL_PHONE = "9999999999"
REAL_ID = "TEST-001"

payload = {
    "patient_name": REAL_NAME,
    "patient_phone": REAL_PHONE,
    "patient_id": REAL_ID,
    "notes": "Patient has Vata imbalance. Prescribe 20 grams of Ashwagandha and 15 grams of Brahmi.",
    "clinic_id": "default_clinic",
}

print("=" * 70)
print("STEPS 1 & 2: calling /prescribe/preview with a REAL Gemini call")
print("Watch for [GEMINI TOOL] lines below -- that's Step 1's proof.")
print("=" * 70)
print()

preview_resp = client.post("/prescribe/preview", json=payload)

print()
print("API response:")
print(preview_resp.status_code, preview_resp.json() if preview_resp.status_code == 200 else preview_resp.text)

print()
print("=" * 70)
print("STEP 2 result: checking the REAL outgoing prompt for PII")
print("=" * 70)

if not captured_prompts:
    print("FAIL: no prompt was captured -- the AI call never happened (check the API response above for why).")
else:
    outgoing = captured_prompts[0]
    leaks = []
    if REAL_NAME in outgoing:
        leaks.append("patient_name")
    if REAL_PHONE in outgoing:
        leaks.append("patient_phone")
    if REAL_ID in outgoing:
        leaks.append("patient_id")
    has_token = "[[PAT_" in outgoing

    if leaks:
        print(f"FAIL: real PII found in the outgoing Gemini prompt: {leaks}")
    else:
        print("PASS: none of patient_name/patient_phone/patient_id appear in the real outgoing prompt.")
    print(f"{'PASS' if has_token else 'FAIL'}: a [[PAT_...]] token {'is' if has_token else 'is NOT'} present in the outgoing prompt.")

print()
print(f"Gemini calls made so far (should be exactly 1): {call_count['n']}")

if preview_resp.status_code != 200:
    print("\nCan't continue to Step 3 -- /preview did not succeed.")
    sys.exit(1)

preview_id = preview_resp.json()["preview_id"]

print()
print("=" * 70)
print("STEP 3: calling /prescribe/confirm -- must make ZERO further Gemini calls")
print("=" * 70)

calls_before_confirm = call_count["n"]
confirm_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id})
calls_after_confirm = call_count["n"]

print(f"Gemini calls before confirm: {calls_before_confirm}")
print(f"Gemini calls after confirm:  {calls_after_confirm}")
print(f"{'PASS' if calls_after_confirm == calls_before_confirm else 'FAIL'}: /confirm made no additional Gemini call.")
print()
print("Confirm response:", confirm_resp.status_code, confirm_resp.json() if confirm_resp.status_code == 200 else confirm_resp.text)
