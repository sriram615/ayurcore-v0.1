/**
 * Ask about this patient -- how REAL backend replies read on screen, and how the panel behaves around them (browser, mocked API whose
 * replies are the captured real ones in e2e/fixtures/ask-real.ts): one time zone for answers and evidence, predictable keyboard focus,
 * 44px touch targets, no raw executor output anywhere, a labelled comparison table, and the explicit-uncertainty states.
 */
import { expect, test, type Page, type Route } from '@playwright/test'

import { API_ORIGIN } from '../playwright.config'
import { REAL, reply } from './fixtures/ask-real'

type Call = { method: string; path: string; body: unknown }
type Reply = { status?: number; body?: unknown } | undefined
type Handler = (call: Call) => Reply | Promise<Reply>

const TOKEN = 'test-session-token'
const CORS = { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': 'Authorization, Content-Type, Idempotency-Key', 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS' }
const ANJALI = { patient_pk: 5, clinic_patient_id: 'OP-1001', display_name: 'Anjali Nair' }
const RX = { prescription_id: 4, created_at: '2026-10-10 05:40:36', status: 'Active', dosha_imbalance: 'Kapha', prescribed_herbs: [{ name: 'Guggulu', grams: 12 }] }

async function mockApi(page: Page, handler: Handler): Promise<Call[]> {
  const calls: Call[] = []
  await page.route((url) => url.origin !== new URL(API_ORIGIN).origin && !['127.0.0.1', 'localhost'].includes(url.hostname), (route) => route.abort('blockedbyclient'))
  await page.route(`${API_ORIGIN}/**`, async (route: Route) => {
    const req = route.request()
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: CORS })
    const url = new URL(req.url())
    let body: unknown = null
    try { body = req.postDataJSON() } catch { body = req.postData() }
    calls.push({ method: req.method(), path: url.pathname, body })
    if (url.pathname === '/auth/login') return route.fulfill({ status: 200, headers: CORS, json: { token: TOKEN, expires_at: '2099-01-01T00:00:00Z', username: 'test_user', clinic_id: 'fixture_clinic_do_not_display', role: 'DOCTOR' } })
    if (url.pathname === '/auth/logout') return route.fulfill({ status: 200, headers: CORS, json: { status: 'logged_out' } })
    if (url.pathname === '/patients') return route.fulfill({ status: 200, headers: CORS, json: { results: [ANJALI], truncated: false } })
    if (url.pathname === '/patients/5/prescriptions') return route.fulfill({ status: 200, headers: CORS, json: { patient: ANJALI, prescriptions: [RX], limit: 20, offset: 0, has_more: false } })
    const r = await handler({ method: req.method(), path: url.pathname, body })
    if (!r) return route.fulfill({ status: 599, headers: CORS, json: { detail: `unexpected request in test: ${req.method()} ${url.pathname}` } })
    return route.fulfill({ status: r.status ?? 200, headers: CORS, json: r.body })
  })
  return calls
}

async function signInAndOpen(page: Page) {
  await page.goto('/')
  await page.getByRole('button', { name: /Enter AyurCore/ }).click()
  await page.getByRole('textbox', { name: 'Username' }).fill('test_user')
  await page.getByRole('textbox', { name: 'Password' }).fill('not-a-real-password')
  await page.locator('form').getByRole('button', { name: /Sign in/ }).click()
  await expect(page.locator('.app-shell')).toBeVisible()
  const menu = page.getByRole('button', { name: 'Open navigation' })
  if (await menu.isVisible()) await menu.click()
  await page.locator('nav.side-nav').getByRole('button', { name: 'Patients' }).click()
  await page.getByRole('searchbox', { name: /Search patients/ }).fill('an')
  await page.getByRole('button', { name: /Anjali Nair/ }).click()
  await expect(page.locator('.patient-record')).toBeVisible()
}

const ask = (page: Page) => page.getByRole('region', { name: 'ASK ABOUT THIS PATIENT' })
const box = (page: Page) => page.getByRole('textbox', { name: /Your question about/ })
const askButton = (page: Page) => ask(page).getByRole('button', { name: /^(Ask|Asking…)$/ })
const result = (page: Page) => ask(page).locator('.ask-result')
const headline = (page: Page) => result(page).locator('.ask-answer-headline')

