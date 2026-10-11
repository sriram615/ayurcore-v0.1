"""
Phase 3A.4 Block 4 -- guardrail shadow recording of confirmations
(backend/shadow_store.record_confirm, called at the end of /prescribe/confirm
after the commit, the replay return and corrections logging).

Coverage:
  - OFF by default: no activation, or a latest OFF row, records nothing;
  - SHADOW: one CONFIRM row per confirmed AI or manual draft, with the right
    prescription, draft, is_manual flag, latest activation and fingerprints;
  - the doctor's final committed values are evaluated (corrections and
    merged duplicate lines), not the stored draft;
  - strict pairing through the real endpoint: the row's (prescription_id,
    preview_id) is the PRESCRIPTION_CONFIRMED audit event's;
  - never recorded: an idempotent replay, a second confirmation, an unknown
    draft, insufficient stock, an empty correction, an unknown herb, a closed
    encounter, a rolled-back confirmation (audit write failure);
  - authoritative-write invariance: under OFF, SHADOW, a raising recorder,
    an unimportable recorder and a busy database, the same status, body
    (apart from prescription_id / created_at), items, stock, audit event,
    idempotency record, corrections row and draft status;
  - corrections logging is unchanged (AI drafts logged, manual drafts not);
  - two concurrent confirmations of one draft: one prescription, one row;
  - the activation race: switched OFF during evaluation -> no row and
    shadow_record_superseded stage=CONFIRM;
  - no patient, dosha or herb text in the rows or shadow log lines;
  - all patched shared state is restored.

The whole application runs on a fresh temporary database; real Gemini
clients cannot be constructed. The project database is never opened.

Run directly (no pytest required):
    python tests/test_shadow_confirm.py
"""
import contextlib
import io
import json
import logging
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PATIENT = "Shadow Confirm Patient"
NOW = "2026-10-02 09:00:00"

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


@contextlib.contextmanager
def _app_on_temp_db():
    import db.database as database
    import backend.main as main_module
    from fastapi.testclient import TestClient
    tmp = tempfile.mkdtemp(prefix="ayurcore_shadow_confirm_")
    saved = database.DB_PATH
    database.DB_PATH = os.path.join(tmp, "app.db")
    try:
        with TestClient(main_module.app) as client:
            yield client, database.DB_PATH
    finally:
        database.DB_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def _ai_returns(herbs, dosha="Vata"):
    """Stubs the AI parser; no real Gemini client may be constructed."""
    import google.genai as genai_real
    import backend.main as main_module
    from backend.schemas import ClinicalParse

    def _parse(notes, clinic_id=None):
        return ClinicalParse(dosha_imbalance=dosha, prescribed_herbs=herbs)

    def _boom(*args, **kwargs):
        raise AssertionError("no real Gemini client may be constructed in these tests")

    saved = (genai_real.Client, main_module.parse_clinical_notes)
    genai_real.Client, main_module.parse_clinical_notes = _boom, _parse
    try:
        yield
    finally:
        genai_real.Client, main_module.parse_clinical_notes = saved


@contextlib.contextmanager
def _patched(owner, name, value):
    saved = getattr(owner, name)
    setattr(owner, name, value)
    try:
        yield
    finally:
        setattr(owner, name, saved)


@contextlib.contextmanager
def _captured_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


@contextlib.contextmanager
def _stack(*managers):
    with contextlib.ExitStack() as stack:
        for manager in managers:
            stack.enter_context(manager)
        yield


def _setup(client, db_path, tag="confirm") -> tuple[str, dict]:
    from backend.auth_service import create_user
    clinic = f"{tag}_{secrets.token_hex(4)}"
    username = f"{tag}_test_{secrets.token_hex(6)}"
    password = "shadow-confirm-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic, role="DOCTOR")
    conn = sqlite3.connect(db_path)
    try:
        for herb in ("Brahmi", "Ashwagandha"):
            conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                         "VALUES (?, ?, 'test botanical name', 100000, 'TEST-BATCH', 90)", (clinic, herb))
        conn.commit()
    finally:
        conn.close()
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return clinic, {"Authorization": f"Bearer {resp.json()['token']}"}


