"""
Patient Registry Phase 1 (migration 009, API Contract Addendum v1.5).

Covers:
  - migration 009: fresh bootstrap + upgrade from 008 (no backfill of
    existing prescriptions), and the write-time triggers themselves
    (cross-clinic links, immutability, no delete) with foreign keys ON and OFF;
  - POST /patients: DOCTOR/ADMIN allowed, AUDITOR and unauthenticated
    rejected, clinic_id always from the session (client clinic_id -> 422),
    per-clinic uniqueness, trimming and length/format validation;
  - linking a preview (AI and manual) to a same-clinic patient, 404 for a
    foreign or missing patient with no preview written and no AI call;
  - confirm copying the patient link from the STORED preview only;
  - locked preview/confirm response shapes unchanged;
  - no patient data in audit exports; AUDITOR sees none.

Run directly (no pytest required):
    python tests/test_patients.py
"""
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail="" ):
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
    return f"patients_{tag}_{secrets.token_hex(4)}"


def _login(client, clinic_id: str, role: str = "DOCTOR") -> dict:
    from backend.auth_service import create_user

    username = f"patients_test_{secrets.token_hex(6)}"
    password = "patients-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id, role=role)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _set_stock(herb_name: str, grams: int, clinic_id: str):
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            "UPDATE inventory SET stock_grams = ? WHERE herb_name = ? AND clinic_id = ?",
            (grams, herb_name, clinic_id),
        )
        if cursor.rowcount == 0:
            conn.execute(
                "INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (clinic_id, herb_name, "test botanical name", grams, "TEST-BATCH", 90),
            )
        conn.commit()
    finally:
        conn.close()


def _query(sql: str, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _register(client, headers, clinic_patient_id=None, display_name="Test Patient One", phone=None):
    body = {"clinic_patient_id": clinic_patient_id or f"OP-{secrets.token_hex(3)}", "display_name": display_name}
    if phone is not None:
        body["phone"] = phone
    return client.post("/patients", json=body, headers=headers)


def _manual_preview(client, headers, patient_pk=None, patient_name="Free Text Name"):
    body = {
        "patient_name": patient_name,
        "dosha_imbalance": "Vata",
        "prescribed_herbs": [{"name": "Ashwagandha", "grams": 5}],
    }
    if patient_pk is not None:
        body["patient_pk"] = patient_pk
    return client.post("/prescribe/manual-preview", json=body, headers=headers)


def _app_client():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    return TestClient(main_module.app)


# --- migration 009 --------------------------------------------------------------

def _fresh_db_with(migrations_dir: str) -> tuple[sqlite3.Connection, str]:
    from db.migrations import run_migrations
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn=conn, migrations_dir=migrations_dir)
    return conn, path


def test_migration_009_fresh_bootstrap_and_upgrade_without_backfill():
    from db.migrations import MIGRATIONS_DIR, discover_migrations, run_migrations

    real = discover_migrations()
    check("migration 009 exists", any(m.version == 9 and m.filename == "009_patients.py" for m in real))

    conn, path = _fresh_db_with(MIGRATIONS_DIR)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        check("fresh bootstrap creates patients", "patients" in tables)
        for table in ("prescriptions", "prescription_previews"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            check(f"{table} has patient_pk", "patient_pk" in cols, cols)
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        check("all 8 clinic-isolation/immutability triggers exist", {
            "patients_identity_immutable", "patients_no_delete",
            "previews_patient_clinic_insert", "previews_patient_clinic_update", "previews_patient_link_immutable",
            "prescriptions_patient_clinic_insert", "prescriptions_patient_clinic_update",
            "prescriptions_patient_link_immutable",
        } <= triggers, triggers)
    finally:
        conn.close()
        os.remove(path)

    with tempfile.TemporaryDirectory() as up_to_008:
        for m in real:
            if m.version <= 8:
                shutil.copy(m.path, os.path.join(up_to_008, m.filename))
        conn, path = _fresh_db_with(up_to_008)
        try:
            conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status) "
                         "VALUES ('c1', 'Legacy Patient', 'd', 'Active')")
            conn.commit()
            applied = run_migrations(conn=conn)
            check("upgrading from 008 applies exactly 009 (and any later)", applied[:1] == [9], applied)
            row = conn.execute("SELECT patient_name, patient_pk FROM prescriptions").fetchone()
            check("pre-existing prescription is untouched and stays unlinked (no backfill)",
                  row["patient_name"] == "Legacy Patient" and row["patient_pk"] is None, dict(row))
            try:
                conn.execute("UPDATE prescriptions SET patient_pk = 1")
                linked = True
            except sqlite3.IntegrityError:
                linked = False
            check("an old unlinked prescription cannot be linked by a later UPDATE", linked is False)
        finally:
            conn.close()
            os.remove(path)


