# S2 v1 — synthetic multi-visit patient histories (AI-2)

**SYNTHETIC DATA ONLY — NOT CLINICAL DATA, NOT CLINICAL VALIDATION.** This is the history dataset for the deterministic AI-2 memory foundation (`memory/`).

| File | Content |
|---|---|
| `s2_v1.json` | A deterministic specification (seed `20261006`): clinics, patients and ordered visits |
| `manifest.json` | Counts, dataset fingerprint (sha256 of the canonical `clinics` block) and file sha256 |

- **Generate:** `python scripts/s2_generator.py generate`. It refuses if the files already exist.
- **Check:** `python scripts/s2_generator.py verify`. It regenerates the spec from the seed, which must equal the file and the manifest.
- **Dataset fingerprint:** `d25c3235c33e36febb386f74aeb868c456f723452559e0e3a18f14e2ac50c440`.

## Contents

- **2 clinics, 20 patients, 70 visits.** Both clinics use the same clinic-patient IDs (`S2-P01` … `S2-P10`), so clinic isolation is exercised.
- **Visit kinds:**

  | Kind | Count | What it is |
  |---|---|---|
  | `rx` | 53 | an encounter with one confirmed prescription |
  | `encounter_only` | 11 | an encounter closed without a prescription |
  | `legacy_unknown` | 4 | an old, unstructured prescription row; dosha and herbs stay **unknown** |
  | `legacy_readable` | 2 | an old row in the exact legacy description format, which the store's existing compatibility reader returns as known values |

- **Fixed patient shapes:** one patient per clinic with no history at all, and patients with unknown, readable-legacy and encounter-only visits.
- **Plausible variation:** herbs carry over between visits with changed amounts, are dropped and added, so `diff` and `compare` have something to find.

## How it becomes data

`memory/s2_build.py` writes the spec into a **new, isolated SQLite file** (never `db/ayurcore.db`). It creates a doctor per clinic and synthetic stock, then for each visit goes through the real API with FastAPI's TestClient:
- register the patient;
- open an encounter;
- manual preview (no AI call);
- confirm;
- close the encounter.

Legacy rows are inserted directly as prescription rows without structured values, which is how pre-structured data exists.

**Chronology is visit order.** Confirmation timestamps come from the database clock when the spec is built, so S2 encodes no calendar dates. Events of different kinds in the same second are flagged by the timeline.

## Use

- Correct answers are always recomputed from the specification or the snapshot; S2 isn't a one-shot holdout.
- Questions with gold answers belong to S3, a later block.
- A change to S2 means a new version (`s2_v2`), with a new fingerprint.
