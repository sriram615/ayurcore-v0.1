# AyurCore OS — clinic workflow demonstration

**Demonstration context:** Athma Ayurveda Clinics, Kerala  
**Build:** v0.1 · **Data:** synthetic demonstration records only

AyurCore OS is an AI-assisted clinic operations system designed around the day-to-day workflows of an Ayurvedic clinic. This public repository presents a reproducible demonstration build for reviewing those workflows in the context of Athma Ayurveda Clinics, Kerala.

> **Important:** This is a software demonstration, not a live clinic deployment or a clinically validated product. It has not been connected to Athma Ayurveda Clinics' systems and has not been used with real patient records. All patient and inventory data shown in the demo are synthetic. The reference to Athma Ayurveda Clinics identifies the intended demonstration context; it does not imply clinic endorsement, approval or production use.

![AyurCore demonstration: dashboard stock summary, inventory, synthetic patient history and a cited answer from the patient Q&A panel](docs/assets/ayurcore-v0.1-demo.gif)

*Short recording of the running demonstration using synthetic data: Dashboard, Inventory, a synthetic patient's history and a record-grounded answer.*

## What the demonstration covers

The current build brings together prescription, patient-record and inventory workflows behind a FastAPI backend and a Next.js interface.

| Area | Demonstrated capability | Current boundary |
|---|---|---|
| Prescription workflow | Draft → doctor review → deterministic confirmation; stock checks, idempotency and audit logging | The AI-assisted draft path can use Gemini. Confirmation is deterministic and makes no AI call. |
| Patient records | Patient search, recorded prescription history and visit information | The demo uses invented patient records. |
| AI-2 — Ask about this patient | Answers supported questions from one patient's recorded prescriptions and visits, with record citations and verification; supports English, Manglish and Malayalam question forms | Read-only; closed-vocabulary scope. It does not provide diagnosis, treatment or dosage advice. |
| AI-3 — Consultation Brief | Produces a concise, cited summary from verified patient-history facts using a learned salience ranker | Synthetic evaluation only; not a clinical assessment. |
| Inventory | Clinic-scoped stock view and stock checks during prescription confirmation | Demonstration inventory only. |
| Access and audit | Role-based access, clinic isolation and an append-only, hash-chained audit trail | Not a substitute for a production security, hosting or compliance review. |

**Core design boundary:** AI may prepare a draft or retrieve recorded information. A doctor remains responsible for clinical decisions; deterministic backend validation governs confirmation and writes. The read-only AI-2 and AI-3 components do not write to patient records.

AI-1, a local notes-to-structured-prescription extractor, is an evaluated research component and is **not wired into the product workflow**.

## Demonstration walkthrough

The included browser demo is intended to let a reviewer explore a typical clinic workflow without using real patient information.

1. **Dashboard and inventory** — review the synthetic stock summary and inventory list.
2. **Patient record** — open synthetic patient DEMO-0101 and inspect the dated prescription and visit history.
3. **Record-grounded questions** — ask what was prescribed first or what changed between prescriptions. Answers cite the underlying records.
4. **Unknown information** — open DEMO-0103 and ask which herbs were prescribed. The system reports that the information is not recorded rather than inventing an answer.
5. **Safety boundaries** — try a dosage-advice question or a request to view another patient's records. These are refused.
6. **Role boundaries** — sign in with the demo roles to see which functions are available to each role.

See the [step-by-step demo guide](docs/V01_DEMO_GUIDE.md) for the complete walkthrough and API journey.

## Run the demonstration locally

The demo uses a disposable SQLite database inside this checkout. The setup scripts seed invented records and refuse to use the development database. No external API key or network call is required for the local AI-2 and AI-3 demonstration paths after dependencies are installed.

**Requirements:** Python 3.14, Node.js and pnpm. The recorded environment used Python 3.14.3, Node 24.3 and pnpm 12.5.

    git clone https://github.com/sriram615/ayurcore-v0.1.git
    cd ayurcore-v0.1

    python3.14 -m venv venv
    ./venv/bin/pip install -r requirements-dev.txt

    ./venv/bin/python scripts/setup_v01_demo.py
    ./venv/bin/python scripts/seed_v01_demo_histories.py

Start the API:

    ./venv/bin/python scripts/run_v01_demo_server.py

In a second terminal, start the frontend:

    cd frontend
    pnpm install
    pnpm build
    pnpm start -p 3000 -H 127.0.0.1

Open http://127.0.0.1:3000. Demo credentials are generated in demo/credentials.v01-demo.json; the file is git-ignored. Open it locally when signing in and never include it in a recording or commit.

For a terminal-only demonstration of AI-2 and AI-3:

    ./venv/bin/python scripts/demo_v01_ai.py

For setup details, example questions, API walkthroughs and safe test commands, see [V01_DEMO_GUIDE.md](docs/V01_DEMO_GUIDE.md).

## Current implementation and evidence

This repository is a standalone demonstration snapshot. Its README and supporting documents distinguish implemented behaviour from evaluation-only components and work that remains planned.

| Area | Evidence currently documented | Limitation |
|---|---|---|
| Backend and AI tests | 862 passed in an isolated scratch sandbox on the source development tree | 8 additional AI-1 tests were not runnable in that environment because NumPy was absent. This result was not rerun on this public snapshot. |
| Frontend | 530/530 mocked-API Playwright tests passed on a scratch export of the development tree; 12/12 real-backend Ask-flow tests passed in an earlier run | The mocked suite does not establish live-backend integration for every screen. Inventory integration and accessibility have not been fully verified. |
| AI-2 | Registered development-set checks passed for the recorded 176-case development run | The development set was used during tuning. This is not independent generalisation evidence; validation and acceptance work remains outstanding. |
| AI-3 | On 988 held-out synthetic patients, the learned ranker missed 0.0% of policy-labelled critical facts at k=5 versus 56.5% for the fixed-order baseline; no unsupported facts were shown | Labels reflect a written selection policy by the same author. This measures policy reproduction, not clinical usefulness. |

These figures are historical evidence from the development tree, not claims that every test has been rerun against this public snapshot. See the [evaluation report](docs/V01_EVALUATION_REPORT.md) for the claim-by-claim qualifications.

## What is not yet claimed

- No clinical validation or clinician-approved clinical rules.
- No live deployment, production hosting or integration with Athma Ayurveda Clinics.
- No use of real patient data in this demonstration.
- No production security, privacy, regulatory or compliance certification.
- No automated clinical decision-making or dosage recommendation.
- AI-1 is not part of the live prescription workflow.
- Phase 3B guardrail enforcement has not been implemented.
- The planned CRM application for interaction history, lead/patient follow-ups and permission-controlled communications is a future extension, not part of this v0.1 demonstration.

A clinic pilot would require clinician-led validation, approved clinical rules, operational security and backup controls, deployment hardening, and explicit clinic approval before any real records are used.

## Architecture and project documentation

- [Architecture overview](docs/V01_ARCHITECTURE.md)
- [Demo guide](docs/V01_DEMO_GUIDE.md)
- [Evaluation report](docs/V01_EVALUATION_REPORT.md)
- [Technical risk register](docs/TECHNICAL_RISK_REGISTER.md)
- [Phase 3 plan](docs/PHASE_3_PLAN.md)
- [API contract](AyurCore_API_Contract.md)
- [Engineering instructions](CLAUDE.md)

The existing roadmap and master-plan filenames are retained to avoid breaking document links; their descriptions are being reframed around demonstration readiness rather than personal-portfolio presentation.
