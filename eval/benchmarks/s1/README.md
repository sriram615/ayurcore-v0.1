# S1 — synthetic Notes → Structured Prescription benchmark

**NOT CLINICAL VALIDATION.** Every note is programmatically generated (`scripts/s1_generator.py`); gold labels are known by construction. Results describe extraction on synthetic text only.

| File | Use |
|---|---|
| `s1_train.json` (360) | development: reading examples, writing prompts and rules, few-shot examples |
| `s1_dev.json` (120) | iteration: every comparison while anything is still being changed |
| `s1_test.json` (120) | **frozen holdout: final reported numbers only** |
| `manifest.json` | sizes, file hashes, dataset fingerprint; `python scripts/s1_generator.py verify` checks the files |

## Holdout policy for `s1_test.json`

- **Never** use it to tune prompts, rules, model settings, the baseline or the generator, and never read its individual failures while changing any of them.
- Run it once per frozen configuration (model, prompt and parser fingerprints recorded by the harness), after the decision to report. Every run is reported, including the ones that look bad; there is no picking the best.
- If anything is changed after a test run, the earlier test numbers describe the old configuration only. Re-run on `dev` first.
- No test in `tests/` computes results on `s1_test.json`; tests only check its format, gold labels and freeze.

**Disclosure (dataset v1, fingerprint `b6ccaca6…`):** while S1 was being built, baseline aggregate scores on all three splits and four example notes from `s1_test.json` were viewed. Two baseline bugs (`:` and decimal points splitting clauses) were found from `dev` failures. Three harder phenomena (self-correction, “instead of”, shared quantity) were added to the generator after seeing that the baseline solved most categories on every split. So `s1_test.json` is a clean holdout for **Control and A0** (nothing about them has been tuned), but **not fully clean for the deterministic baseline or the generator design**. Report baseline test numbers with this caveat.

## Running

Use the isolated evaluation procedure: a temporary copy of the v0.1 checkout with its own fresh, synthetic database, never a clinic checkout or database. See the v0.1 evaluation notes in the report for the exact commands.
