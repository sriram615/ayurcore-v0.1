"""
No path may create a prescription with zero herbs.

  1. /prescribe/confirm rejects an explicit empty prescribed_herbs override
     (ConfirmRequest min_length=1).
  2. /prescribe/preview rejects an AI proposal with zero herbs before any
     preview is stored (422 "Invalid AI schema returned"), so there is
     nothing to confirm.
  3. /prescribe/confirm refuses an empty STORED proposal (a preview saved
     before these checks existed) inside its transaction: 422, rolled back.

Before this fix, ConfirmRequest.prescribed_herbs had max_length=50 but no
min_length, so `"prescribed_herbs": []` returned 200 and committed a
prescription with no herbs, no prescription_items and no stock deduction.
It is now a 422 (min_length=1, mirroring ManualPreviewRequest).

Coverage (all on a TEMPORARY database -- the live clinic database is never
touched):
  - an empty override is a 422 on the prescribed_herbs field and writes
    nothing: no prescription, items, audit event, corrections row or
    idempotency record, no deduction, and the preview stays confirmable;
  - with an Idempotency-Key, the rejected request does not burn the key;
  - every valid shape still works: no override, explicit null, a one-line
    override, a duplicate-line override (merged), a dosha-only correction,
    and the existing 50/51-line bound is unchanged;
  - an empty AI proposal stores no preview and writes nothing; an AI
    proposal with herbs still previews and confirms;
  - an empty stored AI or manual proposal cannot be confirmed, writes
    nothing (including no idempotency record), stays PENDING, and then
    confirms once the doctor supplies herbs -- with the same key.

Run directly (no pytest required):
    python tests/test_confirm_empty_herbs.py
"""
import contextlib
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


@contextlib.contextmanager
def _app_on_temp_db():
    """The whole application on a fresh temporary database (startup
    migrations run on it); the real path is always restored."""
    import db.database as database
    import backend.main as main_module
    from fastapi.testclient import TestClient
    tmp = tempfile.mkdtemp(prefix="ayurcore_empty_")
    saved = database.DB_PATH
    database.DB_PATH = os.path.join(tmp, "app.db")
    try:
        with TestClient(main_module.app) as client:
            yield client, database.DB_PATH
    finally:
        database.DB_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)


def _setup(client, db_path) -> tuple[str, dict]:
    from backend.auth_service import create_user
    clinic = f"empty_{secrets.token_hex(4)}"
    username = f"empty_test_{secrets.token_hex(6)}"
    password = "empty-test-password-not-a-real-credential"
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


def _preview(client, headers, herbs=None) -> str:
    resp = client.post("/prescribe/manual-preview", headers=headers, json={
        "patient_name": "Empty Test Patient", "dosha_imbalance": "Vata",
        "prescribed_herbs": herbs or [{"name": "Brahmi", "grams": 5}]})
    assert resp.status_code == 200, resp.text
    return resp.json()["preview_id"]


def _state(db_path, clinic) -> tuple:
    conn = sqlite3.connect(db_path)
    try:
        count = lambda table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE clinic_id = ?", (clinic,)).fetchone()[0]  # noqa: E731
        stock = tuple(r[0] for r in conn.execute(
            "SELECT stock_grams FROM inventory WHERE clinic_id = ? ORDER BY herb_name", (clinic,)))
        return (count("prescriptions"), count("prescription_items"), count("audit_events"), count("idempotency_keys"), stock,
                count("corrections"))
    finally:
        conn.close()


def _preview_status(db_path, preview_id) -> str:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT status FROM prescription_previews WHERE preview_id = ?", (preview_id,)).fetchone()[0]
    finally:
        conn.close()


