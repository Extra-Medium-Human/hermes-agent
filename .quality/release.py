#!/usr/bin/env python3
"""Promote a verified production artifact and recover without touching persistent data."""
from __future__ import annotations
import argparse
import base64
import concurrent.futures
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import ci


class ReleaseError(RuntimeError):
    pass


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def vercel(path, token, team, method='GET', body=None):
    separator = '&' if '?' in path else '?'
    url = 'https://api.vercel.com' + path + separator + urllib.parse.urlencode({'teamId': team})
    request = urllib.request.Request(url, method=method, headers={'Authorization': 'Bearer '+token,
                                    'Content-Type': 'application/json'},
                                    data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        # Provider responses may contain credentials or environment values.
        provider_code = None
        try:
            value = json.loads(error.read(65536)).get('error', {}).get('code')
            if value == 'incorrect_git_source_info':
                provider_code = value
        except (ValueError, AttributeError):
            pass
        failure = ReleaseError(f'Vercel {method} {path.split("?")[0]} returned HTTP {error.code}')
        failure.provider_code, failure.http_status = provider_code, error.code
        raise failure from None


def committed_source(candidate, exclusions=(), root='.'):
    """Read deployment inputs from Git objects, never the credential-bearing checkout."""
    if not re.fullmatch(r'[0-9a-f]{40}', candidate):
        raise ReleaseError('Source upload requires an immutable commit')
    if not isinstance(exclusions, (list, tuple)) or not all(isinstance(p, str) for p in exclusions):
        raise ReleaseError('Source exclusions must be a list of explicit paths')
    for path in exclusions:
        parts = PurePosixPath(path).parts
        if not parts or path.startswith('/') or any(p in {'.', '..'} for p in path.split('/')):
            raise ReleaseError('Source exclusions require explicit repository-relative paths')
    tree = subprocess.check_output(['git', 'rev-parse', candidate+'^{tree}'], cwd=root).decode().strip()
    entries = subprocess.check_output(['git', 'ls-tree', '-rlz', '--full-tree', candidate], cwd=root)
    files, total = [], 0
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        metadata, raw_path = entry.split(b'\t', 1)
        mode, kind, oid, size = metadata.decode().split()
        path = raw_path.decode()
        if path.startswith('/') or any(p in {'.', '..', '.git'} for p in path.split('/')) or any(ord(c) < 32 for c in path):
            raise ReleaseError('Unsafe committed source path')
        if any(path == skip or path.startswith(skip+'/') for skip in exclusions):
            continue
        if kind != 'blob' or mode not in {'100644', '100755'}:
            raise ReleaseError('Source upload requires regular tracked files; declare any non-runtime exclusions explicitly')
        name = PurePosixPath(path).name
        if name.startswith('.env') and not (name.endswith(('.example', '.template')) or name in {'.env.example', '.env.template'}):
            raise ReleaseError('Refusing to upload a tracked environment file')
        total += int(size)
        if total > 256*1024*1024 or len(files) >= 10000:
            raise ReleaseError('Committed deployment source exceeds the bounded upload limit')
        data = subprocess.check_output(['git', 'cat-file', 'blob', oid], cwd=root)
        identity = hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        if len(data) != int(size) or identity != oid:
            raise ReleaseError('Committed source identity changed')
        files.append({'file':path, 'data':data, 'sha':hashlib.sha1(data).hexdigest(), 'size':len(data)})
    if not files:
        raise ReleaseError('Deployment source is empty')
    return tree, files, total


def upload_source_file(file, token, team):
    request = urllib.request.Request('https://api.vercel.com/v2/files?'+urllib.parse.urlencode({'teamId':team}),
        method='POST', data=file['data'], headers={'Authorization':'Bearer '+token,
        'Content-Type':'application/octet-stream', 'Content-Length':str(file['size']), 'x-vercel-digest':file['sha']})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
    except urllib.error.HTTPError as error:
        raise ReleaseError(f'Vercel source upload returned HTTP {error.code}') from None


def create_staged_deployment(manifest, candidate, project, token, team):
    config = manifest['release']['vercel']
    body = {'name':project['name'], 'project':config['project_id'], 'target':'production',
            'gitSource':{'type':'github', 'repoId':config['repository_id'], 'ref':candidate, 'sha':candidate},
            'meta':{'githubCommitSha':candidate, 'qualityRepository':manifest['repository']}}
    try:
        return vercel('/v13/deployments', token, team, 'POST', body)
    except ReleaseError as error:
        if getattr(error, 'http_status', None) != 400 or getattr(error, 'provider_code', None) != 'incorrect_git_source_info':
            raise
    # A moved private repository can retain its stable project identity while
    # Vercel's old Git installation cannot read it. GitHub already checked out
    # and verified this exact commit; upload those committed bytes via the API.
    tree, files, total = committed_source(candidate, config.get('source_exclude_paths', []))
    if not is_current_candidate(manifest, candidate):
        raise ReleaseError('Candidate was superseded before source upload')
    if total <= 2*1024*1024:
        payload = [{'file':f['file'], 'encoding':'base64', 'data':base64.b64encode(f['data']).decode()} for f in files]
    else:
        unique = {f['sha']:f for f in files}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda f: upload_source_file(f, token, team), unique.values()))
        payload = [{key:f[key] for key in ('file', 'sha', 'size')} for f in files]
    if not is_current_candidate(manifest, candidate):
        raise ReleaseError('Candidate was superseded during source upload')
    current = vercel('/v9/projects/'+config['project_id'], token, team)
    if current.get('autoAssignCustomDomains') is not False or str(current.get('link',{}).get('repoId')) != str(config['repository_id']):
        raise ReleaseError('Project identity or staging settings changed during source upload')
    body.pop('gitSource')
    body['files'] = payload
    body['meta'].update(qualitySourceTree=tree, qualitySourceTransport='committed-files')
    body['gitMetadata'] = {'remoteUrl':'https://github.com/'+manifest['repository'],
                          'commitRef':manifest.get('default_branch','main'), 'commitSha':candidate, 'dirty':False}
    print(f'Uploading verified committed source: {len(files)} files, {total} bytes')
    return vercel('/v13/deployments', token, team, 'POST', body)


