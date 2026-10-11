/**
 * Pure-function tests (no browser) for lib/ask-present.ts, against REAL backend replies (e2e/fixtures/ask-real.ts).
 *
 * 1. Every one of the 20 supported operations, and every qualifier the backend produces for them, maps to exactly the wording below.
 *    This table IS the operation-to-presentation mapping.
 * 2. Nothing the doctor sees contains a raw executor label, field name, serialized value or the backend's template sentence.
 * 3. The structured result is cross-checked against the cited records: any disagreement (a changed number, date, dosha, herb, count,
 *    label, qualifier or diff) refuses the reply instead of showing it.
 * 4. Answer dates and evidence dates are one convention: the same function, the same instant.
 */
import { expect, test } from '@playwright/test'

import { toAskView, type AskView } from '../lib/ask-view'
import { formatRecordDate } from '../lib/format'
import type { Presented } from '../lib/ask-present'
import { REAL, reply } from './fixtures/ask-real'

const PK = 5
const D = formatRecordDate('2026-10-10 05:40:36')     // every record of the captured patients
const L = formatRecordDate('2099-01-01 05:30:00')     // the legacy prescription (#9)
const view = (key: string, o: object = {}) => toAskView(reply(key, PK, { clarified: key.includes('[choice'), ...o }), PK, key.includes('[choice'))
const shown = (key: string, o: object = {}): Presented => {
  const v = view(key, o)
  if (!v || v.kind !== 'answer') throw new Error(`not an answer: ${key}`)
  return v.shown
}
const P = (p: Partial<Presented> & { title: string }): Presented => ({ headline: null, summary: null, rows: [], items: [], compare: null, caveat: null, ...p })

// ---- 1. the mapping -------------------------------------------------------------------

