/**
 * Ask about this patient (AI-2 panel foundation) -- browser tests against a mocked API.
 *
 * Placement, role visibility, the absence of any request until the doctor asks, the controls, and the idle / loading / result / error
 * states. How each kind of reply is rendered (and malformed replies) is covered by patient-ask-results.spec.ts.
 */
import { expect, test, type Page, type Route } from '@playwright/test'

import { API_ORIGIN } from '../playwright.config'
import { reply as real } from './fixtures/ask-real'

type Role = 'DOCTOR' | 'ADMIN'
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
const ANSWER = (pk: number) => real('anjali: What is the latest prescription?', pk)      // a REAL backend reply: prescription #4
const ANSWER_HEADLINE = 'Prescription #4'

async function mockApi(page: Page, role: Role, handler: Handler): Promise<Call[]> {
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
      return route.fulfill({ status: 200, headers: CORS, json: { token: TOKEN, expires_at: '2099-01-01T00:00:00Z', username: 'test_user', clinic_id: CLINIC, role } })
    }
    if (url.pathname === '/auth/logout') return route.fulfill({ status: 200, headers: CORS, json: { status: 'logged_out' } })
    const reply = await handler(call)
    if (reply === 'network-error') return route.abort('failed')
    if (!reply) return route.fulfill({ status: 599, headers: CORS, json: { detail: `unexpected request in test: ${call.method} ${url.pathname}` } })
    return route.fulfill({ status: reply.status ?? 200, headers: CORS, json: reply.body })
  })
  return calls
}

/** The patient registry and history, plus whatever the test supplies for /ask. */
const withPatients = (ask?: Handler): Handler => (c) => {
  if (c.path === '/patients') return { body: { results: [ANJALI, ANJANA], truncated: false } }
  if (c.path === '/patients/5/prescriptions') return { body: { patient: ANJALI, prescriptions: [RX], limit: 20, offset: 0, has_more: false } }
  if (c.path === '/patients/6/prescriptions') return { body: { patient: ANJANA, prescriptions: [], limit: 20, offset: 0, has_more: false } }
  if (c.path.endsWith('/ask') && ask) return ask(c)
  return undefined
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
const clearButton = (page: Page) => ask(page).getByRole('button', { name: 'Clear' })
const askCalls = (calls: Call[]) => calls.filter((c) => c.path.endsWith('/ask'))

// --- visibility + placement -----------------------------------------------------------

test('DOCTOR sees the Ask section between the patient heading and the prescription history', async ({ page }) => {
  await mockApi(page, 'DOCTOR', withPatients())
  await signIn(page)
  await openPatient(page)
  await expect(ask(page)).toBeVisible()
  await expect(ask(page).getByText('ASK ABOUT THIS PATIENT')).toBeVisible()
  const heading = await page.locator('.patient-record .panel-heading').boundingBox()
  const section = await ask(page).boundingBox()
  const kicker = await page.locator('.patient-record-kicker').boundingBox()
  expect(heading && section && kicker).toBeTruthy()
  expect(section!.y).toBeGreaterThanOrEqual(heading!.y + heading!.height - 1)
  expect(kicker!.y).toBeGreaterThanOrEqual(section!.y + section!.height - 1)
  await expect(page.locator('.history-item')).toHaveCount(1)          // the history itself is unchanged
  await expect(ask(page).locator('.history-item')).toHaveCount(0)      // and the panel does not borrow its class
})

test('ADMIN opens the same history and does not see the Ask section', async ({ page }) => {
  await mockApi(page, 'ADMIN', withPatients())
  await signIn(page)
  await openPatient(page)
  await expect(page.locator('.history-item')).toHaveCount(1)
  await expect(page.getByText('ASK ABOUT THIS PATIENT')).toHaveCount(0)
  await expect(page.getByRole('textbox', { name: /Your question about/ })).toHaveCount(0)
})

// --- no request until asked -----------------------------------------------------------

test('opening a patient history makes no Ask request, and nothing is sent until the doctor asks', async ({ page }) => {
  const calls = await mockApi(page, 'DOCTOR', withPatients())
  await signIn(page)
  await openPatient(page)
  await expect(ask(page)).toBeVisible()
  await box(page).fill('Neem count?')                                    // typing is local
  await page.waitForTimeout(400)
  expect(askCalls(calls)).toHaveLength(0)
  expect(calls.filter((c) => c.method === 'POST' && !c.path.startsWith('/auth'))).toHaveLength(0)
  expect(calls.filter((c) => c.path === '/patients/5/prescriptions')).toHaveLength(1)
})

// --- input + controls -----------------------------------------------------------------

test('Ask is disabled for an empty or whitespace-only question; Clear is disabled while idle and empty', async ({ page }) => {
  await mockApi(page, 'DOCTOR', withPatients())
  await signIn(page)
  await openPatient(page)
  await expect(askButton(page)).toBeDisabled()
  await expect(clearButton(page)).toBeDisabled()
  await box(page).fill('   ')
  await expect(askButton(page)).toBeDisabled()
  await box(page).fill('Neem count?')
  await expect(askButton(page)).toBeEnabled()
  await expect(clearButton(page)).toBeEnabled()
})

test('the question accepts English, Manglish and Malayalam text, up to the API limit', async ({ page }) => {
  await mockApi(page, 'DOCTOR', withPatients())
  await signIn(page)
  await openPatient(page)
  for (const q of ['When was Ashwagandha last prescribed?', 'Avasanam Neem ethra gram koduthu?', 'അവസാനത്തെ കുറിപ്പടിയിൽ എന്താണുള്ളത്?']) {
    await box(page).fill(q)
    await expect(box(page)).toHaveValue(q)
  }
  await expect(box(page)).toHaveAttribute('maxlength', '1000')
  await box(page).fill('n'.repeat(1200))
  expect((await box(page).inputValue()).length).toBeLessThanOrEqual(1000)
})

test('Ask sends exactly the trimmed question with the session token and nothing else', async ({ page }) => {
  const calls = await mockApi(page, 'DOCTOR', withPatients(() => ({ body: ANSWER(5) })))
  await signIn(page)
  await openPatient(page)
  await box(page).fill('  Avasanam Neem ethra gram koduthu?  ')
  await askButton(page).click()
  await expect(ask(page).locator('.ask-answer-headline')).toBeVisible()
  const sent = askCalls(calls)
  expect(sent).toHaveLength(1)
  expect(sent[0]).toMatchObject({ method: 'POST', path: '/patients/5/ask', auth: `Bearer ${TOKEN}`, body: { question: 'Avasanam Neem ethra gram koduthu?' } })
  expect(Object.keys(sent[0].body as object)).toEqual(['question'])
})

test('Enter asks, Shift+Enter adds a line, and an empty Enter sends nothing', async ({ page }) => {
  const calls = await mockApi(page, 'DOCTOR', withPatients(() => ({ body: ANSWER(5) })))
  await signIn(page)
  await openPatient(page)
  await box(page).press('Enter')
  expect(askCalls(calls)).toHaveLength(0)
  await box(page).fill('first')
  await box(page).press('Shift+Enter')
  await box(page).pressSequentially('second')
  await expect(box(page)).toHaveValue('first\nsecond')
  expect(askCalls(calls)).toHaveLength(0)
  await box(page).press('Enter')
  await expect(ask(page).locator('.ask-answer-headline')).toBeVisible()
  expect(askCalls(calls)).toHaveLength(1)
  expect((askCalls(calls)[0].body as { question: string }).question).toBe('first\nsecond')
})

// --- states ---------------------------------------------------------------------------

test('loading is announced, the form is marked busy, and Ask cannot be sent twice', async ({ page }) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  const calls = await mockApi(page, 'DOCTOR', withPatients(async () => { await gate; return { body: ANSWER(5) } }))
  await signIn(page)
  await openPatient(page)
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await expect(ask(page).getByRole('status').filter({ hasText: 'Looking through the record…' })).toBeVisible()
  await expect(ask(page)).toHaveAttribute('aria-busy', 'true')
  await expect(askButton(page)).toHaveText(/Asking…/)
  await expect(askButton(page)).toBeDisabled()
  await expect(box(page)).toHaveAttribute('readonly', '')       // read-only, not disabled: focus is never lost while asking
  expect(askCalls(calls)).toHaveLength(1)
  release()
  await expect(ask(page).locator('.ask-answer-headline')).toBeVisible()
  await expect(ask(page)).toHaveAttribute('aria-busy', 'false')
  await expect(askButton(page)).toBeEnabled()
})

