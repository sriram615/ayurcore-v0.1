"""
AI-3 salience: a small LEARNED ranker that decides which verified facts a pre-consultation brief should show first.

The model is a logistic regression over (fact kind x patient context) features, trained in pure Python on synthetic patients whose
labels come from a written reference policy (ai3/synthetic.py). It has no language model, no network and no randomness at inference:
the same snapshot and date always give the same ranking. It ranks facts that were ALREADY verified (ai3.facts); it cannot add, edit or
invent a fact.

Context features (all in [0, 1]) are derived from the snapshot and an `as_of` date; they are inputs to ranking only and are never shown.
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import random
from typing import Iterable, Optional

from ai2.snapshot import PatientSnapshot
from ai3.facts import KINDS, Fact, days_between

MODEL_FILE = pathlib.Path(__file__).resolve().parent / "data" / "salience_model.json"
CONTEXT_KEYS = ("n_rx", "n_enc", "has_diff", "herbs_changed", "dosha_changed", "any_unknown", "last_unknown", "open_visit",
                "days_since_rx", "days_since_visit", "inexact")


def context(snapshot: PatientSnapshot, as_of: str, facts: list[Fact]) -> dict[str, float]:
    rx, enc = snapshot.prescriptions, snapshot.encounters
    kinds = {f.kind for f in facts}
    last_rx_day = rx[-1].created_at if rx else None
    last_visit_day = enc[-1].created_at if enc else None
    d_rx, d_visit = days_between(as_of, last_rx_day), days_between(as_of, last_visit_day)
    last = rx[-1] if rx else None
    return {
        "n_rx": min(len(rx), 10) / 10.0,
        "n_enc": min(len(enc), 10) / 10.0,
        "has_diff": 1.0 if len(rx) >= 2 else 0.0,
        "herbs_changed": 1.0 if "herbs_changed" in kinds else 0.0,
        "dosha_changed": 1.0 if "dosha_changed" in kinds else 0.0,
        "any_unknown": 1.0 if any(p.herbs is None or p.dosha is None for p in rx) else 0.0,
        "last_unknown": 1.0 if last is not None and (last.herbs is None or last.dosha is None) else 0.0,
        "open_visit": 1.0 if "open_visit" in kinds else 0.0,
        "days_since_rx": 1.0 if d_rx is None else min(max(d_rx, 0), 365) / 365.0,
        "days_since_visit": 1.0 if d_visit is None else min(max(d_visit, 0), 365) / 365.0,
        "inexact": 0.0,
    }


def features(fact: Fact, ctx: dict[str, float]) -> dict[str, float]:
    f = {f"{fact.kind}|bias": 1.0}
    for key in CONTEXT_KEYS:
        value = (0.0 if fact.exact else 1.0) if key == "inexact" else ctx[key]
        if value:
            f[f"{fact.kind}|{key}"] = value
    return f


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(min(x, 30.0), -30.0)))


class SalienceModel:
    def __init__(self, weights: Optional[dict[str, float]] = None, meta: Optional[dict] = None):
        self.weights = weights or {}
        self.meta = meta or {}

    def score(self, feats: dict[str, float]) -> float:
        return sum(self.weights.get(k, 0.0) * v for k, v in feats.items())

    def probability(self, feats: dict[str, float]) -> float:
        return _sigmoid(self.score(feats))

    # ---- training (offline; the only place randomness is used, seeded) ----
    @classmethod
    def train(cls, rows: Iterable[tuple[dict[str, float], int]], epochs: int = 60, lr: float = 0.5, l2: float = 1e-4, seed: int = 20261010) -> "SalienceModel":
        data = list(rows)
        rng = random.Random(seed)
        w: dict[str, float] = {}
        for epoch in range(epochs):
            rng.shuffle(data)
            step = lr / (1.0 + 0.05 * epoch)
            for feats, y in data:
                p = _sigmoid(sum(w.get(k, 0.0) * v for k, v in feats.items()))
                g = p - y
                for k, v in feats.items():
                    w[k] = w.get(k, 0.0) - step * (g * v + l2 * w.get(k, 0.0))
        return cls({k: round(v, 6) for k, v in sorted(w.items())}, {"epochs": epochs, "lr": lr, "l2": l2, "seed": seed, "examples": len(data)})

    def to_json(self) -> dict:
        return {"model": "logistic regression over fact-kind x context features", "kinds": list(KINDS), "context": list(CONTEXT_KEYS), "meta": self.meta, "weights": self.weights}

    def weights_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.weights, sort_keys=True).encode()).hexdigest()

    @classmethod
    def load(cls, path: pathlib.Path = MODEL_FILE) -> "SalienceModel":
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        if data.get("kinds") != list(KINDS) or data.get("context") != list(CONTEXT_KEYS):
            raise ValueError("salience model does not match this code")
        return cls(data["weights"], data.get("meta"))


def rank(model: SalienceModel, facts: list[Fact], ctx: dict[str, float]) -> list[tuple[Fact, float]]:
    """Facts by descending probability; ties broken by the fixed kind order, so the result is a pure function of the inputs."""
    order = {k: i for i, k in enumerate(KINDS)}
    scored = [(f, model.probability(features(f, ctx))) for f in facts]
    return sorted(scored, key=lambda fp: (-round(fp[1], 9), order[fp[0].kind], fp[0].text))
