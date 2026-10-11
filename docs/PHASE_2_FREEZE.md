# AyurCore OS — Phase 2 Release Freeze

**Verification date:** 2026-09-27, 17:30–17:40 IST (UTC+05:30).
**Scope of this document:** Phase 2A (structured prescription items) and Phase 2B (structured data as the authoritative source).
**Nature:** documentation only. No application code, migration, test, database, frontend, Streamlit or A0 file was changed to produce it.

Labels used below:
- **VERIFIED** — checked on 2026-09-27 against this checkout, by the command or inspection named.
- **ASSUMED** — believed true but not re-checked for this freeze.
- **PROPOSED** — future work, not implemented.

---

## 1. Version-control status

> **Post-freeze update (2026-09-27, 17:50 IST): resolved.** A Git repository was initialised at the project root after this freeze was written. The frozen tree is commit **`41a2d1f864e5704e8a695748edef09c0d8ee8c2f`** ("Freeze Phase 2 structured prescription baseline", branch `main`). Before committing, the SHA-256 fingerprint below was recomputed from the staged files and matched exactly, so that commit *is* the verified freeze. Later commits only add documents and edit docs and `CLAUDE.md`; none changes code, migrations or tests. The repository has no remote and no tag yet (see `docs/TECHNICAL_RISK_REGISTER.md`, R5). The rest of this section records the state **at verification time** and is kept unchanged as evidence.

**At verification time there was no Git repository.** `git rev-parse --is-inside-work-tree` in `<clinic checkout>` returns `fatal: not a git repository (or any of the parent directories): .git`. There is also no `.git` under `frontend/` or any other top-level directory. A `.gitignore` exists (it lists `.env` and `.env.*`), but no repository has been initialised.

Consequences, stated plainly:
- **There is no commit hash to record**, and no committed/uncommitted distinction exists. Every file in the working tree is, in effect, uncommitted.
- Nothing was committed or discarded for this freeze.

**Substitute fingerprint (VERIFIED).** SHA-256 over the sorted list of `sha256  path` lines for every `*.py` and `*.sql` file under `backend/`, `db/`, `migrations/`, `tests/`, `scripts/`, `agents/` and `ui/` (93 files, `__pycache__` excluded):

```
ced5223cc842a5573dc4cd9b4d454bd0d26aeac56593cbc52d98bf318080b377
```

Per-file SHA-256 prefixes (first 16 hex characters) of the Phase 2 files:

| File | SHA-256 (prefix) |
|---|---|
| `migrations/015_prescription_items.py` | `d38718eaa420926d` |
| `migrations/016_prescription_structured_authority.py` | `d72cfc2ada437dc0` |
| `backend/main.py` | `c96d18c731ce3c57` |
| `backend/inventory_service.py` | `8d5ada695a434e14` |
| `backend/patient_store.py` | `be0aa6ed09cb79e0` |
| `backend/audit_store.py` | `cf21abd998c60647` |
| `db/migrations.py` | `08143e039ba67854` |
| `scripts/backup_db.py` | `e75284e014f4b5a8` |
| `tests/test_prescription_items.py` | `25a5c637a15c350c` |
| `tests/test_structured_authority.py` | `5621df87d52b01bd` |

**Recommendation (at verification time):** initialise a repository and commit this exact tree before any Phase 3 work, so that this fingerprint can be replaced by a commit hash. **Done post-freeze:** commit `41a2d1f`, see the update at the top of this section.

---

## 2. Scope

### Phase 2A — structured prescription items
- New table `prescription_items`: one row per canonical herb of a confirmed prescription (`prescription_id`, `clinic_id`, `line_no`, `herb_name`, `grams`).
- Rows are written by `backend/inventory_service.deduct_and_prescribe` in the same transaction as the stock deduction, the prescription row and (on confirm) the audit event.
- Herb names come from the existing resolver, `backend/herb_resolver.normalize_herb`. Duplicate lines (including different aliases of one herb) are merged into one item in first-appearance order, as the deduction already did.
- `prescriptions.description` is still written, unchanged, for backward compatibility.
- `/prescribe/confirm` gained one check (`backend/main.py:1101-1116`): a herb whose merged total exceeds 10,000 g returns 422 with nothing written. Before this, that case failed with a rolled-back generic 500.

### Phase 2B — structured data as the authoritative source
- New column `prescriptions.dosha_imbalance`, written at confirm.
- Patient history (`backend/patient_store.get_prescription_history`, `:160`) reads dosha from the column and herbs from `prescription_items`, and parses `description` only for a field with no structured value.
- Database rules closing the gaps left by 2A (content immutability, no delete, gram limit, items only on the newest prescription).

