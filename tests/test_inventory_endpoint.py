"""
v0.1 -- GET /inventory (read-only).

Covers RBAC (DOCTOR/ADMIN allowed; AUDITOR, anonymous and bad tokens
rejected), clinic isolation (session clinic only; a client-supplied clinic is
ignored), the response shape, and that the endpoint never mutates inventory
or reaches the deduction code.

Run directly (no pytest required):
    python tests/test_inventory_endpoint.py
"""
import os
import secrets
import sqlite3
import sys
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail=""):
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
    return f"inv_{tag}_{secrets.token_hex(4)}"


def _login(client, clinic_id: str, role: str = "DOCTOR") -> dict:
    from backend.auth_service import create_user

    username = f"inv_test_{secrets.token_hex(6)}"
    password = "inv-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id, role=role)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _add_stock(clinic_id: str, rows: dict):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executemany(
            "INSERT INTO inventory (clinic_id, herb_name, botanical_name, stock_grams, batch_code, potency_score) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(clinic_id, name, "test botanical name", grams, "TEST-BATCH", 90) for name, grams in rows.items()],
        )
        conn.commit()
    finally:
        conn.close()


def _inventory_snapshot():
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute("SELECT * FROM inventory ORDER BY id").fetchall()
    finally:
        conn.close()


def _table_counts():
    conn = sqlite3.connect(DB_PATH)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("inventory", "prescriptions", "prescription_items", "prescription_previews",
                          "audit_events", "corrections", "idempotency_keys")}
    finally:
        conn.close()


def _app_client():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    return TestClient(main_module.app)


def _items(resp) -> dict:
    return {i["herb_name"]: i["stock_grams"] for i in resp.json()["items"]}


# --- tests ------------------------------------------------------------------------

def test_doctor_sees_own_clinic_stock_sorted():
    with _app_client() as client:
        clinic = _clinic("doc")
        _add_stock(clinic, {"Triphala": 700, "Ashwagandha": 1200, "Brahmi": 300})
        resp = client.get("/inventory", headers=_login(client, clinic))
        check("DOCTOR -> 200", resp.status_code == 200, resp.text)
        body = resp.json()
        check("only an items list is returned", set(body) == {"items"}, body)
        check("items are herb_name + stock_grams only",
              all(set(i) == {"herb_name", "stock_grams"} for i in body["items"]), body)
        check("sorted by herb name",
              [i["herb_name"] for i in body["items"]] == ["Ashwagandha", "Brahmi", "Triphala"], body)
        check("stock values", _items(resp) == {"Ashwagandha": 1200, "Brahmi": 300, "Triphala": 700}, body)


def test_admin_sees_own_clinic_stock():
    with _app_client() as client:
        clinic = _clinic("adm")
        _add_stock(clinic, {"Shatavari": 450})
        resp = client.get("/inventory", headers=_login(client, clinic, "ADMIN"))
        check("ADMIN -> 200", resp.status_code == 200, resp.text)
        check("ADMIN sees the clinic's stock", _items(resp) == {"Shatavari": 450}, resp.text)


def test_unauthorized_access_is_rejected():
    with _app_client() as client:
        clinic = _clinic("unauth")
        _add_stock(clinic, {"Brahmi": 100})
        check("AUDITOR -> 403", client.get("/inventory", headers=_login(client, clinic, "AUDITOR")).status_code == 403)
        anonymous = client.get("/inventory")
        check("no token -> 401", anonymous.status_code == 401, anonymous.text)
        check("401 body has no stock", "Brahmi" not in anonymous.text)
        bogus = client.get("/inventory", headers={"Authorization": "Bearer not-a-real-session-token"})
        check("invalid token -> 401", bogus.status_code == 401, bogus.text)
        malformed = client.get("/inventory", headers={"Authorization": "Basic abc"})
        check("non-Bearer scheme -> 401", malformed.status_code == 401, malformed.text)

        doctor = _login(client, clinic)
        client.post("/auth/logout", headers=doctor)
        check("revoked token -> 401", client.get("/inventory", headers=doctor).status_code == 401)


def test_clinic_isolation():
    with _app_client() as client:
        clinic_a, clinic_b, clinic_empty = _clinic("a"), _clinic("b"), _clinic("empty")
        _add_stock(clinic_a, {"Ashwagandha": 111, "Brahmi": 222})
        _add_stock(clinic_b, {"Ashwagandha": 999, "Guggulu": 333})
        doctor_a, doctor_b = _login(client, clinic_a), _login(client, clinic_b)

        check("clinic A sees only its rows",
              _items(client.get("/inventory", headers=doctor_a)) == {"Ashwagandha": 111, "Brahmi": 222})
        check("clinic B sees only its rows",
              _items(client.get("/inventory", headers=doctor_b)) == {"Ashwagandha": 999, "Guggulu": 333})
        for params in ({"clinic_id": clinic_b}, {"clinic": clinic_b}):
            resp = client.get("/inventory", params=params, headers=doctor_a)
            check(f"query {list(params)} cannot select another clinic",
                  resp.status_code == 200 and _items(resp) == {"Ashwagandha": 111, "Brahmi": 222}, resp.text)
        resp = client.get("/inventory", headers={**doctor_a, "X-Clinic-Id": clinic_b})
        check("header cannot select another clinic", _items(resp) == {"Ashwagandha": 111, "Brahmi": 222})

        empty = client.get("/inventory", headers=_login(client, clinic_empty))
        check("clinic without stock -> 200 and empty, not another clinic's rows",
              empty.status_code == 200 and empty.json() == {"items": []}, empty.text)


def test_no_inventory_mutation():
    import backend.main as main_module

    with _app_client() as client:
        clinic = _clinic("nomut")
        _add_stock(clinic, {"Ashwagandha": 500, "Brahmi": 400})
        doctor, admin = _login(client, clinic), _login(client, clinic, "ADMIN")
        inventory_before, counts_before = _inventory_snapshot(), _table_counts()

        def _forbidden(*args, **kwargs):
            raise AssertionError("GET /inventory reached deduct_and_prescribe")

        with mock.patch.object(main_module, "deduct_and_prescribe", _forbidden):
            for headers in (doctor, admin, doctor):
                check("GET -> 200", client.get("/inventory", headers=headers).status_code == 200)

        check("inventory rows unchanged (every column)", _inventory_snapshot() == inventory_before)
        check("no rows written anywhere relevant", _table_counts() == counts_before)

        for method in ("post", "put", "patch", "delete"):
            resp = getattr(client, method)("/inventory", headers=doctor)
            check(f"{method.upper()} /inventory -> 405", resp.status_code == 405, resp.text)
        check("inventory still unchanged after other methods", _inventory_snapshot() == inventory_before)

        names = set(main_module.get_inventory.__code__.co_names)
        check("handler references list_stock", "list_stock" in names, names)
        check("handler references no mutation function",
              not names & {"deduct_and_prescribe", "get_connection", "execute", "commit"}, names)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{_passed} passed, {_failed} failed")
