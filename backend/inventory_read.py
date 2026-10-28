"""
Read-only bulk stock reader for agents/a0's context-engineering contract.

agents/copilot.py's check_stock tool does one SELECT per herb, round-tripped
through Gemini's automatic function calling -- N herbs prescribed means N
tool-call round trips. The A0 architecture report's context-engineering
contract (Read-Only Inspection Report, section 6) calls instead for
"current stock for this clinic, one bulk read keyed on ctx.clinic_id,
replacing N check_stock round trips" -- assembled once into the prompt's
context rather than fetched mid-conversation.

Deliberately its own file, not an addition to backend/inventory_service.py:
that module is on the architecture report's frozen-files list (section 10)
specifically because it also contains deduct_and_prescribe, the write path
that must never become reachable from agents/. Keeping the bulk reader
here means agents/a0/tools.py's import surface can be checked statically
(tests/test_a0_no_write_imports.py) to prove nothing under agents/a0/ can
reach deduct_and_prescribe, even transitively -- which would not be
provable by inspection if this function lived inside inventory_service.py
next to it.
"""
from typing import Optional
from db.database import get_connection


def list_stock(clinic_id: str) -> dict[str, int]:
    """All current stock levels for one clinic: canonical herb name ->
    stock_grams. Read-only (a single SELECT, no write) and PII-free (herb
    names and integers only). Backward-compatible: unchanged return type."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT herb_name, stock_grams FROM inventory WHERE clinic_id = ?",
            (clinic_id,),
        ).fetchall()
        return {row["herb_name"]: row["stock_grams"] for row in rows}
    finally:
        conn.close()


def list_stock_detailed(clinic_id: str) -> list[dict]:
    """All current stock for one clinic: one dict per herb with batch,
    expiry, supplier and reorder level (migration 018). Read-only (a single
    SELECT, no write) and PII-free (herb names and integers only). Used by
    GET /inventory for the enriched clinic-like stock view."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT herb_name, stock_grams, batch_code, potency_score,
                   expiry_date, supplier, reorder_level_grams
            FROM inventory WHERE clinic_id = ?
            ORDER BY herb_name
            """,
            (clinic_id,),
        ).fetchall()
        return [
            {
                "herb_name": row["herb_name"],
                "stock_grams": row["stock_grams"],
                "batch_code": row["batch_code"],
                "potency_score": row["potency_score"],
                "expiry_date": row["expiry_date"],
                "supplier": row["supplier"],
                "reorder_level_grams": row["reorder_level_grams"],
            }
            for row in rows
        ]
    finally:
        conn.close()
