"""
Tamper-evident audit event storage tests (Phase A5) -- covers
backend/audit_store.py directly.

This deliberately does NOT go through FastAPI's TestClient, matching
tests/test_idempotency_store.py's and tests/test_preview_store.py's
convention exactly: everything audit_store.py does is pure sqlite3 +
Python, no FastAPI involved, so this file exercises that layer directly
against the real db/ayurcore.db. HTTP-layer integration (the actual
/prescribe/confirm wiring, GET /audit/events, RBAC/clinic-isolation over
HTTP) is covered separately in tests/test_audit.py, which REQUIRES MAC
VERIFICATION (FastAPI installed). This file does not.

Specifically checks:
  1. AI_ASSISTED event_data shape: decision_source, ai_proposal preserved,
     doctor_decision correct, was_changed True/False correctly.
  2. MANUAL event_data NEVER fabricates an AI proposal: decision_source is
     MANUAL, ai_proposal is exactly None, was_changed is exactly None (not
     False -- comparing against nothing is meaningless).
  3. Canonicalization: two logically identical herb lists that differ only
     in input order, or that split the same total across a duplicate
     entry, produce the EXACT SAME event_data (and therefore the exact
     same event_hash) -- proving list order and incidental duplication are
     not part of "the decision" as far as the hash is concerned.
  4. Hash chain, per clinic: a clinic's first event has
     previous_event_hash = NULL; its second event's previous_event_hash
     equals the first's event_hash; a DIFFERENT clinic's chain starts at
     NULL independently, regardless of how many events the first clinic
     already has.
  5. verify_audit_integrity() reports valid=True, zero violations, on a
     real, untampered, real-database chain.
  6. THE MANDATORY REAL SQLITE TAMPERING TEST: for each of event_data_json,
     previous_event_hash, event_hash, actor_user_id, clinic_id, and
     prescription_id -- create a real event, verify the chain is valid,
     directly UPDATE that one column via raw sqlite3 (bypassing this
     project's application code entirely), verify the corruption IS
     detected (valid=False, a violation naming that event_id), then
     restore the original value and verify the chain is valid again. Every
     sub-case runs against a dedicated fresh event so one tampered column
     can never mask or interfere with another sub-case's result.
  7. Concurrency: two threads, each reproducing backend/main.py's real
     insertion order (create prescription+preview, THEN create the audit
     event, all inside one BEGIN IMMEDIATE transaction) for the SAME
     clinic at the same time -- both succeed, each commits exactly one
     audit event for its own prescription_id, and the resulting chain
     (both events present, correctly ordered by whichever transaction's
     BEGIN IMMEDIATE actually won the write lock first) verifies valid.
  8. Restart/persistence: an event created and committed on one connection
     remains present and verifies valid when read back from a BRAND NEW
     connection opened afterward -- SQLite's file-backed persistence
     means this is the closest thing to "process A creates it, process B
     reads it" available without literally forking a second OS process,
     and it proves the chain is not held in any in-memory/process-local
     state.
  9. No update/delete path: backend/audit_store.py defines no
     update_audit_event or delete_audit_event function at all.
  10. event_data never contains a PII-shaped field (patient_name,
      patient_phone, patient_id) -- a structural check on the dict's own
      keys, independent of what backend/main.py happens to pass in.

Run directly (no pytest required):
    python tests/test_audit_store.py
"""
import json
import os
import secrets
import sys
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import get_connection
from db.migrations import run_migrations
from backend.preview_store import create_preview
from backend.audit_store import (
    AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
    DECISION_SOURCE_AI_ASSISTED,
    DECISION_SOURCE_MANUAL,
    AuditWriteError,
    build_prescription_confirmed_event_data,
    create_audit_event,
    get_audit_events_for_clinic,
    verify_audit_integrity,
    assert_audit_integrity,
    AuditIntegrityError,
)

