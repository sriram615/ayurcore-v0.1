"""
Persistent storage for prescription previews (Phase A, A1).

Replaces backend/main.py's old module-level _PENDING_PREVIEWS /
_CONSUMED_PREVIEWS dicts. Those did not survive a process restart, and were
not shared across multiple worker processes -- a preview created by one
worker literally could not be confirmed by another, since each worker had
its own separate dict in its own separate memory. Everything they held now
lives in the `prescription_previews` table (migrations/003_...sql) instead.

Lifecycle: PENDING -> CONFIRMED, or PENDING -> EXPIRED. A preview is
created PENDING by /prescribe/preview or /prescribe/manual-preview, and
only ever leaves that state via a SUCCESSFUL /prescribe/confirm (->
CONFIRMED) or by expiring (-> EXPIRED, enforced live, not by a cleanup
job -- see get_pending_preview_for_confirm below). A failed confirm
attempt (unknown herb, insufficient stock, wrong session) leaves the row
exactly as it was, still PENDING, so the doctor can correct and retry.

"Original AI proposal" vs. "current doctor-edited proposal": stored as two
separate column pairs, ai_* (written once, at creation -- for a manual
preview this is simply what the doctor entered up front, per the existing
is_manual convention) and confirmed_* (written once, at the moment of a
successful confirm, holding the doctor's final values whether or not they
differ from the AI's). There is no third, "in-progress edit" state to
persist -- the locked API contract's /prescribe/confirm takes the doctor's
correction and the confirmation as a single call, not a separate
save-draft step, so confirmed_* is simply NULL until a preview is actually
confirmed.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from db.database import get_connection, DEFAULT_CLINIC_ID

# The locked API contract's guarantee: "preview_id expires after 30 minutes
# of not being confirmed." Single source of truth for that number.
PREVIEW_TTL_SECONDS = 30 * 60

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# How many times to retry preview_id generation on a (astronomically
# unlikely) collision against an existing row before giving up. With
# secrets.token_hex(8) (64 bits of entropy) this is pure defense in depth,
# not something expected to ever actually retry in practice -- but now
# that preview_id is a real PRIMARY KEY, a collision raises a real
# IntegrityError instead of silently overwriting another patient's preview
# the way the old plain dict would have. Retrying is strictly safer than
# either crashing the request or letting the collision through.
_MAX_ID_GENERATION_ATTEMPTS = 5


class PreviewNotFoundError(Exception):
    """No preview exists with this preview_id -- never existed, or this is
    a forged/guessed ID. Maps to 404 in backend/main.py, same as an
    expired one -- the locked contract treats "not found" and "expired" as
    one response, deliberately not distinguishing them to a caller."""


class PreviewExpiredError(Exception):
    """The preview exists but its 30-minute window has passed. Maps to 404
    in backend/main.py -- see PreviewNotFoundError's docstring for why
    these two are not distinguished at the HTTP layer."""


class PreviewAlreadyConfirmedError(Exception):
    """The preview exists and was already successfully confirmed once.
    Maps to 409 in backend/main.py -- distinct from 404 specifically so a
    doctor's browser retry of an already-successful confirm doesn't look
    like the preview simply vanished."""


class PatientLinkError(Exception):
    """The preview's patient_pk was rejected by migration 009's write-time
    clinic-isolation trigger (patient missing, or of another clinic).
    backend/main.py validates the patient first, so reaching this means the
    database-level backstop caught something the application check did not;
    it is surfaced, never retried."""


class EncounterLinkError(Exception):
    """The preview's encounter_pk was rejected by migration 012's write-time
    trigger (encounter missing, of another clinic or patient, or no longer
    OPEN). backend/main.py validates the encounter first, so reaching this
    means the database backstop caught a change in between (e.g. the
    encounter was closed concurrently). Surfaced, never retried."""


class SchemaNotMigratedError(RuntimeError):
    """Raised when a required table (e.g. prescription_previews) does not
    exist in the database this connection points at.

    This is NEVER a transient condition -- it means
    db.migrations.run_migrations() has not been invoked against this
    database file by any of its sanctioned entry points (the FastAPI
    @app.on_event("startup") hook, or a manual `python db/database.py`).

    This exists because of a real incident (Phase A, A1 hard Mac
    verification failure): the real ayurcore.db had never had
    run_migrations() run against it at all -- not even for migrations
    001/002 -- because every test constructed FastAPI's TestClient without
    the `with` block that's required to trigger ASGI startup events. The
    resulting sqlite3.OperationalError: no such table: prescription_previews
    was, at one call site (mark_stale_previews_expired()'s caller in
    backend/main.py), being caught by a blanket `except Exception` and
    logged as routine best-effort cleanup noise -- exactly the wrong
    treatment for "the schema is fundamentally missing." A distinct
    exception type lets callers refuse to swallow this class of failure
    while still treating genuinely transient errors as best-effort.
    """


def _reraise_as_schema_error_if_missing_table(exc: sqlite3.OperationalError, table: str = "prescription_previews") -> None:
    """Turns sqlite3's own terse 'no such table: X' text into a loud,
    actionable SchemaNotMigratedError; re-raises any OTHER
    OperationalError completely unchanged, so a genuinely different error
    (e.g. a lock timeout) is never misreported as a schema problem it
    isn't. Always raises -- never returns normally."""
    if f"no such table: {table}" in str(exc):
        raise SchemaNotMigratedError(
            f"Database schema is not up to date: table {table!r} does not exist. "
            "This means db.migrations.run_migrations() has never been run against "
            "this database file -- via the FastAPI startup event, or manually with "
            "`python db/database.py`. This is a deployment/bootstrap problem, not a "
            "transient error: do not retry without applying pending migrations first."
        ) from exc
    raise exc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format(dt: datetime) -> str:
    return dt.strftime(_TIMESTAMP_FORMAT)


