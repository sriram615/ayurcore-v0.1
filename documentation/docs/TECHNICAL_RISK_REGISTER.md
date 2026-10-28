# AyurCore OS — Technical Risk Register

**As of:** 2026-09-27 (Phase 2 freeze; see `documentation/docs/PHASE_2_FREEZE.md`). **Last updated:** 2026-09-27 at the final handoff (see `documentation/docs/HANDOFF.md`).
**Owner:** engineering. Clinical items need a named Ayurvedic clinician as co-owner.

**Blocking columns:**
- **Engineering:** blocks further development work.
- **Pilot:** blocks a supervised pilot with real patients in one clinic.
- **Production:** blocks general, unsupervised use.

"Yes" means the risk must be closed (or formally accepted by the named approver) before that stage. These are engineering recommendations; the go/no-go decision for a pilot or production is the project owner's.

**Summary**

| ID | Risk | Engineering | Pilot | Production |
|---|---|:-:|:-:|:-:|
| R1 | Legacy prescriptions are text-only (no automatic backfill) | No | No | No (accept, with R1 closure) |
| R2 | SQLite file-level tampering | No | No (accept with controls) | **Yes** |
| R3 | Clinician-led clinical validation outstanding | No | **Yes** | **Yes** |
| R4 | Stale API server — **resolved** 2026-09-27 21:39 (PID 87712 on `12f2e6a`); no restart-on-deploy procedure yet | No | **Yes** | **Yes** |
| R5 | Version control: baseline committed and tagged; no remote or branch protection yet | No | **Yes** | **Yes** |
| R6 | Tests write to the development database | No | No | **Yes** |
| R7 | Backups and anchors are not scheduled; single machine | No | **Yes** | **Yes** |
| R8 | Live Gemini path unverified | No | **Yes** (for the AI-assisted path) | **Yes** |
| R9 | Accidental test writes in the development database (not reversed) | No | **Yes** (if this database would become the pilot database) | **Yes** |
| R10 | Encrypted pre-restart backup not independently verified; plaintext copy on disk | No | **Yes** | **Yes** |

**Update (2026-09-27, post-freeze):**
- R4 is mostly closed: the stale server was restarted from the frozen code and verified. A restart-on-deploy procedure is still missing.
- R5 is mostly closed: the baseline is committed and tagged `phase-2-freeze`. It isn't on a protected remote yet.
- Phase 3A (an evaluation-only guardrail engine, not wired into any request) changes no risk in this register.

**Update (2026-09-27, final handoff):**
- **R4 resolved for the running server.** A second restart at 21:39 IST put commit `12f2e6a` into service as PID 87712, with `/health` and `/openapi.json` verified. The restart-on-deploy procedure is still open.
- **R9 (new):** records the accidental test writes into the live development database.
- **R10 (new):** records that the encrypted pre-restart backup was not independently verified.

R1–R3 are the risks this register was asked to record. R4–R8 were observed during the freeze and are recorded so that they are not lost.

---

## R1 — Legacy prescriptions are text-only (no automatic backfill)

**Description (VERIFIED).**
- Prescriptions confirmed before migration 015 have no `prescription_items`, and those before 016 have a NULL `dosha_imbalance`. Their clinical content exists only in the free-text `prescriptions.description`.
- At freeze, 3,594 of 3,906 prescriptions in the development database have no items and 3,741 have no dosha column value. Most rows are test-created (R6).
- The history reader falls back to parsing the text (`backend/patient_store._parse_prescription_description`). Text that doesn't match the exact format written by `deduct_and_prescribe` is returned as unknown (`null`).

**Impact.**
- A patient's older prescriptions may show as "unknown" in history.
- Any future feature that needs structured data (Phase 3 guardrails, analytics, dispensing) cannot rely on legacy rows.
- No wrong value is shown: the parser returns nothing rather than guessing.

