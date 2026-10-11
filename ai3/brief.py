"""
AI-3 Consultation Brief: a short, cited, verified summary of one patient's recorded history for the start of a consultation.

    snapshot (read-only, ai2.snapshot) -> candidate facts (deterministic, ai3.facts) -> verification -> salience ranking (learned,
    ai3.salience) -> top facts -> brief

Two arms are built from the same verified facts so they can be compared:
    template   a fixed, standard order (TEMPLATE_ORDER), the deterministic baseline
    learned    the facts ordered by the learned salience model
Both show at most `max_items` facts; neither can show a fact the verifier rejected, and neither states more than the facts say:
no diagnosis, no treatment rationale, no dose advice, no outcome, no inference about unknown values. The sentence shown for each fact
is a deterministic template (there is no generative phrasing), so the AI component is the selection and ordering only.
"""
from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass, field
from typing import Optional

from ai2.snapshot import PatientSnapshot
from ai3.facts import Fact, candidate_facts, fact_supported
from ai3.salience import MODEL_FILE, SalienceModel, context, rank

# SHA-256 of ai3/data/salience_model.json (written by scripts/ai3_train_eval.py train --write); load_model refuses any other file.
MODEL_SHA256: Optional[str] = "4ac9b5a746ef48478bd11d67d3be36dccbda8ffe3e2defb43471fbaa00657126"
MAX_ITEMS = 5
CAVEAT = "Based only on this patient's recorded prescriptions and visits. It reports what was recorded; it is not advice."

# The deterministic baseline order, fixed BEFORE any model was trained: the usual "standard summary" a template would print.
TEMPLATE_ORDER = ("last_prescription", "open_visit", "last_visit", "count_prescriptions", "first_prescription", "count_visits",
                  "herbs_changed", "dosha_changed", "unknown_data", "no_change")


@dataclass(frozen=True)
class BriefItem:
    rank: int
    kind: str
    text: str
    labels: tuple
    exact: bool
    score: Optional[float] = None


@dataclass(frozen=True)
class Brief:
    patient_pk: int
    as_of: str
    arm: str
    items: tuple
    omitted: int                     # verified facts not shown because of the item limit
    rejected: int                    # candidate facts the verifier rejected (always 0 for facts built by ai3.facts; kept visible)
    caveat: str = CAVEAT

    def to_dict(self) -> dict:
        return {"patient_pk": self.patient_pk, "as_of": self.as_of, "arm": self.arm, "caveat": self.caveat, "omitted": self.omitted, "rejected": self.rejected,
                "items": [{"rank": i.rank, "kind": i.kind, "text": i.text, "records": list(i.labels), "exact": i.exact, "score": i.score} for i in self.items]}


def verified_facts(snapshot: PatientSnapshot) -> tuple[list[Fact], int]:
    kept, rejected = [], 0
    for fact in candidate_facts(snapshot):
        ok, _ = fact_supported(snapshot, fact)
        if ok:
            kept.append(fact)
        else:
            rejected += 1
    return kept, rejected


def template_order(facts: list[Fact]) -> list[Fact]:
    position = {k: i for i, k in enumerate(TEMPLATE_ORDER)}
    return sorted(facts, key=lambda f: (position[f.kind], f.text))


def make_brief(snapshot: PatientSnapshot, as_of: str, arm: str = "learned", model: Optional[SalienceModel] = None, max_items: int = MAX_ITEMS) -> Brief:
    """`as_of` is a date (YYYY-MM-DD) supplied by the caller, so the result is a pure function of (snapshot, as_of, model)."""
    if arm not in ("template", "learned"):
        raise ValueError("arm must be 'template' or 'learned'")
    facts, rejected = verified_facts(snapshot)
    if arm == "template":
        ordered = [(f, None) for f in template_order(facts)]
    else:
        model = model or load_model()
        ordered = rank(model, facts, context(snapshot, as_of, facts))
    shown = ordered[:max_items]
    items = tuple(BriefItem(i + 1, f.kind, f.text, f.labels, f.exact, None if s is None else round(s, 4)) for i, (f, s) in enumerate(shown))
    return Brief(snapshot.patient_pk, as_of, arm, items, len(ordered) - len(shown), rejected)


def load_model(path: pathlib.Path = MODEL_FILE) -> SalienceModel:
    path = pathlib.Path(path)
    if MODEL_SHA256 is not None and hashlib.sha256(path.read_bytes()).hexdigest() != MODEL_SHA256:
        raise RuntimeError(f"salience model {path.name} is not the evaluated one")
    return SalienceModel.load(path)
