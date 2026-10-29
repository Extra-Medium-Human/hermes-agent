"""Read-only Nous upstream awareness, separate from fork apply authorization.

Only fetched objects and refs/hermes/upstream/main may be written. This module
never produces an UpdateAuthority, handoff token, or an integration candidate.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from hermes_cli.update_authority import _resolve_git

OFFICIAL_URL = "https://github.com/NousResearch/hermes-agent.git"
UPSTREAM_REF = "refs/hermes/upstream/main"


def check_upstream(repo: Path) -> dict:
    """Compare official main ancestry to the checkout HEAD, failing closed."""
    result = {
        "state": "unverified",
        "head": None,
        "upstream_tip": None,
        "checked_at": int(time.time() * 1000),
        "branch": "main",
        "remote_url": OFFICIAL_URL,
        "message": "Could not verify Nous upstream.",
    }
    # A parent's GIT_DIR/index/config injection must not redirect this probe.
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GCM_INTERACTIVE="Never",
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
    )
    deadline = time.monotonic() + 60
    try:
        repo = repo.resolve(strict=True)
        executable, env = _resolve_git(env)

        def git(*args: str) -> str:
            completed = subprocess.run(
                [executable, "-c", f"core.hooksPath={os.devnull}", *args],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(0.01, min(45, deadline - time.monotonic())),
            )
            if completed.returncode:
                raise RuntimeError("Git could not verify upstream history.")
            return completed.stdout.strip()

        if Path(git("rev-parse", "--show-toplevel")).resolve() != repo:
            raise RuntimeError("Probe path is not the checkout root.")
        if git("remote", "get-url", "origin") != OFFICIAL_URL:
            raise RuntimeError("Origin is not the canonical Nous repository.")
        if git("rev-parse", "--is-shallow-repository") != "false":
            raise RuntimeError("Shallow history cannot prove integration.")
        if git("for-each-ref", "--format=%(refname)", "refs/replace/"):
            raise RuntimeError("Replacement history cannot prove integration.")
        grafts = Path(git("rev-parse", "--git-path", "info/grafts"))
        if not grafts.is_absolute():
            grafts = repo / grafts
        if grafts.exists():
            raise RuntimeError("Grafted history cannot prove integration.")
        head = git("rev-parse", "--verify", "HEAD^{commit}")
        result["head"] = head
        git(
            "-c",
            "gc.auto=0",
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--no-recurse-submodules",
            "--no-auto-maintenance",
            "--refmap=",
            OFFICIAL_URL,
            f"+refs/heads/main:{UPSTREAM_REF}",
        )
        tip = git("rev-parse", "--verify", f"{UPSTREAM_REF}^{{commit}}")
        result["upstream_tip"] = tip
        # Walk both commit graphs before deciding: missing ancestry is unknown,
        # not a count of zero updates. Trees/blobs are not needed for this check.
        git("rev-list", "--count", head, tip)
        base = git("merge-base", head, tip)
        if git("rev-parse", "--verify", "HEAD^{commit}") != head:
            raise RuntimeError("Checkout HEAD changed during upstream verification.")
        result["state"] = "current" if base == tip else "upstream-not-integrated"
        result["message"] = (
            "Nous upstream history is integrated into this checkout."
            if base == tip
            else "Nous upstream history is not fully integrated; this is not a count of unique app changes."
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        result["message"] = (
            str(exc)
            if isinstance(exc, RuntimeError)
            else "Could not verify Nous upstream."
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check_upstream(args.repo)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
