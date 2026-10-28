"""
Rules and the rule registry.

A rule is a pure function of a Candidate: no I/O, no clock, no randomness,
no AI. It returns the Findings it observed (an empty list means PASS). Its
`params` are data, and are part of the rule set's content hash.

A rule must run in time linear in the candidate's size. Herb lines are
bounded by the formulary on the confirm path (20 herbs today), but the dosha
text is not (AI proposals), so a rule must never apply backtracking-prone
patterns or quadratic work to it. There is no per-rule timeout: one cannot
be enforced safely in-process without making results depend on timing
(Phase 3B decides where evaluation runs relative to confirm's database lock).

Only ENGINEERING rules exist in Phase 3A. They check data integrity and
mirror checks the system already makes; they encode no clinical judgement.
A CLINICAL rule cannot be registered without a recorded approval
(`approved_by` and `source`), so clinical content cannot enter the registry
by accident. No clinical rule is defined anywhere -- every clinical category
awaits clinician input and explicit approval (documentation/docs/PHASE_3_PLAN.md §3).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping

from backend.guardrails.candidate import Candidate
from backend.guardrails.model import Finding, Outcome, ReasonCode, RuleCategory
from backend.herb_resolver import HERB_ALIASES
from backend.schemas import PrescribedHerb


class RuleRegistrationError(ValueError):
    """A rule definition is invalid or would be registered twice."""


class UnapprovedRuleError(RuleRegistrationError):
    """A CLINICAL rule was registered without a recorded approval."""


@dataclass(frozen=True)
class Rule:
    rule_id: str
    version: int
    category: RuleCategory
    description: str
    source: str
    check: Callable[[Candidate, Mapping], list[Finding]]
    params: Mapping = field(default_factory=dict)
    approved_by: str | None = None

    def evaluate(self, candidate: Candidate) -> list[Finding]:
        return self.check(candidate, self.params)


_REGISTRY: dict[str, Rule] = {}


def _param_problem(value, path: str) -> str | None:
    """Rule params must be plain JSON data -- str-keyed objects, lists or
    tuples, strings, finite numbers, booleans and null -- so the rule-set
    content hash is well defined and identical in every process. Returns a
    description of the first problem, or None."""
    if value is None or isinstance(value, (str, bool, int)):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else f"{path} is not a finite number"
    if isinstance(value, (list, tuple)):
        return next((p for i, v in enumerate(value) if (p := _param_problem(v, f"{path}[{i}]"))), None)
    if isinstance(value, Mapping):
        for key, v in value.items():
            if not isinstance(key, str):
                return f"{path} has a non-string key"
            if problem := _param_problem(v, f"{path}.{key}"):
                return problem
        return None
    return f"{path} has a non-JSON value of type {type(value).__name__}"


def _freeze(value):
    """A deep, read-only copy of validated params (see _param_problem): every
    object becomes a read-only mapping and every list a tuple, so neither the
    caller nor a rule can change a registered rule's params afterwards. Rules
    therefore see sequences as tuples and objects as read-only mappings."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(v) for key, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _present(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def register_rule(rule: Rule) -> Rule:
    if not rule.rule_id or not isinstance(rule.version, int) or isinstance(rule.version, bool) or rule.version < 1:
        raise RuleRegistrationError("a rule needs a non-empty rule_id and a positive integer version")
    if not isinstance(rule.category, RuleCategory) or not callable(rule.check):
        raise RuleRegistrationError("a rule needs a RuleCategory and a callable check")
    if rule.rule_id in _REGISTRY:
        raise RuleRegistrationError(f"rule {rule.rule_id!r} is already registered")
    if not isinstance(rule.params, Mapping) or (problem := _param_problem(rule.params, "params")):
        raise RuleRegistrationError(
            f"rule {rule.rule_id!r} params must be JSON-serialisable data"
            + (f": {problem}" if isinstance(rule.params, Mapping) else ""))
    if rule.category is RuleCategory.CLINICAL and not (_present(rule.approved_by) and _present(rule.source)):
        raise UnapprovedRuleError(
            f"clinical rule {rule.rule_id!r} needs a recorded approval (approved_by and source)")
    # description, source and approved_by are part of the rule-set content hash
    # (engine._content_hash), so they must serialise the same way in every
    # process: text (approved_by may also be None), never an arbitrary object.
    if not (isinstance(rule.description, str) and isinstance(rule.source, str)
            and (rule.approved_by is None or isinstance(rule.approved_by, str))):
        raise RuleRegistrationError(
            f"rule {rule.rule_id!r} description and source must be strings, approved_by a string or None")
    frozen = Rule(rule.rule_id, rule.version, rule.category, rule.description, rule.source,
                  rule.check, _freeze(rule.params), rule.approved_by)
    _REGISTRY[rule.rule_id] = frozen
    return frozen


