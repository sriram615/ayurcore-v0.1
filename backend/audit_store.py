"""
Tamper-evident audit infrastructure (Phase A5).

Preserves an authoritative, append-only record of clinically meaningful
prescription decisions, and makes unauthorized historical modification of
that record detectable. Deliberately small and boring: one table
(migrations/007_audit_events.sql), one event type, one deterministic
hashing algorithm, one verifier. No blockchain, no external service, no
Merkle tree, no cryptographic signatures -- see this module's own
docstrings below for exactly what problem each piece solves and why
nothing more elaborate is needed.

--------------------------------------------------------------------------
WHAT GETS AUDITED
--------------------------------------------------------------------------
Exactly one event type today: PRESCRIPTION_CONFIRMED, written the moment
POST /prescribe/confirm's transaction actually commits a prescription (see
backend/main.py). This is deliberately NOT an HTTP access log -- previews,
failed confirms, logins, and audit reads themselves are never recorded
here. Only the one genuinely clinically meaningful moment this whole
system exists to gate correctly: a doctor turning a proposal (AI or
manual) into a real, dispensed prescription.

The event's payload (event_data, see build_prescription_confirmed_event_data
below) preserves exactly the decision: what was proposed (if anything was
-- see decision_source), what the doctor actually confirmed, and whether
they differ. It is never a raw dump of the request, the response, the full
database row, or HTTP headers.

--------------------------------------------------------------------------
AI_ASSISTED vs. MANUAL -- never fabricate a proposal
--------------------------------------------------------------------------
A manual-fallback preview (see /prescribe/manual-preview) has no AI
proposal at all -- the doctor entered the dosha/herbs directly because the
AI was unreachable. Its preview row's ai_dosha_imbalance/
ai_prescribed_herbs_json columns exist (prescription_previews' schema
uses one column pair for both cases -- see preview_store.py) but hold the
doctor's own manually-typed values, copied, NOT an AI proposal. Treating
those columns as "what the AI proposed" for a manual preview -- the exact
mistake the pre-existing `corrections` table's own /prescribe/confirm call
site (backend/main.py) was careful never to make -- would fabricate a
false "AI proposed this and the doctor left it unchanged" data point that
never happened. build_prescription_confirmed_event_data() below takes an
explicit `is_manual` flag and sets `decision_source` + `ai_proposal`
accordingly: AI_ASSISTED events carry the real proposal; MANUAL events
carry `ai_proposal: null` and no `was_changed` (comparing against nothing
is meaningless, not `false`).

--------------------------------------------------------------------------
ACTOR / CLINIC ATTRIBUTION
--------------------------------------------------------------------------
create_audit_event()'s `actor_user_id` and `clinic_id` parameters MUST
always be the caller's own server-derived `auth.user_id` / `auth.clinic_id`
(Phase A, A2/A3) -- never anything client-supplied. This module does not
enforce that itself (it has no access to a request); backend/main.py's
/prescribe/confirm is the one call site, and it passes auth.user_id /
entry["clinic_id"] (itself derived from auth.clinic_id at preview-creation
time and re-validated by get_pending_preview_for_confirm's clinic-ownership
check -- see preview_store.py), exactly like every other write in that
handler.

--------------------------------------------------------------------------
PII MINIMIZATION
--------------------------------------------------------------------------
No patient name, phone, government ID, raw consultation notes, auth token,
password hash, API key, or Gemini credential is ever written into
event_data. The only identifiers stored are prescription_id and preview_id
(opaque foreign keys -- looking up a patient's actual identity from a
prescription_id requires querying the pre-existing `prescriptions` table
directly, exactly the same privacy posture the `corrections` table already
has -- see corrections_service.py's module docstring) and actor_user_id /
clinic_id (identity of the DOCTOR and clinic, not the patient).

--------------------------------------------------------------------------
APPEND-ONLY
--------------------------------------------------------------------------
This module exposes exactly one write operation: create_audit_event().
There is no update_audit_event() or delete_audit_event(), and
backend/main.py exposes no endpoint that edits or removes a historical
audit_events row. "Immutable" here is enforced two ways: by convention (no
code path to do it) and, more importantly, by verify_audit_integrity()
below actually detecting it if someone bypasses the application and edits
the SQLite file directly -- see that function's docstring.

--------------------------------------------------------------------------
CANONICALIZATION
--------------------------------------------------------------------------
Before hashing, an event's content is serialized with
json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False):
  - sort_keys=True:        dictionary key order can never change the hash.
  - separators=(",", ":"): no incidental whitespace can change the hash.
  - ensure_ascii=False:    a herb's Malayalam alias resolves through
                            normalize_herb() to its canonical ASCII name
                            (see backend/herb_resolver.py) before it ever
                            reaches this module, so this mostly future-proofs
                            against a canonical name that isn't pure ASCII;
                            it is set for the same reason A4's
                            compute_request_fingerprint() and this
                            project's other canonical-JSON call sites don't
                            silently \\uXXXX-escape non-ASCII content,
                            which would otherwise make the SAME logical
                            value hash differently depending on whether it
                            happened to round-trip through ensure_ascii's
                            default of True somewhere else in the pipeline.
That alone does not solve everything -- see _canonicalize_herbs() below for
the herb-specific rules (re-resolved through the SAME normalize_herb()
this project already uses everywhere else, duplicates merged, sorted by
name) that make two logically-identical herb lists hash identically
regardless of input order or an incidental duplicate split across two
entries.

--------------------------------------------------------------------------
HASH ALGORITHM -- exact, documented, and tested
--------------------------------------------------------------------------
For one audit event:

    canonical_envelope = json.dumps(
        {
            "event_type": event_type,
            "clinic_id": clinic_id,
            "actor_user_id": actor_user_id,
            "prescription_id": prescription_id,
            "preview_id": preview_id,
            "created_at": created_at,        # the exact stored TEXT value
            "event_data": event_data,         # nested dict, not pre-serialized
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    event_hash = sha256(
        (canonical_envelope + (previous_event_hash or "")).encode("utf-8")
    ).hexdigest()

Deliberately EXCLUDES:
  - event_id: SQLite AUTOINCREMENT row identity, assigned only once the row
    is actually inserted -- including it would force a two-step
    INSERT-then-UPDATE (this project already uses that pattern where it's
    unavoidable, e.g. migrations/006's idempotency rows, but it is not
    needed here: event_id is storage bookkeeping, not clinical content, and
    every tamper scenario this module needs to catch -- content edited,
    chain re-pointed, actor/clinic/prescription linkage forged -- is
    already caught without it; see verify_audit_integrity()'s docstring for
    the full list of what IS caught).
  - event_hash itself (obviously -- a hash cannot include itself).

previous_event_hash is treated as the empty string "" when NULL (a
clinic's first-ever event) -- documented here once, used identically by
both create_audit_event() (at creation) and verify_audit_integrity() (at
verification), so the two are guaranteed to agree on a clinic's genesis
event.

The hash is reproducible from ONLY the row's own stored, immutable
columns (event_type, clinic_id, actor_user_id, prescription_id,
preview_id, event_data_json, created_at, previous_event_hash) -- never
from arbitrary database serialization, and never dependent on anything
outside this one row plus the single previous_event_hash value it points
to.

--------------------------------------------------------------------------
HASH CHAIN -- per clinic, not global
--------------------------------------------------------------------------
Clinic A's events and clinic B's events form two completely independent
chains that happen to share one table (see migrations/007's own docstring
for why: this project is explicitly multi-tenant, and a global
cross-clinic chain would mean one clinic's audit history's integrity
depends on activity in a clinic it has no relationship to -- an
unnecessary coupling with no corresponding benefit, and arguably a privacy
leak in its own right, since chain position would reveal relative
activity volume across clinics to anyone who could see one clinic's own
chain).

create_audit_event() determines "the previous event for this clinic" with:

    SELECT event_hash FROM audit_events
    WHERE clinic_id = ? ORDER BY event_id DESC LIMIT 1

This is safe under concurrent prescription confirmation WITHOUT any new
locking primitive because create_audit_event() -- like
backend/preview_store.py's mark_confirmed() and
backend/idempotency_store.py's reserve_idempotent_operation() before it --
MUST be called from inside the caller's own already-open `BEGIN IMMEDIATE`
transaction (see backend/main.py's /prescribe/confirm). SQLite's write
lock is acquired the moment that statement runs, for the remainder of the
transaction; a second, concurrent confirm attempt (same clinic or not)
blocks on ITS OWN BEGIN IMMEDIATE until the first transaction fully
commits or rolls back, then re-runs this SELECT and correctly sees the
first transaction's newly-committed event as "the previous one." There is
no "SELECT latest, COMMIT, INSERT later" gap of the kind this module's
design explicitly avoids -- the SELECT and the INSERT happen inside the
SAME transaction, on the SAME connection, while holding the SAME write
lock the entire time.

--------------------------------------------------------------------------
TRANSACTION INTEGRATION -- audit failure is never silent
--------------------------------------------------------------------------
create_audit_event() is called by backend/main.py's /prescribe/confirm
AFTER deduct_and_prescribe() (so prescription_id is known) and BEFORE
mark_confirmed() -- inside the SAME `BEGIN IMMEDIATE` transaction as both.
Unlike corrections_service.log_correction() (deliberately best-effort,
outside the transaction, can never roll back a real prescription -- see
that module's own docstring), an audit-event write failure here is NOT
swallowed: any exception raised by create_audit_event() propagates
straight out of /prescribe/confirm's transaction body, which rolls back
the ENTIRE transaction -- the prescription and the inventory deduction
included -- and returns 500. backend/main.py logs this specific failure
mode explicitly (event=audit_event_write_failed) before re-raising, so an
operator can distinguish "the audit write failed and nothing committed"
from any other 500. This is a deliberate, documented tradeoff: a doctor
retries a failed confirm (the preview is untouched -- nothing committed),
rather than the system ever recording a real, dispensed prescription with
NO corresponding tamper-evident audit trail for it.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from backend.herb_resolver import merge_herb_lines

AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED = "PRESCRIPTION_CONFIRMED"
# Encounter lifecycle (migrations 010/011/013). These events carry NO
# prescription_id / preview_id (both NULL, enforced by migration 011's CHECK).
# The encounter is identified inside the hashed event_data by its random
# encounter_ref (migration 013) -- not the global, sequential encounter_pk,
# which would let an auditor infer other clinics' encounter volume -- and
# never by patient_pk or any patient identity. (Events written before 013
# carry encounter_pk; they are hashed and can never be rewritten.)
AUDIT_EVENT_TYPE_ENCOUNTER_OPENED = "ENCOUNTER_OPENED"
AUDIT_EVENT_TYPE_ENCOUNTER_CLOSED = "ENCOUNTER_CLOSED"
_ENCOUNTER_EVENT_TYPES = frozenset({AUDIT_EVENT_TYPE_ENCOUNTER_OPENED, AUDIT_EVENT_TYPE_ENCOUNTER_CLOSED})
_ALL_EVENT_TYPES = frozenset({AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED}) | _ENCOUNTER_EVENT_TYPES

DECISION_SOURCE_AI_ASSISTED = "AI_ASSISTED"
DECISION_SOURCE_MANUAL = "MANUAL"

# Documented sentinel for "no previous event" (a clinic's genesis event),
# used identically at creation and verification time -- see this module's
# docstring, HASH ALGORITHM section.
_GENESIS_PREVIOUS_HASH = ""

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class AuditWriteError(Exception):
    """Raised by create_audit_event() for a caller-side data problem (e.g.
    an unrecognized event_type) -- distinct from a plain sqlite3 error
    (schema/lock/IO problems, left to propagate untouched) so a caller can
    tell "you asked me to write something invalid" apart from "the write
    itself failed." Both are equally fatal to the caller's transaction --
    see this module's TRANSACTION INTEGRATION docstring section -- this
    distinction is for logging/diagnosis, not for different handling.
    """


class AuditIntegrityError(Exception):
    """Raised by assert_audit_integrity() (never by verify_audit_integrity()
    itself, which always returns a report -- see its docstring) when a
    caller wants a hard failure instead of inspecting a report."""


@dataclass(frozen=True)
class AuditIntegrityViolation:
    """One concrete, specific problem found by verify_audit_integrity()."""
    event_id: Optional[int]
    clinic_id: str
    reason: str


@dataclass(frozen=True)
class AuditIntegrityReport:
    """Result of verify_audit_integrity(). `valid` is True only when
    `violations` is empty. `checked_count` is the number of audit_events
    rows actually inspected (0 and valid=True is a legitimate, unremarkable
    result -- an empty audit trail is not itself a violation)."""
    valid: bool
    checked_count: int
    violations: list[AuditIntegrityViolation] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format(dt: datetime) -> str:
    return dt.strftime(_TIMESTAMP_FORMAT)


def _canonicalize_herbs(herbs: list[dict]) -> list[dict]:
    """Deterministic herb-list canonicalization, reusing the SAME
    normalize_herb() boundary every other herb-name resolution in this
    project uses (backend/main.py's /prescribe/preview,
    /prescribe/manual-preview, and /prescribe/confirm) -- never a second,
    parallel normalization scheme.

    - Names are re-resolved through normalize_herb(). This is idempotent
      for an already-canonical name (every canonical name in
      HERB_ALIASES also appears, lowercased, as its own alias key -- see
      herb_resolver.py) and additionally closes the gap where an AI
      proposal or a doctor's confirmation was stored under an alias that
      happens to differ in case from the canonical form.
    - Duplicate herbs (the same canonical name appearing more than once,
      e.g. two separate 10g entries for the same herb in a raw,
      not-yet-merged proposal) are merged by summing grams -- the SAME
      merge inventory_service.deduct_and_prescribe() already performs, so
      an audited decision's herb list matches what was actually deducted.
    - The result is sorted by canonical name ascending. Herb list order
      is not clinically meaningful (a prescription for "Ashwagandha 10g,
      Brahmi 5g" and "Brahmi 5g, Ashwagandha 10g" are the same decision),
      so two logically identical proposals must hash identically
      regardless of the order they happened to arrive in.
    - grams is stored as a plain int (already enforced upstream by
      PrescribedHerb's Pydantic field, but re-asserted here so this
      function's output type is never ambiguous between e.g. `10` and
      `10.0`, which json.dumps would serialize differently).

    Raises UnknownHerbError (from herb_resolver.py) if a name doesn't
    resolve -- this should be unreachable in practice, since every herb
    list passed in here has already passed through normalize_herb() once
    upstream (at /preview, /manual-preview, or /confirm) before ever
    reaching this module; re-raising here rather than silently passing
    through an unresolvable name is a defensive, fail-loud choice, not an
    expected path.
    """
    # int() per line BEFORE the shared merge, exactly as before, so every
    # audited herb list -- and therefore every event hash -- is unchanged.
    merged = merge_herb_lines({"name": herb["name"], "grams": int(herb["grams"])} for herb in herbs)
    return sorted(merged, key=lambda h: h["name"])


def build_prescription_confirmed_event_data(
    *,
    is_manual: bool,
    ai_dosha_imbalance: Optional[str],
    ai_prescribed_herbs: Optional[list[dict]],
    confirmed_dosha_imbalance: str,
    confirmed_prescribed_herbs: list[dict],
) -> dict:
    """Builds the PRESCRIPTION_CONFIRMED event_data snapshot -- the ONLY
    place this project decides what "the clinically meaningful decision"
    means for this event type. Never a raw dump of the request/response;
    exactly the fields documented in this module's own WHAT GETS AUDITED
    section.

    `is_manual` MUST be the SAME flag the preview row itself was created
    with (prescription_previews.is_manual -- see preview_store.py) --
    backend/main.py passes entry["is_manual"] directly, never re-derives
    it. When True, `ai_dosha_imbalance`/`ai_prescribed_herbs` are IGNORED
    even if the caller passes them (defensive: the preview row's ai_*
    columns hold the doctor's own manually-typed values for a manual
    preview, copied -- see this module's own docstring -- never a real AI
    proposal, so they must never be reported as one here regardless of
    what a caller passes). decision_source is set to MANUAL,
    `ai_proposal` is explicitly `None`, and `was_changed` is explicitly
    `None` (comparing the doctor's decision against a proposal that never
    existed is meaningless, not `False`).

    When `is_manual` is False, `ai_dosha_imbalance` and
    `ai_prescribed_herbs` are required (a caller passing None for either
    is a programming error, not a legitimate "no AI proposal" case -- that
    case is exactly what is_manual=True is for) and are preserved
    alongside the doctor's confirmed decision, with `was_changed` computed
    the same way corrections_service.log_correction() already does (dosha
    OR herb list differs, after canonicalization).
    """
    confirmed = {
        "dosha": confirmed_dosha_imbalance,
        "herbs": _canonicalize_herbs(confirmed_prescribed_herbs),
    }

    if is_manual:
        return {
            "decision_source": DECISION_SOURCE_MANUAL,
            "ai_proposal": None,
            "doctor_decision": confirmed,
            "was_changed": None,
        }

    if ai_dosha_imbalance is None or ai_prescribed_herbs is None:
        raise AuditWriteError(
            "build_prescription_confirmed_event_data() called with is_manual=False but "
            "ai_dosha_imbalance/ai_prescribed_herbs missing -- this is a programming error, "
            "not a legitimate 'no AI proposal' case (use is_manual=True for that)."
        )

    proposal = {
        "dosha": ai_dosha_imbalance,
        "herbs": _canonicalize_herbs(ai_prescribed_herbs),
    }
    was_changed = proposal != confirmed

    return {
        "decision_source": DECISION_SOURCE_AI_ASSISTED,
        "ai_proposal": proposal,
        "doctor_decision": confirmed,
        "was_changed": was_changed,
    }


def build_encounter_event_data(encounter_ref: str) -> dict:
    """event_data for ENCOUNTER_OPENED / ENCOUNTER_CLOSED: only the random,
    non-sequential encounter_ref -- no encounter_pk, patient_pk, name, phone
    or ID (PII MINIMIZATION). PRESCRIPTION_CONFIRMED events deliberately
    carry no encounter reference at all, so an auditor cannot group a
    clinic's prescriptions by encounter (i.e. by patient visit)."""
    return {"encounter_ref": encounter_ref}


def _canonical_envelope(
    *,
    event_type: str,
    clinic_id: str,
    actor_user_id: int,
    prescription_id: Optional[int],
    preview_id: Optional[str],
    created_at: str,
    event_data: dict,
) -> str:
    """The exact canonical serialization hashed by _compute_event_hash().
    See this module's HASH ALGORITHM docstring section for the full
    rationale of exactly these fields, this key set, and this JSON
    formatting."""
    return json.dumps(
        {
            "event_type": event_type,
            "clinic_id": clinic_id,
            "actor_user_id": actor_user_id,
            "prescription_id": prescription_id,
            "preview_id": preview_id,
            "created_at": created_at,
            "event_data": event_data,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _compute_event_hash(canonical_envelope: str, previous_event_hash: Optional[str]) -> str:
    chained = canonical_envelope + (previous_event_hash if previous_event_hash is not None else _GENESIS_PREVIOUS_HASH)
    return hashlib.sha256(chained.encode("utf-8")).hexdigest()


def create_audit_event(
    conn: sqlite3.Connection,
    *,
    clinic_id: str,
    actor_user_id: int,
    event_type: str,
    prescription_id: Optional[int],
    preview_id: Optional[str],
    event_data: dict,
) -> int:
    """Appends one audit event to `clinic_id`'s hash chain and returns its
    new event_id. MUST be called from inside the caller's own already-open
    `BEGIN IMMEDIATE` transaction -- see this module's HASH CHAIN
    docstring section for why that, and not any new locking primitive, is
    what makes the previous-event lookup below race-free under concurrent
    prescription confirmation. This function never opens, commits, or
    rolls back a transaction itself -- the same convention
    backend/preview_store.py and backend/idempotency_store.py already use.

    `clinic_id` and `actor_user_id` MUST be the caller's own
    server-derived values (auth.clinic_id / auth.user_id) -- see this
    module's ACTOR / CLINIC ATTRIBUTION docstring section. This function
    does not and cannot verify that itself; it trusts its caller exactly
    as much as every other *_store.py module in this project trusts
    backend/main.py to pass real, server-derived identity.

    Raises AuditWriteError for an unrecognized `event_type` (only
    AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED exists today -- see this
    module's WHAT GETS AUDITED section on why no broader taxonomy exists
    yet). Any sqlite3 error (a genuine FK violation -- an unknown
    prescription_id/preview_id/actor_user_id -- a lock timeout, a missing
    table) propagates completely unmodified; per this module's
    TRANSACTION INTEGRATION docstring section, backend/main.py is
    responsible for logging and rolling back its OWN transaction on any
    exception from this function, not this module.
    """
    if event_type not in _ALL_EVENT_TYPES:
        raise AuditWriteError(
            f"Unrecognized audit event_type {event_type!r}. Defined types: {sorted(_ALL_EVENT_TYPES)} -- "
            "see this module's WHAT GETS AUDITED docstring section before adding a new one."
        )
    # Same shape rule as migration 011's CHECK, checked here first so a
    # programming error surfaces as AuditWriteError, not an opaque IntegrityError.
    if event_type == AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED:
        if prescription_id is None or preview_id is None:
            raise AuditWriteError("PRESCRIPTION_CONFIRMED requires prescription_id and preview_id.")
    elif prescription_id is not None or preview_id is not None:
        raise AuditWriteError(f"{event_type} must not carry prescription_id or preview_id.")

    created_at = _format(_now())

    previous_row = conn.execute(
        "SELECT event_hash FROM audit_events WHERE clinic_id = ? ORDER BY event_id DESC LIMIT 1",
        (clinic_id,),
    ).fetchone()
    previous_event_hash = previous_row["event_hash"] if previous_row is not None else None

    canonical_envelope = _canonical_envelope(
        event_type=event_type,
        clinic_id=clinic_id,
        actor_user_id=actor_user_id,
        prescription_id=prescription_id,
        preview_id=preview_id,
        created_at=created_at,
        event_data=event_data,
    )
    event_hash = _compute_event_hash(canonical_envelope, previous_event_hash)

    event_data_json = json.dumps(event_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    cursor = conn.execute(
        """
        INSERT INTO audit_events (
            clinic_id, actor_user_id, event_type, prescription_id, preview_id,
            event_data_json, created_at, previous_event_hash, event_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clinic_id, actor_user_id, event_type, prescription_id, preview_id,
            event_data_json, created_at, previous_event_hash, event_hash,
        ),
    )
    return cursor.lastrowid


def get_audit_events_for_clinic(clinic_id: str) -> list[dict]:
    """Read-only: every audit event for one clinic, oldest first (chain
    order). Powers GET /audit/events (backend/main.py) -- the RBAC- and
    clinic-scoped read path for this table, exactly analogous to
    corrections_service.get_corrections_for_clinic() for the pre-existing
    `corrections` table. Never touches patient identity (this table never
    stores any -- see this module's PII MINIMIZATION docstring section)."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT event_id, clinic_id, actor_user_id, event_type, prescription_id, preview_id,
                   event_data_json, created_at, previous_event_hash, event_hash
            FROM audit_events
            WHERE clinic_id = ?
            ORDER BY event_id ASC
            """,
            (clinic_id,),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "event_id": row["event_id"],
            "clinic_id": row["clinic_id"],
            "actor_user_id": row["actor_user_id"],
            "event_type": row["event_type"],
            "prescription_id": row["prescription_id"],
            "preview_id": row["preview_id"],
            "event_data": json.loads(row["event_data_json"]),
            "created_at": row["created_at"],
            "previous_event_hash": row["previous_event_hash"],
            "event_hash": row["event_hash"],
        }
        for row in rows
    ]


def _get_connection() -> sqlite3.Connection:
    # Deferred import (mirrors corrections_service.py) so this module has
    # zero import-time dependency on db.database beyond what it actually
    # uses at call time -- keeps it importable in the same minimal-dependency
    # contexts backend/idempotency_store.py already is.
    from db.database import get_connection
    return get_connection()


def verify_audit_integrity(clinic_id: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> AuditIntegrityReport:
    """Deterministic, non-LLM integrity check over the real, persisted
    audit_events table (optionally scoped to a single clinic_id -- default
    checks every clinic). ALWAYS returns an AuditIntegrityReport; never
    raises for a violation it finds (that is precisely the condition it
    exists to detect and report, not to crash on) -- see
    assert_audit_integrity() below for a raising wrapper. A malformed
    event_data_json (itself a tamper signature -- the column is NOT NULL
    TEXT, so this can only happen via direct, out-of-band modification) is
    caught and reported as a violation for that row, not allowed to crash
    verification of every OTHER row.

    Checks performed, per clinic, walking each clinic's chain independently
    in event_id ascending order:

      1. CONTENT INTEGRITY: for every row, recompute event_hash from its
         own stored, immutable columns (event_type, clinic_id,
         actor_user_id, prescription_id, preview_id, event_data_json
         parsed back to a dict, created_at, and its OWN stored
         previous_event_hash) and compare to the stored event_hash. Catches
         any row whose event_data_json, actor_user_id, clinic_id,
         prescription_id, preview_id, event_type, or created_at was edited
         directly, OR whose event_hash itself was edited directly, OR
         whose previous_event_hash was edited without also recomputing a
         matching event_hash.

      2. CHAIN LINKAGE: for every row after a clinic's first, its stored
         previous_event_hash must equal the ACTUAL previous row's (the one
         immediately before it in this clinic's own event_id order)
         event_hash. This is what catches an internally self-consistent
         forged row -- one where someone edited previous_event_hash AND
         recomputed a matching event_hash for that edited value (so check 1
         alone would pass) -- because the chain no longer links to what
         actually came before it in this clinic's history. A clinic's
         first event must have previous_event_hash exactly NULL.

      3. ORDERING: rows are walked in event_id ascending order per clinic,
         which is what "chain ordering is valid" means concretely here --
         check 2 above only passes when every row's previous_event_hash
         matches the row that is, by event_id, immediately before it; any
         reordering (or a row's previous_event_hash pointing to a
         non-immediate predecessor, or to a different clinic's event, or
         to a nonexistent hash) surfaces as a check-2 violation.

      4. CLINIC BOUNDARIES: each clinic's chain is verified using ONLY that
         clinic's own rows (the per-clinic SELECT below never reads
         another clinic's rows at all) -- a previous_event_hash that
         happens to match some OTHER clinic's event_hash is still a
         violation here, because that hash is not the immediately
         preceding row within THIS clinic's own chain.

      5. NO UNEXPECTED BROKEN LINKS: the union of checks 1-4 -- there is no
         separate fifth mechanism; this line exists because A5's own
         checklist lists it explicitly. Every row is checked by both 1 and
         2 (or 2's "must be NULL" variant for a chain's first row); there
         is no row this function skips.
    """
    # `conn` (optional): verify through a caller's connection -- e.g. inside
    # the migration runner's own open transaction (db/migrations.py
    # integrity guard) or against a backup file (scripts/backup_db.py).
    # Rows are read through a cursor with its own row_factory so the caller's
    # connection settings are left untouched; a caller's connection is never
    # closed here.
    owns_connection = conn is None
    if conn is None:
        conn = _get_connection()
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    try:
        if clinic_id is not None:
            clinic_ids = [clinic_id]
        else:
            clinic_ids = [
                row["clinic_id"]
                for row in cur.execute("SELECT DISTINCT clinic_id FROM audit_events ORDER BY clinic_id").fetchall()
            ]

        violations: list[AuditIntegrityViolation] = []
        checked_count = 0

        for cid in clinic_ids:
            rows = cur.execute(
                """
                SELECT event_id, clinic_id, actor_user_id, event_type, prescription_id, preview_id,
                       event_data_json, created_at, previous_event_hash, event_hash
                FROM audit_events
                WHERE clinic_id = ?
                ORDER BY event_id ASC
                """,
                (cid,),
            ).fetchall()

            expected_previous_hash: Optional[str] = None  # a clinic's genesis expects NULL
            for row in rows:
                checked_count += 1

                # Check 2 (+3+4): chain linkage, before content -- a row
                # whose previous_event_hash doesn't match this clinic's
                # actual preceding hash is a violation regardless of
                # whether its own content hash is internally consistent.
                if row["previous_event_hash"] != expected_previous_hash:
                    violations.append(AuditIntegrityViolation(
                        event_id=row["event_id"],
                        clinic_id=cid,
                        reason=(
                            f"previous_event_hash={row['previous_event_hash']!r} does not match this "
                            f"clinic's actual preceding event hash {expected_previous_hash!r} -- chain "
                            "linkage broken (edited previous_event_hash, reordered rows, or a row "
                            "pointing outside this clinic's own chain)."
                        ),
                    ))

                # Check 1: content integrity -- recompute purely from this
                # row's own stored columns (including its OWN stored
                # previous_event_hash, even if check 2 above just flagged
                # it as wrong for THIS clinic's chain -- a row's event_hash
                # is still expected to be internally consistent with
                # whatever previous_event_hash it actually stores).
                try:
                    event_data = json.loads(row["event_data_json"])
                except (TypeError, ValueError) as e:
                    violations.append(AuditIntegrityViolation(
                        event_id=row["event_id"],
                        clinic_id=cid,
                        reason=f"event_data_json is not valid JSON ({e}) -- content cannot be verified.",
                    ))
                    expected_previous_hash = row["event_hash"]
                    continue

                canonical_envelope = _canonical_envelope(
                    event_type=row["event_type"],
                    clinic_id=row["clinic_id"],
                    actor_user_id=row["actor_user_id"],
                    prescription_id=row["prescription_id"],
                    preview_id=row["preview_id"],
                    created_at=row["created_at"],
                    event_data=event_data,
                )
                recomputed_hash = _compute_event_hash(canonical_envelope, row["previous_event_hash"])
                if recomputed_hash != row["event_hash"]:
                    violations.append(AuditIntegrityViolation(
                        event_id=row["event_id"],
                        clinic_id=cid,
                        reason=(
                            f"stored event_hash={row['event_hash']!r} does not match the hash recomputed "
                            f"from this row's own stored content ({recomputed_hash!r}) -- the row's "
                            "event_data, actor_user_id, clinic_id, prescription_id, preview_id, "
                            "event_type, created_at, previous_event_hash, or event_hash itself was "
                            "modified after creation."
                        ),
                    ))

                expected_previous_hash = row["event_hash"]

        return AuditIntegrityReport(valid=not violations, checked_count=checked_count, violations=violations)
    finally:
        if owns_connection:
            conn.close()


def audit_chain_validity(conn: sqlite3.Connection) -> dict:
    """{clinic_id: chain_valid} for every clinic with audit events, read
    through `conn` (so it sees that connection's uncommitted changes). {} if
    audit_events does not exist yet. Used by db/migrations.run_migrations'
    integrity guard: a migration batch that turns any valid chain invalid is
    rolled back entirely. Recomputes every hash from the stored content
    columns, so it also catches a migration that alters event CONTENT."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_events'"
    ).fetchone()
    if not exists:
        return {}
    report = verify_audit_integrity(None, conn=conn)
    broken = {v.clinic_id for v in report.violations}
    clinics = [r[0] for r in conn.execute("SELECT DISTINCT clinic_id FROM audit_events")]
    return {clinic: clinic not in broken for clinic in clinics}


def assert_audit_integrity(clinic_id: Optional[str] = None) -> AuditIntegrityReport:
    """Strict wrapper around verify_audit_integrity(): returns the same
    report on success, but raises AuditIntegrityError (with every
    violation's reason included) instead of returning an invalid report.
    Use this where the caller genuinely wants a hard failure (an admin
    tool, a scheduled integrity check); use verify_audit_integrity()
    directly where the caller wants to inspect or display the report (e.g.
    GET /audit/events' `chain_valid` field -- see backend/main.py)."""
    report = verify_audit_integrity(clinic_id)
    if not report.valid:
        details = "; ".join(f"event_id={v.event_id} clinic_id={v.clinic_id}: {v.reason}" for v in report.violations)
        raise AuditIntegrityError(f"Audit chain integrity check failed ({len(report.violations)} violation(s)): {details}")
    return report
