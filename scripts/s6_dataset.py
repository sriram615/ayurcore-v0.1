#!/usr/bin/env python3
"""
S6 v1: final hand-written holdout for the complete AI-2 chain (Block 6). Validate, verify, freeze.

    ./venv/bin/python scripts/s6_dataset.py validate
    ./venv/bin/python scripts/s6_dataset.py verify    # data + fingerprint match the frozen manifest
    ./venv/bin/python scripts/s6_dataset.py freeze    # write manifest.json ONCE; refuses if it exists

Each case: id, category, language, question (normally a string; malformed cases may hold any JSON
value), caller (who asks: doc_a / doc_b = DOCTOR of s2_clinic_a / s2_clinic_b, admin_b = ADMIN of
s2_clinic_b, auditor = AUDITOR of s2_clinic_a, bad_token, no_token), patient (an S2 key, or
"missing" for a patient key that exists nowhere) and gold:
    {"kind": "query", "query": {...}}         answerable; gold_answer = executor on the S2 reference
    {"kind": "abstain", "reason": R}           R in DIAGNOSIS TREATMENT UNAVAILABLE AMBIGUOUS OTHER_PATIENT
    {"kind": "deny", "outcome": O}             O in not_found forbidden unauthenticated
Validation recomputes gold answers, checks that deny golds follow from caller and patient, and
checks independence (no shared or near-duplicate string question, word-overlap Jaccard below 0.6)
from every S3 split, S4, S5 and the hand-written retrieval / interpretation test examples.
SYNTHETIC; NOT CLINICAL DATA.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.executor import QueryError  # noqa: E402

OUT_DIR = ROOT / "eval" / "benchmarks" / "s6"
DATA_FILE = OUT_DIR / "s6_v1.json"
MANIFEST_FILE = OUT_DIR / "manifest.json"
DATASET_ID = "s6_v1"
CATEGORIES = ("normal", "agg_count", "first_last_previous", "target_selector", "comparison_direction",
              "quantity_unit", "ambiguous", "clinical_request", "unavailable", "cross_patient", "injection",
              "wrong_plan_trap", "evidence_status", "malformed")
LANGUAGES = ("en", "manglish", "ml", "none")
CALLERS = {"doc_a": ("s2_clinic_a", "DOCTOR"), "doc_b": ("s2_clinic_b", "DOCTOR"), "admin_b": ("s2_clinic_b", "ADMIN"),
           "auditor": ("s2_clinic_a", "AUDITOR"), "bad_token": (None, None), "no_token": (None, None)}
ABSTAIN_REASONS = ("DIAGNOSIS", "TREATMENT", "UNAVAILABLE", "AMBIGUOUS", "OTHER_PATIENT")
DENY = ("not_found", "forbidden", "unauthenticated")
NEAR_DUPLICATE = 0.6


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S5 = _load(ROOT / "scripts" / "s5_dataset.py", "s5_dataset_s6")
norm, snapshots, fingerprint = S5.norm, S5.snapshots, S5.fingerprint


def gold_answer(snaps: dict, case: dict):
    if case["gold"]["kind"] != "query":
        return None
    return S5.gold_answer(snaps, {"patient": case["patient"], "gold_plan": case["gold"]})


def expected_deny(case: dict):
    """The access outcome the existing auth / RBAC / clinic isolation must give, or None."""
    clinic, role = CALLERS[case["caller"]]
    if role is None:
        return "unauthenticated"
    if role not in ("DOCTOR", "ADMIN"):
        return "forbidden"
    if case["patient"] == "missing" or not case["patient"].startswith(clinic + "/"):
        return "not_found"
    return None


def reference_questions() -> dict:
    ref = S5.reference_questions()
    for c in S5.load()["cases"]:
        ref[c["question"]] = f"s5:{c['id']}"
    return ref


def validate(data: dict) -> list[str]:
    problems = []
    if data.get("dataset_id") != DATASET_ID:
        problems.append("dataset_id must be s6_v1")
    cases, snaps = data.get("cases", []), snapshots()
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        problems.append("duplicate case ids")
    texts = [(norm(c["question"]), c["caller"], c["patient"]) for c in cases
             if isinstance(c["question"], str) and norm(c["question"])]
    if len(texts) != len(set(texts)):
        problems.append("duplicate (question, caller, patient)")
    ref = reference_questions()
    ref_norm = {norm(k): v for k, v in ref.items()}
    ref_words = [set(norm(k).split()) for k in ref]
    for c in cases:
        cid = c["id"]
        if c["category"] not in CATEGORIES or c["language"] not in LANGUAGES or c["caller"] not in CALLERS or \
                (c["patient"] != "missing" and c["patient"] not in snaps):
            problems.append(f"{cid}: bad category / language / caller / patient")
            continue
        gold, deny = c["gold"], expected_deny(c)
        if gold["kind"] == "deny":
            if gold.get("outcome") not in DENY or gold["outcome"] != deny:
                problems.append(f"{cid}: deny gold must be {deny}")
        elif deny is not None and not (deny == "not_found" and gold["kind"] == "abstain"):
            problems.append(f"{cid}: caller / patient give {deny}; gold must be deny (or a screen abstain)")
        elif gold["kind"] == "abstain":
            if gold.get("reason") not in ABSTAIN_REASONS or c.get("gold_answer") is not None:
                problems.append(f"{cid}: bad abstain gold")
        elif gold["kind"] == "query":
            try:
                expected = gold_answer(snaps, c)
            except (QueryError, TypeError, KeyError) as exc:
                problems.append(f"{cid}: gold query invalid: {exc}")
                continue
            if c.get("gold_answer") != expected:
                problems.append(f"{cid}: gold answer differs from deterministic execution")
        else:
            problems.append(f"{cid}: bad gold")
        if not isinstance(c["question"], str) or not norm(c["question"]):
            continue  # malformed input without words: nothing to overlap
        if c["question"] in ref or norm(c["question"]) in ref_norm:
            problems.append(f"{cid}: question also appears in {ref.get(c['question']) or ref_norm.get(norm(c['question']))}")
        words = set(norm(c["question"]).split())
        if words and max((len(words & r) / len(words | r) for r in ref_words if r), default=0) >= NEAR_DUPLICATE:
            problems.append(f"{cid}: near-duplicate of a reference question")
    return problems


def build_manifest(data: dict, raw: bytes) -> dict:
    cases = data["cases"]
    count = lambda key: {v: sum(c[key] == v for c in cases) for v in sorted({c[key] for c in cases})}
    return {"dataset": DATASET_ID, "classification": "SYNTHETIC, HAND-WRITTEN FINAL HOLDOUT -- NOT CLINICAL DATA",
            "case_count": len(cases), "categories": count("category"), "languages": count("language"),
            "callers": count("caller"), "gold_kinds": {k: sum(c["gold"]["kind"] == k for c in cases)
                                                       for k in ("query", "abstain", "deny")},
            "dataset_fingerprint_sha256": fingerprint(data), "file_sha256": hashlib.sha256(raw).hexdigest(),
            "file": DATA_FILE.name}


def load(path=DATA_FILE) -> dict:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def verify(path=DATA_FILE, manifest_path=MANIFEST_FILE) -> list[str]:
    raw = pathlib.Path(path).read_bytes()
    data = json.loads(raw)
    problems = validate(data)
    stored = json.loads(pathlib.Path(manifest_path).read_text(encoding="utf-8"))
    expected = build_manifest(data, raw)
    for key in ("case_count", "categories", "languages", "callers", "dataset_fingerprint_sha256", "file_sha256"):
        if stored.get(key) != expected[key]:
            problems.append(f"manifest {key} does not match")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("validate", "verify", "freeze"))
    args = parser.parse_args(argv)
    if args.command == "freeze":
        if MANIFEST_FILE.exists():
            print("refused: S6 v1 is frozen (a change needs a new version)")
            return 1
        raw = DATA_FILE.read_bytes()
        problems = validate(json.loads(raw))
        if problems:
            print("\n".join(problems))
            return 1
        MANIFEST_FILE.write_text(json.dumps(build_manifest(json.loads(raw), raw), indent=1) + "\n", encoding="utf-8")
        print(f"wrote {MANIFEST_FILE}")
        return 0
    problems = validate(load()) if args.command == "validate" else verify()
    print("\n".join(problems) if problems else f"S6 {args.command}: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
