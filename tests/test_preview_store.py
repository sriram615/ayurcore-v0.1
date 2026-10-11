"""
Persistent-preview tests -- covers backend/preview_store.py and the new
one-transaction confirm logic in backend/main.py's /prescribe/confirm
(Phase A, A1).

This deliberately does NOT go through FastAPI's TestClient. Everything A1
actually changed -- preview_store.py's CRUD, inventory_service.py's
deduct_and_prescribe(conn=...) refactor, herb_resolver.py's normalize_herb
-- is pure sqlite3 + Python, no FastAPI involved. So this file exercises
that layer directly, including reproducing, line for line, the exact
transaction sequence backend/main.py's /prescribe/confirm now runs (BEGIN
IMMEDIATE -> get_pending_preview_for_confirm -> normalize_herb ->
deduct_and_prescribe(conn=conn) -> mark_confirmed -> commit) -- everything
here is something that was genuinely executed against the real
db/ayurcore.db, not FastAPI-mocked or merely reasoned about. The
`REQUIRES MAC VERIFICATION` items are the ones that need an actual HTTP
round-trip through main.py's routes (status codes, request/response JSON
shapes) -- those are covered by tests/test_preview_lifecycle.py,
tests/test_golden.py, and tests/test_manual_fallback.py, which the Mac run
already re-confirmed still pass unchanged after this milestone's refactor.

Specifically checks:
  1. create_preview produces a real, opaque row with the right fields.
  2. get_pending_preview_for_confirm: not-found, expired (live comparison,
     not dependent on any cleanup job), and the happy path.
  3. The full one-transaction confirm sequence: success commits preview +
     inventory + prescription together; a failure (insufficient stock,
     unknown herb) rolls back ALL of it, leaving the preview retryable and
     stock/prescriptions completely untouched -- then a corrected retry
     against the SAME preview_id succeeds.
  4. Single-use: once CONFIRMED, a second attempt raises
     PreviewAlreadyConfirmedError before touching anything else.
  5. mark_stale_previews_expired: flips only what should flip, scoped by
     clinic_id when given.
  6. RESTART PERSISTENCE, literally: process A creates a preview and
     exits; a genuinely separate OS process (not just a new connection in
     this same test process) then confirms it successfully. This is the
     actual claim A1 exists to make true, tested as literally as this
     layer allows.
  7. Concurrency: two threads racing to confirm the SAME preview_id --
     exactly one must succeed, the other must see
     PreviewAlreadyConfirmedError, and stock must reflect exactly one
     deduction, never a torn or double one.

Run directly (no pytest required):
    python tests/test_preview_store.py
"""
import json
import os
import subprocess
import sys
import sqlite3
import tempfile
import threading

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import get_connection, DB_PATH
from db.migrations import run_migrations
from backend.herb_resolver import normalize_herb, UnknownHerbError
from backend.inventory_service import deduct_and_prescribe, InsufficientStockError
from backend.preview_store import (
    create_preview,
    get_pending_preview_for_confirm,
    mark_confirmed,
    mark_stale_previews_expired,
    SchemaNotMigratedError,
    PreviewNotFoundError,
    PreviewExpiredError,
    PreviewAlreadyConfirmedError,
)

# Regression fix for the hard A1 Mac verification failure: this file talks
# to db/ayurcore.db directly (create_preview, etc.) with no FastAPI/ASGI app
# in between, so there is no @app.on_event("startup") to run pending
# migrations for it. The ONLY sanctioned place schema changes happen in
# this project is db/migrations.py's run_migrations() -- called either here
# explicitly, manually via `python db/database.py`, or by the FastAPI
# startup hook. Skipping this call is exactly what let "no such table:
# prescription_previews" through: the real ayurcore.db on Mac had never
# had run_migrations() invoked against it by ANY path (not even for
# migrations 001/002), so a file that talks to the db directly must
# bootstrap it itself, the same way the real application's entrypoints do.
# Cheap and idempotent when there's nothing pending, by design (see
# migrations.py).
run_migrations()

