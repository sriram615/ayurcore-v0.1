from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from typing_extensions import Annotated
from typing import Any, Dict, List, Optional
from datetime import datetime


class PrescribedHerb(BaseModel):
    # A6.3 hardening (Sep 2026): max_length=200 closes the one bound this
    # field was missing -- it previously had NO length constraint at all,
    # unlike every other free-text identifier field in this file
    # (patient_name/dosha_imbalance are both max_length=200). name is used
    # as a REQUEST field via ManualPreviewRequest.prescribed_herbs and
    # ConfirmRequest.prescribed_herbs (both doctor-supplied), so an
    # unbounded value here previously reached backend.herb_resolver.
    # normalize_herb() (a .strip().lower() dict lookup) with no upper
    # bound on the string's size first. 200 matches this file's own
    # existing convention for this class of field; every real canonical
    # herb name and alias in backend/herb_resolver.py's HERB_ALIASES is
    # well under 30 characters, so this is not a tight fit -- it simply
    # removes "unbounded" as a possibility while leaving generous room
    # for any legitimate spelling, transliteration, or botanical-name
    # variant a doctor might type.
    name: str = Field(max_length=200)
    grams: int = Field(gt=0, le=10000)


class ClinicalParse(BaseModel):
    # NOTE: patient identity is deliberately NOT parsed by the AI. The
    # doctor supplies it explicitly via PreviewRequest.patient_name, which
    # is what gets persisted -- we never trust an LLM's echo of identity as
    # the source of truth for a clinical record. The AI only ever sees a
    # tokenized version of the notes anyway (see backend/pii_sanitizer.py),
    # so it has nothing real to extract here.
    dosha_imbalance: str
    prescribed_herbs: List[PrescribedHerb]


class PreviewRequest(BaseModel):
    """Request body for POST /prescribe/preview.

    This is the ONLY endpoint that talks to the AI. Nothing here writes to
    the database -- it tokenizes, calls Gemini, rehydrates, resolves herb
    names, and hands back a proposal plus a preview_id the doctor can
    confirm afterward (or let expire by doing nothing).
    """
    patient_name: str = Field(min_length=1, max_length=200)
    patient_phone: Optional[str] = Field(default=None, max_length=32)
    patient_id: Optional[str] = Field(default=None, max_length=64)
    notes: str = Field(min_length=1, max_length=10000)
    # Patient Registry Phase 1 (API Contract Addendum v1.5): optional link to
    # a registered patient of the CALLER'S OWN clinic. When present, the
    # registry is authoritative for the stored identity fields (see
    # backend/main.py); a patient_pk from another clinic is a 404.
    patient_pk: Optional[int] = Field(default=None, gt=0)
    # Encounter Phase 1B (Addendum v1.6): optional link to an OPEN encounter
    # of the caller's own clinic that belongs to the same patient_pk (so it
    # requires patient_pk). Validated server-side before any AI call.
    encounter_pk: Optional[int] = Field(default=None, gt=0)
    # Multi-tenancy placeholder: one clinic today, but every write path
    # threads this through now so a second clinic doesn't require touching
    # this layer again later.
    clinic_id: str = Field(default="default_clinic", max_length=64)


class PreviewResponse(BaseModel):
    preview_id: str
    patient_name: str
    dosha_imbalance: str
    prescribed_herbs: List[PrescribedHerb]


