"""
Clinical evaluation v2 -- template and ground-truth protocol checks.

Validates eval/cases/clinical_eval_v2_template.json against
docs/CLINICAL_EVAL_GROUND_TRUTH.md, and -- whenever the real dataset
eval/cases/clinical_eval_v2.json exists -- validates that file too, so the
frozen dataset is re-checked on every test run.

Offline and deterministic: no Gemini, no database. Every "filled" case below
is a TEST FIXTURE built from the template's own case_template, not clinical
ground truth.

Run directly (no pytest required):
    python tests/test_clinical_eval_v2_template.py
"""
import copy
import hashlib
import importlib.util
import json
import os
import pathlib
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "eval" / "cases" / "clinical_eval_v2_template.json"
DATASET_PATH = REPO_ROOT / "eval" / "cases" / "clinical_eval_v2.json"
PROTOCOL_DOC = REPO_ROOT / "docs" / "CLINICAL_EVAL_GROUND_TRUTH.md"
REVIEW_FORM_DOC = REPO_ROOT / "docs" / "CLINICAL_CASE_REVIEW_FORM.md"

_spec = importlib.util.spec_from_file_location(
    "clinical_eval_harness", REPO_ROOT / "scripts" / "clinical_eval_harness.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

DISPOSITIONS = ("include", "include_ambiguous", "exclude")
FORBIDDEN_KEY_FRAGMENTS = ("frequency", "duration", "per_dose", "dose_per", "times_per", "schedule")


# --- The v2 protocol, as code (docs/CLINICAL_EVAL_GROUND_TRUTH.md) -------------

def canonical_cases_sha256(data: dict) -> str:
    """Section 7's freeze hash: cases + excluded_cases, canonical JSON."""
    payload = {"cases": data["cases"], "excluded_cases": data["excluded_cases"]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _has_placeholder(value) -> bool:
    if isinstance(value, str):
        return "<FILL" in value
    if isinstance(value, dict):
        return any(_has_placeholder(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_placeholder(v) for v in value)
    return False


def _keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


def _review_problems(case: dict, where: str, expected_placement: str) -> list[str]:
    problems = []
    review = case.get("review") or {}
    disposition = review.get("disposition")
    if disposition not in DISPOSITIONS:
        return [f"{where}: review.disposition must be one of {DISPOSITIONS}"]
    if expected_placement == "cases" and disposition == "exclude":
        problems.append(f"{where}: excluded case must be in excluded_cases, not cases")
    if expected_placement == "excluded_cases":
        if disposition != "exclude":
            problems.append(f"{where}: excluded_cases may only hold disposition=exclude")
        if not review.get("exclusion_reason"):
            problems.append(f"{where}: exclusion_reason is required")
    if disposition == "include" and case.get("ambiguous") is not False:
        problems.append(f"{where}: disposition=include requires ambiguous=false")
    if disposition == "include_ambiguous":
        if case.get("ambiguous") is not True:
            problems.append(f"{where}: disposition=include_ambiguous requires ambiguous=true")
        if not case.get("ambiguity_reason"):
            problems.append(f"{where}: ambiguity_reason is required for ambiguous cases")
    if not review.get("primary_reviewer"):
        problems.append(f"{where}: review.primary_reviewer is required")
    sign_off = review.get("sign_off") or {}
    for field in ("signed_by", "signed_at", "review_form_ref"):
        if not sign_off.get(field):
            problems.append(f"{where}: review.sign_off.{field} is required")
    return problems


def v2_protocol_problems(data: dict) -> list[str]:
    """Everything the protocol requires beyond the harness's own schema check."""
    problems = []
    if data.get("template_status"):
        problems.append("dataset still carries template_status (it is a template, not a dataset)")
    if not isinstance(data.get("cases"), list) or not isinstance(data.get("excluded_cases"), list):
        return problems + ["cases and excluded_cases must both be lists"]
    if _has_placeholder(data["cases"]) or _has_placeholder(data["excluded_cases"]):
        problems.append("unfilled <FILL ...> placeholders remain")
    bad_keys = sorted({k for k in _keys(data["cases"] + data["excluded_cases"])
                       if any(f in k.lower() for f in FORBIDDEN_KEY_FRAGMENTS)})
    if bad_keys:
        problems.append(f"frequency/duration-style fields are not allowed: {bad_keys}")

    ids = [c.get("case_id") for c in data["cases"] + data["excluded_cases"]]
    if len(ids) != len(set(ids)):
        problems.append("case_id values must be unique across cases and excluded_cases")

    for placement in ("cases", "excluded_cases"):
        for case in data[placement]:
            where = f"{placement}:{case.get('case_id')}"
            provenance = case.get("provenance") or {}
            if placement == "cases" and provenance.get("status") != "clinician_reviewed":
                problems.append(f"{where}: only clinician_reviewed cases may enter the clinical dataset")
            if provenance.get("origin") not in ("deidentified_real_consultation", "clinician_authored_realistic"):
                problems.append(f"{where}: provenance.origin must be a real de-identified or clinician-authored note")
            deid = case.get("deidentification") or {}
            for flag in ("direct_identifiers_removed", "dates_and_locations_removed_or_generalized",
                         "free_text_rechecked"):
                if deid.get(flag) is not True:
                    problems.append(f"{where}: deidentification.{flag} must be true")
            if not deid.get("performed_by") or not deid.get("checked_by") \
                    or deid.get("performed_by") == deid.get("checked_by"):
                problems.append(f"{where}: de-identification needs a different performed_by and checked_by")
            problems += _review_problems(case, where, placement)

    included = [c for c in data["cases"] if (c.get("review") or {}).get("disposition") == "include"]
    dataset = data.get("dataset") or {}
    if dataset.get("status") == "FROZEN":
        if not 30 <= len(included) <= 50:
            problems.append(f"a FROZEN dataset needs 30-50 include cases, found {len(included)}")
        if dataset.get("cases_sha256") != canonical_cases_sha256(data):
            problems.append("dataset.cases_sha256 does not match the case content (edited after freezing?)")
        for field in ("frozen_at", "frozen_by"):
            if not dataset.get(field):
                problems.append(f"dataset.{field} is required when FROZEN")
        lead = dataset.get("clinical_lead_sign_off") or {}
        if not lead.get("signed_by") or not lead.get("signed_at"):
            problems.append("clinical_lead_sign_off is required when FROZEN")
    elif dataset.get("status") != "DRAFT":
        problems.append("dataset.status must be DRAFT or FROZEN")
    return problems


def check_v2_dataset(data: dict) -> None:
    """Harness schema + v2 protocol. Raises with every problem found."""
    runnable = {k: v for k, v in data.items() if k != "excluded_cases"}
    try:
        ev.validate_case_file(runnable)
        problems = []
    except ev.CaseFileError as exc:
        problems = list(exc.args[0])
    problems += v2_protocol_problems(data)
    if problems:
        raise AssertionError("v2 dataset invalid:\n  " + "\n  ".join(problems))


# --- Fixtures (test-only values, NOT clinical ground truth) --------------------

def _template() -> dict:
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


def _fixture_case(template: dict, n: int, disposition: str = "include") -> dict:
    case = copy.deepcopy(template["case_template"])
    case["case_id"] = f"fixture_{n:03d}"
    case["label"] = "TEST FIXTURE -- not clinical data"
    case["provenance"].update({"origin": "clinician_authored_realistic", "source": "TEST",
                               "clinician_reviewer": "REV-TEST", "reviewed_at": "2026-01-01"})
    case["deidentification"] = {
        "performed_by": "P-TEST-1", "checked_by": "P-TEST-2", "checked_at": "2026-01-01",
        "direct_identifiers_removed": True, "dates_and_locations_removed_or_generalized": True,
        "free_text_rechecked": True,
    }
    case["notes"] = f"TEST FIXTURE NOTE {n}"
    case["expected"] = {"dosha": ["Vata"], "herbs": {"Ashwagandha": {"grams": 1, "tolerance_grams": 0}}}
    case["review"].update({"disposition": disposition, "primary_reviewer": "REV-TEST",
                           "sign_off": {"signed_by": "REV-TEST", "signed_at": "2026-01-01",
                                        "review_form_ref": f"FORM-fixture_{n:03d}"}})
    if disposition == "include_ambiguous":
        case["ambiguous"] = True
        case["ambiguity_reason"] = "TEST FIXTURE"
        case["expected"]["herbs"] = None
    if disposition == "exclude":
        case["review"]["exclusion_reason"] = "TEST FIXTURE"
    return case


def _fixture_dataset(n_include: int = 30, frozen: bool = False) -> dict:
    data = _template()
    data.pop("template_status")
    data["cases"] = [_fixture_case(data, i) for i in range(1, n_include + 1)]
    data["cases"].append(_fixture_case(data, 900, "include_ambiguous"))
    data["excluded_cases"] = [_fixture_case(data, 901, "exclude")]
    if frozen:
        data["dataset"].update({"status": "FROZEN", "frozen_at": "2026-01-01", "frozen_by": "LEAD-TEST",
                                "cases_sha256": canonical_cases_sha256(data)})
        data["dataset"]["clinical_lead_sign_off"].update({"signed_by": "LEAD-TEST", "signed_at": "2026-01-01"})
    return data


def _expect_problem(data: dict, fragment: str):
    try:
        check_v2_dataset(data)
    except AssertionError as exc:
        assert fragment in str(exc), f"expected {fragment!r} in:\n{exc}"
        return
    raise AssertionError(f"expected a problem containing {fragment!r}")


# --- Template -----------------------------------------------------------------

def test_template_is_not_runnable_and_carries_no_ground_truth():
    t = _template()
    assert t["template_status"] == "TEMPLATE_NOT_FOR_EVALUATION"
    assert t["cases"] == [] and t["excluded_cases"] == []
    try:
        ev.validate_case_file(t)
    except ev.CaseFileError:
        pass
    else:
        raise AssertionError("the harness must refuse to run the empty template")
    ct = t["case_template"]
    assert ct["expected"] == {"dosha": [], "herbs": {}}
    assert ct["safety_annotations"] == [] and ct["mentioned_not_stocked"] == []
    assert ct["provenance"]["status"] == "clinician_reviewed"
    assert t["dataset"]["status"] == "DRAFT" and t["dataset"]["cases_sha256"] is None


def test_template_preserves_total_grams_and_has_no_frequency_duration_fields():
    t = _template()
    assert "TOTAL" in t["conventions"]["expected_grams"]
    assert t["schema_version"] == ev.CASE_SCHEMA_VERSION
    bad = [k for k in _keys(t) if any(f in k.lower() for f in FORBIDDEN_KEY_FRAGMENTS)]
    # conventions.frequency_and_duration is the rule saying they are excluded, not a data field.
    assert bad == ["frequency_and_duration"], bad


def test_filled_template_fixture_passes_harness_and_protocol():
    check_v2_dataset(_fixture_dataset())
    check_v2_dataset(_fixture_dataset(frozen=True))


# --- Protocol rules ---------------------------------------------------------------

def test_unreviewed_or_synthetic_cases_cannot_enter_the_dataset():
    for status in ("synthetic_unreviewed", "deidentified_unreviewed"):
        data = _fixture_dataset()
        data["cases"][0]["provenance"]["status"] = status
        _expect_problem(data, "only clinician_reviewed")
    data = _fixture_dataset()
    data["cases"][0]["provenance"]["origin"] = "AI-written synthetic note"
    _expect_problem(data, "provenance.origin")


def test_the_v1_pilot_cases_are_rejected_by_the_v2_protocol():
    v1 = json.loads(ev.DEFAULT_CASE_FILE.read_text(encoding="utf-8"))
    data = _fixture_dataset()
    data["cases"].append(v1["cases"][0])
    _expect_problem(data, "only clinician_reviewed")


def test_sign_off_and_deidentification_are_required():
    data = _fixture_dataset()
    data["cases"][0]["review"]["sign_off"]["signed_by"] = None
    _expect_problem(data, "sign_off.signed_by")
    data = _fixture_dataset()
    data["cases"][0]["deidentification"]["free_text_rechecked"] = False
    _expect_problem(data, "free_text_rechecked")
    data = _fixture_dataset()
    data["cases"][0]["deidentification"]["checked_by"] = "P-TEST-1"
    _expect_problem(data, "different performed_by and checked_by")
    data = _fixture_dataset()
    data["cases"][0]["notes"] = "<FILL: de-identified note text>"
    _expect_problem(data, "placeholders")


def test_dispositions_must_match_placement_and_ambiguous_flag():
    data = _fixture_dataset()
    data["cases"][0]["review"]["disposition"] = "exclude"
    _expect_problem(data, "must be in excluded_cases")
    data = _fixture_dataset()
    data["excluded_cases"][0]["review"]["disposition"] = "include"
    _expect_problem(data, "may only hold disposition=exclude")
    data = _fixture_dataset()
    data["cases"][0]["ambiguous"] = True
    _expect_problem(data, "requires ambiguous=false")


def test_frequency_or_duration_fields_are_rejected():
    data = _fixture_dataset()
    data["cases"][0]["expected"]["herbs"]["Ashwagandha"]["frequency"] = "TDS"
    _expect_problem(data, "frequency/duration-style fields")


def test_freeze_hash_detects_any_edit_and_size_is_enforced():
    data = _fixture_dataset(frozen=True)
    data["cases"][3]["notes"] += " edited"
    _expect_problem(data, "cases_sha256 does not match")
    data = _fixture_dataset(frozen=True)
    data["excluded_cases"][0]["review"]["exclusion_reason"] = "changed"
    _expect_problem(data, "cases_sha256 does not match")
    small = _fixture_dataset(n_include=29, frozen=True)
    _expect_problem(small, "30-50 include cases")


def test_ambiguous_and_excluded_cases_never_count_as_pass_or_fail():
    data = _fixture_dataset(n_include=1)
    cases_by_id = {c["case_id"]: c for c in data["cases"]}
    graded = []
    for case in data["cases"]:
        herbs = [{"name": "Ashwagandha", "grams": 1}]
        raw = {"case_id": case["case_id"], "repeat": 0, "arm": "control", "status": "ok",
               "parsed": {"dosha_imbalance": "Vata", "prescribed_herbs": herbs},
               "exception": None, "latency_ms": 1, "tool_calls": [], "log_records": []}
        graded.append(ev.grade_record(case, raw))
    by_id = {g["case_id"]: g for g in graded}
    assert by_id["fixture_001"]["outcome"] == ev.PASS
    assert by_id["fixture_900"]["outcome"] == ev.AMBIGUOUS_REVIEW
    assert "fixture_901" not in by_id, "excluded cases are never in `cases`, so never graded"
    s = ev.summarize(graded, cases_by_id)
    assert s["clinical"]["graded_attempts"] == 1 and s["outcomes"][ev.AMBIGUOUS_REVIEW] == 1
    assert s["clinical_validation_eligible"] is True


def test_protocol_docs_state_the_key_rules():
    protocol = PROTOCOL_DOC.read_text(encoding="utf-8")
    for phrase in ("TOTAL prescribed/dispensed grams", "frequency or duration fields", "FROZEN",
                   "never edited", "clinician_reviewed", "excluded_cases", "Never** PASS/FAIL",
                   "De-identified"):
        assert phrase in protocol, phrase
    form = REVIEW_FORM_DOC.read_text(encoding="utf-8")
    for phrase in ("total prescribed/dispensed grams", "Do not record frequency or duration",
                   "Include as ambiguous", "Exclude", "Primary reviewer"):
        assert phrase in form, phrase


# --- The real dataset, once it exists --------------------------------------------

def test_real_v2_dataset_if_present_satisfies_the_protocol():
    if not DATASET_PATH.exists():
        print("   (eval/cases/clinical_eval_v2.json not created yet -- nothing to check)")
        return
    check_v2_dataset(json.loads(DATASET_PATH.read_text(encoding="utf-8")))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
        print(f"=> PASS: {name}")
    print(f"{len(tests)} passed")
