"""
Migration 021 -- proposal_source on prescriptions.

Adds a `proposal_source` column to `prescriptions` so every confirmed
prescription records where its proposal came from:

  - `local_extractor` -- AI-1 local extractor (T1 tagger + T2 assembly +
    T3 verifier, no LLM, no network)
  - `gemini`          -- Google Gemini via agents/copilot.parse_clinical_notes
  - `manual`          -- doctor-entered via /prescribe/manual-preview

Copied from the preview row at confirm time (backend/main.py passes
entry["proposal_source"] through deduct_and_prescribe). Existing rows keep
the default 'gemini' (the only AI path that existed before this migration);
new manual previews set 'manual' explicitly at confirm time.

Nothing else changes: no existing column is modified, no trigger is added.
"""

STATEMENTS = [
    "ALTER TABLE prescriptions ADD COLUMN proposal_source TEXT DEFAULT 'gemini' CHECK (proposal_source IN ('local_extractor', 'gemini', 'manual'))",
]


def upgrade(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