const RAW = /rx:\d|enc:|created_at|opened_at|closed_at|dosha_a|dosha_b|grams_a|grams_b|dosha_changed|prescription_id|encounter_ref|plan_|COUNT_HERB|SUM_HERB|LAST_RX|\bsum herb\b|\blast herb\b|\bcount herb\b|\blist (prescription|herb|dosha|encounter):|From the known records only|undefined|\[object|\bnull\b|[{}]/

// ---- one time-zone convention ------------------------------------------------------------

for (const [timeZone, hhmm, day] of [['Asia/Kolkata', '11:10', '10 Oct 2026'], ['America/Los_Angeles', '22:40', '09 Oct 2026'], ['UTC', '05:40', '10 Oct 2026']] as const) {
  test.describe(`time zone ${timeZone}`, () => {
    test.use({ timezoneId: timeZone, locale: 'en-GB' })

    test('the answer\'s date, its evidence card\'s date and Patient History\'s date are the same instant in the same zone', async ({ page }) => {
      await mockApi(page, (c) => (c.path.endsWith('/ask') ? { body: reply('anjali: When was Neem last prescribed?', 5) } : undefined))
      await signInAndOpen(page)
      await box(page).fill('When was Neem last prescribed?')
      await askButton(page).click()
      await expect(headline(page)).toBeVisible()
      const answerDate = (await headline(page).innerText()).trim()
      const cardDate = (await result(page).locator('.ask-evidence-item .history-meta span').nth(1).innerText()).trim()   // [#id, date, status]
      const historyDate = (await page.locator('.history-item .history-meta').first().innerText())
      expect(answerDate).toBe(cardDate)
      expect(answerDate).toContain(hhmm)
      expect(answerDate).toContain(day)
      expect(historyDate).toContain(hhmm)
    })

    test('a list, a diff and a comparison show their records\' dates in that same zone', async ({ page }) => {
      let key = 'anjali: List all her prescriptions.'
      await mockApi(page, () => ({ body: reply(key, 5) }))
      await signInAndOpen(page)
      for (const [k, q] of [['anjali: List all her prescriptions.', 'List all her prescriptions.'], ['anjali: What changed from the previous prescription to the latest?', 'What changed?'], ['anjali: Did the Neem dose change since the previous prescription?', 'Neem changed?']] as const) {
        key = k
        await box(page).fill(q)
        await askButton(page).click()
        await expect(result(page).locator('.ask-answer-body')).toContainText(hhmm)
        await expect(result(page).locator('.ask-answer-body')).toContainText(day)
        await expect(result(page).locator('.ask-evidence-item').first()).toContainText(hhmm)
      }
    })
  })
}

// ---- every real reply, rendered ------------------------------------------------------------

test('every real answer renders a title and a result, shows its records under "Based on these records", and shows no raw executor output', async ({ page }) => {
  const byQuestion = new Map<string, string>()
  for (const key of Object.keys(REAL)) {
    const r = REAL[key] as { category: string }
    if (key.includes('[choice') || !['answer', 'incomplete'].includes(r.category)) continue
    byQuestion.set(key.slice(key.indexOf(': ') + 2) + '|' + key.slice(0, key.indexOf(': ')), key)
  }
  let current = ''
  await mockApi(page, (c) => (c.path.endsWith('/ask') ? { body: reply(current, 5) } : undefined))
  await signInAndOpen(page)
  let rendered = 0
  for (const key of byQuestion.values()) {
    current = key
    await box(page).fill(key.slice(key.indexOf(': ') + 2))
    await askButton(page).click()
    await expect(result(page).locator('.ask-answer-title'), key).toBeVisible()
    await expect(result(page).getByText('BASED ON THESE RECORDS'), key).toBeVisible()
    expect(await result(page).locator('.ask-evidence-item').count(), key).toBeGreaterThan(0)
    expect(await ask(page).innerText(), key).not.toMatch(RAW)
    rendered += 1
  }
  expect(rendered).toBeGreaterThanOrEqual(38)
})

test('a comparison is a labelled table: both records are identified in column headings and the amount has a row heading', async ({ page }) => {
  await mockApi(page, () => ({ body: reply('anjali: Did the Neem dose change since the previous prescription?', 5) }))
  await signInAndOpen(page)
  await box(page).fill('Neem changed?')
  await askButton(page).click()
  const table = result(page).getByRole('table')
  await expect(table.getByRole('columnheader')).toHaveCount(2)
  await expect(table.getByRole('columnheader').nth(0)).toContainText('#3')
  await expect(table.getByRole('columnheader').nth(1)).toContainText('#4')
  await expect(table.getByRole('rowheader', { name: 'Neem' })).toBeVisible()
  await expect(table.locator('tbody').getByRole('cell').nth(0)).toHaveText('20 g')
  await expect(table.locator('tbody').getByRole('cell').nth(1)).toHaveText('10 g')
  await expect(headline(page)).toHaveText('Decreased by 10 g')
})

test('the question context is shown, the evidence highlights the herb asked about, and the verified mark is defined', async ({ page }) => {
  await mockApi(page, () => ({ body: reply('anjali: How much Neem in total?', 5) }))
  await signInAndOpen(page)
  await box(page).fill('How much Neem in total?')
  await askButton(page).click()
  await expect(result(page).getByText('You asked: How much Neem in total?')).toBeVisible()
  await expect(result(page).getByText('Verified against the record')).toBeVisible()
  await expect(headline(page)).toHaveText('40 g')
  await expect(result(page).locator('.ask-evidence-item')).toHaveCount(3)
  await expect(result(page).locator('.ask-herb-focus')).toHaveCount(3)           // Neem in each of the three cited prescriptions, and only Neem
})

// ---- explicit uncertainty ------------------------------------------------------------------

test('an incomplete total puts "At least" in the result and the reason right beside it, before the evidence, without red styling', async ({ page }) => {
  await mockApi(page, () => ({ body: reply('legacy: How much Neem in total?', 5) }))
  await signInAndOpen(page)
  await box(page).fill('How much Neem in total?')
  await askButton(page).click()
  await expect(headline(page)).toHaveText('At least 16 g')
  const caveat = result(page).locator('.ask-caveat')
  await expect(caveat).toContainText('has herbs not recorded')
  const [h, c, e] = await Promise.all([headline(page).boundingBox(), caveat.boundingBox(), result(page).locator('.ask-evidence').boundingBox()])
  expect(h && c && e && h.y < c.y && c.y < e.y).toBeTruthy()
  expect(await caveat.evaluate((el) => getComputedStyle(el).borderLeftColor)).not.toBe('rgb(185, 87, 80)')      // not the error colour
  await expect(result(page).locator('.ask-evidence-item').filter({ hasText: 'Herbs not recorded.' })).toHaveCount(1)
})

test('a result that cannot be determined says so and shows which records are not recorded', async ({ page }) => {
  await mockApi(page, () => ({ body: reply('legacy: Did the Neem dose change since the previous prescription?', 5) }))
  await signInAndOpen(page)
  await box(page).fill('Neem changed?')
  await askButton(page).click()
  await expect(headline(page)).toHaveText('Not known')
  await expect(result(page).locator('.ask-caveat')).toHaveText('Cannot be determined from the record. The herbs are not recorded on #9.')
  await expect(result(page).getByText('Incomplete')).toBeVisible()
})

// ---- keyboard focus ------------------------------------------------------------------------

test.describe('focus', () => {
  test('Enter in the question box: focus stays there while the request runs, then moves to the answer', async ({ page }) => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => { release = resolve })
    await mockApi(page, async () => { await gate; return { body: reply('anjali: What is the latest prescription?', 5) } })
    await signInAndOpen(page)
    await box(page).fill('latest?')
    await box(page).press('Enter')
    await expect(askButton(page)).toHaveText(/Asking…/)
    await expect(box(page)).toBeFocused()
    release()
    await expect(headline(page)).toBeVisible()
    await expect(result(page)).toBeFocused()
    await page.keyboard.press('Shift+Tab')
    await expect(ask(page).getByRole('button', { name: 'Clear', exact: true })).toBeFocused()
  })

  test('clicking Ask: focus is never left on the page body, and ends on the answer', async ({ page }) => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => { release = resolve })
    await mockApi(page, async () => { await gate; return { body: reply('anjali: How many prescriptions has she had?', 5) } })
    await signInAndOpen(page)
    await box(page).fill('How many?')
    await askButton(page).click()
    await expect(askButton(page)).toHaveText(/Asking…/)
    await expect(askButton(page)).toBeFocused()
    release()
    await expect(result(page)).toBeFocused()
  })

  test('a clarification moves focus to its first option; choosing one moves focus to the answer; options work from the keyboard', async ({ page }) => {
    const calls = await mockApi(page, (c) => !c.path.endsWith('/ask') ? undefined : ({ body: (c.body as { choice?: unknown }).choice ? reply('anjali: Neem count? [choice SUM_HERB]', 5) : reply('anjali: Neem count?', 5) }))
    await signInAndOpen(page)
    await box(page).fill('Neem count?')
    await askButton(page).click()
    const options = ask(page).getByRole('list', { name: 'Possible readings' }).getByRole('button')
    await expect(options).toHaveCount(2)
    await expect(options.nth(0)).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(options.nth(1)).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(headline(page)).toHaveText('40 g')
    await expect(result(page)).toBeFocused()
    await expect(result(page).getByText('Answered as: The total grams of Neem across all prescriptions.')).toBeVisible()
    expect(calls.filter((c) => c.path.endsWith('/ask')).map((c) => (c.body as { choice?: { frame: string } }).choice?.frame ?? null)).toEqual([null, 'SUM_HERB'])
  })

  test('after a failure focus returns to the question box, which still holds the question', async ({ page }) => {
    await mockApi(page, () => ({ status: 500, body: { detail: 'boom' } }))
    await signInAndOpen(page)
    await box(page).fill('Neem count?')
    await askButton(page).click()
    await expect(ask(page).getByRole('alert')).toBeVisible()
    await expect(box(page)).toBeFocused()
    await expect(box(page)).toHaveValue('Neem count?')
  })

  test('a refusal takes focus like an answer, and Clear returns focus to the question box', async ({ page }) => {
    await mockApi(page, () => ({ body: reply('anjali: Should we increase the Neem dose?', 5) }))
    await signInAndOpen(page)
    await box(page).fill('Should we increase the Neem dose?')
    await askButton(page).click()
    await expect(result(page)).toHaveAttribute('data-category', 'unsafe')
    await expect(result(page)).toBeFocused()
    await ask(page).getByRole('button', { name: 'Clear', exact: true }).click()
    await expect(box(page)).toBeFocused()
  })

  test('the answer has a visible focus indicator', async ({ page }) => {
    await mockApi(page, () => ({ body: reply('anjali: How many prescriptions has she had?', 5) }))
    await signInAndOpen(page)
    await box(page).fill('How many?')
    await box(page).press('Enter')
    await expect(result(page)).toBeFocused()
    expect(await result(page).evaluate((el) => getComputedStyle(el).outlineStyle)).not.toBe('none')
  })
})