def test_migration_009_triggers_enforce_clinic_isolation_regardless_of_foreign_keys():
    from db.migrations import MIGRATIONS_DIR

    for fk in ("ON", "OFF"):
        conn, path = _fresh_db_with(MIGRATIONS_DIR)
        try:
            conn.execute(f"PRAGMA foreign_keys = {fk}")
            conn.execute("INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
                         "VALUES ('u', 'x', 'cA', 'DOCTOR', 1, 't', 't')")
            conn.execute("INSERT INTO patients (clinic_id, clinic_patient_id, display_name, created_by_user_id, created_at) "
                         "VALUES ('cA', 'OP-1', 'A', 1, 't')")

            def blocked(sql, reason):
                try:
                    conn.execute(sql)
                except sqlite3.IntegrityError as e:
                    return reason in str(e)
                return False

            check(f"[fk={fk}] preview cannot link a patient of another clinic", blocked(
                "INSERT INTO prescription_previews (preview_id, clinic_id, patient_name, is_manual, ai_dosha_imbalance, "
                "ai_prescribed_herbs_json, created_at, expires_at, patient_pk) VALUES ('p1', 'cB', 'x', 1, 'Vata', '[]', 't', 't', 1)",
                "patient_clinic_mismatch"))
            check(f"[fk={fk}] prescription cannot link a patient of another clinic", blocked(
                "INSERT INTO prescriptions (clinic_id, patient_name, description, status, patient_pk) "
                "VALUES ('cB', 'x', 'd', 'Active', 1)", "patient_clinic_mismatch"))
            check(f"[fk={fk}] prescription cannot link a nonexistent patient", blocked(
                "INSERT INTO prescriptions (clinic_id, patient_name, description, status, patient_pk) "
                "VALUES ('cA', 'x', 'd', 'Active', 999)", "patient_clinic_mismatch"))
            conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status, patient_pk) "
                         "VALUES ('cA', 'x', 'd', 'Active', 1)")
            check(f"[fk={fk}] linked prescription cannot move to another clinic",
                  blocked("UPDATE prescriptions SET clinic_id = 'cB'", "patient_clinic_mismatch"))
            check(f"[fk={fk}] prescription's patient link cannot be changed",
                  blocked("UPDATE prescriptions SET patient_pk = NULL", "patient_link_immutable"))
            check(f"[fk={fk}] patient clinic_id is immutable",
                  blocked("UPDATE patients SET clinic_id = 'cB'", "patient_identity_immutable"))
            check(f"[fk={fk}] clinic_patient_id is immutable",
                  blocked("UPDATE patients SET clinic_patient_id = 'OP-2'", "patient_identity_immutable"))
            check(f"[fk={fk}] patients cannot be deleted",
                  blocked("DELETE FROM patients", "patient_delete_forbidden"))
            conn.execute("UPDATE patients SET display_name = 'A renamed', phone = '123'")
            check(f"[fk={fk}] name/phone are attributes and remain editable",
                  conn.execute("SELECT display_name FROM patients").fetchone()[0] == "A renamed")
        finally:
            conn.close()
            os.remove(path)


# --- POST /patients: RBAC, clinic from session, validation ------------------------