test('the result area shows the verified answer and nothing the backend did not give for display', async ({ page }) => {
  await mockApi(page, 'DOCTOR', withPatients(() => ({ body: { ...ANSWER(5), reason: 'RAW-REASON', stage: 'raw-stage' } })))
  await signIn(page)
  await openPatient(page)
  await box(page).fill('Neem count?')
  await askButton(page).click()
  const result = ask(page).locator('.ask-answer')
  await expect(result).toBeVisible()
  await expect(result.locator('.ask-answer-headline')).toHaveText(ANSWER_HEADLINE)
  await expect(result).toContainText('Verified against the record')
  await expect(page.locator('body')).not.toContainText(/RAW-REASON|raw-stage|coming in the next update/)
})

test('a failed question shows one fixed message (never a backend detail) and can be asked again', async ({ page }) => {
  let fail = true
  const calls = await mockApi(page, 'DOCTOR', withPatients(() => (fail ? { status: 500, body: { detail: 'Traceback: sqlite3.OperationalError at /db/ayurcore.db' } } : { body: ANSWER(5) })))
  await signIn(page)
  await openPatient(page)
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await expect(ask(page).getByRole('alert')).toHaveText("That question couldn't be answered right now. Try again.")
  await expect(page.locator('body')).not.toContainText('Traceback')
  await expect(box(page)).toHaveValue('Neem count?')               // the question is kept
  fail = false
  await askButton(page).click()
  await expect(ask(page).locator('.ask-answer-headline')).toBeVisible()
  await expect(ask(page).getByRole('alert')).toHaveCount(0)
  expect(askCalls(calls)).toHaveLength(2)
})

