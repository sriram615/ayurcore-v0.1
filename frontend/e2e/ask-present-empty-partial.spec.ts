/**
 * Pure-function tests (no browser) for the one legitimate EMPTY list: a list question about a patient whose prescriptions ALL have unknown
 * herbs (or unknown dosha). The backend then answers outcome "answer", category "incomplete", qualifier "partial" and value [], citing the
 * unknown prescriptions (ai2/executor.py "list herb" / "list dosha"). lib/ask-present.ts must show that as an incomplete answer that does not
 * claim nothing was prescribed -- and must keep refusing every empty result the contract does not justify.
 *
 * Replies are REAL backend replies for a synthetic all-unknown patient (e2e/fixtures/ask-empty-partial.ts), mutated one field at a time;
 * the existing mixed / exact / legacy replies come from e2e/fixtures/ask-real.ts. Rendering in the browser is NOT covered here.
 */
import { expect, test } from '@playwright/test'

import { toAskView, type AskView } from '../lib/ask-view'
import { formatRecordDate } from '../lib/format'
import type { Presented } from '../lib/ask-present'
import { reply } from './fixtures/ask-real'
import { emptyReply, REAL_EMPTY } from './fixtures/ask-empty-partial'

const PK = 5
const HERBS = 'allunknown: List all the herbs prescribed so far'
const HERBS_2 = 'allunknown: Which herbs have been prescribed?'
const DOSHAS = 'allunknown: List the dosha of each prescription'
const DOSHAS_2 = 'allunknown: What were the doshas recorded on each prescription?'
const D = formatRecordDate('2026-10-10 05:40:36')

type Raw = Record<string, any>
const copy = (key: string, mutate?: (r: Raw) => void): Raw => {
  const raw = emptyReply(key, PK) as Raw
  mutate?.(raw)
  return raw
}
const view = (raw: unknown, pk = PK) => toAskView(raw, pk)
const answerOf = (raw: unknown): Extract<AskView, { kind: 'answer' }> => {
  const v = view(raw)
  if (!v || v.kind !== 'answer') throw new Error('not an answer')
  return v
}
const P = (p: Partial<Presented> & { title: string }): Presented => ({ headline: null, summary: null, rows: [], items: [], compare: null, caveat: null, ...p })
/** The real, fully known prescription #4 (Kapha, Guggulu + Neem) from the existing fixtures, re-labelled so it can be cited next to #101..#103. */
const knownRx = (id: number) => ({ ...(reply('anjali: What is the latest prescription?', PK).evidence as Raw[])[0], prescription_id: id, label: `rx:${id}` })
const encounter = () => (reply('anjali: When did her most recent visit end?', PK).evidence as Raw[])[0]

// ---- 1 & 2. the reproduced defect, now an incomplete answer -------------------------------------

const EXPECT_HERBS = P({
  title: 'Herbs prescribed', headline: 'Not known', summary: 'No herbs can be listed: none are recorded on the cited prescriptions.',
  caveat: '3 prescriptions (#101, #102, #103) have herbs not recorded. This is not a record that no herbs were prescribed.',
})
const EXPECT_DOSHAS = P({
  title: 'Dosha by prescription, oldest first', headline: 'Not known', summary: 'No dosha can be listed: none is recorded on the cited prescriptions.',
  caveat: '3 prescriptions (#101, #102, #103) have no dosha recorded. This is not a record that no dosha applied.',
})

for (const key of [HERBS, HERBS_2]) {
  test(`all-unknown herb list is an incomplete answer, not a refusal: ${key}`, () => {
    const v = answerOf(copy(key))
    expect(v.incomplete).toBe(true)
    expect(v.shown).toEqual(EXPECT_HERBS)
    expect(v.evidenceTotal).toBe(3)
    expect(v.evidence.map((e) => (e.kind === 'prescription' ? e.number : -1))).toEqual([101, 102, 103])
    expect(v.clarifiedAs).toBeNull()
  })
}

for (const key of [DOSHAS, DOSHAS_2]) {
  test(`all-unknown dosha list is an incomplete answer, not a refusal: ${key}`, () => {
    const v = answerOf(copy(key))
    expect(v.incomplete).toBe(true)
    expect(v.shown).toEqual(EXPECT_DOSHAS)
    expect(v.evidenceTotal).toBe(3)
  })
}

