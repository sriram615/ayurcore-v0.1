"""
Clinical evaluation grader -- offline, deterministic tests for
scripts/clinical_eval_harness.py and eval/cases/clinical_eval_v1.json.

No network and no live Gemini calls: grading tests use hand-built raw
records, and the end-to-end tests use --selfcheck mode (the baseline
harness's scripted Gemini fakes). The only database access is the same
read-only stock/alias lookups the existing A0 baseline self-check performs.

Run directly (no pytest required):
    python tests/test_clinical_eval_grader.py
"""
import copy
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "clinical_eval_harness", REPO_ROOT / "scripts" / "clinical_eval_harness.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def _case(**overrides):
    case = {
        "case_id": "t_01",
        "label": "test",
        "provenance": {"status": "synthetic_unreviewed"},
        "tags": [],
        "notes": "test note",
        "ambiguous": False,
        "expected": {
            "dosha": ["Vata", "Pitta"],
            "herbs": {
                "Ashwagandha": {"grams": 20, "tolerance_grams": 0},
                "Brahmi": {"grams": 15, "tolerance_grams": 2},
            },
        },
        "mentioned_not_stocked": [],
        "safety_annotations": [],
    }
    case.update(overrides)
    return case


def _ok(dosha, herbs, **extra):
    record = {
        "case_id": "t_01", "repeat": 0, "arm": "control", "status": "ok",
        "parsed": {"dosha_imbalance": dosha,
                   "prescribed_herbs": [{"name": n, "grams": g} for n, g in herbs]},
        "exception": None, "latency_ms": 10, "tool_calls": [], "log_records": [],
    }
    record.update(extra)
    return record


def _err(message, chain=None, exc_type="ValueError"):
    return {
        "case_id": "t_01", "repeat": 0, "arm": "a0", "status": "exception", "parsed": None,
        "exception": {"type": exc_type, "message": message, "chain": chain or []},
        "latency_ms": 5, "tool_calls": [], "log_records": [],
    }


def _file(cases):
    return {"schema_version": ev.CASE_SCHEMA_VERSION, "corpus_id": "t", "cases": cases}


def _expect_invalid(data, fragment):
    try:
        ev.validate_case_file(data)
    except ev.CaseFileError as exc:
        assert any(fragment in p for p in exc.args[0]), exc.args[0]
        return
    raise AssertionError(f"expected CaseFileError containing {fragment!r}")


# --- Case file -------------------------------------------------------------

def test_pilot_case_file_is_valid_and_marked_synthetic():
    data = ev.load_case_file(ev.DEFAULT_CASE_FILE)
    cases = data["cases"]
    assert len(cases) == 12
    assert len({c["case_id"] for c in cases}) == 12
    assert all(c["provenance"]["status"] == "synthetic_unreviewed" for c in cases)
    assert all(c["provenance"]["clinician_reviewer"] is None for c in cases)
    assert all(c["safety_annotations"] == [] for c in cases), "no invented safety rules in the pilot"
    assert "TOTAL" in data["conventions"]["expected_grams"]
    ambiguous = [c for c in cases if c["ambiguous"]]
    assert [c["case_id"] for c in ambiguous] == ["pilot_stress_08"]
    assert ambiguous[0]["expected"]["herbs"] is None


def test_pilot_cases_match_their_source_scripts_verbatim():
    h = ev.baseline_harness()
    stress = h.load_cases()
    ours = {c["case_id"]: c for c in ev.load_case_file(ev.DEFAULT_CASE_FILE)["cases"]}
    for i, src in enumerate(stress, start=1):
        case = ours[f"pilot_stress_{i:02d}"]
        assert case["notes"] == src["notes"]
        if src["expected_herbs"] is not None:
            assert {k: v["grams"] for k, v in case["expected"]["herbs"].items()} == src["expected_herbs"]


