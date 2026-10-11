#!/usr/bin/env python3
"""
False-rejection measurement for the candidate T3 context checks R1-R5 on S1 train/dev.

    ./venv/bin/python -m extractor.evaluate_t3_context [--model ...] [--out artifacts/models/t3_context_eval]

Runs: oracle_<split> (gold spans from the S1 construction record) and
predicted_<split> (T1 tagger spans), for train and dev. For every case the spans
go through the T2 assembler and the existing T3 verifier exactly as in
extractor.evaluate_t3, are graded by the existing grader, and are then passed to
extractor.verify_context.check_context. Since integration verify() also runs the
context checks, correctness is judged on V1-V9 only: an output that the grader
passes and that has no V1-V9 violation is correct, so any R1-R4 violation on it is a false rejection and any
R5 warning a false warning. s1_test.json is never opened; S1-OOD is not used.

Evidence status: all of it is DEVELOPMENT evidence. S1 dev was inspected (the
pre-fix false rejections were read) before the Manglish clause-boundary fix.
T1's outputs on S1 are all correct, so the predicted runs show integration
behaviour only and cannot measure error detection.
Preliminary integration gate: oracle_dev R1-R4 false-rejection rate <= 1%.
SYNTHETIC TECHNICAL RESULT, NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extractor import evaluate_t2, train as t1  # noqa: E402
from extractor.labels import case_spans  # noqa: E402
from extractor.pipeline import Extractor  # noqa: E402
from extractor.verify import verify  # noqa: E402
from extractor.verify_context import LEXICONS, check_context  # noqa: E402

DEFAULT_OUT = ROOT / "artifacts" / "models" / "t3_context_eval"
RULES = ("R1", "R2", "R3", "R4", "R5")
GATE_RATE = 0.01
EVIDENCE = {
    "oracle_train": "development evidence: lexicons were derived from the S1-train templates",
    "oracle_dev": "development evidence: S1 dev was inspected before the Manglish clause-boundary fix",
    "predicted_train": "integration behaviour only (T1 outputs on S1 are all correct; no error-detection evidence)",
    "predicted_dev": "integration behaviour only (T1 outputs on S1 are all correct; no error-detection evidence)",
}


def _wilson_upper(k: int, n: int, z: float = 1.96) -> float | None:
    if not n:
        return None
    p = k / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre + margin) / (1 + z * z / n)


def run(model_path=evaluate_t2.DEFAULT_MODEL, out_dir=DEFAULT_OUT) -> dict:
    h = evaluate_t2._harness()
    extractor = Extractor(t1.load_model(model_path))
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"classification": t1.CLASSIFICATION, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "t1_weights_sha256": extractor.model.weights_sha256(), "evidence_status": EVIDENCE,
               "integration_gate": f"oracle_dev R1-R4 false-rejection rate <= {GATE_RATE:.0%}, each remaining one explained",
               "checks_sha256": hashlib.sha256((ROOT / "extractor" / "verify_context.py").read_bytes()).hexdigest(),
               "verify_sha256": hashlib.sha256((ROOT / "extractor" / "verify.py").read_bytes()).hexdigest(),
               "t2_code_sha256": evaluate_t2._code_sha256(), "lexicons": LEXICONS,
               "definition": "false rejection = an R1-R4 violation on an output that is graded PASS and "
                             "verified by T3; R5 counts false warnings (never a rejection)",
               "runs": {}}
    for mode, split, path in (("oracle", "train", t1.TRAIN_FILE), ("oracle", "dev", t1.DEV_FILE),
                              ("predicted", "train", t1.TRAIN_FILE), ("predicted", "dev", t1.DEV_FILE)):
        name = f"{mode}_{split}"
        spans_for = (lambda c: case_spans(c)) if mode == "oracle" else (lambda c: extractor.spans(c["notes"]))
        case_data = {**h.load_case_file(path), "cases": t1.load_cases(path)}
        cases = case_data["cases"]
        records = evaluate_t2._records(f"t3_context_{mode}", cases, spans_for)
        graded = {g["case_id"]: g["outcome"] for g in h.build_graded_report(
            {"schema_version": h.RAW_SCHEMA_VERSION, "metadata": {"arm": f"t3_context_{mode}"}, "records": records},
            case_data, None)["results"]}
        rows = []
        for case, rec in zip(cases, records):
            spans = [(s["start"], s["end"], s["type"]) for s in rec["spans"]]
            base = verify(case["notes"], spans, rec["parsed"], rec["assembly"])
            ctx = check_context(case["notes"], spans, rec["parsed"], rec["assembly"])
            rows.append({"case_id": case["case_id"], "category": case["s1_meta"]["category"],
                         "grader_outcome": graded[case["case_id"]], "t3_verdict": base["verdict"],
                         "correct": graded[case["case_id"]] == "PASS"
                                    and not [v for v in base["violations"] if "rule" not in v],  # V1-V9 only
                         "violations": ctx["violations"], "warnings": ctx["warnings"]})
        with open(out_dir / f"rows_{name}.json", "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=1, ensure_ascii=False)
        correct = [r for r in rows if r["correct"]]
        per_rule = {}
        for rule in RULES:
            key = "warnings" if rule == "R5" else "violations"
            hit = [r for r in correct if any(v["rule"] == rule for v in r[key])]
            per_rule[rule] = {"kind": "warning" if rule == "R5" else "rejection", "fired_on_correct": len(hit),
                              "rate": len(hit) / len(correct) if correct else None,
                              "wilson95_upper": _wilson_upper(len(hit), len(correct)),
                              "by_category": dict(collections.Counter(r["category"] for r in hit)),
                              "cases": [r["case_id"] for r in hit]}
        any_reject = [r for r in correct if r["violations"]]
        summary["runs"][name] = {
            "evidence": EVIDENCE[name], "cases": len(rows), "correct_outputs": len(correct),
            "incorrect_outputs": len(rows) - len(correct),
            "fired_on_incorrect": sum(bool(r["violations"]) for r in rows if not r["correct"]),
            "per_rule": per_rule,
            "any_rejection_R1_R4": {"fired_on_correct": len(any_reject),
                                    "rate": len(any_reject) / len(correct) if correct else None,
                                    "wilson95_upper": _wilson_upper(len(any_reject), len(correct))},
            "codes": dict(collections.Counter(v["code"] for r in rows for v in r["violations"] + r["warnings"])),
        }
    dev = summary["runs"]["oracle_dev"]["any_rejection_R1_R4"]
    summary["gate_rate_met"] = dev["rate"] is not None and dev["rate"] <= GATE_RATE
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, ensure_ascii=False)
    summary["_out"] = str(out_dir)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=str(evaluate_t2.DEFAULT_MODEL))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    summary = run(args.model, args.out)
    print(t1.CLASSIFICATION)
    for name, s in summary["runs"].items():
        print(f"{name}: {s['correct_outputs']}/{s['cases']} correct outputs  [{s['evidence']}]")
        for rule, r in s["per_rule"].items():
            print(f"  {rule} {r['kind']:<9} {r['fired_on_correct']:>3}  rate {r['rate']:.4f}  "
                  f"95% upper {r['wilson95_upper']:.4f}  {r['by_category']}")
        a = s["any_rejection_R1_R4"]
        print(f"  any R1-R4 rejection {a['fired_on_correct']}  rate {a['rate']:.4f}  95% upper {a['wilson95_upper']:.4f}")
    print(f"preliminary gate (oracle_dev rate <= {GATE_RATE:.0%}) met: {summary['gate_rate_met']}")
    print(f"artifacts: {summary['_out']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
