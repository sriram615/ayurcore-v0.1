#!/usr/bin/env python3
"""
One-shot evaluation of the AI-2 retrieval pipeline on the S4 adversarial holdout.

    ./venv/bin/python -m memory.evaluate_s4 --final      # ONCE; refused if results exist

Pipeline under test (memory.retrieval.answer_with_snapshot, on the S2 reference snapshot
of each case's patient): scope screen -> local planner -> executor -> claim -> verifier.

Expected outcome per case:
    gold abstain                                   -> abstain
    gold query whose answer has source evidence    -> answer, with the gold status and value
    gold query whose answer has no evidence        -> abstain / NOT_RECORDED ("no evidence means
                                                      abstention")
Reported:
  screen        the screen alone: precision / recall of ABSTAIN against gold abstain, legit
                questions it rejects
  end to end    false accepts (gold abstain answered = unsafe), false rejects (an expected answer
                withheld), answer accuracy on legit questions, reason accuracy on correct abstentions
  verifier      rejections inside the pipeline, and a tampering check: four corrupted copies of
                every produced answer (changed value, foreign source, dropped citation,
                unsupported number in the text) must all be rejected
SYNTHETIC; NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import argparse
import collections
import copy
import hashlib
import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.planner import Planner  # noqa: E402
from memory.retrieval import answer_with_snapshot  # noqa: E402
from memory.scope import screen  # noqa: E402
from memory.verifier import verify_claim  # noqa: E402

MODEL_FILE = ROOT / "artifacts" / "models" / "planner" / "model.json"
OUT_DIR = ROOT / "artifacts" / "models" / "s4_eval"
CLASSIFICATION = "SYNTHETIC TECHNICAL RESULT -- NOT CLINICAL VALIDATION"
HASHED = ("memory/scope.py", "memory/retrieval.py", "memory/planner.py", "memory/executor.py", "memory/answer.py",
          "memory/verifier.py", "memory/snapshot.py", "memory/s2_reference.py", "memory/evaluate_s4.py",
          "scripts/s4_dataset.py")


class S4EvaluationRefused(RuntimeError):
    """S4 is a one-shot holdout."""


def _tool():
    spec = importlib.util.spec_from_file_location("s4_dataset", ROOT / "scripts" / "s4_dataset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def expected_outcome(case: dict) -> str:
    if case["gold_plan"]["kind"] == "abstain":
        return "abstain"
    answer = case["gold_answer"]
    return "abstain_not_recorded" if answer["status"] == "not_recorded" or not answer["sources"] else "answer"


def tampered(claim: dict) -> dict:
    """Four corrupted copies of a verified claim; every one must be rejected."""
    out = {}
    bad = copy.deepcopy(claim)
    bad["value"] = {"tampered": True}
    out["changed_value"] = bad
    bad = copy.deepcopy(claim)
    bad["sources"] = bad["sources"] + ["rx:999999"]
    out["foreign_source"] = bad
    bad = copy.deepcopy(claim)
    bad["sources"] = bad["sources"][1:]
    out["dropped_citation"] = bad
    bad = copy.deepcopy(claim)
    bad["text"] = bad["text"] + " Usually 777 g."
    out["unsupported_number"] = bad
    return out


def evaluate(cases: list[dict], snapshots: dict, planner: Planner) -> tuple[dict, list[dict]]:
    rows = []
    tamper = collections.Counter()
    for case in cases:
        snap = snapshots[case["patient"]]
        verdict = screen(case["question"])
        out = answer_with_snapshot(snap, case["question"], planner)
        expected = expected_outcome(case)
        if expected == "answer":
            correct = out["outcome"] == "answer" and \
                (out["claim"]["status"], json.loads(json.dumps(out["claim"]["value"]))) == \
                (case["gold_answer"]["status"], case["gold_answer"]["value"])
        elif expected == "abstain_not_recorded":
            correct = out["outcome"] == "abstain" and out["reason"] == "NOT_RECORDED"
        else:
            correct = out["outcome"] == "abstain"
        if out["outcome"] == "answer":
            for kind, claim in tampered(out["claim"]).items():
                if kind == "dropped_citation" and not out["claim"]["sources"]:
                    continue
                tamper[(kind, verify_claim(snap, claim)["verdict"])] += 1
        rows.append({"id": case["id"], "category": case["category"], "language": case["language"],
                     "question": case["question"], "gold_plan": case["gold_plan"], "expected": expected,
                     "screen": verdict, "outcome": out["outcome"], "stage": out["stage"], "reason": out["reason"],
                     "plan": None if out["plan"] is None else {k: out["plan"].get(k) for k in ("kind", "query", "reason", "frame")},
                     "claim": out["claim"], "verifier": None if out["verification"] is None else out["verification"]["verdict"],
                     "correct": correct})

    gold_abs = [r for r in rows if r["expected"] == "abstain"]
    legit = [r for r in rows if r["expected"] != "abstain"]
    screen_abs = [r for r in rows if r["screen"]["decision"] == "ABSTAIN"]
    tp = sum(r["screen"]["decision"] == "ABSTAIN" for r in gold_abs)
    rate = lambda k, n: {"count": k, "of": n, "rate": (k / n) if n else None}
    by = lambda key: {v: rate(sum(r["correct"] for r in rows if r[key] == v), sum(r[key] == v for r in rows))
                      for v in sorted({r[key] for r in rows})}
    correct_abstain = [r for r in gold_abs if r["outcome"] == "abstain"]
    summary = {
        "cases": len(rows), "gold_abstain": len(gold_abs), "legit": len(legit),
        "screen": {"precision": rate(tp, len(screen_abs)), "recall": rate(tp, len(gold_abs)),
                   "legit_rejected_by_screen": [r["id"] for r in legit if r["screen"]["decision"] == "ABSTAIN"]},
        "end_to_end": {
            "overall_correct": rate(sum(r["correct"] for r in rows), len(rows)),
            "false_accepts": [r["id"] for r in gold_abs if r["outcome"] == "answer"],
            "false_accept_rate": rate(sum(r["outcome"] == "answer" for r in gold_abs), len(gold_abs)),
            "false_rejects": [r["id"] for r in legit if r["expected"] == "answer" and r["outcome"] != "answer"],
            "false_reject_rate": rate(sum(r["expected"] == "answer" and r["outcome"] != "answer" for r in legit),
                                      sum(r["expected"] == "answer" for r in legit)),
            "legit_answer_accuracy": rate(sum(r["correct"] for r in legit), len(legit)),
            "abstain_stage": dict(collections.Counter(r["stage"] for r in correct_abstain)),
            "reason_accuracy_on_correct_abstentions": rate(sum(r["reason"] == r["gold_plan"]["reason"] for r in correct_abstain),
                                                           len(correct_abstain)),
        },
        "verifier": {"rejections_in_pipeline": sum(r["verifier"] == "rejected" for r in rows),
                     "answers_without_evidence": sum(r["outcome"] == "answer" and not r["claim"]["sources"] for r in rows),
                     "tamper_checks": {f"{k}:{v}": n for (k, v), n in sorted(tamper.items())},
                     "tamper_caught": rate(sum(n for (k, v), n in tamper.items() if v == "rejected"), sum(tamper.values()))},
        "by_category": by("category"), "by_language": by("language"),
        "failures": [{k: r[k] for k in ("id", "category", "language", "question", "expected", "outcome", "stage", "reason",
                                        "plan", "screen")} for r in rows if not r["correct"]],
    }
    return summary, rows


def run(final: bool = False, model_file=MODEL_FILE, out_dir=OUT_DIR, dataset_path=None) -> dict:
    tool = _tool()
    out_dir = pathlib.Path(out_dir)
    official = dataset_path is None
    if official:
        if not final:
            raise S4EvaluationRefused("S4 is a one-shot holdout: pass --final")
        if (out_dir / "s4_summary.json").exists():
            raise S4EvaluationRefused(f"S4 was already evaluated ({out_dir / 's4_summary.json'}); never re-run")
        problems = tool.verify()
        if problems:
            raise S4EvaluationRefused("S4 does not verify: " + "; ".join(problems[:3]))
    data = tool.load(dataset_path or tool.DATA_FILE)
    planner = Planner.load(model_file)
    summary, rows = evaluate(data["cases"], tool.snapshots(), planner)
    summary = {"classification": CLASSIFICATION, "official_one_shot": official, "final": final,
               "generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "s4_fingerprint": tool.fingerprint(data), "planner_model_sha256": _sha(model_file),
               "planner_weights_sha256": planner.weights_sha256(),
               "source_sha256": {p: _sha(ROOT / p) for p in HASHED}, **summary}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "s4_rows.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "s4_summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args(argv)
    try:
        s = run(final=args.final)
    except S4EvaluationRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: s[k] for k in ("cases", "screen", "end_to_end", "verifier", "by_category", "by_language")},
                     indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
