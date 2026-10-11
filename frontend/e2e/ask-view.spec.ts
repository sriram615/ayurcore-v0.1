/**
 * Pure-function tests (no browser) for lib/ask-view.ts: every contract category maps to exactly what the panel may show, readings are kept
 * verbatim, cited records are validated against their schema and against their own label, and anything that does not match the contract
 * maps to null (the panel then shows a fixed message and nothing from the reply). Answers are REAL backend replies (e2e/fixtures/ask-real.ts),
 * mutated one field at a time; how each operation is worded is tested in ask-present.spec.ts.
 */
import { expect, test } from '@playwright/test'

import { MAX_EVIDENCE_SHOWN, toAskView } from '../lib/ask-view'
import { reply } from './fixtures/ask-real'

const PK = 5
const LATEST = 'anjali: What is the latest prescription?'      // prescription #4: Kapha, Guggulu 12 g + Neem 10 g, one cited record
const LEGACY_LATEST = 'legacy: What is the latest prescription?' // prescription #9 has no recorded dosha or herbs: partial
const LEGACY_COMPARE = 'legacy: Did the Neem dose change since the previous prescription?' // depends entirely on unknown values: unknown
const VISIT = 'anjali: When did her most recent visit end?'
const rx = (o: object = {}) => ({ ...(reply(LATEST, PK).evidence as object[])[0], ...o })
const enc = (o: object = {}) => ({ ...(reply(VISIT, PK).evidence as object[])[0], ...o })
const base = { patient_pk: PK, stage: 'done', reason: null, text: null, value: null, qualifier: null, verified: false, options: [], evidence: [], plan_frame: null, plan_query: null, clarified: false }
const answer = (o: object = {}) => reply(LATEST, PK, o)
const incomplete = (o: object = {}) => reply(LEGACY_LATEST, PK, o)
const reading = (text: string, o: object = {}) => ({ frame: 'COUNT_HERB', text, query: { op: 'count', target: 'herb', herb: 'Neem', dosha: null, last_n: null, a: 'previous', b: 'last' }, ...o })
const clarify = (options: unknown[], o: object = {}) => ({ ...base, outcome: 'clarify', category: 'clarify', stage: 'clarify', reason: 'UNDERSPECIFIED', options, ...o })
const refusal = (category: string, o: object = {}) => ({ ...base, outcome: 'abstain', category, stage: 'screen', reason: 'X', ...o })

// --- answers + evidence ---------------------------------------------------------------

test('an exact verified answer is worded from its structured result and keeps its cited records, with no qualification', () => {
  const view = toAskView(answer(), PK)
  expect(view).toMatchObject({
    kind: 'answer', incomplete: false, clarifiedAs: null, evidenceTotal: 1, focusHerb: null,
    shown: { title: 'Latest prescription', headline: 'Prescription #4', caveat: null, rows: [{ label: 'Dosha', value: 'Kapha' }, { label: 'Herbs', value: 'Guggulu 12 g, Neem 10 g' }] },
    evidence: [{ kind: 'prescription', number: 4, status: 'Active', dosha: 'Kapha', herbs: [{ name: 'Guggulu', grams: 12 }, { name: 'Neem', grams: 10 }] }],
  })
})

test('the backend template sentence is required but never reaches the view', () => {
  const view = toAskView(answer({ text: 'DIFFERENT TEMPLATE rx:4 created_at' }), PK)
  expect(view).toMatchObject({ kind: 'answer' })
  expect(JSON.stringify(view)).not.toMatch(/DIFFERENT TEMPLATE|created_at/)
  expect(toAskView(answer({ text: null }), PK)).toBeNull()
  expect(toAskView(answer({ text: '   ' }), PK)).toBeNull()
})

