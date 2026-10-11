"""
The guardrail engine: immutable, versioned rule sets and a deterministic
evaluate().

evaluate(candidate, ruleset) runs every rule in the rule set's order and
returns an EvaluationResult. It never raises because of a rule: a rule that
raises, or returns something malformed, yields a REVIEW result with reason
RULE_ERROR / RULE_INVALID_RESULT -- a guardrail failure is never silently a
PASS, and never takes down its caller. It raises only for caller errors: a
candidate that is not a Candidate, or a rule set that was not built here.

Phase 3A is evaluation-only. Nothing in the request path calls this module:
preview, confirm, manual fallback, inventory, audit and every API response
are unchanged. Wiring it in is Phase 3B (docs/PHASE_3_PLAN.md §4).
"""
from __future__ import annotations

import hashlib
import inspect
import json
import sys
import types
from dataclasses import dataclass
from types import MappingProxyType

from backend.guardrails.candidate import Candidate, InvalidCandidateError
from backend.guardrails.model import (ALLOWED_OUTCOMES, REASON_DESCRIPTIONS, EvaluationResult, Finding, Outcome,
                                      ReasonCode, RuleResult)
from backend.guardrails.rules import ENG_DUPLICATE, ENG_FORMULARY, ENG_QUANTITY, ENG_STRUCTURED, Rule, registered_rule

ENGINE_VERSION = "3A.3"


class UnknownRuleError(LookupError):
    """A rule set names a rule that is not registered (or is not the
    registered definition of that rule id)."""


class EngineChangedError(RuntimeError):
    """A function or table the engine fingerprint covers was replaced after
    import, so ENGINE_FINGERPRINT no longer describes the logic that would
    run. Evaluation refuses rather than report a stale fingerprint."""


class InvalidRuleSetError(ValueError):
    """A rule set is empty, repeats a rule, has no id/version, or contains a
    rule whose code cannot be fingerprinted (no readable source)."""


_CODE_FINGERPRINTS: dict[tuple[str, int], str] = {}


def _code_fingerprint(rule: Rule) -> str:
    key = (rule.rule_id, rule.version)
    if key not in _CODE_FINGERPRINTS:
        _CODE_FINGERPRINTS[key] = _source_sha256(rule)
    return _CODE_FINGERPRINTS[key]