def create_preview(
    *,
    patient_name: str,
    patient_phone: Optional[str],
    patient_id: Optional[str],
    clinic_id: str,
    dosha_imbalance: str,
    prescribed_herbs: list[dict],
    is_manual: bool,
    created_by_session: Optional[str],
    patient_pk: Optional[int] = None,
    encounter_pk: Optional[int] = None,
) -> str:
    """Creates a new PENDING preview and returns its preview_id. Used by
    both /prescribe/preview (is_manual=False) and /prescribe/manual-preview
    (is_manual=True) -- the only difference between them is this flag and
    where dosha_imbalance/prescribed_herbs came from (Gemini vs. the
    doctor's direct input); everything downstream treats both identically.

    preview_id is generated here (not by the caller) with
    secrets.token_hex(8) -- 64 bits of CSPRNG entropy, opaque, and with
    nothing about patient identity, clinic identity, or time encoded into
    it, per the locked contract's "unguessable lookup key" guarantee.
    """
    created_at = _now()
    expires_at = created_at + timedelta(seconds=PREVIEW_TTL_SECONDS)

    conn = get_connection()
    try:
        for attempt in range(_MAX_ID_GENERATION_ATTEMPTS):
            preview_id = secrets.token_hex(8)
            try:
                conn.execute(
                    """
                    INSERT INTO prescription_previews (
                        preview_id, clinic_id, patient_name, patient_phone, patient_id,
                        is_manual, ai_dosha_imbalance, ai_prescribed_herbs_json,
                        status, created_by_session, created_at, expires_at, patient_pk, encounter_pk
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
                    """,
                    (
                        preview_id, clinic_id, patient_name, patient_phone, patient_id,
                        1 if is_manual else 0, dosha_imbalance, json.dumps(prescribed_herbs),
                        created_by_session, _format(created_at), _format(expires_at), patient_pk,
                        encounter_pk,
                    ),
                )
                conn.commit()
                return preview_id
            except sqlite3.IntegrityError as e:
                conn.rollback()
                # Only a preview_id PRIMARY KEY collision is retryable. A
                # patient-link rejection (migration 009's trigger, or the
                # foreign key) is not a collision and must never be retried.
                if "encounter_link_invalid" in str(e):
                    raise EncounterLinkError() from e
                if "patient_clinic_mismatch" in str(e) or "FOREIGN KEY" in str(e):
                    raise PatientLinkError() from e
                continue  # preview_id collision -- vanishingly unlikely; try a fresh one
            except sqlite3.OperationalError as e:
                conn.rollback()
                _reraise_as_schema_error_if_missing_table(e)
                # Anything else (e.g. "database is locked") is NOT an id
                # collision: surface it instead of retrying the insert (each
                # retry would wait out the full busy timeout again).
                raise
        raise RuntimeError(
            f"Could not generate a unique preview_id after {_MAX_ID_GENERATION_ATTEMPTS} attempts."
        )
    finally:
        conn.close()


