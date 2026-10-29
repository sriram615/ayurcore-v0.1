import json
import logging
import os
import pathlib
import secrets
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

# Add the project root to sys.path so we can import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Header, Path, Query
from fastapi.responses import JSONResponse
from db.database import get_connection
from db.migrations import run_migrations
from backend.schemas import (
    PrescribedHerb,
    PreviewRequest,
    PreviewResponse,
    ManualPreviewRequest,
    ConfirmRequest,
    PrescriptionResponse,
    ClinicalParse,
    AuditRecord,
    AuditExportResponse,
    AuditEventRecord,
    AuditEventsExportResponse,
    LoginRequest,
    LoginResponse,
    PatientCreateRequest,
    PatientResponse,
    PatientSummary,
    PatientSearchResponse,
    PatientPrescriptionRecord,
    PatientHistoryResponse,
    AskRequest,
    AskResponse,
    EncounterCreateRequest,
    EncounterResponse,
    EncounterListResponse,
    InventoryItem,
    InventoryResponse,
    DispenseRequest,
    DispenseResponse,
    PaymentRequest,
    PaymentResponse,
    InvoiceResponse,
    BriefResponse,
)
from backend.herb_resolver import merge_herb_lines, normalize_herb, UnknownHerbError
from backend.inventory_service import deduct_and_prescribe, InsufficientStockError
from backend.inventory_read import list_stock, list_stock_detailed
from backend.corrections_service import log_correction, get_corrections_for_clinic
from backend.audit_store import (
    AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
    build_prescription_confirmed_event_data,
    create_audit_event,
    get_audit_events_for_clinic,
    verify_audit_integrity,
    audit_chain_validity,
)
from backend.preview_store import (
    create_preview,
    get_pending_preview_for_confirm,
    mark_confirmed,
    mark_stale_previews_expired,
    PreviewNotFoundError,
    PreviewExpiredError,
    PreviewAlreadyConfirmedError,
    PatientLinkError,
    EncounterLinkError,
    SchemaNotMigratedError,
)
from backend.patient_store import (
    DuplicatePatientError,
    create_patient,
    get_patient_for_clinic,
    get_prescription_history,
    search_patients,
)
from backend.encounter_store import (
    EncounterAlreadyClosedError,
    EncounterClinicMismatchError,
    EncounterNotFoundError,
    OpenEncounterExistsError,
    close_encounter,
    get_encounter_for_clinic,
    list_encounters_for_patient,
    open_encounter,
)
from backend.idempotency_store import (
    OPERATION_PRESCRIBE_CONFIRM,
    compute_request_fingerprint,
    reserve_idempotent_operation,
    complete_idempotent_reservation,
    IdempotencyConflictError,
    IdempotencyClinicMismatchError,
)
from backend.auth_service import (
    AuthenticatedSession,
    authenticate_user,
    create_session,
    revoke_session,
)
from backend.rbac import (
    PERMISSION_MATRIX,
    ROLE_ADMIN,
    ROLE_AUDITOR,
    ROLE_DOCTOR,
    require_auth,
    require_role,
)
from backend.rate_limit import limiter
from backend.pii_sanitizer import (
    tokenize_payload,
    assert_no_pii_leak,
    rehydrate_payload,
    find_unresolved_tokens,
    expand_name_variants,
    contains_identity,
    PIIViolationError,
)
from agents.copilot import parse_clinical_notes

app = FastAPI(title="Ayurveda Core API")

# CORS (frontend integration, Sep 2026): the only backend change this
# integration required. The approved Next.js frontend (v0-generated,
# ui/frontend integration task) runs on its own dev-server origin
# (http://localhost:3000) and calls this API directly from the browser
# with `fetch`, which is a different origin than this API's own
# (http://127.0.0.1:8000) -- without an explicit CORS allowance, the
# browser blocks every request before it reaches any route below,
# including a plain OPTIONS preflight (verified: without this
# middleware, an OPTIONS request here 405s instead of getting a CORS
# response at all).
#
# Deliberately narrow, per this task's explicit "do not weaken security
# with wildcard origins" instruction:
#   - allow_origins: the exact, hardcoded origins the approved frontend
#     is actually served from in dev (localhost and 127.0.0.1 on the
#     Next.js default port) -- never "*", and never a pattern that could
#     match an arbitrary site.
#   - allow_credentials=False: this API's auth is a bearer token read
#     from the `Authorization` header, not a cookie, so the browser never
#     needs to send credentials (cookies/HTTP auth) cross-origin here --
#     turning this on would additionally forbid a wildcard origin under
#     the CORS spec, but it is left off because nothing in this API relies
#     on cookie-based auth at all.
#   - allow_methods / allow_headers: only what the frontend's API client
#     actually sends (GET/POST/OPTIONS; Content-Type, Authorization, and
#     the Idempotency-Key header from Addendum v1.4) -- not every method
#     or header.
#   - expose_headers=["X-Error-Code"]: without this, `fetch()` in the
#     browser cannot read this response header at all on a cross-origin
#     call, even though it's present on the wire -- only a small
#     CORS-safelisted set of headers (Content-Type etc.) is readable by
#     JS by default. X-Error-Code is how /prescribe/preview signals
#     "the AI itself is down" (AI_SERVICE_UNAVAILABLE) so the frontend
#     can show that specific, more useful message instead of a generic
#     server-error one -- confirmed missing by an end-to-end browser
#     test: without this, the frontend fell back to "something went
#     wrong on our side" on an AI outage instead of the intended
#     AI-unavailable copy. Exposing one non-sensitive, already-documented
#     diagnostic header is not a security weakening.
# This changes nothing about authentication, RBAC, or any route's
# business logic -- it only tells the browser which origins may see the
# response to a request that FastAPI would otherwise already accept.
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Idempotency-Key"],
    expose_headers=["X-Error-Code"],
)

# Structured, PII-free logging. Never pass raw notes or raw patient
# identity to this logger -- only request IDs, tokens, timings, and status.
#
# EXPLICIT LEVEL (A6.1 logging-pipeline fix, Sep 2026): a bare
# logging.getLogger("ayurcore") has level=NOTSET, so it inherits its
# EFFECTIVE level from the root logger -- and Python's root logger
# defaults to WARNING with nothing else in this codebase ever
# configuring it. INFO (20) < WARNING (30), so every logger.info(...)
# call on this logger (this one and the 15+ others throughout this
# file -- login success, logout, migrations-applied, preview
# lifecycle, schema events, etc.) was a SILENT NO-OP whenever nothing
# else in the process happened to lower the root level first --
# verified by direct reproduction: with no other configuration,
# logger.info(...) never even builds a LogRecord, so no handler
# (including a test's own capture handler attached directly to this
# logger) ever receives anything, regardless of what's attached to it.
# Setting the level explicitly HERE makes emission deterministic and
# independent of import order or any other module's side effects
# (e.g. a third-party import elsewhere in the process incidentally
# lowering the root level). This changes ONLY whether these
# already-written, already-intended INFO log lines are emitted -- it
# does not alter any HTTP response, auth decision, RBAC decision, or
# other application behavior.
logger = logging.getLogger("ayurcore")
logger.setLevel(logging.INFO)


@app.exception_handler(sqlite3.OperationalError)
def _sqlite_operational_error(request, exc: sqlite3.OperationalError):
    """A write that could not get SQLite's lock within the busy timeout
    (another writer held it) surfaces here -- most often from a
    BEGIN IMMEDIATE that sits outside a handler's own error handling. Every
    such transaction had not started writing, so nothing was saved: answer
    with a retryable 503 and a machine-readable code instead of the
    framework's default 500. Any other OperationalError stays a generic 500
    with no internal detail."""
    message = str(exc).lower()
    if "locked" in message or "busy" in message:
        logger.warning("event=database_busy path=%s", request.url.path)
        return JSONResponse(
            status_code=503,
            content={"detail": "The system is busy. Nothing was saved. Please try again."},
            headers={"Retry-After": "5", "X-Error-Code": "DATABASE_BUSY"},
        )
    logger.error("event=database_operational_error path=%s", request.url.path, exc_info=exc)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


_DOSHA_IDENTITY_DETAIL = "Dosha must not contain patient identity (name, phone or ID)."
_EMPTY_PRESCRIPTION_DETAIL = "A prescription needs at least one herb. Add a herb and confirm again."

# The per-line gram bound of PrescribedHerb (schemas.py), read from the model
# so the merged-total check in /prescribe/confirm cannot drift from it.
_MAX_GRAMS_PER_HERB = next(m.le for m in PrescribedHerb.model_fields["grams"].metadata if hasattr(m, "le"))


def _identity_values(name, phone, patient_id) -> list:
    """The identity values a free-text clinical field must not contain:
    the same set tokenization uses (full name + name parts, phone, ID)."""
    return [*expand_name_variants(name or ""), phone, patient_id]


@app.on_event("startup")
def _apply_pending_migrations() -> None:
    # Phase A (migration-infrastructure milestone): every process start now
    # brings the schema fully up to date before serving a single request,
    # rather than relying on someone having manually run
    # `python db/database.py` beforehand. On all but the very first start
    # after a new migration ships, run_migrations() finds nothing pending
    # and returns immediately -- this is deliberately cheap to call on
    # every boot, not just something to remember to run once.
    applied = run_migrations(integrity_guard=audit_chain_validity)
    if applied:
        logger.info("event=migrations_applied_on_startup versions=%s", applied)


@app.get(
    "/health",
    summary="Liveness check. Unauthenticated; returns a fixed body and touches no database, AI service, or clinical data.",
)
def health():
    # Deliberately static: no DB query, no Gemini check, no version/config/
    # path details, so an unauthenticated caller learns only that the
    # process is up and serving requests.
    return {"status": "ok"}


