# S1-OOD v3 — final safety benchmark

**SYNTHETIC, NOT CLINICAL VALIDATION.** 62 hand-written consultation notes with gold labels: the last one-shot measurement of the AI-1 extractor (T1 tagger → T2 assembly → T3 v3 verifier). T3 v3 is R1–R5 plus the structural fixes made after the S1-OOD v2 failure analysis:
- unstocked-item recognition;
- broader alternatives;
- broader past-use context;
- longer-range negation.

| File | Content |
|---|---|
| `s1_ood_v3.json` | The cases, in the same format and gold conventions as S1-OOD v2 (see `../s1_ood_v2/README.md`) |
| `manifest.json` | Counts, dataset fingerprint and file sha256. Written once by `freeze`, which refuses to run again; both files are read-only |

- **Check:** `python scripts/s1_ood_v3_dataset.py verify`.
- **Integrity tests:** `tests/test_s1_ood_v3.py`.
- **Dataset fingerprint:** `0d8e9f9ad2699c1ce4969bc6eef722f24c6036c2bb24b4b7a8c1fba9b38ed49e`.

## Contents

| Category | Cases | Category | Cases |
|---|---|---|---|
| `r1_conflict` | 7 | `r5_unaccounted` | 7 |
| `r2_alternative` | 8 | `cross_rule` | 6 |
| `r3_history` | 8 | `clean_control` | 10 |
| `r4_negation_replacement` | 8 | `malayalam_manglish` | 8 |

**Cases targeting each rule** (a case can target several):

| R1 | R2 | R3 | R4 | R5 | Controls |
|---|---|---|---|---|---|
| 14 | 11 | 13 | 11 | 11 | 18 |

**Gold items:** 17 items that must not be output (abstain) and 8 doses that nothing can account for (R5 should warn).

**Languages:** 52 English, 7 Manglish, 3 Malayalam script.

## Independence and leakage

- **When it was written:** after T3 v3 was frozen (`verify_context.py` `6856be82…`, `verify.py` `f5096345…`). It was hand-written as literal data and never run through the extractor or verifier before freezing.
- **Overlap checks** (enforced by tests) cover S1 train, dev and test, S1-OOD v1, S1-OOD v2, and every hand-written note used to develop or test T3:
  - no case IDs shared;
  - no notes shared, exact or after normalisation;
  - no near-duplicates (word overlap below 0.6).

  Three cases were reworded before freezing, after a wording-only overlap check: `ood3-r1-03`, `ood3-r2-03`, `ood3-r5-06`.
- **Limitation, stated plainly:** I wrote this set and also designed T3 v3. The set is not independent of the rules' design and may share my blind spots. It includes natural phrasing outside the rules' word lists, for example:
  - "swap to … if …";
  - "drop Haridra";
  - "was on … until March";
  - "her mother takes …";
  - "not to be mixed with …".
- **Small:** 6–10 cases per category.

## Holdout rule

- **One-shot:** the set is evaluated only by `extractor/evaluate_ood_v3.py --final`, once. The evaluator refuses a second run.
- **No tuning:** nothing may be tuned on the result.
- **This is the final extractor evaluation cycle;** further changes need a new set.
