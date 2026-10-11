"""
Phase 3A.4 Block 5 -- adversarial regression tests for guardrail shadow
recording (migration 017, backend/shadow_store.py and its two call sites),
from the Block 5 Phase A probes.

Coverage (each from a verified Phase A attack):
  - kill switch: only the exact value "1" disables recording (A7, fixed);
  - concurrency: a 20-request burst of previews and of confirmations, OFF and
    SHADOW -- no request fails, no duplicate row, every success accounted for
    by a row or a logged best-effort loss (A3);
  - activation flapping: every row is recorded under the activation that was
    the clinic's latest at the moment of insert, never under OFF (A4);
  - forged clinic_id in the request body (A5);
  - identity in the dosha, unusual characters, a 1 MB dosha and 5,000 lines:
    no input text stored, bounded results, bounded evaluation time (A6);
  - privacy: six failure paths raising exceptions that carry input text --
    nothing reaches logs or rows (A8);
  - completeness: the reusable COMPLETENESS_SQL finds exactly the
    confirmations made while recording was on that have no CONFIRM row (A9);
  - engine code swapped in and out while requests run (A10);
  - trigger integrity against rows produced by a real confirmation (A11);
  - connection stability: no file-descriptor growth and no ResourceWarning
    over hundreds of failing recordings (A2);
  - all patched shared state is restored.

Every database is a temporary file; real Gemini clients cannot be
constructed; the project database is never opened. Bursts are 20 requests,
each test well inside a 60-second ceiling.

Run directly (no pytest required):
    python tests/test_shadow_adversarial.py
"""
import contextlib
import gc
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
import time
import warnings
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PATIENT = "Adversarial Patient Zephyrine"
BURST = 20
CEILING_SECONDS = 60

# A9 -- completeness of CONFIRM recording. A successful confirmation (its
# PRESCRIPTION_CONFIRMED audit event) is IN a recording-enabled period when the
# clinic's latest activation created at or before the event is SHADOW.
# Timestamps have one-second resolution, so an activation and a confirmation
# in the same second are classified by timestamp order alone: trial reports
# must treat boundary seconds as uncertain.
COMPLETENESS_SQL = """
    SELECT e.prescription_id, e.preview_id,
           EXISTS (SELECT 1 FROM guardrail_shadow_results r WHERE r.stage = 'CONFIRM'
                   AND r.prescription_id = e.prescription_id) AS recorded
    FROM audit_events e
    WHERE e.event_type = 'PRESCRIPTION_CONFIRMED' AND e.clinic_id = ?
      AND (SELECT a.mode FROM guardrail_shadow_activations a
           WHERE a.clinic_id = e.clinic_id AND a.created_at <= e.created_at
           ORDER BY a.activation_id DESC LIMIT 1) = 'SHADOW'
"""

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


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


@contextlib.contextmanager
def _app_on_temp_db():
    import db.database as database
    import backend.main as main_module
    from fastapi.testclient import TestClient
    tmp = tempfile.mkdtemp(prefix="ayurcore_shadow_adv_")
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


def _setup(client, db_path, tag="adv") -> tuple[str, dict]:
    from backend.auth_service import create_user
    clinic = f"{tag}_{secrets.token_hex(4)}"
    username = f"{tag}_test_{secrets.token_hex(6)}"
    password = "shadow-adversarial-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic, role="DOCTOR")
    conn = sqlite3.connect(db_path)
    try:
        for herb in ("Brahmi", "Ashwagandha"):
            conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                         "VALUES (?, ?, 'test botanical name', 10000000, 'TEST-BATCH', 90)", (clinic, herb))
        conn.commit()
    finally:
        conn.close()
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return clinic, {"Authorization": f"Bearer {resp.json()['token']}"}


