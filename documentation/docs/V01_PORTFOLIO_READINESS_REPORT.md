# AyurCore v0.1 — portfolio readiness report

Date: 2026-10-10 · Branch `v0.1` at the commit that contains this file · Reviewer: the independent QA session (Claude Sonnet 5.5). All data synthetic. Nothing was pushed, merged, tagged, reset or cleaned.

## Verdict

**Ready to demonstrate and to present as an engineering portfolio piece, with its limits stated. Not ready to be described as clinically validated, accepted, or deployed — and that is stated in the README.**

- **Ready (verified):** a reproducible local demo (clean clone: setup → seed → AI demo script ran with exit 0); a working DOCTOR-only "Ask about this patient" flow end to end in the browser; AI-3 brief; API contract v1.7; setup, architecture, demo and evaluation documents; test procedures that cannot open the application database or the restricted acceptance set.
- **Not ready / not claimed:** any unseen-data result for AI-2 (validation sets and the one-shot acceptance run do not exist/were not run); registered gate A6; any clinical claim; AI-1 end-to-end verification in this build; a push/remote backup.

## Checklist (roadmap §E)

| Line | Status | Evidence / note |
|---|---|---|
| All intended work committed; remote backup; clean status | **Partly.** 12 local commits since `89d4af3` (including the one holding this file), none pushed (no remote backup). **Held, uncommitted by instruction (AR-1): about 50 untracked files** (`git status`), including the registered longitudinal protocol (`eval/protocols/`), its run log, evaluation scripts and tests, the PARA v2 cluster, protocol review documents, three root helper scripts | Single-copy risk remains for those files — see request 1 |
| Accept file untouched; protocol artifacts unchanged | **Met** — the acceptance file was never opened, hashed, copied or listed by content; no pinned artifact or frozen protocol file was edited | metadata snapshots recorded in the QA scratch area |
| No known open "wrong answer shown as verified" defect | **Met on what was tested** — Defect Fixes 1–2 and the evidence-field fix closed; dev gates A1/A3/A5 report 0. Not a proof of absence on unseen questions | evaluation report rows 3, 6 |
| Full backend suite through an approved isolated procedure | **Met with exclusions** — 862 passed, 8 failed (numpy missing), exclusions listed in `scripts/run_scratch_sandbox_tests.py` | |
| Frontend specs + real-backend end-to-end | **Partly met.** Mocked-API suite: 530/530 passed (16 spec files, Chrome, scratch export of `dd5c34b`; strict `tsc` and production build clean). Real-backend: 12/12 for the Ask flow, run before the Inventory page existed. **Not verified:** real-backend integration of the Inventory page, accessibility, other browsers | The 530 tests mock the API; `e2e-real/` was excluded from that run |
| Other browsers / accessibility | **Not done**, listed as a limitation | |
| Block 7 protocol executed or an owner-approved stopping point | **Owner decision needed.** Dev gates pass; validation, A6 and accept not done | requests 2–4 |
| Numbers cite artifacts; synthetic caveat on each | **Met** in the evaluation report; AI-2 dev and AI-1 artifacts are local files (labelled) | |
| AI-3 comparison done or descoped | **Done** (synthetic, same-author labels, disclosed) | `eval/ai3/documentation/README.md` |
| README current; architecture; demo; limitations; risk register | **Met** except `documentation/docs/TECHNICAL_RISK_REGISTER.md` and `documentation/docs/HANDOFF.md`, which are still dated 2026-09-27 and were not rewritten | |
| Clean-clone reproduction with transcript | **Partly.** Python side verified from a clean clone of the committed tree using the existing venv. **Not verified:** a fresh `pip install` and a fresh `pnpm install` (network not approved for this) | |
| Scripted demo of AI-2 (answer, incomplete, missing, unsafe, clarification) and an **AI-1 path** | AI-2 and AI-3: **met**. **AI-1 path: not met** — it requires numpy and is not wired into the product | request 5 |
| No claim of clinical validation/efficacy/deployment | **Met** | README, evaluation report |
| Defect-fix records state evidence type | **Met** | evaluation report |

## Consolidated request (one list; nothing below was done)

**Owner approvals / decisions**
1. **Preserve the held files.** Authorise a commit (or a different safe copy) of the registered protocol (`eval/protocols/`, protocol docs), dev-run evaluation scripts/tests, `tests/data/ai2_dev_run_09_golden.json`, and the PARA v2 cluster, excluding the acceptance file. Until then they exist once, on this machine.
2. **D-02 / A6:** decide DOCTOR-only final + register protocol v1.2 (recommended; draft in `documentation/docs/PROPOSED_PROTOCOL_v1_2_A6_ADMIN_CONTROL.md`), or widen `/ask` to ADMIN with an RBAC change. Without this A6 cannot pass as registered.
3. **Evaluation stopping point:** present AI-2 results as development-set results only (current wording), or fund the validation work in request 4 and the one-shot acceptance run. The acceptance run needs your immediate explicit approval at run time.
4. **Evidence preservation (C7):** commit the 152 prepared result files (≈23 MB), exclude them, or sanitise the 11 that contain an absolute local path (`/Users/<name>/…`). The 476 MB pretrained model and `semantic_v3/embeddings.npz` stay excluded.
5. **Network install:** approve `pip install numpy==2.5.3` (pinned) into the scratch venv to run the 8 AI-1 tests; and fresh `pip`/`pnpm` installs for a true clean-room check. AI-1's final one-shot S1 run remains yours to authorise.
6. **Push / remote backup:** none done. The branch lives in a worktree of the clinic repository; where it may be pushed, and whether the repository is private, is your call.

**Human reviewers required (not invented here)**
- **Validation question author** — 60 intents (val_a 36, val_b 24; English ×2, Manglish ×1, Malayalam ×1 for ≥30 intents and every safety/access intent), written independently of the developer who tuned the system.
- **Native Malayalam and Manglish reviewers** for those wordings (and the still-open v1 Malayalam review sheet).
- **A clinician or senior Ayurvedic practitioner** to author or approve the "what a doctor needs first" standard for AI-3 (today's labels are the developer's own), and for any clinical wording claim.
- **An independent engineer/reviewer** to repeat the clean-clone check on another machine.

**Blocked until the above:** validation runs, registered A6, the fix freeze, the accept run, any unseen-data AI-2 claim, any clinical claim, Phase 3B.

## What I recommend you do next

Stop adding scope. Record the walkthrough using `documentation/docs/V01_DEMO_GUIDE.md` §3 and keep the limits slide from the README table. Then answer requests 1 and 6 first (they protect your work); the rest can wait until after the portfolio is live.
