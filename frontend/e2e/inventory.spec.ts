/**
 * Inventory page -- browser tests against a mocked API.
 *
 * The page shows exactly what GET /inventory returns (herb, grams) with a status that depends only on grams > 0, is read-only, and says
 * so plainly when loading, failing, empty or signed out. The fixtures below live only in this file; they are test data for the mocked
 * API, never shown by the application unless a test serves them.
 */
import { expect, test, type Page, type Route } from '@playwright/test'

import { API_ORIGIN } from '../playwright.config'

type Role = 'DOCTOR' | 'ADMIN' | 'AUDITOR'
type Call = { method: string; path: string; auth?: string; body: unknown }
type Reply = { status?: number; body?: unknown } | 'network-error' | undefined
type Handler = (call: Call) => Reply | Promise<Reply>

const TOKEN = 'test-session-token'
const CORS = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key', 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS' }
const STOCK = { items: [{ herb_name: 'Ashwagandha', stock_grams: 12500 }, { herb_name: 'Brahmi', stock_grams: 1 }, { herb_name: 'Neem', stock_grams: 0 }] }

async function mockApi(page: Page, role: Role, handler: Handler): Promise<Call[]> {
  const calls: Call[] = []
  await page.route((url) => url.origin !== new URL(API_ORIGIN).origin && !['127.0.0.1', 'localhost'].includes(url.hostname), (route) => route.abort('blockedbyclient'))
  await page.route(`${API_ORIGIN}/**`, async (route: Route) => {
    const req = route.request()
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    const url = new URL(req.url())
    let body: unknown = null
    try { body = req.postDataJSON() } catch { body = req.postData() }
    const call: Call = { method: req.method(), path: url.pathname, auth: req.headers()['authorization'], body }
    calls.push(call)
    if (url.pathname === '/auth/login') return route.fulfill({ status: 200, headers: CORS, json: { token: TOKEN, expires_at: '2099-01-01T00:00:00Z', username: 'test_user', clinic_id: 'fixture_clinic_do_not_display', role } })
    if (url.pathname === '/auth/logout') return route.fulfill({ status: 200, headers: CORS, json: { status: 'logged_out' } })
    const reply = await handler(call)
    if (reply === 'network-error') return route.abort('failed')
    if (!reply) return route.fulfill({ status: 599, headers: CORS, json: { detail: `unexpected request in test: ${call.method} ${url.pathname}` } })
    return route.fulfill({ status: reply.status ?? 200, headers: CORS, json: reply.body })
  })
  return calls
}

/** The Dashboard also reads /inventory on landing; the audit sources are only asked for by ADMIN / AUDITOR. */
const serve = (stock: () => Reply | Promise<Reply>): Handler => (c) => (c.path === '/inventory' ? stock() : undefined)

async function openInventory(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: /Enter AyurCore/ }).click()
  await page.getByRole('textbox', { name: 'Username' }).fill('test_user')
  await page.getByRole('textbox', { name: 'Password' }).fill('not-a-real-password')
  await page.locator('form').getByRole('button', { name: /Sign in/ }).click()
  await expect(page.locator('.app-shell')).toBeVisible()
  const menu = page.getByRole('button', { name: 'Open navigation' })
  if (await menu.isVisible()) await menu.click()
  await page.locator('nav.side-nav').getByRole('button', { name: 'Inventory' }).click()
  await expect(page.getByRole('heading', { name: 'Inventory', level: 1 })).toBeVisible()
}

const rows = (page: Page) => page.getByRole('list', { name: 'Stock by herb' }).getByRole('listitem')
const inventoryCalls = (calls: Call[]) => calls.filter((c) => c.path === '/inventory')