@app.post(
    "/auth/login",
    response_model=LoginResponse,
    summary="Authenticate with username/password and receive a server-side session token. New in A2 (see API Contract Addendum v1.2).",
)
def login(request: LoginRequest):
    # authenticate_user() is deliberately the ONLY thing consulted here --
    # see its docstring in backend/auth_service.py for why a nonexistent
    # username, a wrong password, and an inactive account all collapse to
    # the identical None, and therefore to the identical response below.
    # Do not add a branch here that distinguishes them; doing so would
    # reopen exactly the username-enumeration hole A2 was asked to close.
    user = authenticate_user(request.username, request.password)
    if user is None:
        # A6.1: one uniform log line for EVERY rejection reason
        # authenticate_user() can return None for -- nonexistent username,
        # wrong password, inactive account, or (new) an active throttle
        # cooldown. There is no branching here on which of those it was --
        # that is precisely what keeps this line from becoming a second,
        # log-based enumeration/state-disclosure channel alongside the
        # response itself (see backend/auth_service.py's authenticate_user()
        # docstring). Logs the caller-SUPPLIED username only (already known
        # to whoever sent this request -- not a new disclosure); never the
        # password, never a token, never an internal reason.
        logger.info("event=login_failed username=%s", request.username)
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    raw_token, expires_at = create_session(user["user_id"])

    logger.info(
        "event=login_success user_id=%s",
        user["user_id"],
    )

    return LoginResponse(
        token=raw_token,
        expires_at=expires_at,
        username=user["username"],
        clinic_id=user["clinic_id"],
        role=user["role"],
    )


@app.post(
    "/auth/logout",
    summary="Revoke the current session. Once revoked, this token immediately stops authenticating. New in A2 (see API Contract Addendum v1.2).",
)
def logout(auth: AuthenticatedSession = Depends(require_auth)):
    revoke_session(auth.raw_token)
    logger.info("event=logout user_id=%s session_id=%s", auth.user_id, auth.session_id)
    return {"status": "logged_out"}


@app.post(
    "/patients",
    response_model=PatientResponse,
    status_code=201,
    summary="Register a patient in the caller's own clinic. DOCTOR/ADMIN only. New in Addendum v1.5 (Patient Registry Phase 1).",
)
def register_patient(
    request: PatientCreateRequest,
    auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR, ROLE_ADMIN)),
):
    # clinic_id is the authenticated session's, never the client's --
    # PatientCreateRequest has no clinic_id field and rejects extra fields.
    try:
        patient = create_patient(
            clinic_id=auth.clinic_id,
            clinic_patient_id=request.clinic_patient_id,
            display_name=request.display_name,
            phone=request.phone,
            created_by_user_id=auth.user_id,
            # Migration 018: clinic-enriched demographics.
            date_of_birth=request.date_of_birth,
            gender=request.gender,
            address_line=request.address_line,
            city=request.city,
            state=request.state,
            pincode=request.pincode,
            prakriti=request.prakriti,
            allergies=request.allergies,
            registered_at=request.registered_at,
        )
    except DuplicatePatientError:
        # Uniqueness is per clinic, so this only ever reveals something about
        # the caller's OWN clinic -- never whether another clinic uses the ID.
        raise HTTPException(status_code=409, detail="A patient with this ID is already registered in this clinic.")
    # PII-free log line: the registry's internal key only, never name/phone/ID.
    logger.info("event=patient_registered patient_pk=%s user_id=%s", patient["patient_pk"], auth.user_id)
    return PatientResponse(**patient)


PATIENT_SEARCH_MIN_QUERY_LENGTH = 2
PATIENT_SEARCH_MAX_RESULTS = 20
PATIENT_HISTORY_DEFAULT_LIMIT = 20
PATIENT_HISTORY_MAX_LIMIT = 50


@app.get(
    "/patients",
    response_model=PatientSearchResponse,
    summary="Search the caller's own clinic's patient registry by OP/registration ID or name. DOCTOR/ADMIN only. Read-only.",
)
def search_patients_endpoint(
    q: str = Query(max_length=100),
    auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR, ROLE_ADMIN)),
):
    query = q.strip()
    if len(query) < PATIENT_SEARCH_MIN_QUERY_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"Search query must be at least {PATIENT_SEARCH_MIN_QUERY_LENGTH} characters.",
        )
    # Scoped to the session's clinic in SQL -- another clinic's patients can
    # never match, so the result (including an empty one) reveals nothing
    # about any other clinic.
    results, truncated = search_patients(auth.clinic_id, query, PATIENT_SEARCH_MAX_RESULTS)
    return PatientSearchResponse(results=[PatientSummary(**r) for r in results], truncated=truncated)


@app.get(
    "/patients/{patient_pk}/prescriptions",
    response_model=PatientHistoryResponse,
    summary="Confirmed prescription history for one patient of the caller's own clinic, newest first. DOCTOR/ADMIN only. Read-only.",
)
def patient_prescription_history(
    patient_pk: int = Path(gt=0),
    limit: int = Query(default=PATIENT_HISTORY_DEFAULT_LIMIT, ge=1, le=PATIENT_HISTORY_MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=100000),
    auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR, ROLE_ADMIN)),
):
    # Same 404 for a patient of another clinic and a nonexistent one.
    patient = _resolve_linked_patient(patient_pk, auth.clinic_id)
    history, has_more = get_prescription_history(auth.clinic_id, patient["patient_pk"], limit, offset)
    return PatientHistoryResponse(
        patient=PatientSummary(
            patient_pk=patient["patient_pk"],
            clinic_patient_id=patient["clinic_patient_id"],
            display_name=patient["display_name"],
        ),
        prescriptions=[PatientPrescriptionRecord(**h) for h in history],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@app.get(
    "/inventory",
    response_model=InventoryResponse,
    summary="Current stock of the caller's own clinic, sorted by herb name. DOCTOR/ADMIN only. Read-only.",
)
def get_inventory(auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR, ROLE_ADMIN))):
    # backend/inventory_read.list_stock_detailed: one SELECT, scoped to the
    # session's clinic; returns batch, expiry, supplier and reorder level
    # (migration 018) alongside herb name and stock.
    stock = list_stock_detailed(auth.clinic_id)
    return InventoryResponse(items=[InventoryItem(**item) for item in stock])


# --- Prescription dispensing / payment / invoice (clinic workflow) -----------

