"""
Encounter Phase 1B -- previews and prescriptions linked to an encounter,
plus encounter lifecycle auditing (migrations 011 and 012).

Adversarial coverage:
  - migration 011 rebuilds audit_events without changing a single existing
    row, and the unchanged verifier still accepts every chain; the new
    CHECK keeps PRESCRIPTION_CONFIRMED exactly as strict as before;
  - migration 012's write-time rules (clinic, patient, OPEN, immutability)
    hold with foreign keys ON and OFF;
  - cross-clinic and nonexistent encounters -> identical 404, cross-patient
    and closed encounters -> 409, encounter without patient -> 422 -- all
    before any AI call, with nothing written;
  - confirm inherits the encounter from the stored preview only;
  - invalid lifecycle: an encounter closed after the draft blocks the
    confirm atomically (no stock, prescription, audit or idempotency row);
  - replay after the encounter closed returns the original response;
  - restart: a persisted draft keeps its encounter across an app restart;
  - concurrent confirms commit exactly once; a close racing a confirm is
    always serialized consistently;
  - backward compatibility: no-encounter previews/prescriptions/audit
    events are exactly as before;
  - audit output gains no patient PII.

Run directly (no pytest required):
    python tests/test_encounter_linkage.py
"""
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

def _clinic(tag: str) -> str:
    return f"encl_{tag}_{secrets.token_hex(4)}"


def _login(client, clinic_id: str, role: str = "DOCTOR") -> tuple[dict, int]:
    from backend.auth_service import create_user

    username = f"encl_test_{secrets.token_hex(6)}"
    password = "encl-test-password-not-a-real-credential"
    user_id = create_user(username, password, clinic_id=clinic_id, role=role)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}, user_id


def _register(client, headers, display_name="Linkage Patient", phone="9000000000"):
    resp = client.post("/patients", json={"clinic_patient_id": f"OP-{secrets.token_hex(3)}",
                                          "display_name": display_name, "phone": phone}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _open(client, headers, patient_pk) -> dict:
    resp = client.post(f"/patients/{patient_pk}/encounters", headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _close(client, headers, encounter_pk):
    return client.post(f"/encounters/{encounter_pk}/close", headers=headers)


def _set_stock(clinic_id: str, grams: int = 1000):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                     "VALUES (?, 'Ashwagandha', 'test botanical name', ?, 'TEST-BATCH', 90)", (clinic_id, grams))
        conn.commit()
    finally:
        conn.close()


def _manual(client, headers, patient_pk=None, encounter_pk=None, grams=5):
    body = {"patient_name": "Typed Name", "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": grams}]}
    if patient_pk is not None:
        body["patient_pk"] = patient_pk
    if encounter_pk is not None:
        body["encounter_pk"] = encounter_pk
    return client.post("/prescribe/manual-preview", json=body, headers=headers)


def _confirm(client, headers, preview_id, key=None, **extra):
    h = dict(headers)
    if key:
        h["Idempotency-Key"] = key
    return client.post("/prescribe/confirm", json={"preview_id": preview_id, **extra}, headers=h)