def _activate(db_path, clinic, mode="SHADOW", ruleset_hash=None) -> int:
    from backend.guardrails import ENGINEERING_BASELINE
    shadow = mode == "SHADOW"
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        activation_id = conn.execute(
            "INSERT INTO guardrail_shadow_activations (clinic_id, mode, ruleset_id, ruleset_version, ruleset_hash, "
            "activated_by, reason, created_at) VALUES (?, ?, ?, ?, ?, 'test operator', 'test', ?)",
            (clinic, mode, ENGINEERING_BASELINE.ruleset_id if shadow else None,
             ENGINEERING_BASELINE.version if shadow else None,
             (ruleset_hash or ENGINEERING_BASELINE.content_hash) if shadow else None, _utc())).lastrowid
        conn.commit()
        return activation_id
    finally:
        conn.close()


def _query(db_path, sql, args=()) -> list[dict]:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _manual(client, headers, dosha="Vata", herbs=None, **extra):
    return client.post("/prescribe/manual-preview", headers=headers, json={
        "patient_name": PATIENT, "dosha_imbalance": dosha,
        "prescribed_herbs": herbs or [{"name": "Brahmi", "grams": 5}], **extra})


def _confirm(client, headers, preview_id):
    return client.post("/prescribe/confirm", headers=headers, json={"preview_id": preview_id})


def _burst(call, n=BURST, ceiling=CEILING_SECONDS):
    """Runs call(client, i) in n daemon threads released together, each with
    its own TestClient, under ONE overall deadline of `ceiling` seconds.
    Returns (results, seconds) only if every worker finished and none raised;
    otherwise fails with the live workers and each worker's exception, so a
    hung or crashed request is reported as itself, never as a later None."""
    import backend.main as main_module
    from fastapi.testclient import TestClient
    results, errors = [None] * n, {}
    barrier = threading.Barrier(n, timeout=ceiling)

    def run(i):
        try:
            own = TestClient(main_module.app)
            barrier.wait()
            results[i] = call(own, i)
        except BaseException as e:          # reported below, never swallowed
            errors[i] = f"{type(e).__name__}: {str(e)[:200]}"

    started = time.perf_counter()
    deadline = started + ceiling
    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, deadline - time.perf_counter()))
    elapsed = time.perf_counter() - started
    alive = [i for i, t in enumerate(threads) if t.is_alive()]
    check(f"burst of {n}: every worker finished within the {ceiling} s overall deadline ({elapsed:.1f} s)",
          alive == [], f"still running: workers {alive}")
    check(f"burst of {n}: no worker raised", errors == {}, errors)
    return results, elapsed


def _open_fds() -> int:
    return len(os.listdir("/dev/fd"))


# --- A7 kill switch ------------------------------------------------------------------------

def test_kill_switch_only_the_exact_value_1_disables_recording():
    import backend.shadow_store as shadow_store
    real_connect = shadow_store._connect
    opened = []

    def counting_connect():
        opened.append(True)
        return real_connect()

    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path)
        _activate(db_path, clinic)
        for value in ("1", " 1 ", "1\n", "\t1", "true", "TRUE", "yes", "0", "", "01"):
            opened.clear()
            with _env("AYURCORE_SHADOW_DISABLED", value), _patched(shadow_store, "_connect", counting_connect):
                check(f"AYURCORE_SHADOW_DISABLED={value!r}: the preview succeeds", _manual(client, headers).status_code == 200)
            disabled = not opened
            check(f"AYURCORE_SHADOW_DISABLED={value!r}: recording {'disabled' if value == '1' else 'enabled'}",
                  disabled == (value == "1"))


# --- A3 concurrency --------------------------------------------------------------------------

