# AyurCore OS — Engineering Rules

## Project
AyurCore OS is an AI-assisted Ayurvedic clinic system.

Stack:
- FastAPI backend
- SQLite database
- Next.js frontend
- Streamlit legacy/fallback UI
- Gemini AI

Core safety principle:

AI proposes → doctor reviews/corrects → deterministic backend validates/commits.

## Protected clinical boundary

`/prescribe/preview`
- May call AI.
- Creates persistent preview state.
- Must not commit clinical/inventory changes.

`/prescribe/confirm`
- Deterministic.
- ZERO Gemini/AI calls.
- Only clinical write/inventory deduction boundary.
- Idempotent.
- Backend authoritative.

`/prescribe/manual-preview`
- Deterministic AI-outage/manual fallback.
- No Gemini.

Never move clinical writes into AI code.

## Security substrate

Already implemented and tested:
- opaque server-side sessions
- Argon2id passwords
- RBAC
- clinic isolation
- persistent idempotency
- atomic SQLite inventory deduction
- tamper-evident per-clinic audit hash chain
- PII tokenization before Gemini
- A0 bounded agentic parser
- patient registry and encounters (clinic-scoped, trigger-enforced)
- append-only audit table and off-database audit anchors (scripts/audit_anchor.py)
- structured prescription storage (Phase 2, migrations 015–016)

Do not redesign these unless explicitly instructed.

## Structured prescription data (Phase 2)

- `prescription_items` (herbs) and `prescriptions.dosha_imbalance` are the
  authoritative record for prescriptions confirmed since migrations 015/016.
- `prescriptions.description` is still written for backward compatibility;
  read it only as the fallback for legacy prescriptions without structured
  values. Never add a new reader that parses it.
- Confirmed prescriptions and items are immutable (database triggers).
- No automatic backfill of legacy prescriptions.

Applied migrations (001–016) must never be edited; changes need a new migration.

Reference: documentation/docs/ARCHITECTURE_BASELINE.md, documentation/docs/PHASE_2_FREEZE.md,
documentation/docs/TECHNICAL_RISK_REGISTER.md.

## A0

A0 is a bounded agentic parsing/evaluation arm.

Safety properties:
- read-only tools
- authenticated clinic context
- clinic-isolated stock reads
- bounded tool/reasoning calls
- total time budget
- no clinical DB writes
- deterministic confirmation remains unchanged

Do not modify A0 production behavior during evaluation work unless explicitly authorized.

## Current baseline

Protected baseline (Phase 2 freeze, 2026-09-27, Git commit 41a2d1f):

- pytest: 446/446 passing (complete suite; see documentation/docs/PHASE_2_FREEZE.md §4)
- migrations 001–016 applied; checksums verified
- Next.js TypeScript: passing (last verified 2026-09-26/27; not re-run at freeze)
- Next.js production build: passing (same)
- Playwright/UI checks: passing (same)
- real-browser acceptance: passing (earlier milestone; not re-run at freeze)
- Phase 3 security regression (an earlier milestone, unrelated to
  documentation/docs/PHASE_3_PLAN.md): passing (not re-run at freeze)

Current state (final handoff, 2026-09-27, Git commit 12f2e6a):

- pytest: 473/473 passing on an isolated checkout with a fresh, migrated and
  seeded database (procedure below)
- running API: PID 87712 on 127.0.0.1:8000, started 21:39 from 12f2e6a;
  GET /health and GET /openapi.json verified (see documentation/docs/HANDOFF.md)
- frontend and Streamlit code unchanged since phase-2-freeze; not re-run
  against the current backend
- NOT clinically validated; NOT production-ready (documentation/docs/TECHNICAL_RISK_REGISTER.md)

Setup: Python 3.14; `pip install -r requirements-dev.txt` (runtime + pytest/httpx;
add requirements-legacy-ui.txt for the 5 legacy Streamlit test files). All
pins and documentation/constraints.txt match the verified venv; see documentation/README.md.

Note: most test files open db/ayurcore.db of the checkout they run from.
Run tests ONLY through the isolated runner, which performs the procedure below
in a temporary copy and verified 473/473:

    ./venv/bin/python scripts/run_isolated_tests.py [pytest args]

tests/conftest.py makes pytest refuse to run (exit 4) in any checkout that is
not such a sandbox. Running `python tests/test_x.py` directly bypasses that
guard -- only do it inside a sandbox. The manual procedure the runner automates
(never touches the real db/ayurcore.db):

1. Copy the checkout WITHOUT any *.db / *.db-wal / *.db-shm, .env, venv or
   frontend (e.g. rsync with those excludes) into a scratch directory.
2. In the copy: `python -c "from db.database import init_db; init_db()"`
   (fresh database, all migrations).
3. In the copy: `python db/seed.py` -- REQUIRED: several tests assume the
   seeded default_clinic stock. seed.py deletes ALL inventory rows, so run it
   only in the copy, never against the real database.
4. In the copy: create db/.isolated_test_sandbox containing the copy's real
   path (the conftest guard requires it), then
   `python -m pytest tests/ -q -p no:cacheprovider`.

"Phase 3" going forward means the deterministic guardrail plan in
documentation/docs/PHASE_3_PLAN.md — PROPOSED, not implemented, and requiring explicit
approval. Do not start it without that approval.

Do not unnecessarily reopen completed work.

## Current phase

Clinical AI validation.

Goal:
Compare existing Control parser vs A0 on 30–50 realistic,
de-identified, clinician-reviewed cases.

Measure:
- dosha correctness
- herb precision/recall
- missing herbs
- unexpected herbs
- quantities
- hallucinated/unknown herbs
- provider/system failures
- required doctor corrections
- clinician-supplied safety flags

Clinical ground truth must come from an Ayurvedic clinician.
Never invent clinical ground truth.

Synthetic cases may be used for technical self-checks only.
They must never be presented as clinical validation.

## Evaluation rules

Expected grams mean TOTAL prescribed/dispensed grams,
not per-dose grams.

Do not infer frequency or duration because the current ClinicalParse
schema does not represent them.

Separate:
- provider/system errors
- parsing failures
- clinical grading failures

Do not count provider failures as clinical misses.

Dosha grading must use sets, not substring matching.

Do not invent contraindications, maximum doses, or safety rules.
Safety annotations must come from clinician-reviewed case data.

Preserve raw model output separately from graded results.

Record reproducibility metadata:
- case-file fingerprint
- model
- settings
- arm
- parser fingerprints
- stock snapshot/hash where relevant
- latency
- error cause

## Engineering discipline

Before changing files:
- inspect existing implementation
- reuse existing infrastructure where appropriate
- make the smallest change that satisfies the task

For single-file/small tasks:
- work directly
- do not spawn unnecessary subagents
- do not explore unrelated parts of the repository

Never modify production code when an evaluation-only implementation is sufficient.

Never change:
- backend
- frontend
- auth
- RBAC
- inventory logic
- audit logic
- database schema
- migrations
- clinical AI behavior

unless the prompt explicitly authorizes it.

## Testing

Prefer targeted tests first.

After an approved change:
1. run the relevant targeted test
2. run self-check/offline evaluation
3. run full pytest only when requested

Do not run live Gemini calls unless explicitly requested.

## Reporting

At the end of every task report:
- files changed
- files explicitly untouched
- exact tests/commands run
- results
- remaining issues
- whether production behavior changed

Do not claim a clinical result unless the evidence supports it.