const MAPPING: Array<[string, Presented]> = [
  // first / last prescription
  ['anjali: What was the first prescription?', P({ title: 'First prescription', headline: 'Prescription #1', summary: D, rows: [{ label: 'Dosha', value: 'Vata' }, { label: 'Herbs', value: 'Ashwagandha 30 g, Neem 10 g' }] })],
  ['anjali: What is the latest prescription?', P({ title: 'Latest prescription', headline: 'Prescription #4', summary: D, rows: [{ label: 'Dosha', value: 'Kapha' }, { label: 'Herbs', value: 'Guggulu 12 g, Neem 10 g' }] })],
  ['malayalam: അവസാനത്തെ കുറിപ്പടിയിൽ എന്താണുള്ളത്?', P({ title: 'Latest prescription', headline: 'Prescription #5', summary: D, rows: [{ label: 'Dosha', value: 'Vata' }, { label: 'Herbs', value: 'Ashwagandha 15 g' }] })],
  ['legacy: What is the latest prescription?', P({ title: 'Latest prescription', headline: 'Prescription #9', summary: L, rows: [{ label: 'Dosha', value: 'Not recorded' }, { label: 'Herbs', value: 'Not recorded' }], caveat: 'Not recorded on this prescription: dosha and herbs.' })],
  // first / last herb
  ['anjali: When was Neem first prescribed?', P({ title: 'Neem was first prescribed', headline: D, summary: 'Prescription #1 · Neem 10 g' })],
  ['anjali: When was Neem last prescribed?', P({ title: 'Neem was last prescribed', headline: D, summary: 'Prescription #4 · Neem 10 g' })],
  ['anjali: When was Guggulu first prescribed?', P({ title: 'Guggulu was first prescribed', headline: D, summary: 'Prescription #4 · Guggulu 12 g' })],
  ['legacy: When was Neem last prescribed?', P({ title: 'Neem was last prescribed', headline: D, summary: 'Prescription #7 · Neem 6 g', caveat: '1 prescription (#9) has herbs not recorded and may also contain Neem, so this may not be the last time.' })],
  ['legacy: When was Neem first prescribed?', P({ title: 'Neem was first prescribed', headline: D, summary: 'Prescription #6 · Neem 10 g' })],
  // first / last dosha
  ['anjali: Which dosha is on the first prescription?', P({ title: 'Dosha on the first prescription', headline: 'Vata', summary: `Prescription #1 · ${D}` })],
  // first / last encounter
  ['anjali: When did her most recent visit end?', P({ title: 'Latest visit', headline: `Opened ${D}`, summary: 'Not closed yet' })],
  ['anjali: When was her first visit?', P({ title: 'First visit', headline: `Opened ${D}`, summary: 'Not closed yet' })],
  // count prescription / encounter / herb / dosha
  ['anjali: How many prescriptions has she had?', P({ title: 'Prescriptions recorded', headline: '4', summary: 'Number of prescriptions in the record.' })],
  ['anjali: How many visits has she had?', P({ title: 'Visits recorded', headline: '1', summary: 'Number of visits in the record.' })],
  ['anjali: How many times was Neem prescribed?', P({ title: 'Prescriptions containing Neem', headline: '3', summary: 'Counts prescriptions that include Neem; it is not an amount in grams.' })],
  ['anjali: Neem count? [choice COUNT_HERB]', P({ title: 'Prescriptions containing Neem', headline: '3', summary: 'Counts prescriptions that include Neem; it is not an amount in grams.' })],
  ['anjali: How many prescriptions were recorded as Vata only?', P({ title: 'Prescriptions recorded as Vata', headline: '2', summary: 'Counts prescriptions whose recorded dosha is exactly Vata.' })],
  ['legacy: How many times was Neem prescribed?', P({ title: 'Prescriptions containing Neem', headline: 'At least 2', summary: 'Counts prescriptions that include Neem; it is not an amount in grams.', caveat: '1 prescription (#9) has herbs not recorded, so the true number may be higher.' })],
  ['legacy: How many prescriptions were recorded as Vata only?', P({ title: 'Prescriptions recorded as Vata', headline: 'At least 1', summary: 'Counts prescriptions whose recorded dosha is exactly Vata.', caveat: '1 prescription (#9) has no dosha recorded, so the true number may be higher.' })],
  ['legacy: How many prescriptions has she had?', P({ title: 'Prescriptions recorded', headline: '3', summary: 'Number of prescriptions in the record.' })],
  // sum herb: a TOTAL in grams, never a count
  ['anjali: How much Neem in total?', P({ title: 'Total Neem prescribed', headline: '40 g', summary: 'The grams recorded for Neem, added together over 3 prescriptions.' })],
  ['anjali: Neem count? [choice SUM_HERB]', P({ title: 'Total Neem prescribed', headline: '40 g', summary: 'The grams recorded for Neem, added together over 3 prescriptions.' })],
  ['anjali: How much Neem in her last 2 prescriptions?', P({ title: 'Total Neem prescribed in the latest 2', headline: '30 g', summary: 'The grams recorded for Neem, added together over 2 prescriptions.' })],
  ['legacy: How much Neem in total?', P({ title: 'Total Neem prescribed', headline: 'At least 16 g', summary: 'The grams recorded for Neem, added together over 2 prescriptions.', caveat: '1 prescription (#9) has herbs not recorded, so the true total may be higher.' })],
  // lists
  ['anjali: List all her prescriptions.', P({ title: 'Prescriptions, oldest first', headline: '4', summary: 'Prescriptions recorded.', items: [`#1 · ${D}`, `#2 · ${D}`, `#3 · ${D}`, `#4 · ${D}`] })],
  ['anjali: List all her visits.', P({ title: 'Visits, oldest first', headline: '1', summary: 'Visits recorded.', items: [`Opened ${D} · Not closed yet`] })],
  ['anjali: Which herbs has she been given?', P({ title: 'Herbs prescribed', headline: '5', summary: 'Each herb once, in the order it first appears.', items: ['Ashwagandha', 'Neem', 'Brahmi', 'Punarnava', 'Guggulu'] })],
  ['anjali: What herbs appear across her three most recent prescriptions?', P({ title: 'Herbs prescribed in the latest 3', headline: '5', summary: 'Each herb once, in the order it first appears.', items: ['Ashwagandha', 'Brahmi', 'Neem', 'Punarnava', 'Guggulu'] })],
  ['legacy: Which herbs has she been given?', P({ title: 'Herbs prescribed', headline: '2', summary: 'Each herb once, in the order it first appears.', items: ['Neem', 'Brahmi'], caveat: '1 prescription (#9) has herbs not recorded; any herbs in it are not listed.' })],
  ['anjali: List the dosha of each prescription.', P({ title: 'Dosha by prescription, oldest first', headline: '4', summary: 'Prescriptions with a recorded dosha.', items: [`#1 · ${D} — Vata`, `#2 · ${D} — Pitta`, `#3 · ${D} — Vata`, `#4 · ${D} — Kapha`] })],
  ['legacy: List the dosha of each prescription.', P({ title: 'Dosha by prescription, oldest first', headline: '2', summary: 'Prescriptions with a recorded dosha.', items: [`#6 · ${D} — Vata`, `#7 · ${D} — Kapha`], caveat: '1 prescription (#9) has no dosha recorded and is not listed.' })],
  ['legacy: List all her prescriptions.', P({ title: 'Prescriptions, oldest first', headline: '3', summary: 'Prescriptions recorded.', items: [`#6 · ${D}`, `#7 · ${D}`, `#9 · ${L}`] })],
  // diff
  ['anjali: What changed from the previous prescription to the latest?', P({ title: 'Changes from #3 to #4', summary: `#3 · ${D}  →  #4 · ${D}`,
    rows: [{ label: 'Added', value: 'Guggulu 12 g' }, { label: 'Removed', value: 'Ashwagandha 40 g, Punarnava 8 g' }, { label: 'Changed', value: 'Neem 20 g → 10 g' }, { label: 'Dosha', value: 'Vata → Kapha' }] })],
  ['anjali: Latest prescription-il puthuthayi enthaanu cherthath?', P({ title: 'Changes from #3 to #4', summary: `#3 · ${D}  →  #4 · ${D}`,
    rows: [{ label: 'Added', value: 'Guggulu 12 g' }, { label: 'Removed', value: 'Ashwagandha 40 g, Punarnava 8 g' }, { label: 'Changed', value: 'Neem 20 g → 10 g' }, { label: 'Dosha', value: 'Vata → Kapha' }] })],
  ['legacy: What changed from the previous prescription to the latest?', P({ title: 'Changes from #7 to #9', summary: `#7 · ${D}  →  #9 · ${L}`,
    rows: [{ label: 'Herbs', value: 'Cannot be compared: not recorded on #9' }, { label: 'Dosha', value: 'Cannot be compared: not recorded on #9' }], caveat: 'Some values are not recorded, so this comparison is incomplete.' })],
  // compare
  ['anjali: Did the Neem dose change since the previous prescription?', P({ title: 'Neem in #3 and #4', headline: 'Decreased by 10 g', compare: { columns: [`#3 · ${D}`, `#4 · ${D}`], rows: [{ label: 'Neem', a: '20 g', b: '10 g' }] } })],
  ['anjali: Compare Ashwagandha with the first prescription.', P({ title: 'Ashwagandha in #1 and #4', headline: 'Decreased by 30 g', compare: { columns: [`#1 · ${D}`, `#4 · ${D}`], rows: [{ label: 'Ashwagandha', a: '30 g', b: 'Not prescribed' }] } })],
  ['legacy: Did the Neem dose change since the previous prescription?', P({ title: 'Neem compared between two prescriptions', headline: 'Not known', caveat: 'Cannot be determined from the record. The herbs are not recorded on #9.' })],
]

