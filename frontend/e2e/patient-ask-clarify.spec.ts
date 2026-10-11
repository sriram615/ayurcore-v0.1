/**
 * Ask about this patient -- choosing a reading of a clarification, and the records an answer cites. Browser tests against a mocked API.
 *
 * A clarification offers readings; the doctor picks one and the panel resubmits the ORIGINAL question with that reading exactly as the
 * server sent it (backend/schemas.py AskRequest { question, choice: { frame, query } }). The panel never builds a frame or a query, and
 * every reply is checked against the contract before anything is shown. Cited records come from the reply itself; nothing else is fetched.
 * Answers here are REAL backend replies re-shaped only in which records they cite (see `answer`), so every one is internally consistent
 * and passes the same cross-check as a live reply; the wording of each operation is tested in ask-present.spec.ts.
 */
import { expect, test, type Page, type Route } from '@playwright/test'

import { API_ORIGIN } from '../playwright.config'
import { reply as real } from './fixtures/ask-real'

type Call = { method: string; path: string; params: URLSearchParams; auth?: string; body: unknown }
type Reply = { status?: number; body?: unknown; headers?: Record<string, string> } | 'network-error' | undefined
type Handler = (call: Call) => Reply | Promise<Reply>

const TOKEN = 'test-session-token'
const CLINIC = 'fixture_clinic_do_not_display'
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Expose-Headers': 'X-Error-Code',
}
const ANJALI = { patient_pk: 5, clinic_patient_id: 'OP-1001', display_name: 'Anjali Nair' }
const ANJANA = { patient_pk: 6, clinic_patient_id: 'OP-1002', display_name: 'Anjana Rao' }
// the loaded history holds ONE prescription (#12) and says there are more; evidence below cites others that are not on this page
const RX12 = { prescription_id: 12, created_at: '2026-09-20 10:15:00', status: 'Active', dosha_imbalance: 'Vata', prescribed_herbs: [{ name: 'Ashwagandha', grams: 7 }] }

const QUESTION = 'Neem count?'
const Q_COUNT = { op: 'count', target: 'herb', herb: 'Neem', dosha: null, last_n: null, a: 'previous', b: 'last' }
const Q_SUM = { op: 'sum', target: 'herb', herb: 'Neem', dosha: null, last_n: null, a: 'previous', b: 'last' }
const Q_LAST = { op: 'last', target: 'herb', herb: 'Neem', dosha: null, last_n: 3, a: 'first', b: 'last' }
const READINGS = [
  { frame: 'COUNT_HERB', text: 'The number of prescriptions containing Neem.', query: Q_COUNT },
  { frame: 'SUM_HERB', text: 'The total grams of Neem across all prescriptions.', query: Q_SUM },
  { frame: 'LAST_HERB_N', text: 'When Neem was last prescribed, within the latest 3.', query: Q_LAST },
]

