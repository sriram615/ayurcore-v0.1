"""
Local trained question planner for AI-2: question -> structured plan (no LLM, no paid API).

    plan = Planner.load(path).plan(question)
      {"kind": "query",   "query": {...closed vocabulary...}, "frame": F, "slots": {...}}
      {"kind": "abstain", "reason": DIAGNOSIS | TREATMENT | UNAVAILABLE | AMBIGUOUS, "frame": F, "slots": {...}}

The planner NEVER answers: it does not import or call the executor, the snapshot or any
patient data. The deterministic executor answers a planned query and the verifier checks it.

How it works:
  1. delexicalise (deterministic): herb names -> HERB (backend.herb_resolver aliases, 1-3 word
     n-grams, English / Manglish / Malayalam), dosha values -> DOSHA (closed lexicon; "Vata-Pitta",
     "vata and pitta" become one combined value), numbers and number words -> NUM
  2. classify (learned, factored): three multiclass averaged perceptrons over word unigrams /
     bigrams and character 3-4-grams of the delexicalised question score the OPERATION (or an
     ABSTAIN class), the TARGET and the SELECTOR (previous / first, for diff and compare). Each
     factor learns from every frame that shares it ("first" from all first-frames, "visit" from all
     encounter frames), so unseen combinations can still be planned. Decoding: the best operation;
     if it is an ABSTAIN class the plan abstains, else the best target that the closed vocabulary
     allows for that operation, then the selector. The "last N" variant of a frame is chosen
     deterministically by the presence of a number.
  3. fill slots (deterministic): the frame's herb / dosha / last_n come from step 1. Every
     content slot in the question must be used by the frame, and every slot the frame needs must
     be present exactly once -- otherwise the plan is ABSTAIN / AMBIGUOUS. A plan is never
     completed by guessing a value.
"""
from __future__ import annotations

import hashlib
import json
import random
import re

from backend.herb_resolver import HERB_ALIASES

FRAMES = {
    "FIRST_RX": ("first", "prescription", None, None, ""), "LAST_RX": ("last", "prescription", None, None, ""),
    "FIRST_HERB": ("first", "herb", None, None, "h"), "LAST_HERB": ("last", "herb", None, None, "h"),
    "FIRST_DOSHA": ("first", "dosha", None, None, ""), "LAST_DOSHA": ("last", "dosha", None, None, ""),
    "FIRST_ENC": ("first", "encounter", None, None, ""), "LAST_ENC": ("last", "encounter", None, None, ""),
    "COUNT_RX": ("count", "prescription", None, None, ""), "COUNT_RX_N": ("count", "prescription", None, None, "n"),
    "COUNT_ENC": ("count", "encounter", None, None, ""), "COUNT_HERB": ("count", "herb", None, None, "h"),
    "COUNT_HERB_N": ("count", "herb", None, None, "hn"), "COUNT_DOSHA": ("count", "dosha", None, None, "d"),
    "SUM_HERB": ("sum", "herb", None, None, "h"), "SUM_HERB_N": ("sum", "herb", None, None, "hn"),
    "LIST_RX": ("list", "prescription", None, None, ""), "LIST_ENC": ("list", "encounter", None, None, ""),
    "LIST_HERB": ("list", "herb", None, None, ""), "LIST_HERB_N": ("list", "herb", None, None, "n"),
    "LIST_DOSHA": ("list", "dosha", None, None, ""),
    "DIFF_PREV": ("diff", "prescription", "previous", "last", ""), "DIFF_FIRST": ("diff", "prescription", "first", "last", ""),
    "CMP_HERB_PREV": ("compare", "herb", "previous", "last", "h"), "CMP_HERB_FIRST": ("compare", "herb", "first", "last", "h"),
    "CMP_DOSHA_PREV": ("compare", "dosha", "previous", "last", ""), "CMP_DOSHA_FIRST": ("compare", "dosha", "first", "last", ""),
}
ABSTAIN = ("DIAGNOSIS", "TREATMENT", "UNAVAILABLE", "AMBIGUOUS")
LABELS = tuple(FRAMES) + ABSTAIN
OPS = ("first", "last", "count", "sum", "list", "diff", "compare") + ABSTAIN
TARGETS = ("prescription", "herb", "dosha", "encounter")
SELECTORS = ("previous", "first")
FACTORS = {"op": OPS, "target": TARGETS, "selector": SELECTORS}
# (op, target, selector, with_n) -> frame
FRAME_OF = {(op, t, a or "", "n" in needs): f for f, (op, t, a, b, needs) in FRAMES.items()}


