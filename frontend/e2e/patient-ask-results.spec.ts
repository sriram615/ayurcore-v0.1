/**
 * Ask about this patient -- how each kind of AI-2 reply is rendered, against a mocked API.
 *
 * Answers, incomplete answers and clarifications are REAL backend replies (e2e/fixtures/ask-real.ts); only refusals and malformed replies
 * are built here. The panel words an answer from its structured result (lib/ask-present.ts), shows fixed wording per refusal category, and
 * never shows a reason code, a stage, the backend's template sentence, an internal label, or anything from a reply that does not match the
 * contract. What each operation says is tested in ask-present.spec.ts; choosing a reading and the evidence cards in patient-ask-clarify.spec.ts.
 */
import { expect, test, type Page, type Route } from '@playwright/test'

import { API_ORIGIN } from '../playwright.config'
import { reply as real } from './fixtures/ask-real'

type Call = { method: string; path: string; params: URLSearchParams; auth?: string; body: unknown }
type Reply = { status?: number; body?: unknown } | 'network-error' | undefined
type Handler = (call: Call) => Reply | Promise<Reply>

const TOKEN = 'test-session-token'
const CLINIC = 'fixture_clinic_do_not_display'
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
}
const ANJALI = { patient_pk: 5, clinic_patient_id: 'OP-1001', display_name: 'Anjali Nair' }
const ANJANA = { patient_pk: 6, clinic_patient_id: 'OP-1002', display_name: 'Anjana Rao' }
const RX = { prescription_id: 12, created_at: '2026-09-20 10:15:00', status: 'Active', dosha_imbalance: 'Vata', prescribed_herbs: [{ name: 'Ashwagandha', grams: 7 }] }

const base = (pk = 5) => ({
  patient_pk: pk, stage: 'done', reason: null, text: null, value: null, qualifier: null, verified: false, options: [], evidence: [], plan_frame: null, plan_query: null, clarified: false,
})
const LATEST = 'anjali: What is the latest prescription?'
const COUNT = 'anjali: How many prescriptions has she had?'
const INCOMPLETE = { at_least: 'legacy: How much Neem in total?', partial: 'legacy: What is the latest prescription?', unknown: 'legacy: Did the Neem dose change since the previous prescription?' } as const
const answer = (pk = 5, o: object = {}) => real(LATEST, pk, o)
const incomplete = (qualifier: keyof typeof INCOMPLETE) => real(INCOMPLETE[qualifier], 5)
const refusal = (category: string, reason: string, stage = 'screen', o: object = {}) => ({ ...base(), outcome: 'abstain', category, reason, stage, ...o })
const reading = (frame: string, text: string) => ({ frame, text, query: { op: 'count', target: 'herb', herb: 'Neem', dosha: null, last_n: null, a: 'previous', b: 'last' } })
const clarify = (options: unknown[]) => ({ ...base(), outcome: 'clarify', category: 'clarify', stage: 'clarify', reason: 'UNDERSPECIFIED', options })

async function mockApi(page: Page, handler: Handler): Promise<Call[]> {
  const calls: Call[] = []
  await page.route(
    (url) => url.origin !== new URL(API_ORIGIN).origin && !['127.0.0.1', 'localhost'].includes(url.hostname),
    (route) => route.abort('blockedbyclient')
  )
  await page.route(`${API_ORIGIN}/**`, async (route: Route) => {
    const req = route.request()
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    const url = new URL(req.url())
    let body: unknown = null
    try { body = req.postDataJSON() } catch { body = req.postData() }
    const call: Call = { method: req.method(), path: url.pathname, params: url.searchParams, auth: req.headers()['authorization'], body }
    calls.push(call)
    if (url.pathname === '/auth/login') {
      return route.fulfill({ status: 200, headers: CORS, json: { token: TOKEN, expires_at: '2099-01-01T00:00:00Z', username: 'test_user', clinic_id: CLINIC, role: 'DOCTOR' } })
    }
    if (url.pathname === '/auth/logout') return route.fulfill({ status: 200, headers: CORS, json: { status: 'logged_out' } })
    if (url.pathname === '/patients') return route.fulfill({ status: 200, headers: CORS, json: { results: [ANJALI, ANJANA], truncated: false } })
    if (url.pathname === '/patients/5/prescriptions') return route.fulfill({ status: 200, headers: CORS, json: { patient: ANJALI, prescriptions: [RX], limit: 20, offset: 0, has_more: false } })
    if (url.pathname === '/patients/6/prescriptions') return route.fulfill({ status: 200, headers: CORS, json: { patient: ANJANA, prescriptions: [], limit: 20, offset: 0, has_more: false } })
    const reply = await handler(call)
    if (reply === 'network-error') return route.abort('failed')
    if (!reply) return route.fulfill({ status: 599, headers: CORS, json: { detail: `unexpected request in test: ${call.method} ${url.pathname}` } })
    return route.fulfill({ status: reply.status ?? 200, headers: CORS, json: reply.body })
  })
  return calls
}

