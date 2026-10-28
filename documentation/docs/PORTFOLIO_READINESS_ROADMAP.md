# AyurCore v0.1 — Portfolio Readiness Roadmap

**Status:** AUTHORITATIVE for *remaining work, ordering and completion criteria* until v0.1 is portfolio-ready. It is the execution contract for QA and implementation sessions.
**Written:** 2026-10-10, from a read-only inspection of the repository at `v0.1` / `89d4af3` plus an uncommitted working tree.
**Relationship to other documents:** `AyurCore AI Portfolio Master Plan.md` remains the project contract (framing, AI capabilities, evaluation rules, the definition of done in its §19). This roadmap does **not** replace it and does not duplicate it; it records what is left, in what order, and how each item is verified. Where the two disagree, §F.4 lists the discrepancy instead of resolving it silently.
**Synthetic data only. Nothing here is clinical validation.**

## 0. How to use this document

1. Before starting any work package, read §D and §F. Work only on the **next authorized block** (§D.12).
2. A block is finished only when its completion criteria and its evidence (§D) exist. Implementation alone is never completion.
3. A roadmap entry is **not** authorization. Every item marked "Owner approval: yes" needs an explicit approval in the conversation before it is executed.
4. If evidence changes the plan, state the evidence and the proposed change first; do not edit this document silently.
5. Preserve completed work and owner decisions. Do not reopen completed fixes only to repeat earlier QA.

**Status vocabulary** (used in every table):
**VC** Verified complete (implementation *and* test evidence exist) · **IVI** Implemented, verification incomplete · **IP** In progress · **NS** Not started (no implementation evidence) · **BL** Blocked (a named dependency, decision or authorization) · **UNK** Unknown (repository evidence insufficient).

**Standing restrictions (from the owner; unchanged by this document):**
- The acceptance set (`documentation/eval/benchmarks/v2_longitudinal/longitudinal_v1_accept.json`) must not be read, hashed, copied, executed or otherwise inspected before the official acceptance run. The accept run is one-shot.
- The Block 7 protocol (`documentation/docs/AI2_LONGITUDINAL_EVALUATION_PROTOCOL_v1_1.md`, hash-registered, immutable) and its validation-before-acceptance sequence are preserved.
- `/ask` stays **DOCTOR-only, provisionally**; RBAC changes need explicit approval. Phase 3B needs explicit authorization before any work. No pinned evaluation file or frozen protocol artifact may change.
- Synthetic data only; no clinic checkout, live clinic database, real patient data or paid API calls.
- No commit, push, merge, reset, clean or discard without explicit approval. No raw `pytest`; no test runner that copies or exposes restricted material (§B.7).

---

## A. Target definition — what "portfolio-ready" means

AyurCore v0.1 is an **AI-first portfolio project**, not a clinic deployment. It is portfolio-ready when a technical reviewer can, from a clean clone and without the author present:

1. understand the problem, the two AI capabilities that were built (AI-1 notes → structured prescription; AI-2 grounded longitudinal patient questions) and the deterministic/AI boundary;
2. run a demonstration end to end on synthetic data;
3. reproduce the headline numbers, or see exactly which artifacts they came from and which were frozen;
4. read honest verification: what was tested, how, with what result, and what was *not* tested;
5. see sensible security, privacy and failure handling for a clinical-adjacent system, and the known limitations.

### A.1 Essential (required to declare readiness)
- A coherent demonstrable path (backend + one frontend surface) exercising AI-2, with a documented AI-1 demonstration path.
- Honest verification: full-suite result on the current tree through an isolated procedure compatible with the benchmark restrictions; real-backend end-to-end check of the AI-2 flow; unverified areas stated.
- The Block 7 evaluation protocol executed in order (freeze → validation → actor-aware evaluator → val runs → one accept run) **or** an owner-approved, documented decision to stop earlier and report only what was measured.
- Master plan §19 items that the owner has not descoped in writing — including the Consultation Brief (AI-3) comparison (item 6) — see decision D-05.
- README (current), architecture + decisions document, demo instructions, evaluation report with a claims-to-evidence table, dataset/model cards, known limitations.
- Reproducible setup from a clean clone, including how the git-ignored part of `artifacts/` (≈24 MB of result files and a 476 MB third-party pretrained model; 17 AI-1 files are tracked) is regenerated or why it is not.
- Repository cleanliness: all intended work committed on a branch that exists on a remote backup; no stray scripts; `.gitignore` reviewed; docs consistent with code.

### A.2 Optional polish (do only after A.1)
CI workflow; LICENSE; cross-browser run; automated accessibility scan; a short recorded demo; second demo patient set; latency/size measurements.

### A.3 Production-scale aspirations — explicitly **out of scope**
Cloud hosting, multi-tenant operations, scheduled backups/anchors, SQLite tamper-resistance (R2), clinician validation (R3), Phase 3B guardrail enforcement, live Gemini path verification (R8), new frameworks, new features not in the master plan.
These stay documented as limitations (`documentation/docs/TECHNICAL_RISK_REGISTER.md`), not as work.

---

## B. Verified current-state inventory

Evidence collected 2026-10-10 (read-only). "Not re-run" means no test was executed by this QA session unless stated.

### B.1 Repository and Git state
| Item | Finding | Status | Evidence |
|---|---|---|---|
| Branch / HEAD | `v0.1` at `89d4af3` (2026-10-06); 40 commits in history; tag `phase-2-freeze` | VC | `git log`, `git tag` |
| Uncommitted work | 12 tracked files modified; 93 untracked entries (216 files). **All of AI-2 (`ai2/`, `memory/`), most evaluation scripts/datasets, the longitudinal protocol and run log, 3 new frontend components, all new frontend specs and the new backend tests are untracked** | **Risk: single copy** | `git status --porcelain=v1 --untracked-files=all`; grouped in §D Block 1 |
| Remote | `origin` = `github.com/sriram615/ayurcore-os`; branches `main`, `backend/sriram`, `backend/shadow-recording`, `frontend/member-2`. **`v0.1` does not exist on the remote.** `HANDOFF.md`/README still say "no remote" | VC (finding) / docs stale | `git remote -v`, `git branch -a` |
| Merge/review status | No PR or merge record for v0.1 in the repository. **Correction (2026-10-10):** the remote is not merely possibly shared: `v0.1` is a worktree of the clinic project's repository (its `.git` file points into the clinic checkout's `.git/worktrees/`), so it shares that repository's object store, refs, config and `origin`. Repository visibility and access are not verifiable offline (owner requirement: private) | VC (structure) / UNK (visibility) | `.git` pointer file; `git config --show-origin`; `git worktree list` |
| Other worktrees | Two sibling worktrees (the clinic checkout and a shadow-recording worktree) of the same repository; their working trees and databases were **not accessed**; only repository metadata (config, refs, hooks listing) was read through Git | — | `git worktree list` |
| Stray root scripts | `dump_cases.py`, `parse_results.py`, `parse_results2.py` (untracked; evaluation helpers reading `artifacts/models/semantic_v3`); purpose not documented | UNK | file heads only |

### B.2 Backend (Phase 2 / 3A) — pre-AI work
| Item | Status | Evidence / note |
|---|---|---|
| Auth, RBAC, clinic isolation, audit chain, structured prescriptions, idempotent confirm | VC **as of 2026-09-27** | `documentation/docs/HANDOFF.md` §2: 473 passed through the isolated runner at `12f2e6a`. **Not re-run on the current tree** (see B.7) |
| Phase 3A guardrails (evaluation-only) | VC historical | `documentation/docs/PHASE_3_PLAN.md` §2.4–2.6; no production import |
| Phase 3B (enforcement) | NS — **proposed, needs explicit approval** | `documentation/docs/PHASE_3_PLAN.md` status line |
| API contract | IVI — v1.6 (2026-09-28) documents neither `/patients/{pk}/ask` nor `/inventory` | `documentation/AyurCore_API_Contract.md` (no match) |

