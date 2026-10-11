# AyurCore OS — Backend-to-Frontend Handoff

**Prepared:** 2026-09-27. **Backend code baseline:** Git `12f2e6a` (branch `main`); documentation baseline `0cbcf41` and later.
**Status:** backend engineering-complete for Phase 2 and Phase 3A (evaluation-only). **Not clinically validated and not production-ready** (`docs/TECHNICAL_RISK_REGISTER.md`, `docs/HANDOFF.md`).

**Audience:** Member 2, who is building a **new frontend from scratch**. This is a backend API and integration guide. It does not depend on, describe or recommend any existing UI code.

**How this document was built.** It was compiled from the API contract (v1 plus addenda v1.1–v1.6, since merged into `AyurCore_API_Contract.md`, which keeps the section tags cited here, e.g. v1.6 F5) and static reading of the backend (`backend/main.py`, `backend/schemas.py`, `backend/rbac.py`, `backend/auth_service.py`, `agents/copilot.py`). No request was sent to the running API, and no database was read.

**Frontend ownership (updated 2026-10-11, decision D-03).** `frontend/` (Next.js) is the selected v0.1 portfolio demo surface; `apps/web/` is a README-only placeholder and no migration to it is authorized. `ui/` (Streamlit) remains legacy material due for retirement (§8): do not copy, extend or learn behaviour from it. Where this document and any UI disagree, this document and the contract are right (the contract is v1.7; this document's endpoint descriptions were written against v1.6).

**Source order when anything disagrees:** the code, then the contract, then this document. Report any disagreement instead of working around it.

Labels:
- **CONTRACT §x** — stated in the contract.
- **CODE** — read from source.
- **UNKNOWN** — not established; each says how to verify it.

---

## 1. Architecture and where things live

| Concern | Location |
|---|---|
| All HTTP routes (15 operations, 13 paths) | `backend/main.py` |
| Request/response models (the schema of record) | `backend/schemas.py` |
| Roles and permissions | `backend/rbac.py` (`PERMISSION_MATRIX`, `require_role`) |
| Login, sessions, throttling | `backend/auth_service.py` |
| Preview storage and expiry | `backend/preview_store.py` |
| Confirm, inventory deduction, structured items | `backend/main.py` (`prescribe_confirm`), `backend/inventory_service.py` |
| Herb resolution (the formulary) | `backend/herb_resolver.py` (`normalize_herb`, `merge_herb_lines`) |
| Patients, history | `backend/patient_store.py` |
| Encounters | `backend/encounter_store.py` |
| Audit | `backend/audit_store.py` |
| Guardrails (evaluation-only, **not called by any endpoint**) | `backend/guardrails/` |
| Architecture overview | `docs/ARCHITECTURE_BASELINE.md` |
| Demo frontend (v0.1, D-03) | `frontend/` (Next.js); `apps/web/` is a placeholder |
| Legacy UI (**not an integration source**) | `ui/` + `.streamlit/` (Streamlit) — see §8 |

**Trust boundary (CONTRACT §1–2; `CLAUDE.md`):** *AI proposes → doctor reviews and corrects → deterministic backend validates and commits.*
- Only `/prescribe/preview` calls the AI.
- `/prescribe/confirm` makes **no AI call** and is the only clinical write and stock deduction.

**Backend address in development:** `http://127.0.0.1:8000` (`uvicorn backend.main:app --host 127.0.0.1 --port 8000`; see `README.md`). Make the API base URL configurable in the new frontend (one setting, no secrets).

**CORS (CODE, `backend/main.py`):**
- origins: `http://localhost:3000` and `http://127.0.0.1:3000` only;
- methods: `GET`, `POST`, `OPTIONS`;
- request headers: `Content-Type`, `Authorization`, `Idempotency-Key`;
- exposed response headers: **only** `X-Error-Code`;
- `allow_credentials=False`.

---

## 2. Authentication, sessions, RBAC, clinic isolation

- **Login** (`POST /auth/login`, CONTRACT v1.2 B1, v1.3 C4):
  - **Request:** `{"username": 1–128 chars, "password": 1–256 chars}`.
  - **200:** `{"token", "expires_at", "username", "clinic_id", "role"}`.
  - **Token:** an opaque bearer string; never decode it.
  - **Sessions:** 12 h absolute, not extended by use (CODE `SESSION_TTL_SECONDS`).
- **Failed login:** always `401 {"detail": "Invalid username or password."}`. This covers an unknown user, a wrong password, a deactivated account and an **active throttle** (5 failures, then a 45 s lock; CODE `FAILURE_THRESHOLD`, `COOLDOWN_SECONDS`). The UI cannot tell these apart, by design.
- **Authenticated requests:** send `Authorization: Bearer <token>`.
  - **Missing, invalid, expired or revoked token:** `401`, with `"Missing or malformed Authorization header. Expected 'Bearer <token>'."` or `"Invalid, expired, or revoked session."` Treat every 401 the same: re-authenticate.
- **Logout** (`POST /auth/logout`): `200 {"status": "logged_out"}`. It revokes the token immediately; a second logout with the same token is `401`.
- **`role`** (`DOCTOR`, `ADMIN` or `AUDITOR`) is informational for the UI only. The server re-derives the role on every request.
  - **Wrong role:** `403 {"detail": "Your role is not permitted to perform this action."}`.
  - **Order of checks:** 401 takes priority over 403.
- **Clinic isolation:**
  - The clinic is **always** the session's clinic.
  - `clinic_id` in preview request bodies is accepted but **ignored**. On `POST /patients` and the encounter-open body it is **rejected with 422**.
  - Another clinic's patient, encounter or preview returns the **same 404** as a nonexistent one.
  - The audit endpoints answer `403 {"detail": "Cannot access another clinic's audit data."}` to a mismatched `clinic_id` query.

**Role matrix (CODE `backend/main.py` `require_role`; CONTRACT v1.3, v1.5 E5, v1.6 F7):**

| Operation | DOCTOR | ADMIN | AUDITOR |
|---|:-:|:-:|:-:|
| `GET /health`, `POST /auth/login` | public | public | public |
| `POST /auth/logout` | ✓ | ✓ | ✓ |
| `POST /patients`, `GET /patients?q=`, `GET /patients/{pk}/prescriptions` | ✓ | ✓ | 403 |
| `GET /patients/{pk}/encounters`, `GET /encounters/{pk}` | ✓ | ✓ | 403 |
| `POST /patients/{pk}/encounters`, `POST /encounters/{pk}/close` | ✓ | 403 | 403 |
| `POST /prescribe/preview`, `/prescribe/manual-preview`, `/prescribe/confirm` | ✓ | 403 | 403 |
| `GET /audit/export`, `GET /audit/events` | 403 | ✓ | ✓ |

---

## 3. Endpoint reference

Exact request and response fields are the models in `backend/schemas.py`. "Writes" names database effects beyond the housekeeping in §6.

| Method and path | Request | Success | Errors (besides 401/403) | Writes |
|---|---|---|---|---|
| `GET /health` | — | `200 {"status":"ok"}` | — | none |
| `POST /auth/login` | `LoginRequest` | `200 LoginResponse` | 401, 422 | session row; throttle counters |
| `POST /auth/logout` | no body | `200 {"status":"logged_out"}` | — | revokes the session |
| `POST /patients` | `PatientCreateRequest` (`clinic_patient_id` 1–50 trimmed, no control characters; `display_name` 1–200; `phone` optional 1–32; **unknown fields → 422**) | `201 PatientResponse` | 409 `"A patient with this ID is already registered in this clinic."`, 422 | patient row |
| `GET /patients?q=` | `q` 2–100 after trimming | `200 {"results":[PatientSummary], "truncated"}` (at most 20; no phone) | 422 | none |
| `GET /patients/{pk}/prescriptions?limit=&offset=` | `limit` 1–50 (default 20), `offset` ≥ 0 | `200 PatientHistoryResponse` | 404 `"Patient not found."`, 422 | none |
| `POST /patients/{pk}/encounters` | empty body; any field → 422 | `201 EncounterResponse` | 404, 409 `"This patient already has an open encounter."` | encounter + `ENCOUNTER_OPENED` audit event |
| `GET /patients/{pk}/encounters?limit=&offset=` | as for history | `200 EncounterListResponse` | 404 | none |
| `GET /encounters/{pk}` | — | `200 EncounterResponse` | 404 `"Encounter not found."` | none |
| `POST /encounters/{pk}/close` | — | `200 EncounterResponse` (`CLOSED`) | 404, 409 `"This encounter is already closed."` | status + `ENCOUNTER_CLOSED` audit event |
| `POST /prescribe/preview` | `PreviewRequest` (`patient_name`, `notes` 1–10000, optional `patient_phone`, `patient_id`, `patient_pk`, `encounter_pk`) | `200 PreviewResponse` | see §5.2 | preview row (and the stale-preview sweep) |
| `POST /prescribe/manual-preview` | `ManualPreviewRequest` (`dosha_imbalance` 1–200, `prescribed_herbs` ≥ 1, plus the identity fields above) | `200 PreviewResponse` | see §5.3 | preview row (and the sweep) |
| `POST /prescribe/confirm` | `ConfirmRequest` (`preview_id`; optional `dosha_imbalance` ≤ 200; optional `prescribed_herbs` 1–50 lines); optional `Idempotency-Key` header ≤ 200 chars | `200 PrescriptionResponse` | see §5.4 | prescription, items, stock deduction, audit event, preview `CONFIRMED`, idempotency record; corrections row (AI previews only, after commit) |
| `GET /audit/export?clinic_id=` | — | `200 AuditExportResponse` | 403 (clinic) | none |
| `GET /audit/events?clinic_id=` | — | `200 AuditEventsExportResponse` (`chain_valid`, `integrity_violations`, `events`) | 403 (clinic) | none |

**Herb line** (`PrescribedHerb`, everywhere): `{"name": string ≤ 200, "grams": integer, > 0, ≤ 10000}`.

**Timestamps (CODE):**
- **No timezone suffix on any timestamp.** They are UTC but carry no offset. `created_at` on patients, encounters and history is a `"YYYY-MM-DD HH:MM:SS"` string. `PrescriptionResponse.created_at` and `LoginResponse.expires_at` serialize as ISO strings with no offset, e.g. `"2026-09-28T09:00:00"`.
- **Parse all timestamps as UTC** (see §9, L1).

---

## 4. Workflows

### 4.1 Sign-in
1. Login.
2. Store the token as an opaque string, and never decode it. Where to store it is a frontend security decision. Tab-scoped storage (e.g. `sessionStorage`) limits exposure; keep it out of URLs and logs. Compute expiry from `expires_at` **parsed as UTC** (§9, L1).
3. Send the token on every call.
4. On any 401, clear the session and return to login.
5. Show only the screens the role can use.

### 4.2 Patients
- **Registration:** DOCTOR or ADMIN. `clinic_patient_id` is the clinic's OP number: unique per clinic, **immutable**. No API edits a name or phone (CONTRACT v1.5 E7).
- **Search:** DOCTOR or ADMIN; the query must be at least 2 characters.
- **History:** lists only prescriptions **linked** to that patient. A prescription made without `patient_pk` never appears.
  - `dosha_imbalance` and `prescribed_herbs` may be `null` for an old record whose text can't be parsed; show that as "unknown", never guess.
  - `status` is always `"Active"`; there is no lifecycle (CODE).

### 4.3 Encounters (backend ready; no UI has used them yet, CONTRACT v1.6 F9)
- **Lifecycle:** open (DOCTOR) → `OPEN` → close (DOCTOR) → `CLOSED`, which is final. At most one open encounter per patient.
- **Linking a draft:** pass `encounter_pk` (with `patient_pk`) on preview or manual preview to put the draft inside the encounter. Confirm inherits it; it can't be chosen at confirm.
- **A closed encounter blocks confirm:** `409 "This draft's encounter is closed. Open a new encounter and draft again."`

### 4.4 Prescribing: AI proposal versus confirmed prescription
- **`/prescribe/preview` (AI) and `/prescribe/manual-preview` (doctor-entered, no AI) return a PROPOSAL.**
  - It is identified by `preview_id` and stored server-side for **30 minutes** (CONTRACT §1).
  - **AI preview timing (CODE `agents/copilot.py`):** the whole AI step is capped at **40 s** (`GEMINI_TOTAL_BUDGET_SECONDS`), with a 45 s ceiling per AI request (`GEMINI_TIMEOUT_SECONDS`). Past that the backend answers `502` + `AI_SERVICE_UNAVAILABLE` itself. Give the preview call a client timeout **longer than 45 s** (e.g. 60 s) so that answer, not a client timeout, reaches the user. Typical latency is UNKNOWN (§10).
  - **Nothing is committed, and no stock is deducted.**
  - It is not a prescription and must never be labelled or printed as one.
  - Herb names come back already resolved to canonical formulary names.
  - Preview does **not enforce** stock (CODE). The AI's copilot may *read* the caller's clinic stock through a read-only tool while drafting, but insufficient stock is rejected only at confirm (`409`), which is also the only place stock is deducted (CONTRACT §2).
- **The doctor reviews and may correct.** A correction is sent to confirm as `dosha_imbalance` and/or `prescribed_herbs`. **Omitting a field, or sending `null`, means "use the stored proposal unchanged."** An **empty list is rejected** (§5.4).
- **`/prescribe/confirm` is the ONLY step that creates a prescription.** Only its `200 PrescriptionResponse` (with `prescription_id` and `created_at`) is a confirmed prescription.
  - Duplicate lines of one herb (including aliases) are **merged** into one line; the response shows the merged lines.
  - A preview can be confirmed **once**.
  - A **failed** confirm (400, 409 stock, 422) leaves the preview confirmable, so the doctor can correct and retry without re-running the AI.
- **Session binding:** only the session that created the preview may confirm it; another session gets `403 "This preview belongs to a different session."`

### 4.5 Manual fallback
- **When to offer it:** when preview returns `502` **with** `X-Error-Code: AI_SERVICE_UNAVAILABLE`. That header is readable cross-origin; it's the only exposed header.
- **Rules:**
  - the manual preview requires a dosha and at least one herb;
  - its `preview_id` confirms through the same `/prescribe/confirm`;
  - it is never logged to corrections.

### 4.6 Voice and other input methods
- **The backend accepts clinical notes only as text** (`notes` on `/prescribe/preview`, 1–10,000 characters). It has **no transcription endpoint** and makes no speech call.
- **Any voice input is a new-frontend design decision.** It must **not** put an AI or transcription API key in the browser. A key-bearing speech service needs a backend-mediated design, which is a backend change request (§8).
- **Identity tokenization applies to the text the backend receives.** Audio sent to any third party before it becomes text is outside the backend's PII protection.

### 4.7 Inventory
- **Read-only stock read: `GET /inventory`** (contract v1.7 §6B; DOCTOR and ADMIN, AUDITOR `403`; the clinic is the session's). It returns `{"items": [{"herb_name", "stock_grams"}]}` sorted by herb name, with no batch or potency data. `frontend/` shows it on the Dashboard stock summary and on a read-only Inventory page (herb, grams, and an in-stock / out-of-stock label from grams > 0).
- **There is still no endpoint to restock or adjust stock.** The only stock change is the deduction inside a successful confirm; insufficient stock is `409` at confirm.
- **Stock setup** comes only from seeding scripts. An inventory *editing* UI would need new backend work (§8).

---

## 5. Errors, validation and idempotency

### 5.1 Response body shapes (CODE)
- **Handler errors:** `{"detail": "<string>"}`.
- **Request-validation errors (FastAPI 422):** `{"detail": [{"type", "loc", "msg", ...}]}`, where `detail` is a **list**. **A 422 can come in either shape; handle both.**

### 5.2 `POST /prescribe/preview`
| Status | Detail / header | Meaning |
|---|---|---|
| 400 | `"Herb '<name>' is not in inventory."` | the AI named an unknown herb |
| 404 | `"Patient not found."` / `"Encounter not found."` | `patient_pk` or `encounter_pk` is not in the caller's clinic |
| 409 | `"This encounter belongs to a different patient."` / `"This encounter is closed."` / `"This encounter is no longer open for this patient."` | encounter checks (v1.6 F5) |
| 422 | `"Invalid AI schema returned"` | unusable AI reply: malformed, echoed a patient token, identity in the dosha, or **zero herbs** (v1.6 F10, F12) |
| 422 | `"encounter_pk requires patient_pk."` or a request-validation list | input |
| 500 | `"Internal privacy safeguard triggered; request aborted before reaching the AI service."` | PII pre-flight abort; no AI call was made |
| 502 | `"AI parsing failed"` + `X-Error-Code: AI_SERVICE_UNAVAILABLE` | AI unreachable, rate-limited or timed out → **offer manual entry** |
| 502 | `"AI response could not be safely de-tokenized; request aborted."` (no header) | not an outage; do not auto-offer manual entry for this reason |

### 5.3 `POST /prescribe/manual-preview`
- **400:** unknown herb.
- **404, 409:** patient and encounter checks, as for preview.
- **422:** `"Dosha must not contain patient identity (name, phone or ID)."`, or request validation. That includes no herbs, and grams that aren't a whole number from 1 to 10,000.

### 5.4 `POST /prescribe/confirm`
Precedence (CONTRACT v1.4 D1, extended by v1.6):

`401` → `403` role → `409` idempotency conflict → `404` preview missing, expired or another clinic's → `403` different session → `409` already confirmed → validation and business errors → `200`.

| Status | Detail |
|---|---|
| 400 | `"Herb '<name>' is not in inventory."` (a corrected herb) |
| 403 | `"This preview belongs to a different session."` |
| 404 | `"Preview not found or expired. Please re-run /prescribe/preview."` → send the doctor back to draft again |
| 409 | `"Preview '<id>' has already been confirmed."` |
| 409 | `"Insufficient stock for herb '<name>'. Requested: <n>g."` (nothing was deducted) |
| 409 | `"This draft's encounter is closed. Open a new encounter and draft again."` |
| 409 | `"Idempotency key '<key>' was already used with a different request."` / `"… is no longer valid for the current authorization context."` |
| 422 | `"A prescription needs at least one herb. Add a herb and confirm again."` (empty stored proposal) |
| 422 | a request-validation list with `type: too_short` on `prescribed_herbs` (an empty override) |
| 422 | `"Total for herb '<name>' is <n> g after combining duplicate lines; the maximum is 10000 g."` |
| 422 | `"Dosha must not contain patient identity (name, phone or ID)."` |
| 500 | `"Transaction failed due to an internal error."` (rolled back) |

**Every non-200 from confirm writes nothing:** no prescription, no stock change, and no idempotency record (CONTRACT v1.6 F5, F12).

### 5.5 Any endpoint
- **503:** `{"detail": "The system is busy. Nothing was saved. Please try again."}` with `X-Error-Code: DATABASE_BUSY` (`Retry-After: 5` is sent but **not readable cross-origin**, §9 L2). Safe to retry.
- **500:** `{"detail": "Internal server error"}`.

### 5.6 Idempotency (CONTRACT v1.4)
- **Send one fresh `Idempotency-Key` per confirm attempt** (a random UUID, at most 200 characters), and **reuse the same key and identical body** when retrying after a timeout or dropped connection.
- **Same key, same body after a success:** replays the original `200` byte-for-byte, with no second prescription or deduction.
- **Same key, different body:** `409`.
- **A rejected confirm stores no key**, so the corrected request may reuse it.

---

## 6. Read-only versus writing operations

- **Clinical and inventory writes happen only in:**
  - `POST /prescribe/confirm`;
  - `POST /patients`;
  - `POST /patients/{pk}/encounters` and `POST /encounters/{pk}/close`;
  - preview creation (preview rows only).
- **Account writes:** login (session and throttle) and logout (revoke).
- **No clinical writes:** every `GET`.
- **Housekeeping writes (CODE):**
  - **every authenticated request** makes a best-effort `UPDATE sessions SET last_seen_at`;
  - both preview endpoints mark stale previews `EXPIRED`.

  So no authenticated call is strictly write-free at the database level. Only `GET /health` touches no database at all (CONTRACT v1.6 F1).

---

## 7. Expected UI states

| State | When | Expected UI |
|---|---|---|
| Loading | Any request in flight; preview can take tens of seconds (AI call, §4.4) | Disable the submit action; prevent double-submit of confirm (an in-flight guard plus the idempotency key) |
| Empty | Search with no results; history, encounters or audit with no rows | Explicit empty message, not an error |
| Proposal | After preview or manual preview | Clearly labelled **draft / AI proposal (not saved)**, editable, with its 30-minute expiry in mind |
| Confirmed | After confirm `200` | Show `prescription_id`, `created_at` and the **merged** herb lines from the response, not the edited form |
| Validation | 400, 422 (either shape), 409 duplicate patient | Field-level where `loc` allows; keep the user's input |
| Authorization | 403 role or clinic | "Not permitted", with no retry loop; hide actions by `role`, but still handle 403 |
| Not found / expired | 404 (patient, encounter, preview) | Preview 404 → back to drafting; others → not found |
| Conflict | 409 (stock, already confirmed, encounter, idempotency) | Specific message per detail; already-confirmed → show the confirmed result if known, never re-confirm |
| AI outage | 502 + `AI_SERVICE_UNAVAILABLE` | Offer manual entry |
| Session expiry | Any 401 | Clear the session and re-login; unsaved drafts are server-side for 30 min, but session-bound (a new session can't confirm the old draft) |
| Busy | 503 `DATABASE_BUSY` | "Nothing was saved, try again" |
| Server / network error | 500, 5xx, fetch failure or timeout | Generic error; for a confirm with an unknown outcome, retry with the **same** key and body |

---

## 8. Ownership and the boundary for backend changes

- **Frontend and UI: Member 2.**
  - The v0.1 demo frontend is **`frontend/`** (decision D-03; no migration authorized). `apps/web/` exists only as a README placeholder. Any frontend depends on the backend **only through HTTP and the contract**.
  - The original plan (below) was a from-scratch frontend in `apps/web/` that chooses its own framework and tooling; it was not built.
- **Legacy Streamlit UI: retired, not reused.**
  - `ui/app.py`, `.streamlit/config.toml`, and the backend test files that exercise it (`tests/test_ui_*.py`, `tests/test_streamlit_config.py`).
  - Their removal or archiving is a separate, approved step.
  - **Do not copy, extend or modify them, and do not change the backend to suit them.**
- **Recommended structure for the new frontend** (a suggestion, not a requirement):
  ```
  apps/web/
  ├── src/api/        one typed client written from the contract: base URL, bearer header,
  │                   both 422 shapes, X-Error-Code, Idempotency-Key, UTC timestamp parsing
  ├── src/session/    token storage and expiry (parse as UTC)
  ├── src/features/   auth · patients · prescribe (draft → review → confirm) · encounters · audit
  ├── src/ui/         the design system
  ├── tests/          unit tests; end-to-end tests against a mocked API built from contract
  │                   fixtures, plus a smoke run against a non-production backend
  └── .env.example    API base URL only -- no AI, transcription or backend secrets
  ```
  Serve it in development from `http://localhost:3000` or `http://127.0.0.1:3000`, the only CORS-allowed origins. Any other origin needs an approved backend CORS change.
- **The backend is not changed from the frontend side.** Per `CLAUDE.md`, backend, auth, RBAC, inventory, audit, schema, migrations and clinical-AI behaviour change only with explicit authorization. A frontend need for a new endpoint or field (for example inventory editing, encounter UI support, patient editing) goes through:
  1. a written request;
  2. backend implementation;
  3. an update to `AyurCore_API_Contract.md` with a new version (1.7+) and change-log entry.

  Never infer backend behaviour from the UI's needs.
- **Protected invariants the UI must not work around:**
  - confirm is the only commit;
  - clinic and role come from the session;
  - the preview is session-bound;
  - confirmed prescriptions are immutable (no edit or delete API exists).

---

## 9. Known limitations and risks relevant to the frontend

- **L1, timestamps without timezone (CODE):**
  - `LoginResponse.expires_at` is UTC, serialized without an offset.
  - JavaScript's `new Date("2026-09-28T09:00:00")` reads an offset-less date-time as **local** time. In IST (UTC+5:30) a naive client would treat a session as expired 5.5 h early (the server still enforces the real expiry).
  - **Parse every backend timestamp as UTC** (e.g. append `Z` before parsing). A timezone-aware response would need a backend change and a contract update (§8).
- **L2, headers:** `Retry-After` is not exposed by CORS, so a browser can't read it. Use `X-Error-Code: DATABASE_BUSY` to recognise a busy database, and a fixed short retry delay.
- **L3, deployment:** CORS allows only `localhost:3000` and `127.0.0.1:3000`. The deployed frontend origin, HTTPS and hosting are not configured (risk register R4, R7; `docs/HANDOFF.md`).
- **L4, encounters:** encounters and `encounter_pk` exist in the API but in no UI.
- **L5, data gaps:** no inventory write API (`GET /inventory` is read-only); no patient edit API; no prescription lifecycle (`status` always `"Active"`).
- **L6, error wording:** for an empty AI proposal, the UI shows its "reply couldn't be read" message (the wording is imprecise; the advice is correct).
- **L7, operational and clinical:**
  - not clinically validated (R3);
  - live Gemini path never exercised (R8);
  - encrypted backup not independently verified (R10);
  - development database contains accidental test writes (R9).
- **L8, no frontend verification exists yet** for the new UI. Integration must be proven by the §11 checklist against a non-production backend, not by any legacy UI's tests.

## 10. UNKNOWN — not established by the contract or code review; verify before relying on it

| Item | How to verify |
|---|---|
| Typical preview latency (the **caps** are known: 40 s total, 45 s per AI request; §4.4) | Measure against a non-production backend with a real key; the live Gemini path has never been exercised (R8) |
| Voice-input design, if wanted | Product and backend decision (§4.6); needs a backend change request |
| Production origin, HTTPS and hosting | Owner decision; requires a backend CORS change and deployment work |
| Product requirements for encounter UI, and whether `patient_pk` / `encounter_pk` become required | Owner decision (CONTRACT v1.5 E7, v1.6 F9) |
| Exact shape of `event_data` inside audit events, beyond the keys listed in CONTRACT v1.6 F6 | `backend/audit_store.py` (`build_prescription_confirmed_event_data`, `build_encounter_event_data`) |
| Behaviour under concurrent tabs of the same user beyond session binding and idempotency | Test against a non-production backend |

## 11. Frontend acceptance checklist

For each screen, verify against a **non-production** backend with a fresh, migrated and seeded database (the procedure in `CLAUDE.md`), never the live database:

- [ ] **Loading:** controls disabled while pending; preview shows progress; confirm cannot double-submit.
- [ ] **Empty:** search with no match, a patient with no history or encounters, and empty audit lists each show an empty state.
- [ ] **Success:** registration shows the new patient; confirm shows the **response's** `prescription_id`, `created_at` and merged herbs.
- [ ] **AI proposal vs confirmed:** preview and manual preview results are labelled as an unsaved proposal; only a confirm `200` is presented as a prescription; nothing is printed or shared from a proposal.
- [ ] **Validation:** both 422 shapes are rendered; 400 unknown herb; 409 duplicate patient; an empty herb list can't be submitted, and is handled if the server rejects it.
- [ ] **Authorization:** each role sees only its actions; a forced 403 is handled without looping; AUDITOR sees no patient data.
- [ ] **Session expiry:** a 401 anywhere clears the session and returns to login; expiry time is parsed as UTC (L1); logout works, and a reused token is rejected.
- [ ] **Not found / expired:** preview 404 returns to drafting; patient and encounter 404s are handled.
- [ ] **Conflicts:** insufficient stock, already confirmed, closed encounter and idempotency conflict each have a specific message; a retry after a timeout reuses the same `Idempotency-Key` and body.
- [ ] **AI outage:** a 502 with `AI_SERVICE_UNAVAILABLE` offers manual entry; a 502 without the header does not claim an outage.
- [ ] **Server error:** 500, 503 (`DATABASE_BUSY`) and network failures show a safe message and never imply a prescription was saved.

## 12. Resuming frontend work later

1. **Start from a known state:** `git log --oneline` should include the backend baseline `12f2e6a` (or a later, reviewed commit), and `git status` should be clean. Read `README.md`, this document, `docs/HANDOFF.md` and the risk register. **Don't open or study `frontend/` or `ui/`.**
2. **Treat the contract as the interface:** `AyurCore_API_Contract.md`, with this document as the practical guide. The backend's own models are in `backend/schemas.py`; if anything disagrees, the backend code is authoritative, so ask the backend owner to correct this document.
3. **Run your own non-production backend:** `db/database.py` always uses `db/ayurcore.db` **inside the checkout it runs from**, so use a **separate copy** of the backend, never the clinic's working copy or its running API.
   - In that copy: install with `pip install -r requirements.txt`, create a fresh database (`python -c "from db.database import init_db; init_db()"`), seed stock (`python db/seed.py`) and, if you want demo accounts, run `python scripts/seed_demo_data.py` (`docs/DEMO_DATA.md`).
   - Then start `uvicorn` as in `README.md`.
   - **Never run these seed scripts against the clinic's database.**
4. **Create the new frontend in its own directory** (§8), serve it from `localhost:3000`, and build the API client first (§3–§5, §9 L1–L2). Then implement the workflows (§4) and prove each screen against the §11 checklist.
5. **Record any needed backend change as a request** (§8); don't implement backend changes from the frontend task.