test('the cited records are shown as unknown, never filled in', () => {
  for (const key of [HERBS, DOSHAS]) {
    for (const record of answerOf(copy(key)).evidence) expect(record).toMatchObject({ kind: 'prescription', dosha: null, herbs: null })
  }
})

test('a single unknown prescription uses the singular wording', () => {
  const one = (key: string) => copy(key, (r) => { r.evidence = [r.evidence[0]] })
  expect(answerOf(one(HERBS)).shown.caveat).toBe('1 prescription (#101) has herbs not recorded. This is not a record that no herbs were prescribed.')
  expect(answerOf(one(DOSHAS)).shown.caveat).toBe('1 prescription (#101) has no dosha recorded. This is not a record that no dosha applied.')
})

test('the scope of a "latest N" question is kept in the title', () => {
  expect(answerOf(copy(HERBS, (r) => { r.plan_query.last_n = 3 })).shown.title).toBe('Herbs prescribed in the latest 3')
  expect(answerOf(copy(DOSHAS, (r) => { r.plan_query.last_n = 3 })).shown.title).toBe('Dosha by prescription, oldest first in the latest 3')
})

test('a cited record whose OTHER field is known is still fine: only the field the list is about must be unknown', () => {
  const herbsListWithKnownDosha = copy(HERBS, (r) => { r.evidence[0].dosha = 'Vata' })
  expect(answerOf(herbsListWithKnownDosha).shown).toEqual(EXPECT_HERBS)
  const doshaListWithKnownHerbs = copy(DOSHAS, (r) => { r.evidence[0].herbs = [{ name: 'Neem', grams: 5 }] })
  expect(answerOf(doshaListWithKnownHerbs).shown).toEqual(EXPECT_DOSHAS)
})

// ---- 3 & 4. existing behaviour is unchanged -------------------------------------------------------

const EXISTING: Array<[string, Presented]> = [
  ['legacy: Which herbs has she been given?', P({ title: 'Herbs prescribed', headline: '2', summary: 'Each herb once, in the order it first appears.', items: ['Neem', 'Brahmi'], caveat: '1 prescription (#9) has herbs not recorded; any herbs in it are not listed.' })],
  ['legacy: List the dosha of each prescription.', P({ title: 'Dosha by prescription, oldest first', headline: '2', summary: 'Prescriptions with a recorded dosha.', items: [`#6 · ${D} — Vata`, `#7 · ${D} — Kapha`], caveat: '1 prescription (#9) has no dosha recorded and is not listed.' })],
  ['anjali: Which herbs has she been given?', P({ title: 'Herbs prescribed', headline: '5', summary: 'Each herb once, in the order it first appears.', items: ['Ashwagandha', 'Neem', 'Brahmi', 'Punarnava', 'Guggulu'] })],
  ['anjali: List the dosha of each prescription.', P({ title: 'Dosha by prescription, oldest first', headline: '4', summary: 'Prescriptions with a recorded dosha.', items: [`#1 · ${D} — Vata`, `#2 · ${D} — Pitta`, `#3 · ${D} — Vata`, `#4 · ${D} — Kapha`] })],
  ['anjali: What herbs appear across her three most recent prescriptions?', P({ title: 'Herbs prescribed in the latest 3', headline: '5', summary: 'Each herb once, in the order it first appears.', items: ['Ashwagandha', 'Brahmi', 'Neem', 'Punarnava', 'Guggulu'] })],
]
for (const [key, expected] of EXISTING) {
  test(`unchanged: mixed and exact lists keep their known entries, order, scope and caveat: ${key}`, () => {
    const v = answerOf(reply(key, PK))
    expect(v.shown).toEqual(expected)
    expect(v.shown.items.length).toBeGreaterThan(0)          // the non-empty path never takes the empty-list branch
  })
}

// ---- 5. empty results the contract does not justify fail closed ------------------------------------