class ManualPreviewRequest(BaseModel):
    """Request body for POST /prescribe/manual-preview.

    This is the AI-outage fallback: used ONLY when /prescribe/preview's
    call to Gemini can't be completed (quota exhausted, service down,
    timeout -- see backend/main.py's X-Error-Code: AI_SERVICE_UNAVAILABLE
    response header on that failure). It skips the AI call entirely -- the
    doctor supplies dosha_imbalance and prescribed_herbs directly instead
    of free-text notes. Every downstream safety rail /prescribe/confirm
    already enforces (canonical herb-name resolution, stock validation,
    single-use preview/confirm split) is IDENTICAL for a manual preview as
    for an AI one -- this endpoint's only difference is where the proposal
    came from. The clinic can keep prescribing with the AI down; the AI is
    a convenience layer, never a hard dependency.
    """
    patient_name: str = Field(min_length=1, max_length=200)
    patient_phone: Optional[str] = Field(default=None, max_length=32)
    patient_id: Optional[str] = Field(default=None, max_length=64)
    dosha_imbalance: str = Field(min_length=1, max_length=200)
    prescribed_herbs: List[PrescribedHerb] = Field(min_length=1)
    # Patient Registry Phase 1 (API Contract Addendum v1.5): optional link to
    # a registered patient of the CALLER'S OWN clinic. When present, the
    # registry is authoritative for the stored identity fields (see
    # backend/main.py); a patient_pk from another clinic is a 404.
    patient_pk: Optional[int] = Field(default=None, gt=0)
    # Encounter Phase 1B (Addendum v1.6): optional link to an OPEN encounter
    # of the caller's own clinic that belongs to the same patient_pk (so it
    # requires patient_pk). Validated server-side before any AI call.
    encounter_pk: Optional[int] = Field(default=None, gt=0)
    clinic_id: str = Field(default="default_clinic", max_length=64)


class ConfirmRequest(BaseModel):
    """Request body for POST /prescribe/confirm.

    Makes NO AI call. `preview_id` references a prior /prescribe/preview
    call; `dosha_imbalance` / `prescribed_herbs` are optional doctor
    corrections -- if omitted, the AI's own proposal from that preview is
    used unchanged. Either way, /confirm logs what the AI proposed versus
    what was actually confirmed (see backend/corrections_service.py).
    """
    preview_id: str
    # A6.3 hardening (Sep 2026): both fields below previously had NO bound
    # at all -- a real asymmetry against ManualPreviewRequest's equivalent
    # fields just above (dosha_imbalance: max_length=200; prescribed_herbs:
    # Field(min_length=1)), flagged by the A6.3 review's Gate 7 finding.
    # max_length=200 on dosha_imbalance mirrors ManualPreviewRequest's own
    # bound on the identical concept exactly -- not a new number invented
    # for this field. max_length=50 on prescribed_herbs is new (neither
    # PreviewRequest nor ManualPreviewRequest bounds this list either, so
    # there was no existing number to mirror): this clinic's entire
    # formulary is 20 herbs (see agents/copilot.py's SEED_HERBS), and
    # deduct_and_prescribe() merges duplicate entries before dispensing,
    # so 50 comfortably covers every stocked herb plus room for a doctor
    # listing a few duplicates/corrections before that merge, while ruling
    # out an unbounded list. Both fields stay Optional with no change to
    # their None-means-"use the AI's own proposal unchanged" semantics.
    # min_length=1 (Sep 2026) mirrors ManualPreviewRequest.prescribed_herbs:
    # an explicit empty list used to be accepted and committed a
    # prescription with no herbs, no items and no deduction. It is now a
    # 422 before the handler runs, so nothing is written. Omitting the
    # field (or null) still means "use the stored proposal".
    dosha_imbalance: Optional[str] = Field(default=None, max_length=200)
    prescribed_herbs: Optional[List[PrescribedHerb]] = Field(default=None, min_length=1, max_length=50)


# Prescription payment status — only three valid values.
PaymentStatus = Annotated[
    Optional[str],
    Field(default=None, description="paid, partial, or unpaid"),
]

# Payment method — only three valid values.
PaymentMethod = Annotated[
    Optional[str],
    Field(default=None, description="cash, upi, or card"),
]


class PrescriptionResponse(BaseModel):
    patient_name: str
    dosha_imbalance: str
    prescribed_herbs: List[PrescribedHerb]
    prescription_id: int
    created_at: datetime
    # Migration 018: dispensing + payment fields.
    payment_status: PaymentStatus = None
    payment_amount_cents: Optional[int] = Field(default=None, ge=0, le=1000000)
    payment_method: PaymentMethod = None
    payment_reference: Optional[str] = Field(default=None, max_length=100)
    dispensed_at: Optional[str] = None
    dispensed_by_user_id: Optional[int] = Field(default=None, gt=0)
    invoice_number: Optional[str] = Field(default=None, max_length=64)


