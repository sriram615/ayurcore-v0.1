#!/usr/bin/env python3
"""
S1-OOD v2 (safety benchmark) dataset tool: validate, verify, freeze.

    ./venv/bin/python scripts/s1_ood_v2_dataset.py validate
    ./venv/bin/python scripts/s1_ood_v2_dataset.py verify    # file + fingerprint match the frozen manifest
    ./venv/bin/python scripts/s1_ood_v2_dataset.py freeze    # write manifest.json ONCE; refuses if it exists

The dataset (eval/benchmarks/s1_ood_v2/s1_ood_v2.json) is hand-authored; this
tool never generates cases. It uses the existing case-file schema plus the S1-OOD
`ood` block (category, phenomena, spans, abstain) and a `safety` block:
    target_rules  subset of R1..R5 and/or "control" (a legitimate note that must not be rejected)
    unaccounted   [[amount text, occurrence]]: stated gram doses no output fact may use (R5 should warn)
Span resolution and the fingerprint are imported from scripts/s1_ood_dataset.py
(S1-OOD v1's tool), which is not modified. SYNTHETIC; NOT CLINICAL VALIDATION.
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

from backend.herb_resolver import UnknownHerbError, normalize_herb  # noqa: E402

_spec = importlib.util.spec_from_file_location("s1_ood_v1_tool", ROOT / "scripts" / "s1_ood_dataset.py")
v1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v1)

OOD_DIR = ROOT / "eval" / "benchmarks" / "s1_ood_v2"
DATA_FILE = OOD_DIR / "s1_ood_v2.json"
MANIFEST_FILE = OOD_DIR / "manifest.json"
DATASET_ID = "s1_ood_v2"
CASE_ID_PREFIX = "ood2-"
CATEGORIES = ("r1_conflict", "r2_alternative", "r3_history", "r4_negation_replacement", "r5_unaccounted",
              "cross_rule", "clean_control", "malayalam_manglish")
RULES = ("R1", "R2", "R3", "R4", "R5", "control")
ABSTAIN_REASONS = ("conflicting_quantities", "alternative_quantities", "alternative_herbs", "range", "contested_status")
FORMULARY = v1.FORMULARY
HERB_TYPES = v1.HERB_TYPES
fingerprint = v1.fingerprint
resolve_spans = v1.resolve_spans
OODDataError = v1.OODDataError


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _occurrence(note: str, text: str, nth: int) -> int | None:
    i = -1
    for _ in range(nth + 1):
        i = note.find(text, i + 1)
        if i == -1:
            return None
    return i


def validate_cases(data: dict) -> list[str]:
    """Every problem found (empty list = valid)."""
    problems: list[str] = []
    try:
        v1._harness().validate_case_file(data)
    except Exception as exc:  # CaseFileError carries the list of problems
        return [f"schema: {p}" for p in (exc.args[0] if exc.args and isinstance(exc.args[0], list) else [str(exc)])]
    if data.get("corpus_id") != DATASET_ID:
        problems.append(f"corpus_id {data.get('corpus_id')!r} is not {DATASET_ID!r}")
    seen = set()
    for case in data["cases"]:
        cid = case["case_id"]
        if cid in seen:
            problems.append(f"duplicate case_id {cid}")
        seen.add(cid)
        if not cid.startswith(CASE_ID_PREFIX):
            problems.append(f"{cid}: case_id must start with {CASE_ID_PREFIX!r}")
        ood, safety = case.get("ood"), case.get("safety")
        if not isinstance(ood, dict) or ood.get("category") not in CATEGORIES:
            problems.append(f"{cid}: ood.category must be one of {CATEGORIES}")
            continue
        if case.get("label") != ood["category"]:
            problems.append(f"{cid}: label must equal ood.category")
        if not isinstance(safety, dict) or not safety.get("target_rules") \
                or not set(safety["target_rules"]) <= set(RULES):
            problems.append(f"{cid}: safety.target_rules must be a non-empty subset of {RULES}")
            continue
        try:
            spans = resolve_spans(case)
        except OODDataError as exc:
            problems.append(str(exc))
            continue
        expected = case["expected"]["herbs"] or {}
        abstain = {a["herb"]: a["reason"] for a in ood.get("abstain", [])}
        for herb, reason in abstain.items():
            if reason not in ABSTAIN_REASONS:
                problems.append(f"{cid}: abstain reason {reason!r} not in {ABSTAIN_REASONS}")
            if herb not in FORMULARY:
                problems.append(f"{cid}: abstain item {herb!r} is not a formulary herb")
        rx = {c for _, _, t, c in spans if t == "HERB_RX"}
        for s, e, t, c in spans:
            if t in HERB_TYPES and not c:
                problems.append(f"{cid}: herb span {case['notes'][s:e]!r} has no canonical annotation")
            if t in HERB_TYPES[:4] and c not in FORMULARY:
                problems.append(f"{cid}: {c!r} is not a formulary herb")
            if t == "UNSTOCKED":
                try:
                    normalize_herb(c)
                    problems.append(f"{cid}: UNSTOCKED {c!r} resolves to a formulary herb")
                except UnknownHerbError:
                    pass
                if c not in case.get("mentioned_not_stocked", []):
                    problems.append(f"{cid}: UNSTOCKED {c!r} missing from mentioned_not_stocked")
        if rx != set(expected) | set(abstain):
            problems.append(f"{cid}: HERB_RX spans {sorted(rx)} do not match expected herbs + abstain items")
        if set(expected) & set(abstain):
            problems.append(f"{cid}: herb(s) {sorted(set(expected) & set(abstain))} both expected and to be abstained")
        if expected and not any(t == "QTY" for _, _, t, _ in spans):
            problems.append(f"{cid}: expected herbs but no QTY span")
        if bool(case["expected"]["dosha"]) != any(t == "DOSHA" for _, _, t, _ in spans):
            problems.append(f"{cid}: dosha gold and DOSHA span disagree")
        qty_spans = {(s, e) for s, e, t, _ in spans if t in ("QTY", "QTY_OTHER", "QTY_OLD")}
        for item in safety.get("unaccounted", []):
            start = _occurrence(case["notes"], item[0], item[1]) if isinstance(item, list) and len(item) == 2 else None
            if start is None or (start, start + len(item[0])) not in qty_spans:
                problems.append(f"{cid}: unaccounted {item!r} is not an annotated quantity span")
        if "control" in safety["target_rules"] and ood.get("abstain"):
            problems.append(f"{cid}: a control case must not require abstention")
    return problems


def load(path=DATA_FILE) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_manifest(data: dict, file_bytes: bytes) -> dict:
    cases = data["cases"]
    return {
        "dataset": DATASET_ID,
        "classification": "SYNTHETIC, HAND-AUTHORED SAFETY BENCHMARK FOR T3 R1-R5 -- NOT CLINICAL VALIDATION",
        "case_count": len(cases),
        "category_counts": {c: sum(1 for x in cases if x["ood"]["category"] == c) for c in CATEGORIES},
        "target_rule_counts": {r: sum(1 for x in cases if r in x["safety"]["target_rules"]) for r in RULES},
        "control_cases": sum(1 for x in cases if "control" in x["safety"]["target_rules"]),
        "abstain_items": sum(len(x["ood"]["abstain"]) for x in cases),
        "unaccounted_items": sum(len(x["safety"]["unaccounted"]) for x in cases),
        "language_counts": {lang: sum(1 for x in cases if lang in x["ood"]["phenomena"])
                            for lang in ("english", "manglish", "malayalam_script")},
        "dataset_fingerprint_sha256": fingerprint(cases),
        "file_sha256": _sha256_bytes(file_bytes),
        "file": DATA_FILE.name,
    }


MANIFEST_KEYS = ("case_count", "category_counts", "target_rule_counts", "control_cases", "abstain_items",
                 "unaccounted_items", "language_counts", "dataset_fingerprint_sha256", "file_sha256")


def verify(path=DATA_FILE, manifest_path=MANIFEST_FILE) -> list[str]:
    raw = pathlib.Path(path).read_bytes()
    data = json.loads(raw)
    problems = validate_cases(data)
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    expected = build_manifest(data, raw)
    for key in MANIFEST_KEYS:
        if manifest.get(key) != expected[key]:
            problems.append(f"manifest {key} does not match the data ({manifest.get(key)!r} vs {expected[key]!r})")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("validate", "verify", "freeze"))
    args = parser.parse_args(argv)
    if args.command == "freeze":
        if MANIFEST_FILE.exists():
            print(f"refused: {MANIFEST_FILE} exists; v2 is frozen (a change needs a new version)")
            return 1
        raw = DATA_FILE.read_bytes()
        data = json.loads(raw)
        problems = validate_cases(data)
        if problems:
            print("\n".join(problems))
            return 1
        MANIFEST_FILE.write_text(json.dumps(build_manifest(data, raw), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {MANIFEST_FILE}")
        return 0
    problems = validate_cases(load()) if args.command == "validate" else verify()
    print("\n".join(problems) if problems else f"S1-OOD v2 {args.command}: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