def _query(sql: str, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _count(table: str, where: str = "1=1", args=()) -> int:
    return _query(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", args)[0]["n"]


def _stock(clinic_id: str) -> int:
    return _query("SELECT stock_grams FROM inventory WHERE clinic_id = ?", (clinic_id,))[0]["stock_grams"]


def _app_client():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    return TestClient(main_module.app)


class _NoAI:
    """Replaces the AI parser/client; counts calls so tests can prove a
    rejected request never reached it."""

    def __init__(self):
        self.calls = 0

    def __enter__(self):
        import google.genai as genai_real
        import backend.main as main_module
        from backend.schemas import ClinicalParse

        def _parse(notes, clinic_id=None):
            self.calls += 1
            return ClinicalParse(dosha_imbalance="Vata", prescribed_herbs=[{"name": "Ashwagandha", "grams": 5}])

        def _boom(*args, **kwargs):
            raise AssertionError("no real Gemini client may be constructed in these tests")

        self._saved = (genai_real.Client, main_module.parse_clinical_notes)
        genai_real.Client, main_module.parse_clinical_notes = _boom, _parse
        return self

    def __exit__(self, *exc):
        import google.genai as genai_real
        import backend.main as main_module
        genai_real.Client, main_module.parse_clinical_notes = self._saved
        return False


# --- migration 011: audit rebuild -------------------------------------------------

def _fresh_db_with(migrations_dir: str) -> tuple[sqlite3.Connection, str]:
    from db.migrations import run_migrations
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn=conn, migrations_dir=migrations_dir)
    return conn, path


def test_migration_011_rebuild_preserves_every_audit_row_and_the_chain():
    import db.database as database
    from backend.audit_store import (
        AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED, build_prescription_confirmed_event_data,
        create_audit_event, verify_audit_integrity,
    )
    from db.migrations import discover_migrations, run_migrations

    real = discover_migrations()
    check("migrations 011 and 012 exist", {m.filename for m in real} >= {"011_audit_encounter_events.py", "012_encounter_links.py"})

    with tempfile.TemporaryDirectory() as up_to_010:
        for m in real:
            if m.version <= 10:
                shutil.copy(m.path, os.path.join(up_to_010, m.filename))
        conn, path = _fresh_db_with(up_to_010)
        saved_path = database.DB_PATH
        try:
            conn.execute("INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
                         "VALUES ('u', 'x', 'c1', 'DOCTOR', 1, 't', 't')")
            for i in range(3):
                conn.execute("INSERT INTO prescription_previews (preview_id, clinic_id, patient_name, is_manual, ai_dosha_imbalance, "
                             "ai_prescribed_herbs_json, created_at, expires_at) VALUES (?, 'c1', 'x', 1, 'Vata', '[]', 't', 't')", (f"p{i}",))
                conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status) VALUES ('c1', 'x', 'd', 'Active')")
                conn.execute("BEGIN IMMEDIATE") if not conn.in_transaction else None
                create_audit_event(conn, clinic_id="c1", actor_user_id=1, event_type=AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
                                   prescription_id=i + 1, preview_id=f"p{i}",
                                   event_data=build_prescription_confirmed_event_data(
                                       is_manual=True, ai_dosha_imbalance=None, ai_prescribed_herbs=None,
                                       confirmed_dosha_imbalance="Vata", confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 5}]))
                conn.commit()
            before = [tuple(r) for r in conn.execute("SELECT * FROM audit_events ORDER BY event_id")]
            database.DB_PATH = path
            check("pre-011 chain is valid", verify_audit_integrity("c1").valid)

            applied = run_migrations(conn=conn)
            check("upgrade applies 011 then 012", applied[:2] == [11, 12], applied)
            after = [tuple(r) for r in conn.execute("SELECT * FROM audit_events ORDER BY event_id")]
            check("every audit row is byte-identical after the rebuild", before == after)
            check("the unchanged verifier still accepts the chain", verify_audit_integrity("c1").valid)
            indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_events' "
                                                  "AND name NOT LIKE 'sqlite_%'")}
            check("original index names recreated", indexes == {"idx_audit_events_prescription_id",
                                                                 "idx_audit_events_clinic_id_event_id",
                                                                 "idx_audit_events_actor_user_id"}, indexes)

            def blocked(sql, args):
                try:
                    conn.execute(sql, args)
                except sqlite3.IntegrityError:
                    return True
                return False

            ins = ("INSERT INTO audit_events (clinic_id, actor_user_id, event_type, prescription_id, preview_id, "
                   "event_data_json, created_at, previous_event_hash, event_hash) VALUES ('c1', 1, ?, ?, ?, '{}', 't', NULL, 'h')")
            check("PRESCRIPTION_CONFIRMED still requires prescription_id", blocked(ins, ("PRESCRIPTION_CONFIRMED", None, "p0")))
            check("PRESCRIPTION_CONFIRMED still requires preview_id", blocked(ins, ("PRESCRIPTION_CONFIRMED", 1, None)))
            check("an encounter event may not carry a prescription_id", blocked(ins, ("ENCOUNTER_OPENED", 1, None)))
            check("unknown event types are still rejected", blocked(ins, ("PRESCRIPTION_VOIDED", None, None)))
            conn.rollback()
            next_id = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'audit_events'").fetchone()[0]
            check("event_id sequence continues after the old maximum", next_id >= before[-1][0], (next_id, before[-1][0]))
        finally:
            database.DB_PATH = saved_path
            conn.close()
            os.remove(path)


# --- migration 012: link rules at write time -----------------------------------

