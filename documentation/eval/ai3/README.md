# AI-3 Consultation Brief — model and dataset card

**SYNTHETIC DATA, SYNTHETIC SAME-AUTHOR GOLD. NOT CLINICAL VALIDATION.** Nothing here shows that a brief helps a doctor.

## What the model is

`ai3/data/salience_model.json` (SHA-256 `4ac9b5a7…57126`, pinned in `ai3/brief.py`): a logistic regression over ~110 weights on features of one patient's history (number and recency of prescriptions and visits, whether the latest prescription has unknown data, the fact's kind, …). For one patient it scores each already-verified fact; the top 5 are shown. It selects and orders facts only. It never writes a sentence: wording is a fixed template per fact kind, and a displayed number or herb name is re-checked against the records. No LLM, no network, no new framework.

## Data

| Item | Value |
|---|---|
| Source | `ai3/synthetic.py`: invented patients (0–8 prescriptions, herbs from the AI-2 vocabulary, dosha changes, 8% unknown fields, visits, 15% chance the last visit is open) |
| Splits | train / dev / test generated from fixed seeds; fingerprints are in the reports |
| Labels ("gold") | `reference_policy` — a written rule set authored by the same developer who built the ranker, before training. Not a clinician's judgement |
| Training noise | 10% of training labels flipped (seeded) to model annotator inconsistency; evaluation uses the clean policy |

## Results (test split, 988 patients with facts, evaluated once)

At 5 facts per brief:

| Arm | Reference-salient recall | Critical facts shown | Critical miss rate | Unsupported facts shown |
|---|---|---|---|---|
| Fixed template order | 0.788 (95% CI 0.772–0.803) | 177 / 407 | 56.5% | 0 |
| Learned ranker | 1.000 (1.000–1.000) | 407 / 407 | 0.0% | 0 |
| Reference ceiling (uses the labels) | 1.000 | 407 / 407 | 0.0% | 0 |

At 3 facts: learned recall 0.996 vs template 0.755; critical miss 3.2% vs 70.3%. "Critical" means a dosha change, an open visit, or unknown data on the latest prescription. Intervals are percentile bootstrap over patients (2000 resamples, fixed seed). Full numbers: `test_report.json`.

## How to read this

- The learned ranker reaches the ceiling the labels allow; that shows it **reproduces the written policy** and that a fixed template ordering misses the facts the policy marks critical. It does not show the policy is clinically right.
- The result is not independent: the author wrote both the policy and the ranker.
- **Disclosure of a design change.** A first dev run without label noise and with 5 facts per brief reached the ceiling trivially and so could not distinguish the arms; label noise and a 3-fact budget were added after seeing that dev result, before the test split was touched. The test split was then evaluated once (`FINAL_DONE`); it must not be re-run.
- Safety property tested: 0 unsupported facts shown by either arm on dev and test (every shown fact is re-verified).

## Intended use and limits

Demo and method illustration on synthetic data. Not for patient care. Before any clinical claim: a clinician-authored salience standard, real (consented, de-identified) histories, and an independent review. The brief is a library/demo-script feature; there is no HTTP endpoint.

## Reproduce

`python scripts/ai3_train_eval.py train` (dev only, prints a report; `--write` regenerates the model file — its hash must then be re-pinned, which is a deliberate act). `python scripts/ai3_train_eval.py final` refuses to run (the test split is spent). Tests: `tests/test_ai3_brief.py`.
