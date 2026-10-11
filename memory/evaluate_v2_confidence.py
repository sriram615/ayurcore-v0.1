#!/usr/bin/env python3
"""
AI-2 v2 Block 3 evaluation: calibrated confidence and the selective gate.

    ./venv/bin/python -m memory.evaluate_v2_confidence

Fitting / choosing used DEV data only (devcal dev for the temperature; devcal dev + hardneg dev / train for
the threshold). devcal cal is REPORT-ONLY.
Arms (memory.v2 on S2 reference snapshots; all include the accepted Block 2A reference check):
    H_block2a              current default (no confidence gate)
    I_top1_gate            withhold an answer whose plan is not the planner's top-scored plan
    J_calibrated_0.02      withhold an answer whose calibrated confidence is < 0.02 (the Block 3 proposal)
    K_calibrated_0.10      the same with 0.10 (contrast: a stricter threshold)
A withheld answer becomes a clarification confirming that single reading.
Calibration report: ECE / Brier of the top-plan probability (is the planner's best plan the gold plan?)
and of answer confidence, raw softmax (T=1) vs temperature-scaled; risk-coverage of answers.
Previous-error probes: hand-written variants of earlier failure patterns, interpretation only.
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
from memory.confidence import TEMPERATURE, fit_temperature, nll  # noqa: E402
from memory.planner import Planner  # noqa: E402
from memory.v2 import V2Planner  # noqa: E402

OUT_DIR = ROOT / "artifacts" / "models" / "v2_block3"
BASE = dict(ev.ARMS["E_v2_foundations"], reference_check=True)
ARMS = {"H_block2a": BASE,
        "I_top1_gate": dict(BASE, confidence_gate="top1"),
        "J_calibrated_0.02": dict(BASE, confidence_gate="threshold", confidence_threshold=0.02),
        "K_calibrated_0.10": dict(BASE, confidence_gate="threshold", confidence_threshold=0.10)}
PROBES = [  # (question, expected behaviour) -- hand-written variants of earlier failure patterns
    ("What was the biggest single Neem dose she got?", "not an answer (largest is unsupported)"),
    ("What is the total of Neem she got?", "answer SUM_HERB"),
    ("When was her visit before the last one?", "not an answer (second-to-last is unsupported)"),
    ("Date of her penultimate visit?", "not an answer"),
    ("When did this patient first come in to the clinic?", "FIRST_ENC (not FIRST_RX)"),
    ("When was her most recent visit to the clinic?", "LAST_ENC"),
    ("How has Neem changed since the first prescription?", "CMP_HERB_FIRST"),
    ("How has Neem changed since the previous prescription?", "CMP_HERB_PREV"),
    ("Was there less Neem in the previous prescription than in the latest?", "CMP_HERB_PREV"),
    ("Has Neem fallen since her first prescription?", "CMP_HERB_FIRST or a clarification"),
    ("neem aadyathe kurippadiyekkaal ippol kooduthal aano?", "CMP_HERB_FIRST or a clarification"),
    ("kazhinja thavana enthokke marunnu koduthu?", "LAST_RX or a clarification"),
    ("അവസാന സന്ദർശനം എന്നായിരുന്നു?", "LAST_ENC or a clarification"),
    ("ബ്രഹ്മി ആകെ എത്ര ഗ്രാം നൽകി?", "SUM_HERB"),
]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fit_groups(cases: list[dict], planner) -> list[tuple[list[float], int]]:
    """(scores of every compatible candidate, index of the gold plan) for gold-query cases."""
    v = V2Planner(planner)
    groups = []
    for c in cases:
        if c["gold"]["kind"] != "query" or not isinstance(c["question"], str):
            continue
        interp = v.interpret(c["question"])["interpretation"]
        if not interp:
            continue
        cands = interp["candidates"]
        gold = [i for i, x in enumerate(cands) if x["query"] == c["gold"]["query"]]
        if gold:
            groups.append(([x["score"] for x in cands], gold[0]))
    return groups


def run(out_dir=OUT_DIR) -> dict:
    devcal = _load("v2_devcal_c", ROOT / "scripts" / "v2_devcal.py")
    hardneg = _load("v2_hardneg_c", ROOT / "scripts" / "v2_hardneg.py")
    if devcal.verify() or hardneg.verify():
        raise RuntimeError("devcal / hardneg do not verify")
    planner = Planner.load(ev.MODEL_FILE)
    dev_groups = fit_groups(devcal.load("dev"), planner)
    fitted = fit_temperature(dev_groups)
    cal_groups = fit_groups(devcal.load("cal"), planner)
    result = {"classification": "SYNTHETIC DEVELOPMENT RESULT -- NOT A HOLDOUT, NOT CLINICAL VALIDATION",
              "temperature": {"fitted_on_devcal_dev": round(fitted, 4), "module_constant": TEMPERATURE,
                              "dev_nll_T1": round(nll(dev_groups, 1.0), 4), "dev_nll_T": round(nll(dev_groups, TEMPERATURE), 4),
                              "cal_nll_T1_report_only": round(nll(cal_groups, 1.0), 4),
                              "cal_nll_T_report_only": round(nll(cal_groups, TEMPERATURE), 4),
                              "n_dev": len(dev_groups), "n_cal": len(cal_groups)},
              "datasets": {}}
    datasets = {"devcal_dev": (devcal.load("dev"), devcal.snapshots()),
                "devcal_cal_report_only": (devcal.load("cal"), devcal.snapshots()),
                "hardneg_dev": (hardneg.load("dev"), hardneg.snapshots()),
                "hardneg_train": (hardneg.load("train"), hardneg.snapshots())}
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, (cases, snaps) in datasets.items():
        summaries, rows = ev.evaluate(cases, snaps, planner, ARMS, prob_key="calibrated_probability")
        raw, _ = ev.evaluate(cases, snaps, planner, {"H_block2a": BASE}, prob_key="probability")
        result["datasets"][name] = {
            "summaries": summaries,
            "calibration": {"raw_T1": {"top_plan": raw["H_block2a"]["calibration_top_plan"],
                                       "answers": raw["H_block2a"]["calibration_answers"],
                                       "risk_coverage": raw["H_block2a"]["risk_coverage"]},
                            "temperature_scaled": {"top_plan": summaries["H_block2a"]["calibration_top_plan"],
                                                   "answers": summaries["H_block2a"]["calibration_answers"],
                                                   "risk_coverage": summaries["H_block2a"]["risk_coverage"]}}}
        (out_dir / f"{name}_rows.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    result["probes"] = probes(planner)
    (out_dir / "summary.json").write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def probes(planner) -> list[dict]:
    arms = {name: V2Planner(planner, **{k: v for k, v in cfg.items()}) for name, cfg in ARMS.items()}
    out = []
    for q, expected in PROBES:
        row = {"question": q, "expected": expected}
        for name, v in arms.items():
            o = v.interpret(q)
            row[name] = [o["outcome"], o["reason"], (o["plan"] or {}).get("frame"), [x["frame"] for x in o["options"]],
                         (o["interpretation"] or {}).get("calibrated_confidence")]
        out.append(row)
    return out


def main(argv=None) -> int:
    r = run()
    print("temperature", r["temperature"])
    for name, d in r["datasets"].items():
        for arm, s in d["summaries"].items():
            print(f"{name:24} {arm:18} ans {s['coverage_answer_rate']['count']:3} unsafe {s['unsafe']} "
                  f"wrong_valid {s['wrong_but_valid']['count']} sel_acc {s['selective_accuracy']['rate']} "
                  f"wrongful_ref {s['wrongful_refusals']['count']}/{s['wrongful_refusals']['of']} "
                  f"clar {s['clarification_rate']['count']} clar_rec {s['clarification_recall']['rate']} "
                  f"eff_cov {s['effective_coverage_answerable']['rate']}")
        c = d["calibration"]
        print(f"   calibration top-plan ECE raw {c['raw_T1']['top_plan']['ece']} -> T {c['temperature_scaled']['top_plan']['ece']}; "
              f"Brier {c['raw_T1']['top_plan']['brier']} -> {c['temperature_scaled']['top_plan']['brier']}; "
              f"answers ECE {c['raw_T1']['answers']['ece']} -> {c['temperature_scaled']['answers']['ece']}; "
              f"AURC {c['raw_T1']['risk_coverage']['aurc']} -> {c['temperature_scaled']['risk_coverage']['aurc']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