**Why there is no automatic backfill.**
- Deriving items from free text would present a parse as a structured, authoritative record.
- The migration runner forbids editing applied migrations.
- Migration 016 now makes older prescriptions unable to gain items at all.

**Mitigation in place.**
- Field-by-field fallback.
- A static test (`tests/test_structured_authority.py`) guards that no other reader parses `description`.
- Legacy rows are immutable, so they can't drift.

**Closure criteria.** Either of:
1. **Formal acceptance** by the project owner that legacy rows stay text-only and are labelled as such wherever they are shown. This needs a frontend change, which is not authorized yet. Or:
2. **An approved, audited backfill design.** This would be a new migration writing to a **separate** table marked `derived_from_text`, never to `prescription_items`. It needs clinician sign-off on how parse failures are handled and a full dry run on a backup copy.

**Blocks:** engineering No; pilot No (a pilot starts with structured records); production No, provided criterion 1 or 2 is met before migrating any real historical clinic data.

---

## R2 — SQLite file-level tampering

**Description (VERIFIED).** Integrity rules live in SQLite triggers:
- audit append-only (014);
- prescription and item immutability (015, 016);
- clinic and link rules (009, 010, 012).

Anyone with write access to the database **file** can open it directly and `DROP TRIGGER`, then edit or delete rows. The audit hash chain detects an in-place edit of an audit event, but not:
- a consistent recomputation of the whole chain; or
- deletion of the most recent events.

Only an off-database anchor detects those.

**Impact.**
- Undetected alteration of clinical or audit records by a privileged local actor or malware on the host.
- The medico-legal value of the audit trail depends on detection.

**Mitigation in place.**
- Triggers stop application-level and accidental modification.
- `verify_audit_integrity` detects in-place edits.
- `verify_backup` rejects backups with broken chains.
- The migration integrity guard blocks chain-breaking migrations.
- `scripts/audit_anchor.py` writes HMAC-signed checkpoints outside the database, and `verify` detects truncation, rewrite and forgery up to the last checkpoint.

**Gaps.**
- No checkpoint schedule is installed; no anchors have been taken.
- The anchor key's custody isn't defined.
- The database file isn't encrypted at rest.
- Prescriptions and items have no hash chain of their own: only audit events are chained. Each confirm event records the confirmed dosha and herbs, so a tampered prescription would disagree with its audit event, but **no automated cross-check exists**.

**Closure criteria.**
1. Scheduled anchor checkpoints (for example hourly and at the end of each clinic session). The anchor file is stored off-host or on WORM storage, and the key is held by someone other than the database operator.
2. OS-level controls: the database file is owned by the service account only, with no interactive write access, plus disk encryption.
3. A scheduled `verify` of the anchors, with alerting.
4. **Proposed:** an automated check that every prescription's items and dosha match its audit event.
5. A documented incident procedure.

**Blocks:** engineering No. Pilot No, if the pilot owner accepts the risk in writing with criterion 2 in place. Production **Yes**.

---

## R3 — Clinician-led clinical validation outstanding

**Description (VERIFIED from repository state).**
- No clinician-reviewed ground-truth case set exists.
- The only evaluation cases are 12 synthetic cases, classified `technical_synthetic_unreviewed` and explicitly **not** clinical validation.
- The Control-vs-A0 evaluation is paused. No live baseline was produced because of API quota and funds (dossier §10).
- The live Gemini parsing path is unverified (R8).

**Impact.**
- There is no evidence of the AI proposal's clinical accuracy: dosha, herbs, quantities, hallucinations, or corrections needed.
- Any clinical claim would be unsupported.
- The safety net today is structural, not clinical: the doctor must review and confirm, and confirm is deterministic.
- The system enforces **no clinical rules**: no maximum doses, no contraindications. The only limits are formulary membership, stock and a technical 10,000 g input bound, which is **not** a clinical dose limit.