_passed = 0
_failed = 0


def check(label: str, condition: bool, detail: str = ""):
    """Records AND enforces one assertion. Raises AssertionError
    immediately on failure (rather than only printing a FAIL line and
    incrementing a counter) so that `pytest tests/test_preview_store.py`
    genuinely fails when a check fails -- not just `python
    tests/test_preview_store.py`'s own __main__ block. This file now
    carries test_cross_clinic_confirm_is_indistinguishable_from_not_found_regardless_of_lifecycle_state,
    a security-critical regression test (Phase A, A3 Mac-verification
    finding -- see tests/test_rbac.py's module docstring, "PYTEST
    COMPATIBILITY", for the full incident of a security assertion that
    pytest silently never actually ran); this file's own check() is fixed
    the same way for the same reason."""
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"=> PASS: {label}")
    else:
        _failed += 1
        message = label + (f" ({detail})" if detail else "")
        print(f"=> FAIL: {message}")
        raise AssertionError(message)


def _set_stock(herb_name: str, grams: int):
    conn = get_connection()
    try:
        conn.execute("UPDATE inventory SET stock_grams = ? WHERE herb_name = ?", (grams, herb_name))
        conn.commit()
    finally:
        conn.close()


def _get_stock(herb_name: str) -> int:
    conn = get_connection()
    try:
        row = conn.execute("SELECT stock_grams FROM inventory WHERE herb_name = ?", (herb_name,)).fetchone()
        return row["stock_grams"] if row else 0
    finally:
        conn.close()


def _set_stock_for_clinic(herb_name: str, grams: int, clinic_id: str):
    """Like _set_stock above, but for a clinic that may have no seeded
    inventory row at all yet (db/seed.py only seeds "default_clinic") --
    used by test_cross_clinic_confirm_is_indistinguishable_from_not_found_regardless_of_lifecycle_state
    below, which deliberately exercises two clinics that are not
    "default_clinic". UPDATE-or-INSERT so it works whether or not a row
    already exists."""
    conn = get_connection()
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


def _row_for(preview_id: str) -> sqlite3.Row:
    conn = get_connection()
    try:
        return conn.execute(
            "SELECT * FROM prescription_previews WHERE preview_id = ?", (preview_id,)
        ).fetchone()
    finally:
        conn.close()


def _backdate_expiry(preview_id: str, seconds_ago: int = 60):
    """Test-only helper: directly rewrites expires_at into the past, the
    same way test_manual_fallback.py's _set_stock directly manipulates a
    row to set up a scenario, rather than waiting 30 real minutes."""
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE prescription_previews SET expires_at = datetime('now', ?) WHERE preview_id = ?",
            (f"-{seconds_ago} seconds", preview_id),
        )
        conn.commit()
    finally:
        conn.close()