// ---- touch targets and wrapping ------------------------------------------------------------

test.describe('mobile', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })

  test('Ask, Clear and every reading are at least 44 x 44 CSS pixels', async ({ page }) => {
    await mockApi(page, () => ({ body: reply('anjali: Neem count?', 5) }))
    await signInAndOpen(page)
    await box(page).fill('Neem count?')
    for (const button of [askButton(page), ask(page).getByRole('button', { name: 'Clear', exact: true })]) {
      const b = await button.boundingBox()
      expect(b && b.height >= 44 && b.width >= 44).toBeTruthy()
    }
    await askButton(page).click()
    for (let i = 0; i < 2; i++) {
      const b = await ask(page).getByRole('list', { name: 'Possible readings' }).getByRole('button').nth(i).boundingBox()
      expect(b && b.height >= 44 && b.width >= 44).toBeTruthy()
    }
  })

  test('long herb names, a diff, a comparison, a long list and Malayalam fit at 390px', async ({ page }) => {
    const LONG = 'Ashwagandha_Churna_Extra_Long_Herb_Name_Without_Any_Break_'.repeat(3)
    const longHerb = JSON.parse(JSON.stringify(reply('anjali: How much Neem in total?', 5)).replaceAll('Neem', LONG))
    const longCompare = JSON.parse(JSON.stringify(reply('anjali: Did the Neem dose change since the previous prescription?', 5)).replaceAll('Neem', LONG))
    const longDiff = JSON.parse(JSON.stringify(reply('anjali: What changed from the previous prescription to the latest?', 5)).replaceAll('Guggulu', LONG))
    const replies = [longHerb, longCompare, longDiff, reply('anjali: List the dosha of each prescription.', 5), reply('malayalam: അവസാനത്തെ കുറിപ്പടിയിൽ എന്താണുള്ളത്?', 5), reply('legacy: What changed from the previous prescription to the latest?', 5)]
    let i = 0
    await mockApi(page, () => ({ body: replies[i] }))
    await signInAndOpen(page)
    for (i = 0; i < replies.length; i++) {
      await box(page).fill(i === 4 ? 'അവസാനത്തെ കുറിപ്പടിയിൽ എന്താണുള്ളത്?' : 'q')
      await askButton(page).click()
      await expect(result(page).locator('.ask-answer-title')).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth), `reply ${i}`).toBeLessThanOrEqual(0)
      const r = await result(page).boundingBox()
      expect(r && r.x >= 0 && r.x + r.width <= 390, `reply ${i}`).toBeTruthy()
    }
  })
})