test('an incomplete answer is marked and carries its qualification for each qualifier the backend sends', () => {
  expect(toAskView(incomplete(), PK)).toMatchObject({ kind: 'answer', incomplete: true, shown: { caveat: 'Not recorded on this prescription: dosha and herbs.' } })
  expect(toAskView(reply('legacy: How much Neem in total?', PK), PK)).toMatchObject({ incomplete: true, focusHerb: 'Neem', shown: { headline: 'At least 16 g' } })
  expect(toAskView(reply(LEGACY_COMPARE, PK), PK)).toMatchObject({ incomplete: true, shown: { headline: 'Not known', caveat: expect.stringContaining('Cannot be determined') } })
})

test('records with unknown values are shown as unknown, never filled in', () => {
  const view = toAskView(incomplete(), PK)
  expect(view).toMatchObject({ evidence: [{ kind: 'prescription', number: 9, dosha: null, herbs: null }] })
})

test('an encounter is kept as its status and dates only; its reference is held only to identify it and is never drawn', () => {
  const view = toAskView(answer(reply(VISIT, PK)), PK)
  expect(view).toMatchObject({ kind: 'answer', shown: { title: 'Latest visit', summary: 'Not closed yet' }, evidence: [{ kind: 'encounter', closed: null }] })
  expect(JSON.stringify(view && view.kind === 'answer' && view.shown)).not.toMatch(/enc:|encounter_ref/)
})

test('prescriptions and encounters can be cited together, in the order given', () => {
  const view = toAskView(answer({ evidence: [enc(), rx(), { ...rx(), prescription_id: 3, label: 'rx:3' }] }), PK)
  expect(view && view.kind === 'answer' && view.evidence.map((e) => e.kind)).toEqual(['encounter', 'prescription', 'prescription'])
})