def test_case_file_fingerprint_is_stable_sha256():
    a = ev.sha256_file(ev.DEFAULT_CASE_FILE)
    assert a == ev.sha256_file(ev.DEFAULT_CASE_FILE) and len(a) == 64


def test_validation_rejects_bad_cases():
    _expect_invalid(_file([_case(provenance={"status": "real"})]), "provenance.status")
    _expect_invalid(_file([_case(provenance={"status": "clinician_reviewed"})]), "clinician_reviewer")
    _expect_invalid(_file([_case(), _case()]), "duplicate case_id")
    _expect_invalid(_file([_case(expected={"dosha": None, "herbs": {}})]), "only when ambiguous")
    _expect_invalid(_file([_case(expected={"dosha": ["Vata-Pitta"], "herbs": {}})]), "expected.dosha")
    _expect_invalid(_file([_case(expected={"dosha": ["Vata"], "herbs": {"turmeric": {"grams": 5}}})]),
                    "not a canonical")
    _expect_invalid(_file([_case(expected={"dosha": ["Vata"], "herbs": {
        "Haridra": {"grams": 5, "tolerance_grams": -1}}})]), "tolerance_grams")
    _expect_invalid(_file([_case(safety_annotations=[
        {"id": "s1", "type": "herb_must_not_be_proposed", "herb": "Kutki", "source": "model", "annotated_by": "x"}])]),
        "source must be 'clinician'")
    _expect_invalid(_file([_case(safety_annotations=[
        {"id": "s1", "type": "herb_must_not_be_proposed", "herb": "Kutki", "source": "clinician"}])]),
        "annotated_by")
    _expect_invalid(_file([_case(safety_annotations=[
        {"id": "s1", "type": "max_dose_rule", "source": "clinician", "annotated_by": "x"}])]), "type must be")
    ev.validate_case_file(_file([_case(ambiguous=True, expected={"dosha": None, "herbs": None})]))


# --- Dosha ----------------------------------------------------------------

def test_dosha_is_set_based_not_substring():
    assert ev.parse_dosha_set("Vata-Pitta")[0] == ["Vata", "Pitta"]
    assert ev.parse_dosha_set("pitta / VATA imbalance")[0] == ["Vata", "Pitta"]
    assert ev.parse_dosha_set("Vatadosha")[0] == ["Vata"]
    assert ev.parse_dosha_set(None) == ([], [])
    # The old substring grader passed this; the set grader must not.
    g = ev.grade_record(_case(expected={"dosha": ["Vata"], "herbs": {"Ashwagandha": {"grams": 20}}}),
                        _ok("Vata-Pitta", [("Ashwagandha", 20)]))
    assert g["outcome"] == ev.CLINICAL_MISS
    assert g["dosha"]["extra"] == ["Pitta"] and g["dosha"]["exact"] is False
    # Order-insensitive.
    g = ev.grade_record(_case(), _ok("Pitta-Vata", [("Ashwagandha", 20), ("Brahmi", 15)]))
    assert g["outcome"] == ev.PASS


# --- Herbs, quantities, edits -------------------------------------------------

def test_exact_pass_and_zero_edits():
    g = ev.grade_record(_case(), _ok("Vata-Pitta", [("Ashwagandha", 20), ("Brahmi", 15)]))
    assert g["outcome"] == ev.PASS and g["counts_as_clinical_grade"]
    assert g["herbs"]["precision"] == 1.0 and g["herbs"]["recall"] == 1.0
    assert g["doctor_edits"] == {"dosha_change": 0, "herb_additions": 0, "herb_removals": 0,
                                 "quantity_changes": 0, "total": 0, "complete": True}