# --- Dispense / Payment / Invoice (clinic workflow, migration 018) ----------

class DispenseRequest(BaseModel):
    """Request body for POST /prescriptions/{prescription_pk}/dispense.

    Marks a confirmed prescription as physically dispensed. The dispenser
    and timestamp come from the authenticated session -- nothing in this
    body is patient identity or clinic-scoping data (both come from the
    session)."""
    model_config = ConfigDict(extra="forbid")

    notes: Optional[str] = Field(default=None, max_length=500, description="Dispensing notes")


class DispenseResponse(BaseModel):
    """Response for POST /prescriptions/{prescription_pk}/dispense."""
    prescription_id: int
    dispensed_at: str
    dispensed_by_user_id: int
    status: str  # "dispensed"


class PaymentRequest(BaseModel):
    """Request body for POST /prescriptions/{prescription_pk}/payment.

    Records payment against a confirmed prescription. `payment_amount_cents`
    must not exceed the outstanding amount (validated server-side)."""
    model_config = ConfigDict(extra="forbid")

    payment_status: PaymentStatus
    payment_amount_cents: Optional[int] = Field(default=None, ge=0, le=1000000)
    payment_method: PaymentMethod
    payment_reference: Optional[str] = Field(default=None, max_length=100)


class PaymentResponse(BaseModel):
    """Response for POST /prescriptions/{prescription_pk}/payment."""
    prescription_id: int
    payment_status: str
    payment_amount_cents: Optional[int] = None
    payment_method: Optional[str] = None
    payment_reference: Optional[str] = None
    invoice_number: Optional[str] = None


class InvoiceResponse(BaseModel):
    """Response for GET /prescriptions/{prescription_pk}/invoice.

    A receipt-like summary for the doctor/dispenser. Deliberately contains
    no patient identity beyond the registry key (clinic_patient_id) -- see
    PII minimization rules."""
    prescription_id: int
    clinic_id: str
    patient_clinic_patient_id: str
    patient_display_name: str
    dosha_imbalance: Optional[str]
    prescribed_herbs: List[PrescribedHerb]
    total_amount_cents: int
    payment_status: str
    payment_method: Optional[str] = None
    payment_reference: Optional[str] = None
    invoice_number: Optional[str] = None
    dispensed_at: Optional[str] = None
    created_at: str


class AuditRecord(BaseModel):
    """One row of the AI-proposal-vs-doctor-confirmation audit trail.

    Deliberately contains no patient identity (name/phone/ID) -- see the
    corrections table's own docstring in backend/corrections_service.py.
    This exists to audit the pipeline itself, not to double as a patient
    record lookup.
    """
    prescription_id: int
    clinic_id: str
    created_at: datetime
    ai_dosha_imbalance: str
    ai_prescribed_herbs: List[PrescribedHerb]
    confirmed_dosha_imbalance: str
    confirmed_prescribed_herbs: List[PrescribedHerb]
    was_changed: bool


class LoginRequest(BaseModel):
    """Request body for POST /auth/login (Phase A, A2 -- new, additive
    endpoint; see API Contract Addendum v1.2).

    Deliberately just username/password -- no clinic_id here. Which
    clinic a user belongs to is a property of their persisted account
    (users.clinic_id, set at account creation), never something a login
    call gets to assert for itself.
    """
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class LoginResponse(BaseModel):
    """Response body for POST /auth/login (Phase A, A2 -- new, additive
    endpoint; see API Contract Addendum v1.2; `role` added in A3, see
    Addendum v1.3).

    `token` is an opaque, server-generated bearer token -- not a JWT, and
    not decodable by the frontend for identity/role/clinic information.
    Everything a client needs to display or act on (username, clinic_id,
    role) is returned here, in the response BODY, once, at login time --
    that is not the same thing as encoding it into the token itself,
    which the locked contract and A2's spec both explicitly forbid.
    `role` is informational for the client only (e.g. so a UI can hide a
    button a DOCTOR has no access to); it grants nothing by itself -- every
    protected endpoint still re-derives the caller's role live from the
    server-side session on every single request (see backend/rbac.py),
    never from anything returned here or sent back by the client.
    """
    token: str
    expires_at: datetime
    username: str
    clinic_id: str
    role: str


