"""
Idempotency HTTP-layer tests (Phase B, A4) -- covers the `Idempotency-Key`
header on POST /prescribe/confirm in backend/main.py, built on
backend/idempotency_store.py (see tests/test_idempotency_store.py for that
module's own, sandbox-runnable, non-FastAPI tests).

Like every other FastAPI-dependent test in this project, this file
REQUIRES MAC VERIFICATION (FastAPI + argon2-cffi installed) -- it is not
expected to run in the sandbox.

Specifically checks, per the approved Phase B v2 design's adversarial test
plan:
  1. `Idempotency-Key` remains optional at the API level: a confirm request
     that omits it behaves exactly as it did before A4 -- no
     idempotency_keys row is ever created, no dedup guarantee, normal
     success/failure semantics unchanged.
  2. A retry with the SAME key and the SAME payload (standing in for
     "reload during confirmation" / "network drop after backend commit,
     client retries") returns the exact original response, byte-for-byte
     (as parsed JSON), and creates exactly ONE prescription, ONE inventory
     deduction, and -- critically -- does NOT log a second correction row.
  3. A duplicate click (two genuinely concurrent HTTP requests, same key,
     same payload) resolves to exactly one commit; the other replays.
  4. A changed payload reusing the OLD key (simulating a frontend bug --
     Feature 1's design explicitly requires the backend to catch this even
     though the frontend is supposed to prevent it) gets 409, and does NOT
     touch the original committed prescription/inventory/correction state.
  5. "Two tabs" -- two DIFFERENT keys confirming the SAME preview -- still
     results in exactly one committed prescription; the second key's
     attempt gets A1's ordinary PreviewAlreadyConfirmedError -> 409,
     unaffected by (and not confused with) the idempotency machinery.
  6. Cross-user identical key string: two different DOCTORs who happen to
     send the identical Idempotency-Key value for two different previews
     never collide -- both succeed independently. (Storage-layer proof
     already exists in tests/test_idempotency_store.py; this proves it at
     the real HTTP layer too.)
  7. Session expiry mid-flow: after logout + a fresh login as the SAME
     user, the ORIGINAL idempotency key is still safely reusable and
     correctly replays -- A4's scope is (user_id, operation, key), not
     session-bound, exactly so a re-login mid-retry doesn't orphan a
     legitimate in-flight confirmation.
  8. A fresh (non-replay) confirm's response shape is completely unchanged
     from pre-A4 behavior -- A4 adds no new required request/response
     field to the locked contract.

TEST-ISOLATION INCIDENT (fixed after a real Mac-verification finding):
5 of this file's 8 test functions initially failed under `pytest
tests/test_idempotency.py -v` (but passed under `python3
tests/test_idempotency.py`, run just before it) -- not because of any
idempotency defect, but because `_count_prescriptions_for_patient()`
counts ALL historical rows in the real, persistent db/ayurcore.db for an
exact patient_name, with no way to scope to "rows created by just this
invocation." Five tests used a hardcoded, literal patient_name -- every
OTHER identifier in this file (username, clinic_id-adjacent test data,
and especially the Idempotency-Key itself) was already randomized per
run, but patient_name was not. Running the direct script and then pytest
back-to-back against the SAME shared database meant the second run's
`count == 1` assertion was measuring TWO independent runs' worth of
real, correct, legitimately-separate prescriptions -- proven by direct
database inspection (see the A4 remediation report): a genuine replay of
the SAME idempotency key, exercised twice in a row against a fresh local
database, always returns the identical prescription_id and never creates
a second row; two independent confirmations sharing only a literal
patient_name correctly and legitimately produce two different
prescription_ids.

Fixed via `_unique_patient_name()` below -- every patient_name this file
uses in a `_count_prescriptions_for_patient()` assertion is now generated
fresh per test invocation, the same isolation strategy already used
everywhere else in this file (randomized username, randomized
Idempotency-Key). This is NOT a database-isolation fix and NOT a
weakened assertion (never `== 1` softened to `<= 1`) -- it keeps testing
against the real, persistent, shared db/ayurcore.db, per this project's
own established convention (see tests/test_preview_store.py and
tests/test_rbac.py's module docstrings), and every assertion still
proves exactly what it originally claimed to: one logical confirmation,
one prescription, one inventory deduction, one idempotency row, no
duplicate correction, and a replay that never re-executes business
logic.

Run directly (no pytest required):
    python tests/test_idempotency.py
Or under pytest:
    pytest tests/test_idempotency.py -v
"""
import json
import os
import secrets
import sqlite3
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "ayurcore.db")

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Records AND enforces one assertion -- raises AssertionError
    immediately on failure. See tests/test_rbac.py's module docstring,
    "PYTEST COMPATIBILITY", for exactly why every security-relevant test
    file in this project uses a raising check(), not a printing-only one."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _login_new_user(client, clinic_id: str = "default_clinic", role: str = "DOCTOR") -> dict:
    """Same convention as tests/test_rbac.py's identical helper."""
    from backend.auth_service import create_user

    username = f"idem_test_{secrets.token_hex(6)}"
    password = "idem-test-password-not-a-real-credential"
    create_user(username, password, clinic_id=clinic_id, role=role)
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"test setup failed to log in: {resp.status_code} {resp.text}"
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}, username, password


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
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT stock_grams FROM inventory WHERE herb_name = ? AND clinic_id = ?", (herb_name, clinic_id)
        ).fetchone()
        return row["stock_grams"] if row else 0
    finally:
        conn.close()


