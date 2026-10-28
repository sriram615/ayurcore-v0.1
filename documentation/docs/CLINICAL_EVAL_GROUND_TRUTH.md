# Clinical Evaluation — Ground Truth Protocol (v2 dataset)

This document defines how the 30–50 case clinician-reviewed dataset
(`eval/cases/clinical_eval_v2.json`) is built, reviewed, frozen and used to
compare the Control parser with A0. It covers the dataset only. It does not
change any production code, parser, prompt or schema.

Files:

| File | Role |
|---|---|
| `eval/cases/clinical_eval_v2_template.json` | Empty template. Not runnable (its `cases` list is empty). |
| `eval/cases/clinical_eval_v2.json` | The real dataset, created from the template. Does not exist yet. |
| `documentation/docs/CLINICAL_CASE_REVIEW_FORM.md` | The per-case form a clinician fills in and signs. |
| `scripts/clinical_eval_harness.py` | Runs and grades one arm against a case file. |
| `tests/test_clinical_eval_v2_template.py` | Checks the template, and checks `clinical_eval_v2.json` against this protocol whenever that file exists. |

`eval/cases/clinical_eval_v1.json` (12 AI-written synthetic cases) is a
**technical pilot only**. It is never part of the clinical dataset and its
results are never reported as clinical validation.

---

## 1. What "ground truth" means here

The parsers are **extractors**. They read a consultation note and propose
the dosha and the herbs/grams the treating clinician documented. Ground
truth is therefore:

> What the treating clinician documented and intended in the note, as
> confirmed by a qualified Ayurvedic clinician reviewer.

It is **not** a second-opinion diagnosis. If the reviewer disagrees
clinically with what the note prescribes, the expected values still reflect
the note. The reviewer's concern goes into `safety_annotations` (or
`annotation_notes`), not into `expected`.

Only a clinician may supply:

- expected dosha
- expected herbs and total grams
- gram tolerances
- safety annotations
- the ambiguous/exclude decision

Engineers transcribe these values from signed review forms. Engineers must
never invent, "fix" or infer clinical values. When a form is unclear, the
case goes back to the reviewer.

## 2. Case eligibility and de-identification

A case may enter the dataset only if it is:

1. **A real consultation note, de-identified** (`origin:
   deidentified_real_consultation`), or a realistic note written by a
   clinician (`origin: clinician_authored_realistic`). AI-written notes are
   not eligible.
2. **De-identified before it reaches the repository.** Remove or generalize:
   - names, phone numbers, IDs, and addresses/locations
   - exact dates
   - employer or relatives
   - any rare detail that could identify the patient
   - identifying information about the clinic or clinician inside the note
     text
3. **Checked by a second person.** `deidentification.checked_by` must differ
   from `performed_by`, and all three attestation flags must be `true`.

Text removed during de-identification must not change the clinical content
(dosha, herbs, quantities). If it would, exclude the case.

The harness still tokenizes the note with the existing PII sanitizer before
any Gemini call. That is defence in depth, not a substitute for
de-identification.

## 3. Field rules

### Expected dosha
A list of distinct values from `Vata`, `Pitta`, `Kapha`, graded as a
**set** (order-insensitive, never substring). Use `[]` only if the reviewer
confirms the note documents no dosha.

### Expected herbs and grams
- Keys must be **canonical clinic herb names** (the harness rejects anything
  `normalize_herb` does not return unchanged).
- `grams` is the **TOTAL prescribed/dispensed grams** for that herb, as a
  positive integer (at most 10000). It is never a per-dose amount.
- If the note gives a dose pattern rather than a total, the reviewer works
  out the total and records **only the total**. The dataset has **no
  frequency or duration fields**, and the system never infers them.
- If the total cannot be determined from the note, the case is
  `include_ambiguous` or `exclude` (see §4). The reviewer never picks a
  number.
- `tolerance_grams` defaults to `0`. A non-zero tolerance is allowed only
  when the reviewer explicitly accepts a range, with the reason written in
  `annotation_notes`.

### Herbs mentioned but not stocked
`mentioned_not_stocked` lists any herb named in the note that the clinic
does not stock, with the spellings the reviewer expects. A proposal
containing one of these is flagged.

### Safety annotations
Optional, and only from the clinician (`source: "clinician"`,
`annotated_by` required). Three types are supported:

| Type | Meaning | Required fields |
|---|---|---|
| `herb_must_not_be_proposed` | Proposing this herb for this case is a safety concern | `herb` |
| `herb_max_total_grams` | Proposing more than this many total grams is a safety concern | `herb`, `max_grams` |
| `requires_clinician_review` | Flag for human review; not auto-evaluated | `description` |

`severity` and `rationale` are the reviewer's own labels and words. The
harness contains no contraindications, dose limits or safety rules of its
own. It only applies what is in the case file. A triggered safety
annotation stops the case from counting as a PASS.

