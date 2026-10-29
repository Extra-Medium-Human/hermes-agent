import { useStore } from '@nanostores/react'

import { Button } from '@/components/ui/button'
import { $localBackendStatus, checkLocalBackendStatus } from '@/store/local-backend-status'
import { $desktopVersion, $updateStatus } from '@/store/updates'
import { $upstreamChecking, $upstreamStatus, checkUpstreamStatus } from '@/store/upstream-status'

export function UpstreamStatusCard() {
  const upstream = useStore($upstreamStatus)
  const upstreamChecking = useStore($upstreamChecking)

  return (
    <div className="rounded-xl border border-border/70 bg-muted/20 px-4 py-3 text-sm">
      <p className="mb-1 text-xs font-semibold text-muted-foreground">Nous upstream</p>
      <p className="font-medium">
        {upstream?.state === 'upstream-not-integrated'
          ? 'Changes not integrated'
          : upstream?.state === 'current'
            ? 'Current with Nous main'
            : 'Could not verify'}
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        main · {upstream?.upstream_tip?.slice(0, 12) ?? 'SHA unverified'} ·{' '}
        {upstream ? new Date(upstream.checked_at).toLocaleString() : 'Never checked'}
      </p>
      {upstream?.state === 'upstream-not-integrated' && (
        <p className="mt-1 text-xs text-muted-foreground">
          Integration is not available from this read-only check. Prepare and review a separate candidate.
        </p>
      )}
      <Button
        disabled={upstreamChecking}
        onClick={() => void checkUpstreamStatus({ force: true })}
        size="sm"
        variant="textStrong"
      >
        {upstreamChecking ? 'Checking…' : 'Refresh upstream'}
      </Button>
    </div>
  )
}

export function InstalledDesktopStatusCard() {
  const version = useStore($desktopVersion)
  const fork = useStore($updateStatus)
  const installedCommit = version?.commit
  const checkoutCommit = fork?.error ? undefined : fork?.currentSha
  const pinsVerified = /^[0-9a-f]{40}$/.test(installedCommit ?? '') && /^[0-9a-f]{40}$/.test(checkoutCommit ?? '')

  return (
    <div className="rounded-xl border border-border/70 bg-muted/20 px-4 py-3 text-sm">
      <p className="mb-1 text-xs font-semibold text-muted-foreground">Installed Desktop</p>
      <p className="text-xs text-muted-foreground">
        Build stamp: {version?.commit?.slice(0, 12) ?? 'Unverified'} · Checkout SHA:{' '}
        {!fork?.error ? (fork?.currentSha?.slice(0, 12) ?? 'Unverified') : 'Unverified'}
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        {version?.bundleOutOfSync || (pinsVerified && installedCommit !== checkoutCommit)
          ? 'Desktop build differs from checkout'
          : pinsVerified
            ? 'Desktop build matches checkout'
            : 'Build and checkout match not verified'}
      </p>
    </div>
  )
}

export function LocalBackendStatusCard() {
  const localBackend = useStore($localBackendStatus)

  return (
    <div className="rounded-xl border border-border/70 bg-muted/20 px-4 py-3 text-sm">
      <p className="mb-1 text-xs font-semibold text-muted-foreground">Connected backend</p>
      <p className="font-medium">
        {localBackend?.error
          ? 'Could not verify'
          : localBackend?.behind === 0 && localBackend.updateAvailable === false
            ? 'Backend check current'
            : localBackend?.updateAvailable
              ? 'Backend update available'
              : 'Backend status unverified'}
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        Local · {localBackend?.currentVersion ?? 'Version unverified'} ·{' '}
        {localBackend?.fetchedAt ? new Date(localBackend.fetchedAt).toLocaleString() : 'Never checked'}
      </p>
      <Button onClick={() => void checkLocalBackendStatus(true)} size="sm" variant="textStrong">
        Refresh backend
      </Button>
    </div>
  )
}
