#!/usr/bin/env python3
"""
Train and evaluate the AI-3 salience ranker on frozen SYNTHETIC patients (not clinical data, not clinical validation).

    python scripts/ai3_train_eval.py train              # train on the train split, evaluate on dev, print (writes nothing)
    python scripts/ai3_train_eval.py train --write      # also write ai3/data/salience_model.json and eval/ai3/dev_report.json
    python scripts/ai3_train_eval.py final              # ONE-SHOT: evaluate the written model on the test split

Splits are generated from fixed seeds (constants below, fixed before the first run). The model has no tuned hyper-parameters: the
values below are the first and only configuration. `final` writes eval/ai3/test_report.json and the lock eval/ai3/FINAL_DONE, and refuses
to run again. Gold labels are the same-author reference policy in ai3/synthetic.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai3.evaluate import evaluate  # noqa: E402
from ai3.salience import MODEL_FILE, SalienceModel  # noqa: E402
from ai3.synthetic import fingerprint, generate_cases, labelled_rows  # noqa: E402

SPLITS = {"train": (20261101, 3000), "dev": (20261102, 600), "test": (20261103, 1000)}
HYPER = {"epochs": 60, "lr": 0.5, "l2": 1e-4, "seed": 20261010}
LABEL_NOISE = 0.10          # training labels flipped with this probability (seeded); evaluation uses the clean reference policy
BUDGETS = (3, 5)            # facts per brief
OUT = ROOT / "eval" / "ai3"
LOCK = OUT / "FINAL_DONE"


def split(name):
    seed, n = SPLITS[name]
    return generate_cases(seed, n)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("train", "final"))
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    if a.mode == "train":
        train, dev = split("train"), split("dev")
        model = SalienceModel.train(labelled_rows(train, LABEL_NOISE, 20261105), **HYPER)
        report = {"classification": "SYNTHETIC DATA, SYNTHETIC SAME-AUTHOR GOLD; NOT CLINICAL VALIDATION", "split": "dev", "hyper": {**HYPER, "label_noise": LABEL_NOISE},
                  "fingerprints": {"train": fingerprint(train), "dev": fingerprint(dev)}, "weights_sha256": model.weights_sha256(), "result": {f"k={k}": evaluate(dev, model, k) for k in BUDGETS},
                  "history": "a first dev run without label noise and with k=5 only reached the reference ceiling (recall 1.0, no critical misses); label noise and k=3 were added before the test split was ever evaluated"}
        print(json.dumps(report, indent=1))
        if a.write:
            OUT.mkdir(parents=True, exist_ok=True)
            MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
            MODEL_FILE.write_text(json.dumps(model.to_json(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
            (OUT / "dev_report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
            print("wrote", MODEL_FILE.relative_to(ROOT), "sha256", hashlib.sha256(MODEL_FILE.read_bytes()).hexdigest())
        return 0
    if LOCK.exists():
        print("refused: the test split was already evaluated (eval/ai3/FINAL_DONE); never re-run")
        return 2
    model = SalienceModel.load()
    test = split("test")
    report = {"classification": "SYNTHETIC DATA, SYNTHETIC SAME-AUTHOR GOLD; NOT CLINICAL VALIDATION", "split": "test", "one_shot": True,
              "model_file_sha256": hashlib.sha256(MODEL_FILE.read_bytes()).hexdigest(), "weights_sha256": model.weights_sha256(),
              "fingerprints": {"test": fingerprint(test)}, "result": {f"k={k}": evaluate(test, model, k) for k in BUDGETS}}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "test_report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    LOCK.write_text("evaluated once; do not re-run\n", encoding="utf-8")
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
