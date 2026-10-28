"""
Result model for the Phase 3A guardrail engine: outcomes, reason codes and
the immutable result records the engine returns.

Everything here is plain data -- no I/O, no clock, no randomness -- so the
same candidate evaluated under the same rule set always produces an equal
EvaluationResult (see backend/guardrails/engine.py).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


class Outcome(str, Enum):
    """A rule's verdict. Ordered by severity: REJECT > REVIEW > PASS.

    Phase 3A is evaluation-only: no outcome blocks or changes anything.
    What REJECT or REVIEW will *do* at preview/confirm is Phase 3B, and needs
    approval (documentation/docs/PHASE_3_PLAN.md §4, §5)."""
    PASS = "PASS"
    REVIEW = "REVIEW"
    REJECT = "REJECT"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]


# The tables below are read-only (Phase 3A B5): they are part of the engine
# fingerprint, so they must not change after import.
_SEVERITY = MappingProxyType({Outcome.PASS: 0, Outcome.REVIEW: 1, Outcome.REJECT: 2})


class ReasonCode(str, Enum):
    """Stable, machine-readable reasons. Values are part of the contract:
    never rename or reuse one; add a new code instead."""
    # Generic
    PASSED = "PASSED"
    # Engine-level (never produced by a rule's own logic)
    RULE_ERROR = "RULE_ERROR"
    RULE_INVALID_RESULT = "RULE_INVALID_RESULT"
    # Engineering data-integrity rules (backend/guardrails/rules.py)
    HERB_NOT_IN_FORMULARY = "HERB_NOT_IN_FORMULARY"
    DUPLICATE_HERB = "DUPLICATE_HERB"
    QUANTITY_NOT_INTEGER = "QUANTITY_NOT_INTEGER"
    QUANTITY_OUT_OF_RANGE = "QUANTITY_OUT_OF_RANGE"
    STRUCTURED_DOSHA_MISSING = "STRUCTURED_DOSHA_MISSING"
    STRUCTURED_HERBS_MISSING = "STRUCTURED_HERBS_MISSING"


# The outcomes each reason code may carry (Phase 3A.2). A finding that pairs a
# code with any other outcome -- e.g. PASS with HERB_NOT_IN_FORMULARY -- is a
# malformed rule result (RULE_INVALID_RESULT). Engine codes never come from a
# rule. A new code must be added here before any rule can use it.
ALLOWED_OUTCOMES: Mapping[ReasonCode, frozenset[Outcome]] = MappingProxyType({
    ReasonCode.PASSED: frozenset({Outcome.PASS}),
    ReasonCode.RULE_ERROR: frozenset({Outcome.REVIEW}),
    ReasonCode.RULE_INVALID_RESULT: frozenset({Outcome.REVIEW}),
    ReasonCode.HERB_NOT_IN_FORMULARY: frozenset({Outcome.REJECT}),
    ReasonCode.DUPLICATE_HERB: frozenset({Outcome.REJECT}),
    ReasonCode.QUANTITY_NOT_INTEGER: frozenset({Outcome.REJECT}),
    ReasonCode.QUANTITY_OUT_OF_RANGE: frozenset({Outcome.REJECT}),
    ReasonCode.STRUCTURED_DOSHA_MISSING: frozenset({Outcome.REVIEW}),
    ReasonCode.STRUCTURED_HERBS_MISSING: frozenset({Outcome.REVIEW}),
})

# The only text a result ever carries (Phase 3A.2): a fixed description per
# reason code. Rules cannot supply free text, so no herb name, dosha, quantity
# or other clinician- or patient-entered value can reach a result record.
REASON_DESCRIPTIONS: Mapping[ReasonCode, str] = MappingProxyType({
    ReasonCode.PASSED: "No issue found.",
    ReasonCode.RULE_ERROR: "The rule raised an exception; it was not evaluated.",
    ReasonCode.RULE_INVALID_RESULT: "The rule returned a malformed result; it was not evaluated.",
    ReasonCode.HERB_NOT_IN_FORMULARY: "Herb is not a canonical formulary herb.",
    ReasonCode.DUPLICATE_HERB: "Herb appears on more than one line; a candidate holds merged lines.",
    ReasonCode.QUANTITY_NOT_INTEGER: "Quantity is not a whole number of grams.",
    ReasonCode.QUANTITY_OUT_OF_RANGE: "Quantity is outside the technical input bound.",
    ReasonCode.STRUCTURED_DOSHA_MISSING: "No structured dosha value.",
    ReasonCode.STRUCTURED_HERBS_MISSING: "No structured herb lines.",
})


class RuleCategory(str, Enum):
    """ENGINEERING rules check data integrity only and mirror checks the
    system already makes. CLINICAL rules encode clinical judgement and can
    only be registered with a recorded approval (see rules.register_rule)."""
    ENGINEERING = "ENGINEERING"
    CLINICAL = "CLINICAL"


@dataclass(frozen=True)
class Finding:
    """What a rule's evaluate() returns for one observation. `line_no` is the
    1-based herb line it concerns, or None for the whole candidate. There is
    deliberately no free-text field: dosha and herb names are clinician- or
    AI-entered text and may carry identity, so a finding says only which
    line and which reason code; the result's text is REASON_DESCRIPTIONS."""
    outcome: Outcome
    reason_code: ReasonCode
    line_no: int | None = None


@dataclass(frozen=True)
class RuleResult:
    """One traceable result: which rule (id + version) said what, and why.
    `detail` is always REASON_DESCRIPTIONS[reason_code], set by the engine."""
    rule_id: str
    rule_version: int
    outcome: Outcome
    reason_code: ReasonCode
    detail: str = ""
    line_no: int | None = None

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "outcome": self.outcome.value,
            "reason_code": self.reason_code.value,
            "detail": self.detail,
            "line_no": self.line_no,
        }


@dataclass(frozen=True)
class EvaluationResult:
    """The full, reproducible record of one evaluation.

    `outcome` is the most severe result. `results` are in rule-set order,
    then line order within a rule; every rule in the set contributes at
    least one result, so a rule that was skipped cannot look like a PASS.
    `candidate_fingerprint` and `ruleset_hash` identify exactly what was
    evaluated under exactly which rules; `engine_fingerprint` identifies the
    engine code and tables that interpreted the rules' findings."""
    engine_version: str
    engine_fingerprint: str
    ruleset_id: str
    ruleset_version: str
    ruleset_hash: str
    candidate_fingerprint: str
    outcome: Outcome
    results: tuple[RuleResult, ...]

    def to_dict(self) -> dict:
        return {
            "engine_version": self.engine_version,
            "engine_fingerprint": self.engine_fingerprint,
            "ruleset_id": self.ruleset_id,
            "ruleset_version": self.ruleset_version,
            "ruleset_hash": self.ruleset_hash,
            "candidate_fingerprint": self.candidate_fingerprint,
            "outcome": self.outcome.value,
            "results": [r.to_dict() for r in self.results],
        }
