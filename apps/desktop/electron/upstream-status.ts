import path from 'node:path'

import { runOwnedUpstreamProbe } from './upstream-probe-process'

export interface UpstreamStatus {
  state: 'current' | 'upstream-not-integrated' | 'unverified'
  head: string | null
  upstream_tip: string | null
  checked_at: number
  branch: 'main'
  remote_url: 'https://github.com/NousResearch/hermes-agent.git'
  message: string
}

const REMOTE_URL = 'https://github.com/NousResearch/hermes-agent.git'
const sha = (value: unknown): value is string => typeof value === 'string' && /^[0-9a-f]{40}$/.test(value)

function unverified(): UpstreamStatus {
  return {
    state: 'unverified',
    head: null,
    upstream_tip: null,
    checked_at: Date.now(),
    branch: 'main',
    remote_url: REMOTE_URL,
    message: 'Could not verify Nous upstream status.'
  }
}

function validPins(row: Record<string, unknown>): boolean {
  return (
    (row.head === null || sha(row.head)) &&
    (row.upstream_tip === null || sha(row.upstream_tip)) &&
    (row.state === 'unverified' || (sha(row.head) && sha(row.upstream_tip)))
  )
}

export function parseUpstreamStatus(code: number, stdout: string): UpstreamStatus {
  if (code !== 0) {
    return unverified()
  }

  try {
    const value: unknown = JSON.parse(stdout)

    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      return unverified()
    }

    const row = value as Record<string, unknown>

    if (
      !['current', 'upstream-not-integrated', 'unverified'].includes(String(row.state)) ||
      row.branch !== 'main' ||
      row.remote_url !== REMOTE_URL ||
      typeof row.checked_at !== 'number' ||
      !Number.isSafeInteger(row.checked_at) ||
      row.checked_at <= 0 ||
      typeof row.message !== 'string' ||
      !row.message.trim() ||
      !validPins(row)
    ) {
      return unverified()
    }

    return row as unknown as UpstreamStatus
  } catch {
    return unverified()
  }
}

/** A passive flight never satisfies an explicit refresh made while it is running. */
export function createUpstreamStatusChecker(
  probe: (root: string) => Promise<{ code: number; stdout: string }>,
  ttlMs = 30 * 60_000
) {
  const entries = new Map<string, { result?: UpstreamStatus; expires?: number; flight?: Promise<UpstreamStatus> }>()

  return {
    check(root: string, force = false): Promise<UpstreamStatus> {
      let entry = entries.get(root)

      if (!entry) {
        entry = {}
        entries.set(root, entry)
      }

      const slot = entry

      if (!force && slot.flight) {
        return slot.flight
      }

      if (!force && slot.result && Date.now() < (slot.expires ?? 0)) {
        return Promise.resolve(slot.result)
      }

      const predecessor = force ? slot.flight : undefined

      const flight = (async () => {
        // Publish the flight before a synchronously throwing launcher can settle it.
        await Promise.resolve()

        if (predecessor) {
          await predecessor
        }

        let result: UpstreamStatus

        try {
          const output = await probe(root)
          result = parseUpstreamStatus(output.code, output.stdout)
        } catch {
          result = unverified()
        }

        if (slot.flight === flight) {
          slot.result = result
          slot.expires = Date.now() + ttlMs
          slot.flight = undefined
        }

        return result
      })()

      slot.flight = flight

      return flight
    }
  }
}

interface CapturedProbeOptions {
  cwd: string
  env?: NodeJS.ProcessEnv
  timeoutMs?: number
}

/** The Settings subprocess seam, shared by the native runtime and real-Git canary. */
export function createPythonUpstreamStatusChecker(
  python: (root: string) => Promise<string | null>,
  capture: (
    command: string,
    args: string[],
    options: CapturedProbeOptions
  ) => Promise<{ code: number; stdout: string }> = runOwnedUpstreamProbe
) {
  return createUpstreamStatusChecker(async updateRoot => {
    const executable = await python(updateRoot)

    if (!executable) {
      throw new Error('Python unavailable')
    }

    return capture(executable, ['-m', 'hermes_cli.upstream_status', '--repo', updateRoot], {
      cwd: updateRoot,
      env: {
        PYTHONPATH: [updateRoot, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
        GIT_TERMINAL_PROMPT: '0',
        GCM_INTERACTIVE: 'Never'
      },
      timeoutMs: 30_000
    })
  })
}
