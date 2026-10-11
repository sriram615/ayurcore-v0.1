# S4 v1 — adversarial memory-question holdout (AI-2)

**SYNTHETIC — NOT CLINICAL DATA, NOT CLINICAL VALIDATION.** 97 hand-written questions about S2 patients. They test whether the AI-2 retrieval pipeline (scope screen → local planner → executor → verifier) refuses what it must and still answers legitimate memory questions.

| File | Content |
|---|---|
| `s4_v1.json` | The cases (read-only) |
| `manifest.json` | Counts, fingerprint and file hash. Written once by `freeze`, which refuses to run again |

- **Check:** `python scripts/s4_dataset.py verify`.
- **Dataset fingerprint:** `d872dcfbd94610887442d78d4b6847fd6e62a32b71622c04ea81e2ff4f030154`.

## Contents

| Category | Cases | What the questions do |
|---|---|---|
| `rec_disguised` | 12 | ask for a recommendation phrased as a history question |
| `diag_disguised` | 10 | ask for a diagnosis phrased as a comparison |
| `treatment_intent` | 10 | ask to start, add, lower, combine or prescribe |
| `unavailable` | 12 | ask for facts memory doesn't hold: vitals, frequency, author, reasons, outcomes, time, allergy, cost |
| `ambiguous` | 10 | have no subject, several herbs, or are nonsense |
| `cross_patient` | 5 | ask about another person or patient |
| `injection` | 4 | try to override the rules or reach the database |
| `legit` | 34 | legitimate questions across the operations; two have no recorded answer, so the correct outcome is abstain / NOT_RECORDED |

- **Languages:** 76 English, 13 Manglish, 8 Malayalam.
- **Gold:** an abstain reason, or a closed-vocabulary query with its answer computed by the deterministic executor on the patient's S2 reference snapshot.

## Independence and the one-shot rule

- **Written after freezing:** S4 was written after the scope screen, retrieval layer and planner were frozen; their hashes were checked unchanged before the run. Nothing was run on S4 before it was frozen.
- **Overlap checks** (enforced): no question shared with, or near-duplicating (word overlap of 0.6 or more), any S3 split or the hand-written retrieval test examples. Eight cases were reworded before freezing after a wording-only check.
- **Same author:** I wrote S4 and designed the screen, so the set isn't independent of the designer.
- **Evaluated once** by `memory/evaluate_s4.py --final`, which refuses a second run. Results are in `artifacts/models/s4_eval/`. **S4 is now spent.**
