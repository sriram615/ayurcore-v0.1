# AyurCore — API Contract Addendum v1.6

**Date:** September 27, 2026.
**Revised:** September 27, 2026, after the post-1B security review (sections F4, F5, F6 and F10 updated; migrations 013–014).
**Status:** additive. `AyurCore_API_Contract_v1.md` and addenda v1.1–v1.5 still hold as written, with **one schema relaxation** described in F6: `GET /audit/events` may now return events whose `prescription_id` and `preview_id` are `null`. Every request and response shape for callers that don't use the new fields is unchanged, including `PreviewResponse` and `PrescriptionResponse`.

This addendum:

1. **gives first formal entries** to endpoints that already existed without a contract section: `GET /health`, `GET /patients?q=`, `GET /patients/{patient_pk}/prescriptions` and `GET /audit/events`;
2. **adds encounters** (migration 010): four endpoints;
3. **adds encounter linkage** to previews, inherited at confirm (migration 012);
4. **adds encounter lifecycle events** to the tamper-evident audit chain (migration 011).

In every endpoint below, the clinic is the caller's authenticated clinic. No request field can choose it.

---

## F1. `GET /health`

- **Public:** no `Authorization` header needed.
- **Response:** always `200 {"status": "ok"}`.
- **What it checks:** nothing beyond the process answering. It touches no database, AI or configuration, so it's a liveness check, not a readiness check.
- **Other methods** return 405.

## F2. `GET /patients?q=` (DOCTOR, ADMIN)

- **Query:** `q` is required. It is trimmed and must then be 2–100 characters, otherwise 422.
- **Matching:** case-insensitive substring match on `clinic_patient_id` or `display_name`, within the caller's clinic only. `%` and `_` in `q` match only themselves.
- **Ranking:** an exact ID match first, then ID-prefix matches, then the rest, sorted by name.
- **200:**
  ```json
  {"results": [{"patient_pk": 5, "clinic_patient_id": "OP-1001", "display_name": "…"}], "truncated": false}
  ```
  At most 20 results; `truncated` is `true` when more matched. No phone number and no `clinic_id` are returned.
- **Errors:** 401, 403 (AUDITOR), 422.

## F3. `GET /patients/{patient_pk}/prescriptions?limit=&offset=` (DOCTOR, ADMIN)

- **Patient check:** the patient must belong to the caller's clinic. A patient of another clinic and a nonexistent one both return `404 {"detail": "Patient not found."}`.
- **Paging:** `limit` 1–50 (default 20), `offset` ≥ 0, otherwise 422.
- **Contents:** only prescriptions **linked** to this patient (`prescriptions.patient_pk`), newest first. Unlinked prescriptions are never included, even with the same name.
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
  `dosha_imbalance` and `prescribed_herbs` are `null` only if a stored record isn't in the format the confirm step writes. `created_at` is UTC in `YYYY-MM-DD HH:MM:SS` form.

## F4. Encounters (migration 010)

An encounter is one consultation episode for one registered patient.
- **States:** `OPEN` → `CLOSED`, and `CLOSED` is final (no reopening).
- **One open encounter per patient** at a time.
- **Permanence:** encounters are never deleted, and their identity fields never change.

**Response shape** (`EncounterResponse`) for F4.1, F4.3 and F4.4; F4.2 returns a page of them:

```json
{"encounter_pk": 7, "patient_pk": 5, "created_by_user_id": 3, "status": "OPEN",
 "created_at": "2026-09-27 09:00:00", "closed_at": null, "encounter_ref": "9f2c41d07ab35e18"}
```

There is no `clinic_id` and no patient identity beyond `patient_pk`. `encounter_ref` (migration 013) is a random 16-hex-character reference, unique and immutable. It is the only encounter identifier used in audit events (F6), so an ADMIN can match an audit event to an encounter.

### F4.1 `POST /patients/{patient_pk}/encounters` (DOCTOR)
- **Body:** optional and must be empty (`{}`). **Any field, `clinic_id` included, is rejected with 422.** The clinic and the creator come from the session.
- **201:** the new `OPEN` encounter.
- **404:** the patient isn't in the caller's clinic, or doesn't exist (the same response in both cases).
- **409:** `{"detail": "This patient already has an open encounter."}`
- **Audit:** writes an `ENCOUNTER_OPENED` audit event in the same transaction (F6).

