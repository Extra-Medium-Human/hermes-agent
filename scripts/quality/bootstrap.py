#!/usr/bin/env python3
"""Install the existing lockfiles and the pinned desktop binary, without launch."""
import subprocess
import sys

commands = [
    ['uv', 'sync', '--locked', '--python', '3.12.10', '--extra', 'all', '--extra', 'dev',
     '--extra', 'anthropic', '--extra', 'mistral', '--extra', 'fal', '--extra', 'modal',
     '--extra', 'daytona', '--extra', 'hindsight', '--extra', 'parallel-web'],
    ['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'],
    ['node', 'node_modules/electron/install.js'],
]
for command in commands:
    subprocess.run([sys.executable, 'scripts/quality/run.py', *command], check=True)
