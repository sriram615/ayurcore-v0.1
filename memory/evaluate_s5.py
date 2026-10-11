#!/usr/bin/env python3
"""
One-shot comparison on the S5 interpretation holdout: single-plan planner vs candidate planning.

    ./venv/bin/python -m memory.evaluate_s5 --final      # ONCE; refused if results exist

Both arms run the unchanged memory.retrieval.answer_with_snapshot (scope screen -> plan -> executor
-> claim -> verifier) on the S2 reference snapshot of each case's patient; they differ only in the
plan step:
    single     the Block 3 planner (memory.planner.Planner.plan, its best frame)
    candidate  memory.interpret.CandidatePlanner over the SAME planner model (top-K + alignment)
Expected outcome per case (as S4): gold abstain -> abstain; gold query with evidence -> answer with
the gold status and value; gold query without evidence -> abstain / NOT_RECORDED.
Reported per arm:
  exact plan        gold query: the planned query equals it; gold abstain: abstained
  wrong-but-valid   an answer (verified, with evidence) to a query other than the gold one, or to a
                    question whose gold is abstain: a CONFIDENT SEMANTIC ERROR
  wrongful refusal  an expected answer withheld
  abstention rate, ambiguity detection (abstain rate on gold-abstain cases), per category / language
Paired: exact two-sided McNemar test on confident semantic errors and on correctness.
SYNTHETIC; NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import math
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.evaluate_s4 import expected_outcome, tampered  # noqa: E402
from memory.interpret import K, CandidatePlanner  # noqa: E402
from memory.planner import Planner  # noqa: E402
from memory.retrieval import answer_with_snapshot  # noqa: E402
from memory.verifier import verify_claim  # noqa: E402

MODEL_FILE = ROOT / "artifacts" / "models" / "planner" / "model.json"
OUT_DIR = ROOT / "artifacts" / "models" / "s5_eval"
CLASSIFICATION = "SYNTHETIC TECHNICAL RESULT -- NOT CLINICAL VALIDATION"
HASHED = ("memory/interpret.py", "memory/scope.py", "memory/retrieval.py", "memory/planner.py", "memory/executor.py",
          "memory/answer.py", "memory/verifier.py", "memory/snapshot.py", "memory/s2_reference.py",
          "memory/evaluate_s5.py", "scripts/s5_dataset.py")
ARMS = ("single", "candidate")


class S5EvaluationRefused(RuntimeError):
    """S5 is a one-shot holdout."""


def _tool():
    spec = importlib.util.spec_from_file_location("s5_dataset", ROOT / "scripts" / "s5_dataset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for b, c discordant pairs."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def _row(case: dict, out: dict, snap) -> dict:
    expected = expected_outcome(case)
    gold = case["gold_plan"]
    planned = out["plan"]["query"] if out["plan"] and out["plan"].get("kind") == "query" else None
    if expected == "answer":
        correct = out["outcome"] == "answer" and \
            (out["claim"]["status"], json.loads(json.dumps(out["claim"]["value"]))) == \
            (case["gold_answer"]["status"], case["gold_answer"]["value"])
    elif expected == "abstain_not_recorded":
        correct = out["outcome"] == "abstain" and out["reason"] == "NOT_RECORDED"
    else:
        correct = out["outcome"] == "abstain"
    exact_plan = (planned == gold["query"]) if gold["kind"] == "query" else out["outcome"] == "abstain"
    answered = out["outcome"] == "answer"
    wrong_valid = answered and (gold["kind"] == "abstain" or planned != gold["query"])
    value_coincides = bool(wrong_valid and gold["kind"] == "query" and
                           json.loads(json.dumps(out["claim"]["value"])) == case["gold_answer"]["value"])
    tamper = collections.Counter()
    if answered:
        for kind, claim in tampered(out["claim"]).items():
            if kind == "dropped_citation" and not out["claim"]["sources"]:
                continue
            tamper[(kind, verify_claim(snap, claim)["verdict"])] += 1
    plan = out["plan"] or {}
    return {"expected": expected, "outcome": out["outcome"], "stage": out["stage"], "reason": out["reason"],
            "frame": plan.get("frame"), "query": planned, "decision": plan.get("decision"),
            "candidates": [{k: c[k] for k in ("frame", "rank", "consistent", "unexplained", "unsupported")}
                           for c in plan.get("candidates", [])],
            "verifier": None if out["verification"] is None else out["verification"]["verdict"],
            "sources": out["claim"]["sources"] if answered else None,
            "correct": correct, "exact_plan": exact_plan, "wrong_but_valid": wrong_valid,
            "value_coincides": value_coincides, "wrongful_refusal": expected == "answer" and not answered,
            "_tamper": tamper}


def _arm_summary(rows: list[dict], arm: str) -> dict:
    rate = lambda k, n: {"count": k, "of": n, "rate": round(k / n, 4) if n else None}
    r = [x[arm] | {"category": x["category"], "language": x["language"], "id": x["id"], "gold_kind": x["gold_plan"]["kind"]}
         for x in rows]
    gold_q = [x for x in r if x["gold_kind"] == "query"]
    gold_a = [x for x in r if x["gold_kind"] == "abstain"]
    should_answer = [x for x in r if x["expected"] == "answer"]
    tamper = collections.Counter()
    for x in r:
        tamper.update(x["_tamper"])
    by = lambda key: {v: {"cases": sum(x[key] == v for x in r), "correct": sum(x["correct"] for x in r if x[key] == v),
                          "exact_plan": sum(x["exact_plan"] for x in r if x[key] == v),
                          "wrong_but_valid": sum(x["wrong_but_valid"] for x in r if x[key] == v),
                          "abstained": sum(x["outcome"] == "abstain" for x in r if x[key] == v),
                          "wrongful_refusals": sum(x["wrongful_refusal"] for x in r if x[key] == v)}
                      for v in sorted({x[key] for x in r})}
    return {
        "overall_correct": rate(sum(x["correct"] for x in r), len(r)),
        "exact_plan": rate(sum(x["exact_plan"] for x in r), len(r)),
        "exact_plan_on_answerable": rate(sum(x["exact_plan"] for x in gold_q), len(gold_q)),
        "wrong_but_valid": rate(sum(x["wrong_but_valid"] for x in r), len(r)),
        "wrong_but_valid_ids": [x["id"] for x in r if x["wrong_but_valid"]],
        "wrong_but_valid_on_answerable": sum(x["wrong_but_valid"] for x in gold_q),
        "wrong_but_valid_on_gold_abstain": sum(x["wrong_but_valid"] for x in gold_a),
        "wrong_but_valid_value_coincides": sum(x["value_coincides"] for x in r),
        "precision_of_answers": rate(sum(x["outcome"] == "answer" and not x["wrong_but_valid"] for x in r),
                                     sum(x["outcome"] == "answer" for x in r)),
        "abstention_rate": rate(sum(x["outcome"] == "abstain" for x in r), len(r)),
        "wrongful_refusals": rate(sum(x["wrongful_refusal"] for x in r), len(should_answer)),
        "ambiguity_detection": rate(sum(x["outcome"] == "abstain" for x in gold_a), len(gold_a)),
        "abstain_stage": dict(collections.Counter(x["stage"] for x in r if x["outcome"] == "abstain")),
        "decisions": dict(collections.Counter(x["decision"] for x in r if x["decision"])),
        "verifier": {"rejections_in_pipeline": sum(x["verifier"] == "rejected" for x in r),
                     "answers_without_evidence": sum(x["outcome"] == "answer" and not x["sources"] for x in r),
                     "tamper_caught": rate(sum(n for (k, v), n in tamper.items() if v == "rejected"), sum(tamper.values()))},
        "by_category": by("category"), "by_language": by("language"),
    }


def evaluate(cases: list[dict], snapshots: dict, planner: Planner, k: int = K) -> tuple[dict, list[dict]]:
    arms = {"single": planner, "candidate": CandidatePlanner(planner, k)}
    rows = []
    for case in cases:
        snap = snapshots[case["patient"]]
        row = {key: case[key] for key in ("id", "category", "language", "question", "patient", "gold_plan")}
        for arm, plan_with in arms.items():
            row[arm] = _row(case, answer_with_snapshot(snap, case["question"], plan_with), snap)
        rows.append(row)
    summary = {"cases": len(rows), "gold_query": sum(r["gold_plan"]["kind"] == "query" for r in rows),
               "gold_abstain": sum(r["gold_plan"]["kind"] == "abstain" for r in rows), "k": k,
               "arms": {arm: _arm_summary(rows, arm) for arm in ARMS}}
    paired = {}
    for metric in ("wrong_but_valid", "correct"):
        b = sum(r["single"][metric] and not r["candidate"][metric] for r in rows)
        c = sum(r["candidate"][metric] and not r["single"][metric] for r in rows)
        paired[metric] = {"single_only": b, "candidate_only": c, "mcnemar_exact_p": round(mcnemar_exact(b, c), 6)}
    summary["paired"] = paired
    summary["transitions"] = dict(collections.Counter(
        f"{_kind(r['single'])}->{_kind(r['candidate'])}" for r in rows))
    summary["candidate_wrong_but_valid"] = [_brief(r, "candidate") for r in rows if r["candidate"]["wrong_but_valid"]]
    summary["candidate_wrongful_refusals"] = [_brief(r, "candidate") for r in rows if r["candidate"]["wrongful_refusal"]]
    summary["single_wrong_but_valid"] = [_brief(r, "single") for r in rows if r["single"]["wrong_but_valid"]]
    for r in rows:
        for arm in ARMS:
            r[arm].pop("_tamper")
    return summary, rows


def _kind(x: dict) -> str:
    if x["wrong_but_valid"]:
        return "wrong_valid"
    if x["correct"]:
        return "correct"
    return "refused" if x["outcome"] == "abstain" else "other"


def _brief(r: dict, arm: str) -> dict:
    x = r[arm]
    return {"id": r["id"], "category": r["category"], "language": r["language"], "question": r["question"],
            "gold": r["gold_plan"], "outcome": x["outcome"], "stage": x["stage"], "reason": x["reason"],
            "frame": x["frame"], "decision": x["decision"]}


def run(final: bool = False, model_file=MODEL_FILE, out_dir=OUT_DIR, dataset_path=None) -> dict:
    tool = _tool()
    out_dir = pathlib.Path(out_dir)
    official = dataset_path is None
    if official:
        if not final:
            raise S5EvaluationRefused("S5 is a one-shot holdout: pass --final")
        if (out_dir / "s5_summary.json").exists():
            raise S5EvaluationRefused(f"S5 was already evaluated ({out_dir / 's5_summary.json'}); never re-run")
        problems = tool.verify()
        if problems:
            raise S5EvaluationRefused("S5 does not verify: " + "; ".join(problems[:3]))
    data = tool.load(dataset_path or tool.DATA_FILE)
    planner = Planner.load(model_file)
    summary, rows = evaluate(data["cases"], tool.snapshots(), planner)
    summary = {"classification": CLASSIFICATION, "official_one_shot": official, "final": final,
               "generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "s5_fingerprint": tool.fingerprint(data), "planner_model_sha256": _sha(model_file),
               "planner_weights_sha256": planner.weights_sha256(),
               "source_sha256": {p: _sha(ROOT / p) for p in HASHED}, **summary}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "s5_rows.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "s5_summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args(argv)
    try:
        s = run(final=args.final)
    except S5EvaluationRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: s[k] for k in ("cases", "k", "paired", "transitions")} |
                     {"arms": {a: {k: v for k, v in s["arms"][a].items() if k not in ("by_category", "by_language")}
                               for a in ARMS}}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
