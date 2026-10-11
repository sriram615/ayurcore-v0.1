# Clinical Case Review Form (one form per case)

Use this form for every case in the v2 clinical evaluation dataset. Read
`docs/CLINICAL_EVAL_GROUND_TRUTH.md` before starting.

Answer from **what the treating clinician documented in the note**. Your
own clinical concerns belong in Section F (safety), not in Sections C–D.

Do not write any patient-identifying information on this form.

---

## A. Case identity

| Field | Entry |
|---|---|
| Form ID | FORM-cv2_____ |
| Case ID | cv2_____ |
| Short label (non-identifying) | |
| Origin | ☐ De-identified real consultation ☐ Clinician-authored realistic note |
| Source / batch code (no identifying detail) | |

## B. De-identification check

| Check | Yes / No |
|---|---|
| Names, phone numbers, IDs and addresses removed | |
| Exact dates and locations removed or generalized | |
| Free text re-read for anything else identifying | |
| De-identification did not change dosha, herbs or quantities | |

De-identified by (code): ________ Checked by (code, a different person): ________ Date: ________

De-identified note text (verbatim apart from de-identification edits):

```
(paste here)
```

## C. Expected dosha (as documented in the note)

☐ Vata ☐ Pitta ☐ Kapha ☐ The note documents no dosha ☐ Cannot be determined → see Section E

## D. Expected herbs and TOTAL grams

Record the **total prescribed/dispensed grams** for each herb, **not** the
per-dose amount. If the note gives a dose pattern, work out the total and
write only the total. Do not record frequency or duration anywhere on this
form.

| # | Herb (as written in note) | Canonical clinic herb name | Total grams | Accepted tolerance ± g (default 0) | Reason for any tolerance |
|---|---|---|---|---|---|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |

Herbs named in the note that the clinic does **not** stock (and spellings
to watch for):

________________________________________________

☐ The total grams for one or more herbs cannot be determined from the note → see Section E

## E. Disposition

☐ **Include**: dosha and all herbs/total grams are determinable.
☐ **Include as ambiguous**: never graded pass/fail. Which part is not determinable, and why:
   ________________________________________________
☐ **Exclude**: not used in the evaluation. Reason:
   ________________________________________________

## F. Safety annotations (optional; your clinical judgement)

Only add a line if you, as the reviewer, consider it a safety concern **for
this case**. Leave blank otherwise.

| ID | Type | Herb | Max total grams (if applicable) | Severity (your label) | Rationale / description (your words) |
|---|---|---|---|---|---|
| s1 | ☐ Must not be proposed ☐ Max total grams ☐ Needs clinician review | | | | |
| s2 | ☐ Must not be proposed ☐ Max total grams ☐ Needs clinician review | | | | |
| s3 | ☐ Must not be proposed ☐ Max total grams ☐ Needs clinician review | | | | |

Other notes for the evaluation team (not graded):

________________________________________________

## G. Review and sign-off

| Role | Reviewer code | Date (YYYY-MM-DD) | Signature |
|---|---|---|---|
| Primary reviewer | | | |
| Second reviewer (recommended if Section E is ambiguous or Section F has entries) | | | |

Disagreements between reviewers, and how they were resolved:

________________________________________________

> I confirm that Sections C–E reflect what the treating clinician documented
> in this de-identified note, that grams are total prescribed/dispensed
> grams, and that any safety annotation in Section F is my own clinical
> judgement.

---

### For the evaluation team (transcription)

| Check | Initials |
|---|---|
| Transcribed into `eval/cases/clinical_eval_v2.json` exactly as written | |
| `review.sign_off.review_form_ref` set to this Form ID | |
| Disposition placed correctly (`cases` vs `excluded_cases`, `ambiguous` flag) | |
| Queries sent back to the reviewer rather than guessed | |
