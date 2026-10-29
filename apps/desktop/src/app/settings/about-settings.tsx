import { useStore } from '@nanostores/react'
import { type ReactElement, useEffect } from 'react'

import { Button } from '@/components/ui/button'
import { UpdateStatusCard, VersionHero } from '@/components/update-status'
import { VersionDetails } from '@/components/version-details'
import { useI18n } from '@/i18n'
import { RefreshCw } from '@/lib/icons'
import { $localBackendStatus, checkLocalBackendStatus } from '@/store/local-backend-status'
import { $connection } from '@/store/session'
import { $desktopVersion, $updateStatus, checkBackendUpdates, refreshDesktopVersion } from '@/store/updates'
import { $upstreamChecking, $upstreamStatus, checkUpstreamStatus } from '@/store/upstream-status'

import { SectionHeading, SettingsContent } from './primitives'
import { SETTING_IDS, settingElementId } from './settings-manifest'
import { UninstallSection } from './uninstall-section'
import { useSettingDeepLink } from './use-setting-deep-link'

interface AboutSettingsProps {
  subpage?: string
}

export function AboutSettings({ subpage }: AboutSettingsProps = {}): ReactElement {
  useSettingDeepLink('about', page => subpage === undefined || page === subpage)

  if (subpage === 'uninstall') {
    return (
      <SettingsContent>
        <UninstallSection />
      </SettingsContent>
    )
  }

  return <AppUpdatesSettings includeUninstall={subpage === undefined} />
}

interface AppUpdatesSettingsProps {
  includeUninstall: boolean
}

function AppUpdatesSettings({ includeUninstall }: AppUpdatesSettingsProps): ReactElement {
  const { t } = useI18n()
  const version = useStore($desktopVersion)
  const fork = useStore($updateStatus)
  const connection = useStore($connection)
  const upstream = useStore($upstreamStatus)
  const upstreamChecking = useStore($upstreamChecking)
  const localBackend = useStore($localBackendStatus)
  const remote = connection?.mode === 'remote'

  // Refresh the running version when About opens or the active gateway changes.
  useEffect((): void => {
    void refreshDesktopVersion()

    if (remote) {
      void checkBackendUpdates()
    } else if (connection?.mode === 'local') {
      void checkLocalBackendStatus()
    }

    void checkUpstreamStatus()
  }, [connection, remote])

  return (
    <SettingsContent>
      <VersionHero version={version} />
      <div className="mx-auto mt-4 w-full max-w-2xl">
        <SectionHeading icon={RefreshCw} title={t.settings.about.updates} />
        <div className="grid gap-3" id={settingElementId(SETTING_IDS.about.updates)}>
          <UpdateStatusCard
            label="Fork update"
            onCheck={() => void checkUpstreamStatus({ force: true })}
            target="client"
          />
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
                Integration unavailable pending explicit conflict resolution. This check cannot apply changes.
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
          <div className="rounded-xl border border-border/70 bg-muted/20 px-4 py-3 text-sm">
            <p className="mb-1 text-xs font-semibold text-muted-foreground">Installed Desktop</p>
            <p className="text-xs text-muted-foreground">
              Build stamp: {version?.commit?.slice(0, 12) ?? 'Unverified'} · Checkout SHA:{' '}
              {!fork?.error ? (fork?.currentSha?.slice(0, 12) ?? 'Unverified') : 'Unverified'}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {version?.bundleOutOfSync
                ? 'Desktop build differs from checkout'
                : 'Build and checkout match not verified'}
            </p>
          </div>
          {remote ? (
            <UpdateStatusCard label="Connected backend" showReleaseNotes={false} target="backend" />
          ) : (
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
          )}
        </div>
        {version && <VersionDetails version={version} />}
        {includeUninstall && <UninstallSection />}
      </div>
    </SettingsContent>
  )
}
