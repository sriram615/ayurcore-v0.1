/**
 * Dashboard -- browser tests against a mocked API.
 *
 * The dashboard may only show what the existing endpoints return (GET /inventory, GET /audit/export, GET /audit/events), may only ask
 * for the sources its role is allowed to read, and must say so -- not invent a number -- where no source exists. Every request to
 * API_ORIGIN is intercepted and recorded; the fixtures below live only in this file.
 */
import { expect, test, type Page, type Route } from '@playwright/test'

import { API_ORIGIN } from '../playwright.config'

type Role = 'DOCTOR' | 'ADMIN' | 'AUDITOR'
type Call = { method: string; path: string; auth?: string; body: unknown }
type Reply = { status?: number; body?: unknown } | 'network-error' | undefined
type Handler = (call: Call) => Reply | Promise<Reply>

const TOKEN = 'test-session-token'
const CLINIC = 'fixture_clinic_do_not_display'
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
}

const STOCK = { items: [{ herb_name: 'Ashwagandha', stock_grams: 5000 }, { herb_name: 'Brahmi', stock_grams: 250 }, { herb_name: 'Neem', stock_grams: 0 }] }

function auditRecord(id: number, createdAt: string, dosha: string, changed: boolean) {
  return {
    prescription_id: id, clinic_id: CLINIC, created_at: createdAt, ai_dosha_imbalance: 'Vata', ai_prescribed_herbs: [{ name: 'Neem', grams: 5 }],
    confirmed_dosha_imbalance: dosha, confirmed_prescribed_herbs: [{ name: 'Neem', grams: 5 }], was_changed: changed,
  }
}
// six records, deliberately NOT in date order: the dashboard shows the five newest, newest first
const RECORDS = [
  auditRecord(11, '2026-09-11T09:00:00Z', 'Vata', false), auditRecord(16, '2026-09-16T09:00:00Z', 'Kapha', true), auditRecord(12, '2026-09-12T09:00:00Z', 'Pitta', false),
  auditRecord(15, '2026-09-15T09:00:00Z', 'Vata-Pitta', false), auditRecord(10, '2026-09-10T09:00:00Z', 'Pitta', true), auditRecord(14, '2026-09-14T09:00:00Z', 'Vata', false),
]
const EXPORT = { clinic_id: CLINIC, generated_at: '2026-09-20T00:00:00Z', record_count: 6, changed_count: 2, records: RECORDS, integrity_notes: [] }
function auditEvent(id: number, type: string, rx: number | null, createdAt: string) {
  return { event_id: id, clinic_id: CLINIC, actor_user_id: 3, event_type: type, prescription_id: rx, preview_id: 'p', event_data: {}, created_at: createdAt, previous_event_hash: null, event_hash: 'h' }
}
const EVENTS = {
  clinic_id: CLINIC, generated_at: '2026-09-20T00:00:00Z', event_count: 7, chain_valid: true, integrity_violations: [],
  events: [auditEvent(1, 'PRESCRIPTION_CONFIRMED', 10, '2026-09-10T09:00:00Z'), auditEvent(2, 'ENCOUNTER_OPENED', null, '2026-09-11T08:00:00Z'), auditEvent(3, 'PRESCRIPTION_CONFIRMED', 14, '2026-09-14T09:00:00Z'),
           auditEvent(4, 'ENCOUNTER_CLOSED', null, '2026-09-14T10:00:00Z'), auditEvent(5, 'PRESCRIPTION_CONFIRMED', 15, '2026-09-15T09:00:00Z'), auditEvent(6, 'PRESCRIPTION_CONFIRMED', 16, '2026-09-16T09:00:00Z'),
           auditEvent(7, 'ENCOUNTER_OPENED', null, '2026-09-17T08:00:00Z')],
}

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
    const call: Call = { method: req.method(), path: url.pathname, auth: req.headers()['authorization'], body }
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

