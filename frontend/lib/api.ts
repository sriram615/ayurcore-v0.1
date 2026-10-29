/**
 * ONE centralized, typed API client for the AyurCore FastAPI backend.
 *
 * Every backend call in this frontend goes through a function exported
 * here -- no component calls `fetch()` directly. This is deliberate: it
 * is the single place that knows the base URL, attaches the bearer token,
 * and turns a failed response into the same ApiError shape every caller
 * already knows how to translate via lib/errors.ts.
 *
 * The base URL comes from NEXT_PUBLIC_API_BASE_URL (see .env.local) --
 * never hardcoded elsewhere in the app. FastAPI is authoritative: this
 * file adds no client-side business logic beyond shaping requests and
 * responses exactly as the locked API contract already defines them.
 */
import type { ApiError, ApiOperation } from './errors'
import type {
  AuditEventsExportResponse,
  AskRequest,
  AskResponse,
  AuditExportResponse,
  BriefResponse,
  InventoryResponse,
  ConfirmRequest,
  LoginRequest,
  LoginResponse,
  ManualPreviewRequest,
  PatientCreateRequest,
  PatientHistoryResponse,
  PatientResponse,
  PatientSearchResponse,
  PreviewRequest,
  PreviewResponse,
  PrescriptionResponse,
} from './api-types'

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://127.0.0.1:8000'

interface RequestOptions {
  token?: string | null
  idempotencyKey?: string
  signal?: AbortSignal
}

/** Thrown by every function below on any failure -- HTTP error, network
 *  failure, or timeout. Always an ApiError; never a raw fetch/DOMException
 *  or a bare string, so every call site can catch-and-translate uniformly. */
export class ApiCallError extends Error implements ApiError {
  status: number | null
  detail: string
  errorCode?: string
  operation?: ApiOperation

  constructor(error: ApiError, operation?: ApiOperation) {
    super(error.detail)
    this.status = error.status
    this.detail = error.detail
    this.errorCode = error.errorCode
    this.operation = operation
  }
}

const DEFAULT_TIMEOUT_MS = 30_000

async function request<TResponse>(
  path: string,
  operation: ApiOperation,
  init: {
    method: 'GET' | 'POST'
    body?: unknown
  },
  options: RequestOptions = {}
): Promise<TResponse> {
  const headers: Record<string, string> = {}
  if (init.body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }
  if (options.token) {
    headers['Authorization'] = `Bearer ${options.token}`
  }
  if (options.idempotencyKey) {
    headers['Idempotency-Key'] = options.idempotencyKey
  }

  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS)
  // Let an externally-supplied signal abort us too (e.g. a component
  // unmounting mid-request).
  options.signal?.addEventListener('abort', () => controller.abort())

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: init.method,
      headers,
      body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
      signal: controller.signal,
    })
  } catch (err) {
    clearTimeout(timeout)
    const wasAbort = err instanceof DOMException && err.name === 'AbortError'
    throw new ApiCallError(
      {
        status: null,
        detail: wasAbort
          ? `The request to ${path} timed out.`
          : `Could not reach the backend at ${API_BASE}.`,
      },
      operation
    )
  }
  clearTimeout(timeout)

  const errorCode = response.headers.get('X-Error-Code') ?? undefined

  if (!response.ok) {
    let detail = ''
    try {
      const body = await response.json()
      detail = typeof body?.detail === 'string' ? body.detail : JSON.stringify(body?.detail ?? body)
    } catch {
      detail = response.statusText
    }
    throw new ApiCallError({ status: response.status, detail, errorCode }, operation)
  }

  // 204/empty-body responses (none currently in this contract, but
  // defensive against a future one) -- avoid throwing on empty JSON parse.
  const text = await response.text()
  if (!text) return undefined as unknown as TResponse
  return JSON.parse(text) as TResponse
}

// ---------------------------------------------------------------------
// Auth (A2 -- see API Contract Addendum v1.2)
// ---------------------------------------------------------------------

export function login(body: LoginRequest): Promise<LoginResponse> {
  return request<LoginResponse>('/auth/login', 'login', { method: 'POST', body })
}

export function logout(token: string): Promise<{ status: string }> {
  return request<{ status: string }>('/auth/logout', 'logout', { method: 'POST' }, { token })
}

// ---------------------------------------------------------------------
// Consultation -- AI preview, manual preview, confirm (DOCTOR only)
// ---------------------------------------------------------------------

export function prescribePreview(body: PreviewRequest, token: string): Promise<PreviewResponse> {
  return request<PreviewResponse>('/prescribe/preview', 'preview', { method: 'POST', body }, { token })
}

