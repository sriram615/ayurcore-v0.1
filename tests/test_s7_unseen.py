"""
S7 unseen synthetic holdout -- end-to-end through the real HTTP API.

Every question in eval/benchmarks/s7/s7_unseen.json is asked through
POST /patients/{patient_pk}/ask, authenticated as a DOCTOR of that patient's
own clinic, and the formulated answer is compared with the gold answer
computed by the deterministic executor.

This is stricter than the executor-level runner (scripts/run_s7_evaluation.py),
which checks the executor against gold it produced itself and therefore only
proves determinism. This test exercises auth, RBAC, clinic scoping, snapshot
build, planner, executor, verifier and answer formulation.

The accepted failures are enumerated in eval/reports/s7/known_gaps.json and
this test asserts the observed failure set matches it *exactly*:

  * a new failure (regression) fails the test;
  * fixing a known gap also fails the test until known_gaps.json is
    regenerated, so the published number cannot silently go stale.

SYNTHETIC; NOT CLINICAL DATA.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

import backend.main as main_module
from db.database import DB_PATH
from scripts.s7_api_eval import load_questions, run_all

GAPS_FILE = ROOT / "eval" / "reports" / "s7" / "known_gaps.json"
MIN_PASS_RATE = 0.85


def test_s7_unseen_holdout_through_api():
    questions = load_questions()
    assert questions, "S7 dataset is empty"

    import backend.rate_limit as rate_limit
    rate_limit.configure("ask", 0)

    with TestClient(main_module.app) as client:
        report = run_all(client, str(DB_PATH), questions)

    print(f"\nS7 API e2e: {report['passed']}/{report['total']} passed ({report['pass_rate']:.1%})")
    for frame, counts in report["by_frame"].items():
        print(f"  {frame:<16} {counts['passed']:>3}/{counts['total']:<3}")
    for failure in report["failures"][:15]:
        print(f"  FAIL {failure['id']} [{failure['frame']}] {failure.get('reason')}")

    assert report["pass_rate"] >= MIN_PASS_RATE, (
        f"S7 API pass rate {report['pass_rate']:.1%} fell below the {MIN_PASS_RATE:.0%} floor"
    )

    expected = json.loads(GAPS_FILE.read_text())
    observed_ids = sorted(f["id"] for f in report["failures"])
    expected_ids = sorted(g["id"] for g in expected["gaps"])

    new = sorted(set(observed_ids) - set(expected_ids))
    fixed = sorted(set(expected_ids) - set(observed_ids))
    assert not new, f"{len(new)} new S7 failures not in known_gaps.json: {new[:10]}"
    assert not fixed, (
        f"{len(fixed)} known gaps no longer fail -- regenerate "
        f"eval/reports/s7/known_gaps.json and the docs: {fixed[:10]}"
    )