const rx = (id: number, o: object = {}) => ({ kind: 'prescription', label: `rx:${id}`, prescription_id: id, created_at: '2026-09-18 09:30:00', status: 'CONFIRMED', dosha: 'Pitta', herbs: [{ name: 'Neem', grams: 10 }, { name: 'Brahmi', grams: 5 }], ...o })
const enc = (ref: string, o: object = {}) => ({ kind: 'encounter', label: `enc:${ref}`, encounter_ref: ref, status: 'OPEN', created_at: '2026-09-19 08:00:00', closed_at: null, ...o })
const base = (pk = 5) => ({ patient_pk: pk, stage: 'done', reason: null, text: null, value: null, qualifier: null, verified: false, options: [], evidence: [], plan_frame: null, plan_query: null, clarified: false })
const LIST = 'anjali: List all her prescriptions.'
/** A real "list prescriptions" reply that cites exactly `evidence` (default: prescription #4242), its result naming the cited prescriptions. */
const answer = (pk = 5, o: { evidence?: unknown; [key: string]: unknown } = {}) => {
  const evidence = 'evidence' in o ? o.evidence : [rx(4242)]
  const labels = Array.isArray(evidence) ? evidence.filter((r) => r && r.kind === 'prescription' && typeof r.label === 'string').map((r) => r.label) : []
  return real(LIST, pk, { value: labels, evidence, ...o })
}
const clarify = (pk = 5, readings: unknown[] = READINGS) => ({ ...base(pk), outcome: 'clarify', category: 'clarify', stage: 'clarify', reason: 'UNDERSPECIFIED', options: readings })
const refusal = (category: string, o: object = {}) => ({ ...base(), outcome: 'abstain', category, reason: 'X', stage: 'screen', ...o })

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
    if (url.pathname === '/patients/5/prescriptions') return route.fulfill({ status: 200, headers: CORS, json: { patient: ANJALI, prescriptions: [RX12], limit: 20, offset: 0, has_more: true } })
    if (url.pathname === '/patients/6/prescriptions') return route.fulfill({ status: 200, headers: CORS, json: { patient: ANJANA, prescriptions: [], limit: 20, offset: 0, has_more: false } })
    const reply = await handler(call)
    if (reply === 'network-error') return route.abort('failed')
    if (!reply) return route.fulfill({ status: 599, headers: CORS, json: { detail: `unexpected request in test: ${call.method} ${url.pathname}` } })
    return route.fulfill({ status: reply.status ?? 200, headers: { ...CORS, ...(reply.headers ?? {}) }, json: reply.body })
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
const reading = (page: Page, i: number) => ask(page).getByRole('list', { name: 'Possible readings' }).getByRole('button').nth(i)
const askCalls = (calls: Call[]) => calls.filter((c) => c.path.endsWith('/ask'))
const answerText = (page: Page) => result(page).locator('.ask-answer-headline')

/** Sign in, open Anjali, ask QUESTION (typed with surrounding spaces) and wait for the first reply to be on screen. */
async function start(page: Page, handler: Handler) {
  const calls = await mockApi(page, handler)
  await signIn(page)
  await openPatient(page)
  await box(page).fill(`  ${QUESTION}  `)
  await askButton(page).click()
  return calls
}

const isChoice = (c: Call) => typeof c.body === 'object' && c.body !== null && 'choice' in (c.body as object)
/** A server that offers READINGS for the question and answers a chosen reading with `onChoice`. */
const clarifyThen = (onChoice: Handler): Handler => (c) => (c.path.endsWith('/ask') ? (isChoice(c) ? onChoice(c) : { body: clarify() }) : undefined)

const FORBIDDEN = /ENCREF|enc:|rx:|encounter_pk|HIDDEN-CLINIC|created_by|plan_query|plan_frame|LAST_HERB|COUNT_HERB|SUM_HERB|UNDERSPECIFIED/

// ===== clarification =====================================================================

for (const [index, option] of READINGS.entries()) {
  test(`choosing reading ${index + 1} sends the original question and that reading, verbatim, and shows its answer`, async ({ page }) => {
    const calls = await start(page, clarifyThen(() => ({ body: answer(5, { clarified: true, evidence: [rx(7001 + index)] }) })))
    await expect(reading(page, index)).toContainText(option.text)
    await reading(page, index).click()
    await expect(result(page).locator('.ask-evidence-item')).toContainText(`#${7001 + index}`)
    const sent = askCalls(calls)
    expect(sent).toHaveLength(2)
    expect(sent[1]).toMatchObject({ method: 'POST', path: '/patients/5/ask', auth: `Bearer ${TOKEN}` })
    expect(sent[1].body).toEqual({ question: QUESTION, choice: { frame: option.frame, query: option.query } })
    expect(Object.keys(sent[1].body as object)).toEqual(['question', 'choice'])
    expect(Object.keys((sent[1].body as { choice: object }).choice)).toEqual(['frame', 'query'])
    await expect(result(page).getByText(`Answered as: ${option.text}`)).toBeVisible()
    await expect(result(page).getByRole('list', { name: 'Possible readings' })).toHaveCount(0)       // the readings are gone once answered
  })
}

test('the first request is the question alone; only a chosen reading adds a choice', async ({ page }) => {
  const calls = await start(page, clarifyThen(() => ({ body: answer(5, { clarified: true }) })))
  await expect(reading(page, 0)).toBeVisible()
  expect(askCalls(calls)[0].body).toEqual({ question: QUESTION })
  expect(askCalls(calls)).toHaveLength(1)
})

