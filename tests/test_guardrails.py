"""
Phase 3A -- deterministic guardrail foundation (backend/guardrails/).

Coverage:
  - determinism: equal results for equal input, across repeats and across
    processes (fingerprints and rule-set hash included);
  - outcomes and stable reason codes for every engineering rule, and
    aggregate severity (REJECT > REVIEW > PASS);
  - rule ordering: results follow rule-set order, then line order; order is
    part of the rule-set hash;
  - boundaries: gram bounds, integer-valued floats, empty and blank values,
    line and text limits;
  - invalid inputs rejected structurally with InvalidCandidateError;
  - unknown rules, invalid and tampered rule sets, registration rules
    (a CLINICAL rule cannot register without a recorded approval);
  - rule metadata (description, source, approved_by) is part of the rule-set
    hash, stable across processes; whitespace-only clinical approval is
    refused; non-text metadata is refused;
  - code fingerprint: covers helper functions (own module or exactly
    backend.guardrails, incl. closures and mutual recursion); unreadable
    source fails closed;
  - rule params are deeply frozen at registration (caller references and
    the rule itself cannot change them); fingerprints are unchanged;
  - evaluation cost: a confirm candidate has at most one line per formulary
    herb; worst-case evaluation timing smoke checks (no runtime limit);
  - engine fingerprint in every result: read-only tables plus the code that
    interprets findings (functions and methods, transitively); evaluation
    refuses if covered code or tables were replaced after import;
  - the engine guard also detects in-place __code__ replacement and covers
    staticmethod/classmethod descriptors (discovery, fingerprint, replacement);
  - candidate fingerprint: whole-number grams written as integers (5 / 5.0
    alike), fractional values distinct, rules see the original value;
  - error handling: a raising or malformed rule becomes REVIEW, is isolated,
    and leaks no exception message;
  - independence and evaluation-only: the package imports no Gemini, database,
    inventory, audit or request-path module; only backend/shadow_store.py
    imports the package, and only main.py imports that recorder, both lazily
    (Phase 3A.4);
  - Phase 2 consistency: what /prescribe/confirm would commit passes.

Pure unit tests: no database, no network, no app startup.

Run directly (no pytest required):
    python tests/test_guardrails.py
"""
import ast
import dataclasses
import glob
import json
import os
import secrets
import subprocess
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail=""):
    """Raises AssertionError immediately on a failed assertion -- same
    convention as every other test file in this project."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail != "" else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _raises(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    return False


def _codes(result) -> list[tuple]:
    return [(r.rule_id, r.outcome.value, r.reason_code.value, r.line_no) for r in result.results]


def _eval(dosha, herbs, ruleset=None):
    from backend.guardrails import ENGINEERING_BASELINE, build_candidate, evaluate
    return evaluate(build_candidate(dosha, herbs), ruleset or ENGINEERING_BASELINE)


def _test_rule(check_fn, category=None, **kw):
    """Registers a uniquely named test rule (the registry is process-wide)."""
    from backend.guardrails import Rule, RuleCategory, register_rule
    return register_rule(Rule(f"TEST-{secrets.token_hex(4)}", 1, category or RuleCategory.ENGINEERING,
                              "test rule", "tests/test_guardrails.py", check_fn, **kw))


# --- baseline ----------------------------------------------------------------------

def test_baseline_ruleset_is_engineering_only_and_versioned():
    from backend.guardrails import ENGINE_VERSION, ENGINEERING_BASELINE, RuleCategory
    check("engine version recorded", ENGINE_VERSION == "3A.3")
    check("baseline rule order", [r.rule_id for r in ENGINEERING_BASELINE.rules]
          == ["ENG-STRUCTURED", "ENG-FORMULARY", "ENG-DUPLICATE", "ENG-QUANTITY"])
    check("baseline holds engineering rules only (no clinical content)",
          all(r.category is RuleCategory.ENGINEERING for r in ENGINEERING_BASELINE.rules))
    check("every rule records a source", all(r.source for r in ENGINEERING_BASELINE.rules))
    check("rule-set hash is a SHA-256 hex digest", len(ENGINEERING_BASELINE.content_hash) == 64)
    check("rule set is immutable", _raises(lambda: setattr(ENGINEERING_BASELINE, "version", "2"),
                                           dataclasses.FrozenInstanceError))
    params = ENGINEERING_BASELINE.rules[3].params

    def mutate():
        params["max_grams"] = 99999

    check("rule params are read-only", _raises(mutate, TypeError) and params["max_grams"] == 10000)


# --- outcomes and reason codes ---------------------------------------------------------

def test_outcomes_and_reason_codes():
    ok = _eval("Vata", [{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 3}])
    check("valid candidate -> PASS, one PASSED result per rule", ok.outcome.value == "PASS" and _codes(ok) == [
        ("ENG-STRUCTURED", "PASS", "PASSED", None), ("ENG-FORMULARY", "PASS", "PASSED", None),
        ("ENG-DUPLICATE", "PASS", "PASSED", None), ("ENG-QUANTITY", "PASS", "PASSED", None)], _codes(ok))

    r = _eval("Vata", [{"name": "ashwagandha", "grams": 5}])
    check("non-canonical spelling -> REJECT HERB_NOT_IN_FORMULARY on line 1",
          r.outcome.value == "REJECT" and ("ENG-FORMULARY", "REJECT", "HERB_NOT_IN_FORMULARY", 1) in _codes(r))
    r = _eval("Vata", [{"name": "Unobtainium", "grams": 5}])
    check("unknown herb -> REJECT HERB_NOT_IN_FORMULARY",
          ("ENG-FORMULARY", "REJECT", "HERB_NOT_IN_FORMULARY", 1) in _codes(r))
    r = _eval("Vata", [{"name": "Brahmi", "grams": 1}, {"name": "Brahmi", "grams": 2}])
    check("duplicate line -> REJECT DUPLICATE_HERB on the repeat",
          ("ENG-DUPLICATE", "REJECT", "DUPLICATE_HERB", 2) in _codes(r))
    r = _eval(None, [{"name": "Brahmi", "grams": 1}])
    check("missing dosha -> REVIEW STRUCTURED_DOSHA_MISSING (data-quality flag)",
          r.outcome.value == "REVIEW" and ("ENG-STRUCTURED", "REVIEW", "STRUCTURED_DOSHA_MISSING", None) in _codes(r))
    r = _eval("Vata", [])
    check("no herb lines -> REVIEW STRUCTURED_HERBS_MISSING",
          r.outcome.value == "REVIEW" and ("ENG-STRUCTURED", "REVIEW", "STRUCTURED_HERBS_MISSING", None) in _codes(r))
    r = _eval(None, [{"name": "Brahmi", "grams": 0}])
    check("REVIEW and REJECT together -> aggregate REJECT", r.outcome.value == "REJECT")

    from backend.guardrails import ReasonCode
    check("reason codes are stable string values", {c.value for c in ReasonCode} == {
        "PASSED", "RULE_ERROR", "RULE_INVALID_RESULT", "HERB_NOT_IN_FORMULARY", "DUPLICATE_HERB",
        "QUANTITY_NOT_INTEGER", "QUANTITY_OUT_OF_RANGE", "STRUCTURED_DOSHA_MISSING", "STRUCTURED_HERBS_MISSING"})


# --- determinism -------------------------------------------------------------------------

def test_determinism_within_and_across_processes():
    herbs = [{"name": "Triphala", "grams": 30}, {"name": "brahmi", "grams": 0}, {"name": "Brahmi", "grams": 2.5}]
    first = _eval("Pitta", herbs)
    check("100 repeats give equal results", all(_eval("Pitta", herbs) == first for _ in range(100)))
    check("to_dict is JSON-serialisable and stable",
          json.dumps(first.to_dict(), sort_keys=True) == json.dumps(_eval("Pitta", herbs).to_dict(), sort_keys=True))
    code = ("import json,sys; sys.path.insert(0, %r)\n"
            "from backend.guardrails import build_candidate, evaluate, ENGINEERING_BASELINE\n"
            "print(json.dumps(evaluate(build_candidate('Pitta', %r), ENGINEERING_BASELINE).to_dict(), sort_keys=True))"
            % (ROOT, herbs))
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT)
    check("a fresh process gives the identical record (hashes included)",
          other.returncode == 0 and json.loads(other.stdout) == first.to_dict(), other.stderr[-300:])
    check("evaluation does not mutate its input", herbs[1] == {"name": "brahmi", "grams": 0})
    check("different input -> different candidate fingerprint",
          _eval("Pitta", herbs[:1]).candidate_fingerprint != first.candidate_fingerprint)


# --- ordering --------------------------------------------------------------------------------

def test_rule_ordering():
    from backend.guardrails import ENGINEERING_BASELINE, Finding, Outcome, ReasonCode, build_ruleset
    herbs = [{"name": "Nope", "grams": 0}, {"name": "Brahmi", "grams": 5}, {"name": "Brahmi", "grams": 20000}]
    forward = _eval("Vata", herbs)
    check("results follow rule-set order", [r.rule_id for r in forward.results]
          == ["ENG-STRUCTURED", "ENG-FORMULARY", "ENG-DUPLICATE", "ENG-QUANTITY", "ENG-QUANTITY"])
    check("within a rule, results follow line order",
          [r.line_no for r in forward.results if r.rule_id == "ENG-QUANTITY"] == [1, 3])
    reversed_set = build_ruleset("ENGINEERING-BASELINE-REVERSED", "1", [r.rule_id for r in reversed(ENGINEERING_BASELINE.rules)])
    backward = _eval("Vata", herbs, reversed_set)
    check("reversed rule set reverses rule order", [r.rule_id for r in backward.results]
          == ["ENG-QUANTITY", "ENG-QUANTITY", "ENG-DUPLICATE", "ENG-FORMULARY", "ENG-STRUCTURED"])
    check("aggregate outcome does not depend on order", backward.outcome == forward.outcome)
    same_rules_other_order = build_ruleset("ENGINEERING-BASELINE", "1", [r.rule_id for r in reversed(ENGINEERING_BASELINE.rules)])
    check("order is part of the rule-set hash", same_rules_other_order.content_hash != ENGINEERING_BASELINE.content_hash)
    check("rebuilding the same rule set reproduces its hash", build_ruleset(
        "ENGINEERING-BASELINE", "1", [r.rule_id for r in ENGINEERING_BASELINE.rules]).content_hash == ENGINEERING_BASELINE.content_hash)

    unsorted = _test_rule(lambda c, p: [Finding(Outcome.REJECT, ReasonCode.QUANTITY_OUT_OF_RANGE, 2),
                                        Finding(Outcome.REVIEW, ReasonCode.STRUCTURED_DOSHA_MISSING, None),
                                        Finding(Outcome.REJECT, ReasonCode.QUANTITY_NOT_INTEGER, 1),
                                        Finding(Outcome.REJECT, ReasonCode.DUPLICATE_HERB, 1)])
    r = _eval("Vata", herbs, build_ruleset("T", "1", [unsorted.rule_id]))
    check("a rule's findings are put in a canonical order (whole-candidate first, then by line)",
          [(x.line_no, x.reason_code.value) for x in r.results]
          == [(None, "STRUCTURED_DOSHA_MISSING"), (1, "DUPLICATE_HERB"), (1, "QUANTITY_NOT_INTEGER"), (2, "QUANTITY_OUT_OF_RANGE")])


# --- boundaries ----------------------------------------------------------------------------

def test_quantity_and_size_boundaries():
    from backend.guardrails import InvalidCandidateError, build_candidate
    from backend.guardrails.candidate import MAX_HERB_NAME_LENGTH
    from backend.guardrails.rules import MAX_GRAMS
    from backend.schemas import PrescribedHerb

    check("gram bound is read from PrescribedHerb (10000)", MAX_GRAMS == 10000 and
          any(getattr(m, "le", None) == MAX_GRAMS for m in PrescribedHerb.model_fields["grams"].metadata))
    for grams, expected in ((1, "PASS"), (10000, "PASS"), (5.0, "PASS"), (0, "QUANTITY_OUT_OF_RANGE"),
                            (-1, "QUANTITY_OUT_OF_RANGE"), (10001, "QUANTITY_OUT_OF_RANGE"),
                            (0.5, "QUANTITY_NOT_INTEGER"), (2.5, "QUANTITY_NOT_INTEGER"), (10000.5, "QUANTITY_NOT_INTEGER")):
        quantity = [r for r in _eval("Vata", [{"name": "Brahmi", "grams": grams}]).results if r.rule_id == "ENG-QUANTITY"]
        got = "PASS" if quantity[0].outcome.value == "PASS" else quantity[0].reason_code.value
        check(f"grams={grams!r} -> {expected}", got == expected, got)

    check("blank dosha -> REVIEW", _eval("   ", [{"name": "Brahmi", "grams": 1}]).outcome.value == "REVIEW")
    check("herb-name bound is read from PrescribedHerb (200)", MAX_HERB_NAME_LENGTH == 200 and any(
        getattr(m, "max_length", None) == MAX_HERB_NAME_LENGTH for m in PrescribedHerb.model_fields["name"].metadata))
    check(f"{MAX_HERB_NAME_LENGTH}-character herb name accepted",
          len(build_candidate("Vata", [{"name": "B" * MAX_HERB_NAME_LENGTH, "grams": 1}]).herbs) == 1)
    check(f"{MAX_HERB_NAME_LENGTH + 1}-character herb name rejected, as PrescribedHerb rejects it", _raises(
        lambda: build_candidate("Vata", [{"name": "B" * (MAX_HERB_NAME_LENGTH + 1), "grams": 1}]), InvalidCandidateError))


def test_inputs_the_confirm_path_accepts_are_evaluable():
    """Regression (Phase 3A review): the candidate must not be stricter than
    /prescribe/confirm. With no doctor correction, confirm commits the stored
    AI proposal, validated only by ClinicalParse, which bounds neither dosha
    length nor the number of herb lines."""
    from backend.guardrails.rules import FORMULARY
    from backend.schemas import ClinicalParse, ConfirmRequest
    from pydantic import ValidationError

    long_dosha = ("Vata-Pitta with secondary Kapha involvement; " * 6)[:250]
    proposal = ClinicalParse.model_validate({"dosha_imbalance": long_dosha,
                                             "prescribed_herbs": [{"name": "Brahmi", "grams": 5}]})
    check("confirm path: a 250-character AI dosha passes ClinicalParse", len(proposal.dosha_imbalance) == 250)
    try:
        ConfirmRequest.model_validate({"preview_id": "p", "dosha_imbalance": long_dosha})
        errors = []
    except ValidationError as e:
        errors = [(x["loc"], x["type"]) for x in e.errors()]
    check("(a doctor's correction is capped at 200, so the stored AI proposal is the route)",
          errors == [(("dosha_imbalance",), "string_too_long")], errors)
    r = _eval(proposal.dosha_imbalance, [h.model_dump() for h in proposal.prescribed_herbs])
    check("250-character dosha: evaluated, not raised; dosha kept whole in the fingerprinted candidate",
          r.outcome.value == "PASS" and len(r.results) == 4)

    herbs = sorted(FORMULARY)
    lines = [{"name": herbs[i % len(herbs)], "grams": 1} for i in range(250)]
    proposal = ClinicalParse.model_validate({"dosha_imbalance": "Vata", "prescribed_herbs": lines})
    check("confirm path: 250 AI herb lines pass ClinicalParse", len(proposal.prescribed_herbs) == 250)
    raw = _eval("Vata", [h.model_dump() for h in proposal.prescribed_herbs])
    check("250 unmerged lines: evaluated, not raised, and never PASS (duplicates -> REJECT)",
          raw.outcome.value == "REJECT"
          and {r.reason_code.value for r in raw.results if r.outcome.value != "PASS"} == {"DUPLICATE_HERB"})
    merged: dict[str, int] = {}
    for h in proposal.prescribed_herbs:
        merged[h.name] = merged.get(h.name, 0) + h.grams
    committed = _eval("Vata", [{"name": n, "grams": g} for n, g in merged.items()])
    check("the same 250 lines, merged as confirm commits them -> PASS", committed.outcome.value == "PASS")


# --- invalid inputs ------------------------------------------------------------------------------

def test_invalid_inputs_are_rejected_structurally():
    from backend.guardrails import ENGINEERING_BASELINE, InvalidCandidateError, build_candidate, evaluate
    cases = {
        "herbs not a list": ("Vata", "Ashwagandha 5g"),
        "herb not an object": ("Vata", ["Ashwagandha"]),
        "missing grams": ("Vata", [{"name": "Brahmi"}]),
        "extra key (e.g. patient identity)": ("Vata", [{"name": "Brahmi", "grams": 1, "patient_name": "x"}]),
        "grams as text": ("Vata", [{"name": "Brahmi", "grams": "5"}]),
        "grams as bool": ("Vata", [{"name": "Brahmi", "grams": True}]),
        "grams NaN": ("Vata", [{"name": "Brahmi", "grams": float("nan")}]),
        "grams infinite": ("Vata", [{"name": "Brahmi", "grams": float("inf")}]),
        "name not text": ("Vata", [{"name": 7, "grams": 1}]),
        "name null": ("Vata", [{"name": None, "grams": 1}]),
        "dosha not text": (["Vata"], [{"name": "Brahmi", "grams": 1}]),
    }
    for label, (dosha, herbs) in cases.items():
        check(f"invalid input rejected: {label}", _raises(lambda: build_candidate(dosha, herbs), InvalidCandidateError))
    check("evaluate() refuses a raw dict", _raises(
        lambda: evaluate({"dosha_imbalance": "Vata", "prescribed_herbs": []}, ENGINEERING_BASELINE), InvalidCandidateError))
    check("None herbs is an empty candidate (flagged by ENG-STRUCTURED, not an error)",
          build_candidate("Vata", None).herbs == ())


# --- unknown rules, rule sets and registration ------------------------------------------------

def test_unknown_rules_and_invalid_rulesets():
    from backend.guardrails import (ENGINEERING_BASELINE, InvalidRuleSetError, Rule, RuleCategory, RuleSet,
                                    UnknownRuleError, build_candidate, build_ruleset, evaluate)
    check("unknown rule id -> UnknownRuleError", _raises(lambda: build_ruleset("X", "1", ["NO-SUCH-RULE"]), UnknownRuleError))
    check("empty rule set -> InvalidRuleSetError", _raises(lambda: build_ruleset("X", "1", []), InvalidRuleSetError))
    check("repeated rule -> InvalidRuleSetError",
          _raises(lambda: build_ruleset("X", "1", ["ENG-QUANTITY", "ENG-QUANTITY"]), InvalidRuleSetError))
    check("missing id/version -> InvalidRuleSetError", _raises(lambda: build_ruleset("", "1", ["ENG-QUANTITY"]), InvalidRuleSetError))

    candidate = build_candidate("Vata", [{"name": "Brahmi", "grams": 1}])
    rogue = Rule("ENG-QUANTITY", 1, RuleCategory.ENGINEERING, "impostor", "none", lambda c, p: [])
    forged = RuleSet("ENGINEERING-BASELINE", "1", (rogue,), ENGINEERING_BASELINE.content_hash)
    check("an unregistered rule object is refused at evaluation", _raises(lambda: evaluate(candidate, forged), UnknownRuleError))
    tampered = dataclasses.replace(ENGINEERING_BASELINE, rules=ENGINEERING_BASELINE.rules[:1])
    check("a rule set whose content no longer matches its hash is refused",
          _raises(lambda: evaluate(candidate, tampered), InvalidRuleSetError))
    check("a non-RuleSet is refused", _raises(lambda: evaluate(candidate, ["ENG-QUANTITY"]), UnknownRuleError))


def test_registration_rules():
    from backend.guardrails import (Rule, RuleCategory, RuleRegistrationError, UnapprovedRuleError,
                                    register_rule, registered_rule)
    noop = lambda c, p: []  # noqa: E731
    check("duplicate rule id refused", _raises(lambda: register_rule(
        Rule("ENG-QUANTITY", 2, RuleCategory.ENGINEERING, "d", "s", noop)), RuleRegistrationError))
    check("registered definition unchanged by the refused duplicate", registered_rule("ENG-QUANTITY").version == 1)
    for version in (0, -1, True, "1"):
        check(f"version {version!r} refused", _raises(lambda: register_rule(
            Rule(f"T-{secrets.token_hex(4)}", version, RuleCategory.ENGINEERING, "d", "s", noop)), RuleRegistrationError))
    check("non-callable check refused", _raises(lambda: register_rule(
        Rule(f"T-{secrets.token_hex(4)}", 1, RuleCategory.ENGINEERING, "d", "s", None)), RuleRegistrationError))
    rid = f"T-CLIN-{secrets.token_hex(4)}"
    check("a CLINICAL rule without a recorded approval is refused", _raises(lambda: register_rule(
        Rule(rid, 1, RuleCategory.CLINICAL, "d", "", noop)), UnapprovedRuleError))
    check("... and never enters the registry", registered_rule(rid) is None)
    check("a CLINICAL rule with an approver but no source is refused", _raises(lambda: register_rule(
        Rule(rid, 1, RuleCategory.CLINICAL, "d", "", noop, approved_by="someone")), UnapprovedRuleError))


def test_rule_metadata_is_part_of_the_ruleset_hash():
    """Phase 3A B1: description, source and approved_by are part of the
    rule-set content hash. Each field is varied alone on one registered rule
    (same id, version, params and code), so only that field can explain a
    change. Whitespace-only clinical approval and non-text metadata are
    refused at registration; empty text on an ENGINEERING rule is accepted."""
    from backend.guardrails import (ENGINEERING_BASELINE, Rule, RuleCategory, RuleRegistrationError,
                                    UnapprovedRuleError, build_ruleset, register_rule, registered_rule)
    from backend.guardrails.engine import _content_hash

    base = registered_rule("ENG-QUANTITY")

    def digest(rule):
        return _content_hash("T-META", "1", [rule])

    reference = digest(base)
    check("identical inputs -> identical hash", digest(base) == reference
          and digest(dataclasses.replace(base)) == reference)
    for field_name, value in (("description", base.description + " "), ("source", "another source"),
                              ("approved_by", "Dr Example (synthetic)")):
        check(f"changing {field_name} changes the hash", digest(dataclasses.replace(base, **{field_name: value})) != reference)
    approved = dataclasses.replace(base, approved_by="Dr Example (synthetic)")
    check("different approvers -> different hashes",
          digest(approved) != digest(dataclasses.replace(base, approved_by="Dr Other (synthetic)")))
    check("rebuilding the baseline gives the same hash", build_ruleset(
        "ENGINEERING-BASELINE", "1", [r.rule_id for r in ENGINEERING_BASELINE.rules]).content_hash
        == ENGINEERING_BASELINE.content_hash)

    code = ("import dataclasses,sys; sys.path.insert(0, %r)\n"
            "from backend.guardrails import registered_rule\n"
            "from backend.guardrails.engine import _content_hash\n"
            "r = dataclasses.replace(registered_rule('ENG-QUANTITY'), approved_by='Dr Example (synthetic)')\n"
            "print(_content_hash('T-META', '1', [r]))" % ROOT)
    other = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT)
    check("a fresh process gives the identical hash for the same metadata",
          other.returncode == 0 and other.stdout.strip() == digest(approved), other.stderr[-300:])

    noop = lambda c, p: []  # noqa: E731
    # Existing behaviour preserved: an ENGINEERING rule needs no approval, and
    # empty text is accepted (it is still recorded in, and part of, the hash).
    for label, description, source, approved_by in (
            ("empty description", "", "s", None), ("blank source", "d", "  ", None), ("empty approved_by", "d", "s", "")):
        rule = register_rule(Rule(f"T-META-{secrets.token_hex(4)}", 1, RuleCategory.ENGINEERING,
                                  description, source, noop, approved_by=approved_by))
        check(f"ENGINEERING rule with {label} still registers", registered_rule(rule.rule_id) is rule)
    check('approved_by "" and None are recorded distinctly in the hash',
          digest(dataclasses.replace(base, approved_by="")) != reference)
    # Non-text metadata cannot be serialised deterministically -> refused.
    for label, kw in (("non-string description", {"description": None}), ("non-string source", {"source": 7}),
                      ("non-string approved_by", {"approved_by": 7})):
        rid = f"T-META-{secrets.token_hex(4)}"
        fields = {"description": "d", "source": "s", "approved_by": None, **kw}
        check(f"{label} -> RuleRegistrationError", _raises(lambda: register_rule(
            Rule(rid, 1, RuleCategory.ENGINEERING, fields["description"], fields["source"], noop,
                 approved_by=fields["approved_by"])), RuleRegistrationError))
        check("... and never enters the registry", registered_rule(rid) is None)
    # Clinical approval: whitespace-only or non-text is not an approval.
    for label, source, approved_by in (("blank approver", "s", "   "), ("blank source", " ", "Dr Example (synthetic)"),
                                       ("non-string approver", "s", 7)):
        rid = f"T-CLIN-{secrets.token_hex(4)}"
        check(f"CLINICAL rule with {label} -> UnapprovedRuleError", _raises(lambda: register_rule(
            Rule(rid, 1, RuleCategory.CLINICAL, "d", source, noop, approved_by=approved_by)), UnapprovedRuleError))
        check("... and never enters the registry", registered_rule(rid) is None)
    check("a CLINICAL rule with real approval metadata registers", register_rule(Rule(
        f"T-CLIN-{secrets.token_hex(4)}", 1, RuleCategory.CLINICAL, "d", "synthetic source",
        noop, approved_by="Dr Example (synthetic)")).category is RuleCategory.CLINICAL)


def test_code_fingerprint_covers_helpers_and_fails_closed():
    """Phase 3A B2: the code fingerprint covers helper functions a rule
    reaches (by global name or closure, in its own module or exactly
    backend.guardrails), and a rule whose source cannot be read cannot join
    a rule set (it used to be fingerprinted by its name alone)."""
    import importlib.util
    import tempfile
    from backend.guardrails import InvalidRuleSetError, Rule, RuleCategory, build_ruleset, register_rule
    from backend.guardrails.engine import _helpers, _source_sha256
    from backend.guardrails.rules import _present

    def load(directory, source, name=None):
        name = name or f"b2_{secrets.token_hex(4)}"
        path = os.path.join(directory, f"{name.replace('.', '_')}.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(source)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fingerprint(check_fn):
        return _source_sha256(Rule("T-B2", 1, RuleCategory.ENGINEERING, "d", "s", check_fn))

    chain = ("def _inner(c):\n    return {inner}\n\n"
             "def _helper(c):\n    return _inner(c)\n\n"
             "def check(c, p):\n    return _helper(c)\n")
    mutual = ("def _ping(c, n):\n    return [] if n <= 0 else _pong(c, n - 1)\n\n"
              "def _pong(c, n):\n    return {pong}\n\n"
              "def check(c, p):\n    return _ping(c, 3)\n")
    with tempfile.TemporaryDirectory() as work:
        original = load(work, chain.format(inner="[]"))
        copy = load(work, chain.format(inner="[]"))
        edited = load(work, chain.format(inner="list()"))
        check("identical code in two modules -> identical fingerprint", fingerprint(original.check) == fingerprint(copy.check))
        check("fingerprinting is stable", fingerprint(original.check) == fingerprint(original.check))
        check("a change two helpers deep changes the fingerprint", fingerprint(edited.check) != fingerprint(original.check))

        factory = load(work, "def make(inner):\n    def check(c, p):\n        return inner(c)\n    return check\n\n"
                             "def one(c):\n    return []\n\ndef two(c):\n    return list()\n")
        check("a helper captured in a closure is covered",
              fingerprint(factory.make(factory.one)) != fingerprint(factory.make(factory.two)))

        recursive = load(work, "def check(c, p):\n    return [] if c is None else check(None, p)\n")
        check("a recursive rule is fingerprinted (no endless loop)", len(fingerprint(recursive.check)) == 64)
        ping_pong = load(work, mutual.format(pong="_ping(c, n - 1)"))
        check("mutually recursive helpers: both covered, no endless loop",
              {f.__name__ for f in _helpers(ping_pong.check)} == {"_ping", "_pong"})
        ping_changed = load(work, mutual.format(pong="_ping(c, n - 2)"))
        check("... and a change in either one changes the fingerprint",
              fingerprint(ping_changed.check) != fingerprint(ping_pong.check))

        # Package boundary: a helper defined in backend.guardrails is followed
        # from any rule module; a look-alike module name is not.
        uses_package = load(work, "from backend.guardrails.rules import _present\n\n"
                                  "def check(c, p):\n    return [] if _present(c) else []\n")
        check("a backend.guardrails helper is followed", _present in _helpers(uses_package.check))
        inside = load(work, "def helper(c):\n    return []\n", name="backend.guardrails.b2_fake_submodule")
        lookalike = load(work, "def helper(c):\n    return []\n", name="backend.guardrails_b2_lookalike")
        caller = load(work, "def check(c, p):\n    return helper(c)\n")
        caller.helper = inside.helper
        check("a module inside backend.guardrails is in scope", _helpers(caller.check) == [inside.helper])
        caller.helper = lookalike.helper
        check("a look-alike module (backend.guardrails_x) is out of scope", _helpers(caller.check) == [])

    namespace = {}
    exec("def generated(c, p):\n    return []\n", namespace)

    class CallableCheck:
        def __call__(self, c, p):
            return []

    for label, check_fn in (("a dynamically generated function", namespace["generated"]), ("a callable object", CallableCheck())):
        rid = f"T-B2-{secrets.token_hex(4)}"
        register_rule(Rule(rid, 1, RuleCategory.ENGINEERING, "d", "s", check_fn))
        check(f"{label} registers, but cannot join a rule set (fails closed)",
              _raises(lambda: build_ruleset("T-B2", "1", [rid]), InvalidRuleSetError))


def test_rule_params_are_deeply_frozen():
    """Phase 3A B3: registration deep-freezes params (objects -> read-only
    mappings, lists -> tuples) as a full copy, so neither caller-held
    references nor the rule itself can change a registered rule. Frozen
    params serialise exactly as the plain JSON they came from, so rule-set
    fingerprints are unchanged."""
    import operator
    from types import MappingProxyType
    from backend.guardrails import (ENGINEERING_BASELINE, Finding, Outcome, ReasonCode, Rule, RuleCategory,
                                    build_candidate, build_ruleset, evaluate, register_rule)
    from backend.guardrails.engine import _content_hash

    def plain(value):
        if isinstance(value, MappingProxyType):
            return {k: plain(v) for k, v in value.items()}
        if isinstance(value, tuple):
            return [plain(v) for v in value]
        return value

    deep = [0]
    nested = {"allowed": ["Ashwagandha"], "limits": {"Brahmi": {"max": [1, 2]}}, "flag": True}
    rule = register_rule(Rule(f"T-B3-{secrets.token_hex(4)}", 1, RuleCategory.ENGINEERING, "d", "s",
                              lambda c, p: [], {"nested": nested, "deep": [[deep]]}))
    ruleset = build_ruleset("T-B3", "1", [rule.rule_id])
    candidate = build_candidate("Vata", [{"name": "Brahmi", "grams": 1}])

    # The failure from the A1 review: the caller edits its own nested objects after registration.
    nested["allowed"].append("Injected")
    nested["limits"]["Brahmi"]["max"].append(99)
    nested["limits"]["New"] = {}
    nested["flag"] = False
    deep.append(1)
    check("caller-held references cannot change the registered params", plain(rule.params) == {
        "nested": {"allowed": ["Ashwagandha"], "limits": {"Brahmi": {"max": [1, 2]}}, "flag": True}, "deep": [[[0]]]},
        plain(rule.params))
    check("... and a rule set using the rule still evaluates (no InvalidRuleSetError)",
          evaluate(candidate, ruleset).outcome is Outcome.PASS)

    params = rule.params
    attempts = (
        ("set a top-level key", lambda: operator.setitem(params, "x", 1), TypeError),
        ("set a key 2 levels deep", lambda: operator.setitem(params["nested"], "flag", False), TypeError),
        ("set a key 3 levels deep", lambda: operator.setitem(params["nested"]["limits"], "Brahmi", {}), TypeError),
        ("append to a nested list", lambda: params["nested"]["allowed"].append("x"), AttributeError),
        ("append to a list 4 levels deep", lambda: params["nested"]["limits"]["Brahmi"]["max"].append(3), AttributeError),
        ("assign into a nested list", lambda: operator.setitem(params["deep"][0], 0, None), TypeError),
    )
    for label, attempt, error in attempts:
        check(f"frozen params: cannot {label}", _raises(attempt, error))
    check("objects are read-only mappings, lists are tuples",
          isinstance(params["nested"], MappingProxyType) and isinstance(params["nested"]["allowed"], tuple))
    check("contents and key order are preserved", list(params["nested"]) == ["allowed", "limits", "flag"]
          and params["nested"]["limits"]["Brahmi"]["max"] == (1, 2))

    def mutating(c, p):
        p["bucket"].append(len(p["bucket"]))
        return [Finding(Outcome.REVIEW, ReasonCode.STRUCTURED_DOSHA_MISSING)] if len(p["bucket"]) > 1 else []

    mutator = register_rule(Rule(f"T-B3-{secrets.token_hex(4)}", 1, RuleCategory.ENGINEERING, "d", "s",
                                 mutating, {"bucket": []}))
    mutator_set = build_ruleset("T-B3-MUT", "1", [mutator.rule_id])
    first = evaluate(candidate, mutator_set)
    check("a rule that tries to mutate its params gets REVIEW / RULE_ERROR",
          [(r.outcome, r.reason_code) for r in first.results] == [(Outcome.REVIEW, ReasonCode.RULE_ERROR)])
    check("... and cannot change its own later results", evaluate(candidate, mutator_set) == first
          and mutator.params["bucket"] == ())

    unfrozen = dataclasses.replace(rule, params=plain(rule.params))
    check("frozen params hash exactly as the same plain JSON",
          _content_hash("T-B3", "1", [rule]) == _content_hash("T-B3", "1", [unfrozen]))
    plain_baseline = [dataclasses.replace(r, params=plain(r.params)) for r in ENGINEERING_BASELINE.rules]
    check("baseline fingerprint is unchanged by freezing (equal to its plain-params form)",
          _content_hash("ENGINEERING-BASELINE", "1", plain_baseline) == ENGINEERING_BASELINE.content_hash)
    check("baseline rebuilds with the same fingerprint", build_ruleset(
        "ENGINEERING-BASELINE", "1", [r.rule_id for r in ENGINEERING_BASELINE.rules]).content_hash
        == ENGINEERING_BASELINE.content_hash)
    check("baseline still evaluates a valid candidate to PASS", evaluate(candidate, ENGINEERING_BASELINE).outcome is Outcome.PASS)


def test_confirm_candidate_size_is_bounded_by_the_formulary():
    """Phase 3A B4 (deterministic): there is no size cap -- it would be
    stricter than /prescribe/confirm -- but a confirm candidate never has more
    lines than the formulary has herbs, however many lines are submitted."""
    from backend.guardrails import build_confirm_candidate
    from backend.guardrails.rules import FORMULARY
    from backend.herb_resolver import HERB_ALIASES

    aliases = sorted(HERB_ALIASES)
    for submitted in (len(aliases), 5_000):
        candidate = build_confirm_candidate("Vata", [{"name": aliases[i % len(aliases)], "grams": 1} for i in range(submitted)])
        check(f"{submitted} submitted lines merge to one line per formulary herb ({len(FORMULARY)})",
              len(candidate.herbs) == len(FORMULARY) and {h.name for h in candidate.herbs} == set(FORMULARY))


def test_evaluation_cost_smoke():
    """Phase 3A B4 (smoke alarm, not a guarantee): worst-case evaluation stays
    far inside generous budgets (about 10x-40x headroom over measurements on
    the development machine), catching gross regressions such as quadratic
    work on the unbounded dosha text. Medians of repeated runs limit noise."""
    import time
    from backend.guardrails import ENGINEERING_BASELINE, build_confirm_candidate, evaluate
    from backend.herb_resolver import HERB_ALIASES

    aliases = sorted(HERB_ALIASES)
    every_alias_many_times = [{"name": aliases[i % len(aliases)], "grams": 1} for i in range(5_000)]
    typical = build_confirm_candidate("Vata", every_alias_many_times)
    worst = build_confirm_candidate("V" * 1_000_000, every_alias_many_times)

    def median_seconds(fn, runs):
        times = []
        for _ in range(runs):
            start = time.perf_counter()
            fn()
            times.append(time.perf_counter() - start)
        return sorted(times)[runs // 2]

    check("worst confirm-path evaluation (every herb, 1,000,000-character dosha) < 100 ms",
          median_seconds(lambda: evaluate(worst, ENGINEERING_BASELINE), 5) < 0.1)
    check("typical evaluation < 10 ms", median_seconds(lambda: evaluate(typical, ENGINEERING_BASELINE), 21) < 0.01)
    check("building a candidate from 5,000 submitted lines < 300 ms",
          median_seconds(lambda: build_confirm_candidate("Vata", every_alias_many_times), 3) < 0.3)


def test_results_record_the_engine_fingerprint():
    """Phase 3A B5: every result records ENGINE_FINGERPRINT -- the engine
    tables plus the source of the code that interprets findings, found from
    evaluate() through functions and package-class methods (transitively).
    The tables are read-only, and evaluate() refuses (EngineChangedError) if
    a covered function or table was replaced after import."""
    import operator
    from backend.guardrails import ENGINEERING_BASELINE, Outcome, ReasonCode, build_candidate, build_ruleset, evaluate
    from backend.guardrails import candidate as candidate_module, engine, model, rules
    from backend.guardrails.engine import ENGINE_FINGERPRINT, EngineChangedError, _engine_fingerprint, _engine_scan

    sample = build_candidate("Vata", [{"name": "Brahmi", "grams": 1}])
    record = evaluate(sample, ENGINEERING_BASELINE).to_dict()
    check("every result records the engine fingerprint",
          record["engine_fingerprint"] == ENGINE_FINGERPRINT and len(ENGINE_FINGERPRINT) == 64)

    functions, tables, _ = _engine_scan()
    check("rescanning gives the same fingerprint", _engine_fingerprint(functions, tables) == ENGINE_FINGERPRINT)
    covered = set(functions)
    expected = {engine.evaluate, engine._run_rule, engine._valid_finding, engine._finding_order,  # the evaluation functions
                engine._content_hash, engine._thaw, engine._code_fingerprint, engine._source_sha256,  # helpers, transitively
                engine._helpers, engine._in_scope, rules.registered_rule, engine._engine_intact,
                rules.Rule.evaluate, model.Outcome.severity.fget,  # methods named by that code
                candidate_module.Candidate.fingerprint, candidate_module.Candidate.canonical_json,  # method -> method
                candidate_module._canonical_grams}  # method -> function (B6)
    check("covers the evaluation functions, their helpers and the methods they call, transitively",
          covered == expected, sorted(f.__qualname__ for f in covered ^ expected))
    check("does not cover result serialisation or the rules' own checks (the rule-set hash covers those)",
          not covered & {model.RuleResult.to_dict, model.EvaluationResult.to_dict, rules._formulary, rules._quantity,
                         engine.build_ruleset})
    check("the tables fingerprinted are the objects the engine reads",
          tables["_SEVERITY"] is model._SEVERITY and tables["ALLOWED_OUTCOMES"] is engine.ALLOWED_OUTCOMES
          and tables["REASON_DESCRIPTIONS"] is engine.REASON_DESCRIPTIONS)

    for name, key in (("_SEVERITY", Outcome.PASS), ("ALLOWED_OUTCOMES", ReasonCode.PASSED),
                      ("REASON_DESCRIPTIONS", ReasonCode.PASSED)):
        check(f"{name} is read-only", _raises(lambda: operator.setitem(tables[name], key, None), TypeError))

    def altered(name, key, value):
        return _engine_fingerprint(functions, {**tables, name: {**tables[name], key: value}})

    check("a different severity changes the fingerprint", altered("_SEVERITY", Outcome.REVIEW, 5) != ENGINE_FINGERPRINT)
    check("a different allowed outcome changes the fingerprint",
          altered("ALLOWED_OUTCOMES", ReasonCode.PASSED, frozenset({Outcome.REVIEW})) != ENGINE_FINGERPRINT)
    check("a different result text changes the fingerprint",
          altered("REASON_DESCRIPTIONS", ReasonCode.PASSED, "changed") != ENGINE_FINGERPRINT)
    check("different code for a covered helper changes the fingerprint", _engine_fingerprint(
        [f for f in functions if f is not engine._in_scope] + [engine._code_names], tables) != ENGINE_FINGERPRINT)

    code = ("import sys; sys.path.insert(0, %r)\n"
            "from backend.guardrails.model import ALLOWED_OUTCOMES, Outcome, ReasonCode\n"
            "from backend.guardrails.engine import _engine_fingerprint, _engine_scan\n"
            "functions, tables, _ = _engine_scan()\n"
            "many = {**ALLOWED_OUTCOMES, ReasonCode.PASSED: frozenset(Outcome)}\n"
            "print(_engine_fingerprint(functions, {**tables, 'ALLOWED_OUTCOMES': many}))" % ROOT)
    outputs = set()
    for seed in ("0", "1", "2", "3"):
        run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT,
                             env={**os.environ, "PYTHONHASHSEED": seed})
        outputs.add(run.stdout.strip() if run.returncode == 0 else "error: " + run.stderr[-200:])
    check("a multi-outcome table gives one fingerprint under 4 hash seeds (sets are sorted)",
          len(outputs) == 1 and len(next(iter(outputs))) == 64, outputs)

    for label, owner, name, replacement in (
            ("a covered helper is replaced", engine, "_finding_order", lambda f: (0,)),
            ("a table is rebound", model, "_SEVERITY", {Outcome.PASS: 2, Outcome.REVIEW: 1, Outcome.REJECT: 0}),
            ("a covered method is replaced", rules.Rule, "evaluate", lambda self, c: [])):
        original = vars(owner)[name]
        setattr(owner, name, replacement)
        try:
            check(f"evaluate() refuses when {label} after import",
                  _raises(lambda: evaluate(sample, ENGINEERING_BASELINE), EngineChangedError))
        finally:
            setattr(owner, name, original)
        check(f"... and evaluates normally once it is restored",
              evaluate(sample, ENGINEERING_BASELINE).to_dict() == record)
    check("the baseline rule-set fingerprint is unchanged", build_ruleset(
        "ENGINEERING-BASELINE", "1", [r.rule_id for r in ENGINEERING_BASELINE.rules]).content_hash
        == ENGINEERING_BASELINE.content_hash
        == "37c3fe7f19c8a28bf1998326bc9ce1cd48279720cfb2d3e6033de43302792baf")


def test_engine_guard_detects_code_mutation_and_descriptors():
    """Phase 3A B5 follow-up: (1) a scanned function whose __code__ is
    replaced in place keeps its identity, so the code object itself is
    captured and re-checked; (2) staticmethod and classmethod descriptors
    reached by name are discovered, the functions they wrap are fingerprinted,
    and replacing a descriptor is detected. Both make evaluation refuse
    (EngineChangedError) rather than report a stale fingerprint."""
    import importlib.util
    import tempfile
    from backend.guardrails import ENGINEERING_BASELINE, build_candidate, evaluate
    from backend.guardrails import engine, rules
    from backend.guardrails.engine import EngineChangedError, _engine_fingerprint, _engine_intact, _engine_scan

    sample = build_candidate("Vata", [{"name": "Brahmi", "grams": 1}])
    record = evaluate(sample, ENGINEERING_BASELINE).to_dict()
    check("every scanned function's code object is captured",
          {fn for fn, _ in engine._ENGINE_CODES} == set(engine._ENGINE_FUNCTIONS)
          and all(code is fn.__code__ for fn, code in engine._ENGINE_CODES))

    for label, fn, replacement in (("a covered helper", engine._finding_order, lambda f: (0,)),
                                   ("a covered method", rules.Rule.evaluate, lambda self, c: [])):
        original = fn.__code__
        fn.__code__ = replacement.__code__
        try:
            check(f"evaluate() refuses when {label}'s code is replaced in place",
                  _raises(lambda: evaluate(sample, ENGINEERING_BASELINE), EngineChangedError))
        finally:
            fn.__code__ = original
        check("... and evaluates normally once the code is restored", evaluate(sample, ENGINEERING_BASELINE).to_dict() == record)

    template = ("from backend.guardrails.model import _SEVERITY, ALLOWED_OUTCOMES, REASON_DESCRIPTIONS\n\n"
                "class Helper:\n"
                "    @staticmethod\n    def b5_static_part(x):\n        return {static}\n\n"
                "    @classmethod\n    def b5_class_part(cls, x):\n        return x\n\n"
                "def root(c):\n"
                "    return (_SEVERITY, ALLOWED_OUTCOMES, REASON_DESCRIPTIONS,\n"
                "            Helper.b5_static_part(c), Helper.b5_class_part(c))\n")

    def scan_fake(work, static_body):
        """A throwaway module inside the backend.guardrails namespace (so the
        scan treats its class as a package class), scanned from its root and
        then removed from sys.modules again."""
        name = f"backend.guardrails.b5_fake_{secrets.token_hex(4)}"
        path = os.path.join(work, name.replace(".", "_") + ".py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(template.format(static=static_body))
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
            return module, _engine_scan(module.root)
        finally:
            del sys.modules[name]

    with tempfile.TemporaryDirectory() as work:
        fake, (functions, tables, bindings) = scan_fake(work, "x")
        helper = fake.Helper
        static_descriptor, class_descriptor = vars(helper)["b5_static_part"], vars(helper)["b5_class_part"]
        check("staticmethod and classmethod are discovered; their wrapped functions are fingerprinted",
              {static_descriptor.__func__, class_descriptor.__func__} <= set(functions))
        check("... and the descriptors themselves are the bindings checked",
              all(any(bound_name == name and bound is obj for _, bound_name, bound in bindings)
                  for name, obj in (("b5_static_part", static_descriptor), ("b5_class_part", class_descriptor))))
        _, (other_functions, other_tables, _) = scan_fake(work, "[x]")
        check("a change inside a staticmethod changes the fingerprint",
              _engine_fingerprint(functions, tables) != _engine_fingerprint(other_functions, other_tables))

        codes = tuple((fn, fn.__code__) for fn in functions)
        check("the fake engine is intact as scanned", _engine_intact(bindings, codes))
        for attr_name, original, replacement in (("b5_static_part", static_descriptor, staticmethod(lambda x: 0)),
                                                 ("b5_class_part", class_descriptor, classmethod(lambda cls, x: 0))):
            setattr(helper, attr_name, replacement)
            try:
                check(f"replacing the {type(original).__name__} descriptor is detected", not _engine_intact(bindings, codes))
            finally:
                setattr(helper, attr_name, original)
            check("... and restoring it makes the engine intact again", _engine_intact(bindings, codes))
        wrapped = static_descriptor.__func__
        original = wrapped.__code__
        wrapped.__code__ = (lambda x: 0).__code__
        try:
            check("replacing a staticmethod's code in place is detected", not _engine_intact(bindings, codes))
        finally:
            wrapped.__code__ = original
        check("... and restoring it makes the engine intact again", _engine_intact(bindings, codes))
    check("the baseline rule-set fingerprint is unchanged",
          ENGINEERING_BASELINE.content_hash == "37c3fe7f19c8a28bf1998326bc9ce1cd48279720cfb2d3e6033de43302792baf")


def test_candidate_fingerprint_treats_whole_number_grams_alike():
    """Phase 3A B6: whole-number grams are fingerprinted as integers, so equal
    amounts written differently (5 / 5.0, 0 / -0.0) give one fingerprint, as
    they already compare and evaluate alike. Fractional values stay distinct,
    rules still see the original value, integer candidates keep their
    existing fingerprints, and the engine fingerprint (which covers this code)
    changes as expected."""
    from backend.guardrails import ENGINEERING_BASELINE, Outcome, ReasonCode, build_candidate, build_confirm_candidate, evaluate
    from backend.guardrails import candidate as candidate_module, engine
    from backend.guardrails.engine import ENGINE_FINGERPRINT

    def one_line(grams):
        return build_candidate("Vata", [{"name": "Brahmi", "grams": grams}])

    for whole, decimal in ((5, 5.0), (0, -0.0), (0, 0.0), (10**15, 1e15)):
        check(f"{whole!r} and {decimal!r} give one fingerprint", one_line(whole).fingerprint() == one_line(decimal).fingerprint())
        check("... and an identical evaluation record",
              evaluate(one_line(whole), ENGINEERING_BASELINE).to_dict() == evaluate(one_line(decimal), ENGINEERING_BASELINE).to_dict())
    check("fractional grams stay distinct (2.5 vs 2 and 3)",
          len({one_line(g).fingerprint() for g in (2.5, 2, 3)}) == 3 and '"Brahmi",2.5' in one_line(2.5).canonical_json())

    check("rules still see the original value: 5.0 is kept as given",
          one_line(5.0).herbs[0].grams == 5.0 and isinstance(one_line(5.0).herbs[0].grams, float))
    quantity = {r.rule_id: r for r in evaluate(one_line(2.5), ENGINEERING_BASELINE).results}["ENG-QUANTITY"]
    check("... 2.5 is still rejected as not a whole number of grams",
          (quantity.outcome, quantity.reason_code) == (Outcome.REJECT, ReasonCode.QUANTITY_NOT_INTEGER))
    check("... and 5.0 still passes", evaluate(one_line(5.0), ENGINEERING_BASELINE).outcome is Outcome.PASS)

    check("an integer candidate keeps its pre-B6 fingerprint", build_candidate(
        "Vata", [{"name": "Brahmi", "grams": 5}, {"name": "Triphala", "grams": 30}]).fingerprint()
        == "e79da498fb6d3235adf5002d166c72a7dcf9aaca506c58b7de557999ee426a91")
    confirm = build_confirm_candidate("Vata", [{"name": "Brahmi", "grams": 5}, {"name": "Triphala", "grams": 30}])
    check("confirm-path candidates (always integers) are unchanged",
          all(isinstance(h.grams, int) for h in confirm.herbs) and confirm.fingerprint() == "e79da498fb6d3235adf5002d166c72a7dcf9aaca506c58b7de557999ee426a91")
    check("the baseline rule-set fingerprint is unchanged",
          ENGINEERING_BASELINE.content_hash == "37c3fe7f19c8a28bf1998326bc9ce1cd48279720cfb2d3e6033de43302792baf")

    check("B5: _canonical_grams is in the engine's discovered coverage",
          candidate_module._canonical_grams in engine._ENGINE_FUNCTIONS)
    check("B5: the engine fingerprint changed from its pre-B6 value",
          ENGINE_FINGERPRINT != "3feb66e03197747ca708fd8abce984ee92e3e647e27da49f930ab7ccdfbc88fa")
    check("... and still matches a fresh scan", engine._engine_fingerprint(*engine._engine_scan()[:2]) == ENGINE_FINGERPRINT)


# --- error handling ---------------------------------------------------------------------------------

def test_a_failing_or_malformed_rule_is_isolated():
    from backend.guardrails import Finding, Outcome, ReasonCode, build_ruleset

    def boom(c, p):
        raise RuntimeError("secret detail that must not leak: Rahul 9876543210")

    raising = _test_rule(boom)
    malformed = {
        "returns None": _test_rule(lambda c, p: None),
        "returns a string": _test_rule(lambda c, p: "PASS"),
        "returns a non-Finding": _test_rule(lambda c, p: [("REJECT", "X")]),
        "line out of range": _test_rule(lambda c, p: [Finding(Outcome.REJECT, ReasonCode.DUPLICATE_HERB, 99)]),
        "line zero": _test_rule(lambda c, p: [Finding(Outcome.REJECT, ReasonCode.DUPLICATE_HERB, 0)]),
        "claims an engine reason code": _test_rule(lambda c, p: [Finding(Outcome.PASS, ReasonCode.RULE_ERROR)]),
        "outcome as plain text": _test_rule(lambda c, p: [Finding("PASS", ReasonCode.PASSED)]),
    }
    herbs = [{"name": "Brahmi", "grams": 1}]
    rs = build_ruleset("T-ERR", "1", ["ENG-STRUCTURED", raising.rule_id, "ENG-QUANTITY"])
    r = _eval("Vata", herbs, rs)
    check("a raising rule yields REVIEW RULE_ERROR, never PASS", _codes(r)[1] == (raising.rule_id, "REVIEW", "RULE_ERROR", None))
    check("neither the exception message nor its type is recorded (fixed description only)",
          r.results[1].detail == "The rule raised an exception; it was not evaluated."
          and "9876543210" not in json.dumps(r.to_dict()) and "RuntimeError" not in json.dumps(r.to_dict()))
    check("rules before and after still run", _codes(r)[0][2] == "PASSED" and _codes(r)[2][2] == "PASSED")
    check("aggregate becomes REVIEW", r.outcome.value == "REVIEW")
    for label, rule in malformed.items():
        r = _eval("Vata", herbs, build_ruleset("T-BAD", "1", [rule.rule_id]))
        check(f"malformed rule result ({label}) -> REVIEW RULE_INVALID_RESULT",
              _codes(r) == [(rule.rule_id, "REVIEW", "RULE_INVALID_RESULT", None)], _codes(r))


# --- independence and evaluation-only -------------------------------------------------------------

_ALLOWED_FIRST_PARTY = {"backend.guardrails", "backend.herb_resolver", "backend.schemas"}
_ALLOWED_THIRD_PARTY = {"pydantic"}


def _imports(path: str) -> set[str]:
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_guardrails_import_nothing_with_side_effects():
    stdlib = set(sys.stdlib_module_names)
    bad = {}
    for path in glob.glob(os.path.join(ROOT, "backend", "guardrails", "*.py")):
        for name in _imports(path):
            top = name.split(".")[0]
            ok = (top in stdlib and top not in {"sqlite3", "socket", "urllib", "http", "subprocess", "random"}) \
                or top in _ALLOWED_THIRD_PARTY or any(name == m or name.startswith(m + ".") for m in _ALLOWED_FIRST_PARTY)
            if not ok:
                bad.setdefault(os.path.basename(path), []).append(name)
    check("guardrail modules import only stdlib, pydantic and the allowed pure backend modules", not bad, bad)

    code = ("import sys; sys.path.insert(0, %r); import backend.guardrails\n"
            "bad=[m for m in sys.modules if m in ('sqlite3','backend.main','db.database','backend.inventory_service',"
            "'backend.audit_store','backend.preview_store','agents.copilot') or m.startswith('google.genai')]\n"
            "print(','.join(sorted(bad)))" % ROOT)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=ROOT)
    check("importing the package loads no database, Gemini, inventory, audit or app module",
          out.returncode == 0 and out.stdout.strip() == "", out.stdout + out.stderr[-300:])


def _module_level_imports(path: str) -> set[str]:
    """Imports executed when the module is imported: everything outside a
    function body (module level, including inside module-level if/try)."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = set()

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Import):
                names.update(a.name for a in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module:
                names.add(child.module)
            visit(child)

    visit(tree)
    return names


# Third-party, generated and non-production directories; everything else in the
# repository that is Python is production code and is scanned.
_NOT_PRODUCTION_DIRS = {".git", "venv", ".venv", "env", "node_modules", "__pycache__", ".pytest_cache", ".next",
                        "build", "dist", "out", "coverage", "artifacts", "logs", "tests"}


def _production_python_files() -> list[str]:
    """Every .py file in the repository outside _NOT_PRODUCTION_DIRS and
    outside backend/guardrails itself (the package may import itself)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in _NOT_PRODUCTION_DIRS)
        rel_dir = os.path.relpath(dirpath, ROOT)
        if rel_dir == os.path.join("backend", "guardrails") or rel_dir.startswith(os.path.join("backend", "guardrails") + os.sep):
            continue
        found.extend(os.path.normpath(os.path.join(rel_dir, f)) for f in sorted(filenames) if f.endswith(".py"))
    return found


def test_only_the_shadow_recorder_uses_guardrails_and_only_lazily():
    """Phase 3A.4 (decision 6) -- replaces "nothing in the request path uses
    guardrails yet". The evaluation-only boundary is now: only
    backend/shadow_store.py imports backend.guardrails, and only inside a
    function; only backend/main.py imports backend.shadow_store, and only
    inside a function. So importing the application never loads the engine,
    and no other production module can reach it. Every production Python file
    in the repository is scanned (migrations and scripts included), not a
    fixed list of directories."""
    def is_guardrails(name):
        return name == "backend.guardrails" or name.startswith("backend.guardrails.")

    guardrail_users, recorder_users, eager = [], [], []
    scanned = _production_python_files()
    for rel in scanned:
        path = os.path.join(ROOT, rel)
        every, module_level = _imports(path), _module_level_imports(path)
        if any(is_guardrails(n) for n in every):
            guardrail_users.append(rel)
        if "backend.shadow_store" in every:
            recorder_users.append(rel)
        if any(is_guardrails(n) for n in module_level) or "backend.shadow_store" in module_level:
            eager.append(rel)
    check("only backend/shadow_store.py imports backend.guardrails",
          guardrail_users == [os.path.join("backend", "shadow_store.py")], guardrail_users)
    check("only backend/main.py imports backend.shadow_store",
          recorder_users == [os.path.join("backend", "main.py")], recorder_users)
    check("neither import happens at module level (both are inside functions)", eager == [], eager)
    expected = {os.path.join(*p.split("/")) for p in (
        "backend/main.py", "backend/shadow_store.py", "agents/copilot.py", "agents/a0/orchestrator.py", "ui/app.py",
        "db/database.py", "db/migrations.py", "migrations/017_guardrail_shadow.py", "scripts/backup_db.py")}
    check("the scan covers every production package, migrations included", expected <= set(scanned),
          sorted(expected - set(scanned)))
    check("the scan excludes tests and the guardrail package's own internal imports",
          not any(r.startswith(("tests" + os.sep, os.path.join("backend", "guardrails") + os.sep)) for r in scanned))


# --- Phase 2 consistency --------------------------------------------------------------------------------

def test_what_confirm_would_commit_passes():
    from backend.guardrails.rules import FORMULARY
    from backend.herb_resolver import HERB_ALIASES, normalize_herb

    check("formulary equals every canonical name normalize_herb can produce", FORMULARY == set(HERB_ALIASES.values()))
    # The same normalise-then-merge deduct_and_prescribe applies before writing prescription_items.
    raw = [{"name": "ashwagandha", "grams": 5}, {"name": "Withania somnifera", "grams": 7}, {"name": "brahmi", "grams": 2}]
    merged: dict[str, int] = {}
    for h in raw:
        merged[normalize_herb(h["name"])] = merged.get(normalize_herb(h["name"]), 0) + h["grams"]
    committed = [{"name": n, "grams": g} for n, g in merged.items()]
    check("a confirm-shaped candidate (canonical, merged) -> PASS", _eval("Vata", committed).outcome.value == "PASS")
    check("every formulary herb passes at 1 g", all(
        _eval("Vata", [{"name": h, "grams": 1}]).outcome.value == "PASS" for h in sorted(FORMULARY)))


# --- Phase 3A.2 --------------------------------------------------------------------------------------

def test_result_text_is_privacy_safe():
    from backend.guardrails import ENGINEERING_BASELINE, build_candidate, build_ruleset, evaluate
    from backend.guardrails.model import REASON_DESCRIPTIONS, Finding, Outcome, ReasonCode

    identity = ["Rahul Varma", "9876543210", "OP-12345"]
    herbs = [{"name": "Rahul Varma", "grams": 9876543210}, {"name": "Rahul Varma", "grams": 2.5},
             {"name": "OP-12345", "grams": 0}, {"name": "Brahmi", "grams": 5}]
    r = evaluate(build_candidate("Vata -- Rahul Varma 9876543210", herbs), ENGINEERING_BASELINE)
    record = json.dumps(r.to_dict())
    check("identity typed into herb names, dosha or grams never reaches the result record",
          r.outcome.value == "REJECT" and not any(x in record for x in identity) and "2.5" not in record, record[:200])
    check("every result's text is the fixed description of its reason code",
          all(x.detail == REASON_DESCRIPTIONS[x.reason_code] for x in r.results))
    check("every reason code has a fixed description", set(REASON_DESCRIPTIONS) == set(ReasonCode))
    check("a finding has no free-text field a rule could fill",
          [f.name for f in dataclasses.fields(Finding)] == ["outcome", "reason_code", "line_no"])
    check("a rule cannot pass text (Finding rejects a detail argument)",
          _raises(lambda: Finding(Outcome.REJECT, ReasonCode.DUPLICATE_HERB, detail="Rahul"), TypeError))

    def echoes(c, p):
        raise ValueError(c.herbs[0].name)

    r = evaluate(build_candidate("Vata", herbs), build_ruleset("T-PRIV", "1", [_test_rule(echoes).rule_id]))
    check("a rule that raises with candidate text leaks nothing", "Rahul" not in json.dumps(r.to_dict()))
    record = json.dumps(evaluate(build_candidate("Rahul Varma", [{"name": "Brahmi", "grams": 1}]), ENGINEERING_BASELINE).to_dict())
    check("a passing candidate's dosha text is not in its record either", "Rahul" not in record)


def test_outcome_and_reason_code_must_agree():
    from backend.guardrails import ENGINEERING_BASELINE, build_candidate, build_ruleset, evaluate
    from backend.guardrails.model import ALLOWED_OUTCOMES, Finding, Outcome, ReasonCode

    check("every reason code has an allowed-outcome set", set(ALLOWED_OUTCOMES) == set(ReasonCode)
          and all(ALLOWED_OUTCOMES[c] for c in ReasonCode))
    check("PASSED is PASS-only; engine codes are REVIEW-only",
          ALLOWED_OUTCOMES[ReasonCode.PASSED] == {Outcome.PASS}
          and ALLOWED_OUTCOMES[ReasonCode.RULE_ERROR] == ALLOWED_OUTCOMES[ReasonCode.RULE_INVALID_RESULT] == {Outcome.REVIEW})
    herbs = [{"name": "Brahmi", "grams": 1}]
    for outcome, code in ((Outcome.PASS, ReasonCode.HERB_NOT_IN_FORMULARY), (Outcome.REJECT, ReasonCode.PASSED),
                          (Outcome.REVIEW, ReasonCode.QUANTITY_OUT_OF_RANGE), (Outcome.REJECT, ReasonCode.STRUCTURED_DOSHA_MISSING),
                          (Outcome.PASS, ReasonCode.STRUCTURED_HERBS_MISSING)):
        rule = _test_rule(lambda c, p, o=outcome, k=code: [Finding(o, k)])
        r = evaluate(build_candidate("Vata", herbs), build_ruleset("T-PAIR", "1", [rule.rule_id]))
        check(f"{outcome.value} with {code.value} -> REVIEW RULE_INVALID_RESULT",
              _codes(r) == [(rule.rule_id, "REVIEW", "RULE_INVALID_RESULT", None)], _codes(r))
    ok = _test_rule(lambda c, p: [Finding(Outcome.PASS, ReasonCode.PASSED), Finding(Outcome.REJECT, ReasonCode.DUPLICATE_HERB, 1)])
    r = evaluate(build_candidate("Vata", herbs), build_ruleset("T-PAIR-OK", "1", [ok.rule_id]))
    check("consistent pairs are accepted as given", [x.reason_code.value for x in r.results] == ["PASSED", "DUPLICATE_HERB"])
    samples = [("Vata", herbs), (None, []), ("  ", [{"name": "x", "grams": 0}, {"name": "x", "grams": 2.5}, {"name": "Neem", "grams": 99999}])]
    check("every built-in rule's results respect the allowed outcomes",
          all(x.outcome in ALLOWED_OUTCOMES[x.reason_code]
              for d, h in samples for x in evaluate(build_candidate(d, h), ENGINEERING_BASELINE).results))


def test_rule_params_must_be_serialisable_at_registration():
    from backend.guardrails import Rule, RuleCategory, RuleRegistrationError, register_rule, registered_rule

    def attempt(params):
        rid = f"T-PARAM-{secrets.token_hex(4)}"
        try:
            register_rule(Rule(rid, 1, RuleCategory.ENGINEERING, "d", "s", lambda c, p: [], params=params))
            return rid, None
        except RuleRegistrationError as e:
            return rid, str(e)

    for label, params in (("a set", {"s": {1, 2}}), ("a nested set", {"a": {"b": [1, {2}]}}), ("NaN", {"x": float("nan")}),
                          ("infinity", {"x": [float("inf")]}), ("a non-string key", {1: "x"}), ("a nested non-string key", {"a": {(1, 2): 1}}),
                          ("an object", {"o": object()}), ("bytes", {"b": b"x"}), ("a function", {"f": len}), ("not a mapping", [("a", 1)])):
        rid, error = attempt(params)
        check(f"params with {label} refused at registration", error is not None and registered_rule(rid) is None, error)
    rid, error = attempt({"s": "x", "n": 1, "f": 1.5, "b": True, "z": None, "l": [1, "a"], "t": (1, 2), "d": {"k": [{"x": 1}]}})
    check("plain JSON params (nested) register", error is None and registered_rule(rid) is not None, error)
    from backend.guardrails import build_ruleset
    check("... and build a rule set without error", len(build_ruleset("T-PARAM-OK", "1", [rid]).content_hash) == 64)


def test_invalid_candidate_errors_leak_no_input():
    """Phase 3A.2 privacy: an InvalidCandidateError carries no raw input in its
    message, repr, args, exception chain, formatted traceback or logged form.
    Synthetic identity markers only; no database."""
    import io
    import logging
    import traceback
    from backend.guardrails import InvalidCandidateError, build_candidate, build_confirm_candidate

    who, phone = "Synthetic Person", "9000012345"
    marker = f"{who} {phone}"
    long_name = (marker + " ") * 10
    cases = {
        # build_confirm_candidate: pydantic and herb-resolution failures
        "confirm: unknown herb named after a person": (build_confirm_candidate, "Vata", [{"name": marker, "grams": 1}]),
        "confirm: grams given as identity text": (build_confirm_candidate, "Vata", [{"name": "Brahmi", "grams": marker}]),
        "confirm: 0 g line under an identity name": (build_confirm_candidate, "Vata", [{"name": marker, "grams": 0}]),
        "confirm: over-long identity name": (build_confirm_candidate, "Vata", [{"name": long_name, "grams": 1}]),
        "confirm: identity as the herb list": (build_confirm_candidate, "Vata", marker),
        "confirm: identity inside a non-text dosha": (build_confirm_candidate, [marker], [{"name": "Brahmi", "grams": 1}]),
        # build_candidate: structural failures
        "candidate: grams given as identity text": (build_candidate, "Vata", [{"name": "Brahmi", "grams": marker}]),
        "candidate: extra key holding identity": (build_candidate, "Vata", [{"name": "Brahmi", "grams": 1, "patient": marker}]),
        "candidate: over-long identity name": (build_candidate, "Vata", [{"name": long_name, "grams": 1}]),
        "candidate: identity inside a non-text dosha": (build_candidate, {"who": marker}, []),
        "candidate: identity as a herb line": (build_candidate, "Vata", [marker]),
    }
    logger = logging.getLogger("ayurcore.test_guardrails.privacy")
    for label, (builder, dosha, herbs) in cases.items():
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        try:
            try:
                builder(dosha, herbs)
                raised = None
            except InvalidCandidateError as exc:
                raised = exc
                logger.exception("candidate rejected")  # what a 3B caller logging with exc_info would emit
        finally:
            logger.removeHandler(handler)
        check(f"[{label}] raises InvalidCandidateError", raised is not None)
        surfaces = [str(raised), repr(raised), repr(raised.args), "".join(traceback.format_exception(raised)), stream.getvalue()]
        check(f"[{label}] no identity in message, repr, args, traceback or log",
              not any(who in t or phone in t for t in surfaces), [t[:120] for t in surfaces if who in t or phone in t])
        check(f"[{label}] no chained exception to inspect",
              raised.__cause__ is None and (raised.__context__ is None or raised.__suppress_context__) and raised.__context__ is None)


def test_evaluation_with_identity_input_leaks_nothing_anywhere():
    """Serialized results and anything logged during evaluation carry no raw
    input, including when a rule raises with the input in its message."""
    import io
    import logging
    from backend.guardrails import ENGINEERING_BASELINE, build_candidate, build_ruleset, evaluate

    who, phone = "Synthetic Person", "9000012345"

    def raises_with_input(c, p):
        raise RuntimeError(f"{c.dosha_imbalance} {c.herbs[0].name} {c.herbs[0].grams}")

    candidate = build_candidate(f"Vata {who} {phone}", [{"name": f"{who} {phone}", "grams": int(phone)}, {"name": who, "grams": 2.5}])
    ruleset = build_ruleset("T-PRIV-LOG", "1", [r.rule_id for r in ENGINEERING_BASELINE.rules] + [_test_rule(raises_with_input).rule_id])
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        record = json.dumps(evaluate(candidate, ruleset).to_dict())
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    check("serialized result carries no identity (findings, details, rule-error path)",
          who not in record and phone not in record and "2.5" not in record, record[:160])
    check("nothing logged during evaluation carries identity", who not in stream.getvalue() and phone not in stream.getvalue())


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