def _count_idempotency_rows(idempotency_key: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM idempotency_keys WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()[0]
    finally:
        conn.close()


def _count_corrections_for_prescription(prescription_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM corrections WHERE prescription_id = ?", (prescription_id,)
        ).fetchone()[0]
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
    """TEST-ISOLATION FIX (Phase B, A4 Mac-verification finding -- see this
    file's module docstring addendum below for the full incident). Every
    prescription this suite creates is written into the REAL, persistent
    db/ayurcore.db with NO teardown -- the same deliberate project
    convention tests/test_preview_store.py and tests/test_rbac.py already
    use (see either's module docstring). _count_prescriptions_for_patient()
    counts ALL historical rows for a given patient_name; it has no way to
    scope to "rows created by just this invocation." A HARDCODED literal
    patient_name therefore silently accumulates one extra row every time
    this suite runs again (direct execution, then pytest, then pytest
    again...), which makes any `count_prescriptions(name) == 1` assertion
    correct only on the very first run ever, against a given database,
    for that literal name -- exactly the bug a real Mac run exposed: 5 of
    this file's test functions failed under pytest specifically because
    `python3 tests/test_idempotency.py` had already run once immediately
    before it, against the SAME persistent database, using the SAME
    literal patient names.

    The fix is NOT a weaker assertion (never `<= 1`) and NOT a switch to
    an isolated test database (which would break from this project's own
    established, documented convention of testing against the real
    persistent database everywhere else) -- it is exactly the same
    isolation strategy this file already uses for every OTHER identifier
    that gets counted or looked up by exact value: _login_new_user's
    randomized username, and every test's randomly-generated
    Idempotency-Key. patient_name was the one field left as a fixed
    literal; making it unique-per-invocation, the same way, closes the
    gap without touching production code, without weakening what the
    assertion proves, and without diverging from project convention.
    See _contamination_repro.py-style reasoning in the A4 remediation
    report for the real-database proof that (a) idempotency itself was
    never at fault -- a genuine replay of the same key always returns the
    same prescription_id and never creates a second row -- and (b) two
    independent runs sharing a literal patient_name legitimately produce
    two real, correct, DIFFERENT prescriptions -- exactly what a real
    clinic seeing two different patients who happen to share a name would
    also produce. Counting by patient_name alone was never a valid proxy
    for "did idempotency work," only for "has this literal name been used
    before, ever, against this database."
    """
    return f"{label} {secrets.token_hex(6)}"


def _manual_preview_payload(patient_name="Idem Test Patient", herb="Ashwagandha", grams=5):
    return {
        "patient_name": patient_name,
        "dosha_imbalance": "Vata",
        "prescribed_herbs": [{"name": herb, "grams": grams}],
    }


# ---------------------------------------------------------------------------
# 1. Idempotency-Key is optional -- omitting it is unchanged pre-A4 behavior
# ---------------------------------------------------------------------------

def test_missing_idempotency_key_behaves_exactly_as_pre_a4():
    print("\nTest: omitting Idempotency-Key entirely -- no dedup row created, normal confirm succeeds")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers, _, _ = _login_new_user(client, clinic_id="idem_missing_key_clinic")
        _set_stock("Ashwagandha", 5000, clinic_id="idem_missing_key_clinic")

        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name="Idem Missing Key"), headers=headers)
        preview_id = preview_resp.json()["preview_id"]

        confirm_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check("confirm without any Idempotency-Key header succeeds normally", confirm_resp.status_code == 200, confirm_resp.text)

        # Structural proof that no dedup happened: main.py only reserves an
        # idempotency row when the header is truthy (see the `if
        # idempotency_key:` guard in prescribe_confirm) -- so re-confirming
        # the SAME preview a second time, again with no key, must hit A1's
        # ordinary "already confirmed" 409, never a silent idempotent replay.
        second_resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check(
            "a second confirm of the SAME preview with NO Idempotency-Key header gets the ordinary "
            "A1 'already confirmed' 409 -- NOT a silent idempotent replay, proving no dedup happened",
            second_resp.status_code == 409, second_resp.text,
        )


