"""
Encounter foundation Phase 1A (migration 010).

Covers:
  - migration 010: fresh bootstrap and upgrade from 009 (existing data
    untouched), and its write-time rules with foreign keys ON and OFF
    (patient/creator clinic match, identity immutability, CLOSED is final,
    no delete, one OPEN encounter per patient, status/closed_at CHECK);
  - API: DOCTOR opens/closes, DOCTOR+ADMIN read, AUDITOR and anonymous
    rejected, clinic always from the session (client clinic_id -> 422),
    foreign/missing patient or encounter -> identical 404 with nothing
    written or leaked, one OPEN per patient -> 409, CLOSED final -> 409,
    newest-first listing with bounded pagination;
  - no AI calls, no prescription/preview/audit writes, and the existing
    preview/confirm flow unchanged while an encounter is open.

Run directly (no pytest required):
    python tests/test_encounters.py
"""
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading

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
    return f"enc_{tag}_{secrets.token_hex(4)}"


def _login(client, clinic_id: str, role: str = "DOCTOR") -> tuple[dict, int]:
    from backend.auth_service import create_user

    username = f"enc_test_{secrets.token_hex(6)}"
    password = "enc-test-password-not-a-real-credential"
    user_id = create_user(username, password, clinic_id=clinic_id, role=role)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}, user_id


def _register(client, headers, display_name="Encounter Patient"):
    resp = client.post("/patients", json={"clinic_patient_id": f"OP-{secrets.token_hex(3)}", "display_name": display_name},
                       headers=headers)
    assert resp.status_code == 201, f"test setup failed to register a patient: {resp.status_code} {resp.text}"
    return resp.json()


def _open(client, headers, patient_pk, **kwargs):
    return client.post(f"/patients/{patient_pk}/encounters", headers=headers, **kwargs)


def _query(sql: str, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _count(table: str, where: str = "1=1", args=()) -> int:
    return _query(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", args)[0]["n"]


def _app_client():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    return TestClient(main_module.app)


_RESPONSE_FIELDS = {"encounter_pk", "patient_pk", "created_by_user_id", "status", "created_at", "closed_at", "encounter_ref"}


# --- migration 010 ------------------------------------------------------------------

def _fresh_db_with(migrations_dir: str) -> tuple[sqlite3.Connection, str]:
    from db.migrations import run_migrations
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn=conn, migrations_dir=migrations_dir)
    return conn, path


def test_migration_010_fresh_bootstrap_and_upgrade_leave_existing_data_untouched():
    from db.migrations import MIGRATIONS_DIR, discover_migrations, run_migrations

    real = discover_migrations()
    check("migration 010 exists", any(m.version == 10 and m.filename == "010_encounters.py" for m in real))

    conn, path = _fresh_db_with(MIGRATIONS_DIR)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(encounters)")]
        check("encounters has exactly the specified columns",
              cols == ["encounter_pk", "clinic_id", "patient_pk", "created_by_user_id", "status", "created_at", "closed_at",
                       "encounter_ref"], cols)  # encounter_ref added by migration 013
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='encounters'")}
        check("the 4 encounter triggers from 010 exist (013 adds encounters_ref_required)", triggers == {
            "encounters_patient_clinic_insert", "encounters_identity_immutable",
            "encounters_closed_is_final", "encounters_no_delete", "encounters_ref_required"}, triggers)
    finally:
        conn.close()
        os.remove(path)

    with tempfile.TemporaryDirectory() as up_to_009:
        for m in real:
            if m.version <= 9:
                shutil.copy(m.path, os.path.join(up_to_009, m.filename))
        conn, path = _fresh_db_with(up_to_009)
        try:
            conn.execute("INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
                         "VALUES ('u', 'x', 'c1', 'DOCTOR', 1, 't', 't')")
            conn.execute("INSERT INTO patients (clinic_id, clinic_patient_id, display_name, created_by_user_id, created_at) "
                         "VALUES ('c1', 'OP-1', 'Existing Patient', 1, 't')")
            conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status, patient_pk) "
                         "VALUES ('c1', 'Existing Patient', 'd', 'Active', 1)")
            conn.commit()
            applied = run_migrations(conn=conn)
            check("upgrading from 009 applies 010 first", applied[:1] == [10], applied)
            check("existing patient untouched",
                  dict(conn.execute("SELECT clinic_patient_id, display_name FROM patients").fetchone())
                  == {"clinic_patient_id": "OP-1", "display_name": "Existing Patient"})
            check("existing prescription untouched and still linked",
                  conn.execute("SELECT patient_pk FROM prescriptions").fetchone()[0] == 1)
            check("no encounters are created by the migration", conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0] == 0)
        finally:
            conn.close()
            os.remove(path)


