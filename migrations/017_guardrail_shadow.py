"""
Migration 017 -- guardrail shadow recording (Phase 3A.4, docs/PHASE_3_PLAN.md).

Two append-only tables for evaluation-only guardrail results. Nothing here
changes any existing table, row, response or write path, and nothing reads
these tables yet: the best-effort recorder is a separate, later block.

guardrail_shadow_activations -- per-clinic activation metadata. Rule
definitions and versions stay in the repository; only which rule set a
clinic shadow-evaluates is stored here.

  mode             'SHADOW' (evaluate and record) or 'OFF'. There is no
                   enforcement mode: this table cannot express one.
  ruleset_*        the activated rule set (id, version, 64-hex content
                   hash); set for SHADOW, NULL for OFF
  activated_by     the operator who ran the activation (free text, not a
                   user account: activation is an admin script, not an API
                   session); never patient data
  reason           why (free text, bounded); never patient data

  A clinic with no row is OFF. The clinic's row with the highest
  activation_id is the one in effect. Switching off appends an OFF row.

guardrail_shadow_results -- one row per recorded evaluation.

  stage            'PREVIEW' (a stored draft) or 'CONFIRM' (a committed
                   prescription); prescription_id is set exactly for CONFIRM
  is_manual        the draft's own manual flag (manual-preview drafts are
                   recorded too)
  status           EVALUATED        -> outcome and results_json set
                   NOT_EVALUABLE    -> error_code CANDIDATE_INVALID
                   ENGINE_ERROR     -> error_code ENGINE_CHANGED,
                                       RULESET_INVALID or UNEXPECTED
                   RULESET_MISMATCH -> the in-code rule set's hash
                                       (code_ruleset_hash) differs from the
                                       activation's; nothing was evaluated
  results_json     the rule results only (rule id, version, outcome, reason
                   code, fixed text, line number)

  Deliberately NOT stored: the candidate fingerprint (salting is deferred),
  patient identity, dosha or herb text. Error codes are a fixed vocabulary;
  no exception text is ever stored.

Enforced by the database (triggers fire regardless of PRAGMA foreign_keys):

  - guardrail_shadow_results_activation_check: the activation exists, is in
    the same clinic and is a SHADOW row; and the drift decision is
    consistent with it -- an EVALUATED row's code_ruleset_hash must equal
    the activation's ruleset_hash, a RULESET_MISMATCH row's must differ
    (shadow_ruleset_hash_inconsistent). It does NOT require that row to be
    the clinic's latest: a recorder that looked up the activation just
    before a newer row was appended can still record under it. Recording
    only under the latest activation is the recorder's job (and is tested
    there); the database only guarantees same clinic and SHADOW mode.
  - guardrail_shadow_results_preview_check: the draft exists in the same
    clinic with the same is_manual flag.
  - guardrail_shadow_results_prescription_check (CONFIRM only): the
    prescription exists in the same clinic, and its PRESCRIPTION_CONFIRMED
    audit event names exactly this (prescription_id, preview_id) pair --
    the only place that pairing is recorded (migration 015's
    prescription_items_sealed trigger reads audit_events the same way).
  - *_no_update / *_no_delete on both tables: append-only. A future
    retention purge needs its own reviewed migration.
  - CHECK constraints fix which columns each mode and status may set, and
    UNIQUE (preview_id, stage) allows one row per draft per stage. The
    error-code tests use IS / IS NOT NULL: SQLite accepts a CHECK that
    evaluates to NULL, so `error_code IN (...)` alone would let a row with
    no error code through.
  - Formats: the three 64-character fingerprints are lowercase hex (SHA-256
    hexdigest); results_json is a JSON array (json_type is only consulted
    once json_valid holds, because json_type raises on malformed JSON);
    required strings are non-blank.

REQUIRES SQLite >= 3.38 (JSON functions json_valid / json_type built in).
The migration runs at API startup and a failed migration stops startup, so
check the target runtime first: python scripts/check_sqlite_compat.py
(it opens no database). The verified runtime is Python 3.14.3 / SQLite
3.51.2.
"""

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS guardrail_shadow_activations (
        activation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id       TEXT    NOT NULL CHECK (length(trim(clinic_id)) >= 1 AND length(clinic_id) <= 64),
        mode            TEXT    NOT NULL CHECK (mode IN ('SHADOW', 'OFF')),
        ruleset_id      TEXT,
        ruleset_version TEXT,
        ruleset_hash    TEXT,
        activated_by    TEXT    NOT NULL CHECK (length(trim(activated_by)) BETWEEN 1 AND 100),
        reason          TEXT    NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
        created_at      TEXT    NOT NULL CHECK (length(trim(created_at)) >= 1),
        CHECK (
            (mode = 'SHADOW' AND ruleset_id IS NOT NULL AND ruleset_version IS NOT NULL AND ruleset_hash IS NOT NULL
                 AND length(trim(ruleset_id)) >= 1 AND length(trim(ruleset_version)) >= 1
                 AND length(ruleset_hash) = 64 AND ruleset_hash NOT GLOB '*[^0-9a-f]*')
         OR (mode = 'OFF' AND ruleset_id IS NULL AND ruleset_version IS NULL AND ruleset_hash IS NULL)
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_shadow_activations_clinic ON guardrail_shadow_activations(clinic_id, activation_id)",
    """
    CREATE TRIGGER IF NOT EXISTS guardrail_shadow_activations_no_update
    BEFORE UPDATE ON guardrail_shadow_activations
    BEGIN
        SELECT RAISE(ABORT, 'shadow_activation_immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS guardrail_shadow_activations_no_delete
    BEFORE DELETE ON guardrail_shadow_activations
    BEGIN
        SELECT RAISE(ABORT, 'shadow_activation_immutable');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS guardrail_shadow_results (
        result_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        clinic_id            TEXT    NOT NULL,
        activation_id        INTEGER NOT NULL REFERENCES guardrail_shadow_activations(activation_id),
        stage                TEXT    NOT NULL CHECK (stage IN ('PREVIEW', 'CONFIRM')),
        preview_id           TEXT    NOT NULL REFERENCES prescription_previews(preview_id),
        prescription_id      INTEGER REFERENCES prescriptions(id),
        is_manual            INTEGER NOT NULL CHECK (is_manual IN (0, 1)),
        status               TEXT    NOT NULL CHECK (status IN
                                 ('EVALUATED', 'NOT_EVALUABLE', 'ENGINE_ERROR', 'RULESET_MISMATCH')),
        error_code           TEXT    CHECK (error_code IS NULL OR error_code IN
                                 ('CANDIDATE_INVALID', 'ENGINE_CHANGED', 'RULESET_INVALID', 'UNEXPECTED')),
        engine_version       TEXT    CHECK (engine_version IS NULL OR length(trim(engine_version)) >= 1),
        engine_fingerprint   TEXT    CHECK (engine_fingerprint IS NULL
                                        OR (length(engine_fingerprint) = 64 AND engine_fingerprint NOT GLOB '*[^0-9a-f]*')),
        code_ruleset_id      TEXT    CHECK (code_ruleset_id IS NULL OR length(trim(code_ruleset_id)) >= 1),
        code_ruleset_version TEXT    CHECK (code_ruleset_version IS NULL OR length(trim(code_ruleset_version)) >= 1),
        code_ruleset_hash    TEXT    CHECK (code_ruleset_hash IS NULL
                                        OR (length(code_ruleset_hash) = 64 AND code_ruleset_hash NOT GLOB '*[^0-9a-f]*')),
        outcome              TEXT    CHECK (outcome IS NULL OR outcome IN ('PASS', 'REVIEW', 'REJECT')),
        results_json         TEXT    CHECK (results_json IS NULL
                                        OR CASE WHEN json_valid(results_json) THEN json_type(results_json) = 'array' ELSE 0 END),
        created_at           TEXT    NOT NULL CHECK (length(trim(created_at)) >= 1),
        UNIQUE (preview_id, stage),
        CHECK ((stage = 'CONFIRM') = (prescription_id IS NOT NULL)),
        CHECK (
            (status = 'EVALUATED' AND error_code IS NULL AND outcome IS NOT NULL AND results_json IS NOT NULL
                 AND engine_version IS NOT NULL AND engine_fingerprint IS NOT NULL AND code_ruleset_hash IS NOT NULL)
         OR (status = 'NOT_EVALUABLE' AND error_code IS 'CANDIDATE_INVALID' AND outcome IS NULL AND results_json IS NULL)
         OR (status = 'ENGINE_ERROR' AND error_code IS NOT NULL
                 AND error_code IN ('ENGINE_CHANGED', 'RULESET_INVALID', 'UNEXPECTED')
                 AND outcome IS NULL AND results_json IS NULL)
         OR (status = 'RULESET_MISMATCH' AND error_code IS NULL AND outcome IS NULL AND results_json IS NULL
                 AND code_ruleset_hash IS NOT NULL)
        )
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_shadow_results_clinic_time ON guardrail_shadow_results(clinic_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_shadow_results_activation ON guardrail_shadow_results(activation_id)",
    """
    CREATE TRIGGER IF NOT EXISTS guardrail_shadow_results_activation_check
    BEFORE INSERT ON guardrail_shadow_results
    BEGIN
        SELECT RAISE(ABORT, 'shadow_activation_invalid')
        WHERE NOT EXISTS (
            SELECT 1 FROM guardrail_shadow_activations a
            WHERE a.activation_id = NEW.activation_id AND a.clinic_id = NEW.clinic_id AND a.mode = 'SHADOW'
        );
        SELECT RAISE(ABORT, 'shadow_ruleset_hash_inconsistent')
        WHERE (NEW.status = 'EVALUATED' AND NEW.code_ruleset_hash IS NOT (
                   SELECT a.ruleset_hash FROM guardrail_shadow_activations a WHERE a.activation_id = NEW.activation_id))
           OR (NEW.status = 'RULESET_MISMATCH' AND NEW.code_ruleset_hash IS (
                   SELECT a.ruleset_hash FROM guardrail_shadow_activations a WHERE a.activation_id = NEW.activation_id));
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS guardrail_shadow_results_preview_check
    BEFORE INSERT ON guardrail_shadow_results
    BEGIN
        SELECT RAISE(ABORT, 'shadow_preview_invalid')
        WHERE NOT EXISTS (
            SELECT 1 FROM prescription_previews p
            WHERE p.preview_id = NEW.preview_id AND p.clinic_id = NEW.clinic_id AND p.is_manual = NEW.is_manual
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS guardrail_shadow_results_prescription_check
    BEFORE INSERT ON guardrail_shadow_results
    WHEN NEW.stage = 'CONFIRM'
    BEGIN
        SELECT RAISE(ABORT, 'shadow_prescription_invalid')
        WHERE NOT EXISTS (
            SELECT 1 FROM prescriptions p WHERE p.id = NEW.prescription_id AND p.clinic_id = NEW.clinic_id
        )
        OR NOT EXISTS (
            SELECT 1 FROM audit_events e
            WHERE e.event_type = 'PRESCRIPTION_CONFIRMED' AND e.clinic_id = NEW.clinic_id
              AND e.prescription_id = NEW.prescription_id AND e.preview_id = NEW.preview_id
        );
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS guardrail_shadow_results_no_update
    BEFORE UPDATE ON guardrail_shadow_results
    BEGIN
        SELECT RAISE(ABORT, 'shadow_result_immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS guardrail_shadow_results_no_delete
    BEFORE DELETE ON guardrail_shadow_results
    BEGIN
        SELECT RAISE(ABORT, 'shadow_result_immutable');
    END
    """,
]


def upgrade(conn):
    for statement in STATEMENTS:
        conn.execute(statement)
