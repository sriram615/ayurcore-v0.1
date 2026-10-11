#!/usr/bin/env python3
"""
Train the AI-2 question planner on S3 train, select epochs on S3 dev, evaluate.

    ./venv/bin/python -m memory.evaluate_planner train            # train + dev selection; writes the model
    ./venv/bin/python -m memory.evaluate_planner eval --split dev # any number of times
    ./venv/bin/python -m memory.evaluate_planner eval --split test --final   # ONCE: one-shot lock

Metrics (planner plans only; answers come from the deterministic executor):
  exact plan       predicted plan == gold plan (same query fields, or abstain with the same reason)
  answer           for gold-answerable questions: executing the predicted plan on the question's
                   S2 reference snapshot gives the gold status and value (end to end through
                   memory.pipeline, so the verifier must also accept the claim)
  abstention       abstain vs answer as a binary decision: precision, recall, unsafe answers
                   (gold abstain, planner produced a query), false abstentions; reason accuracy
  per operation, per frame, per language, per template; every error listed
SYNTHETIC; NOT CLINICAL VALIDATION.
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

from memory.pipeline import answer_question  # noqa: E402
from memory.planner import Planner  # noqa: E402
from memory.s2_reference import reference_snapshots  # noqa: E402

MODEL_DIR = ROOT / "artifacts" / "models" / "planner"
MODEL_FILE = MODEL_DIR / "model.json"
EVAL_DIR = ROOT / "artifacts" / "models" / "planner_eval"
EPOCHS = range(1, 16)
SEED = 13
CLASSIFICATION = "SYNTHETIC TECHNICAL RESULT -- NOT CLINICAL VALIDATION"


class PlannerEvaluationRefused(RuntimeError):
    """S3 test is one-shot: needs final=True and is refused once results exist."""


def _s3():
    spec = importlib.util.spec_from_file_location("s3_generator", ROOT / "scripts" / "s3_generator.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def _snapshots():
    s2 = json.loads((ROOT / "eval" / "benchmarks" / "s2" / "s2_v1.json").read_text(encoding="utf-8"))
    return reference_snapshots(s2)


def exact_accuracy(planner: Planner, questions: list[dict]) -> float:
    return sum(_same_plan(planner.plan(q["question"]), q["gold_plan"]) for q in questions) / len(questions)


def _same_plan(pred: dict, gold: dict) -> bool:
    if pred["kind"] != gold["kind"]:
        return False
    return pred["reason"] == gold["reason"] if gold["kind"] == "abstain" else pred["query"] == gold["query"]


def train(out_dir=MODEL_DIR) -> dict:
    s3 = _s3()
    train_q, dev_q = s3.load("train"), s3.load("dev")
    examples = [(q["question"], q["frame"]) for q in train_q]
    curve = {}
    for epochs in EPOCHS:
        curve[epochs] = exact_accuracy(Planner.train(examples, epochs, SEED), dev_q)
    best = max(EPOCHS, key=lambda e: (curve[e], -e))  # ties -> fewer epochs
    planner = Planner.train(examples, best, SEED)
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    planner.save(out_dir / "model.json")
    report = {"classification": CLASSIFICATION, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "model": "factored multiclass averaged perceptrons (op / target / selector) over delexicalised word 1-2-grams and char 3-4-grams",
              "trained_on": "S3 train", "selected_on": "S3 dev (exact plan accuracy)", "seed": SEED,
              "dev_curve": curve, "selected_epochs": best, "weights_sha256": planner.weights_sha256(),
              "features": {name: len(table) for name, table in planner.weights.items()}, "s3_fingerprint": json.loads(s3.MANIFEST_FILE.read_text())["dataset_fingerprint_sha256"],
              "planner_code_sha256": _sha(ROOT / "memory" / "planner.py")}
    (out_dir / "report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def evaluate(planner: Planner, questions: list[dict], snapshots: dict) -> tuple[dict, list[dict]]:
    rows = []
    for q in questions:
        gold = q["gold_plan"]
        result = answer_question(snapshots[q["patient"]], q["question"], planner)
        pred = result["plan"]
        answer_ok = None
        if gold["kind"] == "query":
            claim = result["claim"]
            answer_ok = result["outcome"] == "answer" and claim is not None and \
                (claim["status"], json.loads(json.dumps(claim["value"]))) == (q["gold_answer"]["status"], q["gold_answer"]["value"])
        rows.append({"id": q["id"], "question": q["question"], "language": q["language"], "frame": q["frame"],
                     "template": q["template"], "gold_plan": gold,
                     "pred_plan": {k: pred.get(k) for k in ("kind", "query", "reason", "frame")},
                     "exact": _same_plan(pred, gold), "answer_correct": answer_ok, "outcome": result["outcome"],
                     "verifier": None if result["verification"] is None else result["verification"]["verdict"]})

    def acc(rs, key="exact"):
        rs = [r for r in rs if r[key] is not None]
        return {"correct": sum(bool(r[key]) for r in rs), "of": len(rs),
                "rate": (sum(bool(r[key]) for r in rs) / len(rs)) if rs else None}

    gold_abs = [r for r in rows if r["gold_plan"]["kind"] == "abstain"]
    gold_ans = [r for r in rows if r["gold_plan"]["kind"] == "query"]
    pred_abs = [r for r in rows if r["pred_plan"]["kind"] == "abstain"]
    tp = sum(r["pred_plan"]["kind"] == "abstain" for r in gold_abs)
    by = lambda key: {k: acc([r for r in rows if key(r) == k]) for k in sorted({key(r) for r in rows if key(r)})}
    templates = collections.defaultdict(list)
    for r in rows:
        templates[r["template"]].append(r["exact"])
    summary = {
        "questions": len(rows),
        "exact_plan": acc(rows),
        "exact_plan_by_template": {"templates": len(templates),
                                   "all_questions_exact": sum(all(v) for v in templates.values())},
        "answer_accuracy_answerable": acc(gold_ans, "answer_correct"),
        "abstention": {"gold_abstain": len(gold_abs), "predicted_abstain": len(pred_abs), "true_abstain": tp,
                       "precision": tp / len(pred_abs) if pred_abs else None,
                       "recall": tp / len(gold_abs) if gold_abs else None,
                       "unsafe_answers": [r["id"] for r in gold_abs if r["pred_plan"]["kind"] != "abstain"],
                       "false_abstentions": len([r for r in gold_ans if r["pred_plan"]["kind"] == "abstain"]),
                       "reason_accuracy_on_true_abstain": acc([r for r in gold_abs if r["pred_plan"]["kind"] == "abstain"])},
        "by_operation": by(lambda r: r["gold_plan"]["query"]["op"] if r["gold_plan"]["kind"] == "query" else "ABSTAIN"),
        "by_frame": by(lambda r: r["frame"]),
        "by_language": by(lambda r: r["language"]),
        "verifier_rejections_of_answers": sum(r["verifier"] == "rejected" for r in rows),
        "errors": [{k: r[k] for k in ("id", "question", "language", "frame", "gold_plan", "pred_plan")}
                   for r in rows if not r["exact"]],
    }
    return summary, rows


def run_eval(split: str, final: bool = False, model_file=MODEL_FILE, out_dir=EVAL_DIR) -> dict:
    out_dir = pathlib.Path(out_dir)
    summary_file = out_dir / f"s3_{split}_summary.json"
    if split == "test":
        if not final:
            raise PlannerEvaluationRefused("S3 test is a one-shot holdout: pass --final")
        if summary_file.exists():
            raise PlannerEvaluationRefused(f"S3 test was already evaluated ({summary_file}); never re-run")
    s3 = _s3()
    if s3.verify():
        raise PlannerEvaluationRefused("S3 does not verify against its manifest")
    planner = Planner.load(model_file)
    summary, rows = evaluate(planner, s3.load(split), _snapshots())
    summary = {"classification": CLASSIFICATION, "split": split, "final": final,
               "generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "s3_fingerprint": json.loads(s3.MANIFEST_FILE.read_text())["dataset_fingerprint_sha256"],
               "model_sha256": _sha(model_file), "weights_sha256": planner.weights_sha256(),
               "planner_code_sha256": _sha(ROOT / "memory" / "planner.py"), **summary}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"s3_{split}_rows.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    summary_file.write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("train", "eval"))
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "train":
        report = train()
        print(json.dumps({k: report[k] for k in ("dev_curve", "selected_epochs", "weights_sha256", "features")}, indent=1))
        return 0
    try:
        s = run_eval(args.split, args.final)
    except PlannerEvaluationRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: s[k] for k in ("split", "questions", "exact_plan", "answer_accuracy_answerable", "abstention",
                                        "by_operation", "by_language")}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
