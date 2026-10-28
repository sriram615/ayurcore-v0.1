"""
Phase 2B -- structured prescription data as the authoritative source
(migration 016, backend/patient_store.get_prescription_history).

Coverage:
  - the audit of description readers stays true: only the documented
    compatibility fallback reads `prescriptions.description`;
  - migration 016 changes no existing row and backfills nothing;
  - structured data (items, dosha column) takes precedence over the text,
    field by field, with the text as fallback only where a field has no
    structured value;
  - legacy prescriptions (and unparseable ones) still display as before;
  - confirmed structured data and text cannot be silently altered or
    deleted, with foreign keys ON and OFF; the gram limit and the
    newest-prescription rule hold in the database;
  - patient history agrees with the audit trail for AI-assisted and manual
    confirms, and the chain verifies;
  - confirm's response shape and idempotency behavior are unchanged.

Run directly (no pytest required):
    python tests/test_structured_authority.py
"""
import glob
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "db", "ayurcore.db")
MIGRATIONS_DIR = os.path.join(ROOT, "migrations")

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
    return f"auth2b_{tag}_{secrets.token_hex(4)}"


def _login(client, clinic_id: str, role: str = "DOCTOR") -> dict:
    from backend.auth_service import create_user

    username = f"auth2b_test_{secrets.token_hex(6)}"
    password = "auth2b-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id, role=role)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _set_stock(clinic_id: str, stock: dict):
    conn = sqlite3.connect(DB_PATH)
    try:
        for herb, grams in stock.items():
            conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                         "VALUES (?, ?, 'test botanical name', ?, 'TEST-BATCH', 90)", (clinic_id, herb, grams))
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


