#!/usr/bin/env python3
"""GitHub transport for the pinned quality engine; no application credentials."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ENGINE = Path(__file__).with_name('quality.py')
WORKFLOW = '.github/workflows/quality.yml'
MAX_ARTIFACT = 20 * 1024 * 1024


def api(path, binary=False):
    result = subprocess.run(['gh', 'api', path], capture_output=True, timeout=120)
    if result.returncode:
        raise RuntimeError('GitHub API unavailable for ' + path.split('?')[0])
    return result.stdout if binary else json.loads(result.stdout)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')


def engine(*args):
    return subprocess.run([sys.executable, str(ENGINE), *map(str, args)], check=True,
                          capture_output=True, text=True).stdout


def trusted_run(run, repository, branch):
    return (run.get('event') in {'push', 'schedule', 'workflow_dispatch'}
            and run.get('head_branch') == branch
            and run.get('path', '').split('@')[0] == WORKFLOW
            and run.get('status') == 'completed'
            and run.get('repository', {}).get('full_name') == repository
            and run.get('head_repository', {}).get('full_name') == repository)


def extract_evidence(data, directory):
    if len(data) > MAX_ARTIFACT:
        raise ValueError('Oversized quality artifact')
    extracted = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        total = sum(item.file_size for item in archive.infolist())
        if total > MAX_ARTIFACT:
            raise ValueError('Oversized expanded quality artifact')
        for item in archive.infolist():
            # Never extract paths from an archive. Only bounded JSON evidence is read.
            if not item.filename.endswith('.json') or item.is_dir():
                continue
            raw = archive.read(item)
            value = json.loads(raw)
            if not isinstance(value, dict) or not {'head', 'checks', 'provenance'} <= value.keys():
                continue
            digest = hashlib.sha256(raw).hexdigest()
            path = directory / (digest + '.json')
            path.write_bytes(raw)
            extracted.append({'path': str(path), 'sha256': digest, 'value': value})
    return extracted


def history(repository, branch, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    runs = api(f'repos/{repository}/actions/runs?branch={branch}&per_page=100')['workflow_runs']
    trusted, evidence = [], []
    for run in runs:
        if not trusted_run(run, repository, branch):
            continue
        # Capture failed attempts too; a retry never rewrites first-attempt metrics.
        record = {k: run.get(k) for k in ['id', 'run_attempt', 'head_sha', 'event', 'status',
                  'conclusion', 'path', 'head_branch', 'repository', 'created_at', 'updated_at']}
        record['evidence_sha256'] = []
        artifacts = api(f'repos/{repository}/actions/runs/{run["id"]}/artifacts')['artifacts']
        for artifact in artifacts:
            if artifact['name'] != 'quality-evidence' or artifact.get('expired'):
                continue
            if artifact.get('size_in_bytes', MAX_ARTIFACT + 1) > MAX_ARTIFACT:
                continue
            try:
                raw = api(f'repos/{repository}/actions/artifacts/{artifact["id"]}/zip', binary=True)
                found = extract_evidence(raw, directory)
            except (ValueError, zipfile.BadZipFile, RuntimeError):
                continue  # Missing/unreadable evidence forces verification fallback.
            record['evidence_sha256'].extend(item['sha256'] for item in found)
            for item in found:
                item['run_id'] = run['id']
                evidence.append(item)
        trusted.append(record)
    result = {'runs': trusted, 'evidence': evidence}
    write(directory/'index.json', result)
    write(directory/'trusted-runs.json', trusted)
    return result


def ancestor(base, head):
    return subprocess.run(['git', 'merge-base', '--is-ancestor', base, head],
                          capture_output=True).returncode == 0


def choose_baseline(records, head, fresh=True):
    now = dt.datetime.now(dt.timezone.utc)
    for run in records['runs']:
        if not ancestor(run.get('head_sha', ''), head):
            continue
        if run.get('conclusion') != 'success':
            return None  # A newer failure with unknown coverage is not reusable evidence.
        items = [item for item in records['evidence'] if item['run_id'] == run['id'] and item['value'].get('mode') == 'full']
        if not items:
            continue
        for item in items:
            ev = item['value']
            if ev.get('status') != 'success' or ev.get('head') != run['head_sha']:
                return None
            try:
                completed = dt.datetime.fromisoformat(ev['completed_at'].replace('Z', '+00:00'))
                if fresh and not dt.timedelta(0) <= now - completed < dt.timedelta(hours=24):
                    return None
            except (KeyError, ValueError, TypeError):
                return None
        return {'head': run['head_sha'], 'items': items}
    return None


def select(event_path, outdir):
    event = json.loads(Path(event_path).read_text(encoding='utf-8'))
    manifest = json.loads(Path('.quality.json').read_text(encoding='utf-8'))
    event_name = os.environ.get('GITHUB_EVENT_NAME', 'workflow_dispatch')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    base = event.get('pull_request', {}).get('base', {}).get('sha') or event.get('before')
    full = event_name == 'workflow_dispatch' and event.get('inputs', {}).get('mode', 'full') == 'full'
    if not base or set(base) == {'0'}:
        base = head
        full = True
    reason = 'affected changes'
    if event_name in {'schedule', 'push'}:
        try:
            records = history(manifest['repository'], manifest.get('default_branch', 'main'), '.quality-history')
            previous = choose_baseline(records, head, fresh=event_name != 'schedule')
        except (RuntimeError, KeyError):
            previous = None
        if event_name == 'schedule':
            full = True
            if previous:
                args = ['nightly', '--head', head, '--trusted-runs', '.quality-history/trusted-runs.json']
                for item in previous['items']:
                    args.extend(['--previous', item['path']])
                decision = json.loads(engine(*args))
                if decision.get('run_full') is False or decision.get('run') is False:
                    full, base, reason = False, head, 'unchanged verified regression inputs'
        elif manifest.get('release', {}).get('kind', 'none') != 'none':
            if previous:
                args = ['release', 'verify', '--candidate', previous['head'], '--trusted-runs', '.quality-history/trusted-runs.json']
                for item in previous['items']:
                    args.extend(['--baseline', item['path']])
                try:
                    valid = json.loads(engine(*args)).get('eligible') is True
                except subprocess.CalledProcessError:
                    valid = False
                base = previous['head']
                full = not valid
                reason = 'all changes since fresh ancestor regression' if valid else 'invalid or incompatible baseline: full release fallback'
            else:
                full = True
                reason = 'missing, stale, or failed baseline: release fallback'
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    args = ['select', '--base', base, '--head', head, '--output', outdir/'selection.json']
    if full:
        args.append('--full')
    engine(*args)
    plan = json.loads((outdir/'selection.json').read_text(encoding='utf-8'))
    runners = sorted({check['runner'] for check in plan['checks']})
    matrix = {'include': [{'runner': runner, 'bootstrap': any(c['runner'] == runner and c['kind'] != 'docs' for c in plan['checks'])} for runner in runners] or [{'runner': 'ubuntu-24.04', 'bootstrap': False}]}
    outputs = {'base': base, 'head': head, 'mode': 'full' if plan['full'] else 'affected',
               'has_checks': str(bool(runners)).lower(), 'matrix': json.dumps(matrix), 'reason': reason,
               'runtimes': json.dumps(manifest.get('runtimes', {}))}
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as output:
            for key, value in outputs.items():
                if '\n' in str(value):
                    raise ValueError('Invalid multiline workflow output')
                output.write(f'{key}={value}\n')
    write(outdir/'decision.json', outputs)
    print(json.dumps(outputs))


def aggregate(directory, needs):
    directory = Path(directory)
    args = ['aggregate', '--selection', directory/'selection.json', '--needs-json', needs,
            '--output', directory/'summary.json']
    for path in sorted(directory.glob('checks/**/*.json')):
        value = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(value, dict) and {'head', 'checks', 'provenance'} <= value.keys():
            args.extend(['--evidence', path])
    result = subprocess.run([sys.executable, str(ENGINE), *map(str, args)])
    summary = directory/'summary.json'
    if summary.exists():
        print(summary.read_text(encoding='utf-8'))
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['select', 'history', 'aggregate', 'release-verify'])
    parser.add_argument('--event', default=os.environ.get('GITHUB_EVENT_PATH'))
    parser.add_argument('--directory', default='.quality-results')
    parser.add_argument('--needs', default='.quality-results/needs.json')
    parser.add_argument('--candidate', default='HEAD')
    parser.add_argument('--ensure-full', action='store_true', help='Dispatch one full default-branch fallback when release evidence cannot be reused')
    parser.add_argument('--force-full', action='store_true', help='Dispatch one full run to restore a missing tested release artifact')
    args = parser.parse_args()
    if args.command == 'select':
        select(args.event, args.directory)
    elif args.command == 'history':
        manifest = json.loads(Path('.quality.json').read_text(encoding='utf-8'))
        history(manifest['repository'], manifest.get('default_branch', 'main'), args.directory)
    elif args.command == 'release-verify':
        from release import ReleaseError, verify_coverage, ensure_coverage
        manifest = json.loads(Path('.quality.json').read_text(encoding='utf-8'))
        candidate = subprocess.check_output(['git', 'rev-parse', args.candidate], text=True).strip()
        try:
            if args.ensure_full or args.force_full:
                result = ensure_coverage(manifest, candidate, '.quality-release-history', force=args.force_full)
            else:
                result = verify_coverage(manifest, candidate, '.quality-release-history')
        except (ReleaseError, RuntimeError) as error:
            print(json.dumps({'eligible':False, 'status':'full_required', 'candidate':candidate, 'reason':str(error)}))
            return 3
        print(json.dumps(result))
    else:
        return aggregate(args.directory, args.needs)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
