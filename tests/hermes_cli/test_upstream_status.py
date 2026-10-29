"""Read-only official-upstream awareness using real disposable Git graphs."""

import importlib
import subprocess
from pathlib import Path

import pytest

URL = "https://github.com/NousResearch/hermes-agent.git"


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def graph(tmp_path, monkeypatch):
    upstream = tmp_path / "official"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    git(upstream, "config", "user.email", "test@example.invalid")
    git(upstream, "config", "user.name", "Test")
    (upstream / "base").write_text("base")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "base")
    fork = tmp_path / "fork"
    git(tmp_path, "clone", str(upstream), str(fork))
    git(fork, "config", "user.email", "test@example.invalid")
    git(fork, "config", "user.name", "Test")
    git(fork, "remote", "set-url", "origin", URL)
    git(fork, "remote", "add", "fork", "https://example.invalid/ryan.git")
    original = subprocess.run
    calls = []

    def transport(args, *a, **kw):
        calls.append(list(args))
        if "fetch" in args:
            assert URL in args
            assert (
                "refs/heads/main:refs/hermes/upstream/main" in args
                or "+refs/heads/main:refs/hermes/upstream/main" in args
            )
            args = [str(upstream) if s == URL else s for s in args]
        return original(args, *a, **kw)

    module = importlib.import_module("hermes_cli.upstream_status")
    monkeypatch.setattr(module.subprocess, "run", transport)
    return module, upstream, fork, calls


def test_current_check_preserves_dirty_work_index_and_fork_authority(graph):
    module, _, fork, calls = graph
    (fork / "base").write_text("staged")
    git(fork, "add", "base")
    (fork / "base").write_text("unstaged")
    (fork / "untracked").write_text("user work")
    gitdir = Path(git(fork, "rev-parse", "--absolute-git-dir"))
    (gitdir / "info/exclude").write_text("ignored-user-work\n")
    (fork / "ignored-user-work").write_text("ignored customization")
    refs = git(fork, "show-ref")
    config = gitdir / "config"
    index = config.with_name("index")
    fetch_head = config.with_name("FETCH_HEAD")
    fetch_head.write_text("preserve this")
    before = (
        git(fork, "rev-parse", "HEAD"),
        index.read_bytes(),
        config.read_bytes(),
        git(fork, "status", "--porcelain"),
        fetch_head.read_bytes(),
    )
    result = module.check_upstream(fork)
    assert result["state"] == "current"
    assert result["upstream_tip"] == result["head"] == before[0]
    assert result["remote_url"] == URL and result["branch"] == "main"
    assert isinstance(result["checked_at"], int)
    assert before == (
        git(fork, "rev-parse", "HEAD"),
        index.read_bytes(),
        config.read_bytes(),
        git(fork, "status", "--porcelain"),
        fetch_head.read_bytes(),
    )
    assert (fork / "base").read_text() == "unstaged"
    assert (fork / "untracked").read_text() == "user work"
    assert (fork / "ignored-user-work").read_text() == "ignored customization"
    assert (
        "\n".join(
            line
            for line in git(fork, "show-ref").splitlines()
            if not line.endswith(" refs/hermes/upstream/main")
        )
        == refs
    )
    assert git(fork, "rev-parse", "refs/hermes/upstream/main") == result["upstream_tip"]
    assert not any("merge" in c or "reset" in c or "stash" in c for c in calls)


@pytest.mark.parametrize(
    "history", ["advanced", "divergent", "fork-only", "merged", "unrelated"]
)
def test_ancestry_not_version_or_unique_change_count_decides_integration(
    graph, history
):
    module, upstream, fork, _ = graph
    if history in ("divergent", "fork-only", "merged"):
        (fork / "custom").write_text("fork customization")
        git(fork, "add", ".")
        git(fork, "commit", "-m", "fork customization")
    if history == "unrelated":
        git(upstream, "checkout", "--orphan", "new-history")
    if history != "fork-only":
        (upstream / "new").write_text("upstream advance")
        git(upstream, "add", ".")
        git(upstream, "commit", "-m", "upstream advance")
        if history == "unrelated":
            git(upstream, "branch", "-M", "main")
    if history == "merged":
        module.check_upstream(fork)
        git(fork, "merge", "--no-edit", "refs/hermes/upstream/main")
    result = module.check_upstream(fork)
    expected = {
        "advanced": "upstream-not-integrated",
        "divergent": "upstream-not-integrated",
        "fork-only": "current",
        "merged": "current",
        "unrelated": "unverified",
    }
    assert result["state"] == expected[history]


@pytest.mark.parametrize(
    "problem",
    ["missing", "wrong-url", "shallow", "fetch-failure", "timeout", "replace"],
)
def test_uncertainty_never_reports_current_even_with_a_stale_success_ref(
    graph, monkeypatch, problem
):
    module, upstream, fork, calls = graph
    assert module.check_upstream(fork)["state"] == "current"
    if problem == "missing":
        git(fork, "remote", "remove", "origin")
    elif problem == "wrong-url":
        git(
            fork,
            "remote",
            "set-url",
            "origin",
            "https://example.invalid/hermes-agent.git",
        )
    elif problem == "shallow":
        shallow = Path(git(fork, "rev-parse", "--absolute-git-dir")) / "shallow"
        shallow.write_text(git(fork, "rev-parse", "HEAD") + "\n")
    elif problem == "replace":
        (fork / "replacement").write_text("replacement")
        git(fork, "add", ".")
        git(fork, "commit", "-m", "replacement")
        git(fork, "replace", "HEAD~", "HEAD")
    else:
        original = module.subprocess.run

        def failing(args, *a, **kw):
            if "fetch" in args:
                if problem == "timeout":
                    raise subprocess.TimeoutExpired(args, 45)
                return subprocess.CompletedProcess(args, 1, "", "offline")
            return original(args, *a, **kw)

        monkeypatch.setattr(module.subprocess, "run", failing)
    result = module.check_upstream(fork)
    assert result["state"] == "unverified"


def test_inherited_git_repository_override_cannot_redirect_the_probe(
    graph, monkeypatch
):
    module, upstream, fork, _ = graph
    monkeypatch.setenv("GIT_DIR", str(upstream / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(upstream))
    result = module.check_upstream(fork)
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert result["state"] == "current"
    assert git(fork, "rev-parse", "refs/hermes/upstream/main") == result["upstream_tip"]
    assert not git(upstream, "for-each-ref", "refs/hermes/")


def test_repo_must_be_the_checkout_root_not_an_inherited_parent(graph):
    module, _, fork, calls = graph
    subdir = fork / "child"
    subdir.mkdir()
    assert module.check_upstream(subdir)["state"] == "unverified"
    assert not any("fetch" in call for call in calls)


def test_head_movement_during_fetch_is_unverified(graph, monkeypatch):
    module, upstream, fork, _ = graph
    (fork / "custom").write_text("new head")
    git(fork, "add", ".")
    git(fork, "commit", "-m", "new head")
    alternate = git(fork, "rev-parse", "HEAD~")
    original = module.subprocess.run

    def moving(args, *a, **kw):
        completed = original(args, *a, **kw)
        if "fetch" in args:
            git(fork, "update-ref", "HEAD", alternate)
        return completed

    monkeypatch.setattr(module.subprocess, "run", moving)
    assert module.check_upstream(fork)["state"] == "unverified"


def test_cli_returns_only_json_for_unverifiable_repository(tmp_path):
    import json
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.upstream_status", "--repo", str(tmp_path)],
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["state"] == "unverified"
    assert payload["head"] is None and payload["upstream_tip"] is None