async function signIn(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: /Enter AyurCore/ }).click()
  await page.getByRole('textbox', { name: 'Username' }).fill('test_user')
  await page.getByRole('textbox', { name: 'Password' }).fill('not-a-real-password')
  await page.locator('form').getByRole('button', { name: /Sign in/ }).click()
  await expect(page.locator('.app-shell')).toBeVisible()
}

async function openPatient(page: Page, name: RegExp = /Anjali Nair/) {
  const menu = page.getByRole('button', { name: 'Open navigation' })
  if (await menu.isVisible()) await menu.click()
  await page.locator('nav.side-nav').getByRole('button', { name: 'Patients' }).click()
  await page.getByRole('searchbox', { name: /Search patients/ }).fill('an')
  await page.getByRole('button', { name }).click()
  await expect(page.locator('.patient-record')).toBeVisible()
}

const ask = (page: Page) => page.getByRole('region', { name: 'ASK ABOUT THIS PATIENT' })
const box = (page: Page) => page.getByRole('textbox', { name: /Your question about/ })
const askButton = (page: Page) => ask(page).getByRole('button', { name: /^(Ask|Asking…)$/ })
const clearButton = (page: Page) => ask(page).getByRole('button', { name: 'Clear', exact: true })
const result = (page: Page) => ask(page).locator('.ask-result')
const askCalls = (calls: Call[]) => calls.filter((c) => c.path.endsWith('/ask'))

async function askWith(page: Page, reply: unknown, question = 'Neem count?') {
  const calls = await mockApi(page, () => ({ body: reply }))
  await signIn(page)
  await openPatient(page)
  await box(page).fill(question)
  await askButton(page).click()
  return calls
}