def get_pending_preview_for_confirm(conn: sqlite3.Connection, preview_id: str, requesting_clinic_id: str) -> sqlite3.Row:
    """Reads a preview row and classifies it for /prescribe/confirm, WITHOUT
    writing anything -- this is a pure read, deliberately. It must be called
    from inside the caller's own already-open transaction (main.py issues
    `BEGIN IMMEDIATE` before calling this), which is what makes the
    "concurrent confirm of the same preview_id" race safe: a second
    concurrent caller blocks on SQLite's own write lock until the first's
    transaction fully commits or rolls back, then re-reads this row fresh
    and correctly sees CONFIRMED (-> 409) rather than racing against a
    PENDING read that's about to become stale.

    `requesting_clinic_id` is REQUIRED (Phase A, A3 security fix) and MUST
    be the caller's own server-derived `auth.clinic_id` -- never anything
    client-supplied. It is checked immediately after the row is found and
    BEFORE the row's lifecycle status (PENDING / EXPIRED / CONFIRMED) is
    examined at all.

    Why that ordering matters -- this is a real bug found during Mac
    verification of A3, not a hypothetical: an earlier version of this
    function took no clinic argument and only classified status (not
    found / expired / already confirmed / pending), leaving the CALLER
    (backend/main.py) to check clinic ownership afterward, on the
    returned row. That worked correctly for a PENDING foreign-clinic
    preview (the not-found/expired branches below still fire and both map
    to 404), but a foreign-clinic preview that was already CONFIRMED
    raised PreviewAlreadyConfirmedError -- a status DISTINCT from "not
    found" -- before the caller ever got a row back to check clinic_id
    against. The result: a clinic-A caller confirming a clinic-B
    preview_id that happened to already be confirmed got HTTP 409 (with a
    message literally saying "has already been confirmed") instead of the
    404 every other cross-clinic case correctly returns -- a real
    cross-tenant information disclosure (existence + lifecycle state of a
    resource the caller has no authorization to know anything about).

    Fixed by moving clinic authorization to be the FIRST classification
    that runs against a found row, before status is examined at all: a
    row that exists but belongs to a different clinic raises
    PreviewNotFoundError -- the exact same exception, and therefore the
    exact same 404 with the exact same message, that a genuinely
    nonexistent preview_id raises -- regardless of whether that foreign
    row is PENDING, EXPIRED, or CONFIRMED. A caller outside the resource's
    clinic learns nothing about it: not that it exists, and not what
    state it's in. Only once clinic ownership is confirmed does status
    classification (expired / already confirmed / pending) run at all,
    which is also what preserves the SAME-clinic behaviors unchanged:
    a same-clinic already-CONFIRMED preview still raises
    PreviewAlreadyConfirmedError (-> 409, unchanged), a same-clinic
    expired one still raises PreviewExpiredError (-> 404, unchanged), and
    a same-clinic pending one is still returned normally.

    Raises PreviewNotFoundError (unknown preview_id, OR a real preview_id
    belonging to a different clinic than `requesting_clinic_id`),
    PreviewExpiredError, or PreviewAlreadyConfirmedError; returns the row
    unchanged otherwise. Expiry is a live comparison against the current
    time on every call -- never dependent on mark_stale_previews_expired()
    having already run.
    """
    try:
        row = conn.execute(
            "SELECT * FROM prescription_previews WHERE preview_id = ?", (preview_id,)
        ).fetchone()
    except sqlite3.OperationalError as e:
        _reraise_as_schema_error_if_missing_table(e)
        raise

    if row is None:
        raise PreviewNotFoundError(f"Preview {preview_id!r} not found.")

    # Clinic authorization FIRST -- before any lifecycle status is ever
    # inspected or allowed to influence which exception gets raised. See
    # this function's docstring above for the exact bug this ordering
    # fixes. A foreign-clinic row is indistinguishable, to the caller,
    # from a preview_id that was never issued at all.
    if row["clinic_id"] != requesting_clinic_id:
        raise PreviewNotFoundError(f"Preview {preview_id!r} not found.")

    if row["status"] == "CONFIRMED":
        raise PreviewAlreadyConfirmedError(f"Preview {preview_id!r} has already been confirmed.")

    if row["status"] == "EXPIRED" or row["expires_at"] <= _format(_now()):
        raise PreviewExpiredError(f"Preview {preview_id!r} has expired.")

    return row