### F4.2 `GET /patients/{patient_pk}/encounters?limit=&offset=` (DOCTOR, ADMIN)
- **Paging and order:** newest first; `limit` 1–50 (default 20), `offset` ≥ 0.
- **200:** `{"encounters": [EncounterResponse…], "limit", "offset", "has_more"}`.
- **404:** the patient isn't in the caller's clinic.

### F4.3 `GET /encounters/{encounter_pk}` (DOCTOR, ADMIN)
- **200:** `EncounterResponse`.
- **404:** `{"detail": "Encounter not found."}` for another clinic's encounter or a nonexistent one.

### F4.4 `POST /encounters/{encounter_pk}/close` (DOCTOR)
- **200:** the encounter, now `CLOSED` with `closed_at` set. Any doctor of the clinic can close it.
- **404:** as in F4.3.
- **409:** `{"detail": "This encounter is already closed."}`
- **Concurrency:** concurrent closes resolve to exactly one 200.
- **Audit:** writes an `ENCOUNTER_CLOSED` audit event in the same transaction.

## F5. Encounter linkage on previews and confirm (migration 012)

**`POST /prescribe/preview` and `POST /prescribe/manual-preview`** take a new optional field, `"encounter_pk": <positive integer>`. When present, it's validated **before any AI call or write**:

| Condition | Response |
|---|---|
| `encounter_pk` without `patient_pk` | 422 `{"detail": "encounter_pk requires patient_pk."}` |
| Encounter of another clinic, or nonexistent | 404 `{"detail": "Encounter not found."}` (identical) |
| Encounter of a different patient (same clinic) | 409 `{"detail": "This encounter belongs to a different patient."}` |
| Encounter is `CLOSED` | 409 `{"detail": "This encounter is closed."}` |
| Encounter closed between the check and the write (database backstop) | 409 `{"detail": "This encounter is no longer open for this patient."}` |

Omitting `encounter_pk` keeps the v1.5 behaviour exactly. Response shapes are unchanged.

**`POST /prescribe/confirm`** request and response shapes are unchanged.
- **The encounter can't be chosen at confirm.** It is copied **only from the stored preview**; `ConfirmRequest` has no encounter or patient field, and extra fields are ignored.
- **A closed encounter blocks the confirm.** If the draft's encounter has been closed since the draft was created, confirm returns:

  ```
  409 {"detail": "This draft's encounter is closed. Open a new encounter and draft again."}
  ```

  **Nothing is written:** no stock deduction, prescription, audit event, correction or idempotency record. The draft stays `PENDING` and keeps its (immutable) encounter link, so it can't be moved to a new encounter; draft again instead.
- **Idempotency is unchanged (v1.4).** A retry with the same `Idempotency-Key` and payload **after a successful confirm** still replays the original 200, even if the encounter has since been closed. A same-key retry of a *refused* confirm is evaluated again, because nothing was stored.
- **No race with closing:** confirm checks the encounter inside its `BEGIN IMMEDIATE` transaction, so a close can't slip between the check and the insert. A concurrent close either happens before the confirm (which then refuses) or after it (the prescription is inside the encounter).

## F6. `GET /audit/events` (ADMIN, AUDITOR): formal entry and new event types

- **Scope:** `clinic_id` query parameter is optional; any value other than the caller's own clinic gets 403. The chain is verified live on every call.
- **200:**
  ```json
  {"clinic_id": "…", "generated_at": "…", "event_count": 3, "chain_valid": true,
   "integrity_violations": [], "events": [AuditEventRecord…]}
  ```
- **Event types:** `event_type` is one of:

  | `event_type` | `prescription_id` | `preview_id` | `event_data` |
  |---|---|---|---|
  | `PRESCRIPTION_CONFIRMED` | set | set | `decision_source`, `ai_proposal`, `doctor_decision`, `was_changed`. **No encounter reference**, so auditors can't group a clinic's prescriptions by visit. |
  | `ENCOUNTER_OPENED` | **null** | **null** | `{"encounter_ref": "<16 hex>"}` |
  | `ENCOUNTER_CLOSED` | **null** | **null** | `{"encounter_ref": "<16 hex>"}` |

  **Earlier events are unchanged.** Events written before migration 013 carry the sequential `encounter_pk` instead (`{"encounter_pk": N}` on encounter events, and on confirm events inside an encounter). They are hash-chained and are never rewritten.

  **Schema relaxation:** `prescription_id` and `preview_id` may now be `null`, for the two encounter event types only. Clients that assumed they are always present must tolerate `null`.