def state_compatible(previous, candidate, patterns):
    if not previous or not patterns or not ci.ancestor(previous, candidate):
        return False
    result = subprocess.run(['git', 'diff', '--name-only', '-z', '--no-renames', previous, candidate],
                            capture_output=True, check=True)
    paths = [p for p in result.stdout.decode().split('\0') if p]
    return not any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in patterns)


def rollback_allowed(state, current_id, compatible):
    return (state.get('promoted') is True and current_id == state.get('deployment_id')
            and compatible is True and state.get('previous_verified') is True
            and state.get('rollback_attempted') is not True and bool(state.get('previous_id')))


def github_post(path, body):
    result = subprocess.run(['gh', 'api', '--method', 'POST', path, '--input', '-'],
                            input=json.dumps(body).encode(), capture_output=True, timeout=60)
    if result.returncode:
        raise ReleaseError('Cannot persist release state in GitHub; refusing an unrecorded production mutation')
    return json.loads(result.stdout)


def deployment_records(manifest):
    repo = manifest['repository']
    rows = ci.api(f'repos/{repo}/deployments?task=quality-release&environment=production&per_page=100')
    records = []
    for row in rows:
        if row.get('task') != 'quality-release' or row.get('creator', {}).get('login') != 'github-actions[bot]':
            continue
        row['quality_statuses'] = ci.api(f'repos/{repo}/deployments/{row["id"]}/statuses?per_page=100')
        records.append(row)
    return records


def previous_is_verified(records, deployment_id):
    for record in records:
        payload = record.get('payload') or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        if payload.get('artifact_id') != deployment_id:
            continue
        return any(s.get('state') == 'success' and s.get('description') == 'quality: healthy after ten-minute observation'
                   for s in record.get('quality_statuses', []))
    return False


def previously_rolled_back(records, candidate):
    return any(record.get('sha') == candidate and any(s.get('description') == 'quality: rollback attempted'
               for s in record.get('quality_statuses', [])) for record in records)


def release_status(manifest, record_id, state, description):
    return github_post(f'repos/{manifest["repository"]}/deployments/{record_id}/statuses',
                       {'state': state, 'description': description, 'auto_inactive': False})