### Explicitly out of scope
- Frontend and Streamlit: neither reads `prescription_items` or the dosha column.
- A0, auth, RBAC, audit logic.
- Backfilling legacy prescriptions (see §6 and `docs/TECHNICAL_RISK_REGISTER.md`, R1).

---

## 3. Migrations 015 and 016

| Version | File | Adds | Applied to the development database (UTC) |
|---|---|---|---|
| 015 | `migrations/015_prescription_items.py` | Table `prescription_items`; index `idx_prescription_items_clinic_herb`; triggers `prescription_items_insert_check`, `prescription_items_sealed`, `prescription_items_no_update`, `prescription_items_no_delete` | 2026-09-27 07:21:27 |
| 016 | `migrations/016_prescription_structured_authority.py` | Column `prescriptions.dosha_imbalance`; triggers `prescriptions_content_immutable`, `prescriptions_no_delete`, `prescription_items_grams_limit`, `prescription_items_newest_only` | 2026-09-27 07:33:42 |

**VERIFIED:** the SHA-256 checksum recorded in `schema_migrations` matches the file on disk for **all 16 migrations** (computed with `db.migrations._checksum`). Neither 015 nor 016 has been edited since it was applied, and **neither may be edited now**. Any change must be a new migration (017+).

Neither migration backfills anything. Rows created before 015 have no items, and rows created before 016 have a NULL `dosha_imbalance`.

---

## 4. Test results

**VERIFIED — complete suite, run for this freeze:**

| Item | Value |
|---|---|
| Command | `…/venv/bin/python -m pytest tests/ -q -p no:cacheprovider -rfEs` |
| Result | **446 passed, 0 failed, 0 errors, 0 skipped**, 9 deprecation warnings |
| Duration | 126.98 s |
| When | 2026-09-27 17:34:21 – 17:36:31 IST |
| Interpreter | Python 3.14.3 (`venv/`), pytest 9.1.1 |
| Test functions | 446 `def test_` definitions across the `tests/test_*.py` files (`tests/` holds 42 `.py` files) |

**Where it ran, and why.** The suite writes to `db/ayurcore.db` (there is no test isolation; `db/database.py:4` hard-codes `DB_PATH`), and this task forbids modifying the database. So it ran in an **isolated copy**:
- the checkout (`venv/`, `frontend/`, `.env*`, `*.db`, `__pycache__` excluded) was copied to the session scratchpad;
- the development database was snapshotted into it with `VACUUM INTO` from a read-only connection;
- `diff -rq` confirmed `backend/`, `migrations/` and `tests/` identical to the real checkout;
- `backend.main.__file__` and `db.database.DB_PATH` were confirmed to resolve inside the copy.

**VERIFIED — the real database was not modified:** SHA-256 prefix `1d10eaa1942dd656` and mtime `13:06:02` were identical before and after the run. Only its `-shm` shared-memory index was touched, by the read-only snapshot.

**Test configuration (VERIFIED):** there is no `pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini` or `tests/conftest.py`, so pytest runs with defaults. Tests use the project's own `check()` helper, and each file is also runnable directly with `python tests/<file>.py`.

**Not re-run for this freeze:** the frontend (`tsc`, build, Playwright), which is outside Phase 2 scope. Its last recorded result is in `docs/AYURCORE_FEATURE_DOSSIER.md` §11.3.

Phase 2 tests:
- `tests/test_prescription_items.py` (15)
- `tests/test_structured_authority.py` (8)

---

## 5. Verified invariants

Each row names its evidence. "Suite" means the 446-test run in §4. "DB check" means a read-only query against `db/ayurcore.db` at 17:3x IST on 2026-09-27.