@app.post(
    "/prescriptions/{prescription_pk}/dispense",
    response_model=DispenseResponse,
    summary="Mark a confirmed prescription as physically dispensed. DOCTOR only. Idempotent.",
)
def dispense_prescription(
    prescription_pk: int = Path(gt=0),
    request: Optional[DispenseRequest] = None,
    auth: AuthenticatedSession = Depends(require_role(*PERMISSION_MATRIX["prescription_dispense"])),
):
    # Clinic isolation: prescription must belong to the caller's clinic.
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT id, clinic_id, patient_name, status, dispensed_at, dispensed_by_user_id
                FROM prescriptions WHERE id = ? AND clinic_id = ?
                """,
                (prescription_pk, auth.clinic_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Prescription not found.")

            if row["dispensed_at"] is not None:
                # Idempotent: already dispensed. Return the existing state.
                return DispenseResponse(
                    prescription_id=row["id"],
                    dispensed_at=row["dispensed_at"],
                    dispensed_by_user_id=row["dispensed_by_user_id"],
                    status="dispensed",
                )

            conn.execute(
                """
                UPDATE prescriptions
                SET dispensed_at = ?, dispensed_by_user_id = ?
                WHERE id = ? AND clinic_id = ? AND dispensed_at IS NULL
                """,
                (_now(), auth.user_id, prescription_pk, auth.clinic_id),
            )
            if conn.execute(
                "SELECT changes()"
            ).fetchone()[0] != 1:
                raise HTTPException(
                    status_code=409, detail="Prescription was dispensed by another request."
                )

            row = conn.execute(
                "SELECT id, dispensed_at, dispensed_by_user_id FROM prescriptions WHERE id = ?",
                (prescription_pk,),
            ).fetchone()
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return DispenseResponse(
            prescription_id=row["id"],
            dispensed_at=row["dispensed_at"],
            dispensed_by_user_id=row["dispensed_by_user_id"],
            status="dispensed",
        )
    finally:
        conn.close()


@app.post(
    "/prescriptions/{prescription_pk}/payment",
    response_model=PaymentResponse,
    summary="Record payment against a confirmed prescription. DOCTOR only.",
)
def record_payment(
    prescription_pk: int = Path(gt=0),
    request: PaymentRequest = None,
    auth: AuthenticatedSession = Depends(require_role(*PERMISSION_MATRIX["prescription_payment"])),
):
    # Clinic isolation: prescription must belong to the caller's clinic.
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT id, clinic_id, status, payment_status, payment_amount_cents,
                       payment_method, payment_reference, invoice_number, created_at
                FROM prescriptions WHERE id = ? AND clinic_id = ?
                """,
                (prescription_pk, auth.clinic_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Prescription not found.")

            if row["status"] != "CONFIRMED":
                raise HTTPException(
                    status_code=409,
                    detail="Payment can only be recorded against a confirmed prescription.",
                )

            # Validate amount against any previously recorded amount.
            # The prescription has no separate "total" column; the amount
            # recorded here is what the clinic charged for this prescription.
            if request.payment_amount_cents is not None and request.payment_amount_cents < 0:
                raise HTTPException(
                    status_code=422, detail="Payment amount cannot be negative."
                )

            invoice_number = row["invoice_number"]
            if request.payment_status == "paid" and not invoice_number:
                invoice_number = f"INV-{secrets.token_hex(4)}"

            conn.execute(
                """
                UPDATE prescriptions
                SET payment_status = ?, payment_amount_cents = ?,
                    payment_method = ?, payment_reference = ?,
                    invoice_number = ?
                WHERE id = ? AND clinic_id = ?
                """,
                (
                    request.payment_status,
                    request.payment_amount_cents,
                    request.payment_method,
                    request.payment_reference,
                    invoice_number,
                    prescription_pk,
                    auth.clinic_id,
                ),
            )

            row = conn.execute(
                """
                SELECT id, payment_status, payment_amount_cents,
                       payment_method, payment_reference, invoice_number
                FROM prescriptions WHERE id = ?
                """,
                (prescription_pk,),
            ).fetchone()
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return PaymentResponse(
            prescription_id=row["id"],
            payment_status=row["payment_status"],
            payment_amount_cents=row["payment_amount_cents"],
            payment_method=row["payment_method"],
            payment_reference=row["payment_reference"],
            invoice_number=row["invoice_number"],
        )
    finally:
        conn.close()


@app.get(
    "/prescriptions/{prescription_pk}/invoice",
    response_model=InvoiceResponse,
    summary="Receipt-like invoice summary for a confirmed prescription. DOCTOR/ADMIN only. Read-only.",
)
def get_invoice(
    prescription_pk: int = Path(gt=0),
    auth: AuthenticatedSession = Depends(require_role(*PERMISSION_MATRIX["prescription_invoice"])),
):
    # Clinic isolation: prescription must belong to the caller's clinic.
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT p.id, p.clinic_id, p.patient_name, p.dosha_imbalance,
                   p.payment_status, p.payment_amount_cents, p.payment_method,
                   p.payment_reference, p.invoice_number, p.dispensed_at,
                   p.created_at, pt.clinic_patient_id
            FROM prescriptions p
            JOIN patients pt ON pt.patient_pk = p.patient_pk AND pt.clinic_id = p.clinic_id
            WHERE p.id = ? AND p.clinic_id = ?
            """,
            (prescription_pk, auth.clinic_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Prescription not found.")

        # Structured herb items (migration 015).
        items = conn.execute(
            """
            SELECT herb_name, grams FROM prescription_items
            WHERE prescription_id = ? AND clinic_id = ?
            ORDER BY line_no
            """,
            (prescription_pk, auth.clinic_id),
        ).fetchall()

        prescribed_herbs = [
            {"name": item["herb_name"], "grams": item["grams"]} for item in items
        ]

        return InvoiceResponse(
            prescription_id=row["id"],
            clinic_id=row["clinic_id"],
            patient_clinic_patient_id=row["clinic_patient_id"],
            patient_display_name=row["patient_name"],
            dosha_imbalance=row["dosha_imbalance"],
            prescribed_herbs=prescribed_herbs,
            total_amount_cents=row["payment_amount_cents"] or 0,
            payment_status=row["payment_status"],
            payment_method=row["payment_method"],
            payment_reference=row["payment_reference"],
            invoice_number=row["invoice_number"],
            dispensed_at=row["dispensed_at"],
            created_at=row["created_at"],
        )
    finally:
        conn.close()


# --- AI-3 consultation brief (read-only, no LLM, no network) ----------------

@app.get(
    "/patients/{patient_pk}/brief",
    response_model=BriefResponse,
    summary="Short cited summary of one patient's recorded history for the start of a consultation. DOCTOR/ADMIN only. Read-only.",
)
def get_consultation_brief(
    patient_pk: int = Path(gt=0),
    auth: AuthenticatedSession = Depends(require_role(*PERMISSION_MATRIX["patient_brief"])),
    as_of: Optional[str] = Query(default=None, max_length=16, description="Reference date YYYY-MM-DD; defaults to today"),
    arm: Optional[str] = Query(default="learned", description="'learned' (salience model) or 'template' (fixed order)"),
):
    """Build a read-only, verifiable consultation brief from one patient's
    confirmed prescriptions and encounters. Every fact is re-verified against
    the snapshot; nothing is inferred beyond what was recorded.

    Uses the same read-only PatientSnapshot as AI-2, built through
    ai2.adapters.clinic_stores. The AI component is only the salience
    ranking (learned model) or fixed order (template); the wording is a
    deterministic template. No diagnosis, treatment rationale, dose advice,
    or outcome is ever stated.
    """
    from ai3.brief import make_brief
    from ai2.adapters.clinic_stores import ClinicStoreRecords

    # Default as_of to today's date.
    if as_of is None:
        from datetime import datetime, timezone
        as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if arm not in ("learned", "template"):
        raise HTTPException(status_code=422, detail="arm must be 'learned' or 'template'")

    # Build a read-only snapshot of the patient's record (clinic-scoped).
    try:
        snapshot = ClinicStoreRecords().load_snapshot(auth.clinic_id, patient_pk)
    except PatientNotFound:
        raise HTTPException(status_code=404, detail="Patient not found.")
    except SnapshotUnavailable:
        # No consistent snapshot: nothing partial is answered.
        logger.error("event=ai2_snapshot_unavailable user_id=%s", auth.user_id)
        raise HTTPException(status_code=503, detail=_ASK_UNAVAILABLE, headers={"X-Error-Code": "MEMORY_SERVICE_UNAVAILABLE"})

    brief = make_brief(snapshot, as_of=as_of, arm=arm)
    return BriefResponse(**brief.to_dict())
# (DOCTOR, from the session) and which clinic's patient is read (the session's, never the request's); ai2 owns everything about
# understanding and answering. The planner is loaded once, on the first question, and cached.
_ASK_CHOICE_ERROR = "The chosen reading is not one of the readings offered for this question."
_ASK_UNAVAILABLE = "The question service is unavailable."


def get_ai2_service():
    """The shared AI-2 service (loaded lazily; a load failure is a 503 that names nothing internal)."""
    global _AI2_SERVICE
    if _AI2_SERVICE is None:
        try:
            from ai2 import Ai2Service
            _AI2_SERVICE = Ai2Service.default()
        except Exception:
            logger.error("event=ai2_service_load_failed")
            raise HTTPException(status_code=503, detail=_ASK_UNAVAILABLE, headers={"X-Error-Code": "MEMORY_SERVICE_UNAVAILABLE"})
    return _AI2_SERVICE


_AI2_SERVICE = None


@app.post(
    "/patients/{patient_pk}/ask",
    response_model=AskResponse,
    summary="Ask AI-2 a question about one patient's recorded history (confirmed prescriptions and encounters). DOCTOR only. Read-only: writes nothing, calls no external service.",
)
def ask_patient_history(
    body: AskRequest,
    patient_pk: int = Path(gt=0),
    auth: AuthenticatedSession = Depends(require_role(*PERMISSION_MATRIX["memory_ask"])),
    _rate_limit: None = Depends(limiter("ask")),
):
    from ai2 import InvalidChoice, PatientNotFound, SnapshotUnavailable
    from ai2.adapters.clinic_stores import ClinicStoreRecords

    # The clinic is the session's. A patient of another clinic gets the same 404 as a nonexistent one.
    try:
        snapshot = ClinicStoreRecords().load_snapshot(auth.clinic_id, patient_pk)
    except PatientNotFound:
        raise HTTPException(status_code=404, detail="Patient not found.")
    except SnapshotUnavailable:
        # No consistent snapshot of the record: nothing partial is answered, and nothing about the cause is returned.
        logger.error("event=ai2_snapshot_unavailable user_id=%s", auth.user_id)
        raise HTTPException(status_code=503, detail=_ASK_UNAVAILABLE, headers={"X-Error-Code": "MEMORY_SERVICE_UNAVAILABLE"})
    service = get_ai2_service()
    try:
        if body.choice is not None:
            answer = service.choose(snapshot, body.question, body.choice.frame, body.choice.query)
        else:
            answer = service.ask(snapshot, body.question)
    except InvalidChoice:
        raise HTTPException(status_code=422, detail=_ASK_CHOICE_ERROR, headers={"X-Error-Code": "CHOICE_NOT_OFFERED"})
    except SnapshotUnavailable:
        logger.error("event=ai2_snapshot_unavailable user_id=%s", auth.user_id)
        raise HTTPException(status_code=503, detail=_ASK_UNAVAILABLE, headers={"X-Error-Code": "MEMORY_SERVICE_UNAVAILABLE"})
    # No question text, no patient identity and no record content in the log: only who asked and what kind of result it was.
    logger.info("event=memory_ask user_id=%s outcome=%s category=%s stage=%s clarified=%s",
                auth.user_id, answer.outcome, answer.category, answer.stage, body.choice is not None)
    return AskResponse(patient_pk=patient_pk, clarified=body.choice is not None, **answer.to_dict())


ENCOUNTER_LIST_DEFAULT_LIMIT = 20
ENCOUNTER_LIST_MAX_LIMIT = 50
_ENCOUNTER_NOT_FOUND = "Encounter not found."


@app.post(
    "/patients/{patient_pk}/encounters",
    response_model=EncounterResponse,
    status_code=201,
    summary="Open an encounter for a patient of the caller's own clinic. DOCTOR only. Makes no AI call.",
)
def open_patient_encounter(
    patient_pk: int = Path(gt=0),
    request: Optional[EncounterCreateRequest] = None,
    auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR)),
):
    # Clinic and creator come only from the session; the (field-less) body
    # rejects any client-supplied value, clinic_id included. A patient of
    # another clinic gets the same 404 as a nonexistent one.
    patient = _resolve_linked_patient(patient_pk, auth.clinic_id)
    try:
        encounter = open_encounter(
            clinic_id=auth.clinic_id,
            patient_pk=patient["patient_pk"],
            created_by_user_id=auth.user_id,
            # Migration 018: encounter enrichment (all optional).
            visit_type=request.visit_type if request else None,
            chief_complaint=request.chief_complaint if request else None,
            vitals_bp=request.vitals_bp if request else None,
            vitals_weight_kg=request.vitals_weight_kg if request else None,
            vitals_temperature_c=request.vitals_temperature_c if request else None,
            follow_up_date=request.follow_up_date if request else None,
        )
    except OpenEncounterExistsError:
        raise HTTPException(status_code=409, detail="This patient already has an open encounter.")
    except EncounterClinicMismatchError:
        logger.error("event=encounter_rejected_by_database")
        raise HTTPException(status_code=404, detail="Patient not found.")
    logger.info("event=encounter_opened encounter_pk=%s user_id=%s", encounter["encounter_pk"], auth.user_id)
    return EncounterResponse(**encounter)


@app.get(
    "/patients/{patient_pk}/encounters",
    response_model=EncounterListResponse,
    summary="Encounters for one patient of the caller's own clinic, newest first. DOCTOR/ADMIN only. Read-only.",
)
def list_patient_encounters(
    patient_pk: int = Path(gt=0),
    limit: int = Query(default=ENCOUNTER_LIST_DEFAULT_LIMIT, ge=1, le=ENCOUNTER_LIST_MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=100000),
    auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR, ROLE_ADMIN)),
):
    patient = _resolve_linked_patient(patient_pk, auth.clinic_id)
    encounters, has_more = list_encounters_for_patient(auth.clinic_id, patient["patient_pk"], limit, offset)
    return EncounterListResponse(
        encounters=[EncounterResponse(**e) for e in encounters],
        limit=limit,
        offset=offset,
        has_more=has_more,
    )


@app.get(
    "/encounters/{encounter_pk}",
    response_model=EncounterResponse,
    summary="One encounter of the caller's own clinic. DOCTOR/ADMIN only. Read-only.",
)
def get_encounter(
    encounter_pk: int = Path(gt=0),
    auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR, ROLE_ADMIN)),
):
    encounter = get_encounter_for_clinic(encounter_pk, auth.clinic_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail=_ENCOUNTER_NOT_FOUND)
    return EncounterResponse(**encounter)


@app.post(
    "/encounters/{encounter_pk}/close",
    response_model=EncounterResponse,
    summary="Close an OPEN encounter of the caller's own clinic (CLOSED is final). DOCTOR only.",
)
def close_encounter_endpoint(
    encounter_pk: int = Path(gt=0),
    auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR)),
):
    try:
        encounter = close_encounter(encounter_pk, auth.clinic_id, actor_user_id=auth.user_id)
    except EncounterNotFoundError:
        raise HTTPException(status_code=404, detail=_ENCOUNTER_NOT_FOUND)
    except EncounterAlreadyClosedError:
        raise HTTPException(status_code=409, detail="This encounter is already closed.")
    logger.info("event=encounter_closed encounter_pk=%s user_id=%s", encounter["encounter_pk"], auth.user_id)
    return EncounterResponse(**encounter)


def _resolve_linked_patient(patient_pk: Optional[int], clinic_id: str) -> Optional[dict]:
    """Patient Registry Phase 1: the registered patient a preview request
    links to, looked up ONLY within the caller's authenticated clinic. A
    patient_pk from another clinic gets the same 404 as one that does not
    exist, so a preview request cannot probe another clinic's registry."""
    if patient_pk is None:
        return None
    patient = get_patient_for_clinic(patient_pk, clinic_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    return patient


def _preview_identity(request, patient: Optional[dict]) -> tuple:
    """(patient_name, patient_phone, patient_id) to store on the preview.
    Linked: the registry is authoritative, so the stored identity is the
    registered patient's, not whatever free text the request carried.
    Unlinked: the request's own fields, exactly as before Phase 1."""
    if patient is None:
        return request.patient_name, request.patient_phone, request.patient_id
    return patient["display_name"], patient["phone"], patient["clinic_patient_id"]


def _resolve_preview_encounter(encounter_pk: Optional[int], patient: Optional[dict], clinic_id: str) -> Optional[int]:
    """Encounter Phase 1B: validates an optional encounter link for a new
    preview, entirely from the authenticated clinic context:
      - an encounter needs the preview's patient_pk (422 otherwise);
      - the encounter must exist in the caller's clinic -- another clinic's
        encounter gets the same 404 as a nonexistent one;
      - it must belong to the SAME patient (409) and still be OPEN (409).
    Runs before any AI call or write. Migration 012's trigger re-checks the
    same rules at insert time."""
    if encounter_pk is None:
        return None
    if patient is None:
        raise HTTPException(status_code=422, detail="encounter_pk requires patient_pk.")
    encounter = get_encounter_for_clinic(encounter_pk, clinic_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail=_ENCOUNTER_NOT_FOUND)
    if encounter["patient_pk"] != patient["patient_pk"]:
        raise HTTPException(status_code=409, detail="This encounter belongs to a different patient.")
    if encounter["status"] != "OPEN":
        raise HTTPException(status_code=409, detail="This encounter is closed.")
    return encounter["encounter_pk"]


def _create_preview_or_404(**kwargs) -> str:
    """create_preview(), mapping migration 009's write-time clinic-isolation
    rejection to the same 404 as _resolve_linked_patient -- reachable only if
    the patient check above were somehow bypassed."""
    try:
        return create_preview(**kwargs)
    except PatientLinkError:
        logger.error("event=patient_link_rejected_by_database")
        raise HTTPException(status_code=404, detail="Patient not found.")
    except EncounterLinkError:
        # e.g. the encounter was closed between the check above and this insert.
        logger.error("event=encounter_link_rejected_by_database")
        raise HTTPException(status_code=409, detail="This encounter is no longer open for this patient.")


def _record_shadow_preview(**draft) -> None:
    """Phase 3A.4 shadow recording of a stored draft: best-effort and
    evaluation-only (backend/shadow_store.py). Imported here, not at module
    level, and wrapped, so nothing about it -- not even a failed import --
    can change or fail the preview response."""
    try:
        from backend.shadow_store import record_preview
        record_preview(**draft)
    except Exception:
        logger.warning("preview_id=%s event=shadow_record_failed stage=PREVIEW", draft.get("preview_id"))


def _record_shadow_confirm(**committed) -> None:
    """Phase 3A.4 shadow recording of a committed confirmation: called only
    after the confirm transaction has committed and its connection closed,
    and after corrections logging. Best-effort and evaluation-only, like
    _record_shadow_preview: nothing about it can change the response or any
    clinical write."""
    try:
        from backend.shadow_store import record_confirm
        record_confirm(**committed)
    except Exception:
        logger.warning("preview_id=%s event=shadow_record_failed stage=CONFIRM", committed.get("preview_id"))


@app.post(
    "/prescribe/preview",
    response_model=PreviewResponse,
    summary="Parse clinical notes via AI into a proposed dosha + herb list. Writes nothing.",
)
def prescribe_preview(
    request: PreviewRequest,
    auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR)),
    _rate_limit: None = Depends(limiter("preview")),
):
    request_id = uuid.uuid4().hex[:12]

    # Patient Registry Phase 1: resolve an optional patient link FIRST, within
    # the caller's own clinic, so an invalid link fails before any AI call.
    linked_patient = _resolve_linked_patient(request.patient_pk, auth.clinic_id)
    linked_encounter_pk = _resolve_preview_encounter(request.encounter_pk, linked_patient, auth.clinic_id)
    stored_name, stored_phone, stored_patient_id = _preview_identity(request, linked_patient)

    # --- PII air-gap: real identity -> local tokenization -> Gemini -> local rehydration ---
    # Never: real identity -> Gemini -> redact response. Tokenization
    # happens here, before anything else touches this request.
    #
    # expand_name_variants adds the individual words of patient_name (e.g.
    # "John" and "Mathew" alongside "John Mathew") so a later bare
    # first-name or surname mention in free-text notes collapses to the
    # SAME token as the full name, instead of leaking through untokenized.
    pii_values = [
        *expand_name_variants(request.patient_name),
        request.patient_phone,
        request.patient_id,
    ]
    if linked_patient is not None:
        # The registry's identifiers are tokenized too, so a note mentioning
        # the registered name/phone/OP number never reaches Gemini untokenized.
        pii_values += [
            *expand_name_variants(linked_patient["display_name"]),
            linked_patient["phone"],
            linked_patient["clinic_patient_id"],
        ]
    sanitized_notes, pii_mapping = tokenize_payload(request.notes, pii_values)
    patient_token = next(iter(pii_mapping), None)

    # Pre-flight invariant, checked on the exact text about to be sent.
    # If this raises, we abort BEFORE calling Gemini -- the AI call below
    # must never be reached in that case.
    try:
        assert_no_pii_leak(sanitized_notes, pii_values)
    except PIIViolationError:
        logger.error(
            "request_id=%s event=pii_leak_detected action=aborted_before_ai_call",
            request_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Internal privacy safeguard triggered; request aborted before reaching the AI service.",
        )

    # 1. Parse the SANITIZED notes via the AI copilot. Gemini only ever
    #    sees `sanitized_notes` -- request.notes (raw) is never passed to it.
    logger.info(
        "request_id=%s patient_token=%s event=ai_call_start",
        request_id, patient_token,
    )
    ai_started_at = time.monotonic()
    try:
        # A0 Step 0: scope the copilot's stock tool to the authenticated
        # doctor's OWN clinic (server-derived auth.clinic_id, never request input).
        parsed: ClinicalParse = parse_clinical_notes(sanitized_notes, clinic_id=auth.clinic_id)
    except ValueError as e:
        error_msg = str(e)
        logger.error(
            "request_id=%s patient_token=%s event=ai_call_failed reason=%s",
            request_id, patient_token, error_msg,
        )
        if error_msg == "AI_SERVICE_UNAVAILABLE":
            # X-Error-Code lets a caller (ui/app.py's manual-fallback
            # offer) distinguish "the AI itself is unreachable -- quota,
            # outage, timeout" from every other 4xx/5xx this endpoint can
            # return, WITHOUT string-matching the human-readable `detail`
            # text (which is free to change wording later without quietly
            # breaking that check). Deliberately not set on any other
            # branch here -- a 400 UnknownHerbError or the 502 just below
            # for a failed de-tokenization are not "AI is down" problems
            # manual entry would actually route around the same way.
            raise HTTPException(
                status_code=502,
                detail="AI parsing failed",
                headers={"X-Error-Code": "AI_SERVICE_UNAVAILABLE"},
            )
        elif error_msg == "INVALID_AI_SCHEMA":
            raise HTTPException(status_code=422, detail="Invalid AI schema returned")
        else:
            raise HTTPException(status_code=500, detail="Internal server error")
    ai_latency_ms = int((time.monotonic() - ai_started_at) * 1000)

    # 2. Rehydrate locally. Even though ClinicalParse no longer asks Gemini
    #    to echo patient identity back, we still rehydrate + scan the full
    #    response defensively: if a token ended up anywhere in the AI's
    #    output, it gets restored here, and a token we DON'T recognize is
    #    treated as an anomaly.
    # Security review 3.1: the model is told placeholders are not clinical
    # content, so ANY [[PAT_...]] token in its structured output (e.g. echoed
    # into dosha_imbalance) is rejected here, BEFORE rehydration could turn it
    # back into the real name inside a field that is persisted into the audit
    # trail and corrections dataset. Same response as any unusable AI reply.
    if find_unresolved_tokens(parsed.model_dump()):
        logger.error("request_id=%s patient_token=%s event=ai_output_echoed_identity_token", request_id, patient_token)
        raise HTTPException(status_code=422, detail="Invalid AI schema returned")
    rehydrated = rehydrate_payload(parsed.model_dump(), pii_mapping)
    leftover_tokens = find_unresolved_tokens(rehydrated)
    if leftover_tokens:
        logger.error(
            "request_id=%s patient_token=%s event=rehydration_incomplete count=%d",
            request_id, patient_token, len(leftover_tokens),
        )
        raise HTTPException(
            status_code=502,
            detail="AI response could not be safely de-tokenized; request aborted.",
        )
    parsed = ClinicalParse.model_validate(rehydrated)
    if contains_identity(parsed.dosha_imbalance, pii_values):
        logger.error("request_id=%s patient_token=%s event=ai_dosha_contained_identity", request_id, patient_token)
        raise HTTPException(status_code=422, detail="Invalid AI schema returned")

    logger.info(
        "request_id=%s patient_token=%s event=ai_call_success latency_ms=%d",
        request_id, patient_token, ai_latency_ms,
    )

    # 3. Normalize and resolve canonical herb names now, so the preview the
    #    doctor sees already shows resolvable, canonical names -- and so
    #    a doctor's later correction at /confirm passes through this exact
    #    same normalize_herb() boundary, not a separate one (an AI-proposed
    #    "Turmeric" and a doctor-typed "turmeric powder" must both resolve
    #    the same way, and both must still 400 on a genuinely unknown herb).
    normalized_herbs = []
    try:
        for herb in parsed.prescribed_herbs:
            canon_name = normalize_herb(herb.name)
            normalized_herbs.append({"name": canon_name, "grams": herb.grams})
    except UnknownHerbError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # A proposal with no herbs is not a prescription (manual previews require
    # at least one herb). Rejected like any other unusable AI reply, before a
    # preview is stored, so there is nothing to confirm; the doctor can retry
    # or write it manually.
    if not normalized_herbs:
        logger.error("request_id=%s patient_token=%s event=ai_proposal_empty", request_id, patient_token)
        raise HTTPException(status_code=422, detail="Invalid AI schema returned")

    # 4. Stash the proposal under a fresh, unguessable preview_id. Nothing
    #    about tokens or Gemini is kept -- only the final, real values plus
    #    enough identity to write a real record once confirmed. This is
    #    the ONLY place this data lives -- persisted now (Phase A, A1) in
    #    the prescription_previews table via backend/preview_store.py,
    #    rather than an in-memory dict that didn't survive a restart.
    #    preview_id itself is still just an opaque, unguessable lookup key
    #    (64 bits of secrets.token_hex(8) entropy, generated inside
    #    create_preview) -- nothing about patient identity, clinic
    #    identity, or time is ever encoded into it.
    #
    #    created_by_session now records the REAL, server-resolved session
    #    identity (auth.session_id, from require_auth above) rather than
    #    an arbitrary caller-supplied header string -- this is what makes
    #    the session-binding check in /prescribe/confirm a real security
    #    control instead of the inert placeholder it used to be (Phase A,
    #    A2). clinic_id likewise now comes from the authenticated user's
    #    own persisted record (auth.clinic_id), not from request.clinic_id
    #    -- the request body still carries clinic_id (the locked request
    #    shape is unchanged), but its value is no longer trusted for
    #    anything authorization-relevant; only the session's own clinic_id
    #    is. See API Contract Addendum v1.2.
    try:
        mark_stale_previews_expired()
    except SchemaNotMigratedError:
        # NOT a transient housekeeping hiccup -- the schema itself is
        # missing a required table. Papering over this as best-effort
        # noise (the old `except Exception` here did exactly that) is
        # what let the Phase A, A1 hard Mac verification failure go
        # unnoticed one line above create_preview() hitting the same
        # missing table with a raw, unhelpful sqlite3 error. Fail loudly
        # and immediately instead -- see preview_store.py's
        # SchemaNotMigratedError docstring for the full incident.
        logger.error("event=schema_not_migrated_detected", exc_info=True)
        raise
    except Exception:
        # Best-effort housekeeping only (see preview_store.py) -- any
        # OTHER failure here must never block creating this preview.
        logger.warning("event=stale_preview_cleanup_failed", exc_info=True)

    preview_id = _create_preview_or_404(
        patient_name=stored_name,
        patient_phone=stored_phone,
        patient_id=stored_patient_id,
        clinic_id=auth.clinic_id,
        dosha_imbalance=parsed.dosha_imbalance,
        prescribed_herbs=normalized_herbs,
        is_manual=False,
        proposal_source="gemini",
        created_by_session=str(auth.session_id),
        patient_pk=linked_patient["patient_pk"] if linked_patient else None,
        encounter_pk=linked_encounter_pk,
    )

    logger.info(
        "request_id=%s patient_token=%s preview_id=%s event=preview_ready",
        request_id, patient_token, preview_id,
    )
    _record_shadow_preview(clinic_id=auth.clinic_id, preview_id=preview_id, is_manual=False,
                           dosha_imbalance=parsed.dosha_imbalance, prescribed_herbs=normalized_herbs)

    return PreviewResponse(
        preview_id=preview_id,
        patient_name=stored_name,
        dosha_imbalance=parsed.dosha_imbalance,
        prescribed_herbs=normalized_herbs,
        proposal_source="gemini",
    )


