# S1-OOD v2 — safety benchmark for the T3 context checks

**SYNTHETIC, NOT CLINICAL VALIDATION.** 73 hand-written consultation notes with gold labels. It measures what the AI-1 extractor (T1 tagger → T2 assembly → T3 verifier with the R1–R5 context checks) does on notes that call for abstention, and how often it wrongly rejects legitimate notes.

| File | Content |
|---|---|
| `s1_ood_v2.json` | The cases: existing case-file schema (`ayurcore.clinical_eval.cases/v1`), plus an `ood` block and a `safety` block per case |
| `manifest.json` | Counts, dataset fingerprint (sha256 of the canonical cases) and file sha256. Written once by `freeze`, which refuses to run again |

- **Check:** `python scripts/s1_ood_v2_dataset.py verify`.
- **Integrity tests:** `tests/test_s1_ood_v2.py`.
- **Dataset fingerprint:** `48b7c2ffb4f7b6a871d83c0be0a44adf88eeaea7519726ce40cdead02e1c684e`.

## What it targets

| Category | Cases | What the notes contain |
|---|---|---|
| `r1_conflict` | 9 | Corrections across sentences, amount before the herb, a correction after another herb, the same amount repeated, and unresolved conflicts (abstain) |
| `r2_alternative` | 9 | "or", "alternatively", a slash, ranges, a conditional substitute (abstain); one "or" between vehicles (control) |
| `r3_history` | 9 | Past use with amounts, the same herb past and current, history after the current prescription; one "repeat … as given last month" (control) |
| `r4_negation_replacement` | 9 | Hold, not to be given, replace, rather than, refused, contested status (abstain); a correction using "no" and a negation of a non-herb (controls) |
| `r5_unaccounted` | 7 | Doses for plants the clinic doesn't stock (R5 should warn); body weight in kg (control) |
| `cross_rule` | 8 | Two rules in one note |
| `clean_control` | 12 | Legitimate notes with words that look like triggers: "before food", "or" between vehicles, "last" as a verb, a past-time word in another clause or sentence, kg and BP, a shared "each", a pack amount, a negation of timing, "now" |
| `malayalam_manglish` | 10 | R1–R5 phenomena and controls in Manglish (6) and Malayalam script (5, including one in `r5_unaccounted`) |

**Rule coverage** (cases that target each rule; a case can target several):

| R1 | R2 | R3 | R4 | R5 | Controls |
|---|---|---|---|---|---|
| 19 | 14 | 14 | 14 | 10 | 20 |

**Gold items:** 21 items that must not be output (abstain) and 7 doses that nothing can account for (R5 should warn).

## Gold conventions

- **`expected.herbs`:** what a correct system outputs, as total grams written in the note. A clear correction counts as its final amount, and nothing is inferred from frequency or duration.
- **`ood.abstain`:** items a correct system must not output, each with one reason:
  - `conflicting_quantities`
  - `alternative_quantities`
  - `alternative_herbs`
  - `range`
  - `contested_status`

  Outputting one is a confident error.
- **Past, stopped and replaced herbs** are neither expected nor abstain items: outputting them is a spurious herb.
- **`safety.target_rules`:** R1–R5 the case exercises. `control` marks a legitimate note: rejecting it is a false rejection, and a control never requires abstention.
- **`safety.unaccounted`:** stated gram doses that no output fact can use, so R5 should warn.
- **`ood.spans`:** `[text, type, occurrence, canonical]` in the T1 label set, the same conventions as S1-OOD v1:
  - superseded amount: `QTY_OLD`;
  - amount of a past, stopped or unstocked plant: `QTY_OTHER`;
  - amount of an abstain item: `QTY`.

### Judgement calls

The annotator made these calls; another annotator might differ:
- "Repeat Shatavari 60 g as given last month" and "restart Brahmi 30 g today" are current prescriptions.
- "Cancel that" applies to the previous herb.
- A conditional substitute ("may use Punarnava 100 g instead if out of stock") makes both herbs alternatives.

## Independence and leakage

- **Hand-written as literal data.** No S1 generator templates were used, and no case was run through the extractor or verifier while the set was written or frozen.
- **Overlap checks** (enforced by tests) cover S1 train, dev and test, S1-OOD v1, and the hand-written notes used to develop and test R1–R5 (`tests/test_t3_context.py`, `tests/test_t3_integration.py`):
  - no case IDs shared;
  - no notes shared, exact or after normalisation;
  - no near-duplicates (word overlap must be below 0.6).
- **Near-duplicates were reworded before freezing.** Six cases were reworded, after an overlap check, to stay away from the R1–R5 development notes and from S1-OOD v1: the 0.8 overlap of `ood2-ctl-03` and five at about 0.6. That check compared wording only; no system output was involved.
- **Limitation, stated plainly:** the author is the same AI assistant that designed R1–R5 and wrote their word lists. So the set is **not independent of the rules' design**: its phenomena follow the rules' targets, and it may share the author's blind spots. It does contain phrasing outside the rules' word lists (for example "hold", "cancel that", "refused", "until last week", "in January", "kazhichu"), because that is how such notes are written, not to make the rules fail. A clinician- or third-party-written set is still needed for stronger claims.
- **Small:** 7–12 cases per category, so per-category results are very uncertain.

## Holdout rule

S1-OOD v2 is a **one-shot holdout** for the frozen T1/T2/T3, with R1–R5 integrated.
- Do not tune T1, T2, T3, R1–R5 or their word lists on it, and do not read individual results while changing any of them.
- The existing OOD evaluator (`extractor/evaluate_ood.py`) currently refuses this file, because it validates against v1's categories. Evaluating v2 needs an explicit v2-aware, one-shot evaluation step that has to be approved first.
- **Changing the data means a new version** (`s1_ood_v3`) with a new manifest; v2 is never edited. The `freeze` command refuses to rewrite the manifest.
