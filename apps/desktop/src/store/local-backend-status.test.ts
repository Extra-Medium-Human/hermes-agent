import { expect, test, vi } from 'vitest'

vi.mock('@/hermes', () => ({ checkHermesUpdate: vi.fn() }))

import { checkHermesUpdate } from '@/hermes'
import { $connection } from '@/store/session'

import { $localBackendStatus, checkLocalBackendStatus } from './local-backend-status'

test('local backend check reads backend API without changing fork status or apply flow', async () => {
  vi.mocked(checkHermesUpdate).mockResolvedValue({
    install_method: 'git',
    current_version: '0.16.0',
    behind: 0,
    update_available: false,
    can_apply: true,
    update_command: 'hermes update',
    message: null
  })
  expect((await checkLocalBackendStatus(true)).behind).toBe(0)
  expect($localBackendStatus.get()?.currentVersion).toBe('0.16.0')
  expect(checkHermesUpdate).toHaveBeenCalledWith(true)
})

test('a slow response cannot replace a newer connection check', async () => {
  let complete!: (value: any) => void

  const response = {
    install_method: 'git',
    current_version: 'old',
    behind: 0,
    update_available: false,
    can_apply: true,
    update_command: 'hermes update',
    message: null
  }

  vi.mocked(checkHermesUpdate).mockImplementationOnce(
    () =>
      new Promise(resolve => {
        complete = resolve
      })
  )
  const old = checkLocalBackendStatus()
  $connection.set({ ...$connection.get()!, baseUrl: 'http://new-local' })
  vi.mocked(checkHermesUpdate).mockResolvedValue({ ...response, current_version: 'new' })
  await checkLocalBackendStatus()
  complete(response)
  await old
  expect($localBackendStatus.get()?.currentVersion).toBe('new')
})

test('local backend error never retains an old affirmative result', async () => {
  vi.mocked(checkHermesUpdate).mockRejectedValue(new Error('offline'))
  expect((await checkLocalBackendStatus(true)).error).toBe('check-failed')
  expect($localBackendStatus.get()?.behind).toBeUndefined()
})