test('a reading is sent exactly as the server offered it, even an unusual one: the panel adds nothing and drops nothing', async ({ page }) => {
  const odd = { frame: 'CMP_HERB_FIRST', text: 'An unusual reading.', query: { op: 'compare', target: 'herb', herb: 'Ashwagandha', dosha: null, last_n: null, a: 'first', b: 'last' } }
  const calls = await start(page, (c) => (c.path.endsWith('/ask') ? (isChoice(c) ? { body: answer(5, { clarified: true }) } : { body: clarify(5, [odd]) }) : undefined))
  await reading(page, 0).click()
  await expect(answerText(page)).toBeVisible()
  expect(askCalls(calls)[1].body).toEqual({ question: QUESTION, choice: { frame: 'CMP_HERB_FIRST', query: odd.query } })
})

test('the original question is preserved: editing the box while readings are shown does not change what is resubmitted', async ({ page }) => {
  const calls = await start(page, clarifyThen(() => ({ body: answer(5, { clarified: true }) })))
  await expect(result(page).getByText(`You asked: ${QUESTION}`)).toBeVisible()
  await box(page).fill('something completely different')
  await reading(page, 1).click()
  await expect(answerText(page)).toBeVisible()
  expect((askCalls(calls)[1].body as { question: string }).question).toBe(QUESTION)
  await expect(box(page)).toHaveValue('something completely different')       // what the doctor typed is left alone
})

test('the original question survives an emptied box and a failed choice', async ({ page }) => {
  let fail = true
  const calls = await start(page, clarifyThen(() => (fail ? { status: 500, body: { detail: 'boom' } } : { body: answer(5, { clarified: true }) })))
  await box(page).fill('')
  await reading(page, 0).click()
  await expect(ask(page).getByRole('alert')).toBeVisible()
  fail = false
  await reading(page, 0).click()
  await expect(answerText(page)).toBeVisible()
  expect(askCalls(calls).slice(1).map((c) => (c.body as { question: string }).question)).toEqual([QUESTION, QUESTION])
})

test('while a chosen reading is pending the readings stay visible but cannot be chosen again, and only one request is sent', async ({ page }) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  const calls = await start(page, clarifyThen(async () => { await gate; return { body: answer(5, { clarified: true }) } }))
  await reading(page, 1).click()
  await expect(ask(page).getByRole('status').filter({ hasText: 'Looking through the record…' })).toBeVisible()
  await expect(reading(page, 1)).toContainText('Asking…')
  await expect(reading(page, 1)).toContainText(READINGS[1].text)
  for (let i = 0; i < 3; i++) await expect(reading(page, i)).toBeDisabled()
  await expect(ask(page)).toHaveAttribute('aria-busy', 'true')
  await expect(askButton(page)).toBeDisabled()
  expect(askCalls(calls)).toHaveLength(2)
  release()
  await expect(answerText(page)).toBeVisible()
  expect(askCalls(calls)).toHaveLength(2)
})

test('a chosen reading can be answered with a refusal, and that is rendered like any other', async ({ page }) => {
  await start(page, clarifyThen(() => ({ body: refusal('missing', { clarified: true }) })))
  await reading(page, 0).click()
  await expect(result(page)).toHaveAttribute('data-category', 'missing')
  await expect(result(page)).toContainText('Nothing is recorded for that.')
})

test('after a clarified answer the doctor can ask a new question normally', async ({ page }) => {
  const calls = await start(page, (c) => {
    if (!c.path.endsWith('/ask')) return undefined
    if (isChoice(c)) return { body: answer(5, { clarified: true }) }
    return { body: (c.body as { question: string }).question === QUESTION ? clarify() : answer(5, { evidence: [rx(7100)] }) }
  })
  await reading(page, 0).click()
  await expect(answerText(page)).toBeVisible()
  await box(page).fill('How many visits?')
  await askButton(page).click()
  await expect(result(page).locator('.ask-evidence-item')).toContainText('#7100')
  await expect(result(page).getByText(/Answered as:/)).toHaveCount(0)
  expect(askCalls(calls)[2].body).toEqual({ question: 'How many visits?' })
})