### B.3 AI-1 (notes → structured prescription; local extractor T1/T2/T3 + R1–R5)
| Item | Status | Evidence / note |
|---|---|---|
| Implementation and frozen design | IVI | `extractor/`, master plan §4A (frozen 2026-10-06) |
| Evaluation | IVI | S1-OOD v1/v2/v3 spent: v3 prevented 24/29 confident errors (83%), falsely rejected 4/35 (11.4%) — **claims in master plan §4A; raw outputs in `artifacts/models/ood*_eval`, not re-inspected here. **Correction (2026-10-10): 17 files under `artifacts/models/{ood_eval,ood_v2_eval,ood_v3_eval,t1_tagger}` are tracked in Git, including the T1 model (`t1_tagger/model.json`); the rest of `artifacts/` is git-ignored (§B.10)** |
| Final S1 test run | NS (owner-gated, one-shot) | master plan §4A/§18 Block 10: `s1_test.json` not used |
| Wired into the backend | **No** — no `extractor` import in `backend/` or `agents/` | grep. The production `/prescribe/preview` still uses the Gemini parser; the local extractor is an evaluated component, not a product path |
| Unit tests (`tests/test_extractor_t*.py`, `test_t3_*`) | IVI | exist; not re-run by this QA |

### B.4 AI-2 (grounded longitudinal patient questions)
| Item | Status | Evidence / note |
|---|---|---|
| Production package `ai2/` (local, deterministic, no LLM) | IVI | 23 files untracked; `tests/test_ai2_package.py` + `test_ai2_service.py` |
| `POST /patients/{pk}/ask` (DOCTOR-only, read-only) | IVI | `backend/main.py`, `backend/rbac.py` (`memory_ask`), `backend/schemas.py`; `tests/test_ai2_api.py` |
| Defect Fix 1 — consistent snapshots (one read transaction; duplicate guard; 503 on failure) | IVI — targeted verification passed | **153 passed** in an allowlisted scratch sandbox (9 test files incl. the new `tests/test_ai2_snapshot_consistency.py`); negative control on the original adapter: 9 failed. Not run through the official runner (§B.7); owner review/closure not recorded; not in the master plan |
| Defect Fix 2 — empty incomplete herb/dosha list + `explicitNull` | IVI — targeted browser verification passed | master plan §18 Block 7 record: 319 shim tests, `tsc` pass, 7 Playwright tests pass (second run), screenshots reviewed. Full-suite, real-backend, other-browser, accessibility **outstanding** |
| Frontend "Ask about this patient" panel + Dashboard | IVI | `frontend/` (Next.js). **README/CONTRIBUTING call `frontend/` legacy and `apps/web/` the new frontend; `apps/web/` contains only a README** — see D-03 |
| Frontend suite | IVI | 13 spec files. Last full Playwright run (2026-10-10 12:42) **failed 4 tests, all timeouts** under heavy machine load (`frontend/test-results/.last-run.json`; traces show setup/sign-in stalls); not re-run — cause UNK |
| Real-backend end-to-end check | NS | no test drives the built UI against the real backend |

### B.5 Block 7 evaluation protocol (`longitudinal_v1`, protocol v1.1, registered 2026-10-08)
| Item | Status | Evidence / note |
|---|---|---|
| Protocol registered, immutable | VC | `eval/protocols/longitudinal_v1/REGISTRY_v1_1.json` |
| Development runs | VC | `run_log.jsonl`: dev-run-00 (baseline) … dev-run-09; all gate rows true |
| dev-run-09 on the dev set (176 cases) | VC (dev only) | `artifacts/models/longitudinal_eval/dev-run-09/report.json`: A1–A5, A7 pass; **A6 not applicable on dev**; B1–B8, C1–C3 pass; no stop condition. **Dev is the set the system was tuned on — this is not generalisation evidence.** |
| `fix_freeze` entry | **NS** — 0 entries in `run_log.jsonl` | verified by parsing the log |
| Validation sets `val_a` / `val_b` | **NS** — the spec is "SPECIFICATION ONLY: no questions generated, no cohort built". Requirement: **60 intents = 36 (val_a) + 24 (val_b)**; English ×2, Manglish ×1, Malayalam ×1 for ≥30 intents and every safety/access intent | `validation_spec_v1.json`; protocol §8. (Counted in *intents*, not cases.) |
| Native-speaker review of validation wordings (D2) | **NS** for the validation set (nothing to review yet). The v1-benchmark wording reviews are partly recorded: `AI2_LONGITUDINAL_LANGUAGE_REVIEW.md` says decisions applied, `AI2_LONGITUDINAL_MALAYALAM_REVIEW.md` still says "65 Malayalam wordings awaiting review" | UNK / discrepancy (§F.4) |
| Actor-aware evaluator (D4, gate A6) | **NS** — `scripts/eval_actor_aware.py` does not exist; spec says "to be built in a later block" | `actor_aware_evaluator_spec_v1.json` |
| Accept run | NS — **unopened by this QA**; protocol registry recorded the one-shot lock absent on 2026-10-08 | not inspected (restricted) |
| D5 configuration fingerprint | UNK — `backend/` files have changed since dev-run-09 (Defect Fixes 1–2 and the endpoint); the fingerprint is taken at the (not yet recorded) fix freeze | protocol §10 |

### B.6 Other evaluation assets (master plan §14)
S2–S6 and `v2_*` datasets, generators and tests exist but are **untracked**; their evaluation results are not recorded in the master plan. Status: IVI/UNK (a small inspection task, W-14).

### B.7 Test execution and isolation — important constraints
| Fact | Evidence |
|---|---|
| `scripts/run_isolated_tests.py` copies every tracked or untracked non-ignored file (minus `frontend/`, `*.db`, `.env*`). **The acceptance file is untracked and not ignored, so the runner copies it.** | `_files_to_copy`, line 98 |
| `tests/test_v2_longitudinal.py:37` → `scripts/v2_longitudinal.py:604` **hashes the accept file** during a full run; protocol §10 `no_tuning_on_accept` says accept is not "loaded, hashed, executed or inspected" before the accept run | source |
| `tests/test_ai2_equivalence.py` executes the pipeline on all 176 dev cases (counts as a dev "run" under protocol §10) | source |
| Consequence | The documented full-suite procedure (README, `documentation/CLAUDE.md`) is **not currently usable without conflicting with the protocol.** Targeted verification used an allowlisted scratch copy (`qa/tools/build_sandbox2.py`, 157 files, proven free of the accept file, databases and secrets). Whether that procedure may be adopted, or the runner amended, is decision D-04 |
| Latest results on record | 473 passed (2026-09-27, `12f2e6a`); 153 passed (targeted, current tree, scratch sandbox); 319 shim + 7 Playwright (Defect Fix 2). **No full-suite result exists for the current tree.** |

### B.7a Read-only assessment of the allowlisted scratch test procedure (2026-10-10, per D-04)

Inspected, not executed: `qa/tools/build_sandbox2.py` (scratch tooling outside the repository), `scripts/run_isolated_tests.py`, `tests/conftest.py`, `scripts/v2_longitudinal.py`, `scripts/protocol_longitudinal.py`, `scripts/eval_longitudinal_*.py` and the tests that reference them. The accept file was not opened, statted, hashed or copied.