def _register(client, headers) -> int:
    resp = client.post("/patients", json={"clinic_patient_id": f"OP-{secrets.token_hex(3)}",
                                          "display_name": "Authority Patient"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["patient_pk"]


def _manual(client, headers, herbs, dosha="Vata", patient_pk=None):
    body = {"patient_name": "Authority Patient", "dosha_imbalance": dosha, "prescribed_herbs": herbs}
    if patient_pk is not None:
        body["patient_pk"] = patient_pk
    resp = client.post("/prescribe/manual-preview", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["preview_id"]


def _confirm(client, headers, preview_id, key=None, **extra):
    h = dict(headers)
    if key:
        h["Idempotency-Key"] = key
    return client.post("/prescribe/confirm", json={"preview_id": preview_id, **extra}, headers=h)


def _history(client, headers, patient_pk) -> dict:
    resp = client.get(f"/patients/{patient_pk}/prescriptions", headers=headers)
    assert resp.status_code == 200, resp.text
    return {r["prescription_id"]: r for r in resp.json()["prescriptions"]}


def _raw_prescription(clinic, patient_pk, description, dosha=None, items=(), patient_name="Authority Patient") -> int:
    """Inserts a prescription the way an older code version (or a direct
    writer) would, plus optional items -- immediately, while it is the newest
    prescription, as migration 016 requires."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rx = conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status, patient_pk, dosha_imbalance) "
                          "VALUES (?, ?, ?, 'Active', ?, ?)", (clinic, patient_name, description, patient_pk, dosha)).lastrowid
        for line_no, (herb, grams) in enumerate(items, start=1):
            conn.execute("INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) "
                         "VALUES (?, ?, ?, ?, ?)", (rx, clinic, line_no, herb, grams))
        conn.commit()
        return rx
    finally:
        conn.close()


def _legacy_text(name, dosha, herbs):
    return f"Patient: {name} | Dosha: {dosha} | Formulation: {json.dumps([{'name': n, 'grams': g} for n, g in herbs])}"


def _raises(conn, sql, args, expect: str) -> bool:
    try:
        conn.execute(sql, args)
    except sqlite3.DatabaseError as e:
        return expect in str(e)
    return False


def _app_client():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    return TestClient(main_module.app)


class _NoAI:
    """Replaces the AI parser; no real Gemini client may be constructed."""

    def __enter__(self):
        import google.genai as genai_real
        import backend.main as main_module
        from backend.schemas import ClinicalParse

        def _parse(notes, clinic_id=None):
            return ClinicalParse(dosha_imbalance="Pitta", prescribed_herbs=[{"name": "Brahmi", "grams": 4},
                                                                           {"name": "ashwagandha", "grams": 6}])

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


def _migrated_db(up_to=None):
    from db.migrations import run_migrations
    mig_dir = tempfile.mkdtemp()
    for name in os.listdir(MIGRATIONS_DIR):
        if name[:3].isdigit() and (up_to is None or int(name[:3]) <= up_to):
            shutil.copy(os.path.join(MIGRATIONS_DIR, name), mig_dir)
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    run_migrations(conn=conn, migrations_dir=mig_dir)
    return conn, path, mig_dir


# --- the audit of description readers ------------------------------------------------

def test_only_the_compatibility_fallback_reads_the_description():
    readers = []
    for path in sorted(glob.glob(os.path.join(ROOT, "backend", "*.py")) + glob.glob(os.path.join(ROOT, "agents", "**", "*.py"), recursive=True)):
        source = open(path, encoding="utf-8").read()
        for match in re.finditer(r"SELECT\b[^;]*?\bFROM\s+prescriptions\b", source, flags=re.IGNORECASE | re.DOTALL):
            if re.search(r"\bdescription\b", match.group(0)):
                readers.append(os.path.relpath(path, ROOT))
    check("patient history is the only backend reader of prescriptions.description",
          readers == ["backend/patient_store.py"], readers)

    source = open(os.path.join(ROOT, "backend", "patient_store.py"), encoding="utf-8").read()
    calls = [m.start() for m in re.finditer(r"_parse_prescription_description\(", source)]
    check("the text parser is defined once and called once", len(calls) == 2, len(calls))
    call_site = source[:calls[1]].rsplit("\n", 3)
    check("its one call sits under the 'no structured value' branch",
          "if dosha is None or herbs is None:" in "\n".join(call_site[-3:]), call_site[-3:])


# --- migration 016 ---------------------------------------------------------------------

def test_migration_016_changes_no_existing_row_and_backfills_nothing():
    from db.migrations import run_migrations

    conn, path, mig_dir = _migrated_db(up_to=15)
    try:
        conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                     "VALUES ('c', 'Ashwagandha', 'x', 100, 'B', 90)")
        legacy = conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status) VALUES "
                              "('c', 'Old', ?, 'Active')", (_legacy_text("Old", "Vata", [("Ashwagandha", 5)]),)).lastrowid
        era15 = conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status) VALUES "
                             "('c', 'Mid', ?, 'Active')", (_legacy_text("Mid", "Kapha", [("Ashwagandha", 3)]),)).lastrowid
        conn.execute("INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) "
                     "VALUES (?, 'c', 1, 'Ashwagandha', 3)", (era15,))
        conn.commit()

        def snapshot():
            return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                    for t in ("prescriptions", "prescription_items", "inventory", "audit_events")}

        before = snapshot()
        shutil.copy(os.path.join(MIGRATIONS_DIR, "016_prescription_structured_authority.py"), mig_dir)
        applied = run_migrations(conn=conn, migrations_dir=mig_dir)
        check("migration 016 applies on a version-15 database", applied == [16], applied)
        after = snapshot()
        check("existing prescriptions keep every value, gaining only a NULL dosha column",
              [row[:-1] for row in after["prescriptions"]] == before["prescriptions"]
              and all(row[-1] is None for row in after["prescriptions"]), after["prescriptions"])
        check("items, inventory and audit unchanged", all(after[t] == before[t] for t in ("prescription_items", "inventory", "audit_events")))
        check("no dosha backfilled", conn.execute("SELECT COUNT(*) FROM prescriptions WHERE dosha_imbalance IS NOT NULL").fetchone()[0] == 0)
        check("the legacy row is sealed against new items", _raises(
            conn, "INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) VALUES (?, 'c', 1, 'Ashwagandha', 1)",
            (legacy,), "prescription_items_immutable"))
    finally:
        conn.close()
        os.remove(path)
        shutil.rmtree(mig_dir)


# --- precedence and legacy display --------------------------------------------------------

def test_structured_data_takes_precedence_field_by_field():
    with _app_client() as client:
        clinic = _clinic("precedence")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000, "Brahmi": 1000})
        pk = _register(client, doctor)
        text = _legacy_text("Authority Patient", "Pitta", [("Brahmi", 4)])

        both = _raw_prescription(clinic, pk, text, dosha="Vata", items=[("Ashwagandha", 9)])
        dosha_only = _raw_prescription(clinic, pk, text, dosha="Kapha")
        items_only = _raw_prescription(clinic, pk, text, items=[("Ashwagandha", 2), ("Brahmi", 1)])
        records = _history(client, doctor, pk)

        check("items and dosha column both override the text",
              records[both]["dosha_imbalance"] == "Vata"
              and records[both]["prescribed_herbs"] == [{"name": "Ashwagandha", "grams": 9}], records[both])
        check("dosha column overrides; herbs fall back to the text",
              records[dosha_only]["dosha_imbalance"] == "Kapha"
              and records[dosha_only]["prescribed_herbs"] == [{"name": "Brahmi", "grams": 4}], records[dosha_only])
        check("items override; dosha falls back to the text (a 015-era prescription)",
              records[items_only]["dosha_imbalance"] == "Pitta"
              and records[items_only]["prescribed_herbs"] == [{"name": "Ashwagandha", "grams": 2}, {"name": "Brahmi", "grams": 1}],
              records[items_only])

        # Structured data wins even when the text cannot be parsed at all.
        garbled = _raw_prescription(clinic, pk, "free text a clinician typed long ago", dosha="Vata-Pitta",
                                    items=[("Brahmi", 7)])
        records = _history(client, doctor, pk)
        check("structured values shown for a row whose text is unparseable",
              records[garbled]["dosha_imbalance"] == "Vata-Pitta"
              and records[garbled]["prescribed_herbs"] == [{"name": "Brahmi", "grams": 7}], records[garbled])


def test_legacy_prescriptions_still_display_as_before():
    from backend.patient_store import _parse_prescription_description

    with _app_client() as client:
        clinic = _clinic("legacy")
        doctor = _login(client, clinic)
        pk = _register(client, doctor)
        legacy_text = _legacy_text("Authority Patient", "Vata-Kapha", [("Triphala", 30), ("Brahmi", 5)])
        legacy = _raw_prescription(clinic, pk, legacy_text)
        unparseable = _raw_prescription(clinic, pk, "handwritten note transcribed without a formulation")
        renamed = _raw_prescription(clinic, pk, _legacy_text("Someone Else", "Vata", [("Brahmi", 1)]))
        records = _history(client, doctor, pk)

        expected_dosha, expected_herbs = _parse_prescription_description("Authority Patient", legacy_text)
        check("legacy row shows exactly what the text parser yields",
              records[legacy]["dosha_imbalance"] == expected_dosha == "Vata-Kapha"
              and records[legacy]["prescribed_herbs"] == expected_herbs
              == [{"name": "Triphala", "grams": 30}, {"name": "Brahmi", "grams": 5}], records[legacy])
        check("unparseable legacy text is reported unknown, not guessed",
              records[unparseable]["dosha_imbalance"] is None and records[unparseable]["prescribed_herbs"] is None)
        check("text whose name prefix does not match is reported unknown, not guessed",
              records[renamed]["dosha_imbalance"] is None and records[renamed]["prescribed_herbs"] is None)
        check("record shape unchanged", all(set(r) == {"prescription_id", "created_at", "status", "dosha_imbalance",
                                                       "prescribed_herbs", "payment_status", "payment_amount_cents",
                                                       "payment_method", "payment_reference", "dispensed_at",
                                                       "dispensed_by_user_id", "invoice_number"} for r in records.values()))


# --- immutability ---------------------------------------------------------------------

def test_confirmed_prescription_content_cannot_be_altered_with_foreign_keys_on_and_off():
    conn, path, mig_dir = _migrated_db()
    try:
        conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                     "VALUES ('c', 'Ashwagandha', 'x', 100000, 'B', 90)")
        for fk in ("ON", "OFF"):
            conn.execute(f"PRAGMA foreign_keys = {fk}")
            tag = f"fk {fk}"
            older = conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status, dosha_imbalance) "
                                 "VALUES ('c', 'P', 'd', 'Active', 'Vata')").lastrowid
            conn.execute("INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) "
                         "VALUES (?, 'c', 1, 'Ashwagandha', 5)", (older,))
            newest = conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status, dosha_imbalance) "
                                  "VALUES ('c', 'P', 'd', 'Active', 'Vata')").lastrowid
            for column, value in (("dosha_imbalance", "Kapha"), ("dosha_imbalance", None), ("description", "edited"),
                                  ("patient_name", "Q"), ("created_at", "2000-01-01 00:00:00"), ("clinic_id", "other"),
                                  ("id", 999999)):
                check(f"[{tag}] UPDATE {column} rejected", _raises(
                    conn, f"UPDATE prescriptions SET {column} = ? WHERE id = ?", (value, older), "prescription_content_immutable"))
            check(f"[{tag}] DELETE rejected", _raises(conn, "DELETE FROM prescriptions WHERE id = ?", (older,),
                                                      "prescription_content_immutable"))
            check(f"[{tag}] an older prescription cannot gain items", _raises(
                conn, "INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) "
                      "VALUES (?, 'c', 2, 'Ashwagandha', 1)", (older,), "prescription_items_immutable"))
            check(f"[{tag}] an item over 10000 g rejected", _raises(
                conn, "INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) "
                      "VALUES (?, 'c', 1, 'Ashwagandha', 10001)", (newest,), "prescription_item_grams_limit"))
            conn.execute("INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) "
                         "VALUES (?, 'c', 1, 'Ashwagandha', 10000)", (newest,))
            conn.execute("UPDATE prescriptions SET status = 'Active' WHERE id = ?", (older,))
            row = conn.execute("SELECT patient_name, description, dosha_imbalance, clinic_id FROM prescriptions WHERE id = ?",
                               (older,)).fetchone()
            check(f"[{tag}] row unchanged; status alone stays writable (no lifecycle yet)",
                  tuple(row) == ("P", "d", "Vata", "c"), tuple(row))
    finally:
        conn.close()
        os.remove(path)
        shutil.rmtree(mig_dir)


def test_an_api_confirmed_prescription_cannot_be_altered():
    with _app_client() as client:
        clinic = _clinic("immutable")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000, "Brahmi": 1000})
        pk = _register(client, doctor)
        resp = _confirm(client, doctor, _manual(client, doctor, [{"name": "Ashwagandha", "grams": 5}], patient_pk=pk))
        check("confirm succeeds", resp.status_code == 200, resp.text)
        rx = resp.json()["prescription_id"]
        before = _history(client, doctor, pk)[rx]
        conn = sqlite3.connect(DB_PATH)
        try:
            for sql, expect in (
                ("UPDATE prescriptions SET dosha_imbalance = 'Kapha' WHERE id = ?", "prescription_content_immutable"),
                ("UPDATE prescriptions SET description = 'x' WHERE id = ?", "prescription_content_immutable"),
                ("DELETE FROM prescriptions WHERE id = ?", "prescription_content_immutable"),
                ("UPDATE prescription_items SET grams = 1 WHERE prescription_id = ?", "prescription_items_immutable"),
                ("DELETE FROM prescription_items WHERE prescription_id = ?", "prescription_items_immutable"),
                ("UPDATE prescriptions SET clinic_id = 'elsewhere' WHERE id = ?", "patient_clinic_mismatch"),
            ):
                check(f"rejected: {sql.split(' WHERE')[0]}", _raises(conn, sql, (rx,), expect))
        finally:
            conn.close()
        check("history unchanged", _history(client, doctor, pk)[rx] == before)


# --- history and audit ------------------------------------------------------------------

def test_history_agrees_with_the_audit_trail_for_ai_and_manual_confirms():
    from backend.audit_store import verify_audit_integrity

    with _app_client() as client, _NoAI():
        clinic = _clinic("agree")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000, "Brahmi": 1000, "Triphala": 1000})
        pk = _register(client, doctor)

        ai = client.post("/prescribe/preview", json={"patient_name": "Authority Patient", "patient_pk": pk,
                                                     "notes": "Dry skin, poor sleep for two weeks."}, headers=doctor)
        check("AI preview succeeds (stubbed parser)", ai.status_code == 200, ai.text)
        confirmed = [
            _confirm(client, doctor, ai.json()["preview_id"]),
            _confirm(client, doctor, client.post("/prescribe/preview", json={
                "patient_name": "Authority Patient", "patient_pk": pk, "notes": "Follow-up visit."}, headers=doctor
            ).json()["preview_id"], dosha_imbalance="Vata-Pitta", prescribed_herbs=[{"name": "Triphala", "grams": 20}]),
            _confirm(client, doctor, _manual(client, doctor, [{"name": "Brahmi", "grams": 3}, {"name": "brahmi", "grams": 2}],
                                             dosha="Kapha", patient_pk=pk)),
        ]
        check("all three confirms succeed", [r.status_code for r in confirmed] == [200] * 3, [r.text for r in confirmed])
        records = _history(client, doctor, pk)
        for label, resp in zip(("AI unchanged", "AI corrected", "manual with duplicates"), confirmed):
            body = resp.json()
            rx = body["prescription_id"]
            event = json.loads(_query("SELECT event_data_json FROM audit_events WHERE prescription_id = ?",
                                      (rx,))[0]["event_data_json"])["doctor_decision"]
            row = _query("SELECT dosha_imbalance FROM prescriptions WHERE id = ?", (rx,))[0]
            check(f"[{label}] dosha column == response == audit == history",
                  row["dosha_imbalance"] == body["dosha_imbalance"] == event["dosha"] == records[rx]["dosha_imbalance"],
                  (row["dosha_imbalance"], body["dosha_imbalance"], event["dosha"], records[rx]["dosha_imbalance"]))
            check(f"[{label}] items == response == history, and match the audit's herbs",
                  records[rx]["prescribed_herbs"] == body["prescribed_herbs"]
                  and sorted((h["name"], h["grams"]) for h in body["prescribed_herbs"])
                  == sorted((h["name"], h["grams"]) for h in event["herbs"]), (records[rx], event["herbs"]))
        check("manual duplicates merged in history", records[confirmed[2].json()["prescription_id"]]["prescribed_herbs"]
              == [{"name": "Brahmi", "grams": 5}])
        report = verify_audit_integrity(clinic)
        check("clinic audit chain verifies", report.valid and report.checked_count == 3,
              (report.valid, report.checked_count))


# --- confirmation and idempotency unchanged ------------------------------------------------

def test_confirm_response_and_idempotency_behavior_unchanged():
    with _app_client() as client:
        clinic = _clinic("idem")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000})
        pid = _manual(client, doctor, [{"name": "Ashwagandha", "grams": 5}])
        key = f"idem-{secrets.token_hex(4)}"
        first = _confirm(client, doctor, pid, key=key)
        check("confirm succeeds", first.status_code == 200, first.text)
        check("response has exactly the locked fields",
              set(first.json()) == {"patient_name", "dosha_imbalance", "prescribed_herbs", "prescription_id", "created_at",
                                    "payment_status", "payment_amount_cents", "payment_method", "payment_reference",
                                    "dispensed_at", "dispensed_by_user_id", "invoice_number", "proposal_source"},
              set(first.json()))
        replay = _confirm(client, doctor, pid, key=key)
        check("replay returns the byte-identical original body", replay.status_code == 200 and replay.content == first.content)
        conflict = _confirm(client, doctor, pid, key=key, dosha_imbalance="Kapha")
        check("same key, different body -> 409", conflict.status_code == 409, conflict.text)
        again = _confirm(client, doctor, pid)
        check("no key on an already confirmed preview -> 409", again.status_code == 409, again.text)
        rx = first.json()["prescription_id"]
        check("one prescription, one item, one audit event, stock deducted once",
              _query("SELECT COUNT(*) AS n FROM prescriptions WHERE clinic_id = ?", (clinic,))[0]["n"] == 1
              and _query("SELECT COUNT(*) AS n FROM prescription_items WHERE clinic_id = ?", (clinic,))[0]["n"] == 1
              and _query("SELECT COUNT(*) AS n FROM audit_events WHERE clinic_id = ?", (clinic,))[0]["n"] == 1
              and _query("SELECT stock_grams FROM inventory WHERE clinic_id = ?", (clinic,))[0]["stock_grams"] == 995)
        check("structured dosha stored for the confirmed prescription",
              _query("SELECT dosha_imbalance FROM prescriptions WHERE id = ?", (rx,))[0]["dosha_imbalance"] == "Vata")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
