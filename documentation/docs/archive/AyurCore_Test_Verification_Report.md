# AyurCore — Test Verification Report

**Date:** September 13, 2026
**Scope:** the five automated test files covering the PII air-gap, the preview/confirm split, the agentic tool-calling loop, and the three original golden tests — 87 individual checks in total, all passing as of the final run in your real environment.

## Summary

| Test file | Checks | Final result | Where it actually ran |
|---|---|---|---|
| `tests/test_pii_sanitizer.py` | 32 | 32 passed, 0 failed | Your real venv |
| `tests/test_pii_airgap.py` | 26 | 26 passed, 0 failed | Built/debugged in a sandbox with stand-in libraries, then re-verified in your real venv |
| `tests/test_preview_lifecycle.py` | 19 | 19 passed, 0 failed | Built/debugged in a sandbox with stand-in libraries, then re-verified in your real venv |
| `tests/test_copilot_tool_calling.py` | 7 | 7 passed, 0 failed | Built/debugged in a sandbox with stand-in libraries, then re-verified in your real venv |
| `tests/test_golden.py` | 3 | 3 passed, 0 failed | Your real venv only (needs a live server) |
| **Total** | **87** | **87 passed, 0 failed** | |

None of this was a single clean run start to finish. Three real bugs surfaced along the way — one in the actual application code, two in the tests themselves — and one operational hiccup (a stale server process) briefly made the last file look broken when it wasn't. All of that is documented below rather than smoothed over, because "what actually happened" is more useful to you than a flat scoreboard.

## Methodology: two environments, and why

This session's cloud workspace doesn't have `fastapi`, `google-genai`, or network access to install them (pip installs to PyPI are blocked here by an organization policy — confirmed earlier in this project, not something worth retrying). So every test touching those libraries was first built and debugged against small hand-written stand-ins: a fake `fastapi` package that reimplements just enough of `FastAPI`/`HTTPException`/`Header`/`TestClient` to run the real endpoint functions in `backend/main.py` directly, and a fake `google.genai` package that lets `agents/copilot.py` import cleanly while a test substitutes its own canned Gemini responses.

That's enough to prove the application's *own* logic is correct — the state machine in `main.py`, the SQL in `inventory_service.py`, the JSON-parsing logic in `copilot.py`. It proves nothing about whether the real `fastapi`/`pydantic`/`google-genai` libraries behave the way the stand-ins assumed. That's exactly why every file was re-run in your actual venv before being called verified — and it's also why one thing found in the sandbox (see Bug 2 below) turned out to matter: a stand-in can only be as accurate as its author's understanding of the real library's shape, and it's easy for that understanding to drift out of sync with the real code as the real code changes.

## File-by-file

### 1. `tests/test_pii_sanitizer.py` — 32 checks

Tests the core PII tokenization module (`backend/pii_sanitizer.py`) in isolation — no server, no database, no AI. This is the module responsible for making sure a patient's real name, phone, or ID never appears in anything sent to Gemini.

Grouped by scenario:
- **Basic tokenization** (5 checks): the real name is removed from sanitized text, exactly one token is minted, the token matches the `[[PAT_xxxxxxxx]]` shape, the token contains no fragment of the real name, and a name mentioned only in a different form is correctly left untouched when it wasn't supplied as a value to redact.
- **Name-variant collapsing** (6 checks): confirms `expand_name_variants` makes a bare first-name mention (e.g. "John requires...") collapse to the *same* token as the full name ("John Mathew") elsewhere in the same notes, rather than minting two disconnected tokens for the same person.
- **Case-insensitive matching** (3 checks): every casing of a name is caught, and all mentions of one value share one token.
- **Longest-value-first ordering** (2 checks): a shorter value (like a surname) doesn't get partially matched inside a longer one before the longer one is processed.
- **Empty/None handling** (2 checks): blank or missing PII values are silently ignored rather than corrupting the text.
- **Multi-field tokenization** (4 checks): name, phone, and patient ID are all tokenized together, three distinct tokens minted.
- **`assert_no_pii_leak` — the pre-flight safety check** (3 checks across three scenarios): passes silently on clean text, raises on a real leak, and raises even when only *one* of several PII values leaked (a partial leak still counts).
- **`rehydrate_payload`** (3 checks): restores the real value from a token, leaves unrelated fields untouched, and works correctly through nested lists/dicts even if the AI changed the token's letter casing.
- **`find_unresolved_tokens`** (1 check): correctly flags a token the mapping doesn't recognize — the defense-in-depth anomaly detector.
- **End-to-end simulation** (3 checks): what would actually leave the server contains no real name or phone, and the final persisted database record has the real name.

**What happened:** clean 32/32 on your first run, and identical on your immediate second run. No issues.

