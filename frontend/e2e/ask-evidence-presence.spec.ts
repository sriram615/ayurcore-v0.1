/**
 * Pure-function tests (no browser) for evidence FIELD PRESENCE across the normalisation boundary of lib/ask-view.ts.
 *
 * A prescription record cites its herbs and dosha as a value or as an explicit JSON null ("unknown"). lib/ask-view.ts normalises both an
 * explicit null and an ABSENT property to null, so lib/ask-present.ts could not tell "the backend said unknown" from "the field is missing".
 * The prescription view therefore also carries explicitNull = { herbs: raw.herbs === null, dosha: raw.dosha === null } (strict comparison with
 * the raw property), and the one case that depends on it -- the legitimate EMPTY list of an all-unknown history -- requires the flag of the
 * field the list is about on EVERY cited prescription. Everything else is unchanged: non-empty lists, wording, evidence validation.
 *
 * Replies are REAL backend replies (e2e/fixtures/ask-empty-partial.ts, e2e/fixtures/ask-real.ts), mutated one field at a time.
 * Rendering in the browser is NOT covered here.
 */
import { expect, test } from '@playwright/test'

import { toAskView, type AskView } from '../lib/ask-view'
import { reply } from './fixtures/ask-real'
import { emptyReply } from './fixtures/ask-empty-partial'

const PK = 5
type Raw = Record<string, any>
type Field = 'herbs' | 'dosha'
const CASES: Array<{ key: string; field: Field; other: Field; label: string }> = [
  { key: 'allunknown: List all the herbs prescribed so far', field: 'herbs', other: 'dosha', label: 'herb list' },
  { key: 'allunknown: Which herbs have been prescribed?', field: 'herbs', other: 'dosha', label: 'herb list (2)' },
  { key: 'allunknown: List the dosha of each prescription', field: 'dosha', other: 'herbs', label: 'dosha list' },
  { key: 'allunknown: What were the doshas recorded on each prescription?', field: 'dosha', other: 'herbs', label: 'dosha list (2)' },
]
const copy = (key: string, mutate?: (r: Raw) => void): Raw => {
  const raw = emptyReply(key, PK) as Raw
  mutate?.(raw)
  return raw
}
const view = (raw: unknown) => toAskView(raw, PK)
const answerOf = (raw: unknown): Extract<AskView, { kind: 'answer' }> => {
  const v = view(raw)
  if (!v || v.kind !== 'answer') throw new Error('not an answer')
  return v
}
const APPROVED_HEADLINE = 'Not known'
const approved = (field: Field) => ({
  title: field === 'herbs' ? 'Herbs prescribed' : 'Dosha by prescription, oldest first', headline: APPROVED_HEADLINE,
  summary: field === 'herbs' ? 'No herbs can be listed: none are recorded on the cited prescriptions.' : 'No dosha can be listed: none is recorded on the cited prescriptions.',
  caveat: field === 'herbs'
    ? '3 prescriptions (#101, #102, #103) have herbs not recorded. This is not a record that no herbs were prescribed.'
    : '3 prescriptions (#101, #102, #103) have no dosha recorded. This is not a record that no dosha applied.',
})
const KNOWN_VALUE: Record<Field, unknown> = { herbs: [{ name: 'Neem', grams: 5 }], dosha: 'Vata' }