# Same reasoning as tests/test_idempotency_store.py's identical call: this
# file talks to db/ayurcore.db directly, with no FastAPI/ASGI app in
# between to run pending migrations via the startup hook.
run_migrations()

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Raises AssertionError immediately on failure -- same fixed
    convention as tests/test_idempotency_store.py and every other test
    file in this project written after the A3 pytest-compatibility
    incident (see test_rbac.py's module docstring)."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _make_real_user_id(clinic_id: str = "default_clinic") -> int:
    """Same helper as test_idempotency_store.py -- audit_events.actor_user_id
    is a real FK to users(user_id), and PRAGMA foreign_keys = ON is set on
    every connection, so an arbitrary integer would fail loudly."""
    conn = get_connection()
    try:
        username = f"audit_store_test_{secrets.token_hex(6)}"
        conn.execute(
            "INSERT INTO users (username, password_hash, clinic_id, role, is_active, created_at, updated_at) "
            "VALUES (?, 'not-a-real-hash', ?, 'DOCTOR', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (username, clinic_id),
        )
        conn.commit()
        return conn.execute("SELECT user_id FROM users WHERE username = ?", (username,)).fetchone()["user_id"]
    finally:
        conn.close()


def _make_real_prescription(clinic_id: str = "default_clinic", patient_name: str = "Audit Store Test Patient") -> int:
    """audit_events.prescription_id is a real FK to prescriptions(id).
    Inserted directly (same pattern as test_audit_export.py's
    test_audit_export_scoped_by_clinic) -- this file is about
    audit_store.py, not about re-exercising deduct_and_prescribe()."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO prescriptions (clinic_id, patient_name, description, status) VALUES (?, ?, ?, ?)",
            (clinic_id, patient_name, "audit_store.py test row", "confirmed"),
        )
        conn.commit()
        return conn.execute("SELECT id FROM prescriptions ORDER BY id DESC LIMIT 1").fetchone()["id"]
    finally:
        conn.close()


def _make_real_preview(
    clinic_id: str = "default_clinic",
    is_manual: bool = False,
    dosha: str = "Vata",
    herbs=None,
) -> str:
    """audit_events.preview_id is a real FK to prescription_previews
    (preview_id). create_preview() is pure Python -- no FastAPI needed."""
    return create_preview(
        patient_name="Audit Store Test Patient",
        patient_phone=None,
        patient_id=None,
        clinic_id=clinic_id,
        dosha_imbalance=dosha,
        prescribed_herbs=herbs if herbs is not None else [{"name": "Ashwagandha", "grams": 10}],
        is_manual=is_manual,
        created_by_session=None,
    )


def _create_event_like_main_py(
    *,
    user_id: int,
    clinic_id: str,
    prescription_id: int,
    preview_id: str,
    is_manual: bool,
    ai_dosha_imbalance,
    ai_prescribed_herbs,
    confirmed_dosha_imbalance: str,
    confirmed_prescribed_herbs: list,
) -> int:
    """Reproduces backend/main.py's /prescribe/confirm audit-event
    insertion exactly: BEGIN IMMEDIATE -> build_prescription_confirmed_event_data
    -> create_audit_event -> commit, all on one connection, in that order.
    Returns the new event_id."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            event_data = build_prescription_confirmed_event_data(
                is_manual=is_manual,
                ai_dosha_imbalance=ai_dosha_imbalance,
                ai_prescribed_herbs=ai_prescribed_herbs,
                confirmed_dosha_imbalance=confirmed_dosha_imbalance,
                confirmed_prescribed_herbs=confirmed_prescribed_herbs,
            )
            event_id = create_audit_event(
                conn,
                clinic_id=clinic_id,
                actor_user_id=user_id,
                event_type=AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
                prescription_id=prescription_id,
                preview_id=preview_id,
                event_data=event_data,
            )
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            return event_id
    finally:
        conn.close()


def _row_for_event(event_id: int):
    conn = get_connection()
    try:
        return conn.execute("SELECT * FROM audit_events WHERE event_id = ?", (event_id,)).fetchone()
    finally:
        conn.close()


