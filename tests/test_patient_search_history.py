"""
Patient Registry Phase 2A -- GET /patients?q= and
GET /patients/{patient_pk}/prescriptions (read-only).

Covers RBAC, clinic isolation, exact-ID ranking, search behavior (case,
wildcard escaping, min/max query length), no-match, bounded results,
foreign-patient 404 indistinguishable from a missing one, newest-first
history, exclusion of unlinked prescriptions, pagination bounds, no audit
internals in responses, and that neither endpoint calls the AI or writes.

Run directly (no pytest required):
    python tests/test_patient_search_history.py
"""
import os
import secrets
import sqlite3
import sys

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
    return f"psh_{tag}_{secrets.token_hex(4)}"


def _login(client, clinic_id: str, role: str = "DOCTOR") -> dict:
    from backend.auth_service import create_user

    username = f"psh_test_{secrets.token_hex(6)}"
    password = "psh-test-password-not-a-real-credential"
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


def _register(client, headers, clinic_patient_id, display_name, phone=None):
    body = {"clinic_patient_id": clinic_patient_id, "display_name": display_name}
    if phone:
        body["phone"] = phone
    resp = client.post("/patients", json=body, headers=headers)
    assert resp.status_code == 201, f"test setup failed to register a patient: {resp.status_code} {resp.text}"
    return resp.json()


def _prescribe(client, headers, patient_pk=None, patient_name="Unlinked Name", grams=5, dosha="Vata"):
    body = {"patient_name": patient_name, "dosha_imbalance": dosha,
            "prescribed_herbs": [{"name": "Ashwagandha", "grams": grams}]}
    if patient_pk is not None:
        body["patient_pk"] = patient_pk
    prev = client.post("/prescribe/manual-preview", json=body, headers=headers)
    assert prev.status_code == 200, prev.text
    conf = client.post("/prescribe/confirm", json={"preview_id": prev.json()["preview_id"]}, headers=headers)
    assert conf.status_code == 200, conf.text
    return conf.json()["prescription_id"]


def _search(client, headers, q):
    return client.get("/patients", params={"q": q}, headers=headers)


def _history(client, headers, patient_pk, **params):
    return client.get(f"/patients/{patient_pk}/prescriptions", params=params, headers=headers)


def _app_client():
    import backend.main as main_module
    from fastapi.testclient import TestClient
    return TestClient(main_module.app)


def _table_counts():
    conn = sqlite3.connect(DB_PATH)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("patients", "prescriptions", "prescription_previews", "audit_events",
                          "corrections", "idempotency_keys")}
    finally:
        conn.close()


# --- RBAC -----------------------------------------------------------------------

def test_rbac_doctor_and_admin_allowed_auditor_and_anonymous_rejected():
    with _app_client() as client:
        clinic = _clinic("rbac")
        doctor = _login(client, clinic)
        patient = _register(client, doctor, "OP-R1", "Rbac Patient")
        for role in ("DOCTOR", "ADMIN"):
            headers = doctor if role == "DOCTOR" else _login(client, clinic, "ADMIN")
            check(f"{role} can search", _search(client, headers, "Rbac").status_code == 200)
            check(f"{role} can read history", _history(client, headers, patient["patient_pk"]).status_code == 200)
        auditor = _login(client, clinic, "AUDITOR")
        check("AUDITOR search -> 403", _search(client, auditor, "Rbac").status_code == 403)
        check("AUDITOR history -> 403", _history(client, auditor, patient["patient_pk"]).status_code == 403)
        check("anonymous search -> 401", client.get("/patients", params={"q": "Rbac"}).status_code == 401)
        check("anonymous history -> 401",
              client.get(f"/patients/{patient['patient_pk']}/prescriptions").status_code == 401)


# --- search ---------------------------------------------------------------------