class AuditEventRecord(BaseModel):
    """One row of the Phase A5 tamper-evident audit trail (`audit_events` --
    distinct from the pre-existing `corrections`-backed AuditRecord above,
    which is unchanged). event_data is the clinically meaningful decision
    snapshot built by backend/audit_store.py's
    build_prescription_confirmed_event_data() -- never patient identity;
    see that module's PII MINIMIZATION docstring section.
    """
    event_id: int
    clinic_id: str
    actor_user_id: int
    event_type: str
    # Set for PRESCRIPTION_CONFIRMED; null for ENCOUNTER_OPENED/CLOSED, whose
    # opaque encounter_pk is in event_data instead (migration 011).
    prescription_id: Optional[int] = None
    preview_id: Optional[str] = None
    event_data: Dict[str, Any]
    created_at: str
    previous_event_hash: Optional[str]
    event_hash: str


class AuditEventsExportResponse(BaseModel):
    """Response for GET /audit/events (Phase A5). `chain_valid` reflects a
    LIVE call to backend.audit_store.verify_audit_integrity() scoped to
    this caller's own clinic, run fresh on every request -- never a cached
    or precomputed flag -- so a direct database edit is reflected the very
    next time anyone reads this endpoint, not just whenever some separate
    job happens to check.
    """
    clinic_id: str
    generated_at: datetime
    event_count: int
    chain_valid: bool
    integrity_violations: List[str]
    events: List[AuditEventRecord]


class AuditExportResponse(BaseModel):
    """Response for GET /audit/export: a read-only, provably PII-free
    record of every confirmed prescription for a clinic -- what the AI
    proposed, what the doctor actually confirmed, and whether they
    differed. `integrity_notes` spells out, in plain language, exactly
    what this report does and doesn't prove -- meant to be read by someone
    who isn't this codebase's author (a clinic owner, eventually an
    auditor), not just by another engineer.
    """
    clinic_id: str
    generated_at: datetime
    record_count: int
    changed_count: int
    records: List[AuditRecord]
    integrity_notes: List[str]


# --- Patient Registry Phase 1 (API Contract Addendum v1.5) ------------------

# clinic_patient_id: the clinic-issued OP/registration number. Trimmed, then
# 1-50 characters, no control characters. Immutable once registered.
ClinicPatientId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50, pattern=r"^[^\x00-\x1f\x7f]+$")
]


# --- Patient demographics validation (migration 018) -------------------------

# prakriti: Ayurvedic constitution — only the four accepted values.
Prakriti = Annotated[
    Optional[str],
    Field(
        default=None,
        description="Patient's prakriti: Vata, Pitta, Kapha, or Tridosha",
    ),
]

# Gender — free text but bounded to reasonable values.
Gender = Annotated[
    Optional[str],
    Field(default=None, max_length=32, description="Patient's gender"),
]

# Pincode: Indian-style 6-digit postal code, stored as text to preserve
# leading zeros.
Pincode = Annotated[
    Optional[str],
    Field(
        default=None,
        max_length=10,
        description="Synthetic Indian-style pincode (6 digits, stored as text)",
    ),
]

# Allergies: free text, bounded.
Allergies = Annotated[
    Optional[str],
    Field(default=None, max_length=500, description="Recorded patient allergies"),
]


