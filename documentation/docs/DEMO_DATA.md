# Demo data

A small, deterministic, clearly-synthetic dataset for manual clinic
simulation and the final project demonstration. Created and managed
entirely by `scripts/seed_demo_data.py`, which uses the real application
code (real login, real `/prescribe/*` endpoints, real Argon2id password
hashing) to produce it — nothing here is hand-fabricated in the database.

**All data is synthetic.** No real patient, clinic, or credential
information is used anywhere in this dataset.

## Seed command

```bash
python scripts/seed_demo_data.py
```

Safe to run repeatedly — users, clinic-B inventory, and the 13-record
prescription/audit history are each created at most once.

## Reset command

```bash
python scripts/seed_demo_data.py --reset
```

See "Restoring the clean demo state" below for exactly what this does and
does not remove, and why.

## Demo accounts

| Username | Password | Role | Clinic |
|---|---|---|---|
| `demo_doctor` | `ayurcore-demo-password-not-a-real-credential` | DOCTOR | `default_clinic` |
| `demo_admin` | `ayurcore-demo-password-not-a-real-credential` | ADMIN | `default_clinic` |
| `demo_auditor` | `ayurcore-demo-password-not-a-real-credential` | AUDITOR | `default_clinic` |
| `demo_doctor_b` | `ayurcore-demo-password-not-a-real-credential` | DOCTOR | `test_clinic_b` |

All four share one documented, non-secret password (the same naming
convention this project's own test suite already uses for synthetic
credentials). Password hashing is real Argon2id — nothing is weakened for
these accounts.

## Clinics used

- **`default_clinic`** — where `demo_doctor` / `demo_admin` / `demo_auditor`
  operate. This clinic's inventory already exists (used by the rest of
  this project's test suite) and is **never modified** by this seed
  script — only read, to confirm the herbs the demo needs are already
  stocked.
- **`test_clinic_b`** — a second clinic, used only by `demo_doctor_b`, to
  demonstrate cross-clinic isolation (DEMO-008). This clinic had zero
  pre-existing inventory, so the seed script owns it completely: 5 herb
  rows, each with a `DEMO-` batch-code prefix.

## What's seeded

- **4 users** (table above).
- **5 inventory rows in `test_clinic_b`** (Ashwagandha, Brahmi, Haridra,
  Triphala, Shatavari), batch-coded `DEMO-*`. Shatavari is deliberately
  stocked low (40g) as a persistent low-stock example.
- **13 confirmed demo prescriptions**, each with a real, hash-chained audit
  event — 10 under `demo_doctor` / `default_clinic`, 3 under
  `demo_doctor_b` / `test_clinic_b`. Every one uses a synthetic patient
  name prefixed `(DEMO) ` (e.g. `(DEMO) Anil Kumar`) — this prefix is the
  dataset's marker: `SELECT * FROM prescriptions WHERE patient_name LIKE
  '(DEMO) %'` finds exactly this data and nothing else.

## Demo scenarios this dataset supports

| ID | Scenario | How to see it |
|---|---|---|
| DEMO-001 | Normal AI-assisted consultation, confirmed unchanged | `(DEMO) Anil Kumar` — Kapha / Ashwagandha 400g |
| DEMO-002 | Doctor edits the AI proposal before confirming | `(DEMO) Meera Nair` — AI proposed Kapha/Ashwagandha 400g; confirmed as Vata-Kapha / Ashwagandha 350g + Shatavari 50g |
| DEMO-003 | Manual fallback (no AI call) | the other 11 demo patients |
| DEMO-004 | Insufficient inventory → safe rejection | re-run live by the seed script every time (see below); requests more Brahmi than `default_clinic` currently has, gets HTTP 409 |
| DEMO-005 | Unknown herb → safe rejection | re-run live every time; "Tulsi" is not a herb this clinic's formulary recognizes (see Limitations), so requesting it gets HTTP 400 |
| DEMO-006 | Admin audit workflow | log in as `demo_admin`, open Audit / Prescriptions |
| DEMO-007 | Auditor workflow (read-only, cannot prescribe) | log in as `demo_auditor` |
| DEMO-008 | Cross-clinic isolation | re-run live every time; `demo_doctor` cannot confirm a `test_clinic_b` preview (HTTP 404) |

DEMO-004, DEMO-005, and DEMO-008 are **live rejection checks** the seed
script re-runs on every invocation (they never persist anything, so
re-running them is free and safe) — their latest pass/fail result is
recorded in `scripts/.demo_seed_manifest.json` under `last_verification`.

## Limitations (found while inspecting the existing schema/API, not worked around)

- **No patient table or API exists.** `PreviewRequest` /
  `ManualPreviewRequest` carry only `patient_name`, `patient_phone`,
  `patient_id` as free-text fields on a preview/prescription — there is no
  standalone patient record, and none was invented for this task. Patient
  variety is expressed only through these fields.
- **No age/sex fields exist anywhere in the schema.** The synthetic
  patients' ages and sexes (used only for this document's own narrative
  variety) are *not* stored anywhere in the application — there is no
  field to put them in. They live only in `scripts/seed_demo_data.py`'s
  `DEMO_PATIENTS_*` lists and this file.
- **"Tulsi" is not a valid herb in this clinic's formulary.** It does not
  appear in `backend/herb_resolver.py`'s `HERB_ALIASES` (a pre-existing
  fact about this codebase, not something this task changed). Rather than
  silently drop it from the requested herb list, this dataset uses it on
  purpose as the concrete example for DEMO-005 (unknown-herb rejection).
- **"Turmeric" is not its own canonical herb** — it's an alias that
  resolves to the canonical herb **Haridra**. Everywhere this document or
  the seed script mentions "Turmeric," the actual stocked/prescribed herb
  name is `Haridra`.

## Restoring the clean demo state

`--reset` removes everything that is safely removable:

- demo sessions and login-throttle rows,
- any of this dataset's prescription previews that were never confirmed,
- the 5 `test_clinic_b` demo inventory rows,
- and the demo user accounts themselves — **but only once they have no
  confirmed prescriptions**. `demo_admin` and `demo_auditor` (which only
  ever read audit data) can always be removed this way. `demo_doctor` and
  `demo_doctor_b`, once they've confirmed at least one demo prescription,
  **cannot** be deleted without either violating a real foreign-key
  constraint (`audit_events.actor_user_id`) or breaking that clinic's
  tamper-evident audit hash chain — both of which this project's own
  safety rules explicitly forbid doing. `--reset` detects this, leaves
  those two accounts and their confirmed history in place, and reports
  exactly why in its output — it never forces the deletion through.

In practice this means: once the demo has actually been run through
end-to-end at least once, "the clean demo state" is the state where the
users and their existing, permanent audit trail are unchanged, and only
the incidental, safely-removable bits (sessions, unconfirmed previews,
clinic-B inventory) are reset. This mirrors how the rest of the
application already treats confirmed prescriptions and audit events —
permanent, tamper-evident records — for every other clinic, demo or not.
Re-running `python scripts/seed_demo_data.py` after a `--reset` recreates
any users/inventory that were actually removed and leaves the rest
untouched; it never creates a second copy of the 13-record history.