def test_migration_010_write_time_rules_hold_with_foreign_keys_on_and_off():
    from db.migrations import MIGRATIONS_DIR

    for fk in ("ON", "OFF"):
        conn, path = _fresh_db_with(MIGRATIONS_DIR)
        try:
            conn.execute(f"PRAGMA foreign_keys = {fk}")
            for name, clinic in (("ua", "cA"), ("ub", "cB")):
                conn.execute("INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
                             "VALUES (?, 'x', ?, 'DOCTOR', 1, 't', 't')", (name, clinic))
            conn.execute("INSERT INTO patients (clinic_id, clinic_patient_id, display_name, created_by_user_id, created_at) "
                         "VALUES ('cA', 'OP-1', 'A', 1, 't')")
            conn.execute("INSERT INTO patients (clinic_id, clinic_patient_id, display_name, created_by_user_id, created_at) "
                         "VALUES ('cA', 'OP-2', 'B', 1, 't')")
            # encounter_ref is required since migration 013; a valid random one keeps these 010 rules in focus.
            ins = ("INSERT INTO encounters (clinic_id, patient_pk, created_by_user_id, status, created_at, closed_at, encounter_ref) "
                   "VALUES (?, ?, ?, ?, 't', ?, lower(hex(randomblob(8))))")

            def blocked(sql, args, reason):
                try:
                    conn.execute(sql, args)
                except sqlite3.IntegrityError as e:
                    return reason in str(e)
                return False

            check(f"[fk={fk}] encounter clinic must equal patient clinic",
                  blocked(ins, ("cB", 1, 2, "OPEN", None), "encounter_clinic_mismatch"))
            check(f"[fk={fk}] creator must belong to the encounter's clinic",
                  blocked(ins, ("cA", 1, 2, "OPEN", None), "encounter_clinic_mismatch"))
            check(f"[fk={fk}] nonexistent patient rejected",
                  blocked(ins, ("cA", 999, 1, "OPEN", None), "encounter_clinic_mismatch"))
            check(f"[fk={fk}] OPEN may not carry closed_at", blocked(ins, ("cA", 1, 1, "OPEN", "t"), "CHECK"))
            check(f"[fk={fk}] CLOSED requires closed_at", blocked(ins, ("cA", 1, 1, "CLOSED", None), "CHECK"))
            check(f"[fk={fk}] unknown status rejected", blocked(ins, ("cA", 1, 1, "CANCELLED", None), "CHECK"))

            conn.execute(ins, ("cA", 1, 1, "OPEN", None))
            check(f"[fk={fk}] a second OPEN encounter for the same patient is rejected",
                  blocked(ins, ("cA", 1, 1, "OPEN", None), "UNIQUE"))
            conn.execute(ins, ("cA", 2, 1, "OPEN", None))  # another patient: fine
            check(f"[fk={fk}] patient_pk is immutable",
                  blocked("UPDATE encounters SET patient_pk = 2 WHERE encounter_pk = 1", (), "encounter_identity_immutable"))
            check(f"[fk={fk}] clinic_id is immutable",
                  blocked("UPDATE encounters SET clinic_id = 'cB' WHERE encounter_pk = 1", (), "encounter_identity_immutable"))
            check(f"[fk={fk}] created_by_user_id is immutable",
                  blocked("UPDATE encounters SET created_by_user_id = 2 WHERE encounter_pk = 1", (), "encounter_identity_immutable"))
            check(f"[fk={fk}] created_at is immutable",
                  blocked("UPDATE encounters SET created_at = 'x' WHERE encounter_pk = 1", (), "encounter_identity_immutable"))

            conn.execute("UPDATE encounters SET status = 'CLOSED', closed_at = 't2' WHERE encounter_pk = 1")
            check(f"[fk={fk}] a CLOSED encounter cannot be reopened",
                  blocked("UPDATE encounters SET status = 'OPEN', closed_at = NULL WHERE encounter_pk = 1", (), "encounter_closed_is_final"))
            check(f"[fk={fk}] a CLOSED encounter's closed_at cannot change",
                  blocked("UPDATE encounters SET closed_at = 't3' WHERE encounter_pk = 1", (), "encounter_closed_is_final"))
            check(f"[fk={fk}] encounters cannot be deleted",
                  blocked("DELETE FROM encounters WHERE encounter_pk = 2", (), "encounter_delete_forbidden"))
            conn.execute(ins, ("cA", 1, 1, "OPEN", None))
            check(f"[fk={fk}] a new OPEN encounter is allowed once the previous one is CLOSED",
                  conn.execute("SELECT COUNT(*) FROM encounters WHERE patient_pk = 1").fetchone()[0] == 2)
        finally:
            conn.close()
            os.remove(path)