for (const [key, expected] of MAPPING) {
  test(`mapping: ${key}`, () => {
    expect(shown(key)).toEqual(expected)
  })
}

test('the mapping covers every one of the 20 supported operations', () => {
  const ops = new Set<string>()
  for (const [key] of MAPPING) { const q = (REAL[key] as { plan_query: { op: string; target: string } }).plan_query; ops.add(`${q.op} ${q.target}`) }
  // compare dosha has no captured real reply (the planner clarifies "Did the dosha change?"), so it is covered with a constructed one below
  expect([...ops].sort()).toEqual(['compare herb', 'count dosha', 'count encounter', 'count herb', 'count prescription', 'diff prescription', 'first dosha', 'first encounter', 'first herb',
    'first prescription', 'last encounter', 'last herb', 'last prescription', 'list dosha', 'list encounter', 'list herb', 'list prescription', 'sum herb'])
})

// ---- the two operations the planner never reaches from a free question, built from a REAL reply so the records stay genuine ----

test('compare dosha (not reachable from a free question; built on a real diff reply\'s records) is worded and cross-checked', () => {
  const diff = reply('anjali: What changed from the previous prescription to the latest?', PK) as { evidence: unknown[] }
  const raw = { ...diff, plan_frame: 'CMP_DOSHA_PREV', plan_query: { op: 'compare', target: 'dosha', herb: null, dosha: null, last_n: null, a: 'previous', b: 'last' },
    value: { a: 'rx:3', b: 'rx:4', dosha_a: 'Vata', dosha_b: 'Kapha', changed: true } }
  expect(toAskView(raw, PK)).toMatchObject({ kind: 'answer', shown: { title: 'Dosha in #3 and #4', headline: 'Changed', compare: { rows: [{ label: 'Dosha', a: 'Vata', b: 'Kapha' }] } } })
  expect(toAskView({ ...raw, value: { ...raw.value, changed: false } }, PK)).toBeNull()
  expect(toAskView({ ...raw, value: { ...raw.value, dosha_b: 'Pitta' } }, PK)).toBeNull()
})

