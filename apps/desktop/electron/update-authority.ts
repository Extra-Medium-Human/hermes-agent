import path from 'node:path'

export interface UpdateAuthority {
  repo: string
  remote: string
  remoteUrl: string
  branch: string
  trackingRef: string
}

export interface AuthorityRefusal {
  ok: false
  code: string
  message: string
}

export type AuthorityConfigResult = { ok: true; authority: UpdateAuthority } | AuthorityRefusal

const CONFIG_MESSAGE = 'Update authority must define repo, remote, remote URL, branch, and tracking ref.'

function configuredString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

export function parseUpdateAuthorityConfig(value: unknown, expectedRepo: string): AuthorityConfigResult {
  const config = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
  const repo = configuredString(config.repo)
  const remote = configuredString(config.remote)
  const remoteUrl = configuredString(config.remote_url)
  const branch = configuredString(config.branch)
  const trackingRef = configuredString(config.tracking_ref)

  if (!repo || !remote || !remoteUrl || !branch || !trackingRef) {
    return { ok: false, code: 'AUTHORITY_CONFIG_MISSING', message: CONFIG_MESSAGE }
  }
  if (
    path.resolve(repo) !== path.resolve(expectedRepo) ||
    !/^[A-Za-z0-9._-]+$/.test(remote) ||
    branch.startsWith('-') ||
    /[\s~^:?*[\\]/.test(branch) ||
    trackingRef !== `refs/remotes/${remote}/${branch}`
  ) {
    return {
      ok: false,
      code: 'AUTHORITY_CONFIG_INVALID',
      message: 'Configured update authority is malformed or does not match this install.'
    }
  }
  return { ok: true, authority: { repo: path.resolve(repo), remote, remoteUrl, branch, trackingRef } }
}

export function authorityPreflightInvocation(
  python: string,
  authority: UpdateAuthority,
  values: { nonce: string; ownerPid: number; tokenPath?: string; action?: 'check' | 'preflight' | 'validate' }
): { command: string; args: string[] } {
  return {
    command: python,
    args: [
      '-m',
      'hermes_cli.update_authority',
      values.action ?? 'preflight',
      '--repo',
      authority.repo,
      '--remote',
      authority.remote,
      '--remote-url',
      authority.remoteUrl,
      '--branch',
      authority.branch,
      '--tracking-ref',
      authority.trackingRef,
      '--nonce',
      values.nonce,
      '--owner',
      String(values.ownerPid),
      ...(values.tokenPath ? ['--token', values.tokenPath] : [])
    ]
  }
}

export type AuthorityPreflightResult =
  | { ok: true; topology: 'equal' | 'behind'; head: string; remoteTip: string }
  | AuthorityRefusal

export function parseAuthorityPreflightResult(
  exitCode: number,
  stdout: string,
  _stderr: string
): AuthorityPreflightResult {
  try {
    const parsed = JSON.parse(stdout)
    if (exitCode === 0 && parsed?.ok === true && ['equal', 'behind'].includes(parsed.topology)) {
      if (typeof parsed.head === 'string' && typeof parsed.remote_tip === 'string') {
        return { ok: true, topology: parsed.topology, head: parsed.head, remoteTip: parsed.remote_tip }
      }
    }
    if (
      exitCode !== 0 &&
      parsed?.ok === false &&
      typeof parsed.code === 'string' &&
      typeof parsed.message === 'string'
    ) {
      return { ok: false, code: parsed.code, message: parsed.message }
    }
  } catch {
    // Fail closed below.
  }
  return {
    ok: false,
    code: 'AUTHORITY_UNVERIFIED',
    message: 'Update authority preflight failed or returned malformed output.'
  }
}

export function parseHandoffReadiness(
  raw: string,
  expected: {
    authority: UpdateAuthority
    ownerPid: number
    nonce: string
    head: string
    remoteTip: string
  }
): { ok: true; helperPid: number } | AuthorityRefusal {
  try {
    const parsed = JSON.parse(raw)
    if (parsed?.ok === false && typeof parsed.code === 'string' && typeof parsed.message === 'string') {
      return { ok: false, code: parsed.code, message: parsed.message }
    }
    if (
      parsed?.ok === true &&
      parsed.owner_pid === expected.ownerPid &&
      Number.isInteger(parsed.helper_pid) &&
      parsed.helper_pid > 0 &&
      parsed.nonce === expected.nonce &&
      parsed.topology === 'behind' &&
      parsed.head === expected.head &&
      parsed.remote_tip === expected.remoteTip &&
      JSON.stringify(parsed.authority) === JSON.stringify(expected.authority)
    ) {
      return { ok: true, helperPid: parsed.helper_pid }
    }
  } catch {
    void 0
  }
  return {
    ok: false,
    code: 'HANDOFF_TOKEN_MISMATCH',
    message: 'Detached updater readiness did not match this update transaction.'
  }
}
