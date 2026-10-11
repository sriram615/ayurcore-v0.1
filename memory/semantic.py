"""
AI-2 v3 experimental semantic interpreter (evaluation module, not production; OFF unless an arm wraps a planner).

    encoder = E5Encoder.load(MODEL_DIR)                     frozen intfloat/multilingual-e5-small, pure numpy
    head = FrameHead.train(vectors, labels, l2)             multinomial logistic regression on the embedding
    sem = SemanticInterpreter(encoder, head)
    sem.predict(text) -> {"label", "probability", "ranked"}  reads the question text ONLY
    SemanticArm(base, sem, tiers).interpret(question)        base: any planner with .interpret (Block 2A, ...)

Labels: the existing frames (memory.interpret.FRAMES) and abstain classes (ABSTAIN_*). The model never produces
slot values: a predicted frame becomes a plan only through the base pipeline's own slot-compatible candidate for
that frame (deterministic herb / dosha / number extraction, memory.interpret._query).

SemanticArm decision (fixed before any held-out run; tiers are chosen on tuning sets only):
  - base outcome UNCHANGED when: refused at normalize / screen / reference / coverage (UNSUPPORTED) / detector;
    refused by the planner (DIAGNOSIS / TREATMENT / UNAVAILABLE); an UNDERSPECIFIED clarification (incl. C10);
    the semantic label is an ABSTAIN_* class, or a frame with no slot-compatible candidate; or the probability
    is below tiers["medium"]  (semantic abstention -> the existing clarification / abstention path).
  - base PLAN, semantic frame differs with probability >= tiers["high"] -> CLARIFY between the two readings
    (the semantic model can never replace an answer, only withhold it).
  - base clarify / abstain(alignment):
        probability >= tiers["high"]   -> plan of that frame (then executed by the UNCHANGED executor/verifier)
        probability >= tiers["medium"] -> abstain(alignment) becomes CLARIFY with the semantic options;
                                          an existing clarification is left as it is.
No patient data, no retrieval, no execution here: the arm returns an interpretation; memory.v2 executes plans.
SYNTHETIC; NOT CLINICAL VALIDATION.
"""
from __future__ import annotations

import hashlib
import json
import math
import pathlib
import struct
import unicodedata

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "artifacts" / "models" / "pretrained" / "multilingual-e5-small"
MODEL_COMMIT = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_SHA256 = {"model.safetensors": "1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477",
                "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
                "config.json": "69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959"}
PREFIX = "query: "  # e5 convention for a query-side input
MAX_TOKENS = 128
MAX_OPTIONS = 3
PROTECTED_STAGES = ("normalize", "screen", "reference", "coverage", "detector")
CLINICAL = ("DIAGNOSIS", "TREATMENT", "UNAVAILABLE")


# ---- encoder: safetensors + XLM-R unigram tokenizer + BERT forward pass, numpy only ------------------------
def load_safetensors(path) -> dict:
    raw = pathlib.Path(path).read_bytes()
    n = struct.unpack("<Q", raw[:8])[0]
    head = json.loads(raw[8:8 + n])
    out = {}
    for k, v in head.items():
        if k == "__metadata__":
            continue
        if k.endswith("position_ids"):  # buffer, not a weight
            continue
        if v["dtype"] != "F32":
            raise ValueError(f"unexpected dtype {v['dtype']} for {k}")
        a, b = v["data_offsets"]
        out[k] = np.frombuffer(raw, dtype=np.float32, count=(b - a) // 4, offset=8 + n + a).reshape(v["shape"])
    return out


class UnigramTokenizer:
    """tokenizer.json Unigram model: NFKC (approximates the precompiled nmt_nfkc map), runs of spaces collapsed,
    Metaspace pre-tokenizer, Viterbi segmentation, <s> ... </s>; unknown characters fused into one <unk>."""

    def __init__(self, spec: dict):
        model = spec["model"]
        self.pieces = {p: (i, s) for i, (p, s) in enumerate(model["vocab"])}
        self.unk_id = model["unk_id"]
        self.unk_score = min(s for _, s in model["vocab"]) - 10.0
        self.max_len = max(len(p) for p in self.pieces)
        self.bos, self.eos = self.pieces["<s>"][0], self.pieces["</s>"][0]

    def _word(self, w: str) -> list[int]:
        n = len(w)
        best = [(-math.inf, -1, None)] * (n + 1)  # (score, start, id)
        best[0] = (0.0, -1, None)
        for end in range(1, n + 1):
            for start in range(max(0, end - self.max_len), end):
                if best[start][0] == -math.inf:
                    continue
                hit = self.pieces.get(w[start:end])
                if hit is not None:
                    cand = (best[start][0] + hit[1], start, hit[0])
                elif end - start == 1:
                    cand = (best[start][0] + self.unk_score, start, self.unk_id)
                else:
                    continue
                if cand[0] > best[end][0]:
                    best[end] = cand
        ids, i = [], n
        while i > 0:
            ids.append(best[i][2])
            i = best[i][1]
        ids.reverse()
        fused = []
        for t in ids:
            if not (t == self.unk_id and fused and fused[-1] == self.unk_id):
                fused.append(t)
        return fused

    def encode(self, text: str) -> list[int]:
        text = " ".join(unicodedata.normalize("NFKC", text).split(" "))
        while "  " in text:
            text = text.replace("  ", " ")
        text = "▁" + text.replace(" ", "▁")
        words, cur = [], ""
        for ch in text:
            if ch == "▁" and cur:
                words.append(cur)
                cur = ""
            cur += ch
        if cur:
            words.append(cur)
        ids = [i for w in words for i in self._word(w)]
        return [self.bos] + ids[: MAX_TOKENS - 2] + [self.eos]


def _erf(x: np.ndarray) -> np.ndarray:
    """Abramowitz-Stegun 7.1.26 (|error| < 1.5e-7)."""
    s = np.sign(x)
    a = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-a * a)
    return s * y