def test_doctor_and_admin_can_register_in_their_own_clinic():
    with _app_client() as client:
        for role in ("DOCTOR", "ADMIN"):
            clinic = _clinic(f"reg_{role.lower()}")
            headers = _login(client, clinic, role)
            resp = _register(client, headers, clinic_patient_id="  OP-100  ", display_name="  Asha Menon ", phone=" 98470 ")
            check(f"{role} registration returns 201", resp.status_code == 201, resp.text)
            body = resp.json()
            check(f"{role}: response shape", set(body) == {"patient_pk", "clinic_patient_id", "display_name", "phone", "created_at", "date_of_birth", "gender", "address_line", "city", "state", "pincode", "prakriti", "allergies", "registered_at"}, body)
            check(f"{role}: values are trimmed", (body["clinic_patient_id"], body["display_name"], body["phone"]) == ("OP-100", "Asha Menon", "98470"), body)
            rows = _query("SELECT clinic_id, created_by_user_id FROM patients WHERE patient_pk = ?", (body["patient_pk"],))
            check(f"{role}: stored under the session's clinic", rows[0]["clinic_id"] == clinic, dict(rows[0]))
            check(f"{role}: response never echoes clinic_id", "clinic_id" not in body)


def test_auditor_and_unauthenticated_are_rejected():
    with _app_client() as client:
        clinic = _clinic("auditor")
        auditor = _login(client, clinic, "AUDITOR")
        before = _query("SELECT COUNT(*) AS n FROM patients WHERE clinic_id = ?", (clinic,))[0]["n"]
        resp = _register(client, auditor)
        check("AUDITOR gets 403 on POST /patients", resp.status_code == 403, resp.text)
        resp = client.post("/patients", json={"clinic_patient_id": "OP-1", "display_name": "X"})
        check("no Authorization gets 401", resp.status_code == 401, resp.text)
        after = _query("SELECT COUNT(*) AS n FROM patients WHERE clinic_id = ?", (clinic,))[0]["n"]
        check("no patient row was created", before == after == 0, (before, after))


def test_client_supplied_clinic_id_is_rejected_not_trusted():
    with _app_client() as client:
        clinic = _clinic("own")
        foreign = _clinic("foreign")
        headers = _login(client, clinic)
        resp = client.post("/patients", json={"clinic_patient_id": "OP-9", "display_name": "X", "clinic_id": foreign},
                           headers=headers)
        check("a body with clinic_id is rejected with 422", resp.status_code == 422, resp.text)
        check("nothing written in either clinic",
              _query("SELECT COUNT(*) AS n FROM patients WHERE clinic_id IN (?, ?)", (clinic, foreign))[0]["n"] == 0)


def test_uniqueness_is_per_clinic_and_duplicate_reveals_nothing_cross_clinic():
    with _app_client() as client:
        a, b = _clinic("uniq_a"), _clinic("uniq_b")
        ha, hb = _login(client, a), _login(client, b)
        check("clinic A registers OP-7", _register(client, ha, "OP-7").status_code == 201)
        dup = _register(client, ha, " OP-7 ", display_name="Someone Else")
        check("same ID again in clinic A (after trimming) is 409", dup.status_code == 409, dup.text)
        check("409 message names only the caller's own clinic",
              dup.json()["detail"] == "A patient with this ID is already registered in this clinic.", dup.text)
        other = _register(client, hb, "OP-7")
        check("the same ID in clinic B is a separate, allowed patient", other.status_code == 201, other.text)


def test_clinic_patient_id_validation():
    with _app_client() as client:
        headers = _login(client, _clinic("validation"))
        for bad, why in (("", "empty"), ("   ", "whitespace only"), ("x" * 51, "51 chars"), ("OP\n1", "control char")):
            resp = client.post("/patients", json={"clinic_patient_id": bad, "display_name": "X"}, headers=headers)
            check(f"clinic_patient_id rejected: {why}", resp.status_code == 422, resp.text)
        check("exactly 50 chars is accepted", _register(client, headers, "y" * 50).status_code == 201)
        resp = client.post("/patients", json={"clinic_patient_id": "OP-v", "display_name": "   "}, headers=headers)
        check("blank display_name rejected", resp.status_code == 422, resp.text)