### 2. `tests/test_pii_airgap.py` — 26 checks

An in-process test using FastAPI's `TestClient` (no separate server process) that proves the full chain end to end: real identity in → local tokenization → a faked Gemini call → local rehydration → real SQLite row out. It also proves the corrections ("data flywheel") logging behaves correctly.

- **Network boundary + rehydration + DB reverse-verification** (11 checks): `/preview` succeeds, exactly one AI call is made, the real name and phone are absent from what would have gone out over the network while a `[[PAT_...]]` token is present, the preview response has the real name, `/confirm` succeeds while making *zero* further AI calls, the confirm response and the database row both have the real name — and, explicitly, that the same real name proven absent from the network payload is the one that ends up in the database.
- **Pre-flight abort** (2 checks): when the PII safety check is forced to fail, `/preview` is rejected with a 500 and the AI boundary is never touched at all.
- **Corrections logging** (13 checks, two cases): Case A — doctor accepts the AI's proposal unchanged: preview and confirm both succeed, a corrections row is written, `was_changed` is correctly `0`, and the AI's and confirmed values match. Case B — doctor corrects both the dosha and the herbs: confirm reflects the doctor's correction, a corrections row is written, `was_changed` is correctly `1`, and — critically — the row still records the *original* AI proposal, not something reconstructed from the doctor's edit.

**What happened:** this one broke on the first sandbox run, and not because of anything wrong with the underlying logic. Its fake Gemini client (`_FakeGenAIClient`) was written back when `agents/copilot.py` called `client.models.generate_content(...)` directly. Between that test being written and this pass, `copilot.py` was rewritten to add the agentic tool-calling loop, which changed the call shape entirely, to `client.chats.create(...)` followed by `.send_message(...)`. The test's fake client still mocked the old shape, so every call into it threw `AttributeError: '_FakeGenAIClient' object has no attribute 'chats'`, and both AI-dependent tests failed with a 502 as a result — 4 of 26 checks failed on that first attempt. This is a real example of exactly the kind of drift the two-environment approach above is meant to catch: the test's stand-in silently went stale when the real code changed shape underneath it. Fixed by rewriting `_FakeModels`/`_FakeGenAIClient` into `_FakeChat`/`_FakeChats`/`_FakeGenAIClient` matching the new call shape; re-ran clean at 26/26 in the sandbox, then 26/26 again in your real venv.

### 3. `tests/test_preview_lifecycle.py` — 19 checks

Written specifically in response to the external architecture review, to prove the six issues it raised are actually handled — most importantly the real bug it caught (a preview being consumed before knowing whether confirm would succeed).

- **Single-use enforcement** (3 checks): a preview can be confirmed once; confirming; replaying the same `preview_id` afterward correctly returns 409, not a second prescription.
- **Failed confirm leaves the preview retriable** (4 checks): an insufficient-stock 409 does *not* consume the preview; the doctor can then retry the *same* `preview_id` with a corrected amount and succeed; only *then* does a further replay finally get 409. This is the direct test of the bug fix itself.
- **Session binding** (3 checks): a preview created under one `Authorization` header cannot be confirmed under a different one (403), but confirming with the *same* header succeeds.
- **Unknown preview_id** (1 check): a `preview_id` that was never issued returns 404, distinct from the 409 used for an already-confirmed one.
- **Doctor corrections use the same canonical herb resolver as the AI** (4 checks): the AI's "Turmeric" resolves to the canonical "Haridra" in the preview; a doctor's different alias for the same herb resolves identically at confirm time; and a genuinely unknown herb in a doctor's correction still 400s, exactly as it would from the AI.
- **Doctor corrections get the same numeric validation as AI output** (4 checks): `grams=0`, negative grams, and `grams>10000` are all rejected with 422 in a doctor's correction, and a subsequently valid correction on the same preview still succeeds.

**What happened:** one check failed on the first sandbox run — `doctor's alternate alias ('turmeric powder') also resolves to Haridra`. That was a bug in the *test*, not the code: "turmeric powder" was never actually one of the aliases in `HERB_ALIASES` (only "turmeric" is), so the herb resolver correctly rejected it. Fixed by changing the test to use a real alias ("Curcuma longa"); re-ran clean at 19/19 in the sandbox, then 19/19 in your real venv.

### 4. `tests/test_copilot_tool_calling.py` — 7 checks

Scoped narrowly to what can be verified about the agentic tool-calling loop without a live Gemini key: that `check_herb_alias`/`check_stock` behave correctly against the real database, that both get registered as tools on the Gemini call, and that the final JSON response — fenced in markdown or not — parses correctly.

