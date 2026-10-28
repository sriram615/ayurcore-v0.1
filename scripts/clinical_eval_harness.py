"""
CLINICAL EVALUATION HARNESS -- Step 1 (evaluation tooling only; changes no production code).

Runs one arm -- agents.copilot.parse_clinical_notes (control) or
agents.a0.propose (a0) -- over the case file eval/cases/clinical_eval_v1.json
and writes TWO artifacts per run:

    artifacts/clinical_eval/<stamp>_<arm>_<mode>_raw.json     what the parser returned / raised
    artifacts/clinical_eval/<stamp>_<arm>_<mode>_graded.json  deterministic grading of that raw file

Raw output is kept separate from grading on purpose: the grader is a pure
function of (case file, raw record), so any raw file can be regraded
offline, bit-for-bit, with --regrade.

    # offline self-check of the whole pipeline (scripted responses, no network)
    python scripts/clinical_eval_harness.py --arm control --selfcheck
    python scripts/clinical_eval_harness.py --arm a0 --selfcheck

    # regrade an existing raw file (offline, deterministic)
    python scripts/clinical_eval_harness.py --regrade artifacts/clinical_eval/<...>_raw.json

    # real evaluation (REAL, BILLED Gemini calls -- requires explicit --live)
    python scripts/clinical_eval_harness.py --arm control --live [--repeats N]

REUSE, NOT DUPLICATION
----------------------
Arm dispatch, tool-call counting, parser fingerprints and the self-check
scripted Gemini fakes all come from scripts/a0_baseline_harness.py, which is
imported (by path) and never modified. Like that harness, this one calls the
parser functions directly rather than POST /prescribe/preview, so it writes
nothing to the database and keeps the model's raw herb names.

GRADING RULES (see documentation/CLAUDE.md "Evaluation rules")
------------------------------------------------
  - Expected grams are TOTAL prescribed/dispensed grams. Frequency/duration
    are not represented by ClinicalParse and are never inferred.
  - Dosha is compared as a SET of {Vata, Pitta, Kapha}, never by substring.
  - Provider/system errors (AI_SERVICE_UNAVAILABLE), parse failures
    (INVALID_AI_SCHEMA) and harness errors are separate outcomes and are
    never counted as clinical misses.
  - Ambiguous cases (ambiguous=true) are never counted as pass or fail.
  - Safety annotations come only from the case file (clinician-supplied).
    This file contains no clinical rules of its own.
"""
from __future__ import annotations

import argparse
import contextlib
import contextvars
import functools
import hashlib
import importlib.util
import json
import logging
import os
import pathlib
import platform
import statistics
import sys
import time
from datetime import datetime, timezone

# First, not last: backend/, db/ and agents/ are namespace packages, so a checkout
# appended after PYTHONPATH could lose to another checkout's db/database.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.herb_resolver import UnknownHerbError, normalize_herb  # noqa: E402 - pure dict lookup, no DB

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_CASE_FILE = REPO_ROOT / "eval" / "cases" / "clinical_eval_v1.json"
OUTPUT_DIR = REPO_ROOT / "artifacts" / "clinical_eval"
BASELINE_HARNESS_PATH = REPO_ROOT / "scripts" / "a0_baseline_harness.py"
EVAL_COST_PATH = REPO_ROOT / "scripts" / "eval_cost.py"
HERB_RESOLVER_PATH = REPO_ROOT / "backend" / "herb_resolver.py"

CASE_SCHEMA_VERSION = "ayurcore.clinical_eval.cases/v1"
RAW_SCHEMA_VERSION = "ayurcore.clinical_eval.raw/v1"
GRADED_SCHEMA_VERSION = "ayurcore.clinical_eval.graded/v1"

ARMS = ("control", "a0")
CANONICAL_DOSHAS = ("Vata", "Pitta", "Kapha")
PROVENANCE_STATUSES = ("synthetic_unreviewed", "deidentified_unreviewed", "clinician_reviewed")
# Optional case-file-level class. A technical class can never be reported as
# clinical validation, whatever its cases' provenance says.
TECHNICAL_EVALUATION_CLASSES = ("technical_synthetic_unreviewed",)
NOT_CLINICAL_VALIDATION = "NOT CLINICAL VALIDATION"
SAFETY_ANNOTATION_TYPES = ("herb_must_not_be_proposed", "herb_max_total_grams", "requires_clinician_review")
GRAMS_MAX = 10000  # mirrors backend/schemas.py PrescribedHerb.grams le=10000

# Outcomes. Only PASS and CLINICAL_MISS are clinical grades.
PASS = "PASS"
CLINICAL_MISS = "CLINICAL_MISS"
AMBIGUOUS_REVIEW = "AMBIGUOUS_REVIEW"
PROVIDER_ERROR = "PROVIDER_ERROR"
PARSE_ERROR = "PARSE_ERROR"
HARNESS_ERROR = "HARNESS_ERROR"
ERROR_OUTCOMES = (PROVIDER_ERROR, PARSE_ERROR, HARNESS_ERROR)

# Spelling variants of the three dosha words only -- a transliteration table,
# not a clinical rule. Anything else in the model's dosha text is recorded as
# unrecognized and left for review; the raw string is always preserved.
_DOSHA_SPELLINGS = {
    "vata": "Vata", "vaata": "Vata", "vatha": "Vata", "vaatha": "Vata",
    "pitta": "Pitta", "pita": "Pitta", "pitha": "Pitta", "pittha": "Pitta",
    "kapha": "Kapha", "kafa": "Kapha", "kapham": "Kapha",
}

_LOG_TEXT_LIMIT = 4000


# --- Reuse of the existing A0 baseline harness -------------------------------

_baseline_module = None