**Mitigation in place.**
- The AI-proposes, doctor-confirms boundary.
- A manual fallback with no AI.
- `was_changed` in the audit trail and the corrections dataset record every doctor override.
- The evaluation tooling and protocol are ready (`documentation/docs/CLINICAL_EVAL_GROUND_TRUTH.md`, `documentation/docs/CLINICAL_CASE_REVIEW_FORM.md`, `scripts/clinical_eval_harness.py`).

**Closure criteria.**
1. 30–50 realistic, de-identified cases with ground truth supplied **and signed** by an Ayurvedic clinician (per `documentation/CLAUDE.md`).
2. The live evaluation run with reproducibility metadata; provider failures separated from clinical misses.
3. Results reviewed by the clinician.
4. Acceptance thresholds for the pilot set **by the clinician and project owner**. Engineering does not set them.

**Blocks:** engineering No; pilot **Yes**; production **Yes**. Phase 3 guardrail rules also depend on clinician input (see `documentation/docs/PHASE_3_PLAN.md`).

---

## R4 — Running API server is stale (observed at freeze)

**Description (VERIFIED).**
- `uvicorn backend.main:app --port 8000` (PID 70407) has been running since 10:33 IST on 2026-09-27, with no `--reload`.
- Every code and migration change from 11:35 IST onward is newer than the process. That covers migrations 010–016, the encounter work, security hardening and Phase 2.
- If it served a confirm, it would write a prescription without items or a dosha value, and 016 would then make that permanent.
- No such writes were found (freeze I14).

**Impact.** Stale code would violate the Phase 2 invariants on real use.

**Mitigation (2026-09-27, 18:00 IST).**
- The stale process (PID 70407) was stopped gracefully (SIGTERM, with no open connections).
- It was restarted from the current checkout with the same interpreter (`venv/`), working directory and command, as PID 78851, with output in `logs/api-server.log`.
- The startup log was clean, with no pending migrations.
- Verified afterwards:
  - `/health` returned 200;
  - no token, a bogus token and an unknown user each got 401;
  - demo doctor and auditor logins succeeded;
  - read-only patient search, encounter list and prescription history returned 200;
  - RBAC gave 403 in both directions;
  - a doctor from another clinic got 404 (clinic isolation);
  - logout worked, and revoked tokens then got 401.

**Closure criteria.**
- ~~Restart the server from the frozen checkout.~~ Done (see above).
- ~~Confirm the startup log shows no pending migrations.~~ Done.
- **End-to-end confirm writing items and dosha: deliberately not run against the real database.** It is covered by the complete suite on an isolated copy (`tests/test_prescription_items.py`, `tests/test_structured_authority.py`). Repeat it on a pilot or staging database before a pilot.
- Adopt a deploy procedure that always restarts on code change: **open**.

**Second restart (2026-09-27, 21:39 IST) -- running server now current. VERIFIED unless stated.**
- **Why:** the server started at 18:00 (PID 78851) predated commits `26f61d7`, `c0ecd2f` and `12f2e6a`. By file times, it was running backend code as of `d00b17b`, and could still commit an empty prescription.
- **Before the restart:**
  - a pre-restart backup was made, verified on a scratch copy and preserved (R10);
  - migration state (1-16 applied, checksums match, nothing pending) was verified on the backup copy only;
  - the incident evidence was preserved.
- **Stop:** PID 78851 was stopped with `SIGTERM`, with no open connections. Its log ends `Application shutdown complete` / `Finished server process [78851]`, and port 8000 was then free.
- **Old log:** renamed, not deleted, to `logs/api-server.2026-09-27_1800-2140_pid78851.log` (read-only). A hashed copy is in `~/AyurCore_backups/2026-09-27_restart/evidence/`.
- **Start:** from the project root with `./venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000`, logging to `logs/api-server.log`, at HEAD `12f2e6a` with a clean tree. The new process is PID 87712, started 21:39:06 IST.
- **Migrations:** the startup log has no `migrations_applied_on_startup` line. That line is emitted only when a migration is applied. The live database was not queried.
- **Checks (only these, by instruction):**
  - `GET /health` -> 200 `{"status":"ok"}`;
  - `GET /openapi.json` -> 200, 13 paths, with `ConfirmRequest.prescribed_herbs` `minItems: 1` (present only in the new code).

  No login, no application-level writes, no prescriptions, no inventory change, no database restore.
