import { defineConfig } from '@playwright/test'
import { fileURLToPath } from 'node:url'

const frontend = fileURLToPath(new URL('.', import.meta.url))
const root = fileURLToPath(new URL('..', import.meta.url))
const api = 'http://127.0.0.1:8117'
const site = 'http://127.0.0.1:5178'
const secret = 'frontend-local-test-only-secret-0987654321'
process.env.UI_TEST_ADMIN_SECRET = secret

export default defineConfig({
  testDir: './tests',
  outputDir: '../storage/frontend-test-results',
  workers: 1,
  expect: { timeout: 10_000 },
  use: {
    baseURL: site,
    viewport: { width: 1440, height: 900 },
    channel: process.env.UI_TEST_CHROME === '1' ? 'chrome' : undefined,
    launchOptions: {
      executablePath: process.env.UI_TEST_BROWSER_BINARY,
      args: process.env.UI_TEST_SINGLE_PROCESS === '1' ? ['--single-process', '--disable-gpu', '--disable-breakpad'] : [],
    },
  },
  webServer: [
    { command: '.venv/bin/python -B frontend/tests/local_api.py', cwd: root, url: api + '/health',
      env: { UI_TEST_ADMIN_SECRET: secret }, reuseExistingServer: false },
    { command: 'npm run dev -- --port 5178 --strictPort', cwd: frontend, url: site,
      env: { API_PROXY_TARGET: api }, reuseExistingServer: false },
  ],
})