# ---------------------------------------------------------------------------
# 2. Reload / retry with the same key + same payload -> replay
# ---------------------------------------------------------------------------

def test_same_key_same_payload_replays_exact_original_response():
    print("\nTest: retry with the SAME key + SAME payload (reload / network-drop-after-commit) replays the exact original response")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers, _, _ = _login_new_user(client, clinic_id="idem_replay_clinic")
        _set_stock("Ashwagandha", 5000, clinic_id="idem_replay_clinic")

        patient_name = _unique_patient_name("Idem Replay Patient")
        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name=patient_name), headers=headers)
        preview_id = preview_resp.json()["preview_id"]

        key = secrets.token_hex(16)
        body = {"preview_id": preview_id}

        first = client.post("/prescribe/confirm", json=body, headers={**headers, "Idempotency-Key": key})
        check("first confirm with a fresh key succeeds", first.status_code == 200, first.text)

        second = client.post("/prescribe/confirm", json=body, headers={**headers, "Idempotency-Key": key})
        check("retry with the SAME key + SAME payload returns 200 (not 409, not a new resource)", second.status_code == 200, second.text)
        check(
            "the retried response body is byte-for-byte (as parsed JSON) identical to the original",
            second.json() == first.json(), (first.json(), second.json()),
        )

        check(
            "exactly ONE prescription exists for this patient, never two",
            _count_prescriptions_for_patient(patient_name) == 1,
        )
        check(
            "inventory reflects exactly ONE deduction, never a double one",
            _get_stock("Ashwagandha", clinic_id="idem_replay_clinic") == 5000 - 5,
        )
        check(
            "exactly ONE idempotency_keys row exists for this key",
            _count_idempotency_rows(key) == 1,
        )
        check(
            "exactly ZERO corrections rows exist for the resulting prescription -- this is a manual "
            "preview (no AI proposal to compare against), so production never logs a correction for it, "
            "and the idempotency replay did not create one either",
            _count_corrections_for_prescription(first.json()["prescription_id"]) == 0,
        )

        # A third retry, after the second, must ALSO still replay correctly --
        # idempotency persists beyond just the first retry.
        third = client.post("/prescribe/confirm", json=body, headers={**headers, "Idempotency-Key": key})
        check("a THIRD retry with the same key still replays correctly", third.status_code == 200 and third.json() == first.json(), third.text)


# ---------------------------------------------------------------------------
# 3. Duplicate click -- real concurrent HTTP requests, same key
# ---------------------------------------------------------------------------

