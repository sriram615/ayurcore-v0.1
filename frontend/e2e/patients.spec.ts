/**
 * Patients workspace -- browser tests against a mocked API.
 *
 * Every request the app makes to API_ORIGIN is intercepted here and recorded,
 * so tests can assert exactly what was sent (path, query, Authorization,
 * body) as well as what is rendered. All data below is test fixture data
 * living only in this file.
 */
import { expect, test, type Page, type Route } from '@playwright/test'

import { API_ORIGIN } from '../playwright.config'

type Role = 'DOCTOR' | 'ADMIN' | 'AUDITOR'
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

function rx(id: number, createdAt: string, dosha: string | null, herbs: { name: string; grams: number }[] | null) {
  return { prescription_id: id, created_at: createdAt, status: 'Active', dosha_imbalance: dosha, prescribed_herbs: herbs }
}

function history(patient: typeof ANJALI, prescriptions: unknown[], page: { offset?: number; has_more?: boolean } = {}) {
  return { patient, prescriptions, limit: 20, offset: page.offset ?? 0, has_more: page.has_more ?? false }
}

async function mockApi(page: Page, role: Role, handler: Handler): Promise<Call[]> {
  const calls: Call[] = []
  // Anything leaving the browser that is neither the app itself nor the
  // mocked API is blocked outright (analytics, fonts, a mis-set API URL).
  // Registered first, so the API route below takes precedence for its origin.
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

async function signIn(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: /Enter AyurCore/ }).click()
  await page.getByRole('textbox', { name: 'Username' }).fill('test_user')
  await page.getByRole('textbox', { name: 'Password' }).fill('not-a-real-password')
  await page.locator('form').getByRole('button', { name: /Sign in/ }).click()
  await expect(page.locator('.app-shell')).toBeVisible()
}

async function openPatients(page: Page) {
  const menu = page.getByRole('button', { name: 'Open navigation' })
  if (await menu.isVisible()) await menu.click()
  await page.locator('nav.side-nav').getByRole('button', { name: 'Patients' }).click()
  await expect(page.getByRole('heading', { name: 'Patients', level: 1 })).toBeVisible()
}

const searchBox = (page: Page) => page.getByRole('searchbox', { name: /Search patients/ })
const patientsView = (page: Page) => page.locator('.patients-view')
const patientCalls = (calls: Call[], path = '/patients') => calls.filter((c) => c.path === path)

// --- role visibility --------------------------------------------------------------

for (const role of ['DOCTOR', 'ADMIN'] as const) {
  test(`${role} is offered the Patients workspace`, async ({ page }) => {
    await mockApi(page, role, () => undefined)
    await signIn(page)
    await openPatients(page)
    await expect(searchBox(page)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Register patient' })).toBeVisible()
  })
}

test('AUDITOR is not offered the Patients workspace and makes no patient calls', async ({ page }) => {
  const calls = await mockApi(page, 'AUDITOR', (c) => (c.path.startsWith('/audit') ? { body: c.path === '/audit/export'
    ? { clinic_id: CLINIC, generated_at: 'now', record_count: 0, changed_count: 0, records: [], integrity_notes: [] }
    : { clinic_id: CLINIC, generated_at: 'now', event_count: 0, chain_valid: true, integrity_violations: [], events: [] } } : undefined))
  await signIn(page)
  const nav = page.locator('nav.side-nav')
  await expect(nav.getByRole('button', { name: 'Audit' })).toBeVisible()
  await expect(nav.getByRole('button', { name: 'Patients' })).toHaveCount(0)
  expect(calls.filter((c) => c.path.startsWith('/patients'))).toHaveLength(0)
})

// --- search -----------------------------------------------------------------------

test('search sends the session token, shows ID + name, and never shows clinic or phone', async ({ page }) => {
  const calls = await mockApi(page, 'DOCTOR', (c) => (c.path === '/patients' ? { body: { results: [ANJALI, ANJANA], truncated: false } } : undefined))
  await signIn(page)
  await openPatients(page)

  await expect(page.getByText('Type at least 2 characters of a name or patient ID.')).toBeVisible()
  await searchBox(page).fill('a')
  await expect(page.getByText('Keep typing — at least 2 characters.')).toBeVisible()
  await page.waitForTimeout(600)
  expect(patientCalls(calls)).toHaveLength(0)

  await searchBox(page).fill('  an ')
  const results = page.getByRole('list', { name: 'Matching patients' }).getByRole('button')
  await expect(results).toHaveCount(2)
  await expect(results.nth(0)).toContainText('Anjali Nair')
  await expect(results.nth(0)).toContainText('OP-1001')
  await expect(results.nth(1)).toContainText('Anjana Rao')
  await expect(page.getByText('2 patients')).toBeVisible()

  const searches = patientCalls(calls)
  expect(searches.at(-1)?.params.get('q')).toBe('an')
  expect(searches.at(-1)?.auth).toBe(`Bearer ${TOKEN}`)
  expect(searches.every((c) => !c.params.has('clinic_id'))).toBe(true)
  await expect(patientsView(page)).not.toContainText(CLINIC)
})

test('no-match and truncated results have clear states', async ({ page }) => {
  await mockApi(page, 'DOCTOR', (c) => {
    if (c.path !== '/patients') return undefined
    return c.params.get('q') === 'zz'
      ? { body: { results: [], truncated: false } }
      : { body: { results: [ANJALI], truncated: true } }
  })
  await signIn(page)
  await openPatients(page)

  await searchBox(page).fill('zz')
  await expect(page.getByText('No patients match “zz”.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Register a new patient' })).toBeVisible()

  await searchBox(page).fill('an')
  await expect(page.getByText(/refine your search/)).toBeVisible()
})