def test_gram_tolerance_is_explicit():
    within = ev.grade_record(_case(), _ok("Vata Pitta", [("Ashwagandha", 20), ("Brahmi", 17)]))
    assert within["outcome"] == ev.PASS and within["herbs"]["quantities"]["Brahmi"]["delta_grams"] == 2
    outside = ev.grade_record(_case(), _ok("Vata Pitta", [("Ashwagandha", 21), ("Brahmi", 15)]))
    assert outside["outcome"] == ev.CLINICAL_MISS
    assert outside["herbs"]["quantities"]["Ashwagandha"]["within_tolerance"] is False
    assert outside["doctor_edits"]["quantity_changes"] == 1 and outside["doctor_edits"]["total"] == 1


def test_missing_and_unexpected_herbs_precision_recall():
    g = ev.grade_record(_case(), _ok("Vata-Pitta", [("Ashwagandha", 20), ("Triphala", 10), ("Neem", 5)]))
    h = g["herbs"]
    assert h["missing"] == ["Brahmi"] and h["unexpected_resolved"] == ["Triphala", "Neem"]
    assert (h["tp"], h["fp"], h["fn"]) == (1, 2, 1)
    assert abs(h["precision"] - 1 / 3) < 1e-6 and h["recall"] == 0.5
    assert g["doctor_edits"]["herb_additions"] == 1 and g["doctor_edits"]["herb_removals"] == 2
    assert g["doctor_edits"]["total"] == 3 and g["outcome"] == ev.CLINICAL_MISS


def test_hallucinated_and_not_stocked_herbs():
    case = _case(mentioned_not_stocked=["thulasi", "tulsi"])
    g = ev.grade_record(case, _ok("Vata Pitta", [("Ashwagandha", 20), ("Brahmi", 15), ("Tulsi", 10)]))
    hal = g["hallucination"]
    assert hal["unresolved_herbs"] == [{"name": "Tulsi", "grams": 10}]
    assert hal["mentioned_not_stocked_proposed"] == ["Tulsi"]
    assert hal["production_preview_would_be_rejected"] is True
    assert g["herbs"]["fp"] == 1 and g["outcome"] == ev.CLINICAL_MISS


def test_alias_spelling_resolves_and_duplicates_count_as_extra():
    case = _case(expected={"dosha": ["Kapha"], "herbs": {"Haridra": {"grams": 8, "tolerance_grams": 0}}})
    g = ev.grade_record(case, _ok("Kapha", [("turmeric", 8)]))
    assert g["outcome"] == ev.PASS
    assert g["hallucination"]["noncanonical_spellings"] == [{"name": "turmeric", "canonical_name": "Haridra"}]
    g = ev.grade_record(case, _ok("Kapha", [("Haridra", 8), ("Turmeric", 8)]))
    assert g["hallucination"]["duplicate_herbs"][0]["canonical_name"] == "Haridra"
    assert g["herbs"]["fp"] == 1 and g["outcome"] == ev.CLINICAL_MISS


# --- Error separation ---------------------------------------------------------

def test_provider_parse_and_harness_errors_are_not_clinical_misses():
    provider = ev.grade_record(_case(), _err("AI_SERVICE_UNAVAILABLE",
                                             chain=[{"type": "ClientError", "message": "429 RESOURCE_EXHAUSTED"}]))
    assert provider["outcome"] == ev.PROVIDER_ERROR and not provider["counts_as_clinical_grade"]
    assert provider["error_cause"] == "ClientError: 429 RESOURCE_EXHAUSTED"
    assert provider["herbs"] is None and provider["doctor_edits"] is None
    assert ev.grade_record(_case(), _err("INVALID_AI_SCHEMA"))["outcome"] == ev.PARSE_ERROR
    harness = ev.grade_record(_case(), _err("boom", exc_type="KeyError"))
    assert harness["outcome"] == ev.HARNESS_ERROR and "no chained cause" in harness["error_cause"]


# --- Ambiguous ------------------------------------------------------------------