// ----- invalid, tampered and stale choices ---------------------------------------------

const REFUSAL_HEADERS: Array<[string, Record<string, string>]> = [['with the error code', { 'X-Error-Code': 'CHOICE_NOT_OFFERED' }], ['without it', {}]]
for (const [label, headers] of REFUSAL_HEADERS) {
  test(`a choice the server refuses (422 ${label}) is reported, the readings are dropped, the question is kept and can be asked again`, async ({ page }) => {
    let refuse = true
    const calls = await start(page, (c) => {
      if (!c.path.endsWith('/ask')) return undefined
      if (isChoice(c)) return refuse ? { status: 422, headers, body: { detail: 'The chosen reading is not one of the readings offered for this question.' } } : { body: answer(5, { clarified: true }) }
      return { body: clarify() }
    })
    await reading(page, 0).click()
    await expect(ask(page).getByRole('alert')).toHaveText('That choice is no longer valid. Ask your question again.')
    await expect(page.locator('body')).not.toContainText('not one of the readings')                  // never the backend's wording
    await expect(ask(page).getByRole('list', { name: 'Possible readings' })).toHaveCount(0)
    await expect(askButton(page)).toBeEnabled()
    refuse = false
    await box(page).fill(QUESTION)
    await askButton(page).click()
    await expect(reading(page, 0)).toBeVisible()                                                      // a fresh clarification
    await reading(page, 2).click()
    await expect(answerText(page)).toBeVisible()
    expect(askCalls(calls).map((c) => isChoice(c))).toEqual([false, true, false, true])
  })
}

test('a 422 on an ordinary question is not mistaken for a refused choice', async ({ page }) => {
  await start(page, () => ({ status: 422, body: { detail: 'x' } }))
  await expect(ask(page).getByRole('alert')).toHaveText("That question couldn't be answered right now. Try again.")
})

const TAMPERED: Record<string, unknown> = {
  'a reply that does not say it answers a chosen reading': answer(5, { clarified: false, text: 'LEAK-1' }),
  'another clarification instead of an answer': { ...clarify(), clarified: true, options: [{ ...READINGS[0], text: 'LEAK-2' }] },
  'a clarification with no clarified flag': { ...clarify(), clarified: undefined, options: [{ ...READINGS[0], text: 'LEAK-2' }] },
  'an answer about another patient': answer(6, { clarified: true, text: 'LEAK-3' }),
  'an answer that is not verified': answer(5, { clarified: true, verified: false, text: 'LEAK-4' }),
  'an answer with no evidence': answer(5, { clarified: true, evidence: [], text: 'LEAK-5' }),
  'an answer with a contradictory record': answer(5, { clarified: true, evidence: [rx(7, { label: 'rx:8' })], text: 'LEAK-6' }),
  'a body that is not an object': 'LEAK-7',
}
for (const [label, reply] of Object.entries(TAMPERED)) {
  test(`${label} to a chosen reading is refused with one fixed message, and the readings come back for another try`, async ({ page }) => {
    await start(page, clarifyThen(() => ({ body: reply })))
    await reading(page, 0).click()
    await expect(ask(page).getByRole('alert')).toHaveText("That answer couldn't be shown. Try asking again.")
    await expect(page.locator('body')).not.toContainText(/LEAK-/)
    await expect(result(page).locator('.ask-answer-headline')).toHaveCount(0)
    for (let i = 0; i < 3; i++) await expect(reading(page, i)).toBeEnabled()
  })
}

test('a failed choice (server error, then unreachable) shows one message, brings the readings back, and the retry sends the identical request', async ({ page }) => {
  let mode: 'server' | 'network' | 'ok' = 'server'
  const calls = await start(page, clarifyThen(() => (mode === 'server' ? { status: 500, body: { detail: 'Traceback: sqlite3.OperationalError /db/x' } } : mode === 'network' ? 'network-error' : { body: answer(5, { clarified: true }) })))
  await reading(page, 1).click()
  await expect(ask(page).getByRole('alert')).toHaveText("That question couldn't be answered right now. Try again.")
  await expect(page.locator('body')).not.toContainText('Traceback')
  await expect(reading(page, 1)).toBeEnabled()
  mode = 'network'
  await reading(page, 1).click()
  await expect(ask(page).getByRole('alert')).toBeVisible()
  mode = 'ok'
  await reading(page, 1).click()
  await expect(answerText(page)).toBeVisible()
  await expect(ask(page).getByRole('alert')).toHaveCount(0)
  const bodies = askCalls(calls).slice(1).map((c) => JSON.stringify(c.body))
  expect(bodies).toHaveLength(3)
  expect(new Set(bodies).size).toBe(1)
  expect(JSON.parse(bodies[0])).toEqual({ question: QUESTION, choice: { frame: 'SUM_HERB', query: Q_SUM } })
})

