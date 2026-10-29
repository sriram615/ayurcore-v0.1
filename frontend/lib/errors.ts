/**
 * Friendly clinical error translation.
 *
 * Ported, message-for-message, from the categories and copy already
 * reviewed and shipped in the existing AyurCore Streamlit UI
 * (ui/app.py's _friendly_error / _translate_call_error / MSG_* constants).
 * Reusing that exact wording -- rather than inventing new copy here --
 * keeps the two frontends speaking with one clinical voice, and reuses
 * language that has already been through this project's own UX review.
 *
 * Pure functions only: no network calls, no React, no backend detail
 * string is ever returned to a caller. Every message this module can
 * produce is one of the MSG_* constants below, so a raw FastAPI `detail`
 * (a stack trace, a DB error, a Python exception) can never reach a
 * clinician through this layer, however it is worded.
 */

export const ErrorCategory = {
  SESSION: 'SESSION',
  SIGN_IN_FAILED: 'SIGN_IN_FAILED',
  WRONG_ROLE: 'WRONG_ROLE',
  UNKNOWN_HERB: 'UNKNOWN_HERB',
  VALIDATION: 'VALIDATION',
  AI_UNAVAILABLE: 'AI_UNAVAILABLE',
  AI_INVALID_RESPONSE: 'AI_INVALID_RESPONSE',
  PREVIEW_EXPIRED: 'PREVIEW_EXPIRED',
  PATIENT_DUPLICATE: 'PATIENT_DUPLICATE',
  PATIENT_NOT_FOUND: 'PATIENT_NOT_FOUND',
  INSUFFICIENT_STOCK: 'INSUFFICIENT_STOCK',
  ALREADY_CONFIRMED: 'ALREADY_CONFIRMED',
  IDEMPOTENCY_CONFLICT: 'IDEMPOTENCY_CONFLICT',
  CONNECTION: 'CONNECTION',
  TIMEOUT_RETRYABLE: 'TIMEOUT_RETRYABLE',
  TIMEOUT_UNCERTAIN: 'TIMEOUT_UNCERTAIN',
  FORBIDDEN_SCOPE: 'FORBIDDEN_SCOPE',
  SERVER: 'SERVER',
  UNKNOWN: 'UNKNOWN',
} as const

export type ErrorCategoryType = (typeof ErrorCategory)[keyof typeof ErrorCategory]

const MSG_SESSION = "Your session has ended. Sign in again to carry on — your notes are kept."
const MSG_SIGN_IN_FAILED = "That username and password don't match."
const MSG_WRONG_ROLE = "This account can't perform that action. Sign in with an account that has access."
const MSG_UNKNOWN_HERB = 'Not a herb this clinic stocks. Check the spelling or pick a different medicine.'
const MSG_VALIDATION = "Some of the prescription details couldn't be accepted. Check the dosha and the herb amounts, then try again."
const MSG_AI_UNAVAILABLE =
  "The assistant isn't responding right now. You can write this prescription yourself — the same herb and stock checks still apply."
const MSG_EXTRACTOR_ABSTAINED =
  "The local extractor could not verify this prescription from the notes. You can write it yourself, or try Cloud AI."
const MSG_AI_INVALID_RESPONSE = "The assistant's reply couldn't be read. Try again, or write it yourself."
const MSG_PREVIEW_EXPIRED = 'This draft is over 30 minutes old and has lapsed. Draft it again from your notes?'
const MSG_INSUFFICIENT_STOCK = 'Not enough stock to dispense this amount.'
const MSG_ALREADY_CONFIRMED = 'This prescription has already been issued.'
const MSG_IDEMPOTENCY_CONFLICT = 'Something changed while confirming. Check the herbs below and confirm again.'
const MSG_CONNECTION = "AyurCore can't be reached. Check the clinic's connection, or ask whoever set this up."
const MSG_SERVER = 'Something went wrong on our side and nothing was saved. Try once more.'
const MSG_UNKNOWN = 'Something went wrong and nothing was saved. Try once more.'
const MSG_FORBIDDEN_SCOPE = "You don't have access to that clinic's data."
// Patient registry. Written for this screen rather than borrowed from the
// prescription messages above, whose wording (dosha, herbs, confirming) would
// be wrong here.
const MSG_PATIENT_DUPLICATE = 'A patient with this ID is already registered in this clinic. Search for them instead.'
const MSG_PATIENT_INVALID = 'Check the patient ID (up to 50 characters) and the name, then try again.'
const MSG_PATIENT_NOT_FOUND = "That patient record isn't available. Search again and pick the patient from the list."
const MSG_PATIENT_SEARCH_INVALID = 'Type at least 2 characters to search.'