def test_ambiguous_case_is_never_pass_or_fail():
    case = _case(ambiguous=True, expected={"dosha": ["Vata", "Pitta"], "herbs": None})
    g = ev.grade_record(case, _ok("Vata-Pitta", [("Ashwagandha", 22), ("Moonpetal Root", 3)]))
    assert g["outcome"] == ev.AMBIGUOUS_REVIEW and not g["counts_as_clinical_grade"]
    assert g["dosha"]["exact"] is True and g["herbs"] is None
    assert g["hallucination"]["unresolved_herbs"][0]["name"] == "Moonpetal Root"
    assert g["doctor_edits"]["complete"] is False


# --- Safety annotations -------------------------------------------------------

def test_clinician_safety_annotations_are_applied_mechanically():
    annotations = [
        {"id": "s1", "type": "herb_must_not_be_proposed", "herb": "Kutki", "source": "clinician",
         "annotated_by": "Dr. Reviewer (test fixture)", "severity": "critical"},
        {"id": "s2", "type": "herb_max_total_grams", "herb": "Ashwagandha", "max_grams": 20,
         "source": "clinician", "annotated_by": "Dr. Reviewer (test fixture)"},
        {"id": "s3", "type": "requires_clinician_review", "description": "check context",
         "source": "clinician", "annotated_by": "Dr. Reviewer (test fixture)"},
    ]
    case = _case(safety_annotations=annotations)
    ev.validate_case_file(_file([case]))

    clean = ev.grade_record(case, _ok("Vata-Pitta", [("Ashwagandha", 20), ("Brahmi", 15)]))
    assert clean["outcome"] == ev.PASS
    assert [f["triggered"] for f in clean["safety"]["flags"]] == [False, False, None]
    assert clean["safety"]["needs_clinician_review_count"] == 1

    # Case tolerance would not help here: Ashwagandha tolerance is 0 anyway,
    # and the Kutki flag fires even though Kutki is merely an extra herb.
    flagged = ev.grade_record(case, _ok("Vata-Pitta", [("Ashwagandha", 25), ("Brahmi", 15), ("Kutki", 5)]))
    assert [f["triggered"] for f in flagged["safety"]["flags"]] == [True, True, None]
    assert flagged["safety"]["triggered_count"] == 2 and flagged["outcome"] == ev.CLINICAL_MISS


def test_safety_flag_alone_prevents_pass():
    case = _case(expected={"dosha": ["Vata"], "herbs": {"Kutki": {"grams": 5}}}, safety_annotations=[
        {"id": "s1", "type": "herb_must_not_be_proposed", "herb": "Kutki", "source": "clinician",
         "annotated_by": "Dr. Reviewer (test fixture)"}])
    g = ev.grade_record(case, _ok("Vata", [("Kutki", 5)]))
    assert g["herbs"]["exact_set"] and g["safety"]["triggered_count"] == 1
    assert g["outcome"] == ev.CLINICAL_MISS


# --- Summary and determinism ------------------------------------------------------

def test_summary_separates_errors_and_is_deterministic():
    amb = _case(case_id="t_amb", ambiguous=True, expected={"dosha": None, "herbs": None})
    cases = {"t_01": _case(), "t_amb": amb}
    graded = [
        ev.grade_record(cases["t_01"], _ok("Vata-Pitta", [("Ashwagandha", 20), ("Brahmi", 15)])),
        ev.grade_record(cases["t_01"], dict(_ok("Vata", [("Ashwagandha", 20), ("Xyz", 1)]), repeat=1)),
        ev.grade_record(cases["t_01"], dict(_err("AI_SERVICE_UNAVAILABLE"), repeat=2)),
        ev.grade_record(amb, dict(_ok("Vata", [("Ashwagandha", 20)]), case_id="t_amb")),
    ]
    s = ev.summarize(graded, cases)
    assert s["outcomes"][ev.PASS] == 1 and s["outcomes"][ev.CLINICAL_MISS] == 1
    assert s["outcomes"][ev.PROVIDER_ERROR] == 1 and s["outcomes"][ev.AMBIGUOUS_REVIEW] == 1
    assert s["clinical"]["graded_attempts"] == 2 and s["clinical"]["pass_rate"] == 0.5
    # tp: 2 + 1, fp: 0 + 1 (Xyz), fn: 0 + 1 (Brahmi) -- the provider error adds no fn.
    assert (s["clinical"]["herb_tp"], s["clinical"]["herb_fp"], s["clinical"]["herb_fn"]) == (3, 1, 1)
    assert s["errors"]["provider_error"] == 1 and s["errors"]["error_rate"] == 0.25
    assert s["all_successful_parses"]["attempts_with_unresolved_herbs"] == 1
    assert s["cases_with_inconsistent_outcomes_across_repeats"] == ["t_01"]
    assert s["clinical_validation_eligible"] is False and "NOT CLINICAL VALIDATION" in s["disclaimer"]

    again = ev.summarize(copy.deepcopy(graded), cases)
    assert json.dumps(s, sort_keys=True) == json.dumps(again, sort_keys=True)