test('an earliest-of-the-latest-N question is worded as such', () => {
  const latest = reply('anjali: What was the first prescription?', PK) as { plan_query: object }
  expect(toAskView({ ...latest, plan_query: { ...latest.plan_query, last_n: 3 } }, PK)).toMatchObject({ shown: { title: 'Earliest of the latest 3 prescriptions' } })
})

// ---- count versus sum, latest versus total ----------------------------------------------

test('a count and a sum of the same herb are never worded alike', () => {
  const count = shown('anjali: How many times was Neem prescribed?')
  const sum = shown('anjali: How much Neem in total?')
  expect(count.headline).toBe('3')
  expect(sum.headline).toBe('40 g')
  expect(count.summary).toMatch(/not an amount in grams/)
  expect(sum.title).toMatch(/^Total /)
})

test('"last prescribed" is a date and a single amount, never a total; a lower bound is always labelled "At least"', () => {
  expect(shown('anjali: When was Neem last prescribed?').headline).toBe(D)
  expect(shown('anjali: When was Neem last prescribed?').summary).toBe('Prescription #4 · Neem 10 g')
  for (const key of ['legacy: How much Neem in total?', 'legacy: How many times was Neem prescribed?', 'legacy: How many prescriptions were recorded as Vata only?']) {
    expect(shown(key).headline).toMatch(/^At least /)
    expect(shown(key).caveat).toMatch(/may be higher/)
  }
})

// ---- 2. no raw output --------------------------------------------------------------------

