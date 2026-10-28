"""
Clinic-scoped patient registry (Patient Registry Phase 1, migration 009).

A patient's identity is (clinic_id, clinic_patient_id): unique per clinic,
immutable once created. display_name and phone are attributes. clinic_id is
always the caller's AUTHENTICATED clinic (backend/main.py passes
auth.clinic_id) -- nothing in this module accepts a clinic from a request.

Every read here is scoped by clinic_id, so a patient belonging to another
clinic is indistinguishable from one that does not exist. The same rule is
also enforced at write time by migration 009's triggers, independently of
this code.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from db.database import get_connection


class DuplicatePatientError(Exception):
    """(clinic_id, clinic_patient_id) is already registered in this clinic."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "patient_pk": row["patient_pk"],
        "clinic_patient_id": row["clinic_patient_id"],
        "display_name": row["display_name"],
        "phone": row["phone"],
        "created_at": row["created_at"],
        # Migration 018: clinic-enriched demographics.
        "date_of_birth": row["date_of_birth"],
        "gender": row["gender"],
        "address_line": row["address_line"],
        "city": row["city"],
        "state": row["state"],
        "pincode": row["pincode"],
        "prakriti": row["prakriti"],
        "allergies": row["allergies"],
        "registered_at": row["registered_at"],
    }


def create_patient(
    *,
    clinic_id: str,
    clinic_patient_id: str,
    display_name: str,
    phone: Optional[str],
    created_by_user_id: int,
    # Migration 018: clinic-enriched demographics (all optional).
    date_of_birth: Optional[str] = None,
    gender: Optional[str] = None,
    address_line: Optional[str] = None,
    city: Optional[str] = None,
    state: Optional[str] = None,
    pincode: Optional[str] = None,
    prakriti: Optional[str] = None,
    allergies: Optional[str] = None,
    registered_at: Optional[str] = None,
) -> dict:
    """Registers a patient in `clinic_id`. Raises DuplicatePatientError if
    that clinic already has this clinic_patient_id. Values arrive already
    trimmed and length-checked by backend/schemas.py."""
    conn = get_connection()
    try:
        try:
            cursor = conn.execute(
                """
                INSERT INTO patients (clinic_id, clinic_patient_id, display_name, phone, created_by_user_id, created_at,
                                      date_of_birth, gender, address_line, city, state, pincode, prakriti, allergies,
                                      registered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clinic_id, clinic_patient_id, display_name, phone, created_by_user_id, _now(),
                    date_of_birth, gender, address_line, city, state, pincode, prakriti, allergies, registered_at,
                ),
            )
        except sqlite3.IntegrityError as e:
            conn.rollback()
            if "UNIQUE" in str(e):
                raise DuplicatePatientError() from e
            raise
        conn.commit()
        row = conn.execute("SELECT * FROM patients WHERE patient_pk = ?", (cursor.lastrowid,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def get_patient_for_clinic(patient_pk: int, clinic_id: str, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """The patient with this patient_pk IN THIS CLINIC, or None -- a
    patient from another clinic is reported exactly like a missing one.
    `conn` is only for read_snapshot(): the read then runs on the caller's
    connection (and transaction), which this function never closes."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM patients WHERE patient_pk = ? AND clinic_id = ?",
            (patient_pk, clinic_id),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        if own:
            conn.close()


class SnapshotReadError(RuntimeError):
    """A consistent read transaction could not be opened or completed (database locked, I/O error, ...). Nothing partial is ever returned."""


@contextmanager
def read_snapshot() -> Iterator[sqlite3.Connection]:
    """ONE connection and ONE read-only transaction for a multi-statement read that must see a single point in time (AI-2 snapshot).

    The transaction is deferred, so SQLite fixes the read snapshot at the FIRST statement run on it: the caller must make its first read on
    the yielded connection. Under WAL (db/database.get_connection) every later read on that connection sees the database exactly as of that
    first read -- a prescription (with its items) or encounter committed meanwhile by another connection or process is excluded in full,
    and a writer is never blocked. PRAGMA query_only makes any accidental write fail. Any sqlite3 error becomes SnapshotReadError."""
    conn = None
    try:
        conn = get_connection()
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        yield conn
    except sqlite3.Error as exc:
        raise SnapshotReadError("the patient record could not be read consistently") from exc
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            conn.close()


# --- Phase 2A: search + history (read-only) -----------------------------------

def _like_contains(text: str) -> str:
    """A LIKE pattern matching `text` anywhere, with LIKE's own wildcards
    (% and _) escaped so a query can never widen its own match."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_patients(clinic_id: str, query: str, limit: int) -> tuple[list[dict], bool]:
    """Patients of `clinic_id` whose clinic_patient_id or display_name
    contains `query` (case-insensitive for ASCII). Ranked: exact ID match,
    then ID prefix, then everything else; ties by name, then patient_pk.
    Returns at most `limit` rows plus whether more matched. Only the fields
    needed to pick a patient -- phone is deliberately not returned."""
    pattern = _like_contains(query)
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT patient_pk, clinic_patient_id, display_name
            FROM patients
            WHERE clinic_id = ?
              AND (clinic_patient_id LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\')
            ORDER BY
              CASE
                WHEN lower(clinic_patient_id) = lower(?) THEN 0
                WHEN lower(clinic_patient_id) LIKE lower(?) || '%' ESCAPE '\\' THEN 1
                ELSE 2
              END,
              lower(display_name), patient_pk
            LIMIT ?
            """,
            (clinic_id, pattern, pattern, query, pattern[1:-1], limit + 1),
        ).fetchall()
    finally:
        conn.close()
    results = [dict(r) for r in rows[:limit]]
    return results, len(rows) > limit