def test_concurrent_bursts_never_fail_a_request_and_account_for_every_row():
    with _app_on_temp_db() as (client, db_path):
        clinics = [_setup(client, db_path, "burst_a"), _setup(client, db_path, "burst_b")]
        for mode in ("OFF", "SHADOW"):
            if mode == "SHADOW":
                for clinic, _ in clinics:
                    _activate(db_path, clinic)
            with _captured_logs() as preview_logs:
                previews, preview_secs = _burst(lambda c, i: _manual(c, clinics[i % 2][1]))
            check(f"[{mode}] all {BURST} concurrent previews succeed",
                  [r.status_code for r in previews] == [200] * BURST,
                  [(r.status_code, r.text[:120]) for r in previews if r.status_code != 200])
            drafts = [r.json()["preview_id"] for r in previews]
            with _captured_logs() as confirm_logs:
                confirms, confirm_secs = _burst(lambda c, i: _confirm(c, clinics[i % 2][1], drafts[i]))
            check(f"[{mode}] all {BURST} concurrent confirmations succeed",
                  [r.status_code for r in confirms] == [200] * BURST,
                  [(r.status_code, r.text[:120]) for r in confirms if r.status_code != 200])
            rows = _query(db_path, f"SELECT preview_id, stage FROM guardrail_shadow_results "
                                   f"WHERE preview_id IN ({','.join('?' * BURST)})", drafts)
            check(f"[{mode}] no duplicate rows", len(rows) == len({(r["preview_id"], r["stage"]) for r in rows}))
            if mode == "OFF":
                check("[OFF] nothing recorded", rows == [])
                continue
            for stage, log in (("PREVIEW", preview_logs), ("CONFIRM", confirm_logs)):
                recorded = sum(1 for r in rows if r["stage"] == stage)
                lost = log.getvalue().count(f"event=shadow_record_failed stage={stage}")
                superseded = log.getvalue().count(f"event=shadow_record_superseded stage={stage}")
                check(f"[SHADOW] {stage}: every success is a row or a logged best-effort loss "
                      f"({recorded} rows + {lost} lost + {superseded} superseded = {BURST})",
                      recorded + lost + superseded == BURST)


# --- A4 activation flapping ---------------------------------------------------------------------

def test_rows_are_recorded_only_under_the_latest_activation_while_it_flaps():
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path, "flap")
        conn = sqlite3.connect(db_path)
        try:
            # Observation only, in this temporary database: the clinic's latest
            # activation at the exact moment each shadow row is inserted.
            conn.executescript("""
                CREATE TABLE test_observed (result_id INTEGER, latest_at_insert INTEGER, latest_mode TEXT);
                CREATE TRIGGER test_observe AFTER INSERT ON guardrail_shadow_results BEGIN
                    INSERT INTO test_observed SELECT NEW.result_id,
                        (SELECT MAX(activation_id) FROM guardrail_shadow_activations WHERE clinic_id = NEW.clinic_id),
                        (SELECT mode FROM guardrail_shadow_activations WHERE clinic_id = NEW.clinic_id
                         ORDER BY activation_id DESC LIMIT 1);
                END;""")
        finally:
            conn.close()
        _activate(db_path, clinic)
        stop, flips = threading.Event(), [0]

        def flapper():
            modes = ("OFF", "SHADOW", "SHADOW", "OFF")
            while not stop.is_set():
                try:
                    _activate(db_path, clinic, mode=modes[flips[0] % 4])
                    flips[0] += 1
                except sqlite3.OperationalError:
                    pass
                time.sleep(0.003)

        thread = threading.Thread(target=flapper)
        thread.start()
        try:
            previews, _ = _burst(lambda c, i: _manual(c, headers))
            check("every preview succeeds while the activation flips", [r.status_code for r in previews] == [200] * BURST,
                  [(r.status_code, r.text[:120]) for r in previews if r.status_code != 200])
            drafts = [r.json()["preview_id"] for r in previews]
            confirms, _ = _burst(lambda c, i: _confirm(c, headers, drafts[i]))
            check("every confirmation succeeds while the activation flips", [r.status_code for r in confirms] == [200] * BURST,
                  [(r.status_code, r.text[:120]) for r in confirms if r.status_code != 200])
        finally:
            stop.set()
            thread.join(CEILING_SECONDS)
        check(f"the activation really flipped during the requests ({flips[0]} flips)", flips[0] > 0)
        observed = _query(db_path, """
            SELECT r.activation_id, o.latest_at_insert, o.latest_mode, a.mode
            FROM guardrail_shadow_results r
            JOIN test_observed o ON o.result_id = r.result_id
            JOIN guardrail_shadow_activations a ON a.activation_id = r.activation_id""")
        wrong = [r for r in observed if r["activation_id"] != r["latest_at_insert"]
                 or r["mode"] != "SHADOW" or r["latest_mode"] != "SHADOW"]
        check(f"every row ({len(observed)}) was recorded under the activation that was latest at insert, never OFF",
              wrong == [], wrong[:3])


