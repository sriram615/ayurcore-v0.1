#!/usr/bin/env python3
"""
S1-OOD evaluator for the existing T1 -> T2 -> T3 extractor.

    ./venv/bin/python -m extractor.evaluate_ood --final [--dataset eval/benchmarks/s1_ood/s1_ood_v1.json]

The official S1-OOD file is a one-shot holdout: it is evaluated only with an
explicit --final (final=True), after the extractor is frozen, and nothing may be
tuned on the result. Without it the official file is refused. Any other file must
be an S1-OOD-format dataset (corpus_id "s1_ood*", case ids "ood-*", valid per
scripts/s1_ood_dataset.py) -- S1 splits are refused, so an S1 file cannot be
substituted by accident. Refusals happen before the model is loaded.

Reported separately:
  T1  span extraction vs the gold spans (exact start/end/type), per type and micro
  T2  ClinicalParse graded by the existing grader (pass, herb P/R, quantities, dosha)
  T3  verification verdicts; a rejected output is an abstention
  outcome analysis on what the system finally emits (nothing, if T3 rejected):
      correct facts, confident errors (wrong quantity, spurious herb, missed
      abstention, wrong dosha), correct abstentions, omissions
SYNTHETIC TECHNICAL RESULT, NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATASET_TOOL_PATH = ROOT / "scripts" / "s1_ood_dataset.py"
S1_DIR = ROOT / "eval" / "benchmarks" / "s1"
DEFAULT_OUT = ROOT / "artifacts" / "models" / "ood_eval"
CLASSIFICATION = "SYNTHETIC TECHNICAL RESULT -- NOT CLINICAL VALIDATION"


class OODEvaluationRefused(RuntimeError):
    """The requested dataset may not be evaluated (S1 substitution, invalid data, or no --final)."""


def _dataset_tool():
    spec = importlib.util.spec_from_file_location("s1_ood_dataset", DATASET_TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def check_dataset(path, final: bool) -> dict:
    """Load and vet a dataset; raises OODEvaluationRefused. Runs before any model is loaded."""
    tool = _dataset_tool()
    path = pathlib.Path(path).resolve()
    s1_hashes = {_sha256(p) for p in sorted(S1_DIR.glob("s1_*.json"))}
    if path.parent == S1_DIR.resolve() or path.name.startswith("s1_") and not path.name.startswith("s1_ood"):
        raise OODEvaluationRefused(f"{path.name} is an S1 split, not an S1-OOD dataset")
    if _sha256(path) in s1_hashes:
        raise OODEvaluationRefused(f"{path} has the same content as an S1 split")
    if path == tool.DATA_FILE.resolve():
        if not final:
            raise OODEvaluationRefused("the official S1-OOD set is a one-shot holdout: pass final=True / --final")
        problems = tool.verify(path)
    else:
        problems = tool.validate_cases(tool.load(path))
    if problems:
        raise OODEvaluationRefused("dataset invalid: " + "; ".join(problems[:5]))
    data = tool.load(path)
    if not all(c["case_id"].startswith(tool.CASE_ID_PREFIX) for c in data["cases"]):
        raise OODEvaluationRefused("case ids must start with 'ood-'")
    return data


def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f}


def _outcomes(case: dict, emitted: dict, emitted_dosha: list) -> dict:
    gold = {h: s["grams"] for h, s in (case["expected"]["herbs"] or {}).items()}
    abstain = {a["herb"] for a in case["ood"]["abstain"]}
    out = collections.Counter()
    for name, grams in emitted.items():
        if name in gold:
            out["correct_fact" if grams == gold[name] else "confident_error:wrong_quantity"] += 1
        elif name in abstain:
            out["confident_error:missed_abstention"] += 1
        else:
            out["confident_error:spurious_herb"] += 1
    out["omission"] += sum(1 for name in gold if name not in emitted)
    out["correct_abstention"] += sum(1 for name in abstain if name not in emitted)
    gold_dosha = set(case["expected"]["dosha"] or [])
    if emitted_dosha and set(emitted_dosha) != gold_dosha:
        out["confident_error:wrong_dosha"] += 1
    elif not emitted_dosha and gold_dosha:
        out["dosha_omission"] += 1
    elif emitted_dosha:
        out["correct_dosha"] += 1
    return collections.Counter({k: v for k, v in out.items() if v})


def run(dataset_path, model_path, out_dir=DEFAULT_OUT, final: bool = False) -> dict:
    data = check_dataset(dataset_path, final)  # refusals happen here, before the model is loaded
    from extractor import evaluate_t2, train as t1
    from extractor.assemble import assemble
    from extractor.labels import SPAN_TYPES
    from extractor.pipeline import Extractor
    from extractor.verify import verify

    tool = _dataset_tool()
    extractor = Extractor(t1.load_model(model_path))
    harness = evaluate_t2._harness()
    per_type = {t: [0, 0, 0] for t in SPAN_TYPES}
    rows, records, totals = [], [], collections.Counter()
    for case in data["cases"]:
        note = case["notes"]
        gold = {(s, e, t) for s, e, t, _ in tool.resolve_spans(case)}
        pred_spans = extractor.spans(note)
        pred = set(pred_spans)
        for t in SPAN_TYPES:
            g = {x for x in gold if x[2] == t}
            p = {x for x in pred if x[2] == t}
            per_type[t][0] += len(g & p)
            per_type[t][1] += len(p - g)
            per_type[t][2] += len(g - p)
        parsed, report = assemble(note, pred_spans)
        verdict = verify(note, pred_spans, parsed, report)
        emitted = {} if verdict["verdict"] != "verified" else {h.name: h.grams for h in parsed.prescribed_herbs}
        emitted_dosha = [] if verdict["verdict"] != "verified" or not parsed.dosha_imbalance else parsed.dosha_imbalance.split("-")
        outcome = _outcomes(case, emitted, emitted_dosha)
        totals.update(outcome)
        records.append({"case_id": case["case_id"], "repeat": 0, "arm": "extractor_t1_t2_t3", "status": "ok",
                        "parsed": parsed.model_dump(), "exception": None, "latency_ms": 0, "tool_calls": [],
                        "log_records": []})
        rows.append({"case_id": case["case_id"], "category": case["ood"]["category"], "t1_exact": pred == gold,
                     "t1_missed": sorted(gold - pred), "t1_spurious": sorted(pred - gold),
                     "t2_parse": parsed.model_dump(), "t3_verdict": verdict["verdict"],
                     "t3_violations": [v["code"] for v in verdict["violations"]],
                     "emitted": emitted, "emitted_dosha": emitted_dosha, "outcomes": dict(outcome)})
    graded = harness.build_graded_report(
        {"schema_version": harness.RAW_SCHEMA_VERSION, "metadata": {"arm": "extractor_t1_t2_t3"}, "records": records},
        data, None)
    grade_by_case = {g["case_id"]: g["outcome"] for g in graded["results"]}
    for row in rows:
        row["t2_grader_outcome"] = grade_by_case[row["case_id"]]
    categories = sorted({r["category"] for r in rows})
    confident = sum(v for k, v in totals.items() if k.startswith("confident_error"))
    emitted_facts = totals["correct_fact"] + sum(v for k, v in totals.items() if k.startswith("confident_error:") and k != "confident_error:wrong_dosha")
    clinical = graded["summary"]["clinical"]
    summary = {
        "classification": CLASSIFICATION,
        "dataset": data["corpus_id"], "dataset_fingerprint_sha256": tool.fingerprint(data["cases"]),
        "final_run": final, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "t1_weights_sha256": extractor.model.weights_sha256(), "cases": len(rows),
        "t1": {"spans_micro": _prf(*[sum(v[i] for v in per_type.values()) for i in range(3)]),
               "spans_by_type": {t: _prf(*v) for t, v in per_type.items()},
               "cases_exact": sum(r["t1_exact"] for r in rows),
               "cases_exact_by_category": {c: [sum(r["t1_exact"] for r in rows if r["category"] == c),
                                               sum(1 for r in rows if r["category"] == c)] for c in categories}},
        "t2": {"outcomes": graded["summary"]["outcomes"], "pass": clinical["passed"], "graded": clinical["graded_attempts"],
               "herb_precision": clinical["herb_micro_precision"], "herb_recall": clinical["herb_micro_recall"],
               "dosha_exact": clinical["dosha_exact"], "quantities_exact": clinical["quantities_within_tolerance"],
               "quantities_matched": clinical["matched_quantities"],
               "pass_by_category": {c: [sum(r["t2_grader_outcome"] == "PASS" for r in rows if r["category"] == c),
                                        sum(1 for r in rows if r["category"] == c)] for c in categories}},
        "t3": {"verified": sum(r["t3_verdict"] == "verified" for r in rows),
               "rejected_abstained": sum(r["t3_verdict"] == "rejected" for r in rows),
               "violations_by_code": dict(collections.Counter(c for r in rows for c in r["t3_violations"]))},
        "outcomes": {**dict(sorted(totals.items())), "confident_errors": confident, "emitted_herb_facts": emitted_facts,
                     "confident_error_rate_per_emitted_fact": (confident / emitted_facts) if emitted_facts else None},
    }
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"{data['corpus_id']}_cases.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    with open(out_dir / f"{data['corpus_id']}_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, ensure_ascii=False)
    return summary


def main(argv=None) -> int:
    from extractor import evaluate_t2
    tool = _dataset_tool()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=str(tool.DATA_FILE))
    parser.add_argument("--model", default=str(evaluate_t2.DEFAULT_MODEL))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--final", action="store_true", help="required for the official S1-OOD holdout")
    args = parser.parse_args(argv)
    try:
        summary = run(args.dataset, args.model, args.out, final=args.final)
    except OODEvaluationRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: summary[k] for k in ("classification", "dataset", "cases", "t2", "t3", "outcomes")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
