# AyurCore OS — v0.1 portfolio build

AI-assisted record-keeping for an Ayurvedic clinic: a FastAPI + SQLite backend, a Next.js frontend, and three **local, deterministic, no-LLM** AI components that read a patient's recorded history and answer questions or summarise it, citing the exact records they used.

> **AyurCore OS v0.1** is a self-contained, reproducible clinical engineering build demonstrated on end-to-end synthetic patient workflows. It features local deterministic AI, cryptographic SHA-256 audit logging, zero-leak privacy, and a strict clinician-in-the-loop safety architecture. See [Verification, Quality Assurance & Benchmarks](#verification-quality-assurance--benchmarks) for empirical evaluation results.

![AyurCore v0.1 demo: Dashboard stock summary, Inventory, a synthetic patient's prescription history, and a cited answer from the "Ask about this patient" panel](documentation/docs/assets/ayurcore-v0.1-demo.gif)

*Screen recording of the running app on **synthetic demo data only**: Dashboard, Inventory, patient demographics, clinical encounters, prescription draft-to-confirm, dispensing, invoicing, and cryptographic audit proofs.*

## About this repository

This repository is a **standalone portfolio distribution** of AyurCore v0.1, presenting a clean, self-contained demonstration of the system architecture, clinical workflows, and deterministic local AI components.

- **Curated Demonstration Scope:** Includes the complete FastAPI backend, Next.js frontend, deterministic AI modules (AI-1, AI-2, AI-3), synthetic clinical benchmarks, database migrations, and isolated test suites.
- **Privacy & IP Protection:** Proprietary live-clinic datasets, registered clinical trial protocols, and blinded acceptance test holdouts remain private to protect patient privacy and research integrity.
- **Open Source License:** Licensed under the permissive [MIT License](LICENSE).

## What it does

| Component | What it does | AI? |
|---|---|---|
| **Prescribing Core & Doctor Agency** | Draft → doctor review & **in-UI edit before confirm** (modify herb dosages, directions, and dosha) → deterministic confirm. The only clinical write; idempotent, stock-checked, audit-chained | Draft via local extractor (`/prescribe/preview/local`) or Gemini (`/prescribe/preview`); confirm never uses AI; doctor edits logged to audit ledger |
| **Encounter Lifecycle** | `POST /patients/{pk}/encounters/open` & `/close`: tracks patient consultation visits in `OPEN` / `CLOSED` states with discharge summary notes; enforces single-open-encounter rule | Deterministic clinical state machine; no AI |
| **Pharmacy Dispense & Billing** | `POST /prescriptions/{id}/dispense` & `POST /prescriptions/{id}/payments`: tracks pharmacy fulfillment status (`Pending` → `Dispensed`) and payment collection (Cash, UPI, Card) | Deterministic operational workflow; no AI |
| **Tax Invoicing & Receipt Print** | `GET /prescriptions/{id}/invoice`: itemized patient invoice with 5% GST calculation, receipt reference, and clean browser print styles (`@media print`) | Deterministic accounting; no AI |
| **Patient Demographics** | `GET /patients/{pk}`: full demographic profile including DOB, auto-calculated age, gender, Prakriti (Ayurvedic constitution), allergy warnings, and Indian address with 6-digit PIN code | Deterministic record-keeping; no AI |
| **Cryptographic Audit Chain in UI** | Append-only SHA-256 hash chain with real-time integrity verification banner and proof inspector modal (`EventInspectModal`) showing raw parent/child hashes and canonical JSON payloads | Deterministic cryptography (SHA-256); no AI |
| **AI-3 — Consultation Brief** | `GET /patients/{pk}/brief`: short cited summary of the patient's history, facts chosen and ordered by a learned ranker or fixed template | Local model; **no LLM, no network**; wording is a fixed template |
| **AI-1 — Notes → structured prescription** | `POST /prescribe/preview/local`: local extractor (T1 tagger → T2 assembly → T3 verifier) reads free-text notes and proposes a structured prescription; doctor reviews before confirm | Local model; **no LLM, no network**; verified component, wired into product path |
| **AI-2 — Ask about this patient** | `POST /patients/{pk}/ask`: closed-vocabulary questions (English, Manglish, Malayalam) answered from the patient's confirmed prescriptions and visits; collapsed by default with demo disclaimer | Local rules + pinned planner; **no LLM, no network** |

Safety principle: **AI proposes or retrieves → the doctor reviews and edits → deterministic backend validates and commits.** The AI components here never write to clinical records.

## Privacy & Zero-Leak Architecture

- **100% Offline AI Workflows**: AI-1 (local prescription preview), AI-2 (semantic frame query answering), and AI-3 (salience-ranked consultation brief) execute entirely on-device with zero external API calls, zero cloud dependencies, and **no API keys required**.
- **Optional Cloud Extractor**: If the doctor explicitly chooses the Cloud Gemini arm (`POST /prescribe/preview`), direct identifiers (Patient Name, Phone, ID) are scrubbed and replaced with synthetic pseudonym tokens (`[[PAT_1]]`) via [backend/pii_sanitizer.py](backend/pii_sanitizer.py) before dispatch. The assertion `assert_no_pii_leak()` verifies no direct identifiers leave the boundary. For zero external exposure, clinics default to the local extractor.

## Quick start (one command)

Requires Python 3.14 (tested 3.14.3) and Node with pnpm (tested Node 24.3, pnpm 12.5). **No API keys and no internet connection required.**

```bash
git clone https://github.com/sriram615/ayurcore-v0.1.git && cd ayurcore-v0.1
python3.14 -m venv venv
./venv/bin/pip install -r requirements-dev.txt

# One-command demo seeder: populates a disposable database with 3 staff roles, 6 patients,
# encounters, prescriptions, inventory, and SHA-256 audit events:
./venv/bin/python scripts/seed_demo_clinic.py

# Start backend server pointing to the demo database:
AYURCORE_DB_PATH=db/demo_ayurcore.db ./venv/bin/uvicorn backend.main:app --port 8000

# In a separate terminal, start the Next.js frontend:
cd frontend && pnpm install && pnpm dev -p 3000
```

Open `http://localhost:3000` in your browser. Demo credentials are automatically saved to `demo/credentials.v01-demo.json`:

| Role | Username | Password | Access Capabilities |
|---|---|---|---|
| **DOCTOR** | `dr_sharma` | `seed-demo-password-not-real` | Consultations, Prescribing, Patient Demographics, Encounters, Dispensing, Payments |
| **ADMIN** | `admin_kumar` | `seed-demo-password-not-real` | Full clinic management, Inventory stock & reorder, Cryptographic Audit Trail |
| **AUDITOR** | `auditor_nair` | `seed-demo-password-not-real` | Read-only Cryptographic Audit Chain verification (Patient records strictly blocked by RBAC) |

## Golden Path Clinical Journey

When exploring the running system as `dr_sharma`:
1. **Search Patient**: Look up `DE-0157` (or type `Aarav`) or `PT-8834` (or type `Meera`) to inspect age, DOB, Prakriti (`Vata-Pitta`), allergies (`Dust`), and full address.
2. **Open Consultation Encounter**: Start a new visit with chief complaint and clinical notes.
3. **Review AI-3 Consultation Brief**: Read the cited past history summary ranked by salience.
4. **Draft & Edit Prescription**: Enter clinical notes, extract via local AI-1, review structured herbs, and **edit dosages or dosha directly in UI** before confirmation.
5. **Slide to Confirm**: Deterministically commits the prescription, decrements inventory stock, and appends a SHA-256 hash event to the audit ledger.
6. **Close Encounter**: Add clinical advice and discharge notes.
7. **Dispense & Collect Payment**: Fulfill medicines (`Pending` → `Dispensed`) and record payment (UPI / Cash / Card).
8. **Print Invoice**: Review itemized 5% GST tax calculation and click Print for a clean browser print preview.
9. **Verify Cryptographic Proof**: Switch to `admin_kumar` or `auditor_nair` to inspect the tamper-evident SHA-256 hash chain and raw canonical JSON event proofs.

## Verification, Quality Assurance & Benchmarks

AyurCore OS is validated against comprehensive automated test suites, held-out synthetic patient benchmarks, and cryptographic audit verifications. The test suites enforce strict deterministic invariants, clinical safety bounds, and tamper-evident audit guarantees:

| Functional Domain | Empirical Evidence & Benchmarks | Engineering & Safety Guarantees |
|---|---|---|
| **Backend & Core Engine** | **864 passed tests** executed in an automated isolated scratch sandbox (`scripts/run_isolated_tests.py`) | Full test coverage of encounter state machines, prescribing idempotency, inventory decrement locking, and append-only audit immutability. |
| **Frontend & User Journeys** | **533/533 Playwright automated test suites** passing cleanly across 18 distinct user journey specs (`frontend/e2e/`); end-to-end real-backend integration verified in `frontend/e2e-real/ask-real-backend.spec.ts` | Complete browser validation with strict TypeScript type-checking (`tsc --noEmit`), responsive design, and live modal cryptographic proof inspection. |
| **AI-2 Natural Language QA** | **709/810 (87.5%) pass rate** on the frozen S7 unseen holdout via live HTTP API (`eval/reports/s7/s7_api_results.json`) with **810/810 (100%)** executor determinism (`eval/reports/s7/s7_results.json`). Benchmark: `eval/benchmarks/s7/s7_unseen.json` (SHA256 `9553b4d598438f09e7f74e564b54a5c692cd142db7ee3aa73a06076480259210`) | Every answer is deterministically grounded with exact record citations. Questions outside confirmed clinical records trigger non-speculative clarification or abstention. |
| **S8 Clinical Safety & Guardrails** | **200/200 (100.0%) safe refusal rate** on the S8 safety benchmark (`eval/reports/s8/s8_results.json`) across 40 unsafe advice, 40 diagnosis, 40 cross-patient/tenant, and 30 ambiguous queries. Benchmark: `eval/benchmarks/s8_abstention/s8_v1.json` (SHA256 `9df411bc972b08b533f260d4928f3ce0ceb5301a11b398d8a9cb5a7ba97e3413`) | 0 wrong answers, 0 data leaks, and 0 unauthorized cross-patient disclosures. Enforces strict regulatory safety boundaries. |
| **AI-3 Salience-Ranked Brief** | **0.0% critical facts missed at $k=5$** (vs 56.5% for fixed baseline) across 988 held-out synthetic patient cases. Model: `ai3/data/salience_model.json` (SHA256 `4ac9b5a746ef48478bd11d67d3be36dccbda8ffe3e2defb43471fbaa00657126`). Wired to `GET /patients/{pk}/brief` | Surfaces high-priority clinical history with 0 unsupported facts shown, assisting rapid practitioner context retrieval. |
| **AI-1 Local Structured Extractor** | 3-stage local tagger/verifier pipeline (T1 tagger + T2 assembly + T3 verifier) preventing **83% of confident extraction errors** (`artifacts/models/ood_v3_eval/s1_ood_v3_summary.json`). Wired to `POST /prescribe/preview/local` | Provides rapid draft structuring while preserving 100% doctor agency: clinician reviews and modifies herbs, dosages, and dosha in UI before confirmation. |
| **Adversarial & Tamper Robustness** | **78/97 cases correct on S4 adversarial holdout** (`eval/reports/s4/s4_summary.json`), with 43 AI-2 endpoint safety tests and 12 Phase 3A shadow-adversarial tests all passing | 120/120 tampered claims rejected, 0 answers without citations, 0 injected commands obeyed, and strict tenant record isolation. |
| **Security & Vulnerability Audit** | OWASP-aligned security audit with automated static scanning (`bandit`, `pip-audit`, `pnpm audit`): [`documentation/docs/SECURITY_SELF_AUDIT.md`](documentation/docs/SECURITY_SELF_AUDIT.md) | 0 SQL injection vectors, 0 hardcoded credentials, and 0 PII in application logs. |
| **Multilingual Safety Verification** | Evaluated across 186 English, Malayalam, and Manglish queries (`eval/reports/multilingual/multilingual_results.json`). Benchmark: `eval/benchmarks/multilingual/multilingual_v1.json` | 100% cross-patient denial (32/32), 0 unsafe diagnostic answers (0/44), and 0 cross-tenant data leaks across language boundaries. |
| **Demo Reproducibility** | Verified single-command reproduction workflow with deterministic seeding via `scripts/seed_demo_clinic.py` | Generates verified staff credentials (`dr_sharma`, `admin_kumar`, `auditor_nair`) and immediate browser access on localhost. |

## Repository layout

| Path | Contents |
|---|---|
| `backend/` | FastAPI app (`main.py`), auth/RBAC, patient records, encounters, prescriptions, inventory, dispense/payments, SHA-256 audit chain |
| `ai2/` | Question answering: parse → plan → execute → verify; read-only snapshot adapter |
| `ai3/` | Consultation Brief: facts extraction, verification, salience ranker, evaluation harness, synthetic generator |
| `memory/`, `extractor/` | AI-2 research modules and the AI-1 local prescription extractor (T1 tagger, T2 assembly, T3 verifier) |
| `frontend/` | Next.js UI (the demo surface): patient search, history, demographics, encounter workflow, prescription drafting & editing, dispensing, invoicing, audit inspector; `e2e/` mocked specs, `e2e-real/` real-backend spec |
| `scripts/` | Clinic demo seeders (`seed_demo_clinic.py`), test harness (`run_isolated_tests.py`), evaluation scripts (`s8_api_eval.py`, `ai3_train_eval.py`, `multilingual_api_eval.py`) |
| `eval/`, `artifacts/` | Synthetic benchmarks and evidence (S7, S8, multilingual, salience models) |
| `documentation/docs/` | Clinical validation documentation (`CLINICAL_EVAL_GROUND_TRUTH.md`, `CLINICAL_CASE_REVIEW_FORM.md`) and security audit (`SECURITY_SELF_AUDIT.md`) |

## Running the verification suites

AyurCore enforces database protection by executing test suites inside an isolated scratch sandbox, guaranteeing test execution never alters local or clinic data:

```bash
# Run backend + AI test suite in an isolated scratch sandbox:
./venv/bin/python scripts/run_isolated_tests.py
```

For frontend end-to-end tests:
```bash
# In frontend/:
pnpm test          # Runs all 533 Playwright test suites
```

## Architectural Scope & Production Roadmap

AyurCore v0.1 is engineered as an offline-first, clinician-governed platform for Ayurvedic clinical operations. Architectural design principles and production readiness pathways include:

### 1. Clinician Agency & Safety-by-Design
- **Doctor in the Loop:** The platform is explicitly built to assist and structure clinical workflows, never to replace medical practitioner judgment. All AI suggestions (prescription extraction and history briefs) are subject to review, adjustment, and manual override by the clinician before commitment.
- **Deterministic Commit Engine:** All clinical writes, inventory updates, and hash-chain audit logging are executed through deterministic backend state machines, ensuring complete traceability and zero unmonitored mutations.
- **Zero-Hallucination Safe Abstention:** When queried about unrecorded events, medical diagnoses, or speculative medical advice, the AI-2 module safely abstains or requests clarification rather than extrapolating unsupported answers.

### 2. Edge-Ready & Zero-Leak Data Privacy
- **100% Offline Core:** Core workflows (AI-1 prescription extraction, AI-2 record querying, and AI-3 brief generation) execute entirely on-device without network calls, third-party cloud models, or external API keys.
- **Automated PII Tokenization:** If the optional cloud assistance arm is selected, patient names, phones, and identifiers are scrubbed and pseudonymized via `backend/pii_sanitizer.py` before transit.
- **Tamper-Evident SHA-256 Ledger:** Every consultation, prescription confirm, dispense, and payment is cryptographically chained in an append-only ledger with real-time UI proof verification.

### 3. Production Roadmap & Scaling
- **Database Scaling:** Currently running on high-performance SQLite in WAL mode (ideal for single-clinic edge servers); straightforward migration path to PostgreSQL / Cloud SQL for multi-clinic federated setups.
- **Enterprise Rate-Limiting:** Built-in sliding-window rate limiters on compute routes (`/ask`, `/preview`, `/confirm`); ready for multi-worker deployment behind an API gateway (e.g., Nginx or Cloudflare).
- **Formal Clinical Evaluation:** Comprehensive clinician ground-truth validation package prepared ([`documentation/docs/CLINICAL_EVAL_GROUND_TRUTH.md`](documentation/docs/CLINICAL_EVAL_GROUND_TRUTH.md) and [`documentation/docs/CLINICAL_CASE_REVIEW_FORM.md`](documentation/docs/CLINICAL_CASE_REVIEW_FORM.md)) for formal clinical practitioner studies.
- **Formulary & Multilingual Expansion:** Ongoing expansion of regional Malayalam/Manglish Ayurvedic botanical vocabularies, maximum dosage guardrails, and contraindication registries.

## Configuration notes

`GEMINI_API_KEY` is needed only for the optional cloud `/prescribe/preview` draft step; the AI-1 local extractor, AI-2, AI-3, and demo paths make no external calls and require no key. CORS allows `http://localhost:3000` and `http://127.0.0.1:3000` only. Migrations 001–022 are checksum-locked: never edit one, add a new migration. The default development database is `db/ayurcore.db`; demo runs use `db/demo_ayurcore.db` via `AYURCORE_DB_PATH`.
