"""
Structured prescription items (migration 015).

Coverage:
  - schema and database rules: clinic match, contiguous ordering, stocked
    herb only, positive integer grams, one row per herb, immutable, sealed
    once the prescription's audit event exists -- with foreign keys ON and OFF;
  - confirm writes the items in the same transaction as the deduction, the
    prescription, the description and the audit event, matching the response;
  - duplicate herbs (including different aliases of one herb) merge into one
    line, in first-appearance order;
  - unknown herbs and out-of-range quantities write nothing;
  - transaction rollback: an audit failure or an item-insert failure leaves no
    prescription, no items, stock untouched and the preview confirmable;
  - concurrency: parallel confirms of one preview write one item set; parallel
    confirms of different previews write disjoint, correct item sets;
  - replay: an Idempotency-Key replay writes no second item set;
  - historical compatibility: migration 015 changes no existing row;
    pre-015 prescriptions have no items and history still reads them from
    the description; items are the source of truth when present.

Run directly (no pytest required):
    python tests/test_prescription_items.py
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
    return f"items_{tag}_{secrets.token_hex(4)}"


def _login(client, clinic_id: str, role: str = "DOCTOR") -> dict:
    from backend.auth_service import create_user

    username = f"items_test_{secrets.token_hex(6)}"
    password = "items-test-password-not-a-real-credential"
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


def _count(table: str, where: str = "1=1", args=()) -> int:
    return _query(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", args)[0]["n"]


def _stock(clinic_id: str) -> dict:
    return {r["herb_name"]: r["stock_grams"] for r in
            _query("SELECT herb_name, stock_grams FROM inventory WHERE clinic_id = ?", (clinic_id,))}


def _items(prescription_id: int) -> list[tuple]:
    return [tuple(r) for r in _query(
        "SELECT line_no, herb_name, grams FROM prescription_items WHERE prescription_id = ? ORDER BY line_no",
        (prescription_id,))]


def _manual(client, headers, herbs, patient_pk=None):
    body = {"patient_name": "Items Patient", "dosha_imbalance": "Vata", "prescribed_herbs": herbs}
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


def _preview_status(preview_id: str) -> str:
    return _query("SELECT status FROM prescription_previews WHERE preview_id = ?", (preview_id,))[0]["status"]


def _app_client():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    return TestClient(main_module.app)


def _nothing_written(clinic: str, preview_id: str, stock_before: dict, label: str):
    check(f"[{label}] no prescription written", _count("prescriptions", "clinic_id = ?", (clinic,)) == 0)
    check(f"[{label}] no items written", _count("prescription_items", "clinic_id = ?", (clinic,)) == 0)
    check(f"[{label}] no audit event written", _count("audit_events", "clinic_id = ?", (clinic,)) == 0)
    check(f"[{label}] stock untouched", _stock(clinic) == stock_before, _stock(clinic))
    check(f"[{label}] preview still PENDING (confirmable after a fix)", _preview_status(preview_id) == "PENDING")


def _migrated_db(up_to: int | None = None) -> tuple[sqlite3.Connection, str, str]:
    """A temp database migrated from scratch, optionally only up to `up_to`
    (the migrations directory is copied and later files left out)."""
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


def _raises(conn, sql, args, expect: str) -> bool:
    try:
        conn.execute(sql, args)
    except sqlite3.DatabaseError as e:
        return expect in str(e)
    return False


# --- schema and database rules ------------------------------------------------------

def test_migration_015_database_rules_hold_with_foreign_keys_on_and_off():
    conn, path, mig_dir = _migrated_db()
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(prescription_items)")]
        check("prescription_items columns",
              cols == ["item_id", "prescription_id", "clinic_id", "line_no", "herb_name", "grams", "created_at"], cols)
        triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='prescription_items'")}
        check("migration 015's four item triggers exist (016 adds two more)", triggers >= {
            "prescription_items_insert_check", "prescription_items_sealed",
            "prescription_items_no_update", "prescription_items_no_delete"}, triggers)
        check("prescriptions.description is kept", "description" in
              [r["name"] for r in conn.execute("PRAGMA table_info(prescriptions)")])

        for fk in ("ON", "OFF"):
            conn.execute(f"PRAGMA foreign_keys = {fk}")
            tag = f"fk {fk}"
            for clinic in ("clinic_a", "clinic_b"):
                conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                             "VALUES (?, 'Ashwagandha', 'x', 100, 'B', 90)", (clinic,))
            rx = conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status) "
                              "VALUES ('clinic_a', 'P', 'd', 'Active')").lastrowid
            ins = "INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) VALUES (?, ?, ?, ?, ?)"
            check(f"[{tag}] foreign-clinic line rejected",
                  _raises(conn, ins, (rx, "clinic_b", 1, "Ashwagandha", 5), "prescription_item_prescription_invalid"))
            check(f"[{tag}] line for a nonexistent prescription rejected",
                  _raises(conn, ins, (rx + 999, "clinic_a", 1, "Ashwagandha", 5), "prescription_item_prescription_invalid"))
            check(f"[{tag}] first line must be 1",
                  _raises(conn, ins, (rx, "clinic_a", 2, "Ashwagandha", 5), "prescription_item_line_invalid"))
            check(f"[{tag}] herb not stocked by this clinic rejected",
                  _raises(conn, ins, (rx, "clinic_a", 1, "Brahmi", 5), "prescription_item_unknown_herb"))
            check(f"[{tag}] non-canonical spelling rejected",
                  _raises(conn, ins, (rx, "clinic_a", 1, "ashwagandha", 5), "prescription_item_unknown_herb"))
            for bad in (0, -5, 2.5, "5g"):
                # Text compares greater than any number in SQLite, so for "5g"
                # migration 016's gram-limit trigger fires before this CHECK;
                # either way the row is rejected.
                expect = ("CHECK", "prescription_item_grams_limit") if isinstance(bad, str) else ("CHECK",)
                check(f"[{tag}] grams={bad!r} rejected", any(
                    _raises(conn, ins, (rx, "clinic_a", 1, "Ashwagandha", bad), e) for e in expect))
            conn.execute(ins, (rx, "clinic_a", 1, "Ashwagandha", 5))
            check(f"[{tag}] same herb twice rejected",
                  _raises(conn, ins, (rx, "clinic_a", 2, "Ashwagandha", 5), "UNIQUE"))
            check(f"[{tag}] a gap in ordering rejected",
                  _raises(conn, ins, (rx, "clinic_a", 3, "Ashwagandha", 5), "prescription_item_line_invalid"))
            check(f"[{tag}] UPDATE rejected",
                  _raises(conn, "UPDATE prescription_items SET grams = 50 WHERE prescription_id = ?", (rx,),
                          "prescription_items_immutable"))
            check(f"[{tag}] DELETE rejected",
                  _raises(conn, "DELETE FROM prescription_items WHERE prescription_id = ?", (rx,),
                          "prescription_items_immutable"))
            check(f"[{tag}] item unchanged", [tuple(r) for r in conn.execute(
                "SELECT line_no, herb_name, grams FROM prescription_items WHERE prescription_id = ?", (rx,))]
                  == [(1, "Ashwagandha", 5)])
            conn.execute("DELETE FROM inventory")
    finally:
        conn.close()
        os.remove(path)
        shutil.rmtree(mig_dir)


def test_items_are_sealed_once_the_confirmation_is_audited():
    with _app_client() as client:
        clinic = _clinic("sealed")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000, "Brahmi": 1000})
        resp = _confirm(client, doctor, _manual(client, doctor, [{"name": "Ashwagandha", "grams": 5}]))
        check("confirm succeeds", resp.status_code == 200, resp.text)
        rx = resp.json()["prescription_id"]
        conn = sqlite3.connect(DB_PATH)
        try:
            check("adding a line after confirmation is rejected (every other rule satisfied)",
                  _raises(conn, "INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) "
                                "VALUES (?, ?, 2, 'Brahmi', 5)", (rx, clinic), "prescription_items_immutable"))
            check("changing a confirmed line is rejected",
                  _raises(conn, "UPDATE prescription_items SET grams = 1 WHERE prescription_id = ?", (rx,),
                          "prescription_items_immutable"))
            check("deleting a confirmed line is rejected",
                  _raises(conn, "DELETE FROM prescription_items WHERE prescription_id = ?", (rx,),
                          "prescription_items_immutable"))
        finally:
            conn.close()
        check("confirmed item set unchanged", _items(rx) == [(1, "Ashwagandha", 5)])


# --- confirm ----------------------------------------------------------------------------

def test_confirm_writes_items_matching_the_response_description_and_audit():
    with _app_client() as client:
        clinic = _clinic("confirm")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000, "Brahmi": 1000, "Triphala": 1000})
        herbs = [{"name": "Triphala", "grams": 30}, {"name": "ashwagandha", "grams": 12}, {"name": "Bacopa monnieri", "grams": 7}]
        resp = _confirm(client, doctor, _manual(client, doctor, herbs))
        check("confirm succeeds", resp.status_code == 200, resp.text)
        body = resp.json()
        rx = body["prescription_id"]
        expected = [(1, "Triphala", 30), (2, "Ashwagandha", 12), (3, "Brahmi", 7)]
        check("items are canonical, in prescription order, one per herb", _items(rx) == expected, _items(rx))
        check("items equal the response's prescribed_herbs",
              [(h["name"], h["grams"]) for h in body["prescribed_herbs"]] == [(n, g) for _, n, g in expected])
        check("response shape unchanged (no items field added)", "items" not in body and "prescription_items" not in body)
        row = _query("SELECT clinic_id FROM prescription_items WHERE prescription_id = ?", (rx,))
        check("every item carries the prescription's clinic", {r["clinic_id"] for r in row} == {clinic})

        from backend.patient_store import _parse_prescription_description
        rx_row = _query("SELECT patient_name, description FROM prescriptions WHERE id = ?", (rx,))[0]
        dosha, parsed = _parse_prescription_description(rx_row["patient_name"], rx_row["description"])
        check("description still written in the legacy format, agreeing with the items",
              dosha == "Vata" and [(h["name"], h["grams"]) for h in parsed] == [(n, g) for _, n, g in expected])

        event = json.loads(_query("SELECT event_data_json FROM audit_events WHERE prescription_id = ?", (rx,))[0]["event_data_json"])
        audited = sorted((h["name"], h["grams"]) for h in event["doctor_decision"]["herbs"])
        check("audit event's confirmed herbs agree with the items", audited == sorted((n, g) for _, n, g in expected), audited)
        check("stock deducted per item", _stock(clinic) == {"Ashwagandha": 988, "Brahmi": 993, "Triphala": 970})


def test_duplicate_herbs_merge_into_one_line_in_first_appearance_order():
    with _app_client() as client:
        clinic = _clinic("dupes")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000, "Brahmi": 1000})
        pid = _manual(client, doctor, [{"name": "Ashwagandha", "grams": 5}])
        resp = _confirm(client, doctor, pid, prescribed_herbs=[
            {"name": "Brahmi", "grams": 2}, {"name": "ashwagandha", "grams": 5},
            {"name": "Withania somnifera", "grams": 7}, {"name": "brahmi", "grams": 1}])
        check("confirm with duplicates succeeds", resp.status_code == 200, resp.text)
        rx = resp.json()["prescription_id"]
        check("aliases of one herb merge into one line; order of first appearance kept",
              _items(rx) == [(1, "Brahmi", 3), (2, "Ashwagandha", 12)], _items(rx))
        check("stock deducted by the merged totals", _stock(clinic) == {"Ashwagandha": 988, "Brahmi": 997})

        # The per-herb bound (10000 g) applies to the merged line too.
        _set_stock(clinic, {"Triphala": 20000})
        pid = _manual(client, doctor, [{"name": "Triphala", "grams": 1}])
        count_before = _count("prescriptions", "clinic_id = ?", (clinic,))
        resp = _confirm(client, doctor, pid,
                        prescribed_herbs=[{"name": "Triphala", "grams": 6000}, {"name": "triphala", "grams": 6000}])
        check("two 6000 g lines of one herb -> 422 naming the merged total", resp.status_code == 422
              and "12000" in resp.json()["detail"], resp.text)
        check("nothing written for the over-limit merge", _count("prescriptions", "clinic_id = ?", (clinic,)) == count_before
              and _stock(clinic)["Triphala"] == 20000 and _preview_status(pid) == "PENDING")
        resp = _confirm(client, doctor, pid,
                        prescribed_herbs=[{"name": "Triphala", "grams": 6000}, {"name": "triphala", "grams": 4000}])
        check("two lines merging to exactly 10000 g confirm as one line", resp.status_code == 200
              and _items(resp.json()["prescription_id"]) == [(1, "Triphala", 10000)], resp.text)


def test_unknown_herb_writes_nothing_and_the_preview_stays_confirmable():
    with _app_client() as client:
        clinic = _clinic("unknown")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000})
        before = _stock(clinic)
        pid = _manual(client, doctor, [{"name": "Ashwagandha", "grams": 5}])
        resp = _confirm(client, doctor, pid, prescribed_herbs=[
            {"name": "Ashwagandha", "grams": 5}, {"name": "Unobtainium root", "grams": 5}])
        check("unknown herb -> 400", resp.status_code == 400, resp.text)
        _nothing_written(clinic, pid, before, "unknown herb")

        # A canonical herb the resolver knows but this clinic does not stock.
        resp = _confirm(client, doctor, pid, prescribed_herbs=[{"name": "Brahmi", "grams": 5}])
        check("known but unstocked herb -> 409 (insufficient stock)", resp.status_code == 409, resp.text)
        _nothing_written(clinic, pid, before, "unstocked herb")

        resp = _confirm(client, doctor, pid)
        check("the same preview then confirms", resp.status_code == 200 and
              _items(resp.json()["prescription_id"]) == [(1, "Ashwagandha", 5)], resp.text)


def test_quantity_limits_write_nothing_outside_the_bounds():
    with _app_client() as client:
        clinic = _clinic("qty")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 10000, "Brahmi": 10})
        before = _stock(clinic)
        pid = _manual(client, doctor, [{"name": "Ashwagandha", "grams": 5}])
        for bad in (0, -1, 10001, 2.5, "ten"):
            resp = _confirm(client, doctor, pid, prescribed_herbs=[{"name": "Ashwagandha", "grams": bad}])
            check(f"grams={bad!r} -> 422", resp.status_code == 422, resp.status_code)
        _nothing_written(clinic, pid, before, "invalid grams")

        resp = _confirm(client, doctor, pid, prescribed_herbs=[
            {"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 11}])
        check("second herb over stock -> 409", resp.status_code == 409, resp.text)
        _nothing_written(clinic, pid, before, "insufficient stock on a later line")

        resp = _confirm(client, doctor, pid, prescribed_herbs=[{"name": "Ashwagandha", "grams": 10000}])
        check("exactly 10000 g confirms", resp.status_code == 200, resp.text)
        check("the 10000 g line is stored exactly", _items(resp.json()["prescription_id"]) == [(1, "Ashwagandha", 10000)])
        check("stock reaches zero", _stock(clinic)["Ashwagandha"] == 0)


# --- rollback -----------------------------------------------------------------------------

def test_audit_failure_rolls_back_the_items_with_everything_else():
    import backend.main as main_module

    with _app_client() as client:
        clinic = _clinic("rb_audit")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000, "Brahmi": 1000})
        before = _stock(clinic)
        pid = _manual(client, doctor, [{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 5}])

        saved = main_module.create_audit_event

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated audit write failure")

        main_module.create_audit_event = _boom
        try:
            resp = _confirm(client, doctor, pid, key=f"rb-{secrets.token_hex(4)}")
        finally:
            main_module.create_audit_event = saved
        check("audit failure -> 500", resp.status_code == 500, resp.text)
        _nothing_written(clinic, pid, before, "audit failure")
        check("no idempotency row left behind", _count("idempotency_keys", "clinic_id = ?", (clinic,)) == 0)

        resp = _confirm(client, doctor, pid)
        check("the preview confirms once the failure is gone", resp.status_code == 200, resp.text)
        check("exactly one item set", _count("prescription_items", "clinic_id = ?", (clinic,)) == 2)


def test_item_insert_failure_rolls_back_deduction_prescription_and_audit():
    with _app_client() as client:
        clinic = _clinic("rb_item")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000, "Brahmi": 1000})
        before = _stock(clinic)
        pid = _manual(client, doctor, [{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 5}])

        # Fail the SECOND line only, for this test clinic only: the first
        # line, the deduction and the prescription row are already written
        # inside the transaction when it fires.
        trigger = f"test_fail_item_{secrets.token_hex(4)}"
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(f"""
                CREATE TRIGGER {trigger} BEFORE INSERT ON prescription_items
                WHEN NEW.clinic_id = '{clinic}' AND NEW.line_no = 2
                BEGIN SELECT RAISE(ABORT, 'simulated_item_failure'); END""")
            conn.commit()
        finally:
            conn.close()
        try:
            resp = _confirm(client, doctor, pid)
        finally:
            conn = sqlite3.connect(DB_PATH)
            try:
                conn.execute(f"DROP TRIGGER {trigger}")
                conn.commit()
            finally:
                conn.close()
        check("item insert failure -> 500", resp.status_code == 500, resp.text)
        _nothing_written(clinic, pid, before, "item failure")

        resp = _confirm(client, doctor, pid)
        check("the preview confirms once the failure is gone", resp.status_code == 200
              and _items(resp.json()["prescription_id"]) == [(1, "Ashwagandha", 5), (2, "Brahmi", 5)], resp.text)


def test_direct_deduct_and_prescribe_writes_items_and_rolls_back_on_insufficient_stock():
    import db.database as database
    from backend.inventory_service import deduct_and_prescribe, InsufficientStockError

    clinic = _clinic("direct")
    _set_stock(clinic, {"Ashwagandha": 100, "Brahmi": 3})
    try:
        deduct_and_prescribe("Direct Patient", "Vata", [{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 4}],
                             clinic_id=clinic)
        raised = False
    except InsufficientStockError:
        raised = True
    check("own-connection call: insufficient stock raises", raised)
    check("own-connection call: nothing written", _count("prescriptions", "clinic_id = ?", (clinic,)) == 0
          and _count("prescription_items", "clinic_id = ?", (clinic,)) == 0 and _stock(clinic) == {"Ashwagandha": 100, "Brahmi": 3})
    result = deduct_and_prescribe("Direct Patient", "Vata", [{"name": "Ashwagandha", "grams": 5}], clinic_id=clinic)
    check("own-connection call writes its items", _items(result["prescription_id"]) == [(1, "Ashwagandha", 5)])
    check("DB_PATH untouched by this test", os.path.realpath(database.DB_PATH) == os.path.realpath(DB_PATH))


# --- concurrency and replay -----------------------------------------------------------

def test_parallel_confirms_of_one_preview_write_one_item_set():
    with _app_client() as client:
        clinic = _clinic("race_one")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000, "Brahmi": 1000})
        pid = _manual(client, doctor, [{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 2}])
        with ThreadPoolExecutor(max_workers=6) as pool:
            codes = sorted(f.result().status_code for f in
                           [pool.submit(_confirm, client, doctor, pid, f"k-{i}") for i in range(6)])
        check("exactly one confirm succeeds, the rest 409", codes == [200, 409, 409, 409, 409, 409], codes)
        check("one prescription", _count("prescriptions", "clinic_id = ?", (clinic,)) == 1)
        check("one item set of two lines", _count("prescription_items", "clinic_id = ?", (clinic,)) == 2)
        check("stock deducted once", _stock(clinic) == {"Ashwagandha": 995, "Brahmi": 998})


def test_parallel_confirms_of_different_previews_write_disjoint_correct_item_sets():
    with _app_client() as client:
        clinic = _clinic("race_many")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000, "Brahmi": 1000})
        plans = [[{"name": "Ashwagandha", "grams": i + 1}, {"name": "Brahmi", "grams": 10 + i}] for i in range(8)]
        pids = [_manual(client, doctor, plan) for plan in plans]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [f.result() for f in [pool.submit(_confirm, client, doctor, pid) for pid in pids]]
        check("all eight confirms succeed", [r.status_code for r in results] == [200] * 8, [r.status_code for r in results])
        for i, r in enumerate(results):
            rx = r.json()["prescription_id"]
            check(f"prescription {i}: its own items", _items(rx) == [(1, "Ashwagandha", i + 1), (2, "Brahmi", 10 + i)], _items(rx))
        check("sixteen lines in total", _count("prescription_items", "clinic_id = ?", (clinic,)) == 16)
        check("stock equals the sum of the items",
              _stock(clinic) == {"Ashwagandha": 1000 - sum(range(1, 9)), "Brahmi": 1000 - sum(range(10, 18))})


def test_replay_writes_no_second_item_set():
    with _app_client() as client:
        clinic = _clinic("replay")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000})
        pid = _manual(client, doctor, [{"name": "Ashwagandha", "grams": 5}])
        key = f"replay-{secrets.token_hex(4)}"
        first = _confirm(client, doctor, pid, key=key)
        second = _confirm(client, doctor, pid, key=key)
        check("first confirm succeeds", first.status_code == 200, first.text)
        check("replay returns the original response", second.status_code == 200 and second.json() == first.json())
        check("one item set", _count("prescription_items", "clinic_id = ?", (clinic,)) == 1)
        check("stock deducted once", _stock(clinic) == {"Ashwagandha": 995})
        third = _confirm(client, doctor, pid, key=key, prescribed_herbs=[{"name": "Ashwagandha", "grams": 6}])
        check("same key, different body -> 409, nothing written", third.status_code == 409
              and _count("prescription_items", "clinic_id = ?", (clinic,)) == 1, third.text)


# --- historical compatibility ---------------------------------------------------------------

def test_migration_015_changes_no_existing_row_and_adds_no_items_for_old_prescriptions():
    from db.migrations import run_migrations

    conn, path, mig_dir = _migrated_db(up_to=14)
    try:
        conn.execute("INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
                     "VALUES ('c', 'Ashwagandha', 'x', 100, 'B', 90)")
        conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status) VALUES "
                     "('c', 'Old Patient', 'Patient: Old Patient | Dosha: Vata | Formulation: [{\"name\": \"Ashwagandha\", \"grams\": 5}]', 'Active')")
        conn.commit()

        def snapshot():
            return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                    for t in ("prescriptions", "inventory", "audit_events", "prescription_previews")}

        before = snapshot()
        shutil.copy(os.path.join(MIGRATIONS_DIR, "015_prescription_items.py"), mig_dir)
        applied = run_migrations(conn=conn, migrations_dir=mig_dir)
        check("migration 015 applies on a version-14 database", applied == [15], applied)
        check("no existing row changed", snapshot() == before)
        check("nothing backfilled", conn.execute("SELECT COUNT(*) FROM prescription_items").fetchone()[0] == 0)
    finally:
        conn.close()
        os.remove(path)
        shutil.rmtree(mig_dir)


def test_history_reads_items_first_and_falls_back_to_the_description():
    with _app_client() as client:
        clinic = _clinic("history")
        doctor = _login(client, clinic)
        _set_stock(clinic, {"Ashwagandha": 1000, "Brahmi": 1000})
        patient = client.post("/patients", json={"clinic_patient_id": f"OP-{secrets.token_hex(3)}",
                                                 "display_name": "History Patient"}, headers=doctor).json()
        pk = patient["patient_pk"]

        # A prescription written the pre-015 way: description only, no items.
        conn = sqlite3.connect(DB_PATH)
        try:
            legacy_desc = ('Patient: History Patient | Dosha: Pitta | Formulation: '
                           '[{"name": "Brahmi", "grams": 4}]')
            legacy = conn.execute("INSERT INTO prescriptions (clinic_id, patient_name, description, status, patient_pk, created_at) "
                                  "VALUES (?, 'History Patient', ?, 'Active', ?, '2026-01-01 10:00:00')",
                                  (clinic, legacy_desc, pk)).lastrowid
            conn.commit()
        finally:
            conn.close()

        new = _confirm(client, doctor, _manual(client, doctor, [{"name": "Ashwagandha", "grams": 5},
                                                                {"name": "Brahmi", "grams": 2}], patient_pk=pk))
        check("new confirm succeeds", new.status_code == 200, new.text)

        hist = client.get(f"/patients/{pk}/prescriptions", headers=doctor)
        check("history -> 200", hist.status_code == 200, hist.text)
        records = {r["prescription_id"]: r for r in hist.json()["prescriptions"]}
        check("legacy prescription still readable from its description",
              records[legacy]["prescribed_herbs"] == [{"name": "Brahmi", "grams": 4}]
              and records[legacy]["dosha_imbalance"] == "Pitta", records[legacy])
        check("new prescription's herbs come from its items",
              records[new.json()["prescription_id"]]["prescribed_herbs"]
              == [{"name": "Ashwagandha", "grams": 5}, {"name": "Brahmi", "grams": 2}])
        check("record shape unchanged", set(records[legacy]) == {"prescription_id", "created_at", "status",
                                                                 "dosha_imbalance", "prescribed_herbs",
                                                                 "payment_status", "payment_amount_cents",
                                                                 "payment_method", "payment_reference",
                                                                 "dispensed_at", "dispensed_by_user_id",
                                                                 "invoice_number"})

        # Migration 016: a legacy row has no audit event, so migration 015's
        # seal does not cover it -- but it is no longer the newest
        # prescription, so it cannot gain items either. (Precedence of
        # structured data over the text: tests/test_structured_authority.py.)
        conn = sqlite3.connect(DB_PATH)
        try:
            check("items cannot be added to an older legacy prescription",
                  _raises(conn, "INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams) "
                                "VALUES (?, ?, 1, 'Ashwagandha', 9)", (legacy, clinic), "prescription_items_immutable"))
        finally:
            conn.close()
        records = {r["prescription_id"]: r for r in
                   client.get(f"/patients/{pk}/prescriptions", headers=doctor).json()["prescriptions"]}
        check("legacy record unchanged", records[legacy]["prescribed_herbs"] == [{"name": "Brahmi", "grams": 4}])


def test_backup_verification_requires_the_items_table_from_version_15():
    from scripts.backup_db import TABLE_INTRODUCED_IN, KNOWN_TABLES
    check("prescription_items is introduced by migration 15", TABLE_INTRODUCED_IN.get("prescription_items") == 15)
    check("prescription_items is a known backup table", "prescription_items" in KNOWN_TABLES)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
