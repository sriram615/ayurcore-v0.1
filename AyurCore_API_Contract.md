# AyurCore OS — API Contract

**Version:** 1.7, 2026-10-10 (additive: `POST /patients/{patient_pk}/ask` and `GET /inventory` documented; no existing behaviour changed).
**Status:** the single authoritative contract between the backend and any frontend. It merges `AyurCore_API_Contract_v1.md` and addenda v1.1–v1.6 into one current description. **Nothing was changed in the merge:** every shape, status code and guarantee is the one those documents define. The originals are kept, unchanged, in `docs/archive/api-contract/`.

- **Section tags** such as **[v1 §2]**, **[B1]**, **[F5]** name the original document and section a rule came from, so older references still resolve (§13).
- **Marked (CODE):** a few details that no earlier contract document wrote down were taken from the current backend code: the login throttle and the exact error texts. They describe existing behaviour; they add nothing new.
- **Changing this contract:** see §14.

> AyurCore is **not clinically validated and not production-ready** (`docs/TECHNICAL_RISK_REGISTER.md`).

---

## 1. Conventions

- **Auth header:** `Authorization: Bearer <token>` on every endpoint except `GET /health` and `POST /auth/login`. The token is an opaque server-side session token, **not a JWT**; store and replay it, never decode it. **[v1 §3]**
- **Clinic is always the session's clinic.** No request field can choose it. **[B5, C5, E1, F-intro]**
  - `clinic_id` in the preview and manual-preview bodies is accepted but **ignored**.
  - On `POST /patients` and the encounter-open body, `clinic_id` (like any unknown field) is **rejected with 422**.
  - Another clinic's patient, encounter or preview returns the **same 404** as a nonexistent one.
- **Error bodies:**
  - handler errors: `{"detail": "<string>"}`;
  - request-validation errors (FastAPI 422): `{"detail": [{"type", "loc", "msg", ...}]}`, where `detail` is a **list**. A 422 can come in either shape.
- **Timestamps** are UTC with **no offset suffix**. Patient, encounter and history `created_at` values are `"YYYY-MM-DD HH:MM:SS"`; `PrescriptionResponse.created_at` and `LoginResponse.expires_at` are ISO strings such as `"2026-09-28T09:00:00"`. **[F3]** (CODE)
- **Herb line** (`PrescribedHerb`), everywhere: `{"name": string ≤ 200 chars, "grams": integer > 0 and ≤ 10000}`. **[v1 §2]**
- **Database busy, any endpoint: 503.** **[F10]**
  ```
  503 {"detail": "The system is busy. Nothing was saved. Please try again."}
  X-Error-Code: DATABASE_BUSY
  Retry-After: 5
  ```
  Nothing was written, so the request is safe to retry. Other database errors are a generic `500` with no internal detail.

## 2. Roles and access **[C1, C3, E5, F7]**

Roles are `DOCTOR`, `ADMIN` and `AUDITOR`. The server re-derives the role from the session on every request. **`401` always takes priority over `403`.**

| Endpoint | DOCTOR | ADMIN | AUDITOR |
|---|:-:|:-:|:-:|
| `GET /health`, `POST /auth/login` | public | public | public |
| `POST /auth/logout` | ✓ | ✓ | ✓ |
| `POST /patients`, `GET /patients?q=`, `GET /patients/{pk}/prescriptions` | ✓ | ✓ | 403 |
| `GET /patients/{pk}/encounters`, `GET /encounters/{pk}` | ✓ | ✓ | 403 |
| `POST /patients/{pk}/encounters`, `POST /encounters/{pk}/close` | ✓ | 403 | 403 |
| `POST /prescribe/preview`, `/prescribe/manual-preview`, `/prescribe/confirm` | ✓ | 403 | 403 |
| `GET /inventory` | ✓ | ✓ | 403 |
| `POST /patients/{pk}/ask` (provisional: DOCTOR only) | ✓ | 403 | 403 |
| `GET /audit/export`, `GET /audit/events` | 403 | ✓ | ✓ |

Wrong role: `403 {"detail": "Your role is not permitted to perform this action."}`.

## 3. Authentication

### 3.1 `POST /auth/login` **[B1, C4]**

```json
{ "username": "string, 1-128 chars", "password": "string, 1-256 chars" }
```

