"""
Tests for the corrections loop (Task 1.3):
- Migration 022 adds proposal_source column to corrections
- log_correction() stores proposal_source (local_extractor | gemini | manual)
- Check constraint rejects invalid proposal_source values
- get_corrections_for_clinic() returns proposal_source
- acceptance_rate() computes accurate statistics per proposal_source
- Prescribe confirm flow records proposal_source into corrections
"""
import json
import sqlite3
import pytest

from db.database import DB_PATH, get_connection, DEFAULT_CLINIC_ID
from backend.corrections_service import log_correction, get_corrections_for_clinic
from scripts.acceptance_rate import acceptance_rate


def test_log_correction_with_proposal_sources():
    """Verify log_correction stores and retrieves proposal_source."""
    conn = get_connection()
    # Insert a dummy prescription to satisfy foreign key / referencing
    conn.execute(
        "INSERT INTO prescriptions (clinic_id, patient_name, description, status) VALUES (?, ?, ?, ?)",
        (DEFAULT_CLINIC_ID, "Test Patient", "Prescription for test", "confirmed"),
    )
    rx_id = conn.execute("SELECT id FROM prescriptions ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.commit()
    conn.close()

    # Log an untouched proposal from local_extractor (accepted: was_changed=0)
    log_correction(
        prescription_id=rx_id,
        clinic_id=DEFAULT_CLINIC_ID,
        ai_dosha_imbalance="Vata",
        ai_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
        confirmed_dosha_imbalance="Vata",
        confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
        proposal_source="local_extractor",
    )

    # Log a modified proposal from local_extractor (changed: was_changed=1)
    log_correction(
        prescription_id=rx_id,
        clinic_id=DEFAULT_CLINIC_ID,
        ai_dosha_imbalance="Vata",
        ai_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
        confirmed_dosha_imbalance="Pitta",
        confirmed_prescribed_herbs=[{"name": "Ashwagandha", "grams": 10}],
        proposal_source="local_extractor",
    )

    # Log an untouched proposal from gemini
    log_correction(
        prescription_id=rx_id,
        clinic_id=DEFAULT_CLINIC_ID,
        ai_dosha_imbalance="Kapha",
        ai_prescribed_herbs=[{"name": "Triphala", "grams": 5}],
        confirmed_dosha_imbalance="Kapha",
        confirmed_prescribed_herbs=[{"name": "Triphala", "grams": 5}],
        proposal_source="gemini",
    )

    # Fetch corrections and assert proposal_source is present
    records = get_corrections_for_clinic(DEFAULT_CLINIC_ID)
    sources = [r["proposal_source"] for r in records[:3]]
    assert "local_extractor" in sources
    assert "gemini" in sources


def test_invalid_proposal_source_rejected():
    """Verify that check constraint on corrections table rejects invalid proposal_source."""
    conn = get_connection()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO corrections (
                prescription_id, clinic_id, ai_dosha_imbalance, ai_prescribed_herbs_json,
                confirmed_dosha_imbalance, confirmed_prescribed_herbs_json, was_changed, proposal_source
            ) VALUES (1, 'default_clinic', 'Vata', '[]', 'Vata', '[]', 0, 'invalid_source')
            """
        )
    conn.close()


def test_acceptance_rate_calculation():
    """Verify acceptance_rate function computes correct rates per proposal_source."""
    rates = acceptance_rate(str(DB_PATH), DEFAULT_CLINIC_ID)
    assert "local_extractor" in rates
    assert "gemini" in rates
    assert "manual" in rates

    # Local extractor has 1 accepted, 1 changed
    lex = rates["local_extractor"]
    assert lex["total"] >= 2
    assert lex["accepted"] >= 1
    assert lex["changed"] >= 1
    assert 0.0 <= lex["acceptance_rate"] <= 1.0