test('an unreachable backend is the same fixed message, and an expired session returns to sign-in', async ({ page }) => {
  let mode: 'network' | 'expired' = 'network'
  await mockApi(page, 'DOCTOR', withPatients(() => (mode === 'network' ? 'network-error' : { status: 401, body: { detail: 'Invalid, expired, or revoked session.' } })))
  await signIn(page)
  await openPatient(page)
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await expect(ask(page).getByRole('alert')).toBeVisible()
  mode = 'expired'
  await askButton(page).click()
  await expect(page.getByText('Your session has ended. Sign in again to carry on — your notes are kept.')).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Username' })).toBeVisible()
})

// --- Clear + patient change -----------------------------------------------------------

test('Clear returns to idle: question emptied, result and error removed, controls disabled again', async ({ page }) => {
  await mockApi(page, 'DOCTOR', withPatients(() => ({ body: ANSWER(5) })))
  await signIn(page)
  await openPatient(page)
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await expect(ask(page).locator('.ask-answer-headline')).toBeVisible()
  await clearButton(page).click()
  await expect(box(page)).toHaveValue('')
  await expect(box(page)).toBeFocused()
  await expect(ask(page).locator('.ask-answer-headline')).toHaveCount(0)
  await expect(askButton(page)).toBeDisabled()
  await expect(clearButton(page)).toBeDisabled()
})

test('Clear while a question is pending abandons it: a late answer never appears', async ({ page }) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await mockApi(page, 'DOCTOR', withPatients(async () => { await gate; return { body: ANSWER(5) } }))
  await signIn(page)
  await openPatient(page)
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await expect(askButton(page)).toHaveText(/Asking…/)
  await clearButton(page).click()
  await expect(ask(page).getByRole('status')).toHaveCount(0)
  await expect(box(page)).toBeEnabled()
  release()
  await page.waitForTimeout(400)
  await expect(ask(page).locator('.ask-answer-headline')).toHaveCount(0)
  await expect(box(page)).toHaveValue('')
})

test('choosing another patient resets the panel, and a late answer for the first patient never shows under the second', async ({ page }) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await mockApi(page, 'DOCTOR', withPatients(async () => { await gate; return { body: ANSWER(5) } }))
  await signIn(page)
  await openPatient(page, /Anjali Nair/)
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await expect(askButton(page)).toHaveText(/Asking…/)
  await page.getByRole('button', { name: /Anjana Rao/ }).click()
  await expect(page.getByRole('heading', { name: 'Anjana Rao' })).toBeVisible()
  await expect(box(page)).toHaveValue('')
  await expect(box(page)).toBeEnabled()
  await expect(ask(page).getByRole('status')).toHaveCount(0)
  release()
  await page.waitForTimeout(400)
  await expect(ask(page).locator('.ask-answer-headline')).toHaveCount(0)
})

test('closing the patient and reopening starts from an empty panel', async ({ page }) => {
  await mockApi(page, 'DOCTOR', withPatients(() => ({ body: ANSWER(5) })))
  await signIn(page)
  await openPatient(page)
  await box(page).fill('Neem count?')
  await askButton(page).click()
  await expect(ask(page).locator('.ask-answer-headline')).toBeVisible()
  await page.getByRole('button', { name: /Close/ }).click()
  await expect(ask(page)).toHaveCount(0)
  await page.getByRole('button', { name: /Anjali Nair/ }).click()
  await expect(box(page)).toHaveValue('')
  await expect(ask(page).locator('.ask-answer-headline')).toHaveCount(0)
})

// --- accessibility + layout -----------------------------------------------------------

test('the panel is a labelled region with a labelled textbox and named buttons, and the page keeps one h1', async ({ page }) => {
  await mockApi(page, 'DOCTOR', withPatients())
  await signIn(page)
  await openPatient(page)
  await expect(ask(page)).toBeVisible()
  await expect(box(page)).toHaveAccessibleName('Your question about Anjali Nair')
  await expect(askButton(page)).toHaveAccessibleName('Ask')
  await expect(clearButton(page)).toHaveAccessibleName('Clear')
  await expect(page.getByRole('heading', { level: 1 })).toHaveCount(1)
})

test.describe('mobile', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('the panel fits at 390px with a long question and a result, with no horizontal scrolling', async ({ page }) => {
    await mockApi(page, 'DOCTOR', withPatients(() => ({ body: ANSWER(5) })))
    await signIn(page)
    await openPatient(page)
    await expect(ask(page)).toBeVisible()
    await box(page).fill('അവസാനത്തെ കുറിപ്പടിയിൽ പുതുതായി എന്താണ് ചേർത്തത് ' + 'Avasanam Neem ethra gram koduthu '.repeat(8))
    await askButton(page).click()
    await expect(ask(page).locator('.ask-answer-headline')).toBeVisible()
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow).toBeLessThanOrEqual(0)
    const section = await ask(page).boundingBox()
    expect(section && section.x >= 0 && section.x + section.width <= 390).toBeTruthy()
    const ask2 = await askButton(page).boundingBox()
    expect(ask2 && ask2.height >= 36).toBeTruthy()
  })
})
