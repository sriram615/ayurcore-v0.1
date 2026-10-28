"""
Migration 020 -- proposal_source on prescription previews.

Adds a `proposal_source` column to `prescription_previews` so every draft
records where its proposal came from:

  - `local_extractor` -- AI-1 local extractor (T1 tagger + T2 assembly +
    T3 verifier, no LLM, no network)
  - `gemini`          -- Google Gemini via agents/copilot.parse_clinical_notes
  - `manual`          -- doctor-entered via /prescribe/manual-preview

Existing rows are backfilled: `is_manual = 1` rows get 'manual', everything
else gets 'gemini' (the only AI path that existed before this migration).

Nothing else changes: no existing column is modified, no trigger is added,
no write path is altered beyond storing the new column.
"""

STATEMENTS = [
    "ALTER TABLE prescription_previews ADD COLUMN proposal_source TEXT DEFAULT 'gemini' CHECK (proposal_source IN ('local_extractor', 'gemini', 'manual'))",
    "UPDATE prescription_previews SET proposal_source = 'manual' WHERE is_manual = 1",
]


def upgrade(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