test('search failures show friendly messages, and an expired session returns to sign-in', async ({ page }) => {
  let mode: 'server' | 'network' | 'expired' = 'server'
  await mockApi(page, 'DOCTOR', (c) => {
    if (c.path !== '/patients') return undefined
    if (mode === 'server') return { status: 500, body: { detail: 'Traceback: sqlite3.OperationalError at /db/ayurcore.db' } }
    if (mode === 'network') return 'network-error'
    return { status: 401, body: { detail: 'Invalid, expired, or revoked session.' } }
  })
  await signIn(page)
  await openPatients(page)

  await searchBox(page).fill('an')
  await expect(patientsView(page).getByRole('alert')).toHaveText('Something went wrong on our side and nothing was saved. Try once more.')
  await expect(patientsView(page)).not.toContainText('Traceback')

  mode = 'network'
  await searchBox(page).fill('anj')
  await expect(patientsView(page).getByRole('alert')).toContainText('AyurCore can')

  mode = 'expired'
  await searchBox(page).fill('anja')
  await expect(page.getByText('Your session has ended. Sign in again to carry on — your notes are kept.')).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Username' })).toBeVisible()
})

// --- selection + history ------------------------------------------------------------

test('selecting a patient opens their history newest first, with dosha, herbs, quantity, date and status', async ({ page }) => {
  const calls = await mockApi(page, 'DOCTOR', (c) => {
    if (c.path === '/patients') return { body: { results: [ANJALI, ANJANA], truncated: false } }
    if (c.path === '/patients/5/prescriptions') {
      return c.params.get('offset') === '0'
        ? { body: history(ANJALI, [
            rx(12, '2026-09-20 10:15:00', 'Vata-Kapha', [{ name: 'Ashwagandha', grams: 7 }, { name: 'Brahmi', grams: 15 }]),
            rx(7, '2026-08-02 09:00:00', 'Pitta', [{ name: 'Triphala', grams: 10 }]),
          ], { has_more: true }) }
        : { body: history(ANJALI, [rx(3, '2026-07-01 08:30:00', null, null)], { offset: 2 }) }
    }
    return undefined
  })
  await signIn(page)
  await openPatients(page)
  await expect(page.getByText('Select a patient to see their prescription history.')).toBeVisible()

  await searchBox(page).fill('an')
  await page.getByRole('button', { name: /Anjali Nair/ }).click()
  await expect(page.getByRole('button', { name: /Anjali Nair/ })).toHaveAttribute('aria-pressed', 'true')

  const record = page.locator('.patient-record')
  await expect(record.getByRole('heading', { name: 'Anjali Nair' })).toBeVisible()
  await expect(record).toContainText('ID OP-1001')

  const items = page.getByRole('list', { name: 'Prescriptions for Anjali Nair' }).locator(':scope > li')
  await expect(items).toHaveCount(2)
  await expect(items.nth(0)).toContainText('#12')
  await expect(items.nth(0)).toContainText('Vata-Kapha')
  await expect(items.nth(0)).toContainText('Ashwagandha')
  await expect(items.nth(0)).toContainText('7 g')
  await expect(items.nth(0)).toContainText('Brahmi')
  await expect(items.nth(0)).toContainText('15 g')
  await expect(items.nth(0)).toContainText('Active')
  await expect(items.nth(0)).toContainText('2026')
  await expect(items.nth(1)).toContainText('#7')
  await expect(items.nth(1)).toContainText('Pitta')

  const first = patientCalls(calls, '/patients/5/prescriptions')[0]
  expect(first.params.get('limit')).toBe('20')
  expect(first.params.get('offset')).toBe('0')
  expect(first.auth).toBe(`Bearer ${TOKEN}`)

  await page.getByRole('button', { name: 'Load older prescriptions' }).click()
  await expect(items).toHaveCount(3)
  await expect(items.nth(2)).toContainText('#3')
  await expect(items.nth(2)).toContainText('Details for this record couldn’t be displayed.')
  expect(patientCalls(calls, '/patients/5/prescriptions').at(-1)?.params.get('offset')).toBe('2')
  await expect(page.getByRole('button', { name: 'Load older prescriptions' })).toHaveCount(0)
  await expect(patientsView(page)).not.toContainText(CLINIC)
})

