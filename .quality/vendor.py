#!/usr/bin/env python3
"""Install byte-exact quality tools from a committed central revision; verify pins."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

FILES = {".quality/quality.py": ".quality/quality.py", ".quality/ci.py": ".quality/ci.py",
         ".quality/workflow.py": ".quality/workflow.py", ".quality/release.py": ".quality/release.py", "schemas/quality.schema.json": ".quality/schema.json",
         "tools/vendor-quality.py": ".quality/vendor.py"}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def install(source, target, ref):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise ValueError("Install into a consumer worktree, not the central source checkout")
    revision = git(source, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    if not (target / ".git").exists():
        raise ValueError("Target must be an existing git checkout or worktree")
    # Read every file first so a bad pin cannot leave a half-installed engine.
    contents = {dest: git(source, "show", f"{revision}:{path}") for path, dest in FILES.items()}
    for path in contents:
        destination = target / path
        if destination.is_symlink() or destination.parent.is_symlink():
            raise ValueError("Refusing symlink destination: " + path)
    for path, data in contents.items():
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    manifest = {"schema_version": 1, "source": "deathxdefeat/repository-operations", "revision": revision,
                "files": {path: sha(data) for path, data in contents.items()}}
    (target / ".quality/vendor.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def verify(target):
    target = Path(target).resolve()
    manifest = json.loads((target / ".quality/vendor.json").read_text())
    if manifest.get("schema_version") != 1 or manifest.get("source") != "deathxdefeat/repository-operations":
        raise ValueError("Unsupported vendor source or schema")
    if len(manifest.get("revision", "")) != 40 or any(c not in "0123456789abcdef" for c in manifest["revision"]):
        raise ValueError("Vendor revision must be a full git SHA")
    if set(manifest.get("files", {})) != set(FILES.values()):
        raise ValueError("Missing or unexpected vendor file inventory")
    for path, expected in manifest["files"].items():
        destination = target / path
        if destination.is_symlink() or destination.parent.is_symlink() or not destination.is_file() or sha(destination.read_bytes()) != expected:
            raise ValueError("Vendored file does not match pin: " + path)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "verify"])
    parser.add_argument("target", nargs="?", default=".")
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    try:
        result = install(args.source, args.target, args.ref) if args.command == "install" else verify(args.target)
        print(json.dumps({"status": "success", "revision": result["revision"]}))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(json.dumps({"status": "failure", "error": str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
