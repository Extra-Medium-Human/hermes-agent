import { atom } from 'nanostores'

import type { NousUpstreamStatus } from '@/global'

export const $upstreamStatus = atom<NousUpstreamStatus | null>(null)
export const $upstreamChecking = atom(false)

let flight: Promise<NousUpstreamStatus> | null = null

const unverified = (): NousUpstreamStatus => ({
  state: 'unverified',
  head: null,
  upstream_tip: null,
  checked_at: Date.now(),
  branch: 'main',
  remote_url: 'https://github.com/NousResearch/hermes-agent.git',
  message: 'Could not verify Nous upstream status.'
})

export function checkUpstreamStatus({ force = false }: { force?: boolean } = {}): Promise<NousUpstreamStatus> {
  if (flight && !force) {
    return flight
  }
  const previous = flight
  let current!: Promise<NousUpstreamStatus>
  current = (async () => {
    if (previous) {
      await previous
    }

    try {
      const result = (await window.hermesDesktop?.upstream?.check({ force })) ?? unverified()

      if (flight === current) {
        $upstreamStatus.set(result)
      }

      return result
    } catch {
      const result = unverified()

      if (flight === current) {
        $upstreamStatus.set(result)
      }

      return result
    } finally {
      if (flight === current) {
        flight = null
        $upstreamChecking.set(false)
      }
    }
  })()
  flight = current
  $upstreamChecking.set(true)

  return current
}
