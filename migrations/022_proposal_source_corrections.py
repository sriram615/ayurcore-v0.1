"""
Migration 022 -- proposal_source on corrections table.

Adds a `proposal_source` column to `corrections` so every logged correction
records where the AI proposal came from:

  - `local_extractor` -- AI-1 local extractor (T1 tagger + T2 assembly +
    T3 verifier, no LLM, no network)
  - `gemini`          -- Google Gemini via agents/copilot.parse_clinical_notes
  - `manual`          -- doctor-entered via /prescribe/manual-preview

Existing rows keep the default 'gemini' (the only AI path that existed
before this migration); new corrections set it explicitly from the preview
row's proposal_source at confirm time.

Nothing else changes: no existing column is modified, no trigger is added.
"""

STATEMENTS = [
    "ALTER TABLE corrections ADD COLUMN proposal_source TEXT DEFAULT 'gemini' CHECK (proposal_source IN ('local_extractor', 'gemini', 'manual'))",
]


def upgrade(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