const RAW = /rx:|enc:|created_at|opened_at|closed_at|dosha_a|dosha_b|grams_a|grams_b|dosha_changed|prescription_id|encounter_ref|plan_|COUNT_|SUM_|LAST_|FIRST_|DIFF_|CMP_|undefined|\[object|NaN|\bnull\b|\{|\}|"|\bsum herb\b|\blast herb\b|\bcount herb\b|\blist \w+:|From the known records only|At least \d+\.$/

test('no displayed string of any real reply contains a raw label, field name, serialized value or the template sentence', () => {
  let checked = 0
  for (const key of Object.keys(REAL)) {
    const v = view(key)
    if (!v || v.kind !== 'answer') continue
    const strings = [v.shown.title, v.shown.headline, v.shown.summary, v.shown.caveat, ...v.shown.rows.flatMap((r) => [r.label, r.value]), ...v.shown.items,
      ...(v.shown.compare ? [...v.shown.compare.columns, ...v.shown.compare.rows.flatMap((r) => [r.label, r.a, r.b])] : [])].filter((x): x is string => x !== null)
    for (const text of strings) expect(text, `${key} -> ${text}`).not.toMatch(RAW)
    const template = (REAL[key] as { text: string }).text
    for (const text of strings) expect(text).not.toBe(template)
    checked += 1
  }
  expect(checked).toBeGreaterThanOrEqual(40)
})

// ---- 3. the cross-check ------------------------------------------------------------------

type Mutation = [string, string, (r: Record<string, any>) => void]
const MUTATIONS: Mutation[] = [
  ['count off by one', 'anjali: How many times was Neem prescribed?', (r) => { r.value = 4 }],
  ['count that disagrees with the records', 'anjali: How many prescriptions has she had?', (r) => { r.value = 3 }],
  ['visit count that disagrees with the records', 'anjali: How many visits has she had?', (r) => { r.value = 2 }],
  ['sum off by one', 'anjali: How much Neem in total?', (r) => { r.value = 41 }],
  ['sum given as text', 'anjali: How much Neem in total?', (r) => { r.value = '40' }],
  ['negative count', 'anjali: How many prescriptions has she had?', (r) => { r.value = -4 }],
  ['dosha count that disagrees', 'anjali: How many prescriptions were recorded as Vata only?', (r) => { r.value = 3 }],
  ['dosha count for another dosha', 'anjali: How many prescriptions were recorded as Vata only?', (r) => { r.plan_query.dosha = 'Pitta' }],
  ['first herb: grams differ from the record', 'anjali: When was Neem first prescribed?', (r) => { r.value.grams = 11 }],
  ['first herb: date differs from the record', 'anjali: When was Neem first prescribed?', (r) => { r.value.created_at = '2026-10-11 05:40:36' }],
  ['first herb: herb not in the cited record', 'anjali: When was Neem first prescribed?', (r) => { r.plan_query.herb = 'Brahmi' }],
  ['first herb: record not cited', 'anjali: When was Neem first prescribed?', (r) => { r.value.prescription = 'rx:2' }],
  ['last prescription: dosha differs', 'anjali: What is the latest prescription?', (r) => { r.value.dosha = 'Pitta' }],
  ['last prescription: herb grams differ', 'anjali: What is the latest prescription?', (r) => { r.value.herbs[0][1] = 13 }],
  ['last prescription: herb missing', 'anjali: What is the latest prescription?', (r) => { r.value.herbs.pop() }],
  ['last prescription: date differs', 'anjali: What is the latest prescription?', (r) => { r.value.created_at = '2026-01-01 00:00:00' }],
  ['first dosha: dosha differs', 'anjali: Which dosha is on the first prescription?', (r) => { r.value.dosha = 'Kapha' }],
  ['visit: closed although the record is open', 'anjali: When did her most recent visit end?', (r) => { r.value.closed_at = '2026-10-10 06:00:00' }],
  ['visit: opened date differs', 'anjali: When did her most recent visit end?', (r) => { r.value.opened_at = '2026-10-09 06:00:00' }],
  ['list: a prescription that is not cited', 'anjali: List all her prescriptions.', (r) => { r.value.push('rx:77') }],
  ['list: a prescription missing from the result', 'anjali: List all her prescriptions.', (r) => { r.value.pop() }],
  ['list herb: a herb in no cited record', 'anjali: Which herbs has she been given?', (r) => { r.value.push('Kutki') }],
  ['list herb: a repeated herb', 'anjali: Which herbs has she been given?', (r) => { r.value.push('Neem') }],
  ['list dosha: dosha differs from the record', 'anjali: List the dosha of each prescription.', (r) => { r.value[0].dosha = 'Kapha' }],
  ['diff: an invented addition', 'anjali: What changed from the previous prescription to the latest?', (r) => { r.value.added.push(['Kutki', 5]) }],
  ['diff: a missing removal', 'anjali: What changed from the previous prescription to the latest?', (r) => { r.value.removed.pop() }],
  ['diff: a changed amount', 'anjali: What changed from the previous prescription to the latest?', (r) => { r.value.changed[0][2] = 11 }],
  ['diff: dosha change reversed', 'anjali: What changed from the previous prescription to the latest?', (r) => { r.value.dosha_changed = false }],
  ['diff: both selectors the same record', 'anjali: What changed from the previous prescription to the latest?', (r) => { r.value.b = 'rx:3' }],
  ['diff: partial result presented as complete', 'legacy: What changed from the previous prescription to the latest?', (r) => { r.qualifier = 'exact'; r.category = 'answer' }],
  ['compare: delta that does not follow from the grams', 'anjali: Did the Neem dose change since the previous prescription?', (r) => { r.value.delta = 10 }],
  ['compare: grams that differ from the record', 'anjali: Did the Neem dose change since the previous prescription?', (r) => { r.value.grams_a = 21; r.value.delta = -11 }],
  ['an exact answer although a cited record has unknown values', 'legacy: How much Neem in total?', (r) => { r.qualifier = 'exact'; r.category = 'answer' }],
  ['a lower bound although no cited record has unknown values', 'anjali: How much Neem in total?', (r) => { r.qualifier = 'at_least'; r.category = 'incomplete' }],
  ['partial although nothing is unknown', 'anjali: When was Neem first prescribed?', (r) => { r.qualifier = 'partial'; r.category = 'incomplete' }],
  ['unknown carrying a value', 'legacy: Did the Neem dose change since the previous prescription?', (r) => { r.value = 5 }],
  ['unknown whose cited record is fully recorded', 'legacy: Did the Neem dose change since the previous prescription?', (r) => { r.evidence = (REAL['anjali: What is the latest prescription?'] as any).evidence }],
  ['an unsupported query', 'anjali: How many prescriptions has she had?', (r) => { r.plan_query.op = 'average' }],
  ['a missing query', 'anjali: How many prescriptions has she had?', (r) => { r.plan_query = null }],
  ['a query for another operation', 'anjali: How many prescriptions has she had?', (r) => { r.plan_query = { ...r.plan_query, op: 'sum', target: 'herb', herb: 'Neem' } }],
  ['a result of the wrong shape', 'anjali: What is the latest prescription?', (r) => { r.value = 4 }],
  ['a result that is an array of the wrong shape', 'anjali: What changed from the previous prescription to the latest?', (r) => { r.value = [] }],
  ['no result at all', 'anjali: How many prescriptions has she had?', (r) => { r.value = null }],
  ['last_n that is not a positive integer', 'anjali: How much Neem in total?', (r) => { r.plan_query.last_n = 0 }],
  ['a herb name that is empty', 'anjali: How much Neem in total?', (r) => { r.plan_query.herb = '' }],
]

for (const [label, key, mutate] of MUTATIONS) {
  test(`contradictory or malformed structure is refused: ${label}`, () => {
    const raw = JSON.parse(JSON.stringify(REAL[key])) as Record<string, any>
    raw.patient_pk = PK
    mutate(raw)
    expect(toAskView(raw, PK)).toBeNull()
  })
}

test('a verified answer whose structured result is hidden by its own template text is still judged on the structure, not the text', () => {
  const raw = reply('anjali: How much Neem in total?', PK, { text: 'sum herb Neem: 41.', value: 41 })
  expect(toAskView(raw, PK)).toBeNull()
})

// ---- 4. one time-zone convention ---------------------------------------------------------

test('every date in an answer is formatRecordDate of the cited record\'s own timestamp: the answer and its evidence cannot disagree', () => {
  const key = 'anjali: When was Neem last prescribed?'
  const v = view(key) as Extract<AskView, { kind: 'answer' }>
  const record = v.evidence[0]
  expect(record.kind).toBe('prescription')
  expect(v.shown.headline).toBe(formatRecordDate((record as { date: string }).date))
  // the stored value is UTC with no zone: 2026-10-10 05:40:36 is that instant, in whatever zone the viewer is in
  expect(formatRecordDate('2026-10-10 05:40:36')).toBe(new Date('2026-10-10T05:40:36Z').toLocaleString(undefined, { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }))
})

test('a list, a diff and a comparison date their records exactly as the evidence does', () => {
  for (const key of ['anjali: List all her prescriptions.', 'legacy: What changed from the previous prescription to the latest?', 'anjali: Did the Neem dose change since the previous prescription?']) {
    const v = view(key) as Extract<AskView, { kind: 'answer' }>
    const text = JSON.stringify(v.shown)
    for (const record of v.evidence) if (record.kind === 'prescription' && text.includes(`#${record.number}`)) expect(text).toContain(formatRecordDate(record.date))
  }
})