| # | Invariant | Evidence |
|---|---|---|
| I1 | Confirm makes zero AI calls; AI only proposes at preview | Suite (existing A0/copilot/PII tests); unchanged by Phase 2 |
| I2 | A confirm writes stock deduction, prescription, items, dosha column, audit event, preview flip and idempotency completion in **one** `BEGIN IMMEDIATE` transaction, or none of them | `test_audit_failure_rolls_back…`, `test_item_insert_failure_rolls_back…` (nothing written; preview still confirmable) |
| I3 | Items equal the confirm response's `prescribed_herbs` (canonical, merged, in order) and agree with the audit event's confirmed herbs | `test_confirm_writes_items_matching…`, `test_history_agrees_with_the_audit_trail…` |
| I4 | Unknown herb → 400; known-but-unstocked → 409; out-of-range grams → 422; merged total > 10,000 g → 422; nothing written in any case | `test_unknown_herb…`, `test_quantity_limits…`, `test_duplicate_herbs…` |
| I5 | Parallel confirms of one preview write exactly one item set; parallel confirms of different previews write disjoint, correct sets | `test_parallel_confirms_…` (×2) |
| I6 | Idempotency replay returns the byte-identical original body and writes nothing; same key with a different body → 409 | `test_replay_writes_no_second_item_set`, `test_confirm_response_and_idempotency_behavior_unchanged` |
| I7 | Structured values take precedence over `description`, field by field; text is parsed only as a fallback | `test_structured_data_takes_precedence…`; `test_only_the_compatibility_fallback_reads_the_description` (static guard: `backend/patient_store.py` is the only backend `SELECT … description FROM prescriptions`) |
| I8 | Legacy prescriptions display exactly as before; unparseable text is shown as unknown (`null`), never guessed | `test_legacy_prescriptions_still_display_as_before` |
| I9 | Confirmed prescriptions and items cannot be updated or deleted, and items cannot be added to anything but the prescription being written — with foreign keys ON and OFF | `test_confirmed_prescription_content_cannot_be_altered…`, `test_an_api_confirmed_prescription_cannot_be_altered`, `test_migration_015_database_rules…`, `test_items_are_sealed…` |
| I10 | Migrations 015 and 016 change no existing row | `test_migration_015_changes_no_existing_row…`, `test_migration_016_changes_no_existing_row…` |
| I11 | Confirm response shape unchanged (exactly `patient_name`, `dosha_imbalance`, `prescribed_herbs`, `prescription_id`, `created_at`) | `test_confirm_response_and_idempotency_behavior_unchanged` |
| I12 | Development database is internally consistent | DB check: `integrity_check` ok; 0 foreign-key violations; `journal_mode=wal`; migrations 1–16; 27 triggers |
| I13 | Audit hash chain intact | DB check: `verify_audit_integrity()` valid, 3,707 events, 0 violations |
| I14 | Every audited confirm since 016 carries items and a dosha value | DB check: 0 of 149 (excluding `audit_store_*` clinics, whose audit rows are written directly by `test_audit_store` fixtures, not by confirm) |
| I15 | Migration checksums match files on disk | §3 |

Development database row counts at freeze (DB check):

| Table | Rows |
|---|---|
| `prescriptions` | 3,906 |
| `prescription_items` | 411 |
| `audit_events` | 3,707 |
| `encounters` | 323 |
| `patients` | 829 |
| `prescription_previews` | 5,435 |

Most rows were created by the test suite (see R6 in the risk register).

---

## 6. Known limitations

1. **Legacy prescriptions are text-only** (R1).
   - 3,594 of 3,906 prescriptions have no items, and 3,741 have a NULL dosha column. These counts include test-created rows.
   - No backfill was done, by design.
2. **SQLite file-level tampering:** anyone with write access to the database file can drop triggers (R2).
3. **Clinical validation not performed.** No clinician-reviewed case set exists; Phase 2 makes no clinical claim (R3).
4. **Frontend and Streamlit don't consume structured data.** The history API returns structured values through the unchanged response shape, so the UI benefits without changes, but nothing displays item line numbers or distinguishes legacy records.
5. **`prescriptions.status` stays writable.** It's always `"Active"`; there is no void or amend lifecycle.
6. **The database gram limit is hard-coded** in migration 016 (`> 10000`), while the API reads its bound from `backend/schemas.PrescribedHerb`. If the schema bound changes, a new migration is needed to keep them aligned.
7. **Error-message precedence.** For a text `grams` value, 016's gram-limit trigger reports before 015's type CHECK. The row is rejected either way; only the message differs.
8. **Only one prescription can be "newest" at a time.** The newest-only rule depends on `deduct_and_prescribe` inserting the prescription and its items back to back inside `BEGIN IMMEDIATE`. Any future writer must do the same.

---

## 7. Discrepancies found during this freeze