def test_concurrent_confirms_with_the_same_key_result_in_exactly_one_commit():
    print("\nTest: duplicate click -- two genuinely concurrent HTTP requests, same key, same payload")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers, _, _ = _login_new_user(client, clinic_id="idem_concurrent_clinic")
        _set_stock("Ashwagandha", 5000, clinic_id="idem_concurrent_clinic")

        patient_name = _unique_patient_name("Idem Concurrent Patient")
        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name=patient_name, grams=7), headers=headers)
        preview_id = preview_resp.json()["preview_id"]

        key = secrets.token_hex(16)
        body = {"preview_id": preview_id}

        results = {}
        errors = {}
        barrier = threading.Barrier(2)

        def attempt(name: str):
            try:
                barrier.wait()
                results[name] = client.post("/prescribe/confirm", json=body, headers={**headers, "Idempotency-Key": key})
            except Exception as e:
                errors[name] = e

        t1 = threading.Thread(target=attempt, args=("t1",))
        t2 = threading.Thread(target=attempt, args=("t2",))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        check("both concurrent requests completed without raising", not errors, errors)
        check("both concurrent requests got HTTP 200", all(r.status_code == 200 for r in results.values()), {n: r.text for n, r in results.items()})
        check(
            "both concurrent requests' response bodies are identical -- one committed, one replayed",
            results["t1"].json() == results["t2"].json(), (results["t1"].json(), results["t2"].json()),
        )
        check(
            "exactly ONE prescription exists for this patient, despite two concurrent requests",
            _count_prescriptions_for_patient(patient_name) == 1,
        )
        check(
            "inventory reflects exactly ONE deduction, never a torn or double one",
            _get_stock("Ashwagandha", clinic_id="idem_concurrent_clinic") == 5000 - 7,
        )
        check(
            "exactly ZERO correction rows exist for the resulting prescription -- this is a manual "
            "preview (no AI proposal to compare against), so production never logs a correction for it, "
            "and concurrent idempotent handling did not create one either",
            _count_corrections_for_prescription(results["t1"].json()["prescription_id"]) == 0,
        )


# ---------------------------------------------------------------------------
# 4. Changed payload reusing the old key -> 409, original untouched
# ---------------------------------------------------------------------------

