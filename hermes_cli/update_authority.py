"""Fail-closed Git authority for updater check/apply/handoff paths."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath


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
    source_path: str | None = None
    source_mode: int | None = None
    source_sha256: str | None = None


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


def _path_identity(
    authority: UpdateAuthority, relpath: str, *, absent_allowed: bool
) -> tuple[int, str]:
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
        if not absent_allowed:
            raise AuthorityRefusal(
                "DIRTY_UNVERIFIED", f"dirty path identity unavailable: {relpath}"
            ) from exc
        mode = 0
        data = b""
    except OSError as exc:
        raise AuthorityRefusal(
            "DIRTY_UNVERIFIED", f"dirty path identity unavailable: {relpath}"
        ) from exc
    return mode, hashlib.sha256(data).hexdigest()


def _head_blob_paths(authority: UpdateAuthority) -> dict[str, list[str]]:
    result = _git(authority, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    if result.returncode:
        raise AuthorityRefusal("DIRTY_UNVERIFIED", "HEAD copy sources could not be read")
    paths_by_blob: dict[str, list[str]] = {}
    for record in result.stdout.split("\0"):
        if not record:
            continue
        try:
            metadata, relpath = record.split("\t", 1)
            _mode, object_type, object_id = metadata.split(" ", 2)
        except ValueError as exc:
            raise AuthorityRefusal("DIRTY_UNVERIFIED", "HEAD copy source data was malformed") from exc
        if object_type == "blob":
            paths_by_blob.setdefault(object_id, []).append(relpath)
    return paths_by_blob


def _index_copy_sources(authority: UpdateAuthority) -> dict[str, str]:
    """Return exact staged copy destinations and their unique HEAD sources."""
    result = _git(
        authority,
        "diff",
        "--cached",
        "--name-status",
        "-z",
        "--find-copies-harder",
        "--find-copies=100%",
        "HEAD",
        "--",
    )
    if result.returncode:
        raise AuthorityRefusal("DIRTY_UNVERIFIED", "staged copy state could not be read")
    fields = result.stdout.split("\0")
    detected: dict[str, str] = {}
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status:
            continue
        path_count = 2 if status.startswith(("C", "R")) else 1
        if index + path_count > len(fields) or any(not value for value in fields[index : index + path_count]):
            raise AuthorityRefusal("DIRTY_UNVERIFIED", "staged copy state was malformed")
        paths = fields[index : index + path_count]
        index += path_count
        if status == "C100":
            source_path, destination_path = paths
            detected[destination_path] = source_path

    if not detected:
        return {}

    paths_by_blob = _head_blob_paths(authority)
    for destination_path, detected_source in detected.items():
        blob = _git(authority, "rev-parse", f":{destination_path}")
        if blob.returncode or not blob.stdout.strip():
            raise AuthorityRefusal("DIRTY_UNVERIFIED", f"staged copy identity unavailable: {destination_path}")
        candidates = paths_by_blob.get(blob.stdout.strip(), [])
        if len(candidates) != 1 or candidates[0] != detected_source:
            raise AuthorityRefusal("DIRTY_UNVERIFIED", f"staged copy source is ambiguous: {destination_path}")
    return detected


def _dirty_manifest(authority: UpdateAuthority) -> tuple[DirtyEntry, ...]:
    result = _git(authority, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if result.returncode:
        raise AuthorityRefusal("DIRTY_UNVERIFIED", "working tree state could not be read")
    fields = result.stdout.split("\0")
    copy_sources = _index_copy_sources(authority)
    entries: list[DirtyEntry] = []
    represented_copy_destinations: set[str] = set()
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        if len(field) < 4 or field[2] != " ":
            raise AuthorityRefusal("DIRTY_UNVERIFIED", "working tree status was malformed")
        status, relpath = field[:2], field[3:]
        source_path = copy_sources.get(relpath)
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                raise AuthorityRefusal("DIRTY_UNVERIFIED", "rename status was malformed")
            porcelain_source = fields[index]
            index += 1
            if source_path is not None and source_path != porcelain_source:
                raise AuthorityRefusal("DIRTY_UNVERIFIED", "copy source attribution changed")
            source_path = porcelain_source
        if relpath in copy_sources:
            represented_copy_destinations.add(relpath)
        mode, digest = _path_identity(authority, relpath, absent_allowed="D" in status)
        source_mode = None
        source_digest = None
        if source_path is not None:
            # A rename binds the absent old path; a copy binds the still-present
            # source. Both identities participate in collision and stale checks.
            source_mode, source_digest = _path_identity(
                authority, source_path, absent_allowed="R" in status
            )
        entries.append(
            DirtyEntry(
                relpath,
                status,
                mode,
                digest,
                source_path,
                source_mode,
                source_digest,
            )
        )
    if represented_copy_destinations != set(copy_sources):
        raise AuthorityRefusal("DIRTY_UNVERIFIED", "staged copy destination was missing from status")
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _remote_delta(authority: UpdateAuthority, head: str, remote_tip: str) -> set[str]:
    result = _git(authority, "diff", "--name-only", "-z", head, remote_tip)
    if result.returncode:
        raise AuthorityRefusal("AUTHORITY_UNVERIFIED", "remote delta could not be read")
    return {path for path in result.stdout.split("\0") if path}


def _probe_filesystem_case_sensitive(authority: UpdateAuthority) -> bool:
    """Probe case behavior on the checkout filesystem without leaving state."""
    stem = f".hermes-case-probe-{uuid.uuid4().hex}"
    lower_path = authority.repo / f"{stem}a"
    upper_path = authority.repo / f"{stem}A"
    result: bool | None = None
    error: OSError | None = None
    try:
        lower_fd = os.open(lower_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(lower_fd)
        try:
            upper_fd = os.open(upper_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if not os.path.samefile(lower_path, upper_path):
                raise OSError("case probe paths collided without identifying the same file")
            result = False
        else:
            os.close(upper_fd)
            result = True
    except OSError as exc:
        error = exc
    finally:
        for candidate in (upper_path, lower_path):
            try:
                candidate.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                error = error or exc
    if error is not None or result is None:
        raise AuthorityRefusal("DIRTY_UNVERIFIED", "checkout filesystem case behavior could not be verified") from error
    return result


def _normalized_git_path(value: str, *, case_sensitive: bool) -> str:
    """Normalize a repo-relative Git path for structural collision checks."""
    normalized = unicodedata.normalize("NFC", PurePosixPath(value).as_posix())
    if not case_sensitive:
        normalized = normalized.casefold()
    return normalized.removeprefix("./").rstrip("/")


def _paths_collide(left: str, right: str, *, case_sensitive: bool) -> bool:
    left_path = _normalized_git_path(left, case_sensitive=case_sensitive)
    right_path = _normalized_git_path(right, case_sensitive=case_sensitive)
    return (
        left_path == right_path
        or left_path.startswith(f"{right_path}/")
        or right_path.startswith(f"{left_path}/")
    )


def _dirty_collision_paths(
    manifest: tuple[DirtyEntry, ...], remote_delta: set[str], *, case_sensitive: bool
) -> list[str]:
    identities = {
        identity
        for entry in manifest
        for identity in (entry.path, entry.source_path)
        if identity
    }
    return sorted(
        identity
        for identity in identities
        if any(_paths_collide(identity, remote_path, case_sensitive=case_sensitive) for remote_path in remote_delta)
    )


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
    case_sensitive = _probe_filesystem_case_sensitive(authority)
    manifest = _dirty_manifest(authority)
    collisions = _dirty_collision_paths(
        manifest,
        _remote_delta(authority, head, remote_tip),
        case_sensitive=case_sensitive,
    )
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


def process_start_identity(pid: int) -> str:
    """Return the canonical Desktop PID-reuse-resistant process identity."""
    if pid <= 0:
        raise AuthorityRefusal("HANDOFF_HELPER_UNVERIFIED", "helper pid is invalid")
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            fields = stat[stat.rfind(")") + 1 :].strip().split()
            started = fields[19]
        except (OSError, IndexError, UnicodeError) as exc:
            raise AuthorityRefusal(
                "HANDOFF_HELPER_UNVERIFIED",
                "helper process start identity is unavailable",
            ) from exc
        if not started.isdigit():
            raise AuthorityRefusal(
                "HANDOFF_HELPER_UNVERIFIED",
                "helper process start identity is malformed",
            )
        return f"linux:{started}"
    if sys.platform == "darwin":
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        started = result.stdout.strip()
        if result.returncode or not started:
            raise AuthorityRefusal(
                "HANDOFF_HELPER_UNVERIFIED",
                "helper process start identity is unavailable",
            )
        return f"ps:{started}"
    raise AuthorityRefusal(
        "HANDOFF_HELPER_UNVERIFIED",
        "helper process start identity is unsupported",
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
    helper_pid = 0
    helper_start_identity = ""
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
            helper_pid = args.helper or os.getpid()
            helper_start_identity = process_start_identity(helper_pid)
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
            helper_pid=helper_pid,
            helper_start_identity=helper_start_identity,
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