# --- A5 forged clinic -------------------------------------------------------------------------

def test_a_forged_clinic_id_in_the_body_never_records_in_the_forged_clinic():
    with _app_on_temp_db() as (client, db_path):
        clinic_a, headers_a = _setup(client, db_path, "forge_a")
        clinic_b, _ = _setup(client, db_path, "forge_b")
        _activate(db_path, clinic_b)
        resp = _manual(client, headers_a, clinic_id=clinic_b)
        check("clinic A (OFF) sending clinic B's id: the preview succeeds in clinic A", resp.status_code == 200
              and _query(db_path, "SELECT clinic_id FROM prescription_previews WHERE preview_id = ?",
                         (resp.json()["preview_id"],))[0]["clinic_id"] == clinic_a)
        check("... and nothing is recorded in either clinic",
              _query(db_path, "SELECT 1 FROM guardrail_shadow_results WHERE clinic_id IN (?, ?)", (clinic_a, clinic_b)) == [])
        _activate(db_path, clinic_a)
        resp = _manual(client, headers_a, clinic_id=clinic_b)
        check("with both active, the row is recorded in the caller's clinic", [r["clinic_id"] for r in _query(
            db_path, "SELECT clinic_id FROM guardrail_shadow_results WHERE preview_id = ?",
            (resp.json()["preview_id"],))] == [clinic_a])


# --- A6 unusual and oversized input -------------------------------------------------------------

def test_unusual_and_oversized_inputs_store_no_text_and_stay_bounded():
    import backend.shadow_store as shadow_store
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path, "inputs")
        _activate(db_path, clinic)
        drafts_before = len(_query(db_path, "SELECT 1 FROM prescription_previews"))
        resp = _manual(client, headers, dosha=f"Vata {PATIENT.split()[-1]}")
        check("patient identity in the dosha: 422, no draft and no row", resp.status_code == 422
              and len(_query(db_path, "SELECT 1 FROM prescription_previews")) == drafts_before
              and _query(db_path, "SELECT 1 FROM guardrail_shadow_results") == [])
        for label, dosha in (("emoji", "Vata \U0001F33F\U0001F525"), ("right-to-left", "Vata ‮الدوشا"),
                             ("NUL", "Vata\u0000Pitta"), ("combining", "Váta")):
            resp = _manual(client, headers, dosha=dosha)
            row = _query(db_path, "SELECT * FROM guardrail_shadow_results WHERE preview_id = ?", (resp.json()["preview_id"],))
            text = json.dumps(row, ensure_ascii=False)
            check(f"{label} dosha: recorded as EVALUATED with none of the input text",
                  resp.status_code == 200 and row[0]["status"] == "EVALUATED"
                  and not any(ch in text for ch in dosha if not ch.isascii()) and "Pitta" not in text)
        real_evaluate, timings = shadow_store._evaluate, []

        def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return real_evaluate(*args, **kwargs)
            finally:
                timings.append(time.perf_counter() - started)

        for label, dosha, herbs in (
                ("a 1 MB AI dosha", "V" * 1_000_000, [{"name": "Brahmi", "grams": 5}]),
                ("5,000 AI lines", "Vata", [{"name": ("Brahmi", "brahmi", "Ashwagandha")[i % 3], "grams": 1} for i in range(5000)])):
            timings.clear()
            with _ai_returns(herbs, dosha=dosha), _patched(shadow_store, "_evaluate", timed):
                resp = client.post("/prescribe/preview", headers=headers, json={"patient_name": PATIENT, "notes": "synthetic"})
            row = _query(db_path, "SELECT status, results_json FROM guardrail_shadow_results WHERE preview_id = ?",
                         (resp.json()["preview_id"],))
            check(f"{label}: recorded as EVALUATED with bounded results and no input text",
                  resp.status_code == 200 and row[0]["status"] == "EVALUATED" and len(row[0]["results_json"]) < 4000
                  and "VVVV" not in row[0]["results_json"], (len(row[0]["results_json"]) if row else None))
            check(f"{label}: recorder evaluation under 250 ms (about 10x the Phase A measurement)",
                  timings and timings[0] < 0.25, timings)


