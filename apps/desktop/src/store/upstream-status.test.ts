import { beforeEach, expect, test, vi } from 'vitest'

import { $upstreamStatus, checkUpstreamStatus } from './upstream-status'

const status = {
  state: 'current' as const, head: 'a'.repeat(40), upstream_tip: 'b'.repeat(40),
  checked_at: 1793280000000, branch: 'main' as const,
  remote_url: 'https://github.com/NousResearch/hermes-agent.git' as const, message: 'Integrated'
}

beforeEach(() => { $upstreamStatus.set(null); vi.unstubAllGlobals() })

test('independent upstream check publishes without the fork bridge', async () => {
  const checkUpstream = vi.fn().mockResolvedValue(status)
  vi.stubGlobal('window', { hermesDesktop: { upstream: { check: checkUpstream } } })
  expect(await checkUpstreamStatus({ force: true })).toEqual(status)
  expect($upstreamStatus.get()).toEqual(status)
  expect(checkUpstream).toHaveBeenCalledWith({ force: true })
})

test('a failed forced upstream check clears stale affirmative status', async () => {
  $upstreamStatus.set(status)
  vi.stubGlobal('window', { hermesDesktop: { upstream: { check: vi.fn().mockRejectedValue(new Error('offline')) } } })
  expect((await checkUpstreamStatus({ force: true }))?.state).toBe('unverified')
  expect($upstreamStatus.get()?.state).toBe('unverified')
})