def wait_current(project_id, deployment_id, token, team):
    for _ in range(18):
        project = vercel('/v9/projects/'+project_id, token, team)
        if project.get('targets', {}).get('production', {}).get('id') == deployment_id:
            return
        time.sleep(10)
    raise ReleaseError('Production alias did not reach the requested artifact within three minutes')


def is_current_candidate(manifest, candidate):
    branch = manifest.get('default_branch', 'main')
    remote = ci.api(f'repos/{manifest["repository"]}/branches/{branch}')
    return remote['commit']['sha'] == candidate


def verify_coverage(manifest, candidate, directory):
    records = ci.history(manifest['repository'], manifest.get('default_branch', 'main'), directory)
    baseline = ci.choose_baseline(records, candidate)
    if not baseline:
        raise ReleaseError('Missing, stale, or failed full baseline. Run Quality full on this default-branch candidate before release.')
    args = ['release', 'verify', '--candidate', candidate, '--trusted-runs', Path(directory)/'trusted-runs.json']
    for item in baseline['items']:
        args.extend(['--baseline', item['path']])
    for item in records['evidence']:
        value = item['value']
        if item in baseline['items'] or value.get('status') != 'success':
            continue
        if ci.ancestor(baseline['head'], value.get('head', '')) and ci.ancestor(value['head'], candidate):
            args.extend(['--evidence', item['path']])
    try:
        result = json.loads(ci.engine(*args))
    except subprocess.CalledProcessError:
        raise ReleaseError('Release coverage is incomplete or incompatible; full Quality fallback is required.') from None
    if result.get('eligible') is not True:
        raise ReleaseError('Release coverage is not eligible')
    return result


def ensure_coverage(manifest, candidate, directory, timeout=2400, force=False):
    """Reuse valid evidence or dispatch one full run for this exact default candidate."""
    reason = 'A required tested artifact is unavailable'
    if not force:
        try:
            return verify_coverage(manifest, candidate, directory)
        except (ReleaseError, RuntimeError) as error:
            reason = str(error)
    if not is_current_candidate(manifest, candidate):
        raise ReleaseError('Candidate was superseded before the full verification fallback')
    request_id = 'release-' + uuid.uuid4().hex
    branch = manifest.get('default_branch', 'main')
    repository = manifest['repository']
    state = {'candidate': candidate, 'request_id': request_id, 'reason': reason,
             'status': 'dispatching', 'started_at': stamp()}
    ci.write(Path(directory)/'fallback.json', state)
    # GitHub dispatch returns no run ID. A unique visible run title binds the response;
    # branch, source identity and full evidence are independently verified below.
    result = subprocess.run(['gh', 'api', '--method', 'POST',
        f'repos/{repository}/actions/workflows/quality.yml/dispatches', '--input', '-'],
        input=json.dumps({'ref': branch, 'inputs': {'mode': 'full', 'request_id': request_id}}).encode(),
        capture_output=True, timeout=60)
    if result.returncode:
        raise ReleaseError('Automatic full verification could not be dispatched; workflow Actions write permission is required')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_current_candidate(manifest, candidate):
            raise ReleaseError('Candidate was superseded during the full verification fallback')
        rows = ci.api(f'repos/{repository}/actions/workflows/quality.yml/runs?event=workflow_dispatch&per_page=100')['workflow_runs']
        matches = [row for row in rows if row.get('display_title') == 'Quality ' + request_id]
        if matches:
            row = matches[0]
            state.update(run_id=row['id'], status=row['status'])
            ci.write(Path(directory)/'fallback.json', state)
            if row.get('head_sha') != candidate:
                raise ReleaseError('Full verification dispatched for a different candidate; release refused')
            if row['status'] == 'completed':
                if not ci.trusted_run(row, repository, branch) or row.get('conclusion') != 'success':
                    raise ReleaseError('Automatic full verification failed or returned untrusted provenance')
                coverage = verify_coverage(manifest, candidate, directory)
                coverage['fallback_run_id'] = row['id']
                return coverage
        time.sleep(20)
    raise ReleaseError('Automatic full verification did not finish within forty minutes')