const everything: Handler = (c) => {
  if (c.path === '/inventory') return { body: STOCK }
  if (c.path === '/audit/export') return { body: EXPORT }
  if (c.path === '/audit/events') return { body: EVENTS }
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

const dashboard = (page: Page) => page.locator('.dashboard')
const stat = (page: Page, label: string) => page.locator('.dash-stat', { has: page.locator('dt', { hasText: new RegExp(`^${label}$`) }) })
const paths = (calls: Call[]) => calls.map((c) => c.path).filter((p) => !p.startsWith('/auth'))

// --- landing + navigation ------------------------------------------------------------

for (const role of ['DOCTOR', 'ADMIN', 'AUDITOR'] as const) {
  test(`${role} lands on the Dashboard, which is the first and active navigation item`, async ({ page }) => {
    await mockApi(page, role, everything)
    await signIn(page)
    await expect(page.getByRole('heading', { name: 'Dashboard', level: 1 })).toBeVisible()
    const nav = page.locator('nav.side-nav')
    await expect(nav.getByRole('button').first()).toHaveText(/Dashboard/)
    await expect(nav.getByRole('button', { name: 'Dashboard' })).toHaveClass(/active/)
  })
}

test('the existing destinations are still offered after the Dashboard item', async ({ page }) => {
  await mockApi(page, 'DOCTOR', everything)
  await signIn(page)
  const names = await page.locator('nav.side-nav').getByRole('button').allInnerTexts()
  expect(names.map((n) => n.trim())).toEqual(['Dashboard', 'Consultation', 'Patients', 'Prescriptions', 'Inventory'])
})

// --- DOCTOR ---------------------------------------------------------------------------

test('DOCTOR: stock summary comes from GET /inventory only, with the session token, and no audit or patient call is made', async ({ page }) => {
  const calls = await mockApi(page, 'DOCTOR', everything)
  await signIn(page)
  await expect(stat(page, 'Herbs recorded')).toContainText('3')
  await expect(stat(page, 'In stock')).toContainText('2')
  await expect(stat(page, 'Out of stock')).toContainText('1')
  await expect(stat(page, 'Total in stock')).toContainText(/5,?250 g/)
  await expect(dashboard(page).locator('.dash-out')).toContainText('Neem')

  expect(paths(calls)).toEqual(['/inventory'])
  expect(calls.find((c) => c.path === '/inventory')?.auth).toBe(`Bearer ${TOKEN}`)
  expect(calls.every((c) => c.method === 'GET' || c.path.startsWith('/auth'))).toBe(true)
  await expect(dashboard(page)).not.toContainText(CLINIC)
})

test('DOCTOR: recent patients and AI activity say that no data source exists, and show no numbers', async ({ page }) => {
  await mockApi(page, 'DOCTOR', everything)
  await signIn(page)
  const patients = page.getByRole('region', { name: 'Recent patients' })
  await expect(patients).toContainText('No recent-patients list is available.')
  await expect(patients.getByRole('button', { name: /Find a patient/ })).toBeVisible()
  const ai = page.getByRole('region', { name: 'AI proposals and confirmations' })
  await expect(ai).toContainText('No AI activity summary is available to this account.')
  await expect(ai.locator('.dash-stat')).toHaveCount(0)
  await expect(dashboard(page)).not.toContainText(/NaN|undefined|null/)
})

test('DOCTOR: the header action and "Find a patient" go to the existing screens', async ({ page }) => {
  await mockApi(page, 'DOCTOR', everything)
  await signIn(page)
  await page.getByRole('button', { name: /Find a patient/ }).click()
  await expect(page.getByRole('heading', { name: 'Patients', level: 1 })).toBeVisible()
  await page.locator('nav.side-nav').getByRole('button', { name: 'Dashboard' }).click()
  await page.getByRole('button', { name: /Start a consultation/ }).click()
  await expect(page.getByRole('heading', { name: /Consultation|Clinical|Draft/i }).first()).toBeVisible()
})

test('DOCTOR: empty stock is a clear empty state, with no zero-filled summary', async ({ page }) => {
  await mockApi(page, 'DOCTOR', (c) => (c.path === '/inventory' ? { body: { items: [] } } : undefined))
  await signIn(page)
  await expect(dashboard(page)).toContainText('No stock is recorded for this clinic.')
  await expect(page.getByLabel('Stock summary')).toHaveCount(0)
})

test('DOCTOR: stock with nothing out of stock lists no out-of-stock herbs', async ({ page }) => {
  await mockApi(page, 'DOCTOR', (c) => (c.path === '/inventory' ? { body: { items: [{ herb_name: 'Brahmi', stock_grams: 10 }] } } : undefined))
  await signIn(page)
  await expect(stat(page, 'Out of stock')).toContainText('0')
  await expect(dashboard(page).locator('.dash-out')).toHaveCount(0)
})

test('a failed stock read shows a friendly message and retries without reloading the page', async ({ page }) => {
  let fail = true
  const calls = await mockApi(page, 'DOCTOR', (c) => {
    if (c.path !== '/inventory') return undefined
    return fail ? { status: 500, body: { detail: 'Traceback: sqlite3.OperationalError at /db/ayurcore.db' } } : { body: STOCK }
  })
  await signIn(page)
  await expect(dashboard(page).getByRole('alert')).toHaveText("This summary couldn't be loaded. Try again.")
  await expect(dashboard(page)).not.toContainText('Traceback')
  fail = false
  await dashboard(page).getByRole('button', { name: 'Try again' }).click()
  await expect(stat(page, 'Herbs recorded')).toContainText('3')
  await expect(dashboard(page).getByRole('alert')).toHaveCount(0)
  expect(calls.filter((c) => c.path === '/inventory')).toHaveLength(2)
})

test('an unreachable backend says so, and an expired session returns to sign-in', async ({ page }) => {
  let mode: 'network' | 'expired' = 'network'
  await mockApi(page, 'DOCTOR', (c) => (c.path !== '/inventory' ? undefined : mode === 'network' ? 'network-error' : { status: 401, body: { detail: 'Invalid, expired, or revoked session.' } }))
  await signIn(page)
  await expect(dashboard(page).getByRole('alert')).toContainText('AyurCore can')
  mode = 'expired'
  await dashboard(page).getByRole('button', { name: 'Try again' }).click()
  await expect(page.getByText('Your session has ended. Sign in again to carry on — your notes are kept.')).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Username' })).toBeVisible()
})

test('a role that may not read stock gets a friendly access message, not a backend detail', async ({ page }) => {
  await mockApi(page, 'DOCTOR', (c) => (c.path === '/inventory' ? { status: 403, body: { detail: 'Your role is not permitted to perform this action.' } } : undefined))
  await signIn(page)
  await expect(dashboard(page).getByRole('alert')).toContainText("This account can't perform that action.")
})

// --- ADMIN ----------------------------------------------------------------------------

test('ADMIN: summary cards use the counts the API returns, and read exactly the three permitted sources', async ({ page }) => {
  const calls = await mockApi(page, 'ADMIN', everything)
  await signIn(page)
  await expect(stat(page, 'Confirmed prescriptions')).toContainText('6')
  await expect(stat(page, 'Changed from the AI’s proposal')).toContainText('2')
  await expect(stat(page, 'Audit events')).toContainText('7')
  await expect(stat(page, 'Event chain')).toContainText('Intact')
  await expect(stat(page, 'Herbs recorded')).toContainText('3')
  expect([...new Set(paths(calls))].sort()).toEqual(['/audit/events', '/audit/export', '/inventory'])
  expect(calls.every((c) => c.method === 'GET' || c.path.startsWith('/auth'))).toBe(true)
})

test('ADMIN: recent prescriptions are the five newest, newest first, with no patient identity', async ({ page }) => {
  await mockApi(page, 'ADMIN', everything)
  await signIn(page)
  const rows = page.getByRole('table', { name: 'Recent prescriptions' }).locator('tbody tr')
  await expect(rows).toHaveCount(5)
  await expect(rows.nth(0)).toContainText('#16')
  await expect(rows.nth(0)).toContainText('Kapha')
  await expect(rows.nth(0)).toContainText('Yes')
  await expect(rows.nth(4)).toContainText('#11')
  await expect(page.getByRole('table', { name: 'Recent prescriptions' })).not.toContainText('#10')
  await expect(dashboard(page)).not.toContainText(/patient name|phone/i)
})

test('ADMIN: recent activity is readable, newest first, and a missing prescription is a dash', async ({ page }) => {
  await mockApi(page, 'ADMIN', everything)
  await signIn(page)
  const rows = page.getByRole('table', { name: 'Recent activity' }).locator('tbody tr')
  await expect(rows).toHaveCount(5)
  await expect(rows.nth(0)).toContainText('Encounter opened')
  await expect(rows.nth(0)).toContainText('—')
  await expect(rows.nth(1)).toContainText('Prescription confirmed')
  await expect(rows.nth(1)).toContainText('#16')
  await expect(dashboard(page)).not.toContainText('PRESCRIPTION_CONFIRMED')
})

test('ADMIN: a broken event chain is reported, not hidden', async ({ page }) => {
  await mockApi(page, 'ADMIN', (c) => (c.path === '/audit/events' ? { body: { ...EVENTS, chain_valid: false, integrity_violations: ['event 4 hash mismatch'] } } : everything(c)))
  await signIn(page)
  await expect(stat(page, 'Event chain')).toContainText('Violation')
  await expect(dashboard(page).getByRole('alert')).toContainText('event 4 hash mismatch')
})

test('ADMIN: with no records the panels say so and nothing is divided or zero-filled into a statistic', async ({ page }) => {
  await mockApi(page, 'ADMIN', (c) => {
    if (c.path === '/audit/export') return { body: { ...EXPORT, record_count: 0, changed_count: 0, records: [] } }
    if (c.path === '/audit/events') return { body: { ...EVENTS, event_count: 0, events: [] } }
    return c.path === '/inventory' ? { body: { items: [] } } : undefined
  })
  await signIn(page)
  await expect(dashboard(page)).toContainText('No confirmed prescriptions yet.')
  await expect(dashboard(page)).toContainText('No activity has been recorded yet.')
  await expect(dashboard(page)).not.toContainText(/NaN|Infinity|%/)
})

test('ADMIN: one failing source does not hide the others, and only that panel offers a retry', async ({ page }) => {
  await mockApi(page, 'ADMIN', (c) => (c.path === '/audit/events' ? { status: 500, body: { detail: 'boom' } } : everything(c)))
  await signIn(page)
  await expect(stat(page, 'Confirmed prescriptions')).toContainText('6')
  await expect(stat(page, 'Herbs recorded')).toContainText('3')
  await expect(page.getByRole('table', { name: 'Recent prescriptions' })).toBeVisible()
  const events = page.getByRole('region', { name: 'Recent activity' })
  await expect(events.getByRole('alert')).toHaveText("This summary couldn't be loaded. Try again.")
  await expect(events.getByRole('button', { name: 'Try again' })).toBeVisible()
  await expect(dashboard(page).getByRole('button', { name: 'Try again' })).toHaveCount(1)
})

// --- AUDITOR --------------------------------------------------------------------------

test('AUDITOR: reads only the audit sources, shows no stock or patient panel, and the audit action opens the Audit screen', async ({ page }) => {
  const calls = await mockApi(page, 'AUDITOR', everything)
  await signIn(page)
  await expect(stat(page, 'Confirmed prescriptions')).toContainText('6')
  expect([...new Set(paths(calls))].sort()).toEqual(['/audit/events', '/audit/export'])
  await expect(page.getByRole('region', { name: 'Clinic stock' })).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'Recent patients' })).toHaveCount(0)
  await expect(page.getByRole('region', { name: 'AI proposals and confirmations' })).toContainText('Confirmed prescriptions')
  await page.getByRole('button', { name: /Open the audit trail/ }).click()
  await expect(page.getByRole('heading', { name: 'Audit trail', level: 1 })).toBeVisible()
})

