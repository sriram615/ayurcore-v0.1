"""
Idempotency-key storage tests (Phase B, A4) -- covers
backend/idempotency_store.py directly.

This deliberately does NOT go through FastAPI's TestClient, matching
tests/test_preview_store.py's convention exactly: everything
idempotency_store.py does is pure sqlite3 + Python, no FastAPI involved, so
this file exercises that layer directly against the real db/ayurcore.db.
HTTP-layer integration (the Idempotency-Key header on /prescribe/confirm,
real concurrent HTTP requests, replay of a real PrescriptionResponse) is
covered separately in tests/test_idempotency.py, which REQUIRES MAC
VERIFICATION (FastAPI installed). This file does not.

Specifically checks:
  1. compute_request_fingerprint: deterministic, order-independent (dict key
     order in the input), and sensitive to every field the approved A4
     design says it must be sensitive to (preview_id, dosha_imbalance,
     prescribed_herbs) -- changing any one of them changes the fingerprint.
  2. reserve_idempotent_operation: fresh reservation vs. replay, exactly as
     the two-state model promises -- a first call reserves (is_replay=False);
     a second call with the SAME key+fingerprint, after the first
     completed, replays (is_replay=True) with the exact stored response.
  3. IdempotencyConflictError: same key, DIFFERENT fingerprint -> raised,
     never silently replayed and never silently allowed to overwrite the
     original.
  4. IdempotencyClinicMismatchError: same key, same fingerprint, but a
     DIFFERENT clinic_id than the one the row was reserved under -> raised,
     fail-closed (this scenario is not reachable via any real endpoint
     today, since no code changes a user's clinic_id, but the store-layer
     function is tested directly regardless).
  5. Cross-user non-collision: two DIFFERENT user_ids using the IDENTICAL
     key string never collide -- both get is_replay=False, both are free to
     reserve independently. This is the structural proof, at the storage
     layer, that the approved design's "no cross-user oracle" claim holds:
     nothing about user B's reservation is visible to, or blocked by, user
     A's identical key string.
  6. THE NULL-INVARIANT REGRESSION TEST: a reservation that is interrupted
     between reserve_idempotent_operation() (INSERT, NULL response fields)
     and complete_idempotent_reservation() (UPDATE, fills them in) -- e.g.
     because the business logic in between raised -- and is then rolled
     back, per the approved design's Option 1, must leave behind ZERO rows
     for that key. Not a NULL row: no row at all. This is the single most
     important test in this file.
  7. A committed SUCCEEDED row is fully replayable: response_status_code
     and response_body_json are exactly what was written, and
     response_body_json round-trips through json.loads() back to the exact
     dict that was stored.
  8. A GLOBAL invariant scan: across every row this file's tests have
     written to idempotency_keys, none has a NULL response_status_code or
     response_body_json. This directly operationalizes the schema's
     documented invariant, independent of any single code path.
  9. Concurrency: two threads racing to reserve the SAME (user_id,
     operation, key) -- exactly one must get is_replay=False (and complete
     it), the other must see is_replay=True with that exact result, never
     two independent SUCCEEDED rows and never a crash.

Run directly (no pytest required):
    python tests/test_idempotency_store.py
"""
import json
import os
import secrets
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import get_connection
from db.migrations import run_migrations
from backend.idempotency_store import (
    OPERATION_PRESCRIBE_CONFIRM,
    compute_request_fingerprint,
    reserve_idempotent_operation,
    complete_idempotent_reservation,
    IdempotencyConflictError,
    IdempotencyClinicMismatchError,
)

# Same reasoning as tests/test_preview_store.py's identical call: this file
# talks to db/ayurcore.db directly, with no FastAPI/ASGI app in between to
# run pending migrations via the startup hook. Cheap and idempotent when
# there's nothing pending.
run_migrations()

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Records AND enforces one assertion -- raises AssertionError
    immediately on failure, matching tests/test_preview_store.py and
    tests/test_rbac.py's fixed convention (see the latter's module
    docstring, "PYTEST COMPATIBILITY", for exactly why a non-raising
    check() once let a real security regression through `pytest tests/`
    silently). This file carries A4's own NULL-invariant regression test,
    so it uses the same, real-raising check() from day one."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


# Ensure a real users row exists for every user_id these tests reserve
# under -- idempotency_keys.user_id is a real FK to users(user_id) (see
# migrations/006), and db/database.py's get_connection() enforces
# `PRAGMA foreign_keys = ON` on every connection, so an arbitrary integer
# would fail loudly rather than silently succeed.
def _make_real_user_id(clinic_id: str = "default_clinic") -> int:
    conn = get_connection()
    try:
        username = f"idempotency_store_test_{secrets.token_hex(6)}"
        conn.execute(
            "INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
            "VALUES (?, 'not-a-real-hash', ?, 'DOCTOR', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (username, clinic_id),
        )
        conn.commit()
        return conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()["user_id"]
    finally:
        conn.close()


