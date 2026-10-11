"""
The extractor pipeline: T1 tagger (trained) -> assemble (deterministic).

Kept as thin glue so the two halves stay separate: extractor/tagger.py and
features.py hold everything learned; units.py, normalize.py and assemble.py
hold everything deterministic and import nothing from the tagger side.
"""
from __future__ import annotations

from backend.schemas import ClinicalParse

from extractor.assemble import assemble
from extractor.features import sentence_features
from extractor.labels import tags_to_spans, tokenize
from extractor.tagger import StructuredPerceptron
from extractor.verify import verify


class Extractor:
    def __init__(self, model: StructuredPerceptron):
        self.model = model

    def spans(self, note: str) -> list[tuple[int, int, str]]:
        tokens = tokenize(note)
        return tags_to_spans(tokens, self.model.predict(sentence_features(tokens)))

    def parse(self, note: str) -> tuple[ClinicalParse, dict]:
        spans = self.spans(note)
        parsed, report = assemble(note, spans)
        report["spans"] = [{"start": s, "end": e, "type": t, "text": note[s:e]} for s, e, t in spans]
        return parsed, report

    def extract(self, note: str) -> dict:
        """parse + T3 verification. The parse is returned exactly as produced;
        a "rejected" verdict means the caller must abstain from using it."""
        spans = self.spans(note)
        parsed, report = assemble(note, spans)
        verification = verify(note, spans, parsed, report)
        return {"parse": parsed, "report": report, "verdict": verification["verdict"],
                "violations": verification["violations"], "abstain": verification["verdict"] != "verified"}