def test_changed_payload_with_reused_key_gets_409_and_original_is_untouched():
    print("\nTest: reusing the SAME key against a CHANGED payload gets 409 (Feature 1's backend backstop for a frontend bug)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers, _, _ = _login_new_user(client, clinic_id="idem_changed_payload_clinic")
        _set_stock("Ashwagandha", 5000, clinic_id="idem_changed_payload_clinic")
        _set_stock("Brahmi", 5000, clinic_id="idem_changed_payload_clinic")

        patient_name = _unique_patient_name("Idem Changed Payload Patient")
        patient_name_two = _unique_patient_name("Idem Changed Payload Patient Two")
        preview_resp = client.post(
            "/prescribe/manual-preview",
            json=_manual_preview_payload(patient_name=patient_name, herb="Ashwagandha", grams=5),
            headers=headers,
        )
        preview_id = preview_resp.json()["preview_id"]

        key = secrets.token_hex(16)
        original_body = {"preview_id": preview_id}
        first = client.post("/prescribe/confirm", json=original_body, headers={**headers, "Idempotency-Key": key})
        check("original confirm with the fresh key succeeds", first.status_code == 200, first.text)

        # Simulate a frontend bug: the SAME key reused against a DIFFERENT
        # preview_id (a materially different request) rather than a fresh key.
        second_preview_resp = client.post(
            "/prescribe/manual-preview",
            json=_manual_preview_payload(patient_name=patient_name_two, herb="Brahmi", grams=9),
            headers=headers,
        )
        second_preview_id = second_preview_resp.json()["preview_id"]
        changed_body = {"preview_id": second_preview_id}

        conflict_resp = client.post("/prescribe/confirm", json=changed_body, headers={**headers, "Idempotency-Key": key})
        check(
            "reusing the same key against a different preview_id gets 409, not a silent replay "
            "and not a silent second confirmation",
            conflict_resp.status_code == 409, conflict_resp.text,
        )
        check(
            "the conflicting attempt's response does NOT match the original successful response",
            conflict_resp.json() != first.json(),
        )

        check(
            "the SECOND preview was never actually confirmed -- Brahmi stock is untouched",
            _get_stock("Brahmi", clinic_id="idem_changed_payload_clinic") == 5000,
        )
        check(
            "only ONE prescription exists (the original) -- the conflicting attempt created nothing",
            _count_prescriptions_for_patient(patient_name) == 1
            and _count_prescriptions_for_patient(patient_name_two) == 0,
        )

        # The second preview is still PENDING and confirmable with its OWN fresh key.
        fresh_key = secrets.token_hex(16)
        retry_with_fresh_key = client.post("/prescribe/confirm", json=changed_body, headers={**headers, "Idempotency-Key": fresh_key})
        check(
            "the second preview, confirmed with a genuinely FRESH key, succeeds normally -- "
            "the earlier conflict did not poison the preview itself",
            retry_with_fresh_key.status_code == 200, retry_with_fresh_key.text,
        )


# ---------------------------------------------------------------------------
# 5. "Two tabs" -- two different keys confirming the same preview
# ---------------------------------------------------------------------------

def test_two_different_keys_same_preview_still_commits_exactly_once():
    print("\nTest: two tabs -- two DIFFERENT idempotency keys attempting to confirm the SAME preview")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers, _, _ = _login_new_user(client, clinic_id="idem_two_tabs_clinic")
        _set_stock("Ashwagandha", 5000, clinic_id="idem_two_tabs_clinic")

        patient_name = _unique_patient_name("Idem Two Tabs Patient")
        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name=patient_name), headers=headers)
        preview_id = preview_resp.json()["preview_id"]
        body = {"preview_id": preview_id}

        key_tab_1 = secrets.token_hex(16)
        key_tab_2 = secrets.token_hex(16)

        first = client.post("/prescribe/confirm", json=body, headers={**headers, "Idempotency-Key": key_tab_1})
        check("tab 1's confirm succeeds", first.status_code == 200, first.text)

        second = client.post("/prescribe/confirm", json=body, headers={**headers, "Idempotency-Key": key_tab_2})
        check(
            "tab 2's confirm (a DIFFERENT key, same preview, arriving after tab 1 already committed) "
            "gets A1's ordinary 409 'already confirmed' -- NOT an idempotent replay of tab 1's result, "
            "and NOT a second prescription",
            second.status_code == 409, second.text,
        )
        check(
            "exactly ONE prescription exists, despite two different keys both attempting to confirm",
            _count_prescriptions_for_patient(patient_name) == 1,
        )
        check(
            "each key has its own row (tab 2's reservation was rolled back along with its failed "
            "business logic, per the two-state model -- see tests/test_idempotency_store.py's "
            "NULL-invariant test)",
            _count_idempotency_rows(key_tab_1) == 1 and _count_idempotency_rows(key_tab_2) == 0,
        )


# ---------------------------------------------------------------------------
# 6. Cross-user identical key string never collides, at the HTTP layer
# ---------------------------------------------------------------------------

def test_cross_user_identical_key_string_never_collides_at_http_layer():
    print("\nTest: two DIFFERENT doctors using the IDENTICAL key string never collide")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers_a, _, _ = _login_new_user(client, clinic_id="idem_crossuser_clinic_a")
        headers_b, _, _ = _login_new_user(client, clinic_id="idem_crossuser_clinic_b")
        _set_stock("Ashwagandha", 5000, clinic_id="idem_crossuser_clinic_a")
        _set_stock("Ashwagandha", 5000, clinic_id="idem_crossuser_clinic_b")

        preview_a = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name="Idem CrossUser A"), headers=headers_a).json()["preview_id"]
        preview_b = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name="Idem CrossUser B"), headers=headers_b).json()["preview_id"]

        shared_key = secrets.token_hex(16)  # deliberately identical string for both doctors

        resp_a = client.post("/prescribe/confirm", json={"preview_id": preview_a}, headers={**headers_a, "Idempotency-Key": shared_key})
        resp_b = client.post("/prescribe/confirm", json={"preview_id": preview_b}, headers={**headers_b, "Idempotency-Key": shared_key})

        check("doctor A's confirm succeeds despite sharing the literal key string with doctor B", resp_a.status_code == 200, resp_a.text)
        check("doctor B's confirm ALSO succeeds, completely independently", resp_b.status_code == 200, resp_b.text)
        check("the two confirmations produced two DIFFERENT prescriptions", resp_a.json()["prescription_id"] != resp_b.json()["prescription_id"])


