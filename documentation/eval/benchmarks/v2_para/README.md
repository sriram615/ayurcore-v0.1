# PARA v1 — AI-2 v2 Block 4 paraphrase and word-form benchmark

**SYNTHETIC — NOT CLINICAL DATA. Not S7.** 203 hand-written questions in 30 minimal-pair groups, built by `scripts/v2_para.py`. It was frozen in Block 4A, **before** any vocabulary or code change.

| Split | Groups | Cases | Use |
|---|---|---|---|
| `dev` | 15 | 100 | building Block 4B (may be inspected and run) |
| `accept` | 15 | 103 | **frozen**: evaluated once, by Block 4B, for its accept / reject decision. No pipeline is run on it before then. |

- **Fingerprints:** whole set `29901a16541f33c4be9016d1cb7a89427909c6c04b6f762652a2435fe2a9237a`; `accept` `6d6345a593edbb9eabf9d09495b3a543c08dcf3719d5bbb04670664e9b33ea08`.
- **Check:** `python scripts/v2_para.py verify`.

## What each group targets

Each group targets one failure class from the Block 4 investigation. It holds answerable questions, minimal-pair questions that must be refused, and/or ambiguous questions.

| Class | What it covers |
|---|---|
| `defect_on` | a clause-initial "on" read as a prescription verb |
| `defect_most_recent` | "most recently" read as the unsupported "most" |
| `defect_count_noun` | Malayalam എണ്ണം ("count") read as a "should" ending |
| `defect_in_total` | "in total" with a count read as an amount without a herb |
| `defect_day` | Manglish "ethu divasam" ("which day") read as a duration |
| `synonym_*` | English words outside the word lists (change, recent, prescription, current) |
| `morph_manglish`, `morph_malayalam` | inflected forms and suffixes of known words |
| `ambiguity` | two legitimate readings |
| `slot` | "two latest" naming a pair |
| `control` | plain questions Block 2A already handles |

**Languages:** `dev` has 61 English, 21 Manglish, 18 Malayalam; `accept` has 64 English, 21 Manglish, 18 Malayalam.

## Gold

| Gold kind | `dev` | `accept` | Meaning |
|---|---|---|---|
| query | 58 | 62 | gold answer from the executor on the S2 reference snapshot |
| ambiguous | 15 | 12 | lists every legitimate reading |
| abstain | 27 | 29 | a refusal: clinical / other-patient / unavailable / unsupported |

## Independence (enforced by `validate`)

- **No overlap with earlier material:** no question equals or near-duplicates (word-overlap Jaccard 0.6 or more) any S3 split, S4, S5, S6, devcal, hardneg, or any multi-word string in the memory / v2 tests and evaluators.
- **`accept` vs `dev`:** no `accept` question near-duplicates a `dev` question. `accept` deliberately uses *different* synonyms and word forms of the same classes, so a fix that only lists `dev` words won't pass it.
- **Reworded during construction:** many templates were reworded after the wording-only overlap check.

## Caveats

- **Same author:** I wrote this set and the system, and I knew the failure classes when writing `accept`.
- **Strict Malayalam overlap check:** the checker splits Malayalam words at vowel signs, so it errs on the strict side.