def _activate(db_path, clinic, mode="SHADOW", ruleset=None) -> int:
    from backend.guardrails import ENGINEERING_BASELINE
    ruleset = ruleset or ENGINEERING_BASELINE
    shadow = mode == "SHADOW"
    conn = sqlite3.connect(db_path)
    try:
        activation_id = conn.execute(
            "INSERT INTO guardrail_shadow_activations (clinic_id, mode, ruleset_id, ruleset_version, ruleset_hash, "
            "activated_by, reason, created_at) VALUES (?, ?, ?, ?, ?, 'test operator', 'test', ?)",
            (clinic, mode, ruleset.ruleset_id if shadow else None, ruleset.version if shadow else None,
             ruleset.content_hash if shadow else None, NOW)).lastrowid
        conn.commit()
        return activation_id
    finally:
        conn.close()


def _query(db_path, sql, args=()) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _rows(db_path, clinic, stage="CONFIRM") -> list[dict]:
    return _query(db_path, "SELECT * FROM guardrail_shadow_results WHERE clinic_id = ? AND stage = ? ORDER BY result_id",
                  (clinic, stage))


def _manual(client, headers, herbs=None, **extra) -> str:
    resp = client.post("/prescribe/manual-preview", headers=headers, json={
        "patient_name": PATIENT, "dosha_imbalance": "Vata",
        "prescribed_herbs": herbs or [{"name": "Brahmi", "grams": 5}], **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()["preview_id"]


def _ai_draft(client, headers, herbs=None) -> str:
    with _ai_returns(herbs or [{"name": "Brahmi", "grams": 5}]):
        resp = client.post("/prescribe/preview", headers=headers, json={
            "patient_name": PATIENT, "notes": "Synthetic notes for a shadow-confirm test."})
    assert resp.status_code == 200, resp.text
    return resp.json()["preview_id"]


def _confirm(client, headers, preview_id, key=None, **extra):
    h = dict(headers)
    if key:
        h["Idempotency-Key"] = key
    return client.post("/prescribe/confirm", headers=h, json={"preview_id": preview_id, **extra})


def _effects(db_path, clinic, preview_id, prescription_id) -> dict:
    """Every authoritative write a confirmation makes, for comparison."""
    one = lambda sql, args: _query(db_path, sql, args)  # noqa: E731
    return {
        "items": [(r["line_no"], r["herb_name"], r["grams"]) for r in one(
            "SELECT line_no, herb_name, grams FROM prescription_items WHERE prescription_id = ? ORDER BY line_no",
            (prescription_id,))],
        "audit_pair": len(one("SELECT 1 FROM audit_events WHERE event_type = 'PRESCRIPTION_CONFIRMED' "
                              "AND prescription_id = ? AND preview_id = ? AND clinic_id = ?",
                              (prescription_id, preview_id, clinic))),
        "draft_status": one("SELECT status FROM prescription_previews WHERE preview_id = ?", (preview_id,))[0]["status"],
        "corrections": len(one("SELECT 1 FROM corrections WHERE prescription_id = ?", (prescription_id,))),
        "idempotency_records": len(one("SELECT 1 FROM idempotency_keys WHERE response_body_json LIKE ?",
                                       (f'%"prescription_id":{prescription_id},%',))),
    }


def _stock(db_path, clinic) -> dict:
    return {r["herb_name"]: r["stock_grams"] for r in _query(
        db_path, "SELECT herb_name, stock_grams FROM inventory WHERE clinic_id = ?", (clinic,))}


# --- OFF and recording ------------------------------------------------------------------

def test_off_by_default_and_off_rows_record_nothing():
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        check("no activation: confirm succeeds", _confirm(client, headers, _manual(client, headers)).status_code == 200)
        check("no activation means OFF: no CONFIRM row", _rows(db_path, clinic) == [])
        _activate(db_path, clinic)
        _activate(db_path, clinic, mode="OFF")
        _confirm(client, headers, _manual(client, headers))
        check("a latest OFF row records nothing", _query(
            db_path, "SELECT 1 FROM guardrail_shadow_results WHERE clinic_id = ?", (clinic,)) == [])


def test_shadow_records_confirmed_ai_and_manual_drafts_with_strict_pairing():
    from backend.guardrails import ENGINEERING_BASELINE
    from backend.guardrails.engine import ENGINE_FINGERPRINT
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        latest = _activate(db_path, clinic)
        drafts = {"AI": _ai_draft(client, headers), "manual": _manual(client, headers)}
        confirmed = {kind: _confirm(client, headers, pid) for kind, pid in drafts.items()}
        rows = {r["preview_id"]: r for r in _rows(db_path, clinic)}
        check("one CONFIRM row per confirmed draft", set(rows) == set(drafts.values()), rows)
        for kind, preview_id in drafts.items():
            row, body = rows[preview_id], confirmed[kind].json()
            check(f"[{kind}] the row names this prescription, draft and manual flag",
                  (row["prescription_id"], row["is_manual"]) == (body["prescription_id"], 1 if kind == "manual" else 0))
            check(f"[{kind}] evaluated under the latest activation with the current fingerprints",
                  row["status"] == "EVALUATED" and row["outcome"] == "PASS" and row["activation_id"] == latest
                  and row["engine_fingerprint"] == ENGINE_FINGERPRINT
                  and row["code_ruleset_hash"] == ENGINEERING_BASELINE.content_hash)
            pair = _query(db_path, "SELECT prescription_id, preview_id FROM audit_events WHERE event_type = "
                                   "'PRESCRIPTION_CONFIRMED' AND prescription_id = ?", (row["prescription_id"],))
            check(f"[{kind}] strict pairing: the row's pair is the PRESCRIPTION_CONFIRMED event's",
                  pair == [{"prescription_id": row["prescription_id"], "preview_id": preview_id}], pair)
            check(f"[{kind}] the draft also has its PREVIEW row (two rows per draft)",
                  len(_query(db_path, "SELECT 1 FROM guardrail_shadow_results WHERE preview_id = ?", (preview_id,))) == 2)
        check("corrections logging unchanged: the AI draft is logged, the manual draft is not",
              _effects(db_path, clinic, drafts["AI"], confirmed["AI"].json()["prescription_id"])["corrections"] == 1
              and _effects(db_path, clinic, drafts["manual"], confirmed["manual"].json()["prescription_id"])["corrections"] == 0)


# A test-only rule (defined here so its source can be fingerprinted): any merged
# line above 5 g is REJECT, so a preview at 5 g and a corrected confirmation at
# 7 g produce different results -- proving which values were evaluated.
def _over_five_grams(candidate, params):
    from backend.guardrails import Finding, Outcome, ReasonCode
    return [Finding(Outcome.REJECT, ReasonCode.QUANTITY_OUT_OF_RANGE, i)
            for i, h in enumerate(candidate.herbs, start=1) if h.grams > 5]


@contextlib.contextmanager
def _over_five_ruleset():
    """Registers TEST-OVER-FIVE only for the duration of the block, then
    removes it from the process-wide rule registry and drops its cached code
    fingerprint, so no test state is left behind for later tests in the same
    process (there is no public unregister: this is test-only cleanup)."""
    import backend.guardrails as guardrails
    from backend.guardrails import Rule, RuleCategory, build_ruleset, engine, register_rule, rules
    register_rule(Rule("TEST-OVER-FIVE", 1, RuleCategory.ENGINEERING, "test: a merged line above 5 g",
                       "tests/test_shadow_confirm.py", _over_five_grams))
    try:
        yield build_ruleset("TEST-OVER-FIVE-SET", "1",
                            [r.rule_id for r in guardrails.ENGINEERING_BASELINE.rules] + ["TEST-OVER-FIVE"])
    finally:
        rules._REGISTRY.pop("TEST-OVER-FIVE", None)
        engine._CODE_FINGERPRINTS.pop(("TEST-OVER-FIVE", 1), None)


def test_the_final_committed_values_are_evaluated():
    import backend.guardrails as guardrails
    from backend.guardrails import build_confirm_candidate, evaluate, registered_rule
    with _over_five_ruleset() as ruleset, _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic, ruleset=ruleset)
        with _patched(guardrails, "ENGINEERING_BASELINE", ruleset):
            preview_id = _manual(client, headers, herbs=[{"name": "Brahmi", "grams": 5}])
            correction = {"dosha_imbalance": "Pitta",
                          "prescribed_herbs": [{"name": "Brahmi", "grams": 4}, {"name": "brahmi", "grams": 3}]}
            resp = _confirm(client, headers, preview_id, **correction)
        check("the corrected confirmation succeeds", resp.status_code == 200, resp.text[:200])
        preview_row = _rows(db_path, clinic, stage="PREVIEW")[0]
        confirm_row = _rows(db_path, clinic)[0]
        check("the PREVIEW row evaluated the draft (5 g: PASS)", preview_row["outcome"] == "PASS")
        check("the committed item is the merged 7 g line",
              _effects(db_path, clinic, preview_id, resp.json()["prescription_id"])["items"] == [(1, "Brahmi", 7)])
        expected = evaluate(build_confirm_candidate("Pitta", [{"name": "Brahmi", "grams": 4}, {"name": "Brahmi", "grams": 3}]),
                            ruleset)
        check("the CONFIRM row evaluated the final values (merged 7 g: REJECT on line 1)",
              confirm_row["outcome"] == "REJECT" and any(
                  r["rule_id"] == "TEST-OVER-FIVE" and r["outcome"] == "REJECT" and r["line_no"] == 1
                  for r in json.loads(confirm_row["results_json"])))
        check("... exactly the results of evaluating the committed prescription directly",
              confirm_row["results_json"] == json.dumps([r.to_dict() for r in expected.results],
                                                        sort_keys=True, separators=(",", ":")))
    from backend.guardrails import engine
    check("TEST-OVER-FIVE is unregistered and its cached fingerprint dropped after the test",
          registered_rule("TEST-OVER-FIVE") is None
          and not any(k[0] == "TEST-OVER-FIVE" for k in engine._CODE_FINGERPRINTS))


# --- never recorded -------------------------------------------------------------------------

def test_refused_rolled_back_and_replayed_confirmations_record_nothing():
    """Counts recorder CALLS, not only rows: the database's UNIQUE
    (preview_id, stage) would also stop a replay's second row, so only the
    call count proves the recorder is never reached on these paths."""
    import backend.main as main_module
    import backend.shadow_store as shadow_store
    calls = []
    real_record_confirm = shadow_store.record_confirm

    def counting(**values):
        calls.append(values["preview_id"])
        return real_record_confirm(**values)

    with _app_on_temp_db() as (client, db_path), _patched(shadow_store, "record_confirm", counting):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic)
        key = f"key-{secrets.token_hex(6)}"
        first_draft = _manual(client, headers)
        first = _confirm(client, headers, first_draft, key=key)
        check("a confirmation with an Idempotency-Key succeeds", first.status_code == 200)
        replay = _confirm(client, headers, first_draft, key=key)
        check("its replay returns the original response", replay.status_code == 200 and replay.json() == first.json())
        check("... and the replay records nothing more (one CONFIRM row)", len(_rows(db_path, clinic)) == 1)
        check("... and never reaches the recorder (one call, for the original)", calls == [first_draft], calls)
        check("a second confirmation of the same draft is refused (409)",
              _confirm(client, headers, first_draft).status_code == 409)

        patient = client.post("/patients", headers=headers, json={"clinic_patient_id": f"OP-{secrets.token_hex(3)}",
                                                                  "display_name": PATIENT}).json()
        encounter = client.post(f"/patients/{patient['patient_pk']}/encounters", headers=headers, json={}).json()
        in_encounter = _manual(client, headers, patient_pk=patient["patient_pk"], encounter_pk=encounter["encounter_pk"])
        client.post(f"/encounters/{encounter['encounter_pk']}/close", headers=headers)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("UPDATE inventory SET stock_grams = 1 WHERE clinic_id = ? AND herb_name = 'Ashwagandha'", (clinic,))
            conn.commit()
        finally:
            conn.close()

        def audit_fails(*args, **kwargs):
            raise RuntimeError("synthetic audit write failure")

        cases = [
            ("an unknown draft", lambda: _confirm(client, headers, "0" * 16), 404),
            ("insufficient stock", lambda: _confirm(client, headers, _manual(client, headers, herbs=[
                {"name": "Ashwagandha", "grams": 5}])), 409),
            ("an empty correction", lambda: _confirm(client, headers, _manual(client, headers), prescribed_herbs=[]), 422),
            ("an unknown herb in a correction", lambda: _confirm(client, headers, _manual(client, headers),
                                                                 prescribed_herbs=[{"name": "NotAHerb", "grams": 5}]), 400),
            ("a closed encounter", lambda: _confirm(client, headers, in_encounter), 409),
        ]
        for label, call, status in cases:
            check(f"{label}: {status}", call().status_code == status)
        rolled_back_draft = _manual(client, headers)
        with _patched(main_module, "create_audit_event", audit_fails):
            check("an audit-write failure rolls the confirmation back (500)",
                  _confirm(client, headers, rolled_back_draft).status_code == 500)
        check("none of these recorded a CONFIRM row (still only the first)",
              [r["preview_id"] for r in _rows(db_path, clinic)] == [first_draft])
        check("none of these reached the recorder at all", calls == [first_draft], calls)
        check("the rolled-back draft has no prescription and stays PENDING",
              _query(db_path, "SELECT status FROM prescription_previews WHERE preview_id = ?",
                     (rolled_back_draft,))[0]["status"] == "PENDING")


