/**
 * Typed request/response shapes for the AyurCore FastAPI backend.
 *
 * These mirror backend/schemas.py field-for-field (see
 * AyurCore_API_Contract_v1.md, v1.3 and v1.4 addenda). This file defines
 * NO new fields and invents NO data shape the backend doesn't already
 * return -- it exists purely so the rest of the frontend gets compile-time
 * safety against the backend's actual, locked contract instead of `any`.
 */

export interface PrescribedHerb {
  name: string
  grams: number
}

export interface LoginRequest {
  username: string
  password: string
}

/** Response body for POST /auth/login. `role` is one of DOCTOR/ADMIN/AUDITOR. */
export interface LoginResponse {
  token: string
  expires_at: string
  username: string
  clinic_id: string
  role: string
}

export interface PreviewRequest {
  patient_name: string
  patient_phone?: string
  patient_id?: string
  notes: string
  clinic_id?: string
}

export interface ManualPreviewRequest {
  patient_name: string
  patient_phone?: string
  patient_id?: string
  dosha_imbalance: string
  prescribed_herbs: PrescribedHerb[]
  clinic_id?: string
}

/** Response body shared by /prescribe/preview and /prescribe/manual-preview. */
export interface PreviewResponse {
  preview_id: string
  patient_name: string
  dosha_imbalance: string
  prescribed_herbs: PrescribedHerb[]
  proposal_source?: 'local_extractor' | 'gemini' | 'manual'
}

export interface ConfirmRequest {
  preview_id: string
  dosha_imbalance?: string
  prescribed_herbs?: PrescribedHerb[]
}

export interface PrescriptionResponse {
  patient_name: string
  dosha_imbalance: string
  prescribed_herbs: PrescribedHerb[]
  prescription_id: number
  created_at: string
}

/** One row of GET /audit/export -- the AI-proposal-vs-confirmation trail.
 *  Deliberately contains no patient identity (see backend/schemas.py). */
export interface AuditRecord {
  prescription_id: number
  clinic_id: string
  created_at: string
  ai_dosha_imbalance: string
  ai_prescribed_herbs: PrescribedHerb[]
  confirmed_dosha_imbalance: string
  confirmed_prescribed_herbs: PrescribedHerb[]
  was_changed: boolean
}

export interface AuditExportResponse {
  clinic_id: string
  generated_at: string
  record_count: number
  changed_count: number
  records: AuditRecord[]
  integrity_notes: string[]
}

/** One row of GET /audit/events -- the tamper-evident, hash-chained trail. */
export interface AuditEventRecord {
  event_id: number
  clinic_id: string
  actor_user_id: number
  event_type: string
  prescription_id: number
  preview_id: string
  event_data: Record<string, unknown>
  created_at: string
  previous_event_hash: string | null
  event_hash: string
}

export interface AuditEventsExportResponse {
  clinic_id: string
  generated_at: string
  event_count: number
  chain_valid: boolean
  integrity_violations: string[]
  events: AuditEventRecord[]
}

/** One herb of GET /inventory (read-only; DOCTOR / ADMIN). No clinic_id, batch or potency data. */
export interface InventoryItem {
  herb_name: string
  stock_grams: number
}

export interface InventoryResponse {
  items: InventoryItem[]
}

// ---------------------------------------------------------------------
// AI-2 patient questions (POST /patients/{patient_pk}/ask; DOCTOR only,
// read-only). Mirrors backend/schemas.py AskRequest / AskResponse. The
// request carries a question (and, to answer a clarification, the reading
// the server itself offered) -- never a clinic, a patient identity or a plan.
// ---------------------------------------------------------------------

/** A reading offered by a clarification, sent back verbatim. */
export interface AskChoice {
  frame: string
  query: Record<string, string | number | null>
}

export interface AskRequest {
  question: string
  choice?: AskChoice
}

export interface AskOption {
  frame: string
  text: string
  query: Record<string, string | number | null>
}

/** A record an answer cites. Encounters are identified by their random reference only. */
export interface AskEvidence {
  kind: 'prescription' | 'encounter' | 'unknown' | string
  label: string
  prescription_id?: number | null
  created_at?: string | null
  status?: string | null
  dosha?: string | null
  herbs?: PrescribedHerb[] | null
  encounter_ref?: string | null
  closed_at?: string | null
}

export type AskCategory = 'answer' | 'incomplete' | 'clarify' | 'missing' | 'unsupported' | 'unsafe' | 'access_denied'

export interface AskResponse {
  patient_pk: number
  outcome: 'answer' | 'clarify' | 'abstain'
  category: AskCategory
  stage: string
  reason: string | null
  text: string | null
  value: unknown
  qualifier: 'exact' | 'at_least' | 'partial' | 'unknown' | 'not_recorded' | null
  verified: boolean
  options: AskOption[]
  evidence: AskEvidence[]
  plan_frame: string | null
  plan_query: Record<string, string | number | null> | null
  clarified: boolean
}

/** Roles as returned by the backend (migration 004's CHECK constraint). */
export type Role = 'DOCTOR' | 'ADMIN' | 'AUDITOR'

// ---------------------------------------------------------------------
// Patient registry (API Contract Addendum v1.5 + Phase 2A read endpoints).
// DOCTOR/ADMIN only. None of these shapes carries clinic_id, and search /
// history never return a phone number.
// ---------------------------------------------------------------------

/** One search result / the patient block of a history response. */
export interface PatientSummary {
  patient_pk: number
  clinic_patient_id: string
  display_name: string
}

/** Response body for GET /patients?q=. `truncated` means more patients
 *  matched than the backend's bounded result count. */
export interface PatientSearchResponse {
  results: PatientSummary[]
  truncated: boolean
}

/** Request body for POST /patients. Deliberately no clinic_id: the backend
 *  registers the patient in the caller's authenticated clinic and rejects
 *  any extra field. */
export interface PatientCreateRequest {
  clinic_patient_id: string
  display_name: string
  phone?: string
}

/** Response body for POST /patients (201). */
export interface PatientResponse {
  patient_pk: number
  clinic_patient_id: string
  display_name: string
  phone: string | null
  created_at: string
}

/** One confirmed prescription in a patient's history. dosha/herbs are null
 *  only if the backend could not read a stored record's format. */
export interface PatientPrescriptionRecord {
  prescription_id: number
  created_at: string
  status: string
  dosha_imbalance: string | null
  prescribed_herbs: PrescribedHerb[] | null
}

/** Response body for GET /patients/{patient_pk}/prescriptions. */
export interface PatientHistoryResponse {
  patient: PatientSummary
  prescriptions: PatientPrescriptionRecord[]
  limit: number
  offset: number
  has_more: boolean
}