def test_migration_012_link_rules_hold_with_foreign_keys_on_and_off():
    from db.migrations import MIGRATIONS_DIR

    for fk in ("ON", "OFF"):
        conn, path = _fresh_db_with(MIGRATIONS_DIR)
        try:
            conn.execute(f"PRAGMA foreign_keys = {fk}")
            conn.execute("INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
                         "VALUES ('ua', 'x', 'cA', 'DOCTOR', 1, 't', 't')")
            conn.execute("INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
                         "VALUES ('ub', 'x', 'cB', 'DOCTOR', 1, 't', 't')")
            for clinic, pid in (("cA", "OP-1"), ("cA", "OP-2"), ("cB", "OP-3")):
                conn.execute("INSERT INTO patients (clinic_id, clinic_patient_id, display_name, created_by_user_id, created_at) "
                             "VALUES (?, ?, 'P', ?, 't')", (clinic, pid, 1 if clinic == "cA" else 2))
            # encounter_ref is required since migration 013.
            conn.execute("INSERT INTO encounters (clinic_id, patient_pk, created_by_user_id, status, created_at, encounter_ref) "
                         "VALUES ('cA', 1, 1, 'OPEN', 't', lower(hex(randomblob(8))))")
            conn.execute("INSERT INTO encounters (clinic_id, patient_pk, created_by_user_id, status, created_at, closed_at, encounter_ref) "
                         "VALUES ('cA', 2, 1, 'CLOSED', 't', 't', lower(hex(randomblob(8))))")

            pv = ("INSERT INTO prescription_previews (preview_id, clinic_id, patient_name, is_manual, ai_dosha_imbalance, "
                  "ai_prescribed_herbs_json, created_at, expires_at, patient_pk, encounter_pk) VALUES (?, ?, 'x', 1, 'Vata', '[]', 't', 't', ?, ?)")
            rx = "INSERT INTO prescriptions (clinic_id, patient_name, description, status, patient_pk, encounter_pk) VALUES (?, 'x', 'd', 'Active', ?, ?)"

            def blocked(sql, args, reason):
                try:
                    conn.execute(sql, args)
                except sqlite3.IntegrityError as e:
                    return reason in str(e)
                return False

            for label, sql, extra in (("preview", pv, ("px",)), ("prescription", rx, ())):
                a = lambda clinic, patient, enc: (*extra, clinic, patient, enc)
                check(f"[fk={fk}] {label}: encounter of another clinic rejected", blocked(sql, a("cB", 3, 1), "encounter_link_invalid"))
                check(f"[fk={fk}] {label}: encounter of another patient rejected", blocked(sql, a("cA", 2, 1), "encounter_link_invalid"))
                check(f"[fk={fk}] {label}: encounter without a patient rejected", blocked(sql, a("cA", None, 1), "encounter_link_invalid"))
                check(f"[fk={fk}] {label}: CLOSED encounter rejected", blocked(sql, a("cA", 2, 2), "encounter_link_invalid"))
                check(f"[fk={fk}] {label}: nonexistent encounter rejected", blocked(sql, a("cA", 1, 999), "encounter_link_invalid"))

            conn.execute(pv, ("ok", "cA", 1, 1))
            conn.execute(rx, ("cA", 1, 1))
            conn.execute(rx, ("cA", 1, None))
            for table, key in (("prescription_previews", "preview_id = 'ok'"), ("prescriptions", "id = 1")):
                check(f"[fk={fk}] {table}: encounter link cannot be removed",
                      blocked(f"UPDATE {table} SET encounter_pk = NULL WHERE {key}", (), "encounter_link_immutable"))
            check(f"[fk={fk}] an unlinked prescription cannot be linked later",
                  blocked("UPDATE prescriptions SET encounter_pk = 1 WHERE id = 2", (), "encounter_link_immutable"))
        finally:
            conn.close()
            os.remove(path)


# --- API: fail-safe validation, before any AI call -----------------------------------

