"""
A6.3 hardening, item 3 -- ConfirmRequest / herb input bounds.

The A6.3 read-only review found a real asymmetry in backend/schemas.py:
PrescribedHerb.name had no max_length at all (used as a request field via
both ManualPreviewRequest.prescribed_herbs and ConfirmRequest.
prescribed_herbs), ConfirmRequest.dosha_imbalance had no max_length
(unlike ManualPreviewRequest.dosha_imbalance's existing max_length=200 on
the identical concept), and ConfirmRequest.prescribed_herbs had no
list-size bound at all. This batch closes exactly those three gaps in
backend/schemas.py -- see that file's own new comments for the reasoning
behind each bound's exact value (200 mirrors this project's own existing
convention for free-text identifier fields; 50 is new, sized against this
clinic's real 20-herb formulary).

This file proves, for each of the three bounds:
  - normal, realistic values are still accepted (no regression)
  - the exact boundary value is accepted
  - boundary+1 is rejected with 422
  - an oversized payload never reaches inventory mutation or creates a
    prescription (proven by direct database inspection, not just by the
    HTTP status code)
  - the pre-existing "unknown herb" business-logic rejection (400, not
    422) is completely unchanged for a normal-length but genuinely
    unrecognized herb name

No response field, type, or shape was changed by this batch -- only new
validation constraints on REQUEST fields were added. This file does not
assert a new response shape anywhere; every success-path assertion here
checks the SAME fields the pre-existing test suite already checks.

Run directly (no pytest required):
    python tests/test_confirm_input_bounds.py
Or under pytest:
    pytest tests/test_confirm_input_bounds.py -v
"""
import os
import secrets
import sqlite3
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Raises AssertionError immediately on a failed assertion -- same
    convention as every other test file in this project."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _login_new_user(client, clinic_id: str = "default_clinic") -> dict:
    from backend.auth_service import create_user

    username = f"confirm_bounds_test_{secrets.token_hex(6)}"
    password = "confirm-bounds-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _set_stock(herb_name: str, grams: int, clinic_id: str = "default_clinic"):
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


def _get_stock(herb_name: str, clinic_id: str = "default_clinic") -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT stock_grams FROM inventory WHERE herb_name = ? AND clinic_id = ?", (herb_name, clinic_id)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _count_prescriptions_for_patient(patient_name: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM prescriptions WHERE patient_name = ?", (patient_name,)
        ).fetchone()[0]
    finally:
        conn.close()


def _unique_patient_name(label: str) -> str:
    return f"{label} {secrets.token_hex(6)}"


def _new_preview(client, headers, patient_name, herb="Ashwagandha", grams=5):
    resp = client.post(
        "/prescribe/manual-preview",
        json={
            "patient_name": patient_name,
            "dosha_imbalance": "Vata",
            "prescribed_herbs": [{"name": herb, "grams": grams}],
        },
        headers=headers,
    )
    assert resp.status_code == 200, f"test setup failed to create a preview: {resp.status_code} {resp.text}"
    return resp.json()["preview_id"]


# --- Sanity: normal values are unaffected ---------------------------------

def test_normal_dosha_imbalance_and_herb_name_values_still_accepted():
    print("\nTest: normal, realistic dosha_imbalance and herb name correction values are still accepted (no regression)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Brahmi", 5000)
        patient_name = _unique_patient_name("Normal Values Patient")
        preview_id = _new_preview(client, headers, patient_name, herb="Ashwagandha")

        resp = client.post(
            "/prescribe/confirm",
            json={"preview_id": preview_id, "dosha_imbalance": "Vata-Pitta", "prescribed_herbs": [{"name": "Brahmi", "grams": 15}]},
            headers=headers,
        )
        check("a normal doctor correction is accepted", resp.status_code == 200, resp.text)
        body = resp.json()
        check("response has the expected, unchanged fields", set(body.keys()) >= {"patient_name", "dosha_imbalance", "prescribed_herbs", "prescription_id", "created_at"}, body)
        check("dosha_imbalance reflects the correction", body["dosha_imbalance"] == "Vata-Pitta", body)
        check("prescribed_herbs reflects the correction", body["prescribed_herbs"] == [{"name": "Brahmi", "grams": 15}], body)


# --- dosha_imbalance bound: 200 -------------------------------------------