def _ln(x, g, b, eps):
    m = x.mean(-1, keepdims=True)
    v = ((x - m) ** 2).mean(-1, keepdims=True)
    return (x - m) / np.sqrt(v + eps) * g + b


class E5Encoder:
    def __init__(self, weights: dict, config: dict, tokenizer: UnigramTokenizer):
        self.w, self.cfg, self.tok = weights, config, tokenizer

    @classmethod
    def load(cls, model_dir=MODEL_DIR, verify: bool = True) -> "E5Encoder":
        model_dir = pathlib.Path(model_dir)
        if verify:
            for name, digest in MODEL_SHA256.items():
                if hashlib.sha256((model_dir / name).read_bytes()).hexdigest() != digest:
                    raise RuntimeError(f"{name} does not match the pinned sha256")
        weights = load_safetensors(model_dir / "model.safetensors")
        config = json.loads((model_dir / "config.json").read_text())
        tok = UnigramTokenizer(json.loads((model_dir / "tokenizer.json").read_text(encoding="utf-8")))
        return cls(weights, config, tok)

    def encode(self, text: str) -> np.ndarray:
        ids = np.array(self.tok.encode(PREFIX + text))
        w, cfg, H = self.w, self.cfg, self.cfg["num_attention_heads"]
        eps = cfg["layer_norm_eps"]
        x = w["embeddings.word_embeddings.weight"][ids] + w["embeddings.position_embeddings.weight"][: len(ids)] \
            + w["embeddings.token_type_embeddings.weight"][0]
        x = _ln(x, w["embeddings.LayerNorm.weight"], w["embeddings.LayerNorm.bias"], eps)
        dh = x.shape[1] // H
        for i in range(cfg["num_hidden_layers"]):
            p = f"encoder.layer.{i}."
            q, k, v = (x @ w[p + f"attention.self.{n}.weight"].T + w[p + f"attention.self.{n}.bias"]
                       for n in ("query", "key", "value"))
            q, k, v = (t.reshape(-1, H, dh).transpose(1, 0, 2) for t in (q, k, v))
            a = q @ k.transpose(0, 2, 1) / np.sqrt(dh)
            a = np.exp(a - a.max(-1, keepdims=True))
            a /= a.sum(-1, keepdims=True)
            c = (a @ v).transpose(1, 0, 2).reshape(len(ids), -1)
            c = c @ w[p + "attention.output.dense.weight"].T + w[p + "attention.output.dense.bias"]
            x = _ln(x + c, w[p + "attention.output.LayerNorm.weight"], w[p + "attention.output.LayerNorm.bias"], eps)
            h = x @ w[p + "intermediate.dense.weight"].T + w[p + "intermediate.dense.bias"]
            h = 0.5 * h * (1.0 + _erf(h / np.sqrt(2.0)))
            h = h @ w[p + "output.dense.weight"].T + w[p + "output.dense.bias"]
            x = _ln(x + h, w[p + "output.LayerNorm.weight"], w[p + "output.LayerNorm.bias"], eps)
        e = x.mean(0)
        return (e / np.linalg.norm(e)).astype(np.float32)


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CachedEncoder:
    """Embeddings looked up by text hash (precomputed by the evaluator); falls back to an encoder if given."""

    def __init__(self, table: dict, encoder: E5Encoder | None = None):
        self.table, self.encoder = table, encoder

    def encode(self, text: str) -> np.ndarray:
        key = text_key(text)
        if key not in self.table:
            if self.encoder is None:
                raise KeyError(f"no cached embedding for {text!r}")
            self.table[key] = self.encoder.encode(text)
        return self.table[key]


