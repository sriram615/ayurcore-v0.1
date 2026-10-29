#!/usr/bin/env python3
"""
Run the S7 unseen holdout through the real HTTP API and record the result.

Writes two artifacts:

  eval/reports/s7/s7_api_results.json  full per-question record + summary
  eval/reports/s7/known_gaps.json      the enumerated, accepted failures
                                       (question id -> reason), used as an
                                       exact regression gate by
                                       tests/test_s7_unseen.py

Nothing is "passed" by rounding: a question passes only when the formulated
answer matches the executor-computed gold. Known gaps are listed explicitly
so a new failure shows up as a change, not as noise.

    ./venv/bin/python scripts/run_s7_api_evaluation.py --db db/ayurcore.db

SYNTHETIC; NOT CLINICAL DATA.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import backend.main as main_module
from scripts.s7_api_eval import run_all

REPORT_DIR = ROOT / "eval" / "reports" / "s7"
RESULTS_FILE = REPORT_DIR / "s7_api_results.json"
GAPS_FILE = REPORT_DIR / "known_gaps.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="S7 holdout through the HTTP API")
    parser.add_argument("--db", default=str(ROOT / "db" / "ayurcore.db"))
    args = parser.parse_args()

    import db.database as db_database
    db_database.DB_PATH = args.db

    with TestClient(main_module.app) as client:
        report = run_all(client, args.db)

    # Group the accepted failures so the docs can quote causes, not just counts.
    causes: dict[str, int] = {}
    for failure in report["failures"]:
        key = f"{failure['frame']} / {failure.get('outcome')} / {failure.get('category') or failure.get('stage')}"
        causes[key] = causes.get(key, 0) + 1

    summary = {
        "dataset": "s7_v1",
        "surface": "HTTP API (POST /patients/{pk}/ask)",
        "auth": "clinic doctor per question's clinic",
        "total": report["total"],
        "passed": report["passed"],
        "failed": report["failed"],
        "pass_rate": round(report["pass_rate"], 4),
        "by_frame": report["by_frame"],
        "failure_causes": dict(sorted(causes.items(), key=lambda kv: -kv[1])),
        "known_gaps": len(report["failures"]),
        "records": report["records"],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps(summary, indent=2) + "\n")

    gaps = {
        "dataset": "s7_v1",
        "surface": "HTTP API",
        "generated_by": "scripts/run_s7_api_evaluation.py",
        "note": "Enumerated S7 failures accepted as known AI-2 formulation/coverage gaps. "
                "tests/test_s7_unseen.py asserts the observed failure set equals this set, "
                "so any new failure fails CI and any fix must update this file.",
        "total": report["total"],
        "passed": report["passed"],
        "failed": report["failed"],
        "pass_rate": round(report["pass_rate"], 4),
        "gaps": [
            {
                "id": f["id"],
                "frame": f["frame"],
                "question": f["question"],
                "gold_status": f["gold_status"],
                "outcome": f.get("outcome"),
                "category": f.get("category"),
                "stage": f.get("stage"),
                "reason": f.get("reason"),
            }
            for f in report["failures"]
        ],
    }
    GAPS_FILE.write_text(json.dumps(gaps, indent=2) + "\n")

    print(f"S7 API: {report['passed']}/{report['total']} passed ({report['pass_rate']:.1%})")
    for frame, counts in summary["by_frame"].items():
        print(f"  {frame:<16} {counts['passed']:>3}/{counts['total']:<3}")
    print("failure causes:")
    for cause, count in summary["failure_causes"].items():
        print(f"  {count:>4}  {cause}")
    print(f"wrote {RESULTS_FILE}")
    print(f"wrote {GAPS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