test('a patient with no linked prescriptions gets a clear empty state', async ({ page }) => {
  await mockApi(page, 'ADMIN', (c) => {
    if (c.path === '/patients') return { body: { results: [ANJANA], truncated: false } }
    if (c.path === '/patients/6/prescriptions') return { body: history(ANJANA, []) }
    return undefined
  })
  await signIn(page)
  await openPatients(page)
  await searchBox(page).fill('anjana')
  await page.getByRole('button', { name: /Anjana Rao/ }).click()
  await expect(page.getByText('No confirmed prescriptions for this patient yet.')).toBeVisible()
})

test('history errors are friendly, retryable, and a missing patient says so', async ({ page }) => {
  let historyCalls = 0
  await mockApi(page, 'DOCTOR', (c) => {
    if (c.path === '/patients') return { body: { results: [ANJALI, ANJANA], truncated: false } }
    if (c.path === '/patients/5/prescriptions') {
      historyCalls += 1
      return historyCalls === 1
        ? { status: 500, body: { detail: 'internal' } }
        : { body: history(ANJALI, [rx(12, '2026-09-20 10:15:00', 'Vata', [{ name: 'Ashwagandha', grams: 7 }])]) }
    }
    if (c.path === '/patients/6/prescriptions') return { status: 404, body: { detail: 'Patient not found.' } }
    return undefined
  })
  await signIn(page)
  await openPatients(page)
  await searchBox(page).fill('an')

  await page.getByRole('button', { name: /Anjali Nair/ }).click()
  await expect(patientsView(page).getByRole('alert')).toHaveText('Something went wrong on our side and nothing was saved. Try once more.')
  await page.getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByRole('list', { name: 'Prescriptions for Anjali Nair' }).locator(':scope > li')).toHaveCount(1)

  await page.getByRole('button', { name: /Anjana Rao/ }).click()
  await expect(patientsView(page).getByRole('alert')).toHaveText("That patient record isn't available. Search again and pick the patient from the list.")
})

// --- registration -------------------------------------------------------------------

test('registration validates like the backend and sends no request when invalid', async ({ page }) => {
  const calls = await mockApi(page, 'DOCTOR', () => undefined)
  await signIn(page)
  await openPatients(page)
  await page.getByRole('button', { name: 'Register patient' }).click()
  const form = page.getByRole('form', { name: 'Register a patient' })

  await form.getByRole('button', { name: 'Register patient' }).click()
  await expect(form.getByText('Enter the clinic patient ID (OP / registration number).')).toBeVisible()
  await expect(form.getByText("Enter the patient's name.")).toBeVisible()

  await form.getByLabel(/Clinic patient ID/).fill('x'.repeat(51))
  await form.getByLabel('Patient name').fill('   ')
  await form.getByRole('button', { name: 'Register patient' }).click()
  await expect(form.getByText('Patient ID can be at most 50 characters.')).toBeVisible()
  await expect(form.getByText("Enter the patient's name.")).toBeVisible()
  expect(calls.filter((c) => c.method === 'POST' && c.path === '/patients')).toHaveLength(0)

  await form.getByRole('button', { name: 'Cancel' }).click()
  await expect(searchBox(page)).toBeVisible()
})