type Mutation = [string, string[], (r: Raw) => void]
const BOTH = [HERBS, DOSHAS]
const MUTATIONS: Mutation[] = [
  ['an EXACT empty list', BOTH, (r) => { r.qualifier = 'exact'; r.category = 'answer' }],
  ['an at_least empty list', BOTH, (r) => { r.qualifier = 'at_least' }],
  ['an empty list claimed unknown (unknown carries no value)', BOTH, (r) => { r.qualifier = 'unknown' }],
  ['an empty list whose cited prescription has KNOWN herbs', [HERBS], (r) => { r.evidence[1] = knownRx(7) }],
  ['an empty list whose cited prescription has a KNOWN dosha', [DOSHAS], (r) => { r.evidence[1] = { ...r.evidence[1], dosha: 'Vata' } }],
  ['an empty list citing an encounter next to the unknown prescriptions', BOTH, (r) => { r.evidence.push(encounter()) }],
  ['an empty list citing only an encounter', BOTH, (r) => { r.evidence = [encounter()] }],
  ['an empty list citing no record at all', BOTH, (r) => { r.evidence = [] }],
  ['an empty list with the evidence field missing', BOTH, (r) => { delete r.evidence }],
  ['the same prescription cited twice', BOTH, (r) => { r.evidence[1] = { ...r.evidence[0] } }],
  ['a record cited twice among three', BOTH, (r) => { r.evidence.push({ ...r.evidence[0] }) }],
  ['a herb listed that no cited record contains', [HERBS], (r) => { r.value = ['Neem'] }],
  ['a dosha entry for a prescription that has no recorded dosha', [DOSHAS], (r) => { r.value = [{ prescription: 'rx:101', dosha: 'Vata' }] }],
  ['an empty list on a list of prescriptions', BOTH, (r) => { r.plan_query.target = 'prescription' }],
  ['an empty list on a list of visits', BOTH, (r) => { r.plan_query.target = 'encounter' }],
  ['an empty list on a different operation (diff)', BOTH, (r) => { r.plan_query.op = 'diff'; r.plan_query.target = 'prescription' }],
  ['an empty list on a count', BOTH, (r) => { r.plan_query.op = 'count' }],
  ['a missing plan', BOTH, (r) => { r.plan_query = null }],
  ['a scope that is not a positive integer', BOTH, (r) => { r.plan_query.last_n = 0 }],
  ['a value that is null', BOTH, (r) => { r.value = null }],
  ['a value that is an object', BOTH, (r) => { r.value = {} }],
  ['a value that is a string', BOTH, (r) => { r.value = '' }],
  ['a list holding an empty name', [HERBS], (r) => { r.value = [''] }],
  ['a list holding a non-string', [HERBS], (r) => { r.value = [5] }],
]
for (const [label, keys, mutate] of MUTATIONS) {
  for (const key of keys) {
    test(`fails closed: ${label} (${key.includes('herbs') || key === HERBS_2 ? 'herb' : 'dosha'} list)`, () => {
      expect(view(copy(key, mutate))).toBeNull()
    })
  }
}

// ---- 6. unrelated validation is unchanged: these were rejected before and still are ---------------

const REJECTED: Array<[string, (r: Raw) => void]> = [
  ['an outcome that is not "answer"', (r) => { r.outcome = 'abstain' }],
  ['an outcome that is a clarification', (r) => { r.outcome = 'clarify' }],
  ['an unverified answer', (r) => { r.verified = false }],
  ['an answer with no template text', (r) => { r.text = null }],
  ['a blank template text', (r) => { r.text = '  ' }],
  ['category "answer" with qualifier "partial"', (r) => { r.category = 'answer' }],
  ['category "incomplete" with an unknown qualifier word', (r) => { r.qualifier = 'mostly' }],
  ['a missing qualifier', (r) => { r.qualifier = null }],
  ['a category the contract does not have', (r) => { r.category = 'complete' }],
  ['a reply to a chosen reading that says it was not', (r) => { r.clarified = true }],
  ['a reply about a different patient', (r) => { r.patient_pk = 99 }],
  ['evidence that is not a list', (r) => { r.evidence = 'rx:101' }],
  ['a null record', (r) => { r.evidence[0] = null }],
  ['a record whose label disagrees with its number', (r) => { r.evidence[0].label = 'rx:999' }],
  ['a record of a kind the contract does not have', (r) => { r.evidence[0].kind = 'lab_result' }],
  ['a record of the backend\'s "unknown" kind', (r) => { r.evidence[0] = { kind: 'unknown', label: 'rx:101' } }],
  ['a record with no date', (r) => { r.evidence[0].created_at = null }],
  ['a record with no status', (r) => { r.evidence[0].status = '' }],
  ['a record with a negative number', (r) => { r.evidence[0].prescription_id = -1; r.evidence[0].label = 'rx:-1' }],
  ['one good record and one malformed record', (r) => { r.evidence[2].status = null }],
  ['herbs that are not a list', (r) => { r.evidence[0].herbs = 'Neem' }],
]
for (const [label, mutate] of REJECTED) {
  for (const key of BOTH) {
    test(`still rejected: ${label} (${key === HERBS ? 'herb' : 'dosha'} list)`, () => {
      expect(view(copy(key, mutate))).toBeNull()
    })
  }
}