class PatientCreateRequest(BaseModel):
    """Request body for POST /patients. There is deliberately no clinic_id
    field: the patient is always registered in the caller's authenticated
    clinic, and any extra field (clinic_id included) is rejected with 422
    rather than silently ignored."""
    model_config = ConfigDict(extra="forbid")

    clinic_patient_id: ClinicPatientId
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    phone: Optional[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]] = None
    # Migration 018: clinic-enriched demographics (all optional so existing
    # callers are not broken).
    date_of_birth: Optional[str] = Field(default=None, max_length=16, description="YYYY-MM-DD")
    gender: Gender = None
    address_line: Optional[str] = Field(default=None, max_length=200)
    city: Optional[str] = Field(default=None, max_length=100)
    state: Optional[str] = Field(default=None, max_length=100)
    pincode: Pincode = None
    prakriti: Prakriti = None
    allergies: Allergies = None
    registered_at: Optional[str] = Field(default=None, max_length=32, description="YYYY-MM-DD HH:MM:SS")


class PatientResponse(BaseModel):
    patient_pk: int
    clinic_patient_id: str
    display_name: str
    phone: Optional[str] = None
    created_at: str
    # Migration 018: clinic-enriched demographics.
    date_of_birth: Optional[str] = None
    gender: Optional[str] = None
    address_line: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    prakriti: Optional[str] = None
    allergies: Optional[str] = None
    registered_at: Optional[str] = None


# --- v0.1: read-only inventory -------------------------------------------------

class InventoryItem(BaseModel):
    herb_name: str
    stock_grams: int
    # Migration 018: batch / expiry realism.
    batch_code: Optional[str] = Field(default=None, max_length=64)
    expiry_date: Optional[str] = Field(default=None, max_length=16, description="YYYY-MM-DD")
    supplier: Optional[str] = Field(default=None, max_length=200)
    reorder_level_grams: Optional[int] = Field(default=None, ge=0)
    potency_score: Optional[int] = Field(default=None, ge=0, le=100)


class InventoryResponse(BaseModel):
    """GET /inventory: the caller's own clinic's current stock, sorted by
    herb name. Deliberately no clinic_id."""
    items: List[InventoryItem]


# --- Patient Registry Phase 2A: search + history (read-only) -----------------

class PatientSummary(BaseModel):
    """Only what a doctor needs to pick the right patient. Deliberately no
    phone and no clinic_id."""
    patient_pk: int
    clinic_patient_id: str
    display_name: str


class PatientSearchResponse(BaseModel):
    results: List[PatientSummary]
    # True when more patients matched than the bounded result count; the
    # caller should narrow the query rather than page through the registry.
    truncated: bool


class HistoryHerb(BaseModel):
    # Plain name/grams, not PrescribedHerb: the dispensed formulation merges
    # duplicate entries, so a total can legitimately exceed a single entry's
    # request-side bound.
    name: str
    grams: int


class PatientPrescriptionRecord(BaseModel):
    prescription_id: int
    created_at: str
    status: str
    # None only if a stored record is not in the format deduct_and_prescribe
    # writes -- reported as unknown rather than guessed.
    dosha_imbalance: Optional[str] = None
    prescribed_herbs: Optional[List[HistoryHerb]] = None
    # Migration 018: dispensing + payment fields.
    payment_status: Optional[str] = None
    payment_amount_cents: Optional[int] = Field(default=None, ge=0, le=1000000)
    payment_method: Optional[str] = None
    payment_reference: Optional[str] = None
    dispensed_at: Optional[str] = None
    dispensed_by_user_id: Optional[int] = None
    invoice_number: Optional[str] = None


class PatientHistoryResponse(BaseModel):
    patient: PatientSummary
    prescriptions: List[PatientPrescriptionRecord]
    limit: int
    offset: int
    has_more: bool


# --- Encounter foundation Phase 1A (migration 010) -----------------------------

# Encounter visit type — only two valid values.
VisitType = Annotated[
    Optional[str],
    Field(default=None, description="FIRST_VISIT or FOLLOW_UP"),
]