def test_search_matches_id_and_name_case_insensitively_with_selection_fields_only():
    with _app_client() as client:
        clinic = _clinic("search")
        h = _login(client, clinic)
        _register(client, h, "OP-1001", "Anjali Nair", phone="9847000001")
        _register(client, h, "OP-1002", "Anjana Rao", phone="9847000002")
        _register(client, h, "OP-2001", "Ravi Kumar")

        resp = _search(client, h, "anj")
        names = [r["display_name"] for r in resp.json()["results"]]
        check("name search is case-insensitive and substring", names == ["Anjali Nair", "Anjana Rao"], resp.json())
        check("results carry only selection fields",
              all(set(r) == {"patient_pk", "clinic_patient_id", "display_name"} for r in resp.json()["results"]),
              resp.json())
        check("phone numbers never appear in search results", "9847000001" not in resp.text)
        check("clinic_id never appears in search results", clinic not in resp.text)

        ids = [r["clinic_patient_id"] for r in _search(client, h, "op-100").json()["results"]]
        check("ID search is case-insensitive", ids == ["OP-1001", "OP-1002"], ids)
        check("surrounding whitespace in q is ignored",
              [r["clinic_patient_id"] for r in _search(client, h, "  OP-2001  ").json()["results"]] == ["OP-2001"])


def test_exact_id_match_is_ranked_first_for_selection():
    with _app_client() as client:
        clinic = _clinic("exact")
        h = _login(client, clinic)
        # Registered in an order that would NOT put the exact match first by
        # name or by insertion order.
        _register(client, h, "OP-12345", "Aaron Prefix Match")
        _register(client, h, "X-OP-12-Y", "Aaron Contains Match")
        exact = _register(client, h, "OP-12", "Zubin Exact Match")
        _register(client, h, "OP-123", "Aaron Longer Prefix")
        results = _search(client, h, "op-12").json()["results"]
        check("exact ID match is first", results[0]["patient_pk"] == exact["patient_pk"], results)
        check("then ID-prefix matches, then other matches",
              [r["clinic_patient_id"] for r in results] == ["OP-12", "OP-123", "OP-12345", "X-OP-12-Y"],
              [r["clinic_patient_id"] for r in results])


def test_like_wildcards_in_the_query_are_literal():
    with _app_client() as client:
        clinic = _clinic("wild")
        h = _login(client, clinic)
        _register(client, h, "W%1", "Percent Patient")
        _register(client, h, "W_2", "Underscore Patient")
        _register(client, h, "WAB3", "Plain Patient")
        check("'%' matches only a literal percent",
              [r["clinic_patient_id"] for r in _search(client, h, "W%").json()["results"]] == ["W%1"])
        check("'_' matches only a literal underscore",
              [r["clinic_patient_id"] for r in _search(client, h, "W_").json()["results"]] == ["W_2"])
        check("'%%' matches nothing when no ID/name contains it", _search(client, h, "%%").json()["results"] == [])


def test_query_length_bounds_and_no_match():
    with _app_client() as client:
        h = _login(client, _clinic("bounds"))
        check("missing q -> 422", client.get("/patients", headers=h).status_code == 422)
        check("1-char q -> 422", _search(client, h, "a").status_code == 422)
        check("whitespace-padded 1-char q -> 422", _search(client, h, "   a   ").status_code == 422)
        check("101-char q -> 422", _search(client, h, "a" * 101).status_code == 422)
        resp = _search(client, h, "zz-no-such-patient")
        check("no match -> 200 with empty results, not truncated",
              resp.status_code == 200 and resp.json() == {"results": [], "truncated": False}, resp.text)


def test_search_result_count_is_bounded():
    with _app_client() as client:
        clinic = _clinic("limit")
        h = _login(client, clinic)
        for i in range(25):
            _register(client, h, f"BULK-{i:02d}", f"Bulk Person {i:02d}")
        body = _search(client, h, "Bulk Person").json()
        check("at most 20 results", len(body["results"]) == 20, len(body["results"]))
        check("truncated flag set when more matched", body["truncated"] is True)
        exact = _search(client, h, "BULK-24").json()
        check("a precise query still finds a patient beyond the first page",
              [r["clinic_patient_id"] for r in exact["results"]] == ["BULK-24"] and exact["truncated"] is False, exact)


