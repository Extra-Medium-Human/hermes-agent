import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { expect, test } from 'vitest'

import { runOwnedUpstreamProbe } from './upstream-probe-process'

const alive = (pid: number): boolean => {
  const result = spawnSync('ps', ['-p', String(pid), '-o', 'stat='], { encoding: 'utf8' })

  return result.status === 0 && !result.stdout.trim().startsWith('Z')
}

test.skipIf(process.platform === 'win32')(
  'a timed-out probe stops its real Git-shaped descendant before returning',
  async () => {
    const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-upstream-tree-'))
    const marker = path.join(cwd, 'child-pid')

    const script = `
    const {spawn} = require('node:child_process');
    const fs = require('node:fs');
    const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], {stdio: 'ignore'});
    fs.writeFileSync(${JSON.stringify(marker)}, String(child.pid));
    setInterval(() => {}, 1000);
  `

    let descendant: number | undefined

    try {
      const result = await runOwnedUpstreamProbe(process.execPath, ['-e', script], { cwd, timeoutMs: 2000 })
      expect(fs.existsSync(marker)).toBe(true)
      descendant = Number(fs.readFileSync(marker, 'utf8'))
      expect(result.code).toBe(124)
      expect(alive(descendant)).toBe(false)
    } finally {
      if (fs.existsSync(marker)) {descendant = Number(fs.readFileSync(marker, 'utf8'))}

      if (descendant && alive(descendant)) {process.kill(descendant, 'SIGKILL')}
      fs.rmSync(cwd, { recursive: true })
    }
  }
)

test.skipIf(process.platform === 'win32')('probe exit cannot release a still-running descendant', async () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-upstream-exit-'))
  const marker = path.join(cwd, 'child-pid')

  const script = `const {spawn}=require('node:child_process'); const fs=require('node:fs');
    const child=spawn(process.execPath,['-e','setInterval(() => {}, 1000)'],{stdio:'ignore'});
    fs.writeFileSync(${JSON.stringify(marker)},String(child.pid)); process.exit(1);`

  let descendant: number | undefined

  try {
    const result = await runOwnedUpstreamProbe(process.execPath, ['-e', script], { cwd, timeoutMs: 2000 })
    descendant = Number(fs.readFileSync(marker, 'utf8'))
    expect(result.code).toBe(1)
    expect(alive(descendant)).toBe(false)
  } finally {
    if (descendant && alive(descendant)) {process.kill(descendant, 'SIGKILL')}
    fs.rmSync(cwd, { recursive: true })
  }
})

test('spawn failure is a terminal unverified input to the parser', async () => {
  const result = await runOwnedUpstreamProbe('/missing/hermes-python', [], { cwd: os.tmpdir(), timeoutMs: 1000 })
  expect(result.code).toBe(-1)
})
