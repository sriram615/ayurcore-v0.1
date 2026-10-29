/**
 * E2E tests for AI-1 Local Extractor and AI-3 Consultation Brief UI showcase.
 */
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

const ANJALI = { patient_pk: 5, clinic_patient_id: 'OP-1001', display_name: 'Anjali Nair' }

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

async function signIn(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: /Enter AyurCore/ }).click()
  await page.getByRole('textbox', { name: 'Username' }).fill('test_user')
  await page.getByRole('textbox', { name: 'Password' }).fill('not-a-real-password')
  await page.locator('form').getByRole('button', { name: /Sign in/ }).click()
  await expect(page.locator('.app-shell')).toBeVisible()
}

test('AI-1 Local Extractor drafts offline and showcases proposal source through confirmation', async ({ page }) => {
  const calls = await mockApi(page, 'DOCTOR', (c) => {
    if (c.path === '/prescribe/preview/local') {
      return {
        body: {
          preview_id: 'prev-local-123',
          patient_name: 'Anjali Nair',
          dosha_imbalance: 'Vata',
          prescribed_herbs: [{ name: 'Ashwagandha', grams: 10 }],
          proposal_source: 'local_extractor',
        },
      }
    }
    if (c.path === '/prescribe/confirm') {
      return {
        body: {
          prescription_id: 101,
          patient_name: 'Anjali Nair',
          dosha_imbalance: 'Vata',
          prescribed_herbs: [{ name: 'Ashwagandha', grams: 10 }],
          created_at: '2026-10-29T10:00:00Z',
          proposal_source: 'local_extractor',
        },
      }
    }
    return undefined
  })

  await signIn(page)

  // Navigate to consultation
  await page.getByRole('button', { name: /Start a consultation/ }).click()

  // Fill in consultation notes
  await page.getByPlaceholder('Search or enter patient name').fill('Anjali Nair')
  await page.getByPlaceholder('Describe the patient').fill('Patient presents with joint pain and insomnia. Prescribing Ashwagandha 10g.')

  // Both local and cloud draft options are clearly presented
  const localBtn = page.getByRole('button', { name: /Draft with Local AI \(Offline\)/ })
  const cloudBtn = page.getByRole('button', { name: /Draft with Cloud AI \(Gemini\)/ })
  await expect(localBtn).toBeVisible()
  await expect(cloudBtn).toBeVisible()

  // Click Draft with Local AI
  await localBtn.click()

  // Verify call hit /prescribe/preview/local
  const previewCall = calls.find((c) => c.path === '/prescribe/preview/local')
  expect(previewCall).toBeDefined()
  expect(previewCall?.method).toBe('POST')

  // Review screen shows the proposal source badge
  await expect(page.getByText('Local Extractor (Offline)')).toBeVisible()
  await expect(page.getByText('Ashwagandha')).toBeVisible()

  // Confirm prescription
  await page.getByRole('button', { name: 'confirm without sliding' }).click()

  // Verify confirmed sheet displays Local AI Extractor
  await expect(page.getByText('Prescription confirmed.')).toBeVisible()
  await expect(page.getByText('Local AI Extractor')).toBeVisible()
})

test('AI-3 Consultation Brief displays cited facts with salience ranking and toggles arm', async ({ page }) => {
  const calls = await mockApi(page, 'DOCTOR', (c) => {
    if (c.path === '/patients') {
      return { body: { results: [ANJALI], truncated: false } }
    }
    if (c.path === '/patients/5/prescriptions') {
      return {
        body: {
          patient: ANJALI,
          prescriptions: [
            {
              prescription_id: 12,
              created_at: '2026-09-20 10:15:00',
              status: 'Active',
              dosha_imbalance: 'Vata',
              prescribed_herbs: [{ name: 'Ashwagandha', grams: 10 }],
            },
          ],
          limit: 20,
          offset: 0,
          has_more: false,
        },
      }
    }
    if (c.path === '/patients/5/brief') {
      const arm = c.params.get('arm') || 'learned'
      return {
        body: {
          patient_pk: 5,
          as_of: '2026-10-29',
          arm,
          caveat: 'Deterministic facts verified against patient history. No diagnosis or treatment inference.',
          omitted: 0,
          rejected: 0,
          items: [
            {
              rank: 1,
              kind: 'dosha',
              text: 'Primary documented imbalance is Vata.',
              records: ['rx:12'],
              exact: true,
              score: 0.95,
            },
            {
              rank: 2,
              kind: 'herb',
              text: 'Previously prescribed Ashwagandha (10g).',
              records: ['rx:12'],
              exact: true,
              score: 0.88,
            },
          ],
        },
      }
    }
    return undefined
  })

  await signIn(page)

  // Navigate to patients
  await page.getByRole('button', { name: /Patients/ }).click()
  await page.getByRole('searchbox').fill('anjali')
  await page.getByRole('button', { name: /Anjali Nair/ }).click()

  // Brief card is visible
  const briefCard = page.getByRole('region', { name: 'Consultation brief' })
  await expect(briefCard).toBeVisible()
  await expect(briefCard.getByText('AI-3 BRIEF')).toBeVisible()

  // Click View brief
  await briefCard.getByRole('button', { name: /View brief/ }).click()

  // Verifies brief endpoint called with arm=learned
  const learnedCall = calls.find((c) => c.path === '/patients/5/brief' && c.params.get('arm') === 'learned')
  expect(learnedCall).toBeDefined()

  // Facts, citations, and salience pills are rendered
  await expect(briefCard.getByText('Primary documented imbalance is Vata.')).toBeVisible()
  await expect(briefCard.getByText('Previously prescribed Ashwagandha (10g).')).toBeVisible()
  await expect(briefCard.getByText('rx:12').first()).toBeVisible()
  await expect(briefCard.getByText('salience 95%')).toBeVisible()
  await expect(briefCard.getByText(/Deterministic facts verified against patient history/)).toBeVisible()

  // Switch to Template arm
  await briefCard.getByRole('button', { name: /Template/ }).click()

  // Verifies brief endpoint called with arm=template
  const templateCall = calls.find((c) => c.path === '/patients/5/brief' && c.params.get('arm') === 'template')
  expect(templateCall).toBeDefined()
  await expect(briefCard.getByText('arm: template')).toBeVisible()
})
