"""
Averaged structured perceptron with first-order tag transitions and Viterbi
decoding (Collins 2002). numpy only; deterministic for a given seed.

Training: for each sentence, decode with the current weights; if the
predicted tag sequence differs from the gold one, add the gold features /
transitions and subtract the predicted ones. The returned weights are the
average over all training steps (the standard lazy-averaging trick), which
generalises better than the final weights.
"""
from __future__ import annotations

import json

import numpy as np


class StructuredPerceptron:
    def __init__(self, tags: tuple[str, ...]):
        self.tags = tuple(tags)
        self.features: dict[str, int] = {}
        self.W = np.zeros((0, len(self.tags)))
        self.T = np.zeros((len(self.tags) + 1, len(self.tags)))  # row -1 = start

    # --- decoding -------------------------------------------------------------

    def _ids(self, token_features: list[str]) -> np.ndarray:
        return np.array([self.features[f] for f in token_features if f in self.features], dtype=np.int64)

    def _emissions(self, sentence_feature_ids: list[np.ndarray], W: np.ndarray) -> np.ndarray:
        return np.stack([W[ids].sum(axis=0) if len(ids) else np.zeros(len(self.tags))
                         for ids in sentence_feature_ids])

    def _viterbi(self, emissions: np.ndarray, T: np.ndarray) -> list[int]:
        n, k = emissions.shape
        score = T[-1] + emissions[0]
        back = np.zeros((n, k), dtype=np.int64)
        for i in range(1, n):
            cand = score[:, None] + T[:k] + emissions[i][None, :]
            back[i] = cand.argmax(axis=0)
            score = cand.max(axis=0)
        path = [int(score.argmax())]
        for i in range(n - 1, 0, -1):
            path.append(int(back[i][path[-1]]))
        return path[::-1]

    def predict(self, sentence_features: list[list[str]]) -> list[str]:
        if not sentence_features:
            return []
        ids = [self._ids(f) for f in sentence_features]
        return [self.tags[t] for t in self._viterbi(self._emissions(ids, self.W), self.T)]

    # --- training ---------------------------------------------------------------

    def fit(self, data: list[tuple[list[list[str]], list[str]]], epochs: int, seed: int) -> list[dict]:
        """data: [(sentence_features, gold_tags)]. Returns per-epoch training stats."""
        tag_index = {t: i for i, t in enumerate(self.tags)}
        for sentence, _ in data:  # feature vocabulary in first-seen order: deterministic
            for token_features in sentence:
                for f in token_features:
                    self.features.setdefault(f, len(self.features))
        n_tags = len(self.tags)
        W = np.zeros((len(self.features), n_tags))
        T = np.zeros((n_tags + 1, n_tags))
        W_acc, T_acc = np.zeros_like(W), np.zeros_like(T)  # sum of (step * update)
        encoded = [([self._ids(f) for f in sent], [tag_index[t] for t in gold]) for sent, gold in data]
        rng = np.random.default_rng(seed)
        step, history = 1, []
        for epoch in range(epochs):
            mistakes = 0
            for idx in rng.permutation(len(encoded)):
                ids, gold = encoded[idx]
                pred = self._viterbi(self._emissions(ids, W), T)
                if pred != gold:
                    mistakes += 1
                    prev_g = prev_p = n_tags  # start row
                    for i, (g, p) in enumerate(zip(gold, pred)):
                        if g != p:
                            W[ids[i], g] += 1
                            W[ids[i], p] -= 1
                            W_acc[ids[i], g] += step
                            W_acc[ids[i], p] -= step
                        if (prev_g, g) != (prev_p, p):
                            T[prev_g, g] += 1
                            T[prev_p, p] -= 1
                            T_acc[prev_g, g] += step
                            T_acc[prev_p, p] -= step
                        prev_g, prev_p = g, p
                step += 1
            history.append({"epoch": epoch + 1, "sentences_with_mistakes": mistakes})
        self.W = W - W_acc / step
        self.T = T - T_acc / step
        return history

    # --- persistence ------------------------------------------------------------

    def to_json(self) -> dict:
        names = sorted(self.features, key=self.features.get)
        rows = {name: [float(x) for x in self.W[i]] for i, name in enumerate(names) if np.any(self.W[i])}
        return {"tags": list(self.tags), "weights": rows, "transitions": self.T.tolist()}

    @classmethod
    def from_json(cls, data: dict) -> "StructuredPerceptron":
        model = cls(tuple(data["tags"]))
        names = list(data["weights"])
        model.features = {name: i for i, name in enumerate(names)}
        model.W = np.array([data["weights"][n] for n in names]) if names else np.zeros((0, len(model.tags)))
        model.T = np.array(data["transitions"])
        return model

    def weights_sha256(self) -> str:
        import hashlib
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True).encode()).hexdigest()