@app.post(
    "/prescribe/preview/local",
    response_model=PreviewResponse,
    summary="Parse clinical notes via the local AI-1 extractor (T1 tagger + T2 assembly + T3 verifier). No LLM, no network, no API key. Writes nothing.",
)
def prescribe_preview_local(
    request: PreviewRequest,
    auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR)),
    _rate_limit: None = Depends(limiter("preview_local")),
):
    """Local, deterministic draft path using the AI-1 extractor instead of
    Gemini. Reads the sanitized notes through T1/T2/T3 and returns a
    verified ClinicalParse, or 422 if the verifier abstains.

    The only clinical write remains /prescribe/confirm, which makes no AI
    call of any kind. If the local extractor abstains, the doctor can use
    /prescribe/manual-preview (no AI) or /prescribe/preview (Gemini, if an
    API key is configured).
    """
    request_id = uuid.uuid4().hex[:12]

    # Patient Registry Phase 1: resolve an optional patient link FIRST, within
    # the caller's own clinic, so an invalid link fails before any parsing.
    linked_patient = _resolve_linked_patient(request.patient_pk, auth.clinic_id)
    linked_encounter_pk = _resolve_preview_encounter(request.encounter_pk, linked_patient, auth.clinic_id)
    stored_name, stored_phone, stored_patient_id = _preview_identity(request, linked_patient)

    # PII air-gap: tokenize before anything touches the notes.
    pii_values = [
        *expand_name_variants(request.patient_name),
        request.patient_phone,
        request.patient_id,
    ]
    if linked_patient is not None:
        pii_values += [
            *expand_name_variants(linked_patient["display_name"]),
            linked_patient["phone"],
            linked_patient["clinic_patient_id"],
        ]
    sanitized_notes, pii_mapping = tokenize_payload(request.notes, pii_values)

    # Pre-flight invariant: abort before parsing if any PII leaked through.
    try:
        assert_no_pii_leak(sanitized_notes, pii_values)
    except PIIViolationError:
        logger.error(
            "request_id=%s event=pii_leak_detected action=aborted_before_ai_call",
            request_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Internal privacy safeguard triggered; request aborted before parsing.",
        )

    logger.info(
        "request_id=%s patient_token=%s event=local_extractor_start",
        request_id, next(iter(pii_mapping), None),
    )

    # Load the T1 model and run the local extractor pipeline.
    from extractor.pipeline import Extractor
    from extractor.train import load_model

    model_path = pathlib.Path(__file__).resolve().parent.parent / "artifacts" / "models" / "t1_tagger" / "model.json"
    model = load_model(model_path)
    extractor = Extractor(model)

    result = extractor.extract(sanitized_notes)

    if result["abstain"]:
        logger.info(
            "request_id=%s event=local_extractor_abstain violations=%s",
            request_id, result["violations"],
        )
        raise HTTPException(
            status_code=422,
            detail="The local extractor could not verify this prescription from the notes. Use /prescribe/manual-preview to enter it manually, or /prescribe/preview if Gemini is available.",
            headers={"X-Error-Code": "EXTRACTOR_ABSTAINED"},
        )

    parsed: ClinicalParse = result["parse"]

    # Rehydrate locally: any token in the AI's output gets restored here.
    if find_unresolved_tokens(parsed.model_dump()):
        logger.error("request_id=%s event=local_extractor_output_echoed_identity_token", request_id)
        raise HTTPException(status_code=422, detail="Invalid extractor output")
    rehydrated = rehydrate_payload(parsed.model_dump(), pii_mapping)
    leftover_tokens = find_unresolved_tokens(rehydrated)
    if leftover_tokens:
        logger.error(
            "request_id=%s event=local_extractor_rehydration_incomplete count=%d",
            request_id, len(leftover_tokens),
        )
        raise HTTPException(
            status_code=502,
            detail="Extractor response could not be safely de-tokenized; request aborted.",
        )
    parsed = ClinicalParse.model_validate(rehydrated)
    if contains_identity(parsed.dosha_imbalance, pii_values):
        logger.error("request_id=%s event=local_extractor_dosha_contained_identity", request_id)
        raise HTTPException(status_code=422, detail="Invalid extractor output")

    logger.info(
        "request_id=%s event=local_extractor_success",
        request_id,
    )

    # Normalize and resolve canonical herb names.
    normalized_herbs = []
    try:
        for herb in parsed.prescribed_herbs:
            canon_name = normalize_herb(herb.name)
            normalized_herbs.append({"name": canon_name, "grams": herb.grams})
    except UnknownHerbError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not normalized_herbs:
        logger.error("request_id=%s event=local_extractor_empty_proposal", request_id)
        raise HTTPException(status_code=422, detail="Invalid extractor output")

    try:
        mark_stale_previews_expired()
    except SchemaNotMigratedError:
        logger.error("event=schema_not_migrated_detected", exc_info=True)
        raise
    except Exception:
        logger.warning("event=stale_preview_cleanup_failed", exc_info=True)

    preview_id = _create_preview_or_404(
        patient_name=stored_name,
        patient_phone=stored_phone,
        patient_id=stored_patient_id,
        clinic_id=auth.clinic_id,
        dosha_imbalance=parsed.dosha_imbalance,
        prescribed_herbs=normalized_herbs,
        is_manual=False,
        proposal_source="local_extractor",
        created_by_session=str(auth.session_id),
        patient_pk=linked_patient["patient_pk"] if linked_patient else None,
        encounter_pk=linked_encounter_pk,
    )

    logger.info(
        "request_id=%s patient_token=%s preview_id=%s event=local_extractor_preview_ready",
        request_id, next(iter(pii_mapping), None), preview_id,
    )
    _record_shadow_preview(clinic_id=auth.clinic_id, preview_id=preview_id, is_manual=False,
                           dosha_imbalance=parsed.dosha_imbalance, prescribed_herbs=normalized_herbs)

    return PreviewResponse(
        preview_id=preview_id,
        patient_name=stored_name,
        dosha_imbalance=parsed.dosha_imbalance,
        prescribed_herbs=normalized_herbs,
        proposal_source="local_extractor",
    )


