"""
Phase 3A deterministic guardrail foundation -- evaluation only.

    from backend.guardrails import build_candidate, evaluate, ENGINEERING_BASELINE
    result = evaluate(build_candidate("Vata", [{"name": "Ashwagandha", "grams": 5}]),
                      ENGINEERING_BASELINE)
    result.outcome            # Outcome.PASS / REVIEW / REJECT
    result.to_dict()          # traceable, JSON-serialisable record

No Gemini, no database, no inventory, no audit: rules are pure functions of
the candidate. Nothing in the request path imports this package, so preview,
confirm, manual fallback, inventory, audit and API responses are unchanged.
Only engineering data-integrity rules exist; clinical rules need a recorded
approval to register (documentation/docs/PHASE_3_PLAN.md).
"""
from backend.guardrails.candidate import Candidate, HerbLine, InvalidCandidateError, build_candidate, build_confirm_candidate
from backend.guardrails.engine import (
    ENGINE_VERSION,
    ENGINEERING_BASELINE,
    InvalidRuleSetError,
    RuleSet,
    UnknownRuleError,
    build_ruleset,
    evaluate,
)
from backend.guardrails.model import EvaluationResult, Finding, Outcome, ReasonCode, RuleCategory, RuleResult
from backend.guardrails.rules import Rule, RuleRegistrationError, UnapprovedRuleError, register_rule, registered_rule

__all__ = [
    "Candidate", "HerbLine", "InvalidCandidateError", "build_candidate", "build_confirm_candidate",
    "ENGINE_VERSION", "ENGINEERING_BASELINE", "InvalidRuleSetError", "RuleSet", "UnknownRuleError",
    "build_ruleset", "evaluate",
    "EvaluationResult", "Finding", "Outcome", "ReasonCode", "RuleCategory", "RuleResult",
    "Rule", "RuleRegistrationError", "UnapprovedRuleError", "register_rule", "registered_rule",
]
