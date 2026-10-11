#!/usr/bin/env python3
"""
AI-2 v2 Block 2A evaluation: structural reference safety (memory/reference.py).

    ./venv/bin/python -m memory.evaluate_v2_reference

Arms (memory.v2.answer_with_snapshot_v2 on S2 reference snapshots):
    A0_block6_frozen   merged from the hash-verified frozen Block 6 baseline files when present
    E_block1           Block 1 foundations
    H_block2a          Block 1 + reference_check
Datasets: devcal dev, devcal cal (REPORT-ONLY: never used to choose a rule), hardneg dev, hardneg train
(the rule was not fitted to hardneg; train is reported as further development evidence).
Extra measures:
    structural_unsafe        answers to gold other-patient cases (devcal other_patient, hardneg OTHER_PATIENT)
    reference_false_refusals gold answerable / ambiguous cases refused by the reference check itself
S6 structural re-test: diagnostic only (S6 is spent) -- interpretation of the S6 other-patient /
injection questions, no patient data, no aggregate claim.
SYNTHETIC; NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory import evaluate_v2 as ev  # noqa: E402
from memory.planner import Planner  # noqa: E402
from memory.v2 import V2Planner  # noqa: E402

OUT_DIR = ROOT / "artifacts" / "models" / "v2_block2a"
ARMS = {"E_block1": ev.ARMS["E_v2_foundations"],
        "H_block2a": dict(ev.ARMS["E_v2_foundations"], reference_check=True)}
BASELINES = {"devcal_dev": "artifacts/models/v2_block1/dev_baseline_block6.json",
             "devcal_cal_report_only": "artifacts/models/v2_block1/cal_baseline_block6.json",
             "hardneg_dev": "artifacts/models/v2_block2/hardneg_dev_baseline_block6.json",
             "hardneg_train": "artifacts/models/v2_block2a/hardneg_train_baseline_block6.json"}
S6_STRUCTURAL = ("s6-xpt-01", "s6-xpt-02", "s6-xpt-03", "s6-xpt-10", "s6-xpt-11", "s6-inj-01", "s6-inj-03",
                 "s6-inj-04", "s6-inj-07", "s6-inj-09", "s6-inj-10")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def is_other_patient(case: dict) -> bool:
    return case.get("label") == "OTHER_PATIENT" or case.get("category") == "other_patient"


def extra(cases: list[dict], rows: list[dict]) -> dict:
    structural = [(c, r) for c, r in zip(cases, rows) if is_other_patient(c)]
    valid = [(c, r) for c, r in zip(cases, rows) if c["gold"]["kind"] in ("query", "ambiguous")]
    return {"structural_cases": len(structural),
            "structural_unsafe": sum(r["outcome"] == "answer" for _, r in structural),
            "structural_clarified": sum(r["outcome"] == "clarify" for _, r in structural),
            "structural_refused": sum(r["outcome"] == "abstain" for _, r in structural),
            "reference_false_refusals": [c["question"] for c, r in valid if r["stage"] == "reference"],
            "valid_cases": len(valid)}


def s6_retest(planner) -> list[dict]:
    s6 = {c["id"]: c for c in json.loads((ROOT / "eval/benchmarks/s6/s6_v1.json").read_text())["cases"]}
    b1, b2a = V2Planner(planner, reference_check=False), V2Planner(planner, reference_check=True)
    out = []
    for cid in S6_STRUCTURAL:
        q = s6[cid]["question"]
        a, b = b1.interpret(q), b2a.interpret(q)
        out.append({"id": cid, "question": q, "gold": s6[cid]["gold"],
                    "block1": [a["outcome"], a["stage"], a["reason"]],
                    "block2a": [b["outcome"], b["stage"], b["reason"], (b.get("reference") or {}).get("rules")]})
    return out


def run(out_dir=OUT_DIR) -> dict:
    hardneg = _load("v2_hardneg_r", ROOT / "scripts" / "v2_hardneg.py")
    devcal = _load("v2_devcal_r", ROOT / "scripts" / "v2_devcal.py")
    for name, tool in (("hardneg", hardneg), ("devcal", devcal)):
        if tool.verify():
            raise RuntimeError(f"{name} does not verify")
    planner = Planner.load(ev.MODEL_FILE)
    datasets = {"devcal_dev": (devcal.load("dev"), devcal.snapshots()),
                "devcal_cal_report_only": (devcal.load("cal"), devcal.snapshots()),
                "hardneg_dev": (hardneg.load("dev"), hardneg.snapshots()),
                "hardneg_train": (hardneg.load("train"), hardneg.snapshots())}
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {"classification": "SYNTHETIC DEVELOPMENT RESULT -- NOT A HOLDOUT, NOT CLINICAL VALIDATION",
              "datasets": {}}
    for name, (cases, snaps) in datasets.items():
        summaries, rows = ev.evaluate(cases, snaps, planner, ARMS)
        extras = {arm: extra(cases, rows[arm]) for arm in ARMS}
        base = ROOT / BASELINES[name]
        if base.exists():
            summaries = {"A0_block6_frozen": json.loads(base.read_text())["summaries"]["A0_block6_frozen"], **summaries}
        result["datasets"][name] = {"summaries": summaries, "extra": extras}
        (out_dir / f"{name}_rows.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    result["s6_structural_retest"] = s6_retest(planner)
    (out_dir / "summary.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def main(argv=None) -> int:
    r = run()
    for name, d in r["datasets"].items():
        for arm, s in d["summaries"].items():
            x = d["extra"].get(arm, {})
            print(f"{name:24} {arm:18} unsafe {s['unsafe']} wrong_valid {s['wrong_but_valid']['count']} "
                  f"wrongful_refusals {s['wrongful_refusals']['count']}/{s['wrongful_refusals']['of']} "
                  f"clar_oos {s['clarify_out_of_scope']} eff_cov {s['effective_coverage_answerable']['rate']} "
                  f"structural(answer/clarify/refuse of n) {x.get('structural_unsafe')}/{x.get('structural_clarified')}/"
                  f"{x.get('structural_refused')} of {x.get('structural_cases')} ref_false_refusals "
                  f"{len(x.get('reference_false_refusals', []))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
