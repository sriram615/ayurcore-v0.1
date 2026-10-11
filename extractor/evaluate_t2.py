#!/usr/bin/env python3
"""
T2 evaluation: spans -> ClinicalParse, graded by the existing clinical-eval grader.

    ./venv/bin/python -m extractor.evaluate_t2 [--model artifacts/models/t1_tagger/model.json] [--out artifacts/models/t2_eval]

Runs (each written as raw + graded artifacts in the harness schema):
  oracle_<split>     gold spans from the S1 construction record -> assemble   (train, dev)
                     measures the deterministic rules alone
  predicted_<split>  T1 tagger spans -> assemble                               (train, dev)
                     measures the whole extractor
  predicted_v1       T1 tagger spans -> assemble on the 12 independent v1 notes
                     OUT-OF-DISTRIBUTION EVIDENCE ONLY: no rule or lexicon is tuned on it

s1_test.json is never opened (extractor.train.load_cases refuses it).
SYNTHETIC TECHNICAL RESULT, NOT CLINICAL VALIDATION. S1 train/dev share the
generator's templates; L6 was written with a known dev failure visible.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from extractor import train as t1  # noqa: E402
from extractor.assemble import L6_NOTE, assemble  # noqa: E402
from extractor.labels import case_spans  # noqa: E402
from extractor.pipeline import Extractor  # noqa: E402

HARNESS_PATH = ROOT / "scripts" / "clinical_eval_harness.py"
DEFAULT_MODEL = t1.DEFAULT_OUT / "model.json"
DEFAULT_OUT = ROOT / "artifacts" / "models" / "t2_eval"
CODE_FILES = ("units.py", "normalize.py", "assemble.py", "pipeline.py", "evaluate_t2.py")


def _harness():
    spec = importlib.util.spec_from_file_location("clinical_eval_harness_t2", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _code_sha256() -> str:
    import hashlib
    h = hashlib.sha256()
    for name in CODE_FILES:
        with open(ROOT / "extractor" / name, "rb") as f:
            h.update(name.encode() + b"\0" + f.read())
    return h.hexdigest()


def _records(arm: str, cases: list[dict], spans_for) -> list[dict]:
    records = []
    for case in cases:
        started = time.monotonic()
        spans = spans_for(case)
        parsed, report = assemble(case["notes"], spans)
        records.append({"case_id": case["case_id"], "repeat": 0, "arm": arm, "status": "ok",
                        "parsed": parsed.model_dump(), "exception": None,
                        "latency_ms": int((time.monotonic() - started) * 1000), "tool_calls": [], "log_records": [],
                        "assembly": report,
                        "spans": [{"start": s, "end": e, "type": t, "text": case["notes"][s:e]} for s, e, t in spans]})
    return records


def run(model_path=DEFAULT_MODEL, out_dir=DEFAULT_OUT) -> dict:
    h = _harness()
    extractor = Extractor(t1.load_model(model_path))
    with open(model_path, encoding="utf-8") as f:
        model_provenance = json.load(f)["provenance"]
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    runs = [("oracle", "train", t1.TRAIN_FILE), ("oracle", "dev", t1.DEV_FILE),
            ("predicted", "train", t1.TRAIN_FILE), ("predicted", "dev", t1.DEV_FILE),
            ("predicted", "v1", t1.V1_FILE)]
    summary = {"classification": t1.CLASSIFICATION, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "t1_model": str(model_path), "t1_weights_sha256": extractor.model.weights_sha256(),
               "t1_code_sha256": model_provenance["code_sha256"], "t2_code_sha256": _code_sha256(),
               "s1_dataset_fingerprint": model_provenance["s1_dataset_fingerprint"],
               "l6_note": L6_NOTE, "v1_note": "out-of-distribution evidence only; never used for tuning", "runs": {}}
    for mode, split, path in runs:
        case_data = h.load_case_file(path) if split == "v1" else {**h.load_case_file(path), "cases": t1.load_cases(path)}
        cases = case_data["cases"]
        spans_for = (lambda c: case_spans(c)) if mode == "oracle" else (lambda c: extractor.spans(c["notes"]))
        arm = f"t2_{mode}_spans"
        records = _records(arm, cases, spans_for)
        raw = {"schema_version": h.RAW_SCHEMA_VERSION,
               "metadata": {"arm": arm, "mode": mode, "parser": "extractor (T1 tagger) + extractor.assemble",
                            "case_file": str(path), "case_file_sha256": h.sha256_file(path),
                            "corpus_id": case_data.get("corpus_id"), "evaluation_class": case_data.get("evaluation_class"),
                            "t1_weights_sha256": summary["t1_weights_sha256"], "t2_code_sha256": summary["t2_code_sha256"],
                            "repeats": 1, "generated_at_utc": summary["generated_at_utc"]},
               "records": records}
        graded = h.build_graded_report(raw, case_data, None)
        name = f"{mode}_{split}"
        with open(out_dir / f"{name}_raw.json", "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=1, ensure_ascii=False)
        with open(out_dir / f"{name}_graded.json", "w", encoding="utf-8") as f:
            json.dump(graded, f, indent=1, ensure_ascii=False)
        c = graded["summary"]["clinical"]
        by_category = {}
        for result in graded["results"]:
            label = next(x for x in cases if x["case_id"] == result["case_id"]).get("label") or "v1"
            row = by_category.setdefault(label, {"cases": 0, "pass": 0})
            row["cases"] += 1
            row["pass"] += result["outcome"] == "PASS"
        summary["runs"][name] = {
            "cases": len(cases), "outcomes": graded["summary"]["outcomes"],
            "pass": c["passed"], "graded": c["graded_attempts"], "pass_rate": c["pass_rate"],
            "herb_precision": c["herb_micro_precision"], "herb_recall": c["herb_micro_recall"],
            "dosha_exact": c["dosha_exact"], "quantities_exact": c["quantities_within_tolerance"],
            "quantities_matched": c["matched_quantities"], "edits": c["doctor_edits_total"],
            "l6_revisions": sum(len(r["assembly"]["l6_revisions"]) for r in records),
            "l6_cases": sorted(r["case_id"] for r in records if r["assembly"]["l6_revisions"]),
            "dropped_herbs": sum(len(r["assembly"]["dropped_herbs"]) for r in records),
            "dropped_quantities": sum(len(r["assembly"]["dropped_quantities"]) for r in records),
            "conflicts": sum(len(r["assembly"]["conflicts"]) for r in records),
            "by_category": by_category,
            "failed_cases": [r["case_id"] for r in graded["results"] if r["outcome"] not in ("PASS", "AMBIGUOUS_REVIEW")],
        }
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, ensure_ascii=False)
    summary["_out"] = str(out_dir)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    summary = run(args.model, args.out)
    print(t1.CLASSIFICATION)
    for name, r in summary["runs"].items():
        print(f"{name:<16} pass {r['pass']}/{r['graded']}  herb P/R {r['herb_precision']}/{r['herb_recall']}  "
              f"dosha {r['dosha_exact']}  qty {r['quantities_exact']}/{r['quantities_matched']}  edits {r['edits']}  "
              f"L6 {r['l6_cases']}  failed {r['failed_cases']}")
    print(f"artifacts: {summary['_out']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
