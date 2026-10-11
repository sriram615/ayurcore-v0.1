# AyurCore OS — Engineering Handoff (2026-09-27)

**Status in one line:** Phase 2 and Phase 3A (evaluation-only) are engineering-complete and verified by tests, and the API is running the current commit. **The system is not clinically validated and not production-ready.**

**Frontend continuation:** see `docs/FRONTEND_HANDOFF.md` (API reference, workflows, UI states, acceptance checklist, and how to resume frontend work).

Labels:
- **VERIFIED** — observed on 2026-09-27, with the evidence named.
- **ATTESTED** — reported by the owner; not independently verified.
- **UNVERIFIED** — not checked.

## 1. Snapshot (VERIFIED)

| Item | Value | Evidence |
|---|---|---|
| Git HEAD | `12f2e6a56dc4d683a620db3d1438c34e13c47394`, working tree clean before this handoff | `git rev-parse HEAD`, `git status --porcelain` |
| Tags / remotes | `phase-2-freeze` -> `41a2d1f`; **no remote** | `git tag -l`, `git remote -v` |
| Running API | **PID 87712**, 127.0.0.1:8000, started 2026-09-27 21:39:06 IST from the project root on `venv/`, commit `12f2e6a` | `lsof -iTCP:8000`, `ps`, startup log |
| Previous API | PID 78851 (started 18:00:41), stopped gracefully at ~21:39; log preserved as `logs/api-server.2026-09-27_1800-2140_pid78851.log` (read-only, SHA-256 prefix `c4d32543`) | shutdown lines in that log |
| Backup passphrase | `AYURCORE_BACKUP_PASSPHRASE` **not present** in launchd or the shell environment | presence check only; no value printed |
| Pre-restart backup | `~/AyurCore_backups/2026-09-27_restart/` — see §3 and R10 | `BACKUP_SHA256SUMS`, evidence `SHA256SUMS` |

## 2. Completed engineering (VERIFIED by tests)

| Commit(s) | What |
|---|---|
| `41a2d1f` (tag `phase-2-freeze`) | Phase 2: structured `prescription_items` and dosha, immutable, authoritative (migrations 015-016) |
| `2d2920a`, `d050b1d`, `c0ecd2f` | Phase 3A guardrail engine, **evaluation-only** (engine `3A.2`). Nothing in the request path calls it. |
| `26f61d7` | One shared herb merge (`merge_herb_lines`) for confirm, inventory and audit; audit output byte-identical |
| `12f2e6a` | Empty prescriptions rejected (empty AI proposal at preview, empty override, empty stored proposal); guardrail exception-chain privacy fix; contract §F12 |

- **Complete suite:** **473 passed, 0 failed, 0 skipped** on the `12f2e6a` tree, on an isolated checkout with a fresh migrated and seeded database. The procedure is in `CLAUDE.md`.
- **The live database was not used by any test run since that procedure was adopted.** The one earlier exception is recorded as R9.

## 3. Verified operational checks (2026-09-27)

- **Restart:** graceful stop of PID 78851, then a start from `12f2e6a` as PID 87712.
  - **Migrations:** no `migrations_applied_on_startup` line in the startup log (it is emitted only when a migration runs).
  - **`GET /health`:** 200 `{"status":"ok"}`.
  - **`GET /openapi.json`:** 200, 13 paths, with `ConfirmRequest.prescribed_herbs` `minItems: 1` (present only in the new code).
  - **Nothing else was exercised after the restart:** no login, no writes, no prescriptions, no inventory change.
- **Pre-restart backup (plaintext):** `ayurcore_pre-restart.db`, SHA-256 `f995ef2ea5612b4fe24f007f195e10d7ba2b9b13d5414db4f152c1457d58c7e6`. Verified on a scratch copy:
  - `verify_backup` ok, `integrity_check` ok, 0 foreign-key violations;
  - migrations 1-16 with matching checksums, none pending;
  - audit chain valid across 3,712 events.