# --- invariance -----------------------------------------------------------------------------

def test_authoritative_writes_are_identical_whatever_the_recorder_does():
    import backend.shadow_store as shadow_store
    real_connect = shadow_store._connect

    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        blockers = []

        def busy_connection():
            blocker = sqlite3.connect(db_path, check_same_thread=False)
            blocker.execute("BEGIN IMMEDIATE")      # another writer holds the lock while the recorder runs
            blockers.append(blocker)
            return real_connect()

        def broken(**kwargs):
            raise RuntimeError("synthetic recorder failure")

        @contextlib.contextmanager
        def unimportable():
            saved = sys.modules["backend.shadow_store"]
            sys.modules["backend.shadow_store"] = None
            try:
                yield
            finally:
                sys.modules["backend.shadow_store"] = saved

        @contextlib.contextmanager
        def nothing():
            yield

        @contextlib.contextmanager
        def activated():
            _activate(db_path, clinic)
            try:
                yield
            finally:
                _activate(db_path, clinic, mode="OFF")

        scenarios = [
            ("OFF", nothing, 1 - 1),
            ("SHADOW", activated, 1),
            ("SHADOW, recorder raises", lambda: _stack(activated(), _patched(shadow_store, "record_confirm", broken)), 0),
            ("SHADOW, recorder cannot be imported", lambda: _stack(activated(), unimportable()), 0),
            ("SHADOW, database busy", lambda: _stack(activated(), _patched(shadow_store, "_connect", busy_connection)), 0),
        ]
        observed = {}
        for label, scenario, expected_rows in scenarios:
            drafts = {"AI": _ai_draft(client, headers), "manual": _manual(client, headers)}
            stock_before = _stock(db_path, clinic)
            before = len(_rows(db_path, clinic))
            with _captured_logs() as logs, scenario():
                responses = {}
                for kind, preview_id in drafts.items():
                    responses[kind] = _confirm(client, headers, preview_id, key=f"k-{secrets.token_hex(6)}",
                                               dosha_imbalance="Pitta",
                                               prescribed_herbs=[{"name": "Brahmi", "grams": 4}, {"name": "brahmi", "grams": 3}])
                    for blocker in blockers:          # the other writer finishes after the recorder has given up
                        blocker.rollback()
                        blocker.close()
                    blockers.clear()
            for kind, resp in responses.items():
                check(f"[{label}] {kind} confirmation succeeded", resp.status_code == 200, resp.text[:200])
                body = resp.json()
                observed.setdefault(kind, []).append((label, {k: v for k, v in body.items()
                                                              if k not in ("prescription_id", "created_at")},
                                                      _effects(db_path, clinic, drafts[kind], body["prescription_id"])))
            check(f"[{label}] stock deducted exactly once per confirmation (7 g Brahmi each)",
                  _stock(db_path, clinic)["Brahmi"] == stock_before["Brahmi"] - 14)
            check(f"[{label}] CONFIRM rows recorded: {expected_rows} per confirmation",
                  len(_rows(db_path, clinic)) - before == 2 * expected_rows)
            if label not in ("OFF", "SHADOW"):
                check(f"[{label}] the lost row is logged as shadow_record_failed stage=CONFIRM",
                      "event=shadow_record_failed stage=CONFIRM" in logs.getvalue())
        for kind, results in observed.items():
            reference = results[0][1:]
            check(f"[{kind}] reference effects: merged item, audit pair, CONFIRMED draft, idempotency record",
                  reference[1]["items"] == [(1, "Brahmi", 7)] and reference[1]["audit_pair"] == 1
                  and reference[1]["draft_status"] == "CONFIRMED" and reference[1]["idempotency_records"] == 1
                  and reference[1]["corrections"] == (1 if kind == "AI" else 0), reference)
            for label, *rest in results:
                check(f"[{kind}] {label}: same body and authoritative writes as OFF", tuple(rest) == reference,
                      (label, rest, reference))