test('a session that ends while choosing returns to sign-in', async ({ page }) => {
  await start(page, clarifyThen(() => ({ status: 401, body: { detail: 'Invalid, expired, or revoked session.' } })))
  await reading(page, 0).click()
  await expect(page.getByText('Your session has ended. Sign in again to carry on — your notes are kept.')).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Username' })).toBeVisible()
})

test('Clear while a chosen reading is pending abandons it: the late answer and its records never appear, even after a new question', async ({ page }) => {
  const gates: Array<() => void> = []
  let choices = 0
  await start(page, (c) => {
    if (!c.path.endsWith('/ask')) return undefined
    if (isChoice(c)) {
      choices += 1
      return new Promise((resolve) => gates.push(() => resolve({ body: answer(5, { clarified: true, evidence: [rx(8888)] }) })))
    }
    return { body: (c.body as { question: string }).question === QUESTION ? clarify() : answer(5, { evidence: [rx(4242)] }) }
  })
  await reading(page, 0).click()
  await expect(reading(page, 0)).toContainText('Asking…')
  await clearButton(page).click()
  await expect(result(page)).toHaveCount(0)
  await box(page).fill('How many visits?')
  await askButton(page).click()
  await expect(result(page)).toContainText('#4242')
  gates.forEach((release) => release())
  await page.waitForTimeout(500)
  await expect(result(page)).toContainText('#4242')
  await expect(page.locator('body')).not.toContainText('#8888')
  expect(choices).toBe(1)
})

test('choosing a reading for one patient and switching to another: the late answer never appears under the second patient', async ({ page }) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await start(page, async (c) => {
    if (!c.path.endsWith('/ask')) return undefined
    if (c.path === '/patients/6/ask') return { body: answer(6, { evidence: [rx(6001)] }) }
    if (isChoice(c)) { await gate; return { body: answer(5, { clarified: true, evidence: [rx(5001)] }) } }
    return { body: clarify() }
  })
  await reading(page, 0).click()
  await expect(reading(page, 0)).toContainText('Asking…')
  await page.getByRole('button', { name: /Anjana Rao/ }).click()
  await expect(page.getByRole('heading', { name: 'Anjana Rao' })).toBeVisible()
  await expect(result(page)).toHaveCount(0)
  release()
  await page.waitForTimeout(500)
  await expect(result(page)).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText('#5001')
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await expect(result(page)).toContainText('#6001')
  await expect(page.locator('body')).not.toContainText('#5001')
})

test('closing the record while a chosen reading is pending drops the reply', async ({ page }) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await start(page, clarifyThen(async () => { await gate; return { body: answer(5, { clarified: true, evidence: [rx(7777)] }) } }))
  await reading(page, 0).click()
  await page.getByRole('button', { name: /Close/ }).click()
  release()
  await page.waitForTimeout(500)
  await expect(page.locator('body')).not.toContainText('#7777')
  await page.getByRole('button', { name: /Anjali Nair/ }).click()
  await expect(result(page)).toHaveCount(0)
  await expect(box(page)).toHaveValue('')
})

