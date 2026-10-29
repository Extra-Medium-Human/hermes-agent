import { spawn } from 'node:child_process'

export interface OwnedProbeOptions {
  cwd: string
  env?: NodeJS.ProcessEnv
  timeoutMs?: number
}

/** Own the whole read-only probe group, including Python's Git/HTTPS children. */
export function runOwnedUpstreamProbe(
  command: string,
  args: string[],
  options: OwnedProbeOptions
): Promise<{ code: number; stdout: string; stderr: string }> {
  // No unmanaged taskkill fallback: without a Job Object we cannot prove
  // ownership if the parent exits before its descendants. Refuse before spawn.
  if (process.platform === 'win32') {
    return Promise.resolve({ code: -1, stdout: '', stderr: 'Upstream probe process-tree ownership is unavailable.' })
  }

  return new Promise(resolve => {
    let stdout = ''
    let stderr = ''
    let timedOut = false

    const child = spawn(command, args, {
      cwd: options.cwd,
      env: { ...process.env, ...options.env },
      detached: true,
      stdio: ['ignore', 'pipe', 'pipe']
    })

    const stopGroup = () => {
      if (!child.pid) {return}

      try {
        process.kill(-child.pid, 'SIGKILL')
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ESRCH') {
          stderr += `Probe process group termination failed: ${String(error)}`
        }
      }
    }

    process.once('exit', stopGroup)

    const timer = setTimeout(() => {
      timedOut = true
      stopGroup()
    }, options.timeoutMs ?? 30_000)

    child.stdout.on('data', chunk => {
      stdout += chunk.toString()
    })
    child.stderr.on('data', chunk => {
      stderr += chunk.toString()
    })
    child.once('error', error => {
      clearTimeout(timer)
      process.removeListener('exit', stopGroup)
      resolve({ code: -1, stdout, stderr: String(error) })
    })
    // A SIGKILL request is not completion: drain and reap before publishing.
    child.once('close', code => {
      clearTimeout(timer)
      stopGroup()
      process.removeListener('exit', stopGroup)
      resolve({ code: timedOut ? 124 : (code ?? -1), stdout, stderr })
    })
  })
}
