#!/usr/bin/env python3
"""Run fork checks against disposable homes, without operator credentials."""
from __future__ import annotations

import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit('usage: python3 scripts/quality/run.py COMMAND [ARG ...]')
    if (ROOT / '.env').exists():
        raise SystemExit('Quality requires an isolated checkout without a root .env file.')
    with tempfile.TemporaryDirectory(prefix='hermes-quality-') as directory:
        home = Path(directory)
        for child in ['tmp', 'hermes', 'config', 'cache']:
            (home / child).mkdir()
        env = {key: os.environ[key] for key in ['PATH', 'SYSTEMROOT', 'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY'] if key in os.environ}
        for key in ['QUALITY_BASE', 'QUALITY_HEAD']:
            if re.fullmatch(r'[0-9a-f]{40}', os.environ.get(key, '')):
                env[key] = os.environ[key]
        env.update({
            'HOME': str(home), 'USERPROFILE': str(home), 'TMPDIR': str(home / 'tmp'),
            'TEMP': str(home / 'tmp'), 'TMP': str(home / 'tmp'),
            'HERMES_QUALITY_OFFLINE': '1' if sys.argv[1].endswith('run_tests.sh') else '',
            'HERMES_HOME': str(home / 'hermes'), 'XDG_CONFIG_HOME': str(home / 'config'),
            'XDG_CACHE_HOME': str(home / 'cache'), 'CI': 'true', 'TZ': 'UTC',
            'LANG': 'C.UTF-8', 'PYTHONHASHSEED': '0', 'HERMES_DISABLE_LAZY_INSTALLS': '1',
            'npm_config_cache': str(ROOT / '.quality-cache/npm'),
            'ELECTRON_CACHE': str(ROOT / '.quality-cache/electron'),
            'UV_CACHE_DIR': str(ROOT / '.quality-cache/uv'),
        })
        return subprocess.run(sys.argv[1:], cwd=ROOT, env=env, check=False).returncode


if __name__ == '__main__':
    raise SystemExit(main())
