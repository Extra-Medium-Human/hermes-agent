import { spawn } from 'node:child_process'
import { once } from 'node:events'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, test, vi } from 'vitest'

import { processStartMarker } from './backend-claim'
import { verifyHandoffHelperBeforeQuit } from './update-authority'

const here = path.dirname(fileURLToPath(import.meta.url))
const mainSource = fs.readFileSync(path.join(here, 'main.ts'), 'utf8').replace(/\r\n/g, '\n')

function extractFunction(name: string): string {
  const marker = `async function ${name}(`
  const start = mainSource.indexOf(marker)
  expect(start, `${name} must exist`).toBeGreaterThanOrEqual(0)
  const signatureEnd = mainSource.indexOf('\n', start)
  const brace = mainSource.lastIndexOf('{', signatureEnd)
  let depth = 0

  for (let index = brace; index < mainSource.length; index += 1) {
    if (mainSource[index] === '{') {
      depth += 1
    }

    if (mainSource[index] === '}') {
      depth -= 1
      if (depth === 0) {
        return mainSource.slice(start, index + 1)
      }
    }
  }

  throw new Error(`unterminated ${name}`)
}

function compileFunction(name: string, deps: Record<string, unknown>) {
  const source = extractFunction(name)
    .replace(`async function ${name}(opts: { stopSafeBlockers?: boolean } = {})`, `async function ${name}(opts = {})`)
    .replace(`async function ${name}(opts: any)`, `async function ${name}(opts)`)
  const names = Object.keys(deps)

  return Function(...names, `return (${source})`)(...names.map(key => deps[key]))
}

const authority = {
  repo: '/repo',
  remote: 'fork',
  remoteUrl: 'https://example.invalid/fork.git',
  branch: 'codex/hermes-live-current',
  trackingRef: 'refs/remotes/fork/codex/hermes-live-current'
}

function refusalCases() {
  return [
    ['missing configured authority', { config: { ok: false, code: 'AUTHORITY_CONFIG_MISSING', message: 'missing' } }],
    ['malformed configured authority', { config: { ok: false, code: 'AUTHORITY_CONFIG_INVALID', message: 'malformed' } }],
    ['remote URL mismatch', { probe: { ok: false, code: 'AUTHORITY_URL_MISMATCH', message: 'mismatch' } }],
    ['missing ref / HTTP 404', { probe: { ok: false, code: 'AUTHORITY_MISSING', message: '404' } }],
    ['unprocessable ref / HTTP 422', { probe: { ok: false, code: 'AUTHORITY_UNVERIFIED', message: '422' } }],
    ['network uncertainty', { probe: { ok: false, code: 'AUTHORITY_UNVERIFIED', message: 'timeout' } }]
  ] as const
}

function windowsDeps(overrides: { config?: any; probe?: any } = {}) {
  const effects = {
    backup: vi.fn(),
    marker: vi.fn(),
    release: vi.fn(async () => ({ unlocked: true })),
    spawn: vi.fn(() => ({ pid: 991, unref() {} })),
    quit: vi.fn()
  }
  const deps = {
    IS_WINDOWS: true,
    IS_PACKAGED: true,
    IS_MAC: false,
    updateInFlight: false,
    resolveUpdaterBinary: () => '/updater.exe',
    applyUpdatesPosixHandoff: vi.fn(),
    resolveUpdateRoot: () => '/repo',
    resolveUpdateScriptHandoff: () => ({ command: 'powershell', args: [], scriptPath: '/repo/windows.ps1' }),
    configuredUpdateAuthority: () => overrides.config ?? { ok: true, authority },
    runDesktopAuthorityProbe: async () =>
      overrides.probe ?? { ok: true, topology: 'behind', head: 'a'.repeat(40), remoteTip: 'b'.repeat(40) },
    crypto: { randomUUID: () => 'nonce-windows' },
    process: { pid: 77, env: {}, execPath: '/Hermes.exe' },
    HERMES_HOME: '/home',
    path,
    directoryExists: () => true,
    fileExists: () => true,
    readDesktopUpdateConfig: () => ({ branch: authority.branch }),
    resolveHealedBranch: async () => authority.branch,
    DEFAULT_UPDATE_BRANCH: 'main',
    updateHandoffConflict: () => null,
    emitUpdateProgress: vi.fn(),
    repairMacUpdaterHelper: vi.fn(),
    runningAppBundle: () => null,
    preflightStateDb: effects.backup,
    rememberLog: vi.fn(),
    windowsUpdatePrerequisiteError: () => null,
    releaseBackendLockForUpdate: effects.release,
    scanVenvBlockers: async () => ({ kind: 'clear' }),
    stopSafeVenvBlockers: vi.fn(),
    setTimeout: (callback: () => void) => callback(),
    startHermes: vi.fn(async () => {}),
    startGatewaysAfterUpdateAbort: vi.fn(),
    venvHermesShimPath: () => '/repo/venv/Scripts/hermes.exe',
    pathWithHermesManagedNode: (value: string) => value,
    wrapHandoffForDetachedConsole: (_handoff: unknown, args: string[]) => ({ command: 'cmd.exe', args }),
    spawnUpdaterProcess: effects.spawn,
    writeUpdateMarker: effects.marker,
    stagedUpdaterSupportsPrewrittenMarker: () => true,
    observeUpdaterHandoff: async () => ({ ok: true }),
    UPDATE_HANDOFF_DWELL_MS: 0,
    app: { quit: effects.quit },
    isQuittingForHandoff: false
  }
  return { deps, effects }
}

