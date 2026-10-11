/**
 * Ask about this patient -- what the BROWSER shows for an empty incomplete list, against a mocked API.
 *
 * A patient whose prescriptions all have unknown herbs (or dosha) gets, from the real backend, an incomplete answer with an EMPTY list that cites
 * the unknown prescriptions (e2e/fixtures/ask-empty-partial.ts). lib/ask-present.ts words it; this spec checks what the page then renders: the
 * Incomplete badge, the caveat, the evidence cards, no list, no misleading claim that nothing was prescribed -- and that mixed and exact lists
 * are unchanged. Wording is pinned in ask-present-empty-partial.spec.ts and ask-evidence-presence.spec.ts; here only the rendering is asserted.
 *
 * Every reply is a captured synthetic backend reply (empty-partial: ask-empty-partial.ts; mixed and exact: ask-real.ts). Nothing reaches a backend:
 * the API origin is fulfilled by this spec and every other origin is aborted and counted; a test fails if anything unexpected was requested.
 * Screenshots of the answer region are attached to each test (testInfo.outputPath).
 */
import { expect, test, type Page, type Route } from '@playwright/test'

import { API_ORIGIN } from '../playwright.config'
import { reply as real } from './fixtures/ask-real'
import { emptyReply } from './fixtures/ask-empty-partial'

type Call = { method: string; path: string; auth?: string; body: unknown }
type Mock = { calls: Call[]; unexpected: string[] }

const APP_ORIGIN = 'http://127.0.0.1:3100'                              // playwright.config.ts baseURL: the only origin besides the mocked API
const TOKEN = 'test-session-token'
const CLINIC = 'fixture_clinic_do_not_display'
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Expose-Headers': 'X-Error-Code',
}
const ANJALI = { patient_pk: 5, clinic_patient_id: 'OP-1001', display_name: 'Anjali Nair' }
const RX12 = { prescription_id: 12, created_at: '2026-09-20 10:15:00', status: 'Active', dosha_imbalance: 'Vata', prescribed_herbs: [{ name: 'Ashwagandha', grams: 7 }] }

const HERBS = 'allunknown: List all the herbs prescribed so far'
const DOSHAS = 'allunknown: List the dosha of each prescription'
const MIXED = 'legacy: Which herbs has she been given?'
const EXACT = 'anjali: Which herbs has she been given?'
const typed = (key: string) => key.slice(key.indexOf(': ') + 2)           // the question the doctor types
const replyFor = (key: string): Record<string, unknown> => (key.startsWith('allunknown') ? emptyReply(key, 5) : real(key, 5))

async function mockApi(page: Page, answers: Record<string, Record<string, unknown>>): Promise<Mock> {
  const mock: Mock = { calls: [], unexpected: [] }
  // Only the app itself (loopback, port 3100) and the API origin (fulfilled below) may be requested; anything else is aborted AND recorded.
  await page.route(
    (url) => url.origin !== new URL(API_ORIGIN).origin && url.origin !== APP_ORIGIN,
    (route) => { mock.unexpected.push(route.request().url()); return route.abort('blockedbyclient') }
  )
  await page.route(`${API_ORIGIN}/**`, async (route: Route) => {
    const req = route.request()
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    const url = new URL(req.url())
    let body: unknown = null
    try { body = req.postDataJSON() } catch { body = req.postData() }
    mock.calls.push({ method: req.method(), path: url.pathname, auth: req.headers()['authorization'], body })
    if (url.pathname === '/auth/login') {
      return route.fulfill({ status: 200, headers: CORS, json: { token: TOKEN, expires_at: '2099-01-01T00:00:00Z', username: 'test_user', clinic_id: CLINIC, role: 'DOCTOR' } })
    }
    if (url.pathname === '/auth/logout') return route.fulfill({ status: 200, headers: CORS, json: { status: 'logged_out' } })
    if (url.pathname === '/inventory') return route.fulfill({ status: 200, headers: CORS, json: { items: [] } })      // the Dashboard, the DOCTOR landing page after sign-in
    if (url.pathname === '/patients') return route.fulfill({ status: 200, headers: CORS, json: { results: [ANJALI], truncated: false } })
    if (url.pathname === '/patients/5/prescriptions') return route.fulfill({ status: 200, headers: CORS, json: { patient: ANJALI, prescriptions: [RX12], limit: 20, offset: 0, has_more: true } })
    if (url.pathname === '/patients/5/ask') {
      const question = (body as { question?: string } | null)?.question
      const answer = answers[question ?? '']
      if (answer) return route.fulfill({ status: 200, headers: CORS, json: answer })
    }
    mock.unexpected.push(`${req.method()} ${url.pathname}`)
    return route.fulfill({ status: 599, headers: CORS, json: { detail: `unexpected request in test: ${req.method()} ${url.pathname}` } })
  })
  return mock
}

