import sys
import os
import json
import sqlite3
from typing import Optional

# Add the project root to sys.path so we can import from db and backend
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import get_connection, DEFAULT_CLINIC_ID
from backend.herb_resolver import merge_herb_lines


class InsufficientStockError(Exception):
    pass


def get_stock(herb_name: str, clinic_id: str = DEFAULT_CLINIC_ID) -> int:
    """
    Read-only stock lookup for a single canonical herb name. Used by the
    agentic tool-calling loop (agents/copilot.py) so Gemini can check real,
    current stock before finalizing an answer -- never a decision point on
    its own, just information the AI can look up the way a careful
    assistant double-checks a fact before repeating it.
    """
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT stock_grams FROM inventory WHERE herb_name = ? AND clinic_id = ?",
            (herb_name, clinic_id),
        ).fetchone()
        return row["stock_grams"] if row else 0
    finally:
        conn.close()


def deduct_and_prescribe(
    patient_name: str,
    dosha: str,
    raw_herbs: list[dict],
    clinic_id: str = DEFAULT_CLINIC_ID,
    conn: Optional[sqlite3.Connection] = None,
    patient_pk: Optional[int] = None,
    encounter_pk: Optional[int] = None,
    proposal_source: str = "gemini",
) -> dict:
    """
    Deducts inventory for the prescribed herbs atomically and logs the
    prescription. raw_herbs should be a list of dicts:
    [{"name": "ashwagandha", "grams": 10}, ...]

    clinic_id scopes both the inventory read/write and the prescription
    record. It's a placeholder for real multi-tenancy today (one clinic),
    but every write path threads it through now so a second clinic doesn't
    require touching this function again later.

    Phase A (A1, persistent previews): `conn` is optional. Pass an
    already-open connection (with its own transaction already begun via
    `conn.execute("BEGIN IMMEDIATE")`) to make this deduction part of a
    LARGER atomic unit -- this is what backend/main.py's /prescribe/confirm
    does now, folding this deduction, the prescription insert, and the
    preview's PENDING -> CONFIRMED flip into one transaction, so a process
    crash between them is no longer possible. When `conn` is given, this
    function never calls commit(), rollback(), or close() on it -- the
    caller owns that transaction boundary completely, including on an
    InsufficientStockError, which is re-raised untouched for the caller to
    react to. Called with no `conn` (the default), this function behaves
    exactly as it always has: it opens its own connection and fully owns
    commit/rollback/close itself.

    `patient_pk` (Patient Registry Phase 1): the registered patient this
    prescription belongs to, copied by /prescribe/confirm from the stored
    preview -- never from the confirm request. None leaves the prescription
    unlinked. Migration 009's trigger rejects a patient of another clinic.
    Stock deduction is unaffected by it.

    `encounter_pk` (Encounter Phase 1B): likewise copied from the stored
    preview only. Migration 012's trigger rejects an encounter that is not
    OPEN or not this prescription's clinic + patient; None leaves the
    prescription outside any encounter.

    `proposal_source` (Migration 021): where the draft proposal came from --
    'local_extractor' (AI-1 local extractor), 'gemini' (Google Gemini), or
    'manual' (doctor-entered). Copied from the preview row at confirm time.
    """
    # Normalize and merge duplicate herbs (e.g. two 10g entries become one 20g
    # entry) with the shared merge (backend/herb_resolver.merge_herb_lines).
    merged_herbs = {h["name"]: h["grams"] for h in merge_herb_lines(raw_herbs)}

    owns_connection = conn is None
    if conn is None:
        conn = get_connection()
    try:
        if owns_connection:
            # Start an immediate transaction for atomicity. When a
            # connection was passed in, the caller has already done this.
            conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()

        # Atomic conditional update for each herb, scoped to this clinic
        for h_name, h_grams in merged_herbs.items():
            cursor.execute('''
                UPDATE inventory
                SET stock_grams = stock_grams - ?
                WHERE herb_name = ? AND clinic_id = ? AND stock_grams >= ?
            ''', (h_grams, h_name, clinic_id, h_grams))

            if cursor.rowcount == 0:
                if owns_connection:
                    conn.rollback()
                raise InsufficientStockError(f"Insufficient stock for herb '{h_name}'. Requested: {h_grams}g.")

        # Log the new prescription with canonical formulation JSON
        formulation_json = json.dumps([{"name": k, "grams": v} for k, v in merged_herbs.items()])
        description = f"Patient: {patient_name} | Dosha: {dosha} | Formulation: {formulation_json}"

        # dosha_imbalance (migration 016) is the structured dosha; with
        # prescription_items below it is the authoritative record, and
        # `description` is kept only for backward compatibility.
        cursor.execute('''
            INSERT INTO prescriptions (clinic_id, patient_name, description, status, patient_pk, encounter_pk, dosha_imbalance, proposal_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (clinic_id, patient_name, description, "Active", patient_pk, encounter_pk, dosha, proposal_source))

        prescription_id = cursor.lastrowid

        # Structured items (migration 015): one row per canonical herb, in
        # prescription order, in this same transaction -- they commit or roll
        # back together with the deduction and the prescription row.
        cursor.executemany('''
            INSERT INTO prescription_items (prescription_id, clinic_id, line_no, herb_name, grams)
            VALUES (?, ?, ?, ?, ?)
        ''', [
            (prescription_id, clinic_id, line_no, h_name, h_grams)
            for line_no, (h_name, h_grams) in enumerate(merged_herbs.items(), start=1)
        ])

        # Fetch the auto-generated timestamp
        cursor.execute('SELECT created_at FROM prescriptions WHERE id = ?', (prescription_id,))
        created_at = cursor.fetchone()[0]

        if owns_connection:
            conn.commit()

        return {
            "prescription_id": prescription_id,
            "patient_name": patient_name,
            "dosha_imbalance": dosha,
            "prescribed_herbs": [{"name": k, "grams": v} for k, v in merged_herbs.items()],
            "created_at": created_at,
            "clinic_id": clinic_id,
            "patient_pk": patient_pk,
            "encounter_pk": encounter_pk,
            "proposal_source": proposal_source,
        }
    except Exception as e:
        if owns_connection:
            conn.rollback()
        raise e
    finally:
        if owns_connection:
            conn.close()