for (const { key, field, other, label } of CASES) {
  // ---- explicit null: the approved incomplete answer -------------------------------------------------
  test(`explicit null on every record still gives the approved incomplete answer (${label})`, () => {
    const raw = copy(key)
    for (const record of raw.evidence) expect(record[field]).toBeNull()      // the fixture really sends an explicit null
    const v = answerOf(raw)
    expect(v.incomplete).toBe(true)
    expect(v.shown).toMatchObject(approved(field))
    expect(v.shown.items).toEqual([])
  })

  // ---- absent / undefined relevant field: rejected -----------------------------------------------------
  for (const index of [0, 1, 2]) {
    test(`the relevant field ABSENT on record ${index + 1} of 3 is rejected (${label})`, () => {
      expect(view(copy(key, (r) => { delete r.evidence[index][field] }))).toBeNull()
    })
    test(`the relevant field present but UNDEFINED on record ${index + 1} of 3 is rejected (${label})`, () => {
      expect(view(copy(key, (r) => { r.evidence[index][field] = undefined }))).toBeNull()
    })
  }
  test(`the relevant field absent on every record is rejected (${label})`, () => {
    expect(view(copy(key, (r) => { for (const record of r.evidence) delete record[field] }))).toBeNull()
  })
  test(`the relevant field undefined on every record is rejected (${label})`, () => {
    expect(view(copy(key, (r) => { for (const record of r.evidence) record[field] = undefined }))).toBeNull()
  })
  test(`a mixture of explicit null, absent and undefined is rejected (${label})`, () => {
    expect(view(copy(key, (r) => { delete r.evidence[1][field] }))).toBeNull()
    expect(view(copy(key, (r) => { delete r.evidence[0][field]; r.evidence[2][field] = undefined }))).toBeNull()
    expect(view(copy(key, (r) => { r.evidence = [r.evidence[0], { ...r.evidence[1], [field]: undefined }] }))).toBeNull()
  })
  test(`a single cited record with the field absent is rejected, with it explicit null accepted (${label})`, () => {
    expect(view(copy(key, (r) => { r.evidence = [r.evidence[0]]; delete r.evidence[0][field] }))).toBeNull()
    expect(answerOf(copy(key, (r) => { r.evidence = [r.evidence[0]] })).shown.headline).toBe(APPROVED_HEADLINE)
  })

  // ---- an unrelated field never blocks ------------------------------------------------------------------
  test(`the OTHER field absent or undefined does not block a list whose relevant field is explicit null (${label})`, () => {
    expect(answerOf(copy(key, (r) => { for (const record of r.evidence) delete record[other] })).shown).toMatchObject(approved(field))
    expect(answerOf(copy(key, (r) => { for (const record of r.evidence) record[other] = undefined })).shown).toMatchObject(approved(field))
    expect(answerOf(copy(key, (r) => { delete r.evidence[1][other] })).shown).toMatchObject(approved(field))
  })

  // ---- known and malformed values stay rejected for an empty list ---------------------------------------
  test(`a known value on the relevant field keeps the empty list rejected (${label})`, () => {
    expect(view(copy(key, (r) => { r.evidence[1][field] = KNOWN_VALUE[field] }))).toBeNull()
    expect(view(copy(key, (r) => { for (const record of r.evidence) record[field] = KNOWN_VALUE[field] }))).toBeNull()
  })
  test(`a known EMPTY herb list is a known value, not an unknown one (${label})`, () => {
    if (field === 'herbs') expect(view(copy(key, (r) => { r.evidence[0].herbs = [] }))).toBeNull()
    else expect(view(copy(key, (r) => { r.evidence[0].dosha = '' }))).toBeNull()
  })
  const MALFORMED: Record<Field, unknown[]> = {
    herbs: ['Neem', 5, {}, [null], [{ grams: 5 }], [{ name: 'Neem', grams: 0 }], [{ name: 'Neem', grams: '5' }], ['Neem']],
    dosha: ['', '  ', 5, {}, [], true],
  }
  for (const bad of MALFORMED[field]) {
    test(`a malformed ${field} (${JSON.stringify(bad)}) on one record is rejected (${label})`, () => {
      expect(view(copy(key, (r) => { r.evidence[1][field] = bad }))).toBeNull()
    })
  }

  // ---- the flag itself ---------------------------------------------------------------------------------
  test(`explicitNull mirrors the raw reply exactly: explicit null true, everything else false (${label})`, () => {
    const raw = copy(key)
    const v = answerOf(raw)
    for (const [i, record] of v.evidence.entries()) {
      if (record.kind !== 'prescription') throw new Error('prescription expected')
      expect(record.explicitNull).toEqual({ herbs: raw.evidence[i].herbs === null, dosha: raw.evidence[i].dosha === null })
      expect(record.explicitNull).toEqual({ herbs: true, dosha: true })
    }
  })
}

// ---- explicitNull is derived per field and per record, from the raw property ------------------------------

test('explicitNull is true only for an explicit null: absent and known fields are false, per record and per field', () => {
  // a non-empty list reply (the unknown prescription #101 is cited next to the others), with different presence on each record
  const raw = copy('allunknown: List all the herbs prescribed so far', (r) => {
    r.value = ['Neem']                                                      // a non-empty herb list that the records below can justify
    r.evidence[0] = { ...r.evidence[0], herbs: [{ name: 'Neem', grams: 5 }], dosha: 'Vata' }          // known / known
    delete r.evidence[1].herbs; r.evidence[1].dosha = null                                              // absent / explicit null
    r.evidence[2].herbs = null; delete r.evidence[2].dosha                                              // explicit null / absent
  })
  const v = answerOf(raw)
  const flags = v.evidence.map((e) => (e.kind === 'prescription' ? e.explicitNull : null))
  expect(flags).toEqual([{ herbs: false, dosha: false }, { herbs: false, dosha: true }, { herbs: true, dosha: false }])
  // the existing normalised fields are exactly what they were: absent and explicit null both read as null
  expect(v.evidence.map((e) => (e.kind === 'prescription' ? [e.herbs, e.dosha] : null))).toEqual([[[{ name: 'Neem', grams: 5 }], 'Vata'], [null, null], [null, null]])
})