// Conservative, lowercase substring markers -- used ONLY to disambiguate
// responses the backend cannot yet distinguish structurally (it sets
// X-Error-Code on exactly one branch: AI_SERVICE_UNAVAILABLE). Everything
// else, including the three unrelated failures that all return 409, is
// separable only by `detail` prose. Never a security decision -- every
// authorization decision has already been made by the backend before a
// status code reaches this file. Mirrors ui/app.py's own markers exactly.
const MARK_UNKNOWN_HERB = 'is not in inventory'
const MARK_PREVIEW_MISSING = 'not found or expired'
const MARK_ALREADY_CONFIRMED = 'has already been confirmed'
const MARK_INSUFFICIENT_STOCK = 'insufficient stock for herb'
const MARK_IDEMPOTENCY = 'idempotency key'
const MARK_AI_SCHEMA = 'invalid ai schema'
const MARK_CROSS_CLINIC = "cannot access another clinic"
const MARK_DIFFERENT_SESSION = 'different session'

export interface ApiError {
  /** HTTP status code, or null for a failure that never got a response
   *  at all (network/connection failure, timeout). */
  status: number | null
  /** Raw backend `detail` string. NEVER display this directly -- always
   *  go through friendlyMessage()/translateApiError() first. */
  detail: string
  /** The one structured signal the backend provides today
   *  (X-Error-Code response header on the AI-outage branch). */
  errorCode?: string
}

export type ApiOperation =
  | 'login'
  | 'logout'
  | 'preview'
  | 'preview_local'
  | 'manual_preview'
  | 'confirm'
  | 'audit_export'
  | 'audit_events'
  | 'inventory'
  | 'patient_ask'
  | 'patient_brief'
  | 'patient_search'
  | 'patient_history'
  | 'patient_register'

/**
 * Pure translator: ApiError -> (category, message).
 *
 * `operation` matters in exactly two cases, both ported from the existing
 * Streamlit UI: a 401 from /auth/login means "wrong credentials," not
 * "your session expired" (no session existed yet); and a network-level
 * failure calling /prescribe/confirm is an UNCERTAIN outcome, not a plain
 * error, because that is the one call with a real side effect.
 */
