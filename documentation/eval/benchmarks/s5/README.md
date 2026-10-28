# S5 v1 — semantic-misinterpretation holdout (AI-2 Block 5)

**SYNTHETIC — NOT CLINICAL DATA, NOT CLINICAL VALIDATION.** 119 hand-written questions about S2 patients. They test whether the plan step reads the question the way it was meant. The two arms compared are:
- the single-plan planner;
- candidate planning with deterministic alignment checks (`memory/interpret.py`).

| File | Content |
|---|---|
| `s5_v1.json` | The cases (read-only) |
| `manifest.json` | Counts, fingerprint and file hash. Written once by `freeze`, which refuses to run again |

- **Check:** `python scripts/s5_dataset.py verify`.
- **Dataset fingerprint:** `540b922258d89f79319dcf2b3b1cea0d537f6c51298550a22cce6882bef2b71b`.

## Contents

| Category | Cases | What the questions probe |
|---|---|---|
| `agg_vs_count` | 16 | total grams vs number of prescriptions |
| `first_last_previous` | 14 | first / last / previous for single-record questions; "previous" alone is unsupported (abstain) |
| `target_confusion` | 15 | visits vs prescriptions vs herbs vs doshas |
| `selector_confusion` | 14 | changes since the first vs since the previous prescription |
| `comparison_direction` | 12 | reversed wording of the same comparison; pairs the vocabulary lacks (abstain) |
| `quantity_unit` | 14 | grams vs counts, "last N" windows, "the one before", "the latest two" |
| `legit_ambiguous` | 14 | legitimate questions with two or more readings (abstain) |
| `control` | 20 | plain questions that must stay answerable |

- **Languages:** 88 English, 17 Manglish, 14 Malayalam.
- **Gold:** a closed-vocabulary query whose answer is computed by the deterministic executor, or abstain / AMBIGUOUS.
- **Counts:** 100 answerable, 19 abstain.

## Independence and the one-shot rule

- **Written after freezing:** S5 was written after `memory/interpret.py`, the planner model and the rest of the pipeline were frozen. Their hashes were checked unchanged before the run. Nothing was run on S5 before it was frozen.
- **Overlap checks** (enforced): no question shared with, or near-duplicating (word overlap of 0.6 or more), any S3 split, S4, or the hand-written retrieval / interpretation test examples.
  - 25 cases were reworded before freezing after a wording-only check; two of them had repeated S4 questions verbatim.
- **Same author:** I wrote S5, the alignment lexicons and S3/S4, so the set isn't independent of the designer.
- **Evaluated once** by `memory/evaluate_s5.py --final`, which refuses a second run. Results are in `artifacts/models/s5_eval/`. **S5 is now spent.**