const NEVER_SHOWN = /RAW-REASON|raw-stage|rx:|enc:|created_at|opened_at|dosha_a|grams_a|plan_|LAST_RX|COUNT_HERB|UNDERSPECIFIED|TREATMENT|OTHER_PATIENT|NOT_RECORDED|AMBIGUOUS|QUALIFIER-X|undefined|\[object|prescription rx/

// --- answer + incomplete --------------------------------------------------------------

test('an answer is worded from its structured result, with the verified mark, and no value, label, stage, reason or plan', async ({ page }) => {
  await askWith(page, answer(5, { reason: 'RAW-REASON', stage: 'raw-stage' }))
  await expect(result(page)).toBeVisible()
  await expect(result(page).getByRole('heading', { name: 'Latest prescription' })).toBeVisible()
  await expect(result(page).locator('.ask-answer-headline')).toHaveText('Prescription #4')
  await expect(result(page).getByText('Verified against the record')).toBeVisible()
  await expect(result(page).getByText('Incomplete')).toHaveCount(0)
  await expect(ask(page)).not.toContainText(NEVER_SHOWN)
})

test('the backend template sentence is never displayed, whatever it says', async ({ page }) => {
  await askWith(page, answer(5, { text: 'Total: 7 g <b>bold</b> rx:4 {"a":1} created_at' }))
  await expect(result(page).locator('.ask-answer-headline')).toHaveText('Prescription #4')
  await expect(page.locator('body')).not.toContainText('Total: 7 g')
  await expect(result(page).locator('b')).toHaveCount(0)
})

for (const [qualifier, headline, caveat] of [
  ['at_least', 'At least 16 g', /has herbs not recorded, so the true total may be higher/],
  ['partial', 'Prescription #9', /Not recorded on this prescription: dosha and herbs/],
  ['unknown', 'Not known', /Cannot be determined from the record/],
] as const) {
  test(`an incomplete answer (${qualifier}) is marked incomplete and states what is missing beside the result`, async ({ page }) => {
    await askWith(page, incomplete(qualifier))
    await expect(result(page).locator('.ask-answer-headline')).toHaveText(headline)
    await expect(result(page).getByText('Incomplete')).toBeVisible()
    await expect(result(page).locator('.ask-caveat')).toContainText(caveat)
    await expect(result(page).getByText('Verified against the record')).toHaveCount(0)
    await expect(result(page)).toHaveClass(/ask-answer-incomplete/)
    await expect(ask(page)).not.toContainText(NEVER_SHOWN)
  })
}

// --- refusals -------------------------------------------------------------------------

const REFUSALS = [
  ['missing', 'NOT_RECORDED', 'Nothing is recorded for that.'],
  ['unsupported', 'AMBIGUOUS', 'That can’t be answered from the record.'],
  ['unsafe', 'TREATMENT', 'This looks like a question about treatment, advice or diagnosis.'],
  ['access_denied', 'OTHER_PATIENT', 'Questions are limited to this patient’s own record.'],
] as const

for (const [category, reason, title] of REFUSALS) {
  test(`${category}: fixed wording, no reason code, no backend text, no answer styling`, async ({ page }) => {
    await askWith(page, refusal(category, reason, 'screen', { text: 'RAW-BACKEND-TEXT', value: 'RAW-VALUE' }))
    await expect(result(page)).toBeVisible()
    await expect(result(page)).toContainText(title)
    await expect(result(page)).toHaveAttribute('data-category', category)
    await expect(result(page)).not.toHaveClass(/ask-answer/)
    await expect(result(page).getByText('Verified against the record')).toHaveCount(0)
    await expect(ask(page)).not.toContainText(NEVER_SHOWN)
    await expect(ask(page)).not.toContainText(/RAW-BACKEND-TEXT|RAW-VALUE/)
  })
}

test('each refusal category reads differently, so the doctor can tell why', async ({ page }) => {
  const titles = new Set<string>()
  let reply: unknown = refusal('missing', 'NOT_RECORDED')
  await mockApi(page, () => ({ body: reply }))
  await signIn(page)
  await openPatient(page)
  for (const [category, reason] of REFUSALS) {
    reply = refusal(category, reason)
    await box(page).fill(`question ${category}`)
    await askButton(page).click()
    await expect(result(page)).toHaveAttribute('data-category', category)
    titles.add((await result(page).locator('p').innerText()).trim())
    await clearButton(page).click()
  }
  expect(titles.size).toBe(4)
})

// --- clarification --------------------------------------------------------------------

test('a clarification lists the available readings in order, each as a button, and sends nothing until one is chosen', async ({ page }) => {
  const calls = await askWith(page, clarify([reading('COUNT_HERB', 'The number of prescriptions containing Neem.'), reading('SUM_HERB', 'The total grams of Neem across all prescriptions.')]))
  await expect(result(page)).toContainText('Which of these did you mean?')
  const items = ask(page).getByRole('list', { name: 'Possible readings' }).getByRole('listitem')
  await expect(items).toHaveCount(2)
  await expect(items.nth(0)).toContainText('The number of prescriptions containing Neem.')
  await expect(items.nth(1)).toContainText('The total grams of Neem across all prescriptions.')
  await expect(result(page).getByRole('button')).toHaveCount(2)
  await expect(result(page).getByText('Verified against the record')).toHaveCount(0)
  await expect(ask(page)).not.toContainText(NEVER_SHOWN)                       // no frame, query or reason leaks
  expect(askCalls(calls)).toHaveLength(1)                                      // rendering it sends nothing more
})

test('a clarification with one reading and with three readings both render', async ({ page }) => {
  let reply: unknown = clarify([reading('A', 'Only reading.')])
  await mockApi(page, () => ({ body: reply }))
  await signIn(page)
  await openPatient(page)
  await box(page).fill('first')
  await askButton(page).click()
  await expect(ask(page).getByRole('listitem')).toHaveCount(1)
  reply = clarify([reading('A', 'One.'), reading('B', 'Two.'), reading('C', 'Three.')])
  await askButton(page).click()
  await expect(ask(page).getByRole('listitem')).toHaveCount(3)
})

// --- malformed replies ----------------------------------------------------------------

const UNREADABLE = "That answer couldn't be shown. Try asking again."
const LEAK = 'LEAKED-REPLY-TEXT'
const MALFORMED: Record<string, unknown> = {
  'null body': null,
  'a bare string': 'LEAKED-REPLY-TEXT',
  'an array': [answer()],
  'an empty object': {},
  'another patient': answer(6, { text: LEAK }),
  'patient missing': { ...answer(), patient_pk: undefined, text: LEAK },
  'unknown category': answer(5, { category: 'diagnosis', text: LEAK }),
  'category missing': { ...answer(), category: undefined, text: LEAK },
  'answer not verified': answer(5, { verified: false, text: LEAK }),
  'answer with no text': answer(5, { text: null }),
  'answer with blank text': answer(5, { text: '   ' }),
  'answer with a non-exact qualifier': answer(5, { qualifier: 'at_least', text: LEAK }),
  'answer with no structured result': answer(5, { value: null, text: LEAK }),
  'answer whose result contradicts its records': answer(5, { value: { prescription: 'rx:4', created_at: '2026-10-10 05:40:36', dosha: 'Pitta', herbs: [['Guggulu', 12], ['Neem', 10]] }, text: LEAK }),
  'answer with an unsupported query': answer(5, { plan_query: { op: 'sell', target: 'herb' }, text: LEAK }),
  'incomplete with an exact qualifier': { ...answer(), category: 'incomplete', qualifier: 'exact', text: LEAK },
  'incomplete with an unknown qualifier': { ...answer(), category: 'incomplete', qualifier: 'QUALIFIER-X', text: LEAK },
  'incomplete that cites only complete records': { ...incomplete('partial'), evidence: answer().evidence, text: LEAK },
  'answer category with an abstain outcome': answer(5, { outcome: 'abstain', text: LEAK }),
  'refusal with an answer outcome': refusal('unsafe', 'TREATMENT', 'screen', { outcome: 'answer', text: LEAK }),
  'clarify with no readings': { ...clarify([]), text: LEAK },
  'clarify with four readings': clarify([reading('A', LEAK), reading('B', LEAK), reading('C', LEAK), reading('D', LEAK)]),
  'clarify reading without text': clarify([{ frame: 'A', query: {} }]),
  'clarify reading with blank text': clarify([reading('A', '  ')]),
  'clarify with an answer outcome': { ...clarify([reading('A', LEAK)]), outcome: 'answer' },
}

for (const [label, reply] of Object.entries(MALFORMED)) {
  test(`a malformed reply (${label}) shows one fixed message and nothing from the reply`, async ({ page }) => {
    await askWith(page, reply)
    await expect(ask(page).getByRole('alert')).toHaveText(UNREADABLE)
    await expect(result(page)).toHaveCount(0)
    await expect(page.locator('body')).not.toContainText(LEAK)
    await expect(askButton(page)).toBeEnabled()                                // and the doctor can simply ask again
  })
}

test('an HTTP error and a malformed 200 are told apart, and a good reply afterwards replaces the message', async ({ page }) => {
  let mode: 'http' | 'bad' | 'good' = 'http'
  await mockApi(page, () => (mode === 'http' ? { status: 500, body: { detail: 'Traceback: boom' } } : mode === 'bad' ? { body: { nonsense: true } } : { body: answer() }))
  await signIn(page)
  await openPatient(page)
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await expect(ask(page).getByRole('alert')).toHaveText("That question couldn't be answered right now. Try again.")
  mode = 'bad'
  await askButton(page).click()
  await expect(ask(page).getByRole('alert')).toHaveText(UNREADABLE)
  mode = 'good'
  await askButton(page).click()
  await expect(result(page).locator('.ask-answer-headline')).toHaveText('Prescription #4')
  await expect(ask(page).getByRole('alert')).toHaveCount(0)
})

// --- stale responses ------------------------------------------------------------------

test('a late reply after Clear never appears, even after a new question has been answered', async ({ page }) => {
  const gates: Array<() => void> = []
  let n = 0
  await mockApi(page, async (c) => {
    if (!c.path.endsWith('/ask')) return undefined             // (the Dashboard's own reads are not part of this test)
    const mine = ++n
    if (mine === 1) await new Promise<void>((resolve) => gates.push(resolve))
    return { body: mine === 1 ? real(COUNT, 5) : answer(5) }
  })
  await signIn(page)
  await openPatient(page)
  await box(page).fill('first question')
  await askButton(page).click()
  await expect(askButton(page)).toHaveText(/Asking…/)
  await clearButton(page).click()
  await box(page).fill('second question')
  await askButton(page).click()
  await expect(result(page).locator('.ask-answer-headline')).toHaveText('Prescription #4')
  gates.forEach((release) => release())
  await page.waitForTimeout(500)
  await expect(result(page).locator('.ask-answer-headline')).toHaveText('Prescription #4')
  await expect(page.locator('body')).not.toContainText('Prescriptions recorded')       // the first (cleared) question's answer, never shown
})

test('a late reply for patient A never appears under patient B, and B can then be asked normally', async ({ page }) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  const calls = await mockApi(page, async (c) => {
    if (c.path === '/patients/5/ask') { await gate; return { body: real(COUNT, 5) } }
    return { body: answer(6) }
  })
  await signIn(page)
  await openPatient(page, /Anjali Nair/)
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await expect(askButton(page)).toHaveText(/Asking…/)
  await page.getByRole('button', { name: /Anjana Rao/ }).click()
  await expect(page.getByRole('heading', { name: 'Anjana Rao' })).toBeVisible()
  await expect(result(page)).toHaveCount(0)
  release()
  await page.waitForTimeout(500)
  await expect(result(page)).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText('Prescriptions recorded')
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await expect(result(page).locator('.ask-answer-headline')).toHaveText('Prescription #4')
  expect(askCalls(calls).map((c) => c.path)).toEqual(['/patients/5/ask', '/patients/6/ask'])
})

