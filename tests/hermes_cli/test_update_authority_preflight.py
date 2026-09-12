from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from types import SimpleNamespace
from pathlib import Path

import pytest


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def authority_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "fork.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    git(tmp_path, "init", "--bare", str(remote))
    git(tmp_path, "init", str(seed))
    git(seed, "config", "user.email", "tests@example.invalid")
    git(seed, "config", "user.name", "Hermes tests")
    (seed / "tracked.txt").write_text("one\n", encoding="utf-8")
    git(seed, "add", "tracked.txt")
    git(seed, "commit", "-m", "initial")
    git(seed, "branch", "-M", "codex/hermes-live-current")
    git(seed, "remote", "add", "fork", str(remote))
    git(seed, "push", "-u", "fork", "codex/hermes-live-current")
    git(tmp_path, "clone", "--branch", "codex/hermes-live-current", str(remote), str(checkout))
    git(checkout, "remote", "rename", "origin", "fork")
    git(checkout, "config", "user.email", "tests@example.invalid")
    git(checkout, "config", "user.name", "Hermes tests")
    return checkout, remote, "codex/hermes-live-current"


def test_missing_remote_ref_refuses_with_typed_error(tmp_path: Path) -> None:
    from hermes_cli.update_authority import AuthorityRefusal, UpdateAuthority, probe_authority

    checkout, remote, branch = authority_repo(tmp_path)
    git(checkout, "push", "fork", "--delete", branch)
    authority = UpdateAuthority(
        repo=checkout,
        remote="fork",
        remote_url=str(remote),
        branch=branch,
        tracking_ref=f"refs/remotes/fork/{branch}",
    )

    with pytest.raises(AuthorityRefusal) as raised:
        probe_authority(authority, nonce="nonce-1")

    assert raised.value.code == "AUTHORITY_MISSING"
    assert git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert git(checkout, "status", "--porcelain") == ""


def test_remote_url_mismatch_refuses_before_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_cli import update_authority as module
    from hermes_cli.update_authority import AuthorityRefusal, UpdateAuthority, probe_authority

    checkout, remote, branch = authority_repo(tmp_path)
    authority = UpdateAuthority(
        repo=checkout,
        remote="fork",
        remote_url=str(remote) + "-wrong",
        branch=branch,
        tracking_ref=f"refs/remotes/fork/{branch}",
    )
    calls: list[tuple[str, ...]] = []
    real_git = module._git

    def recording_git(value: UpdateAuthority, *args: str):
        calls.append(args)
        return real_git(value, *args)

    monkeypatch.setattr(module, "_git", recording_git)

    with pytest.raises(AuthorityRefusal) as raised:
        probe_authority(authority, nonce="nonce-2")

    assert raised.value.code == "AUTHORITY_URL_MISMATCH"
    assert all(args[0] != "fetch" for args in calls)


def test_equal_tip_is_noop_authority_probe(tmp_path: Path) -> None:
    from hermes_cli.update_authority import UpdateAuthority, probe_authority

    checkout, remote, branch = authority_repo(tmp_path)
    authority = UpdateAuthority(
        repo=checkout,
        remote="fork",
        remote_url=str(remote),
        branch=branch,
        tracking_ref=f"refs/remotes/fork/{branch}",
    )
    before = git(checkout, "rev-parse", "HEAD")

    probe = probe_authority(authority, nonce="nonce-equal")

    assert probe.topology == "equal"
    assert probe.head == before
    assert probe.remote_tip == before
    assert probe.nonce == "nonce-equal"
    assert git(checkout, "rev-parse", "HEAD") == before
    assert git(checkout, "status", "--porcelain") == ""


def advance_remote(tmp_path: Path, remote: Path, branch: str, *, path: str = "tracked.txt") -> str:
    writer = tmp_path / f"writer-{len(list(tmp_path.glob('writer-*')))}"
    git(tmp_path, "clone", "--branch", branch, str(remote), str(writer))
    git(writer, "config", "user.email", "tests@example.invalid")
    git(writer, "config", "user.name", "Hermes tests")
    target = writer / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("remote advance\n", encoding="utf-8")
    git(writer, "add", path)
    git(writer, "commit", "-m", f"advance {path}")
    git(writer, "push", "origin", branch)
    return git(writer, "rev-parse", "HEAD")