def test_cross_clinic_cross_patient_missing_closed_and_patientless_encounters_fail_safely():
    with _app_client() as client, _NoAI() as ai:
        a, b = _clinic("x_a"), _clinic("x_b")
        doc_a, _ = _login(client, a)
        doc_b, _ = _login(client, b)
        pa1, pa2 = _register(client, doc_a, "Clinic A One"), _register(client, doc_a, "Clinic A Two")
        pb = _register(client, doc_b, "Clinic B Patient")
        enc_a1 = _open(client, doc_a, pa1["patient_pk"])
        closed = _open(client, doc_a, pa2["patient_pk"])
        _close(client, doc_a, closed["encounter_pk"])

        def ai_preview(headers, patient_pk, encounter_pk):
            body = {"patient_name": "x", "notes": "Vata. Ashwagandha 5g.", "patient_pk": patient_pk, "encounter_pk": encounter_pk}
            if patient_pk is None:
                body.pop("patient_pk")
            return client.post("/prescribe/preview", json=body, headers=headers)

        previews_before = _count("prescription_previews", "clinic_id IN (?, ?)", (a, b))
        for kind, send in (("manual", lambda h, p, e: _manual(client, h, p, e)), ("ai", ai_preview)):
            cross_clinic = send(doc_b, pb["patient_pk"], enc_a1["encounter_pk"])
            missing = send(doc_b, pb["patient_pk"], 2_000_000_000)
            check(f"[{kind}] cross-clinic encounter -> 404", cross_clinic.status_code == 404, cross_clinic.text)
            check(f"[{kind}] cross-clinic and nonexistent encounters are indistinguishable",
                  cross_clinic.json() == missing.json() == {"detail": "Encounter not found."}, (cross_clinic.text, missing.text))
            for secret in ("Clinic A One", pa1["clinic_patient_id"], a):
                check(f"[{kind}] cross-clinic 404 leaks nothing ({secret!r})", secret not in cross_clinic.text)

            cross_patient = send(doc_a, pa2["patient_pk"], enc_a1["encounter_pk"])
            check(f"[{kind}] cross-patient encounter -> 409",
                  cross_patient.status_code == 409 and cross_patient.json() == {"detail": "This encounter belongs to a different patient."},
                  cross_patient.text)
            closed_resp = send(doc_a, pa2["patient_pk"], closed["encounter_pk"])
            check(f"[{kind}] closed encounter -> 409",
                  closed_resp.status_code == 409 and closed_resp.json() == {"detail": "This encounter is closed."}, closed_resp.text)
            no_patient = send(doc_a, None, enc_a1["encounter_pk"])
            check(f"[{kind}] encounter without patient_pk -> 422", no_patient.status_code == 422, no_patient.text)
        check("none of the rejected requests reached the AI", ai.calls == 0, ai.calls)
        check("none of the rejected requests wrote a preview",
              _count("prescription_previews", "clinic_id IN (?, ?)", (a, b)) == previews_before)

        ok = client.post("/prescribe/preview", json={"patient_name": "x", "notes": "Vata. Ashwagandha 5g.",
                                                      "patient_pk": pa1["patient_pk"], "encounter_pk": enc_a1["encounter_pk"]}, headers=doc_a)
        check("a valid AI preview in the patient's open encounter succeeds, calling the AI once",
              ok.status_code == 200 and ai.calls == 1, ok.text)
        check("the stored draft carries the encounter",
              _query("SELECT encounter_pk FROM prescription_previews WHERE preview_id = ?", (ok.json()["preview_id"],))[0]["encounter_pk"]
              == enc_a1["encounter_pk"])
        check("preview response shape is unchanged",
              set(ok.json()) == {"preview_id", "patient_name", "dosha_imbalance", "prescribed_herbs"}, ok.json())


# --- confirm inherits from the stored preview ---------------------------------------