test('only the allowed fields survive: no extra field, plan or stage', () => {
  const view = toAskView(answer({ stage: 'S', reason: 'R', evidence: [rx({ clinic_id: 'CLINIC-X', encounter_pk: 77, secret: 'S3' })] }), PK)
  expect(JSON.stringify(view)).not.toMatch(/CLINIC-X|77|S3|"plan|"stage|"reason/)
})

test('more records than the display cap keep the true total', () => {
  const many = Array.from({ length: MAX_EVIDENCE_SHOWN + 5 }, (_, i) => ({ ...rx(), prescription_id: 100 + i, label: `rx:${100 + i}` }))
  const view = toAskView(answer({ evidence: [rx(), ...many] }), PK)
  expect(view && view.kind === 'answer' && [view.evidence.length, view.evidenceTotal]).toEqual([MAX_EVIDENCE_SHOWN, MAX_EVIDENCE_SHOWN + 6])
})

test('malformed or contradictory evidence refuses the whole reply: it is never shown as the basis of a verified answer', () => {
  const bad: Record<string, unknown> = {
    'no evidence at all': answer({ evidence: [] }), 'evidence missing': { ...answer(), evidence: undefined }, 'evidence not a list': answer({ evidence: 'rx:4' }),
    'evidence is an object': answer({ evidence: { kind: 'prescription' } }), 'a null record': answer({ evidence: [null] }), 'a string record': answer({ evidence: ['rx:4'] }),
    'one good and one bad record': answer({ evidence: [rx(), { ...rx({ status: '' }), prescription_id: 5, label: 'rx:5' }] }),
    'unknown kind': answer({ evidence: [{ kind: 'unknown', label: 'rx:4' }] }), 'other kind': answer({ evidence: [rx({ kind: 'lab' })] }), 'kind missing': answer({ evidence: [rx({ kind: undefined })] }),
    'label missing': answer({ evidence: [rx({ label: undefined })] }), 'label empty': answer({ evidence: [rx({ label: '' })] }),
    'label disagrees with the record': answer({ evidence: [rx({ label: 'rx:13' })] }), 'encounter label disagrees': reply(VISIT, PK, { evidence: [enc({ label: 'enc:B' })] }),
    'prescription id missing': answer({ evidence: [rx({ prescription_id: undefined })] }), 'prescription id zero': answer({ evidence: [rx({ prescription_id: 0, label: 'rx:0' })] }),
    'prescription id negative': answer({ evidence: [rx({ prescription_id: -3, label: 'rx:-3' })] }), 'prescription id a string': answer({ evidence: [rx({ prescription_id: '4' })] }),
    'prescription id fractional': answer({ evidence: [rx({ prescription_id: 1.5, label: 'rx:1.5' })] }),
    'date missing': answer({ evidence: [rx({ created_at: null })] }), 'date empty': answer({ evidence: [rx({ created_at: '  ' })] }), 'status missing': answer({ evidence: [rx({ status: undefined })] }),
    'dosha a number': answer({ evidence: [rx({ dosha: 5 })] }), 'dosha empty': answer({ evidence: [rx({ dosha: '' })] }),
    'herbs not a list': answer({ evidence: [rx({ herbs: 'Neem' })] }), 'herb without a name': answer({ evidence: [rx({ herbs: [{ grams: 5 }] })] }),
    'herb with zero grams': answer({ evidence: [rx({ herbs: [{ name: 'Neem', grams: 0 }] })] }), 'herb grams a string': answer({ evidence: [rx({ herbs: [{ name: 'Neem', grams: '5' }] })] }),
    'herb is a string': answer({ evidence: [rx({ herbs: ['Neem'] })] }),
    'encounter without reference': reply(VISIT, PK, { evidence: [enc({ encounter_ref: undefined })] }), 'encounter without date': reply(VISIT, PK, { evidence: [enc({ created_at: undefined })] }),
    'encounter without status': reply(VISIT, PK, { evidence: [enc({ status: '' })] }), 'encounter closed date empty': reply(VISIT, PK, { evidence: [enc({ closed_at: '' })] }),
    'a clarification that cites records': clarify([reading('One.')], { evidence: [rx()] }),
    'a refusal that cites records': refusal('unsafe', { evidence: [rx()] }), 'a missing answer that cites records': refusal('missing', { evidence: [rx()] }),
  }
  for (const [label, raw] of Object.entries(bad)) expect(toAskView(raw, PK), label).toBeNull()
})

// --- clarification --------------------------------------------------------------------

test('a clarification keeps each reading exactly as the server sent it: frame, wording and query', () => {
  const view = toAskView(clarify([reading('The number of prescriptions containing Neem.'), reading('The total grams of Neem.', { frame: 'SUM_HERB', query: { op: 'sum', target: 'herb', herb: 'Neem', dosha: null, last_n: null, a: 'previous', b: 'last' } })]), PK)
  expect(view).toEqual({ kind: 'clarify', options: [
    { frame: 'COUNT_HERB', text: 'The number of prescriptions containing Neem.', query: { op: 'count', target: 'herb', herb: 'Neem', dosha: null, last_n: null, a: 'previous', b: 'last' } },
    { frame: 'SUM_HERB', text: 'The total grams of Neem.', query: { op: 'sum', target: 'herb', herb: 'Neem', dosha: null, last_n: null, a: 'previous', b: 'last' } },
  ] })
})

test('readings the backend could not have produced are refused, because they could not be sent back', () => {
  const bad: Record<string, unknown> = {
    'no readings': clarify([]), 'four readings': clarify([reading('a'), reading('b'), reading('c'), reading('d')]), 'options missing': clarify(undefined as unknown as unknown[]),
    'options not a list': clarify('x' as unknown as unknown[]), 'reading is a string': clarify(['plain']), 'reading is null': clarify([null]),
    'no text': clarify([{ frame: 'F', query: { op: 'count' } }]), 'blank text': clarify([reading('  ')]), 'text numeric': clarify([reading('x', { text: 5 })]),
    'no frame': clarify([reading('x', { frame: undefined })]), 'empty frame': clarify([reading('x', { frame: '' })]), 'frame too long': clarify([reading('x', { frame: 'F'.repeat(41) })]),
    'frame a list': clarify([reading('x', { frame: ['A'] })]), 'no query': clarify([reading('x', { query: undefined })]), 'query a string': clarify([reading('x', { query: 'op=count' })]),
    'query a list': clarify([reading('x', { query: [] })]), 'query without op': clarify([reading('x', { query: { target: 'herb' } })]), 'query with an extra key': clarify([reading('x', { query: { op: 'count', sql: 'DROP' } })]),
    'query value an object': clarify([reading('x', { query: { op: 'count', herb: { a: 1 } } })]), 'query value a boolean': clarify([reading('x', { query: { op: 'count', last_n: true } })]),
    'query value fractional': clarify([reading('x', { query: { op: 'count', last_n: 1.5 } })]), 'query value too long': clarify([reading('x', { query: { op: 'count', herb: 'N'.repeat(201) } })]),
    'one good and one bad reading': clarify([reading('good'), reading('bad', { query: { op: 'count', sql: 'x' } })]),
    'outcome answer': clarify([reading('a')], { outcome: 'answer' }), 'outcome abstain': clarify([reading('a')], { outcome: 'abstain' }),
  }
  for (const [label, raw] of Object.entries(bad)) expect(toAskView(raw, PK), label).toBeNull()
})

test('the reply to a chosen reading must say so, and cannot itself be another clarification', () => {
  expect(toAskView(answer({ clarified: true }), PK, true)).toMatchObject({ kind: 'answer' })
  expect(toAskView(answer({ clarified: false }), PK, true)).toBeNull()
  expect(toAskView(answer({ clarified: true }), PK, false)).toBeNull()
  expect(toAskView(refusal('missing', { clarified: true }), PK, true)).toEqual({ kind: 'notice', category: 'missing' })
  expect(toAskView(clarify([reading('a')], { clarified: true }), PK, true)).toBeNull()
  expect(toAskView(clarify([reading('a')]), PK, true)).toBeNull()
})

// --- refusals + contract --------------------------------------------------------------

test('missing, unsupported, unsafe and access-denied map to a refusal category only', () => {
  for (const category of ['missing', 'unsupported', 'unsafe', 'access_denied']) {
    expect(toAskView(refusal(category), PK)).toEqual({ kind: 'notice', category })
    expect(toAskView(refusal(category, { text: 'SOME BACKEND TEXT', reason: 'TREATMENT' }), PK)).toEqual({ kind: 'notice', category })
  }
})

test('contract violations are refused (null), never repaired or guessed', () => {
  const cases: Record<string, unknown> = {
    'not an object': 'oops', 'null': null, 'undefined': undefined, 'array': [answer()], 'number': 5, 'empty object': {},
    'another patient': answer({ patient_pk: 6 }), 'patient missing': answer({ patient_pk: undefined }), 'patient as string': answer({ patient_pk: '5' }),
    'unknown category': answer({ category: 'diagnosis' }), 'category missing': answer({ category: undefined }), 'category not a string': answer({ category: 7 }),
    'answer not verified': answer({ verified: false }), 'answer verified as a string': answer({ verified: 'true' }), 'answer without text': answer({ text: null }),
    'answer with blank text': answer({ text: '   ' }), 'answer with numeric text': answer({ text: 5 }), 'answer qualifier not exact': answer({ qualifier: 'at_least' }),
    'answer qualifier missing': answer({ qualifier: null }), 'answer with the wrong outcome': answer({ outcome: 'abstain' }), 'answer with a clarify outcome': answer({ outcome: 'clarify' }),
    'incomplete exact qualifier': incomplete({ qualifier: 'exact' }), 'incomplete not_recorded qualifier': incomplete({ qualifier: 'not_recorded' }), 'incomplete not verified': incomplete({ verified: false }),
    'incomplete without text': incomplete({ text: '' }), 'incomplete without a qualifier': incomplete({ qualifier: null }),
    'refusal with an answer outcome': refusal('unsafe', { outcome: 'answer' }), 'refusal with a clarify outcome': refusal('missing', { outcome: 'clarify' }),
    'outcome missing': refusal('unsafe', { outcome: undefined }), 'outcome unknown': refusal('unsafe', { outcome: 'allow' }),
    'clarified flag not a boolean': answer({ clarified: 'no' }),
    'answer without a structured result': answer({ value: null }), 'answer without the executed query': answer({ plan_query: null }),
  }
  for (const [label, raw] of Object.entries(cases)) expect(toAskView(raw, PK), label).toBeNull()
})