def test_concurrent_confirmations_of_one_draft_record_one_row():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic)
        preview_id = _manual(client, headers)
        statuses, start = [], threading.Barrier(2)

        def confirm():
            own_client = TestClient(main_module.app)      # one client per thread
            start.wait()
            statuses.append(own_client.post("/prescribe/confirm", headers=headers,
                                            json={"preview_id": preview_id}).status_code)

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        with _captured_logs() as logs:
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        check("exactly one concurrent confirmation succeeds", sorted(statuses) == [200, 409], statuses)
        check("one prescription for the draft", len(_query(
            db_path, "SELECT 1 FROM audit_events WHERE preview_id = ? AND event_type = 'PRESCRIPTION_CONFIRMED'",
            (preview_id,))) == 1)
        rows = [r["preview_id"] for r in _rows(db_path, clinic)]
        # At most one row: if the losing request still held the write lock when the
        # winner's recorder ran, the recorder failed fast (timeout=0) and lost its
        # row by design -- logged, never a wait or a second row.
        check("at most one CONFIRM row, never a duplicate", rows in ([preview_id], []), rows)
        check("... and a lost row is always logged",
              rows == [preview_id] or f"preview_id={preview_id} event=shadow_record_failed stage=CONFIRM" in logs.getvalue(),
              [line for line in logs.getvalue().splitlines() if "shadow" in line])
        print(f"   (concurrent run outcome: {'row recorded' if rows else 'row lost to lock contention, logged'})")


