"""
Deterministic claim / citation verifier for AI-2 (no model). Reject = abstain; never repairs.

verify_claim(snapshot, claim) re-executes the claim's query against the snapshot with the
deterministic executor and checks the claim against that result -- it trusts nothing the
claim producer computed:

  M1  malformed_claim        keys / types wrong
  M2  unsupported_query      outside the closed vocabulary or malformed
  C1  status_mismatch        claimed status differs from the recomputed one
  C2  qualifier_mismatch     exact / at_least / partial / unknown / not_recorded is wrong
  C3  value_mismatch         claimed value differs from the recomputed value
  C4  invented_source        a cited label is not a record of THIS patient snapshot
      missing_citation       a label the result depends on is not cited
      extra_citation         a cited label the result does not depend on
  C5  unsupported_number     the text states a number that is not in the value or evidence
      unsupported_herb       the text names a formulary herb that is not in the value or evidence
      foreign_label          the text mentions a record label that is not cited
      qualifier_text_missing an at-least / partial / unknown / not-recorded answer is phrased
                             without saying so
"""
from __future__ import annotations

import copy
import json
import re

from memory.answer import qualifier_for
from memory.executor import Query, QueryError, execute
from memory.snapshot import PatientSnapshot

CLAIM_KEYS = frozenset({"query", "status", "value", "qualifier", "sources", "text"})
NUMBER = re.compile(r"\d+(?:\.\d+)?")
LABEL = re.compile(r"\b(?:rx|enc):[A-Za-z0-9_-]+")
QUALIFIER_WORDS = {"at_least": "at least", "partial": "known records only", "unknown": "unknown",
                   "not_recorded": "not recorded"}


def _formulary() -> frozenset[str]:
    from backend.herb_resolver import HERB_ALIASES
    return frozenset(HERB_ALIASES.values())


def _canonical(value):
    return json.loads(json.dumps(value, sort_keys=True))


def verify_claim(snapshot: PatientSnapshot, claim) -> dict:
    claim_copy = copy.deepcopy(claim)
    violations: list[dict] = []

    def flag(code: str, detail: str = "") -> None:
        violations.append({"code": code, "detail": detail})

    def done() -> dict:
        assert claim == claim_copy, "verifier must not modify the claim"
        return {"verdict": "rejected" if violations else "verified", "violations": violations}

    if not isinstance(claim, dict) or set(claim) != CLAIM_KEYS or not isinstance(claim.get("sources"), list) \
            or not all(isinstance(x, str) for x in claim["sources"]) or not isinstance(claim.get("text"), str):
        flag("malformed_claim", "claim must have exactly " + ", ".join(sorted(CLAIM_KEYS)))
        return done()
    try:
        query = Query.from_dict(claim["query"])
        result = execute(snapshot, query)
    except (QueryError, TypeError) as exc:
        flag("unsupported_query", str(exc))
        return done()

    if claim["status"] != result.status:
        flag("status_mismatch", f"claimed {claim['status']!r}, recomputed {result.status!r}")
    expected_qualifier = qualifier_for(query, result)
    if claim["qualifier"] != expected_qualifier:
        flag("qualifier_mismatch", f"claimed {claim['qualifier']!r}, required {expected_qualifier!r}")
    if _canonical(claim["value"]) != _canonical(result.value):
        flag("value_mismatch", f"claimed {claim['value']!r}, recomputed {result.value!r}")

    cited, needed, known = set(claim["sources"]), set(result.labels), snapshot.labels()
    for label in sorted(cited - known):
        flag("invented_source", f"{label} is not a record of this patient")
    for label in sorted(needed - cited):
        flag("missing_citation", label)
    for label in sorted((cited & known) - needed):
        flag("extra_citation", label)

    text = claim["text"]
    support = json.dumps([_canonical(result.value), [[e.label, e.field, _canonical(e.value)] for e in result.evidence],
                          sorted(needed), query.to_dict()], sort_keys=True)
    allowed_numbers = set(NUMBER.findall(support))
    for number in NUMBER.findall(LABEL.sub(" ", text)):
        if number not in allowed_numbers:
            flag("unsupported_number", number)
    lowered_support = support.lower()
    for herb in sorted(_formulary()):
        if re.search(rf"\b{re.escape(herb.lower())}\b", text.lower()) and herb.lower() not in lowered_support:
            flag("unsupported_herb", herb)
    for label in LABEL.findall(text):
        if label not in cited:
            flag("foreign_label", label)
    phrase = QUALIFIER_WORDS.get(expected_qualifier)
    if phrase and phrase not in text.lower():
        flag("qualifier_text_missing", f"text must say {phrase!r}")
    return done()