/** Local deterministic extraction via AI-1 (T1 tagger + T2 assembly + T3 verifier). No LLM, no API key, offline. */
export function prescribePreviewLocal(body: PreviewRequest, token: string): Promise<PreviewResponse> {
  return request<PreviewResponse>('/prescribe/preview/local', 'preview_local', { method: 'POST', body }, { token })
}

export function prescribeManualPreview(
  body: ManualPreviewRequest,
  token: string
): Promise<PreviewResponse> {
  return request<PreviewResponse>(
    '/prescribe/manual-preview',
    'manual_preview',
    { method: 'POST', body },
    { token }
  )
}

export function prescribeConfirm(
  body: ConfirmRequest,
  token: string,
  idempotencyKey: string
): Promise<PrescriptionResponse> {
  return request<PrescriptionResponse>(
    '/prescribe/confirm',
    'confirm',
    { method: 'POST', body },
    { token, idempotencyKey }
  )
}

// ---------------------------------------------------------------------
// Patient registry (DOCTOR / ADMIN only -- enforced by backend/rbac.py).
// The clinic is always the session's, resolved server-side; nothing here
// sends a clinic_id.
// ---------------------------------------------------------------------

export function searchPatients(q: string, token: string, signal?: AbortSignal): Promise<PatientSearchResponse> {
  return request<PatientSearchResponse>(
    `/patients?q=${encodeURIComponent(q)}`,
    'patient_search',
    { method: 'GET' },
    { token, signal }
  )
}

export function patientHistory(
  patientPk: number,
  token: string,
  page: { limit?: number; offset?: number } = {},
  signal?: AbortSignal
): Promise<PatientHistoryResponse> {
  const params = new URLSearchParams()
  if (page.limit !== undefined) params.set('limit', String(page.limit))
  if (page.offset !== undefined) params.set('offset', String(page.offset))
  const query = params.toString()
  return request<PatientHistoryResponse>(
    `/patients/${encodeURIComponent(String(patientPk))}/prescriptions${query ? `?${query}` : ''}`,
    'patient_history',
    { method: 'GET' },
    { token, signal }
  )
}

/** Ask AI-2 a question about one patient's recorded history (DOCTOR only; read-only). The backend decides the clinic from the session and
 *  everything about the answer; nothing here interprets the question or the result. */
export function askPatient(patientPk: number, body: AskRequest, token: string, signal?: AbortSignal): Promise<AskResponse> {
  return request<AskResponse>(
    `/patients/${encodeURIComponent(String(patientPk))}/ask`,
    'patient_ask',
    { method: 'POST', body },
    { token, signal }
  )
}

/** Get AI-3 consultation brief (DOCTOR/ADMIN only; read-only deterministic facts with salience ranking). */
export function getPatientBrief(
  patientPk: number,
  token: string,
  arm: 'learned' | 'template' = 'learned',
  asOf?: string,
  signal?: AbortSignal
): Promise<BriefResponse> {
  const params = new URLSearchParams()
  if (arm) params.set('arm', arm)
  if (asOf) params.set('as_of', asOf)
  const query = params.toString()
  return request<BriefResponse>(
    `/patients/${encodeURIComponent(String(patientPk))}/brief${query ? `?${query}` : ''}`,
    'patient_brief',
    { method: 'GET' },
    { token, signal }
  )
}

export function registerPatient(body: PatientCreateRequest, token: string): Promise<PatientResponse> {
  return request<PatientResponse>('/patients', 'patient_register', { method: 'POST', body }, { token })
}

// ---------------------------------------------------------------------
// Inventory (DOCTOR / ADMIN only; read-only -- the clinic is the session's)
// ---------------------------------------------------------------------

export function getInventory(token: string, signal?: AbortSignal): Promise<InventoryResponse> {
  return request<InventoryResponse>('/inventory', 'inventory', { method: 'GET' }, { token, signal })
}

// ---------------------------------------------------------------------
// Audit (ADMIN / AUDITOR only)
// ---------------------------------------------------------------------

export function auditExport(token: string): Promise<AuditExportResponse> {
  return request<AuditExportResponse>('/audit/export', 'audit_export', { method: 'GET' }, { token })
}

export function auditEvents(token: string): Promise<AuditEventsExportResponse> {
  return request<AuditEventsExportResponse>('/audit/events', 'audit_events', { method: 'GET' }, { token })
}

/** Generates a fresh per-attempt Idempotency-Key, exactly the way the
 *  existing Streamlit UI does (ui/app.py: `str(uuid.uuid4())`) -- see API
 *  Contract Addendum v1.4. One key per confirmation *attempt*; the same
 *  key is reused across retries of that same attempt so a retry after a
 *  timeout safely replays rather than double-confirms. */
export function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  // Fallback for an environment without crypto.randomUUID (older
  // browsers) -- still a per-attempt unique opaque string, per contract.
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}