test('successful registration sends a trimmed body without clinic_id and selects the new patient', async ({ page }) => {
  const created = { patient_pk: 77, clinic_patient_id: 'OP-77', display_name: 'Meera Pillai', phone: '98470', created_at: '2026-09-26 12:00:00' }
  const calls = await mockApi(page, 'DOCTOR', (c) => {
    if (c.method === 'POST' && c.path === '/patients') return { status: 201, body: created }
    if (c.path === '/patients') return { body: { results: [{ patient_pk: 77, clinic_patient_id: 'OP-77', display_name: 'Meera Pillai' }], truncated: false } }
    if (c.path === '/patients/77/prescriptions') return { body: history({ patient_pk: 77, clinic_patient_id: 'OP-77', display_name: 'Meera Pillai' }, []) }
    return undefined
  })
  await signIn(page)
  await openPatients(page)
  await page.getByRole('button', { name: 'Register patient' }).click()
  const form = page.getByRole('form', { name: 'Register a patient' })
  await form.getByLabel(/Clinic patient ID/).fill('  OP-77 ')
  await form.getByLabel('Patient name').fill(' Meera Pillai  ')
  await form.getByLabel('Phone (optional)').fill(' 98470 ')
  await form.getByRole('button', { name: 'Register patient' }).click()

  await expect(page.locator('.patient-record').getByRole('heading', { name: 'Meera Pillai' })).toBeVisible()
  await expect(page.locator('.patient-record')).toContainText('ID OP-77')
  await expect(searchBox(page)).toHaveValue('OP-77')
  await expect(page.getByText('No confirmed prescriptions for this patient yet.')).toBeVisible()

  const post = calls.find((c) => c.method === 'POST' && c.path === '/patients')
  expect(post?.body).toEqual({ clinic_patient_id: 'OP-77', display_name: 'Meera Pillai', phone: '98470' })
  expect(post?.auth).toBe(`Bearer ${TOKEN}`)
  expect(calls.some((c) => c.path === '/patients/77/prescriptions')).toBe(true)
  await expect(patientsView(page)).not.toContainText('98470')
  await expect(patientsView(page)).not.toContainText(CLINIC)
})

test('a duplicate patient ID shows a clear message and keeps the form', async ({ page }) => {
  await mockApi(page, 'ADMIN', (c) => (c.method === 'POST' && c.path === '/patients'
    ? { status: 409, body: { detail: 'A patient with this ID is already registered in this clinic.' } }
    : undefined))
  await signIn(page)
  await openPatients(page)
  await page.getByRole('button', { name: 'Register patient' }).click()
  const form = page.getByRole('form', { name: 'Register a patient' })
  await form.getByLabel(/Clinic patient ID/).fill('OP-1001')
  await form.getByLabel('Patient name').fill('Someone')
  await form.getByRole('button', { name: 'Register patient' }).click()
  await expect(form.getByRole('alert')).toHaveText('A patient with this ID is already registered in this clinic. Search for them instead.')
  await expect(form.getByLabel(/Clinic patient ID/)).toHaveValue('OP-1001')
})

test('registering from a no-match search prefills what was typed', async ({ page }) => {
  await mockApi(page, 'DOCTOR', (c) => (c.path === '/patients' ? { body: { results: [], truncated: false } } : undefined))
  await signIn(page)
  await openPatients(page)
  await searchBox(page).fill('Meera')
  await page.getByRole('button', { name: 'Register a new patient' }).click()
  await expect(page.getByLabel('Patient name')).toHaveValue('Meera')
  await expect(page.getByLabel(/Clinic patient ID/)).toHaveValue('')
})

// --- mobile ---------------------------------------------------------------------------

test.describe('mobile', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('results and history stack without horizontal scrolling', async ({ page }) => {
    await mockApi(page, 'DOCTOR', (c) => {
      if (c.path === '/patients') return { body: { results: [ANJALI, ANJANA], truncated: false } }
      if (c.path === '/patients/5/prescriptions') {
        return { body: history(ANJALI, [rx(12, '2026-09-20 10:15:00', 'Vata-Kapha', [{ name: 'Ashwagandha', grams: 7 }])]) }
      }
      return undefined
    })
    await signIn(page)
    await openPatients(page)
    await searchBox(page).fill('an')
    await page.getByRole('button', { name: /Anjali Nair/ }).click()
    await expect(page.locator('.history-item')).toHaveCount(1)

    const resultsBox = await page.locator('.patients-search').boundingBox()
    const historyBox = await page.locator('.patients-history').boundingBox()
    expect(resultsBox && historyBox && historyBox.y >= resultsBox.y + resultsBox.height - 1).toBeTruthy()
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow).toBeLessThanOrEqual(0)
  })
})
