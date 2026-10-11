# AyurCore — API Contract Addendum v1.5

**Date:** September 26, 2026, delivered with Patient Registry Phase 1 (migration 009).
**Status:** additive only. `AyurCore_API_Contract_v1.md` and addenda v1.1–v1.4 are unchanged. Every existing request and response shape, status code and rejection mode still holds exactly as written for any caller that does not send the new fields below. The `PreviewResponse` and `PrescriptionResponse` shapes are **not** changed.

This addendum adds a clinic-scoped patient registry. It has three parts:

- a new endpoint, `POST /patients`, to register a patient;
- an optional `patient_pk` field on `POST /prescribe/preview` and `POST /prescribe/manual-preview` that links a preview to a registered patient;
- `POST /prescribe/confirm` copying that link from the stored preview onto the prescription.

Patient search and history are **not** part of this addendum (Phase 2).

---

## E1. Patient identity

- A patient's identity is **(clinic, `clinic_patient_id`)**. `clinic_patient_id` is the clinic-issued OP/registration number:
  - it is trimmed, then must be 1–50 characters, with no control characters;
  - it is unique within a clinic, and the same value in two clinics is two unrelated patients;
  - it can never be changed once registered.
- `display_name` and `phone` are **attributes**, not identity.
- `patient_pk` is the registry's internal integer key. It is opaque, and it is meaningful only inside the clinic that owns the patient.
- The clinic is **always** the caller's authenticated clinic. No request field can choose it.
- Existing prescriptions are **not** linked, matched or backfilled. They stay unlinked (`patient_pk` NULL).

## E2. `POST /patients`

```
POST /patients
Authorization: Bearer <token>        (required; DOCTOR or ADMIN)
Content-Type: application/json

{
  "clinic_patient_id": "string, required, trimmed, 1-50 chars",
  "display_name": "string, required, trimmed, 1-200 chars",
  "phone": "string, optional, trimmed, 1-32 chars"
}
```

- **Unknown fields are rejected with 422, `clinic_id` included.** The patient is registered in the caller's authenticated clinic; a client-supplied clinic is never trusted or silently ignored.

**201 Created**

```json
{
  "patient_pk": 123,
  "clinic_patient_id": "OP-100",
  "display_name": "Asha Menon",
  "phone": "98470",
  "created_at": "2026-09-26 15:04:05"
}
```

**Errors**

| Status | When |
|---|---|
| 401 | Missing, invalid, expired or revoked session |
| 403 | Role is AUDITOR |
| 409 | This clinic already has a patient with this `clinic_patient_id` (after trimming). The message concerns only the caller's own clinic. |
| 422 | Validation failure, or any unknown field (such as `clinic_id`) |

## E3. `patient_pk` on `POST /prescribe/preview` and `POST /prescribe/manual-preview`

- **The new field is optional:** `"patient_pk": <positive integer>`. Omitting it keeps the pre-v1.5 behaviour exactly, with the request's own `patient_name`, `patient_phone` and `patient_id` stored on the preview.
- **When present, the patient must belong to the caller's clinic.** A `patient_pk` from another clinic and one that doesn't exist both return the **same 404** (`{"detail": "Patient not found."}`). No preview is written, and on `/prescribe/preview` no AI call is made.
- **The registry is authoritative for identity.** When linked, the preview stores the registered patient's `display_name`, `phone` and `clinic_patient_id` in its `patient_name`, `patient_phone` and `patient_id`. The response's `patient_name` is the registered `display_name`.
- **PII protection:** the registered name, phone and OP number are tokenized before any AI call, in addition to the request's own identity fields.
- **Response shape is unchanged** (`preview_id`, `patient_name`, `dosha_imbalance`, `prescribed_herbs`).

## E4. `POST /prescribe/confirm`

- **Request and response shapes are unchanged.** `ConfirmRequest` has no patient field, so the caller cannot choose, change or remove the patient at confirm.
- **The link comes from the stored preview.** Confirm copies the preview's `patient_pk` onto the new prescription, inside the same transaction as the stock deduction, the preview state change and the audit event. An unlinked preview produces an unlinked prescription.

## E5. RBAC

| Endpoint | DOCTOR | ADMIN | AUDITOR | Clinic scope |
|---|:-:|:-:|:-:|---|
| `POST /patients` | YES | YES | **NO** | the session's clinic; a client `clinic_id` is rejected |
| `patient_pk` on preview / manual-preview | YES | n/a (prescribing is DOCTOR-only, unchanged) | n/a | patient must be in the session's clinic, else 404 |

- **AUDITOR gets no patient endpoint and no patient data.** `/audit/export` and `/audit/events` are unchanged and contain no patient name, phone, OP number or `patient_pk`.

## E6. Write-time enforcement (migration 009)

These are enforced by SQLite triggers, independently of application code and whether or not the connection enables foreign keys:

- A preview or prescription can only reference a patient of **its own** clinic (`patient_clinic_mismatch`). This applies on insert and on any update of the link or of the row's clinic.
- A patient's `patient_pk`, clinic and `clinic_patient_id` are immutable, and patients cannot be deleted.
- A preview's or prescription's `patient_pk` can never change once written. An old, unlinked prescription therefore cannot be linked by a later update; any future linking path would need its own audited design and migration.

## E7. Not in this addendum (Phase 2)

- Patient search and prescription history endpoints, and the Next.js patient picker and patients page.
- Whether `patient_pk` becomes required for new prescriptions.
- Editing a patient's name or phone through the API, merging duplicate patients, and linking legacy prescriptions.
- Registry changes in the tamper-evident audit chain, which today covers confirmed prescriptions only.