# --- linking previews, confirm copies from the stored preview ----------------------

def test_manual_preview_links_patient_and_confirm_copies_from_stored_preview():
    with _app_client() as client:
        clinic = _clinic("link")
        headers = _login(client, clinic)
        _set_stock("Ashwagandha", 5000, clinic)
        patient = _register(client, headers, "OP-55", display_name="Registry Name", phone="555").json()
        other = _register(client, headers, "OP-56", display_name="Other Patient").json()

        prev = _manual_preview(client, headers, patient_pk=patient["patient_pk"], patient_name="Typed Free Text")
        check("linked manual preview succeeds", prev.status_code == 200, prev.text)
        check("preview response shape is unchanged (locked contract)",
              set(prev.json()) == {"preview_id", "patient_name", "dosha_imbalance", "prescribed_herbs", "proposal_source"}, prev.json())
        check("registry is authoritative for the preview's patient name",
              prev.json()["patient_name"] == "Registry Name", prev.json())
        stored = _query("SELECT patient_pk, patient_name, patient_phone, patient_id FROM prescription_previews WHERE preview_id = ?",
                        (prev.json()["preview_id"],))[0]
        check("stored preview carries the patient link and registry identity",
              (stored["patient_pk"], stored["patient_name"], stored["patient_phone"], stored["patient_id"])
              == (patient["patient_pk"], "Registry Name", "555", "OP-55"), dict(stored))

        # A confirm body cannot choose or change the patient: an extra
        # patient_pk field is not part of ConfirmRequest and has no effect.
        conf = client.post("/prescribe/confirm",
                           json={"preview_id": prev.json()["preview_id"], "patient_pk": other["patient_pk"]},
                           headers=headers)
        check("confirm succeeds", conf.status_code == 200, conf.text)
        check("confirm response shape is unchanged (locked contract)",
              set(conf.json()) == {"patient_name", "dosha_imbalance", "prescribed_herbs", "prescription_id", "created_at", "proposal_source"},
              conf.json())
        rx = _query("SELECT clinic_id, patient_pk, patient_name FROM prescriptions WHERE id = ?",
                    (conf.json()["prescription_id"],))[0]
        check("prescription is linked to the STORED preview's patient, not the request's",
              (rx["clinic_id"], rx["patient_pk"], rx["patient_name"]) == (clinic, patient["patient_pk"], "Registry Name"),
              dict(rx))


def test_unlinked_preview_and_confirm_behave_as_before():
    with _app_client() as client:
        clinic = _clinic("unlinked")
        headers = _login(client, clinic)
        _set_stock("Ashwagandha", 5000, clinic)
        prev = _manual_preview(client, headers, patient_name="Plain Name")
        check("unlinked preview succeeds with the request's own name",
              prev.status_code == 200 and prev.json()["patient_name"] == "Plain Name", prev.text)
        conf = client.post("/prescribe/confirm", json={"preview_id": prev.json()["preview_id"]}, headers=headers)
        rx = _query("SELECT patient_pk, patient_name FROM prescriptions WHERE id = ?", (conf.json()["prescription_id"],))[0]
        check("unlinked prescription has patient_pk NULL", rx["patient_pk"] is None and rx["patient_name"] == "Plain Name", dict(rx))


