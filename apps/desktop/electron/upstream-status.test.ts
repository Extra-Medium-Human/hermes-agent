import { expect, test, vi } from 'vitest'

import { createUpstreamStatusChecker, parseUpstreamStatus } from './upstream-status'

const current = (head = 'a'.repeat(40)) =>
  JSON.stringify({
    state: 'current',
    head,
    upstream_tip: 'b'.repeat(40),
    checked_at: 1793280000000,
    branch: 'main',
    remote_url: 'https://github.com/NousResearch/hermes-agent.git',
    message: 'Integrated'
  })

test('synchronous launch failure is unverified, not a rejected promise or stale cache', async () => {
  const checker = createUpstreamStatusChecker(() => {
    throw new Error('python unavailable')
  })

  expect((await checker.check('/repo')).state).toBe('unverified')
  expect((await checker.check('/repo', true)).state).toBe('unverified')
})

test('upstream parser accepts the independent read-only wire result', () => {
  expect(parseUpstreamStatus(0, current()).state).toBe('current')
  expect(
    parseUpstreamStatus(
      0,
      JSON.stringify({
        state: 'upstream-not-integrated',
        head: 'a'.repeat(40),
        upstream_tip: 'b'.repeat(40),
        checked_at: 1793280000000,
        branch: 'main',
        remote_url: 'https://github.com/NousResearch/hermes-agent.git',
        message: 'Pending'
      })
    ).state
  ).toBe('upstream-not-integrated')
})

test('malformed, mismatched authority and failed probes cannot claim current', () => {
  for (const [code, value] of [
    [1, current()],
    [0, '{'],
    [0, current('not-a-sha')],
    [0, current().replace('NousResearch', 'OtherOrg')],
    [0, current().replace('"main"', '"dev"')],
    [0, current().replace('1793280000000', '"yesterday"')]
  ] as const) {
    expect(parseUpstreamStatus(code, value).state).toBe('unverified')
  }

  expect(
    parseUpstreamStatus(
      0,
      JSON.stringify({
        state: 'unverified',
        head: null,
        upstream_tip: null,
        checked_at: 1793280000000,
        branch: 'main',
        remote_url: 'https://github.com/NousResearch/hermes-agent.git',
        message: 'No network'
      })
    ).state
  ).toBe('unverified')
})

test('passive checks coalesce within TTL, but force during a passive flight waits for a fresh probe', async () => {
  let resolveFirst!: (value: { code: number; stdout: string }) => void

  const probe = vi
    .fn()
    .mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveFirst = resolve
        })
    )
    .mockResolvedValue({ code: 0, stdout: current('c'.repeat(40)) })

  const checker = createUpstreamStatusChecker(probe, 60_000)
  const passive = checker.check('/repo')
  await Promise.resolve()
  const duplicate = checker.check('/repo')
  const forced = checker.check('/repo', true)
  resolveFirst({ code: 0, stdout: current() })
  expect((await passive).head).toBe('a'.repeat(40))
  expect((await duplicate).head).toBe('a'.repeat(40))
  expect((await forced).head).toBe('c'.repeat(40))
  expect(probe).toHaveBeenCalledTimes(2)
  expect((await checker.check('/repo')).head).toBe('c'.repeat(40))
  expect(probe).toHaveBeenCalledTimes(2)
})

test('cache is keyed by repo and failed probe does not retain a stale current result', async () => {
  const probe = vi
    .fn()
    .mockResolvedValueOnce({ code: 0, stdout: current() })
    .mockResolvedValueOnce({ code: 1, stdout: current() })
    .mockResolvedValue({ code: 0, stdout: current('c'.repeat(40)) })

  const checker = createUpstreamStatusChecker(probe, 60_000)
  expect((await checker.check('/repo')).state).toBe('current')
  expect((await checker.check('/repo', true)).state).toBe('unverified')
  expect((await checker.check('/other')).head).toBe('c'.repeat(40))
})