## 4. Dispositions: include, ambiguous, exclude

| `review.disposition` | Where it lives | `ambiguous` | Graded as |
|---|---|---|---|
| `include` | `cases` | `false` | PASS or CLINICAL_MISS (clinical denominator) |
| `include_ambiguous` | `cases` | `true` | AMBIGUOUS_REVIEW. **Never** PASS/FAIL. Unknown herbs and safety flags are still reported. |
| `exclude` | `excluded_cases` | n/a | **Never run, never graded.** Kept only for the record, with `exclusion_reason`. |

For an ambiguous case:
- set whichever expected field is not determinable to `null`
- write the reason in `ambiguity_reason`

The harness only reads `cases`. `excluded_cases` is invisible to it by
design.

## 5. What counts toward the clinical denominator

A graded attempt counts toward clinical results only if **all** of these
hold:

- its case is in `cases`
- its case is `clinician_reviewed`
- its disposition is `include` (not ambiguous)
- the parser returned a result: provider errors, parse errors and harness
  errors are reported separately and are never clinical misses

The v2 dataset may contain **only** `clinician_reviewed` cases in `cases`.
Synthetic or unreviewed cases can therefore never enter the clinical
denominator of a v2 run. The protocol test enforces this. Independently,
the harness marks any run that contains a non-`clinician_reviewed` case as
`clinical_validation_eligible: false` with a "NOT CLINICAL VALIDATION"
disclaimer.

**Size target:** 30–50 cases with disposition `include`. Ambiguous and
excluded cases are extra and do not count toward the 30. With this sample
size, results are reported as counts with confidence intervals. They are
not claims of statistical superiority.

Suggested coverage (guidance, not quotas):
- English, Manglish, Malayalam script and mixed-script notes
- 1-herb and multi-herb prescriptions
- notes with distracting history
- notes mentioning herbs that are not stocked
- shorthand-heavy notes

## 6. Review and sign-off

1. The de-identified note is given to the reviewer on a
   `CLINICAL_CASE_REVIEW_FORM.md` form, one form per case.
2. The primary reviewer completes the form and signs it. A second reviewer
   is recommended for every case with a safety annotation or an ambiguous
   decision. Disagreements and their resolution are recorded in
   `review.disagreements_and_resolution`.
3. An engineer transcribes the form into the dataset exactly. Every entry
   references its form (`review.sign_off.review_form_ref`).
4. The clinical lead confirms the whole dataset matches the forms and signs
   `dataset.clinical_lead_sign_off`.

Signed forms are kept outside this repository by the clinical lead.
Reviewers appear in the dataset only by code (e.g. `REV-01`).

## 7. Freezing and versioning (required before any live run)

A live Control-vs-A0 evaluation may only be run against a **FROZEN**
dataset.

**To freeze:**

1. Every case is transcribed and signed off, and the clinical lead has
   signed.
2. Compute the canonical content hash of `cases` + `excluded_cases`:

   ```bash
   ./venv/bin/python -c "import json,hashlib; d=json.load(open('eval/cases/clinical_eval_v2.json',encoding='utf-8')); print(hashlib.sha256(json.dumps({'cases':d['cases'],'excluded_cases':d['excluded_cases']},sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest())"
   ```

3. Set these fields:
   - `dataset.status` to `"FROZEN"`
   - `dataset.cases_sha256` to that hash
   - `dataset.frozen_at` and `dataset.frozen_by`
4. Run the protocol test. It recomputes the hash and fails if anything
   changed:

   ```bash
   ./venv/bin/python -m pytest tests/test_clinical_eval_v2_template.py -q
   ```

5. Record the whole-file SHA-256. The harness also records it as
   `case_file_sha256` in every run artifact. This project is not a git
   repository, so also keep a read-only copy
   (`eval/cases/frozen/clinical_eval_v2_<version>.json`) and store the
   hash with the clinical lead.

**After freezing:**

- A frozen dataset is **never edited**. Any change to a case, including a
  typo in `notes`, creates a new version: bump `dataset_version`, set the
  status back to `DRAFT`, re-review what changed, and freeze again. Results
  from different versions are never pooled.
- `case_id`s are never reused or renumbered across versions.
- Both arms must be run against the **same** `case_file_sha256`, and
  `--regrade` refuses a raw file whose recorded hash does not match.
- The ground truth is fixed before looking at any live output. Values are
  never adjusted after seeing what the model produced. If a reviewer later
  finds a genuine ground-truth error, the fix goes into a new version, and
  both arms are regraded (offline, `--regrade`) against that version.

## 8. What this protocol does not do

- It does not add frequency, duration or dosing-schedule grading.
- It does not define clinical safety rules. Those come only from reviewers,
  case by case.
- It does not make any result "clinical validation" unless every graded
  case is `clinician_reviewed` from a frozen dataset.