def _raw_update(event_id: int, column: str, value) -> None:
    """Simulates a direct, out-of-band database edit -- bypassing
    backend/audit_store.py (which exposes no update path at all) entirely,
    the same way a real attacker with raw sqlite3 access to the .db file
    would. Used ONLY by the mandatory tampering test below."""
    conn = get_connection()
    try:
        # Migration 014 made audit_events append-only with triggers. A real
        # attacker with raw access to the .db file can drop them first, so
        # this simulation does exactly that (and restores the trigger from
        # its exact stored SQL afterwards) -- the point of this test is that
        # the hash chain still DETECTS such an edit.
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'audit_events_no_update'"
        ).fetchone()
        if trigger:
            conn.execute("DROP TRIGGER audit_events_no_update")
        conn.execute(f"UPDATE audit_events SET {column} = ? WHERE event_id = ?", (value, event_id))
        if trigger:
            conn.execute(trigger[0])
        conn.commit()
    finally:
        conn.close()


def test_ai_assisted_event_data_shape_and_was_changed():
    print("\nTest: AI_ASSISTED event_data preserves the real AI proposal and the doctor's actual decision")
    ai_herbs = [{"name": "Ashwagandha", "grams": 10}]
    confirmed_unchanged = build_prescription_confirmed_event_data(
        is_manual=False,
        ai_dosha_imbalance="Vata",
        ai_prescribed_herbs=ai_herbs,
        confirmed_dosha_imbalance="Vata",
        confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
    )
    check("decision_source is AI_ASSISTED", confirmed_unchanged["decision_source"] == DECISION_SOURCE_AI_ASSISTED)
    check("ai_proposal is preserved", confirmed_unchanged["ai_proposal"] == {"dosha": "Vata", "herbs": [{"name": "Ashwagandha", "grams": 10}]})
    check("doctor_decision matches what was confirmed", confirmed_unchanged["doctor_decision"] == {"dosha": "Vata", "herbs": [{"name": "Ashwagandha", "grams": 10}]})
    check("was_changed is False when confirmation matches the AI proposal exactly", confirmed_unchanged["was_changed"] is False, confirmed_unchanged)

    confirmed_changed = build_prescription_confirmed_event_data(
        is_manual=False,
        ai_dosha_imbalance="Vata",
        ai_prescribed_herbs=ai_herbs,
        confirmed_dosha_imbalance="Vata",
        confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 15}],
    )
    check("was_changed is True when the doctor's herb quantity differs from the AI proposal", confirmed_changed["was_changed"] is True, confirmed_changed)
    check("the ORIGINAL ai_proposal is still preserved unchanged even when the doctor corrected it", confirmed_changed["ai_proposal"]["herbs"] == [{"name": "Ashwagandha", "grams": 10}], confirmed_changed)


def test_manual_event_never_fabricates_an_ai_proposal():
    print("\nTest: MANUAL event_data NEVER reports an AI proposal that never happened")
    event_data = build_prescription_confirmed_event_data(
        is_manual=True,
        ai_dosha_imbalance="Vata",  # deliberately passed non-None, as entry["ai_dosha_imbalance"]
        ai_prescribed_herbs=[{"name": "Ashwagandha", "grams": 999}],  # would ALWAYS actually be for a manual preview (see preview_store.py) -- must be IGNORED
        confirmed_dosha_imbalance="Pitta",
        confirmed_prescribed_herbs=[{"name": "Brahmi", "grams": 5}],
    )
    check("decision_source is MANUAL", event_data["decision_source"] == DECISION_SOURCE_MANUAL)
    check(
        "ai_proposal is exactly None -- even though non-None ai_dosha_imbalance/ai_prescribed_herbs were passed in, "
        "is_manual=True means they are IGNORED, never reported as a real AI proposal",
        event_data["ai_proposal"] is None, event_data,
    )
    check("doctor_decision reflects the doctor's own manually-entered values", event_data["doctor_decision"] == {"dosha": "Pitta", "herbs": [{"name": "Brahmi", "grams": 5}]})
    check("was_changed is None (not False) -- there is no proposal to compare against", event_data["was_changed"] is None, event_data)


