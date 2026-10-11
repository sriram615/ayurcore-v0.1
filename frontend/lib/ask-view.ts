/**
 * Turns the backend's AskResponse into exactly what the Ask panel may show -- a pure function, no React and no network.
 *
 * The backend decides everything (the category, the verified flag, the qualifier, the executed query and its structured result, the wording
 * of a reading, the cited records). This file (1) refuses anything that does not match the contract, returning null so the panel shows a
 * fixed "couldn't be shown" message instead of a guess, (2) hands a verified answer's structured result to lib/ask-present.ts, which
 * cross-checks it against the cited records and words it for a doctor, and (3) keeps the few fields the panel must send back. The backend's
 * `text` (the verifier's template sentence) is required to be present but is never displayed or parsed; `stage`, `reason` and the labels of
 * records are never displayed. It never builds a reading: the readings of a clarification are kept exactly as the server sent them, so the
 * only thing the panel can ever send back is one of them.
 *
 * Contract checked here (backend/schemas.py AskResponse, AskEvidence, AskOption; ai2/result.py categories):
 *   answer         outcome answer,  verified, qualifier exact,                      non-empty text, 1+ valid evidence
 *   incomplete     outcome answer,  verified, qualifier at_least | partial | unknown, non-empty text, 1+ valid evidence
 *   clarify        outcome clarify, 1..3 readings (frame, text, query), no evidence, and never the reply to a chosen reading
 *   missing | unsupported | unsafe | access_denied   outcome abstain, no evidence
 *   clarified      true exactly when the request carried a chosen reading
 * and the reply must be about the patient that was asked about. Evidence is valid only if every record matches its kind's schema and its
 * label agrees with its own fields (rx:<prescription_id>, enc:<encounter_ref>); otherwise the whole reply is refused, so evidence that does
 * not hold together is never shown as the basis of a verified answer.
 */
import type { AskCategory } from './api-types'
import { presentAnswer, type Presented } from './ask-present'

export const MAX_READINGS = 3
/** Cited records shown at most; the line "Showing N of M records" says so when there are more. */
export const MAX_EVIDENCE_SHOWN = 20

export type NoticeCategory = 'missing' | 'unsupported' | 'unsafe' | 'access_denied'

/** A reading offered by a clarification, exactly as the server sent it. `frame` and `query` are opaque here: only ever sent back verbatim. */
export interface Reading {
  frame: string
  text: string
  query: Record<string, string | number | null>
}

export type EvidenceView =
  | {
    kind: 'prescription'; label: string; number: number; date: string; status: string; dosha: string | null; herbs: { name: string; grams: number }[] | null
    /** True only where the reply carried that property as an explicit JSON null (`raw.herbs === null`). An absent property or an undefined value reads as
     *  null in `herbs` / `dosha` above (as before) but is false here, so the one case that must tell "unknown" from "missing" can. */
    explicitNull: { herbs: boolean; dosha: boolean }
  }
  | { kind: 'encounter'; label: string; status: string; opened: string; closed: string | null }

export type AskView =
  | { kind: 'answer'; shown: Presented; incomplete: boolean; focusHerb: string | null; evidence: EvidenceView[]; evidenceTotal: number; clarifiedAs: string | null }
  | { kind: 'clarify'; options: Reading[] }
  /** `tooLong`: present (true) only for an `unsupported` refusal at the normalisation stage, i.e. a question over the AI-2 reader's limit (300 characters / 60 words). */
  | { kind: 'notice'; category: NoticeCategory; tooLong?: true }

const CATEGORIES: readonly AskCategory[] = ['answer', 'incomplete', 'clarify', 'missing', 'unsupported', 'unsafe', 'access_denied']
const NOTICES: readonly string[] = ['missing', 'unsupported', 'unsafe', 'access_denied']
/** The fields a reading's query may have (backend AskChoice validator). */
const QUERY_KEYS: readonly string[] = ['op', 'target', 'herb', 'dosha', 'last_n', 'a', 'b']

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const nonEmpty = (v: unknown): v is string => typeof v === 'string' && v.trim().length > 0
const positiveInt = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v > 0

function readingFrom(raw: unknown): Reading | null {
  if (!isRecord(raw) || !nonEmpty(raw.frame) || raw.frame.length > 40 || !nonEmpty(raw.text) || !isRecord(raw.query)) return null
  const query = raw.query
  if (!nonEmpty(query.op)) return null
  for (const [key, value] of Object.entries(query)) {
    if (!QUERY_KEYS.includes(key)) return null
    const ok = value === null || (typeof value === 'string' && value.length <= 200) || (typeof value === 'number' && Number.isInteger(value))
    if (!ok) return null
  }
  return { frame: raw.frame, text: raw.text, query: query as Reading['query'] }
}