**200:**
```json
{
  "token": "string: opaque bearer token, shown only here; only its SHA-256 hash is stored",
  "expires_at": "datetime: 12 hours after creation, absolute, not extended by use",
  "username": "string",
  "clinic_id": "string: the account's own clinic",
  "role": "DOCTOR | ADMIN | AUDITOR (informational only; grants nothing by itself)"
}
```

- **401** `{"detail": "Invalid username or password."}`, identical for an unknown user, a wrong password, a deactivated account and an active throttle. Neither the response nor its timing reveals which.
- **Throttle (CODE):** after 5 consecutive failures an account is locked for 45 seconds (`FAILURE_THRESHOLD`, `COOLDOWN_SECONDS` in `backend/auth_service.py`). *(Addendum v1.2 listed rate limiting as not yet implemented; it has since been added.)*
- **422:** missing or malformed fields.
- Sessions are persistent rows (migration 005) and survive a restart.

### 3.2 `POST /auth/logout` **[B2]**

No body. **200** `{"status": "logged_out"}`. Revocation is immediate and permanent; a second logout with the same token is `401`.

### 3.3 `401` on protected endpoints **[B3]**

`{"detail": "Missing or malformed Authorization header. Expected 'Bearer <token>'."}` or `{"detail": "Invalid, expired, or revoked session."}`. Both are deliberately generic: treat every 401 the same and log in again.

## 4. `GET /health` **[F1]**

Public. Always `200 {"status": "ok"}`. It touches no database, AI or configuration: a liveness check, not a readiness check. Other methods return 405.

## 5. Patients

### 5.1 Identity **[E1]**
- A patient is identified by **(clinic, `clinic_patient_id`)**, the clinic's OP/registration number: trimmed, 1–50 characters, no control characters, unique within a clinic, and never changeable.
- `display_name` and `phone` are attributes, not identity.
- `patient_pk` is the registry's internal integer key, meaningful only inside its own clinic.
- Prescriptions made before the registry are **not** linked, matched or backfilled.

### 5.2 `POST /patients` (DOCTOR, ADMIN) **[E2]**

```json
{
  "clinic_patient_id": "string, required, trimmed, 1-50 chars",
  "display_name": "string, required, trimmed, 1-200 chars",
  "phone": "string, optional, trimmed, 1-32 chars"
}
```

**201:**
```json
{ "patient_pk": 123, "clinic_patient_id": "OP-100", "display_name": "Asha Menon", "phone": "98470", "created_at": "2026-09-26 15:04:05" }
```