def test_search_never_crosses_clinics():
    with _app_client() as client:
        a, b = _clinic("iso_a"), _clinic("iso_b")
        ha, hb = _login(client, a), _login(client, b)
        mine = _register(client, ha, "OP-SAME", "Clinic A Person")
        _register(client, hb, "OP-SAME", "Clinic B Secret Person")
        _register(client, hb, "OP-B-ONLY", "Shared Surname Menon")
        resp = _search(client, ha, "OP-SAME")
        check("same ID in two clinics: only the caller's patient is returned",
              [r["patient_pk"] for r in resp.json()["results"]] == [mine["patient_pk"]], resp.json())
        check("other clinic's name never appears", "Clinic B Secret Person" not in resp.text)
        check("a query matching only another clinic's patients returns nothing",
              _search(client, ha, "Menon").json() == {"results": [], "truncated": False})


# --- history ------------------------------------------------------------------------

def test_history_is_newest_first_and_excludes_unlinked_and_other_patients():
    with _app_client() as client:
        clinic = _clinic("hist")
        h = _login(client, clinic)
        _set_stock("Ashwagandha", 5000, clinic)
        p = _register(client, h, "OP-H1", "History Patient")
        other = _register(client, h, "OP-H2", "Other Patient")

        first = _prescribe(client, h, p["patient_pk"], grams=5, dosha="Vata")
        _prescribe(client, h, patient_name="History Patient", grams=9)           # unlinked, SAME name
        _prescribe(client, h, other["patient_pk"], grams=8)                      # another patient
        second = _prescribe(client, h, p["patient_pk"], grams=6, dosha="Pitta")
        third = _prescribe(client, h, p["patient_pk"], grams=7, dosha="Vata-Kapha")

        resp = _history(client, h, p["patient_pk"])
        check("history 200", resp.status_code == 200, resp.text)
        body = resp.json()
        check("patient block identifies the selected patient",
              body["patient"] == {"patient_pk": p["patient_pk"], "clinic_patient_id": "OP-H1",
                                  "display_name": "History Patient"}, body["patient"])
        rx = body["prescriptions"]
        check("only this patient's linked prescriptions, newest first",
              [r["prescription_id"] for r in rx] == [third, second, first], [r["prescription_id"] for r in rx])
        check("an unlinked prescription with the same name is excluded", len(rx) == 3)
        check("dosha and dispensed herbs are read back from the prescription record",
              [(r["dosha_imbalance"], r["prescribed_herbs"]) for r in rx] == [
                  ("Vata-Kapha", [{"name": "Ashwagandha", "grams": 7}]),
                  ("Pitta", [{"name": "Ashwagandha", "grams": 6}]),
                  ("Vata", [{"name": "Ashwagandha", "grams": 5}]),
              ], rx)
        check("each record has only the history fields",
              all(set(r) == {"prescription_id", "created_at", "status", "dosha_imbalance", "prescribed_herbs"} for r in rx),
              rx)
        check("page metadata", (body["limit"], body["offset"], body["has_more"]) == (20, 0, False), body)
        for internal in ("event_hash", "previous_event_hash", "preview_id", "actor_user_id", "description",
                         "clinic_id", "created_by", "ai_"):
            check(f"no audit/internal field {internal!r} in history", internal not in resp.text)

        empty = _history(client, h, other["patient_pk"]).json()
        check("another patient's history holds only their own prescription", len(empty["prescriptions"]) == 1)
        fresh = _register(client, h, "OP-H3", "No Visits Yet")
        check("a patient with no prescriptions gets an empty list",
              _history(client, h, fresh["patient_pk"]).json()["prescriptions"] == [])


