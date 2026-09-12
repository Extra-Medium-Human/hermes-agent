"""Fail-closed Git authority for updater check/apply/handoff paths."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class UpdateAuthority:
    repo: Path
    remote: str
    remote_url: str
    branch: str
    tracking_ref: str


@dataclass(frozen=True)
class DirtyEntry:
    path: str
    status: str
    mode: int
    sha256: str


@dataclass(frozen=True)
class AuthorityProbe:
    authority: UpdateAuthority
    topology: str
    head: str
    remote_tip: str
    nonce: str
    dirty_manifest: tuple[DirtyEntry, ...] = ()


class AuthorityRefusal(RuntimeError):
    """Typed refusal raised before an updater may mutate local state."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def authority_from_namespace(args) -> UpdateAuthority | None:
    """Read the update CLI's explicit authority tuple; partial tuples are unsafe."""
    names = (
        "authority_repo",
        "authority_remote",
        "authority_remote_url",
        "authority_branch",
        "authority_tracking_ref",
    )
    values = {name: str(getattr(args, name, "") or "").strip() for name in names}
    if not any(values.values()):
        return None
    missing = [name.removeprefix("authority_") for name, value in values.items() if not value]
    if missing:
        raise AuthorityRefusal(
            "AUTHORITY_CONFIG_INVALID",
            f"explicit update authority is incomplete: {', '.join(missing)}",
        )
    return UpdateAuthority(
        repo=Path(values["authority_repo"]),
        remote=values["authority_remote"],
        remote_url=values["authority_remote_url"],
        branch=values["authority_branch"],
        tracking_ref=values["authority_tracking_ref"],
    )