function evidenceFrom(raw: unknown): EvidenceView | null {
  if (!isRecord(raw) || !nonEmpty(raw.label)) return null
  if (raw.kind === 'prescription') {
    if (!positiveInt(raw.prescription_id) || raw.label !== `rx:${raw.prescription_id}`) return null
    if (!nonEmpty(raw.created_at) || !nonEmpty(raw.status)) return null
    if (raw.dosha !== null && raw.dosha !== undefined && !nonEmpty(raw.dosha)) return null
    let herbs: { name: string; grams: number }[] | null = null
    if (raw.herbs !== null && raw.herbs !== undefined) {
      if (!Array.isArray(raw.herbs)) return null
      herbs = []
      for (const herb of raw.herbs) {
        if (!isRecord(herb) || !nonEmpty(herb.name) || !positiveInt(herb.grams)) return null
        herbs.push({ name: herb.name, grams: herb.grams })
      }
    }
    return {
      kind: 'prescription', label: raw.label, number: raw.prescription_id, date: raw.created_at, status: raw.status, dosha: (raw.dosha as string | null | undefined) ?? null, herbs,
      explicitNull: { herbs: raw.herbs === null, dosha: raw.dosha === null },
    }
  }
  if (raw.kind === 'encounter') {
    if (!nonEmpty(raw.encounter_ref) || raw.label !== `enc:${raw.encounter_ref}`) return null
    if (!nonEmpty(raw.created_at) || !nonEmpty(raw.status)) return null
    if (raw.closed_at !== null && raw.closed_at !== undefined && !nonEmpty(raw.closed_at)) return null
    return { kind: 'encounter', label: raw.label, status: raw.status, opened: raw.created_at, closed: (raw.closed_at as string | null | undefined) ?? null }
  }
  return null   // any other kind (including the backend's "unknown") is not a record the answer can be shown to rest on
}

/**
 * `expectClarified` is true for the reply to a request that carried a chosen reading and false otherwise: the reply's own `clarified` flag
 * must agree, and a chosen reading can never be answered by another clarification.
 */
export function toAskView(raw: unknown, expectedPatientPk: number, expectClarified = false): AskView | null {
  if (!isRecord(raw)) return null
  if (raw.patient_pk !== expectedPatientPk) return null
  if (raw.clarified !== undefined && raw.clarified !== expectClarified) return null
  const { outcome, category } = raw
  if (typeof category !== 'string' || !CATEGORIES.includes(category as AskCategory)) return null
  const evidence = raw.evidence === undefined ? [] : raw.evidence
  if (!Array.isArray(evidence)) return null

  if (category === 'answer' || category === 'incomplete') {
    if (outcome !== 'answer' || raw.verified !== true || !nonEmpty(raw.text)) return null
    if (evidence.length < 1) return null
    const records: EvidenceView[] = []
    for (const item of evidence) {
      const record = evidenceFrom(item)
      if (!record) return null
      records.push(record)
    }
    // The qualifier must be the one the category promises; the structured result must agree with the cited records (ask-present.ts).
    const qualifier = raw.qualifier
    if (typeof qualifier !== 'string' || (category === 'answer') !== (qualifier === 'exact') || !['exact', 'at_least', 'partial', 'unknown'].includes(qualifier)) return null
    const shown = presentAnswer(raw.plan_query, raw.value, qualifier, records)
    if (!shown || (qualifier === 'exact') !== (shown.caveat === null)) return null
    const focus = isRecord(raw.plan_query) && nonEmpty(raw.plan_query.herb) ? raw.plan_query.herb : null
    return { kind: 'answer', shown, incomplete: qualifier !== 'exact', focusHerb: focus, evidence: records.slice(0, MAX_EVIDENCE_SHOWN), evidenceTotal: records.length, clarifiedAs: null }
  }

  if (evidence.length > 0) return null   // a clarification or a refusal that cites records contradicts itself

  if (category === 'clarify') {
    if (expectClarified || outcome !== 'clarify' || !Array.isArray(raw.options)) return null
    if (raw.options.length < 1 || raw.options.length > MAX_READINGS) return null
    const options: Reading[] = []
    for (const option of raw.options) {
      const reading = readingFrom(option)
      if (!reading) return null
      options.push(reading)
    }
    return { kind: 'clarify', options }
  }

  if (NOTICES.includes(category) && outcome === 'abstain') {
    // The one stage the panel looks at: the normaliser refuses a question over its length limit (the API itself accepts up to 1000 characters).
    const tooLong = category === 'unsupported' && raw.stage === 'normalize'
    return tooLong ? { kind: 'notice', category: 'unsupported', tooLong: true } : { kind: 'notice', category: category as NoticeCategory }
  }
  return null
}
