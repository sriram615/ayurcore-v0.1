/**
 * E2E tests for the Dispense -> Payment -> Invoice clinic workflow.
 *
 * Verifies:
 * 1. UI displays dispense status (pending vs dispensed) and "Dispense" button.
 * 2. Clicking "Dispense" calls POST /prescriptions/{id}/dispense and updates the badge.
 * 3. Clicking "Record payment" opens modal, validates fields, and records payment.
 * 4. Clicking "Invoice" fetches GET /prescriptions/{id}/invoice and displays printable receipt.
 */

import { expect, test, type Page, type Route } from '@playwright/test'
import { API_ORIGIN } from '../playwright.config'

type Role = 'DOCTOR' | 'ADMIN' | 'AUDITOR'
type Call = { method: string; path: string; params: URLSearchParams; auth?: string; body: unknown }
type Reply = { status?: number; body?: unknown } | undefined
type Handler = (call: Call) => Reply | Promise<Reply>

const TOKEN = 'test-token-doctor-123'
const CLINIC = 'fixture_clinic_do_not_display'
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
}

const PATIENT = { patient_pk: 7, clinic_patient_id: 'OP-1048', display_name: 'Devaki Amma' }

async function mockWorkflowApi(page: Page, role: Role, handler: Handler): Promise<Call[]> {
  const calls: Call[] = []
  await page.route(`${API_ORIGIN}/**`, async (route: Route) => {
    const req = route.request()
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    const url = new URL(req.url())
    let body: unknown = null
    try {
      body = req.postDataJSON()
    } catch {
      body = req.postData()
    }
    const call: Call = {
      method: req.method(),
      path: url.pathname,
      params: url.searchParams,
      auth: req.headers()['authorization'],
      body,
    }
    calls.push(call)

    if (url.pathname === '/auth/login') {
      return route.fulfill({
        status: 200,
        headers: CORS,
        json: {
          token: TOKEN,
          expires_at: '2099-01-01T00:00:00Z',
          username: 'dr_sreedharan',
          clinic_id: CLINIC,
          role,
        },
      })
    }
    if (url.pathname === '/auth/logout') {
      return route.fulfill({ status: 200, headers: CORS, json: { status: 'logged_out' } })
    }

    const reply = await handler(call)
    if (!reply) {
      return route.fulfill({
        status: 404,
        headers: CORS,
        json: { detail: `Unmocked path: ${call.method} ${url.pathname}` },
      })
    }
    return route.fulfill({ status: reply.status ?? 200, headers: CORS, json: reply.body })
  })
  return calls
}

async function signIn(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: /Enter AyurCore/ }).click()
  await page.getByRole('textbox', { name: 'Username' }).fill('dr_sreedharan')
  await page.getByRole('textbox', { name: 'Password' }).fill('password123')
  await page.locator('form').getByRole('button', { name: /Sign in/ }).click()
  await expect(page.locator('.app-shell')).toBeVisible()
}

async function openPatients(page: Page) {
  const menu = page.getByRole('button', { name: 'Open navigation' })
  if (await menu.isVisible()) await menu.click()
  await page.locator('nav.side-nav').getByRole('button', { name: 'Patients' }).click()
  await expect(page.getByRole('heading', { name: 'Patients', level: 1 })).toBeVisible()
}