def _parse_prescription_description(patient_name: str, description: str) -> tuple[Optional[str], Optional[list]]:
    """Reads dosha + dispensed formulation back out of a prescription row.
    COMPATIBILITY FALLBACK ONLY: used for a field the prescription has no
    structured value for (confirmed before migration 015/016).

    backend/inventory_service.deduct_and_prescribe writes exactly
    f"Patient: {patient_name} | Dosha: {dosha} | Formulation: {json}" using
    the same patient_name it stores in the row's own column, so the prefix
    is known exactly; the formulation JSON (canonical herb names) never
    contains " | Formulation: ", so one split from the right is unambiguous.
    Returns (None, None) if a row does not have that shape, rather than
    guessing."""
    import json

    prefix = f"Patient: {patient_name} | Dosha: "
    if not description.startswith(prefix):
        return None, None
    dosha, sep, formulation = description[len(prefix):].rpartition(" | Formulation: ")
    if not sep:
        return None, None
    try:
        herbs = json.loads(formulation)
    except ValueError:
        return None, None
    if not isinstance(herbs, list) or not all(
        isinstance(h, dict) and isinstance(h.get("name"), str) and isinstance(h.get("grams"), int) for h in herbs
    ):
        return None, None
    return dosha, [{"name": h["name"], "grams": h["grams"]} for h in herbs]


def get_prescription_history(clinic_id: str, patient_pk: int, limit: int, offset: int,
                             conn: Optional[sqlite3.Connection] = None) -> tuple[list[dict], bool]:
    """Confirmed prescriptions linked to this patient in this clinic, newest
    first (created_at, then id as a tiebreak within the same second).
    Unlinked prescriptions (patient_pk NULL) never match. Returns at most
    `limit` rows starting at `offset`, plus whether more exist.
    `conn` is only for read_snapshot(): every statement here then runs on the
    caller's connection and transaction, which this function never closes."""
    own = conn is None
    if own:
        conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT id, created_at, status, patient_name, description, dosha_imbalance,
                   payment_status, payment_amount_cents, payment_method, payment_reference,
                   dispensed_at, dispensed_by_user_id, invoice_number
            FROM prescriptions
            WHERE clinic_id = ? AND patient_pk = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (clinic_id, patient_pk, limit + 1, offset),
        ).fetchall()
        # Structured data is authoritative: prescription_items (migration
        # 015) for herbs and prescriptions.dosha_imbalance (migration 016)
        # for dosha. Prescriptions confirmed before those migrations have
        # neither, and fall back to parsing `description`, field by field.
        page = rows[:limit]
        items_by_id: dict[int, list] = {}
        if page:
            placeholders = ",".join("?" * len(page))
            for item in conn.execute(
                f"""
                SELECT prescription_id, herb_name, grams FROM prescription_items
                WHERE clinic_id = ? AND prescription_id IN ({placeholders})
                ORDER BY prescription_id, line_no
                """,
                (clinic_id, *[row["id"] for row in page]),
            ):
                items_by_id.setdefault(item["prescription_id"], []).append(
                    {"name": item["herb_name"], "grams": item["grams"]})
    finally:
        if own:
            conn.close()
    history = []
    for row in page:
        dosha, herbs = row["dosha_imbalance"], items_by_id.get(row["id"])
        if dosha is None or herbs is None:
            legacy_dosha, legacy_herbs = _parse_prescription_description(row["patient_name"], row["description"])
            dosha = dosha if dosha is not None else legacy_dosha
            herbs = herbs if herbs is not None else legacy_herbs
        history.append({
            "prescription_id": row["id"],
            "created_at": str(row["created_at"]),
            "status": row["status"],
            "dosha_imbalance": dosha,
            "prescribed_herbs": herbs,
            # Migration 018: dispensing + payment fields.
            "payment_status": row["payment_status"],
            "payment_amount_cents": row["payment_amount_cents"],
            "payment_method": row["payment_method"],
            "payment_reference": row["payment_reference"],
            "dispensed_at": row["dispensed_at"],
            "dispensed_by_user_id": row["dispensed_by_user_id"],
            "invoice_number": row["invoice_number"],
        })
    return history, len(rows) > limit