- **Rollback (code only, no database restore):** stop PID 87712, `git checkout d00b17b`, and start with the same command.

**Blocks:** engineering No; pilot **Yes** until the deploy procedure exists and a confirm is verified on the pilot database; production **Yes**.

---

## R5 — Version control (observed at freeze; mostly closed)

**Description (VERIFIED at freeze).** At the freeze, the checkout was not a Git repository, so there was no commit history, no diff review and no way to roll back.

**Status update (2026-09-27, post-freeze).**
- **Done:** a repository was initialised at the project root. The frozen tree is commit `41a2d1f864e5704e8a695748edef09c0d8ee8c2f` on `main`, and was verified to reproduce the freeze fingerprint before committing. The `.gitignore` excludes secrets, databases, backups, anchors, non-synthetic clinical case files, evaluation artifacts, logs and local state.
- **Done:** annotated tag `phase-2-freeze` (tag object `5ab193d`) points at `41a2d1f`. Before tagging, the commit's own blobs were verified to reproduce the freeze fingerprint, and all 16 migration checksums to match the database record.
- **Still open:**
  - no remote, so the history exists on one machine only (the GitHub CLI isn't installed and no SSH key is configured);
  - no branch protection, which needs a remote host.

**Impact.**
- No auditable record of what code produced which clinical record.
- A release can't be reproduced except by the content fingerprint in `documentation/docs/PHASE_2_FREEZE.md` §1.

**Closure criteria.**
- ~~Initialise a repository and commit the frozen tree, confirming it reproduces the fingerprint.~~ Done (`41a2d1f`).
- ~~Tag it `phase-2-freeze`.~~ Done.
- Push to a private remote and protect the main branch.

**Blocks:** engineering No; pilot **Yes** until the history is on a protected remote; production **Yes**.

---

## R6 — Tests write to the development database (observed)

**Description (VERIFIED).**
- `db/database.py:4` hard-codes `DB_PATH`, and the test suite writes to `db/ayurcore.db`, creating thousands of synthetic rows (users, clinics, prescriptions).
- The freeze ran the suite on an isolated copy to avoid this.

**Impact.**
- The development database isn't clean or representative.
- Running tests on a machine holding real data would pollute it.

**Closure criteria.**
- The test suite runs against a temporary database per session (for example via an environment variable or fixture).
- Real clinic data is never on a machine where tests run.

**Blocks:** production **Yes**. For a pilot, it's mitigated by keeping pilot data off developer machines.

---

## R7 — Backups and anchors are not scheduled; single machine (observed)

**Description (VERIFIED).**
- The backup tool, verification and runbook exist and a restore drill passed.
- No schedule is installed, there is no off-machine copy, and anchor checkpoints are not scheduled (see R2).

**Closure criteria.**
- Scheduled, encrypted, verified backups stored off the machine.
- A periodic restore drill.
- Scheduled anchors.

**Blocks:** pilot **Yes**; production **Yes**.

---

## R8 — Live Gemini path unverified (observed)

**Description (VERIFIED).**
- All AI tests use stubs. The live call path hasn't been exercised because of quota and funds.
- The manual fallback works without AI.

**Closure criteria.** A live, rate-limited smoke run under an approved key and budget, recording provider failures separately from clinical results.

**Blocks:** pilot **Yes** for the AI-assisted path. A manual-only pilot is not blocked by R8.

---

## R9 — Accidental test writes in the development database (observed 2026-09-27)

**Description (VERIFIED on the pre-restart backup copy, not on the live file).**
- An existing test file (`tests/test_confirm_input_bounds.py`), which writes to `db/ayurcore.db`, was run once against the real checkout during Phase 3A work.
- It wrote synthetic fixtures into **`default_clinic`**:
  - 9 users (`confirm_bounds_test_*`), 9 sessions and 9 previews;
  - 5 prescriptions, with their items and audit events;
  - 65 g Ashwagandha and 15 g Brahmi deducted from that clinic's (demo) stock.
- The audit chain remained valid.
- Prescriptions and audit events are immutable by design. **Nothing was reversed**, by instruction; the records are incident evidence.

**Impact:** the development database's `default_clinic` data and stock no longer reflect only deliberate demo activity.

**Mitigation in place:**
- the incident is preserved in the pre-restart backup (R10);
- `documentation/CLAUDE.md` now documents the isolated test procedure (fresh migrated and seeded copy; never the real database).

**Closure criteria:**
- an owner decision on remediation (e.g. a documented stock correction, or starting the pilot on a fresh database);
- never use this database as a pilot or production database without that decision.

**Blocks:** pilot **Yes** if this database would be used for the pilot; production **Yes**.

---

## R10 — Encrypted pre-restart backup not independently verified (observed 2026-09-27)

**Backup location:** `~/AyurCore_backups/2026-09-27_restart/` (directory `700`; files `400`).

| File | SHA-256 | Status |
|---|---|---|
| `ayurcore_pre-restart.db` (plaintext) | `f995ef2ea5612b4fe24f007f195e10d7ba2b9b13d5414db4f152c1457d58c7e6` (recorded in `BACKUP_SHA256SUMS`) | **VERIFIED** on a scratch copy: `verify_backup` ok, `integrity_check` ok, 0 foreign-key violations, 14 tables / 27 triggers / 26 indexes, migrations 1-16 with matching checksums and none pending, audit chain valid (3,712 events). Contains the R9 evidence. |
| `ayurcore_pre-restart.db.enc` | `92da2ced851f25a5332e6826184903683301b293739ba92b2ee083ef7a0c5713` | Decrypting with the passphrase then held in launchd gave `bad decrypt` (output `5ec9568c...`, not `f995ef2e...`). Not verified. |
| `ayurcore_pre-restart.v2.db.enc` | `6e35132952ee72eeae627d3cdb5c62872561f0ddcbd0b4e226fa8c97eb406678` | Re-encryption by the owner. The launchd passphrase gave `f92e6d36...`. Not verified. |
| `ayurcore_pre-restart.v3.db.enc` | `89d05ca9258c0085bc5c17c83f63803e1ef7d5aa65f2bd7b99692b28daaa2f2c` | Re-encryption by the owner. **The owner reports** it decrypts to `f995ef2e...`. The independent test with the launchd passphrase gave `12aca280...` (MISMATCH). **Not independently verified.** |

- **What the mismatches show:** the passphrase held in launchd did not match any of the three encrypted files. They do not by themselves show that any file is corrupt.
- **Passphrase removed:** `AYURCORE_BACKUP_PASSPHRASE` has since been removed from the launchd environment (verified by presence check only; no value was printed).
- **No backup was deleted, overwritten or altered:** the recorded hashes still match, and no guessed credentials were tried.

**Impact:**
- recovery from an encrypted copy depends on a passphrase whose correctness only the owner has confirmed;
- an unencrypted plaintext copy containing patient data sits on the local disk.

**Closure criteria:**
- the owner (or a second person) decrypts v3 and confirms `f995ef2e...`, or a new encrypted backup is made and verified end to end;
- then decide whether to delete the plaintext copy and the unverified `.enc`/`v2` files (owner decision);
- move the verified encrypted copy off the machine (R7).

**Blocks:** pilot **Yes**; production **Yes**.