def test_remote_ahead_is_classified_as_fast_forwardable(tmp_path: Path) -> None:
    from hermes_cli.update_authority import UpdateAuthority, probe_authority

    checkout, remote, branch = authority_repo(tmp_path)
    remote_tip = advance_remote(tmp_path, remote, branch)
    authority = UpdateAuthority(
        repo=checkout,
        remote="fork",
        remote_url=str(remote),
        branch=branch,
        tracking_ref=f"refs/remotes/fork/{branch}",
    )
    local_head = git(checkout, "rev-parse", "HEAD")

    probe = probe_authority(authority, nonce="nonce-behind")

    assert probe.topology == "behind"
    assert probe.head == local_head
    assert probe.remote_tip == remote_tip
    assert git(checkout, "rev-parse", "HEAD") == local_head


def test_local_ahead_carried_commits_refuse_without_moving_head(tmp_path: Path) -> None:
    from hermes_cli.update_authority import AuthorityRefusal, UpdateAuthority, probe_authority

    checkout, remote, branch = authority_repo(tmp_path)
    (checkout / "local.txt").write_text("carried\n", encoding="utf-8")
    git(checkout, "add", "local.txt")
    git(checkout, "commit", "-m", "carried local commit")
    local_head = git(checkout, "rev-parse", "HEAD")
    authority = UpdateAuthority(
        repo=checkout,
        remote="fork",
        remote_url=str(remote),
        branch=branch,
        tracking_ref=f"refs/remotes/fork/{branch}",
    )

    with pytest.raises(AuthorityRefusal) as raised:
        probe_authority(authority, nonce="nonce-ahead")

    assert raised.value.code == "AUTHORITY_AHEAD"
    assert git(checkout, "rev-parse", "HEAD") == local_head
    assert git(checkout, "cat-file", "-t", local_head) == "commit"


def test_diverged_authority_refuses_without_moving_head(tmp_path: Path) -> None:
    from hermes_cli.update_authority import AuthorityRefusal, UpdateAuthority, probe_authority

    checkout, remote, branch = authority_repo(tmp_path)
    advance_remote(tmp_path, remote, branch, path="remote.txt")
    (checkout / "local.txt").write_text("local\n", encoding="utf-8")
    git(checkout, "add", "local.txt")
    git(checkout, "commit", "-m", "local divergence")
    local_head = git(checkout, "rev-parse", "HEAD")
    authority = UpdateAuthority(
        repo=checkout,
        remote="fork",
        remote_url=str(remote),
        branch=branch,
        tracking_ref=f"refs/remotes/fork/{branch}",
    )

    with pytest.raises(AuthorityRefusal) as raised:
        probe_authority(authority, nonce="nonce-diverged")

    assert raised.value.code == "AUTHORITY_DIVERGED"
    assert git(checkout, "rev-parse", "HEAD") == local_head


def test_untracked_path_colliding_with_remote_delta_refuses(tmp_path: Path) -> None:
    from hermes_cli.update_authority import AuthorityRefusal, UpdateAuthority, probe_authority

    checkout, remote, branch = authority_repo(tmp_path)
    advance_remote(tmp_path, remote, branch, path="future.txt")
    (checkout / "future.txt").write_bytes(b"local untracked bytes\n")
    authority = UpdateAuthority(
        repo=checkout,
        remote="fork",
        remote_url=str(remote),
        branch=branch,
        tracking_ref=f"refs/remotes/fork/{branch}",
    )

    with pytest.raises(AuthorityRefusal) as raised:
        probe_authority(authority, nonce="nonce-collision")

    assert raised.value.code == "DIRTY_COLLISION"
    assert (checkout / "future.txt").read_bytes() == b"local untracked bytes\n"
    assert "?? future.txt" in git(checkout, "status", "--porcelain")


def test_fast_forward_preserves_noncolliding_tracked_deletion(tmp_path: Path) -> None:
    from hermes_cli.update_authority import UpdateAuthority, apply_fast_forward, probe_authority

    checkout, remote, branch = authority_repo(tmp_path)
    remote_tip = advance_remote(tmp_path, remote, branch, path="remote-only.txt")
    (checkout / "tracked.txt").unlink()
    authority = UpdateAuthority(
        repo=checkout,
        remote="fork",
        remote_url=str(remote),
        branch=branch,
        tracking_ref=f"refs/remotes/fork/{branch}",
    )

    probe = probe_authority(authority, nonce="nonce-deleted")
    applied = apply_fast_forward(probe)

    assert applied == remote_tip
    assert not (checkout / "tracked.txt").exists()
    assert git(
        checkout, "status", "--porcelain", "--untracked-files=all"
    ) == "D tracked.txt"


