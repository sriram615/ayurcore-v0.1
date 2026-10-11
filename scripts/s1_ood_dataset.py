#!/usr/bin/env python3
"""
S1-OOD v1 dataset tool: validate, fingerprint, and (re)write the manifest.

    ./venv/bin/python scripts/s1_ood_dataset.py validate
    ./venv/bin/python scripts/s1_ood_dataset.py verify      # file + fingerprint match the manifest
    ./venv/bin/python scripts/s1_ood_dataset.py manifest    # write manifest.json (only when the data is meant to change)

The dataset (eval/benchmarks/s1_ood/s1_ood_v1.json) is hand-authored; this tool
never generates cases. It uses the existing case-file schema, so the existing
grader can grade it, and adds an `ood` block per case:
    category, phenomena, spans [[text, type, occurrence, canonical-or-null]],
    abstain [{herb, reason}]  (items the system must NOT emit)
SYNTHETIC; NOT CLINICAL VALIDATION.
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

from backend.herb_resolver import HERB_ALIASES, UnknownHerbError, normalize_herb  # noqa: E402
from extractor.labels import SPAN_TYPES, tokenize  # noqa: E402

OOD_DIR = ROOT / "eval" / "benchmarks" / "s1_ood"
DATA_FILE = OOD_DIR / "s1_ood_v1.json"
MANIFEST_FILE = OOD_DIR / "manifest.json"
DATASET_ID = "s1_ood_v1"
CASE_ID_PREFIX = "ood-"
CATEGORIES = ("clean", "alias", "malayalam_script", "manglish", "negation_replacement", "distractor", "units",
              "shared_correction", "irrelevant_numbers", "punctuation_order", "abstention")
FORMULARY = frozenset(HERB_ALIASES.values())
HERB_TYPES = ("HERB_RX", "HERB_NEG", "HERB_HIST", "HERB_REPL", "UNSTOCKED")
HARNESS_PATH = ROOT / "scripts" / "clinical_eval_harness.py"


class OODDataError(ValueError):
    """The dataset or one of its cases is invalid."""


def _harness():
    spec = importlib.util.spec_from_file_location("clinical_eval_harness_ood", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(cases: list[dict]) -> str:
    """sha256 of the canonical JSON of all cases, ordered by case_id."""
    canonical = json.dumps(sorted(cases, key=lambda c: c["case_id"]), sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return _sha256_bytes(canonical.encode("utf-8"))


def resolve_spans(case: dict) -> list[tuple[int, int, str, str | None]]:
    """Annotated spans -> [(start, end, type, canonical)], token-aligned, sorted.
    Raises OODDataError if a span cannot be located or spans overlap."""
    note, cid = case["notes"], case["case_id"]
    tokens = tokenize(note)
    starts, ends = {s for _, s, _ in tokens}, {e for _, _, e in tokens}
    out = []
    for item in case["ood"]["spans"]:
        text, kind = item[0], item[1]
        nth = item[2] if len(item) > 2 else 0
        canonical = item[3] if len(item) > 3 else None
        if kind not in SPAN_TYPES:
            raise OODDataError(f"{cid}: unknown span type {kind!r}")
        hits, i = [], note.find(text)
        while i != -1:
            if i in starts and i + len(text) in ends:
                hits.append(i)
            i = note.find(text, i + 1)
        if not isinstance(nth, int) or nth >= len(hits):
            raise OODDataError(f"{cid}: span {text!r} occurrence {nth} not found on token boundaries")
        out.append((hits[nth], hits[nth] + len(text), kind, canonical))
    out.sort()
    for a, b in zip(out, out[1:]):
        if b[0] < a[1]:
            raise OODDataError(f"{cid}: overlapping spans {a} and {b}")
    return out


def validate_cases(data: dict) -> list[str]:
    """Every problem found (empty list = valid). Schema via the existing grader's
    validator, then the OOD-specific gold checks."""
    problems: list[str] = []
    try:
        _harness().validate_case_file(data)
    except Exception as exc:  # CaseFileError carries the list of problems
        problems += [f"schema: {p}" for p in (exc.args[0] if exc.args and isinstance(exc.args[0], list) else [str(exc)])]
        return problems
    if not str(data.get("corpus_id", "")).startswith("s1_ood"):
        problems.append(f"corpus_id {data.get('corpus_id')!r} is not an S1-OOD corpus")
    seen = set()
    for case in data["cases"]:
        cid = case["case_id"]
        if cid in seen:
            problems.append(f"duplicate case_id {cid}")
        seen.add(cid)
        if not cid.startswith(CASE_ID_PREFIX):
            problems.append(f"{cid}: case_id must start with {CASE_ID_PREFIX!r}")
        ood = case.get("ood")
        if not isinstance(ood, dict) or ood.get("category") not in CATEGORIES:
            problems.append(f"{cid}: ood.category must be one of {CATEGORIES}")
            continue
        try:
            spans = resolve_spans(case)
        except OODDataError as exc:
            problems.append(str(exc))
            continue
        expected = case["expected"]["herbs"] or {}
        abstain = {a["herb"]: a["reason"] for a in ood.get("abstain", [])}
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
        if rx != set(expected) | {h for h in abstain if h in FORMULARY}:
            problems.append(f"{cid}: HERB_RX spans {sorted(rx)} do not match expected herbs + abstain items")
        if set(expected) & set(abstain):
            problems.append(f"{cid}: herb(s) {sorted(set(expected) & set(abstain))} both expected and to be abstained")
        if expected and not any(t == "QTY" for _, _, t, _ in spans):
            problems.append(f"{cid}: expected herbs but no QTY span")
        has_dosha_span = any(t == "DOSHA" for _, _, t, _ in spans)
        if bool(case["expected"]["dosha"]) != has_dosha_span:
            problems.append(f"{cid}: dosha gold and DOSHA span disagree")
        if case.get("label") != ood["category"]:
            problems.append(f"{cid}: label must equal ood.category")
    return problems


def load(path=DATA_FILE) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_manifest(data: dict, file_bytes: bytes) -> dict:
    cases = data["cases"]
    counts = {c: sum(1 for x in cases if x["ood"]["category"] == c) for c in CATEGORIES}
    return {
        "dataset": DATASET_ID,
        "classification": "SYNTHETIC, HAND-AUTHORED OUT-OF-DISTRIBUTION SET -- NOT CLINICAL VALIDATION",
        "case_count": len(cases),
        "category_counts": counts,
        "abstain_items": sum(len(x["ood"]["abstain"]) for x in cases),
        "dataset_fingerprint_sha256": fingerprint(cases),
        "file_sha256": _sha256_bytes(file_bytes),
        "file": DATA_FILE.name,
    }


def verify(path=DATA_FILE, manifest_path=MANIFEST_FILE) -> list[str]:
    raw = pathlib.Path(path).read_bytes()
    data = json.loads(raw)
    problems = validate_cases(data)
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    expected = build_manifest(data, raw)
    for key in ("case_count", "category_counts", "abstain_items", "dataset_fingerprint_sha256", "file_sha256"):
        if manifest.get(key) != expected[key]:
            problems.append(f"manifest {key} does not match the data ({manifest.get(key)!r} vs {expected[key]!r})")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("validate", "verify", "manifest"))
    args = parser.parse_args(argv)
    if args.command == "manifest":
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
    print("\n".join(problems) if problems else f"S1-OOD {args.command}: OK")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
