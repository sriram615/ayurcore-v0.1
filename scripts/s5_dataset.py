#!/usr/bin/env python3
"""
S5 v1: hand-written semantic-misinterpretation holdout (AI-2 Block 5). Validate, verify, freeze.

    ./venv/bin/python scripts/s5_dataset.py validate
    ./venv/bin/python scripts/s5_dataset.py verify    # data + fingerprint match the frozen manifest
    ./venv/bin/python scripts/s5_dataset.py freeze    # write manifest.json ONCE; refuses if it exists

Each case: id, category, language, question, patient (an S2 key), gold_plan
({"kind": "query", "query": {...}} or {"kind": "abstain", "reason": "AMBIGUOUS"}) and gold_answer
(for a query: the deterministic executor's status, value and sources on the S2 reference snapshot).
Validation recomputes every gold answer, checks the queries against the closed vocabulary and checks
independence (no shared or near-duplicate question, word-overlap Jaccard below 0.6) from every S3
split, S4 and the hand-written retrieval / interpretation test examples.
SYNTHETIC; NOT CLINICAL DATA.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.executor import QueryError  # noqa: E402

OUT_DIR = ROOT / "eval" / "benchmarks" / "s5"
DATA_FILE = OUT_DIR / "s5_v1.json"
MANIFEST_FILE = OUT_DIR / "manifest.json"
DATASET_ID = "s5_v1"
CATEGORIES = ("agg_vs_count", "first_last_previous", "target_confusion", "selector_confusion",
              "comparison_direction", "quantity_unit", "legit_ambiguous", "control")
LANGUAGES = ("en", "manglish", "ml")
NEAR_DUPLICATE = 0.6


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


S4 = _load(ROOT / "scripts" / "s4_dataset.py", "s4_dataset_s5")
norm, snapshots, gold_answer, fingerprint = S4.norm, S4.snapshots, S4.gold_answer, S4.fingerprint


def reference_questions() -> dict:
    """Questions S5 must be independent of: S3 (all splits), S4, retrieval and interpretation tests."""
    ref = S4.reference_questions()
    for c in S4.load()["cases"]:
        ref[c["question"]] = f"s4:{c['id']}"
    tree = ast.parse((ROOT / "tests" / "test_memory_interpret.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") in ("INTERPRET_PLANNED", "INTERPRET_AMBIGUOUS")
                                                for t in node.targets):
            for elt in ast.literal_eval(node.value):
                ref[elt[0] if isinstance(elt, tuple) else elt] = "interpret_tests"
    return ref


def validate(data: dict) -> list[str]:
    problems = []
    if data.get("dataset_id") != DATASET_ID:
        problems.append("dataset_id must be s5_v1")
    cases, snaps = data.get("cases", []), snapshots()
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        problems.append("duplicate case ids")
    questions = [norm(c["question"]) for c in cases]
    if len(questions) != len(set(questions)):
        problems.append("duplicate questions")
    ref = reference_questions()
    ref_norm = {norm(k): v for k, v in ref.items()}
    ref_words = [set(norm(k).split()) for k in ref]
    for c in cases:
        cid = c["id"]
        if c["category"] not in CATEGORIES or c["language"] not in LANGUAGES or c["patient"] not in snaps:
            problems.append(f"{cid}: bad category / language / patient")
            continue
        plan = c["gold_plan"]
        if plan["kind"] == "abstain":
            if plan.get("reason") != "AMBIGUOUS" or c.get("gold_answer") is not None:
                problems.append(f"{cid}: bad abstain gold")
        elif plan["kind"] == "query":
            try:
                expected = gold_answer(snaps, c)
            except (QueryError, TypeError, KeyError) as exc:
                problems.append(f"{cid}: gold query invalid: {exc}")
                continue
            if c.get("gold_answer") != expected:
                problems.append(f"{cid}: gold answer differs from deterministic execution")
        else:
            problems.append(f"{cid}: bad gold plan")
        if c["question"] in ref or norm(c["question"]) in ref_norm:
            problems.append(f"{cid}: question also appears in {ref.get(c['question']) or ref_norm.get(norm(c['question']))}")
        words = set(norm(c["question"]).split())
        if words and max((len(words & r) / len(words | r) for r in ref_words if r), default=0) >= NEAR_DUPLICATE:
            problems.append(f"{cid}: near-duplicate of a reference question")
    return problems


def build_manifest(data: dict, raw: bytes) -> dict:
    cases = data["cases"]
    count = lambda key: {v: sum(c[key] == v for c in cases) for v in sorted({c[key] for c in cases})}
    return {"dataset": DATASET_ID, "classification": "SYNTHETIC, HAND-WRITTEN INTERPRETATION HOLDOUT -- NOT CLINICAL DATA",
            "case_count": len(cases), "categories": count("category"), "languages": count("language"),
            "abstain_cases": sum(c["gold_plan"]["kind"] == "abstain" for c in cases),
            "answerable_cases": sum(c["gold_plan"]["kind"] == "query" for c in cases),
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
    for key in ("case_count", "categories", "languages", "dataset_fingerprint_sha256", "file_sha256"):
        if stored.get(key) != expected[key]:
            problems.append(f"manifest {key} does not match")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("validate", "verify", "freeze"))
    args = parser.parse_args(argv)
    if args.command == "freeze":
        if MANIFEST_FILE.exists():
            print("refused: S5 v1 is frozen (a change needs a new version)")
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
    print("\n".join(problems) if problems else f"S5 {args.command}: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