# --- API: create, RBAC, clinic from session -------------------------------------

def test_doctor_opens_an_encounter_in_their_own_clinic():
    with _app_client() as client:
        clinic = _clinic("open")
        doctor, doctor_id = _login(client, clinic)
        patient = _register(client, doctor)
        resp = _open(client, doctor, patient["patient_pk"])
        check("DOCTOR open -> 201", resp.status_code == 201, resp.text)
        body = resp.json()
        check("response shape", set(body) == _RESPONSE_FIELDS, body)
        check("new encounter is OPEN with no closed_at",
              (body["status"], body["closed_at"], body["patient_pk"], body["created_by_user_id"])
              == ("OPEN", None, patient["patient_pk"], doctor_id), body)
        check("response carries no clinic_id or patient identity",
              clinic not in resp.text and "Encounter Patient" not in resp.text and patient["clinic_patient_id"] not in resp.text)
        row = _query("SELECT clinic_id, patient_pk, created_by_user_id FROM encounters WHERE encounter_pk = ?",
                     (body["encounter_pk"],))[0]
        check("stored under the session's clinic, patient and user",
              (row["clinic_id"], row["patient_pk"], row["created_by_user_id"]) == (clinic, patient["patient_pk"], doctor_id), dict(row))
        check("an empty JSON body is accepted",
              _open(client, doctor, _register(client, doctor)["patient_pk"], json={}).status_code == 201)


def test_rbac_admin_reads_only_auditor_and_anonymous_get_nothing():
    with _app_client() as client:
        clinic = _clinic("rbac")
        doctor, _ = _login(client, clinic)
        admin, _ = _login(client, clinic, "ADMIN")
        auditor, _ = _login(client, clinic, "AUDITOR")
        patient = _register(client, doctor)
        other = _register(client, doctor)
        enc = _open(client, doctor, patient["patient_pk"]).json()

        check("ADMIN cannot open an encounter (403)", _open(client, admin, other["patient_pk"]).status_code == 403)
        check("ADMIN cannot close an encounter (403)",
              client.post(f"/encounters/{enc['encounter_pk']}/close", headers=admin).status_code == 403)
        check("ADMIN can read an encounter",
              client.get(f"/encounters/{enc['encounter_pk']}", headers=admin).json() == enc)
        check("ADMIN can list a patient's encounters",
              [e["encounter_pk"] for e in client.get(f"/patients/{patient['patient_pk']}/encounters", headers=admin).json()["encounters"]]
              == [enc["encounter_pk"]])

        calls = [
            ("POST", f"/patients/{other['patient_pk']}/encounters"),
            ("GET", f"/patients/{patient['patient_pk']}/encounters"),
            ("GET", f"/encounters/{enc['encounter_pk']}"),
            ("POST", f"/encounters/{enc['encounter_pk']}/close"),
        ]
        for method, path in calls:
            resp = client.request(method, path, headers=auditor)
            check(f"AUDITOR {method} {path.split('/')[1]}... -> 403", resp.status_code == 403, resp.text)
            check(f"AUDITOR response carries no encounter data ({method})", "encounter_pk" not in resp.text)
            check(f"anonymous {method} -> 401", client.request(method, path).status_code == 401)
        check("nothing AUDITOR/ADMIN attempted was written",
              _count("encounters", "clinic_id = ?", (clinic,)) == 1)
        check("the encounter is still OPEN",
              client.get(f"/encounters/{enc['encounter_pk']}", headers=doctor).json()["status"] == "OPEN")


