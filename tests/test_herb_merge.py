"""
One shared herb merge (backend/herb_resolver.merge_herb_lines) for
/prescribe/confirm, deduct_and_prescribe, the audit canonicalization and the
guardrail candidate (Phase 3A review: duplicate-herb mismatch).

Coverage:
  - the merge itself: aliases and duplicates summed, first-appearance order,
    empty input, unknown herbs, no mutation;
  - a single implementation: every former merge site calls it;
  - audit output is byte-identical to the previous implementation, so no
    event hash changes;
  - end to end on a TEMPORARY database: confirm's committed lines
    (response, prescription_items), the audit event and
    build_confirm_candidate agree;
  - quantity boundaries on merged totals (10000 / 10001 g) agree between
    confirm and guardrail evaluation; unknown herbs and invalid lines are
    refused by both and write nothing.

Run directly (no pytest required):
    python tests/test_herb_merge.py
"""
import contextlib
import inspect
import itertools
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


@contextlib.contextmanager
def _temp_app_db():
    """Points the whole application at a fresh temporary database (startup
    migrations run on it), then restores the real path. The live clinic
    database is never touched."""
    import db.database as database
    tmp = tempfile.mkdtemp(prefix="ayurcore_merge_")
    path = os.path.join(tmp, "app.db")
    saved = database.DB_PATH
    database.DB_PATH = path
    try:
        yield path
    finally:
        database.DB_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)


def _setup(client, db_path, stock: dict) -> tuple[str, dict]:
    from backend.auth_service import create_user
    clinic = f"merge_{secrets.token_hex(4)}"
    username = f"merge_test_{secrets.token_hex(6)}"
    password = "merge-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic, role="DOCTOR")
    conn = sqlite3.connect(db_path)
    try:
        for herb, grams in stock.items():
            conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                         "VALUES (?, ?, 'test botanical name', ?, 'TEST-BATCH', 90)", (clinic, herb, grams))
        conn.commit()
    finally:
        conn.close()
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return clinic, {"Authorization": f"Bearer {resp.json()['token']}"}


def _preview(client, headers, herbs, dosha="Vata") -> str:
    resp = client.post("/prescribe/manual-preview", headers=headers,
                       json={"patient_name": "Merge Patient", "dosha_imbalance": dosha, "prescribed_herbs": herbs})
    assert resp.status_code == 200, resp.text
    return resp.json()["preview_id"]