| # | Finding | Evidence | Action taken |
|---|---|---|---|
| X1 | **No Git repository** (§1) | `git rev-parse` fails | Recorded; fingerprint substituted. **Resolved post-freeze:** baseline commit `41a2d1f`, verified against the fingerprint |
| X2 | **The running API server is stale.** `uvicorn backend.main:app --port 8000` (PID 70407) started at **10:33:07 IST**, with no `--reload`. Code and migration files changed after it started, between 11:35 and 13:01 IST: migrations 010–016 and the Encounter, security-hardening and Phase 2 backend changes. It is presumably still executing pre-change code. In particular, if it served a confirm it would write a prescription without items or dosha, which the newest-only rule would then make permanent. | `ps -o lstart`; file mtimes | **Not restarted** (outside a documentation-only task). No evidence it has served writes since the migrations: the latest writes in the database are the test runs, and there are 0 item-less audited confirms since 016 (I14). **Still open post-freeze:** PID 70407 was still running at 17:5x IST. **Restart it before any use.** **Resolved 2026-09-27 18:00 IST:** PID 70407 was stopped gracefully (SIGTERM, no open connections) and the server restarted from the current checkout with the same interpreter (`venv/`), directory and command, as PID 78851 (log: `logs/api-server.log`). Startup was clean with no pending migrations. Health, authentication, RBAC, clinic isolation and read-only patient, encounter, history and audit calls were verified; no test prescription was written to the real database. Remaining items (a restart-on-deploy procedure; a confirm verified on a pilot database) are tracked as R4 in `docs/TECHNICAL_RISK_REGISTER.md`. **Second restart 2026-09-27 21:39 IST:** PID 78851 itself predated later commits (by file times it ran code as of `d00b17b`). It was stopped gracefully after a verified pre-restart backup (R10), and its log is preserved as `logs/api-server.2026-09-27_1800-2140_pid78851.log`. Commit `12f2e6a` started as **PID 87712**, with no migration applied (none logged). `GET /health` 200 and `GET /openapi.json` 200, with `ConfirmRequest.prescribed_herbs` `minItems: 1` proving the new code. No login or application writes. Details: R4 in the risk register. |
| X3 | **`CLAUDE.md` baseline is stale.** It says "pytest: 332/332 passing"; the current suite is 446 | `CLAUDE.md:71` | Not edited (not in this task's file list); flagged. **Resolved post-freeze:** `CLAUDE.md` "Current baseline" now records 446/446 and the freeze commit |
| X4 | **Stray empty `ayurcore.db` at the repository root** (0 bytes, dated 2026-09-15), separate from the real `db/ayurcore.db` | `ls -la` | Not touched; flagged. Post-freeze: kept locally, excluded from Git by the `*.db` rule in `.gitignore` |
| X5 | **Tests write to the development database** (no isolation) | `db/database.py:4` | Worked around for this run (§4); R6 |

---

## 8. Files belonging to Phase 2

- **Created:**
  - `migrations/015_prescription_items.py`
  - `migrations/016_prescription_structured_authority.py`
  - `tests/test_prescription_items.py`
  - `tests/test_structured_authority.py`
- **Modified:**
  - `backend/inventory_service.py` (items and dosha written)
  - `backend/patient_store.py` (history reads structured values first)
  - `backend/main.py` (merged-total 422 only)
  - `scripts/backup_db.py` (`prescription_items` added to `KNOWN_TABLES` and `TABLE_INTRODUCED_IN`)
  - `docs/archive/api-contract/AyurCore_API_Contract_v1.6_addendum.md` (§F11, §F11.1)
  - `docs/AYURCORE_FEATURE_DOSSIER.md`
- **Created by this freeze (documentation only):**
  - `docs/PHASE_2_FREEZE.md`
  - `docs/ARCHITECTURE_BASELINE.md`
  - `docs/TECHNICAL_RISK_REGISTER.md`
  - `docs/PHASE_3_PLAN.md`

---

## 9. Freeze statement

As of the verification date:
- Phase 2A and 2B are implemented;
- the complete backend suite passes (446/446) on an isolated copy of this exact checkout;
- migrations 1–16 are applied to the development database with matching checksums;
- the audit chain verifies.

At verification time, the freeze was **engineering-complete, not clinically validated**, and **not deployable as-is** until X1 (version control) and X2 (stale server) were resolved. Both were resolved later the same day:
- X1 by commit `41a2d1f`, tagged `phase-2-freeze`;
- X2 by the 18:00 IST restart.

It remains **not clinically validated** (R3), and a pilot is still gated by the open items in `docs/TECHNICAL_RISK_REGISTER.md`.

**Post-freeze status (2026-09-27, final handoff):**
- The running API serves commit `12f2e6a` (PID 87712, restarted 21:39 IST; `/health` and `/openapi.json` verified).
- The complete suite passes (473/473) on an isolated, freshly migrated and seeded database.
- See `docs/HANDOFF.md`.