| Status | When |
|---|---|
| 401 / 403 | no valid session / AUDITOR |
| 409 | `{"detail": "A patient with this ID is already registered in this clinic."}` (concerns only the caller's clinic) |
| 422 | validation failure, or **any unknown field, `clinic_id` included** |

### 5.3 `GET /patients?q=` (DOCTOR, ADMIN) **[F2]**
- `q` is required: trimmed, then 2–100 characters, otherwise 422.
- Case-insensitive substring match on `clinic_patient_id` or `display_name`, within the caller's clinic only; `%` and `_` match only themselves.
- Ranking: exact ID match, then ID-prefix matches, then the rest by name.
- **200:** `{"results": [{"patient_pk": 5, "clinic_patient_id": "OP-1001", "display_name": "…"}], "truncated": false}`. At most 20 results; `truncated` is `true` when more matched. No phone and no `clinic_id`.

### 5.4 `GET /patients/{patient_pk}/prescriptions?limit=&offset=` (DOCTOR, ADMIN) **[F3, F11, F11.1]**
- Another clinic's or a nonexistent patient: `404 {"detail": "Patient not found."}`.
- `limit` 1–50 (default 20), `offset` ≥ 0, otherwise 422.
- Returns only prescriptions **linked** to this patient, newest first; unlinked prescriptions never appear, even with the same name.
- **200:**
  ```json
  {
    "patient": {"patient_pk": 5, "clinic_patient_id": "OP-1001", "display_name": "…"},
    "prescriptions": [
      {"prescription_id": 12, "created_at": "2026-09-20 10:15:00", "status": "Active",
       "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 7}]}
    ],
    "limit": 20, "offset": 0, "has_more": false
  }
  ```
- `dosha_imbalance` and `prescribed_herbs` come from the structured record. For prescriptions confirmed before migrations 015/016 they are read from the legacy text, and are `null` if that text can't be parsed; they are never guessed.

## 6. Encounters **[F4]**

One consultation episode for one registered patient. `OPEN` → `CLOSED`, and `CLOSED` is final. At most **one open encounter per patient**. Encounters are never deleted, and their identity fields never change.

**`EncounterResponse`:**
```json
{"encounter_pk": 7, "patient_pk": 5, "created_by_user_id": 3, "status": "OPEN",
 "created_at": "2026-09-27 09:00:00", "closed_at": null, "encounter_ref": "9f2c41d07ab35e18"}
```
No `clinic_id` and no patient identity beyond `patient_pk`. `encounter_ref` is a random, unique, immutable 16-hex reference, and the only encounter identifier used in audit events.

| Endpoint | Role | Success | Errors |
|---|---|---|---|
| `POST /patients/{patient_pk}/encounters` | DOCTOR | `201` the new `OPEN` encounter; writes `ENCOUNTER_OPENED` in the same transaction | body must be empty (`{}`), **any field → 422**; `404` patient not in your clinic; `409 {"detail": "This patient already has an open encounter."}` |
| `GET /patients/{patient_pk}/encounters?limit=&offset=` | DOCTOR, ADMIN | `200 {"encounters": [EncounterResponse…], "limit", "offset", "has_more"}`, newest first; `limit` 1–50 (default 20) | `404` |
| `GET /encounters/{encounter_pk}` | DOCTOR, ADMIN | `200 EncounterResponse` | `404 {"detail": "Encounter not found."}` (another clinic's or nonexistent) |
| `POST /encounters/{encounter_pk}/close` | DOCTOR | `200` the encounter, `CLOSED`, `closed_at` set; any doctor of the clinic may close it; concurrent closes resolve to exactly one 200; writes `ENCOUNTER_CLOSED` | `404` as above; `409 {"detail": "This encounter is already closed."}` |

## 6A. Patient questions (AI-2) **[v1.7]**

`POST /patients/{patient_pk}/ask` answers one question about **one patient's recorded history** (confirmed prescriptions and encounters). It is **read-only**: it writes nothing (no row, no audit event), calls no external service and uses no language model; the same recorded history and the same question always give the same answer. It reports what was recorded and refuses advice, diagnosis and anything it cannot ground in a cited record. It is **not clinically validated.**

**Access (provisional).** DOCTOR only; ADMIN and AUDITOR get `403 {"detail": "Your role is not permitted to perform this action."}`. Whether ADMIN should be allowed is an open owner decision; this section documents the current behaviour, not a commitment. `401` takes priority over `403`, and `403` over request validation and the patient lookup. The clinic is the session's; the body cannot choose it.

**Request:**
```json
{"question": "When was Neem last prescribed?"}
```
- `question`: a string, trimmed, 1 to 1000 characters.
- Optional `choice` `{"frame": string ≤ 40 chars, "query": {…}}`: **only** a reading that an earlier `clarify` reply offered for the *same* question, sent back unchanged (`query` keys ⊆ `op, target, herb, dosha, last_n, a, b`; values null, a string ≤ 200 chars or an integer). Anything else is rejected, never treated as a plan.
- Unknown fields (including `clinic_id` and `patient_pk`) → `422`. `patient_pk` in the path must be an integer > 0 (`422` otherwise).

**Question length.** The service reads at most **300 characters and 60 whitespace-separated words** after normalisation (Unicode NFKC, whitespace collapsed). A longer question is **not** a `422`: it returns `200` with `outcome: "abstain"`, `category: "unsupported"` and `stage: "normalize"`. A client should treat `stage: "normalize"` as "question too long".

**Success `200` (`AskResponse`):**
```json
{"patient_pk": 5, "outcome": "answer", "category": "answer", "stage": "done", "reason": null,
 "text": "<verifier template sentence>", "value": {…}, "qualifier": "exact", "verified": true,
 "options": [], "evidence": [{"kind": "prescription", "label": "rx:7", "prescription_id": 7,
   "created_at": "2026-03-01 09:00:00", "status": "CONFIRMED", "dosha": "Vata",
   "herbs": [{"name": "Neem", "grams": 10}], "encounter_ref": null, "closed_at": null}],
 "plan_frame": "LAST_HERB", "plan_query": {"op": "last", "target": "herb", "herb": "Neem",
   "dosha": null, "last_n": null, "a": "previous", "b": "last"}, "clarified": false}
```
| Field | Meaning |
|---|---|
| `outcome` | `answer`, `clarify` or `abstain` |
| `category` | `answer` (verified, exact), `incomplete` (verified but limited by unknown or partial records), `clarify` (1 to 3 readings to choose from), `missing` (nothing recorded for it), `unsupported`, `unsafe` (advice, treatment, diagnosis, instruction injection), `access_denied` (another patient, other patients or the whole clinic) |
| `qualifier` | `exact`, `at_least`, `partial`, `unknown`, `not_recorded` or null |
| `verified` | true only for an answer that passed the deterministic verifier |
| `options` | for `clarify`: `[{"frame", "text", "query"}]`, 1 to 3 |
| `evidence` | the records the answer cites; `kind` is `prescription` or `encounter` (or `unknown`, never for a verified answer). A prescription carries `herbs`/`dosha` as values or **explicit `null` when unknown** (never inferred). Encounters carry their random `encounter_ref`, never the sequential key |
| `value`, `plan_*`, `stage`, `reason`, `text` | structured result, the executed closed-vocabulary query, the pipeline stage and reason, and the verifier's template sentence; a client should word the answer from `value`, `plan_query` and `evidence`, not from `text` |

An **empty** `value` list with `category: "incomplete"` is valid: when every prescription in scope has unknown herbs (or dosha) the service can list nothing and cites the unknown prescriptions.

**Errors:**
| Status | Body | When |
|---|---|---|
| `401` | as §3.3 | no, malformed or invalid session |
| `403` | wrong-role text above | ADMIN or AUDITOR |
| `404` | `{"detail": "Patient not found."}` | the patient is another clinic's or does not exist (identical) |
| `422` | validation list | a malformed body or path |
| `422` | `{"detail": "The chosen reading is not one of the readings offered for this question."}`, `X-Error-Code: CHOICE_NOT_OFFERED` | a `choice` that the service does not itself offer for that question |
| `503` | `{"detail": "The question service is unavailable."}`, `X-Error-Code: MEMORY_SERVICE_UNAVAILABLE` | the service could not start, or no consistent snapshot of the record could be read (database busy, I/O error, or a record listed twice). Nothing partial is returned; retry |

**Consistency.** The patient, prescription, item and encounter reads share **one read-only database transaction**, so the answer reflects a single point in time: a prescription confirmed while the question is being answered is wholly included or wholly excluded.

## 6B. Inventory **[v1.7]**

`GET /inventory` (DOCTOR, ADMIN; AUDITOR `403`): the caller's own clinic's current stock, read-only, sorted by herb name.

```json
{"items": [{"herb_name": "Ashwagandha", "stock_grams": 1200}, {"herb_name": "Neem", "stock_grams": 0}]}
```
No `clinic_id`, batch or potency data. Errors: `401`, `403`, and the common `503` busy response (§1).

## 7. Prescribing

The core safety rule: **the AI proposes, the doctor reviews and corrects, and a deterministic backend validates and commits.** Only `/prescribe/confirm` writes a prescription or deducts stock, and it makes **no AI call**.

### 7.1 `POST /prescribe/preview` (DOCTOR) **[v1 §1, A2, E3, F5, F10, F12]**

Runs the AI step (Gemini, with read-only herb and stock lookups, within a time budget). It stores a draft only: no prescription and no stock change.

**Request:**
```json
{
  "patient_name": "string, required, 1-200 chars",
  "notes": "string, required, 1-10000 chars: the raw clinical notes (voice transcript, typed or read-aloud)",
  "patient_phone": "string, optional, ≤ 32",
  "patient_id": "string, optional, ≤ 64",
  "patient_pk": "positive integer, optional: link to a registered patient",
  "encounter_pk": "positive integer, optional: requires patient_pk",
  "clinic_id": "string, optional: accepted and ignored"
}
```

**200 (`PreviewResponse`):**
```json
{
  "preview_id": "string: opaque handle; pass unchanged to /prescribe/confirm",
  "patient_name": "string: as sent, or the registered display_name when patient_pk is given",
  "dosha_imbalance": "string",
  "prescribed_herbs": [ { "name": "string", "grams": "integer" } ]
}
```

**Patient and encounter linkage:**
- **`patient_pk`** must belong to the caller's clinic; otherwise, or if it doesn't exist, `404 {"detail": "Patient not found."}`. When linked, the registry is authoritative: the draft stores the registered name, phone and OP number. Without `patient_pk`, the request's own identity fields are stored. **[E3]**
- **`encounter_pk`** is checked before any AI call or write: **[F5]**

  | Condition | Response |
  |---|---|
  | `encounter_pk` without `patient_pk` | `422 {"detail": "encounter_pk requires patient_pk."}` |
  | another clinic's encounter, or nonexistent | `404 {"detail": "Encounter not found."}` |
  | encounter of a different patient | `409 {"detail": "This encounter belongs to a different patient."}` |
  | encounter is `CLOSED` | `409 {"detail": "This encounter is closed."}` |
  | closed between the check and the write | `409 {"detail": "This encounter is no longer open for this patient."}` |

**Errors:**

| Status | Detail / header | Meaning |
|---|---|---|
| 400 | `"Herb '<name>' is not in inventory."` | the AI named an herb the canonical resolver doesn't recognise |
| 404, 409, 422 | see linkage above | patient and encounter checks |
| 422 | `"Invalid AI schema returned"` | unusable AI reply: malformed, contains a `[[PAT_…]]` placeholder, names the patient in its dosha, or proposes **zero herbs** **[F10, F12]** |
| 500 | `"Internal privacy safeguard triggered; request aborted before reaching the AI service."` | the PII pre-flight check stopped the request; **no AI call was made** |
| 502 | `"AI parsing failed"` with `X-Error-Code: AI_SERVICE_UNAVAILABLE` | the AI was unreachable, rate-limited or timed out; **offer manual entry** **[A2]** |
| 502 | `"AI response could not be safely de-tokenized; request aborted."` (no header) | not an outage; don't auto-offer manual entry for this reason |

A caller that doesn't recognise `X-Error-Code` can treat any 502 as a plain AI failure.

**Guarantees:**
- **Privacy:** the patient's name, phone and ID (and, when linked, the registered name, phone and OP number) are tokenized locally, verified absent from the outgoing text, and restored locally after the reply. They are never sent to the AI. **[v1 §1, E3]**
- **`preview_id`:** opaque and unguessable (64 bits of randomness); nothing is encoded in it. All draft state (patient identity, clinic, the AI's exact proposal, the creating session) lives server-side. It expires after **30 minutes** unconfirmed. **[v1 §1, A3]**
- **Drafts are persistent** (migration 003) and survive a restart. **[A3]**

### 7.2 `POST /prescribe/manual-preview` (DOCTOR) **[A1, E3, F5, F10]**

The deterministic fallback for when the AI is unavailable: **no AI call, no `notes` field**, nothing sent to any external service. It creates the same kind of draft and feeds the same `/prescribe/confirm`.

**Request:**
```json
{
  "patient_name": "string, required, 1-200 chars",
  "dosha_imbalance": "string, required, 1-200 chars: the doctor's own judgment",
  "prescribed_herbs": [ { "name": "string", "grams": "integer, > 0, <= 10000" } ],
  "patient_phone": "string, optional", "patient_id": "string, optional",
  "patient_pk": "optional", "encounter_pk": "optional (requires patient_pk)",
  "clinic_id": "string, optional: accepted and ignored"
}
```
`prescribed_herbs` needs at least one line.

**200:** the same `PreviewResponse` shape as §7.1.

**Errors:**
- **400:** an unrecognised herb, the same resolver and rejection the AI path uses.
- **404, 409, 422:** the patient and encounter checks of §7.1.
- **422:** `"Dosha must not contain patient identity (name, phone or ID)."` **[F10]**, or request validation: no herbs, or grams that aren't a whole number from 1 to 10,000.

**Guarantees:** everything §7.1 says about `preview_id` applies. A manual draft is **never** written to `corrections` on confirm: there is no AI proposal to compare it with. **[A1]**

### 7.3 `POST /prescribe/confirm` (DOCTOR) **[v1 §2, A3, B4, C2, D1–D3, E4, F5, F10, F11, F12]**

**Deterministic. No AI call.** The only endpoint that writes a prescription or deducts stock.

**Request:**
```json
{
  "preview_id": "string, required",
  "dosha_imbalance": "string, optional, ≤ 200: the doctor's correction; omit to accept the draft",
  "prescribed_herbs": "array, optional, 1-50 herb lines: the doctor's correction; omit or null to accept the draft"
}
```
Optional header: `Idempotency-Key: <opaque string, ≤ 200 chars>` (§7.4).

There is **no patient or encounter field**: both are copied only from the stored draft, and extra fields are ignored. **[E4, F5]**

**200 (`PrescriptionResponse`):**
```json
{
  "patient_name": "string",
  "dosha_imbalance": "string: as confirmed",
  "prescribed_herbs": [ { "name": "string", "grams": "integer" } ],
  "prescription_id": "integer",
  "created_at": "datetime"
}
```

**Failure precedence:** **[C2, D1, F5, F12]**
`401` → `403` role → `409` idempotency conflict → `404` draft missing, expired or another clinic's → `403` a different session created it → `409` already confirmed → validation and business errors → `200`.

| Status | Detail |
|---|---|
| 400 | `"Herb '<name>' is not in inventory."` (a corrected herb) |
| 403 | `"This preview belongs to a different session."` |
| 404 | `"Preview not found or expired. Please re-run /prescribe/preview."`, also for another clinic's draft **[C2]** |
| 409 | `"Preview '<id>' has already been confirmed."` |
| 409 | `"Insufficient stock for herb '<name>'. Requested: <n>g."` (nothing deducted) |
| 409 | `"This draft's encounter is closed. Open a new encounter and draft again."` **[F5]** |
| 409 | `"Idempotency key '<key>' was already used with a different request."` / `"Idempotency key '<key>' is no longer valid for the current authorization context."` **[D1]** |
| 422 | `"A prescription needs at least one herb. Add a herb and confirm again."` (an empty stored proposal) **[F12]** |
| 422 | a request-validation list with `type: too_short` on `prescribed_herbs` (an empty override) **[F12]** |
| 422 | `"Total for herb '<name>' is <n> g after combining duplicate lines; the maximum is 10000 g."` **[F11]** |
| 422 | `"Dosha must not contain patient identity (name, phone or ID)."` **[F10]** |
| 500 | `"Transaction failed due to an internal error."` (rolled back) |

**Every non-200 writes nothing:** no prescription, items, stock change, audit event, corrections row or idempotency record. The draft stays `PENDING`, so the doctor can correct and retry the **same** `preview_id` without a new AI call. **[v1 §2, F5, F12]**

**Guarantees:**
- **Atomic:** stock deduction, the prescription row, its items, the audit event and the draft's `PENDING` → `CONFIRMED` change commit or roll back together in one `BEGIN IMMEDIATE` transaction, which also survives a crash or restart. **[v1 §2, A3, F11]**
- **Single use:** a draft can be confirmed successfully only once; a second attempt is `409`, never a second prescription. **[v1 §2]**
- **Session binding:** a draft can be confirmed only by the session that created it (`403` otherwise). **[B4]**
- **Clinic, patient and encounter come from the stored draft,** never from the request. **[B5, E4, F5]**
- **Closed encounter:** checked inside the transaction, so a concurrent close either happens first (confirm refuses) or after (the prescription is inside the encounter). **[F5]**
- **One resolver:** AI-proposed and doctor-corrected herb names resolve through the same canonical resolver, and corrections get the same validation as AI output. **[v1 §2]**
- **Duplicate lines** of one herb, including aliases, are merged into one item before the 10,000 g check. **[F11]**
- **Corrections logging is best-effort:** after an AI-sourced prescription commits, exactly one `corrections` row is *attempted*, recording the **original** AI proposal read from the stored draft. A logging failure never rolls back or blocks the prescription. Manual drafts are never logged. **[v1 §2, A1]**

### 7.4 Idempotency **[D1–D3, F5, F12]**
- **Optional:** without the header, each request is a fresh attempt.
- **Recommended:** send one fresh key (e.g. a random UUID) per confirm attempt, and reuse **the same key and identical body** when retrying after a timeout or dropped connection.
- **Same key and body after a success:** the original `200` is replayed exactly: no second prescription, deduction or correction. This holds even if the encounter has since been closed.
- **Same key, different body** (`preview_id`, `dosha_imbalance` or `prescribed_herbs`): `409`. Both fields omitted on both requests count as identical.
- **A rejected confirm stores no key,** so the corrected request may reuse it.
- **Scope and privacy:** keys are scoped to `(the caller's own user, "prescribe_confirm", key)` and never used for authorization. Another user's key string behaves exactly like an unused one, so it reveals nothing. **[D3]**

## 8. Audit

### 8.1 `GET /audit/export?clinic_id=` (ADMIN, AUDITOR) **[C3]**
- `clinic_id` is optional. If given, it must equal the caller's clinic, otherwise `403 {"detail": "Cannot access another clinic's audit data."}`. The data always comes from the caller's clinic.
- **200:**
  ```json
  {
    "clinic_id": "string: always the caller's clinic",
    "generated_at": "datetime",
    "record_count": "integer",
    "changed_count": "integer",
    "records": [
      {
        "prescription_id": "integer", "clinic_id": "string", "created_at": "datetime",
        "ai_dosha_imbalance": "string",
        "ai_prescribed_herbs": [{"name": "string", "grams": "integer"}],
        "confirmed_dosha_imbalance": "string",
        "confirmed_prescribed_herbs": [{"name": "string", "grams": "integer"}],
        "was_changed": "boolean"
      }
    ],
    "integrity_notes": ["string"]
  }
  ```
- No patient identity anywhere.

### 8.2 `GET /audit/events?clinic_id=` (ADMIN, AUDITOR) **[F6]**
- The same `clinic_id` rule as §8.1. The clinic's hash chain is verified live on every call.
- **200:**
  ```json
  {"clinic_id": "…", "generated_at": "…", "event_count": 3, "chain_valid": true,
   "integrity_violations": [], "events": [AuditEventRecord…]}
  ```
- **Event types:**

  | `event_type` | `prescription_id` | `preview_id` | `event_data` |
  |---|---|---|---|
  | `PRESCRIPTION_CONFIRMED` | set | set | `decision_source`, `ai_proposal`, `doctor_decision`, `was_changed`; no encounter reference |
  | `ENCOUNTER_OPENED` | **null** | **null** | `{"encounter_ref": "<16 hex>"}` |
  | `ENCOUNTER_CLOSED` | **null** | **null** | `{"encounter_ref": "<16 hex>"}` |

  Clients must tolerate `null` `prescription_id` and `preview_id` on encounter events. Events written before migration 013 carry `{"encounter_pk": N}` instead of `encounter_ref`; they are never rewritten.
- **No patient identity:** no name, phone, OP number or `patient_pk`.
- **Tamper evidence:** one hash chain per clinic; `audit_events` rejects every `UPDATE` and `DELETE` (migration 014); signed off-database checkpoints come from `scripts/audit_anchor.py` (`docs/BACKUP_RESTORE_RUNBOOK.md`).

## 9. Stored data the contract guarantees

### 9.1 `corrections` table **[v1 §5]**
Written internally by confirm (§7.3); not an endpoint.

| column | type | notes |
|---|---|---|
| `id` | integer | autoincrement |
| `prescription_id` | integer | the prescription |
| `clinic_id` | text | |
| `ai_dosha_imbalance`, `ai_prescribed_herbs_json` | text | what the AI proposed |
| `confirmed_dosha_imbalance`, `confirmed_prescribed_herbs_json` | text | what was confirmed |
| `was_changed` | integer (0/1) | whether the doctor changed anything |
| `created_at` | timestamp | |

**Deliberately no patient identity column.** Keep it that way.

### 9.2 Structured prescriptions **[F11, F11.1]**
- Each confirm writes one `prescription_items` row per canonical herb (`line_no` 1..n in first-appearance order, `herb_name`, total `grams`), and `prescriptions.dosha_imbalance` stores the confirmed dosha. These are the authoritative record.
- `prescriptions.description` is still written for backward compatibility.
- Confirmed prescriptions and their items are **immutable** at the database level; no item may exceed 10,000 g.

### 9.3 Write-time enforcement (database triggers) **[E6, F8]**
Enforced independently of application code:
- A draft, prescription or encounter can reference only a patient of **its own** clinic, and an encounter's creator belongs to that clinic too.
- A patient's `patient_pk`, clinic and `clinic_patient_id` never change, and patients can't be deleted.
- An encounter's identity fields never change, a `CLOSED` encounter can't be updated, and encounters can't be deleted. One open encounter per patient.
- A draft's or prescription's `encounter_pk` must be an `OPEN` encounter of the same clinic and patient; a row without a patient can't carry one.
- A draft's or prescription's `patient_pk` and `encounter_pk` never change once written: no later linking, relinking or unlinking.

## 10. WhatsApp hand-off shape **[v1 §6]**

Not an endpoint: the shape a confirmed prescription is handed to, if a sender is ever wired.

```json
{
  "to_phone": "string: patient_phone from the draft",
  "patient_name": "string",
  "dosha_imbalance": "string",
  "prescribed_herbs": [ { "name": "string", "grams": "integer" } ],
  "clinic_id": "string"
}
```

## 11. Not part of the contract **[v1 "Not locked"]**

How speech, typing or paper becomes the `notes` string; the UI's screens and flow; hosting and deployment. None of these need to match between frontends.

## 12. Not yet decided or implemented **[E7, F9]**

- Frontend adoption of encounters (no client sends `encounter_pk` yet).
- Whether `patient_pk` or `encounter_pk` becomes required.
- Editing a patient's name or phone, merging duplicates, linking legacy prescriptions.
- Encounter notes, diagnoses, vitals or other clinical content.
- Auditing of registry changes, logins or drafts.

## 13. Where each original section went

| Original | Topic | Here |
|---|---|---|
| v1 §1, §2 | preview, confirm | §7.1, §7.3 |
| v1 §3, §3a | session token, session binding | §1, §7.3 |
| v1 §4 | `clinic_id` | §1 |
| v1 §5 | `corrections` | §9.1 |
| v1 §6 | WhatsApp shape | §10 |
| v1.1 A1–A3 | manual preview, `X-Error-Code`, persistent drafts | §7.2, §7.1 |
| v1.2 B1–B5 | login, logout, 401, session binding, clinic not trusted | §3, §7.3, §1 |
| v1.3 C1–C5 | roles, cross-clinic 404, `/audit/export`, `role` on login | §2, §7.3, §8.1, §3.1 |
| v1.4 D1–D3 | idempotency | §7.4 |
| v1.5 E1–E7 | patients, linkage, triggers | §5, §7.1, §9.3, §12 |
| v1.6 F1–F12 | health, search, history, encounters, audit events, cross-cutting errors, structured items, empty prescriptions | §4, §5.3–5.4, §6, §7, §8.2, §1, §9.2 |

The original documents are in `docs/archive/api-contract/`, unchanged.

## 14. Changing this contract

- **This file is the only contract.** Update it in the same pull request as the backend change, along with `docs/FRONTEND_HANDOFF.md`.
- **Bump the version** at the top (1.7, 1.8, …) and add a row to the change log below: what changed, why, and whether it is additive or breaking.
- **Additive by default.** A breaking change (a removed or renamed field, a new rejection on a request that used to succeed, a changed status code) must be called out explicitly and agreed by **both** developers first.
- **Protected invariants no change may weaken:** `/prescribe/confirm` is the only clinical write and stock deduction, with no AI call; `/prescribe/preview` never commits; clinic and role come only from the server-side session; confirmed prescriptions and audit events are immutable.

### Change log

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-09-13 | preview/confirm split, session token, `clinic_id`, `corrections`, WhatsApp shape |
| 1.1 | 2026-09-13 | manual preview; `X-Error-Code: AI_SERVICE_UNAVAILABLE`; persistent drafts |
| 1.2 | 2026-09-14 | real authentication: login, logout, 401 on prescribing (behavioural change, flagged) |
| 1.3 | 2026-09-14 | roles and clinic isolation; `/audit/export` formalised (new rejections, flagged) |
| 1.4 | 2026-09-14 | optional `Idempotency-Key` on confirm |
| 1.5 | 2026-09-26 | patient registry; `patient_pk` linkage |
| 1.6 | 2026-09-27 | health, search, history, audit events formalised; encounters; 503 busy; identity-in-dosha 422; structured items; empty-prescription 422 (schema relaxation: nullable ids on encounter audit events) |
| 1.6 (consolidated) | 2026-09-28 | v1 and addenda v1.1–v1.6 merged into this single document; no contract change |
| 1.7 | 2026-10-10 | **additive:** documents `POST /patients/{patient_pk}/ask` (§6A, DOCTOR only, provisional) and `GET /inventory` (§6B) as they behave in the code; no existing endpoint, field or status changed |