describe('Windows updater callers are authority-first', () => {
  for (const [label, overrides] of refusalCases()) {
    test(`normal apply refuses ${label} before destructive handoff effects`, async () => {
      const { deps, effects } = windowsDeps(overrides)
      const applyUpdates = compileFunction('applyUpdates', deps)
      await expect(applyUpdates()).resolves.toMatchObject({ ok: false })
      expect(effects.backup).not.toHaveBeenCalled()
      expect(effects.marker).not.toHaveBeenCalled()
      expect(effects.release).not.toHaveBeenCalled()
      expect(effects.spawn).not.toHaveBeenCalled()
      expect(effects.quit).not.toHaveBeenCalled()
    })

    test(`bootstrap recovery refuses ${label} before destructive handoff effects`, async () => {
      const { deps, effects } = windowsDeps(overrides)
      const handoff = compileFunction('handOffWindowsBootstrapRecovery', {
        ...deps,
        chooseUpdaterArgs: () => ['--update'],
        localBackendLifecycle: { assertCanStart: vi.fn() }
      })
      await expect(handoff('bootstrap-needed')).resolves.toMatchObject({ ok: false })
      expect(effects.backup).not.toHaveBeenCalled()
      expect(effects.marker).not.toHaveBeenCalled()
      expect(effects.release).not.toHaveBeenCalled()
      expect(effects.spawn).not.toHaveBeenCalled()
      expect(effects.quit).not.toHaveBeenCalled()
    })
  }

  test('normal and bootstrap callers contain no implicit main resolver', () => {
    expect(mainSource.includes('resolveHealedBranch')).toBe(false)
    expect(extractFunction('applyUpdates').includes("'hermes update'")).toBe(false)
    expect(extractFunction('handOffWindowsBootstrapRecovery').includes('DEFAULT_UPDATE_BRANCH')).toBe(false)
  })
})

test('POSIX quit boundary rejects a daemon killed after readiness', async () => {
  const timers: Array<() => void> = []
  const quit = vi.fn()
  const release = vi.fn()
  const helper = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {
    cwd: '/private/tmp',
    env: { PATH: process.env.PATH },
    stdio: 'ignore'
  })
  if (!helper.pid) {
    throw new Error('helper fixture did not start')
  }

  const helperStartIdentity = await processStartMarker(helper.pid)
  const apply = compileFunction('applyUpdatesPosixHandoff', {
    resolveUpdateRoot: () => '/repo',
    resolvePosixScriptHandoff: () => ({ command: '/bin/bash', args: ['/repo/posix.sh'], scriptPath: '/repo/posix.sh' }),
    configuredUpdateAuthority: () => ({ ok: true, authority }),
    crypto: { randomUUID: () => 'nonce-posix' },
    path,
    HERMES_HOME: '/home',
    process: { pid: 77, env: {}, execPath: '/Hermes', argv: [], cwd: () => '/tmp' },
    runDesktopAuthorityProbe: async () => ({ ok: true, topology: 'behind', head: 'a'.repeat(40), remoteTip: 'b'.repeat(40) }),
    updateHandoffConflict: () => null,
    createDesktopStateSnapshot: async () => ({ ok: true, receipt: '/snapshot.json' }),
    IS_MAC: true,
    runningAppBundle: () => '/stage/Hermes.app',
    collectRelaunchArgs: () => [],
    pathWithHermesManagedNode: (value: string) => value,
    spawnUpdaterProcess: () => ({ pid: 123, unref() {} }),
    rememberLog: vi.fn(),
    emitUpdateProgress: vi.fn(),
    observeUpdaterHandoff: async () => ({ ok: true }),
    waitForAuthorityHandoffReady: async () => ({
      ok: true,
      helperPid: helper.pid,
      helperStartIdentity
    }),
    verifyHandoffHelperBeforeQuit,
    processStartMarker,
    UPDATE_HANDOFF_DWELL_MS: 2500,
    Date,
    setTimeout: (callback: () => void) => {
      timers.push(callback)
      return 0
    },
    isQuittingForHandoff: false,
    app: {
      quit: () => {
        release()
        quit()
      }
    }
  })

  await expect(apply({})).resolves.toMatchObject({ ok: true, handedOff: true })
  helper.kill('SIGKILL')
  await once(helper, 'exit')
  await timers[0]?.()
  expect(release).not.toHaveBeenCalled()
  expect(quit).not.toHaveBeenCalled()
})