# --- A8 privacy -----------------------------------------------------------------------------------

def test_exceptions_carrying_input_text_never_reach_logs_or_rows():
    import backend.guardrails as guardrails
    import backend.shadow_store as shadow_store
    marker = "Xylospectra"

    def raising(exc):
        def fn(*args, **kwargs):
            raise exc
        return fn

    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path, "privacy")
        _activate(db_path, clinic)
        text = f"{marker} Brahmi {PATIENT}"
        paths = [
            ("evaluation", guardrails, "evaluate", RuntimeError(text)),
            ("candidate building", guardrails, "build_confirm_candidate", ValueError(text)),
            ("the database connection", shadow_store, "_connect", sqlite3.OperationalError(text)),
            ("the insert", shadow_store, "_insert_if_latest", sqlite3.IntegrityError(text)),
            ("the whole preview recorder", shadow_store, "record_preview", RuntimeError(text)),
            ("the whole confirm recorder", shadow_store, "record_confirm", RuntimeError(text)),
        ]
        for label, owner, name, exc in paths:
            with _captured_logs() as logs, _patched(owner, name, raising(exc)):
                preview = _manual(client, headers, dosha=f"Vata {marker}")
                confirm = _confirm(client, headers, preview.json()["preview_id"])
            leaked = [t for t in (marker, PATIENT, "Brahmi", "Traceback") if t in logs.getvalue()]
            stored = json.dumps(_query(db_path, "SELECT * FROM guardrail_shadow_results WHERE preview_id = ?",
                                       (preview.json()["preview_id"],)))
            check(f"{label} raising with input text: both requests succeed", preview.status_code == confirm.status_code == 200)
            check(f"... no input or exception text in any log line", leaked == [], leaked)
            persisted = [t for t in (marker, PATIENT, "Brahmi", "brahmi", "Ashwagandha", "Vata") if t in stored]
            check(f"... and no input text -- patient, dosha marker, dosha or herb name -- in the stored rows",
                  persisted == [], persisted)


# --- A9 completeness ------------------------------------------------------------------------------

def test_completeness_query_finds_exactly_the_missing_confirm_rows():
    import backend.shadow_store as shadow_store
    real_connect = shadow_store._connect
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path, "complete")

        def confirm_new():
            return _confirm(client, headers, _manual(client, headers).json()["preview_id"]).json()["prescription_id"]

        before_activation = confirm_new()
        time.sleep(1.1)                                  # one-second timestamp resolution
        _activate(db_path, clinic)
        time.sleep(1.1)
        recorded = confirm_new()
        with _patched(shadow_store, "record_confirm", lambda **kw: None):   # simulated crash after the commit
            crashed = confirm_new()
        blockers = []

        def busy_connection():
            blocker = sqlite3.connect(db_path, check_same_thread=False)
            blocker.execute("BEGIN IMMEDIATE")
            blockers.append(blocker)
            return real_connect()

        draft = _manual(client, headers).json()["preview_id"]
        with _patched(shadow_store, "_connect", busy_connection):
            busy_lost = _confirm(client, headers, draft).json()["prescription_id"]
        for blocker in blockers:
            blocker.rollback()
            blocker.close()
        time.sleep(1.1)
        _activate(db_path, clinic, mode="OFF")
        time.sleep(1.1)
        after_off = confirm_new()

        rows = {r["prescription_id"]: r["recorded"] for r in _query(db_path, COMPLETENESS_SQL, (clinic,))}
        check("in-period confirmations are exactly those made while recording was on",
              set(rows) == {recorded, crashed, busy_lost} and before_activation not in rows and after_off not in rows, rows)
        check("the missing rows found are exactly the simulated crash and the busy-database loss",
              sorted(p for p, r in rows.items() if not r) == sorted([crashed, busy_lost]))
        check("completeness is 1 of 3", (sum(rows.values()), len(rows)) == (1, 3))


