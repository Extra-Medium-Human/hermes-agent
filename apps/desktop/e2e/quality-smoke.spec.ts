import { allowErrorBanners, expect, test } from './test'
import { setupMockBackend, setupDeadBackend, waitForAppReady, waitForBootFailure } from './fixtures'

test('isolated desktop boots through the local provider and completes a chat turn', async () => {
  const fixture = await setupMockBackend()
  try {
    await expect(fixture.page).toHaveTitle(/Hermes/)
    await waitForAppReady(fixture, 120_000)
    const composer = fixture.page.locator('[contenteditable="true"]').first()
    await composer.fill('Hello, can you hear me?')
    await composer.press('Enter')
    await expect(fixture.page.getByRole('paragraph').filter({ hasText: 'Hello from the mock inference server' })).toBeVisible({ timeout: 60_000 })
    const runtime = await fixture.app.evaluate(() => ({ home: process.env.HOME, hermesHome: process.env.HERMES_HOME }))
    expect(runtime.home).toBe(`${fixture.sandbox.root}/home`)
    expect(runtime.hermesHome).toBe(fixture.sandbox.hermesHome)
  } finally {
    await fixture.cleanup()
  }
})

test('a failed runtime reports its error instead of a blank window', async () => {
  allowErrorBanners()
  const fixture = await setupDeadBackend({ fakeError: true })
  try {
    await waitForBootFailure(fixture.page, 90_000)
    await expect(fixture.page.getByText('Desktop boot failed', { exact: true }).first()).toBeVisible()
  } finally {
    await fixture.cleanup()
  }
})
