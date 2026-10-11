# AyurCore v0.1 — evaluation evidence and limitations

Date: 2026-10-10. Branch `v0.1`. **All data is synthetic; nothing here is clinical validation.** Each row says what was run, where the evidence is, and what it does *not* show. A claim with no evidence listed is not made.

Evidence status codes: **TRACKED** = in the repository; **LOCAL** = git-ignored file on the machine that produced it (a fresh clone does not have it); **NOT RUN** = not done.

## 1. Claims and evidence

| # | Claim | Evidence | Status | Does not show |
|---|---|---|---|---|
| 1 | The backend and AI test suite passes on the committed tree | Scratch-sandbox procedure (`scripts/run_scratch_sandbox_tests.py`): **862 passed, 1 xfailed, 8 failed**, 199 s; the 8 failures are `ModuleNotFoundError: numpy` (AI-1 extractor / OOD tests) | Reproducible by anyone | Anything about the 8 numpy-dependent tests (not run with numpy in this build); the excluded tests (5 legacy-UI, 2 files that read the reserved `s1_test.json`) |
| 2 | Earlier backend baseline | 473 passed (2026-09-27, `docs/HANDOFF.md`) | Historical | Superseded by row 1 |
| 3 | AI-2 answers are read from one consistent snapshot | `tests/test_ai2_snapshot_consistency.py` (negative control: 9 tests fail on the original adapter, pass on the fixed one) | TRACKED | Behaviour under real concurrent clinic load |
| 4 | Frontend renders every AI-2 reply state correctly, and the Inventory page behaves as specified | **530/530 mocked-API Playwright tests: 0 failed, 0 skipped, 0 flaky; 16 spec files (the 15 earlier ones plus `inventory.spec.ts`, 10 tests); one worker; about 9 minutes; Chrome; scratch export of commit `dd5c34b`.** Production build succeeded and strict `tsc --noEmit` reported no errors (both in the same scratch export) | Reproducible | The API is mocked in all 530 tests and `e2e-real/` was excluded: **real-backend integration of the Inventory page is not verified**; accessibility auditing is not done; other browsers and real devices are untested (390 px mobile-viewport emulation is covered in several specs, including Inventory, in Chrome only) |
| 5 | The built UI works against the real API (Ask flow) | 12/12 real-backend Playwright tests on the demo DB (run before the Inventory page was added; Inventory is not covered); database file hash unchanged after the run (asking is read-only) | Reproducible (`docs/V01_DEMO_GUIDE.md`) | Production data volumes; a real clinic's workflow |
| 6 | AI-2 meets the registered gates on the **development** set | Protocol v1.1 dev-run-09 (176 cases): A1 unsafe answers 0; A2 verifier failures 0; A3 wrong-but-evidenced 0 of 72 answers; A5 fabrications 0; A4/A7 misses within budget; B1–B8 and C1–C3 pass (e.g. B1 64/68 = 94.1%, Wilson 95% 85.8–97.7%; C1 English 95%, Manglish 95%, Malayalam 7/8) | `report.json` is LOCAL (`artifacts/models/longitudinal_eval/dev-run-09/`); protocol and run log TRACKED once committed (currently untracked, held) | **Generalisation.** The development set is what the system was tuned on over nine runs. The validation sets (60 intents) were never written and the one-shot acceptance set has not been run — so there is no unseen-data result for AI-2 |
| 7 | Who may ask (A6): unauthenticated, auditor and cross-clinic requests are denied identically for every wording, without reading the snapshot | `scripts/eval_actor_aware.py` on the shipping route: **policy v1_2 PASS (20 rows)**; **policy v1_1 FAIL on one control** (ADMIN gets 403 because `/ask` is DOCTOR-only); `tests/test_eval_actor_aware.py` (10 tests incl. fakes that the evaluator correctly fails) | TRACKED | Registered A6 is **not** passed: the registered protocol expects ADMIN not to be denied. Resolving that needs owner decision D-02 and a new protocol version (`docs/PROPOSED_PROTOCOL_v1_2_A6_ADMIN_CONTROL.md`, draft). Stand-in questions only, not the registered wordings |
| 8 | AI-3 (learned ranker) shows the facts a fixed template misses | One-shot test split, 988 synthetic patients, 5 facts: critical facts shown 407/407 vs 177/407 (miss rate 0.0% vs 56.5%); recall 1.00 vs 0.79; 0 unsupported facts shown by either arm | TRACKED (`eval/ai3/test_report.json`, `eval/ai3/README.md`) | Clinical usefulness: labels are a policy written by the same author (circular); a design change was made after a first dev run (disclosed in the card) |
| 9 | AI-1 local extractor is an evaluated component | Master plan §4A reports S1-OOD v3 prevented 24/29 confident errors (83%) and falsely rejected 4/35 (11.4%) | Claim from the master plan; raw outputs LOCAL and **not re-inspected in this build**; the unit tests need numpy and were not run | Final S1 test run is NOT RUN (owner-gated, one-shot). The extractor is not wired into the product path |
| 10 | The demo is reproducible from a clean setup | Steps in `docs/V01_DEMO_GUIDE.md`; `tests/test_demo_v01_ai.py` and the demo-script tests in the sandbox run (row 1) | See §3 clean-clone check | — |