async function signIn(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: /Enter AyurCore/ }).click()
  await page.getByRole('textbox', { name: 'Username' }).fill('test_user')
  await page.getByRole('textbox', { name: 'Password' }).fill('not-a-real-password')
  await page.locator('form').getByRole('button', { name: /Sign in/ }).click()
  await expect(page.locator('.app-shell')).toBeVisible()
}

async function openPatient(page: Page) {
  const menu = page.getByRole('button', { name: 'Open navigation' })
  if (await menu.isVisible()) await menu.click()
  await page.locator('nav.side-nav').getByRole('button', { name: 'Patients' }).click()
  await page.getByRole('searchbox', { name: /Search patients/ }).fill('an')
  await page.getByRole('button', { name: /Anjali Nair/ }).click()
  await expect(page.locator('.patient-record')).toBeVisible()
}

const ask = (page: Page) => page.getByRole('region', { name: 'ASK ABOUT THIS PATIENT' })
const box = (page: Page) => page.getByRole('textbox', { name: /Your question about/ })
const result = (page: Page) => ask(page).locator('.ask-result')
const overflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)

/** Sign in, open Anjali, type the question and ask; returns once the answer is on screen. */
async function askQuestion(page: Page, key: string): Promise<Mock> {
  const m = await mockApi(page, { [typed(key)]: replyFor(key) })
  await signIn(page)
  await openPatient(page)
  await box(page).fill(typed(key))
  await ask(page).getByRole('button', { name: /^Ask$/ }).click()
  await expect(result(page).locator('.ask-answer-headline, .ask-answer-items')).not.toHaveCount(0)
  return m
}

const ABSENCE_CLAIM = /\b(no|zero|0)\s+(herbs?|dosha)\w*\s+(were|was|have been|has been|had|applied)\b|\b(nothing|none)\s+(was|were|has been|have been)\s+(prescribed|given|recorded)\b/i

const EMPTY_CASES = [
  {
    key: HERBS, name: 'herb',
    summary: 'No herbs can be listed: none are recorded on the cited prescriptions.',
    caveat: '3 prescriptions (#101, #102, #103) have herbs not recorded. This is not a record that no herbs were prescribed.',
    title: 'Herbs prescribed',
  },
  {
    key: DOSHAS, name: 'dosha',
    summary: 'No dosha can be listed: none is recorded on the cited prescriptions.',
    caveat: '3 prescriptions (#101, #102, #103) have no dosha recorded. This is not a record that no dosha applied.',
    title: 'Dosha by prescription, oldest first',
  },
]

for (const c of EMPTY_CASES) {
  test(`all-unknown ${c.name} list: an incomplete answer with its caveat and the three unknown records, no list`, async ({ page }, testInfo) => {
    const mock = await askQuestion(page, c.key)
    const answer = result(page)

    // the badge says Incomplete and the page does not claim verification-as-complete
    await expect(answer.locator('.inventory-status.low-stock')).toHaveText('Incomplete')
    await expect(answer.getByText('Verified against the record')).toHaveCount(0)
    // the approved wording, as rendered
    await expect(answer.locator('.ask-answer-title')).toHaveText(c.title)
    await expect(answer.locator('.ask-answer-headline')).toHaveText('Not known')
    await expect(answer.locator('.ask-answer-summary')).toHaveText(c.summary)
    await expect(answer.locator('.ask-caveat')).toHaveText(c.caveat)
    // no list, no rows, no refusal
    await expect(answer.locator('.ask-answer-items')).toHaveCount(0)
    await expect(answer.locator('.ask-answer-rows')).toHaveCount(0)
    await expect(ask(page).getByRole('alert')).toHaveCount(0)
    await expect(page.locator('body')).not.toContainText(/couldn.t be shown/)
    // the three unknown prescriptions are shown as unknown, never filled in
    const cards = answer.locator('.ask-evidence-item')
    await expect(cards).toHaveCount(3)
    for (let i = 0; i < 3; i++) {
      await expect(cards.nth(i)).toContainText(`#${101 + i}`)
      await expect(cards.nth(i)).toContainText('Herbs not recorded.')
      await expect(cards.nth(i).locator('.history-dosha')).toContainText('Not recorded')
    }
    await expect(answer.locator('.ask-evidence ul[aria-label="Records used"]')).toBeVisible()
    // request shape: the question and nothing else, once
    const asks = mock.calls.filter((x) => x.path.endsWith('/ask'))
    expect(asks).toHaveLength(1)
    expect(asks[0]).toMatchObject({ method: 'POST', path: '/patients/5/ask', auth: `Bearer ${TOKEN}`, body: { question: typed(c.key) } })
    expect(Object.keys(asks[0].body as object)).toEqual(['question'])
    // focus moves to the answer, and the page does not scroll sideways
    await expect(answer).toBeFocused()
    expect(await overflow(page)).toBeLessThanOrEqual(0)
    // nothing unexpected was requested
    expect(mock.unexpected).toEqual([])
    await answer.screenshot({ path: testInfo.outputPath(`all-unknown-${c.name}.png`) })
  })
}

