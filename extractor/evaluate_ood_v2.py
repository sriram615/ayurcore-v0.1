#!/usr/bin/env python3
"""
S1-OOD v2 one-shot safety evaluation of the T1 -> T2 -> T3 extractor (T3 with R1-R5).

    ./venv/bin/python -m extractor.evaluate_ood_v2 --final

The official v2 file (eval/benchmarks/s1_ood_v2) is a ONE-SHOT holdout: it is
evaluated only with --final, only if it still verifies against its frozen
manifest, and only once -- if official results already exist in the output
directory the run is refused. Any other file must be a valid S1-OOD v2-format
dataset (used for fixtures in tests). Refusals happen before the model is
loaded. extractor/evaluate_ood.py (v1) is not modified; its _prf / _outcomes
helpers are reused.

Reported separately:
  T1      span P/R/F1 per type and micro; exact cases per category
  T2      the assembled parse BEFORE verification, graded by the existing grader,
          plus the outcome analysis as if it were emitted
  T3      verdicts; rejections split into V1-V9 and context R1-R4 (by rule);
          each rejection classified as a CATCH (the parse held a confident error),
          a FALSE REJECTION (the parse was exactly correct) or an INCOMPLETE
          rejection (no confident error, but the parse was not exactly correct)
  R5      warnings vs the gold unaccounted doses
  final   what the system emits (nothing if T3 rejected): exact accuracy,
          confident errors, correct abstentions, omissions; per category
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

from extractor.evaluate_ood import _outcomes, _prf, _sha256  # noqa: E402

DATASET_TOOL_PATH = ROOT / "scripts" / "s1_ood_v2_dataset.py"
DEFAULT_OUT = ROOT / "artifacts" / "models" / "ood_v2_eval"
CLASSIFICATION = "SYNTHETIC TECHNICAL RESULT -- NOT CLINICAL VALIDATION"
HASHED_SOURCES = ("extractor/labels.py", "extractor/features.py", "extractor/tagger.py", "extractor/train.py",
                  "extractor/pipeline.py", "extractor/units.py", "extractor/normalize.py", "extractor/assemble.py",
                  "extractor/verify.py", "extractor/verify_context.py", "extractor/evaluate_ood.py",
                  "extractor/evaluate_ood_v2.py", "scripts/clinical_eval_harness.py", "scripts/s1_ood_v2_dataset.py",
                  "backend/herb_resolver.py", "backend/schemas.py")


class OODv2EvaluationRefused(RuntimeError):
    """The requested dataset may not be evaluated (invalid, not --final, or already evaluated)."""


def _dataset_tool():
    spec = importlib.util.spec_from_file_location("s1_ood_v2_dataset", DATASET_TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _summary_path(out_dir, corpus_id: str) -> pathlib.Path:
    return pathlib.Path(out_dir) / f"{corpus_id}_summary.json"


def check_dataset(path, final: bool, out_dir) -> tuple[dict, bool]:
    """(data, is_official). Raises OODv2EvaluationRefused; runs before any model is loaded."""
    tool = _dataset_tool()
    path = pathlib.Path(path).resolve()
    try:
        data = tool.load(path)
    except (OSError, ValueError) as exc:
        raise OODv2EvaluationRefused(f"cannot read {path}: {exc}") from exc
    frozen = json.loads(tool.MANIFEST_FILE.read_text(encoding="utf-8"))["dataset_fingerprint_sha256"]
    official = path == tool.DATA_FILE.resolve() or (isinstance(data, dict) and isinstance(data.get("cases"), list)
                                                    and _safe_fingerprint(tool, data) == frozen)
    if official:
        if not final:
            raise OODv2EvaluationRefused("S1-OOD v2 is a one-shot holdout: pass final=True / --final")
        if _summary_path(out_dir, tool.DATASET_ID).exists():
            raise OODv2EvaluationRefused(f"S1-OOD v2 was already evaluated ({_summary_path(out_dir, tool.DATASET_ID)}); "
                                         "a one-shot holdout is never re-run")
        problems = tool.verify(path)
    else:
        problems = tool.validate_cases(data)
    if problems:
        raise OODv2EvaluationRefused("dataset invalid: " + "; ".join(problems[:5]))
    return data, official


def _safe_fingerprint(tool, data) -> str | None:
    try:
        return tool.fingerprint(data["cases"])
    except (KeyError, TypeError):
        return None


def classify_rejection(case: dict, parse_herbs: dict, parse_dosha: list) -> str:
    """What a T3 rejection did to this parse: catch | false_rejection | incomplete."""
    pre = _outcomes(case, parse_herbs, parse_dosha)
    if any(k.startswith("confident_error") for k in pre):
        return "catch"
    gold = {h: s["grams"] for h, s in (case["expected"]["herbs"] or {}).items()}
    if parse_herbs == gold and set(parse_dosha) == set(case["expected"]["dosha"] or []):
        return "false_rejection"
    return "incomplete"


def run(dataset_path=None, model_path=None, out_dir=DEFAULT_OUT, final: bool = False) -> dict:
    tool = _dataset_tool()
    dataset_path = dataset_path or tool.DATA_FILE
    data, official = check_dataset(dataset_path, final, out_dir)  # refusals happen here
    from extractor import evaluate_t2, train as t1
    from extractor.assemble import assemble
    from extractor.labels import SPAN_TYPES
    from extractor.pipeline import Extractor
    from extractor.verify import verify

    extractor = Extractor(t1.load_model(model_path or evaluate_t2.DEFAULT_MODEL))
    harness = evaluate_t2._harness()
    per_type = {t: [0, 0, 0] for t in SPAN_TYPES}
    rows, records = [], []
    for case in data["cases"]:
        note = case["notes"]
        gold_spans = {(s, e, t) for s, e, t, _ in tool.resolve_spans(case)}
        pred_spans = extractor.spans(note)
        pred = set(pred_spans)
        for t in SPAN_TYPES:
            g, p = {x for x in gold_spans if x[2] == t}, {x for x in pred if x[2] == t}
            per_type[t][0] += len(g & p)
            per_type[t][1] += len(p - g)
            per_type[t][2] += len(g - p)
        parsed, report = assemble(note, pred_spans)
        result = verify(note, pred_spans, parsed, report)
        parse_herbs = {h.name: h.grams for h in parsed.prescribed_herbs}
        parse_dosha = parsed.dosha_imbalance.split("-") if parsed.dosha_imbalance else []
        verified = result["verdict"] == "verified"
        emitted, emitted_dosha = (parse_herbs, parse_dosha) if verified else ({}, [])
        gold = {h: s["grams"] for h, s in (case["expected"]["herbs"] or {}).items()}
        gold_dosha = set(case["expected"]["dosha"] or [])
        rules = sorted({v.get("rule", "V1-V9") for v in result["violations"]})
        records.append({"case_id": case["case_id"], "repeat": 0, "arm": "extractor_t1_t2_t3_r1_r5", "status": "ok",
                        "parsed": parsed.model_dump(), "exception": None, "latency_ms": 0, "tool_calls": [],
                        "log_records": []})
        rows.append({
            "case_id": case["case_id"], "category": case["ood"]["category"],
            "target_rules": case["safety"]["target_rules"], "control": "control" in case["safety"]["target_rules"],
            "t1_spans": [list(x) for x in sorted(pred)], "t1_exact": pred == gold_spans,
            "t1_missed": sorted(gold_spans - pred), "t1_spurious": sorted(pred - gold_spans),
            "t2_parse": parsed.model_dump(), "t2_exact_herbs": parse_herbs == gold,
            "t2_outcomes": dict(_outcomes(case, parse_herbs, parse_dosha)),
            "t3_verdict": result["verdict"], "t3_violations": result["violations"], "t3_rules": rules,
            "rejection_effect": None if verified else classify_rejection(case, parse_herbs, parse_dosha),
            "r5_warnings": result["warnings"], "gold_unaccounted": case["safety"]["unaccounted"],
            "emitted": emitted, "emitted_dosha": emitted_dosha,
            "final_exact_herbs": emitted == gold,
            "final_exact": emitted == gold and set(emitted_dosha) == gold_dosha,
            "final_outcomes": dict(_outcomes(case, emitted, emitted_dosha)),
        })
    graded = harness.build_graded_report(
        {"schema_version": harness.RAW_SCHEMA_VERSION, "metadata": {"arm": "extractor_t1_t2_t3_r1_r5"}, "records": records},
        data, None)
    grade = {g["case_id"]: g["outcome"] for g in graded["results"]}
    for row in rows:
        row["t2_grader_outcome"] = grade[row["case_id"]]
    summary = _summarise(data, rows, graded, per_type, extractor, official, final, tool)
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"{data['corpus_id']}_cases.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    with open(_summary_path(out_dir, data["corpus_id"]), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, ensure_ascii=False)
    return summary


def _rate(k: int, n: int):
    return {"count": k, "of": n, "rate": (k / n) if n else None}


def _totals(rows, key: str) -> dict:
    total = collections.Counter()
    for r in rows:
        total.update(r[key])
    confident = sum(v for k, v in total.items() if k.startswith("confident_error"))
    emitted = total["correct_fact"] + sum(v for k, v in total.items()
                                          if k.startswith("confident_error:") and k != "confident_error:wrong_dosha")
    return {**dict(sorted(total.items())), "confident_errors": confident, "emitted_herb_facts": emitted,
            "confident_error_rate_per_emitted_fact": (confident / emitted) if emitted else None}


def _summarise(data, rows, graded, per_type, extractor, official, final, tool) -> dict:
    n = len(rows)
    categories = list(tool.CATEGORIES)
    rejected = [r for r in rows if r["t3_verdict"] == "rejected"]
    controls = [r for r in rows if r["control"]]
    by_rule = {}
    for rule in ("R1", "R2", "R3", "R4", "V1-V9"):
        hit = [r for r in rejected if rule in r["t3_rules"]]
        by_rule[rule] = {"rejections": len(hit), "effect": dict(collections.Counter(r["rejection_effect"] for r in hit)),
                         "targeted_cases_rejected": sum(rule in r["target_rules"] for r in hit),
                         "cases": [r["case_id"] for r in hit]}
    warned = [r for r in rows if r["r5_warnings"]]
    gold_unacc = [r for r in rows if r["gold_unaccounted"]]
    clinical = graded["summary"]["clinical"]
    t2_conf = _totals(rows, "t2_outcomes")
    final_conf = _totals(rows, "final_outcomes")
    return {
        "classification": CLASSIFICATION, "dataset": data["corpus_id"], "official_one_shot": official,
        "final_run": final, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_fingerprint_sha256": tool.fingerprint(data["cases"]), "cases": n,
        "t1_weights_sha256": extractor.model.weights_sha256(),
        "source_sha256": {p: _sha256(ROOT / p) for p in HASHED_SOURCES},
        "definitions": {
            "final_exact": "emitted herbs == expected herbs (name and grams) and emitted dosha set == gold dosha set",
            "final_exact_herbs": "emitted herbs == expected herbs; a T3 rejection emits nothing",
            "catch": "T3 rejected a parse holding a confident error (wrong quantity, spurious herb, missed abstention, wrong dosha)",
            "false_rejection": "T3 rejected a parse whose herbs and dosha were exactly correct",
            "incomplete": "T3 rejected a parse with no confident error that was not exactly correct",
        },
        "final": {"exact": _rate(sum(r["final_exact"] for r in rows), n),
                  "exact_herbs": _rate(sum(r["final_exact_herbs"] for r in rows), n),
                  "outcomes": final_conf},
        "t1": {"spans_micro": _prf(*[sum(v[i] for v in per_type.values()) for i in range(3)]),
               "spans_by_type": {t: _prf(*v) for t, v in per_type.items()},
               "cases_exact": _rate(sum(r["t1_exact"] for r in rows), n)},
        "t2": {"exact_herbs": _rate(sum(r["t2_exact_herbs"] for r in rows), n),
               "grader_outcomes": graded["summary"]["outcomes"], "grader_pass": clinical["passed"],
               "herb_precision": clinical["herb_micro_precision"], "herb_recall": clinical["herb_micro_recall"],
               "dosha_exact": clinical["dosha_exact"], "outcomes_if_emitted": t2_conf},
        "t3": {"verified": n - len(rejected), "rejected": len(rejected),
               "rejection_effect": dict(collections.Counter(r["rejection_effect"] for r in rejected)),
               "by_rule": by_rule,
               "confident_errors_prevented": t2_conf["confident_errors"] - final_conf["confident_errors"],
               "violation_codes": dict(collections.Counter(v["code"] for r in rows for v in r["t3_violations"]))},
        "r5": {"cases_warned": len(warned), "warnings": sum(len(r["r5_warnings"]) for r in rows),
               "gold_unaccounted_cases": len(gold_unacc),
               "warned_on_gold_unaccounted": sum(bool(r["r5_warnings"]) for r in gold_unacc),
               "warned_without_gold_unaccounted": [r["case_id"] for r in warned if not r["gold_unaccounted"]]},
        "controls": {"cases": len(controls), "rejected": [r["case_id"] for r in controls if r["t3_verdict"] == "rejected"],
                     "false_rejections": [r["case_id"] for r in controls if r["rejection_effect"] == "false_rejection"],
                     "false_rejection_rate": _rate(sum(r["rejection_effect"] == "false_rejection" for r in controls),
                                                   sum(r["t2_exact_herbs"] for r in controls)),
                     "exact": _rate(sum(r["final_exact"] for r in controls), len(controls))},
        "by_category": {c: {"cases": sum(r["category"] == c for r in rows),
                            "final_exact": sum(r["final_exact"] for r in rows if r["category"] == c),
                            "t2_exact_herbs": sum(r["t2_exact_herbs"] for r in rows if r["category"] == c),
                            "t1_exact": sum(r["t1_exact"] for r in rows if r["category"] == c),
                            "rejected": sum(r["t3_verdict"] == "rejected" for r in rows if r["category"] == c),
                            "final_confident_errors": sum(v for r in rows if r["category"] == c
                                                          for k, v in r["final_outcomes"].items()
                                                          if k.startswith("confident_error"))}
                        for c in categories},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--final", action="store_true", help="required for the official one-shot S1-OOD v2 holdout")
    args = parser.parse_args(argv)
    try:
        summary = run(args.dataset, args.model, args.out, final=args.final)
    except OODv2EvaluationRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: summary[k] for k in ("classification", "dataset", "cases", "final", "t3", "r5", "controls")},
                     indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