def test_confirm_inherits_encounter_from_stored_preview_and_audit_gains_no_pii():
    with _app_client() as client, _NoAI():
        clinic = _clinic("inherit")
        doctor, doctor_id = _login(client, clinic)
        _set_stock(clinic)
        patient = _register(client, doctor, "Audit Secret Name", phone="9812345678")
        other = _register(client, doctor, "Other Patient")
        enc = _open(client, doctor, patient["patient_pk"])
        other_enc = _open(client, doctor, other["patient_pk"])

        prev = _manual(client, doctor, patient["patient_pk"], enc["encounter_pk"])
        check("linked manual preview succeeds", prev.status_code == 200, prev.text)
        # The confirm body tries to redirect both links; ConfirmRequest has no such fields.
        conf = _confirm(client, doctor, prev.json()["preview_id"],
                        encounter_pk=other_enc["encounter_pk"], patient_pk=other["patient_pk"])
        check("confirm succeeds", conf.status_code == 200, conf.text)
        check("confirm response shape is unchanged",
              set(conf.json()) == {"patient_name", "dosha_imbalance", "prescribed_herbs", "prescription_id", "created_at"}, conf.json())
        row = _query("SELECT patient_pk, encounter_pk FROM prescriptions WHERE id = ?", (conf.json()["prescription_id"],))[0]
        check("prescription inherits the STORED preview's patient and encounter, not the request's",
              (row["patient_pk"], row["encounter_pk"]) == (patient["patient_pk"], enc["encounter_pk"]), dict(row))

        event = _query("SELECT event_data_json FROM audit_events WHERE prescription_id = ?", (conf.json()["prescription_id"],))[0]
        data = json.loads(event["event_data_json"])
        check("confirm audit event carries NO encounter reference (auditors cannot group prescriptions by visit)",
              set(data) == {"decision_source", "ai_proposal", "doctor_decision", "was_changed"}, data)

        auditor, _ = _login(client, clinic, "AUDITOR")
        admin, _ = _login(client, clinic, "ADMIN")
        for role, headers in (("AUDITOR", auditor), ("ADMIN", admin)):
            for path in ("/audit/events", "/audit/export"):
                resp = client.get(path, headers=headers)
                check(f"{role} {path} succeeds", resp.status_code == 200, resp.text)
                for secret in ("Audit Secret Name", "9812345678", patient["clinic_patient_id"], "patient_pk"):
                    check(f"{role} {path} exposes no patient PII ({secret!r})", secret not in resp.text)
        events = client.get("/audit/events", headers=auditor).json()
        check("audit chain is valid with encounter events interleaved", events["chain_valid"] is True, events["integrity_violations"])
        types = [e["event_type"] for e in events["events"]]
        check("encounter opens and the confirm are all in the one clinic chain",
              types == ["ENCOUNTER_OPENED", "ENCOUNTER_OPENED", "PRESCRIPTION_CONFIRMED"], types)
        enc_events = [e for e in events["events"] if e["event_type"].startswith("ENCOUNTER_")]
        check("encounter events have null prescription/preview ids and only the random encounter_ref",
              all(e["prescription_id"] is None and e["preview_id"] is None and set(e["event_data"]) == {"encounter_ref"}
                  for e in enc_events), enc_events)
        check("encounter events are attributed to the acting doctor", all(e["actor_user_id"] == doctor_id for e in enc_events))


# --- invalid lifecycle ---------------------------------------------------------------

def test_encounter_closed_after_draft_blocks_confirm_atomically():
    with _app_client() as client, _NoAI():
        clinic = _clinic("closed")
        doctor, _ = _login(client, clinic)
        _set_stock(clinic, 1000)
        patient = _register(client, doctor)
        enc = _open(client, doctor, patient["patient_pk"])
        prev = _manual(client, doctor, patient["patient_pk"], enc["encounter_pk"]).json()
        check("encounter closes", _close(client, doctor, enc["encounter_pk"]).status_code == 200)

        before = {t: _count(t, "clinic_id = ?", (clinic,)) for t in ("prescriptions", "audit_events", "idempotency_keys", "corrections")}
        key = f"k-{secrets.token_hex(4)}"
        resp = _confirm(client, doctor, prev["preview_id"], key=key)
        check("confirming into a closed encounter -> 409", resp.status_code == 409
              and resp.json() == {"detail": "This draft's encounter is closed. Open a new encounter and draft again."}, resp.text)
        after = {t: _count(t, "clinic_id = ?", (clinic,)) for t in before}
        check("nothing written: no prescription, audit event, idempotency record or correction", before == after, (before, after))
        check("no stock deducted", _stock(clinic) == 1000)
        check("the draft stays PENDING and keeps its encounter",
              dict(_query("SELECT status, encounter_pk FROM prescription_previews WHERE preview_id = ?", (prev["preview_id"],))[0])
              == {"status": "PENDING", "encounter_pk": enc["encounter_pk"]})
        retry = _confirm(client, doctor, prev["preview_id"], key=key)
        check("a retry with the same key is re-evaluated (not replayed) and still refused", retry.status_code == 409, retry.text)

        # A new encounter cannot rescue the old draft: its link is immutable.
        new_enc = _open(client, doctor, patient["patient_pk"])
        check("still refused after a new encounter is opened", _confirm(client, doctor, prev["preview_id"]).status_code == 409)
        fresh = _manual(client, doctor, patient["patient_pk"], new_enc["encounter_pk"]).json()
        ok = _confirm(client, doctor, fresh["preview_id"])
        check("a fresh draft in the new encounter confirms", ok.status_code == 200, ok.text)
        check("stock deducted exactly once overall", _stock(clinic) == 995)


