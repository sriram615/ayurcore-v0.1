#!/usr/bin/env python3
"""
T3 evaluation: verify every T2 output (positive controls on S1 train/dev; v1 verdicts reported).

    ./venv/bin/python -m extractor.evaluate_t3 [--model artifacts/models/t1_tagger/model.json] [--out artifacts/models/t3_eval]

For each run (oracle/predicted spans on S1 train and dev; predicted spans on
the 12 v1 notes) the T2 outputs are rebuilt exactly as in extractor.evaluate_t2,
graded by the existing harness grader, and passed to extractor.verify. A
rejected output counts as an ABSTENTION, never as a pass; verification results
are reported separately from grading.

v1 is OUT-OF-DISTRIBUTION EVIDENCE ONLY: its verdicts are reported and never
used to change a check. s1_test.json is never opened.
SYNTHETIC TECHNICAL RESULT, NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import argparse
import collections
import json
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

DEFAULT_OUT = ROOT / "artifacts" / "models" / "t3_eval"


def run(model_path=evaluate_t2.DEFAULT_MODEL, out_dir=DEFAULT_OUT) -> dict:
    h = evaluate_t2._harness()
    extractor = Extractor(t1.load_model(model_path))
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"classification": t1.CLASSIFICATION, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "t1_weights_sha256": extractor.model.weights_sha256(), "t2_code_sha256": evaluate_t2._code_sha256(),
               "v1_note": "out-of-distribution evidence only; verdicts reported, never used to change a check",
               "abstention_rule": "a rejected output is an abstention, never a pass", "runs": {}}
    runs = [("oracle", "train", t1.TRAIN_FILE), ("oracle", "dev", t1.DEV_FILE),
            ("predicted", "train", t1.TRAIN_FILE), ("predicted", "dev", t1.DEV_FILE),
            ("predicted", "v1", t1.V1_FILE)]
    for mode, split, path in runs:
        case_data = h.load_case_file(path) if split == "v1" else {**h.load_case_file(path), "cases": t1.load_cases(path)}
        cases = case_data["cases"]
        spans_for = (lambda c: case_spans(c)) if mode == "oracle" else (lambda c: extractor.spans(c["notes"]))
        records = evaluate_t2._records(f"t3_{mode}_spans", cases, spans_for)
        graded = {g["case_id"]: g for g in h.build_graded_report(
            {"schema_version": h.RAW_SCHEMA_VERSION, "metadata": {"arm": f"t3_{mode}"}, "records": records},
            case_data, None)["results"]}
        rows = []
        for case, rec in zip(cases, records):
            spans = [(s["start"], s["end"], s["type"]) for s in rec["spans"]]
            v = verify(case["notes"], spans, rec["parsed"], rec["assembly"])
            outcome = graded[case["case_id"]]["outcome"]
            rows.append({"case_id": case["case_id"], "verdict": v["verdict"], "violations": v["violations"],
                         "grader_outcome": outcome,
                         "outcome_after_verification": outcome if v["verdict"] == "verified" else "ABSTAIN"})
        name = f"{mode}_{split}"
        with open(out_dir / f"verify_{name}.json", "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=1, ensure_ascii=False)
        codes = collections.Counter(x["code"] for r in rows for x in r["violations"])
        summary["runs"][name] = {
            "cases": len(rows),
            "verified": sum(r["verdict"] == "verified" for r in rows),
            "rejected": sum(r["verdict"] == "rejected" for r in rows),
            "violations_by_code": dict(sorted(codes.items())),
            "rejected_cases": [r["case_id"] for r in rows if r["verdict"] == "rejected"],
            "pass_before_verification": sum(r["grader_outcome"] == "PASS" for r in rows),
            "pass_after_verification": sum(r["outcome_after_verification"] == "PASS" for r in rows),
            "abstentions": sum(r["outcome_after_verification"] == "ABSTAIN" for r in rows),
            "verified_but_not_pass": [(r["case_id"], r["grader_outcome"]) for r in rows
                                      if r["verdict"] == "verified" and r["grader_outcome"] != "PASS"],
        }
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
    for name, r in summary["runs"].items():
        print(f"{name:<16} verified {r['verified']}/{r['cases']}  rejected {r['rejected_cases']}  "
              f"violations {r['violations_by_code']}  pass {r['pass_before_verification']} -> {r['pass_after_verification']}  "
              f"verified-but-not-pass {r['verified_but_not_pass']}")
    print(f"artifacts: {summary['_out']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