def test_client_supplied_clinic_id_is_rejected():
    with _app_client() as client:
        clinic, foreign = _clinic("own"), _clinic("foreign")
        doctor, _ = _login(client, clinic)
        patient = _register(client, doctor)
        for body in ({"clinic_id": foreign}, {"clinic_id": clinic}, {"patient_pk": patient["patient_pk"]},
                     {"created_by_user_id": 1}):
            resp = _open(client, doctor, patient["patient_pk"], json=body)
            check(f"body {sorted(body)} -> 422", resp.status_code == 422, resp.text)
        check("nothing written in either clinic",
              _count("encounters", "clinic_id IN (?, ?)", (clinic, foreign)) == 0)


def test_foreign_or_missing_patient_and_encounter_are_indistinguishable_404s():
    with _app_client() as client:
        a, b = _clinic("iso_a"), _clinic("iso_b")
        doctor_a, _ = _login(client, a)
        doctor_b, _ = _login(client, b)
        admin_b, _ = _login(client, b, "ADMIN")
        patient_a = _register(client, doctor_a, display_name="Clinic A Secret Patient")
        enc_a = _open(client, doctor_a, patient_a["patient_pk"]).json()

        cross = _open(client, doctor_b, patient_a["patient_pk"])
        missing = _open(client, doctor_b, 2_000_000_000)
        check("open for a foreign patient -> 404", cross.status_code == 404, cross.text)
        check("foreign and missing patient give the same 404", cross.json() == missing.json())
        check("no encounter written for clinic B", _count("encounters", "clinic_id = ?", (b,)) == 0)

        for headers, role in ((doctor_b, "DOCTOR"), (admin_b, "ADMIN")):
            pairs = (
                (f"/encounters/{enc_a['encounter_pk']}", "/encounters/2000000000"),
                (f"/patients/{patient_a['patient_pk']}/encounters", "/patients/2000000000/encounters"),
            )
            for path, missing_path in pairs:
                cross = client.get(path, headers=headers)
                missing = client.get(missing_path, headers=headers)
                check(f"{role} GET {path.split('/')[1]} across clinics -> 404", cross.status_code == 404, cross.text)
                check(f"{role}: foreign and missing are indistinguishable", cross.json() == missing.json(), (cross.text, missing.text))
                for secret in ("Clinic A Secret Patient", patient_a["clinic_patient_id"], a, "encounter_pk"):
                    check(f"{role}: 404 leaks nothing ({secret!r})", secret not in cross.text)

        cross_close = client.post(f"/encounters/{enc_a['encounter_pk']}/close", headers=doctor_b)
        missing_close = client.post("/encounters/2000000000/close", headers=doctor_b)
        check("closing a foreign encounter -> same 404 as a missing one",
              (cross_close.status_code, cross_close.json()) == (missing_close.status_code, missing_close.json()) == (404, {"detail": "Encounter not found."}))
        check("the foreign encounter is still OPEN",
              _query("SELECT status FROM encounters WHERE encounter_pk = ?", (enc_a["encounter_pk"],))[0]["status"] == "OPEN")


# --- lifecycle ------------------------------------------------------------------------