- **No patient identity:** events carry no patient name, phone, `clinic_patient_id` or `patient_pk`. The encounter is identified only by its random `encounter_ref`, which, unlike the global sequential `encounter_pk`, reveals nothing about other clinics' encounter volume. (`prescription_id` is still a global sequence; that predates this addendum.)
- **Identity in the dosha field is rejected (F10),** so patient identity can't enter `ai_proposal.dosha` or `doctor_decision.dosha`.
- **Append-only (migration 014):** `audit_events` rejects every `UPDATE` and `DELETE` at the database level. `scripts/audit_anchor.py` provides signed checkpoints held outside the database to detect truncation or rewrite (`documentation/docs/BACKUP_RESTORE_RUNBOOK.md`).
- **One chain per clinic:** all three types share the clinic's single hash chain. The hash algorithm is unchanged. Migration 011 rebuilt the table to allow the new types and copied every existing event byte for byte, so every existing hash still verifies.

## F7. RBAC summary for this addendum

| Endpoint | DOCTOR | ADMIN | AUDITOR |
|---|:-:|:-:|:-:|
| `GET /health` | public | public | public |
| `GET /patients?q=`, `GET /patients/{pk}/prescriptions` | ✅ | ✅ | ❌ 403 |
| `POST /patients/{pk}/encounters`, `POST /encounters/{pk}/close` | ✅ | ❌ 403 | ❌ 403 |
| `GET /patients/{pk}/encounters`, `GET /encounters/{pk}` | ✅ | ✅ | ❌ 403 |
| `encounter_pk` on preview / manual-preview | ✅ (prescribing is DOCTOR-only, unchanged) | n/a | n/a |
| `GET /audit/events` | ❌ 403 | ✅ | ✅ |

## F8. Write-time enforcement (migrations 010–012)

SQLite triggers enforce these independently of application code, whether or not foreign keys are enabled:

- **Encounter must match its patient's clinic:** an encounter's patient belongs to the encounter's clinic, and so does its creator.
- **Encounter identity is fixed:** `encounter_pk`, `clinic_id`, `patient_pk`, `created_by_user_id` and `created_at` never change.
- **Closing is final:** a `CLOSED` encounter can't be updated at all.
- **No deletions:** encounters can't be deleted.
- **One open encounter per patient.**
- **Valid links only:** a preview's or prescription's `encounter_pk` must be an `OPEN` encounter of the same clinic **and** the same patient. A row without a patient can't carry one.
- **Links are permanent:** a preview's or prescription's `encounter_pk` can never change once written (no later linking, relinking or unlinking).

## F10. Cross-cutting error responses (post-1B security review)

- **Database busy: 503.** Any endpoint whose write couldn't obtain SQLite's lock within the busy timeout returns:

  ```
  503 {"detail": "The system is busy. Nothing was saved. Please try again."}
  X-Error-Code: DATABASE_BUSY
  Retry-After: 5
  ```

  **Nothing was written** in that case, so the request is safe to retry; confirm is also covered by `Idempotency-Key`. Other database errors stay a generic `500` with no internal detail. Housekeeping writes (`last_seen_at`, the preview-expiry sweep) no longer wait for the lock, so a busy database costs at most one busy timeout per request.
- **Identity in the dosha field: 422.** Dosha is free text and is copied into the audit trail and corrections, so it must not contain the patient's name (full or any part of 3+ letters), phone or ID. Matching is whole-word and case-insensitive, so a name part inside a clinical word (e.g. "Kap" in "Kapha") isn't a match.
  - `POST /prescribe/manual-preview`, and `POST /prescribe/confirm` with a `dosha_imbalance` correction, return `422 {"detail": "Dosha must not contain patient identity (name, phone or ID)."}`. Nothing is written; for confirm, that means no stock, prescription, audit event or idempotency record.
  - `POST /prescribe/preview` returns the existing `422 {"detail": "Invalid AI schema returned"}` if the AI reply contains any `[[PAT_…]]` placeholder (it is rejected, never turned back into the real identity) or names the patient in its dosha.
  - **Limitation:** only the identity the request or the registry supplies is recognised. It is not a general detector for other people's names.