def factor_labels(frame: str) -> dict:
    """Gold factor labels of a frame (selector only for diff / compare)."""
    if frame in ABSTAIN:
        return {"op": frame}
    op, target, a, _, _ = FRAMES[frame]
    labels = {"op": op, "target": target}
    if a:
        labels["selector"] = a
    return labels

TOKEN = re.compile(r"[ഀ-ൿ‌‍]+|[a-z]+|\d+|\S")
DOSHA_WORDS = {"vata": "Vata", "vaata": "Vata", "vatha": "Vata", "vatham": "Vata", "വാതം": "Vata",
               "pitta": "Pitta", "pitha": "Pitta", "pitham": "Pitta", "pittam": "Pitta", "പിത്തം": "Pitta",
               "kapha": "Kapha", "kafa": "Kapha", "kapham": "Kapha", "കഫം": "Kapha"}
DOSHA_ORDER = ("Vata", "Pitta", "Kapha")
DOSHA_JOINERS = frozenset({"-", "/", "and", "um"})
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "onnu": 1, "randu": 2, "moonu": 3, "naalu": 4, "anchu": 5, "aaru": 6}
MAX_HERB_TOKENS = 3


def tokenize(question: str) -> list[str]:
    return TOKEN.findall(question.lower())


def delexicalise(question: str) -> tuple[list[str], dict]:
    """(tokens with HERB / DOSHA / NUM placeholders, slots {herbs, doshas, numbers})."""
    tokens, out, slots = tokenize(question), [], {"herbs": [], "doshas": [], "numbers": []}
    i = 0
    while i < len(tokens):
        herb = None
        for j in range(min(len(tokens), i + MAX_HERB_TOKENS), i, -1):
            name = " ".join(tokens[i:j])
            if name in HERB_ALIASES and not name.isdigit():
                herb, i_next = HERB_ALIASES[name], j
                break
        if herb:
            out.append("HERB")
            slots["herbs"].append(herb)
            i = i_next
            continue
        t = tokens[i]
        if t in DOSHA_WORDS:
            found, j = {DOSHA_WORDS[t]}, i + 1
            while j + 1 < len(tokens) and tokens[j] in DOSHA_JOINERS and tokens[j + 1] in DOSHA_WORDS:
                found.add(DOSHA_WORDS[tokens[j + 1]])
                j += 2
            out.append("DOSHA")
            slots["doshas"].append("-".join(d for d in DOSHA_ORDER if d in found))
            i = j
            continue
        if t.isdigit() or t in NUMBER_WORDS:
            out.append("NUM")
            slots["numbers"].append(int(t) if t.isdigit() else NUMBER_WORDS[t])
            i += 1
            continue
        out.append(t)
        i += 1
    return out, slots


def features(tokens: list[str]) -> list[str]:
    words = [t for t in tokens if any(ch.isalnum() for ch in t) or t in ("HERB", "DOSHA", "NUM")]
    feats = ["bias"] + [f"w={w}" for w in words]
    feats += [f"b={a}_{b}" for a, b in zip(["<s>"] + words, words + ["</s>"])]
    for w in words:
        if w in ("HERB", "DOSHA", "NUM"):
            continue
        padded = f"^{w}$"
        feats += [f"c{n}={padded[k:k + n]}" for n in (3, 4) for k in range(len(padded) - n + 1)]
    for slot in ("HERB", "DOSHA", "NUM"):
        feats.append(f"n_{slot}={min(tokens.count(slot), 2)}")
    return feats


def _train_factor(data: list[tuple[list[str], str]], labels: tuple, epochs: int, seed: int) -> dict:
    """Multiclass averaged perceptron; deterministic for a given seed. Returns {feature: {label: w}}."""
    w, total, stamp, step = {}, {}, {}, 0
    rng = random.Random(seed)
    order = list(range(len(data)))

    def bump(f, label, delta):
        key = (f, label)
        total[key] = total.get(key, 0.0) + (step - stamp.get(key, 0)) * w.get(key, 0.0)
        stamp[key] = step
        w[key] = w.get(key, 0.0) + delta

    for _ in range(epochs):
        rng.shuffle(order)
        for idx in order:
            feats, gold = data[idx]
            scores = {label: 0.0 for label in labels}
            for f in feats:
                for label in labels:
                    scores[label] += w.get((f, label), 0.0)
            guess = max(labels, key=lambda label: (scores[label], -labels.index(label)))
            step += 1
            if guess != gold:
                for f in feats:
                    bump(f, gold, 1.0)
                    bump(f, guess, -1.0)
    averaged = {}
    for key, value in w.items():
        avg = (total.get(key, 0.0) + (step - stamp.get(key, 0)) * value) / max(step, 1)
        if avg:
            averaged.setdefault(key[0], {})[key[1]] = round(avg, 6)
    return averaged