def _confirm_like_main_py(preview_id: str, dosha_override=None, herbs_override=None, clinic_id: str = "default_clinic"):
    """Reproduces backend/main.py's /prescribe/confirm transaction body
    exactly (BEGIN IMMEDIATE -> lookup -> normalize -> deduct_and_prescribe
    -> mark_confirmed -> commit/rollback), without FastAPI in the way, so
    this file can genuinely execute the one-transaction logic A1 added.
    Raises the same preview_store exceptions main.py catches and maps to
    HTTP status codes; raises UnknownHerbError / InsufficientStockError
    for the other two rejection paths, same as main.py does before it
    converts them.

    `clinic_id` stands in for backend/main.py's `auth.clinic_id` (Phase A,
    A3) -- every preview this file creates uses clinic_id="default_clinic"
    (see every create_preview(...) call below), so that's this parameter's
    default too; test_cross_clinic_confirm_is_indistinguishable_from_not_found
    below is the one caller that passes something else on purpose."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            entry = get_pending_preview_for_confirm(conn, preview_id, clinic_id)

            final_dosha = dosha_override if dosha_override is not None else entry["ai_dosha_imbalance"]
            final_herbs_raw = (
                herbs_override if herbs_override is not None else json.loads(entry["ai_prescribed_herbs_json"])
            )

            normalized_final_herbs = []
            for herb in final_herbs_raw:
                canon_name = normalize_herb(herb["name"])
                normalized_final_herbs.append({"name": canon_name, "grams": herb["grams"]})

            transaction_result = deduct_and_prescribe(
                patient_name=entry["patient_name"],
                dosha=final_dosha,
                raw_herbs=normalized_final_herbs,
                clinic_id=entry["clinic_id"],
                conn=conn,
            )

            mark_confirmed(conn, preview_id, final_dosha, normalized_final_herbs)
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
            return transaction_result
    finally:
        conn.close()


def test_create_and_read_a_pending_preview():
    _set_stock("Ashwagandha", 5000)
    preview_id = create_preview(
        patient_name="Preview Store Test Patient A",
        patient_phone=None,
        patient_id=None,
        clinic_id="default_clinic",
        dosha_imbalance="Vata",
        prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
        is_manual=False,
        created_by_session=None,
    )
    check("create_preview returns a non-empty opaque id", bool(preview_id) and len(preview_id) >= 16, preview_id)

    row = _row_for(preview_id)
    check("the row exists in prescription_previews", row is not None)
    check("status is PENDING at creation", row["status"] == "PENDING", row["status"])
    check("is_manual is stored as 0 for an AI preview", row["is_manual"] == 0)
    check(
        "ai_prescribed_herbs_json round-trips correctly",
        json.loads(row["ai_prescribed_herbs_json"]) == [{"name": "Ashwagandha", "grams": 10}],
    )

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        fetched = get_pending_preview_for_confirm(conn, preview_id, "default_clinic")
        check("get_pending_preview_for_confirm returns the pending row without error", fetched is not None)
        conn.rollback()  # pure read, nothing to commit
    finally:
        conn.close()


def test_not_found_and_expired_are_distinct_but_both_rejected():
    try:
        conn = get_connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            get_pending_preview_for_confirm(conn, "this-preview-id-was-never-created", "default_clinic")
            check("a nonexistent preview_id raises PreviewNotFoundError", False, "no exception raised")
        except PreviewNotFoundError:
            check("a nonexistent preview_id raises PreviewNotFoundError", True)
        finally:
            conn.rollback()
            conn.close()
    except Exception as e:
        check("a nonexistent preview_id raises PreviewNotFoundError", False, str(e))

    preview_id = create_preview(
        patient_name="Preview Store Test Patient B", patient_phone=None, patient_id=None,
        clinic_id="default_clinic", dosha_imbalance="Pitta",
        prescribed_herbs=[{"name": "Brahmi", "grams": 5}], is_manual=False, created_by_session=None,
    )
    _backdate_expiry(preview_id, seconds_ago=60)

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            get_pending_preview_for_confirm(conn, preview_id, "default_clinic")
            check(
                "an expired preview raises PreviewExpiredError, checked LIVE with no cleanup job having run",
                False, "no exception raised",
            )
        except PreviewExpiredError:
            check(
                "an expired preview raises PreviewExpiredError, checked LIVE with no cleanup job having run",
                True,
            )
        finally:
            conn.rollback()
    finally:
        conn.close()


def test_cross_clinic_confirm_is_indistinguishable_from_not_found_regardless_of_lifecycle_state():
    """Phase A, A3 SECURITY REGRESSION TEST -- storage layer.

    Found by a real Mac-verification run of tests/test_rbac.py: a caller
    from clinic A requesting a clinic B preview_id must get EXACTLY the
    same PreviewNotFoundError (-> 404 at the HTTP layer) a never-issued
    preview_id gets, REGARDLESS of whether that clinic-B preview is
    PENDING, EXPIRED, or CONFIRMED. An earlier version of
    get_pending_preview_for_confirm() took no clinic argument at all and
    left clinic-ownership checking to the caller, AFTER this function had
    already raised a status-specific exception -- so an already-CONFIRMED
    foreign-clinic preview leaked as PreviewAlreadyConfirmedError (-> 409
    at the HTTP layer, with a message literally confirming the preview
    exists and was already confirmed) before the caller ever got a row to
    check clinic_id against. This test exercises the fix directly at the
    layer the bug actually lived in -- no FastAPI needed, so unlike
    tests/test_rbac.py's HTTP-layer coverage of the same property, this
    one runs anywhere Python + sqlite3 run, including this project's
    cloud sandbox.

    Covers all three foreign-clinic lifecycle states, PLUS same-clinic
    CONFIRMED as a deliberate contrast (proving the fix didn't
    overcorrect into hiding a legitimate same-clinic 409)."""
    _set_stock_for_clinic("Ashwagandha", 5000, clinic_id="cross_clinic_regression_home")

    # --- Foreign-clinic PENDING: never confirmed, never expired. ---
    pending_id = create_preview(
        patient_name="Cross Clinic Regression Patient Pending", patient_phone=None, patient_id=None,
        clinic_id="cross_clinic_regression_foreign", dosha_imbalance="Vata",
        prescribed_herbs=[{"name": "Ashwagandha", "grams": 5}], is_manual=False, created_by_session=None,
    )
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            get_pending_preview_for_confirm(conn, pending_id, "cross_clinic_regression_home")
            check("foreign-clinic PENDING preview raises PreviewNotFoundError (not a bare pass-through)", False, "no exception raised")
        except PreviewNotFoundError:
            check("foreign-clinic PENDING preview raises PreviewNotFoundError, same as a never-issued id", True)
        except PreviewExpiredError:
            check("foreign-clinic PENDING preview raises PreviewNotFoundError, same as a never-issued id", False, "raised PreviewExpiredError instead -- wrong exception, would leak state")
        finally:
            conn.rollback()
    finally:
        conn.close()

    # --- Foreign-clinic EXPIRED. ---
    expired_id = create_preview(
        patient_name="Cross Clinic Regression Patient Expired", patient_phone=None, patient_id=None,
        clinic_id="cross_clinic_regression_foreign", dosha_imbalance="Vata",
        prescribed_herbs=[{"name": "Ashwagandha", "grams": 5}], is_manual=False, created_by_session=None,
    )
    _backdate_expiry(expired_id, seconds_ago=60)
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            get_pending_preview_for_confirm(conn, expired_id, "cross_clinic_regression_home")
            check("foreign-clinic EXPIRED preview raises PreviewNotFoundError (not a bare pass-through)", False, "no exception raised")
        except PreviewNotFoundError:
            check(
                "foreign-clinic EXPIRED preview raises PreviewNotFoundError -- NOT PreviewExpiredError -- so a "
                "caller cannot even learn the foreign preview existed long enough to expire",
                True,
            )
        except PreviewExpiredError:
            check(
                "foreign-clinic EXPIRED preview raises PreviewNotFoundError, not PreviewExpiredError",
                False, "raised PreviewExpiredError instead -- this is a real (smaller) leak: reveals the foreign preview existed",
            )
        finally:
            conn.rollback()
    finally:
        conn.close()

    # --- Foreign-clinic CONFIRMED -- the exact case the Mac run caught. ---
    confirmed_id = create_preview(
        patient_name="Cross Clinic Regression Patient Confirmed", patient_phone=None, patient_id=None,
        clinic_id="cross_clinic_regression_foreign", dosha_imbalance="Vata",
        prescribed_herbs=[{"name": "Ashwagandha", "grams": 5}], is_manual=False, created_by_session=None,
    )
    _set_stock_for_clinic("Ashwagandha", 5000, clinic_id="cross_clinic_regression_foreign")
    _confirm_like_main_py(confirmed_id, clinic_id="cross_clinic_regression_foreign")
    check(
        "setup: the foreign-clinic preview really is CONFIRMED before the adversarial read below",
        _row_for(confirmed_id)["status"] == "CONFIRMED",
    )

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            get_pending_preview_for_confirm(conn, confirmed_id, "cross_clinic_regression_home")
            check(
                "THE REGRESSION CASE: a foreign-clinic CONFIRMED preview raises PreviewNotFoundError, not a bare pass-through",
                False, "no exception raised -- a foreign clinic's caller would receive this preview's real data",
            )
        except PreviewNotFoundError:
            check(
                "THE REGRESSION CASE: a foreign-clinic CONFIRMED preview raises PreviewNotFoundError (-> 404), "
                "NOT PreviewAlreadyConfirmedError (-> 409) -- fixed",
                True,
            )
        except PreviewAlreadyConfirmedError:
            check(
                "THE REGRESSION CASE: a foreign-clinic CONFIRMED preview must raise PreviewNotFoundError, "
                "not PreviewAlreadyConfirmedError",
                False,
                "raised PreviewAlreadyConfirmedError instead -- this IS the cross-clinic information "
                "disclosure a Mac-verification run of tests/test_rbac.py caught: a clinic-A caller would "
                "receive HTTP 409 'has already been confirmed', learning both that clinic B's preview_id "
                "exists and its lifecycle state, despite having no authorization to know either.",
            )
        finally:
            conn.rollback()
    finally:
        conn.close()

    # --- Contrast: the SAME preview, looked up by its OWN clinic, must
    # still correctly raise PreviewAlreadyConfirmedError (-> 409). The fix
    # must not have overcorrected into hiding a legitimate same-clinic
    # "already confirmed" response behind 404. ---
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            get_pending_preview_for_confirm(conn, confirmed_id, "cross_clinic_regression_foreign")
            check("same-clinic CONFIRMED preview still raises PreviewAlreadyConfirmedError (not silently passed through)", False, "no exception raised")
        except PreviewAlreadyConfirmedError:
            check(
                "CONTRAST: the SAME preview, looked up by its OWN (correct) clinic, still correctly raises "
                "PreviewAlreadyConfirmedError (-> 409) -- the fix did not overcorrect into a blanket 404",
                True,
            )
        except PreviewNotFoundError:
            check(
                "same-clinic CONFIRMED preview must raise PreviewAlreadyConfirmedError, not PreviewNotFoundError",
                False, "raised PreviewNotFoundError instead -- the fix overcorrected and now hides a legitimate same-clinic 409 behind 404",
            )
        finally:
            conn.rollback()
    finally:
        conn.close()

    _set_stock_for_clinic("Ashwagandha", 5000, clinic_id="cross_clinic_regression_home")
    _set_stock_for_clinic("Ashwagandha", 5000, clinic_id="cross_clinic_regression_foreign")


def test_full_transaction_success_and_failure_paths():
    _set_stock("Ashwagandha", 5000)
    initial_stock = _get_stock("Ashwagandha")

    # --- Failure path first: insufficient stock must roll back EVERYTHING,
    # including the preview's status, leaving it retryable. ---
    preview_id = create_preview(
        patient_name="Preview Store Test Patient C", patient_phone=None, patient_id=None,
        clinic_id="default_clinic", dosha_imbalance="Vata",
        prescribed_herbs=[{"name": "Ashwagandha", "grams": 999999}], is_manual=False, created_by_session=None,
    )

    conn = get_connection()
    prescriptions_before = conn.execute("SELECT COUNT(*) FROM prescriptions").fetchone()[0]
    conn.close()

    try:
        _confirm_like_main_py(preview_id)
        check("insufficient stock raises InsufficientStockError", False, "no exception raised")
    except InsufficientStockError:
        check("insufficient stock raises InsufficientStockError", True)

    check("stock is completely unchanged after the failed attempt", _get_stock("Ashwagandha") == initial_stock)
    row_after_failure = _row_for(preview_id)
    check(
        "the preview is STILL PENDING after a failed confirm, not consumed",
        row_after_failure["status"] == "PENDING", row_after_failure["status"],
    )
    conn = get_connection()
    prescriptions_after_failure = conn.execute("SELECT COUNT(*) FROM prescriptions").fetchone()[0]
    conn.close()
    check(
        "no prescription row was created by the failed attempt (the whole transaction rolled back)",
        prescriptions_after_failure == prescriptions_before,
    )

    # --- Now retry the SAME preview_id with a corrected quantity -- this
    # is exactly the "failed confirmation must leave preview retryable"
    # guarantee, exercised end to end. ---
    result = _confirm_like_main_py(preview_id, herbs_override=[{"name": "Ashwagandha", "grams": 10}])
    check("retrying the same preview_id with a corrected amount succeeds", result is not None)
    check("stock reflects exactly the corrected deduction", _get_stock("Ashwagandha") == initial_stock - 10)

    row_after_success = _row_for(preview_id)
    check("the preview is now CONFIRMED", row_after_success["status"] == "CONFIRMED")
    check("confirmed_at was recorded", row_after_success["confirmed_at"] is not None)
    check(
        "confirmed_prescribed_herbs_json reflects the doctor's corrected value, not the original 999999g",
        json.loads(row_after_success["confirmed_prescribed_herbs_json"]) == [{"name": "Ashwagandha", "grams": 10}],
    )

    # --- Single-use: a second confirm attempt on the now-CONFIRMED preview
    # must be rejected before touching stock again. ---
    try:
        _confirm_like_main_py(preview_id)
        check("re-confirming an already-CONFIRMED preview raises PreviewAlreadyConfirmedError", False, "no exception")
    except PreviewAlreadyConfirmedError:
        check("re-confirming an already-CONFIRMED preview raises PreviewAlreadyConfirmedError", True)
    check("stock is unchanged by the rejected replay", _get_stock("Ashwagandha") == initial_stock - 10)

    _set_stock("Ashwagandha", 5000)  # restore for other tests/runs


def test_unknown_herb_rolls_back_the_whole_transaction():
    preview_id = create_preview(
        patient_name="Preview Store Test Patient D", patient_phone=None, patient_id=None,
        clinic_id="default_clinic", dosha_imbalance="Vata",
        prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}], is_manual=False, created_by_session=None,
    )
    try:
        _confirm_like_main_py(preview_id, herbs_override=[{"name": "TotallyMadeUpHerb", "grams": 5}])
        check("an unknown herb at confirm time raises UnknownHerbError", False, "no exception raised")
    except UnknownHerbError:
        check("an unknown herb at confirm time raises UnknownHerbError", True)

    row = _row_for(preview_id)
    check("the preview is still PENDING (retryable) after the unknown-herb rejection", row["status"] == "PENDING")


def test_mark_stale_previews_expired_is_scoped_correctly():
    stale_id = create_preview(
        patient_name="Preview Store Test Patient E", patient_phone=None, patient_id=None,
        clinic_id="default_clinic", dosha_imbalance="Vata",
        prescribed_herbs=[{"name": "Ashwagandha", "grams": 1}], is_manual=False, created_by_session=None,
    )
    _backdate_expiry(stale_id, seconds_ago=60)

    fresh_id = create_preview(
        patient_name="Preview Store Test Patient F", patient_phone=None, patient_id=None,
        clinic_id="default_clinic", dosha_imbalance="Vata",
        prescribed_herbs=[{"name": "Ashwagandha", "grams": 1}], is_manual=False, created_by_session=None,
    )

    mark_stale_previews_expired(clinic_id="default_clinic")

    check("the stale preview was flipped to EXPIRED", _row_for(stale_id)["status"] == "EXPIRED")
    check("the still-valid preview was left PENDING", _row_for(fresh_id)["status"] == "PENDING")


def test_missing_schema_raises_clear_schema_error_not_swallowed():
    """Regression test for the hard A1 Mac verification failure.

    The real ayurcore.db on the user's Mac had never had run_migrations()
    invoked against it by any path, so prescription_previews (and, it
    turned out, schema_migrations itself) simply did not exist.
    create_preview() raised a raw, unhelpful sqlite3.OperationalError, and
    -- more dangerously -- backend/main.py's mark_stale_previews_expired()
    call sites caught it under a blanket `except Exception` and logged it
    as routine best-effort cleanup noise, indistinguishable from a harmless
    hiccup. This proves the fix: both now raise the same clear,
    distinctly-named SchemaNotMigratedError, which backend/main.py no
    longer swallows (see its two mark_stale_previews_expired() call sites).
    """
    original_db_path = DB_PATH
    import db.database as database_module

    fd, empty_db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(empty_db_path)  # completely empty -- no schema_migrations, no tables at all,
    # deliberately more hostile than a database merely missing prescription_previews.

    database_module.DB_PATH = empty_db_path
    try:
        raised = None
        try:
            create_preview(
                patient_name="Schema Error Test Patient", patient_phone=None, patient_id=None,
                clinic_id="default_clinic", dosha_imbalance="Vata",
                prescribed_herbs=[{"name": "Ashwagandha", "grams": 1}], is_manual=False, created_by_session=None,
            )
        except Exception as e:
            raised = e
        check(
            "create_preview() against an unmigrated database raises SchemaNotMigratedError, not a raw sqlite3 error",
            isinstance(raised, SchemaNotMigratedError),
            repr(raised),
        )

        raised2 = None
        try:
            mark_stale_previews_expired()
        except Exception as e:
            raised2 = e
        check(
            "mark_stale_previews_expired() against an unmigrated database ALSO raises SchemaNotMigratedError "
            "-- this is the exact call that used to be silently swallowed as best-effort noise",
            isinstance(raised2, SchemaNotMigratedError),
            repr(raised2),
        )
    finally:
        database_module.DB_PATH = original_db_path
        if os.path.exists(empty_db_path):
            os.remove(empty_db_path)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(empty_db_path + suffix):
                os.remove(empty_db_path + suffix)


def test_restart_persistence_across_a_real_separate_process():
    """The actual A1 claim, tested as literally as this layer allows: one
    OS process creates a preview and exits completely; a SECOND, separate
    OS process (not a new connection inside this same test, an actual
    `python3` subprocess) then confirms it successfully against the same
    database file."""
    _set_stock("Brahmi", 3500)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    create_script = f"""