def _source_sha256(rule: Rule) -> str:
    """SHA-256 of the source of the rule's check function and of every helper
    it reaches (_helpers). Fails closed: a rule whose source cannot be read --
    a generated function, a callable object, a deployment without .py files --
    is never fingerprinted by its name, so it cannot join a rule set."""
    problem = InvalidRuleSetError(f"rule {rule.rule_id!r} has no readable source code, so it cannot be fingerprinted")
    if not isinstance(rule.check, types.FunctionType):
        raise problem
    try:
        root = inspect.getsource(rule.check)
        helpers = sorted(inspect.getsource(f) for f in _helpers(rule.check))
    except (OSError, TypeError):
        raise problem from None
    content = json.dumps([root, *helpers], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


_PACKAGE = "backend.guardrails"


def _in_scope(module: str, rule_module: str) -> bool:
    """The rule's own module, or exactly backend.guardrails or a module inside
    it -- not a look-alike such as backend.guardrails_x."""
    return module == rule_module or module == _PACKAGE or module.startswith(_PACKAGE + ".")


def _helpers(fn: types.FunctionType) -> list:
    """The functions fn reaches, transitively, by global name or through a
    closure, that are defined in fn's own module or in backend.guardrails.
    Anything else a rule depends on -- other packages, module attributes,
    constants, captured data -- is not covered here and must come in through
    params, which are hashed separately."""
    found, pending = {}, [fn]
    while pending:
        current = pending.pop()
        names, codes = set(), [current.__code__]
        while codes:
            code = codes.pop()
            names.update(code.co_names)
            codes.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
        refs = [current.__globals__.get(name) for name in names]
        for cell in current.__closure__ or ():
            try:
                refs.append(cell.cell_contents)
            except ValueError:  # an empty cell
                pass
        for ref in refs:
            if (isinstance(ref, types.FunctionType) and ref is not fn and id(ref) not in found
                    and _in_scope(ref.__module__ or "", fn.__module__ or "")):
                found[id(ref)] = ref
                pending.append(ref)
    return list(found.values())


@dataclass(frozen=True)
class RuleSet:
    ruleset_id: str
    version: str
    rules: tuple[Rule, ...]
    content_hash: str


def build_ruleset(ruleset_id: str, version: str, rule_ids) -> RuleSet:
    """The only way to make a RuleSet. Order is significant (it is the
    evaluation and reporting order) and is part of the content hash, as are
    each rule's id, version, category, params, code fingerprint and its
    provenance metadata (description, source, approved_by). Metadata is
    hashed exactly as given: an empty string and None are distinct values
    (e.g. approved_by="" and approved_by=None give different hashes)."""
    if not ruleset_id or not version:
        raise InvalidRuleSetError("a rule set needs an id and a version")
    rule_ids = list(rule_ids)
    if not rule_ids:
        raise InvalidRuleSetError("a rule set needs at least one rule")
    if len(set(rule_ids)) != len(rule_ids):
        raise InvalidRuleSetError("a rule set may not repeat a rule")
    rules = []
    for rule_id in rule_ids:
        rule = registered_rule(rule_id)
        if rule is None:
            raise UnknownRuleError(f"unknown rule {rule_id!r}")
        rules.append(rule)
    return RuleSet(ruleset_id, version, tuple(rules), _content_hash(ruleset_id, version, rules))


def _content_hash(ruleset_id: str, version: str, rules) -> str:
    content = json.dumps({
        "ruleset_id": ruleset_id,
        "version": version,
        "rules": [{"rule_id": r.rule_id, "version": r.version, "category": r.category.value,
                   "params": r.params, "code_sha256": _code_fingerprint(r),
                   "description": r.description, "source": r.source, "approved_by": r.approved_by}
                  for r in rules],
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_thaw)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _thaw(value):
    """json.dumps hook: frozen params (rules._freeze) hash exactly as the plain
    JSON they were registered from. Anything else stays unserialisable."""
    if isinstance(value, MappingProxyType):
        return dict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _valid_finding(finding, candidate: Candidate) -> bool:
    return (isinstance(finding, Finding)
            and isinstance(finding.outcome, Outcome)
            and isinstance(finding.reason_code, ReasonCode)
            and finding.reason_code not in (ReasonCode.RULE_ERROR, ReasonCode.RULE_INVALID_RESULT)
            and finding.outcome in ALLOWED_OUTCOMES.get(finding.reason_code, frozenset())
            and (finding.line_no is None
                 or (isinstance(finding.line_no, int) and not isinstance(finding.line_no, bool)
                     and 1 <= finding.line_no <= len(candidate.herbs))))


def _finding_order(finding: Finding):
    return (finding.line_no is not None, finding.line_no or 0, finding.reason_code.value, finding.outcome.severity)


def _run_rule(rule: Rule, candidate: Candidate) -> list[RuleResult]:
    def result(outcome, reason, line_no=None):
        # Text is only ever the fixed description of the reason code: never
        # anything a rule, the candidate or an exception supplied.
        return RuleResult(rule.rule_id, rule.version, outcome, reason, REASON_DESCRIPTIONS[reason], line_no)

    try:
        findings = rule.evaluate(candidate)
    except Exception:  # isolate: a broken rule must not break evaluation
        return [result(Outcome.REVIEW, ReasonCode.RULE_ERROR)]
    if not isinstance(findings, (list, tuple)) or not all(_valid_finding(f, candidate) for f in findings):
        return [result(Outcome.REVIEW, ReasonCode.RULE_INVALID_RESULT)]
    if not findings:
        return [result(Outcome.PASS, ReasonCode.PASSED)]
    return [result(f.outcome, f.reason_code, f.line_no) for f in sorted(findings, key=_finding_order)]


def evaluate(candidate: Candidate, ruleset: RuleSet) -> EvaluationResult:
    if not _engine_intact(_ENGINE_BINDINGS, _ENGINE_CODES):
        raise EngineChangedError("engine code or tables changed after the engine fingerprint was taken")
    if not isinstance(candidate, Candidate):
        raise InvalidCandidateError("evaluate() needs a Candidate; use build_candidate()")
    if not isinstance(ruleset, RuleSet) or any(registered_rule(r.rule_id) is not r for r in ruleset.rules):
        raise UnknownRuleError("rule set contains a rule that is not the registered definition; use build_ruleset()")
    if not ruleset.rules or ruleset.content_hash != _content_hash(ruleset.ruleset_id, ruleset.version, ruleset.rules):
        raise InvalidRuleSetError("rule set content does not match its hash; use build_ruleset()")
    results = tuple(r for rule in ruleset.rules for r in _run_rule(rule, candidate))
    outcome = max((r.outcome for r in results), key=lambda o: o.severity)
    return EvaluationResult(
        engine_version=ENGINE_VERSION,
        engine_fingerprint=ENGINE_FINGERPRINT,
        ruleset_id=ruleset.ruleset_id,
        ruleset_version=ruleset.version,
        ruleset_hash=ruleset.content_hash,
        candidate_fingerprint=candidate.fingerprint(),
        outcome=outcome,
        results=results,
    )


# --- engine fingerprint (Phase 3A B5) ---------------------------------------------

# The read-only tables (backend/guardrails/model.py) that decide how findings
# are interpreted: severity order, allowed outcome per reason, result texts.
_ENGINE_TABLES = ("_SEVERITY", "ALLOWED_OUTCOMES", "REASON_DESCRIPTIONS")


def _code_names(fn: types.FunctionType) -> set:
    names, codes = set(), [fn.__code__]
    while codes:
        code = codes.pop()
        names.update(code.co_names)
        codes.extend(c for c in code.co_consts if isinstance(c, types.CodeType))
    return names


def _engine_scan(root=None):
    """What the engine fingerprint covers, found from `root` (evaluate() by
    default) by the same principle as _helpers (B2), extended to methods:
    every function reached by global name in backend.guardrails, every
    method, property, staticmethod or classmethod of a backend.guardrails
    class whose name that code uses (rule.evaluate, outcome.severity,
    candidate.fingerprint -> canonical_json, ...), and the _ENGINE_TABLES
    objects that code actually reads. For a descriptor, the function it
    wraps is fingerprinted and the descriptor itself is the binding checked.
    Not the rules' own checks (the rule-set hash covers them) or unrelated
    module code.

    Returns (functions, tables, bindings). bindings are the (namespace, name,
    object) lookups the scan resolved; evaluate() re-checks them, so a table
    or function replaced after import cannot go unnoticed."""
    methods = {}
    for module_name, module in list(sys.modules.items()):
        if module_name == _PACKAGE or module_name.startswith(_PACKAGE + "."):
            for cls in vars(module).values():
                if isinstance(cls, type) and cls.__module__ == module_name:
                    for name, attr in vars(cls).items():
                        if isinstance(attr, property):
                            fn = attr.fget
                        elif isinstance(attr, (staticmethod, classmethod)):
                            fn = attr.__func__
                        else:
                            fn = attr
                        if isinstance(fn, types.FunctionType) and not name.startswith("__"):
                            methods.setdefault(name, []).append((cls, name, attr, fn))
    root = evaluate if root is None else root
    functions, tables, bindings, pending = {id(root): root}, {}, {}, [root]
    while pending:
        fn = pending.pop()
        for name in _code_names(fn):
            reached = []
            ref = fn.__globals__.get(name)
            if name in _ENGINE_TABLES and ref is not None:
                if tables.setdefault(name, ref) is not ref:
                    raise EngineChangedError(f"two different {name} tables are in use")
                bindings[(id(fn.__globals__), name)] = (fn.__globals__, name, ref)
            elif isinstance(ref, types.FunctionType) and _in_scope(ref.__module__ or "", fn.__module__ or ""):
                bindings[(id(fn.__globals__), name)] = (fn.__globals__, name, ref)
                reached.append(ref)
            for cls, attr_name, attr, method in methods.get(name, ()):
                namespace = vars(cls)
                bindings[(id(cls), attr_name)] = (namespace, attr_name, attr)
                reached.append(method)
            for found in reached:
                if id(found) not in functions:
                    functions[id(found)] = found
                    pending.append(found)
    if set(tables) != set(_ENGINE_TABLES):
        raise EngineChangedError("the engine scan did not find every engine table")
    return list(functions.values()), tables, tuple(bindings.values())


def _engine_intact(bindings, codes) -> bool:
    """True while every binding the scan resolved still holds the same object
    and every scanned function still runs the code it was fingerprinted with:
    a function's __code__ can be replaced in place, keeping its identity."""
    return (all(namespace.get(name) is obj for namespace, name, obj in bindings)
            and all(fn.__code__ is code for fn, code in codes))


def _engine_fingerprint(functions, tables) -> str:
    """SHA-256 of the engine tables' contents and the source of the scanned
    functions. Sets and sources are sorted: their iteration order differs
    between processes. Fails closed: unreadable source raises (at import)."""
    content = json.dumps({
        "severity": {outcome.value: rank for outcome, rank in tables["_SEVERITY"].items()},
        "allowed_outcomes": {code.value: sorted(o.value for o in outcomes)
                             for code, outcomes in tables["ALLOWED_OUTCOMES"].items()},
        "reason_descriptions": {code.value: text for code, text in tables["REASON_DESCRIPTIONS"].items()},
        "source": sorted(inspect.getsource(f) for f in functions),
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


_ENGINE_FUNCTIONS, _ENGINE_TABLE_OBJECTS, _ENGINE_BINDINGS = _engine_scan()
_ENGINE_CODES = tuple((fn, fn.__code__) for fn in _ENGINE_FUNCTIONS)
ENGINE_FINGERPRINT = _engine_fingerprint(_ENGINE_FUNCTIONS, _ENGINE_TABLE_OBJECTS)


# The only rule set in Phase 3A: engineering data-integrity checks, no
# clinical content.
ENGINEERING_BASELINE = build_ruleset(
    "ENGINEERING-BASELINE", "1",
    [ENG_STRUCTURED.rule_id, ENG_FORMULARY.rule_id, ENG_DUPLICATE.rule_id, ENG_QUANTITY.rule_id],
)