def test_clinician_reviewed_only_run_is_eligible():
    case = _case(provenance={"status": "clinician_reviewed", "clinician_reviewer": "x", "reviewed_at": "2026-09-26"})
    s = ev.summarize([ev.grade_record(case, _ok("Vata-Pitta", [("Ashwagandha", 20), ("Brahmi", 15)]))],
                     {"t_01": case})
    assert s["clinical_validation_eligible"] is True and s["disclaimer"] is None


# --- End-to-end self-check (no network) --------------------------------------------

def _selfcheck(arm):
    out = pathlib.Path(tempfile.mkdtemp(prefix="clinical_eval_test_"))
    report = ev.run(arm, "selfcheck", ev.DEFAULT_CASE_FILE, repeats=1, output_dir=out)
    raw_path = pathlib.Path(report["_paths"]["raw"])
    graded_path = pathlib.Path(report["_paths"]["graded"])
    return report, raw_path, graded_path


def test_selfcheck_end_to_end_both_arms_raw_separate_and_regrade_identical():
    previous_key = os.environ.get("GEMINI_API_KEY")
    for arm in ev.ARMS:
        report, raw_path, graded_path = _selfcheck(arm)
        assert raw_path != graded_path and raw_path.exists() and graded_path.exists()
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        assert raw["schema_version"] == ev.RAW_SCHEMA_VERSION
        assert "outcome" not in raw["records"][0], "raw records must not contain grades"
        assert all(r["status"] == "ok" for r in raw["records"])

        meta = raw["metadata"]
        assert meta["arm"] == arm and meta["mode"] == "selfcheck"
        assert meta["case_file_sha256"] == ev.sha256_file(ev.DEFAULT_CASE_FILE)
        for key in ("copilot_sha256", "a0_sha256", "herb_resolver_sha256", "settings",
                    "stock_snapshot_start", "stock_snapshot_end_sha256", "stock_changed_during_run"):
            assert key in meta, key
        assert meta["settings"]["a0"]["max_tool_calls"] == 6

        by_id = {g["case_id"]: g for g in report["results"]}
        assert len(by_id) == 12
        # Scripted perturbations from the baseline harness (index 1 dosage, 3 drop, 5 invented herb).
        assert by_id["pilot_simple_02"]["doctor_edits"]["quantity_changes"] == 1
        assert by_id["pilot_simple_04"]["herbs"]["missing"] == ["Ashwagandha"]
        assert by_id["pilot_stress_02"]["hallucination"]["unresolved_herbs"][0]["name"] == "Moonpetal Root"
        assert by_id["pilot_stress_08"]["outcome"] == ev.AMBIGUOUS_REVIEW
        assert by_id["pilot_simple_01"]["outcome"] == ev.PASS
        assert report["summary"]["errors"]["error_rate"] == 0.0
        assert report["summary"]["clinical_validation_eligible"] is False

        regraded = ev.regrade(raw_path, ev.DEFAULT_CASE_FILE, raw_path.with_name("regraded.json"))
        stored = json.loads(graded_path.read_text(encoding="utf-8"))
        assert json.dumps(regraded["results"], sort_keys=True) == json.dumps(stored["results"], sort_keys=True)
        assert json.dumps(regraded["summary"], sort_keys=True) == json.dumps(stored["summary"], sort_keys=True)
    assert os.environ.get("GEMINI_API_KEY") == previous_key, "selfcheck must restore the environment"