@app.post(
    "/prescribe/manual-preview",
    response_model=PreviewResponse,
    summary="AI-outage fallback: doctor enters dosha + herbs directly. Makes no AI call. Writes nothing.",
)
def prescribe_manual_preview(request: ManualPreviewRequest, auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR))):
    # No tokenization, no PII air-gap machinery, no call to Gemini -- there
    # is no free-text notes field here and nothing is ever sent to an
    # external AI service on this path. This endpoint exists purely so a
    # clinic can keep prescribing when /prescribe/preview can't reach
    # Gemini (quota exhausted, outage, timeout -- see the
    # X-Error-Code: AI_SERVICE_UNAVAILABLE header on that failure).
    #
    # Everything below this point is the EXACT same herb-name resolution
    # /prescribe/preview already runs, and the resulting pending preview
    # flows into the EXACT same /prescribe/confirm code (same stock check,
    # same single-use preview/confirm split) -- this endpoint's only job
    # is to hand /confirm a pending preview that didn't come from the AI.
    # A doctor's manually-typed herb name gets exactly the same
    # unknown-herb rejection an AI-proposed one would.
    linked_patient = _resolve_linked_patient(request.patient_pk, auth.clinic_id)
    linked_encounter_pk = _resolve_preview_encounter(request.encounter_pk, linked_patient, auth.clinic_id)
    stored_name, stored_phone, stored_patient_id = _preview_identity(request, linked_patient)

    # Security review 3.1: dosha is free text that is persisted into the
    # audit trail and corrections dataset; it must not carry this patient's
    # identity (request fields or, when linked, the registry's).
    identity = (_identity_values(request.patient_name, request.patient_phone, request.patient_id)
                + _identity_values(stored_name, stored_phone, stored_patient_id))
    if contains_identity(request.dosha_imbalance, identity):
        raise HTTPException(status_code=422, detail=_DOSHA_IDENTITY_DETAIL)

    normalized_herbs = []
    try:
        for herb in request.prescribed_herbs:
            canon_name = normalize_herb(herb.name)
            normalized_herbs.append({"name": canon_name, "grams": herb.grams})
    except UnknownHerbError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        mark_stale_previews_expired()
    except SchemaNotMigratedError:
        # See the matching branch in /prescribe/preview above -- this must
        # fail loudly, not be logged as routine best-effort noise.
        logger.error("event=schema_not_migrated_detected", exc_info=True)
        raise
    except Exception:
        logger.warning("event=stale_preview_cleanup_failed", exc_info=True)

    # is_manual=True marks this preview as never having touched the AI, so
    # /confirm below knows NOT to log it to the corrections table. That
    # table's entire meaning is "what the AI proposed vs. what the doctor
    # confirmed" (see corrections_service.py's module docstring on why
    # it's this project's most defensible long-term asset) -- a manual
    # entry has no AI proposal to compare against. Logging one anyway
    # would silently misrepresent doctor-entered data as an AI proposal
    # the doctor happened to leave unchanged, quietly corrupting the exact
    # dataset that asset depends on being clean.
    preview_id = _create_preview_or_404(
        patient_name=stored_name,
        patient_phone=stored_phone,
        patient_id=stored_patient_id,
        clinic_id=auth.clinic_id,
        dosha_imbalance=request.dosha_imbalance,
        prescribed_herbs=normalized_herbs,
        is_manual=True,
        proposal_source="manual",
        created_by_session=str(auth.session_id),
        patient_pk=linked_patient["patient_pk"] if linked_patient else None,
        encounter_pk=linked_encounter_pk,
    )

    logger.info(
        "preview_id=%s event=manual_preview_created reason=ai_fallback",
        preview_id,
    )
    _record_shadow_preview(clinic_id=auth.clinic_id, preview_id=preview_id, is_manual=True,
                           dosha_imbalance=request.dosha_imbalance, prescribed_herbs=normalized_herbs)

    return PreviewResponse(
        preview_id=preview_id,
        patient_name=stored_name,
        dosha_imbalance=request.dosha_imbalance,
        prescribed_herbs=normalized_herbs,
        proposal_source="manual",
    )


