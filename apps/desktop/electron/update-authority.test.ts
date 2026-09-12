import { expect, test } from 'vitest'

import {
  authorityBoundManualUpdateCommand,
  authorityPreflightInvocation,
  parseAuthorityPreflightResult,
  parseHandoffReadiness,
  parseUpdateAuthorityConfig,
  verifyHandoffHelperBeforeQuit
} from './update-authority'

test('missing configured authority refuses without a main fallback', () => {
  expect(parseUpdateAuthorityConfig({ branch: 'codex/hermes-live-current' }, '/repo')).toEqual({
    ok: false,
    code: 'AUTHORITY_CONFIG_MISSING',
    message: 'Update authority must define repo, remote, remote URL, branch, and tracking ref.'
  })
})

test('authority binds repo remote URL branch and tracking ref in the preflight command', () => {
  const parsed = parseUpdateAuthorityConfig(
    {
      repo: '/repo',
      remote: 'fork',
      remote_url: 'https://github.com/Extra-Medium-Human/hermes-agent.git',
      branch: 'codex/hermes-live-current',
      tracking_ref: 'refs/remotes/fork/codex/hermes-live-current'
    },
    '/repo'
  )
  expect(parsed.ok).toBe(true)
  if (!parsed.ok) {
    return
  }

  expect(authorityPreflightInvocation('/repo/venv/bin/python3', parsed.authority, {
    nonce: 'nonce-123', ownerPid: 42, tokenPath: '/home/.update-authority.json'
  })).toEqual({
    command: '/repo/venv/bin/python3',
    args: [
      '-m', 'hermes_cli.update_authority', 'preflight',
      '--repo', '/repo',
      '--remote', 'fork',
      '--remote-url', 'https://github.com/Extra-Medium-Human/hermes-agent.git',
      '--branch', 'codex/hermes-live-current',
      '--tracking-ref', 'refs/remotes/fork/codex/hermes-live-current',
      '--nonce', 'nonce-123',
      '--owner', '42',
      '--token', '/home/.update-authority.json'
    ]
  })
})

test('network and auth uncertainty remain typed refusals', () => {
  for (const code of ['AUTHORITY_UNVERIFIED', 'AUTHORITY_URL_MISMATCH', 'AUTHORITY_MISSING']) {
    expect(parseAuthorityPreflightResult(
      2, JSON.stringify({ ok: false, code, message: 'refused' }), ''
    )).toEqual({ ok: false, code, message: 'refused' })
  }

  expect(parseAuthorityPreflightResult(1, '<html>bad gateway</html>', 'timeout')).toEqual({
    ok: false,
    code: 'AUTHORITY_UNVERIFIED',
    message: 'Update authority preflight failed or returned malformed output.'
  })
})

test('explicit main is legal only as a fully tracked authority tuple', () => {
  expect(parseUpdateAuthorityConfig({
    repo: '/repo', remote: 'fork', remote_url: 'https://example.invalid/fork.git', branch: 'main',
    tracking_ref: 'refs/remotes/fork/main'
  }, '/repo').ok).toBe(true)
  expect(parseUpdateAuthorityConfig({
    repo: '/repo', remote: 'fork', remote_url: 'https://example.invalid/fork.git', branch: 'main',
    tracking_ref: 'refs/remotes/origin/main'
  }, '/repo').ok).toBe(false)
})

test('manual Windows command carries the complete configured authority tuple', () => {
  expect(authorityBoundManualUpdateCommand({
    repo: "C:\\Hermes User\\repo",
    remote: 'fork',
    remoteUrl: 'https://example.invalid/fork.git',
    branch: 'main',
    trackingRef: 'refs/remotes/fork/main'
  })).toBe(
    "hermes update --branch 'main' --authority-repo 'C:\\Hermes User\\repo' " +
      "--authority-remote 'fork' --authority-remote-url 'https://example.invalid/fork.git' " +
      "--authority-branch 'main' --authority-tracking-ref 'refs/remotes/fork/main'"
  )
})

test('handoff readiness is bound to owner nonce authority and frozen tips', () => {
  const parsed = parseUpdateAuthorityConfig({
    repo: '/repo', remote: 'fork', remote_url: 'https://example.invalid/fork.git',
    branch: 'codex/hermes-live-current', tracking_ref: 'refs/remotes/fork/codex/hermes-live-current'
  }, '/repo')
  expect(parsed.ok).toBe(true)
  if (!parsed.ok) {
    return
  }
  const expected = {
    authority: parsed.authority,
    ownerPid: 177,
    nonce: 'n-ready',
    head: 'a'.repeat(40),
    remoteTip: 'b'.repeat(40)
  }
  const document = {
    ok: true,
    owner_pid: 177,
    helper_pid: 288,
    helper_start_identity: 'ps:Sat Sep 12 01:23:45 2026',
    nonce: 'n-ready',
    authority: parsed.authority,
    topology: 'behind',
    head: expected.head,
    remote_tip: expected.remoteTip
  }
  expect(parseHandoffReadiness(JSON.stringify(document), expected)).toEqual({
    ok: true,
    helperPid: 288,
    helperStartIdentity: 'ps:Sat Sep 12 01:23:45 2026'
  })
  expect(parseHandoffReadiness(JSON.stringify({ ...document, nonce: 'wrong' }), expected)).toEqual({
    ok: false,
    code: 'HANDOFF_TOKEN_MISMATCH',
    message: 'Detached updater readiness did not match this update transaction.'
  })
})

test('handoff readiness requires a canonical helper process start identity', () => {
  const parsed = parseUpdateAuthorityConfig({
    repo: '/repo', remote: 'fork', remote_url: 'https://example.invalid/fork.git',
    branch: 'main', tracking_ref: 'refs/remotes/fork/main'
  }, '/repo')

  expect(parsed.ok).toBe(true)
  if (!parsed.ok) {
    return
  }

  const expected = {
    authority: parsed.authority,
    ownerPid: 177,
    nonce: 'n-ready',
    head: 'a'.repeat(40),
    remoteTip: 'b'.repeat(40)
  }
  const document = {
    ok: true,
    owner_pid: 177,
    helper_pid: 288,
    nonce: 'n-ready',
    authority: parsed.authority,
    topology: 'behind',
    head: expected.head,
    remote_tip: expected.remoteTip
  }
  expect(parseHandoffReadiness(JSON.stringify(document), expected)).toMatchObject({
    ok: false,
    code: 'HANDOFF_TOKEN_MISMATCH'
  })
})

test('quit-boundary liveness rejects dead unknown and PID-reused helpers', async () => {
  const readiness = { helperPid: 288, helperStartIdentity: 'ps:original' }
  await expect(verifyHandoffHelperBeforeQuit(readiness, async () => 'ps:original')).resolves.toEqual({ ok: true })
  await expect(verifyHandoffHelperBeforeQuit(readiness, async () => 'ps:reused')).resolves.toMatchObject({
    ok: false,
    code: 'HANDOFF_HELPER_REUSED'
  })
  await expect(
    verifyHandoffHelperBeforeQuit(readiness, async () => {
      throw new Error('dead')
    })
  ).resolves.toMatchObject({ ok: false, code: 'HANDOFF_HELPER_UNVERIFIED' })
})