test('a refusal or clarification can never carry the empty list or its records', () => {
  const base = { patient_pk: PK, stage: 'screen', reason: 'X', text: null, value: [], qualifier: null, verified: false, options: [], plan_frame: null, plan_query: null, clarified: false }
  const records = (REAL_EMPTY[HERBS] as Raw).evidence
  for (const category of ['missing', 'unsupported', 'unsafe', 'access_denied']) {
    expect(view({ ...base, outcome: 'abstain', category, evidence: records })).toBeNull()        // a refusal that cites records
    expect(view({ ...base, outcome: 'abstain', category, evidence: [] })).toMatchObject({ kind: 'notice', category })   // the ordinary refusal, as before
  }
  expect(view({ ...base, outcome: 'clarify', category: 'clarify', options: [], evidence: records })).toBeNull()
})

// ---- 7. an empty list is never presented as evidence of absence ------------------------------------

const everyString = (s: Presented) => [s.title, s.headline, s.summary, s.caveat, ...s.rows.flatMap((r) => [r.label, r.value]), ...s.items].filter((x): x is string => x !== null)

for (const key of [HERBS, DOSHAS]) {
  test(`the empty ${key === HERBS ? 'herb' : 'dosha'} list is not presented as a count, a "none", or evidence that nothing was recorded or prescribed`, () => {
    const v = answerOf(copy(key))
    expect(v.incomplete).toBe(true)
    expect(v.shown.items).toEqual([])
    expect(v.shown.rows).toEqual([])
    expect(v.shown.headline).toBe('Not known')
    expect(v.shown.headline).not.toMatch(/\d|none|no /i)
    expect(v.shown.caveat).toContain(`This is not a record that no ${key === HERBS ? 'herbs were prescribed' : 'dosha applied'}`)
    // nothing says the patient had zero herbs / doshas, and no raw backend wording leaks
    const RAW = /rx:|created_at|From the known records only|list herb|list dosha|: none\.|undefined|\[object|NaN|\bnull\b|\{|\}|"/
    for (const text of everyString(v.shown)) {
      expect(text).not.toMatch(RAW)
      // no sentence may ASSERT absence at patient level ("no herbs were prescribed", "nothing was prescribed"); the one sentence that NEGATES that
      // claim is required above and is removed before this check. "N prescriptions have no dosha recorded" is a true statement about records.
      const asserted = text.replace(/This is not a record that [^.]*\./, '')
      expect(asserted).not.toMatch(/\b(no|zero|0)\s+(herbs?|dosha)\w*\s+(were|was|have been|has been|had|applied)\b/i)
      expect(asserted).not.toMatch(/\b(nothing|none)\s+(was|were|has been|have been)\s+(prescribed|given|recorded)\b/i)
    }
    expect(v.shown.summary).toMatch(/cannot be listed|can be listed: none are recorded|can be listed: none is recorded/)
  })
}

test('the backend\'s template sentence for the empty list is required but never shown', () => {
  for (const key of [HERBS, DOSHAS]) {
    const raw = copy(key)
    const v = answerOf(raw)
    expect(JSON.stringify(v)).not.toContain(raw.text)
    expect(view({ ...raw, text: null })).toBeNull()
  }
})

test('the new fixtures contain only synthetic identifiers and enum words', () => {
  const strings = new Set<string>()
  const walk = (x: unknown) => {
    if (Array.isArray(x)) x.forEach(walk)
    else if (x && typeof x === 'object') Object.values(x).forEach(walk)
    else if (typeof x === 'string') strings.add(x)
  }
  walk(REAL_EMPTY)
  for (const s of strings) expect(s).toMatch(/^(rx:\d+|\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}|[A-Z_]+|[a-z_]+|From the known records only: list (herb|dosha): none\.)$/)
})