### 1a. Verification environment for row 4 (isolation note)

- An earlier run of `inventory.spec.ts` alone (10 passed) used a loopback-only `sandbox-exec` profile that was **not proven restrictive**: a later check showed a public address could be reached from inside it. That earlier result is recorded here as a passing test run, **not** as an isolated one. Chrome's host rules and the specs' own request blocking were in force, but that does not make the earlier run's isolation proven.
- Before the full 530-test run the profile was corrected and checked: a public address was blocked, loopback listen and connect worked, and Chrome (with host-resolver rules) could not resolve an external host but could load a local page. The 530-test run then used the corrected profile for the server, `env -i` with a scratch home, a copy-on-write clone of the existing `node_modules` (nothing installed), and Chrome host rules. This verifies the environment of the 530-test run only; it does not retroactively prove the earlier run was isolated.
- The scratch copy of `playwright.config.ts` differed from the committed one only in the Chrome host rule, the sandboxed server command, and `reuseExistingServer: true` (the server was started under the profile by hand because `listen` was intermittently refused). Specs were not changed.

## 2. What was deliberately not done

| Item | Why |
|---|---|
| Acceptance run (`longitudinal_v1_accept`) | One-shot and owner-gated; the file was never opened, hashed, copied or inspected in this build |
| Validation sets `val_a`/`val_b` and native-speaker review | Require authorship and review by humans; the author must not invent either |
| Protocol fix freeze | Blocked by D-02 (ADMIN policy) and the missing validation sets; no freeze was written |
| Final AI-1 S1 run | Owner-gated, one-shot |
| Phase 3B (enforcement guardrails) | Needs explicit approval |
| Clinical validation, pilot, production deployment | Out of scope for v0.1 |
| Any push, merge, tag, or history change | Not authorised; all commits are local on `v0.1` |

## 3. Known defects and weaknesses

- AI-2's measured numbers are development-set numbers (row 6). Treat them as "the system does what it was tuned to do", not as an accuracy estimate.
- Malayalam and Manglish coverage is limited to the closed vocabulary; C1 Malayalam is 7/8 on dev, a very small sample, with no independent native-speaker validation.
- AI-3 labels are same-author; the 0.0% critical-miss result is partly by construction of the policy.
- Evidence files for AI-2 (and AI-1) are git-ignored; a manifest of 152 result files (≈23 MB) was prepared (not committed) so the owner can decide whether to preserve them in Git. 11 of them embed an absolute local path with the developer's username.
- 8 numpy-dependent tests are unverified in this build.
- No rate limiting, single-process SQLite, no deployment or monitoring work.

## 4. How a reviewer can check a claim

Rows 1, 4, 5, 7, 8, 10: follow `docs/V01_DEMO_GUIDE.md` §"Verifying the build". Rows 6 and 9 need the local artifacts (or an owner-approved re-run), and are labelled as such. Row 8's test run is spent; its recorded report is the evidence.
