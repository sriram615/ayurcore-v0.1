# Contributing to AyurCore OS

Two developers share this repository.

**AyurCore is a clinical system.** It is not clinically validated and not production-ready (`documentation/docs/TECHNICAL_RISK_REGISTER.md`), so treat every change as safety-relevant.

## Ownership

| Area | Owner | Paths |
|---|---|---|
| Backend, database, migrations, AI, architecture, backend tests | **Sriram** | `backend/`, `agents/`, `db/`, `migrations/`, `scripts/`, `tests/`, `eval/`, `requirements*.txt`, `documentation/constraints.txt` |
| Frontend / UI (the v0.1 portfolio demo surface, decision D-03) | **Member 2** | `frontend/` (`apps/web/` is a README-only placeholder; no migration to it is authorized) |
| API contract, integration testing | **both** | `documentation/AyurCore_API_Contract.md`, `documentation/docs/FRONTEND_HANDOFF.md` |
| Legacy Streamlit UI: frozen, to be retired by a separate agreed change | Member 2 (with Sriram's approval) | `ui/`, `.streamlit/`, `tests/test_ui_*.py`, `tests/test_streamlit_config.py` |

- **Stay inside your own paths.** A change in the other owner's paths needs that owner's review.
- **Don't extend or copy the legacy Streamlit UI.**

## Branches

- **`main`** is the shared integration branch: always releasable. **Never push to it directly;** changes reach it only through a reviewed pull request.
  - *GitHub branch protection is not enabled yet* (it needs a paid plan for a private repository), so nothing technically stops a direct push, force-push or deletion. Until it is enabled, this rule holds by agreement.
- **Personal development branches:** `backend/sriram` and `frontend/member-2`.
- **Short-lived topic branches:** `backend/<topic>`, `frontend/<topic>`, `documentation/docs/<topic>` and `fix/<topic>`, in lowercase with hyphens, e.g. `frontend/patient-search`.
- **Keep branches current** by merging or rebasing *your own unpublished* work onto `main`.
- **Never** force-push shared branches, rewrite `main`'s history, or move or delete tags (`phase-2-freeze` is a verified freeze point).

## Pull requests

1. **Scope:** one logical change per PR, small enough to review. Say what changed, why, and how it was verified.
2. **Review:** the owner of each touched path reviews. Changes to the API contract or `documentation/docs/FRONTEND_HANDOFF.md` need **both** developers.
3. **Before requesting review:**
   - backend changes pass the full suite through the isolated runner (below);
   - frontend changes pass `frontend/`'s own checks (`tsc` and the Playwright specs in `frontend/e2e/`);
   - **state the results you actually ran**; don't claim a check you didn't run.
4. **Merging:** squash or merge into `main` only after approval. Don't merge your own PR without review.
5. **Commit messages:** an imperative summary line, then the what and why. Don't mix unrelated changes in one commit.

## API contract changes

**The API contract is the only integration boundary** between backend and frontend.

- **The frontend never works around the contract**, and the backend never changes behaviour to suit a particular UI.
- **The frontend needs something new** (endpoint, field, error code, CORS origin):
  1. open a request describing the need;
  2. the backend owner implements it;
  3. the change ships with an update to **`documentation/AyurCore_API_Contract.md`** (bump the version to 1.7, 1.8, …, and add a change-log row) and to `documentation/docs/FRONTEND_HANDOFF.md`, in the same PR.
- **One contract file.** Changes are additive by default; the archived originals in `documentation/docs/archive/api-contract/` are never edited.
- **A breaking change** must be called out explicitly and agreed by both developers first.
- **Protected invariants that no PR may weaken:**
  - `/prescribe/confirm` is the only clinical write and stock deduction, with **no AI calls**;
  - `/prescribe/preview` never commits;
  - clinic and role always come from the server-side session;
  - confirmed prescriptions and audit events are immutable.

  See `documentation/CLAUDE.md`.

## Tests

- **Never run the backend tests against the real database.** Use the isolated runner only:
  ```bash
  ./venv/bin/python scripts/run_isolated_tests.py            # complete suite
  ./venv/bin/python scripts/run_isolated_tests.py -k rbac -x # options or paths
  ```
  A plain `pytest` in the checkout refuses to run (exit 4) by design.
- **Never run `db/seed.py` or `scripts/seed_demo_data.py`** against a database you care about.
- **No live AI calls** (Gemini) in tests unless explicitly agreed.
- **Frontend tests:** they live in `frontend/e2e/` (mocked API) and `frontend/e2e-real/` (demo backend only). Test against a mocked API built from the contract, and against a separate **non-production** backend copy (`documentation/docs/FRONTEND_HANDOFF.md` §12), never the clinic's database or its running API.
- **Migrations:** applied migrations (`001`–`016`) are **never edited**; schema changes need a new numbered migration, reviewed by the backend owner.

## Never commit

The `.gitignore` enforces most of this, but review your diff anyway:

- **Databases and patient data:** `*.db`, `*.sqlite*`, WAL/SHM files, backups (`*.enc`, `backups/`), audit anchors (`*anchor*.jsonl`), and clinician or de-identified case files (only the synthetic `eval/cases` files are tracked).
- **Secrets:** `.env` and `.env.*` (only placeholder `.env.example` files), keys and certificates, `.npmrc`, credential or service-account JSON, API keys in code, and **browser session/login state** (`.auth/`, storage-state files).
- **Local and generated files:** logs, `artifacts/`, `venv/`, `node_modules/`, build output (`.next/`, `dist/`, `build/`, `out/`, `coverage/`), caches, and `.claude/settings.local.json`.

If something sensitive is committed by mistake, **don't try to fix it by rewriting history on your own.** Tell the other developer immediately, and rotate any exposed secret.

## Where to start

- **Everyone:** `documentation/README.md`, `documentation/CLAUDE.md`, `documentation/docs/HANDOFF.md`.
- **Frontend:** `documentation/docs/FRONTEND_HANDOFF.md`, the API contract, `documentation/docs/V01_DEMO_GUIDE.md`.
- **Backend:** `documentation/docs/ARCHITECTURE_BASELINE.md`, `documentation/docs/TECHNICAL_RISK_REGISTER.md`, `documentation/docs/PHASE_3_PLAN.md`.