# --- A10 tampering -------------------------------------------------------------------------------

def test_engine_code_swapped_during_requests_never_fails_a_request():
    from backend.guardrails import engine
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path, "tamper")
        _activate(db_path, clinic)
        original, stop = engine._finding_order.__code__, threading.Event()

        def tamper():
            while not stop.is_set():
                engine._finding_order.__code__ = (lambda f: (0,)).__code__
                time.sleep(0.002)
                engine._finding_order.__code__ = original
                time.sleep(0.002)

        thread = threading.Thread(target=tamper)
        thread.start()
        try:
            previews, _ = _burst(lambda c, i: _manual(c, headers))
        finally:
            stop.set()
            thread.join()
            engine._finding_order.__code__ = original
        check("all requests succeed while engine code is swapped", [r.status_code for r in previews] == [200] * BURST,
              [(r.status_code, r.text[:120]) for r in previews if r.status_code != 200])
        kinds = {(r["status"], r["error_code"]) for r in _query(db_path, "SELECT status, error_code FROM guardrail_shadow_results")}
        check("rows are only EVALUATED or ENGINE_ERROR / ENGINE_CHANGED",
              kinds <= {("EVALUATED", None), ("ENGINE_ERROR", "ENGINE_CHANGED")}, kinds)
        check("the engine is intact afterwards", engine._engine_intact(engine._ENGINE_BINDINGS, engine._ENGINE_CODES))


# --- A11 triggers against real rows ----------------------------------------------------------------

def test_forged_rows_based_on_a_real_confirmation_are_refused():
    with _app_on_temp_db() as (client, db_path):
        clinic_a, headers_a = _setup(client, db_path, "trig_a")
        clinic_b, headers_b = _setup(client, db_path, "trig_b")
        _activate(db_path, clinic_a)
        activation_b = _activate(db_path, clinic_b)
        draft_a = _manual(client, headers_a).json()["preview_id"]
        _confirm(client, headers_a, draft_a)
        draft_b = _manual(client, headers_b).json()["preview_id"]
        rx_b = _confirm(client, headers_b, draft_b).json()["prescription_id"]
        other_draft_a = _manual(client, headers_a).json()["preview_id"]
        real = _query(db_path, "SELECT * FROM guardrail_shadow_results WHERE preview_id = ? AND stage = 'CONFIRM'", (draft_a,))[0]
        columns = [k for k in real if k != "result_id"]
        insert = (f"INSERT INTO guardrail_shadow_results ({', '.join(columns)}) "
                  f"VALUES ({', '.join('?' * len(columns))})")

        def attempt(sql, args=()):
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(sql, args)
                conn.commit()
                return "ACCEPTED"
            except sqlite3.DatabaseError as e:
                return str(e)
            finally:
                conn.close()

        forged = {
            "a copy into clinic B (A's activation)": {"clinic_id": clinic_b},
            "a copy into clinic B (B's activation)": {"clinic_id": clinic_b, "activation_id": activation_b},
            "A's prescription paired with another draft": {"preview_id": other_draft_a},
            "A's draft paired with B's prescription": {"prescription_id": rx_b},
            "a duplicate of the real row": {},
        }
        for label, changes in forged.items():
            row = {**real, **changes}
            check(f"refused: {label}", attempt(insert, tuple(row[c] for c in columns)) != "ACCEPTED")
        for label, sql in (("an UPDATE of the real row", "UPDATE guardrail_shadow_results SET outcome = 'REJECT'"),
                           ("a DELETE of the real row", "DELETE FROM guardrail_shadow_results"),
                           ("an UPDATE of an activation", "UPDATE guardrail_shadow_activations SET mode = 'OFF'"),
                           ("a DELETE of an activation", "DELETE FROM guardrail_shadow_activations")):
            check(f"refused: {label}", "immutable" in attempt(sql))


# --- A2 connection stability ------------------------------------------------------------------------