class EncounterCreateRequest(BaseModel):
    """Optional body for POST /patients/{patient_pk}/encounters. The clinic,
    the patient's identity and the creator all come from the authenticated
    session and the path. Any field sent (clinic_id included) is rejected
    with 422 rather than silently ignored.

    Migration 018: optional encounter-enrichment fields for a realistic
    clinic consultation (visit_type, chief_complaint, vitals, follow_up).
    All optional -- a plain encounter open still works with no body.
    """
    model_config = ConfigDict(extra="forbid")

    visit_type: VisitType = None
    chief_complaint: Optional[str] = Field(default=None, max_length=500)
    vitals_bp: Optional[str] = Field(default=None, max_length=16, description="e.g. 120/80")
    vitals_weight_kg: Optional[float] = Field(default=None, gt=0, le=300)
    vitals_temperature_c: Optional[float] = Field(default=None, gt=30, le=45)
    follow_up_date: Optional[str] = Field(default=None, max_length=16, description="YYYY-MM-DD")


class EncounterResponse(BaseModel):
    """No clinic_id and no patient identity beyond the registry key."""
    encounter_pk: int
    patient_pk: int
    created_by_user_id: int
    status: str
    created_at: str
    closed_at: Optional[str] = None
    # Random, non-sequential reference (migration 013). It is what encounter
    # audit events carry, so an ADMIN can match an audit event to an
    # encounter without the audit trail exposing the sequential encounter_pk.
    encounter_ref: str
    # Migration 018: encounter enrichment.
    visit_type: Optional[str] = None
    chief_complaint: Optional[str] = None
    vitals_bp: Optional[str] = None
    vitals_weight_kg: Optional[float] = None
    vitals_temperature_c: Optional[float] = None
    follow_up_date: Optional[str] = None


class EncounterListResponse(BaseModel):
    encounters: List[EncounterResponse]
    limit: int
    offset: int
    has_more: bool


# --- AI-2 consultation memory (POST /patients/{patient_pk}/ask) -----------------
# Read-only. The request is a question, optionally with the reading the clinician picked from an earlier clarification; the
# response is AI-2's own result (ai2.result.Ai2Answer) with resolved evidence. Nothing here selects the clinic or the patient's
# identity: the clinic comes from the session and the patient from the path.

_ASK_QUERY_KEYS = frozenset({"op", "target", "herb", "dosha", "last_n", "a", "b"})


class AskChoice(BaseModel):
    """A reading offered by a previous clarification, sent back verbatim. The server never trusts it as a plan: it must equal one of the
    readings AI-2 offers for the same question."""
    model_config = ConfigDict(extra="forbid")
    frame: str = Field(min_length=1, max_length=40)
    query: Dict[str, Any]

    @field_validator("query")
    @classmethod
    def _query_shape(cls, value):
        if set(value) - _ASK_QUERY_KEYS:
            raise ValueError("query has fields that are not part of a reading")
        for k, v in value.items():
            if not (v is None or isinstance(v, (str, int)) and not isinstance(v, bool)) or (isinstance(v, str) and len(v) > 200):
                raise ValueError(f"query field {k} has an invalid value")
        return value


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")      # a client-supplied clinic_id / patient identity / plan is rejected, not ignored
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    choice: Optional[AskChoice] = None


class AskOption(BaseModel):
    frame: str
    text: str
    query: Dict[str, Any]


class AskEvidence(BaseModel):
    """A record the answer cites. No sequential encounter key is exposed: encounters are identified by their random reference."""
    kind: str                                   # prescription | encounter | unknown
    label: str
    prescription_id: Optional[int] = None
    created_at: Optional[str] = None
    status: Optional[str] = None
    dosha: Optional[str] = None
    herbs: Optional[List[HistoryHerb]] = None
    encounter_ref: Optional[str] = None
    closed_at: Optional[str] = None


class AskResponse(BaseModel):
    patient_pk: int
    outcome: str                                # answer | clarify | abstain
    category: str                               # answer | incomplete | clarify | missing | unsupported | unsafe | access_denied
    stage: str
    reason: Optional[str] = None
    text: Optional[str] = None
    value: Any = None
    qualifier: Optional[str] = None             # exact | at_least | partial | unknown | not_recorded
    verified: bool = False
    options: List[AskOption] = []
    evidence: List[AskEvidence] = []
    plan_frame: Optional[str] = None
    plan_query: Optional[Dict[str, Any]] = None
    clarified: bool = False                     # true when this answers a reading chosen from a clarification

