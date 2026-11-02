import { expect, test, type Page, type Route } from '@playwright/test'
import { API_ORIGIN } from '../playwright.config'

type Role = 'DOCTOR' | 'ADMIN' | 'AUDITOR'
type Call = { method: string; path: string; params: URLSearchParams; auth?: string; body: unknown }
type Reply = { status?: number; body?: unknown } | 'network-error' | undefined
type Handler = (call: Call) => Reply | Promise<Reply>

const TOKEN = 'test-session-token'
const CLINIC = 'fixture_clinic'
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
}

async function mockApi(page: Page, role: Role, handler: Handler): Promise<Call[]> {
  const calls: Call[] = []
  await page.route(/^http:\/\/127\.0\.0\.1:(8000|8001)\/.*/, async (route: Route) => {
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

test('typing patient name in Consultation brings up matching patients and auto-fills ID on selection', async ({ page }) => {
  await mockApi(page, 'DOCTOR', (c) => {
    if (c.path === '/patients') {
      const q = c.params.get('q') || ''
      if (q.toLowerCase().includes('aarav')) {
        return {
          body: {
            results: [
              {
                patient_pk: 1,
                clinic_patient_id: 'DE-0157',
                display_name: 'Aarav Sharma (synthetic)',
              },
            ],
            truncated: false,
          },
        }
      }
      return { body: { results: [], truncated: false } }
    }
    if (c.path === '/inventory') {
      return { body: { items: [], total_count: 0 } }
    }
    return { status: 200, body: {} }
  })

  await signIn(page)

  // Navigate to consultation
  await page.getByRole('button', { name: /Start a consultation/ }).click()

  // Find the Patient name input
  const nameInput = page.getByPlaceholder(/Search or enter patient name/)
  await expect(nameInput).toBeVisible()

  // Type "Aarav"
  await nameInput.fill('Aarav')

  // The autocomplete dropdown should appear
  const dropdown = page.locator('.patient-autocomplete-dropdown')
  await expect(dropdown).toBeVisible()
  await expect(dropdown).toContainText('Aarav Sharma (synthetic)')
  await expect(dropdown).toContainText('DE-0157')

  // Click on the matching patient button
  await dropdown.locator('button.patient-result').first().click()

  // Name and ID inputs should now be filled
  await expect(nameInput).toHaveValue('Aarav Sharma (synthetic)')
  const idInput = page.getByPlaceholder('e.g. AC-1048 or DE-0157')
  await expect(idInput).toHaveValue('DE-0157')

  // Dropdown should be dismissed
  await expect(dropdown).not.toBeVisible()
})
