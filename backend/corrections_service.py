"""
The correction-logging "data flywheel".

Every call to /prescribe/confirm -- whether the doctor accepted the AI's
proposal untouched or corrected it -- writes exactly one row here recording
what the AI proposed versus what was actually confirmed. This is the single
most defensible, genuinely time-gated asset this project accumulates: it
cannot be shortcut by a competitor copying a demo, only by running real
usage over real time.

Logging here must never be able to block or roll back a real prescription.
By the time log_correction() is called, deduct_and_prescribe() has already
committed -- a failure in this module is a lost data point, not a lost
prescription. Callers should catch and log any exception from this
function rather than let it propagate.
"""
import json

from db.database import get_connection, DEFAULT_CLINIC_ID


def log_correction(
    *,
    prescription_id: int,
    ai_dosha_imbalance: str,
    ai_prescribed_herbs: list[dict],
    confirmed_dosha_imbalance: str,
    confirmed_prescribed_herbs: list[dict],
    clinic_id: str = DEFAULT_CLINIC_ID,
) -> None:
    was_changed = (
        ai_dosha_imbalance != confirmed_dosha_imbalance
        or ai_prescribed_herbs != confirmed_prescribed_herbs
    )

    conn = get_connection()
    try:
        conn.execute(
            '''
            INSERT INTO corrections (
                prescription_id, clinic_id,
                ai_dosha_imbalance, ai_prescribed_herbs_json,
                confirmed_dosha_imbalance, confirmed_prescribed_herbs_json,
                was_changed
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                prescription_id,
                clinic_id,
                ai_dosha_imbalance,
                json.dumps(ai_prescribed_herbs),
                confirmed_dosha_imbalance,
                json.dumps(confirmed_prescribed_herbs),
                1 if was_changed else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_corrections_for_clinic(clinic_id: str = DEFAULT_CLINIC_ID) -> list[dict]:
    """Read-only: every correction row for one clinic, newest first. Powers
    GET /audit/export -- the exportable proof that every dispensed
    prescription was explicitly confirmed, and of exactly what (if
    anything) the doctor changed from the AI's proposal.

    This is a pure read path: it never writes, and it never touches
    patient identity, because this table never stored any (see the module
    docstring above). Herb lists come back already decoded from their
    stored JSON form, ready to hand straight to the AuditRecord schema.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            '''
            SELECT prescription_id, clinic_id, ai_dosha_imbalance, ai_prescribed_herbs_json,
                   confirmed_dosha_imbalance, confirmed_prescribed_herbs_json, was_changed, created_at
            FROM corrections
            WHERE clinic_id = ?
            ORDER BY created_at DESC
            ''',
            (clinic_id,),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "prescription_id": row["prescription_id"],
            "clinic_id": row["clinic_id"],
            "ai_dosha_imbalance": row["ai_dosha_imbalance"],
            "ai_prescribed_herbs": json.loads(row["ai_prescribed_herbs_json"]),
            "confirmed_dosha_imbalance": row["confirmed_dosha_imbalance"],
            "confirmed_prescribed_herbs": json.loads(row["confirmed_prescribed_herbs_json"]),
            "was_changed": bool(row["was_changed"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]
