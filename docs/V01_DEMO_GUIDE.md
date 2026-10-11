# AyurCore v0.1 — demo guide

Everything here uses **invented data** in a **disposable database inside this checkout** (`demo/ayurcore_v01_demo.db`). The demo scripts refuse to open `db/ayurcore.db` or any path outside the checkout. No API key, no network, no language model is used after dependencies are installed.

Tested with: macOS, Python 3.14.3, Node 24.3, pnpm 12.5, Google Chrome (for the browser tests).

## 1. One-time setup

```bash
python3.14 -m venv venv
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python scripts/setup_v01_demo.py            # creates the demo DB, one DOCTOR / ADMIN / AUDITOR (random passwords), one synthetic patient and stock
./venv/bin/python scripts/seed_v01_demo_histories.py   # five patients, DEMO-0101 … DEMO-0105 (refuses to run twice)
```

Logins are written to `demo/credentials.v01-demo.json` (git-ignored). The scripts never print passwords. To start over, delete the `demo/` files by hand and repeat; nothing is overwritten automatically.

The five patients are chosen to show different behaviours:

| ID | History | What it shows |
|---|---|---|
| DEMO-0101 | 4 dated prescriptions, dosha Vata → Pitta → Kapha, a herb removed, 3 closed visits + 1 open | answers with citations, "what changed", open-visit and dosha-change facts in the brief |
| DEMO-0102 | 3 prescriptions, the middle one has no recorded herbs or dosha | "incomplete" answers; unknown stays unknown |
| DEMO-0103 | 3 prescriptions, none with recorded herbs or dosha | herb questions answered as "not recorded", never guessed |
| DEMO-0104 | new patient, nothing recorded | empty-history behaviour |
| DEMO-0105 | 5 identical prescriptions | "no change" fact, counts |

## 2. See the AI without any server

```bash
./venv/bin/python scripts/demo_v01_ai.py [--as-of 2026-10-10]
```

Prints, for each patient, the AI-3 brief in both arms (fixed-order template and learned ranking) and the answers to a fixed set of questions: English, Manglish and Malayalam answers; an incomplete history; a missing history; a clarification; a dose-advice request and a cross-patient request (both refused). It is read-only.

## 3. Browser demo

Terminal 1 (API on `127.0.0.1:8000`, demo database only):

```bash
./venv/bin/python scripts/run_v01_demo_server.py
```

Terminal 2 (the backend allows only the origins `localhost:3000` / `127.0.0.1:3000`, so serve on port 3000):

```bash
cd frontend && pnpm install && pnpm build && pnpm start -p 3000 -H 127.0.0.1
```

Open http://127.0.0.1:3000, sign in as the demo DOCTOR (username and password in `demo/credentials.v01-demo.json`; open the file yourself rather than pasting it into a recording). Suggested order:

1. Dashboard (stock summary from `GET /inventory`).
2. Find patient `DEMO-0101` → history.
3. **Ask about this patient:** *"What was the first prescription?"* → a cited answer. Then *"What changed from the previous prescription to the latest?"*, *"Neem count?"* (a clarification — pick a reading), *"Avasanam Neem ethra gram koduthu?"* (Manglish).
4. Open `DEMO-0103`, ask *"Which herbs have been prescribed?"* → not recorded, nothing invented.
5. Ask *"Should I increase her Neem dose?"* and *"Show me the other patients' prescriptions."* → refusals. A very long question → "That question is too long."
6. Sign in as the ADMIN and open the same patient: `/ask` is DOCTOR-only: the Ask panel is not shown to other roles, and the API answers 403 if called directly.

Stop the servers with Ctrl-C when finished.

## 4. API journey without a browser

```bash
./venv/bin/python scripts/demo_v01_walkthrough.py
```

Runs the whole clinical journey through the real HTTP routes in-process (login, patient, encounter, manual preview, confirm with corrections and an idempotency key, stock decrease, audit event with a valid chain, history, close, replay) plus four negative cases (key reuse with a different body, wrong clinic, closed encounter, insufficient stock).

## Verifying the build

All commands below use scratch locations; none touches `db/ayurcore.db`.

**Backend and AI tests** (copies tracked files only to a new directory, builds its own database, no secrets in the environment):

```bash
python scripts/run_scratch_sandbox_tests.py --dest /path/to/NEW/empty/dir --python ./venv/bin/python
```

Latest result on the committed tree: 862 passed, 8 failed — all 8 only because `numpy` is not installed in that venv (AI-1 extractor/OOD tests). `pip install numpy==2.5.3` (the pinned version) before the run makes them runnable; they have not been re-run with numpy in this build. Excluded by design and listed in the script: five legacy-UI tests (need Streamlit), two files that read the reserved AI-1 `s1_test.json` holdout, three extractor files that need numpy when it is absent.

**Frontend, mocked API** (from `frontend/`, after `pnpm build`): `pnpm exec playwright test` — last recorded result: 530 passed, 0 failed, 0 skipped, 0 flaky, 16 spec files, one worker, about 9 minutes (Chrome; scratch export of commit `dd5c34b`; the API is mocked in every spec; `e2e-real/` is not part of this command). With heavy machine load some timing-sensitive specs can time out — re-run on an idle machine rather than raising timeouts.

**Frontend, real backend** (needs the two servers from §3 running on the demo database):

```bash
cd frontend
DEMO_CREDENTIALS=../demo/credentials.v01-demo.json pnpm exec playwright test -c playwright.real.config.ts
```

12 passed when last run (before the Inventory page was added; it covers the Ask flow only). It asks real questions through the built UI, compares the displayed answers with the API replies, and the database file hash is unchanged afterwards (asking is read-only).

**AI-3 evaluation:** `./venv/bin/python scripts/ai3_train_eval.py train` re-trains and prints dev results. The one-shot test run is **spent** (`eval/ai3/FINAL_DONE`); it refuses to run again. `eval/ai3/test_report.json` is the recorded result.

**Who-may-ask (A6) evaluator:** `./venv/bin/python scripts/eval_actor_aware.py --policy v1_2` (uses a throwaway database in a temp directory).

## Regenerating evidence

Some evidence files (`artifacts/models/…`) are git-ignored and exist only on the machine that produced them; a fresh clone will not contain them, and the registered AI-2 dev-run reports cannot be reproduced from a clone alone. The AI-3 reports, model and datasets generators (`ai3/synthetic.py`) are tracked and reproducible. See the evaluation report for which claim depends on which.