- **Evidence preserved:** `~/AyurCore_backups/2026-09-27_restart/evidence/` holds the logs, the old process details and the Git HEAD. All files match their `SHA256SUMS`.
- **Earlier restart (18:00, PID 78851):** authentication, RBAC, clinic isolation and read-only endpoint checks passed. That was code `d00b17b`, **not** re-run on `12f2e6a` (see §4).

## 4. Unverified functionality

- **Encrypted backups (R10):**
  - `ayurcore_pre-restart.v3.db.enc` (SHA-256 `89d05ca9...aa2f2c`) is **ATTESTED** by the owner to decrypt to `f995ef2e...`. The independent test with the launchd passphrase then set gave `12aca280...`: a MISMATCH.
  - The original `.enc` and `v2` also did not decrypt with that passphrase.
- **On the running `12f2e6a` server:**
  - login, RBAC and read-only endpoints were not exercised after the restart (by instruction);
  - no confirm or preview was run against the live database.
- **Live Gemini path (R8):** never exercised.
- **Frontend and Streamlit:** their code is unchanged since `phase-2-freeze`. Their last recorded checks (TypeScript, build, Playwright; 2026-09-26/27) were **not re-run against the current backend**, and the new 422 responses were checked only by reading the clients' error-mapping code.
- **Live database state since the backup (21:06):** not inspected, by instruction.

## 5. Outstanding before a pilot or production (see `docs/TECHNICAL_RISK_REGISTER.md`)

| Risk | Blocks pilot | Why |
|---|:-:|---|
| R3 clinical validation | **Yes** | No clinician-reviewed ground truth; only 12 synthetic cases exist. **No clinical accuracy claim is supported.** |
| R4 deploy procedure | **Yes** | The running server is current, but no restart-on-deploy procedure exists; a confirm has not been verified on a pilot database |
| R5 protected remote | **Yes** | History exists on one machine only |
| R7 scheduled / off-machine backups and anchors | **Yes** | Backups are manual; no audit anchors taken |
| R8 live Gemini | **Yes** (AI path) | Never exercised |
| R9 accidental test writes | **Yes** if this database becomes the pilot database | Not reversed; owner decision needed |
| R10 encrypted backup verification | **Yes** | v3 attested only; plaintext copy on disk |
| R2 file-level tampering | Accept with controls | Triggers can be dropped by anyone with file access |
| R6 test isolation | Production | Tests write to `db/ayurcore.db` unless run by the isolated procedure |

**Phase 3B** (guardrail enforcement) is **not started**. It needs the approvals and decisions in `docs/PHASE_3_PLAN.md` §4, §5 and §2.5.

## 6. Recommended next actions (in order)

1. **Close R10:**
   - decrypt v3 (or make and verify a fresh encrypted backup) and confirm `f995ef2e...`;
   - then decide on the plaintext copy and the unverified `.enc`/`v2` files;
   - move the verified encrypted copy off the machine.
2. **Close R5:** create a private remote, push `main` and the `phase-2-freeze` tag, and protect `main`.
3. **Decide R9:** remediate the development database, or start any pilot on a fresh database.
4. **Re-run the frontend checks** (TypeScript, build, Playwright) against the current backend, and a read-only authenticated smoke test on the running server when writes (session rows) are acceptable.
5. **Write the restart-on-deploy procedure (R4).** The sequence used here is in R4 of the risk register.
6. **Progress R3:** a named clinician, 30-50 de-identified clinician-reviewed cases, then the live evaluation (R8).

## 7. Operational reference

- **Start (project root):**
  ```
  nohup ./venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 >> logs/api-server.log 2>&1 < /dev/null &
  ```
- **Code-only rollback:** stop the current PID, `git checkout d00b17b`, then start the same way. Return with `git checkout main`. **Never restore the database without a separate, explicit decision.**
- **Tests:** only by the isolated procedure in `CLAUDE.md`. `db/seed.py` must never run against the real database.