def test_failing_recordings_leak_no_connections():
    import backend.shadow_store as shadow_store
    with _app_on_temp_db() as (client, db_path):
        clinic, headers = _setup(client, db_path, "fds")
        _activate(db_path, clinic)
        args = dict(clinic_id=clinic, preview_id="none", is_manual=False, dosha_imbalance="Vata",
                    prescribed_herbs=[{"name": "Brahmi", "grams": 5}])
        for journal, lock in (("DELETE", "BEGIN EXCLUSIVE"), ("WAL", "BEGIN IMMEDIATE")):
            conn = sqlite3.connect(db_path)
            conn.execute(f"PRAGMA journal_mode = {journal}")
            conn.close()
            blocker = sqlite3.connect(db_path)
            blocker.execute(lock)
            try:
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    before = _open_fds()
                    for _ in range(300):
                        shadow_store.record_preview(**args)
                    gc.collect()
                    after = _open_fds()
            finally:
                blocker.rollback()
                blocker.close()
            resource = [w for w in caught if issubclass(w.category, ResourceWarning)]
            check(f"[{journal}] 300 failing recordings: no open-file growth ({before} -> {after}) and no ResourceWarning",
                  after - before <= 2 and resource == [], [str(w.message)[:80] for w in resource][:3])


# --- the burst harness itself --------------------------------------------------------------------

def test_burst_enforces_one_overall_deadline_and_reports_workers():
    """_burst must fail within its overall deadline when workers hang (not
    n x ceiling), name the live workers, and report a worker's exception."""
    def hanging(client, i):
        time.sleep(3)                    # every worker outlives the 1 s deadline
        return i

    started = time.perf_counter()
    try:
        _burst(hanging, n=3, ceiling=1)
        outcome = "no failure"
    except AssertionError as e:
        outcome = str(e)
    elapsed = time.perf_counter() - started
    check("hung workers are reported as still running", "still running: workers [0, 1, 2]" in outcome, outcome)
    check(f"... within the single overall deadline ({elapsed:.1f} s, not 3 x 1 s of joins plus the hang)", elapsed < 2.0)

    def raising(client, i):
        if i == 1:
            raise RuntimeError("synthetic worker failure")
        return i

    try:
        _burst(raising, n=3, ceiling=10)
        outcome = "no failure"
    except AssertionError as e:
        outcome = str(e)
    check("a worker's exception is reported as itself", "RuntimeError: synthetic worker failure" in outcome, outcome)
    results, _ = _burst(lambda client, i: i * 2, n=3, ceiling=10)
    check("a healthy burst returns every worker's result", results == [0, 2, 4], results)
    time.sleep(2.2)                      # let the deliberately hung daemon workers finish before later tests


# --- hygiene ---------------------------------------------------------------------------------------

_ORIGINALS = None


def _shared_state():
    import google.genai as genai_real
    import backend.guardrails as guardrails
    import backend.main as main_module
    import backend.shadow_store as shadow_store
    from backend.guardrails import engine
    return {
        "shadow_store._connect": shadow_store._connect, "shadow_store._evaluate": shadow_store._evaluate,
        "shadow_store._insert_if_latest": shadow_store._insert_if_latest,
        "shadow_store.record_preview": shadow_store.record_preview, "shadow_store.record_confirm": shadow_store.record_confirm,
        "guardrails.evaluate": guardrails.evaluate, "guardrails.build_confirm_candidate": guardrails.build_confirm_candidate,
        "engine._finding_order.__code__": engine._finding_order.__code__,
        "main.parse_clinical_notes": main_module.parse_clinical_notes, "genai.Client": genai_real.Client,
        "env AYURCORE_SHADOW_DISABLED": os.environ.get("AYURCORE_SHADOW_DISABLED"),
    }


_ORIGINALS = _shared_state()


def test_zz_shared_module_state_is_restored():
    now = _shared_state()
    changed = sorted(k for k in _ORIGINALS if now[k] is not _ORIGINALS[k])
    check("all patched shared state was restored", changed == [], changed)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
