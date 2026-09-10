#!/usr/bin/env python3
"""Check tracked prose encoding and unresolved merge markers without dependencies."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
paths = subprocess.check_output(['git', 'ls-files', '-z', '*.md', '*.mdx'], cwd=root).decode().split('\0')
for name in filter(None, paths):
    path = root / name
    if not path.exists():
        continue
    content = path.read_text(encoding='utf-8')
    if any(line.startswith(('<<<<<<< ', '>>>>>>> ')) for line in content.splitlines()):
        raise SystemExit(f'{name}: unresolved merge marker')
print('Tracked prose is valid UTF-8 with no unresolved merge markers.')