class Planner:
    def __init__(self, weights: dict | None = None, meta: dict | None = None):
        self.weights = weights or {name: {} for name in FACTORS}  # factor -> feature -> {label: weight}
        self.meta = meta or {}

    # ---- learning ---------------------------------------------------------------------------------
    @classmethod
    def train(cls, examples: list[tuple[str, str]], epochs: int, seed: int = 0) -> "Planner":
        """examples: (question, gold frame or ABSTAIN class). Each factor is trained on the examples
        that carry it (selector: diff / compare frames only)."""
        feats = [(features(delexicalise(q)[0]), factor_labels(frame)) for q, frame in examples]
        weights = {}
        for name, labels in FACTORS.items():
            data = [(f, gold[name]) for f, gold in feats if name in gold]
            weights[name] = _train_factor(data, labels, epochs, seed)
        return cls(weights, {"epochs": epochs, "seed": seed, "examples": len(examples)})

    def _scores(self, name: str, feats: list[str]) -> dict:
        labels = FACTORS[name]
        scores = {label: 0.0 for label in labels}
        table = self.weights.get(name, {})
        for f in feats:
            for label, value in table.get(f, {}).items():
                scores[label] += value
        return scores

    def classify(self, tokens: list[str]) -> str:
        """Frame (or ABSTAIN class) by factored decoding over the closed vocabulary."""
        feats = features(tokens)
        best = lambda scores, allowed: max(allowed, key=lambda label: (scores[label], -allowed.index(label)))
        op = best(self._scores("op", feats), OPS)
        if op in ABSTAIN:
            return op
        with_n = "NUM" in tokens
        targets = tuple(t for t in TARGETS if any((op, t, sel, n) in FRAME_OF for sel in ("", *SELECTORS) for n in (False, True)))
        target = best(self._scores("target", feats), targets)
        selector = ""
        if op in ("diff", "compare"):
            selector = best(self._scores("selector", feats), SELECTORS)
        return FRAME_OF.get((op, target, selector, with_n)) or FRAME_OF[(op, target, selector, False)]

    # ---- planning (never answering) ---------------------------------------------------------------
    def plan(self, question: str) -> dict:
        if not isinstance(question, str) or not question.strip():
            return {"kind": "abstain", "reason": "AMBIGUOUS", "frame": None, "slots": {}}
        tokens, slots = delexicalise(question)
        frame = self.classify(tokens)
        if frame in ABSTAIN:
            return {"kind": "abstain", "reason": frame, "frame": frame, "slots": slots}
        op, target, a, b, needs = FRAMES[frame]
        herbs, doshas, numbers = slots["herbs"], slots["doshas"], slots["numbers"]
        want = {"h": len(herbs), "d": len(doshas), "n": len(numbers)}
        if any(want[s] != (1 if s in needs else 0) for s in "hdn"):  # missing, extra or unused content
            return {"kind": "abstain", "reason": "AMBIGUOUS", "frame": frame, "slots": slots}
        query = {"op": op, "target": target, "herb": herbs[0] if "h" in needs else None,
                 "dosha": doshas[0] if "d" in needs else None, "last_n": numbers[0] if "n" in needs else None,
                 "a": a or "previous", "b": b or "last"}
        return {"kind": "query", "query": query, "frame": frame, "slots": slots}

    # ---- persistence ------------------------------------------------------------------------------
    def to_json(self) -> dict:
        return {"model": "factored multiclass averaged perceptrons (op / target / selector)", "labels": list(LABELS),
                "factors": {k: list(v) for k, v in FACTORS.items()}, "meta": self.meta,
                "weights": {name: {f: dict(sorted(v.items())) for f, v in sorted(table.items())}
                            for name, table in self.weights.items()}}

    def weights_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_json()["weights"], sort_keys=True).encode()).hexdigest()

    def save(self, path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def load(cls, path) -> "Planner":
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("labels") != list(LABELS) or data.get("factors") != {k: list(v) for k, v in FACTORS.items()}:
            raise ValueError("planner model labels do not match this code")
        return cls(data["weights"], data.get("meta"))
