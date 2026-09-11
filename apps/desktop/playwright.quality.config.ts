import { defineConfig } from '@playwright/test'

// Nonvisual, mocked product checks. Reuses the built desktop artifact.
export default defineConfig({
  testDir: './e2e',
  testMatch: 'quality-smoke.spec.ts',
  timeout: 150_000,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  outputDir: 'test-results/quality',
  use: { screenshot: 'off', video: 'off', trace: 'off' },
})
