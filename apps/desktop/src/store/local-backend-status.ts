import { atom } from 'nanostores'

import type { DesktopUpdateStatus } from '@/global'
import { checkHermesUpdate } from '@/hermes'
import { $connection } from '@/store/session'

export const $localBackendStatus = atom<DesktopUpdateStatus | null>(null)
let latestRequest = 0

export async function checkLocalBackendStatus(force = false): Promise<DesktopUpdateStatus> {
  const request = ++latestRequest
  const connection = $connection.get()
  $localBackendStatus.set(null)
  let status: DesktopUpdateStatus

  try {
    const response = await checkHermesUpdate(force)
    status =
      response.can_apply && response.behind !== null
        ? {
            supported: true,
            behind: response.behind >= 0 ? response.behind : null,
            updateAvailable: response.update_available,
            currentVersion: response.current_version,
            fetchedAt: Date.now()
          }
        : {
            supported: false,
            message: response.message ?? 'Backend status could not be verified.',
            fetchedAt: Date.now()
          }
  } catch (error) {
    status = {
      supported: true,
      error: 'check-failed',
      message: error instanceof Error ? error.message : String(error),
      fetchedAt: Date.now()
    }
  }

  if (request === latestRequest && $connection.get() === connection) {
    $localBackendStatus.set(status)
  }

  return status
}
