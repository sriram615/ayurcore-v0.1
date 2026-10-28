# DEVCAL v1 — AI-2 v2 development / calibration set

**SYNTHETIC — NOT CLINICAL DATA. NOT A HOLDOUT.** 244 template-generated questions about the S2 reference patients, built by `scripts/v2_devcal.py`, for developing and later calibrating AI-2 v2.

| Split | Templates | Cases | Use |
|---|---|---|---|
| `dev` | 60 | 160 | build and tune v2 components; may be inspected freely |
| `cal` | 30 | 84 | held back for later confidence calibration (thresholds, calibration model). Until then it is **report-only**: no rule may be chosen by looking at it |

- **Split by template:** every third template goes to `cal`, so the two splits never share wording.
- **Check:** `python scripts/v2_devcal.py verify` re-generates the file and compares it.
- **Dataset fingerprint:** `53ad5afdb370e05a7cbaf9dee13fbb89c94cc081db038b0c8b2e500019c2e632`.

## Contents

| Category | What the questions do |
|---|---|
| `answerable` | each of the 27 plan frames, in English, Manglish and Malayalam |
| `paraphrase` | answerable, but worded outside the lexicons ("gone out", "scripts", "as things stand") |
| `ambiguous` | two legitimate readings; the gold lists both |
| `unsupported` | in scope but outside the fixed plan space (second prescription, smallest dose, visits without a prescription) |
| `clinical`, `unavailable`, `other_patient`, `injection` | must be refused |
| `malformed` | non-text, invisible-only, escape codes, overlong, zero-width-split, full-width, legacy Malayalam chillu |

**Gold kinds:**
- `query`: answerable; `gold_answer` is the deterministic executor's output.
- `ambiguous`: lists every legitimate reading.
- `abstain`: a refusal, with its reason.

## Independence

Enforced by `validate`: no generated question equals or near-duplicates (word-overlap Jaccard of 0.6 or more) any question of S3 (all splits), S4, S5, S6 or the hand-written test examples. The spent holdouts were read only for this check; no case or result from them is reused.

27 templates were reworded during construction, after a wording-only overlap check.