def test_empty_override_is_rejected_and_writes_nothing():
    with _app_on_temp_db() as (client, db_path):
        clinic, doctor = _setup(client, db_path)
        pid = _preview(client, doctor)
        before = _state(db_path, clinic)

        resp = client.post("/prescribe/confirm", headers=doctor, json={"preview_id": pid, "prescribed_herbs": []})
        errors = resp.json().get("detail", []) if resp.status_code == 422 else []
        check("empty prescribed_herbs -> 422", resp.status_code == 422, resp.text)
        check("the error names the prescribed_herbs field as too short",
              [(e["loc"][-1], e["type"]) for e in errors] == [("prescribed_herbs", "too_short")], errors)
        check("nothing written: no prescription, items, audit event, idempotency record; no deduction",
              _state(db_path, clinic) == before, (_state(db_path, clinic), before))
        check("the preview stays PENDING (confirmable after a fix)", _preview_status(db_path, pid) == "PENDING")

        key = f"empty-{secrets.token_hex(4)}"
        resp = client.post("/prescribe/confirm", headers={**doctor, "Idempotency-Key": key},
                           json={"preview_id": pid, "prescribed_herbs": []})
        check("empty override with an Idempotency-Key -> 422", resp.status_code == 422, resp.text)
        check("... and no idempotency record is created", _state(db_path, clinic) == before)
        ok = client.post("/prescribe/confirm", headers={**doctor, "Idempotency-Key": key},
                         json={"preview_id": pid, "prescribed_herbs": [{"name": "Brahmi", "grams": 3}]})
        check("the same key then confirms a valid override (the key was not burned)", ok.status_code == 200, ok.text)
        after = _state(db_path, clinic)
        check("exactly one prescription, one item, one audit event, one idempotency record; 3 g deducted",
              after[:4] == (1, 1, 1, 1) and after[4] == (100000, 100000 - 3), after)


def test_valid_confirmations_still_work():
    with _app_on_temp_db() as (client, db_path):
        clinic, doctor = _setup(client, db_path)
        confirm = lambda pid, **body: client.post("/prescribe/confirm", headers=doctor, json={"preview_id": pid, **body})  # noqa: E731
        cases = [
            ("no override (stored manual proposal)", {}, [{"name": "Brahmi", "grams": 5}]),
            ("explicit null override (stored proposal)", {"prescribed_herbs": None}, [{"name": "Brahmi", "grams": 5}]),
            ("one-line doctor override", {"prescribed_herbs": [{"name": "Ashwagandha", "grams": 7}]},
             [{"name": "Ashwagandha", "grams": 7}]),
            ("duplicate-line doctor override (merged)",
             {"prescribed_herbs": [{"name": "brahmi", "grams": 2}, {"name": "Ashwagandha", "grams": 1}, {"name": "Bacopa monnieri", "grams": 4}]},
             [{"name": "Brahmi", "grams": 6}, {"name": "Ashwagandha", "grams": 1}]),
            ("dosha-only correction", {"dosha_imbalance": "Pitta"}, [{"name": "Brahmi", "grams": 5}]),
            ("50-line override (existing upper bound)", {"prescribed_herbs": [{"name": "Brahmi", "grams": 1}] * 50},
             [{"name": "Brahmi", "grams": 50}]),
        ]
        for label, body, expected in cases:
            resp = confirm(_preview(client, doctor), **body)
            check(f"{label} -> 200", resp.status_code == 200, resp.text)
            check(f"{label}: committed herbs as expected", resp.json()["prescribed_herbs"] == expected, resp.json()["prescribed_herbs"])
        committed = _state(db_path, clinic)
        check("every valid confirm wrote a prescription with items and an audit event",
              committed[0] == len(cases) and committed[2] == len(cases) and committed[1] >= len(cases), committed)
        resp = confirm(_preview(client, doctor), prescribed_herbs=[{"name": "Brahmi", "grams": 1}] * 51)
        check("51-line override is still a 422 (upper bound unchanged)", resp.status_code == 422, resp.status_code)


# --- empty AI proposals and empty stored proposals ----------------------------------------

@contextlib.contextmanager
def _ai_returns(herbs):
    """Stubs the AI parser; no real Gemini client may be constructed."""
    import google.genai as genai_real
    import backend.main as main_module
    from backend.schemas import ClinicalParse
    calls = []

    def _parse(notes, clinic_id=None):
        calls.append(notes)
        return ClinicalParse(dosha_imbalance="Vata", prescribed_herbs=herbs)

    def _boom(*args, **kwargs):
        raise AssertionError("no real Gemini client may be constructed in these tests")

    saved = (genai_real.Client, main_module.parse_clinical_notes)
    genai_real.Client, main_module.parse_clinical_notes = _boom, _parse
    try:
        yield calls
    finally:
        genai_real.Client, main_module.parse_clinical_notes = saved


def _count_previews(db_path, clinic) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM prescription_previews WHERE clinic_id = ?", (clinic,)).fetchone()[0]
    finally:
        conn.close()