# --- race and privacy ----------------------------------------------------------------------

def test_activation_switched_off_during_evaluation_records_nothing():
    import backend.shadow_store as shadow_store
    real_evaluate = shadow_store._evaluate
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic)
        preview_id = _manual(client, headers)

        def evaluate_then_switch_off(*args, **kwargs):
            row = real_evaluate(*args, **kwargs)
            _activate(db_path, clinic, mode="OFF")
            return row

        with _captured_logs() as logs, _patched(shadow_store, "_evaluate", evaluate_then_switch_off):
            resp = _confirm(client, headers, preview_id)
        check("the confirmation succeeds", resp.status_code == 200)
        check("no CONFIRM row under the superseded activation", _rows(db_path, clinic) == [])
        check("a fixed shadow_record_superseded stage=CONFIRM event is logged",
              f"preview_id={preview_id} event=shadow_record_superseded stage=CONFIRM" in logs.getvalue())


def test_no_patient_dosha_or_herb_text_in_rows_or_shadow_logs():
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic)
        with _captured_logs() as logs:
            preview_id = _ai_draft(client, headers, herbs=[{"name": "Ashwagandha", "grams": 2}])
            _confirm(client, headers, preview_id, dosha_imbalance="Kapha Quintessa",
                     prescribed_herbs=[{"name": "Brahmi", "grams": 3}])
        stored = json.dumps(_rows(db_path, clinic))
        check("a CONFIRM row was recorded", len(_rows(db_path, clinic)) == 1)
        check("no patient, dosha or herb text in the row",
              not any(t in stored for t in (PATIENT, "Kapha", "Quintessa", "Brahmi", "Ashwagandha")))
        shadow_lines = [line for line in logs.getvalue().splitlines() if "shadow" in line]
        check("no patient, dosha or herb text in shadow log lines",
              not any(t in line for line in shadow_lines for t in (PATIENT, "Quintessa", "Brahmi", "Ashwagandha")),
              shadow_lines)