test('a clarification does not survive Clear, a patient switch or closing the record', async ({ page }) => {
  await start(page, (c) => (c.path.endsWith('/ask') ? { body: clarify(c.path === '/patients/5/ask' ? 5 : 6) } : undefined))
  await expect(reading(page, 0)).toBeVisible()
  await clearButton(page).click()
  await expect(ask(page).getByRole('list', { name: 'Possible readings' })).toHaveCount(0)
  await box(page).fill(QUESTION)
  await askButton(page).click()
  await expect(reading(page, 0)).toBeVisible()
  await page.getByRole('button', { name: /Anjana Rao/ }).click()
  await expect(ask(page).getByRole('list', { name: 'Possible readings' })).toHaveCount(0)
  await page.getByRole('button', { name: /Close/ }).click()
  await page.getByRole('button', { name: /Anjali Nair/ }).click()
  await expect(ask(page).getByRole('list', { name: 'Possible readings' })).toHaveCount(0)
})

// ===== evidence ===========================================================================

test('a prescription record shows its type, number, date, status, dosha and herbs from the reply alone', async ({ page }) => {
  await start(page, () => ({ body: answer(5, { evidence: [rx(4242)] }) }))
  const card = result(page).locator('.ask-evidence-item').first()
  await expect(result(page).getByText('BASED ON')).toBeVisible()
  await expect(card).toContainText('Prescription')
  await expect(card).toContainText('#4242')
  await expect(card).toContainText('Sep')
  await expect(card).toContainText('2026')
  await expect(card).toContainText('CONFIRMED')
  await expect(card.locator('.history-dosha')).toContainText('Pitta')
  await expect(card.getByRole('list', { name: 'Herbs' }).getByRole('listitem')).toHaveCount(2)
  await expect(card).toContainText('Neem')
  await expect(card).toContainText('10 g')
  await expect(card).toContainText('Brahmi')
})

test('an encounter record shows a visit with its status and dates, open or closed, and no reference', async ({ page }) => {
  await start(page, () => ({ body: answer(5, { evidence: [enc('ENCREF-OPEN'), enc('ENCREF-DONE', { status: 'CLOSED', closed_at: '2026-09-19 09:15:00' })] }) }))
  const cards = result(page).locator('.ask-evidence-item')
  await expect(cards).toHaveCount(2)
  await expect(cards.nth(0)).toContainText('Visit')
  await expect(cards.nth(0)).toContainText('OPEN')
  await expect(cards.nth(0)).toContainText('Not closed yet')
  await expect(cards.nth(1)).toContainText('CLOSED')
  await expect(cards.nth(1)).toContainText('Closed')
  await expect(cards.nth(1)).toContainText('Sep')
  await expect(page.locator('body')).not.toContainText(/ENCREF|enc:/)
})

test('unknown values are shown as not recorded, never filled in', async ({ page }) => {
  await start(page, () => ({ body: real('legacy: What is the latest prescription?', 5) }))      // a REAL reply: prescription #9 has no recorded dosha or herbs
  const card = result(page).locator('.ask-evidence-item').first()
  await expect(card).toContainText('#9')
  await expect(card.locator('.history-dosha')).toContainText('Not recorded')
  await expect(card).toContainText('Herbs not recorded.')
  await expect(result(page).getByText('Incomplete')).toBeVisible()
})

test('evidence reads the same whether or not the record is on the loaded history page, and nothing extra is fetched to show it', async ({ page }) => {
  const calls = await start(page, () => ({ body: answer(5, { evidence: [rx(9001), rx(12), rx(9002)] }) }))
  await expect(result(page).locator('.ask-evidence-item')).toHaveCount(3)
  await expect(result(page).locator('.ask-evidence-item').nth(0)).toContainText('#9001')
  await expect(result(page).locator('.ask-evidence-item').nth(0)).toContainText('Neem')             // full details though #9001 is not in the history list
  await expect(page.locator('.history-item')).toHaveCount(1)                                         // the history itself is untouched (page 1 only)
  const patientReads = calls.filter((c) => c.path.startsWith('/patients') && c.path !== '/patients')
  expect(patientReads.map((c) => `${c.method} ${c.path}`)).toEqual(['GET /patients/5/prescriptions', 'POST /patients/5/ask'])
  expect(calls.filter((c) => c.path.includes('/encounters') || c.path.startsWith('/prescribe'))).toHaveLength(0)
})