class _IdempotentReplay(Exception):
    """Internal control-flow signal only -- never escapes prescribe_confirm.
    Raised the moment reserve_idempotent_operation() reports an existing,
    fully-populated (SUCCEEDED) row for this (user_id, operation, key), so
    that no business logic below it (herb re-resolution, inventory
    deduction, mark_confirmed, corrections logging) ever runs on a replay
    -- caught immediately below, at the same level as the existing
    `except HTTPException:` handler, specifically so the large existing
    confirm body does not need to be re-indented under a new `if` just to
    make room for this. See backend/idempotency_store.py."""

    def __init__(self, response: JSONResponse):
        self.response = response


@app.post(
    "/prescribe/confirm",
    response_model=PrescriptionResponse,
    summary="Finalize a previewed prescription. Makes no AI call. Single-use.",
)
def prescribe_confirm(
    request: ConfirmRequest,
    auth: AuthenticatedSession = Depends(require_role(ROLE_DOCTOR)),
    # A6.3 hardening (Sep 2026): max_length=200 is the only change here.
    # Before this, the header had no length bound anywhere -- not here,
    # not in idempotency_store.py, not at the SQLite column level (TEXT
    # has no inherent limit) -- so an authenticated caller could send an
    # arbitrarily large Idempotency-Key value, which would be hashed into
    # compute_request_fingerprint()'s JSON and stored verbatim in the
    # idempotency_keys table on every /prescribe/confirm call, unbounded.
    # 200 matches this codebase's own established bound for free-form
    # identifier-like fields (see backend/schemas.py's patient_name /
    # dosha_imbalance, both max_length=200) -- more than 5x this project's
    # own client-generated key (ui/app.py's str(uuid.uuid4()), 36 chars)
    # while ruling out unbounded growth. Empty-string handling is
    # unchanged: `if idempotency_key:` below still treats "" as "no key
    # sent" (falsy), exactly as before -- this bound only rejects values
    # LONGER than 200 characters, with FastAPI's normal 422 response, the
    # same contract every other oversized field in this API already gets.
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key", max_length=200),
    _rate_limit: None = Depends(limiter("confirm")),
):
    # This endpoint makes NO AI call of any kind. Everything below is
    # ordinary, deterministic code -- the doctor's confirmation is what
    # makes anything real. This split (preview proposes, confirm commits)
    # is the single most important safety-critical property of this API:
    # nothing the AI produces is ever automatically finalized.
    #
    # Phase A (A1, persistent previews): the preview lookup, the herb
    # re-resolution, the inventory deduction, the prescription insert, and
    # the preview's PENDING -> CONFIRMED flip all now happen inside ONE
    # SQLite transaction on ONE connection -- not the two separate,
    # sequential operations (an in-memory dict flip, then a separate DB
    # transaction) this endpoint used before persistence. That closes a
    # real gap: with two separate commits, a process crash in the window
    # between the prescription committing and the preview being marked
    # consumed could leave a CONFIRMED prescription next to a still-PENDING
    # (and therefore re-confirmable) preview. With one transaction, that
    # window doesn't exist -- the prescription and the preview's status
    # change either both happen or neither does. BEGIN IMMEDIATE also means
    # a second concurrent confirm attempt on the SAME preview_id blocks on
    # SQLite's own write lock until this one finishes, then correctly sees
    # CONFIRMED and 409s, rather than racing (see
    # backend/preview_store.py's get_pending_preview_for_confirm).
    #
    # Corrections logging is deliberately NOT part of this transaction --
    # see the comment further down, right above that call, for why folding
    # it in would violate the locked contract's explicit "logging is
    # best-effort and can never roll back or block the prescription"
    # guarantee.
    # Idempotency (Phase B, A4). Computed unconditionally, cheaply, before
    # opening the transaction -- it is only ever actually USED below if the
    # caller sent an Idempotency-Key header. Fingerprinted on exactly the
    # request fields that determine clinical content (preview_id,
    # dosha_imbalance, prescribed_herbs) as SENT -- not the resolved/
    # normalized final values -- see compute_request_fingerprint()'s
    # docstring for why. `idempotent_replay_response` and
    # `idempotency_reservation_active` are set inside the transaction below
    # and read again after it closes.
    idempotent_replay_response: Optional[JSONResponse] = None
    idempotency_reservation_active = False
    request_fingerprint = compute_request_fingerprint(
        preview_id=request.preview_id,
        dosha_imbalance=request.dosha_imbalance,
        prescribed_herbs=(
            [h.model_dump() for h in request.prescribed_herbs]
            if request.prescribed_herbs is not None
            else None
        ),
    )

    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Idempotency reservation/replay (Phase B, A4) -- MUST run
            # first, before any business logic, so that a replay never
            # re-executes ANYTHING below (herb re-resolution, inventory
            # deduction, mark_confirmed, or -- critically -- the
            # post-commit corrections log further down, which a replay
            # must never duplicate). `Idempotency-Key` is optional at the
            # API level (see API Contract Addendum v1.4): a caller that
            # omits it gets exactly the pre-A4 behavior below, unchanged,
            # with no dedup guarantee. auth.user_id / auth.clinic_id here
            # are the SAME server-derived values used everywhere else in
            # this handler -- never anything client-supplied -- which is
            # what makes cross-user/cross-clinic replay structurally
            # impossible; see backend/idempotency_store.py's module
            # docstring for the full argument.
            if idempotency_key:
                try:
                    reservation = reserve_idempotent_operation(
                        conn,
                        user_id=auth.user_id,
                        operation=OPERATION_PRESCRIBE_CONFIRM,
                        idempotency_key=idempotency_key,
                        clinic_id=auth.clinic_id,
                        request_fingerprint=request_fingerprint,
                    )
                except IdempotencyConflictError as e:
                    raise HTTPException(status_code=409, detail=str(e))
                except IdempotencyClinicMismatchError as e:
                    raise HTTPException(status_code=409, detail=str(e))

                if reservation.is_replay:
                    # Nothing new to write -- return the ORIGINAL response
                    # unchanged. Rolled back (not committed) just below,
                    # for symmetry with every other early-exit path in this
                    # handler; either is a no-op here since the failed
                    # reservation INSERT left nothing to undo.
                    raise _IdempotentReplay(JSONResponse(
                        content=json.loads(reservation.response_body_json),
                        status_code=reservation.response_status_code,
                    ))

                idempotency_reservation_active = True

            # Clinic isolation (Phase A, A3). auth.clinic_id -- THIS
            # request's live, server-resolved clinic_id from
            # resolve_session()'s join, never anything a client asserted
            # directly -- is passed into get_pending_preview_for_confirm
            # itself, which checks clinic ownership BEFORE it ever
            # classifies the row's lifecycle status (pending / expired /
            # already confirmed). That ordering is load-bearing, not
            # cosmetic: a Mac-verification run of tests/test_rbac.py found
            # that an EARLIER version of this code checked clinic_id only
            # AFTER calling get_pending_preview_for_confirm(conn,
            # preview_id) with no clinic argument at all -- which meant a
            # foreign-clinic preview that happened to already be CONFIRMED
            # raised PreviewAlreadyConfirmedError (-> 409, "has already
            # been confirmed") before this handler ever got a row back to
            # compare clinic_id against, leaking that foreign preview's
            # existence and lifecycle state to a caller with no
            # authorization to know either. See
            # backend/preview_store.py's get_pending_preview_for_confirm
            # docstring for the full incident and the fix. The upshot: a
            # foreign-clinic preview_id now raises PreviewNotFoundError
            # -- indistinguishable from a preview_id that was never
            # issued at all -- regardless of whether that foreign row is
            # PENDING, EXPIRED, or CONFIRMED; only a SAME-clinic preview
            # ever reaches the status-specific 409/404 branches below.
            try:
                entry = get_pending_preview_for_confirm(conn, request.preview_id, auth.clinic_id)
            except PreviewAlreadyConfirmedError as e:
                # Distinct from 404 specifically so a doctor's browser
                # retry of an already-successful confirm doesn't look like
                # the preview simply vanished. Only reachable for a
                # SAME-clinic preview -- see the clinic-ownership check
                # inside get_pending_preview_for_confirm above.
                raise HTTPException(status_code=409, detail=str(e))
            except (PreviewNotFoundError, PreviewExpiredError):
                # The locked contract deliberately does not distinguish
                # "never existed" from "expired" to the caller -- both are
                # "go re-run /preview". A cross-clinic preview (in ANY
                # lifecycle state) falls into this same branch via
                # PreviewNotFoundError -- see above.
                raise HTTPException(
                    status_code=404,
                    detail="Preview not found or expired. Please re-run /prescribe/preview.",
                )

            # Session binding (Phase A, A2 -- now a real, enforced check).
            # A preview created under one authenticated session must not
            # be confirmable by a different one: entry["created_by_session"]
            # was written by /prescribe/preview or /prescribe/manual-preview
            # as str(auth.session_id) for the REAL, server-resolved session
            # that created it, and it's compared here against THIS
            # request's own real, server-resolved auth.session_id -- never
            # against a client-supplied header value directly. Two
            # different users always get two different session_ids (see
            # backend/auth_service.py's create_session), so this is exactly
            # "does the session confirming match the session that created
            # this preview," and a logged-out (revoked) or expired token
            # never even reaches this line -- require_auth's 401 above
            # rejects it first.
            #
            # The `is not None` guard is defensive-only, for any
            # pre-existing preview row created before this migration (a
            # preview's 30-minute TTL makes this vanishingly unlikely in
            # practice, but failing closed here -- rejecting instead of
            # crashing -- costs nothing and breaks nothing real).
            if entry["created_by_session"] is not None and entry["created_by_session"] != str(auth.session_id):
                raise HTTPException(status_code=403, detail="This preview belongs to a different session.")

            # Encounter Phase 1B: the encounter comes ONLY from the stored
            # preview (ConfirmRequest has no encounter field). Re-checked here,
            # inside this BEGIN IMMEDIATE transaction -- which holds SQLite's
            # write lock, so the encounter cannot be closed between this check
            # and the prescription insert below. A draft whose encounter was
            # closed after it was created cannot be confirmed into it.
            # Migration 012's trigger enforces the same rule on the insert.
            if entry["encounter_pk"] is not None:
                encounter_row = conn.execute(
                    "SELECT status, patient_pk FROM encounters WHERE encounter_pk = ? AND clinic_id = ?",
                    (entry["encounter_pk"], entry["clinic_id"]),
                ).fetchone()
                if (
                    encounter_row is None
                    or encounter_row["patient_pk"] != entry["patient_pk"]
                    or encounter_row["status"] != "OPEN"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="This draft's encounter is closed. Open a new encounter and draft again.",
                    )

            final_dosha = (
                request.dosha_imbalance
                if request.dosha_imbalance is not None
                else entry["ai_dosha_imbalance"]
            )
            # Security review 3.1: nothing identifying may reach the audit
            # event or corrections row through the free-text dosha.
            if contains_identity(
                final_dosha,
                _identity_values(entry["patient_name"], entry["patient_phone"], entry["patient_id"]),
            ):
                raise HTTPException(status_code=422, detail=_DOSHA_IDENTITY_DETAIL)
            final_herbs_raw = (
                [h.model_dump() for h in request.prescribed_herbs]
                if request.prescribed_herbs is not None
                else json.loads(entry["ai_prescribed_herbs_json"])
            )

            # Re-resolve herb names through the SAME normalize_herb()
            # boundary /preview used, in case a doctor's correction used a
            # different alias for a herb than the AI did. Grams are
            # already bounded (> 0, <= 10000) by the same PrescribedHerb
            # Pydantic model /preview's response uses -- a doctor's
            # correction gets exactly the same validation an AI proposal
            # does, not a looser one.
            normalized_final_herbs = []
            try:
                for herb in final_herbs_raw:
                    canon_name = normalize_herb(herb["name"])
                    normalized_final_herbs.append({"name": canon_name, "grams": herb["grams"]})
            except UnknownHerbError as e:
                # Preview is intentionally left untouched (not consumed)
                # here -- the doctor can correct the herb name and confirm
                # again. The transaction is rolled back below (nothing
                # was written yet regardless), so this is automatic.
                raise HTTPException(status_code=400, detail=str(e))

            # Defensive: a prescription must have at least one herb. The
            # request model already refuses an empty override, and AI previews
            # with no herbs are refused at /prescribe/preview; this also covers
            # an empty proposal stored before those checks existed. Raised
            # inside the transaction, so the rollback below also undoes the
            # idempotency reservation, and the preview stays PENDING for a
            # corrected confirm.
            if not normalized_final_herbs:
                raise HTTPException(status_code=422, detail=_EMPTY_PRESCRIPTION_DETAIL)

            # Duplicate lines of one herb are merged into one prescription
            # item by deduct_and_prescribe. The merged total is held to the
            # same per-herb bound as a single line; before this check a larger
            # total was rejected only when the response model was built, as a
            # rolled-back generic 500.
            for merged_line in merge_herb_lines(normalized_final_herbs):
                herb_name, total = merged_line["name"], merged_line["grams"]
                if total > _MAX_GRAMS_PER_HERB:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Total for herb '{herb_name}' is {total} g after combining duplicate lines; "
                               f"the maximum is {_MAX_GRAMS_PER_HERB} g.",
                    )

            try:
                transaction_result = deduct_and_prescribe(
                    patient_name=entry["patient_name"],
                    dosha=final_dosha,
                    raw_herbs=normalized_final_herbs,
                    clinic_id=entry["clinic_id"],
                    conn=conn,
                    # Patient Registry Phase 1: copied from the STORED preview
                    # row, never from the confirm request (ConfirmRequest has
                    # no patient field), inside this same transaction.
                    patient_pk=entry["patient_pk"],
                    encounter_pk=entry["encounter_pk"],
                    # Migration 021: where the proposal came from, copied
                    # from the stored preview row.
                    proposal_source=entry["proposal_source"],
                )
            except InsufficientStockError as e:
                # Preview is intentionally left untouched (not consumed)
                # here -- the doctor can lower a quantity or drop a herb
                # and confirm again, without needing a brand new /preview
                # (and a brand new AI call) just because this attempt
                # didn't have enough stock.
                raise HTTPException(status_code=409, detail=str(e))

            # Tamper-evident audit event (Phase A5). Deliberately INSIDE
            # this same transaction, AFTER the prescription exists (so
            # prescription_id is real) and BEFORE mark_confirmed flips the
            # preview -- per the approved A5 design, the prescription and
            # its audit record must either both commit or both roll back,
            # unlike the best-effort `corrections` logging further below
            # (which is deliberately OUTSIDE this transaction -- see that
            # comment for why those two are NOT the same guarantee).
            # entry["is_manual"] (the preview's OWN persisted flag, not
            # re-derived) decides AI_ASSISTED vs. MANUAL exactly the same
            # way it already decides whether corrections logging runs
            # below -- see backend/audit_store.py's module docstring on
            # why a manual preview's ai_* columns must never be reported
            # as a real AI proposal.
            #
            # auth.user_id / entry["clinic_id"] here are the same
            # server-derived values used everywhere else in this handler
            # -- never anything client-supplied (Phase A, A2/A3) -- see
            # backend/audit_store.py's ACTOR / CLINIC ATTRIBUTION section.
            #
            # Any exception here propagates out of this transaction body
            # and is caught by the generic `except Exception:` handler
            # below, which rolls back EVERYTHING (prescription and
            # inventory deduction included) and returns 500 -- an audit
            # write failure is never silent and never leaves a committed
            # prescription with no corresponding audit trail. Logged here,
            # explicitly, before re-raising, specifically so this failure
            # mode is distinguishable in logs from any other unexpected
            # transaction error.
            try:
                audit_event_data = build_prescription_confirmed_event_data(
                    is_manual=bool(entry["is_manual"]),
                    ai_dosha_imbalance=entry["ai_dosha_imbalance"] if not entry["is_manual"] else None,
                    ai_prescribed_herbs=(
                        json.loads(entry["ai_prescribed_herbs_json"]) if not entry["is_manual"] else None
                    ),
                    confirmed_dosha_imbalance=final_dosha,
                    confirmed_prescribed_herbs=normalized_final_herbs,
                )
                create_audit_event(
                    conn,
                    clinic_id=entry["clinic_id"],
                    actor_user_id=auth.user_id,
                    event_type=AUDIT_EVENT_TYPE_PRESCRIPTION_CONFIRMED,
                    prescription_id=transaction_result["prescription_id"],
                    preview_id=request.preview_id,
                    event_data=audit_event_data,
                )
            except Exception:
                logger.error(
                    "prescription_id=%s event=audit_event_write_failed action=rolling_back_transaction",
                    transaction_result["prescription_id"],
                    exc_info=True,
                )
                raise

            # Only now -- inside the same transaction as the prescription
            # that just succeeded above, not after some separate later
            # step -- do we flip the preview to CONFIRMED. This is what
            # makes /confirm single-use: a second call with the same
            # preview_id will hit the PreviewAlreadyConfirmedError check
            # above and get 409, never a second prescription.
            mark_confirmed(conn, request.preview_id, final_dosha, normalized_final_herbs)

            # Built here, inside the transaction, rather than after commit
            # as before A4 -- so that a fresh idempotency reservation can
            # be completed with the EXACT bytes this handler is about to
            # return, atomically, in the same transaction as the
            # prescription itself (Phase B, A4's approved design: response
            # write, reservation completion, and commit all happen
            # together or not at all).
            response_payload = PrescriptionResponse(
                patient_name=transaction_result["patient_name"],
                dosha_imbalance=transaction_result["dosha_imbalance"],
                prescribed_herbs=transaction_result["prescribed_herbs"],
                prescription_id=transaction_result["prescription_id"],
                created_at=transaction_result["created_at"],
                proposal_source=transaction_result["proposal_source"],
            )

            if idempotency_reservation_active:
                complete_idempotent_reservation(
                    conn,
                    user_id=auth.user_id,
                    operation=OPERATION_PRESCRIBE_CONFIRM,
                    idempotency_key=idempotency_key,
                    response_status_code=200,
                    response_body_json=response_payload.model_dump_json(),
                )
        except _IdempotentReplay as replay:
            conn.rollback()
            idempotent_replay_response = replay.response
        except HTTPException:
            conn.rollback()
            raise
        except SchemaNotMigratedError:
            # Same class of failure as the mark_stale_previews_expired()
            # call sites above -- NOT a normal "unexpected internal error"
            # to log-and-mask generically. Distinguished here (rather than
            # falling into the bare `except Exception` below, which used to
            # convert this into a 500 with NO log line at all) so an
            # operator can actually see "the schema is unmigrated" in the
            # logs instead of an opaque, unlogged 500. The client-facing
            # response is unchanged (still 500) -- this is purely about
            # server-side visibility, not the locked API contract.
            conn.rollback()
            logger.error("event=schema_not_migrated_detected", exc_info=True)
            raise HTTPException(status_code=500, detail="Transaction failed due to an internal error.")
        except Exception:
            # Catch unexpected errors to prevent internal data leaks.
            conn.rollback()
            raise HTTPException(status_code=500, detail="Transaction failed due to an internal error.")
        else:
            conn.commit()
    finally:
        conn.close()

    if idempotent_replay_response is not None:
        # Replay path: return the caller's ORIGINAL response unchanged.
        # Nothing below this point -- corrections logging included -- ever
        # runs for a replay. See backend/idempotency_store.py and API
        # Contract Addendum v1.4.
        return idempotent_replay_response

    # Data flywheel: log what the AI proposed vs what was actually
    # confirmed, on every successful call, regardless of whether anything
    # changed. This is best-effort and happens AFTER the transaction above
    # has already committed -- a failure here is a lost data point, never
    # a lost or rolled-back prescription. This is why it is deliberately
    # OUTSIDE the `with`-style transaction block above: folding it into
    # that same atomic unit would mean a corrections-logging failure could
    # roll back an already-successful prescription, directly violating the
    # locked contract's explicit guarantee that this logging step "can
    # never roll back or block the prescription." Note this table stores
    # only dosha/herb data, never patient identity: the data flywheel
    # should stay as privacy-minimized as the rest of this system, not
    # become a reason to retain more than it needs.
    #
    # Deliberately skipped for a manual-fallback preview (see
    # /prescribe/manual-preview): there was no AI proposal for this
    # prescription at all, so there is nothing correct to log here --
    # logging entry's ai_dosha_imbalance/ai_prescribed_herbs_json (which
    # for a manual preview are just the doctor's own input, copied) would
    # record a fabricated "AI proposed exactly this and was unchanged"
    # data point that never happened, quietly polluting the one dataset
    # this project is betting its long-term defensibility on.
    if entry["is_manual"]:
        logger.info(
            "prescription_id=%s event=correction_log_skipped reason=manual_entry",
            transaction_result["prescription_id"],
        )
    else:
        try:
            log_correction(
                prescription_id=transaction_result["prescription_id"],
                clinic_id=entry["clinic_id"],
                ai_dosha_imbalance=entry["ai_dosha_imbalance"],
                ai_prescribed_herbs=json.loads(entry["ai_prescribed_herbs_json"]),
                confirmed_dosha_imbalance=final_dosha,
                confirmed_prescribed_herbs=normalized_final_herbs,
                proposal_source=entry["proposal_source"],
            )
        except Exception:
            logger.error(
                "prescription_id=%s event=correction_log_failed",
                transaction_result["prescription_id"],
                exc_info=True,
            )

    # Phase 3A.4 shadow recording: after the commit, the replay return and
    # corrections logging; evaluates the final committed values.
    _record_shadow_confirm(clinic_id=entry["clinic_id"], preview_id=request.preview_id,
                           prescription_id=transaction_result["prescription_id"],
                           is_manual=bool(entry["is_manual"]),
                           dosha_imbalance=final_dosha, prescribed_herbs=normalized_final_herbs)
    return response_payload


