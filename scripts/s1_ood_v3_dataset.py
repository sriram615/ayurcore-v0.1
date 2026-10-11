#!/usr/bin/env python3
"""
S1-OOD v3 (final safety benchmark) dataset tool: validate, verify, freeze.

    ./venv/bin/python scripts/s1_ood_v3_dataset.py validate
    ./venv/bin/python scripts/s1_ood_v3_dataset.py verify    # file + fingerprint match the frozen manifest
    ./venv/bin/python scripts/s1_ood_v3_dataset.py freeze    # write manifest.json ONCE; refuses if it exists

Same case format and gold checks as S1-OOD v2: this module loads a private copy
of scripts/s1_ood_v2_dataset.py (which is not modified) and points it at the v3
files, corpus id "s1_ood_v3" and case-id prefix "ood3-". The dataset is
hand-authored; this tool never generates cases. SYNTHETIC; NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("s1_ood_v3_base", ROOT / "scripts" / "s1_ood_v2_dataset.py")
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

OOD_DIR = ROOT / "eval" / "benchmarks" / "s1_ood_v3"
DATA_FILE = OOD_DIR / "s1_ood_v3.json"
MANIFEST_FILE = OOD_DIR / "manifest.json"
DATASET_ID = "s1_ood_v3"
CASE_ID_PREFIX = "ood3-"
for _name in ("OOD_DIR", "DATA_FILE", "MANIFEST_FILE", "DATASET_ID", "CASE_ID_PREFIX"):
    setattr(_base, _name, globals()[_name])  # the private copy now validates and fingerprints v3

CATEGORIES = _base.CATEGORIES
RULES = _base.RULES
ABSTAIN_REASONS = _base.ABSTAIN_REASONS
validate_cases = _base.validate_cases
build_manifest = _base.build_manifest
fingerprint = _base.fingerprint
resolve_spans = _base.resolve_spans


def load(path=DATA_FILE) -> dict:  # v2's defaults were bound to the v2 paths at definition time
    return _base.load(path)


def verify(path=DATA_FILE, manifest_path=MANIFEST_FILE) -> list[str]:
    return _base.verify(path, manifest_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("validate", "verify", "freeze"))
    args = parser.parse_args(argv)
    if args.command == "freeze":
        if MANIFEST_FILE.exists():
            print(f"refused: {MANIFEST_FILE} exists; v3 is frozen (a change needs a new version)")
            return 1
        raw = DATA_FILE.read_bytes()
        data = json.loads(raw)
        problems = validate_cases(data)
        if problems:
            print("\n".join(problems))
            return 1
        manifest = build_manifest(data, raw)
        manifest["classification"] = "SYNTHETIC, HAND-AUTHORED FINAL SAFETY BENCHMARK FOR T3 v3 -- NOT CLINICAL VALIDATION"
        MANIFEST_FILE.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {MANIFEST_FILE}")
        return 0
    problems = validate_cases(load()) if args.command == "validate" else verify()
    print("\n".join(problems) if problems else f"S1-OOD v3 {args.command}: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