- **Tool functions against the real DB** (3 checks): a known alias resolves correctly, an unknown herb is honestly reported as unknown, and stock is read as the real, current value.
- **Tools wired in, response parsed** (3 checks): `parse_clinical_notes` returns a valid result, exactly one chat session is created, and both tools are present on that call.
- **Markdown-fenced response** (1 check): a response wrapped in a ` ```json ` fence — which the prompt explicitly asks the model not to do, but which real models sometimes do anyway — still parses correctly.

**What happened:** clean 7/7 on the first run, in both the sandbox and your real venv.

**Important limitation, repeated here on purpose:** this file cannot and does not prove that Gemini's real automatic function-calling machinery actually invokes `check_herb_alias`/`check_stock` mid-conversation the way the prompt asks it to — that's real, network-dependent behavior of the live API. The first real run against your `GEMINI_API_KEY` with a multi-herb note should specifically log or print whether those calls happen.

### 5. `tests/test_golden.py` — 3 checks

The three original golden tests, unchanged in intent from before the preview/confirm split, updated only to call `/prescribe/preview` then `/prescribe/confirm` instead of the old single `/prescribe` endpoint. This one needs an actual running server — it can't run in-process — so it was never runnable in the sandbox at all; it only ever ran against your real venv and a live `uvicorn` process.

- **The Hallucination Test**: a note asking for an herb the clinic doesn't stock ("Tulsi") — `/preview` correctly rejects it with 400 and inventory is untouched.
- **The Transaction Rollback Test**: a note asking for more Brahmi than is in stock — `/confirm` correctly rejects it with 409 and Ashwagandha (the other herb in the same note) is *not* partially deducted.
- **The Race Condition Test**: two concurrent `/confirm` calls against exactly enough stock for one of them — exactly one returns 200, the other 409, and the final stock is exactly what one successful deduction should leave.

**What happened, in two parts.** The first attempt to run this file returned `404 Not Found` on all three tests, which looked alarming — as if the preview/confirm endpoints didn't exist. The actual cause: your `uvicorn backend.main:app --reload` had failed to start (`[Errno 48] Address already in use`), silently leaving an *old* server process — almost certainly one still running the pre-split code with only the original `/prescribe` route — listening on port 8000 instead. The new code was never actually being tested; the old server was. Fixed by killing whatever was bound to port 8000 (`lsof -ti:8000 | xargs kill -9`) and starting a genuinely fresh server. The second attempt passed clean, 3/3, against the real code for the first time.

## Bugs found and fixed during this testing pass

1. **Real application bug — eager preview consumption** (`backend/main.py`): `/confirm` used to remove a preview from the pending store the moment it was looked up, before knowing whether the transaction below would succeed. A doctor hitting a 409 lost the preview entirely and had to re-run `/preview` (and pay for a fresh Gemini call) just to retry with a lower quantity. Caught by the external architecture review, not by testing — but `test_preview_lifecycle.py`'s "failed confirm leaves the preview retriable" test now directly guards against a regression.
2. **Stale test mock** (`test_pii_airgap.py`): the fake Gemini client still mocked the pre-tool-calling `generate_content` call shape after `copilot.py` moved to the chat-based `chats.create().send_message()` shape for the tool-calling loop. Caused 4 of 26 checks to fail with an `AttributeError` before being fixed.
3. **Test bug, not a code bug** (`test_preview_lifecycle.py`): a test asserted that "turmeric powder" should resolve as a herb alias; it was never actually in `HERB_ALIASES`. The code was correctly rejecting it — the test's expectation was wrong, not the resolver.
4. **Operational issue, not a code bug**: a stale `uvicorn` process left over from an earlier session attempt was silently serving the old, pre-split API on port 8000, making `test_golden.py` look like a 404 failure when the real issue was that the new code was never being exercised at all.

None of these were "hidden until production" bugs in the sense of being subtle logic errors that slipped through — 1 was caught by external review before it ever shipped to a real user, and 2–4 were caught by the tests and the process of running them, which is exactly what this stage of testing is for.

## What 87/87 does not prove

This is worth stating plainly rather than letting a clean scoreboard imply more than it should. All 87 checks are real and all currently pass, but they specifically do **not** verify:

- That Gemini's real automatic function-calling actually invokes `check_herb_alias`/`check_stock` the way the tool-calling code assumes — only that the code *would* handle it correctly if it does.
- That Sarvam's speech-to-text API behaves the way its documentation describes, or that the transcription is actually accurate for real Malayalam/Manglish speech — no test here touches Sarvam at all.
- Anything about the Streamlit UI — none of these 87 checks exercise `ui/app.py`; it was never run in either environment.
- Behavior with any note written in Malayalam or Manglish — every single note used across all 87 checks is English-only.

These aren't gaps in the testing that was done; they're outside what this particular set of tests was ever designed to cover. They're the same four items flagged as the real remaining risk before a demo.
