"""
Clinic-scoped encounters (Encounter foundation Phase 1A, migration 010).

An encounter is one consultation episode for one registered patient:
OPEN -> CLOSED, with CLOSED terminal. At most one OPEN encounter per patient.

clinic_id and created_by_user_id are always the caller's AUTHENTICATED
session values (backend/main.py passes auth.clinic_id / auth.user_id); the
patient is always resolved within that clinic before anything is written.
Every read here is scoped by clinic_id, so an encounter or patient of
another clinic is indistinguishable from one that does not exist. The same
rules are enforced again at write time by migration 010's triggers.

Each open/close writes its audit event (ENCOUNTER_OPENED / ENCOUNTER_CLOSED,
backend/audit_store.py) inside the SAME BEGIN IMMEDIATE transaction as the
encounter change, so the two always commit or roll back together, and the
per-clinic hash-chain read is serialized exactly as /prescribe/confirm's is.
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from backend.audit_store import (
    AUDIT_EVENT_TYPE_ENCOUNTER_CLOSED,
    AUDIT_EVENT_TYPE_ENCOUNTER_OPENED,
    build_encounter_event_data,
    create_audit_event,
)
from db.database import get_connection

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"


class OpenEncounterExistsError(Exception):
    """The patient already has an OPEN encounter (one at a time)."""


class EncounterClinicMismatchError(Exception):
    """Migration 010's write-time trigger rejected the insert (patient or
    creator not in the encounter's clinic). The caller checks the patient
    first, so reaching this means the database backstop caught something
    the application check did not."""


class EncounterNotFoundError(Exception):
    """No encounter with this key in the caller's clinic."""


class EncounterAlreadyClosedError(Exception):
    """The encounter is CLOSED; CLOSED is terminal."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "encounter_pk": row["encounter_pk"],
        "patient_pk": row["patient_pk"],
        "created_by_user_id": row["created_by_user_id"],
        "status": row["status"],
        "created_at": row["created_at"],
        "closed_at": row["closed_at"],
        "encounter_ref": row["encounter_ref"],
    }


def open_encounter(*, clinic_id: str, patient_pk: int, created_by_user_id: int) -> dict:
    """Opens a new encounter for `patient_pk` in `clinic_id` and audits it
    (ENCOUNTER_OPENED, actor = the creating user), atomically. The caller
    must already have verified the patient belongs to `clinic_id`."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            try:
                # Random, non-sequential reference used in audit events
                # (migration 013): unlike the global encounter_pk it reveals
                # nothing about other clinics' encounter volume.
                encounter_ref = secrets.token_hex(8)
                cursor = conn.execute(
                    """
                    INSERT INTO encounters (clinic_id, patient_pk, created_by_user_id, status, created_at, encounter_ref)
                    VALUES (?, ?, ?, 'OPEN', ?, ?)
                    """,
                    (clinic_id, patient_pk, created_by_user_id, _now(), encounter_ref),
                )
            except sqlite3.IntegrityError as e:
                message = str(e)
                if "UNIQUE" in message and "encounters.patient_pk" in message:
                    raise OpenEncounterExistsError() from e
                if "encounter_clinic_mismatch" in message or "FOREIGN KEY" in message:
                    raise EncounterClinicMismatchError() from e
                raise
            encounter_pk = cursor.lastrowid
            create_audit_event(
                conn,
                clinic_id=clinic_id,
                actor_user_id=created_by_user_id,
                event_type=AUDIT_EVENT_TYPE_ENCOUNTER_OPENED,
                prescription_id=None,
                preview_id=None,
                event_data=build_encounter_event_data(encounter_ref),
            )
            row = conn.execute("SELECT * FROM encounters WHERE encounter_pk = ?", (encounter_pk,)).fetchone()
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_encounter_for_clinic(encounter_pk: int, clinic_id: str) -> Optional[dict]:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM encounters WHERE encounter_pk = ? AND clinic_id = ?",
            (encounter_pk, clinic_id),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_encounters_for_patient(clinic_id: str, patient_pk: int, limit: int, offset: int,
                                conn: Optional[sqlite3.Connection] = None) -> tuple[list[dict], bool]:
    """Newest first (created_at, then encounter_pk as a same-second
    tiebreak). Returns at most `limit` rows from `offset`, plus has_more.
    `conn` is only for backend.patient_store.read_snapshot(): the read then
    runs on the caller's connection and transaction, never closed here."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT * FROM encounters
            WHERE clinic_id = ? AND patient_pk = ?
            ORDER BY created_at DESC, encounter_pk DESC
            LIMIT ? OFFSET ?
            """,
            (clinic_id, patient_pk, limit + 1, offset),
        ).fetchall()
    finally:
        if own:
            conn.close()
    return [_row_to_dict(r) for r in rows[:limit]], len(rows) > limit


def close_encounter(encounter_pk: int, clinic_id: str, actor_user_id: int) -> dict:
    """OPEN -> CLOSED, audited (ENCOUNTER_CLOSED, actor = the closing user)
    in the same transaction. Raises EncounterNotFoundError for a missing or
    foreign encounter and EncounterAlreadyClosedError if it is already
    CLOSED -- in both cases nothing is written. BEGIN IMMEDIATE plus the
    conditional UPDATE make concurrent closes resolve to exactly one success
    and exactly one audit event."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                UPDATE encounters SET status = 'CLOSED', closed_at = ?
                WHERE encounter_pk = ? AND clinic_id = ? AND status = 'OPEN'
                """,
                (_now(), encounter_pk, clinic_id),
            )
            row = conn.execute(
                "SELECT * FROM encounters WHERE encounter_pk = ? AND clinic_id = ?",
                (encounter_pk, clinic_id),
            ).fetchone()
            if row is None:
                raise EncounterNotFoundError()
            if cursor.rowcount == 0:
                raise EncounterAlreadyClosedError()
            create_audit_event(
                conn,
                clinic_id=clinic_id,
                actor_user_id=actor_user_id,
                event_type=AUDIT_EVENT_TYPE_ENCOUNTER_CLOSED,
                prescription_id=None,
                preview_id=None,
                event_data=build_encounter_event_data(row["encounter_ref"]),
            )
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return _row_to_dict(row)
    finally:
        conn.close()