# ---------------------------------------------------------------------------
# 7. Session expiry mid-flow: re-login preserves the key's safety
# ---------------------------------------------------------------------------

def test_key_remains_safely_reusable_after_relogin_same_user():
    print("\nTest: after logout + re-login as the SAME user, the original idempotency key is still safely reusable")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers, username, password = _login_new_user(client, clinic_id="idem_relogin_clinic")
        _set_stock("Ashwagandha", 5000, clinic_id="idem_relogin_clinic")

        patient_name = _unique_patient_name("Idem Relogin Patient")
        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name=patient_name), headers=headers)
        preview_id = preview_resp.json()["preview_id"]

        key = secrets.token_hex(16)
        first = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers={**headers, "Idempotency-Key": key})
        check("original confirm succeeds", first.status_code == 200, first.text)

        # Simulate session expiry: log out, then log back in as the SAME
        # user -- a NEW session_id, but the SAME user_id.
        client.post("/auth/logout", headers=headers)
        relogin_resp = client.post("/auth/login", json={"username": username, "password": password})
        check("re-login as the same user succeeds", relogin_resp.status_code == 200, relogin_resp.text)
        new_headers = {"Authorization": f"Bearer {relogin_resp.json()['token']}"}

        retry = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers={**new_headers, "Idempotency-Key": key})
        check(
            "retrying with the SAME key under the NEW session (same user_id) still correctly replays "
            "the original result -- A4 is scoped by user_id, not session_id, exactly for this case",
            retry.status_code == 200 and retry.json() == first.json(), retry.text,
        )
        check(
            "still exactly ONE prescription -- the re-login did not cause a second confirmation",
            _count_prescriptions_for_patient(patient_name) == 1,
        )


# ---------------------------------------------------------------------------
# 8. Response shape is unchanged from pre-A4 for a fresh confirm
# ---------------------------------------------------------------------------

def test_fresh_confirm_response_shape_is_unchanged_from_pre_a4():
    print("\nTest: a fresh confirm's response shape is completely unchanged from before A4 (no new required field)")
    import backend.main as main_module
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        headers, _, _ = _login_new_user(client, clinic_id="idem_shape_clinic")
        _set_stock("Ashwagandha", 5000, clinic_id="idem_shape_clinic")

        preview_resp = client.post("/prescribe/manual-preview", json=_manual_preview_payload(patient_name="Idem Shape Patient"), headers=headers)
        preview_id = preview_resp.json()["preview_id"]

        # No Idempotency-Key header at all.
        resp = client.post("/prescribe/confirm", json={"preview_id": preview_id}, headers=headers)
        check("confirm succeeds", resp.status_code == 200, resp.text)
        body = resp.json()
        check(
            "response has exactly the locked PrescriptionResponse fields, nothing added or removed",
            set(body.keys()) == {"patient_name", "dosha_imbalance", "prescribed_herbs", "prescription_id", "created_at"},
            body.keys(),
        )


_ALL_TESTS = [
    test_missing_idempotency_key_behaves_exactly_as_pre_a4,
    test_same_key_same_payload_replays_exact_original_response,
    test_concurrent_confirms_with_the_same_key_result_in_exactly_one_commit,
    test_changed_payload_with_reused_key_gets_409_and_original_is_untouched,
    test_two_different_keys_same_preview_still_commits_exactly_once,
    test_cross_user_identical_key_string_never_collides_at_http_layer,
    test_key_remains_safely_reusable_after_relogin_same_user,
    test_fresh_confirm_response_shape_is_unchanged_from_pre_a4,
]


if __name__ == "__main__":
    _function_failures = []
    for _test_fn in _ALL_TESTS:
        try:
            _test_fn()
        except AssertionError as e:
            _function_failures.append(_test_fn.__name__)
            print(f"=> TEST FUNCTION FAILED: {_test_fn.__name__} ({e})")

    print(f"\n--- {_passed} passed, {_failed} failed ---")
    if _function_failures:
        print(f"--- {len(_function_failures)} test function(s) had a failing assertion: {', '.join(_function_failures)} ---")
    if _failed:
        sys.exit(1)
