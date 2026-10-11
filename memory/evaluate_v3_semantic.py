#!/usr/bin/env python3
"""
AI-2 v3 first semantic-model experiment: Block 2A vs 2A + semantic vs 2A + C10 vs 2A + C10 + semantic.

    ./venv/bin/python -m memory.evaluate_v3_semantic embed     # e5 embeddings of every question used below
    ./venv/bin/python -m memory.evaluate_v3_semantic train     # head; L2 chosen on TUNING sets
    ./venv/bin/python -m memory.evaluate_v3_semantic select    # tiers on TUNING sets -> frozen_config.json
    ./venv/bin/python -m memory.evaluate_v3_semantic heldout   # ONCE: held-out comparison (needs the freeze)

Material (fixed before anything was run):
  TRAIN    S3 train + hardneg train (minus any sentence containing a PARA v2 held-out word; words only)
  TUNING   v2_para dev + devcal dev          -- L2 and tiers are chosen here and nowhere else
  HELD-OUT PARA v2 dev (unseen paraphrases / synonyms / C10 / imperatives, en / manglish / ml), hardneg dev,
           devcal cal (report-only)        -- never used for any choice
  NOT USED PARA v2 accept and held-out labels (locked), v2_para accept, S3 test, S4, S5, S6.
Block 2A is the frozen copy (artifacts/models/v2_block4b/frozen_block2a, hash-checked) in a child interpreter;
the semantic arm and C10 wrap it from outside (memory.semantic.SemanticArm, memory.c10.C10Arm).
Tier rule: grid GRID_HIGH x GRID_MEDIUM (medium <= high). Feasible only if on EACH tuning set, versus 2A + C10:
no unsafe answer that 2A + C10 does not already give (correction recorded in the freeze: the first version said
"0 unsafe", unattainable because 2A + C10 itself has 1 unsafe answer on v2_para dev), wrong-but-valid <=,
clarifications of must-refuse questions <=, must-refuse questions refused >=.
Best = most answerable questions answered-or-recovered, then fewer unnecessary confirmations, then the higher
thresholds. None feasible -> the semantic arm is disabled (inf) and the held-out run reports that.
SYNTHETIC; NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import resource
import subprocess
import sys
import tempfile
import time

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "artifacts" / "models" / "semantic_v3"
EMB = OUT / "embeddings.npz"
HEAD = OUT / "head.json"
FREEZE = OUT / "frozen_config.json"
RESULTS = OUT / "heldout_results.json"
TRAIN = {"s3_train": ("s3_generator", "train"), "hardneg_train": ("v2_hardneg", "train")}
TUNING = {"v2_para_dev": ("v2_para", "dev"), "devcal_dev": ("v2_devcal", "dev")}
HELDOUT = {"para2_dev": ("v2_para2", "dev"), "hardneg_dev": ("v2_hardneg", "dev"),
           "devcal_cal_report_only": ("v2_devcal", "cal")}
EXTRA = {"s3_dev": ("s3_generator", "dev")}  # head accuracy report only
L2_GRID = (1e-3, 1e-2, 1e-1)
GRID_HIGH = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99)
GRID_MEDIUM = (0.3, 0.5, 0.7, 0.9, 0.95, 0.98, 0.99)
CLINICAL = {"DIAGNOSIS", "TREATMENT", "CLINICAL"}


def _tool(name):
    spec = importlib.util.spec_from_file_location(name + "_v3", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def label_of(item: dict) -> str:
    """Head label for a benchmark item: its frame, or an ABSTAIN_* class."""
    if "gold_plan" in item:  # S3
        gp = item["gold_plan"]
        if gp["kind"] == "query":
            return item["frame"]
        reason = gp.get("reason") or item["frame"]
    else:
        g = item["gold"]
        if g["kind"] == "query":
            return g["frame"]
        if g["kind"] == "ambiguous":
            return "ABSTAIN_AMBIGUOUS"
        reason = g.get("reason") or item.get("label")
    if reason in CLINICAL:
        return "ABSTAIN_CLINICAL"
    if reason in ("AMBIGUOUS", "UNAVAILABLE", "OTHER_PATIENT"):
        return "ABSTAIN_" + reason
    return "ABSTAIN_OOS"


def items(sets: dict) -> dict:
    return {name: _tool(t).load(split) for name, (t, split) in sets.items()}


def training_items() -> tuple[list[dict], dict]:
    from memory.planner import tokenize
    words = set(_tool("v2_para2").training_exclusion_words())  # words only; never their categories
    kept, dropped = [], {}
    for name, rows in items(TRAIN).items():
        bad = [r for r in rows if set(tokenize(r["question"])) & words]
        dropped[name] = len(bad)
        kept += [r for r in rows if r not in bad]
    return kept, {"dropped_for_heldout_words": dropped, "examples": len(kept)}


# ---- embed ------------------------------------------------------------------------------------------------
def embed() -> dict:
    from memory.semantic import E5Encoder, text_key
    texts = []
    for sets in (TRAIN, TUNING, HELDOUT, EXTRA):
        for rows in items(sets).values():
            texts += [r["question"] for r in rows if isinstance(r["question"], str)]
    texts = sorted(set(texts))
    t0 = time.time()
    enc = E5Encoder.load()
    load_s = time.time() - t0
    t0, lat = time.time(), []
    vecs = []
    for t in texts:
        s = time.perf_counter()
        vecs.append(enc.encode(t))
        lat.append(time.perf_counter() - s)
    np.savez(EMB, keys=np.array([text_key(t) for t in texts]), vecs=np.stack(vecs))
    lat = sorted(lat)
    info = {"texts": len(texts), "load_and_verify_seconds": round(load_s, 2), "total_seconds": round(time.time() - t0, 1),
            "latency_ms": {"mean": round(1000 * sum(lat) / len(lat), 1), "p50": round(1000 * lat[len(lat) // 2], 1),
                           "p95": round(1000 * lat[int(len(lat) * 0.95)], 1), "max": round(1000 * lat[-1], 1)},
            "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6, 1),
            "embeddings_sha256": _sha(EMB)}
    (OUT / "embed_info.json").write_text(json.dumps(info, indent=1) + "\n")
    return info


def table() -> dict:
    d = np.load(EMB)
    return dict(zip(d["keys"].tolist(), d["vecs"]))


# ---- train ------------------------------------------------------------------------------------------------
def train() -> dict:
    from memory.semantic import FrameHead, text_key
    tab = table()
    rows, info = training_items()
    X = np.stack([tab[text_key(r["question"])] for r in rows])
    y = [label_of(r) for r in rows]

    def acc(head, rs):
        good = [head.labels[int(np.argmax(head.proba(tab[text_key(r["question"])])))] == label_of(r)
                for r in rs if isinstance(r["question"], str)]
        return round(sum(good) / len(good), 4), len(good)

    tuning = [r for rs in items(TUNING).values() for r in rs]
    trace, best = [], None
    for l2 in L2_GRID:
        head = FrameHead.train(X, y, l2)
        t_acc = acc(head, tuning)
        trace.append({"l2": l2, "train_acc": acc(head, rows)[0], "tuning_acc": t_acc[0]})
        if best is None or t_acc[0] > best[0]:  # ties: the smaller L2 seen first wins
            best = (t_acc[0], l2, head)
    head = best[2]
    head.save(HEAD)
    s3dev = items(EXTRA)["s3_dev"]
    record = {**info, "labels": head.labels, "label_counts": {lab: y.count(lab) for lab in head.labels},
              "l2_trace": trace, "chosen_l2": best[1], "s3_dev_acc": acc(head, s3dev),
              "tuning_acc_by_set": {n: acc(head, rs) for n, rs in items(TUNING).items()},
              "head_sha256": head.sha256()}
    (OUT / "train_info.json").write_text(json.dumps(record, indent=1) + "\n")
    return record


# ---- child: frozen Block 2A + wrappers ------------------------------------------------------------------
_CHILD = r'''
import importlib.util, json, sys, pathlib
root, frozen, job_file, out_file = sys.argv[1:5]
sys.path.insert(0, root)
import memory
for name in %r:
    spec = importlib.util.spec_from_file_location("memory." + name, pathlib.Path(frozen) / (name + ".py"))
    mod = importlib.util.module_from_spec(spec); sys.modules["memory." + name] = mod; spec.loader.exec_module(mod)
from memory import evaluate_v2 as ev
from memory.c10 import C10Arm
from memory.evaluate_v3_semantic import child_run
from memory.planner import Planner
from memory.v2 import V2Planner
assert sys.modules["memory.v2"].__file__.startswith(frozen)
base = V2Planner(Planner.load(ev.MODEL_FILE), **dict(ev.ARMS["E_v2_foundations"], reference_check=True))
pathlib.Path(out_file).write_text(json.dumps(child_run(base, json.loads(pathlib.Path(job_file).read_text())),
                                             ensure_ascii=False))
'''


class _Memo:
    def __init__(self, base):
        self.base, self.cache = base, {}

    def interpret(self, q):
        key = json.dumps(q, ensure_ascii=False) if not isinstance(q, str) else q
        if key not in self.cache:
            self.cache[key] = self.base.interpret(q)
        return self.cache[key]


def child_run(base, job: dict) -> dict:
    """Runs inside the frozen-2A interpreter. job: {"sets": {name: [tool, split]}, "tiers": [..]}"""
    from memory import evaluate_v2 as ev
    from memory import evaluate_v2_block4 as b4
    from memory.c10 import C10Arm
    from memory.semantic import CachedEncoder, FrameHead, SemanticArm, SemanticInterpreter
    from memory.v2 import answer_with_snapshot_v2
    sem = SemanticInterpreter(CachedEncoder(table()), FrameHead.load(HEAD))
    memo = _Memo(base)
    result = {}
    for name, (tool_name, split) in job["sets"].items():
        tool = _tool(tool_name)
        cases, snaps = tool.load(split), tool.snapshots()
        per = {}
        for tiers in job["tiers"]:
            arms = {"block2a": memo, "block2a_c10": C10Arm(memo)}
            if tiers is not None:
                sa = SemanticArm(memo, sem, tiers)
                arms = {"block2a_sem": sa, "block2a_c10_sem": C10Arm(sa)} if job.get("only_semantic") else \
                    {**arms, "block2a_sem": sa, "block2a_c10_sem": C10Arm(sa)}
            res = {}
            for arm, obj in arms.items():
                rows = []
                for case in cases:
                    out = answer_with_snapshot_v2(snaps[case["patient"]], case["question"], obj)
                    interp = out.get("interpretation") or {}
                    top = min(interp.get("candidates") or [], key=lambda c: c["rank"], default=None)
                    gold_q = case["gold"].get("query")
                    s = out.get("semantic") or (sem.predict(case["question"]) if isinstance(case["question"], str) else None)
                    rows.append({"id": case["id"], "question": case["question"], "language": case["language"],
                                 "class": case.get("class") or case.get("category"), "outcome": out["outcome"],
                                 "stage": out["stage"], "reason": out["reason"], "result": ev.classify(case, out),
                                 "frame": (out.get("plan") or {}).get("frame"),
                                 "options": [o["frame"] for o in out.get("options") or []],
                                 "gold_label": label_of(case), "planner_top_frame": top["frame"] if top else None,
                                 "semantic": s and {"label": s["label"], "probability": s["probability"]},
                                 "semantic_consulted": "semantic" in out,
                                 "confidence": 1.0 if out["outcome"] == "answer" else None,
                                 "top_probability": top["probability"] if top else None,
                                 "top_is_gold": bool(top and gold_q and top["query"] == gold_q)})
                m = b4.measures(cases, {"summary": ev.summarise(cases, rows), "rows": rows})
                m["clarify_out_of_scope"] = sum(r["result"] == "clarify_out_of_scope" for r in rows)
                res[arm] = {"measures": m, "rows": rows if job.get("rows") else None}
            per[json.dumps(tiers)] = res
        result[name] = per
    return result


def run_child(job: dict) -> dict:
    from memory import evaluate_v2_block4 as b4
    b4.check_frozen()
    with tempfile.TemporaryDirectory() as tmp:
        jf, of = pathlib.Path(tmp) / "job.json", pathlib.Path(tmp) / "out.json"
        jf.write_text(json.dumps(job))
        subprocess.run([sys.executable, "-c", _CHILD % (b4.FROZEN_ORDER,), str(ROOT), str(b4.FROZEN), str(jf), str(of)],
                       check=True)
        return json.loads(of.read_text())


# ---- select -----------------------------------------------------------------------------------------------
def feasible(base: dict, cand: dict, base_rows: list, cand_rows: list) -> bool:
    new_unsafe = [y for x, y in zip(base_rows, cand_rows) if y["result"] == "unsafe" and x["result"] != "unsafe"]
    return not new_unsafe and cand["unsafe"] <= base["unsafe"] and cand["wrong_valid"] <= base["wrong_valid"] and \
        cand["clarify_out_of_scope"] <= base["clarify_out_of_scope"] and \
        cand["refuse_cases_refused"] >= base["refuse_cases_refused"]


def select() -> dict:
    grid = [{"high": h, "medium": m} for h in GRID_HIGH for m in GRID_MEDIUM if m <= h]
    res = run_child({"sets": {k: list(v) for k, v in TUNING.items()}, "tiers": grid, "only_semantic": False, "rows": True})
    trace, best = [], None
    for tiers in grid:
        key = json.dumps(tiers)
        ok, rec, conf = True, 0, 0
        for name in TUNING:
            base = res[name][key]["block2a_c10"]["measures"]
            cand = res[name][key]["block2a_c10_sem"]["measures"]
            ok &= feasible(base, cand, res[name][key]["block2a_c10"]["rows"], res[name][key]["block2a_c10_sem"]["rows"])
            rec += cand["recovered"]
            conf += cand["unnecessary_confirmations"]
        trace.append({**tiers, "feasible": ok, "recovered": rec, "confirmations": conf})
        score = (rec, -conf, tiers["high"], tiers["medium"])
        if ok and (best is None or score > best[0]):
            best = (score, tiers)
    base_rec = sum(res[n][json.dumps(grid[0])]["block2a_c10"]["measures"]["recovered"] for n in TUNING)
    tiers = best[1] if best else {"high": float("inf"), "medium": float("inf")}
    head = json.loads(HEAD.read_text())
    from memory import evaluate_v2_block4 as b4
    freeze = {"tiers": tiers, "disabled": best is None, "baseline_c10_recovered_on_tuning": base_rec,
              "protocol_note": "feasibility rule corrected before any held-out run: '0 unsafe' -> 'no unsafe answer "
                               "beyond 2A + C10' (2A + C10 has 1 unsafe answer on v2_para dev).",
              "model": {"repo": "intfloat/multilingual-e5-small", "commit": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
                        "files_sha256": __import__("memory.semantic", fromlist=["MODEL_SHA256"]).MODEL_SHA256},
              "head_sha256": __import__("memory.semantic", fromlist=["FrameHead"]).FrameHead.load(HEAD).sha256(),
              "head_l2": head["meta"]["l2"], "embeddings_sha256": _sha(EMB),
              "semantic_py_sha256": _sha(ROOT / "memory" / "semantic.py"), "c10_py_sha256": _sha(ROOT / "memory" / "c10.py"),
              "frozen_block2a": b4.check_frozen(), "frozen_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    FREEZE.write_text(json.dumps(freeze, indent=1) + "\n")
    (OUT / "tier_selection.json").write_text(json.dumps({"trace": trace, "chosen": tiers}, indent=1) + "\n")
    return {"chosen": tiers, "disabled": best is None, "feasible": [t for t in trace if t["feasible"]][-5:],
            "baseline_c10_recovered": base_rec, "best_infeasible": max(trace, key=lambda t: t["recovered"])}


# ---- held-out ---------------------------------------------------------------------------------------------
def heldout() -> dict:
    if RESULTS.exists():
        raise SystemExit("Refused: the held-out comparison was already run")
    freeze = json.loads(FREEZE.read_text())
    for key, path in (("head_sha256", None), ("embeddings_sha256", EMB), ("semantic_py_sha256", ROOT / "memory" / "semantic.py"),
                      ("c10_py_sha256", ROOT / "memory" / "c10.py")):
        actual = __import__("memory.semantic", fromlist=["FrameHead"]).FrameHead.load(HEAD).sha256() if path is None else _sha(path)
        if actual != freeze[key]:
            raise SystemExit(f"Refused: {key} changed since the freeze")
    tiers = freeze["tiers"]
    sets = {**HELDOUT, **TUNING}
    res = run_child({"sets": {k: list(v) for k, v in sets.items()}, "tiers": [tiers], "rows": True})
    out = {"freeze": freeze, "heldout_sets": list(HELDOUT), "tuning_sets_in_sample": list(TUNING), "sets": {}}
    for name in sets:
        out["sets"][name] = res[name][json.dumps(tiers)]
    RESULTS.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=("embed", "train", "select", "heldout"))
    args = parser.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if args.step == "heldout":
        res = heldout()
        for name, arms in res["sets"].items():
            for arm, r in arms.items():
                x = r["measures"]
                print(f"{name:24} {arm:16} correct {x['correct']}/{x['cases']} unsafe {x['unsafe']} wrong_valid {x['wrong_valid']} "
                      f"wrongful_ref {x['wrongful_refusals']}/{x['expect_answer']} conf {x['unnecessary_confirmations']} "
                      f"recovered {x['recovered']} refused {x['refuse_cases_refused']}/{x['refuse_cases']} "
                      f"clar_oos {x['clarify_out_of_scope']} by_lang {x['recovered_by_language']}")
        return 0
    print(json.dumps({"embed": embed, "train": train, "select": select}[args.step](), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