# --- replay ----------------------------------------------------------------------------

def test_replay_after_encounter_closed_returns_original_response():
    with _app_client() as client, _NoAI():
        clinic = _clinic("replay")
        doctor, _ = _login(client, clinic)
        _set_stock(clinic, 1000)
        patient = _register(client, doctor)
        enc = _open(client, doctor, patient["patient_pk"])
        prev = _manual(client, doctor, patient["patient_pk"], enc["encounter_pk"]).json()
        key = f"k-{secrets.token_hex(4)}"
        first = _confirm(client, doctor, prev["preview_id"], key=key)
        check("first confirm succeeds", first.status_code == 200, first.text)
        _close(client, doctor, enc["encounter_pk"])

        counts = {t: _count(t, "clinic_id = ?", (clinic,)) for t in ("prescriptions", "audit_events")}
        replay = _confirm(client, doctor, prev["preview_id"], key=key)
        check("same key + same payload after the encounter closed replays the original 200",
              replay.status_code == 200 and replay.json() == first.json(), replay.text)
        check("the replay wrote nothing", counts == {t: _count(t, "clinic_id = ?", (clinic,)) for t in counts})
        check("stock deducted once", _stock(clinic) == 995)
        changed = _confirm(client, doctor, prev["preview_id"], key=key, dosha_imbalance="Pitta")
        check("same key + changed payload -> 409 (unchanged semantics)", changed.status_code == 409, changed.text)
        no_key = _confirm(client, doctor, prev["preview_id"])
        check("a keyless second confirm -> 409 already confirmed (unchanged semantics)", no_key.status_code == 409, no_key.text)


# --- restart ---------------------------------------------------------------------------

def test_restart_preserves_the_drafts_encounter():
    with _app_client() as client:
        clinic = _clinic("restart")
        doctor, _ = _login(client, clinic)
        _set_stock(clinic, 1000)
        patient = _register(client, doctor)
        enc = _open(client, doctor, patient["patient_pk"])
        prev = _manual(client, doctor, patient["patient_pk"], enc["encounter_pk"]).json()

    # New app lifespan: startup re-runs migrations (a no-op) against the same
    # file; nothing survives in process memory that confirm could rely on.
    with _app_client() as client, _NoAI():
        conf = _confirm(client, doctor, prev["preview_id"])
        check("the persisted draft confirms after restart", conf.status_code == 200, conf.text)
        check("and still carries its encounter",
              _query("SELECT encounter_pk FROM prescriptions WHERE id = ?", (conf.json()["prescription_id"],))[0]["encounter_pk"]
              == enc["encounter_pk"])


# --- concurrency -------------------------------------------------------------------------

def test_concurrent_confirms_of_one_encounter_draft_commit_exactly_once():
    with _app_client() as client, _NoAI():
        clinic = _clinic("race_confirm")
        doctor, _ = _login(client, clinic)
        _set_stock(clinic, 1000)
        patient = _register(client, doctor)
        enc = _open(client, doctor, patient["patient_pk"])
        prev = _manual(client, doctor, patient["patient_pk"], enc["encounter_pk"]).json()

        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = sorted(f.result().status_code for f in
                           [pool.submit(_confirm, client, doctor, prev["preview_id"], f"k-{i}") for i in range(4)])
        check("exactly one concurrent confirm succeeds, the rest 409", codes == [200, 409, 409, 409], codes)
        check("exactly one prescription in the encounter",
              _count("prescriptions", "clinic_id = ? AND encounter_pk = ?", (clinic, enc["encounter_pk"])) == 1)
        check("exactly one PRESCRIPTION_CONFIRMED audit event",
              _count("audit_events", "clinic_id = ? AND event_type = 'PRESCRIPTION_CONFIRMED'", (clinic,)) == 1)
        check("stock deducted exactly once", _stock(clinic) == 995)


