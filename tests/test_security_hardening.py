"""
Security-review hardening (post Phase 1B).

  3.1  patient identity cannot reach the audit trail / corrections through
       the free-text dosha field: AI output echoing a [[PAT_...]] token is
       rejected (never rehydrated into dosha), and dosha text containing the
       patient's name/phone/ID is rejected on AI preview, manual preview
       and confirm -- with nothing written.
  3.2  encounter audit events carry a random encounter_ref (migration 013),
       not the global sequential encounter_pk; confirm events carry no
       encounter reference.
  2.1  a database-lock timeout is a clean 503 DATABASE_BUSY with nothing
       written (not a framework 500), and create_preview no longer retries a
       locked insert five times.
  2.2  the per-request last_seen_at write fails fast instead of waiting out
       the busy timeout.
  1.1  the migration runner's integrity guard rolls back an entire batch
       that would break any audit chain, including CONTENT changes.
  4.1  verify_backup requires every table the backup's schema version
       implies and verifies the backup's audit chain.
  audit  audit_events is append-only (migration 014) and scripts/audit_anchor.py
       detects tail truncation, rewrites and forged checkpoints.
  multi-process  close vs confirm race across two OS processes on one file.

Lock and multi-process tests run against a temporary database (the app's
db.database.DB_PATH is patched and always restored), never the shared one.

Run directly (no pytest required):
    python tests/test_security_hardening.py
"""
import contextlib
import json
import multiprocessing
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

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


# --- helpers ------------------------------------------------------------------

def _app():
    import backend.main as main_module
    return main_module


def _client(**kwargs):
    from fastapi.testclient import TestClient
    return TestClient(_app().app, **kwargs)


def _login(client, clinic_id: str, role: str = "DOCTOR") -> tuple[dict, int]:
    from backend.auth_service import create_user
    username = f"sec_{secrets.token_hex(6)}"
    password = "sec-test-password-not-a-real-credential"
    user_id = create_user(username, password, clinic_id=clinic_id, role=role)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}, user_id


def _db_path() -> str:
    import db.database as database
    return database.DB_PATH


def _query(sql, args=()):
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _count(table, where="1=1", args=()):
    return _query(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", args)[0]["n"]


def _stock(clinic_id, grams=1000):
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                     "VALUES (?, 'Ashwagandha', 'test botanical name', ?, 'TEST-BATCH', 90)", (clinic_id, grams))
        conn.commit()
    finally:
        conn.close()


def _register(client, headers, name="Meera Pillai", op=None, phone="9847012345"):
    resp = client.post("/patients", json={"clinic_patient_id": op or f"OP-{secrets.token_hex(3)}",
                                          "display_name": name, "phone": phone}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


@contextlib.contextmanager
def _temp_app_db():
    """Points the whole application at a fresh temporary database for the
    duration (startup migrations run on it), then restores the real path."""
    import db.database as database
    tmp = tempfile.mkdtemp(prefix="ayurcore_sec_")
    path = os.path.join(tmp, "app.db")
    saved = database.DB_PATH
    database.DB_PATH = path
    try:
        yield path
    finally:
        database.DB_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)


class _FakeParse:
    """Replaces the AI parser; `dosha` may reference the notes' token."""

    def __init__(self, dosha_fn):
        self.dosha_fn = dosha_fn
        self.calls = 0

    def __enter__(self):
        import google.genai as genai_real
        from backend.schemas import ClinicalParse
        main_module = _app()

        def _parse(notes, clinic_id=None):
            self.calls += 1
            token = (re.search(r"\[\[PAT_[0-9A-Fa-f]+\]\]", notes) or [None])[0]
            return ClinicalParse(dosha_imbalance=self.dosha_fn(token),
                                 prescribed_herbs=[{"name": "Ashwagandha", "grams": 5}])

        def _boom(*a, **k):
            raise AssertionError("no real Gemini client in these tests")

        self._saved = (genai_real.Client, main_module.parse_clinical_notes)
        genai_real.Client, main_module.parse_clinical_notes = _boom, _parse
        return self

    def __exit__(self, *exc):
        import google.genai as genai_real
        genai_real.Client, _app().parse_clinical_notes = self._saved
        return False