- **What the scratch builder does.** Lists names with `git ls-files --cached --others --exclude-standard`; keeps only six directories (`backend/ agents/ ai2/ memory/ migrations/ extractor/`) and 17 named files; refuses any path containing `accept`, `longitudinal`, `benchmark`, `artifacts`, `__pycache__` or `.env`, or ending in a database/key suffix; copies by path; hashes only its copies. The accept file's path is refused by name before any open, stat or copy.
- **Source fidelity.** It copies from the working tree, so the allowlisted part is the current uncommitted source. It is an **incomplete copy by design**: `scripts/` (one file), `eval/`, `documentation/docs/`, `ui/`, `apps/`, `frontend/` and 91 of the 102 test files are not copied.
- **Not a full-suite procedure.** Many tests need `scripts/` and `documentation/eval/benchmarks/`, which the deny rules exclude.
- **The stock runner cannot be used** (it copies the accept file; §B.7).
- **A full-suite variant would be a different procedure** (copy every git-known file except the exact accept path, same suffix/secret denials). In a copy, tests resolve paths inside the sandbox, so the real accept file would not be opened. It would still need owner rulings on: (a) 13 test files reference the longitudinal tooling — `test_v2_longitudinal.py` reads and hashes the accept file (`:173`), `scripts/protocol_longitudinal.py:694` and `scripts/eval_longitudinal_*.py` stat it, and at least eight others (`test_safety_parity_fix1`, `test_vocabulary_fix2`, `test_ai2_equivalence`, …) load and execute the system on dev cases, which protocol §10 may count as runs; (b) `test_s1_ood_v2.py:171` hashes and `test_s1_benchmark.py:301` compares `s1_test.json`, the reserved final AI-1 holdout; (c) 7 test files reference git-ignored `artifacts/`.
- **Remaining uncertainties:** whether dev-case unit tests count as protocol runs; whether hashing `s1_test.json` is acceptable; which of the 102 files pass today; no network confinement for test processes (the Defect Fix 2 browser run used a loopback-only profile for servers).
- **Checks required before anyone executes any variant:** (1) the owner reviews the dry-run file list (names only, no copy) and it contains neither the accept path nor any database, key, `.env`, credential, `artifacts/` or `__pycache__` entry; (2) after copying, an exclusion proof by name, content-free (no file named or containing the accept file's name as data), no database or secret file, and the copy equals its source byte for byte for the files that were copied; (3) the Python environment, `HOME` and `TMPDIR` are scratch-only (`env -i`), no API keys, and no `.env` in any parent directory; (4) the sandbox database is created only inside the sandbox, with an assertion that its resolved path is inside it, plus the marker `tests/conftest.py` requires; (5) every test file that reads, stats, hashes or executes restricted material (the 13 listed above, `test_s1_ood_v2.py`, `test_s1_benchmark.py`) is either excluded by an explicit list with a reason or covered by an owner ruling; (6) a before/after comparison of the repository metadata snapshot (names, sizes, modification times, modes, `git status`) shows nothing changed; (7) the exact commands, file lists, logs and per-file results are kept.
- **Status:** inspection complete; adoption **not decided** (D-04).

### B.10 Findings from the pre-staging review and the read-only block inspections (2026-10-10)

**Holdout status of the datasets in C2/H (evidence: dataset READMEs, local one-shot summaries, lock files).**
| Dataset | State | Evidence |
|---|---|---|
| S3 test | spent 2026-10-06 | README; `artifacts/models/planner_eval/s3_test_summary.json` (`final: true`); the evaluator refuses a re-run |
| S4, S5, S6 | spent | READMEs; `artifacts/models/s4_eval`, `s5_eval`, `s6_eval` summaries (`final: true`) |
| PARA v1 `accept` | spent (Block 4B) | README; `artifacts/models/v2_block4b/para_accept_rows.json` |
| PARA v2 `heldout_synonyms.json` | probe labels opened 2026-10-07 (lock `PROBE_LABELS_OPENED`, local) | README access rules; lock file |
| PARA v2 `accept` (inside `para2_v1.json`) | **unopened** (no `ACCEPT_OPENED` lock) | README; `artifacts/models/v2_para2_locks/` |
| S2, devcal, hardneg | not holdouts | READMEs |

Consequence: the PARA v2 cluster (9 files) is held out of the preservation commits pending an owner ruling (Appendix A, AR-1).

**One-shot results of earlier AI-2 pipeline versions (descriptive; superseded by the Block 7 protocol; local artifacts only).** S3 test: exact plan 120/210 (57.1%); answer accuracy on answerable questions 103/162; abstention precision and recall 0.75 each, with 12 unsafe answers listed. S4 (97 cases): end-to-end 78/97 (80.4%); false accepts 4/63 (6.3%); false rejects 7/33; verifier tamper checks caught 120/120. S5 (119 cases): the single-reading arm 57/119 (47.9%); in the paired comparison the other arm was correct alone on 40 cases against 11 for the single arm. S6 (161 cases): end-to-end exact 128/161 (79.5%); correct interpretation 58/83 (69.9%).

**Preservation gap.** Those results, the dev-run reports and the other evaluation outputs exist only in git-ignored `artifacts/`: 154 files, about 24 MB, not counting the 476 MB third-party pretrained model. Spent one-shot results cannot be regenerated by design (the evaluators refuse a second final run). Eleven of the 154 files contain absolute local paths. Decision requested in AR-2.

**C3 / RBAC verification (diff and tests read, not run).** `backend/rbac.py` adds one docstring row and one key, `memory_ask = {DOCTOR}`; no existing key is added, removed or changed. `backend/main.py` removes no line and adds one route guarded by that entry, plus the 503 mapping. `tests/test_ai2_api.py` asserts that ADMIN and AUDITOR get 403 and the matrix value; `tests/test_rbac.py` checks only that entries use real roles.

**Pins checked read-only.** `ai2/data/planner_model.json` SHA-256 equals the pin in `ai2/service.py` and `ai2/PROVENANCE.json`; the 13 `memory/` provenance sources match; the 5 hash-registered protocol files match their registry. All of `ai2/`, `memory/` and `eval/protocols/` are currently untracked.

**Commit convention.** Subjects use `v0.1: …`, `Phase …`, `docs:` or `chore:`; 39 of 40 commits carry `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; author `Messi10`. The unpublished history contains absolute local paths in the OOD `run_metadata.json` files of commit `795daed` (sanitized at `HEAD` by `89d4af3`); this matters only if the branch is ever pushed.

**Test-procedure dry run (names only; nothing executed).** If a full-suite variant is adopted, the default exclusions are: the 13 test files that reference the longitudinal tooling (they read, stat, hash or execute dev cases); `tests/test_s1_ood_v2.py` (hashes `s1_test.json`) and `tests/test_s1_benchmark.py` (passes `s1_test.json` to `compare`; behavior unverified); the 4 legacy-UI tests that need `streamlit` (`test_streamlit_config`, `test_ui_auth_integration`, `test_ui_ux2`, `test_ui_ux3`), because installing it needs the network; and the 3 PARA v2 tests while that cluster is held. That leaves 80 of 102 test files. The holdout test in `test_extractor_t1.py` opens nothing (it patches `open` and expects a refusal).

**Consultation Brief (AI-3) design inspection.** No implementation exists. The plan specifies a deterministic template baseline, "AI salience selection + phrasing", a verifier, and a template-versus-AI comparison. The plan's AI-3 text assumes a language model, and Gemini was abandoned with no paid API authorized, so the model source is an owner decision (AR-2). Smallest design consistent with the plan and the repository: reuse the S2 cohort and the `ai2` executor, verifier and claim structure; a local learned salience ranker over deterministic facts; template phrasing; a verifier that rejects unsupported claims; a frozen synthetic evaluation set with gold salience labels; a comparison with the deterministic template baseline.

### B.8 Cross-cutting
| Area | Status | Note |
|---|---|---|
| Security/privacy | IVI | server-side sessions, Argon2id, RBAC, clinic isolation, audit chain, PII tokenization before Gemini (all VC historically). `/ask` writes nothing (verified: all 17 tables fingerprinted, only `sessions.last_seen_at` changes) and logs no question/record text. No audit event for `/ask` reads (consistent with existing history reads) |
| Failure handling | IVI | 401/403/404/422/503 behave as designed in the in-process probes; the UI shows one generic message for 403/404/503/timeout; `X-Error-Code` unused by the UI |
| Docs | IVI | README status dated 2026-09-27, no AI-1/AI-2 content; HANDOFF dated 2026-09-27; master plan's §6 note says AI-2 "assumes an LLM" but the built AI-2 uses none |
| Reproducibility | IVI | pinned `requirements*.txt`/`documentation/constraints.txt`; Python 3.14; no `venv/` in the repo; only 17 AI-1 artifact files are tracked (T1 model and OOD evaluation summaries/cases/metadata); the other 154 result files (≈24 MB) and a 476 MB pretrained model are git-ignored, and no regeneration guide is verified (§B.10) |
| CI / LICENSE | NS | none present (optional) |

### B.9 Confirmed QA findings still open (not re-investigated here)
| ID | Finding | Status |
|---|---|---|
| Q-1 | ADMIN gets 403 on `/ask` while the registered protocol's actor-aware control says an ADMIN own-patient ask "must NOT be denied" | unresolved decision D-02 |
| Q-2 | API/UI accept 1000 characters but the AI-2 normalizer abstains above 300 characters / 60 tokens; the doctor sees "can't be answered from the record" | confirmed, unfixed |
| Q-3 | `/ask` and `/inventory` missing from the API contract | confirmed, unfixed |
| Q-4 | Generic UI message for 403/404/503/timeout; unused `X-Error-Code` | risk |
| Q-5 | Evidence arrays are unbounded for large histories (UI caps display at 20) | risk |
| Q-6 | `memory/snapshot.py` (frozen evaluation builder) keeps the original paged-read defect by design; production uses the fixed `ai2` adapter | documented; limitation |

---

## C. Remaining work register

Priorities: **P0** blocks portfolio readiness; **P1** needed for a credible portfolio; **P2** worthwhile; **Opt** optional. Effort ranges assume one reviewed session of focused work ≈ 2–4 hours and are rough; "UNK" means a small inspection must precede an estimate.

### C.1 Preservation, hygiene and decisions
**W-01 — Preserve the uncommitted work** · P0 · Status: NS · *Blocked on D-01 (on hold: see F.0)*
- Outcome: all intended work committed on `v0.1` in logical commits, staged **by explicit path** (never `git add -A`: it would hash the accept file).
- Why: the AI-2 package, protocol, datasets and tests currently exist only in the working tree.
- Files: groups in §D Block 1. Acceptance: `git status` shows only deliberately excluded files; each commit builds the targeted tests; the accept file is not staged unless D-01 says so.
- Verification: `git status`, `git log --stat`, targeted tests unchanged. Approval: **yes**. Effort: 1–2 sessions (UNK: depends on commit granularity).

**W-02 — Publish a remote backup of `v0.1`** · P1 · NS · *Blocked on D-01 (the remote is the clinic project's repository; visibility unverified)*
- Acceptance: the branch exists on an owner-approved remote; nothing from the clinic project is mixed in. Approval: **yes**. Effort: <1 session.

**W-03 — Repository hygiene** · P1 · NS · after W-01
- Outcome: stray root scripts documented or removed (owner decides); `.gitignore` reviewed; generated `__pycache__` ignored; stale doc statements corrected only where evidence clearly supports it (remote exists, README AI-2 content).
- Acceptance: no unexplained untracked or root-level files; every kept script has a header saying what it is. Approval: yes for deletions. Effort: 0.5–1 session.

### C.2 Verification
**W-04 — Isolation-compatible test procedure** · P0 · NS · *Blocked on D-04 (read-only inspection done: §B.7a; adoption undecided)*
- Outcome: one written, owner-approved procedure for running backend tests that never copies, hashes or loads the accept file, and states which tests it excludes and why.
- Candidates: (a) the allowlisted scratch procedure already used; (b) amend `run_isolated_tests.py` to exclude restricted paths; (c) amend the hashing test. (b)/(c) change pinned files and need explicit approval.
- Acceptance: documented in README/`documentation/CLAUDE.md`; a dry-run proves exclusion without touching the accept file. Effort: 1 session.

**W-05 — Full backend suite on the current tree** · P0 · NS · deps W-04, W-01
- Acceptance: pass/fail counts recorded for every test file the procedure includes; every excluded file listed with its reason (equivalence, longitudinal); failures triaged, not hidden. Not "all pass" by exclusion alone.
- Verification evidence: logs, JUnit XML, file lists. Approval: yes (execution). Effort: 1 session + triage (UNK).

**W-06 — Real-backend end-to-end verification of the AI-2 Ask flow** · P0 · NS · deps W-04, D-03, D-02
- Outcome: the built frontend driven against the real FastAPI app on a **sandbox database** seeded with synthetic data: login → patient search → Ask on all-unknown, mixed, exact histories → rendered answer; plus 401/403/404/503 behavior.
- Acceptance: real replies (not captured fixtures) rendered correctly; no write beyond sessions; port/server cleanup shown. Approval: **yes** (starts servers). Effort: 1–2 sessions.

**W-07 — Resolve the four timed-out Playwright specs** · P1 · UNK · deps W-06 environment
- Outcome: controlled rerun (single worker, load recorded) distinguishing environment slowness from a defect (`patient-ask-clarify.spec.ts:326, 350, 482`). Acceptance: each passes, or a defect is filed with a trace. Approval: yes. Effort: 0.5–1 session.

**W-08 — Re-verify AI-1 and document artifact regeneration** · P1 · NS · dep W-04
- Outcome: AI-1 unit tests run through the approved procedure; a short guide stating which artifacts (`artifacts/models/t1_tagger`, OOD evaluation dirs) are regenerated by which script, and which are only reported. Acceptance: reviewer can regenerate or understands why not. Effort: 1–2 sessions (UNK).

### C.3 Defects and contract alignment
**W-09 — Resolve the ADMIN access policy** · P0 · BL (D-02: DOCTOR-only provisional, no RBAC change authorized; ADMIN question open: see F.0)
- Outcome: either `memory_ask` stays DOCTOR-only and a **new protocol version** changes the A6 control, or `memory_ask` widens to ADMIN with RBAC documentation/docs/tests/UI updated. Must precede the fix freeze (backend files are fingerprinted).
- Acceptance: policy written in `rbac.py` docs, the API contract and the protocol; tests agree. Approval: **yes**. Effort: 0.5–1 session.

**W-10 — Align the question-length limits** · P1 · NS · deps D-06
- Outcome: the user is told clearly when a question is too long. Do **not** change `ai2/normalize.py` (provenance-pinned). Options: UI `maxLength` and API limit to 300; or a specific message for `normalize` abstentions.
- Acceptance: 300/301/1000 characters and 60/61 tokens behave and read as intended in API and UI tests. Approval: yes (behavior). Effort: 0.5–1 session.

**W-11 — Update the API contract** · P1 · NS
- Outcome: `documentation/AyurCore_API_Contract.md` documents `/patients/{pk}/ask` (request/response, categories, 401/403/404/422/503, `X-Error-Code` values) and `/inventory`. Acceptance: matches observed behavior; reviewed against the OpenAPI output. Approval: yes (contract). Effort: 0.5 session.

**W-12 — Status-aware Ask error messages** · P2 · NS. 403/404/503/timeout distinguished using status and `X-Error-Code`. Approval: yes. Effort: 0.5 session.
**W-13 — Evidence payload bound check** · P2 · NS. Measure size/latency for a large synthetic history; add a limit only if needed. Effort: 0.5–1 session (UNK).

### C.4 Block 7 evaluation protocol (strict order)
**W-14 — Inspect S2–S6 / `v2_*` evaluation status** · P1 · **VC (done 2026-10-10, §B.10)** (read-only) · Outcome: for each dataset in master plan §14, which evaluation was run, with which artifact and result, or that none was. Acceptance: a table in this document (§B.6) replacing "UNK". Approval: no (read-only; the accept set stays excluded). Effort: 0.5 session.

**W-15 — Fix freeze** · P0 · BL · deps W-09, W-10, W-11 (docs), W-05, W-06
- Outcome: a `fix_freeze` entry in `run_log.jsonl` with the configuration fingerprint; no `memory/` or `backend/` change afterwards. Approval: **yes**. Effort: 0.5 session.

**W-16 — Build the validation sets (60 intents: val_a 36, val_b 24)** · P0 · NS · deps D-07
- Outcome: per `validation_spec_v1.json`: new controlled cohort with reference snapshots, quotas per capability, English×2/Manglish/Malayalam rules, ground truth, frozen manifest. The pipeline must **not** be run on candidate wordings before the freeze, and wordings must not enter lexicons, rules, tests or documentation visible to the fix author.
- Acceptance: every `pre_freeze_checks` item passes; sizes exactly 36 and 24 intents. Approval: **yes**. Effort: 3–5 sessions (UNK — new generator + cohort).

**W-17 — Native-speaker review of every Manglish/Malayalam validation wording** · P0 · NS · *human, owner-attested* · dep W-16
- Acceptance: a recorded verdict OK per wording; excluded wordings removed before the freeze of the set. Effort: owner-dependent (UNK).

**W-18 — Actor-aware evaluator (D4, gate A6)** · P0 · NS · deps D-02, W-09
- Outcome: `scripts/eval_actor_aware.py` per the spec; tests required by the spec (denies-everything fails controls, never-denies fails denials, snapshot-reading denial detected, leaky not_found detected, wording-dependent denial detected, no credentials in outputs); qualified on the dev-side controls and synthetic stand-ins (not accept) before val_b.
- It must evaluate the path that actually ships (`/ask`), and the ADMIN control must match the decided policy. Effort: 2–3 sessions (UNK).

**W-19 — val_a (≤1 per fix block) and val_b (exactly 1 after freeze)** · P0 · NS · deps W-15..W-18
- Acceptance: A1–A7 and D3 (generalisation gap ≤ 0.1) hold on val_b; failures inspected only as structural categories. Approval: **yes** (each run is budgeted and logged). Effort: 1–2 sessions + any fix-block loop (UNK).

**W-20 — The one-shot accept run** · P0 · NS · deps all of W-15..W-19, D1–D6 of the protocol (§7)
- Acceptance: every gate A1–A7, B1–B8, C1–C3, D1–D6 passes on the single run (both evaluators); otherwise REJECT is recorded. No re-run. Approval: **explicit, immediately before the run**. Effort: 1 session.

### C.5 Breadth required by the master plan
**W-21 — Consultation Brief (AI-3) with a template baseline and comparative evaluation** · P0 **unless descoped by D-05** · NS · master plan §2, §10, §18 Block 8, §19(6). Scope and effort UNK until a design inspection (W-21a, read-only) is done. Approval: yes.
**W-22 — Robustness evidence (master plan Block 9)** · P1 · IVI · consolidate existing adversarial/injection/cross-patient tests (`test_ai2_api.py`, safety-parity and guardrail tests) into a documented matrix and fill gaps. Effort: 1–2 sessions.
**W-23 — AI-1 single final S1 test run** · P1 · NS · owner-gated one-shot (master plan Block 10). Approval: **yes**. Effort: 0.5 session.

### C.6 Portfolio packaging
**W-24 — README rewrite** · P0 · NS. Current status, AI-first framing, setup, test procedure, demo, limitations; remove the stale "no remote"/phase-only status. Effort: 1 session.
**W-25 — Architecture and decisions document** · P0 · NS. Deterministic/AI boundary, AI-1 and AI-2 pipelines, why local models, abstention/verification design, snapshot-consistency design, key trade-offs. Effort: 1–2 sessions.
**W-26 — Reproducible setup from a clean clone** · P0 · NS · deps W-01, W-08. Fresh clone → install pinned deps → seed demo → run documented checks; artifact regeneration or provenance stated. Evidence: a transcript from a clean clone (not the author's tree). Effort: 1–2 sessions.
**W-27 — Demo path and script** · P0 · NS · deps D-03, W-06, `scripts/setup_v01_demo.py` / `run_v01_demo_server.py` (exist, not re-verified). Outcome: a written demo of AI-2 (all-unknown, mixed, exact, unsafe, clarification) and an AI-1 demonstration path (decision D-08: the extractor is not wired into the app). Effort: 1–2 sessions.
**W-28 — Evaluation report, dataset/model cards, claims-to-evidence table, limitations** · P0 · NS · deps W-20 (or the owner-approved stopping point). Every resume/README claim traces to an artifact; nothing says clinical validation. Effort: 2–3 sessions.
**W-29 — Frontend surface resolution** · P0 · BL (D-03 decided: `frontend/` is the demo surface; no migration authorized; wording corrections still need approval: see F.0). Either document `frontend/` as the demo surface (and correct README/CONTRIBUTING) or migrate to `apps/web/`. Migration is large and **not recommended** unless the owner requires it. Effort: docs-only 0.5 session; migration UNK/large.
**W-30 — Optional polish** · Opt · CI workflow; LICENSE; Firefox/Safari run; automated accessibility scan; recorded demo.

**Excluded by decision (not work):** Phase 3B enforcement; production hosting/backups; clinician validation; live Gemini verification; new frameworks; new AI features beyond the master plan.

---

## D. Ordered execution plan

Each block has one owner-reviewable outcome. A block starts only when its entry conditions hold and the previous block's evidence exists.

### Block 0 — Establish the roadmap and the read-only inventory · **DONE (this session)**
Evidence: this document; §B inventory; Block 1 commit manifest below; dev-run-09 gate review (read-only).

### Block 1 — Owner decision gate (no engineering)
- **Entry:** Block 0.
- **Scope:** the owner answers D-01…D-08 (§F). Nothing is executed.
- **Out of scope:** any code, commit, run.
- **Deliverable:** answers recorded in §F.
- **Completion:** D-01, D-02, D-03, D-04 answered (the others may follow in later blocks).
- **Evidence:** the updated §F.

*Commit manifest for W-01 (read-only result of the inventory; 216 files):*
| Group | Files | Note |
|---|---|---|
| `backend/*` (5 modified) | endpoint, RBAC, schemas, snapshot fix | commit with `tests/test_ai2_*` |
| `ai2/` (23) | production package incl. `data/planner_model.json` (327 KB, not ignored) | |
| `tests/` (36 + `tests/data` 1) | AI-2, memory, longitudinal tests; `ai2_dev_run_09_golden.json` (579 KB) | confirm D-01 on large goldens |
| `memory/` (56) | AI-2 evaluation package (provenance-pinned) | |
| `frontend/` (13) + `frontend/e2e` (13) | Ask panel, dashboard, libs, specs, fixtures | |
| `scripts/` (14), `documentation/eval/benchmarks/*` except v2_longitudinal (30), `eval/protocols/` (8), `documentation/docs/` (7) | generators, evaluators, datasets, frozen protocol files | protocol files are pinned: commit unchanged |
| root (4) | master plan (modified), 3 stray scripts | D-01/W-03 |
| **`documentation/eval/benchmarks/v2_longitudinal/` (6)** | **contains the restricted accept file** | **never `git add` the directory. `git add` hashes content. Owner decides separately whether and how the accept file is committed (D-01); the other five files may be staged by explicit path.** |

### Block 2 — Preserve the work (W-01, then W-02)
- **Entry:** D-01 answered. **Scope:** commits by explicit path per the manifest; optional push of `v0.1` if D-01 approves a remote. **Out:** content edits, formatting, history rewrite, pushing to `main`.
- **Deliverables:** commit list; clean `git status` apart from owner-approved exclusions.
- **Verification:** `git status`, `git show --stat` per commit, targeted tests (Block 3 procedure) still pass from the committed tree.
- **Completion:** nothing intended is untracked; the accept file untouched (mtime/size/mode unchanged).

### Block 3 — Isolation-compatible verification (W-04, W-05, W-08)
- **Entry:** D-04 answered; Block 2 done. **Scope:** write the procedure, run the full backend suite and AI-1 tests through it, record results. **Out:** any fix to failures (log them and stop for triage); the equivalence and longitudinal tests unless D-04 explicitly permits.
- **Verification:** exclusion proof (§B.7), logs, JUnit XML. **Completion:** a recorded count per test file; failures triaged with owner.

### Block 4 — Real-backend end-to-end (W-06, W-07)
- **Entry:** Block 3; D-03; D-02 (so the role behavior is known). **Scope:** sandbox backend + fresh scratch frontend build, synthetic data. **Out:** product changes.
- **Completion:** the three Ask histories verified against real replies; the four timed-out specs resolved or filed.

### Block 5 — Pre-freeze defects and contract (W-09, W-10, W-11, then W-12/W-13 if time)
- **Entry:** Block 4; D-02, D-06 answered. **Scope:** only the items listed; each small, reviewed, with tests. **Out:** AI-2 pipeline or `memory/` changes (provenance-pinned).
- **Completion:** acceptance criteria of W-09…W-11; targeted + full-suite results re-run through Block 3's procedure.

### Block 6 — Block 7 protocol, part 1: prerequisites (W-15 → W-18)
- **Entry:** Block 5 complete (no `backend/` or `memory/` changes may follow). **Order:** fix freeze → validation set build → native review → actor-aware evaluator build and qualification. Validation wordings are authored from intent descriptions only; the pipeline is not run on them.
- **Out:** running val_b or accept. **Completion:** D1–D5 of the protocol's gate list hold on paper (registered, validation frozen and reviewed, evaluator tested, fingerprint recorded).

### Block 7 — Block 7 protocol, part 2: runs (W-19, W-20)
- **Entry:** Block 6 evidence. **Order:** val_a (if any fix block remains) → val_b (exactly once) → inspect structurally → **owner approval** → accept (exactly once).
- **Completion:** run-log entries with hashes; verdict recorded as measured, including REJECT.

### Block 8 — Consultation Brief or a recorded descoping (W-21)
- **Entry:** D-05. If descoped, record the limitation and adjust the definition of done in writing. Otherwise: design inspection first, then template baseline, then comparative evaluation under the same discipline (frozen set, verifier, abstention).

### Block 9 — Robustness and AI-1 final run (W-22, W-23)
- **Completion:** robustness matrix with test references; the S1 test run recorded once, or the owner's decision not to run it.

### Block 10 — Packaging (W-24 … W-29)
- **Entry:** results frozen. **Order:** README → architecture/decisions → demo → reproducibility transcript from a clean clone → evaluation report/cards/claims table.
- **Completion:** §E checklist items for documentation, reproducibility and demonstration.

### Block 11 — Final acceptance review (§E)
An independent QA pass against §E using only recorded evidence; any unverified criterion is listed as a limitation, not claimed.

### D.12 Single next authorized action
**Block 1 — owner decisions D-01 to D-04.** Nothing in Blocks 2–11 is authorized by this document. After D-01 is answered, the first engineering action is Block 2 step 1: staging by explicit path the five `backend/` files and `tests/test_ai2_*` per the manifest, in one reviewed commit, with no push.

**Update 2026-10-10 (standing directive):** the independent read-only work is done (§B.10: W-14, the AI-1 and artifact mapping, the AI-3 design inspection, the test-procedure dry run, and the two factual corrections). The next authorized block is **Block 2, once AR-1 is approved**; AR-2 unblocks Blocks 3–9. Nothing else is executable without those approvals.

---

## E. Final portfolio acceptance checklist

The project is declared portfolio-ready only when **every** line is met with recorded evidence or is moved, with the owner's written agreement, to "Known limitations". All tests passing is **not** sufficient.

**Code and repository**
- [ ] All intended work committed; remote backup exists; `git status` clean except owner-approved exclusions; no unexplained scripts.
- [ ] Accept file unchanged (stat) until the accept run; protocol artifacts unchanged.
- [ ] No known open defect of severity "wrong answer shown as verified"; Q-2 resolved or documented.

**Testing**
- [ ] Full backend suite result on the current tree through the approved isolated procedure, with exclusions listed.
- [ ] Frontend: pure-function specs and Playwright specs pass or have recorded, triaged failures; real-backend end-to-end check recorded.
- [ ] Browser/OS/other coverage gaps (other browsers, accessibility) either run or listed.

**Evaluation**
- [ ] Block 7 protocol executed in order, or an owner-approved stopping point with exactly what was measured. Verdict recorded as measured.
- [ ] Every number in the README and evaluation report cites an artifact path and fingerprint; synthetic-data caveat on each.
- [ ] Brief (AI-3) comparison done or descoped in writing (D-05).

**Documentation and reproducibility**
- [ ] README current; architecture and decisions document; demo instructions; known limitations; risk register updated.
- [ ] A reviewer reproduces setup and the documented checks from a clean clone (transcript kept).
- [ ] Dataset and model cards; API contract matches the running API.

**Demonstration**
- [ ] A scripted demo of AI-2 (answer, incomplete, missing, unsafe, clarification) and an AI-1 path on synthetic data, runnable by someone else.

**Honesty**
- [ ] No claim of clinical validation, efficacy, safety or deployment anywhere.
- [ ] The Defect Fix records state which evidence is shim, Playwright, sandbox, or real-backend.

---

## F. Risks and unresolved decisions

### F.0a Approvals recorded (2026-10-10, owner)

**AR-1 approved:** local commits only on `v0.1`, using the exact C1, C2, C3, C5 and C6 manifests (Appendix A) and all pre/post-staging safeguards. Excluded: the nine-file PARA v2 cluster, C4, the five non-accept longitudinal files, the three root helper scripts and the acceptance file. Approved: committing the spent S3 test split and `ai2/data/planner_model.json`. Confirmed in scope: the existing `memory_ask` entry and `/ask` endpoint, DOCTOR-only access unchanged. Messages: the five proposed messages with the `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` trailer. No push, merge, reset, clean, tag, branch change or history rewrite.

**AR-2 approved:**
1. Adopt the extended allowlisted scratch procedure only after its exclusion and isolation safeguards are verified; no tests run until then.
2. `s1_test.json`-dependent tests and the four legacy-UI tests stay excluded from this verification pass; the exclusions are recorded accurately.
3. Question-length mismatch: frontend-only "question too long" notice for the `normalize` stage; API contract documented; API schema unchanged.
4. C7: manifest preparation only (no staging or commit); excludes the 476 MB third-party model and `semantic_v3/embeddings.npz`; the 11 files with local paths are flagged for a later explicit decision.
5. AI-3 Consultation Brief stays in scope: inspect the architecture, then implement the smallest suitable local learned salience ranker, no LLM, no paid API, no unrelated frameworks.
6. `/ask` stays DOCTOR-only (provisional). Before the fix freeze, prepare the proposed new protocol version and seek approval for the A6 ADMIN-control change; no RBAC change or protocol freeze without authorization.
7. No invented validation authorship or native-speaker review; independent work continues and one consolidated request names the human reviewers required and the blocked validation work.

**Execution rule:** work through the remaining authorized blocks in dependency order without per-block approval; stop only at genuine gates (the one-shot acceptance run, any push, Phase 3B). Synthetic data only. Portfolio finish line: a verified, reproducible demo, accurate evaluation evidence and limitations, setup instructions, architecture and API documentation, and the final readiness report; no readiness claim for anything unverified or blocked.

### F.0 Owner decisions recorded (2026-10-10)

Recorded only to the extent confirmed in the conversation. A decision here does **not** authorize any deferred engineering work.

| ID | Decision (owner's words) | What it does **not** do |
|---|---|---|
| D-01 | **Commits and pushes remain on hold until the file manifest, staging strategy, and remote safety are reviewed.** | Authorizes no commit, no push, no staging. Whether the remote is safe, how the accept file and large goldens are handled, and the staging strategy remain **open** pending that review. W-01/W-02 stay blocked. · **decision recorded in F.0; partly open** |
| D-02 | **`/ask` remains DOCTOR-only provisionally. No RBAC changes are authorized.** | Does not decide whether ADMIN may ever use `/ask`; that question, W-09 and the protocol's A6 ADMIN control stay **open**. · **decision recorded in F.0; partly open** |
| D-03 | **`frontend/` is the selected portfolio demo surface. No frontend migration is authorized.** | Authorizes no migration to `apps/web/`. Correcting README/CONTRIBUTING wording about `frontend/` being legacy is **not** authorized by this decision. · **decision recorded in F.0; partly open** |
| D-04 | **Inspect the existing allowlisted scratch test procedure read-only before deciding whether to adopt it. No tests or runner modifications are authorized.** | Does not adopt the procedure and does not authorize running tests, building a sandbox, or changing `run_isolated_tests.py`. The inspection was performed 2026-10-10 (§B.7a); the adoption decision remains with the owner. · **decision recorded in F.0; partly open** |

D-05, D-06 and D-07 have no recorded decision.

### F.5 Approval requests (consolidated 2026-10-10) — AR-1 and AR-2 APPROVED, see F.0a

**AR-1 — Preservation (blocks Block 2).** Approve **local commits only** on `v0.1`, in the order C1, C2, C3, C5, C6, by the literal manifests in Appendix A, with the safeguards in §D Block 2. Decisions inside AR-1: (a) the PARA v2 cluster (9 files) stays **held** (recommended) or is staged; (b) the spent S3 test split and `ai2/data/planner_model.json` may be staged; (c) committing the existing `memory_ask` entry and `/ask` endpoint is within D-02 (DOCTOR-only, provisional, no change to any existing permission); (d) commit messages as drafted in the 2026-10-10 report, trailer to be chosen: this session's attribution (`Claude Sonnet 5.5`, recommended because it names the model that made these changes) or the repository's historical `Claude Opus 5.5`; (e) no push, merge, reset, clean, tag, branch or history rewrite. Excluded in all cases: C4 (33 files), the five non-accept `v2_longitudinal` files, the three root scripts, the acceptance file.

**AR-2 — Bundled decisions that unblock Blocks 3–9 (recommended defaults).**
1. D-04: adopt an extended allowlisted scratch procedure (all git-known files except the exact accept path and database/secret/generated patterns; `env -i`; sandbox database) with the default exclusions in §B.10. Alternative: keep the narrow 11-file procedure only.
2. Whether the two tests that hash or pass `s1_test.json`, and the 4 legacy-UI tests, stay excluded (recommended) or are covered by rulings or installation.
3. D-06: resolve the length mismatch in the frontend only (a specific "question too long" notice when the reply's stage is `normalize`; no API schema change; document it in the contract). Alternative: lower the API and UI limit to 300.
4. Evidence preservation (C7): prepare a manifest to commit the 154 result files (about 24 MB; excluding the 476 MB pretrained model and `semantic_v3/embeddings.npz`), force-added by literal path list, with a ruling on the 11 files containing absolute local paths (commit as is, or exclude). Default: **prepare the manifest only**; staging needs a later explicit approval.
5. D-05: the Consultation Brief is built (not descoped), with a **local learned salience ranker, no LLM and no paid API** (recommended). Alternative: written descoping.
6. Before the fix freeze (not now): the final ADMIN policy. Recommended: DOCTOR-only final, with a new protocol version changing the A6 ADMIN control.
7. D-07: who authors the validation set independently of the fix author, and who is the native reviewer.

Blocked until AR-1: all commits (Block 2). Blocked until AR-2: Block 3 (item 1–2), the length fix in Block 5 (item 3), Block 8 (item 5), Block 6 (items 6–7). Nothing further is executable without these approvals except more read-only inspection.

### F.1 Decisions that **block** readiness
| ID | Question | Blocks | Needed from |
|---|---|---|---|
| D-01 | May the uncommitted work be committed on `v0.1`? How are the restricted accept file (hash-on-add) and large goldens (`ai2_dev_run_09_golden.json`, 579 KB) handled? Is `origin` a safe place to push `v0.1` (it is the clinic project's repository: `v0.1` is a worktree of it)? | W-01, W-02, W-26 | Owner · **decision recorded in F.0; partly open** |
| D-02 | Should ADMIN be allowed to use `/ask`? (Provisionally DOCTOR-only.) If DOCTOR-only: register a new protocol version changing the A6 ADMIN control. | W-09, W-18, W-15 | Owner (product + RBAC) · **decision recorded in F.0; partly open** |
| D-03 | Which frontend is the portfolio's demo surface: `frontend/` (where AI-2 UI exists; README calls it legacy) or `apps/web/` (empty)? | W-06, W-27, W-29 | Owner · **decision recorded in F.0; partly open** |
| D-04 | How to run the full backend suite without touching the accept file: adopt the allowlisted scratch procedure, amend the runner, or amend the hashing test? | W-04, W-05 | Owner · **decision recorded in F.0; partly open** |
| D-05 | Is the Consultation Brief (AI-3) in scope for "portfolio-ready", or formally descoped (changing master plan §19(6))? | W-21 | Owner |
| D-06 | How should the length mismatch be resolved (limit to 300 in UI/API, or a specific message)? | W-10 | Owner |
| D-07 | Who authors the validation set so that it stays independent of the fix author (contamination rules), and who is the native reviewer (D2/W-17)? | W-16, W-17 | Owner |

### F.2 Questions that can be documented as limitations
- D-08: the AI-1 extractor is not wired into the application; the demo shows it via scripts. Wiring it is a feature, not required unless the owner decides otherwise.
- Whether val_a is required if no further fix block occurs (protocol says "once per fix block").
- dev-run-09 passes all gates only on the dev set, which is the tuning set; this is not generalisation evidence until validation exists.

### F.3 Risks
| Risk | Effect | Mitigation |
|---|---|---|
| Work exists only in one working tree | total loss on disk failure or a stray `git clean` | Block 2 |
| `git add -A` or the stock runner touches the accept file | breaks the one-shot holdout and protocol §10 | explicit-path staging; D-04 |
| `backend/` or `memory/` changes after the fix freeze | invalidates D5; accept run blocked | Block 5 before Block 6 |
| Dev gates all pass | over-fitting to dev is undetected until validation exists | Blocks 6–7 |
| Machine load (observed load average up to ~95) | spurious Playwright timeouts | single worker, record load, rerun before judging |
| Stale docs (README, HANDOFF, master plan §6, API contract) | a reviewer distrusts the repo | Blocks 5 and 10 |
| Large untracked datasets/artifacts | clone cannot reproduce | W-26 |

### F.4 Documented discrepancies (not resolved here)
1. `documentation/README.md`/`HANDOFF.md` say there is no git remote; `git remote -v` shows `origin`.
2. README/CONTRIBUTING call `frontend/` legacy and assign new UI to `apps/web/`; all AI-2 UI work is in `frontend/`; `apps/web/` has only a README.
3. Master plan §6 says AI-2/AI-3 assume an LLM and the model source must be settled; the built AI-2 uses no LLM.
4. The two language-review documents disagree on whether 65 Malayalam wordings are reviewed.
5. The master plan does not mention Defect Fix 1, the AI-2 package, or the endpoint; Block statuses 7–11 are unmarked.
6. A request referred to `val_a` (36) and `val_b` (24) as cases; the protocol defines them as **intents** (60 total); the number of wordings is larger.

---

## G. Evidence index

- Plan and protocol: `AyurCore AI Portfolio Master Plan.md` (§§2, 4A, 6, 14, 18, 19); `documentation/docs/AI2_LONGITUDINAL_EVALUATION_PROTOCOL_v1_1.md`; `eval/protocols/longitudinal_v1/` (`protocol_v1_1.json`, `REGISTRY_v1_1.json`, `validation_spec_v1.json`, `actor_aware_evaluator_spec_v1.json`, `run_log.jsonl`).
- Backend history: `documentation/docs/HANDOFF.md`, `documentation/docs/PHASE_3_PLAN.md`, `documentation/docs/TECHNICAL_RISK_REGISTER.md`, `documentation/CLAUDE.md`, `documentation/README.md`, `documentation/CONTRIBUTING.md`.
- AI-2 code: `ai2/`, `backend/main.py` (`ask_patient_history`), `backend/rbac.py`, `backend/schemas.py`, `backend/patient_store.py`, `ai2/adapters/clinic_stores.py`.
- Frontend: `frontend/lib/ask-*.ts`, `frontend/components/patient-ask.tsx`, `ask-result.tsx`, `dashboard.tsx`, `frontend/e2e/`.
- Dev results: `artifacts/models/longitudinal_eval/dev-run-09/report.json` (git-ignored, local).
- QA artifacts (scratch, outside the repo; may be deleted with the session): isolation proofs, probe outputs, shim/Playwright logs, screenshots and the allowlisted-sandbox tools used for Defect Fixes 1–2.
- Test isolation facts: `scripts/run_isolated_tests.py`, `tests/test_v2_longitudinal.py`, `scripts/v2_longitudinal.py`, `tests/test_ai2_equivalence.py`.

## I. Execution record after AR-1 / AR-2 (2026-10-10)

Supersedes the status cells in §B/§C where they disagree. Evidence for every row is in `documentation/docs/V01_EVALUATION_REPORT.md`; the verdict is in `documentation/docs/V01_PORTFOLIO_READINESS_REPORT.md`.

| Item | Result |
|---|---|
| AR-1 preservation | Done locally: commits `6d5a0ef`, `10d8b1e`, `9057b03`, `d401a45`, `71b5ae2` (C1, C2, C3, C5, C6) and follow-ups `58760cb`, `31f701e`, `add0965`, `dc89f73`, `f0b7cf2`, `adb9458`. Nothing pushed. Held: PARA v2 cluster (9), C4, five longitudinal files, three root helper scripts, the acceptance file (never opened) |
| D-04 test procedure | Adopted (`scripts/run_scratch_sandbox_tests.py`, tracked files only). Latest: **862 passed, 1 xfailed, 8 failed** (all `numpy` missing) |
| Frontend | Latest (scratch export of `dd5c34b`, includes the Inventory page): **530/530 mocked-API Playwright tests passed**, 16 spec files, one worker, about 9 min, Chrome; production build and strict `tsc` clean; `e2e-real/` excluded. Earlier: 520/520 before the Inventory commit; 12/12 real-backend Ask-flow tests (before the Inventory page). **Not verified:** real-backend Inventory, accessibility. The earlier Inventory-only run's sandbox was not proven restrictive (see evaluation report §1a) |
| Question-length notice | Done frontend-only; contract v1.7 documents `/ask` and `/inventory` |
| AI-3 Brief | Built and evaluated one-shot (`eval/ai3/`, spent); synthetic same-author labels |
| Demo | `setup_v01_demo` → `seed_v01_demo_histories` → `demo_v01_ai` verified from a clean clone of the committed tree |
| W-18 actor-aware evaluator | Built (`scripts/eval_actor_aware.py`); shipping route passes proposed v1.2, fails registered v1.1 on the ADMIN control only |
| W-09 / D-02 | Still open for the owner: `documentation/docs/PROPOSED_PROTOCOL_v1_2_A6_ADMIN_CONTROL.md` (draft; nothing registered, no RBAC change, no freeze) |
| C7 artifact manifest | Prepared in scratch only (152 files ≈ 23 MB; 11 contain an absolute local path); not staged or committed |
| Validation sets, native review, accept run, AI-1 final S1, Phase 3B | **Not done — blocked on owner / human reviewers** |
| Docs | README rewritten; architecture, demo guide, evaluation report, AI-3 card added |

## H. Change log of this document
- 2026-10-10 — created from the read-only inspection above. Block 0 complete; the next authorized action is the owner decision gate (§D.12).
- 2026-10-10 — standing directive: factual corrections (the worktree shares the clinic repository; AI-1 artifacts partly tracked), §B.10 findings, §F.5 approval requests AR-1 and AR-2, Appendix A manifests, W-14 closed, §D.12 updated.
- 2026-10-10 — owner decisions D-01…D-04 recorded (§F.0); isolation assessment added (§B.7a); §D.12 updated; pointer annotations on W-01, W-04, W-09, W-29. Other sections not rewritten (see the session report for wording now in tension).

- 2026-10-10 — execution record §I added after AR-1/AR-2 (other sections not rewritten; §I wins where they disagree).

---

## Appendix A — Literal path manifests for AR-1 (generated 2026-10-10; one path per line)

Counts reconcile: 58 + 48 + 32 + 26 + 2 = 166 approved candidates; 9 held; 33 (C4) and 3 root scripts excluded; 211 files in all. The five non-accept `documentation/eval/benchmarks/v2_longitudinal/` files and the acceptance file appear in no list.

### C1 — AI-2 evaluation package (58)

```
memory/__init__.py
memory/answer.py
memory/c10.py
memory/confidence.py
memory/coverage.py
memory/cue_tagger.py
memory/evaluate_planner.py
memory/evaluate_s4.py
memory/evaluate_s5.py
memory/evaluate_s6.py
memory/evaluate_v2.py
memory/evaluate_v2_block4.py
memory/evaluate_v2_block5.py
memory/evaluate_v2_confidence.py
memory/evaluate_v2_oos.py
memory/evaluate_v2_reference.py
memory/evaluate_v3_semantic.py
memory/executor.py
memory/interpret.py
memory/morph.py
memory/normalize.py
memory/oos.py
memory/pipeline.py
memory/planner.py
memory/reference.py
memory/retrieval.py
memory/s2_build.py
memory/s2_reference.py
memory/scope.py
memory/semantic.py
memory/snapshot.py
memory/systems/baseline_2a/confidence.py
memory/systems/baseline_2a/coverage.py
memory/systems/baseline_2a/interpret.py
memory/systems/baseline_2a/normalize.py
memory/systems/baseline_2a/reference.py
memory/systems/baseline_2a/scope.py
memory/systems/baseline_2a/v2.py
memory/systems/fix1/reference.py
memory/systems/fix1/scope.py
memory/systems/fix2/coverage.py
memory/systems/fix2/interpret.py
memory/systems/fix3/coverage.py
memory/systems/fix4/scope.py
memory/systems/fix5/coverage.py
memory/systems/fix5/interpret.py
memory/systems/fix6/c10.py
memory/systems/fix7/coverage.py
memory/systems/fix7/interpret.py
memory/systems/fix8a/coverage.py
memory/systems/fix8a/interpret.py
memory/systems/fix8b/coverage.py
memory/systems/fix8b/interpret.py
memory/timeline.py
memory/v2.py
memory/verifier.py
tests/test_memory_normalize.py
tests/test_memory_coverage.py
```

### C2 — datasets, generators, tests (48)

```
scripts/s2_generator.py
scripts/s3_generator.py
scripts/s4_dataset.py
scripts/s5_dataset.py
scripts/s6_dataset.py
scripts/v2_devcal.py
scripts/v2_hardneg.py
scripts/v2_para.py
documentation/eval/benchmarks/s2/documentation/README.md
documentation/eval/benchmarks/s2/manifest.json
documentation/eval/benchmarks/s2/s2_v1.json
documentation/eval/benchmarks/s3/documentation/README.md
documentation/eval/benchmarks/s3/manifest.json
documentation/eval/benchmarks/s3/s3_dev.json
documentation/eval/benchmarks/s3/s3_test.json
documentation/eval/benchmarks/s3/s3_train.json
documentation/eval/benchmarks/s4/documentation/README.md
documentation/eval/benchmarks/s4/manifest.json
documentation/eval/benchmarks/s4/s4_v1.json
documentation/eval/benchmarks/s5/documentation/README.md
documentation/eval/benchmarks/s5/manifest.json
documentation/eval/benchmarks/s5/s5_v1.json
documentation/eval/benchmarks/s6/documentation/README.md
documentation/eval/benchmarks/s6/manifest.json
documentation/eval/benchmarks/s6/s6_v1.json
documentation/eval/benchmarks/v2_devcal/documentation/README.md
documentation/eval/benchmarks/v2_devcal/devcal_v1.json
documentation/eval/benchmarks/v2_devcal/manifest.json
documentation/eval/benchmarks/v2_hardneg/documentation/README.md
documentation/eval/benchmarks/v2_hardneg/hardneg_v1.json
documentation/eval/benchmarks/v2_hardneg/manifest.json
documentation/eval/benchmarks/v2_para/documentation/README.md
documentation/eval/benchmarks/v2_para/manifest.json
documentation/eval/benchmarks/v2_para/para_v1.json
tests/test_memory_foundation.py
tests/test_memory_interpret.py
tests/test_memory_planner.py
tests/test_memory_retrieval.py
tests/test_memory_v2.py
tests/test_s4.py
tests/test_s5.py
tests/test_s6.py
tests/test_v2_block2.py
tests/test_v2_block2a.py
tests/test_v2_block3.py
tests/test_v2_block4b.py
tests/test_v2_devcal.py
tests/test_v2_para.py
```

### C3 — AI-2 service, endpoint, snapshot fix (32)

```
backend/encounter_store.py
backend/main.py
backend/patient_store.py
backend/rbac.py
backend/schemas.py
ai2/PROVENANCE.json
ai2/__init__.py
ai2/adapters/__init__.py
ai2/adapters/clinic_stores.py
ai2/answer.py
ai2/answering.py
ai2/c10.py
ai2/confidence.py
ai2/coverage.py
ai2/data/planner_model.json
ai2/executor.py
ai2/interpret.py
ai2/interpretation.py
ai2/normalize.py
ai2/planner.py
ai2/ports.py
ai2/reference.py
ai2/result.py
ai2/scope.py
ai2/service.py
ai2/snapshot.py
ai2/verifier.py
ai2/vocabulary.py
tests/test_ai2_api.py
tests/test_ai2_service.py
tests/test_ai2_package.py
tests/test_ai2_snapshot_consistency.py
```

### C5 — frontend (26)

```
frontend/app/globals.css
frontend/components/ask-result.tsx
frontend/components/ayurcore-app.tsx
frontend/components/dashboard.tsx
frontend/components/patient-ask.tsx
frontend/components/patients.tsx
frontend/e2e/ask-evidence-presence.spec.ts
frontend/e2e/ask-logic.spec.ts
frontend/e2e/ask-present-empty-partial.spec.ts
frontend/e2e/ask-present-ui.spec.ts
frontend/e2e/ask-present.spec.ts
frontend/e2e/ask-view.spec.ts
frontend/e2e/dashboard.spec.ts
frontend/e2e/fixtures/ask-empty-partial.ts
frontend/e2e/fixtures/ask-real.ts
frontend/e2e/patient-ask-clarify.spec.ts
frontend/e2e/patient-ask-empty-partial.spec.ts
frontend/e2e/patient-ask-results.spec.ts
frontend/e2e/patient-ask.spec.ts
frontend/lib/api-types.ts
frontend/lib/api.ts
frontend/lib/ask-present.ts
frontend/lib/ask-state.ts
frontend/lib/ask-view.ts
frontend/lib/errors.ts
frontend/lib/format.ts
```

### C6 — project documents (2)

```
AyurCore AI Portfolio Master Plan.md
documentation/docs/PORTFOLIO_READINESS_ROADMAP.md
```

### H — PARA v2 cluster, HELD pending owner ruling (9)

```
documentation/eval/benchmarks/v2_para2/documentation/README.md
documentation/eval/benchmarks/v2_para2/heldout_synonyms.json
documentation/eval/benchmarks/v2_para2/manifest.json
documentation/eval/benchmarks/v2_para2/para2_v1.json
scripts/v2_para2.py
tests/test_v2_para2.py
tests/test_semantic_v3.py
tests/test_v2_block5.py
scripts/train_cue_tagger.py
```
