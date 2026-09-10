import * as fs from 'node:fs'
import * as path from 'node:path'

/** The desktop fixture never inherits a logged-in runtime or operator home. */
export function isolatedDesktopEnv(
  sandbox: { root: string; hermesHome: string; userDataDir: string },
  repoRoot: string,
  inherited: NodeJS.ProcessEnv,
  extra: Record<string, string> = {},
): Record<string, string> {
  const clean: Record<string, string> = {}
  for (const key of ['PATH', 'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'SystemRoot', 'SYSTEMROOT', 'TEST_WORKER_INDEX']) {
    if (inherited[key]) clean[key] = inherited[key]!
  }
  const home = path.join(sandbox.root, 'home')
  const temp = path.join(sandbox.root, 'tmp')
  const runtime = path.join(sandbox.root, 'xdg-runtime')
  for (const directory of [home, temp, runtime]) fs.mkdirSync(directory, { recursive: true, mode: 0o700 })
  return {
    ...clean,
    HOME: home,
    USERPROFILE: home,
    TMPDIR: temp,
    TMP: temp,
    TEMP: temp,
    XDG_RUNTIME_DIR: runtime,
    XDG_CONFIG_HOME: path.join(home, '.config'),
    XDG_CACHE_HOME: path.join(home, '.cache'),
    PATH: [path.join(repoRoot, '.venv', process.platform === 'win32' ? 'Scripts' : 'bin'), clean.PATH || ''].join(path.delimiter),
    PYTHONPATH: path.join(repoRoot, 'scripts', 'quality', 'offline'),
    HERMES_HOME: sandbox.hermesHome,
    HERMES_DESKTOP_USER_DATA_DIR: sandbox.userDataDir,
    HERMES_DESKTOP_IGNORE_EXISTING: '1',
    HERMES_DESKTOP_HERMES_ROOT: repoRoot,
    HERMES_DESKTOP_APP_NAME: `HermesE2E-${path.basename(sandbox.root)}`,
    HERMES_DESKTOP_SKIP_QUIT_CONFIRM: '1',
    HERMES_DISABLE_LAZY_INSTALLS: '1',
    ...extra,
  }
}