def smoke(manifest, url):
    command = manifest['release'].get('smoke_command')
    if not command or not url.startswith('https://'):
        raise ReleaseError('A deterministic read-only smoke command and HTTPS URL are required')
    # Keep only tool/runtime paths and the explicitly scoped smoke bypass secret.
    env = {key: value for key, value in os.environ.items() if key in {'PATH','HOME','TMPDIR','PLAYWRIGHT_BROWSERS_PATH'}}
    env.update({'QUALITY_DEPLOYMENT_URL': url, 'CI': 'true', 'QUALITY_LIVE_SMOKE': '1', 'NEXT_TELEMETRY_DISABLED':'1'})
    bypass = os.environ.get('VERCEL_AUTOMATION_BYPASS_SECRET')
    if bypass:
        env['VERCEL_AUTOMATION_BYPASS_SECRET'] = bypass
    result = subprocess.run(['bash', '-eu', '-o', 'pipefail', '-c', command], env=env,
                            capture_output=True, timeout=180)
    # Test output is intentionally not echoed; summaries contain status only.
    return result.returncode == 0


def confirmed_failure(probe, sleep=None):
    sleep = sleep or time.sleep
    for index in range(3):
        if probe():
            return False
        if index < 2:
            sleep(20)
    return True


def run(manifest, candidate, deployment_id, state_path):
    release = manifest['release']
    if release.get('kind') != 'vercel':
        raise ReleaseError('This transport only handles an established Vercel target')
    config = release['vercel']
    token = os.environ.get('VERCEL_TOKEN')
    if not token:
        raise ReleaseError('VERCEL_TOKEN is unavailable. A reusable team deployment credential is required; no plan upgrade is needed.')
    if not re.fullmatch(r'[0-9a-f]{40}', candidate) or not is_current_candidate(manifest, candidate):
        raise ReleaseError('Stale or invalid default-branch candidate')
    coverage = ensure_coverage(manifest, candidate, '.quality-release-history')
    project_id, team = config['project_id'], config['team_id']
    project = vercel('/v9/projects/'+project_id, token, team)
    if project.get('autoAssignCustomDomains') is not False:
        raise ReleaseError('Automatic production domain assignment must be disabled before verified releases')
    if str(project.get('link', {}).get('repoId')) != str(config['repository_id']):
        raise ReleaseError('Hosting project is not linked to the configured stable GitHub repository identity')
    previous = (project.get('targets') or {}).get('production') or {}
    records = deployment_records(manifest)
    if previously_rolled_back(records, candidate):
        raise ReleaseError('This candidate already consumed its one automatic rollback; a new verified candidate is required')
    state = {'schema_version':1, 'repository':manifest['repository'], 'candidate':candidate,
             'started_at':stamp(), 'coverage':coverage, 'previous_id':previous.get('id'),
             'previous_verified':previous_is_verified(records, previous.get('id')), 'promoted':False, 'rollback_attempted':False}
    previous_sha = previous.get('meta', {}).get('githubCommitSha')
    compatible = state_compatible(previous_sha, candidate, release.get('persistent_state_paths', []))
    state['state_compatible'] = compatible
    ci.write(state_path, state)
    if not deployment_id:
        deployments = vercel('/v6/deployments?'+urllib.parse.urlencode({'projectId':project_id, 'target':'production', 'limit':100}), token, team)['deployments']
        matches = [d for d in deployments if d.get('meta',{}).get('githubCommitSha') == candidate
                   and d.get('target') == 'production'
                   and d.get('state', d.get('readyState')) in {'READY', 'INITIALIZING', 'QUEUED', 'BUILDING'}]
        matches.sort(key=lambda d: d.get('state', d.get('readyState')) != 'READY')
        if not matches:
            created = create_staged_deployment(manifest, candidate, project, token, team)
            deployment_id = created['id']
        else:
            deployment_id = matches[0].get('uid') or matches[0].get('id')
        state.update(deployment_id=deployment_id, status='staging')
        ci.write(state_path, state)
        for _ in range(120):
            pending = vercel('/v13/deployments/'+deployment_id, token, team)
            if pending.get('readyState') == 'READY':
                break
            if pending.get('readyState') in {'ERROR', 'CANCELED'}:
                raise ReleaseError('Production-environment build failed')
            time.sleep(10)
        else:
            raise ReleaseError('Production-environment build timed out')
    deployment = vercel('/v13/deployments/'+deployment_id, token, team)
    if (deployment.get('target') != 'production' or deployment.get('readyState') != 'READY'
            or deployment.get('projectId') != project_id or deployment.get('meta',{}).get('githubCommitSha') != candidate):
        raise ReleaseError('Deployment environment, source, project, or readiness mismatch')
    state['deployment_id'] = deployment_id
    state['artifact_url'] = 'https://'+deployment['url']
    ci.write(state_path, state)
    if not smoke(manifest, state['artifact_url']):
        raise ReleaseError('Staged production artifact failed its read-only smoke check')
    if not is_current_candidate(manifest, candidate):
        raise ReleaseError('Candidate was superseded during verification')
    current = vercel('/v9/projects/'+project_id, token, team).get('targets',{}).get('production',{}).get('id')
    if current != state['previous_id']:
        raise ReleaseError('Production changed during verification; refusing to overwrite it')
    # Promote exactly the verified deployment, without a rebuild or database operation.
    record = github_post(f'repos/{manifest["repository"]}/deployments',
                         {'ref':candidate, 'task':'quality-release', 'environment':'production',
                          'auto_merge':False, 'required_contexts':[],
                          'payload':{'artifact_id':deployment_id, 'previous_artifact_id':state['previous_id'],
                                     'persistent_state_compatible':compatible}})
    state['github_deployment_id'] = record['id']
    release_status(manifest, record['id'], 'in_progress', 'quality: promoting verified production artifact')
    vercel(f'/v10/projects/{project_id}/promote/{deployment_id}', token, team, method='POST', body={})
    # Promotion can restore Vercel auto-assignment. Keep the next release staged.
    vercel('/v9/projects/'+project_id, token, team, 'PATCH', {'autoAssignCustomDomains':False})
    wait_current(project_id, deployment_id, token, team)
    state.update(promoted=True, promoted_at=stamp(), status='observing')
    ci.write(state_path, state)
    deadline = time.monotonic()+600
    while time.monotonic() < deadline:
        if confirmed_failure(lambda: smoke(manifest, release['production_url'])):
            current = vercel('/v9/projects/'+project_id, token, team).get('targets',{}).get('production',{}).get('id')
            state['status'] = 'failed'
            if rollback_allowed(state, current, compatible):
                # Persist before mutation so interrupted/retried runs cannot roll back twice.
                state['rollback_attempted'] = True
                state['recovery_started_at'] = stamp()
                ci.write(state_path, state)
                release_status(manifest, record['id'], 'failure', 'quality: rollback attempted')
                vercel(f'/v1/projects/{project_id}/rollback/{state["previous_id"]}', token, team, method='POST', body={})
                wait_current(project_id, state['previous_id'], token, team)
                state['status'] = 'recovered' if smoke(manifest, release['production_url']) else 'recovery_failed'
                state['recovery_completed_at'] = stamp()
            else:
                state['recovery_blocked'] = 'Previous verified artifact, state compatibility, and current candidate identity are required'
            ci.write(state_path, state)
            release_status(manifest, record['id'], 'failure', 'quality: '+state['status'])
            raise ReleaseError('Confirmed production failure: '+state['status'])
        time.sleep(min(20, max(0, deadline-time.monotonic())))
    state.update(status='healthy', completed_at=stamp())
    ci.write(state_path, state)
    release_status(manifest, record['id'], 'success', 'quality: healthy after ten-minute observation')
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--deployment')
    parser.add_argument('--state', default='.quality-release/release.json')
    args = parser.parse_args()
    try:
        result = run(json.loads(Path('.quality.json').read_text(encoding='utf-8')), args.candidate, args.deployment, args.state)
        print(json.dumps({k:result[k] for k in ['status','candidate','deployment_id','completed_at']}))
        return 0
    except (ReleaseError, ValueError, OSError, subprocess.SubprocessError) as error:
        print('Release stopped: '+str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