test('a legacy reply (one unknown prescription among known ones) has the flag only on the unknown record', () => {
  const raw = reply('legacy: Which herbs has she been given?', PK) as Raw
  const v = answerOf(raw)
  for (const [i, record] of v.evidence.entries()) {
    if (record.kind !== 'prescription') throw new Error('prescription expected')
    expect(record.explicitNull).toEqual({ herbs: raw.evidence[i].herbs === null, dosha: raw.evidence[i].dosha === null })
  }
  expect(v.evidence.some((e) => e.kind === 'prescription' && e.explicitNull.herbs)).toBe(true)
  expect(v.evidence.some((e) => e.kind === 'prescription' && !e.explicitNull.herbs)).toBe(true)
})

test('encounter evidence is unchanged: it has no explicitNull', () => {
  const v = answerOf(reply('anjali: When did her most recent visit end?', PK))
  expect(v.evidence[0].kind).toBe('encounter')
  expect('explicitNull' in v.evidence[0]).toBe(false)
})

// ---- non-empty lists are unchanged -------------------------------------------------------------------------

test('a NON-empty list never reads the flag: an omitted field on the unknown record gives exactly the same answer as before', () => {
  for (const key of ['legacy: Which herbs has she been given?', 'legacy: List the dosha of each prescription.']) {
    const original = answerOf(reply(key, PK))
    const raw = reply(key, PK) as Raw
    for (const record of raw.evidence) if (record.herbs === null || record.dosha === null) { if (record.herbs === null) delete record.herbs; if (record.dosha === null) delete record.dosha }
    expect(answerOf(raw).shown).toEqual(original.shown)
  }
})

test('mixed-history and exact non-empty lists keep their known entries, order, scope suffix and caveat', () => {
  const shown = (key: string) => answerOf(reply(key, PK)).shown
  expect(shown('legacy: Which herbs has she been given?')).toMatchObject({ title: 'Herbs prescribed', headline: '2', items: ['Neem', 'Brahmi'], caveat: '1 prescription (#9) has herbs not recorded; any herbs in it are not listed.' })
  expect(shown('legacy: List the dosha of each prescription.')).toMatchObject({ title: 'Dosha by prescription, oldest first', headline: '2', caveat: '1 prescription (#9) has no dosha recorded and is not listed.' })
  expect(shown('anjali: Which herbs has she been given?')).toMatchObject({ title: 'Herbs prescribed', headline: '5', items: ['Ashwagandha', 'Neem', 'Brahmi', 'Punarnava', 'Guggulu'], caveat: null })
  expect(shown('anjali: What herbs appear across her three most recent prescriptions?')).toMatchObject({ title: 'Herbs prescribed in the latest 3', items: ['Ashwagandha', 'Brahmi', 'Neem', 'Punarnava', 'Guggulu'] })
  expect(shown('anjali: List the dosha of each prescription.')).toMatchObject({ headline: '4', caveat: null })
})

// ---- evidence label validation and fail-closed behaviour are unchanged ------------------------------------

test('evidence label validation and fail-closed behaviour are unchanged', () => {
  const key = 'allunknown: List all the herbs prescribed so far'
  expect(view(copy(key, (r) => { r.evidence[0].label = 'rx:999' }))).toBeNull()                          // label disagrees with the number
  expect(view(copy(key, (r) => { r.evidence[0].label = '' }))).toBeNull()
  expect(view(copy(key, (r) => { delete r.evidence[0].label }))).toBeNull()
  expect(view(copy(key, (r) => { r.evidence[0].kind = 'lab_result' }))).toBeNull()
  expect(view(copy(key, (r) => { r.evidence[0] = { kind: 'unknown', label: 'rx:101' } }))).toBeNull()
  expect(view(copy(key, (r) => { r.evidence[1] = { ...r.evidence[0] } }))).toBeNull()                    // the same record twice
  expect(view(copy(key, (r) => { r.evidence = [] }))).toBeNull()
  expect(view(copy(key, (r) => { r.qualifier = 'exact'; r.category = 'answer' }))).toBeNull()
  expect(view(copy(key, (r) => { r.verified = false }))).toBeNull()
  expect(view(copy(key, (r) => { r.outcome = 'abstain' }))).toBeNull()
  expect(view(copy(key, (r) => { r.evidence.push((reply('anjali: When did her most recent visit end?', PK).evidence as Raw[])[0]) }))).toBeNull()
  expect(answerOf(copy(key)).shown).toMatchObject(approved('herbs'))                                      // and the untouched reply still works
})