@app.get(
    "/audit/export",
    response_model=AuditExportResponse,
    summary="Read-only audit trail: every AI proposal vs. doctor confirmation for a clinic. Contains no patient identity. Now requires ADMIN/AUDITOR authentication (Phase A, A3 -- see API Contract Addendum v1.3).",
)
def audit_export(
    clinic_id: Optional[str] = None,
    auth: AuthenticatedSession = Depends(require_role(ROLE_ADMIN, ROLE_AUDITOR)),
):
    # Phase A, A3: this endpoint was, until now, completely unauthenticated
    # and trusted an arbitrary client-supplied `clinic_id` query parameter
    # outright -- anyone who knew (or guessed) a clinic_id could pull that
    # clinic's entire audit trail with no login at all. That is now closed
    # in two independent layers, per this module's own "role check and
    # clinic check are separate questions" rule (see backend/rbac.py):
    #
    #   1. ROLE: only ADMIN or AUDITOR may call this at all (require_role
    #      above) -- a DOCTOR's valid session gets a 403 here, per the
    #      documented permission matrix. require_role's own require_auth
    #      dependency means an unauthenticated caller gets 401 first and
    #      never learns anything about the role requirement.
    #   2. CLINIC: the data actually returned is always scoped to
    #      auth.clinic_id -- the authenticated caller's OWN, server-derived
    #      clinic, from resolve_session()'s live join, never from anything
    #      the client sends. The `clinic_id` query parameter still exists
    #      in the request shape (so a caller explicitly naming their own
    #      clinic is not an error -- e.g. a UI that always passes it), but
    #      it is now checked, not trusted: if present it MUST equal
    #      auth.clinic_id, or the request is rejected outright with 403
    #      rather than silently substituting the caller's real clinic for
    #      whatever they asked for (a silent substitution would let a
    #      caller keep probing different clinic_id values and infer
    #      something from response differences even though the DATA
    #      returned would always be their own -- rejecting outright avoids
    #      that ambiguity entirely). This is unlike the /prescribe/confirm
    #      cross-clinic case above: there, a preview genuinely may or may
    #      not exist in the caller's own clinic, so hiding that behind 404
    #      is the correct multi-tenant posture. Here, `clinic_id` is not a
    #      resource lookup key at all -- it's an assertion about whose data
    #      the caller expects back -- so a wrong assertion is a plain,
    #      undisguised 403, the same way a role mismatch is.
    if clinic_id is not None and clinic_id != auth.clinic_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot access another clinic's audit data.",
        )

    # Pure read path -- no AI call, no write, no interaction with the
    # preview/confirm flow above. This is meant to be safe to call as
    # often as anyone likes, including by a future scheduled job that
    # mines it (see backend/corrections_service.py's module docstring on
    # why this table is the project's most defensible long-term asset).
    rows = get_corrections_for_clinic(auth.clinic_id)
    records = [AuditRecord(**row) for row in rows]
    changed_count = sum(1 for r in records if r.was_changed)

    return AuditExportResponse(
        clinic_id=auth.clinic_id,
        generated_at=datetime.now(timezone.utc),
        record_count=len(records),
        changed_count=changed_count,
        records=records,
        integrity_notes=[
            "Every record reflects a prescription a doctor explicitly confirmed at /prescribe/confirm -- "
            "nothing in this system is ever dispensed automatically.",
            "/prescribe/confirm makes zero calls to any external AI service; the deduction and the record "
            "below are written by deterministic code, not an AI decision.",
            "This table never stores patient identity (name, phone, or ID) by design -- it exists to audit "
            "the AI-proposal-vs-confirmation pipeline, not to serve as a patient record lookup.",
            "'was_changed' is true wherever the doctor's confirmed dosha reading or herb list differs from "
            "the AI's original proposal in any way, however small.",
        ],
    )


