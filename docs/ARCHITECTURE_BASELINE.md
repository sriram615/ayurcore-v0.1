# AyurCore OS — Architecture Baseline (Phase 2 freeze)

**Baseline date:** 2026-09-27 (see `docs/PHASE_2_FREEZE.md` for the checkout fingerprint and test evidence).

**Purpose:** a map of how the backend works today, pointing at source and contracts. It doesn't restate them.
- Line numbers refer to the frozen checkout.
- Where this document and a contract disagree, **the contract and the code win**; report the difference.
- Statements are **VERIFIED by inspection of the named source** unless marked otherwise.

**Authoritative contract:** `AyurCore_API_Contract.md` (v1 and addenda v1.1–v1.6, merged 2026-09-28; the section tags below, such as §F4, are kept there). The original files are in `docs/archive/api-contract/`.
  - v1.6 covers encounters (§F4–F8), cross-cutting errors (§F10) and structured prescription items (§F11, §F11.1).

**Wider reference:** `docs/AYURCORE_FEATURE_DOSSIER.md`. **Operations:** `docs/BACKUP_RESTORE_RUNBOOK.md`.

---

## 1. Components and the trust boundary

| Component | Where | Role |
|---|---|---|
| API | `backend/main.py` (FastAPI, synchronous handlers) | All reads and writes |
| Database | `db/ayurcore.db` (SQLite, WAL), opened by `db/database.get_connection` (15 s busy timeout, `row_factory=Row`) | Single store |
| AI parser | `agents/copilot.py` via `backend/main.parse_clinical_notes` (Gemini) | Proposes dosha and herbs at preview only |
| A0 | `agents/a0/` | Evaluation arm; **not wired into production** |
| UIs | `frontend/` (Next.js), `ui/` (Streamlit, legacy) | Clients of the API; out of scope here |

**The trust boundary is the whole design:** *AI proposes → doctor reviews and corrects → deterministic backend validates and commits.*

| Endpoint | May call AI | Writes |
|---|---|---|
| `POST /prescribe/preview` (`main.py:554`) | Yes, on PII-tokenized notes (`backend/pii_sanitizer.py`) | A persistent **preview** only |
| `POST /prescribe/manual-preview` (`main.py:759`) | **No** | A persistent preview only |
| `POST /prescribe/confirm` (`main.py:859`) | **No — zero AI calls** | The only clinical write and inventory deduction |

Clinical writes must never move into AI code (`CLAUDE.md`).

---

## 2. Confirmation flow

`prescribe_confirm` runs every step below on **one connection inside one `BEGIN IMMEDIATE` transaction** (`main.py:931` to the commit at `:1250`). Any exception rolls back everything.