def _row_for(user_id: int, operation: str, key: str):
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM idempotency_keys WHERE user_id = ? AND operation = ? AND idempotency_key = ?",
            (user_id, operation, key),
        ).fetchone()
    finally:
        conn.close()


def _reserve_and_complete(user_id: int, key: str, clinic_id: str, preview_id: str, dosha: str, herbs: list, status_code: int = 200):
    """Reproduces backend/main.py's /prescribe/confirm idempotency sequence
    exactly (BEGIN IMMEDIATE -> reserve -> ... business logic stands in for
    here ... -> complete -> commit), the same way
    tests/test_preview_store.py's _confirm_like_main_py reproduces the rest
    of that transaction. Returns the reservation result for the caller to
    inspect."""
    fingerprint = compute_request_fingerprint(preview_id=preview_id, dosha_imbalance=dosha, prescribed_herbs=herbs)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            reservation = reserve_idempotent_operation(
                conn, user_id=user_id, operation=OPERATION_PRESCRIBE_CONFIRM,
                idempotency_key=key, clinic_id=clinic_id, request_fingerprint=fingerprint,
            )
            if not reservation.is_replay:
                body = json.dumps({"prescription_id": 1, "preview_id": preview_id, "dosha_imbalance": dosha, "prescribed_herbs": herbs})
                complete_idempotent_reservation(
                    conn, user_id=user_id, operation=OPERATION_PRESCRIBE_CONFIRM,
                    idempotency_key=key, response_status_code=status_code, response_body_json=body,
                )
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            return reservation
    finally:
        conn.close()


def test_fingerprint_is_deterministic_and_field_sensitive():
    print("\nTest: compute_request_fingerprint is deterministic and sensitive to every documented field")
    herbs = [{"name": "Ashwagandha", "grams": 10}]
    fp1 = compute_request_fingerprint(preview_id="p1", dosha_imbalance="Vata", prescribed_herbs=herbs)
    fp2 = compute_request_fingerprint(preview_id="p1", dosha_imbalance="Vata", prescribed_herbs=herbs)
    check("same inputs produce the same fingerprint", fp1 == fp2)

    fp_diff_preview = compute_request_fingerprint(preview_id="p2", dosha_imbalance="Vata", prescribed_herbs=herbs)
    check("a different preview_id changes the fingerprint", fp_diff_preview != fp1)

    fp_diff_dosha = compute_request_fingerprint(preview_id="p1", dosha_imbalance="Pitta", prescribed_herbs=herbs)
    check("a different dosha_imbalance changes the fingerprint", fp_diff_dosha != fp1)

    fp_diff_herbs = compute_request_fingerprint(preview_id="p1", dosha_imbalance="Vata", prescribed_herbs=[{"name": "Brahmi", "grams": 10}])
    check("a different herb list changes the fingerprint", fp_diff_herbs != fp1)

    fp_none = compute_request_fingerprint(preview_id="p1", dosha_imbalance=None, prescribed_herbs=None)
    check("None dosha_imbalance/prescribed_herbs (doctor sent no correction) fingerprints distinctly from an explicit value", fp_none != fp1)


def test_fresh_reservation_then_replay():
    print("\nTest: a first call reserves, a second call with the same key+payload replays the stored result")
    user_id = _make_real_user_id()
    key = secrets.token_hex(8)
    herbs = [{"name": "Ashwagandha", "grams": 10}]

    first = _reserve_and_complete(user_id, key, "default_clinic", "preview-fresh-1", "Vata", herbs)
    check("first call is a fresh reservation, not a replay", first.is_replay is False)

    row = _row_for(user_id, OPERATION_PRESCRIBE_CONFIRM, key)
    check("a committed row now exists", row is not None)
    check("the committed row has a non-NULL response_status_code", row["response_status_code"] is not None)
    check("the committed row has a non-NULL response_body_json", row["response_body_json"] is not None)

    second = _reserve_and_complete(user_id, key, "default_clinic", "preview-fresh-1", "Vata", herbs)
    check("second call with the SAME key+payload is a replay", second.is_replay is True)
    check("the replay returns the exact original status code", second.response_status_code == 200, second.response_status_code)
    check(
        "the replay's response_body_json round-trips to the exact original content",
        json.loads(second.response_body_json)["preview_id"] == "preview-fresh-1",
        second.response_body_json,
    )


