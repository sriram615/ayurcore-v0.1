import { defineConfig, devices } from '@playwright/test'

/**
 * Frontend tests for the Patients workspace.
 *
 * Runs against the PRODUCTION build (`pnpm build` first, then this starts
 * `next start` on its own port). Every browser test intercepts the app's API
 * origin with page.route() before any request leaves the browser, and blocks
 * every other external request, so these tests never reach a real FastAPI
 * backend or database -- every response they see is a fixture written in the
 * spec. Backend behaviour itself (RBAC, clinic isolation, limits) is covered by
 * the pytest suite; these tests cover what the UI does with it.
 *
 * API_ORIGIN is lib/api.ts's default base URL (no NEXT_PUBLIC_API_BASE_URL is
 * set for this build), so the tested build is the real one, not a test variant.
 *
 * Uses the locally installed Google Chrome (channel: 'chrome') so no Playwright
 * browser download is needed.
 *
 *   pnpm build && pnpm exec playwright test
 */
export const API_ORIGIN = 'http://127.0.0.1:8000'
const PORT = 3100

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    channel: 'chrome',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], channel: 'chrome' } },
  ],
  webServer: {
    command: `pnpm exec next start -p ${PORT} -H 127.0.0.1`,
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
})