def test_foreign_or_missing_patient_is_404_with_nothing_written_and_nothing_leaked():
    with _app_client() as client:
        a, b = _clinic("iso_a"), _clinic("iso_b")
        ha, hb = _login(client, a), _login(client, b)
        foreign = _register(client, ha, "OP-SECRET-1", display_name="Clinic A Only Patient", phone="9999").json()

        cross = _manual_preview(client, hb, patient_pk=foreign["patient_pk"])
        missing = _manual_preview(client, hb, patient_pk=2_000_000_000)
        check("cross-clinic patient_pk -> 404", cross.status_code == 404, cross.text)
        check("nonexistent patient_pk -> 404", missing.status_code == 404, missing.text)
        check("foreign and missing are indistinguishable", cross.json() == missing.json(), (cross.text, missing.text))
        for secret in ("Clinic A Only Patient", "OP-SECRET-1", "9999", a):
            check(f"404 body does not leak {secret!r}", secret not in cross.text)
        check("no preview was written for clinic B",
              _query("SELECT COUNT(*) AS n FROM prescription_previews WHERE clinic_id = ?", (b,))[0]["n"] == 0)
        check("patient_pk must be a positive integer",
              _manual_preview(client, hb, patient_pk=0).status_code == 422)


def test_ai_preview_links_patient_tokenizes_registry_identity_and_rejects_foreign_before_ai():
    import backend.main as main_module
    from backend.schemas import ClinicalParse

    seen_notes = []

    def _fake_parse(notes, clinic_id=None):
        seen_notes.append(notes)
        return ClinicalParse(dosha_imbalance="Vata", prescribed_herbs=[{"name": "Ashwagandha", "grams": 5}])

    original = main_module.parse_clinical_notes
    main_module.parse_clinical_notes = _fake_parse
    try:
        with _app_client() as client:
            a, b = _clinic("ai_a"), _clinic("ai_b")
            ha, hb = _login(client, a), _login(client, b)
            patient = _register(client, ha, "OP-7781", display_name="Meera Pillai", phone="9847012345").json()

            notes = "Meera Pillai (OP-7781, ph 9847012345) reports poor sleep. Vata. Ashwagandha 5g."
            resp = client.post("/prescribe/preview", json={
                "patient_name": "Different Typed Name", "notes": notes, "patient_pk": patient["patient_pk"],
            }, headers=ha)
            check("linked AI preview succeeds", resp.status_code == 200, resp.text)
            check("AI preview returns the registry name", resp.json()["patient_name"] == "Meera Pillai", resp.json())
            stored = _query("SELECT patient_pk FROM prescription_previews WHERE preview_id = ?", (resp.json()["preview_id"],))[0]
            check("AI preview stores the patient link", stored["patient_pk"] == patient["patient_pk"])
            sent = seen_notes[-1]
            for value in ("Meera", "Pillai", "OP-7781", "9847012345"):
                check(f"registry identifier {value!r} is tokenized before the parser sees it", value not in sent, sent)

            calls_before = len(seen_notes)
            cross = client.post("/prescribe/preview", json={
                "patient_name": "X", "notes": "Vata. Ashwagandha 5g.", "patient_pk": patient["patient_pk"],
            }, headers=hb)
            check("cross-clinic patient_pk on AI preview -> 404", cross.status_code == 404, cross.text)
            check("the AI parser was never called for the rejected request", len(seen_notes) == calls_before)
    finally:
        main_module.parse_clinical_notes = original


# --- audit exports and AUDITOR --------------------------------------------------------

def test_audit_exports_contain_no_patient_data():
    with _app_client() as client:
        clinic = _clinic("audit")
        doctor = _login(client, clinic)
        _set_stock("Ashwagandha", 5000, clinic)
        patient = _register(client, doctor, "OP-AUD-42", display_name="Audit Hidden Name", phone="7777").json()
        prev = _manual_preview(client, doctor, patient_pk=patient["patient_pk"])
        conf = client.post("/prescribe/confirm", json={"preview_id": prev.json()["preview_id"]}, headers=doctor)
        check("setup: linked confirm succeeded", conf.status_code == 200, conf.text)

        for role in ("ADMIN", "AUDITOR"):
            headers = _login(client, clinic, role)
            for path in ("/audit/export", "/audit/events"):
                resp = client.get(path, headers=headers)
                check(f"{role} {path} succeeds", resp.status_code == 200, resp.text)
                for secret in ("Audit Hidden Name", "OP-AUD-42", "7777", "patient_pk", "clinic_patient_id"):
                    check(f"{role} {path} contains no {secret!r}", secret not in resp.text)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
