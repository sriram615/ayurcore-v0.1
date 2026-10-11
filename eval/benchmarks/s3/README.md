# S3 v1 — memory questions with gold plans and gold answers (AI-2)

**SYNTHETIC — NOT CLINICAL DATA, NOT CLINICAL VALIDATION.** Natural-language questions about a patient's recorded history, for the local question planner (`memory/planner.py`).

| File | Content |
|---|---|
| `s3_train.json` | 1,048 questions |
| `s3_dev.json` | 186 questions |
| `s3_test.json` | 210 questions — **one-shot holdout, spent on 2026-10-06** |
| `manifest.json` | Sizes, templates, frames, languages, fingerprint and file hashes |

- **Generate:** `python scripts/s3_generator.py generate`. It refuses if the files already exist.
- **Check:** `python scripts/s3_generator.py verify`. It regenerates everything from the seed, which must match exactly.
- **Dataset fingerprint:** `75efd5ab2b2221251cedb906b0e7c17e84c15c34d21f7ddaa3cc845d05316bd8`; seed 20261007. It is built from S2 v1 (`d25c3235…`).

## Contents

- **31 frames:**
  - 27 answerable frames covering all 20 supported operation and target pairs, including the "last N" variants and the previous-vs-first selectors for `diff` and `compare`;
  - 4 abstain classes: `DIAGNOSIS`, `TREATMENT`, `UNAVAILABLE` (facts that aren't recorded, such as blood pressure, symptoms, allergies, frequency, the treating doctor) and `AMBIGUOUS` (missing or extra herbs, no subject, nonsense).
- **197 hand-written templates** in English (`en`), Manglish and Malayalam script (`ml`). Herb names appear as canonical names, English or botanical aliases, or Malayalam names; doshas and numbers in each language's forms.
- **Gold plan:** the closed-vocabulary query, or an abstention with its reason.
- **Gold answer:** the deterministic executor's status, value and source labels on the question's S2 patient, using the reference snapshots in `memory/s2_reference.py`.

## Split and leakage

- **Splits are by template.** Every question from a template is in one split. Dev and test hold one template per answerable frame and two per abstain class, all **never seen in training**.
- **Tests enforce** that templates and normalised question text don't overlap between splits.
- **Templates without slots repeat** (same text, different patients), so results are also reported per template.
- **Same author:** I wrote all the templates and designed the planner. The vocabulary of test phrasings isn't independent of the designer.