test('Dispense, record payment, and view invoice full workflow in patient history', async ({ page }) => {
  let prescriptionState = {
    prescription_id: 101,
    created_at: '2026-11-01T10:00:00Z',
    status: 'Active',
    dosha_imbalance: 'Vata-Pitta',
    prescribed_herbs: [
      { name: 'Ashwagandha', grams: 60 },
      { name: 'Brahmi', grams: 30 },
    ],
    dispensed_at: null as string | null,
    dispensed_by_user_id: null as number | null,
    payment_status: 'unpaid',
    payment_amount_cents: null as number | null,
    payment_method: null as string | null,
    payment_reference: null as string | null,
    invoice_number: null as string | null,
  }

  const calls = await mockWorkflowApi(page, 'DOCTOR', (call) => {
    if (call.path === '/patients' && call.method === 'GET') {
      return { body: { results: [PATIENT], truncated: false } }
    }
    if (call.path === '/patients/7/prescriptions' && call.method === 'GET') {
      return {
        body: {
          patient: PATIENT,
          prescriptions: [prescriptionState],
          limit: 20,
          offset: 0,
          has_more: false,
        },
      }
    }
    if (call.path === '/patients/7/brief' && call.method === 'GET') {
      return {
        body: {
          patient_pk: 7,
          as_of: '2026-11-01T10:00:00Z',
          arm: 'learned',
          caveat: 'Demo',
          omitted: 0,
          rejected: 0,
          items: [],
        },
      }
    }
    if (call.path === '/prescriptions/101/dispense' && call.method === 'POST') {
      prescriptionState.dispensed_at = '2026-11-01T10:15:00Z'
      prescriptionState.dispensed_by_user_id = 42
      return {
        body: {
          prescription_id: 101,
          dispensed_at: '2026-11-01T10:15:00Z',
          dispensed_by_user_id: 42,
          status: 'dispensed',
        },
      }
    }
    if (call.path === '/prescriptions/101/payment' && call.method === 'POST') {
      const b = call.body as any
      prescriptionState.payment_status = b.payment_status
      prescriptionState.payment_amount_cents = b.payment_amount_cents
      prescriptionState.payment_method = b.payment_method
      prescriptionState.payment_reference = b.payment_reference
      prescriptionState.invoice_number = 'INV-4f8a12bc'
      return {
        body: {
          prescription_id: 101,
          payment_status: b.payment_status,
          payment_amount_cents: b.payment_amount_cents,
          payment_method: b.payment_method,
          payment_reference: b.payment_reference,
          invoice_number: 'INV-4f8a12bc',
        },
      }
    }
    if (call.path === '/prescriptions/101/invoice' && call.method === 'GET') {
      return {
        body: {
          prescription_id: 101,
          clinic_id: CLINIC,
          patient_clinic_patient_id: PATIENT.clinic_patient_id,
          patient_display_name: PATIENT.display_name,
          dosha_imbalance: prescriptionState.dosha_imbalance,
          prescribed_herbs: prescriptionState.prescribed_herbs,
          total_amount_cents: prescriptionState.payment_amount_cents || 45000,
          payment_status: prescriptionState.payment_status,
          payment_method: prescriptionState.payment_method,
          payment_reference: prescriptionState.payment_reference,
          invoice_number: prescriptionState.invoice_number,
          dispensed_at: prescriptionState.dispensed_at,
          created_at: prescriptionState.created_at,
        },
      }
    }
    return undefined
  })

  await signIn(page)
  await openPatients(page)

  // Search and select patient
  await page.getByRole('searchbox', { name: /Search patients/ }).fill('Devaki')
  await page.getByRole('button', { name: /Devaki Amma/ }).click()

  // 1. Check initial state: Pending dispense and Unpaid
  await expect(page.getByText('Pending dispense')).toBeVisible()
  await expect(page.getByText('Unpaid')).toBeVisible()
  const dispenseBtn = page.getByRole('button', { name: 'Dispense' })
  await expect(dispenseBtn).toBeVisible()

  // 2. Click "Dispense"
  await dispenseBtn.click()
  await expect(page.getByText('Dispensed')).toBeVisible()
  // Dispense button should disappear once dispensed
  await expect(page.getByRole('button', { name: 'Dispense' })).toHaveCount(0)

  // Verify POST /prescriptions/101/dispense was called
  const dispenseCall = calls.find((c) => c.path === '/prescriptions/101/dispense')
  expect(dispenseCall).toBeDefined()
  expect(dispenseCall?.method).toBe('POST')
  expect(dispenseCall?.auth).toBe(`Bearer ${TOKEN}`)

  // 3. Record payment
  await page.getByRole('button', { name: 'Record payment' }).click()
  const paymentModal = page.getByRole('dialog', { name: /Record Payment/i })
  await expect(paymentModal).toBeVisible()

  // Fill payment form
  await paymentModal.getByRole('spinbutton').fill('450')
  await paymentModal.getByRole('button', { name: 'Save Payment' }).click()

  // Verify modal closes and payment badge updates to Paid (₹450.00)
  await expect(paymentModal).not.toBeVisible()
  await expect(page.getByText(/Paid.*₹450\.00/i)).toBeVisible()
  await expect(page.getByText('INV-4f8a12bc')).toBeVisible()

  const paymentCall = calls.find((c) => c.path === '/prescriptions/101/payment')
  expect(paymentCall).toBeDefined()
  expect(paymentCall?.body).toMatchObject({
    payment_status: 'paid',
    payment_amount_cents: 45000,
  })

  // 4. View and print invoice
  await page.getByRole('button', { name: 'Invoice' }).click()
  const invoiceModal = page.getByRole('dialog', { name: /Prescription Invoice/i })
  await expect(invoiceModal).toBeVisible()

  await expect(invoiceModal.getByText('INV-4f8a12bc').first()).toBeVisible()
  await expect(invoiceModal.getByText('Devaki Amma')).toBeVisible()
  await expect(invoiceModal.getByText('OP-1048')).toBeVisible()
  await expect(invoiceModal.getByText('Ashwagandha')).toBeVisible()
  await expect(invoiceModal.getByText('Brahmi')).toBeVisible()
  await expect(invoiceModal.getByText('₹450.00')).toBeVisible()

  // Print button is present
  await expect(invoiceModal.getByRole('button', { name: /Print Invoice/i })).toBeVisible()

  // Close invoice dialog
  await invoiceModal.getByRole('button', { name: 'Close', exact: true }).click()
  await expect(invoiceModal).not.toBeVisible()
})