def test_one_open_encounter_per_patient_and_closed_is_final():
    with _app_client() as client:
        clinic = _clinic("life")
        doctor, _ = _login(client, clinic)
        colleague, _ = _login(client, clinic)
        patient = _register(client, doctor)
        first = _open(client, doctor, patient["patient_pk"]).json()

        dup = _open(client, colleague, patient["patient_pk"])
        check("a second OPEN encounter for the same patient -> 409", dup.status_code == 409, dup.text)
        check("409 message is about the caller's own clinic's patient",
              dup.json() == {"detail": "This patient already has an open encounter."})

        closed = client.post(f"/encounters/{first['encounter_pk']}/close", headers=colleague)
        check("any DOCTOR of the clinic can close it", closed.status_code == 200, closed.text)
        body = closed.json()
        check("closing sets CLOSED and closed_at, nothing else changes",
              body["status"] == "CLOSED" and body["closed_at"] is not None
              and {k: body[k] for k in ("encounter_pk", "patient_pk", "created_by_user_id", "created_at")}
              == {k: first[k] for k in ("encounter_pk", "patient_pk", "created_by_user_id", "created_at")}, body)

        again = client.post(f"/encounters/{first['encounter_pk']}/close", headers=doctor)
        check("closing again -> 409", again.status_code == 409 and again.json() == {"detail": "This encounter is already closed."}, again.text)
        check("the closed encounter is unchanged by the rejected close",
              client.get(f"/encounters/{first['encounter_pk']}", headers=doctor).json() == body)

        second = _open(client, doctor, patient["patient_pk"])
        check("a new encounter can be opened once the previous one is closed", second.status_code == 201, second.text)


def test_concurrent_closes_resolve_to_exactly_one_success():
    from backend.encounter_store import EncounterAlreadyClosedError, close_encounter

    with _app_client() as client:
        clinic = _clinic("race")
        doctor, doctor_id = _login(client, clinic)
        enc = _open(client, doctor, _register(client, doctor)["patient_pk"]).json()

    outcomes, barrier = [], threading.Barrier(4)

    def _worker():
        barrier.wait()
        try:
            close_encounter(enc["encounter_pk"], clinic, actor_user_id=doctor_id)
            outcomes.append("closed")
        except EncounterAlreadyClosedError:
            outcomes.append("already_closed")

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("exactly one concurrent close succeeds", sorted(outcomes) == ["already_closed"] * 3 + ["closed"], outcomes)
    check("exactly one ENCOUNTER_CLOSED audit event despite the race",
          _count("audit_events", "clinic_id = ? AND event_type = 'ENCOUNTER_CLOSED'", (clinic,)) == 1)


def test_listing_is_newest_first_scoped_to_the_patient_and_bounded():
    with _app_client() as client:
        clinic = _clinic("list")
        doctor, _ = _login(client, clinic)
        patient = _register(client, doctor)
        other = _register(client, doctor)
        pks = []
        for _ in range(3):
            enc = _open(client, doctor, patient["patient_pk"]).json()
            pks.append(enc["encounter_pk"])
            client.post(f"/encounters/{enc['encounter_pk']}/close", headers=doctor)
        _open(client, doctor, other["patient_pk"])

        full = client.get(f"/patients/{patient['patient_pk']}/encounters", headers=doctor).json()
        check("only this patient's encounters, newest first",
              [e["encounter_pk"] for e in full["encounters"]] == pks[::-1], full)
        check("page metadata", (full["limit"], full["offset"], full["has_more"]) == (20, 0, False), full)
        page = client.get(f"/patients/{patient['patient_pk']}/encounters", params={"limit": 2}, headers=doctor).json()
        check("limit=2 -> 2 newest, has_more",
              [e["encounter_pk"] for e in page["encounters"]] == pks[::-1][:2] and page["has_more"] is True, page)
        rest = client.get(f"/patients/{patient['patient_pk']}/encounters", params={"limit": 2, "offset": 2}, headers=doctor).json()
        check("offset=2 -> the oldest, no more",
              [e["encounter_pk"] for e in rest["encounters"]] == [pks[0]] and rest["has_more"] is False, rest)
        for params, why in (({"limit": 0}, "limit 0"), ({"limit": 51}, "limit 51"), ({"offset": -1}, "negative offset")):
            check(f"{why} -> 422",
                  client.get(f"/patients/{patient['patient_pk']}/encounters", params=params, headers=doctor).status_code == 422)
        check("non-positive ids -> 422", client.get("/encounters/0", headers=doctor).status_code == 422
              and _open(client, doctor, 0).status_code == 422)