import sys
sys.path.append({project_root!r})
from backend.preview_store import create_preview
preview_id = create_preview(
    patient_name="Restart Persistence Test Patient",
    patient_phone=None, patient_id=None, clinic_id="default_clinic",
    dosha_imbalance="Kapha", prescribed_herbs=[{{"name": "Brahmi", "grams": 7}}],
    is_manual=False, created_by_session=None,
)
print(preview_id)
"""
    proc1 = subprocess.run([sys.executable, "-c", create_script], capture_output=True, text=True, timeout=30)
    check("process A (creates the preview) exits cleanly", proc1.returncode == 0, proc1.stderr)
    preview_id = proc1.stdout.strip().splitlines()[-1] if proc1.stdout.strip() else ""
    check("process A printed a preview_id", bool(preview_id), proc1.stdout + proc1.stderr)

    # Process A is now completely gone -- no shared memory, no open
    # connection, nothing survives except what's on disk in db/ayurcore.db.

    confirm_script = f"""
import sys, json
sys.path.append({project_root!r})
from db.database import get_connection
from backend.herb_resolver import normalize_herb
from backend.inventory_service import deduct_and_prescribe
from backend.preview_store import get_pending_preview_for_confirm, mark_confirmed

conn = get_connection()
conn.execute("BEGIN IMMEDIATE")
entry = get_pending_preview_for_confirm(conn, {preview_id!r}, "default_clinic")
herbs = json.loads(entry["ai_prescribed_herbs_json"])
normalized = [{{"name": normalize_herb(h["name"]), "grams": h["grams"]}} for h in herbs]
result = deduct_and_prescribe(
    patient_name=entry["patient_name"], dosha=entry["ai_dosha_imbalance"],
    raw_herbs=normalized, clinic_id=entry["clinic_id"], conn=conn,
)
mark_confirmed(conn, {preview_id!r}, entry["ai_dosha_imbalance"], normalized)
conn.commit()
conn.close()
print(result["prescription_id"])
"""
    proc2 = subprocess.run([sys.executable, "-c", confirm_script], capture_output=True, text=True, timeout=30)
    check(
        "process B (a genuinely separate OS process) confirms the preview process A created, successfully",
        proc2.returncode == 0, proc2.stderr,
    )

    row = _row_for(preview_id)
    check(
        "the preview survived the process boundary and shows CONFIRMED after process B's confirm",
        row is not None and row["status"] == "CONFIRMED", dict(row) if row else None,
    )
    check("stock reflects the deduction made by the separate process", _get_stock("Brahmi") == 3500 - 7)

    _set_stock("Brahmi", 3500)  # restore


def test_concurrent_confirms_of_the_same_preview_id():
    _set_stock("Ashwagandha", 5000)
    initial_stock = _get_stock("Ashwagandha")

    preview_id = create_preview(
        patient_name="Preview Store Test Patient G", patient_phone=None, patient_id=None,
        clinic_id="default_clinic", dosha_imbalance="Vata",
        prescribed_herbs=[{"name": "Ashwagandha", "grams": 50}], is_manual=False, created_by_session=None,
    )

    results = {}
    errors = {}
    barrier = threading.Barrier(2)

    def attempt(name: str):
        try:
            barrier.wait()
            results[name] = _confirm_like_main_py(preview_id)
        except Exception as e:
            errors[name] = e

    t1 = threading.Thread(target=attempt, args=("t1",))
    t2 = threading.Thread(target=attempt, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    successes = [n for n in ("t1", "t2") if n in results]
    failures = [n for n in ("t1", "t2") if n in errors]
    check("exactly one of the two concurrent confirms succeeded", len(successes) == 1, results)
    check(
        "the other raised PreviewAlreadyConfirmedError, not some other error or a silent double-success",
        len(failures) == 1 and isinstance(errors.get(failures[0]), PreviewAlreadyConfirmedError),
        errors,
    )
    check(
        "stock reflects exactly ONE deduction, never a torn or double one",
        _get_stock("Ashwagandha") == initial_stock - 50,
        _get_stock("Ashwagandha"),
    )

    _set_stock("Ashwagandha", 5000)  # restore


_ALL_TESTS = [
    test_create_and_read_a_pending_preview,
    test_not_found_and_expired_are_distinct_but_both_rejected,
    test_cross_clinic_confirm_is_indistinguishable_from_not_found_regardless_of_lifecycle_state,
    test_full_transaction_success_and_failure_paths,
    test_unknown_herb_rolls_back_the_whole_transaction,
    test_mark_stale_previews_expired_is_scoped_correctly,
    test_missing_schema_raises_clear_schema_error_not_swallowed,
    test_restart_persistence_across_a_real_separate_process,
    test_concurrent_confirms_of_the_same_preview_id,
]


if __name__ == "__main__":
    # check() now raises AssertionError on failure -- see its docstring
    # above. Run through a loop that catches that per test function so the
    # whole suite still runs and gets tallied, matching this project's
    # "run everything, report a full tally" convention for direct
    # execution, while pytest (calling each test_* function directly)
    # correctly sees and reports a real failure too.
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
