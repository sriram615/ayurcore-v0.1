"""
Guardrail shadow recording (Phase 3A.4, docs/PHASE_3_PLAN.md) -- best-effort,
evaluation-only.

After a draft is stored, record_preview() evaluates it with the in-code
guardrail rule set and appends one PREVIEW row to guardrail_shadow_results
(migration 017), if the clinic's shadow mode is on. After /prescribe/confirm
has committed (and only then), record_confirm() does the same for the
doctor's final, committed values -- corrections and merged lines included --
as a CONFIRM row; the database pairs it with that confirmation's
PRESCRIPTION_CONFIRMED audit event. It never raises and
returns nothing the caller uses: a recording problem is a lost row, never a
changed or failed request.

  - OFF by default: no activation row, or a latest row of mode OFF, means
    nothing is evaluated or written. AYURCORE_SHADOW_DISABLED=1 turns
    recording off without opening the database at all.
  - Its own connection, opened with timeout=0 (busy_timeout 0 from the very
    first statement): a busy or locked database loses the row instead of
    making the doctor wait. It deliberately does NOT use
    db.database.get_connection(), whose 15 s timeout and journal-mode switch
    run before any later PRAGMA could take effect (measured: a 15 s wait on a
    locked rollback-journal database). The recorder never changes the
    journal mode; that is the application's job.
  - backend.guardrails is imported here, lazily: an engine that cannot load
    becomes an ENGINE_ERROR row, never an application failure.
  - Only the clinic's LATEST activation is recorded under. The insert is a
    single INSERT ... SELECT that writes only if the activation read at the
    start is still the clinic's latest; if the clinic was switched off or
    re-activated meanwhile, nothing is written and a fixed
    `shadow_record_superseded` event is logged. No write lock is held while
    evaluating.
  - Nothing patient-related is stored or logged: rows hold rule results
    (rule id, version, outcome, reason code, fixed text, line number) and
    fingerprints of the engine and rule set -- no candidate fingerprint,
    identity, dosha or herb text. Log lines are fixed event names plus the
    opaque preview_id.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

import db.database as database

logger = logging.getLogger("ayurcore")

_KILL_SWITCH = "AYURCORE_SHADOW_DISABLED"
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(database.DB_PATH, timeout=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def shadow_disabled() -> bool:
    # Exactly "1" -- no trimming or other spellings ("1\n", " 1 ", "true"),
    # so the switch cannot be flipped unintentionally (Block 5, finding A7).
    return os.environ.get(_KILL_SWITCH) == "1"


def record_preview(*, clinic_id: str, preview_id: str, is_manual: bool, dosha_imbalance, prescribed_herbs) -> None:
    """Records a PREVIEW shadow result for a stored draft. Never raises."""
    _record_safely(stage="PREVIEW", clinic_id=clinic_id, preview_id=preview_id, prescription_id=None,
                   is_manual=is_manual, dosha_imbalance=dosha_imbalance, prescribed_herbs=prescribed_herbs)


def record_confirm(*, clinic_id: str, preview_id: str, prescription_id: int, is_manual: bool,
                   dosha_imbalance, prescribed_herbs) -> None:
    """Records a CONFIRM shadow result for a committed prescription: the
    doctor's final values, merged as confirmation merged them. Call only
    after the confirmation transaction has committed. Never raises."""
    _record_safely(stage="CONFIRM", clinic_id=clinic_id, preview_id=preview_id, prescription_id=prescription_id,
                   is_manual=is_manual, dosha_imbalance=dosha_imbalance, prescribed_herbs=prescribed_herbs)


def _record_safely(**values) -> None:
    try:
        _record(**values)
    except Exception:
        logger.warning("preview_id=%s event=shadow_record_failed stage=%s", values["preview_id"], values["stage"])


def _record(*, stage, clinic_id, preview_id, prescription_id, is_manual, dosha_imbalance, prescribed_herbs) -> None:
    if shadow_disabled():
        return
    conn = _connect()
    try:
        activation = conn.execute(
            "SELECT activation_id, mode, ruleset_hash FROM guardrail_shadow_activations "
            "WHERE clinic_id = ? ORDER BY activation_id DESC LIMIT 1", (clinic_id,)).fetchone()
        if activation is None or activation["mode"] != "SHADOW":
            return
        row = _evaluate(activation["ruleset_hash"], dosha_imbalance, prescribed_herbs)
        row.update(clinic_id=clinic_id, activation_id=activation["activation_id"], stage=stage,
                   preview_id=preview_id, prescription_id=prescription_id, is_manual=1 if is_manual else 0,
                   created_at=datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT))
        if _insert_if_latest(conn, row):
            conn.commit()
        else:
            conn.rollback()
            logger.info("preview_id=%s event=shadow_record_superseded stage=%s", preview_id, stage)
    finally:
        conn.close()


def _evaluate(activation_hash: str, dosha_imbalance, prescribed_herbs) -> dict:
    """The row's status and evaluation columns. Every failure while
    evaluating or building the result -- expected or not -- is mapped to a
    fixed status and error code (anything unforeseen: ENGINE_ERROR /
    UNEXPECTED); no exception text is kept."""
    row = {"status": None, "error_code": None, "engine_version": None, "engine_fingerprint": None,
           "code_ruleset_id": None, "code_ruleset_version": None, "code_ruleset_hash": None,
           "outcome": None, "results_json": None}
    try:
        from backend.guardrails import (ENGINEERING_BASELINE, InvalidCandidateError, InvalidRuleSetError,
                                        UnknownRuleError, build_confirm_candidate, evaluate)
        from backend.guardrails.engine import ENGINE_FINGERPRINT, ENGINE_VERSION, EngineChangedError
    except Exception:
        return {**row, "status": "ENGINE_ERROR", "error_code": "UNEXPECTED"}

    try:
        row.update(engine_version=ENGINE_VERSION, engine_fingerprint=ENGINE_FINGERPRINT,
                   code_ruleset_id=ENGINEERING_BASELINE.ruleset_id,
                   code_ruleset_version=ENGINEERING_BASELINE.version,
                   code_ruleset_hash=ENGINEERING_BASELINE.content_hash)
        if ENGINEERING_BASELINE.content_hash != activation_hash:
            return {**row, "status": "RULESET_MISMATCH"}
        try:
            candidate = build_confirm_candidate(dosha_imbalance, prescribed_herbs)
        except InvalidCandidateError:
            return {**row, "status": "NOT_EVALUABLE", "error_code": "CANDIDATE_INVALID"}
        try:
            result = evaluate(candidate, ENGINEERING_BASELINE)
        except EngineChangedError:
            return {**row, "status": "ENGINE_ERROR", "error_code": "ENGINE_CHANGED"}
        except (InvalidRuleSetError, UnknownRuleError):
            return {**row, "status": "ENGINE_ERROR", "error_code": "RULESET_INVALID"}
        outcome = result.outcome.value
        results_json = json.dumps([r.to_dict() for r in result.results], sort_keys=True, separators=(",", ":"))
    except Exception:
        # Unforeseen, anywhere above (reading the rule set, building the
        # candidate, evaluating, serialising): recorded, never raised.
        return {**row, "status": "ENGINE_ERROR", "error_code": "UNEXPECTED"}
    return {**row, "status": "EVALUATED", "outcome": outcome, "results_json": results_json}


_COLUMNS = ("clinic_id", "activation_id", "stage", "preview_id", "prescription_id", "is_manual", "status",
            "error_code", "engine_version", "engine_fingerprint", "code_ruleset_id", "code_ruleset_version",
            "code_ruleset_hash", "outcome", "results_json", "created_at")


def _insert_if_latest(conn, row: dict) -> bool:
    """Appends the row only if its activation is still the clinic's latest,
    in one statement; returns whether it was written."""
    cursor = conn.execute(
        f"INSERT INTO guardrail_shadow_results ({', '.join(_COLUMNS)}) "
        f"SELECT {', '.join('?' for _ in _COLUMNS)} "
        "WHERE (SELECT MAX(activation_id) FROM guardrail_shadow_activations WHERE clinic_id = ?) = ?",
        (*(row[c] for c in _COLUMNS), row["clinic_id"], row["activation_id"]))
    return cursor.rowcount == 1
