"""
Idempotency-key reservation and replay (Phase B, A4 -- exactly-once
confirmation).

Backs the `Idempotency-Key` header on POST /prescribe/confirm. This module
owns exactly two operations, both of which MUST be called from inside the
caller's own already-open `BEGIN IMMEDIATE` transaction (the same one
backend/main.py's /prescribe/confirm already opens for A1's atomicity
guarantee) -- this module never opens, commits, or rolls back a
transaction itself, the same convention backend/preview_store.py's
get_pending_preview_for_confirm() / mark_confirmed() already use:

  1. reserve_idempotent_operation() -- called BEFORE any business logic.
     Either claims a fresh reservation (caller proceeds normally) or finds
     an existing, fully-populated row and returns it for replay (caller
     must NOT re-run business logic, must NOT log a correction again --
     see backend/main.py's call site for how it short-circuits).

  2. complete_idempotent_reservation() -- called AFTER business logic
     succeeds and the final response is known, BEFORE commit. Fills in the
     two columns left NULL by the reservation INSERT.

Two-state model, deliberately: a key is either ABSENT (never durably
committed -- retryable, as if never attempted) or SUCCEEDED (a row exists
with both response_status_code and response_body_json populated). There is
no durable IN_PROGRESS or FAILED state, because reservation and business
logic share one atomic transaction -- any failure between the two
operations above rolls back the whole transaction, reservation included.
See migrations/006_idempotency_keys.sql for the schema-level invariant
this enforces and why NULL response columns can never be observed outside
their own (necessarily still-open) transaction.

Scope: composite key (user_id, operation, idempotency_key), where user_id
is ALWAYS the caller's own server-derived auth.user_id -- never anything
client-supplied. This is what makes cross-user information leakage
structurally impossible here, not just policy: two different users
choosing the identical key string can never collide (different primary
keys), so a second user presenting a key some other user already used
simply falls into the "no row -- proceed as fresh" branch, indistinguishable
from a key nobody has ever used. See reserve_idempotent_operation()'s
docstring for the clinic_id re-check, which closes the one remaining
(currently unreachable) edge case: the SAME user's clinic affiliation
changing between the original request and a later replay.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

OPERATION_PRESCRIBE_CONFIRM = "prescribe_confirm"

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class IdempotencyConflictError(Exception):
    """The same (user_id, operation, idempotency_key) was already used to
    reserve a SUCCEEDED result for a DIFFERENT request (Case C: the
    fingerprint of preview_id/dosha_imbalance/prescribed_herbs does not
    match). Maps to 409 in backend/main.py. This is a same-user,
    same-key, different-payload conflict -- never a cross-tenant question;
    it is entirely expected and safe to surface to this exact caller,
    since it is their own key being reused against different content."""


class IdempotencyClinicMismatchError(Exception):
    """The same (user_id, operation, idempotency_key) has a stored
    clinic_id that no longer matches the caller's CURRENT, live
    auth.clinic_id. Maps to 409 in backend/main.py.

    This can only happen for the SAME user_id (the composite key is
    user_id-scoped -- see this module's docstring), so it is never a
    cross-user or cross-clinic leak in the ordinary sense. It means this
    exact user's clinic affiliation changed between the original request
    and this one. No endpoint in this project reassigns users.clinic_id
    today, so this branch is provably unreachable in the current system --
    it exists as defense-in-depth for a capability that doesn't exist yet,
    per the approved A4 design's explicit instruction not to assume that
    away. Deliberately NOT treated as either a normal replay (which could
    leak the old clinic's response data to a caller now live in a
    different clinic) or a silent fresh attempt (which would let a stale
    key be reused across a changed authorization context) -- it fails
    closed instead.
    """


@dataclass(frozen=True)
class IdempotencyReservation:
    """Result of reserve_idempotent_operation().

    is_replay=False: this transaction now owns a fresh row (response
    columns NULL). The caller must run its business logic, then call
    complete_idempotent_reservation() with the real response before
    commit -- and must NOT return early.

    is_replay=True: an existing, fully-populated (SUCCEEDED) row was
    found. response_status_code/response_body_json are the caller's
    original response, to be returned UNCHANGED. The caller must run NO
    business logic and must NOT call complete_idempotent_reservation()
    again for this key.
    """
    is_replay: bool
    response_status_code: Optional[int] = None
    response_body_json: Optional[str] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format(dt: datetime) -> str:
    return dt.strftime(_TIMESTAMP_FORMAT)


def compute_request_fingerprint(
    *,
    preview_id: str,
    dosha_imbalance: Optional[str],
    prescribed_herbs: Optional[list[dict]],
) -> str:
    """Deterministic fingerprint of exactly the ConfirmRequest fields that
    determine the operation's clinical CONTENT -- per the approved A4
    design:

      - preview_id: binds the operation to one specific preview. Reusing a
        key against a different preview is a different logical operation,
        never a replay of the first.
      - dosha_imbalance / prescribed_herbs: the doctor's optional
        corrections at confirm time -- these are exactly what gets
        committed, so they are exactly what "is this the same request"
        must be judged on. Taken as-sent (including None, meaning "use the
        AI's own proposal unchanged") -- NOT the resolved/normalized final
        values, since two requests that resolve to the same final herbs
        via different raw input are still, at the API-contract level, the
        same request only if they SENT the same thing.

    Deliberately excludes clinic_id/user_id (already the reservation's own
    scoping columns -- see this module's docstring) and any request
    metadata. sort_keys + compact separators make this independent of
    field ordering or incidental whitespace in how the caller happened to
    construct the value being hashed.
    """
    canonical = json.dumps(
        {
            "preview_id": preview_id,
            "dosha_imbalance": dosha_imbalance,
            "prescribed_herbs": prescribed_herbs,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reserve_idempotent_operation(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    operation: str,
    idempotency_key: str,
    clinic_id: str,
    request_fingerprint: str,
) -> IdempotencyReservation:
    """Claims a fresh reservation, or returns an existing SUCCEEDED result
    for replay. MUST be called inside the caller's own already-open
    `BEGIN IMMEDIATE` transaction, before any business logic runs.

    `user_id` and `clinic_id` MUST be the caller's own server-derived
    auth.user_id / auth.clinic_id -- never anything client-supplied. This
    is what makes cross-user replay structurally impossible: the INSERT's
    primary key includes user_id, so a different user presenting the
    identical key string can never collide with this row at all -- their
    call takes the fresh-reservation branch below, not this one.

    Raises IdempotencyConflictError (-> 409) if a row already exists for
    this exact (user_id, operation, idempotency_key) with a DIFFERENT
    request_fingerprint (the doctor edited the request and the frontend
    incorrectly reused the old key -- see Feature 1's design for why the
    frontend should prevent this, and why the backend must reject it
    regardless of whether it does).

    Raises IdempotencyClinicMismatchError (-> 409) if the existing row's
    stored clinic_id no longer matches the live `clinic_id` passed in --
    see that exception's own docstring.
    """
    try:
        conn.execute(
            """
            INSERT INTO idempotency_keys (
                user_id, operation, idempotency_key, clinic_id,
                request_fingerprint, response_status_code, response_body_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (user_id, operation, idempotency_key, clinic_id, request_fingerprint, _format(_now())),
        )
        return IdempotencyReservation(is_replay=False)
    except sqlite3.IntegrityError:
        # A row already exists for this exact (user_id, operation, key).
        # Still inside the same BEGIN IMMEDIATE transaction, so this SELECT
        # is guaranteed to see it.
        row = conn.execute(
            """
            SELECT clinic_id, request_fingerprint, response_status_code, response_body_json
            FROM idempotency_keys
            WHERE user_id = ? AND operation = ? AND idempotency_key = ?
            """,
            (user_id, operation, idempotency_key),
        ).fetchone()

        if row is None:
            # Unreachable under SQLite's isolation guarantees: the
            # IntegrityError above means a row exists, and BEGIN IMMEDIATE
            # holds the write lock for the rest of this transaction, so no
            # other connection can delete it out from under us. A real hit
            # here means the invariant this module depends on has been
            # violated elsewhere -- fail loudly, not silently.
            raise RuntimeError(
                "idempotency_keys row vanished between the INSERT conflict and the "
                "follow-up SELECT inside the same transaction -- this should be unreachable."
            )

        if row["clinic_id"] != clinic_id:
            raise IdempotencyClinicMismatchError(
                f"Idempotency key {idempotency_key!r} is no longer valid for the current "
                "authorization context."
            )

        if row["request_fingerprint"] != request_fingerprint:
            raise IdempotencyConflictError(
                f"Idempotency key {idempotency_key!r} was already used with a different request."
            )

        if row["response_status_code"] is None or row["response_body_json"] is None:
            # Structurally unreachable -- see migrations/006's docstring:
            # BEGIN IMMEDIATE means a NULL-response row can never be
            # visible to a transaction other than the one that inserted
            # it, and that transaction is still holding the write lock we
            # ourselves just needed in order to even reach this SELECT.
            # Treated as a hard internal error, never as "retry me".
            raise RuntimeError(
                f"idempotency_keys row for key={idempotency_key!r} has a NULL response field "
                "but was visible to a different transaction -- this violates the schema's "
                "documented invariant (see migrations/006_idempotency_keys.sql) and indicates "
                "a serious bug, not a race worth retrying."
            )

        return IdempotencyReservation(
            is_replay=True,
            response_status_code=row["response_status_code"],
            response_body_json=row["response_body_json"],
        )


def complete_idempotent_reservation(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    operation: str,
    idempotency_key: str,
    response_status_code: int,
    response_body_json: str,
) -> None:
    """Fills in the two columns a fresh reservation left NULL, once the
    real response is known. MUST be called inside the SAME transaction as
    the matching reserve_idempotent_operation() call (is_replay=False),
    BEFORE that transaction commits, and must never be called after a
    replay (is_replay=True) -- there is nothing to complete in that case.

    The `AND response_status_code IS NULL AND response_body_json IS NULL`
    guard is a defensive sanity check, not the primary safeguard (the
    reservation INSERT moments earlier, in this same transaction, is) --
    mirrors backend/preview_store.py's mark_confirmed() exactly: if this
    ever matches zero rows, something is genuinely wrong, so that is
    raised loudly rather than silently ignored.
    """
    cursor = conn.execute(
        """
        UPDATE idempotency_keys
        SET response_status_code = ?, response_body_json = ?, completed_at = ?
        WHERE user_id = ? AND operation = ? AND idempotency_key = ?
          AND response_status_code IS NULL AND response_body_json IS NULL
        """,
        (
            response_status_code, response_body_json, _format(_now()),
            user_id, operation, idempotency_key,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(
            f"complete_idempotent_reservation() expected to update exactly 1 freshly-reserved "
            f"row for key={idempotency_key!r}, updated {cursor.rowcount}. This should be "
            "unreachable given reserve_idempotent_operation() was called moments earlier in "
            "this same transaction -- treat this as a bug, not a normal outcome."
        )
