#!/usr/bin/env python3
"""
AI-2 v2 Block 4B evaluation: Block 2A (frozen copy) vs Block 4B (current code).

    ./venv/bin/python -m memory.evaluate_v2_block4            # development sets only (repeatable)
    ./venv/bin/python -m memory.evaluate_v2_block4 --final    # + v2_para ACCEPT, ONCE; refused if results exist

Block 2A runs in a separate interpreter from the frozen module copies in
artifacts/models/v2_block4b/frozen_block2a/ (hash-checked against SHA256SUMS; reference.py there has
sha e006c093..., the accepted Block 2A rule). Block 4B is the current memory/ code (morphology on).
Sets: v2_para dev, v2_para accept (final only), devcal dev, devcal cal (report-only), hardneg dev, hardneg train.

PRE-REGISTERED CRITERIA (Block 4 design, C10 deferred) -- ACCEPT only if all hold:
  K1 unsafe answers == 0 for Block 4B on every set
  K2 wrong-but-valid answers: Block 4B <= Block 2A on every set, and <= 1% of Block 4B answers on accept
  K3 on accept: wrongful refusals AND unnecessary confirmations (clarifications of answerable questions)
     each fall by >= 30% relative to Block 2A
  K4 answerable questions answered or recovered by one clarification never fall, on any set, overall or in
     any language
  K5 hardneg refuse-cases refused: Block 4B >= Block 2A on both splits; no reference-check fire on the
     1,200 legitimate development questions
(K6 -- tests and minimal pairs -- is checked by the isolated test suite, not here.)
SYNTHETIC; NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FROZEN = ROOT / "artifacts" / "models" / "v2_block4b" / "frozen_block2a"
OUT_DIR = ROOT / "artifacts" / "models" / "v2_block4b"
FROZEN_ORDER = ("normalize", "confidence", "interpret", "scope", "coverage", "reference", "v2")
BLOCK2A_REFERENCE_SHA = "e006c0936545c6f8960942c49e2978793ffce4fb642ea043bb506650bae3cfab"
SETS = {"para_dev": ("v2_para", "dev"), "para_accept": ("v2_para", "accept"), "devcal_dev": ("v2_devcal", "dev"),
        "devcal_cal_report_only": ("v2_devcal", "cal"), "hardneg_dev": ("v2_hardneg", "dev"),
        "hardneg_train": ("v2_hardneg", "train")}


class AcceptRefused(RuntimeError):
    """v2_para accept is one-shot."""


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_frozen() -> dict:
    sums = {}
    for line in (FROZEN / "SHA256SUMS").read_text().splitlines():
        digest, path = line.split()
        sums[pathlib.Path(path).name] = digest
    for name in FROZEN_ORDER:
        actual = hashlib.sha256((FROZEN / f"{name}.py").read_bytes()).hexdigest()
        if sums.get(f"{name}.py") != actual:
            raise RuntimeError(f"frozen Block 2A {name}.py does not match SHA256SUMS")
    if sums["reference.py"] != BLOCK2A_REFERENCE_SHA:
        raise RuntimeError("frozen reference.py is not the accepted Block 2A rule")
    return sums


_CHILD = r'''
import importlib.util, json, sys, pathlib
root, frozen, tool_name, split, out = sys.argv[1:6]
sys.path.insert(0, root)
import memory
for name in %r:
    spec = importlib.util.spec_from_file_location("memory." + name, pathlib.Path(frozen) / (name + ".py"))
    mod = importlib.util.module_from_spec(spec); sys.modules["memory." + name] = mod; spec.loader.exec_module(mod)
from memory import evaluate_v2 as ev
from memory.planner import Planner
spec = importlib.util.spec_from_file_location("tool", pathlib.Path(root) / "scripts" / (tool_name + ".py"))
tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)
cases = tool.load(split)
summ, rows = ev.evaluate(cases, tool.snapshots(), Planner.load(ev.MODEL_FILE),
                         {"block2a": dict(ev.ARMS["E_v2_foundations"], reference_check=True)})
assert sys.modules["memory.v2"].__file__.startswith(frozen)
pathlib.Path(out).write_text(json.dumps({"summary": summ["block2a"], "rows": rows["block2a"]}, ensure_ascii=False))
''' % (FROZEN_ORDER,)


def run_block2a(tool_name: str, split: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out = pathlib.Path(tmp) / "out.json"
        subprocess.run([sys.executable, "-c", _CHILD, str(ROOT), str(FROZEN), tool_name, split, str(out)], check=True)
        return json.loads(out.read_text())


def run_block4b(tool, split: str) -> dict:
    from memory import evaluate_v2 as ev
    from memory.planner import Planner
    summ, rows = ev.evaluate(tool.load(split), tool.snapshots(), Planner.load(ev.MODEL_FILE),
                             {"block4b": dict(ev.ARMS["E_v2_foundations"], reference_check=True)})
    return {"summary": summ["block4b"], "rows": rows["block4b"]}


def measures(cases: list[dict], res: dict) -> dict:
    s, rows = res["summary"], res["rows"]
    expect = [(c, r) for c, r in zip(cases, rows) if c["gold"]["kind"] == "query"
              and c["gold_answer"]["status"] != "not_recorded" and c["gold_answer"]["sources"]]
    refuse = [(c, r) for c, r in zip(cases, rows) if c["gold"]["kind"] == "abstain"]
    langs = sorted({c["language"] for c, _ in expect})
    recovered = lambda pairs: sum(r["result"] in ("correct", "clarify_recoverable") for _, r in pairs)
    return {"unsafe": s["unsafe"], "wrong_valid": s["wrong_but_valid"]["count"], "answers": s["coverage_answer_rate"]["count"],
            "wrongful_refusals": s["wrongful_refusals"]["count"], "expect_answer": len(expect),
            "unnecessary_confirmations": sum(r["outcome"] == "clarify" for c, r in zip(cases, rows)
                                             if c["gold"]["kind"] == "query"),
            "recovered": recovered(expect),
            "recovered_by_language": {lang: recovered([(c, r) for c, r in expect if c["language"] == lang]) for lang in langs},
            "refuse_cases_refused": sum(r["outcome"] == "abstain" for _, r in refuse), "refuse_cases": len(refuse),
            "correct": s["end_to_end_correct"]["count"], "cases": s["cases"], "results": s["results"]}


def legit_false_fires() -> list[str]:
    from memory.reference import outside_scope
    s3 = _load("s3_gen_b4", ROOT / "scripts" / "s3_generator.py")
    dc = _load("dc_b4", ROOT / "scripts" / "v2_devcal.py")
    hn = _load("hn_b4", ROOT / "scripts" / "v2_hardneg.py")
    legit = [q["question"] for sp in ("train", "dev") for q in s3.load(sp) if q["gold_plan"]["kind"] == "query"]
    legit += [c["question"] for c in dc.load("dev") if c["category"] in ("answerable", "paraphrase", "ambiguous")]
    legit += [c["question"] for c in hn.load() if c["label"] == "IN_SCOPE_MEMORY"]
    return [q for q in legit if outside_scope(q)["outside"]]


def criteria(m: dict, false_fires: list) -> dict:
    sets = list(m)
    k = {}
    k["K1_no_unsafe"] = all(m[s]["block4b"]["unsafe"] == 0 for s in sets)
    k["K2_no_more_wrong_valid"] = all(m[s]["block4b"]["wrong_valid"] <= m[s]["block2a"]["wrong_valid"] for s in sets)
    if "para_accept" in m:
        a, b = m["para_accept"]["block2a"], m["para_accept"]["block4b"]
        k["K2_accept_wrong_valid_le_1pct"] = b["wrong_valid"] <= 0.01 * max(b["answers"], 1)
        k["K3_accept_wrongful_refusals_-30pct"] = b["wrongful_refusals"] <= 0.7 * a["wrongful_refusals"]
        k["K3_accept_unnecessary_confirmations_-30pct"] = b["unnecessary_confirmations"] <= 0.7 * a["unnecessary_confirmations"]
    k["K4_recovery_never_falls"] = all(
        m[s]["block4b"]["recovered"] >= m[s]["block2a"]["recovered"] and
        all(m[s]["block4b"]["recovered_by_language"].get(lang, 0) >= v
            for lang, v in m[s]["block2a"]["recovered_by_language"].items()) for s in sets)
    k["K5_hardneg_refusals_kept"] = all(m[s]["block4b"]["refuse_cases_refused"] >= m[s]["block2a"]["refuse_cases_refused"]
                                        for s in sets if s.startswith("hardneg"))
    k["K5_no_reference_false_fire"] = not false_fires
    return k


def run(final: bool = False, out_dir=OUT_DIR) -> dict:
    out_dir = pathlib.Path(out_dir)
    sums = check_frozen()
    para = _load("v2_para_b4", ROOT / "scripts" / "v2_para.py")
    tools = {"v2_para": para, "v2_devcal": _load("v2_devcal_b4", ROOT / "scripts" / "v2_devcal.py"),
             "v2_hardneg": _load("v2_hardneg_b4", ROOT / "scripts" / "v2_hardneg.py")}
    for name, tool in tools.items():
        if tool.verify():
            raise RuntimeError(f"{name} does not verify")
    sets = dict(SETS)
    if final:
        if (out_dir / "final_summary.json").exists():
            raise AcceptRefused("v2_para accept was already evaluated; never re-run")
    else:
        sets.pop("para_accept")
    result = {"classification": "SYNTHETIC RESULT -- NOT CLINICAL VALIDATION", "final": final,
              "frozen_block2a_sha256": sums, "para_accept_fingerprint": para.fingerprint(para.load("accept")),
              "sets": {}}
    m = {}
    for name, (tool_name, split) in sets.items():
        cases = tools[tool_name].load(split)
        a, b = run_block2a(tool_name, split), run_block4b(tools[tool_name], split)
        m[name] = {"block2a": measures(cases, a), "block4b": measures(cases, b)}
        result["sets"][name] = m[name]
        (out_dir / f"{name}_rows.json").write_text(json.dumps({"block2a": a["rows"], "block4b": b["rows"]},
                                                              indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    fires = legit_false_fires()
    result["reference_false_fires"] = fires
    result["criteria"] = criteria(m, fires)
    result["verdict"] = "ACCEPT" if final and all(result["criteria"].values()) else \
        ("REJECT" if final else "development run (no verdict)")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / ("final_summary.json" if final else "dev_summary.json")).write_text(
        json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args(argv)
    try:
        r = run(final=args.final)
    except AcceptRefused as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2
    for name, d in r["sets"].items():
        for arm in ("block2a", "block4b"):
            x = d[arm]
            print(f"{name:24} {arm}: correct {x['correct']}/{x['cases']} unsafe {x['unsafe']} wrong_valid {x['wrong_valid']} "
                  f"wrongful_ref {x['wrongful_refusals']}/{x['expect_answer']} confirmations {x['unnecessary_confirmations']} "
                  f"recovered {x['recovered']}/{x['expect_answer']} by_lang {x['recovered_by_language']} "
                  f"refused {x['refuse_cases_refused']}/{x['refuse_cases']}")
    print("reference false fires:", len(r["reference_false_fires"]))
    print(json.dumps(r["criteria"], indent=1), r["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
