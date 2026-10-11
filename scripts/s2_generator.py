#!/usr/bin/env python3
"""
S2 v1: synthetic multi-visit patient histories for AI-2 (Consultation Memory).

    ./venv/bin/python scripts/s2_generator.py generate   # write eval/benchmarks/s2/s2_v1.json + manifest (refuses if present)
    ./venv/bin/python scripts/s2_generator.py verify     # regenerate from the seed; must equal the file and manifest

The dataset is a deterministic SPECIFICATION (fixed seed). memory/s2_build.py
materialises it through the real API (register patient, open encounter, manual
preview, confirm, close encounter) into an isolated database. Two clinics share the
same clinic-patient IDs, so clinic isolation is exercised.

Visit kinds:
    rx                one encounter with one confirmed prescription (dosha + herbs)
    encounter_only    an encounter closed without a prescription
    legacy_unknown    a pre-structured prescription row the store cannot read
                      (dosha and herbs UNKNOWN; inserted directly, as legacy data would be)
    legacy_readable   a pre-structured row in the exact legacy description format, which the
                      store's existing compatibility reader returns as known values

Chronology is visit order: confirmation timestamps come from the database clock when the
spec is materialised, so S2 deliberately encodes no calendar dates.
SYNTHETIC DATA ONLY; NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.herb_resolver import HERB_ALIASES  # noqa: E402

OUT_DIR = ROOT / "eval" / "benchmarks" / "s2"
DATA_FILE = OUT_DIR / "s2_v1.json"
MANIFEST_FILE = OUT_DIR / "manifest.json"
DATASET_ID = "s2_v1"
SCHEMA = "ayurcore.s2.histories/v1"
SEED = 20261006
CLINICS = ("s2_clinic_a", "s2_clinic_b")
PATIENTS_PER_CLINIC = 10
DOSHAS = ("Vata", "Pitta", "Kapha", "Vata-Pitta", "Pitta-Kapha", "Vata-Kapha")
GRAMS = (10, 15, 20, 25, 30, 40, 50, 60, 75, 80, 90, 100, 120, 150)
HERBS = tuple(sorted(set(HERB_ALIASES.values())))


def _herbs_next(rng: random.Random, previous: list) -> list:
    kept = []
    for name, grams in previous:
        if rng.random() < 0.7:
            kept.append([name, rng.choice(GRAMS) if rng.random() < 0.4 else grams])
    for _ in range(rng.choice((0, 0, 1, 1, 2))):
        name = rng.choice([h for h in HERBS if h not in {n for n, _ in kept}])
        kept.append([name, rng.choice(GRAMS)])
    if not kept:
        kept.append([rng.choice(HERBS), rng.choice(GRAMS)])
    return kept


def _patient(rng: random.Random, n_visits: int, forced: tuple = ()) -> list:
    visits, herbs = [], []
    kinds = list(forced) + ["rx"] * max(0, n_visits - len(forced))
    rng.shuffle(kinds)
    for kind in kinds:
        if kind == "rx" and rng.random() < 0.12:
            kind = "encounter_only"
        if kind in ("rx", "legacy_readable"):
            herbs = _herbs_next(rng, herbs)
            visits.append({"kind": kind, "dosha": rng.choice(DOSHAS), "herbs": [list(h) for h in herbs]})
        else:
            visits.append({"kind": kind})
    return visits


def generate(seed: int = SEED) -> dict:
    rng = random.Random(seed)
    clinics = []
    for c, clinic_id in enumerate(CLINICS):
        patients = []
        for i in range(PATIENTS_PER_CLINIC):
            if i == 0:
                visits = []                                        # no history at all
            elif i == 1:
                visits = _patient(rng, 5, forced=("legacy_unknown",))
            elif i == 2:
                visits = _patient(rng, 6, forced=("legacy_unknown", "legacy_readable"))
            elif i == 3:
                visits = _patient(rng, 3, forced=("encounter_only",))
            else:
                visits = _patient(rng, rng.choice((1, 2, 3, 4, 5, 6)))
            patients.append({"key": f"{clinic_id}/P{i + 1:02d}", "clinic_patient_id": f"S2-P{i + 1:02d}",
                             "display_name": f"S2 synthetic patient {'AB'[c]}{i + 1:02d}", "visits": visits})
        clinics.append({"clinic_id": clinic_id, "patients": patients})
    return {"schema": SCHEMA, "dataset_id": DATASET_ID, "seed": seed,
            "classification": "SYNTHETIC -- NOT CLINICAL DATA", "clinics": clinics}


def fingerprint(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec["clinics"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def counts(spec: dict) -> dict:
    visits = [v for c in spec["clinics"] for p in c["patients"] for v in p["visits"]]
    return {"clinics": len(spec["clinics"]), "patients": sum(len(c["patients"]) for c in spec["clinics"]),
            "visits": len(visits), **{k: sum(v["kind"] == k for v in visits)
                                      for k in ("rx", "encounter_only", "legacy_unknown", "legacy_readable")}}


def manifest(spec: dict, raw: bytes) -> dict:
    return {"dataset": DATASET_ID, "classification": "SYNTHETIC -- NOT CLINICAL DATA", "seed": spec["seed"],
            "counts": counts(spec), "dataset_fingerprint_sha256": fingerprint(spec),
            "file_sha256": hashlib.sha256(raw).hexdigest(), "file": DATA_FILE.name}


def verify() -> list[str]:
    problems = []
    raw = DATA_FILE.read_bytes()
    spec = json.loads(raw)
    regenerated = generate(spec.get("seed", SEED))
    if spec != regenerated:
        problems.append("file differs from the deterministic regeneration")
    stored = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    expected = manifest(spec, raw)
    for key in ("counts", "dataset_fingerprint_sha256", "file_sha256", "seed"):
        if stored.get(key) != expected[key]:
            problems.append(f"manifest {key} does not match")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("generate", "verify"))
    args = parser.parse_args(argv)
    if args.command == "generate":
        if DATA_FILE.exists() or MANIFEST_FILE.exists():
            print("refused: S2 v1 exists (a change needs a new version)")
            return 1
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        spec = generate()
        raw = (json.dumps(spec, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
        DATA_FILE.write_bytes(raw)
        MANIFEST_FILE.write_text(json.dumps(manifest(spec, raw), indent=1) + "\n", encoding="utf-8")
        print(f"wrote {DATA_FILE} and {MANIFEST_FILE}: {counts(spec)}")
        return 0
    problems = verify()
    print("\n".join(problems) if problems else "S2 verify: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