def baseline_harness():
    """scripts/a0_baseline_harness.py, imported by path (scripts/ is not a
    package). Its top level only defines functions/classes, so importing it
    has no side effects."""
    global _baseline_module
    if _baseline_module is None:
        spec = importlib.util.spec_from_file_location("a0_baseline_harness", BASELINE_HARNESS_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _baseline_module = module
    return _baseline_module


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def sha256_json(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _display_path(path: pathlib.Path) -> str:
    path = pathlib.Path(path).resolve()
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# --- Case file ----------------------------------------------------------------

class CaseFileError(ValueError):
    """The case file violates its schema. Carries every problem found."""


def validate_case_file(data: dict) -> None:
    problems: list[str] = []

    if not isinstance(data, dict):
        raise CaseFileError(["case file must be a JSON object"])
    if data.get("schema_version") != CASE_SCHEMA_VERSION:
        problems.append(f"schema_version must be {CASE_SCHEMA_VERSION!r}")
    if "evaluation_class" in data and data["evaluation_class"] not in TECHNICAL_EVALUATION_CLASSES:
        problems.append(f"evaluation_class, when present, must be one of {TECHNICAL_EVALUATION_CLASSES}")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise CaseFileError(problems + ["cases must be a non-empty list"])

    seen_ids = set()
    for index, case in enumerate(cases):
        where = f"cases[{index}]"
        if not isinstance(case, dict):
            problems.append(f"{where}: must be an object")
            continue
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            problems.append(f"{where}: case_id must be a non-empty string")
        elif case_id in seen_ids:
            problems.append(f"{where}: duplicate case_id {case_id!r}")
        else:
            seen_ids.add(case_id)
            where = case_id

        if not isinstance(case.get("notes"), str) or not case["notes"].strip():
            problems.append(f"{where}: notes must be a non-empty string")

        provenance = case.get("provenance")
        if not isinstance(provenance, dict) or provenance.get("status") not in PROVENANCE_STATUSES:
            problems.append(f"{where}: provenance.status must be one of {PROVENANCE_STATUSES}")
        elif provenance["status"] == "clinician_reviewed":
            if not provenance.get("clinician_reviewer") or not provenance.get("reviewed_at"):
                problems.append(f"{where}: clinician_reviewed requires clinician_reviewer and reviewed_at")

        ambiguous = case.get("ambiguous")
        if not isinstance(ambiguous, bool):
            problems.append(f"{where}: ambiguous must be true or false")

        expected = case.get("expected")
        if not isinstance(expected, dict):
            problems.append(f"{where}: expected must be an object")
            expected = {}

        dosha = expected.get("dosha")
        if dosha is None:
            if ambiguous is not True:
                problems.append(f"{where}: expected.dosha may be null only when ambiguous=true")
        elif (not isinstance(dosha, list) or len(set(dosha)) != len(dosha)
              or any(d not in CANONICAL_DOSHAS for d in dosha)):
            problems.append(f"{where}: expected.dosha must be a list of distinct values from {CANONICAL_DOSHAS}")

        herbs = expected.get("herbs")
        if herbs is None:
            if ambiguous is not True:
                problems.append(f"{where}: expected.herbs may be null only when ambiguous=true")
        elif not isinstance(herbs, dict):
            problems.append(f"{where}: expected.herbs must be an object")
        else:
            for name, spec in herbs.items():
                try:
                    canonical = normalize_herb(name)
                except UnknownHerbError:
                    canonical = None
                if canonical != name:
                    problems.append(f"{where}: expected herb {name!r} is not a canonical clinic herb name")
                if not isinstance(spec, dict):
                    problems.append(f"{where}: expected herb {name!r} must be {{grams, tolerance_grams}}")
                    continue
                grams = spec.get("grams")
                if not isinstance(grams, int) or isinstance(grams, bool) or not 0 < grams <= GRAMS_MAX:
                    problems.append(f"{where}: {name!r} grams must be an integer in 1..{GRAMS_MAX}")
                tolerance = spec.get("tolerance_grams", 0)
                if not isinstance(tolerance, int) or isinstance(tolerance, bool) or tolerance < 0:
                    problems.append(f"{where}: {name!r} tolerance_grams must be a non-negative integer")

        not_stocked = case.get("mentioned_not_stocked", [])
        if not isinstance(not_stocked, list) or any(not isinstance(n, str) for n in not_stocked):
            problems.append(f"{where}: mentioned_not_stocked must be a list of strings")

        annotations = case.get("safety_annotations", [])
        if not isinstance(annotations, list):
            problems.append(f"{where}: safety_annotations must be a list")
            annotations = []
        annotation_ids = set()
        for a_index, ann in enumerate(annotations):
            a_where = f"{where}.safety_annotations[{a_index}]"
            if not isinstance(ann, dict):
                problems.append(f"{a_where}: must be an object")
                continue
            if not ann.get("id") or ann["id"] in annotation_ids:
                problems.append(f"{a_where}: id must be present and unique within the case")
            annotation_ids.add(ann.get("id"))
            if ann.get("source") != "clinician":
                problems.append(f"{a_where}: source must be 'clinician' (safety rules are never invented)")
            if not ann.get("annotated_by"):
                problems.append(f"{a_where}: annotated_by is required")
            kind = ann.get("type")
            if kind not in SAFETY_ANNOTATION_TYPES:
                problems.append(f"{a_where}: type must be one of {SAFETY_ANNOTATION_TYPES}")
            elif kind in ("herb_must_not_be_proposed", "herb_max_total_grams"):
                if not isinstance(ann.get("herb"), str) or not ann["herb"].strip():
                    problems.append(f"{a_where}: herb is required")
                if kind == "herb_max_total_grams":
                    max_grams = ann.get("max_grams")
                    if not isinstance(max_grams, int) or isinstance(max_grams, bool) or max_grams <= 0:
                        problems.append(f"{a_where}: max_grams must be a positive integer")
            elif kind == "requires_clinician_review" and not ann.get("description"):
                problems.append(f"{a_where}: description is required")

    if problems:
        raise CaseFileError(problems)


def load_case_file(path: pathlib.Path = DEFAULT_CASE_FILE) -> dict:
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    validate_case_file(data)
    return data


# --- Normalization (deterministic, offline) ----------------------------------

def parse_dosha_set(text) -> tuple[list[str], list[str]]:
    """Free-text dosha string -> (sorted canonical dosha set, unrecognized words).

    Word-based, never substring-based: "Vata-Pitta" -> [Pitta, Vata].
    Known limitation: negation ("not Kapha") is not understood; the raw
    string is preserved in the raw artifact for review.
    """
    if not isinstance(text, str):
        return [], []
    words = "".join(ch.lower() if ch.isalpha() else " " for ch in text).split()
    found, unrecognized = set(), []
    for word in words:
        if word in _DOSHA_SPELLINGS:
            found.add(_DOSHA_SPELLINGS[word])
        elif word.endswith("dosha") and word[: -len("dosha")] in _DOSHA_SPELLINGS:
            found.add(_DOSHA_SPELLINGS[word[: -len("dosha")]])
        else:
            unrecognized.append(word)
    return sorted(found, key=CANONICAL_DOSHAS.index), unrecognized


def resolve_proposed_herbs(prescribed_herbs: list[dict]) -> dict:
    """Maps each proposed entry through the same normalize_herb() boundary
    production applies at /prescribe/preview.

    Returns resolved (canonical -> grams, first occurrence wins), plus the
    entries that could not be resolved (hallucinated/unknown), duplicates of
    an already-seen canonical herb, and resolvable-but-non-canonical
    spellings (e.g. "turmeric" -> Haridra).
    """
    resolved: dict[str, int] = {}
    unresolved, duplicates, noncanonical = [], [], []
    for entry in prescribed_herbs:
        name, grams = entry.get("name"), entry.get("grams")
        try:
            canonical = normalize_herb(name)
        except (UnknownHerbError, AttributeError):
            unresolved.append({"name": name, "grams": grams})
            continue
        if canonical != name:
            noncanonical.append({"name": name, "canonical_name": canonical})
        if canonical in resolved:
            duplicates.append({"name": name, "canonical_name": canonical, "grams": grams})
            continue
        resolved[canonical] = grams
    return {
        "resolved": resolved,
        "unresolved": unresolved,
        "duplicates": duplicates,
        "noncanonical_spellings": noncanonical,
    }


# --- Error classification -----------------------------------------------------

def classify_error(raw: dict) -> str | None:
    """None for a successful parse; otherwise which of the three
    non-clinical outcomes this raw record is."""
    if raw.get("status") == "ok":
        return None
    exc = raw.get("exception") or {}
    if exc.get("type") == "ValueError" and exc.get("message") == "AI_SERVICE_UNAVAILABLE":
        return PROVIDER_ERROR
    if exc.get("type") == "ValueError" and exc.get("message") == "INVALID_AI_SCHEMA":
        return PARSE_ERROR
    return HARNESS_ERROR


def error_cause(raw: dict) -> str | None:
    """The most specific cause available: the innermost chained exception
    (e.g. the SDK's 429 behind AI_SERVICE_UNAVAILABLE), else the top one."""
    exc = raw.get("exception")
    if not exc:
        return None
    chain = exc.get("chain") or []
    innermost = chain[-1] if chain else exc
    cause = f"{innermost.get('type')}: {innermost.get('message')}"
    if not chain:
        cause += " (no chained cause; see log_records)"
    return cause


# --- Grading (pure, deterministic) --------------------------------------------

def _ratio(numerator: int, denominator: int):
    return None if denominator == 0 else round(numerator / denominator, 6)


def _grade_safety(case: dict, resolved: dict, proposed_raw_names: list[str]) -> list[dict]:
    """Evaluates the case's clinician-supplied annotations mechanically."""
    lowered_raw = {str(n).strip().lower() for n in proposed_raw_names}
    flags = []
    for ann in case.get("safety_annotations", []):
        kind = ann["type"]
        triggered, detail = None, None
        if kind in ("herb_must_not_be_proposed", "herb_max_total_grams"):
            herb = ann["herb"]
            try:
                target = normalize_herb(herb)
            except UnknownHerbError:
                target = None
            proposed = (target in resolved) if target else (herb.strip().lower() in lowered_raw)
            if kind == "herb_must_not_be_proposed":
                triggered = proposed
                detail = f"{herb} proposed" if proposed else None
            else:
                grams = resolved.get(target) if target else None
                triggered = grams is not None and grams > ann["max_grams"]
                detail = f"{herb} proposed at {grams}g > {ann['max_grams']}g" if triggered else None
        flags.append({
            "id": ann["id"],
            "type": kind,
            "severity": ann.get("severity"),
            "annotated_by": ann["annotated_by"],
            "triggered": triggered,
            "needs_clinician_review": kind == "requires_clinician_review",
            "detail": detail,
        })
    return flags


def grade_record(case: dict, raw: dict) -> dict:
    """Grades one raw record against its case. Pure: same inputs, same output."""
    base = {
        "case_id": case["case_id"],
        "repeat": raw.get("repeat", 0),
        "arm": raw.get("arm"),
        "provenance_status": case["provenance"]["status"],
        "ambiguous": case["ambiguous"],
        "latency_ms": raw.get("latency_ms"),
        "tool_call_count": len(raw.get("tool_calls") or []),
    }

    error_outcome = classify_error(raw)
    if error_outcome is not None:
        # Not a clinical grade: no missing herbs, no edits, no safety verdict.
        return {
            **base,
            "outcome": error_outcome,
            "counts_as_clinical_grade": False,
            "error_category": error_outcome,
            "error_cause": error_cause(raw),
            "dosha": None,
            "herbs": None,
            "hallucination": None,
            "safety": None,
            "doctor_edits": None,
        }

    parsed = raw["parsed"]
    proposed = parsed.get("prescribed_herbs") or []
    herbs_view = resolve_proposed_herbs(proposed)
    resolved = herbs_view["resolved"]
    expected = case["expected"]

    # Dosha (set-based).
    actual_dosha, unrecognized = parse_dosha_set(parsed.get("dosha_imbalance"))
    dosha = {
        "raw": parsed.get("dosha_imbalance"),
        "actual_set": actual_dosha,
        "unrecognized_words": unrecognized,
        "expected_set": None, "exact": None, "missing": None, "extra": None,
    }
    if expected.get("dosha") is not None:
        expected_set = sorted(expected["dosha"], key=CANONICAL_DOSHAS.index)
        dosha.update({
            "expected_set": expected_set,
            "exact": set(actual_dosha) == set(expected_set),
            "missing": [d for d in expected_set if d not in actual_dosha],
            "extra": [d for d in actual_dosha if d not in expected_set],
        })

    # Hallucination / unknown herbs: needs no ground truth, graded always.
    not_stocked = {n.strip().lower() for n in case.get("mentioned_not_stocked", [])}
    hallucination = {
        "unresolved_herbs": herbs_view["unresolved"],
        "mentioned_not_stocked_proposed": [
            e["name"] for e in proposed if str(e.get("name", "")).strip().lower() in not_stocked
        ],
        "duplicate_herbs": herbs_view["duplicates"],
        "noncanonical_spellings": herbs_view["noncanonical_spellings"],
        # backend/main.py raises 400 on the first herb normalize_herb rejects,
        # so the doctor would get no preview at all, not a partial one.
        "production_preview_would_be_rejected": bool(herbs_view["unresolved"]),
    }

    # Herbs and quantities.
    herbs = None
    if expected.get("herbs") is not None:
        expected_herbs = expected["herbs"]
        matched = [h for h in resolved if h in expected_herbs]
        missing = [h for h in expected_herbs if h not in resolved]
        unexpected = [h for h in resolved if h not in expected_herbs]
        quantities = {}
        for herb in matched:
            spec = expected_herbs[herb]
            tolerance = spec.get("tolerance_grams", 0)
            delta = resolved[herb] - spec["grams"]
            quantities[herb] = {
                "expected_grams": spec["grams"],
                "actual_grams": resolved[herb],
                "delta_grams": delta,
                "tolerance_grams": tolerance,
                "within_tolerance": abs(delta) <= tolerance,
            }
        tp = len(matched)
        fp = len(proposed) - tp  # every proposed entry that is not a match: unexpected, unresolved, duplicate
        fn = len(missing)
        herbs = {
            "expected": sorted(expected_herbs),
            "matched": matched,
            "missing": missing,
            "unexpected_resolved": unexpected,
            "tp": tp, "fp": fp, "fn": fn,
            "precision": _ratio(tp, tp + fp),
            "recall": _ratio(tp, tp + fn),
            "exact_set": fp == 0 and fn == 0,
            "quantities": quantities,
            "quantities_within_tolerance": sum(1 for q in quantities.values() if q["within_tolerance"]),
            "all_quantities_within_tolerance": all(q["within_tolerance"] for q in quantities.values()),
        }

    safety = _grade_safety(case, resolved, [e.get("name") for e in proposed])
    safety_triggered = sum(1 for f in safety if f["triggered"])

    # Minimum edits a doctor makes to turn this proposal into the expected one.
    doctor_edits = {
        "dosha_change": None if dosha["exact"] is None else int(not dosha["exact"]),
        "herb_additions": None if herbs is None else herbs["fn"],
        "herb_removals": None if herbs is None else herbs["fp"],
        "quantity_changes": None if herbs is None else sum(
            1 for q in herbs["quantities"].values() if not q["within_tolerance"]),
    }
    available = [v for v in doctor_edits.values() if v is not None]
    doctor_edits["total"] = sum(available)
    doctor_edits["complete"] = len(available) == 4

    if case["ambiguous"]:
        outcome = AMBIGUOUS_REVIEW
    elif (dosha["exact"] and herbs["exact_set"] and herbs["all_quantities_within_tolerance"]
          and safety_triggered == 0):
        outcome = PASS
    else:
        outcome = CLINICAL_MISS

    return {
        **base,
        "outcome": outcome,
        "counts_as_clinical_grade": outcome in (PASS, CLINICAL_MISS),
        "error_category": None,
        "error_cause": None,
        "dosha": dosha,
        "herbs": herbs,
        "hallucination": hallucination,
        "safety": {
            "flags": safety,
            "triggered_count": safety_triggered,
            "needs_clinician_review_count": sum(1 for f in safety if f["needs_clinician_review"]),
        },
        "doctor_edits": doctor_edits,
    }


def summarize(graded: list[dict], cases_by_id: dict, evaluation_class: str | None = None) -> dict:
    """Aggregates graded records. Clinical metrics use only PASS/CLINICAL_MISS
    records; error and ambiguous records are counted separately. A technical
    evaluation_class is never clinical-validation eligible."""
    outcomes = {o: 0 for o in (PASS, CLINICAL_MISS, AMBIGUOUS_REVIEW, *ERROR_OUTCOMES)}
    for g in graded:
        outcomes[g["outcome"]] += 1

    clinical = [g for g in graded if g["counts_as_clinical_grade"]]
    parsed_ok = [g for g in graded if g["error_category"] is None]
    tp = sum(g["herbs"]["tp"] for g in clinical)
    fp = sum(g["herbs"]["fp"] for g in clinical)
    fn = sum(g["herbs"]["fn"] for g in clinical)
    quantities = [q for g in clinical for q in g["herbs"]["quantities"].values()]
    latencies = [g["latency_ms"] for g in graded if g["latency_ms"] is not None]

    by_case: dict[str, set] = {}
    for g in graded:
        by_case.setdefault(g["case_id"], set()).add(g["outcome"])

    statuses = sorted({cases_by_id[g["case_id"]]["provenance"]["status"] for g in graded})
    eligible = bool(graded) and statuses == ["clinician_reviewed"] and evaluation_class is None
    tool_calls = [g["tool_call_count"] for g in graded]

    def _by_case(pick):
        return {f"{g['case_id']}#r{g['repeat']}": pick(g) for g in parsed_ok if pick(g)}

    return {
        "evaluation_class": evaluation_class,
        "attempts": len(graded),
        "distinct_cases": len(by_case),
        "outcomes": outcomes,
        "clinical": {
            "graded_attempts": len(clinical),
            "passed": outcomes[PASS],
            "pass_rate": _ratio(outcomes[PASS], len(clinical)),
            "dosha_exact": sum(1 for g in clinical if g["dosha"]["exact"]),
            "dosha_exact_rate": _ratio(sum(1 for g in clinical if g["dosha"]["exact"]), len(clinical)),
            "herb_tp": tp, "herb_fp": fp, "herb_fn": fn,
            "herb_micro_precision": _ratio(tp, tp + fp),
            "herb_micro_recall": _ratio(tp, tp + fn),
            "herb_exact_set": sum(1 for g in clinical if g["herbs"]["exact_set"]),
            "missing_herb_occurrences": fn,
            "unexpected_resolved_herb_occurrences": sum(len(g["herbs"]["unexpected_resolved"]) for g in clinical),
            "matched_quantities": len(quantities),
            "quantities_within_tolerance": sum(1 for q in quantities if q["within_tolerance"]),
            "mean_abs_quantity_delta_grams": (
                round(statistics.fmean(abs(q["delta_grams"]) for q in quantities), 6) if quantities else None),
            "doctor_edits_total": sum(g["doctor_edits"]["total"] for g in clinical),
            "doctor_edits_mean": (
                round(statistics.fmean(g["doctor_edits"]["total"] for g in clinical), 6) if clinical else None),
            "attempts_needing_no_edits": sum(1 for g in clinical if g["doctor_edits"]["total"] == 0),
            "safety_flags_triggered": sum(g["safety"]["triggered_count"] for g in clinical),
            "attempts_with_safety_flag_triggered": sum(1 for g in clinical if g["safety"]["triggered_count"]),
        },
        "all_successful_parses": {
            "attempts": len(parsed_ok),
            "unresolved_herb_occurrences": sum(len(g["hallucination"]["unresolved_herbs"]) for g in parsed_ok),
            "attempts_with_unresolved_herbs": sum(1 for g in parsed_ok if g["hallucination"]["unresolved_herbs"]),
            "attempts_proposing_mentioned_not_stocked": sum(
                1 for g in parsed_ok if g["hallucination"]["mentioned_not_stocked_proposed"]),
            "production_previews_that_would_be_rejected": sum(
                1 for g in parsed_ok if g["hallucination"]["production_preview_would_be_rejected"]),
            "safety_flags_needing_clinician_review": sum(
                g["safety"]["needs_clinician_review_count"] for g in parsed_ok),
        },
        "errors": {
            "provider_error": outcomes[PROVIDER_ERROR],
            "parse_error": outcomes[PARSE_ERROR],
            "harness_error": outcomes[HARNESS_ERROR],
            "error_rate": _ratio(sum(outcomes[o] for o in ERROR_OUTCOMES), len(graded)),
        },
        "details_by_attempt": {
            "missing_herbs": _by_case(lambda g: g["herbs"]["missing"] if g["herbs"] else None),
            "unexpected_resolved_herbs": _by_case(
                lambda g: g["herbs"]["unexpected_resolved"] if g["herbs"] else None),
            "gram_differences": _by_case(lambda g: {
                h: q["delta_grams"] for h, q in g["herbs"]["quantities"].items() if q["delta_grams"]
            } if g["herbs"] else None),
            "hallucinated_herbs": _by_case(
                lambda g: [e["name"] for e in g["hallucination"]["unresolved_herbs"]]),
            "errors": {f"{g['case_id']}#r{g['repeat']}": {"category": g["error_category"], "cause": g["error_cause"]}
                       for g in graded if g["error_category"]},
        },
        "latency_ms": {
            "median": statistics.median(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "total": sum(latencies),
        },
        "tool_calls": {
            "total": sum(tool_calls),
            "mean": round(statistics.fmean(tool_calls), 6) if tool_calls else None,
            "max": max(tool_calls) if tool_calls else None,
        },
        "cases_with_inconsistent_outcomes_across_repeats": sorted(
            case_id for case_id, seen in by_case.items() if len(seen) > 1),
        "provenance_statuses": statuses,
        "clinical_validation_eligible": eligible,
        "disclaimer": None if eligible else (
            f"{NOT_CLINICAL_VALIDATION}: "
            + (f"case file is classified {evaluation_class}. " if evaluation_class
               else "at least one graded case is not clinician_reviewed. ")
            + "Results are a technical check of the pipeline only."),
    }


def grade_raw_report(raw_report: dict, case_data: dict) -> list[dict]:
    cases_by_id = {c["case_id"]: c for c in case_data["cases"]}
    return [grade_record(cases_by_id[r["case_id"]], r) for r in raw_report["records"]]


def build_graded_report(raw_report: dict, case_data: dict, raw_path: str | None) -> dict:
    cases_by_id = {c["case_id"]: c for c in case_data["cases"]}
    graded = grade_raw_report(raw_report, case_data)
    summary = summarize(graded, cases_by_id, case_data.get("evaluation_class"))
    return {
        "schema_version": GRADED_SCHEMA_VERSION,
        "report_classification": summary["disclaimer"] or "clinical validation (clinician_reviewed cases only)",
        "raw_artifact": raw_path,
        "metadata": raw_report["metadata"],
        "grader": {
            "harness_sha256": sha256_file(pathlib.Path(__file__)),
            "herb_resolver_sha256": sha256_file(HERB_RESOLVER_PATH),
        },
        "summary": summary,
        "results": graded,
    }


# --- Running an arm -------------------------------------------------------------

class _LogCapture(logging.Handler):
    """Collects WARNING+ records from the parser loggers during one call, so
    the underlying error cause is recorded without touching parser code."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[dict] = []

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)
        self.records.append({
            "logger": record.name,
            "level": record.levelname,
            "message": message[:_LOG_TEXT_LIMIT],
        })


@contextlib.contextmanager
def _capture_parser_logs():
    handler = _LogCapture()
    loggers = [logging.getLogger("agents.copilot"), logging.getLogger("agents.a0")]
    for lg in loggers:
        lg.addHandler(handler)
    try:
        yield handler
    finally:
        for lg in loggers:
            lg.removeHandler(handler)


def _describe_exception(exc: BaseException) -> dict:
    chain, seen, current = [], {id(exc)}, exc.__cause__ or exc.__context__
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        chain.append({"type": type(current).__name__, "message": str(current)[:_LOG_TEXT_LIMIT]})
        current = current.__cause__ or current.__context__
    return {"type": type(exc).__name__, "message": str(exc)[:_LOG_TEXT_LIMIT], "chain": chain}


@contextlib.contextmanager
def _scripted_gemini(arm: str, payload: dict, tool_herbs: list[str]):
    """--selfcheck only: swaps google.genai.Client for the baseline harness's
    own scripted fakes and sets a dummy key, then restores both."""
    h = baseline_harness()
    import agents.copilot as copilot
    import google.genai as genai_real

    previous_key = os.environ.get("GEMINI_API_KEY")
    real_client = genai_real.Client
    os.environ["GEMINI_API_KEY"] = "selfcheck-key-not-a-real-credential"
    if arm == "control":
        class _ScriptedChats:
            def create(self, model, config):
                return h._ControlScriptedChat(copilot, payload, tool_herbs)

        class _ScriptedClient:
            def __init__(self, api_key):
                self.chats = _ScriptedChats()

        genai_real.Client = _ScriptedClient
    else:
        genai_real.Client = lambda api_key: h._A0ScriptedClient(api_key, tool_herbs, payload)
    try:
        yield
    finally:
        genai_real.Client = real_client
        if previous_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = previous_key


def _selfcheck_script(case: dict, index: int) -> tuple[dict, list[str]]:
    """Reuses the baseline harness's scripted payloads (exact, +5g dosage,
    dropped herb, invented herb) via a shim in its old case shape."""
    expected = case["expected"]
    shim = {
        "expected_herbs": ({h: s["grams"] for h, s in expected["herbs"].items()}
                           if expected.get("herbs") is not None else None),
        "expected_dosha_contains": "-".join(expected["dosha"]) if expected.get("dosha") else None,
    }
    return baseline_harness()._scripted_payload_for(shim, index)


def _stock_snapshot(clinic_id: str) -> dict:
    try:
        from backend.inventory_read import list_stock
        stock = dict(sorted(list_stock(clinic_id).items()))
        return {"stock": stock, "sha256": sha256_json(stock), "error": None}
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal
        return {"stock": None, "sha256": None, "error": f"{type(exc).__name__}: {exc}"}


def _settings_snapshot() -> dict:
    import agents.copilot as copilot
    import agents.a0.orchestrator as orch
    try:
        from importlib.metadata import version
        genai_version = version("google-genai")
    except Exception:  # noqa: BLE001
        genai_version = None
    return {
        "control": {
            "gemini_model": copilot.GEMINI_MODEL,
            "gemini_timeout_seconds": copilot.GEMINI_TIMEOUT_SECONDS,
            "gemini_total_budget_seconds": copilot.GEMINI_TOTAL_BUDGET_SECONDS,
        },
        "a0": {
            "gemini_model": orch.GEMINI_MODEL,
            "gemini_timeout_seconds": orch.GEMINI_TIMEOUT_SECONDS,
            "gemini_total_budget_seconds": orch.GEMINI_TOTAL_BUDGET_SECONDS,
            "max_tool_calls": orch.MAX_TOOL_CALLS,
            "max_reasoning_calls": orch.MAX_REASONING_CALLS,
        },
        "temperature": "fixed in parser source (covered by parser fingerprints)",
        "google_genai_version": genai_version,
        "python_version": platform.python_version(),
    }


# --- Usage recording (recording only) -------------------------------------------
#
# Every real Gemini request either arm makes -- Control's chat with automatic
# function calling (one request per tool round) and A0's own loop -- goes
# through google.genai.models.Models._generate_content. During a run that one
# method is wrapped: the response is returned unchanged and its usage_metadata
# (whatever the API reports) is appended to the current case's sink. The sink
# is a ContextVar, so Control's worker thread (agents/copilot.py runs
# send_message under contextvars.copy_context()) records into the right case.
# Nothing in agents/ or the prompts changes, and the original method is
# restored when the run ends. Cost is NOT computed here: see scripts/eval_cost.py.

PROVIDER = "Google Gemini API via the google-genai SDK"
USAGE_HOOK = "google.genai.models.Models._generate_content"
USAGE_FIELDS = ("prompt_token_count", "candidates_token_count", "thoughts_token_count",
                "tool_use_prompt_token_count", "cached_content_token_count", "total_token_count")
_USAGE_SINK: contextvars.ContextVar = contextvars.ContextVar("clinical_eval_usage_sink", default=None)


def _usage_entry(response) -> dict:
    """One API request's reported usage. Fields the API did not report stay None."""
    usage = getattr(response, "usage_metadata", None)
    entry = {field: getattr(usage, field, None) if usage is not None else None for field in USAGE_FIELDS}
    entry.update({"usage_metadata_present": usage is not None,
                  "model_version": getattr(response, "model_version", None),
                  "response_id": getattr(response, "response_id", None),
                  "failed": False, "error_type": None})
    return entry


@contextlib.contextmanager
def _record_usage(models_cls=None):
    """Wraps models_cls._generate_content (default: google.genai's Models) for
    the duration of the block; restores the original afterwards."""
    if models_cls is None:
        from google.genai import models as genai_models
        models_cls = genai_models.Models
    original = models_cls.__dict__["_generate_content"]

    @functools.wraps(original)
    def _recording_generate_content(self, *args, **kwargs):
        sink = _USAGE_SINK.get()
        try:
            response = original(self, *args, **kwargs)
        except BaseException as exc:
            if sink is not None:
                sink.append({**{f: None for f in USAGE_FIELDS}, "usage_metadata_present": False,
                             "model_version": None, "response_id": None,
                             "failed": True, "error_type": type(exc).__name__})
            raise
        if sink is not None:
            sink.append(_usage_entry(response))
        return response

    models_cls._generate_content = _recording_generate_content
    try:
        yield
    finally:
        models_cls._generate_content = original


def summarize_usage(calls: list[dict], mode: str) -> dict:
    """Per-record usage block. Totals sum only what the API reported; a field
    no call reported stays None (never 0)."""
    if mode == "selfcheck":
        status = "not_applicable_selfcheck"
    elif not calls:
        status = "no_api_calls_recorded"
    elif all(c["usage_metadata_present"] for c in calls):
        status = "recorded"
    else:
        status = "partial"
    totals = {}
    for field in USAGE_FIELDS:
        values = [c[field] for c in calls if c[field] is not None]
        totals[field] = sum(values) if values else None
    return {
        "status": status,
        "api_calls": len(calls),
        "failed_api_calls": sum(1 for c in calls if c["failed"]),
        "calls_without_usage_metadata": sum(1 for c in calls if not c["usage_metadata_present"]),
        "model_versions": sorted({c["model_version"] for c in calls if c["model_version"]}),
        "totals": totals,
        "calls": calls,
    }


def _eval_cost_module():
    spec = importlib.util.spec_from_file_location("eval_cost", EVAL_COST_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_own_checkout_database() -> None:
    """Refuses to run unless the database the parsers would read (stock for
    A0's context and the stock snapshot) is this checkout's own -- never
    another checkout's, whatever sys.path, PYTHONPATH or the working
    directory say."""
    import db.database as database
    db_path = pathlib.Path(database.DB_PATH).resolve()
    module_path = pathlib.Path(database.__file__).resolve()
    if REPO_ROOT not in db_path.parents or REPO_ROOT not in module_path.parents:
        raise RuntimeError(f"refusing to run: database {db_path} (from {module_path}) "
                           f"is not inside this checkout {REPO_ROOT}")


def run(arm: str, mode: str, case_file: pathlib.Path = DEFAULT_CASE_FILE, repeats: int = 1,
        wait_seconds: int = 0, output_dir: pathlib.Path = OUTPUT_DIR) -> dict | None:
    """Runs one arm over the case file. mode is 'selfcheck' (scripted, no
    network) or 'live' (real Gemini). Returns the graded report, or None if
    live mode was requested without a key."""
    if arm not in ARMS:
        raise ValueError(f"arm must be one of {ARMS}")
    if mode not in ("selfcheck", "live"):
        raise ValueError("mode must be 'selfcheck' or 'live'")
    if mode == "live" and not os.environ.get("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not set; refusing to run live mode.")
        return None
    if mode == "selfcheck":
        wait_seconds = 0

    assert_own_checkout_database()
    h = baseline_harness()
    import agents.copilot as copilot
    import agents.a0 as a0
    import agents.a0.orchestrator as a0_orchestrator
    from backend.pii_sanitizer import assert_no_pii_leak, tokenize_payload
    from db.database import DEFAULT_CLINIC_ID

    case_file = pathlib.Path(case_file)
    case_data = load_case_file(case_file)
    copilot_sha256, _ = h.copilot_fingerprint()
    a0_sha256, _ = h.a0_fingerprint()
    stock_start = _stock_snapshot(DEFAULT_CLINIC_ID)

    metadata = {
        "arm": arm,
        "mode": mode,
        "parser": ("agents/copilot.py::parse_clinical_notes" if arm == "control"
                   else "agents/a0/orchestrator.py::propose"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "case_file": _display_path(case_file),
        "case_file_sha256": sha256_file(case_file),
        "case_schema_version": case_data["schema_version"],
        "corpus_id": case_data.get("corpus_id"),
        "evaluation_class": case_data.get("evaluation_class"),
        "copilot_sha256": copilot_sha256,
        "a0_sha256": a0_sha256,
        "harness_sha256": sha256_file(pathlib.Path(__file__)),
        "baseline_harness_sha256": sha256_file(BASELINE_HARNESS_PATH),
        "herb_resolver_sha256": sha256_file(HERB_RESOLVER_PATH),
        "settings": _settings_snapshot(),
        "clinic_id": DEFAULT_CLINIC_ID,
        "stock_snapshot_start": stock_start,
        "repeats": repeats,
        "seconds_between_calls": wait_seconds,
        "provider": PROVIDER,
        "usage_recording": {"hook": USAGE_HOOK, "fields": list(USAGE_FIELDS),
                            "note": "recording only; cost is computed separately by scripts/eval_cost.py"},
    }

    print(f"clinical eval | arm={arm} mode={mode} cases={len(case_data['cases'])} repeats={repeats}")
    if case_data.get("evaluation_class"):
        print(f"  {NOT_CLINICAL_VALIDATION} -- evaluation_class={case_data['evaluation_class']}")
    records = []
    calls_made = 0
    with h._tool_counter_for(arm, copilot, a0_orchestrator) as counter, _record_usage():
        for repeat in range(repeats):
            for index, case in enumerate(case_data["cases"]):
                if calls_made and wait_seconds:
                    time.sleep(wait_seconds)
                calls_made += 1

                pii_values = ["Test Patient"]
                sanitized, _mapping = tokenize_payload(case["notes"], pii_values)
                assert_no_pii_leak(sanitized, pii_values)

                counter.reset()
                scripted = (_scripted_gemini(arm, *_selfcheck_script(case, index))
                            if mode == "selfcheck" else contextlib.nullcontext())
                record = {
                    "case_id": case["case_id"],
                    "repeat": repeat,
                    "arm": arm,
                    "notes_sha256": hashlib.sha256(case["notes"].encode("utf-8")).hexdigest(),
                    "status": None,
                    "parsed": None,
                    "exception": None,
                }
                request_id = f"{case['case_id']}#r{repeat}"
                usage_calls: list[dict] = []
                sink_token = _USAGE_SINK.set(usage_calls)
                started = time.monotonic()
                try:
                    with _capture_parser_logs() as logs, scripted:
                        try:
                            if arm == "control":
                                parsed = h._parse_control(copilot, sanitized, DEFAULT_CLINIC_ID)
                            else:
                                parsed = h._parse_a0(a0, sanitized, DEFAULT_CLINIC_ID, request_id)
                            record["status"] = "ok"
                            record["parsed"] = parsed.model_dump()
                        except Exception as exc:  # noqa: BLE001 - raw record keeps whatever happened
                            record["status"] = "exception"
                            record["exception"] = _describe_exception(exc)
                finally:
                    _USAGE_SINK.reset(sink_token)
                record["latency_ms"] = int((time.monotonic() - started) * 1000)
                record["usage"] = summarize_usage(list(usage_calls), mode)
                record["tool_calls"] = list(counter.calls)
                record["log_records"] = logs.records
                records.append(record)
                print(f"  {request_id:<24} {record['status']:<10} {record['latency_ms']:>7} ms")

    stock_end = _stock_snapshot(DEFAULT_CLINIC_ID)
    metadata["stock_snapshot_end_sha256"] = stock_end["sha256"]
    metadata["stock_changed_during_run"] = stock_start["sha256"] != stock_end["sha256"]

    raw_report = {"schema_version": RAW_SCHEMA_VERSION, "metadata": metadata, "records": records}

    output_dir = pathlib.Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = output_dir / f"{stamp}_{arm}_{mode}_raw.json"
    graded_path = output_dir / f"{stamp}_{arm}_{mode}_graded.json"
    raw_path.write_text(json.dumps(raw_report, indent=2, ensure_ascii=False), encoding="utf-8")

    graded_report = build_graded_report(raw_report, case_data, _display_path(raw_path))
    graded_path.write_text(json.dumps(graded_report, indent=2, ensure_ascii=False), encoding="utf-8")

    # Separate from grading: usage totals and cost (or "unavailable") in their own artifact.
    cost_path = output_dir / f"{stamp}_{arm}_{mode}_cost.json"
    cost_report = _eval_cost_module().cost_report_for(raw_report, _display_path(raw_path))
    cost_path.write_text(json.dumps(cost_report, indent=2, ensure_ascii=False), encoding="utf-8")
    graded_report["_paths"] = {"raw": str(raw_path), "graded": str(graded_path), "cost": str(cost_path)}

    _print_summary(graded_report)
    print(f"  cost   : {cost_report['summary']['cost_status']} (tokens: {cost_report['summary']['usage_totals']})")
    print(f"  raw    : {_display_path(raw_path)}")
    print(f"  graded : {_display_path(graded_path)}")
    print(f"  cost   : {_display_path(cost_path)}")
    return graded_report


def regrade(raw_path: pathlib.Path, case_file: pathlib.Path = DEFAULT_CASE_FILE,
            output_path: pathlib.Path | None = None) -> dict:
    """Regrades a raw artifact offline. Refuses if the case file changed
    since the raw run, so a regrade is always against the same ground truth."""
    raw_path, case_file = pathlib.Path(raw_path), pathlib.Path(case_file)
    raw_report = json.loads(raw_path.read_text(encoding="utf-8"))
    recorded = raw_report["metadata"]["case_file_sha256"]
    current = sha256_file(case_file)
    if recorded != current:
        raise CaseFileError([f"case file sha256 {current} does not match the raw run's {recorded}"])
    case_data = load_case_file(case_file)
    graded_report = build_graded_report(raw_report, case_data, _display_path(raw_path))
    if output_path is None:
        output_path = raw_path.with_name(raw_path.name.replace("_raw.json", "_regraded.json"))
    pathlib.Path(output_path).write_text(json.dumps(graded_report, indent=2, ensure_ascii=False), encoding="utf-8")
    _print_summary(graded_report)
    print(f"  regraded : {_display_path(pathlib.Path(output_path))}")
    return graded_report


def _print_summary(report: dict) -> None:
    s = report["summary"]
    c = s["clinical"]
    print("-" * 80)
    print(f"  arm={report['metadata']['arm']}  {report['report_classification']}")
    for g in report["results"]:
        print(f"  {g['case_id']:<18} r{g['repeat']:<3} {g['outcome']:<17} edits="
              f"{'-' if g['doctor_edits'] is None else g['doctor_edits']['total']}")
    print("-" * 80)
    print(f"  outcomes          : {s['outcomes']}")
    print(f"  clinical pass     : {c['passed']}/{c['graded_attempts']}  dosha exact {c['dosha_exact']}"
          f"  herb P/R {c['herb_micro_precision']}/{c['herb_micro_recall']}")
    print(f"  missing/unexpected: {c['missing_herb_occurrences']}/{c['unexpected_resolved_herb_occurrences']}"
          f"  quantities within tolerance {c['quantities_within_tolerance']}/{c['matched_quantities']}")
    print(f"  unresolved herbs  : {s['all_successful_parses']['unresolved_herb_occurrences']}"
          f"  errors {s['errors']}")
    print(f"  latency ms        : {s['latency_ms']}  tool calls {s['tool_calls']}")
    if s["disclaimer"]:
        print(f"  {s['disclaimer']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="AyurCore clinical evaluation harness (Control or A0)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--selfcheck", action="store_true", help="scripted responses, no network")
    mode.add_argument("--live", action="store_true", help="REAL, billed Gemini calls")
    mode.add_argument("--regrade", metavar="RAW_JSON", help="regrade an existing raw artifact offline")
    parser.add_argument("--arm", choices=ARMS, default="control")
    parser.add_argument("--cases", default=str(DEFAULT_CASE_FILE), help="case file path")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--wait", type=int, default=None,
                        help="seconds between live calls (default: the baseline harness's 65s)")
    args = parser.parse_args()

    if args.regrade:
        regrade(pathlib.Path(args.regrade), pathlib.Path(args.cases))
        return 0
    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    wait = args.wait if args.wait is not None else baseline_harness().DEFAULT_SECONDS_BETWEEN_CASES
    report = run(args.arm, "selfcheck" if args.selfcheck else "live", pathlib.Path(args.cases),
                 repeats=args.repeats, wait_seconds=wait)
    return 0 if report else 1


if __name__ == "__main__":
    sys.exit(main())
