/**
 * Pure-function tests (no browser): a question over the AI-2 reader's length limit is refused at the normalisation stage
 * (backend: 200, outcome "abstain", category "unsupported", stage "normalize"; the API itself accepts up to 1000 characters).
 * lib/ask-view.ts marks exactly that refusal `tooLong` so the panel can say "too long" instead of "can't be answered from the record".
 * Nothing else changes: other refusals, other stages and the contract checks stay as they were.
 */
import { expect, test } from '@playwright/test'

import { toAskView } from '../lib/ask-view'

const PK = 5
const base = { patient_pk: PK, stage: 'screen', reason: 'AMBIGUOUS', text: null, value: null, qualifier: null, verified: false, options: [], evidence: [], plan_frame: null, plan_query: null, clarified: false }
const refusal = (category: string, o: object = {}) => ({ ...base, outcome: 'abstain', category, ...o })
const rx = { kind: 'prescription', label: 'rx:1', prescription_id: 1, created_at: '2026-03-01 09:00:00', status: 'CONFIRMED', dosha: null, herbs: null }

test('an unsupported refusal at the normalize stage is marked tooLong', () => {
  expect(toAskView(refusal('unsupported', { stage: 'normalize' }), PK)).toEqual({ kind: 'notice', category: 'unsupported', tooLong: true })
})

test('an unsupported refusal at any other stage is the ordinary notice, without the marker', () => {
  for (const stage of ['screen', 'reference', 'planner', 'coverage', 'alignment', 'executor', 'verifier']) {
    expect(toAskView(refusal('unsupported', { stage }), PK), stage).toEqual({ kind: 'notice', category: 'unsupported' })
  }
})

test('other categories at the normalize stage are not marked', () => {
  for (const category of ['missing', 'unsafe', 'access_denied']) {
    expect(toAskView(refusal(category, { stage: 'normalize' }), PK), category).toEqual({ kind: 'notice', category })
  }
})

test('the marker never loosens validation: a refusal citing records, a wrong patient or a wrong outcome is still refused', () => {
  expect(toAskView(refusal('unsupported', { stage: 'normalize', evidence: [rx] }), PK)).toBeNull()
  expect(toAskView(refusal('unsupported', { stage: 'normalize', patient_pk: 99 }), PK)).toBeNull()
  expect(toAskView({ ...refusal('unsupported', { stage: 'normalize' }), outcome: 'answer' }, PK)).toBeNull()
  expect(toAskView({ ...refusal('unsupported', { stage: 'normalize' }), outcome: 'clarify' }, PK)).toBeNull()
  expect(toAskView({ ...refusal('unsupported', { stage: 'normalize' }), clarified: true }, PK)).toBeNull()
})

test('a missing or non-string stage is the ordinary notice', () => {
  const { stage: _omit, ...noStage } = refusal('unsupported', { stage: 'normalize' }) as Record<string, unknown>
  expect(toAskView(noStage, PK)).toEqual({ kind: 'notice', category: 'unsupported' })
  expect(toAskView(refusal('unsupported', { stage: 5 }), PK)).toEqual({ kind: 'notice', category: 'unsupported' })
})