_ORIGINALS = None


def _shared_state():
    import google.genai as genai_real
    import backend.guardrails as guardrails
    import backend.main as main_module
    import backend.shadow_store as shadow_store
    return {
        "shadow_store._connect": shadow_store._connect, "shadow_store._evaluate": shadow_store._evaluate,
        "shadow_store.record_confirm": shadow_store.record_confirm,
        "guardrails.ENGINEERING_BASELINE": guardrails.ENGINEERING_BASELINE,
        "main.create_audit_event": main_module.create_audit_event,
        "main.parse_clinical_notes": main_module.parse_clinical_notes, "genai.Client": genai_real.Client,
        "sys.modules[backend.shadow_store]": sys.modules.get("backend.shadow_store"),
    }


_ORIGINALS = _shared_state()


def test_zz_shared_module_state_is_restored():
    from backend.guardrails import engine, registered_rule
    now = _shared_state()
    changed = sorted(k for k in _ORIGINALS if now[k] is not _ORIGINALS[k])
    check("all patched shared state was restored", changed == [], changed)
    check("no rule registered by this file remains in the registry or the fingerprint cache",
          registered_rule("TEST-OVER-FIVE") is None
          and not any(k[0] == "TEST-OVER-FIVE" for k in engine._CODE_FINGERPRINTS))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