test('evidence never carries an internal key, label, clinic identifier or planning metadata onto the page', async ({ page }) => {
  await start(page, () => ({
    body: answer(5, {
      stage: 'planner', reason: 'LEAKED-REASON', plan_frame: 'LAST_HERB',
      evidence: [rx(55, { clinic_id: 'HIDDEN-CLINIC', encounter_pk: 99, created_by_user_id: 3 }), enc('ENCREF-SECRET', { encounter_pk: 4321, clinic_id: 'HIDDEN-CLINIC' })],
    }),
  }))
  await expect(result(page).locator('.ask-evidence-item')).toHaveCount(2)
  await expect(ask(page)).not.toContainText(FORBIDDEN)
  await expect(ask(page)).not.toContainText(/LEAKED-REASON|4321|99|planner/)
  await expect(page.locator('body')).not.toContainText(/HIDDEN-CLINIC|fixture_clinic_do_not_display/)
  const html = await ask(page).innerHTML()
  expect(html).not.toMatch(/ENCREF|HIDDEN-CLINIC|encounter_pk|rx:55|enc:/)
})

test('more records than the display cap show the cap and the true total', async ({ page }) => {
  await start(page, () => ({ body: answer(5, { evidence: Array.from({ length: 25 }, (_, i) => rx(100 + i)) }) }))
  await expect(result(page).locator('.ask-evidence-item')).toHaveCount(20)
  await expect(result(page).getByText('Showing 20 of 25 records.')).toBeVisible()
})

test('a short evidence list shows no "showing N of M" line', async ({ page }) => {
  await start(page, () => ({ body: answer(5, { evidence: [rx(1), rx(2)] }) }))
  await expect(result(page).locator('.ask-evidence-item')).toHaveCount(2)
  await expect(result(page).getByText(/Showing \d+ of/)).toHaveCount(0)
})

const BAD_EVIDENCE: Record<string, unknown> = {
  'no evidence': answer(5, { evidence: [], text: 'LEAK-A' }),
  'evidence missing': { ...answer(5, { text: 'LEAK-A' }), evidence: undefined },
  'evidence not a list': answer(5, { evidence: 'rx:4242', text: 'LEAK-A' }),
  'a null record': answer(5, { evidence: [null], text: 'LEAK-A' }),
  'a record that is a string': answer(5, { evidence: ['rx:12'], text: 'LEAK-A' }),
  'a label that disagrees with the record': answer(5, { evidence: [rx(12, { label: 'rx:13' })], text: 'LEAK-A' }),
  'an encounter label that disagrees': answer(5, { evidence: [enc('A', { label: 'enc:B' })], text: 'LEAK-A' }),
  'an unknown kind': answer(5, { evidence: [{ kind: 'unknown', label: 'rx:99' }], text: 'LEAK-A' }),
  'a kind the contract does not have': answer(5, { evidence: [rx(12, { kind: 'lab_result' })], text: 'LEAK-A' }),
  'a prescription with no number': answer(5, { evidence: [rx(12, { prescription_id: null })], text: 'LEAK-A' }),
  'a prescription with a negative number': answer(5, { evidence: [{ ...rx(1), prescription_id: -4, label: 'rx:-4' }], text: 'LEAK-A' }),
  'a prescription with no date': answer(5, { evidence: [rx(12, { created_at: null })], text: 'LEAK-A' }),
  'a prescription with no status': answer(5, { evidence: [rx(12, { status: '' })], text: 'LEAK-A' }),
  'a herb with no name': answer(5, { evidence: [rx(12, { herbs: [{ grams: 5 }] })], text: 'LEAK-A' }),
  'a herb with zero grams': answer(5, { evidence: [rx(12, { herbs: [{ name: 'Neem', grams: 0 }] })], text: 'LEAK-A' }),
  'herbs that are not a list': answer(5, { evidence: [rx(12, { herbs: 'Neem' })], text: 'LEAK-A' }),
  'an encounter with no reference': answer(5, { evidence: [enc('x', { encounter_ref: null })], text: 'LEAK-A' }),
  'an encounter with no date': answer(5, { evidence: [enc('x', { created_at: undefined })], text: 'LEAK-A' }),
  'one good record and one bad': answer(5, { evidence: [rx(12), rx(13, { status: null })], text: 'LEAK-A' }),
  'a clarification that cites records': { ...clarify(), evidence: [rx(12)] },
  'a refusal that cites records': refusal('unsafe', { evidence: [rx(12)], text: 'LEAK-A' }),
  'a missing answer that cites records': refusal('missing', { evidence: [rx(12)], text: 'LEAK-A' }),
}
for (const [label, reply] of Object.entries(BAD_EVIDENCE)) {
  test(`evidence problem (${label}) refuses the whole reply: no answer, no records, one fixed message`, async ({ page }) => {
    await start(page, () => ({ body: reply }))
    await expect(ask(page).getByRole('alert')).toHaveText("That answer couldn't be shown. Try asking again.")
    await expect(result(page)).toHaveCount(0)
    await expect(ask(page).locator('.ask-evidence-item')).toHaveCount(0)
    await expect(page.locator('body')).not.toContainText(/LEAK-A|Verified against the record/)
    await expect(askButton(page)).toBeEnabled()
  })
}

