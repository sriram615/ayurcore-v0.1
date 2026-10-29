# S7 — Unseen synthetic holdout for AI-2 memory evaluation

S7 is a held-out evaluation set for AI-2 (grounded patient QA). It contains
810 questions about patients in the demo database (`default_clinic` and
`clinic_north`), with gold plans and gold answers computed by running each
question through the AI-2 executor.

This is an **unseen set**, not an independently authored one: the wordings
come from developer-written templates, the patient set was not used while
building the planner, and gold answers are computed by the executor that
answers the questions. It therefore measures behaviour on unseen synthetic
wordings and end-to-end consistency — **not** generalisation to real
clinicians' phrasing and not clinical validity.

## Results (2026-10-29)

| Surface | Harness | Passed | Failed | Rate |
|---|---|---|---|---|
| HTTP API — auth → RBAC → snapshot → planner → executor → verifier → formulation | `scripts/run_s7_api_evaluation.py`, gated by `tests/test_s7_unseen.py` | 709 | 101 | **87.5%** |
| Executor determinism — same plans re-executed | `scripts/run_s7_evaluation.py` | 810 | 0 | 100% |

The executor number is self-consistency by construction (gold and evaluation
share the executor); 87.5% is the number worth quoting. All 101 failures are
safe failures — a clarification or an unsupported abstention, never a wrong
value — and every one is enumerated in `known_gaps.json`:

| Frame | Failures | API behaviour | Cause |
|---|---|---|---|
| LIST_DOSHA | 25/50 | clarify | planner asks for clarification instead of answering |
| COUNT_DOSHA | 23/50 | unsupported | dosha vocabulary coverage; 18 target the literal placeholder "Not recorded" |
| LAST_DOSHA | 20/50 | clarify | planner asks for clarification instead of answering |
| FIRST_RX | 17/50 | unsupported | one of three phrasings ("Which herbs were prescribed first?") is not aligned to a plan |
| DIFF_PREV | 16/50 | unsupported | "Compare the previous prescription with the most recent one." is not aligned |

These were deliberately left unfixed: S7 is frozen, and tuning the planner
against it would remove the only held-out evidence in the project. The test
asserts the observed failures equal this enumerated set, so a regression and
a silent improvement both fail CI.

## Correction, 2026-10-29

The first freeze reported 810/810. That number was invalid: 650 of the 810
gold answers were `{"status": "error", "value": "bad selector None"}`. The
generator wrote unset selectors as explicit `null`s, `Query` validation
rejected them, and `scripts/run_s7_evaluation.py` rebuilt the same malformed
query and scored error-vs-error as a pass.

Fixed by recomputing gold from the unchanged frozen plans with plan→`Query`
construction that omits unset selectors (`scripts/regenerate_s7_gold.py`).
Questions, frames, patients and plans were not touched, so the set stayed
unseen. Gold statuses went from `{error: 650, ok: 98, not_recorded: 52,
refused: 10}` to `{ok: 748, not_recorded: 52, refused: 10}`. Generation now
raises instead of recording a failed plan as gold, and the runner counts
`invalid_gold` separately and never as a pass. Full record:
`manifest.json` → `corrections`.


## Dataset statistics

- **Total questions:** 810
- **Clinics:** default_clinic (410), clinic_north (400)
- **Patients:** 50 per clinic (25 per clinic with prescriptions)
- **Frames:** 17 question frames (first, last, count, list, sum, diff,
  compare, plus abstention)
- **Language:** English only

## Question frames

Each frame corresponds to a closed executor vocabulary operation:

| Frame | Op | Target | Description |
|-------|-----|---------|-------------|
| FIRST_RX | first | prescription | First prescription |
| LAST_RX | last | prescription | Last prescription |
| FIRST_HERB | first | herb | First prescription mentioning herb |
| LAST_HERB | last | herb | Last prescription mentioning herb |
| FIRST_DOSHA | first | dosha | First recorded dosha |
| LAST_DOSHA | last | dosha | Last recorded dosha |
| COUNT_RX | count | prescription | Count prescriptions |
| COUNT_RX_N | count | prescription | Count prescriptions (with limit) |
| COUNT_HERB | count | herb | Count prescriptions for herb |
| COUNT_DOSHA | count | dosha | Count prescriptions for dosha |
| SUM_HERB | sum | herb | Sum grams of herb |
| LIST_RX | list | prescription | List all prescriptions |
| LIST_HERB | list | herb | List all herbs |
| LIST_DOSHA | list | dosha | List all doshas |
| DIFF_PREV | diff | prescription | Diff previous vs last |
| DIFF_FIRST | diff | prescription | Diff first vs last |
| CMP_HERB_PREV | compare | herb | Compare herb prev vs last |
| CMP_DOSHA_PREV | compare | dosha | Compare dosha prev vs last |

Plus `UNSAFE` frame: treatment advice, diagnosis requests, etc. (abstention).

## Gold answers

Each question has:
- `gold_plan`: the closed-executor query plan (`{"kind": "query", "query": {...}}`)
- `gold_answer`: the executor's computed result (`status`, `value`, `sources`)

Gold answers are computed by running each plan through
`memory.executor.execute` against a read-only snapshot of the patient's
record. This ensures the ground truth is deterministic and reproducible.

## Generation

Generated by `scripts/s7_generator.py` with seed 20261028.

```
./venv/bin/python scripts/s7_generator.py generate   # write s7_unseen.json
./venv/bin/python scripts/s7_generator.py verify     # verify file integrity
```

## Classification

**SYNTHETIC — NOT CLINICAL DATA.** All patient names, phones, prescriptions,
and clinical content are invented for evaluation purposes. No real patient
data is used.