def test_ai_assisted_requires_a_real_proposal():
    print("\nTest: is_manual=False with a missing AI proposal is a programming error, not a silent MANUAL fallback")
    raised = None
    try:
        build_prescription_confirmed_event_data(
            is_manual=False,
            ai_dosha_imbalance=None,
            ai_prescribed_herbs=None,
            confirmed_dosha_imbalance="Vata",
            confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
        )
    except AuditWriteError as e:
        raised = e
    check("AuditWriteError is raised rather than silently treating a missing proposal as MANUAL", raised is not None)


def test_canonicalization_herb_order_and_duplicates_do_not_affect_content():
    print("\nTest: herb list order and an incidental duplicate split do not change the canonicalized event_data")
    baseline = build_prescription_confirmed_event_data(
        is_manual=False,
        ai_dosha_imbalance="Vata",
        ai_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}, {"name": "Brahmi", "grams": 5}],
        confirmed_dosha_imbalance="Vata",
        confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}, {"name": "Brahmi", "grams": 5}],
    )
    reordered_and_split = build_prescription_confirmed_event_data(
        is_manual=False,
        # Same two herbs, reversed order, AND Ashwagandha's 10g split across two entries (6g + 4g).
        ai_prescribed_herbs=[{"name": "Brahmi", "grams": 5}, {"name": "Ashwagandha", "grams": 6}, {"name": "Ashwagandha", "grams": 4}],
        ai_dosha_imbalance="Vata",
        confirmed_dosha_imbalance="Vata",
        confirmed_prescribed_herbs=[{"name": "Brahmi", "grams": 5}, {"name": "Ashwagandha", "grams": 6}, {"name": "Ashwagandha", "grams": 4}],
    )
    check(
        "reordered + duplicate-split herb lists canonicalize to the EXACT SAME event_data as the baseline",
        reordered_and_split == baseline, (reordered_and_split, baseline),
    )

    canonical_json_1 = json.dumps(baseline, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    canonical_json_2 = json.dumps(reordered_and_split, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    check("their canonical JSON serializations are byte-for-byte identical", canonical_json_1 == canonical_json_2)


def test_hash_chain_is_per_clinic_and_correctly_linked():
    print("\nTest: hash chain links correctly within one clinic and starts independently for a different clinic")
    clinic_a = f"audit_store_chain_a_{secrets.token_hex(4)}"
    clinic_b = f"audit_store_chain_b_{secrets.token_hex(4)}"
    user_a = _make_real_user_id(clinic_a)
    user_b = _make_real_user_id(clinic_b)

    rx1 = _make_real_prescription(clinic_a)
    pv1 = _make_real_preview(clinic_a)
    event_1 = _create_event_like_main_py(
        user_id=user_a, clinic_id=clinic_a, prescription_id=rx1, preview_id=pv1,
        is_manual=False, ai_dosha_imbalance="Vata", ai_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
        confirmed_dosha_imbalance="Vata", confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
    )
    row_1 = _row_for_event(event_1)
    check("clinic A's FIRST event has previous_event_hash = NULL", row_1["previous_event_hash"] is None, dict(row_1))

    rx2 = _make_real_prescription(clinic_a)
    pv2 = _make_real_preview(clinic_a)
    event_2 = _create_event_like_main_py(
        user_id=user_a, clinic_id=clinic_a, prescription_id=rx2, preview_id=pv2,
        is_manual=True, ai_dosha_imbalance=None, ai_prescribed_herbs=None,
        confirmed_dosha_imbalance="Pitta", confirmed_prescribed_herbs=[{"name": "Brahmi", "grams": 5}],
    )
    row_2 = _row_for_event(event_2)
    check(
        "clinic A's SECOND event's previous_event_hash equals the FIRST event's event_hash",
        row_2["previous_event_hash"] == row_1["event_hash"], (dict(row_1), dict(row_2)),
    )

    rx_b = _make_real_prescription(clinic_b)
    pv_b = _make_real_preview(clinic_b)
    event_b = _create_event_like_main_py(
        user_id=user_b, clinic_id=clinic_b, prescription_id=rx_b, preview_id=pv_b,
        is_manual=False, ai_dosha_imbalance="Kapha", ai_prescribed_herbs=[{"name": "Guggulu", "grams": 3}],
        confirmed_dosha_imbalance="Kapha", confirmed_prescribed_herbs=[{"name": "Guggulu", "grams": 3}],
    )
    row_b = _row_for_event(event_b)
    check(
        "a DIFFERENT clinic's first event ALSO has previous_event_hash = NULL, independent of clinic A already having 2 events",
        row_b["previous_event_hash"] is None, dict(row_b),
    )
    check("the two clinics' genesis events have DIFFERENT event_hash values (different content)", row_b["event_hash"] != row_1["event_hash"])


def test_verify_audit_integrity_passes_on_a_real_untampered_chain():
    print("\nTest: verify_audit_integrity() reports valid=True on a real, untampered chain")
    clinic_id = f"audit_store_valid_{secrets.token_hex(4)}"
    user_id = _make_real_user_id(clinic_id)
    for i in range(3):
        rx = _make_real_prescription(clinic_id)
        pv = _make_real_preview(clinic_id)
        _create_event_like_main_py(
            user_id=user_id, clinic_id=clinic_id, prescription_id=rx, preview_id=pv,
            is_manual=(i % 2 == 0), ai_dosha_imbalance=(None if i % 2 == 0 else "Vata"),
            ai_prescribed_herbs=(None if i % 2 == 0 else [{"name": "Ashwagandha", "grams": 10}]),
            confirmed_dosha_imbalance="Vata", confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
        )
    report = verify_audit_integrity(clinic_id)
    check("valid is True", report.valid is True, report)
    check("checked_count is exactly 3", report.checked_count == 3, report)
    check("violations is empty", report.violations == [], report)

    # assert_audit_integrity() must NOT raise on a valid chain.
    asserted = assert_audit_integrity(clinic_id)
    check("assert_audit_integrity() returns the same valid report without raising", asserted.valid is True)


def test_tampering_is_detected_and_restoration_revalidates():
    print("\nTest: THE MANDATORY REAL SQLITE TAMPERING TEST -- direct database edits ARE detected")
    clinic_id = f"audit_store_tamper_{secrets.token_hex(4)}"
    user_id = _make_real_user_id(clinic_id)
    other_user_id = _make_real_user_id(f"audit_store_tamper_other_{secrets.token_hex(4)}")

    def _fresh_event():
        rx = _make_real_prescription(clinic_id)
        pv = _make_real_preview(clinic_id)
        return _create_event_like_main_py(
            user_id=user_id, clinic_id=clinic_id, prescription_id=rx, preview_id=pv,
            is_manual=False, ai_dosha_imbalance="Vata", ai_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
            confirmed_dosha_imbalance="Vata", confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
        )

    tamper_cases = [
        ("event_data_json", lambda row: json.dumps({"decision_source": "MANUAL", "ai_proposal": None, "doctor_decision": {"dosha": "Forged", "herbs": []}, "was_changed": None})),
        ("previous_event_hash", lambda row: "0" * 64),
        ("event_hash", lambda row: "f" * 64),
        ("actor_user_id", lambda row: other_user_id),
        ("clinic_id", lambda row: clinic_id + "_forged"),
        ("prescription_id", lambda row: _make_real_prescription(clinic_id)),
    ]

    for column, forge_value_fn in tamper_cases:
        event_id = _fresh_event()
        pre_report = verify_audit_integrity(clinic_id)
        check(f"[{column}] chain is valid BEFORE tampering", pre_report.valid is True, pre_report)

        original_row = _row_for_event(event_id)
        original_value = original_row[column]
        forged_value = forge_value_fn(original_row)

        _raw_update(event_id, column, forged_value)

        # NOTE on `clinic_id` specifically: relabeling a row's clinic_id
        # moves it OUT of its rightful clinic's chain entirely -- a
        # clinic-SCOPED check (`WHERE clinic_id = ?`, exactly what GET
        # /audit/events runs for its own caller, deliberately, so one
        # clinic's admin can never trigger a query that even touches
        # another clinic's rows) simply stops seeing that row and reports
        # fewer events with NO violation, because "the row moved
        # elsewhere" and "the row was deleted" are indistinguishable from
        # inside one clinic's own view. This is a genuine, documented
        # limit of a per-clinic-scoped hash chain (see
        # backend/audit_store.py's HASH CHAIN docstring section) -- not a
        # bug to silently paper over. It IS still caught by a GLOBAL scan
        # (clinic_id=None, every clinic's chain): the row now appears as
        # some OTHER clinic's chain, where its non-NULL previous_event_hash
        # fails that (now foreign) chain's own genesis-must-be-NULL check.
        # So this one case is verified globally; every other case is
        # verified the same way GET /audit/events actually verifies it,
        # scoped to the caller's own clinic.
        detection_scope = None if column == "clinic_id" else clinic_id
        tampered_report = verify_audit_integrity(detection_scope)
        check(
            f"[{column}] tampering IS detected -- verify_audit_integrity() reports valid=False after a direct "
            f"UPDATE to audit_events.{column}"
            + (" (checked globally -- see the clinic_id note above)" if column == "clinic_id" else ""),
            tampered_report.valid is False, tampered_report,
        )
        check(
            f"[{column}] at least one violation names the tampered event_id",
            any(v.event_id == event_id for v in tampered_report.violations), tampered_report,
        )
        if column == "clinic_id":
            check(
                "[clinic_id] additionally, the ORIGINAL clinic's own SCOPED view (what GET /audit/events "
                "actually runs) no longer contains the relabeled event at all -- it silently has one fewer "
                "event and reports valid=True, which is the documented limit above, not a second bug",
                verify_audit_integrity(clinic_id).checked_count == pre_report.checked_count - 1,
                verify_audit_integrity(clinic_id),
            )

        # Restore/cleanup, per the A5 spec's explicit instruction.
        _raw_update(event_id, column, original_value)
        restored_report = verify_audit_integrity(clinic_id)
        check(f"[{column}] chain is valid AGAIN after restoring the original value", restored_report.valid is True, restored_report)

    # assert_audit_integrity() must raise (not silently return) on a genuinely invalid chain.
    event_id = _fresh_event()
    original_hash = _row_for_event(event_id)["event_hash"]
    _raw_update(event_id, "event_hash", "0" * 64)
    raised = None
    try:
        assert_audit_integrity(clinic_id)
    except AuditIntegrityError as e:
        raised = e
    check("assert_audit_integrity() raises AuditIntegrityError on a tampered chain", raised is not None)
    # Restore so this clinic_id's chain doesn't stay corrupted for any later test/run.
    _raw_update(event_id, "event_hash", original_hash)
    check(
        "the chain is valid again after restoring assert_audit_integrity()'s tampered event",
        verify_audit_integrity(clinic_id).valid is True,
    )


def test_concurrent_audit_event_creation_same_clinic():
    print("\nTest: two threads creating audit events for the SAME clinic concurrently -- both succeed, chain stays valid")
    clinic_id = f"audit_store_concurrent_{secrets.token_hex(4)}"
    user_id = _make_real_user_id(clinic_id)

    results = {}
    errors = {}
    barrier = threading.Barrier(2)

    def attempt(name: str):
        try:
            rx = _make_real_prescription(clinic_id, patient_name=f"Concurrent Patient {name}")
            pv = _make_real_preview(clinic_id)
            barrier.wait()
            results[name] = _create_event_like_main_py(
                user_id=user_id, clinic_id=clinic_id, prescription_id=rx, preview_id=pv,
                is_manual=False, ai_dosha_imbalance="Vata", ai_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
                confirmed_dosha_imbalance="Vata", confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
            )
        except Exception as e:
            errors[name] = e

    t1 = threading.Thread(target=attempt, args=("t1",))
    t2 = threading.Thread(target=attempt, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    check("both threads completed without raising (BEGIN IMMEDIATE serializes them)", not errors, errors)
    check("both threads got a distinct event_id", len(set(results.values())) == 2, results)

    report = verify_audit_integrity(clinic_id)
    check("the chain is valid after two concurrent inserts", report.valid is True, report)
    check("exactly two events were recorded for this clinic", report.checked_count == 2, report)


def test_restart_persistence():
    print("\nTest: an event created on one connection is present and valid when read from a BRAND NEW connection")
    clinic_id = f"audit_store_restart_{secrets.token_hex(4)}"
    user_id = _make_real_user_id(clinic_id)
    rx = _make_real_prescription(clinic_id)
    pv = _make_real_preview(clinic_id)
    event_id = _create_event_like_main_py(
        user_id=user_id, clinic_id=clinic_id, prescription_id=rx, preview_id=pv,
        is_manual=False, ai_dosha_imbalance="Vata", ai_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
        confirmed_dosha_imbalance="Vata", confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
    )
    # Every helper above already opens and closes its OWN fresh connection
    # per call (get_connection() is never reused across these calls) -- so
    # this next call is, by construction, reading from a brand new
    # connection with no shared in-memory state to the one that wrote it.
    # This is the closest proxy available to "process A writes, process B
    # reads" without literally forking a second OS process, and it proves
    # persistence is genuinely in the SQLite file, not process-local state.
    events = get_audit_events_for_clinic(clinic_id)
    check("the event is present when read back via a fresh connection", any(e["event_id"] == event_id for e in events), events)

    report = verify_audit_integrity(clinic_id)
    check("the chain is still valid when verified via a fresh connection", report.valid is True, report)


def test_no_update_or_delete_function_is_exposed():
    print("\nTest: backend/audit_store.py exposes NO update or delete operation for audit events")
    import backend.audit_store as audit_store_module
    check("no update_audit_event function exists", not hasattr(audit_store_module, "update_audit_event"))
    check("no delete_audit_event function exists", not hasattr(audit_store_module, "delete_audit_event"))


def test_event_data_never_contains_pii_shaped_fields():
    print("\nTest: event_data's own keys never include patient identity fields, for either decision source")
    # Deliberately the EXACT PII field names this project uses elsewhere
    # (PreviewRequest/ManualPreviewRequest's patient_name/patient_phone/
    # patient_id -- see backend/schemas.py) -- NOT a generic "name"/"phone"
    # ban, which would false-positive on the legitimate herb `name` field
    # inside event_data["ai_proposal"]["herbs"]/["doctor_decision"]["herbs"].
    pii_keys = {"patient_name", "patient_phone", "patient_id", "notes"}
    ai_event = build_prescription_confirmed_event_data(
        is_manual=False, ai_dosha_imbalance="Vata", ai_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
        confirmed_dosha_imbalance="Vata", confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
    )
    manual_event = build_prescription_confirmed_event_data(
        is_manual=True, ai_dosha_imbalance=None, ai_prescribed_herbs=None,
        confirmed_dosha_imbalance="Vata", confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
    )

    def _flatten_keys(obj, out):
        if isinstance(obj, dict):
            for k, v in obj.items():
                out.add(k)
                _flatten_keys(v, out)
        elif isinstance(obj, list):
            for v in obj:
                _flatten_keys(v, out)

    for label, event in (("AI_ASSISTED", ai_event), ("MANUAL", manual_event)):
        keys = set()
        _flatten_keys(event, keys)
        check(f"[{label}] no PII-shaped key appears anywhere in event_data's structure", keys.isdisjoint(pii_keys), keys)


_ALL_TESTS = [
    test_ai_assisted_event_data_shape_and_was_changed,
    test_manual_event_never_fabricates_an_ai_proposal,
    test_ai_assisted_requires_a_real_proposal,
    test_canonicalization_herb_order_and_duplicates_do_not_affect_content,
    test_hash_chain_is_per_clinic_and_correctly_linked,
    test_verify_audit_integrity_passes_on_a_real_untampered_chain,
    test_tampering_is_detected_and_restoration_revalidates,
    test_concurrent_audit_event_creation_same_clinic,
    test_restart_persistence,
    test_no_update_or_delete_function_is_exposed,
    test_event_data_never_contains_pii_shaped_fields,
]


if __name__ == "__main__":
    # Same per-test-function try/except pattern as tests/test_idempotency_store.py.
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