test('evidence is removed by Clear, by choosing another patient and by closing the record, and a late reply never brings it back', async ({ page }) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await start(page, async (c) => {
    if (c.path === '/patients/6/ask') { await gate; return { body: answer(6, { evidence: [rx(6002)] }) } }
    return { body: answer(5, { evidence: [rx(5002)] }) }
  })
  await expect(result(page).locator('.ask-evidence-item')).toHaveCount(1)
  await expect(result(page)).toContainText('#5002')
  await clearButton(page).click()
  await expect(ask(page).locator('.ask-evidence-item')).toHaveCount(0)
  await box(page).fill(QUESTION)
  await askButton(page).click()
  await expect(result(page)).toContainText('#5002')
  await page.getByRole('button', { name: /Anjana Rao/ }).click()
  await expect(ask(page).locator('.ask-evidence-item')).toHaveCount(0)
  await expect(page.locator('body')).not.toContainText('#5002')
  await box(page).fill(QUESTION)
  await askButton(page).click()
  await page.getByRole('button', { name: /Close/ }).click()
  release()
  await page.waitForTimeout(500)
  await expect(page.locator('body')).not.toContainText(/#6002|#5002/)
})

test('an answer with evidence, then a refusal: the first answer\'s records are gone', async ({ page }) => {
  let n = 0
  await start(page, (c) => (c.path.endsWith('/ask') ? { body: ++n === 1 ? answer(5, { evidence: [rx(5003)] }) : refusal('unsafe') } : undefined))
  await expect(result(page)).toContainText('#5003')
  await box(page).fill('Should we stop Neem?')
  await askButton(page).click()
  await expect(result(page)).toHaveAttribute('data-category', 'unsafe')
  await expect(page.locator('body')).not.toContainText('#5003')
})

// ===== layout =============================================================================

test.describe('mobile', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('readings and cited records fit at 390px: no horizontal scrolling, readings are comfortable tap targets', async ({ page }) => {
    const long = 'The number of prescriptions containing Neem ' + 'across_the_whole_recorded_history_'.repeat(5)
    const readings = [{ ...READINGS[0], text: long }, READINGS[1], READINGS[2]]
    await start(page, (c) => (c.path.endsWith('/ask') ? (isChoice(c)
      ? { body: answer(5, { clarified: true, evidence: [rx(41, { herbs: [{ name: 'Ashwagandha_with_a_very_long_name_'.repeat(4), grams: 12 }, { name: 'Brahmi', grams: 5 }] }), enc('E1', { status: 'CLOSED', closed_at: '2026-09-19 09:15:00' }), rx(42)] }) }
      : { body: clarify(5, readings) }) : undefined))
    const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    await expect(reading(page, 0)).toBeVisible()
    expect(await overflow()).toBeLessThanOrEqual(0)
    for (let i = 0; i < 3; i++) {
      const b = await reading(page, i).boundingBox()
      expect(b && b.height >= 44 && b.x >= 0 && b.x + b.width <= 390).toBeTruthy()
    }
    await reading(page, 0).click()
    await expect(result(page).locator('.ask-evidence-item')).toHaveCount(3)
    expect(await overflow()).toBeLessThanOrEqual(0)
    const r = await result(page).boundingBox()
    expect(r && r.x >= 0 && r.x + r.width <= 390).toBeTruthy()
  })
})