export function translateApiError(
  error: ApiError,
  operation?: ApiOperation
): { category: ErrorCategoryType; message: string } {
  const { status, detail, errorCode } = error
  const text = (detail || '').toLowerCase()

  if (errorCode === 'AI_SERVICE_UNAVAILABLE') {
    return { category: ErrorCategory.AI_UNAVAILABLE, message: MSG_AI_UNAVAILABLE }
  }
  if (errorCode === 'EXTRACTOR_ABSTAINED') {
    return { category: ErrorCategory.AI_INVALID_RESPONSE, message: MSG_EXTRACTOR_ABSTAINED }
  }

  if (status === null) {
    if (operation === 'confirm') {
      return {
        category: ErrorCategory.TIMEOUT_UNCERTAIN,
        message:
          'We could not confirm whether that request went through. Do not assume it failed — ' +
          'if you retry with the same details, it is safe: a duplicate will not create a second ' +
          'prescription or deduct inventory twice.',
      }
    }
    return { category: ErrorCategory.CONNECTION, message: MSG_CONNECTION }
  }

  if (status === 401) {
    if (operation === 'login') {
      return { category: ErrorCategory.SIGN_IN_FAILED, message: MSG_SIGN_IN_FAILED }
    }
    return { category: ErrorCategory.SESSION, message: MSG_SESSION }
  }

  // Patient registry: only the statuses whose generic meaning above/below
  // would be misleading for these three calls. 401/403/5xx/network fall
  // through to the shared handling unchanged.
  if (operation === 'patient_register' && status === 409) {
    return { category: ErrorCategory.PATIENT_DUPLICATE, message: MSG_PATIENT_DUPLICATE }
  }
  if (operation === 'patient_register' && status === 422) {
    return { category: ErrorCategory.VALIDATION, message: MSG_PATIENT_INVALID }
  }
  if (operation === 'patient_search' && status === 422) {
    return { category: ErrorCategory.VALIDATION, message: MSG_PATIENT_SEARCH_INVALID }
  }
  if (operation === 'patient_history' && status === 404) {
    return { category: ErrorCategory.PATIENT_NOT_FOUND, message: MSG_PATIENT_NOT_FOUND }
  }

  if (status === 403) {
    if (text.includes(MARK_CROSS_CLINIC)) {
      return { category: ErrorCategory.FORBIDDEN_SCOPE, message: MSG_FORBIDDEN_SCOPE }
    }
    if (text.includes(MARK_DIFFERENT_SESSION)) {
      return { category: ErrorCategory.PREVIEW_EXPIRED, message: MSG_PREVIEW_EXPIRED }
    }
    return { category: ErrorCategory.WRONG_ROLE, message: MSG_WRONG_ROLE }
  }

  if (status === 400) {
    if (text.includes(MARK_UNKNOWN_HERB)) {
      return { category: ErrorCategory.UNKNOWN_HERB, message: MSG_UNKNOWN_HERB }
    }
    return { category: ErrorCategory.VALIDATION, message: MSG_VALIDATION }
  }

  if (status === 404) {
    if (text.includes(MARK_PREVIEW_MISSING)) {
      return { category: ErrorCategory.PREVIEW_EXPIRED, message: MSG_PREVIEW_EXPIRED }
    }
    return { category: ErrorCategory.UNKNOWN, message: MSG_UNKNOWN }
  }

  if (status === 409) {
    if (text.includes(MARK_INSUFFICIENT_STOCK)) {
      return { category: ErrorCategory.INSUFFICIENT_STOCK, message: MSG_INSUFFICIENT_STOCK }
    }
    if (text.includes(MARK_ALREADY_CONFIRMED)) {
      return { category: ErrorCategory.ALREADY_CONFIRMED, message: MSG_ALREADY_CONFIRMED }
    }
    if (text.includes(MARK_IDEMPOTENCY)) {
      return { category: ErrorCategory.IDEMPOTENCY_CONFLICT, message: MSG_IDEMPOTENCY_CONFLICT }
    }
    // Unrecognised 409: say the safest true thing rather than guess which.
    return { category: ErrorCategory.IDEMPOTENCY_CONFLICT, message: MSG_IDEMPOTENCY_CONFLICT }
  }

  if (status === 422) {
    if (text.includes(MARK_AI_SCHEMA)) {
      return { category: ErrorCategory.AI_INVALID_RESPONSE, message: MSG_AI_INVALID_RESPONSE }
    }
    return { category: ErrorCategory.VALIDATION, message: MSG_VALIDATION }
  }

  if (status >= 500) {
    return { category: ErrorCategory.SERVER, message: MSG_SERVER }
  }

  return { category: ErrorCategory.UNKNOWN, message: MSG_UNKNOWN }
}

/** Convenience wrapper: the message alone, for a call site that doesn't
 *  need the category. */
export function friendlyMessage(error: ApiError, operation?: ApiOperation): string {
  return translateApiError(error, operation).message
}

export function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === 'object' &&
    value !== null &&
    'detail' in value &&
    'status' in value
  )
}