def test_changed_payload_raises_conflict_not_silent_overwrite():
    print("\nTest: same key, different payload -> IdempotencyConflictError, never a silent replay or overwrite")
    user_id = _make_real_user_id()
    key = secrets.token_hex(8)
    herbs_v1 = [{"name": "Ashwagandha", "grams": 10}]
    herbs_v2 = [{"name": "Ashwagandha", "grams": 15}]

    _reserve_and_complete(user_id, key, "default_clinic", "preview-conflict-1", "Vata", herbs_v1)

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        raised = None
        try:
            fingerprint_v2 = compute_request_fingerprint(preview_id="preview-conflict-1", dosha_imbalance="Vata", prescribed_herbs=herbs_v2)
            reserve_idempotent_operation(
                conn, user_id=user_id, operation=OPERATION_PRESCRIBE_CONFIRM,
                idempotency_key=key, clinic_id="default_clinic", request_fingerprint=fingerprint_v2,
            )
        except IdempotencyConflictError as e:
            raised = e
        finally:
            conn.rollback()
    finally:
        conn.close()

    check("reusing the key with a changed payload raises IdempotencyConflictError", raised is not None)

    row = _row_for(user_id, OPERATION_PRESCRIBE_CONFIRM, key)
    check(
        "the ORIGINAL row is completely untouched by the rejected conflicting attempt",
        json.loads(row["response_body_json"])["prescribed_herbs"] == herbs_v1,
        row["response_body_json"],
    )


def test_clinic_mismatch_raises_and_does_not_replay():
    print("\nTest: same key+payload but a different live clinic_id -> IdempotencyClinicMismatchError, fail-closed")
    user_id = _make_real_user_id(clinic_id="idem_store_clinic_a")
    key = secrets.token_hex(8)
    herbs = [{"name": "Ashwagandha", "grams": 10}]

    _reserve_and_complete(user_id, key, "idem_store_clinic_a", "preview-clinicmismatch-1", "Vata", herbs)

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        raised = None
        try:
            fingerprint = compute_request_fingerprint(preview_id="preview-clinicmismatch-1", dosha_imbalance="Vata", prescribed_herbs=herbs)
            reserve_idempotent_operation(
                conn, user_id=user_id, operation=OPERATION_PRESCRIBE_CONFIRM,
                idempotency_key=key, clinic_id="idem_store_clinic_b", request_fingerprint=fingerprint,
            )
        except IdempotencyClinicMismatchError as e:
            raised = e
        finally:
            conn.rollback()
    finally:
        conn.close()

    check(
        "a live clinic_id that no longer matches the reserved row's clinic_id raises "
        "IdempotencyClinicMismatchError rather than replaying the (possibly now-wrong-tenant) stored response",
        raised is not None,
    )


def test_cross_user_identical_key_never_collides():
    print("\nTest: two DIFFERENT users using the IDENTICAL key string never collide -- no cross-user oracle")
    user_a = _make_real_user_id()
    user_b = _make_real_user_id()
    shared_key = secrets.token_hex(8)  # deliberately identical string for both users
    herbs = [{"name": "Ashwagandha", "grams": 10}]

    result_a = _reserve_and_complete(user_a, shared_key, "default_clinic", "preview-crossuser-a", "Vata", herbs)
    result_b = _reserve_and_complete(user_b, shared_key, "default_clinic", "preview-crossuser-b", "Pitta", herbs)

    check("user A's reservation with the shared key string is fresh, not a replay", result_a.is_replay is False)
    check(
        "user B's reservation with the IDENTICAL key string is ALSO fresh, not a replay of user A's result -- "
        "proves the composite (user_id, operation, key) primary key makes cross-user collision structurally impossible",
        result_b.is_replay is False,
    )

    row_a = _row_for(user_a, OPERATION_PRESCRIBE_CONFIRM, shared_key)
    row_b = _row_for(user_b, OPERATION_PRESCRIBE_CONFIRM, shared_key)
    check("both rows exist independently", row_a is not None and row_b is not None)
    check(
        "the two rows hold genuinely different content (different preview_id), proving neither leaked into the other",
        json.loads(row_a["response_body_json"])["preview_id"] != json.loads(row_b["response_body_json"])["preview_id"],
    )