# ---- classifier head ----------------------------------------------------------------------------------------
class FrameHead:
    def __init__(self, labels: list[str], W: np.ndarray, b: np.ndarray, meta: dict | None = None,
                 mu: np.ndarray | None = None, sd: np.ndarray | None = None):
        self.labels, self.W, self.b, self.meta = list(labels), W, b, meta or {}
        self.mu = mu if mu is not None else np.zeros(W.shape[0])
        self.sd = sd if sd is not None else np.ones(W.shape[0])

    @classmethod
    def train(cls, X: np.ndarray, y: list[str], l2: float, steps: int = 2000, lr: float = 0.5) -> "FrameHead":
        """Features standardised with TRAINING statistics (e5 vectors share one narrow cone); full-batch gradient
        descent on the L2-regularised softmax loss; deterministic (zero init)."""
        labels = sorted(set(y))
        Y = np.zeros((len(y), len(labels)), dtype=np.float64)
        for i, lab in enumerate(y):
            Y[i, labels.index(lab)] = 1.0
        X = X.astype(np.float64)
        mu, sd = X.mean(0), X.std(0) + 1e-6
        X = (X - mu) / sd
        W, b = np.zeros((X.shape[1], len(labels))), np.zeros(len(labels))
        for _ in range(steps):
            z = X @ W + b
            z -= z.max(1, keepdims=True)
            p = np.exp(z)
            p /= p.sum(1, keepdims=True)
            g = (p - Y) / len(y)
            W -= lr * (X.T @ g + l2 * W)
            b -= lr * g.sum(0)
        return cls(labels, W, b, {"l2": l2, "steps": steps, "lr": lr, "examples": len(y)}, mu, sd)

    def proba(self, x: np.ndarray) -> np.ndarray:
        z = ((x.astype(np.float64) - self.mu) / self.sd) @ self.W + self.b
        z = np.exp(z - z.max())
        return z / z.sum()

    def sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_json(), sort_keys=True).encode()).hexdigest()

    def to_json(self) -> dict:
        return {"labels": self.labels, "W": np.round(self.W, 8).tolist(), "b": np.round(self.b, 8).tolist(),
                "mu": np.round(self.mu, 8).tolist(), "sd": np.round(self.sd, 8).tolist(), "meta": self.meta}

    def save(self, path) -> None:
        pathlib.Path(path).write_text(json.dumps(self.to_json()))

    @classmethod
    def load(cls, path) -> "FrameHead":
        d = json.loads(pathlib.Path(path).read_text())
        return cls(d["labels"], np.array(d["W"]), np.array(d["b"]), d.get("meta"), np.array(d["mu"]), np.array(d["sd"]))


class SemanticInterpreter:
    """Text in, label distribution out. Holds no patient data and no planner / executor / snapshot."""

    def __init__(self, encoder, head: FrameHead):
        self.encoder, self.head = encoder, head

    def predict(self, text: str) -> dict:
        p = self.head.proba(self.encoder.encode(text))
        order = np.argsort(-p, kind="stable")
        ranked = [(self.head.labels[i], round(float(p[i]), 6)) for i in order]
        return {"label": ranked[0][0], "probability": ranked[0][1], "ranked": ranked[:5]}


# ---- the bounded arm --------------------------------------------------------------------------------------
def _protected(out: dict) -> bool:
    if out["outcome"] not in ("plan", "clarify", "abstain"):
        return True
    if out["stage"] in PROTECTED_STAGES:
        return True
    if out["outcome"] == "abstain" and (out["stage"] == "planner" or out["reason"] in CLINICAL):
        return True
    return out["outcome"] == "clarify" and out["reason"] == "UNDERSPECIFIED"


class SemanticArm:
    def __init__(self, base, semantic: SemanticInterpreter, tiers: dict):
        if not tiers or tiers["medium"] > tiers["high"]:
            raise ValueError("tiers need medium <= high")
        self.base, self.semantic, self.tiers = base, semantic, tiers

    def interpret(self, question) -> dict:
        out = self.base.interpret(question)
        if _protected(out) or not isinstance(question, str):
            return out
        from memory.v2 import describe
        cands = {c["frame"]: c["query"] for c in (out.get("interpretation") or {}).get("candidates") or []}
        pred = self.semantic.predict(question)
        out = {**out, "semantic": pred}
        label, p = pred["label"], pred["probability"]
        if label not in cands or p < self.tiers["medium"]:
            return out  # semantic abstention: the existing path decides
        option = lambda f: {"query": cands[f], "frame": f, "text": describe(cands[f]), "probability": None}
        if out["outcome"] == "plan":
            if label != out["plan"]["frame"] and p >= self.tiers["high"]:
                return {**out, "outcome": "clarify", "stage": "semantic", "reason": "SEMANTIC_DISAGREES",
                        "plan": None, "options": [option(out["plan"]["frame"]), option(label)]}
            return out

        if out["outcome"] == "abstain":
            frames = [f for f, q in pred["ranked"] if f in cands and q >= self.tiers["medium"]][:MAX_OPTIONS]
            return {**out, "outcome": "clarify", "stage": "semantic", "reason": "SEMANTIC_CONFIRM",
                    "options": [option(f) for f in frames]}
        return out