# --- 3.1 identity in free-text dosha ------------------------------------------------

def test_ai_output_echoing_an_identity_token_is_rejected_not_rehydrated():
    with _client() as client:
        clinic = f"sec_ai_{secrets.token_hex(4)}"
        doctor, _ = _login(client, clinic)
        patient = _register(client, doctor, "Zebulon Quartermaine")
        body = {"patient_name": "x", "patient_pk": patient["patient_pk"],
                "notes": "Zebulon Quartermaine, vata. Ashwagandha 5g."}
        before = _count("prescription_previews", "clinic_id = ?", (clinic,))
        with _FakeParse(lambda tok: f"Vata {tok}"):
            echoed = client.post("/prescribe/preview", json=body, headers=doctor)
        check("AI reply echoing [[PAT_...]] into dosha -> 422 (never rehydrated)",
              echoed.status_code == 422 and echoed.json() == {"detail": "Invalid AI schema returned"}, echoed.text)
        with _FakeParse(lambda tok: "Vata - Zebulon"):
            named = client.post("/prescribe/preview", json=body, headers=doctor)
        check("AI reply naming the patient in dosha -> 422", named.status_code == 422, named.text)
        check("the name never appears in a response", "Zebulon" not in echoed.text + named.text)
        check("nothing was written", _count("prescription_previews", "clinic_id = ?", (clinic,)) == before)
        with _FakeParse(lambda tok: "Vata-Kapha"):
            ok = client.post("/prescribe/preview", json=body, headers=doctor)
        check("a clean AI reply still works", ok.status_code == 200 and ok.json()["dosha_imbalance"] == "Vata-Kapha", ok.text)


