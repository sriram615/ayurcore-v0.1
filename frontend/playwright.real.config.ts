import { defineConfig, devices } from '@playwright/test'

/**
 * Real-backend end-to-end check of the AI-2 Ask flow (no mocks). It does NOT start any server: start the demo backend and the frontend yourself
 * (README "Demo"), then run it against the SYNTHETIC demo database only:
 *
 *   DEMO_CREDENTIALS=<path to demo/credentials.v01-demo.json> pnpm exec playwright test -c playwright.real.config.ts
 *
 * The frontend must be served on http://127.0.0.1:3000 (the only origin the backend allows) and its default API base (http://127.0.0.1:8000)
 * is the demo backend's address. One worker and no retries, on purpose.
 */
export default defineConfig({
  testDir: './e2e-real',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:3000', channel: 'chrome', trace: 'retain-on-failure' },
  projects: [{ name: 'desktop', use: { ...devices['Desktop Chrome'], channel: 'chrome' } }],
})