test('a reply that names another patient is refused even if it arrives for the current question', async ({ page }) => {
  await askWith(page, answer(6))
  await expect(ask(page).getByRole('alert')).toHaveText(UNREADABLE)
  await expect(result(page)).toHaveCount(0)
})

test('closing the patient while a question is pending drops the reply', async ({ page }) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await mockApi(page, async () => { await gate; return { body: real(COUNT, 5) } })
  await signIn(page)
  await openPatient(page)
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await page.getByRole('button', { name: /Close/ }).click()
  release()
  await page.waitForTimeout(500)
  await expect(page.locator('body')).not.toContainText('Prescriptions recorded')
})

// --- layout ---------------------------------------------------------------------------

test.describe('mobile', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  const LONG = 'Ashwagandha_Churna_Extra_Long_Herb_Name_Without_Any_Break_'.repeat(3)
  // a REAL reply whose herb is renamed everywhere it appears (plan, result, records), so it stays internally consistent
  const longHerb = (key: string) => JSON.parse(JSON.stringify(real(key, 5)).replaceAll('Neem', LONG))
  for (const [label, reply] of [
    ['an answer with a very long herb name', longHerb('anjali: How much Neem in total?')],
    ['a comparison', real('anjali: Did the Neem dose change since the previous prescription?', 5)],
    ['a diff', real('anjali: What changed from the previous prescription to the latest?', 5)],
    ['a list', real('anjali: List the dosha of each prescription.', 5)],
    ['an incomplete answer', incomplete('at_least')],
    ['a clarification', clarify([reading('A', 'The number of prescriptions containing Neem. ' + 'x'.repeat(120)), reading('B', 'The total grams of Neem across all prescriptions.')])],
    ['a refusal', refusal('unsafe', 'TREATMENT')],
  ] as const) {
    test(`${label} fits at 390px without horizontal scrolling`, async ({ page }) => {
      await askWith(page, reply)
      await expect(result(page)).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0)
      const r = await result(page).boundingBox()
      expect(r && r.x >= 0 && r.x + r.width <= 390).toBeTruthy()
    })
  }
})