## F11. Structured prescription items (migration 015)

- **No request or response shape changes.** `POST /prescribe/confirm` returns exactly the same body as before.
- **What is stored:** every successful confirm writes one `prescription_items` row per canonical herb (`line_no` 1..n in first-appearance order, `herb_name`, total `grams`) in the same transaction as the stock deduction, the prescription row and the audit event. `prescriptions.description` is still written unchanged.
- **Duplicate lines** of one herb (including different aliases) are merged into one item, as the deduction already did.
- **New 422:** if a herb's merged total exceeds 10,000 g, confirm returns `422 {"detail": "Total for herb '<name>' is <n> g after combining duplicate lines; the maximum is 10000 g."}` and writes nothing. Previously this case failed with a generic `500` (also writing nothing).
- **Immutable:** items cannot be updated or deleted, and none can be added once the prescription's `PRESCRIPTION_CONFIRMED` audit event exists.
- **`GET /patients/{patient_pk}/prescriptions`:** `prescribed_herbs` now comes from the items when a prescription has them; prescriptions confirmed before migration 015 still read from `description`. Same response shape.

### F11.1 Phase 2B: structured data is authoritative (migration 016)

- **No request or response shape changes.**
- `prescriptions.dosha_imbalance` stores the confirmed dosha. `GET /patients/{patient_pk}/prescriptions` takes `dosha_imbalance` from it and `prescribed_herbs` from the items; it parses `description` only for a field with no structured value (prescriptions confirmed before migration 015/016). Unparseable legacy text is still returned as `null`, never guessed.
- A confirmed prescription's content can't be updated or deleted, and an item can't exceed 10,000 g, at the database level.

## F12. Empty prescriptions are rejected (post-Phase 3A.2)

No request or success-response shape changes. A prescription must contain at least one herb; three paths that could previously commit a prescription with no herbs (no items, no stock deduction) now return `422` and write nothing.

| Path | Response | What is written |
|---|---|---|
| `POST /prescribe/preview`: the AI proposal resolves to zero herbs | `422 {"detail": "Invalid AI schema returned"}` (the existing response for any unusable AI reply) | Nothing. No preview is stored, so there is nothing to confirm. |
| `POST /prescribe/confirm` with `"prescribed_herbs": []` | `422` standard request-validation body: `{"detail": [{"type": "too_short", "loc": ["body", "prescribed_herbs"], "msg": "List should have at least 1 item after validation, not 0", ...}]}` | Nothing. The request is rejected before the handler runs. |
| `POST /prescribe/confirm` for a preview whose **stored** proposal is empty (saved before these checks), with no override | `422 {"detail": "A prescription needs at least one herb. Add a herb and confirm again."}` | Nothing. Rejected inside the confirm transaction and rolled back. |

- **Nothing is written in any of the three cases:** no prescription, `prescription_items`, audit event or corrections row, and no stock deducted.
- **Idempotency:** a rejected request creates **no** idempotency record.
  - An empty override never reaches the reservation step.
  - For an empty stored proposal, the reservation is rolled back with the transaction.
  - The same `Idempotency-Key` can therefore be reused for a corrected request, which then succeeds normally.
- **Preview state:** a preview whose stored proposal is empty stays `PENDING`. The doctor can confirm it with a non-empty `prescribed_herbs` override.
- **Unchanged:**
  - omitting `prescribed_herbs`, or sending `null`, still means "use the stored proposal";
  - a one-herb override, a duplicate-line override (merged) and a dosha-only correction behave as before;
  - the 50-line override bound is unchanged.
- **Clients:** the Next.js and Streamlit UIs already prevent confirming with no herbs.
  - They show their existing AI-reply message ("…couldn't be read. Try again, or write it yourself.") for the preview case.
  - They show their generic validation message for the stored-proposal case.

## F9. Not in this addendum

- Frontend adoption. No client sends `encounter_pk` or opens encounters yet.
- Whether `encounter_pk` (or `patient_pk`) becomes required.
- Encounter notes, diagnoses, vitals, or any clinical content on the encounter itself.
- Auditing of patient-registry changes, logins or previews.