def test_dosha_imbalance_boundary_value_accepted():
    print("\nTest: dosha_imbalance of exactly 200 characters is accepted (boundary)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        patient_name = _unique_patient_name("Dosha Boundary Patient")
        preview_id = _new_preview(client, headers, patient_name)

        boundary_value = "V" * 200
        resp = client.post(
            "/prescribe/confirm",
            json={"preview_id": preview_id, "dosha_imbalance": boundary_value},
            headers=headers,
        )
        check("a 200-character dosha_imbalance (exactly at the bound) is accepted", resp.status_code == 200, resp.text)
        check("the confirmed dosha_imbalance is exactly the 200-character value sent", resp.json().get("dosha_imbalance") == boundary_value)


def test_dosha_imbalance_boundary_plus_one_rejected():
    print("\nTest: dosha_imbalance of 201 characters is rejected with 422 (boundary+1), and nothing is mutated")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        patient_name = _unique_patient_name("Dosha Over Boundary Patient")
        preview_id = _new_preview(client, headers, patient_name)
        stock_before = _get_stock("Ashwagandha")

        oversized_value = "V" * 201
        resp = client.post(
            "/prescribe/confirm",
            json={"preview_id": preview_id, "dosha_imbalance": oversized_value},
            headers=headers,
        )
        check("a 201-character dosha_imbalance is rejected with 422", resp.status_code == 422, resp.text)
        check("no prescription was created", _count_prescriptions_for_patient(patient_name) == 0)
        check("inventory is unchanged", _get_stock("Ashwagandha") == stock_before)

        # The preview must still be usable afterward -- a rejected, oversized
        # correction must not corrupt or consume it.
        followup = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check("the SAME preview can still be confirmed normally after the rejected oversized correction", followup.status_code == 200, followup.text)


# --- PrescribedHerb.name bound: 200 ---------------------------------------

def test_herb_name_boundary_value_is_accepted_by_the_schema():
    print("\nTest: a herb name of exactly 200 characters passes SCHEMA validation (reaches herb resolution, gets the normal 'unknown herb' 400 -- not a 422)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        patient_name = _unique_patient_name("Herb Name Boundary Patient")
        preview_id = _new_preview(client, headers, patient_name)

        boundary_name = "x" * 200
        resp = client.post(
            "/prescribe/confirm",
            json={"preview_id": preview_id, "prescribed_herbs": [{"name": boundary_name, "grams": 5}]},
            headers=headers,
        )
        check(
            "a 200-character herb name passes schema validation (400 'not in inventory', NOT 422) -- "
            "proves the schema boundary itself, distinct from herb-resolution rejection",
            resp.status_code == 400,
            resp.text,
        )


def test_herb_name_boundary_plus_one_rejected_before_reaching_herb_resolution():
    print("\nTest: a herb name of 201 characters is rejected with 422 BEFORE herb resolution ever runs, and nothing is mutated")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        patient_name = _unique_patient_name("Herb Name Over Boundary Patient")
        preview_id = _new_preview(client, headers, patient_name)
        stock_before = _get_stock("Ashwagandha")

        oversized_name = "x" * 201
        resp = client.post(
            "/prescribe/confirm",
            json={"preview_id": preview_id, "prescribed_herbs": [{"name": oversized_name, "grams": 5}]},
            headers=headers,
        )
        check("a 201-character herb name is rejected with 422, not the 400 herb-resolution error", resp.status_code == 422, resp.text)
        check("no prescription was created", _count_prescriptions_for_patient(patient_name) == 0)
        check("inventory is unchanged -- validation rejected this before herb resolution or deduction ran", _get_stock("Ashwagandha") == stock_before)


# --- ConfirmRequest.prescribed_herbs list-size bound: 50 -------------------

def test_prescribed_herbs_list_boundary_of_50_is_accepted_and_dispenses_correctly():
    print("\nTest: a prescribed_herbs correction list of exactly 50 entries is accepted (boundary) and merges/dispenses correctly")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        patient_name = _unique_patient_name("List Boundary Patient")
        preview_id = _new_preview(client, headers, patient_name)

        fifty_entries = [{"name": "Ashwagandha", "grams": 1} for _ in range(50)]
        resp = client.post(
            "/prescribe/confirm",
            json={"preview_id": preview_id, "prescribed_herbs": fifty_entries},
            headers=headers,
        )
        check("a 50-entry prescribed_herbs list (exactly at the bound) is accepted", resp.status_code == 200, resp.text)
        check("the 50 duplicate entries correctly merged into a single 50g deduction", _get_stock("Ashwagandha") == 5000 - 50, _get_stock("Ashwagandha"))


def test_prescribed_herbs_list_boundary_plus_one_rejected():
    print("\nTest: a prescribed_herbs correction list of 51 entries is rejected with 422 (boundary+1), nothing mutated")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        patient_name = _unique_patient_name("List Over Boundary Patient")
        preview_id = _new_preview(client, headers, patient_name)
        stock_before = _get_stock("Ashwagandha")

        fifty_one_entries = [{"name": "Ashwagandha", "grams": 1} for _ in range(51)]
        resp = client.post(
            "/prescribe/confirm",
            json={"preview_id": preview_id, "prescribed_herbs": fifty_one_entries},
            headers=headers,
        )
        check("a 51-entry prescribed_herbs list is rejected with 422", resp.status_code == 422, resp.text)
        check("no prescription was created", _count_prescriptions_for_patient(patient_name) == 0)
        check("inventory is completely unchanged", _get_stock("Ashwagandha") == stock_before)


def test_grossly_oversized_prescribed_herbs_list_cannot_reach_inventory_mutation_or_create_a_prescription():
    print("\nTest: a grossly oversized (500-entry) prescribed_herbs list is rejected and cannot reach inventory mutation or prescription creation")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        _set_stock("Ashwagandha", 5000)
        patient_name = _unique_patient_name("Grossly Oversized List Patient")
        preview_id = _new_preview(client, headers, patient_name)
        stock_before = _get_stock("Ashwagandha")

        huge_list = [{"name": "Ashwagandha", "grams": 1} for _ in range(500)]
        resp = client.post(
            "/prescribe/confirm",
            json={"preview_id": preview_id, "prescribed_herbs": huge_list},
            headers=headers,
        )
        check("a 500-entry prescribed_herbs list is rejected with 422, not a 500 or a slow success", resp.status_code == 422, resp.status_code)
        check("inventory was never touched", _get_stock("Ashwagandha") == stock_before)
        check("no prescription was created", _count_prescriptions_for_patient(patient_name) == 0)

        # The preview must still be usable afterward.
        followup = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check("the SAME preview can still be confirmed normally after the rejected oversized list", followup.status_code == 200, followup.text)


# --- Existing unknown-herb behavior is completely unchanged ---------------

def test_existing_unknown_herb_behavior_is_unchanged():
    print("\nTest: a normal-length but genuinely unrecognized herb name still gets the pre-existing 400 'not in inventory' -- unchanged by this batch")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers = _login_new_user(client)
        patient_name = _unique_patient_name("Unknown Herb Patient")
        preview_id = _new_preview(client, headers, patient_name)

        resp = client.post(
            "/prescribe/confirm",
            json={"preview_id": preview_id, "prescribed_herbs": [{"name": "Unicorn Root", "grams": 5}]},
            headers=headers,
        )
        check("an unrecognized herb name still gets 400, not 422 -- schema bounds did not change this business-logic path", resp.status_code == 400, resp.text)
        check("the error message is the existing 'not in inventory' message, unchanged", "not in inventory" in resp.text, resp.text)


_ALL_TESTS = [
    test_normal_dosha_imbalance_and_herb_name_values_still_accepted,
    test_dosha_imbalance_boundary_value_accepted,
    test_dosha_imbalance_boundary_plus_one_rejected,
    test_herb_name_boundary_value_is_accepted_by_the_schema,
    test_herb_name_boundary_plus_one_rejected_before_reaching_herb_resolution,
    test_prescribed_herbs_list_boundary_of_50_is_accepted_and_dispenses_correctly,
    test_prescribed_herbs_list_boundary_plus_one_rejected,
    test_grossly_oversized_prescribed_herbs_list_cannot_reach_inventory_mutation_or_create_a_prescription,
    test_existing_unknown_herb_behavior_is_unchanged,
]


def run_tests():
    print("--- A6.3 hardening: ConfirmRequest / herb input bounds ---\n")
    _function_failures = []
    for _test_fn in _ALL_TESTS:
        try:
            _test_fn()
        except AssertionError as e:
            _function_failures.append(_test_fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {_test_fn.__name__} ({e})")
        print()
    print(f"--- {_passed} passed, {_failed} failed ---")
    if _function_failures:
        print(f"--- {len(_function_failures)} test function(s) had a failing assertion: {', '.join(_function_failures)} ---")
    return _function_failures


if __name__ == "__main__":
    _failures = run_tests()
    if _failures:
        sys.exit(1)
