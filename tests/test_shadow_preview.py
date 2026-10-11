"""
Phase 3A.4 Block 3 -- guardrail shadow recording of previews
(backend/shadow_store.py, called from /prescribe/preview and
/prescribe/manual-preview after the draft is stored).

Coverage:
  - OFF by default: no activation, or a latest OFF row, records nothing;
  - kill switch: AYURCORE_SHADOW_DISABLED=1 records nothing and never opens
    the database;
  - SHADOW: one PREVIEW row per AI and manual draft, with the right clinic,
    draft, is_manual flag, activation, outcome and fingerprints, and no
    candidate fingerprint, patient, dosha or herb text;
  - statuses: RULESET_MISMATCH, NOT_EVALUABLE, ENGINE_ERROR (ENGINE_CHANGED,
    RULESET_INVALID, UNEXPECTED) and an engine that fails to import;
  - only the latest activation is recorded under; an activation switched off
    or replaced during evaluation writes nothing (the race) and logs
    shadow_record_superseded;
  - clinic isolation;
  - response invariance: OFF, SHADOW, a raising recorder, a recorder that
    cannot be imported and a busy database all give the same status, body
    (apart from the random preview_id) and stored draft;
  - failure paths (400, 409, 422, 502) store no draft and record nothing;
  - no log line carries the patient's name, dosha or herb names;
  - review fixes: a first connection to a locked database (rollback-journal
    or WAL) fails fast and never changes the journal mode; any unforeseen
    failure while reading the rule set, evaluating or serialising is
    recorded as ENGINE_ERROR / UNEXPECTED; a synthetic rule set built to
    leak (a raising rule with candidate text in its message, a malformed
    result carrying it) persists no patient, dosha, herb or exception text.

The whole application runs on a fresh temporary database; real Gemini
clients cannot be constructed. The project database is never opened.

Run directly (no pytest required):
    python tests/test_shadow_preview.py
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

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PATIENT = "Shadow Preview Patient"
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
    """The whole application on a fresh temporary database (startup
    migrations, 017 included, run on it); the real path is always restored."""
    import db.database as database
    import backend.main as main_module
    from fastapi.testclient import TestClient
    tmp = tempfile.mkdtemp(prefix="ayurcore_shadow_")
    saved = database.DB_PATH
    database.DB_PATH = os.path.join(tmp, "app.db")
    try:
        with TestClient(main_module.app) as client:
            yield client, database.DB_PATH
    finally:
        database.DB_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def _ai_returns(herbs, dosha="Vata", error=None):
    """Stubs the AI parser; no real Gemini client may be constructed."""
    import google.genai as genai_real
    import backend.main as main_module
    from backend.schemas import ClinicalParse

    def _parse(notes, clinic_id=None):
        if error:
            raise ValueError(error)
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
def _env(name, value):
    saved = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


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


def _setup(client, db_path, tag="shadow") -> tuple[str, dict]:
    from backend.auth_service import create_user
    clinic = f"{tag}_{secrets.token_hex(4)}"
    username = f"{tag}_test_{secrets.token_hex(6)}"
    password = "shadow-test-password-not-a-real-credential"
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


def _activate(db_path, clinic, mode="SHADOW", ruleset_hash=None) -> int:
    from backend.guardrails import ENGINEERING_BASELINE
    shadow = mode == "SHADOW"
    conn = sqlite3.connect(db_path)
    try:
        activation_id = conn.execute(
            "INSERT INTO guardrail_shadow_activations (clinic_id, mode, ruleset_id, ruleset_version, ruleset_hash, "
            "activated_by, reason, created_at) VALUES (?, ?, ?, ?, ?, 'test operator', 'test', ?)",
            (clinic, mode, ENGINEERING_BASELINE.ruleset_id if shadow else None,
             ENGINEERING_BASELINE.version if shadow else None,
             (ruleset_hash or ENGINEERING_BASELINE.content_hash) if shadow else None, NOW)).lastrowid
        conn.commit()
        return activation_id
    finally:
        conn.close()


def _rows(db_path, clinic) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM guardrail_shadow_results WHERE clinic_id = ? ORDER BY result_id", (clinic,))]
    finally:
        conn.close()


def _draft(db_path, preview_id) -> dict | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM prescription_previews WHERE preview_id = ?", (preview_id,)).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys() if k not in ("preview_id", "created_at", "expires_at", "created_by_session")}
    finally:
        conn.close()


def _manual(client, headers, herbs=None, **extra):
    return client.post("/prescribe/manual-preview", headers=headers, json={
        "patient_name": PATIENT, "dosha_imbalance": "Vata",
        "prescribed_herbs": herbs or [{"name": "Brahmi", "grams": 5}], **extra})


def _ai(client, headers, herbs=None, error=None):
    with _ai_returns(herbs or [{"name": "Brahmi", "grams": 5}], error=error):
        return client.post("/prescribe/preview", headers=headers, json={
            "patient_name": PATIENT, "notes": "Synthetic notes for a shadow-recording test."})


# --- OFF by default and the kill switch ------------------------------------------------

def test_off_by_default_and_off_rows_record_nothing():
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        check("no activation: AI preview succeeds", _ai(client, headers).status_code == 200)
        check("no activation: manual preview succeeds", _manual(client, headers).status_code == 200)
        check("no activation means OFF: nothing recorded", _rows(db_path, clinic) == [])
        _activate(db_path, clinic)
        _activate(db_path, clinic, mode="OFF")
        _ai(client, headers)
        _manual(client, headers)
        check("a latest OFF row records nothing", _rows(db_path, clinic) == [])


def test_kill_switch_records_nothing_and_never_opens_the_database():
    import backend.shadow_store as shadow_store
    opened = []

    def spy():
        opened.append(True)
        raise AssertionError("the recorder must not open the database when disabled")

    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic)
        with _env("AYURCORE_SHADOW_DISABLED", "1"), _patched(shadow_store, "_connect", spy):
            ai, manual = _ai(client, headers), _manual(client, headers)
        check("with the kill switch on, previews still succeed", ai.status_code == manual.status_code == 200)
        check("nothing recorded", _rows(db_path, clinic) == [])
        check("the recorder never opened the database", opened == [])
        _manual(client, headers)
        check("with the kill switch off again, recording resumes", len(_rows(db_path, clinic)) == 1)


# --- recording -------------------------------------------------------------------------

def test_shadow_records_ai_and_manual_previews():
    from backend.guardrails import ENGINEERING_BASELINE
    from backend.guardrails.engine import ENGINE_FINGERPRINT, ENGINE_VERSION
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic)
        latest = _activate(db_path, clinic)
        ai = _ai(client, headers, herbs=[{"name": "Brahmi", "grams": 5}, {"name": "Ashwagandha", "grams": 3}])
        manual = _manual(client, headers)
        rows = _rows(db_path, clinic)
        check("one PREVIEW row per draft", [(r["preview_id"], r["stage"], r["is_manual"]) for r in rows] == [
            (ai.json()["preview_id"], "PREVIEW", 0), (manual.json()["preview_id"], "PREVIEW", 1)], rows)
        for row in rows:
            check(f"[{row['preview_id']}] evaluated under the latest activation, in the right clinic",
                  row["status"] == "EVALUATED" and row["activation_id"] == latest and row["clinic_id"] == clinic
                  and row["prescription_id"] is None)
            check(f"[{row['preview_id']}] outcome and fingerprints recorded",
                  row["outcome"] == "PASS" and row["engine_version"] == ENGINE_VERSION
                  and row["engine_fingerprint"] == ENGINE_FINGERPRINT
                  and row["code_ruleset_hash"] == ENGINEERING_BASELINE.content_hash
                  and row["code_ruleset_id"] == ENGINEERING_BASELINE.ruleset_id)
            results = json.loads(row["results_json"])
            check(f"[{row['preview_id']}] results are the rule results, one per baseline rule",
                  isinstance(results, list) and {r["rule_id"] for r in results} == {r.rule_id for r in ENGINEERING_BASELINE.rules}
                  and all(set(r) == {"rule_id", "rule_version", "outcome", "reason_code", "detail", "line_no"} for r in results))
            text = json.dumps(row)
            check(f"[{row['preview_id']}] no candidate fingerprint, patient, dosha or herb text in the row",
                  "candidate" not in text and PATIENT not in text and "Vata" not in text
                  and "Brahmi" not in text and "Ashwagandha" not in text)


def test_statuses_for_drift_unusable_candidates_and_engine_failures():
    import backend.guardrails as guardrails
    from backend.guardrails import InvalidCandidateError, InvalidRuleSetError
    from backend.guardrails.engine import EngineChangedError

    def raiser(exc):
        def fn(*args, **kwargs):
            raise exc
        return fn

    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        drifted = _activate(db_path, clinic, ruleset_hash="c" * 64)
        _manual(client, headers)
        row = _rows(db_path, clinic)[-1]
        check("an activation whose fingerprint differs from the code's -> RULESET_MISMATCH, nothing evaluated",
              (row["status"], row["activation_id"], row["outcome"], row["results_json"]) == ("RULESET_MISMATCH", drifted, None, None)
              and row["code_ruleset_hash"] == guardrails.ENGINEERING_BASELINE.content_hash)

        _activate(db_path, clinic)
        cases = [
            ("build_confirm_candidate", raiser(InvalidCandidateError("synthetic")), ("NOT_EVALUABLE", "CANDIDATE_INVALID")),
            ("evaluate", raiser(EngineChangedError("synthetic")), ("ENGINE_ERROR", "ENGINE_CHANGED")),
            ("evaluate", raiser(InvalidRuleSetError("synthetic")), ("ENGINE_ERROR", "RULESET_INVALID")),
            ("evaluate", raiser(RuntimeError("synthetic")), ("ENGINE_ERROR", "UNEXPECTED")),
        ]
        for name, replacement, expected in cases:
            with _patched(guardrails, name, replacement):
                check(f"{name} failing ({expected[1]}) leaves the preview successful",
                      _manual(client, headers).status_code == 200)
            row = _rows(db_path, clinic)[-1]
            check(f"... and records {expected}", (row["status"], row["error_code"], row["outcome"]) == (*expected, None), row)

        saved = sys.modules["backend.guardrails"]
        sys.modules["backend.guardrails"] = None   # any import of the package now fails
        try:
            check("an engine that cannot be imported leaves the preview successful",
                  _manual(client, headers).status_code == 200)
        finally:
            sys.modules["backend.guardrails"] = saved
        row = _rows(db_path, clinic)[-1]
        check("... and records ENGINE_ERROR / UNEXPECTED with no engine fields",
              (row["status"], row["error_code"], row["engine_fingerprint"], row["code_ruleset_hash"])
              == ("ENGINE_ERROR", "UNEXPECTED", None, None), row)


# --- latest activation and the race -------------------------------------------------------

def test_only_the_latest_activation_is_recorded_under():
    import backend.shadow_store as shadow_store
    real_evaluate = shadow_store._evaluate

    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        for label, mode in (("switched OFF", "OFF"), ("re-activated", "SHADOW")):
            _activate(db_path, clinic)                  # each scenario starts with shadow mode on

            def evaluate_then_change(*args, **kwargs):
                row = real_evaluate(*args, **kwargs)
                _activate(db_path, clinic, mode=mode)   # the clinic changes between the read and the write
                return row

            with _captured_logs() as logs, _patched(shadow_store, "_evaluate", evaluate_then_change):
                resp = _manual(client, headers)
            check(f"activation {label} during evaluation: the preview still succeeds", resp.status_code == 200)
            check("... nothing is recorded under the superseded activation", _rows(db_path, clinic) == [])
            check("... and a fixed shadow_record_superseded event is logged",
                  f"preview_id={resp.json()['preview_id']} event=shadow_record_superseded stage=PREVIEW" in logs.getvalue(),
                  [line for line in logs.getvalue().splitlines() if "shadow" in line])
        latest = _activate(db_path, clinic)
        _manual(client, headers)
        check("once settled, recording resumes under the latest activation",
              [r["activation_id"] for r in _rows(db_path, clinic)] == [latest])


def test_clinic_isolation():
    with _app_on_temp_db() as (client, db_path):
        clinic_a, headers_a = _setup(client, db_path, tag="clinic_a")
        clinic_b, headers_b = _setup(client, db_path, tag="clinic_b")
        _activate(db_path, clinic_b)
        _manual(client, headers_a)
        _ai(client, headers_a)
        resp_b = _manual(client, headers_b)
        check("clinic A (not activated) records nothing", _rows(db_path, clinic_a) == [])
        check("clinic B's row carries clinic B and its own draft",
              [(r["clinic_id"], r["preview_id"]) for r in _rows(db_path, clinic_b)] == [(clinic_b, resp_b.json()["preview_id"])])


# --- response invariance ------------------------------------------------------------------

def test_responses_and_drafts_are_identical_whatever_the_recorder_does():
    import backend.shadow_store as shadow_store
    real_connect = shadow_store._connect

    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        blockers = []

        def busy_connection():
            blocker = sqlite3.connect(db_path, check_same_thread=False)   # released from the test thread
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
            ("OFF", nothing, 0),
            ("SHADOW", activated, 1),
            ("SHADOW, recorder raises", lambda: _stack(activated(), _patched(shadow_store, "record_preview", broken)), 0),
            ("SHADOW, recorder cannot be imported", lambda: _stack(activated(), unimportable()), 0),
            ("SHADOW, database busy", lambda: _stack(activated(), _patched(shadow_store, "_connect", busy_connection)), 0),
        ]
        observed = {}
        for label, scenario, expected_rows in scenarios:
            before = len(_rows(db_path, clinic))
            def release_other_writer():
                for blocker in blockers:      # the other writer finishes after the recorder has given up
                    blocker.rollback()
                    blocker.close()
                blockers.clear()

            with _captured_logs() as logs, scenario():
                responses = {}
                for kind, call in (("manual", lambda: _manual(client, headers)), ("ai", lambda: _ai(client, headers))):
                    responses[kind] = call()
                    release_other_writer()
            for kind, resp in responses.items():
                check(f"[{label}] {kind} preview succeeded", resp.status_code == 200, resp.text[:200])
            for kind, resp in responses.items():
                body = resp.json()
                observed.setdefault(kind, []).append(
                    (label, resp.status_code, {k: v for k, v in body.items() if k != "preview_id"},
                     _draft(db_path, body["preview_id"])))
            check(f"[{label}] rows recorded: {expected_rows} per preview",
                  len(_rows(db_path, clinic)) - before == 2 * expected_rows)
            if expected_rows == 0 and label != "OFF":
                check(f"[{label}] the lost row is logged as shadow_record_failed",
                      "event=shadow_record_failed stage=PREVIEW" in logs.getvalue())
        for kind, results in observed.items():
            reference = results[0][1:]
            for label, *rest in results:
                check(f"[{kind}] {label}: same status, body (apart from preview_id) and stored draft as OFF",
                      tuple(rest) == reference, (label, rest, reference))


@contextlib.contextmanager
def _stack(*managers):
    with contextlib.ExitStack() as stack:
        for manager in managers:
            stack.enter_context(manager)
        yield


# --- failure paths ------------------------------------------------------------------------

def test_failure_paths_store_no_draft_and_record_nothing():
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic)
        patient = client.post("/patients", headers=headers, json={"clinic_patient_id": f"OP-{secrets.token_hex(3)}",
                                                                  "display_name": PATIENT})
        encounter = client.post(f"/patients/{patient.json()['patient_pk']}/encounters", headers=headers, json={})
        client.post(f"/encounters/{encounter.json()['encounter_pk']}/close", headers=headers)
        cases = [
            ("AI unavailable (502)", lambda: _ai(client, headers, error="AI_SERVICE_UNAVAILABLE"), 502),
            ("unusable AI reply (422)", lambda: _ai(client, headers, error="INVALID_AI_SCHEMA"), 422),
            ("AI names an unknown herb (400)", lambda: _ai(client, headers, herbs=[{"name": "NotAHerb", "grams": 5}]), 400),
            ("manual unknown herb (400)", lambda: _manual(client, headers, herbs=[{"name": "NotAHerb", "grams": 5}]), 400),
            ("closed encounter (409)", lambda: _manual(client, headers, patient_pk=patient.json()["patient_pk"],
                                                       encounter_pk=encounter.json()["encounter_pk"]), 409),
        ]
        conn = sqlite3.connect(db_path)
        try:
            drafts_before = conn.execute("SELECT COUNT(*) FROM prescription_previews").fetchone()[0]
        finally:
            conn.close()
        for label, call, status in cases:
            check(f"{label}: status {status}", call().status_code == status)
        conn = sqlite3.connect(db_path)
        try:
            check("no draft was stored", conn.execute("SELECT COUNT(*) FROM prescription_previews").fetchone()[0] == drafts_before)
        finally:
            conn.close()
        check("nothing was recorded", _rows(db_path, clinic) == [])


# --- privacy of logs ----------------------------------------------------------------------

def test_no_log_line_carries_patient_dosha_or_herb_text():
    import backend.guardrails as guardrails
    from backend.guardrails.engine import EngineChangedError

    def boom(*args, **kwargs):
        raise EngineChangedError(f"{PATIENT} Brahmi Vata")

    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        with _captured_logs() as logs:
            _activate(db_path, clinic)
            _manual(client, headers)
            _ai(client, headers)
            with _patched(guardrails, "evaluate", boom):
                _manual(client, headers)
            _activate(db_path, clinic, ruleset_hash="d" * 64)
            _manual(client, headers)
        shadow_lines = [line for line in logs.getvalue().splitlines() if "shadow" in line]
        check("no captured log line carries the patient's name", PATIENT not in logs.getvalue())
        check("no shadow log line carries dosha or herb text",
              not any(word in line for line in shadow_lines for word in ("Brahmi", "Vata", "Ashwagandha")), shadow_lines)
        check("four rows were recorded (EVALUATED x2, ENGINE_ERROR, RULESET_MISMATCH)",
              [r["status"] for r in _rows(db_path, clinic)] == ["EVALUATED", "EVALUATED", "ENGINE_ERROR", "RULESET_MISMATCH"])



# --- review fixes -------------------------------------------------------------------------

def test_first_connection_to_a_locked_database_fails_fast():
    """The recorder opens its own connection with timeout=0. Before this fix
    it used get_connection() (timeout=15, then a journal-mode switch) and
    waited 15 s on a locked rollback-journal database."""
    import time
    import backend.shadow_store as shadow_store
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic)
        for journal, lock in (("DELETE", "BEGIN EXCLUSIVE"), ("WAL", "BEGIN IMMEDIATE")):
            conn = sqlite3.connect(db_path)
            conn.execute(f"PRAGMA journal_mode = {journal}")
            conn.close()
            blocker = sqlite3.connect(db_path)
            blocker.execute(lock)
            try:
                with _captured_logs() as logs:
                    started = time.perf_counter()
                    shadow_store.record_preview(clinic_id=clinic, preview_id=f"locked-{journal}", is_manual=False,
                                                dosha_imbalance="Vata", prescribed_herbs=[{"name": "Brahmi", "grams": 5}])
                    elapsed = time.perf_counter() - started
            finally:
                blocker.rollback()
                blocker.close()
            check(f"[{journal}] a first connection to a locked database returns in under 1 s ({elapsed:.3f} s)",
                  elapsed < 1.0)
            check(f"[{journal}] ... records nothing and logs the lost row",
                  _rows(db_path, clinic) == [] and "event=shadow_record_failed stage=PREVIEW" in logs.getvalue())
            conn = sqlite3.connect(db_path)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                conn.close()
            check(f"[{journal}] ... and leaves the journal mode unchanged", mode == journal.lower(), mode)


def test_unforeseen_failures_are_recorded_as_engine_error_unexpected():
    import backend.guardrails as guardrails

    class Exploding:
        def __getattr__(self, name):
            raise RuntimeError("synthetic: unreadable rule set")

    class BadResult:
        @property
        def outcome(self):
            raise RuntimeError("synthetic: unreadable outcome")

    class Unserialisable:
        outcome = guardrails.Outcome.PASS

        class _Bad:
            def to_dict(self):
                raise RuntimeError("synthetic: unserialisable result")

        results = (_Bad(),)

    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic)
        cases = [
            ("reading the rule set fails", _patched(guardrails, "ENGINEERING_BASELINE", Exploding())),
            ("the result's outcome cannot be read", _patched(guardrails, "evaluate", lambda c, r: BadResult())),
            ("the results cannot be serialised", _patched(guardrails, "evaluate", lambda c, r: Unserialisable())),
            ("candidate building raises something unforeseen",
             _patched(guardrails, "build_confirm_candidate", lambda d, h: (_ for _ in ()).throw(KeyError("synthetic")))),
        ]
        for label, patch in cases:
            with patch:
                check(f"{label}: the preview succeeds", _manual(client, headers).status_code == 200)
            row = _rows(db_path, clinic)[-1]
            check(f"... and is recorded as ENGINE_ERROR / UNEXPECTED",
                  (row["status"], row["error_code"], row["outcome"], row["results_json"]) == ("ENGINE_ERROR", "UNEXPECTED", None, None), row)
        check("one row per draft, none lost", len(_rows(db_path, clinic)) == len(cases))


# Rules for the leakage test: defined in this file so their source can be
# fingerprinted (B2), registered once, and built to try to leak candidate text.
def _leak_by_raising(candidate, params):
    raise RuntimeError(f"{candidate.dosha_imbalance} {candidate.herbs[0].name} {candidate.herbs[0].grams}")


def _leak_by_malformed_result(candidate, params):
    return [candidate.dosha_imbalance, candidate.herbs[0].name]


_LEAK_RULE_IDS = ("TEST-LEAK-RAISE", "TEST-LEAK-MALFORMED")


@contextlib.contextmanager
def _leak_ruleset():
    """Registers the leak-probe rules only for the duration of the block and
    then restores the process-wide rule registry and code-fingerprint cache
    to exactly what they held before for these rule ids: entries that
    existed are put back (the same objects), entries that did not are
    removed. Registration happens inside the try, so a failure part-way
    through cannot leak either (no public unregister: test-only cleanup)."""
    import backend.guardrails as guardrails
    from backend.guardrails import Rule, RuleCategory, build_ruleset, engine, register_rule, rules
    saved_rules = {r: rules._REGISTRY.pop(r) for r in _LEAK_RULE_IDS if r in rules._REGISTRY}
    saved_fingerprints = {k: engine._CODE_FINGERPRINTS.pop(k) for k in list(engine._CODE_FINGERPRINTS)
                          if k[0] in _LEAK_RULE_IDS}
    try:
        for rule_id, check_fn in zip(_LEAK_RULE_IDS, (_leak_by_raising, _leak_by_malformed_result)):
            register_rule(Rule(rule_id, 1, RuleCategory.ENGINEERING, "synthetic leakage probe",
                               "tests/test_shadow_preview.py", check_fn))
        yield build_ruleset("TEST-LEAK", "1",
                            [r.rule_id for r in guardrails.ENGINEERING_BASELINE.rules] + list(_LEAK_RULE_IDS))
    finally:
        for rule_id in _LEAK_RULE_IDS:
            rules._REGISTRY.pop(rule_id, None)
        for key in [k for k in engine._CODE_FINGERPRINTS if k[0] in _LEAK_RULE_IDS]:
            engine._CODE_FINGERPRINTS.pop(key)
        rules._REGISTRY.update(saved_rules)
        engine._CODE_FINGERPRINTS.update(saved_fingerprints)


def _leak_rules_left_behind() -> list:
    from backend.guardrails import engine, registered_rule
    return [r for r in _LEAK_RULE_IDS if registered_rule(r) is not None] + \
           [k for k in engine._CODE_FINGERPRINTS if k[0] in _LEAK_RULE_IDS]


def test_persisted_results_carry_no_patient_or_candidate_text():
    """Serialisation path: RuleResult.to_dict() holds rule_id, rule_version,
    outcome, reason_code, line_no and detail -- and the engine always sets
    detail to REASON_DESCRIPTIONS[reason_code]; a Finding has no text field.
    Proven here end to end through the recorder, with rules built to leak."""
    import backend.guardrails as guardrails
    from backend.guardrails.model import REASON_DESCRIPTIONS
    marker = "Leakmarker Quintessa"           # synthetic, distinctive dosha text
    with _leak_ruleset() as leaky, _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic, ruleset_hash=leaky.content_hash)
        with _captured_logs() as logs, _patched(guardrails, "ENGINEERING_BASELINE", leaky):
            resp = client.post("/prescribe/manual-preview", headers=headers, json={
                "patient_name": PATIENT, "dosha_imbalance": marker,
                "prescribed_herbs": [{"name": "Brahmi", "grams": 7}, {"name": "Ashwagandha", "grams": 3}]})
        check("the preview succeeds", resp.status_code == 200, resp.text[:200])
        row = _rows(db_path, clinic)[-1]
        results = json.loads(row["results_json"])
        reasons = {(r["rule_id"], r["reason_code"]) for r in results}
        check("the leak probes ran and were contained (RULE_ERROR, RULE_INVALID_RESULT)",
              {("TEST-LEAK-RAISE", "RULE_ERROR"), ("TEST-LEAK-MALFORMED", "RULE_INVALID_RESULT")} <= reasons, reasons)
        check("every persisted result has exactly the six fixed keys",
              all(set(r) == {"rule_id", "rule_version", "outcome", "reason_code", "detail", "line_no"} for r in results))
        check("every persisted detail is the fixed text for its reason code",
              all(r["detail"] == REASON_DESCRIPTIONS[guardrails.ReasonCode(r["reason_code"])] for r in results))
        stored = json.dumps(row)
        for label, text in (("patient name", PATIENT), ("dosha text", marker), ("dosha fragment", "Quintessa"),
                            ("herb name", "Brahmi"), ("herb name", "Ashwagandha"), ("exception type", "RuntimeError")):
            check(f"no {label} anywhere in the stored row", text not in stored)
        check("no dosha, herb or patient text in any log line",
              not any(t in logs.getvalue() for t in (marker, "Quintessa", PATIENT)))


_ORIGINALS = None


def _shared_state():
    import google.genai as genai_real
    import backend.guardrails as guardrails
    import backend.main as main_module
    import backend.shadow_store as shadow_store
    return {
        "shadow_store._connect": shadow_store._connect, "shadow_store._evaluate": shadow_store._evaluate,
        "shadow_store.record_preview": shadow_store.record_preview,
        "guardrails.evaluate": guardrails.evaluate, "guardrails.build_confirm_candidate": guardrails.build_confirm_candidate,
        "guardrails.ENGINEERING_BASELINE": guardrails.ENGINEERING_BASELINE,
        "main.parse_clinical_notes": main_module.parse_clinical_notes, "genai.Client": genai_real.Client,
        "sys.modules[backend.guardrails]": sys.modules.get("backend.guardrails"),
        "sys.modules[backend.shadow_store]": sys.modules.get("backend.shadow_store"),
        "env AYURCORE_SHADOW_DISABLED": os.environ.get("AYURCORE_SHADOW_DISABLED"),
    }


_ORIGINALS = _shared_state()


def test_leak_ruleset_restores_any_previous_registry_and_cache_state():
    """If a leak-probe rule id was already registered (and fingerprinted)
    before the block, the very same objects are back afterwards."""
    from backend.guardrails import Rule, RuleCategory, engine, rules
    sentinel = Rule("TEST-LEAK-RAISE", 1, RuleCategory.ENGINEERING, "pre-existing", "pre-existing", _leak_by_raising)
    rules._REGISTRY["TEST-LEAK-RAISE"] = sentinel
    engine._CODE_FINGERPRINTS[("TEST-LEAK-RAISE", 1)] = "pre-existing fingerprint"
    try:
        with _leak_ruleset() as leaky:
            check("inside the block the probe rules are the registered ones",
                  rules._REGISTRY["TEST-LEAK-RAISE"] is not sentinel
                  and {r.rule_id for r in leaky.rules} >= set(_LEAK_RULE_IDS))
        check("afterwards the pre-existing rule object is restored",
              rules._REGISTRY.get("TEST-LEAK-RAISE") is sentinel)
        check("... with its pre-existing cached fingerprint",
              engine._CODE_FINGERPRINTS.get(("TEST-LEAK-RAISE", 1)) == "pre-existing fingerprint")
        check("... and the id that did not exist before is gone again",
              "TEST-LEAK-MALFORMED" not in rules._REGISTRY
              and ("TEST-LEAK-MALFORMED", 1) not in engine._CODE_FINGERPRINTS)
    finally:
        rules._REGISTRY.pop("TEST-LEAK-RAISE", None)
        engine._CODE_FINGERPRINTS.pop(("TEST-LEAK-RAISE", 1), None)


def test_leak_rules_are_unregistered_after_the_leakage_test():
    check("the leak-probe rules left nothing in the registry or the fingerprint cache",
          _leak_rules_left_behind() == [], _leak_rules_left_behind())


def test_zz_shared_module_state_is_restored():
    """Runs last (by name and by position): every module attribute,
    sys.modules entry and environment variable this file patches is back to
    what it was when the file was imported."""
    now = _shared_state()
    changed = sorted(k for k in _ORIGINALS if now[k] is not _ORIGINALS[k])
    check("all patched shared state was restored", changed == [], changed)
    check("no rule registered by this file remains in the registry or the fingerprint cache",
          _leak_rules_left_behind() == [], _leak_rules_left_behind())

if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