for (const c of EMPTY_CASES) {
  test(`the empty ${c.name} list never claims that nothing was prescribed, recorded or applied`, async ({ page }, testInfo) => {
    const mock = await askQuestion(page, c.key)
    const text = await result(page).innerText()
    // the one sentence that NEGATES the claim is required, then removed before the sweep
    expect(text).toContain(c.caveat.slice(c.caveat.indexOf('This is not a record')))
    const asserted = text.replace(/This is not a record that [^.]*\./, '')
    expect(asserted).not.toMatch(ABSENCE_CLAIM)
    expect(text).not.toMatch(/\b0\s+(herbs?|dosha)/i)
    expect(text).not.toMatch(/\bNone\b/)                                         // no bare "None" headline or item
    expect(text).not.toMatch(/rx:|enc:|created_at|From the known records only|list herb|list dosha|: none\.|undefined|NaN|\[object|\bnull\b/)
    expect(text).not.toMatch(/LIST_HERB|LIST_DOSHA|UNDERSPECIFIED|PARTIAL|partial|stage|reason/)
    expect(mock.unexpected).toEqual([])
    await result(page).screenshot({ path: testInfo.outputPath(`claims-sweep-${c.name}.png`) })
  })
}

test('mixed history: the known herbs are listed, with the caveat, the badge and the cited records', async ({ page }, testInfo) => {
  const mock = await askQuestion(page, MIXED)
  const answer = result(page)
  const evidence = real(MIXED, 5).evidence as unknown[]
  await expect(answer.locator('.inventory-status.low-stock')).toHaveText('Incomplete')
  await expect(answer.getByText('Verified against the record')).toHaveCount(0)
  await expect(answer.locator('.ask-answer-title')).toHaveText('Herbs prescribed')
  await expect(answer.locator('.ask-answer-items li')).toHaveText(['Neem', 'Brahmi'])
  await expect(answer.locator('.ask-caveat')).toHaveText('1 prescription (#9) has herbs not recorded; any herbs in it are not listed.')
  await expect(answer.locator('.ask-evidence-item')).toHaveCount(Math.min(evidence.length, 20))
  await expect(answer.locator('.ask-evidence-item', { hasText: 'Herbs not recorded.' })).toHaveCount(1)
  await expect(answer.locator('.ask-evidence-item', { hasText: '#9' })).toContainText('Herbs not recorded.')
  await expect(ask(page).getByRole('alert')).toHaveCount(0)
  await expect(answer).toBeFocused()
  expect(mock.unexpected).toEqual([])
  await answer.screenshot({ path: testInfo.outputPath('mixed-history.png') })
})

test('exact non-empty list: verified, every herb in order, no caveat and no Incomplete badge', async ({ page }, testInfo) => {
  const mock = await askQuestion(page, EXACT)
  const answer = result(page)
  const evidence = real(EXACT, 5).evidence as unknown[]
  await expect(answer.locator('.status-text', { hasText: 'Verified against the record' })).toBeVisible()
  await expect(answer.locator('.inventory-status.low-stock')).toHaveCount(0)
  await expect(answer.getByText('Incomplete')).toHaveCount(0)
  await expect(answer.locator('.ask-answer-title')).toHaveText('Herbs prescribed')
  await expect(answer.locator('.ask-answer-items li')).toHaveText(['Ashwagandha', 'Neem', 'Brahmi', 'Punarnava', 'Guggulu'])
  await expect(answer.locator('.ask-caveat')).toHaveCount(0)
  await expect(answer.locator('.ask-evidence-item')).toHaveCount(Math.min(evidence.length, 20))
  await expect(answer.getByText('Herbs not recorded.')).toHaveCount(0)
  await expect(ask(page).getByRole('alert')).toHaveCount(0)
  await expect(answer).toBeFocused()
  expect(mock.unexpected).toEqual([])
  await answer.screenshot({ path: testInfo.outputPath('exact-list.png') })
})

test.describe('mobile', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('390px: the empty herb list, its caveat and cards fit; no sideways scrolling', async ({ page }, testInfo) => {
    const mock = await askQuestion(page, HERBS)
    const answer = result(page)
    await expect(answer.locator('.inventory-status.low-stock')).toHaveText('Incomplete')
    await expect(answer.locator('.ask-caveat')).toBeVisible()
    await expect(answer.locator('.ask-evidence-item')).toHaveCount(3)
    expect(await overflow(page)).toBeLessThanOrEqual(0)
    for (const part of [answer, answer.locator('.ask-caveat'), answer.locator('.ask-evidence-item').first(), answer.locator('.ask-evidence-item').last()]) {
      const b = await part.boundingBox()
      expect(b && b.x >= 0 && b.x + b.width <= 390).toBeTruthy()
    }
    const askButton = ask(page).getByRole('button', { name: /^Ask$/ })
    const b = await askButton.boundingBox()
    expect(b && b.height >= 44).toBeTruthy()
    expect(mock.unexpected).toEqual([])
    await answer.screenshot({ path: testInfo.outputPath('mobile-all-unknown-herb.png') })
  })
})