def test_regrade_refuses_changed_case_file():
    _report, raw_path, _graded = _selfcheck("control")
    tmp_cases = raw_path.with_name("cases_copy.json")
    data = json.loads(ev.DEFAULT_CASE_FILE.read_text(encoding="utf-8"))
    data["cases"][0]["label"] = "edited"
    tmp_cases.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        ev.regrade(raw_path, tmp_cases)
    except ev.CaseFileError as exc:
        assert "does not match" in exc.args[0][0]
        return
    raise AssertionError("regrade must refuse a changed case file")


def test_v1_is_classified_technical_and_never_clinical_validation():
    data = ev.load_case_file(ev.DEFAULT_CASE_FILE)
    assert data["evaluation_class"] == "technical_synthetic_unreviewed"
    assert "NOT CLINICAL VALIDATION" in data["report_label"]
    _expect_invalid(dict(_file([_case()]), evaluation_class="clinical_validated"), "evaluation_class")
    # Even an all-clinician_reviewed file is not eligible if classified technical.
    case = _case(provenance={"status": "clinician_reviewed", "clinician_reviewer": "x", "reviewed_at": "2026-09-26"})
    s = ev.summarize([ev.grade_record(case, _ok("Vata-Pitta", [("Ashwagandha", 20), ("Brahmi", 15)]))],
                     {"t_01": case}, "technical_synthetic_unreviewed")
    assert s["clinical_validation_eligible"] is False
    assert s["disclaimer"].startswith("NOT CLINICAL VALIDATION: case file is classified technical_synthetic_unreviewed")


def test_selfcheck_reports_every_required_metric_per_arm_without_a_verdict():
    for arm in ev.ARMS:
        report, raw_path, _graded = _selfcheck(arm)
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        assert raw["metadata"]["evaluation_class"] == "technical_synthetic_unreviewed"
        assert report["metadata"]["arm"] == arm
        assert report["report_classification"].startswith("NOT CLINICAL VALIDATION")
        s = report["summary"]
        c = s["clinical"]
        for key in ("dosha_exact", "dosha_exact_rate", "herb_micro_precision", "herb_micro_recall",
                    "missing_herb_occurrences", "unexpected_resolved_herb_occurrences",
                    "mean_abs_quantity_delta_grams"):
            assert key in c, key
        d = s["details_by_attempt"]
        assert d["gram_differences"] == {"pilot_simple_02#r0": {"Ashwagandha": 5}}
        assert d["missing_herbs"] == {"pilot_simple_04#r0": ["Ashwagandha"]}
        assert d["hallucinated_herbs"] == {"pilot_stress_02#r0": ["Moonpetal Root"]}
        assert d["errors"] == {}
        assert set(s["errors"]) == {"provider_error", "parse_error", "harness_error", "error_rate"}
        assert s["latency_ms"]["total"] >= 0 and s["tool_calls"]["total"] > 0
        assert not any(k in json.dumps(s).lower() for k in ("winner", "verdict", "better_arm"))


def test_live_mode_refuses_without_key():
    previous_key = os.environ.pop("GEMINI_API_KEY", None)
    try:
        assert ev.run("control", "live", ev.DEFAULT_CASE_FILE) is None
    finally:
        if previous_key is not None:
            os.environ["GEMINI_API_KEY"] = previous_key


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
        print(f"=> PASS: {name}")
    print(f"{len(tests)} passed")