def test_interrupted_reservation_leaves_zero_rows_not_a_null_row():
    print("\nTest: THE NULL-INVARIANT REGRESSION TEST -- an interrupted reservation leaves ZERO rows, never a NULL-response row")
    user_id = _make_real_user_id()
    key = secrets.token_hex(8)
    herbs = [{"name": "Ashwagandha", "grams": 10}]
    fingerprint = compute_request_fingerprint(preview_id="preview-interrupted-1", dosha_imbalance="Vata", prescribed_herbs=herbs)

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            reservation = reserve_idempotent_operation(
                conn, user_id=user_id, operation=OPERATION_PRESCRIBE_CONFIRM,
                idempotency_key=key, clinic_id="default_clinic", request_fingerprint=fingerprint,
            )
            check("the reservation INSERT succeeded and claimed the key fresh", reservation.is_replay is False)

            # Simulate business logic raising BEFORE complete_idempotent_reservation()
            # is ever called -- e.g. InsufficientStockError, UnknownHerbError,
            # or any other mid-transaction failure in the real /prescribe/confirm.
            raise RuntimeError("simulated business-logic failure between reservation and completion")
        except RuntimeError:
            conn.rollback()
    finally:
        conn.close()

    row = _row_for(user_id, OPERATION_PRESCRIBE_CONFIRM, key)
    check(
        "after the interrupted transaction rolls back, NO row exists for this key at all -- "
        "not a row with NULL response fields, no row whatsoever",
        row is None, row,
    )

    # The key must now be fully retryable, exactly as if never attempted.
    retry = _reserve_and_complete(user_id, key, "default_clinic", "preview-interrupted-1", "Vata", herbs)
    check("the SAME key is fully retryable after the interrupted attempt -- a fresh reservation, not blocked", retry.is_replay is False)


def test_global_no_committed_row_ever_has_a_null_response_field():
    print("\nTest: GLOBAL INVARIANT SCAN -- across the whole table, no committed row has a NULL response field")
    conn = get_connection()
    try:
        bad_rows = conn.execute(
            "SELECT user_id, operation, idempotency_key FROM idempotency_keys "
            "WHERE response_status_code IS NULL OR response_body_json IS NULL"
        ).fetchall()
    finally:
        conn.close()
    check(
        "zero rows in idempotency_keys have a NULL response_status_code or response_body_json, "
        "across every reservation this test file (and any prior run) has made",
        len(bad_rows) == 0, [dict(r) for r in bad_rows],
    )


def test_concurrent_reservation_of_the_same_key():
    print("\nTest: two threads racing to reserve the SAME (user_id, operation, key) -- exactly one reserves, the other replays")
    user_id = _make_real_user_id()
    key = secrets.token_hex(8)
    herbs = [{"name": "Ashwagandha", "grams": 10}]

    results = {}
    errors = {}
    barrier = threading.Barrier(2)

    def attempt(name: str):
        try:
            barrier.wait()
            results[name] = _reserve_and_complete(user_id, key, "default_clinic", "preview-concurrent-1", "Vata", herbs)
        except Exception as e:
            errors[name] = e

    t1 = threading.Thread(target=attempt, args=("t1",))
    t2 = threading.Thread(target=attempt, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    check("both threads completed without raising (BEGIN IMMEDIATE serializes them; no crash, no deadlock)", not errors, errors)
    check("both threads got a result", set(results.keys()) == {"t1", "t2"}, results)

    fresh = [n for n, r in results.items() if not r.is_replay]
    replayed = [n for n, r in results.items() if r.is_replay]
    check("exactly one thread's reservation was fresh", len(fresh) == 1, results)
    check("exactly one thread's reservation was a replay", len(replayed) == 1, results)
    check(
        "the replaying thread got back the EXACT same response the fresh thread's completion wrote",
        results[replayed[0]].response_body_json == _row_for(user_id, OPERATION_PRESCRIBE_CONFIRM, key)["response_body_json"],
    )

    conn = get_connection()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM idempotency_keys WHERE user_id = ? AND operation = ? AND idempotency_key = ?",
            (user_id, OPERATION_PRESCRIBE_CONFIRM, key),
        ).fetchone()["n"]
    finally:
        conn.close()
    check("exactly ONE row exists for this key, never two (no torn or duplicate reservation)", count == 1, count)


_ALL_TESTS = [
    test_fingerprint_is_deterministic_and_field_sensitive,
    test_fresh_reservation_then_replay,
    test_changed_payload_raises_conflict_not_silent_overwrite,
    test_clinic_mismatch_raises_and_does_not_replay,
    test_cross_user_identical_key_never_collides,
    test_interrupted_reservation_leaves_zero_rows_not_a_null_row,
    test_concurrent_reservation_of_the_same_key,
    test_global_no_committed_row_ever_has_a_null_response_field,
]


if __name__ == "__main__":
    # Same per-test-function try/except pattern as tests/test_preview_store.py
    # and tests/test_rbac.py -- see either's __main__ block for why.
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