def test_history_pagination_bounds():
    with _app_client() as client:
        clinic = _clinic("page")
        h = _login(client, clinic)
        _set_stock("Ashwagandha", 5000, clinic)
        p = _register(client, h, "OP-P1", "Paged Patient")
        ids = [_prescribe(client, h, p["patient_pk"], grams=g) for g in (1, 2, 3)]

        page1 = _history(client, h, p["patient_pk"], limit=2).json()
        check("limit=2 returns 2 newest with has_more",
              [r["prescription_id"] for r in page1["prescriptions"]] == ids[::-1][:2] and page1["has_more"] is True, page1)
        page2 = _history(client, h, p["patient_pk"], limit=2, offset=2).json()
        check("offset=2 returns the remaining one, no more",
              [r["prescription_id"] for r in page2["prescriptions"]] == [ids[0]] and page2["has_more"] is False, page2)
        for params, why in (({"limit": 0}, "limit 0"), ({"limit": 51}, "limit 51"), ({"offset": -1}, "negative offset")):
            check(f"{why} -> 422", _history(client, h, p["patient_pk"], **params).status_code == 422)
        check("non-positive patient_pk -> 422", _history(client, h, 0).status_code == 422)


def test_foreign_patient_history_is_an_indistinguishable_404():
    with _app_client() as client:
        a, b = _clinic("fh_a"), _clinic("fh_b")
        ha = _login(client, a)
        _set_stock("Ashwagandha", 5000, a)
        foreign = _register(client, ha, "OP-FOREIGN", "Foreign Secret Patient", phone="7000000000")
        _prescribe(client, ha, foreign["patient_pk"], grams=5)

        for role in ("DOCTOR", "ADMIN"):
            hb = _login(client, b, role)
            cross = _history(client, hb, foreign["patient_pk"])
            missing = _history(client, hb, 2_000_000_000)
            check(f"{role} of another clinic -> 404", cross.status_code == 404, cross.text)
            check(f"{role}: foreign and nonexistent patients are indistinguishable",
                  (cross.status_code, cross.json()) == (missing.status_code, missing.json()), (cross.text, missing.text))
            for secret in ("Foreign Secret Patient", "OP-FOREIGN", "7000000000", "Ashwagandha", a):
                check(f"{role}: 404 leaks nothing ({secret!r})", secret not in cross.text)


# --- no AI, no writes -----------------------------------------------------------------

def test_search_and_history_make_no_ai_calls_and_no_writes():
    import google.genai as genai_real
    import backend.main as main_module

    def _boom(*args, **kwargs):
        raise AssertionError("patient search/history must never call the AI")

    with _app_client() as client:
        clinic = _clinic("readonly")
        h = _login(client, clinic)
        _set_stock("Ashwagandha", 5000, clinic)
        p = _register(client, h, "OP-RO1", "Read Only Patient")
        _prescribe(client, h, p["patient_pk"], grams=5)
        admin = _login(client, clinic, "ADMIN")

        original_client, original_parse = genai_real.Client, main_module.parse_clinical_notes
        genai_real.Client, main_module.parse_clinical_notes = _boom, _boom
        try:
            before = _table_counts()
            for headers in (h, admin):
                for q in ("Read Only", "OP-RO1", "nothing-matches"):
                    check("search 200", _search(client, headers, q).status_code == 200)
                check("history 200", _history(client, headers, p["patient_pk"]).status_code == 200)
                check("foreign/missing history 404", _history(client, headers, 2_000_000_000).status_code == 404)
            after = _table_counts()
        finally:
            genai_real.Client, main_module.parse_clinical_notes = original_client, original_parse
        check("no rows written to any clinical, registry, audit or idempotency table", before == after, (before, after))


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, func in tests:
        func()
    print(f"\n{_passed} checks passed across {len(tests)} tests")
