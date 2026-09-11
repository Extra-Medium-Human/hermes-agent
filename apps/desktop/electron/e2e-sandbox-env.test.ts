import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import { afterEach, expect, test } from 'vitest'
import { isolatedDesktopEnv } from '../e2e/sandbox-env'

const roots: string[] = []
afterEach(() => { for (const root of roots.splice(0)) fs.rmSync(root, { recursive: true, force: true }) })

test('test launch isolates home, credentials, runtime discovery, and inherited Electron mode', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-fixture-env-'))
  roots.push(root)
  const sandbox = { root, hermesHome: path.join(root, 'hermes'), userDataDir: path.join(root, 'electron') }
  const env = isolatedDesktopEnv(sandbox, '/fixture/repo', {
    HOME: '/operator', PATH: '/usr/bin', TEST_WORKER_INDEX: '0',
    OPENAI_API_KEY: 'operator-secret', ELECTRON_RUN_AS_NODE: '1',
    HERMES_DESKTOP_HERMES: '/operator/runtime', HERMES_VOICE: '1',
    HERMES_DESKTOP_DEV_SERVER: 'https://operator.example',
  })
  expect(env.HOME).toBe(path.join(root, 'home'))
  expect(fs.statSync(env.HOME).isDirectory()).toBe(true)
  expect(env.HERMES_HOME).toBe(sandbox.hermesHome)
  expect(env.PATH.split(path.delimiter)[0]).toContain('.venv')
  expect(env.TEST_WORKER_INDEX).toBe('0')
  for (const key of ['OPENAI_API_KEY', 'ELECTRON_RUN_AS_NODE', 'HERMES_DESKTOP_HERMES', 'HERMES_VOICE', 'HERMES_DESKTOP_DEV_SERVER']) expect(env[key]).toBeUndefined()
})

test('explicit fixture failures are retained while distinct launches own distinct homes', () => {
  const make = () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-fixture-env-'))
    roots.push(root)
    return isolatedDesktopEnv({ root, hermesHome: path.join(root, 'hermes'), userDataDir: path.join(root, 'electron') }, '/fixture', {}, { HERMES_DESKTOP_BOOT_FAKE_ERROR: 'fixture failure' })
  }
  const first = make(), second = make()
  expect(first.HOME).not.toBe(second.HOME)
  expect(first.HERMES_DESKTOP_APP_NAME).not.toBe(second.HERMES_DESKTOP_APP_NAME)
  expect(first.HERMES_DESKTOP_BOOT_FAKE_ERROR).toBe('fixture failure')
})
