#!/usr/bin/env python3
"""
S4 v1: hand-written adversarial memory-question holdout (AI-2). Validate, verify, freeze.

    ./venv/bin/python scripts/s4_dataset.py validate
    ./venv/bin/python scripts/s4_dataset.py verify    # data + fingerprint match the frozen manifest
    ./venv/bin/python scripts/s4_dataset.py freeze    # write manifest.json ONCE; refuses if it exists

Each case: id, category, language, question, patient (an S2 key), gold_plan
({"kind": "query", "query": {...}} or {"kind": "abstain", "reason": ...}) and gold_answer
(for a query: the deterministic executor's status, value and sources on the S2 reference
snapshot of that patient). Validation recomputes every gold answer, checks the gold queries
against the closed vocabulary and checks independence from S3 (no shared or near-duplicate
question; word-overlap Jaccard below 0.6) and from the hand-written retrieval test examples.
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

from memory.executor import Query, QueryError, execute  # noqa: E402
from memory.s2_reference import reference_snapshots  # noqa: E402

OUT_DIR = ROOT / "eval" / "benchmarks" / "s4"
DATA_FILE = OUT_DIR / "s4_v1.json"
MANIFEST_FILE = OUT_DIR / "manifest.json"
DATASET_ID = "s4_v1"
CATEGORIES = ("rec_disguised", "diag_disguised", "treatment_intent", "unavailable", "ambiguous", "cross_patient",
              "injection", "legit")
REASONS = ("DIAGNOSIS", "TREATMENT", "UNAVAILABLE", "AMBIGUOUS", "OTHER_PATIENT")
LANGUAGES = ("en", "manglish", "ml")
NEAR_DUPLICATE = 0.6


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def norm(text: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())


def snapshots() -> dict:
    return reference_snapshots(json.loads((ROOT / "eval" / "benchmarks" / "s2" / "s2_v1.json").read_text(encoding="utf-8")))


def gold_answer(snaps: dict, case: dict):
    if case["gold_plan"]["kind"] != "query":
        return None
    r = execute(snaps[case["patient"]], Query.from_dict(case["gold_plan"]["query"]))
    return {"status": r.status, "value": json.loads(json.dumps(r.value)), "sources": sorted(r.labels)}


def reference_questions() -> dict:
    """Questions S4 must be independent of: all S3 splits and the retrieval test examples."""
    s3 = _load(ROOT / "scripts" / "s3_generator.py", "s3_generator_s4")
    ref = {q["question"]: f"s3:{q['id']}" for s in ("train", "dev", "test") for q in s3.load(s)}
    import ast
    tree = ast.parse((ROOT / "tests" / "test_memory_retrieval.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") in ("SCREEN_ABSTAIN", "SCREEN_PASS") for t in node.targets):
            for elt in ast.literal_eval(node.value):
                ref[elt[0] if isinstance(elt, tuple) else elt] = "retrieval_tests"
    return ref


def validate(data: dict) -> list[str]:
    problems = []
    if data.get("dataset_id") != DATASET_ID:
        problems.append("dataset_id must be s4_v1")
    cases, snaps = data.get("cases", []), snapshots()
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        problems.append("duplicate case ids")
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
            if plan.get("reason") not in REASONS or c["category"] == "legit":
                problems.append(f"{cid}: bad abstain gold")
        elif plan["kind"] == "query":
            if c["category"] != "legit":
                problems.append(f"{cid}: only legit cases have a query gold")
            try:
                expected = gold_answer(snaps, c)
            except (QueryError, TypeError) as exc:
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


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data["cases"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def build_manifest(data: dict, raw: bytes) -> dict:
    cases = data["cases"]
    count = lambda key: {v: sum(c[key] == v for c in cases) for v in sorted({c[key] for c in cases})}
    return {"dataset": DATASET_ID, "classification": "SYNTHETIC, HAND-WRITTEN ADVERSARIAL HOLDOUT -- NOT CLINICAL DATA",
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
            print("refused: S4 v1 is frozen (a change needs a new version)")
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
    print("\n".join(problems) if problems else f"S4 {args.command}: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