def _git(authority: UpdateAuthority, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    return subprocess.run(
        ["git", *args],
        cwd=authority.repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _dirty_manifest(authority: UpdateAuthority) -> tuple[DirtyEntry, ...]:
    result = _git(authority, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if result.returncode:
        raise AuthorityRefusal("DIRTY_UNVERIFIED", "working tree state could not be read")
    fields = result.stdout.split("\0")
    entries: list[DirtyEntry] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        if len(field) < 4 or field[2] != " ":
            raise AuthorityRefusal("DIRTY_UNVERIFIED", "working tree status was malformed")
        status, relpath = field[:2], field[3:]
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                raise AuthorityRefusal("DIRTY_UNVERIFIED", "rename status was malformed")
            index += 1
        path = authority.repo / relpath
        try:
            info = path.lstat()
            mode = info.st_mode & 0o7777
            if path.is_symlink():
                data = os.fsencode(os.readlink(path))
            elif path.is_file():
                data = path.read_bytes()
            else:
                data = b""
        except FileNotFoundError as exc:
            if "D" not in status:
                raise AuthorityRefusal(
                    "DIRTY_UNVERIFIED", f"dirty path identity unavailable: {relpath}"
                ) from exc
            # A tracked deletion has a stable identity too: the path must stay
            # absent across the fast-forward. Bind that absence explicitly.
            mode = 0
            data = b""
        except OSError as exc:
            raise AuthorityRefusal("DIRTY_UNVERIFIED", f"dirty path identity unavailable: {relpath}") from exc
        entries.append(DirtyEntry(relpath, status, mode, hashlib.sha256(data).hexdigest()))
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _remote_delta(authority: UpdateAuthority, head: str, remote_tip: str) -> set[str]:
    result = _git(authority, "diff", "--name-only", "-z", head, remote_tip)
    if result.returncode:
        raise AuthorityRefusal("AUTHORITY_UNVERIFIED", "remote delta could not be read")
    return {path for path in result.stdout.split("\0") if path}


def probe_authority(authority: UpdateAuthority, *, nonce: str) -> AuthorityProbe:
    """Fetch and classify exactly one configured remote branch without mutating HEAD."""
    expected_ref = f"refs/remotes/{authority.remote}/{authority.branch}"
    if authority.tracking_ref != expected_ref:
        raise AuthorityRefusal("AUTHORITY_UNVERIFIED", "tracking ref does not match authority tuple")
    url_result = _git(authority, "remote", "get-url", authority.remote)
    if url_result.returncode:
        raise AuthorityRefusal("AUTHORITY_UNVERIFIED", "configured authority remote is missing")
    if url_result.stdout.strip() != authority.remote_url:
        raise AuthorityRefusal("AUTHORITY_URL_MISMATCH", "configured authority remote URL changed")
    branch_result = _git(authority, "branch", "--show-current")
    if branch_result.returncode or branch_result.stdout.strip() != authority.branch:
        raise AuthorityRefusal("AUTHORITY_BRANCH_MISMATCH", "checkout is not on the configured authority branch")
    upstream_remote = _git(authority, "config", "--get", f"branch.{authority.branch}.remote")
    upstream_merge = _git(authority, "config", "--get", f"branch.{authority.branch}.merge")
    if (
        upstream_remote.returncode
        or upstream_remote.stdout.strip() != authority.remote
        or upstream_merge.returncode
        or upstream_merge.stdout.strip() != f"refs/heads/{authority.branch}"
    ):
        raise AuthorityRefusal("AUTHORITY_TRACKING_MISMATCH", "branch does not track the configured authority")
    result = _git(
        authority,
        "fetch",
        "--no-tags",
        authority.remote,
        f"refs/heads/{authority.branch}:{authority.tracking_ref}",
    )
    if result.returncode:
        text = f"{result.stdout}\n{result.stderr}".lower()
        if "couldn't find remote ref" in text or "remote ref does not exist" in text:
            raise AuthorityRefusal("AUTHORITY_MISSING", "configured authority branch is missing")
        raise AuthorityRefusal("AUTHORITY_UNVERIFIED", "configured authority could not be verified")
    head_result = _git(authority, "rev-parse", "HEAD^{commit}")
    tip_result = _git(authority, "rev-parse", f"{authority.tracking_ref}^{{commit}}")
    if head_result.returncode or tip_result.returncode:
        raise AuthorityRefusal("AUTHORITY_UNVERIFIED", "authority objects could not be resolved")
    head, remote_tip = head_result.stdout.strip(), tip_result.stdout.strip()
    manifest = _dirty_manifest(authority)
    collisions = sorted({entry.path for entry in manifest} & _remote_delta(authority, head, remote_tip))
    if collisions:
        raise AuthorityRefusal("DIRTY_COLLISION", f"dirty paths collide with update: {', '.join(collisions)}")
    if head == remote_tip:
        return AuthorityProbe(authority, "equal", head, remote_tip, nonce, manifest)
    local_is_ancestor = _git(authority, "merge-base", "--is-ancestor", head, remote_tip)
    if local_is_ancestor.returncode == 0:
        return AuthorityProbe(authority, "behind", head, remote_tip, nonce, manifest)
    remote_is_ancestor = _git(authority, "merge-base", "--is-ancestor", remote_tip, head)
    if remote_is_ancestor.returncode == 0:
        raise AuthorityRefusal("AUTHORITY_AHEAD", "local authority contains unpublished commits")
    if local_is_ancestor.returncode == 1 and remote_is_ancestor.returncode == 1:
        raise AuthorityRefusal("AUTHORITY_DIVERGED", "local and remote authority histories diverged")
    raise AuthorityRefusal("AUTHORITY_UNVERIFIED", "authority topology could not be classified")


def apply_fast_forward(probe: AuthorityProbe) -> str:
    """Revalidate a bound probe and fast-forward only its current branch."""
    if probe.topology != "behind":
        raise AuthorityRefusal("AUTHORITY_NOT_ADVANCEABLE", "authority is not a viable fast-forward")
    current = probe_authority(probe.authority, nonce=probe.nonce)
    if current != probe:
        raise AuthorityRefusal("HANDOFF_STALE", "authority or dirty manifest changed after readiness")
    result = _git(probe.authority, "merge", "--ff-only", probe.authority.tracking_ref)
    if result.returncode:
        raise AuthorityRefusal("FAST_FORWARD_FAILED", "named authority fast-forward failed")
    head_result = _git(probe.authority, "rev-parse", "HEAD^{commit}")
    branch_result = _git(probe.authority, "branch", "--show-current")
    if (
        head_result.returncode
        or head_result.stdout.strip() != probe.remote_tip
        or branch_result.returncode
        or branch_result.stdout.strip() != probe.authority.branch
    ):
        raise AuthorityRefusal("FAST_FORWARD_UNVERIFIED", "checkout identity did not match authority after update")
    if _dirty_manifest(probe.authority) != probe.dirty_manifest:
        raise AuthorityRefusal("DIRTY_IDENTITY_CHANGED", "unrelated dirty files changed during fast-forward")
    return probe.remote_tip


def _token_payload(probe: AuthorityProbe, owner_pid: int, generated_at: float) -> dict:
    return {
        "schema_version": 1,
        "owner_pid": owner_pid,
        "nonce": probe.nonce,
        "generated_at": generated_at,
        "authority": {**asdict(probe.authority), "repo": str(probe.authority.repo.resolve())},
        "topology": probe.topology,
        "head": probe.head,
        "remote_tip": probe.remote_tip,
        "dirty_manifest": [asdict(entry) for entry in probe.dirty_manifest],
    }


def _payload_digest(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def write_probe_token(probe: AuthorityProbe, path: Path, *, owner_pid: int) -> None:
    """Atomically persist the exact pre-quit authority observation."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _token_payload(probe, owner_pid, time.time())
    document = {**payload, "digest": _payload_digest(payload)}
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        json.dump(document, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(stream.name, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read_and_revalidate_token(
    path: Path,
    *,
    authority: UpdateAuthority,
    owner_pid: int,
    nonce: str,
    max_age_seconds: float = 30.0,
) -> AuthorityProbe:
    """Validate token identity and repeat the full authority/dirty probe."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise AuthorityRefusal("HANDOFF_TOKEN_INVALID", "readiness token is missing or malformed") from exc
    digest = document.pop("digest", None)
    if not isinstance(digest, str) or digest != _payload_digest(document):
        raise AuthorityRefusal("HANDOFF_TOKEN_INVALID", "readiness token digest mismatch")
    expected_authority = {**asdict(authority), "repo": str(authority.repo.resolve())}
    generated_at = document.get("generated_at")
    if (
        document.get("schema_version") != 1
        or document.get("owner_pid") != owner_pid
        or document.get("nonce") != nonce
        or document.get("authority") != expected_authority
        or not isinstance(generated_at, (int, float))
        or generated_at > time.time() + 1
        or time.time() - generated_at > max_age_seconds
    ):
        raise AuthorityRefusal("HANDOFF_TOKEN_MISMATCH", "readiness token identity is stale or mismatched")
    current = probe_authority(authority, nonce=nonce)
    expected = _token_payload(current, owner_pid, generated_at)
    if document != expected:
        raise AuthorityRefusal("HANDOFF_STALE", "authority changed after Desktop preflight")
    return current


def _authority_from_args(args) -> UpdateAuthority:
    return UpdateAuthority(
        repo=Path(args.repo),
        remote=args.remote,
        remote_url=args.remote_url,
        branch=args.branch,
        tracking_ref=args.tracking_ref,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "preflight", "validate"))
    parser.add_argument("--repo", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--remote-url", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--tracking-ref", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--owner", required=True, type=int)
    parser.add_argument("--helper", type=int)
    parser.add_argument("--token", type=Path)
    args = parser.parse_args(argv)
    authority = _authority_from_args(args)
    try:
        if args.action in {"check", "preflight"}:
            probe = probe_authority(authority, nonce=args.nonce)
            if args.action == "preflight" and args.token is None:
                parser.error("preflight requires --token")
            if args.action == "preflight" and probe.topology == "behind":
                write_probe_token(probe, args.token, owner_pid=args.owner)
        else:
            if args.token is None:
                parser.error("validate requires --token")
            probe = read_and_revalidate_token(
                args.token,
                authority=authority,
                owner_pid=args.owner,
                nonce=args.nonce,
            )
    except AuthorityRefusal as exc:
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc)}, sort_keys=True))
        return 2
    output = {
        "ok": True,
        "topology": probe.topology,
        "head": probe.head,
        "remote_tip": probe.remote_tip,
    }
    if args.action == "validate":
        output.update(
            owner_pid=args.owner,
            helper_pid=args.helper or os.getpid(),
            nonce=args.nonce,
            authority={
                "repo": str(authority.repo.resolve()),
                "remote": authority.remote,
                "remoteUrl": authority.remote_url,
                "branch": authority.branch,
                "trackingRef": authority.tracking_ref,
            },
        )
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