def _query(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _pairs(lines) -> list[tuple]:
    return [(h["name"], h["grams"]) if isinstance(h, dict) else (h.name, h.grams) for h in lines]


# --- the merge itself -------------------------------------------------------------

def test_merge_semantics():
    from backend.herb_resolver import UnknownHerbError, merge_herb_lines

    raw = [{"name": "Brahmi", "grams": 2}, {"name": "ashwagandha", "grams": 5},
           {"name": "Withania somnifera", "grams": 7}, {"name": "bacopa monnieri", "grams": 3}]
    snapshot = json.dumps(raw)
    merged = merge_herb_lines(raw)
    check("aliases resolve and duplicates sum, in first-appearance order",
          _pairs(merged) == [("Brahmi", 5), ("Ashwagandha", 12)], merged)
    check("input not mutated", json.dumps(raw) == snapshot)
    check("deterministic across calls", merge_herb_lines(raw) == merged)
    check("order follows first appearance, not name",
          _pairs(merge_herb_lines([{"name": "Triphala", "grams": 1}, {"name": "Amla", "grams": 1}]))
          == [("Triphala", 1), ("Amalaki", 1)])
    check("already-merged canonical lines are unchanged",
          merge_herb_lines([{"name": "Brahmi", "grams": 5}, {"name": "Ashwagandha", "grams": 12}]) == merged)
    check("empty input -> empty list", merge_herb_lines([]) == [])
    check("totals are not bounded here (callers check bounds)",
          _pairs(merge_herb_lines([{"name": "Brahmi", "grams": 6000}] * 2)) == [("Brahmi", 12000)])
    check("an unknown herb raises UnknownHerbError", _raises(
        lambda: merge_herb_lines([{"name": "Brahmi", "grams": 1}, {"name": "Unobtainium", "grams": 1}]), UnknownHerbError))


def test_single_implementation():
    import backend.audit_store as audit_store
    import backend.inventory_service as inventory_service
    import backend.main as main

    sites = {
        "inventory_service.deduct_and_prescribe": inspect.getsource(inventory_service.deduct_and_prescribe),
        "main.prescribe_confirm": inspect.getsource(main.prescribe_confirm),
        "audit_store._canonicalize_herbs": inspect.getsource(audit_store._canonicalize_herbs),
    }
    for name, source in sites.items():
        check(f"{name} uses merge_herb_lines", "merge_herb_lines(" in source)
        check(f"{name} has no summing merge loop of its own",
              ".get(norm_name, 0)" not in source and ".get(canon_name, 0)" not in source
              and ".get(herb[\"name\"], 0)" not in source, name)


# --- audit output unchanged -----------------------------------------------------------

def _previous_canonicalize(herbs):
    """The pre-refactor audit_store._canonicalize_herbs, verbatim."""
    from backend.herb_resolver import normalize_herb
    merged = {}
    for herb in herbs:
        canon_name = normalize_herb(herb["name"])
        merged[canon_name] = merged.get(canon_name, 0) + int(herb["grams"])
    return [{"name": name, "grams": merged[name]} for name in sorted(merged)]


def test_audit_canonicalization_is_byte_identical():
    from backend.audit_store import _canonicalize_herbs, build_prescription_confirmed_event_data

    base = [{"name": "Brahmi", "grams": 2}, {"name": "ashwagandha", "grams": 5}, {"name": "Withania somnifera", "grams": 7},
            {"name": "TRIPHALA", "grams": 30}, {"name": "amla", "grams": 4.0}, {"name": "Bacopa monnieri", "grams": 1}]
    cases = [list(p) for p in itertools.permutations(base[:4])] + [base, base[::-1], [], base[:1],
                                                                    [{"name": "Neem", "grams": 2.9}, {"name": "neem", "grams": 2.9}]]
    identical = all(json.dumps(_canonicalize_herbs(c), sort_keys=True, separators=(",", ":"))
                    == json.dumps(_previous_canonicalize(c), sort_keys=True, separators=(",", ":")) for c in cases)
    check(f"audit canonicalization byte-identical to the previous implementation ({len(cases)} cases)", identical)
    check("per-line int() kept before summing (2.9 + 2.9 -> 4, as before)",
          _canonicalize_herbs([{"name": "Neem", "grams": 2.9}, {"name": "neem", "grams": 2.9}]) == [{"name": "Neem", "grams": 4}])
    event = build_prescription_confirmed_event_data(
        is_manual=False, ai_dosha_imbalance="Vata", ai_prescribed_herbs=base,
        confirmed_dosha_imbalance="Vata", confirmed_prescribed_herbs=base[::-1])
    check("event data still sorts by name and computes was_changed on the merged set",
          event["doctor_decision"]["herbs"] == _previous_canonicalize(base) and event["was_changed"] is False, event)


# --- end to end on a temporary database -------------------------------------------------

def test_confirm_commits_exactly_what_the_candidate_holds():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.guardrails import ENGINEERING_BASELINE, build_confirm_candidate, evaluate
    from backend.herb_resolver import merge_herb_lines

    with _temp_app_db() as db_path, TestClient(main_module.app) as client:
        clinic, doctor = _setup(client, db_path, {"Brahmi": 1000, "Ashwagandha": 1000, "Triphala": 1000})
        for label, preview_herbs, override in (
            ("manual preview with duplicates", [{"name": "Brahmi", "grams": 2}, {"name": "ashwagandha", "grams": 5},
                                                {"name": "Withania somnifera", "grams": 7}, {"name": "bacopa monnieri", "grams": 3}], None),
            ("doctor override with duplicates", [{"name": "Triphala", "grams": 1}],
             [{"name": "triphala", "grams": 10}, {"name": "Brahmi", "grams": 4}, {"name": "TRIPHALA", "grams": 5}]),
        ):
            pid = _preview(client, doctor, preview_herbs)
            submitted = override if override is not None else preview_herbs
            resp = client.post("/prescribe/confirm", headers=doctor,
                               json={"preview_id": pid, **({"prescribed_herbs": override} if override else {})})
            check(f"[{label}] confirm succeeds", resp.status_code == 200, resp.text)
            rx = resp.json()["prescription_id"]
            committed = _pairs(resp.json()["prescribed_herbs"])
            items = [(r["herb_name"], r["grams"]) for r in _query(
                db_path, "SELECT herb_name, grams FROM prescription_items WHERE prescription_id = ? ORDER BY line_no", (rx,))]
            candidate = build_confirm_candidate("Vata", submitted)
            check(f"[{label}] response == prescription_items == merge_herb_lines == build_confirm_candidate",
                  committed == items == _pairs(merge_herb_lines(submitted)) == _pairs(candidate.herbs),
                  (committed, items, _pairs(candidate.herbs)))
            check(f"[{label}] the committed candidate evaluates PASS",
                  evaluate(candidate, ENGINEERING_BASELINE).outcome.value == "PASS")
            audited = json.loads(_query(db_path, "SELECT event_data_json FROM audit_events WHERE prescription_id = ?",
                                        (rx,))[0]["event_data_json"])["doctor_decision"]["herbs"]
            check(f"[{label}] audit event holds the same merged lines, sorted by name",
                  _pairs(audited) == sorted(committed), audited)


def test_quantity_boundaries_agree_between_confirm_and_evaluation():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    from backend.guardrails import ENGINEERING_BASELINE, InvalidCandidateError, build_confirm_candidate, evaluate

    with _temp_app_db() as db_path, TestClient(main_module.app) as client:
        clinic, doctor = _setup(client, db_path, {"Brahmi": 50000})

        def confirm_and_evaluate(override):
            pid = _preview(client, doctor, [{"name": "Brahmi", "grams": 1}])
            before = _query(db_path, "SELECT COUNT(*) AS n FROM prescriptions")[0]["n"]
            resp = client.post("/prescribe/confirm", headers=doctor, json={"preview_id": pid, "prescribed_herbs": override})
            wrote = _query(db_path, "SELECT COUNT(*) AS n FROM prescriptions")[0]["n"] - before
            try:
                verdict = evaluate(build_confirm_candidate("Vata", override), ENGINEERING_BASELINE)
                verdict = (verdict.outcome.value, [r.reason_code.value for r in verdict.results if r.outcome.value != "PASS"])
            except InvalidCandidateError:
                verdict = ("INVALID", [])
            return resp.status_code, wrote, verdict

        cases = [
            ("two lines merging to exactly 10000 g", [{"name": "Brahmi", "grams": 6000}, {"name": "brahmi", "grams": 4000}],
             200, 1, ("PASS", [])),
            ("two lines merging to 10001 g", [{"name": "Brahmi", "grams": 6000}, {"name": "brahmi", "grams": 4001}],
             422, 0, ("REJECT", ["QUANTITY_OUT_OF_RANGE"])),
            ("one line of exactly 10000 g", [{"name": "Brahmi", "grams": 10000}], 200, 1, ("PASS", [])),
            ("one line of 10001 g (refused per line, before merging)", [{"name": "Brahmi", "grams": 10001}],
             422, 0, ("INVALID", [])),
            ("a 0 g line", [{"name": "Brahmi", "grams": 5}, {"name": "Brahmi", "grams": 0}], 422, 0, ("INVALID", [])),
            ("an unknown herb", [{"name": "Brahmi", "grams": 5}, {"name": "Unobtainium", "grams": 5}], 400, 0, ("INVALID", [])),
        ]
        for label, override, status, wrote, verdict in cases:
            got = confirm_and_evaluate(override)
            check(f"{label}: confirm {status} (writes {wrote}), evaluation {verdict[0]}",
                  got == (status, wrote, verdict), got)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
