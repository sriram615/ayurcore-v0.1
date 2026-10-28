# S1-OOD v1 — out-of-distribution evaluation set

**SYNTHETIC, NOT CLINICAL VALIDATION.** 62 hand-authored consultation notes with gold labels, used to test the AI-1 extractor (T1 tagger → T2 deterministic assembly → T3 verifier) on phrasing that the S1 generator never produces.

| File | Content |
|---|---|
| `s1_ood_v1.json` | The cases, in the existing case-file schema (`ayurcore.clinical_eval.cases/v1`) plus an `ood` block per case |
| `manifest.json` | Case and category counts, dataset fingerprint (sha256 of the canonical cases) and file sha256 |

Check: `python scripts/s1_ood_dataset.py verify`.

## How it was made, and what "independent" means here

- **Hand-written, case by case,** as literal data. It was not produced by `scripts/s1_generator.py`, and it uses none of the generator's templates, filler sentences, cue phrases or unstocked-plant list.
- **Deliberately different surface forms**, for example:
  - verbs and structure: "Dispense …", "Withhold …", "Swap X for Y", "in place of", "oh wait, make that …", "each 60 g" (marker *before* the quantity);
  - layout: newline and numbered lists, no-space text ("Neem-40g"), upper case;
  - quantities: "0.25 kg", "half kilo", "2 packets x 100 g", "1 kg 200 g";
  - names: aliases missing from the formulary table ("Mulethi"); Malayalam inflected dosha words ("വാതവും പിത്തവും"); new Manglish spellings.
- **Checked for leakage:** no case ID and no note, exact or after normalisation, is shared with any S1 split (tests in `tests/test_s1_ood.py`).
- **Written before any run:** no case was run through the extractor while the set was being written. Nothing in T1, T2 or T3 was changed for it.
- **Limitation, stated plainly:** the author is the same AI assistant that built S1 and T1–T3, so the set is **not independent of the author's knowledge of the system's rules**. It is independent of the S1 *generator*, not of the *designer*. A clinician- or third-party-authored set remains necessary for stronger claims.

## Categories (62 cases)

| Category | Cases | Category | Cases |
|---|---|---|---|
| clean | 5 | units | 6 |
| alias | 6 | shared_correction | 6 |
| malayalam_script | 5 | irrelevant_numbers | 5 |
| manglish | 5 | punctuation_order | 6 |
| negation_replacement | 6 | abstention | 7 |
| distractor | 5 | | |

## Gold conventions

- **`expected.herbs`:** only herbs whose prescription *and* total grams are unambiguous. Grams are taken as written and are never inferred from frequency, duration or vague wording.
- **`expected.dosha`:** a canonical set; an empty list means no dosha is stated.
- **`ood.abstain`:** items the system must **not** emit, with a reason: range, unitless, no quantity, non-gram unit, not in the formulary, alternative quantities, vague quantity.
  - Emitting one is a **confident error**; not emitting it is a **correct abstention**.
- **`ood.spans`:** `[text, type, occurrence, canonical]`, token-aligned gold spans in the T1 label set, used for T1 metrics.
  - Herb spans carry the canonical herb, or the plant name for `UNSTOCKED`.
  - A multi-word dosha phrase is one `DOSHA` span ("Vata and Pitta", "pitha vata").
  - A quantity the system should abstain on is still annotated `QTY` (it *is* the stated amount).
  - Boundary choices follow the S1 conventions, but the set was annotated independently, so exact-span T1 scores also measure annotation-convention agreement.
- **`mentioned_not_stocked`:** plants that aren't in the formulary.

## Holdout rule

S1-OOD is a **one-shot holdout** for the frozen extractor.
- Do not tune T1 features or training, T2 rules, units or lexicons, or T3 checks on it, and do not read individual results while changing any of them.
- The evaluator (`extractor/evaluate_ood.py`) refuses the official file unless it's run with `--final`. It also refuses S1 splits.
- If anything changes after a final run, that run describes the old system only.
- Changing the data means a new version (`s1_ood_v2`) with a new manifest; v1 is never edited.