def test_close_racing_confirm_is_always_serialized_consistently():
    from backend.audit_store import verify_audit_integrity

    with _app_client() as client, _NoAI():
        for round_ in range(4):
            clinic = _clinic(f"race_close{round_}")
            doctor, _ = _login(client, clinic)
            _set_stock(clinic, 1000)
            patient = _register(client, doctor)
            enc = _open(client, doctor, patient["patient_pk"])
            prev = _manual(client, doctor, patient["patient_pk"], enc["encounter_pk"]).json()

            with ThreadPoolExecutor(max_workers=2) as pool:
                f_confirm = pool.submit(_confirm, client, doctor, prev["preview_id"])
                f_close = pool.submit(_close, client, doctor, enc["encounter_pk"])
                confirm, close = f_confirm.result(), f_close.result()
            check(f"[round {round_}] the close always succeeds", close.status_code == 200, close.text)
            events = [r["event_type"] for r in _query("SELECT event_type FROM audit_events WHERE clinic_id = ? ORDER BY event_id", (clinic,))]
            if confirm.status_code == 200:
                check(f"[round {round_}] confirm won: prescription is in the encounter, confirmed BEFORE the close",
                      _count("prescriptions", "clinic_id = ? AND encounter_pk = ?", (clinic, enc["encounter_pk"])) == 1
                      and events == ["ENCOUNTER_OPENED", "PRESCRIPTION_CONFIRMED", "ENCOUNTER_CLOSED"]
                      and _stock(clinic) == 995, events)
            else:
                check(f"[round {round_}] close won: confirm refused with nothing written",
                      confirm.status_code == 409 and _count("prescriptions", "clinic_id = ?", (clinic,)) == 0
                      and events == ["ENCOUNTER_OPENED", "ENCOUNTER_CLOSED"] and _stock(clinic) == 1000,
                      (confirm.status_code, events))
            check(f"[round {round_}] chain valid", verify_audit_integrity(clinic).valid)


# --- backward compatibility ------------------------------------------------------------------

def test_previews_without_encounter_behave_exactly_as_before():
    with _app_client() as client, _NoAI():
        clinic = _clinic("compat")
        doctor, _ = _login(client, clinic)
        _set_stock(clinic, 1000)
        patient = _register(client, doctor)
        _open(client, doctor, patient["patient_pk"])  # an open encounter exists but is not referenced

        for label, patient_pk in (("unlinked", None), ("patient-only", patient["patient_pk"])):
            prev = _manual(client, doctor, patient_pk)
            check(f"[{label}] preview without encounter succeeds", prev.status_code == 200, prev.text)
            conf = _confirm(client, doctor, prev.json()["preview_id"])
            check(f"[{label}] confirm succeeds", conf.status_code == 200, conf.text)
            row = _query("SELECT patient_pk, encounter_pk FROM prescriptions WHERE id = ?", (conf.json()["prescription_id"],))[0]
            check(f"[{label}] prescription has no encounter", row["encounter_pk"] is None and row["patient_pk"] == patient_pk, dict(row))
            data = json.loads(_query("SELECT event_data_json FROM audit_events WHERE prescription_id = ?",
                                     (conf.json()["prescription_id"],))[0]["event_data_json"])
            check(f"[{label}] audit event_data has exactly the pre-1B key set",
                  set(data) == {"decision_source", "ai_proposal", "doctor_decision", "was_changed"}, data)

        # A draft written the pre-1B way (no encounter_pk value at all) still confirms.
        conn = sqlite3.connect(DB_PATH)
        try:
            session_id = conn.execute(
                "SELECT s.session_id FROM sessions s JOIN users u ON u.user_id = s.user_id "
                "WHERE u.clinic_id = ? ORDER BY s.session_id DESC LIMIT 1", (clinic,)).fetchone()[0]
            conn.execute(
                "INSERT INTO prescription_previews (preview_id, clinic_id, patient_name, is_manual, ai_dosha_imbalance, "
                "ai_prescribed_herbs_json, status, created_by_session, created_at, expires_at) "
                "VALUES ('legacy-' || ?, ?, 'Legacy Patient', 1, 'Vata', ?, 'PENDING', ?, datetime('now'), datetime('now', '+30 minutes'))",
                (secrets.token_hex(4), clinic, json.dumps([{"name": "Ashwagandha", "grams": 5}]), str(session_id)))
            legacy_id = conn.execute("SELECT preview_id FROM prescription_previews WHERE clinic_id = ? AND preview_id LIKE 'legacy-%'",
                                     (clinic,)).fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        legacy = _confirm(client, doctor, legacy_id)
        check("a legacy-style draft (NULL encounter) confirms unchanged", legacy.status_code == 200, legacy.text)
        check("stock deducted three times in total", _stock(clinic) == 985)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