def test_empty_ai_proposal_is_rejected_before_a_preview_exists():
    with _app_on_temp_db() as (client, db_path):
        clinic, doctor = _setup(client, db_path)
        before = _state(db_path, clinic)
        with _ai_returns([]) as calls:
            resp = client.post("/prescribe/preview", headers=doctor,
                               json={"patient_name": "Empty AI Patient", "notes": "Follow-up; no medicines discussed."})
        check("the AI was asked (the check is on its reply)", len(calls) == 1)
        check("empty AI proposal -> 422 'Invalid AI schema returned' (the clients' existing AI-reply message)",
              resp.status_code == 422 and resp.json() == {"detail": "Invalid AI schema returned"}, resp.text)
        check("no preview stored, so there is nothing to confirm", _count_previews(db_path, clinic) == 0)
        check("nothing else written; no stock deducted", _state(db_path, clinic) == before)

        with _ai_returns([{"name": "brahmi", "grams": 4}]):
            resp = client.post("/prescribe/preview", headers=doctor,
                               json={"patient_name": "Herb AI Patient", "notes": "Poor memory, recommend brahmi."})
        check("an AI proposal with herbs still previews", resp.status_code == 200
              and resp.json()["prescribed_herbs"] == [{"name": "Brahmi", "grams": 4}], resp.text)
        ok = client.post("/prescribe/confirm", headers=doctor, json={"preview_id": resp.json()["preview_id"]})
        check("... and confirms", ok.status_code == 200 and ok.json()["prescribed_herbs"] == [{"name": "Brahmi", "grams": 4}], ok.text)


def _store_empty_preview(db_path, clinic, is_manual: int) -> str:
    """A PENDING preview with an empty stored proposal, bound to the
    doctor's live session -- as code older than these checks could store."""
    conn = sqlite3.connect(db_path)
    try:
        session_id = conn.execute(
            "SELECT s.session_id FROM sessions s JOIN users u ON u.user_id = s.user_id "
            "WHERE u.clinic_id = ? ORDER BY s.session_id DESC LIMIT 1", (clinic,)).fetchone()[0]
        preview_id = f"legacy-empty-{secrets.token_hex(6)}"
        conn.execute(
            "INSERT INTO prescription_previews (preview_id, clinic_id, patient_name, is_manual, ai_dosha_imbalance, "
            "ai_prescribed_herbs_json, status, created_by_session, created_at, expires_at) "
            "VALUES (?, ?, 'Legacy Empty Patient', ?, 'Vata', '[]', 'PENDING', ?, datetime('now'), datetime('now', '+30 minutes'))",
            (preview_id, clinic, is_manual, str(session_id)))
        conn.commit()
        return preview_id
    finally:
        conn.close()


def test_empty_stored_proposal_cannot_be_confirmed():
    import backend.main as main_module

    with _app_on_temp_db() as (client, db_path):
        clinic, doctor = _setup(client, db_path)
        for label, is_manual in (("stored AI proposal", 0), ("stored manual proposal", 1)):
            pid = _store_empty_preview(db_path, clinic, is_manual)
            before = _state(db_path, clinic)
            resp = client.post("/prescribe/confirm", headers=doctor, json={"preview_id": pid})
            check(f"[{label}] empty proposal, no override -> 422", resp.status_code == 422
                  and resp.json() == {"detail": main_module._EMPTY_PRESCRIPTION_DETAIL}, resp.text)
            check(f"[{label}] nothing written, no deduction", _state(db_path, clinic) == before)

            key = f"empty-stored-{secrets.token_hex(4)}"
            resp = client.post("/prescribe/confirm", headers={**doctor, "Idempotency-Key": key}, json={"preview_id": pid})
            check(f"[{label}] with an Idempotency-Key -> 422 and no idempotency record (rolled back)",
                  resp.status_code == 422 and _state(db_path, clinic) == before, _state(db_path, clinic))
            check(f"[{label}] preview stays PENDING", _preview_status(db_path, pid) == "PENDING")
            ok = client.post("/prescribe/confirm", headers={**doctor, "Idempotency-Key": key},
                             json={"preview_id": pid, "prescribed_herbs": [{"name": "Ashwagandha", "grams": 2}]})
            check(f"[{label}] the same key then confirms once the doctor supplies herbs", ok.status_code == 200
                  and ok.json()["prescribed_herbs"] == [{"name": "Ashwagandha", "grams": 2}], ok.text)
            after = _state(db_path, clinic)
            check(f"[{label}] exactly one prescription, item, audit event and idempotency record added",
                  after[:4] == tuple(b + 1 for b in before[:4]), (before, after))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