// --- accessibility + layout -----------------------------------------------------------

test('landmarks and names: one h1, labelled regions, labelled tables and summary lists', async ({ page }) => {
  await mockApi(page, 'ADMIN', everything)
  await signIn(page)
  await expect(page.getByRole('heading', { level: 1 })).toHaveCount(1)
  for (const name of ['Clinic stock', 'AI proposals and confirmations', 'Recent prescriptions', 'Recent activity', 'Recent patients']) {
    await expect(page.getByRole('region', { name })).toBeVisible()
  }
  await expect(page.getByLabel('Stock summary')).toBeVisible()
  await expect(page.getByLabel('Audit summary')).toBeVisible()
  for (const button of await dashboard(page).getByRole('button').all()) await expect(button).toHaveAccessibleName(/\S/)
})

test('loading is announced as a status while a source is pending', async ({ page }) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await mockApi(page, 'DOCTOR', async (c) => {
    if (c.path !== '/inventory') return undefined
    await gate
    return { body: STOCK }
  })
  await signIn(page)
  await expect(dashboard(page).getByRole('status').filter({ hasText: 'Loading…' })).toBeVisible()
  release()
  await expect(stat(page, 'Herbs recorded')).toContainText('3')
})

test('the dashboard makes no request on its own beyond its sources, and none to patient or prescribing endpoints', async ({ page }) => {
  const calls = await mockApi(page, 'ADMIN', everything)
  await signIn(page)
  await expect(stat(page, 'Confirmed prescriptions')).toBeVisible()
  expect(calls.filter((c) => c.path.startsWith('/patients') || c.path.startsWith('/prescribe'))).toHaveLength(0)
})

test.describe('mobile', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('summary cards and panels stack without horizontal scrolling', async ({ page }) => {
    await mockApi(page, 'ADMIN', everything)
    await signIn(page)
    await expect(stat(page, 'Confirmed prescriptions')).toBeVisible()
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow).toBeLessThanOrEqual(0)
    const rx = await page.getByRole('region', { name: 'Recent prescriptions' }).boundingBox()
    const ev = await page.getByRole('region', { name: 'Recent activity' }).boundingBox()
    expect(rx && ev && ev.y >= rx.y + rx.height - 1).toBeTruthy()
    const first = await stat(page, 'Confirmed prescriptions').boundingBox()
    const second = await stat(page, 'Changed from the AI’s proposal').boundingBox()
    expect(first && second && Math.abs(first.y - second.y) < 2).toBeTruthy()   // two cards per row on a phone
  })
})