def mark_confirmed(
    conn: sqlite3.Connection,
    preview_id: str,
    confirmed_dosha_imbalance: str,
    confirmed_prescribed_herbs: list[dict],
) -> None:
    """Flips a preview from PENDING to CONFIRMED, recording the doctor's
    final values. Must be called on the SAME connection and inside the
    SAME transaction as the prescription/inventory write it accompanies --
    see backend/main.py's /prescribe/confirm for why: the whole point of
    A1 is that this and the inventory deduction either both commit or
    neither does, closing the gap where a process crash between two
    separate commits could leave a confirmed prescription next to a
    still-PENDING (and therefore re-confirmable) preview.

    The `AND status = 'PENDING'` guard is a defensive sanity check, not
    the primary safeguard (get_pending_preview_for_confirm, called moments
    earlier in the same transaction, is) -- if it ever matches zero rows,
    something is genuinely wrong, so that is raised loudly rather than
    silently ignored.
    """
    cursor = conn.execute(
        """
        UPDATE prescription_previews
        SET status = 'CONFIRMED',
            confirmed_at = ?,
            confirmed_dosha_imbalance = ?,
            confirmed_prescribed_herbs_json = ?
        WHERE preview_id = ? AND status = 'PENDING'
        """,
        (_format(_now()), confirmed_dosha_imbalance, json.dumps(confirmed_prescribed_herbs), preview_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(
            f"mark_confirmed() expected to update exactly 1 PENDING row for preview_id={preview_id!r}, "
            f"updated {cursor.rowcount}. This should be unreachable given get_pending_preview_for_confirm() "
            "was called moments earlier in this same transaction -- treat this as a bug, not a normal outcome."
        )


def mark_stale_previews_expired(clinic_id: Optional[str] = None) -> int:
    """Best-effort housekeeping only: flips PENDING rows whose expiry has
    passed to EXPIRED, purely so admin/audit views read accurately without
    having to compute "is this actually still pending?" themselves.
    Correctness of expiry enforcement NEVER depends on this having run --
    see get_pending_preview_for_confirm's live check. Safe to call
    liberally; callers should treat a failure here as non-fatal (log and
    continue), never as a reason to fail the request that triggered it.
    """
    conn = get_connection()
    try:
        # Security review 2.1/2.2 follow-up: housekeeping must never wait for
        # the write lock. With the normal 15s busy timeout, a busy database
        # made every new preview wait out this sweep before its real insert
        # waited again (~31s). Fail fast instead; callers log and continue.
        conn.execute("PRAGMA busy_timeout = 0")
        try:
            if clinic_id is None:
                cursor = conn.execute(
                    "UPDATE prescription_previews SET status = 'EXPIRED' WHERE status = 'PENDING' AND expires_at <= ?",
                    (_format(_now()),),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE prescription_previews SET status = 'EXPIRED'
                    WHERE status = 'PENDING' AND expires_at <= ? AND clinic_id = ?
                    """,
                    (_format(_now()), clinic_id),
                )
        except sqlite3.OperationalError as e:
            conn.rollback()
            _reraise_as_schema_error_if_missing_table(e)
            raise
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()
