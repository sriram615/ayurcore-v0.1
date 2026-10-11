"""
Learned cue tagger for AI-2 v2 (Block 5B): labels words that NO lexicon knows with an existing cue category,
so the unchanged deterministic alignment can use them. Local, deterministic; no patient data.

    tagger = CueTagger.train(sentences, epochs, seed)      sentences: training question texts
    tagger.fills(text, tiers) -> {token index: (category, tier, margin)}

Labels: the alignment cue categories (memory.interpret.CUES) plus NEUTRAL.
Training targets come ONLY from the deterministic lexicons (a known cue word -> its category, a neutral word
-> NEUTRAL); words of unknown, slot, punctuation or unsupported role are never targets. Each target token is
seen three ways -- full (word identity + letters + context), letters only (+ context), context only -- so the
model must rely on spelling and context and can generalise to unseen words (word dropout).
Features: the word, its 2-4 letter prefixes / suffixes, script, length, the two words on each side (numbers,
herbs and doshas abstracted) and their lexicon categories.
Model: the planner's multiclass averaged perceptron (memory.planner._train_factor).

GATE (what the tagger may touch, enforced in fills()):
  - only a word whose coverage role is UNKNOWN AND that no authoritative vocabulary knows (memory.morph's
    lexicon: cue / neutral / unsupported / screen / reference / herb / dosha / number words and stems);
  - never the first word of a clause (the imperative-verb position: "Cut the Neem dose.");
  - never a NEUTRAL prediction (a learned "this word does not matter" could hide a meaning-changing word);
  - margin >= tiers["medium"]; tier "high" if margin >= tiers["high"].
In memory.v2 a HIGH fill makes the word explained (an answer becomes possible if alignment is consistent); a
MEDIUM fill feeds alignment but leaves the word unexplained, so the result can only be a clarification.
It runs after the scope screen and the reference check, so it can never override their refusals.
"""
from __future__ import annotations

import hashlib
import json
import random

from backend.herb_resolver import HERB_ALIASES
from memory.coverage import coverage
from memory.interpret import CUES, _find
from memory.morph import _known
from memory.planner import DOSHA_WORDS, NUMBER_WORDS, _train_factor, tokenize

LABELS = tuple(CUES) + ("NEUTRAL",)
CLAUSE_BREAK = {".", ",", ";", ":", "!", "?"}


def _is_word(t: str) -> bool:
    return any(ch.isalnum() for ch in t)


def _abstract(t: str) -> str:
    if t.isdigit() or t in NUMBER_WORDS:
        return "NUM"
    if t in HERB_ALIASES:
        return "HERB"
    if t in DOSHA_WORDS:
        return "DOSHA"
    return t


def lexicon_categories(tokens: list[str]) -> list:
    """Per token: the deterministic category (a CUES category or NEUTRAL) or None."""
    cats = [None] * len(tokens)
    for cat, idx in _find(tokens).items():
        for i in idx:
            if cats[i] is None:
                cats[i] = cat
    roles = coverage(" ".join(tokens))["roles"] if tokens else []
    if len(roles) == len(tokens):
        for i, r in enumerate(roles):
            if cats[i] is None and r in ("neutral", "context"):
                cats[i] = "NEUTRAL"
            if r in ("slot", "UNSUPPORTED", "punct"):
                cats[i] = None
    return cats


def _clause_initial(tokens: list[str], i: int) -> bool:
    j = i - 1
    while j >= 0 and not _is_word(tokens[j]) and tokens[j] not in CLAUSE_BREAK:
        j -= 1
    return j < 0 or tokens[j] in CLAUSE_BREAK


def token_features(tokens: list[str], i: int, cats: list, mode: str = "full") -> list[str]:
    t = tokens[i]
    f = ["bias", "script=" + ("ml" if any("ഀ" <= ch <= "ൿ" for ch in t) else "lat"),
         "len=" + str(min(len(t) // 3, 4)), "initial=" + str(_clause_initial(tokens, i))]
    if mode == "full":
        f.append("w=" + t)
    if mode in ("full", "letters"):
        for n in (2, 3, 4):
            if len(t) > n:
                f += [f"p{n}={t[:n]}", f"s{n}={t[-n:]}"]
    for d in (-2, -1, 1, 2):
        j = i + d
        w = _abstract(tokens[j]) if 0 <= j < len(tokens) else ("<s>" if j < 0 else "</s>")
        c = (cats[j] or "-") if 0 <= j < len(tokens) else "-"
        f += [f"w{d}={w}", f"c{d}={c}"]
    left = _abstract(tokens[i - 1]) if i else "<s>"
    right = _abstract(tokens[i + 1]) if i + 1 < len(tokens) else "</s>"
    f.append(f"lr={left}|{right}")
    return f


def training_examples(sentences: list[str], seed: int = 0) -> list[tuple[list[str], str]]:
    examples = []
    for text in sentences:
        tokens = tokenize(text)
        cats = lexicon_categories(tokens)
        for i, cat in enumerate(cats):
            if cat is None or not _is_word(tokens[i]):
                continue
            for mode in ("full", "letters", "context"):
                examples.append((token_features(tokens, i, cats, mode), cat))
    random.Random(seed).shuffle(examples)
    return examples


class CueTagger:
    def __init__(self, weights: dict | None = None, meta: dict | None = None):
        self.weights = weights or {}
        self.meta = meta or {}

    @classmethod
    def train(cls, sentences: list[str], epochs: int = 5, seed: int = 0) -> "CueTagger":
        examples = training_examples(sentences, seed)
        weights = _train_factor(examples, LABELS, epochs, seed)
        return cls(weights, {"epochs": epochs, "seed": seed, "sentences": len(sentences), "examples": len(examples)})

    def scores(self, feats: list[str]) -> dict:
        s = {label: 0.0 for label in LABELS}
        for f in feats:
            for label, w in self.weights.get(f, {}).items():
                s[label] += w
        return s

    def predict(self, tokens: list[str], i: int, cats: list | None = None) -> tuple[str, float]:
        cats = cats if cats is not None else lexicon_categories(tokens)
        s = self.scores(token_features(tokens, i, cats, "full"))
        ranked = sorted(LABELS, key=lambda label: (-s[label], LABELS.index(label)))
        return ranked[0], round(s[ranked[0]] - s[ranked[1]], 6)

    def fills(self, text: str, tiers: dict) -> dict:
        tokens = tokenize(text)
        roles = coverage(text)["roles"]
        cats = lexicon_categories(tokens)
        out = {}
        for i, t in enumerate(tokens):
            if roles[i] != "UNKNOWN" or _known(t) or not _is_word(t) or len(t) < 3 or _clause_initial(tokens, i):
                continue
            label, margin = self.predict(tokens, i, cats)
            if label == "NEUTRAL" or margin < tiers["medium"]:
                continue
            out[i] = (label, "high" if margin >= tiers["high"] else "medium", margin)
        return out

    # ---- persistence ------------------------------------------------------------------------------
    def to_json(self) -> dict:
        return {"model": "cue tagger: multiclass averaged perceptron", "labels": list(LABELS), "meta": self.meta,
                "weights": {f: dict(sorted(v.items())) for f, v in sorted(self.weights.items())}}

    def weights_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.to_json()["weights"], sort_keys=True).encode()).hexdigest()

    def save(self, path) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def load(cls, path) -> "CueTagger":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("labels") != list(LABELS):
            raise ValueError("cue tagger labels do not match this code")
        return cls(data["weights"], data.get("meta"))
