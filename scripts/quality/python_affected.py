#!/usr/bin/env python3
"""Reuse the per-file runner for changed tests and their nearest Python package."""
import os
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[2]
base, head = os.environ.get('QUALITY_BASE', ''), os.environ.get('QUALITY_HEAD', '')
if not all(re.fullmatch(r'[0-9a-f]{40}', ref) for ref in [base, head]):
    raise SystemExit('Affected Python checks require the engine-selected QUALITY_BASE and QUALITY_HEAD.')
paths = subprocess.check_output(['git', 'diff', '--name-only', '-z', base, head, '--'], cwd=root).decode().split('\0')
targets = set()
for name in filter(None, paths):
    path = Path(name)
    if path.suffix != '.py':
        continue
    if name.startswith('tests/'):
        # A deleted test or changed fixture runs the remaining sibling tests.
        if path.name == 'conftest.py' or not (root / path).exists():
            targets.add(str(path.parent))
        else:
            targets.add(name)
    elif name.startswith(('scripts/', '.quality/')):
        # Policy changes already select the complete offline regression.
        continue
    else:
        candidates = list((root / 'tests').rglob(f'test_{path.stem}.py'))
        if candidates:
            targets.update(str(p.relative_to(root)) for p in candidates)
        else:
            package_tests = root / 'tests' / path.parts[0]
            targets.add(str(package_tests.relative_to(root)) if package_tests.is_dir() else 'tests')
if not targets:
    print('No directly changed Python behavior; declared component contracts still run.')
else:
    environment = dict(os.environ, HERMES_QUALITY_OFFLINE='1')
    raise SystemExit(subprocess.run([
        'scripts/run_tests.sh', '-j', '4', '--file-retries', '0', *sorted(targets),
        '-m', 'not integration and not ssh',
    ], cwd=root, env=environment, check=False).returncode)