@app.get(
    "/audit/events",
    response_model=AuditEventsExportResponse,
    summary=(
        "Tamper-evident audit trail (Phase A5) -- every PRESCRIPTION_CONFIRMED event for a clinic, "
        "hash-chained and integrity-checked live on every call. Requires ADMIN/AUDITOR authentication, "
        "same as GET /audit/export."
    ),
)
def audit_events(
    clinic_id: Optional[str] = None,
    auth: AuthenticatedSession = Depends(require_role(ROLE_ADMIN, ROLE_AUDITOR)),
):
    # Identical RBAC/clinic-isolation posture as GET /audit/export just
    # above -- see that endpoint's own comments for the full reasoning
    # (role check via require_role's own require_auth dependency: 401
    # before 403, generic message, no role enumeration; clinic check:
    # data is ALWAYS scoped to auth.clinic_id, and an explicit clinic_id
    # query parameter is checked, not trusted -- a mismatch is a plain
    # 403, never a silent substitution). Phase A5 does not introduce a
    # second authorization model; it reuses A3's exactly, per this
    # project's "one source of truth for who is allowed to do what"
    # convention (backend/rbac.py).
    if clinic_id is not None and clinic_id != auth.clinic_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot access another clinic's audit data.",
        )

    # Pure read path -- no write, no interaction with the prescribe/confirm
    # flow above beyond reading what it already committed.
    rows = get_audit_events_for_clinic(auth.clinic_id)
    events = [AuditEventRecord(**row) for row in rows]

    # Live integrity check, scoped to THIS caller's own clinic, on every
    # single call -- never a cached or precomputed flag (see
    # AuditEventsExportResponse's own docstring). A direct database edit
    # to a historical row is reflected the very next time anyone reads
    # this endpoint.
    report = verify_audit_integrity(auth.clinic_id)

    return AuditEventsExportResponse(
        clinic_id=auth.clinic_id,
        generated_at=datetime.now(timezone.utc),
        event_count=len(events),
        chain_valid=report.valid,
        integrity_violations=[
            f"event_id={v.event_id}: {v.reason}" for v in report.violations
        ],
        events=events,
    )