def test_identity_in_manual_or_confirm_dosha_is_rejected_with_nothing_written():
    with _client() as client:
        clinic = f"sec_manual_{secrets.token_hex(4)}"
        doctor, _ = _login(client, clinic)
        auditor, _ = _login(client, clinic, "AUDITOR")
        _stock(clinic)
        patient = _register(client, doctor, "Kap Menon", op="OP-7781", phone="9847012345")

        def manual(dosha, **extra):
            return client.post("/prescribe/manual-preview", json={
                "patient_name": "Typed Person", "patient_phone": "5550001111", "dosha_imbalance": dosha,
                "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}], **extra}, headers=doctor)

        for dosha, why in (("Vata - Menon", "registry surname"), ("vata kap menon", "registry full name"),
                           ("Pitta 9847012345", "registry phone"), ("Vata (OP-7781)", "registry OP id"),
                           ("Vata Typed", "request name part"), ("Vata 5550001111", "request phone")):
            resp = manual(dosha, patient_pk=patient["patient_pk"])
            check(f"manual dosha with {why} -> 422",
                  resp.status_code == 422
                  and resp.json() == {"detail": "Dosha must not contain patient identity (name, phone or ID)."}, resp.text)
        check("no preview written by the rejected requests", _count("prescription_previews", "clinic_id = ?", (clinic,)) == 0)
        check("a name part inside a clinical word is NOT a match ('Kap' vs 'Kapha')",
              manual("Vata-Kapha", patient_pk=patient["patient_pk"]).status_code == 200)

        prev = manual("Vata", patient_pk=patient["patient_pk"]).json()
        before = {t: _count(t, "clinic_id = ?", (clinic,)) for t in ("prescriptions", "audit_events", "idempotency_keys", "corrections")}
        conf = client.post("/prescribe/confirm", json={"preview_id": prev["preview_id"], "dosha_imbalance": "Vata, Kap Menon"},
                           headers={**doctor, "Idempotency-Key": "k-" + secrets.token_hex(4)})
        check("confirm correction naming the patient -> 422", conf.status_code == 422, conf.text)
        check("nothing written by the rejected confirm",
              before == {t: _count(t, "clinic_id = ?", (clinic,)) for t in before}
              and _query("SELECT stock_grams FROM inventory WHERE clinic_id = ?", (clinic,))[0]["stock_grams"] == 1000)
        ok = client.post("/prescribe/confirm", json={"preview_id": prev["preview_id"], "dosha_imbalance": "Vata-Pitta"}, headers=doctor)
        check("the same draft confirms with a clean correction", ok.status_code == 200, ok.text)
        for path in ("/audit/events", "/audit/export"):
            text = client.get(path, headers=auditor).text
            for secret in ("Menon", "9847012345", "OP-7781", "Typed Person", "5550001111"):
                check(f"AUDITOR {path} contains no {secret!r}", secret not in text)


# --- 3.2 encounter references in audit ------------------------------------------------

def test_encounter_audit_events_use_random_refs_and_confirms_carry_none():
    with _client() as client:
        clinic = f"sec_ref_{secrets.token_hex(4)}"
        doctor, _ = _login(client, clinic)
        auditor, _ = _login(client, clinic, "AUDITOR")
        _stock(clinic)
        encounters = []
        for _ in range(3):
            p = _register(client, doctor, "Ref Patient")
            enc = client.post(f"/patients/{p['patient_pk']}/encounters", headers=doctor).json()
            encounters.append((p, enc))
        p0, e0 = encounters[0]
        prev = client.post("/prescribe/manual-preview", json={
            "patient_name": "x", "patient_pk": p0["patient_pk"], "encounter_pk": e0["encounter_pk"],
            "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]}, headers=doctor).json()
        client.post("/prescribe/confirm", json={"preview_id": prev["preview_id"]}, headers=doctor)
        client.post(f"/encounters/{e0['encounter_pk']}/close", headers=doctor)

        refs = [e["encounter_ref"] for _, e in encounters]
        check("every encounter has a distinct 16-hex random ref",
              len(set(refs)) == 3 and all(re.fullmatch(r"[0-9a-f]{16}", r) for r in refs), refs)
        events = client.get("/audit/events", headers=auditor).json()["events"]
        enc_events = [e for e in events if e["event_type"].startswith("ENCOUNTER_")]
        check("encounter events carry only encounter_ref, matching the encounter",
              [e["event_data"] for e in enc_events]
              == [{"encounter_ref": r} for r in refs] + [{"encounter_ref": refs[0]}], [e["event_data"] for e in enc_events])
        confirm = [e for e in events if e["event_type"] == "PRESCRIPTION_CONFIRMED"]
        check("the confirm event carries no encounter reference at all",
              len(confirm) == 1 and not any("encounter" in k for k in confirm[0]["event_data"]), confirm)
        text = json.dumps(events)
        check("no sequential encounter_pk appears in any new audit event",
              '"encounter_pk"' not in text, [e["event_data"] for e in enc_events])


def test_migration_013_backfills_refs_and_keeps_encounter_rules():
    from db.migrations import discover_migrations, run_migrations
    from backend.audit_store import audit_chain_validity

    with tempfile.TemporaryDirectory() as up_to_012:
        for m in discover_migrations():
            if m.version <= 12:
                shutil.copy(m.path, os.path.join(up_to_012, m.filename))
        path = os.path.join(up_to_012, "db.sqlite")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            run_migrations(conn=conn, migrations_dir=up_to_012)
            conn.execute("INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
                         "VALUES ('u', 'x', 'c1', 'DOCTOR', 1, 't', 't')")
            for i in (1, 2):
                conn.execute("INSERT INTO patients (clinic_id, clinic_patient_id, display_name, created_by_user_id, created_at) "
                             "VALUES ('c1', ?, 'P', 1, 't')", (f"OP-{i}",))
            conn.execute("INSERT INTO encounters (clinic_id, patient_pk, created_by_user_id, status, created_at) VALUES ('c1', 1, 1, 'OPEN', 't')")
            conn.execute("INSERT INTO encounters (clinic_id, patient_pk, created_by_user_id, status, created_at, closed_at) "
                         "VALUES ('c1', 2, 1, 'CLOSED', 't', 't2')")
            conn.commit()
            applied = run_migrations(conn=conn, integrity_guard=audit_chain_validity)
            check("upgrade from 012 applies 013 and 014 under the integrity guard", applied[:2] == [13, 14], applied)
            refs = [r[0] for r in conn.execute("SELECT encounter_ref FROM encounters ORDER BY encounter_pk")]
            check("both existing encounters (OPEN and CLOSED) were backfilled with distinct refs",
                  len(set(refs)) == 2 and all(r and re.fullmatch(r"[0-9a-f]{16}", r) for r in refs), refs)

            def blocked(sql, reason):
                try:
                    conn.execute(sql)
                except sqlite3.IntegrityError as e:
                    return reason in str(e)
                return False

            check("a CLOSED encounter is still final after the backfill",
                  blocked("UPDATE encounters SET closed_at = 'x' WHERE encounter_pk = 2", "encounter_closed_is_final"))
            check("encounter_ref is immutable",
                  blocked("UPDATE encounters SET encounter_ref = '0123456789abcdef' WHERE encounter_pk = 1", "encounter_identity_immutable"))
            check("a new encounter without a ref is rejected",
                  blocked("INSERT INTO encounters (clinic_id, patient_pk, created_by_user_id, status, created_at, closed_at) "
                          "VALUES ('c1', 2, 1, 'CLOSED', 't', 't')", "encounter_ref_invalid"))
            check("a malformed ref is rejected",
                  blocked("INSERT INTO encounters (clinic_id, patient_pk, created_by_user_id, status, created_at, closed_at, encounter_ref) "
                          "VALUES ('c1', 2, 1, 'CLOSED', 't', 't', 'NOT-HEX-REF-1234')", "encounter_ref_invalid"))
        finally:
            conn.close()


# --- 2.1 / 2.2 database busy ---------------------------------------------------------------

def test_operational_errors_map_to_503_busy_or_generic_500_without_detail():
    import backend.main as main_module
    with _client(raise_server_exceptions=False) as client:
        clinic = f"sec_busy_{secrets.token_hex(4)}"
        doctor, _ = _login(client, clinic)
        original = main_module.close_encounter
        try:
            main_module.close_encounter = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked"))
            busy = client.post("/encounters/1/close", headers=doctor)
            main_module.close_encounter = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("no such column: secret_internal_name"))
            other = client.post("/encounters/1/close", headers=doctor)
        finally:
            main_module.close_encounter = original
        check("'database is locked' -> 503 with retry guidance and a machine-readable code",
              busy.status_code == 503 and busy.headers.get("X-Error-Code") == "DATABASE_BUSY"
              and busy.headers.get("Retry-After") == "5"
              and busy.json() == {"detail": "The system is busy. Nothing was saved. Please try again."}, (busy.status_code, busy.text))
        check("any other OperationalError -> generic 500 with no internal detail",
              other.status_code == 500 and "secret_internal_name" not in other.text, other.text)


def test_real_lock_contention_is_503_fast_with_nothing_written():
    from backend.auth_service import resolve_session

    with _temp_app_db() as path, _client(raise_server_exceptions=False) as client:
        clinic = "sec_lock"
        doctor, _ = _login(client, clinic)
        _stock(clinic)
        patient = _register(client, doctor, "Lock Patient")
        enc = client.post(f"/patients/{patient['patient_pk']}/encounters", headers=doctor).json()
        prev = client.post("/prescribe/manual-preview", json={
            "patient_name": "x", "patient_pk": patient["patient_pk"], "encounter_pk": enc["encounter_pk"],
            "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]}, headers=doctor).json()

        locker = sqlite3.connect(path, timeout=1)
        locker.execute("BEGIN IMMEDIATE")
        try:
            t0 = time.monotonic()
            session = resolve_session(doctor["Authorization"].split(" ", 1)[1])
            auth_elapsed = time.monotonic() - t0
            check("2.2: authentication does not wait for the write lock", session is not None and auth_elapsed < 2.0, auth_elapsed)

            results = {}

            def call(name, method, url, body):
                t = time.monotonic()
                r = client.request(method, url, json=body, headers={**doctor, "Idempotency-Key": "lock-key"} if "confirm" in url else doctor)
                results[name] = (r, time.monotonic() - t)

            threads = [
                threading.Thread(target=call, args=("confirm", "POST", "/prescribe/confirm", {"preview_id": prev["preview_id"]})),
                threading.Thread(target=call, args=("manual", "POST", "/prescribe/manual-preview", {
                    "patient_name": "x", "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]})),
                threading.Thread(target=call, args=("close", "POST", f"/encounters/{enc['encounter_pk']}/close", None)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            locker.rollback()
            locker.close()
        for name, (resp, elapsed) in sorted(results.items()):
            check(f"2.1: {name} under a held lock -> 503 DATABASE_BUSY",
                  resp.status_code == 503 and resp.headers.get("X-Error-Code") == "DATABASE_BUSY", (resp.status_code, resp.text))
            check(f"{name} gave up after one busy timeout, not several (~15s, was up to 31s/75s)", elapsed < 22, elapsed)
        check("nothing was written: no prescription, audit event beyond the open, idempotency row or preview",
              _count("prescriptions") == 0 and _count("idempotency_keys") == 0 and _count("prescription_previews") == 1
              and [r["event_type"] for r in _query("SELECT event_type FROM audit_events")] == ["ENCOUNTER_OPENED"]
              and _query("SELECT stock_grams FROM inventory")[0]["stock_grams"] == 1000)
        check("after the lock is released the same confirm succeeds",
              client.post("/prescribe/confirm", json={"preview_id": prev["preview_id"]},
                          headers={**doctor, "Idempotency-Key": "lock-key"}).status_code == 200)


# --- 1.1 migration integrity guard ------------------------------------------------------------

def _db_with_one_audit_event(migrations_dir):
    from backend.audit_store import build_prescription_confirmed_event_data, create_audit_event
    from db.migrations import run_migrations
    path = os.path.join(migrations_dir, "db.sqlite")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn=conn, migrations_dir=migrations_dir)
    conn.execute("INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
                 "VALUES ('u', 'x', 'c1', 'DOCTOR', 1, 't', 't')")
    conn.execute("INSERT INTO prescription_previews (preview_id, clinic_id, patient_name, is_manual, ai_dosha_imbalance, "
                 "ai_prescribed_herbs_json, created_at, expires_at) VALUES ('p', 'c1', 'x', 1, 'Vata', '[]', 't', 't')")
    conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status) VALUES ('c1', 'x', 'd', 'Active')")
    create_audit_event(conn, clinic_id="c1", actor_user_id=1, event_type="PRESCRIPTION_CONFIRMED", prescription_id=1,
                       preview_id="p", event_data=build_prescription_confirmed_event_data(
                           is_manual=True, ai_dosha_imbalance=None, ai_prescribed_herbs=None,
                           confirmed_dosha_imbalance="Vata", confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 5}]))
    conn.commit()
    return conn


def test_migration_guard_rolls_back_a_batch_that_alters_audit_content():
    import inspect
    from backend.audit_store import audit_chain_validity
    from db.migrations import MigrationIntegrityError, discover_migrations, run_migrations

    check("app startup runs migrations under the audit integrity guard",
          "integrity_guard=audit_chain_validity" in inspect.getsource(_app()._apply_pending_migrations))
    with tempfile.TemporaryDirectory() as mdir:
        for m in discover_migrations():
            if m.version <= 13:  # before 014, so a raw UPDATE of audit_events is still possible
                shutil.copy(m.path, os.path.join(mdir, m.filename))
        conn = _db_with_one_audit_event(mdir)
        try:
            with open(os.path.join(mdir, "014_harmless.py"), "w") as f:
                f.write("def upgrade(conn):\n    conn.execute('CREATE TABLE harmless (x)')\n")
            with open(os.path.join(mdir, "015_content_only_edit.py"), "w") as f:
                # Leaves ids and hashes alone -- exactly the edit migration 011's own self-check could not see.
                f.write("def upgrade(conn):\n    conn.execute(\"UPDATE audit_events SET created_at = 'rewritten'\")\n")
            try:
                run_migrations(conn=conn, migrations_dir=mdir, integrity_guard=audit_chain_validity)
                raised = False
            except MigrationIntegrityError:
                raised = True
            check("guard rejects a content-only audit edit", raised)
            check("the audit event is untouched", conn.execute("SELECT created_at FROM audit_events").fetchone()[0] != "rewritten")
            check("the WHOLE batch rolled back, including the harmless migration",
                  conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version >= 14").fetchone()[0] == 0
                  and conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'harmless'").fetchone()[0] == 0)
            os.remove(os.path.join(mdir, "015_content_only_edit.py"))
            applied = run_migrations(conn=conn, migrations_dir=mdir, integrity_guard=audit_chain_validity)
            check("a clean batch passes the guard", applied == [14], applied)
        finally:
            conn.close()


# --- 4.1 verify_backup ---------------------------------------------------------------------------

def test_verify_backup_requires_version_tables_and_a_valid_audit_chain():
    from db.migrations import discover_migrations, run_migrations
    from scripts.backup_db import backup_database, verify_backup

    with tempfile.TemporaryDirectory() as tmp:
        live = os.path.join(tmp, "live.db")
        conn = sqlite3.connect(live)
        run_migrations(conn=conn)
        conn.close()

        def backup(name):
            dest = os.path.join(tmp, name)
            backup_database(live, dest)
            return dest

        ok = verify_backup(backup("ok.db"))
        check("a normal backup verifies", ok.ok and {"patients", "encounters"} <= set(ok.table_row_counts), ok.reason)

        no_enc = backup("no_encounters.db")
        c = sqlite3.connect(no_enc)
        c.execute("DROP TABLE encounters")
        c.commit()
        c.close()
        r = verify_backup(no_enc)
        check("a backup missing a table its schema version requires fails -- even with no dangling links",
              not r.ok and "encounters" in r.reason, r.reason)

        no_meta = backup("no_schema_migrations.db")
        c = sqlite3.connect(no_meta)
        c.execute("DROP TABLE schema_migrations")
        c.commit()
        c.close()
        r = verify_backup(no_meta)
        check("a backup without schema_migrations fails", not r.ok and "schema_migrations" in r.reason, r.reason)

        old_dir = os.path.join(tmp, "m008")
        os.mkdir(old_dir)
        for m in discover_migrations():
            if m.version <= 8:
                shutil.copy(m.path, os.path.join(old_dir, m.filename))
        old = os.path.join(tmp, "old.db")
        c = sqlite3.connect(old)
        run_migrations(conn=c, migrations_dir=old_dir)
        c.close()
        r = verify_backup(old)
        check("an older-schema backup (v8, no patients/encounters) still verifies", r.ok, r.reason)

        tampered = backup("tampered.db")
        c = _db_with_one_audit_event_at(tampered)
        c.execute("DROP TRIGGER audit_events_no_update")
        c.execute("UPDATE audit_events SET created_at = 'edited'")
        c.commit()
        c.close()
        r = verify_backup(tampered)
        check("a backup with a tampered audit chain fails", not r.ok and "audit hash chain invalid" in r.reason, r.reason)


def _db_with_one_audit_event_at(path):
    from backend.audit_store import build_prescription_confirmed_event_data, create_audit_event
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
                 "VALUES ('u', 'x', 'c1', 'DOCTOR', 1, 't', 't')")
    conn.execute("INSERT INTO prescription_previews (preview_id, clinic_id, patient_name, is_manual, ai_dosha_imbalance, "
                 "ai_prescribed_herbs_json, created_at, expires_at) VALUES ('p', 'c1', 'x', 1, 'Vata', '[]', 't', 't')")
    conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status) VALUES ('c1', 'x', 'd', 'Active')")
    create_audit_event(conn, clinic_id="c1", actor_user_id=1, event_type="PRESCRIPTION_CONFIRMED", prescription_id=1,
                       preview_id="p", event_data={"decision_source": "MANUAL"})
    conn.commit()
    return conn


# --- audit: append-only triggers and the external anchor ---------------------------------------

def test_audit_events_are_append_only_on_the_real_schema():
    conn = sqlite3.connect(DB_PATH)
    try:
        for sql in ("UPDATE audit_events SET event_hash = 'x' WHERE event_id = (SELECT MIN(event_id) FROM audit_events)",
                    "DELETE FROM audit_events WHERE event_id = (SELECT MAX(event_id) FROM audit_events)"):
            try:
                conn.execute(sql)
                blocked = False
            except sqlite3.IntegrityError as e:
                blocked = "audit_events_append_only" in str(e)
            conn.rollback()
            check(f"blocked: {sql.split()[0]} on audit_events", blocked)
    finally:
        conn.close()


def test_audit_anchor_detects_truncation_rewrite_and_forgery():
    from scripts.audit_anchor import AnchorError, checkpoint, verify
    from backend.audit_store import create_audit_event
    from db.migrations import run_migrations

    key = secrets.token_hex(32)
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "a.db")
        anchors = os.path.join(tmp, "anchors.jsonl")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        run_migrations(conn=conn)
        conn.execute("INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
                     "VALUES ('u', 'x', 'c1', 'DOCTOR', 1, 't', 't')")
        for i in range(4):
            conn.execute("INSERT INTO prescription_previews (preview_id, clinic_id, patient_name, is_manual, ai_dosha_imbalance, "
                         "ai_prescribed_herbs_json, created_at, expires_at) VALUES (?, 'c1', 'x', 1, 'Vata', '[]', 't', 't')", (f"p{i}",))
            conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status) VALUES ('c1', 'x', 'd', 'Active')")
            create_audit_event(conn, clinic_id="c1", actor_user_id=1, event_type="PRESCRIPTION_CONFIRMED",
                               prescription_id=i + 1, preview_id=f"p{i}", event_data={"n": i})
            conn.commit()

        def raw(sql):  # a raw-file attacker: drop the append-only triggers, edit, restore them
            trig = {r["name"]: r["sql"] for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='audit_events'")}
            for name in trig:
                conn.execute(f"DROP TRIGGER {name}")
            conn.execute(sql)
            for body in trig.values():
                conn.execute(body)
            conn.commit()

        try:
            records = checkpoint(anchors, key=key, db_path=db)
            check("checkpoint records the clinic head", records[0]["event_count"] == 4 and records[0]["last_event_id"] == 4, records)
            check("an untouched database verifies", verify(anchors, key=key, db_path=db).valid)

            snapshot = [tuple(r) for r in conn.execute("SELECT * FROM audit_events ORDER BY event_id")]
            raw("DELETE FROM audit_events WHERE event_id = 4")
            check("the remaining chain still verifies on its own (why an anchor is needed)",
                  __import__("backend.audit_store", fromlist=["x"]).verify_audit_integrity("c1", conn=conn).valid)
            r = verify(anchors, key=key, db_path=db)
            check("tail truncation past the anchor is detected", not r.valid and "missing" in " ".join(r.violations), r.violations)

            raw("DELETE FROM audit_events")
            conn.executemany("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?)", snapshot)
            conn.commit()
            check("restored database verifies again", verify(anchors, key=key, db_path=db).valid)

            raw("UPDATE audit_events SET event_hash = 'forged' WHERE event_id = 4")
            r = verify(anchors, key=key, db_path=db)
            check("a rewritten anchored hash is detected", not r.valid and "hash changed" in " ".join(r.violations), r.violations)
            raw("DELETE FROM audit_events")
            conn.executemany("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?)", snapshot)
            conn.commit()

            with open(anchors) as f:
                lines = f.readlines()
            forged = json.loads(lines[0])
            forged["record"]["event_count"] = 3
            with open(anchors, "w") as f:
                f.write(json.dumps(forged) + "\n")
            r = verify(anchors, key=key, db_path=db)
            check("an edited checkpoint line fails its signature", not r.valid and "signature" in " ".join(r.violations), r.violations)
            with open(anchors, "w") as f:
                f.writelines(lines)
            check("the wrong key cannot verify the checkpoints", not verify(anchors, key=secrets.token_hex(32), db_path=db).valid)
            try:
                checkpoint(anchors, key="short", db_path=db)
                short_rejected = False
            except AnchorError:
                short_rejected = True
            check("a short key is refused", short_rejected)
            check("no patient data is written to the anchor file",
                  set(json.loads(lines[0])["record"]) == {"clinic_id", "event_count", "last_event_id", "last_event_hash", "anchored_at"})
        finally:
            conn.close()


# --- multi-process race -------------------------------------------------------------------------

def test_close_vs_confirm_race_across_two_processes():
    import _mp_race_worker as worker
    from backend.audit_store import verify_audit_integrity

    ctx = multiprocessing.get_context("spawn")
    with _temp_app_db() as path, _client() as client, ctx.Pool(2) as pool:
        pool.map(worker.warm_up, [path, path])
        clinic = "sec_mp"
        doctor, _ = _login(client, clinic)
        outcomes = []
        for round_ in range(6):
            _stock_clinic = f"{clinic}"  # one clinic; stock checked per round via deltas
            if round_ == 0:
                _stock(clinic, 1000)
            patient = _register(client, doctor, f"MP Patient {round_}")
            enc = client.post(f"/patients/{patient['patient_pk']}/encounters", headers=doctor).json()
            prev = client.post("/prescribe/manual-preview", json={
                "patient_name": "x", "patient_pk": patient["patient_pk"], "encounter_pk": enc["encounter_pk"],
                "dosha_imbalance": "Vata", "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]}, headers=doctor).json()
            stock_before = _query("SELECT stock_grams FROM inventory WHERE clinic_id = ?", (clinic,))[0]["stock_grams"]
            start_at = time.time() + 0.4
            confirm = pool.apply_async(worker.race_request, (path, "POST", "/prescribe/confirm", doctor,
                                                             {"preview_id": prev["preview_id"]}, start_at))
            close = pool.apply_async(worker.race_request, (path, "POST", f"/encounters/{enc['encounter_pk']}/close", doctor,
                                                           None, start_at))
            (c_status, c_body), (x_status, _) = confirm.get(60), close.get(60)
            events = [r["event_type"] for r in _query(
                "SELECT event_type FROM audit_events WHERE event_data_json LIKE ? OR prescription_id IN "
                "(SELECT id FROM prescriptions WHERE encounter_pk = ?) ORDER BY event_id",
                (f'%{enc["encounter_ref"]}%', enc["encounter_pk"]))]
            rx = _count("prescriptions", "encounter_pk = ?", (enc["encounter_pk"],))
            stock_after = _query("SELECT stock_grams FROM inventory WHERE clinic_id = ?", (clinic,))[0]["stock_grams"]
            check(f"[round {round_}] close always succeeds", x_status == 200, x_status)
            if c_status == 200:
                outcomes.append("confirm_first")
                check(f"[round {round_}] confirm first: one prescription, confirmed before the close, stock -5",
                      rx == 1 and events == ["ENCOUNTER_OPENED", "PRESCRIPTION_CONFIRMED", "ENCOUNTER_CLOSED"]
                      and stock_after == stock_before - 5, (events, stock_before, stock_after))
            else:
                outcomes.append("close_first")
                check(f"[round {round_}] close first: confirm refused, nothing written",
                      c_status == 409 and rx == 0 and events == ["ENCOUNTER_OPENED", "ENCOUNTER_CLOSED"]
                      and stock_after == stock_before, (c_status, c_body, events))
        check("every chain on the shared file verifies after the cross-process races",
              verify_audit_integrity(None).valid, outcomes)
        print(f"   cross-process outcomes: {outcomes}")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
