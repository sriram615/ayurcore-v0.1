"""
Learned scope / OOS detector for AI-2 v2 (Block 2). Local, deterministic training; no patient data.

    det = OOSDetector.train(examples, epochs, seed)      examples: [(question, label), ...]
    det.predict(question) -> {"label", "margin", "scores", "fires"}

Labels: IN_SCOPE_MEMORY, CLINICAL, OTHER_PATIENT, UNAVAILABLE, UNSUPPORTED_OOS.
Model: one multiclass averaged perceptron (the planner's own trainer, memory.planner._train_factor)
over the planner's features of the normalised, delexicalised question (word uni/bigrams, character
3-4-grams, slot counts); optionally two coverage features (number of unknown words, presence of an
unsupported word) -- off in the selected configuration, because on dev they made the detector refuse
legitimate paraphrases (unknown words are common to both). No embeddings, no external model.

Safety contract (enforced in memory.v2, tested):
  - it reads only the question text: it is called inside V2Planner.interpret, before any snapshot;
  - it can only ADD a refusal: when it "fires" the outcome becomes abstain; when it does not fire,
    or the input is rejected by the normaliser / screen first, Block 1 decides unchanged;
  - it fires only when the best label is not IN_SCOPE_MEMORY AND its margin over the second best
    label is at least `threshold`; below that it is UNCERTAIN and Block 1 decides.
"""
from __future__ import annotations

import hashlib
import json

from memory.coverage import coverage
from memory.normalize import normalize
from memory.planner import _train_factor, delexicalise, features

LABELS = ("IN_SCOPE_MEMORY", "CLINICAL", "OTHER_PATIENT", "UNAVAILABLE", "UNSUPPORTED_OOS")
REASON = {"CLINICAL": "TREATMENT", "OTHER_PATIENT": "OTHER_PATIENT", "UNAVAILABLE": "UNAVAILABLE",
          "UNSUPPORTED_OOS": "AMBIGUOUS"}


def detector_features(question: str, coverage_features: bool = False) -> list[str]:
    norm = normalize(question)
    text = norm["text"] if norm["ok"] else ""
    feats = features(delexicalise(text)[0])
    if not coverage_features:
        return feats
    cov = coverage(text)
    unknown = sum(r == "UNKNOWN" for r in cov["roles"])
    feats.append(f"cov_unknown={min(unknown, 3)}")
    feats.append(f"cov_unsupported={int(bool(cov['unsupported']))}")
    return feats


class OOSDetector:
    def __init__(self, weights: dict | None = None, threshold: float = 0.0, meta: dict | None = None,
                 coverage_features: bool = False):
        self.weights = weights or {}
        self.threshold = threshold
        self.meta = meta or {}
        self.coverage_features = coverage_features

    @classmethod
    def train(cls, examples: list[tuple[str, str]], epochs: int = 10, seed: int = 0, threshold: float = 0.0,
              coverage_features: bool = False):
        data = [(detector_features(q, coverage_features), label) for q, label in examples]
        weights = _train_factor(data, LABELS, epochs, seed)
        return cls(weights, threshold, {"epochs": epochs, "seed": seed, "examples": len(examples)}, coverage_features)

    def scores(self, question: str) -> dict:
        s = {label: 0.0 for label in LABELS}
        for f in detector_features(question, self.coverage_features):
            for label, w in self.weights.get(f, {}).items():
                s[label] += w
        return s

    def predict(self, question: str, threshold: float | None = None) -> dict:
        t = self.threshold if threshold is None else threshold
        s = self.scores(question)
        ranked = sorted(LABELS, key=lambda label: (-s[label], LABELS.index(label)))
        margin = s[ranked[0]] - s[ranked[1]]
        fires = ranked[0] != "IN_SCOPE_MEMORY" and margin >= t
        return {"label": ranked[0], "margin": round(margin, 6), "fires": fires,
                "reason": REASON.get(ranked[0]) if fires else None}

    # ---- persistence ------------------------------------------------------------------------------
    def to_json(self) -> dict:
        return {"model": "multiclass averaged perceptron (scope / OOS)", "labels": list(LABELS),
                "threshold": self.threshold, "coverage_features": self.coverage_features, "meta": self.meta,
                "weights": {f: dict(sorted(v.items())) for f, v in sorted(self.weights.items())}}

    def weights_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_json()["weights"], sort_keys=True).encode()).hexdigest()

    def save(self, path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def load(cls, path) -> "OOSDetector":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("labels") != list(LABELS):
            raise ValueError("OOS model labels do not match this code")
        return cls(data["weights"], data["threshold"], data.get("meta"), data.get("coverage_features", False))
