/**
 * Ask about this patient -- what the browser shows for a question over the reader's length limit, against a mocked API.
 * The reply is the backend's refusal at the normalisation stage (outcome abstain, category unsupported, stage normalize). The panel
 * says the question is too long and states the limit; any other unsupported refusal keeps the ordinary wording.
 */
import { expect, test, type Page, type Route } from '@playwright/test'

import { API_ORIGIN } from '../playwright.config'

const APP_ORIGIN = 'http://127.0.0.1:3100'
const TOKEN = 'test-session-token'
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Expose-Headers': 'X-Error-Code',
}
const ANJALI = { patient_pk: 5, clinic_patient_id: 'OP-1001', display_name: 'Anjali Nair' }
const RX12 = { prescription_id: 12, created_at: '2026-09-20 10:15:00', status: 'Active', dosha_imbalance: 'Vata', prescribed_herbs: [{ name: 'Ashwagandha', grams: 7 }] }
const refusal = (stage: string) => ({ patient_pk: 5, outcome: 'abstain', category: 'unsupported', stage, reason: 'AMBIGUOUS', text: null, value: null, qualifier: null, verified: false, options: [], evidence: [], plan_frame: null, plan_query: null, clarified: false })

async function setup(page: Page, stage: string): Promise<{ unexpected: string[]; asks: unknown[] }> {
  const state = { unexpected: [] as string[], asks: [] as unknown[] }
  await page.route((url) => url.origin !== new URL(API_ORIGIN).origin && url.origin !== APP_ORIGIN, (route) => { state.unexpected.push(route.request().url()); return route.abort('blockedbyclient') })
  await page.route(`${API_ORIGIN}/**`, async (route: Route) => {
    const req = route.request()
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    const path = new URL(req.url()).pathname
    if (path === '/auth/login') return route.fulfill({ status: 200, headers: CORS, json: { token: TOKEN, expires_at: '2099-01-01T00:00:00Z', username: 'test_user', clinic_id: 'fixture_clinic_do_not_display', role: 'DOCTOR' } })
    if (path === '/auth/logout') return route.fulfill({ status: 200, headers: CORS, json: { status: 'logged_out' } })
    if (path === '/inventory') return route.fulfill({ status: 200, headers: CORS, json: { items: [] } })
    if (path === '/patients') return route.fulfill({ status: 200, headers: CORS, json: { results: [ANJALI], truncated: false } })
    if (path === '/patients/5/prescriptions') return route.fulfill({ status: 200, headers: CORS, json: { patient: ANJALI, prescriptions: [RX12], limit: 20, offset: 0, has_more: true } })
    if (path === '/patients/5/ask') { state.asks.push(req.postDataJSON()); return route.fulfill({ status: 200, headers: CORS, json: refusal(stage) }) }
    state.unexpected.push(`${req.method()} ${path}`)
    return route.fulfill({ status: 599, headers: CORS, json: { detail: 'unexpected request in test' } })
  })
  return state
}

async function ask(page: Page, question: string) {
  await page.goto('/')
  await page.getByRole('button', { name: /Enter AyurCore/ }).click()
  await page.getByRole('textbox', { name: 'Username' }).fill('test_user')
  await page.getByRole('textbox', { name: 'Password' }).fill('not-a-real-password')
  await page.locator('form').getByRole('button', { name: /Sign in/ }).click()
  await expect(page.locator('.app-shell')).toBeVisible()
  await page.locator('nav.side-nav').getByRole('button', { name: 'Patients' }).click()
  await page.getByRole('searchbox', { name: /Search patients/ }).fill('an')
  await page.getByRole('button', { name: /Anjali Nair/ }).click()
  await expect(page.locator('.patient-record')).toBeVisible()
  await page.getByRole('textbox', { name: /Your question about/ }).fill(question)
  await page.getByRole('region', { name: 'ASK ABOUT THIS PATIENT' }).getByRole('button', { name: /^Ask$/ }).click()
}

test('a question over the limit says it is too long and states the limit', async ({ page }) => {
  const state = await setup(page, 'normalize')
  const long = 'What was the first prescription? ' + 'please '.repeat(60)
  await ask(page, long)
  const notice = page.getByRole('region', { name: 'ASK ABOUT THIS PATIENT' }).locator('.ask-notice')
  await expect(notice).toHaveAttribute('data-too-long', 'true')
  await expect(notice).toContainText('That question is too long.')
  await expect(notice).toContainText('300 characters and 60 words')
  await expect(page.locator('body')).not.toContainText(/can.t be answered from the record/)
  await expect(page.getByRole('region', { name: 'ASK ABOUT THIS PATIENT' }).getByRole('alert')).toHaveCount(0)
  expect(state.asks).toEqual([{ question: long.trim() }])
  expect(state.unexpected).toEqual([])
})

test('any other unsupported refusal keeps the ordinary wording', async ({ page }) => {
  const state = await setup(page, 'coverage')
  await ask(page, 'Is her blood pressure going up?')
  const notice = page.getByRole('region', { name: 'ASK ABOUT THIS PATIENT' }).locator('.ask-notice')
  await expect(notice).toContainText('That can’t be answered from the record.')
  await expect(notice).not.toHaveAttribute('data-too-long', 'true')
  await expect(notice).not.toContainText('too long')
  expect(state.unexpected).toEqual([])
})
