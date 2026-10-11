#!/usr/bin/env python3
"""
T1: train the span tagger on S1 train, evaluate on S1 dev (and a weak
out-of-distribution check on the 12 independent v1 notes).

    ./venv/bin/python -m extractor.train [--out artifacts/models/t1_tagger] [--epochs 10] [--seed 20261005]

Writes <out>/model.json (weights + provenance) and <out>/report.json.
s1_test.json is the frozen holdout (eval/benchmarks/s1/README.md): it is
never opened here, and `load_cases` refuses it.

SYNTHETIC TECHNICAL RESULT, NOT CLINICAL VALIDATION. The tagger is trained on
notes from the same generator and templates as dev, so dev scores measure
in-distribution learning; they say little about unseen phrasing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from extractor.features import sentence_features  # noqa: E402
from extractor.labels import SPAN_TYPES, TAGS, AlignmentError, bio_tags, case_spans, tags_to_spans, tokenize  # noqa: E402
from extractor.tagger import StructuredPerceptron  # noqa: E402

S1_DIR = ROOT / "eval" / "benchmarks" / "s1"
TRAIN_FILE = S1_DIR / "s1_train.json"
DEV_FILE = S1_DIR / "s1_dev.json"
V1_FILE = ROOT / "eval" / "cases" / "clinical_eval_v1.json"
FORBIDDEN_FILE = "s1_test.json"
DEFAULT_OUT = ROOT / "artifacts" / "models" / "t1_tagger"
DEFAULT_EPOCHS = 10
DEFAULT_SEED = 20261005
HARD_TAGS = ("negation", "self_correction", "instead_of", "shared_quantity", "history_mention",
             "unstocked_mention", "no_punctuation", "misspelling", "inflection", "malayalam_script")
CLASSIFICATION = "SYNTHETIC TECHNICAL RESULT -- NOT CLINICAL VALIDATION"


class HoldoutError(RuntimeError):
    """Refused to read the frozen S1 test split."""


def _read_json(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _sha256(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load_cases(path) -> list[dict]:
    if pathlib.Path(path).name == FORBIDDEN_FILE:
        raise HoldoutError(f"{FORBIDDEN_FILE} is the frozen holdout; T1 never reads it")
    return _read_json(path)["cases"]


def code_sha256() -> str:
    h = hashlib.sha256()
    for name in ("__init__.py", "labels.py", "features.py", "tagger.py", "train.py"):
        with open(ROOT / "extractor" / name, "rb") as f:
            h.update(name.encode() + b"\0" + f.read())
    return h.hexdigest()


def build_examples(cases: list[dict]) -> tuple[list[dict], list[str]]:
    """[{case_id, tags(case), tokens, features, gold_tags, gold_spans}], alignment errors."""
    examples, errors = [], []
    for case in cases:
        try:
            spans = case_spans(case)
        except AlignmentError as exc:
            errors.append(str(exc))
            continue
        tokens = tokenize(case["notes"])
        examples.append({"case_id": case["case_id"], "label": case.get("label"), "case_tags": case.get("tags", []),
                         "tokens": tokens, "features": sentence_features(tokens),
                         "gold_tags": bio_tags(tokens, spans), "gold_spans": spans})
    return examples, errors


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if (p is not None and r is not None) else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f}


def evaluate(model: StructuredPerceptron, examples: list[dict]) -> dict:
    """Exact span match (start, end, type). Micro P/R/F1 overall and per type;
    token accuracy; per-case exact match overall, by category and by hard tag."""
    per_type = {t: [0, 0, 0] for t in SPAN_TYPES}
    tok_ok = tok_all = 0
    case_rows = []
    for ex in examples:
        pred_tags = model.predict(ex["features"])
        pred = set(tags_to_spans(ex["tokens"], pred_tags))
        gold = set(ex["gold_spans"])
        for t in SPAN_TYPES:
            g = {s for s in gold if s[2] == t}
            p = {s for s in pred if s[2] == t}
            per_type[t][0] += len(g & p)
            per_type[t][1] += len(p - g)
            per_type[t][2] += len(g - p)
        tok_ok += sum(a == b for a, b in zip(pred_tags, ex["gold_tags"]))
        tok_all += len(pred_tags)
        case_rows.append({"case_id": ex["case_id"], "label": ex["label"], "case_tags": ex["case_tags"],
                          "exact": pred == gold,
                          "missed": sorted(gold - pred), "spurious": sorted(pred - gold)})
    tp = sum(v[0] for v in per_type.values())
    fp = sum(v[1] for v in per_type.values())
    fn = sum(v[2] for v in per_type.values())

    def _exact_rate(rows):
        return {"cases": len(rows), "exact": sum(r["exact"] for r in rows),
                "exact_rate": (sum(r["exact"] for r in rows) / len(rows)) if rows else None}

    categories = sorted({r["label"] for r in case_rows if r["label"]})
    return {
        "spans_micro": _prf(tp, fp, fn),
        "spans_by_type": {t: _prf(*v) for t, v in per_type.items()},
        "token_accuracy": tok_ok / tok_all if tok_all else None,
        "cases_exact": _exact_rate(case_rows),
        "cases_exact_by_category": {c: _exact_rate([r for r in case_rows if r["label"] == c]) for c in categories},
        "cases_exact_by_hard_tag": {t: _exact_rate([r for r in case_rows if t in r["case_tags"]]) for t in HARD_TAGS},
        "errors": [r for r in case_rows if not r["exact"]],
    }


def v1_check(model: StructuredPerceptron) -> dict:
    """WEAK out-of-distribution check on the 12 independently written v1 notes,
    which have no span labels: are the expected herbs among the HERB_RX spans,
    when a span is mapped by the production resolver (exact alias lookup only,
    no fuzzy matching -- full normalisation is T2)? Herbs only; quantities,
    roles and dosha are not checked here."""
    from backend.herb_resolver import UnknownHerbError, normalize_herb
    rows, tp, fp, fn = [], 0, 0, 0
    for case in _read_json(V1_FILE)["cases"]:
        tokens = tokenize(case["notes"])
        spans = tags_to_spans(tokens, model.predict(sentence_features(tokens)))
        rx_texts = [case["notes"][s:e] for s, e, t in spans if t == "HERB_RX"]
        resolved = set()
        for text in rx_texts:
            try:
                resolved.add(normalize_herb(text))
            except UnknownHerbError:
                pass
        expected = set((case["expected"].get("herbs") or {}))
        tp += len(resolved & expected)
        fp += len(resolved - expected)
        fn += len(expected - resolved)
        rows.append({"case_id": case["case_id"], "expected_herbs": sorted(expected),
                     "herb_rx_spans": rx_texts, "resolved": sorted(resolved),
                     "predicted_spans": [(case["notes"][s:e], t) for s, e, t in spans]})
    return {"note": "weak OOD check: herb-level only, exact alias lookup, 12 notes", "herbs": _prf(tp, fp, fn),
            "cases": rows}


def train(out_dir=DEFAULT_OUT, epochs: int = DEFAULT_EPOCHS, seed: int = DEFAULT_SEED) -> dict:
    train_cases, dev_cases = load_cases(TRAIN_FILE), load_cases(DEV_FILE)
    train_ex, train_errors = build_examples(train_cases)
    dev_ex, dev_errors = build_examples(dev_cases)
    model = StructuredPerceptron(TAGS)
    history = model.fit([(ex["features"], ex["gold_tags"]) for ex in train_ex], epochs=epochs, seed=seed)
    manifest = _read_json(S1_DIR / "manifest.json")
    provenance = {
        "classification": CLASSIFICATION,
        "block": "AI-1 T1 span tagger",
        "model": "averaged structured perceptron, first-order, Viterbi (numpy)",
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "s1_dataset_fingerprint": manifest["dataset_fingerprint_sha256"],
        "train_file_sha256": _sha256(TRAIN_FILE), "dev_file_sha256": _sha256(DEV_FILE),
        "train_cases_used": len(train_ex), "dev_cases_used": len(dev_ex),
        "alignment_errors": {"train": train_errors, "dev": dev_errors},
        "epochs": epochs, "seed": seed, "tags": list(TAGS),
        "code_sha256": code_sha256(), "numpy_version": np.__version__, "python_version": platform.python_version(),
        "holdout": f"{FORBIDDEN_FILE} not read",
    }
    model_json = {**model.to_json(), "provenance": provenance}
    weights_sha = model.weights_sha256()
    report = {
        "classification": CLASSIFICATION, "provenance": provenance, "weights_sha256": weights_sha,
        "features_kept": len(model_json["weights"]), "training_history": history,
        "train": {k: v for k, v in evaluate(model, train_ex).items() if k != "errors"},
        "dev": evaluate(model, dev_ex),
        "v1_ood_check": v1_check(model),
    }
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "model.json", "w", encoding="utf-8") as f:
        json.dump(model_json, f, ensure_ascii=False)
    with open(out_dir / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, ensure_ascii=False)
    report["_paths"] = {"model": str(out_dir / "model.json"), "report": str(out_dir / "report.json")}
    return report


def load_model(path) -> StructuredPerceptron:
    return StructuredPerceptron.from_json(_read_json(path))


def _fmt(x) -> str:
    return "  -  " if x is None else f"{x:.3f}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)
    report = train(args.out, args.epochs, args.seed)
    dev = report["dev"]
    print(CLASSIFICATION)
    print(f"weights sha256 {report['weights_sha256']}  | alignment errors {report['provenance']['alignment_errors']}")
    print(f"dev spans micro P/R/F1 {_fmt(dev['spans_micro']['precision'])}/{_fmt(dev['spans_micro']['recall'])}/"
          f"{_fmt(dev['spans_micro']['f1'])}  token acc {_fmt(dev['token_accuracy'])}  "
          f"cases exact {dev['cases_exact']['exact']}/{dev['cases_exact']['cases']}")
    for t, m in dev["spans_by_type"].items():
        print(f"  {t:<10} P {_fmt(m['precision'])} R {_fmt(m['recall'])} F1 {_fmt(m['f1'])}  (gold {m['tp'] + m['fn']})")
    v1 = report["v1_ood_check"]["herbs"]
    print(f"v1 weak OOD herb P/R/F1 {_fmt(v1['precision'])}/{_fmt(v1['recall'])}/{_fmt(v1['f1'])}")
    print(f"model  : {report['_paths']['model']}\nreport : {report['_paths']['report']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