| # | Step | Location | Failure → response (nothing written) |
|---|---|---|---|
| 1 | Idempotency reservation or replay (if `Idempotency-Key` sent) | `:949`, `backend/idempotency_store.py:158` | Conflict → 409; replay → the stored original response |
| 2 | Load preview: **clinic check first**, then status | `:1000`, `backend/preview_store.py:215` | Foreign or missing or expired → 404; already confirmed → 409 |
| 3 | Session binding (preview's creating session = this session) | `:1039` | 403 |
| 4 | Encounter re-check (must still be OPEN, same patient) | `:1050` | 409 |
| 5 | Final dosha and herbs = doctor's correction, else the stored proposal; dosha must not contain the patient's identity | `:1071` | 422 |
| 6 | `normalize_herb` on every herb | `:1092`, `backend/herb_resolver.py:187` | 400 |
| 7 | Merged per-herb total ≤ 10,000 g (Phase 2A) | `:1101-1116` | 422 |
| 8 | `deduct_and_prescribe`: stock, prescription, dosha column, items (§3–4) | `:1118`, `backend/inventory_service.py:37` | Insufficient stock → 409 |
| 9 | `PRESCRIPTION_CONFIRMED` audit event | `:1177`, `backend/audit_store.py:447` | 500 |
| 10 | Preview → CONFIRMED | `:1200`, `preview_store.py:300` | 500 |
| 11 | Build response; complete idempotency record with its exact bytes | `:1218`, `idempotency_store.py:258` | 500 |
| 12 | `COMMIT` | `:1250` | Lock → 503 `DATABASE_BUSY` (§F10) |
| — | **After** commit: corrections logging, best-effort; can never roll back a prescription | `:1290`, `backend/corrections_service.py` | Logged only |

Evidence that the steps are atomic:
- `tests/test_prescription_items.py` (rollback tests)
- `tests/test_encounter_linkage.py`
- `tests/test_idempotency*.py`
- `tests/test_golden.py`

---

## 3. Inventory transaction

`backend/inventory_service.deduct_and_prescribe` (`:37`):
- merges duplicate canonical herbs (sum of grams, first-appearance order);
- runs one conditional `UPDATE inventory SET stock_grams = stock_grams - ? WHERE herb_name=? AND clinic_id=? AND stock_grams >= ?` per herb, where zero rows updated raises `InsufficientStockError`;
- is backed by the database `CHECK (stock_grams >= 0)` (`migrations/001_baseline_schema.sql`).

Transaction ownership:
- **Called with a `conn`** (as confirm does): it never commits, rolls back or closes that connection. The caller owns the transaction.
- **Called without one:** it opens its own `BEGIN IMMEDIATE`, and commits or rolls back itself.

There is **no inventory API**. Stock comes from seeding only (dossier §3.4).

---

## 4. Structured prescription storage (migrations 015–016)

| Store | Content | Authority |
|---|---|---|
| `prescription_items` (015) | `line_no` (1..n, no gaps), canonical `herb_name` (must be in the clinic's inventory), `grams` (positive integer, ≤ 10,000) — one row per herb | **Authoritative** for herbs, for prescriptions confirmed since 015 |
| `prescriptions.dosha_imbalance` (016) | Confirmed dosha | **Authoritative** for dosha, for prescriptions confirmed since 016 |
| `prescriptions.description` | `Patient: {name} \| Dosha: {dosha} \| Formulation: {json}` | **Compatibility only.** Still written; read only as a fallback |

**Reader:** `backend/patient_store.get_prescription_history` (`:160`) is the only backend reader of prescription content. It uses structured values first, field by field, and calls `_parse_prescription_description` (`:129`) only for a field without one. Unparseable text is returned as `null`, never guessed. `tests/test_structured_authority.py` guards that no other backend reader of `description` appears.

**Database rules** (triggers; fire regardless of `PRAGMA foreign_keys`):

| Rule | Trigger |
|---|---|
| Item: prescription exists in the same clinic, next line number, stocked herb | `prescription_items_insert_check` (015) |
| No item added once the prescription's audit event exists | `prescription_items_sealed` (015) |
| Items can't be updated or deleted | `prescription_items_no_update`, `_no_delete` (015) |
| Items only on the most recently inserted prescription | `prescription_items_newest_only` (016) |
| Item grams ≤ 10,000 | `prescription_items_grams_limit` (016) |
| Prescription content (`id`, `patient_name`, `description`, `created_at`, `dosha_imbalance`, and `clinic_id` of an unlinked row) can't change | `prescriptions_content_immutable` (016) |
| Prescriptions can't be deleted | `prescriptions_no_delete` (016) |
| Patient and encounter links: same clinic, immutable | Migrations 009 and 012 triggers |

`prescriptions.status` is intentionally still writable (always `"Active"`; no lifecycle exists).

---

## 5. Idempotency

`backend/idempotency_store.py`; table from `migrations/006_idempotency_keys.sql`, primary key `(user_id, operation, idempotency_key)`.
- **Optional header.** `Idempotency-Key` on `/prescribe/confirm` only, at most 200 characters (`main.py:878`). Without it, there is no dedup guarantee beyond single-use previews (a second confirm of a confirmed preview gets 409).
- **Fingerprint** (`compute_request_fingerprint`, `:118`): `preview_id`, `dosha_imbalance` and `prescribed_herbs` **as sent**. Same key with a different fingerprint → 409.
- **Atomic with the write.** The reservation (step 1) and completion (step 11) happen in the confirm transaction, so the stored response is exactly the committed one. A replay returns it byte-for-byte and runs no business logic.
- **Scoped server-side.** `user_id` and `clinic_id` come from the session, never the client, so cross-user and cross-clinic replay is impossible.
- **No retention or cleanup pass exists** (noted in the migration).

Contract: `docs/archive/api-contract/AyurCore_API_Contract_v1.4_addendum.md`.

---

## 6. Audit chain

`backend/audit_store.py`; table from `migrations/007_audit_events.sql`, rebuilt by 011; append-only since 014.
- **Event types:** `PRESCRIPTION_CONFIRMED`, `ENCOUNTER_OPENED`, `ENCOUNTER_CLOSED`.
  - Confirm events carry `decision_source`, `ai_proposal`, `doctor_decision` and `was_changed`, with **no patient identity**.
  - Encounter events carry only a random `encounter_ref`.
  - One confirm event per prescription (unique index on `prescription_id`).
- **Hash:** `SHA-256(canonical envelope + previous_hash)`, one chain per clinic (`_canonical_envelope` `:412`, `_compute_event_hash` `:442`). It is written in the same transaction as the act it records.
- **Verification:** `verify_audit_integrity` (`:580`) recomputes every hash and link. `audit_chain_validity` (`:740`) feeds the migration guard (§8).
- **Append-only:** migration 014 triggers reject `UPDATE`/`DELETE`.
- **External anchor:** `scripts/audit_anchor.py` writes HMAC-signed checkpoints to a file outside the database, and `verify` detects truncation, rewrite and forgery. **No schedule is installed** (R2).
- **Read access:** `GET /audit/events` and `GET /audit/export` (ADMIN, AUDITOR), `main.py:1308`, `:1383`.

---

## 7. Encounters

`backend/encounter_store.py`; `migrations/010_encounters.py`, 012, 013. Contract: v1.6 §F4–F8.
- **Model:** one OPEN encounter per patient (partial unique index). CLOSED is final.
- **`encounter_ref`:** a random 16-hex value, required and immutable (013). It's the only encounter identifier in audit data.
- **Lifecycle:** open and close each write an audit event in the same transaction.
- **Linkage:** previews and prescriptions may carry `encounter_pk`. At insert it must name an OPEN encounter of the same clinic and patient, and it can never change (012). Confirm inherits it **only from the stored preview**, and re-checks it inside the transaction (§2 step 4).
- **Access:** open and close are DOCTOR only; read is DOCTOR and ADMIN; AUDITOR has none.

---

## 8. Authentication, RBAC and clinic isolation

- **Passwords:** Argon2id (argon2-cffi; `backend/auth_service.py:114-186`).
- **Login throttle:** 5 failures → 45 s cooldown (`:74-75`).
- **Sessions:** opaque bearer tokens (`secrets.token_urlsafe(32)`); only the SHA-256 is stored; 12 h TTL; revocable (`:412`, `:441`, `:519`).
- **RBAC:** `backend/rbac.py`.
  - `PERMISSION_MATRIX` (`:117`):

    | Permission | Roles |
    |---|---|
    | `prescribe` | DOCTOR |
    | `audit_export` | ADMIN, AUDITOR |
    | `patients` | DOCTOR, ADMIN |
    | `encounters_write` | DOCTOR |
    | `encounters_read` | DOCTOR, ADMIN |

  - Enforced per route with `Depends(require_role(...))`.
- **Clinic isolation:**
  - `clinic_id` always comes from the resolved session (`auth.clinic_id`), never from the request body.
  - A foreign-clinic resource returns the same 404 as a nonexistent one.
  - The database backs this up with triggers (009, 010, 012, 015).
- **Contracts:** v1.1 (persistent previews), v1.2 (authentication), v1.3 (RBAC and clinic isolation), v1.5 (patients).

---

## 9. Migration system

`db/migrations.py`; files in `migrations/NNN_name.(sql|py)` (`.py` exposes `upgrade(conn)`; triggers need `.py`, because the `.sql` splitter splits on `;`).
- **Applied at startup** (`main.py:237-245`, `run_migrations(integrity_guard=audit_chain_validity)`), in version order, inside one `BEGIN IMMEDIATE` with a SAVEPOINT per migration.
- **Checksums:** the SHA-256 of each applied file is recorded in `schema_migrations`. **An applied migration must never be edited**; a mismatch raises `MigrationChecksumMismatchError`. Change requires a new migration.
- **Integrity guard:** every clinic's audit chain is verified before and after a batch. A migration that would break a valid chain raises `MigrationIntegrityError` and rolls back the **whole batch**.
- **State at freeze:** 1–16 applied to the development database; every checksum matches (VERIFIED, `docs/PHASE_2_FREEZE.md` §3).

---

## 10. Backup and restore boundaries

`scripts/backup_db.py`; procedure in `docs/BACKUP_RESTORE_RUNBOOK.md`.
- **Backup:** `VACUUM INTO` a new file. It never overwrites the live database or an existing file, and needs no downtime.
- **`verify_backup` (`:194`) checks:**
  - `integrity_check`;
  - `foreign_key_check`;
  - every table the backup's own schema version requires (`TABLE_INTRODUCED_IN`, `prescription_items` from 15);
  - every audit chain.
- **Optional encryption:** `openssl` AES-256-CBC/PBKDF2, with the passphrase from `AYURCORE_BACKUP_PASSPHRASE`.
- **Restore:** stop the app, verify, move the live file aside (never delete it), copy the backup in, start (migrations run), verify the audit chain, run one end-to-end confirm.
- **Boundaries — what backup does not cover:**
  - no schedule is installed;
  - no off-machine replication;
  - the live database is not encrypted;
  - anchor files are separate artifacts and must be backed up separately.

An isolated round-trip drill passed on 2026-09-27 (runbook steps 1–2, 4, 6–7 against a temporary copy).

---

## 11. Known architectural constraints

- **Single-node SQLite.** Writers serialize on the database lock; lock timeouts return 503 (§F10). A two-process race test passed; nothing beyond that has been tested.
- **No test isolation.** Tests write to `db/ayurcore.db` (`db/database.py:4`).
- **Triggers can be dropped by anyone with file access** (R2).
- **Version control:** the frozen tree is Git commit `41a2d1f` on `main` (freeze §1). There is no remote or tag yet (risk R5).
