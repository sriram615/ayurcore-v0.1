#!/usr/bin/env python3
"""
AI-2 v2 Block 5B evaluation on DEVELOPMENT sets: Block 2A vs 4B vs 4B+C10 vs 4B+C10+cue tagger.

    ./venv/bin/python -m memory.evaluate_v2_block5 --select-tiers   # tier selection (PARA v2 dev + hardneg train)
    ./venv/bin/python -m memory.evaluate_v2_block5                  # all arms on all dev sets
    ./venv/bin/python -m memory.evaluate_v2_block5 --probe          # held-out synonym probe: ONCE (opens the label lock)

PARA v2 accept is never opened here.
Tier selection (fixed rule, decided before running): candidate high thresholds GRID, medium = high / 2; a candidate
is feasible only if, on PARA v2 dev AND hardneg train, the tagger arm has 0 unsafe answers, no more wrong-but-valid
answers, no more clarifications of must-refuse questions and no fewer refused must-refuse questions than 4B+C10.
Among feasible candidates the one with the most answerable questions recovered wins (ties: fewer unnecessary
confirmations, then the HIGHER threshold). If none is feasible the tagger is effectively disabled (threshold inf).
Sets: para2_dev (tier set), v2_para_dev and hardneg_train (also tagger TRAINING sources: in-sample),
devcal_dev, devcal_cal_report_only, hardneg_dev.
SYNTHETIC; NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory import evaluate_v2 as ev  # noqa: E402
from memory import evaluate_v2_block4 as b4  # noqa: E402
from memory.cue_tagger import CueTagger  # noqa: E402
from memory.planner import Planner  # noqa: E402

MODEL_DIR = ROOT / "artifacts" / "models" / "cue_tagger"
OUT_DIR = ROOT / "artifacts" / "models" / "v2_block5"
GRID = (0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 15.0, 20.0, 25.0, 30.0)
BASE = dict(ev.ARMS["E_v2_foundations"], reference_check=True)
SETS = {"para2_dev": ("v2_para2", "dev"), "v2_para_dev": ("v2_para", "dev"), "devcal_dev": ("v2_devcal", "dev"),
        "devcal_cal_report_only": ("v2_devcal", "cal"), "hardneg_dev": ("v2_hardneg", "dev"),
        "hardneg_train": ("v2_hardneg", "train")}
IN_SAMPLE = {"v2_para_dev": "tagger training source", "hardneg_train": "tagger training source + tier selection",
             "para2_dev": "tier selection"}


def _tool(name):
    spec = importlib.util.spec_from_file_location(name + "_b5", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arms(tagger, tiers) -> dict:
    return {"block4b": BASE, "block4b_c10": dict(BASE, c10=True),
            "block4b_c10_tagger": dict(BASE, c10=True, cue_tagger=tagger, cue_tiers=tiers)}


def run_arm(tool, split, cfg) -> dict:
    planner = Planner.load(ev.MODEL_FILE)
    summ, rows = ev.evaluate(tool.load(split), tool.snapshots(), planner, {"arm": cfg})
    return {"summary": summ["arm"], "rows": rows["arm"]}


def measures(cases, res) -> dict:
    m = b4.measures(cases, res)
    m["clarify_out_of_scope"] = res["summary"]["clarify_out_of_scope"]
    return m


def feasible(base: dict, cand: dict) -> bool:
    return cand["unsafe"] == 0 and cand["wrong_valid"] <= base["wrong_valid"] and \
        cand["clarify_out_of_scope"] <= base["clarify_out_of_scope"] and \
        cand["refuse_cases_refused"] >= base["refuse_cases_refused"]


def select_tiers(tagger) -> dict:
    trace = []
    sets = {"para2_dev": ("v2_para2", "dev"), "hardneg_train": ("v2_hardneg", "train")}
    tools = {n: _tool(t) for n, (t, _) in sets.items()}
    base = {n: measures(tools[n].load(s), run_arm(tools[n], s, dict(BASE, c10=True))) for n, (_, s) in sets.items()}
    best = None
    for high in GRID:
        tiers = {"high": high, "medium": high / 2}
        cfg = dict(BASE, c10=True, cue_tagger=tagger, cue_tiers=tiers)
        m = {n: measures(tools[n].load(s), run_arm(tools[n], s, cfg)) for n, (_, s) in sets.items()}
        ok = all(feasible(base[n], m[n]) for n in sets)
        score = (sum(x["recovered"] for x in m.values()), -sum(x["unnecessary_confirmations"] for x in m.values()), high)
        trace.append({"high": high, "feasible": ok, "recovered": score[0], "confirmations": -score[1],
                      "unsafe": sum(x["unsafe"] for x in m.values()), "wrong_valid": sum(x["wrong_valid"] for x in m.values())})
        if ok and (best is None or score > best[0]):
            best = (score, tiers)
    tiers = best[1] if best else {"high": math.inf, "medium": math.inf}
    return {"tiers": tiers, "baseline_c10": base, "trace": trace, "disabled": best is None}


def run_dev(tagger, tiers) -> dict:
    result = {"tiers": tiers, "in_sample": IN_SAMPLE, "sets": {}}
    for name, (tool_name, split) in SETS.items():
        tool = _tool(tool_name)
        cases = tool.load(split)
        row = {"block2a": b4.measures(cases, b4.run_block2a(tool_name, split))}
        fills = 0
        for arm, cfg in arms(tagger, tiers).items():
            res = run_arm(tool, split, cfg)
            row[arm] = measures(cases, res)
        if tagger is not None:
            from memory.v2 import V2Planner
            v = V2Planner(Planner.load(ev.MODEL_FILE), **arms(tagger, tiers)["block4b_c10_tagger"])
            fills = sum(bool(v.interpret(c["question"]).get("cue_tagger")) for c in cases if isinstance(c["question"], str))
        row["questions_with_tagger_fills"] = fills
        result["sets"][name] = row
    result["reference_false_fires"] = len(b4.legit_false_fires())
    return result


def probe(tagger, note: str) -> dict:
    """ONE-SHOT: opens the held-out label lock; context-free prediction for each held-out word."""
    p2 = _tool("v2_para2")
    words = p2.training_exclusion_words()
    preds = {w: tagger.predict([w], 0, [None]) for w in words}  # predictions fixed BEFORE labels are opened
    labels = p2.open_probe_labels_for_final_evaluation(note)
    rows = [{"word": s["word"], "language": s["language"], "intended": s["category"], "predicted": preds[s["word"]][0],
             "margin": preds[s["word"]][1]} for s in labels]
    by_lang = {}
    for r in rows:
        by_lang.setdefault(r["language"], [0, 0])
        by_lang[r["language"]][0] += r["predicted"] == r["intended"]
        by_lang[r["language"]][1] += 1
    return {"context_free_accuracy": sum(r["predicted"] == r["intended"] for r in rows) / len(rows),
            "by_language": by_lang, "rows": rows}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--select-tiers", action="store_true")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(argv)
    tagger = CueTagger.load(MODEL_DIR / "model.json")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if args.select_tiers:
        sel = select_tiers(tagger)
        (MODEL_DIR / "tiers.json").write_text(json.dumps({"tiers": sel["tiers"], "disabled": sel["disabled"],
                                                          "model_weights_sha256": tagger.weights_sha256()}, indent=1) + "\n")
        (OUT_DIR / "tier_selection.json").write_text(json.dumps(sel, indent=1, default=str) + "\n")
        print(json.dumps({k: sel[k] for k in ("tiers", "disabled", "trace")}, indent=1, default=str))
        return 0
    tiers = json.loads((MODEL_DIR / "tiers.json").read_text())["tiers"]
    if args.probe:
        if (OUT_DIR / "probe.json").exists():
            print("Refused: the held-out probe was already run", file=sys.stderr)
            return 2
        res = probe(tagger, f"Block 5B probe, tagger {tagger.weights_sha256()[:12]}")
        (OUT_DIR / "probe.json").write_text(json.dumps(res, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({k: res[k] for k in ("context_free_accuracy", "by_language")}, indent=1))
        return 0
    res = run_dev(tagger, tiers)
    (OUT_DIR / "dev_summary.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    for name, row in res["sets"].items():
        for arm in ("block2a", "block4b", "block4b_c10", "block4b_c10_tagger"):
            x = row[arm]
            print(f"{name:24} {arm:20} correct {x['correct']}/{x['cases']} unsafe {x['unsafe']} wrong_valid {x['wrong_valid']} "
                  f"wrongful_ref {x['wrongful_refusals']}/{x['expect_answer']} confirmations {x['unnecessary_confirmations']} "
                  f"recovered {x['recovered']} refused {x['refuse_cases_refused']}/{x['refuse_cases']} "
                  f"clar_oos {x.get('clarify_out_of_scope', '-')} by_lang {x['recovered_by_language']}")
        print(f"{'':24} questions with tagger fills: {row['questions_with_tagger_fills']}")
    print("reference false fires:", res["reference_false_fires"], "tiers:", tiers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
