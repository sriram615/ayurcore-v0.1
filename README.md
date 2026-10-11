# AyurCore OS — v0.1 portfolio build

AI-assisted record-keeping for an Ayurvedic clinic: a FastAPI + SQLite backend, a Next.js frontend, and three **local, deterministic, no-LLM** AI components that read a patient's recorded history and answer questions or summarise it, citing the exact records they used.

> **Status (2026-10-11).** A demonstrable, reproducible engineering build on **synthetic data only**. It is **not clinically validated, not production-ready, and has never been run on a real clinic's data.** Read [What is and is not verified](#what-is-and-is-not-verified) before relying on any number in this repository.

![AyurCore v0.1 demo: Dashboard stock summary, Inventory, a synthetic patient's prescription history, and a cited answer from the "Ask about this patient" panel](docs/assets/ayurcore-v0.1-demo.gif)

*Screen recording of the running app (about 30 s) on **synthetic demo data only**: Dashboard, Inventory, a synthetic patient's history, and one record-grounded answer. A demonstration of the engineering build, not clinical validation or a production system.*

## About this repository

This is a **standalone portfolio snapshot** of AyurCore v0.1, taken from the development branch of a separate, private clinic-facing backend project. It has a single initial commit; the development history is not included. Differences from the development branch, so you know what you are reading:

- **Left out:** the reserved AI-1 final holdout (`eval/benchmarks/s1/s1_test.json`), the registered longitudinal evaluation protocol, its run log, the longitudinal and PARA v2 benchmarks, the related evaluation scripts and tests, and local evidence files (for example the AI-2 dev-run reports). Documents under `docs/` that mention them describe files that are **not in this repository**.
- **Changed:** three local-path mentions (in two files) were replaced with placeholders. Nothing else was edited.
- **No licence has been chosen yet,** so no licence file is included.

## What it does

| Component | What it does | AI? |
|---|---|---|
| Prescribing core | Draft → doctor review → deterministic confirm (the only clinical write, idempotent, stock-checked, audit-chained) | The draft step can use Gemini (`/prescribe/preview`); the confirm step never does |
| **AI-2 — Ask about this patient** | `POST /patients/{pk}/ask`: closed-vocabulary questions (English, Manglish, Malayalam) answered from the patient's confirmed prescriptions and visits, every answer cited and re-verified; unclear questions ask for clarification; advice, cross-patient and unknown-data requests are refused | Local rules + a small pinned planner; **no LLM, no network** |
| **AI-3 — Consultation Brief** | A short cited summary of the patient's history, facts chosen and ordered by a small learned ranker (logistic regression, ~110 weights) | Local model; **no LLM, no network**; wording is a fixed template |
| AI-1 — notes → structured prescription | Local extractor (tagger/assembly/verifier); evaluated on a synthetic benchmark | Evaluated component only — **not wired into the product path** |

Safety principle: **AI proposes or retrieves → the doctor reviews → deterministic backend validates and commits.** The AI components here never write.

## Quick start (about 10 minutes)

Clone, then follow the steps below. Requires Python 3.14 (SQLite ≥ 3.38; tested 3.14.3) and Node with pnpm (tested Node 24.3, pnpm 12.5). No API keys, no network after dependencies are installed.

```bash
git clone https://github.com/sriram615/ayurcore-v0.1.git && cd ayurcore-v0.1
python3.14 -m venv venv
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python scripts/setup_v01_demo.py            # disposable demo DB + three demo logins (inside this checkout)
./venv/bin/python scripts/seed_v01_demo_histories.py   # five invented patients with dated histories
./venv/bin/python scripts/demo_v01_ai.py               # AI-2 answers and AI-3 briefs in the terminal (no server)
```

The full API journey and the browser demo are in [`docs/V01_DEMO_GUIDE.md`](docs/V01_DEMO_GUIDE.md). In short:

```bash
./venv/bin/python scripts/run_v01_demo_server.py       # API on http://127.0.0.1:8000 (demo DB only)
cd frontend && pnpm install && pnpm build && pnpm start -p 3000
```

Demo logins are in `demo/credentials.v01-demo.json` (git-ignored; never printed).

*Checked on 2026-10-11 from a fresh clone of this repository: `setup_v01_demo.py`, `seed_v01_demo_histories.py` and `demo_v01_ai.py` ran with exit code 0 and listed the five demo patients. That check used an existing virtual environment; a fresh `pip install` and `pnpm install` were not re-run, and the browser demo was not re-run on this snapshot.*

## What is and is not verified

Numbers below come from runs on the **development branch** this snapshot was taken from (the files are identical except for the items listed under [About this repository](#about-this-repository)). They were **not re-run on this snapshot**; where a run was not made at all, it says so. Details and the claim-by-claim table: [`docs/V01_EVALUATION_REPORT.md`](docs/V01_EVALUATION_REPORT.md).

| Claim | Evidence | Limit |
|---|---|---|
| Backend and AI test suite | 862 tests pass in an isolated scratch sandbox on the committed tree; 8 fail only because `numpy` is not installed there (AI-1 extractor/OOD tests) | the legacy Streamlit UI tests and the 2 test files that read the reserved `s1_test.json` holdout are excluded on purpose |
| Frontend | 530/530 mocked-API Playwright tests pass (16 spec files, Chrome, scratch export of development-branch commit `dd5c34b`, with a clean production build and strict `tsc`); 12/12 real-backend end-to-end tests of the Ask flow passed against the demo database earlier (before the Inventory page was added) | The API was mocked in the 530; `e2e-real/` was not part of that run. Real-backend integration of the Inventory page and any accessibility audit are **not verified**; Chrome only |
| AI-2 on the development set | Registered protocol v1.1 gates A1–A5, A7, B1–B8, C1–C3 pass (dev-run-09, 176 cases) | **The development set is what the system was tuned on — this is not generalisation evidence.** Validation sets were never written; the one-shot acceptance set has **not** been run; gate A6 (who may ask) is unscored on the registered protocol |
| AI-3 | Learned ranker vs fixed template on 988 held-out synthetic patients, one-shot: critical facts missed 0.0% vs 56.5% (k=5); 0 unsupported facts shown | Labels come from a written policy by the same author — it measures reproducing that policy, **not clinical usefulness** |
| Who may ask (A6) | Evaluator built; the shipping route passes under the proposed v1.2 control and fails the registered v1.1 ADMIN control (`/ask` is DOCTOR-only) | Needs an owner decision (D-02); synthetic stand-in cases only |

Not done and **not claimed**: clinical validation, native-speaker review of validation wordings, an acceptance run, production deployment, enforcement guardrails (Phase 3B), a live-clinic pilot.

## Repository layout

| Path | Contents |
|---|---|
| `backend/` | FastAPI app (`main.py`), auth/RBAC, patients, encounters, prescriptions, inventory, audit chain |
| `ai2/` | Question answering: parse → plan → execute → verify; read-only snapshot adapter |
| `ai3/` | Consultation Brief: facts, verification, salience ranker, evaluation harness, synthetic generator |
| `memory/`, `extractor/` | AI-2 research modules and the AI-1 extractor |
| `frontend/` | Next.js UI (the demo surface): patient search, history, Ask panel, Dashboard; `e2e/` mocked specs, `e2e-real/` real-backend spec |
| `scripts/` | demo (`setup_v01_demo.py`, `seed_v01_demo_histories.py`, `demo_v01_ai.py`, `run_v01_demo_server.py`), test procedure (`run_scratch_sandbox_tests.py`), evaluation (`ai3_train_eval.py`, `eval_actor_aware.py`, …) |
| `eval/`, `artifacts/` | synthetic benchmarks and the AI-1 and AI-3 evidence that are included (the AI-2 longitudinal protocol and its dev-run evidence are **not** in this repository; see the evaluation report) |
| `docs/` | [architecture](docs/V01_ARCHITECTURE.md), [demo guide](docs/V01_DEMO_GUIDE.md), [evaluation report](docs/V01_EVALUATION_REPORT.md), [readiness roadmap](docs/PORTFOLIO_READINESS_ROADMAP.md), risk register |
| `AyurCore_API_Contract.md` | API contract (v1.7), including `/patients/{pk}/ask` and `/inventory` |
| `apps/web/`, `ui/` | legacy / placeholder; not part of the v0.1 demo |

## Running the tests safely

**Never run bare `pytest` against a database you care about.** `tests/conftest.py` refuses to run outside a sandbox. Use the scratch-sandbox procedure, which copies only git-tracked files (the restricted acceptance material it guards against is not part of this repository). Its 862-passed result is from the development branch and has not been re-run here:

```bash
python scripts/run_scratch_sandbox_tests.py --dest <NEW dir outside this checkout> --python ./venv/bin/python
```

Installing `numpy==2.5.3` (pinned in `constraints.txt`) into the venv makes the 8 AI-1 tests runnable. See [`docs/V01_DEMO_GUIDE.md`](docs/V01_DEMO_GUIDE.md#verifying-the-build) for the frontend suites.

## Configuration notes

`GEMINI_API_KEY` is needed only for the original `/prescribe/preview` draft step; the AI-2, AI-3 and demo paths make no external call. CORS allows `http://localhost:3000` and `http://127.0.0.1:3000` only. Migrations 001–016 are checksum-locked: never edit one, add a new migration. The real database is `db/ayurcore.db` — treat it as clinic data and do not seed or restore it casually ([backup runbook](docs/BACKUP_RESTORE_RUNBOOK.md)).

## Other documents

[`CLAUDE.md`](CLAUDE.md) engineering rules · [`docs/TECHNICAL_RISK_REGISTER.md`](docs/TECHNICAL_RISK_REGISTER.md) · [`docs/PHASE_3_PLAN.md`](docs/PHASE_3_PLAN.md) (3A evaluation-only; 3B not started) · [`docs/ARCHITECTURE_BASELINE.md`](docs/ARCHITECTURE_BASELINE.md) (original backend design) · [`AyurCore AI Portfolio Master Plan.md`](AyurCore%20AI%20Portfolio%20Master%20Plan.md).