def test_fast_forward_preserves_unrelated_untracked_identity(tmp_path: Path) -> None:
    from hermes_cli.update_authority import UpdateAuthority, apply_fast_forward, probe_authority

    checkout, remote, branch = authority_repo(tmp_path)
    remote_tip = advance_remote(tmp_path, remote, branch, path="remote-only.txt")
    unrelated = checkout / "gateway" / "delivery_events.py"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"unrelated local bytes\n")
    unrelated.chmod(0o640)
    before = (
        unrelated.read_bytes(),
        unrelated.stat().st_mode & 0o7777,
        hashlib.sha256(unrelated.read_bytes()).hexdigest(),
    )
    authority = UpdateAuthority(
        repo=checkout,
        remote="fork",
        remote_url=str(remote),
        branch=branch,
        tracking_ref=f"refs/remotes/fork/{branch}",
    )
    probe = probe_authority(authority, nonce="nonce-apply")

    applied = apply_fast_forward(probe)

    after = (
        unrelated.read_bytes(),
        unrelated.stat().st_mode & 0o7777,
        hashlib.sha256(unrelated.read_bytes()).hexdigest(),
    )
    assert applied == remote_tip
    assert git(checkout, "rev-parse", "HEAD") == remote_tip
    assert before == after
    assert "?? gateway/delivery_events.py" in git(
        checkout, "status", "--porcelain", "--untracked-files=all"
    )


def test_readiness_token_binds_owner_nonce_and_authority(tmp_path: Path) -> None:
    from hermes_cli.update_authority import (
        UpdateAuthority,
        probe_authority,
        read_and_revalidate_token,
        write_probe_token,
    )

    checkout, remote, branch = authority_repo(tmp_path)
    advance_remote(tmp_path, remote, branch, path="remote-only.txt")
    authority = UpdateAuthority(
        repo=checkout,
        remote="fork",
        remote_url=str(remote),
        branch=branch,
        tracking_ref=f"refs/remotes/fork/{branch}",
    )
    probe = probe_authority(authority, nonce="bound-nonce")
    token_path = tmp_path / "ready.json"

    write_probe_token(probe, token_path, owner_pid=4242)
    revalidated = read_and_revalidate_token(
        token_path,
        authority=authority,
        owner_pid=4242,
        nonce="bound-nonce",
    )

    assert revalidated == probe
    payload = json.loads(token_path.read_text(encoding="utf-8"))
    assert payload["owner_pid"] == 4242
    assert payload["nonce"] == "bound-nonce"
    assert payload["authority"]["remote"] == "fork"
    assert payload["remote_tip"] == probe.remote_tip


def test_module_cli_emits_bound_preflight_token(tmp_path: Path) -> None:
    checkout, remote, branch = authority_repo(tmp_path)
    remote_tip = advance_remote(tmp_path, remote, branch, path="remote-only.txt")
    local_head = git(checkout, "rev-parse", "HEAD")
    token = tmp_path / "authority.json"
    result = subprocess.run(
        [
            str(Path(__file__).parents[2] / ".venv" / "bin" / "python"),
            "-m",
            "hermes_cli.update_authority",
            "preflight",
            "--repo",
            str(checkout),
            "--remote",
            "fork",
            "--remote-url",
            str(remote),
            "--branch",
            branch,
            "--tracking-ref",
            f"refs/remotes/fork/{branch}",
            "--nonce",
            "cli-nonce",
            "--owner",
            "5150",
            "--token",
            str(token),
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output == {"ok": True, "topology": "behind", "head": local_head, "remote_tip": remote_tip}
    assert json.loads(token.read_text(encoding="utf-8"))["nonce"] == "cli-nonce"


def test_cli_authority_tuple_is_all_or_nothing(tmp_path: Path) -> None:
    from hermes_cli.update_authority import AuthorityRefusal, authority_from_namespace

    partial = SimpleNamespace(
        authority_repo=str(tmp_path),
        authority_remote="fork",
        authority_remote_url=None,
        authority_branch="codex/hermes-live-current",
        authority_tracking_ref="refs/remotes/fork/codex/hermes-live-current",
    )
    with pytest.raises(AuthorityRefusal) as raised:
        authority_from_namespace(partial)
    assert raised.value.code == "AUTHORITY_CONFIG_INVALID"

    complete = SimpleNamespace(
        authority_repo=str(tmp_path),
        authority_remote="fork",
        authority_remote_url="git@github.com:NousResearch/hermes-agent.git",
        authority_branch="codex/hermes-live-current",
        authority_tracking_ref="refs/remotes/fork/codex/hermes-live-current",
    )
    authority = authority_from_namespace(complete)
    assert authority is not None
    assert authority.repo == tmp_path
    assert authority.remote == "fork"