for (const role of ['DOCTOR', 'ADMIN'] as const) {
  test(`${role}: every returned herb is listed with its actual grams and a status from grams > 0 only`, async ({ page }) => {
    const calls = await mockApi(page, role, serve(() => ({ body: STOCK })))
    await openInventory(page)
    await expect(rows(page)).toHaveCount(3)
    await expect(rows(page).nth(0)).toContainText('Ashwagandha')
    await expect(rows(page).nth(0)).toContainText('12,500')
    await expect(rows(page).nth(0)).toContainText('g')
    await expect(rows(page).nth(0)).toContainText('In stock')
    await expect(rows(page).nth(1)).toContainText('Brahmi')
    await expect(rows(page).nth(1)).toContainText('In stock')               // 1 g is still stock: no invented low-stock threshold
    await expect(rows(page).nth(1).locator('.stock-amount strong')).toHaveText('1')
    await expect(rows(page).nth(2)).toContainText('Neem')
    await expect(rows(page).nth(2).locator('.stock-amount strong')).toHaveText('0')
    await expect(rows(page).nth(2)).toContainText('Out of stock')
    await expect(rows(page).nth(2).locator('.inventory-status')).toHaveClass(/out-of-stock/)
    await expect(page.getByText(/isn't available here yet|read-only endpoint|integration report/i)).toHaveCount(0)    // the stale notice is gone
    expect(inventoryCalls(calls).every((c) => c.method === 'GET' && c.auth === `Bearer ${TOKEN}`)).toBe(true)
    expect(calls.filter((c) => !['GET', 'OPTIONS'].includes(c.method) && !c.path.startsWith('/auth'))).toHaveLength(0)    // read-only: no write request at all
    await expect(page.locator('body')).not.toContainText('fixture_clinic_do_not_display')
  })
}

test('the page is read-only: no form field and no button other than navigation and the account menu', async ({ page }) => {
  await mockApi(page, 'DOCTOR', serve(() => ({ body: STOCK })))
  await openInventory(page)
  await expect(rows(page)).toHaveCount(3)
  await expect(page.locator('.content-wrap').locator('input, textarea, select, button')).toHaveCount(0)
})

test('while loading, a status is shown and no rows', async ({ page }) => {
  let release: () => void = () => {}
  const gate = new Promise<void>((resolve) => { release = resolve })
  await mockApi(page, 'DOCTOR', (c) => (c.path === '/inventory' ? gate.then(() => ({ body: STOCK })) : undefined))
  await openInventory(page)
  await expect(page.locator('.content-wrap').getByRole('status').filter({ hasText: 'Loading…' })).toBeVisible()
  await expect(rows(page)).toHaveCount(0)
  release()
  await expect(rows(page)).toHaveCount(3)
})

test('an empty stock list says so and shows no rows', async ({ page }) => {
  await mockApi(page, 'DOCTOR', serve(() => ({ body: { items: [] } })))
  await openInventory(page)
  await expect(page.locator('.content-wrap').getByText('No stock is recorded for this clinic.')).toBeVisible()
  await expect(rows(page)).toHaveCount(0)
})

for (const [label, failure] of [['a server error', { status: 500, body: { detail: 'Traceback: sqlite3.OperationalError /db/x' } }], ['an unreachable server', 'network-error' as const]] as const) {
  test(`${label} shows one friendly message (never a backend detail) and Try again recovers`, async ({ page }) => {
    let fail = true
    const calls = await mockApi(page, 'DOCTOR', serve(() => (fail ? failure : { body: STOCK })))
    await openInventory(page)
    await expect(page.locator('.content-wrap').getByRole('alert')).toBeVisible()
    await expect(rows(page)).toHaveCount(0)
    await expect(page.locator('body')).not.toContainText(/Traceback|sqlite3/)
    const before = inventoryCalls(calls).length
    fail = false
    await page.locator('.content-wrap').getByRole('button', { name: /Try again/ }).click()
    await expect(rows(page)).toHaveCount(3)
    await expect(page.locator('.content-wrap').getByRole('alert')).toHaveCount(0)
    expect(inventoryCalls(calls).length).toBe(before + 1)
  })
}

test('a 403 (an account the backend does not allow) is shown as an error, with no rows and no stock', async ({ page }) => {
  await mockApi(page, 'DOCTOR', serve(() => ({ status: 403, body: { detail: 'forbidden' } })))
  await openInventory(page)
  await expect(page.locator('.content-wrap').getByRole('alert')).toBeVisible()
  await expect(rows(page)).toHaveCount(0)
})

test('a 401 ends the session and returns to sign-in', async ({ page }) => {
  await mockApi(page, 'DOCTOR', (c) => (c.path === '/inventory' ? { status: 401, body: { detail: 'Invalid, expired, or revoked session.' } } : undefined))
  await page.goto('/')
  await page.getByRole('button', { name: /Enter AyurCore/ }).click()
  await page.getByRole('textbox', { name: 'Username' }).fill('test_user')
  await page.getByRole('textbox', { name: 'Password' }).fill('not-a-real-password')
  await page.locator('form').getByRole('button', { name: /Sign in/ }).click()
  await expect(page.getByText('Your session has ended. Sign in again to carry on — your notes are kept.')).toBeVisible()
  await expect(page.getByRole('textbox', { name: 'Username' })).toBeVisible()
})

test.describe('mobile', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('rows fit at 390px with a very long herb name and a large quantity', async ({ page }) => {
    const LONG = 'Ashwagandha_Churna_Extra_Long_Herb_Name_Without_Any_Break_'.repeat(2)
    await mockApi(page, 'DOCTOR', serve(() => ({ body: { items: [{ herb_name: LONG, stock_grams: 123456789 }, ...STOCK.items] } })))
    await openInventory(page)
    await expect(rows(page)).toHaveCount(4)
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0)
    await expect(rows(page).first()).toContainText('123,456,789')
  })
})