def registered_rule(rule_id: str) -> Rule | None:
    return _REGISTRY.get(rule_id)


# --- engineering rules --------------------------------------------------------------

# The canonical herb names normalize_herb() can produce -- the formulary.
FORMULARY = frozenset(HERB_ALIASES.values())
# The per-line technical bound /prescribe/confirm already enforces, read from
# the same model so it cannot drift (backend/main.py derives it the same way).
MAX_GRAMS = next(m.le for m in PrescribedHerb.model_fields["grams"].metadata if hasattr(m, "le"))


def _formulary(candidate: Candidate, params: Mapping) -> list[Finding]:
    formulary = params["formulary"]
    return [Finding(Outcome.REJECT, ReasonCode.HERB_NOT_IN_FORMULARY, i)
            for i, h in enumerate(candidate.herbs, start=1) if h.name not in formulary]


def _duplicates(candidate: Candidate, params: Mapping) -> list[Finding]:
    seen: set[str] = set()
    findings = []
    for i, h in enumerate(candidate.herbs, start=1):
        if h.name in seen:
            findings.append(Finding(Outcome.REJECT, ReasonCode.DUPLICATE_HERB, i))
        seen.add(h.name)
    return findings


def _quantity(candidate: Candidate, params: Mapping) -> list[Finding]:
    low, high = params["min_grams"], params["max_grams"]
    findings = []
    for i, h in enumerate(candidate.herbs, start=1):
        if isinstance(h.grams, float) and not h.grams.is_integer():
            findings.append(Finding(Outcome.REJECT, ReasonCode.QUANTITY_NOT_INTEGER, i))
        elif not low <= h.grams <= high:
            findings.append(Finding(Outcome.REJECT, ReasonCode.QUANTITY_OUT_OF_RANGE, i))
    return findings


def _structured(candidate: Candidate, params: Mapping) -> list[Finding]:
    findings = []
    if candidate.dosha_imbalance is None or not candidate.dosha_imbalance.strip():
        findings.append(Finding(Outcome.REVIEW, ReasonCode.STRUCTURED_DOSHA_MISSING))
    if not candidate.herbs:
        findings.append(Finding(Outcome.REVIEW, ReasonCode.STRUCTURED_HERBS_MISSING))
    return findings


ENG_FORMULARY = register_rule(Rule(
    "ENG-FORMULARY", 1, RuleCategory.ENGINEERING,
    "Every herb is a canonical formulary herb.",
    "Mirrors backend/herb_resolver.normalize_herb (HERB_ALIASES values).",
    _formulary, {"formulary": tuple(sorted(FORMULARY))}))

ENG_DUPLICATE = register_rule(Rule(
    "ENG-DUPLICATE", 1, RuleCategory.ENGINEERING,
    "Each herb appears on at most one line (candidates hold merged totals).",
    "Mirrors deduct_and_prescribe's merge and UNIQUE(prescription_id, herb_name) (migration 015).",
    _duplicates))

ENG_QUANTITY = register_rule(Rule(
    "ENG-QUANTITY", 1, RuleCategory.ENGINEERING,
    "Each quantity is a whole number of grams within the technical input bound. Not a clinical dose limit.",
    "Mirrors backend/schemas.PrescribedHerb (gt=0, le) and the merged-total check in /prescribe/confirm.",
    _quantity, {"min_grams": 1, "max_grams": MAX_GRAMS}))

ENG_STRUCTURED = register_rule(Rule(
    "ENG-STRUCTURED", 1, RuleCategory.ENGINEERING,
    "Structured dosha and herb values are present. A data-quality flag, not the "
    "clinical treatment of legacy history (documentation/docs/PHASE_3_PLAN.md §5 D7).",
    "Phase 2 structured-data authority (migrations 015-016).",
    _structured))