# --- no AI, no clinical writes, existing flows unchanged ----------------------------

def test_encounters_make_no_ai_calls_and_leave_existing_flows_unchanged():
    import google.genai as genai_real
    import backend.main as main_module

    def _boom(*args, **kwargs):
        raise AssertionError("encounter endpoints must never call the AI")

    with _app_client() as client:
        clinic = _clinic("flows")
        doctor, _ = _login(client, clinic)
        admin, _ = _login(client, clinic, "ADMIN")
        patient = _register(client, doctor)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                     "VALUES (?, 'Ashwagandha', 'test botanical name', 100, 'TEST-BATCH', 90)", (clinic,))
        conn.commit()
        conn.close()

        original_client, original_parse = genai_real.Client, main_module.parse_clinical_notes
        genai_real.Client, main_module.parse_clinical_notes = _boom, _boom
        try:
            tables = ("prescriptions", "prescription_previews", "corrections", "idempotency_keys", "inventory", "patients")
            before = {t: _count(t) for t in tables}
            enc = _open(client, doctor, patient["patient_pk"]).json()
            client.get(f"/encounters/{enc['encounter_pk']}", headers=admin)
            client.get(f"/patients/{patient['patient_pk']}/encounters", headers=doctor)
            client.post(f"/encounters/{enc['encounter_pk']}/close", headers=doctor)
            after = {t: _count(t) for t in tables}
        finally:
            genai_real.Client, main_module.parse_clinical_notes = original_client, original_parse
        check("encounter operations write no prescription/preview/inventory/patient data", before == after, (before, after))
        # Since Phase 1B-gap-fix: open/close ARE audited, with only the opaque encounter_pk.
        from backend.audit_store import verify_audit_integrity
        events = _query("SELECT event_type, prescription_id, preview_id, event_data_json FROM audit_events "
                        "WHERE clinic_id = ? ORDER BY event_id", (clinic,))
        check("exactly one ENCOUNTER_OPENED then one ENCOUNTER_CLOSED audit event",
              [e["event_type"] for e in events] == ["ENCOUNTER_OPENED", "ENCOUNTER_CLOSED"], [dict(e) for e in events])
        check("encounter audit events carry only the random encounter_ref (not the sequential encounter_pk)",
              all(e["event_data_json"] == f'{{"encounter_ref":"{enc["encounter_ref"]}"}}'
                  and e["prescription_id"] is None and e["preview_id"] is None for e in events), [dict(e) for e in events])
        check("the clinic's audit chain is valid", verify_audit_integrity(clinic).valid)

        # The existing manual-preview -> confirm flow is unchanged while an encounter is open.
        _open(client, doctor, patient["patient_pk"])
        prev = client.post("/prescribe/manual-preview", json={
            "patient_name": "x", "patient_pk": patient["patient_pk"], "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}]}, headers=doctor)
        check("manual preview still works", prev.status_code == 200, prev.text)
        check("preview response shape unchanged",
              set(prev.json()) == {"preview_id", "patient_name", "dosha_imbalance", "prescribed_herbs"}, prev.json())
        conf = client.post("/prescribe/confirm", json={"preview_id": prev.json()["preview_id"]}, headers=doctor)
        check("confirm still works", conf.status_code == 200, conf.text)
        check("confirm response shape unchanged",
              set(conf.json()) == {"patient_name", "dosha_imbalance", "prescribed_herbs", "prescription_id", "created_at"}, conf.json())
        check("stock was deducted exactly as before",
              _query("SELECT stock_grams FROM inventory WHERE clinic_id = ?", (clinic,))[0]["stock_grams"] == 95)
        check("prescription linked to the patient exactly as before (Phase 1)",
              _query("SELECT patient_pk FROM prescriptions WHERE id = ?", (conf.json()["prescription_id"],))[0]["patient_pk"]
              == patient["patient_pk"])


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
