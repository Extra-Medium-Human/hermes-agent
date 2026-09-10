#!/usr/bin/env python3
"""Pinned, standard-library quality selection, execution, and release evidence.

The CI driver is the trust boundary for downloaded Actions evidence. Check commands
receive fixture environment values and a disposable HOME, never the caller's secrets.
This is environment hygiene, not a network or filesystem security sandbox.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit

VERSION = "1.0.0"
POLICY_PATHS = [".quality/**", ".quality.json", ".github/workflows/**", ".github/actions/**"]
RUNNERS = {"ubuntu-24.04", "macos-15"}
KINDS = {"critical", "static", "build", "docs", "full"}
PASS = "success"
UTC = dt.timezone.utc


class QualityError(Exception):
    pass


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def now():
    return dt.datetime.now(UTC).isoformat()


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise QualityError("Evidence timestamps must include a timezone")
    return result


def output(value, path=None):
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(target)
    else:
        print(content, end="")


def git(root, *args, check=True):
    proc = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and proc.returncode:
        raise QualityError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout if check else proc


def revision(root, ref):
    # --end-of-options prevents user-provided refs from becoming git flags.
    return git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()


def ancestor(root, base, head):
    result = git(root, "merge-base", "--is-ancestor", base, head, check=False)
    if result.returncode not in (0, 1):
        raise QualityError("Cannot establish release ancestry")
    return result.returncode == 0


def matches(path, patterns):
    # Match against the entire repository-relative path. **/ also matches zero dirs.
    for pattern in patterns:
        candidates = [pattern]
        while any("**/" in p for p in candidates):
            expanded = {p.replace("**/", "", 1) for p in candidates if "**/" in p}
            expanded -= set(candidates)
            if not expanded:
                break
            candidates.extend(expanded)
        if any(fnmatch.fnmatchcase(path, candidate) for candidate in candidates):
            return True
    return False


def strings(value, label, nonempty=False):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value) or (nonempty and not value):
        raise QualityError(f"{label} must be an array of nonempty strings")


def closure(start, graph):
    result = set(start)
    pending = list(start)
    while pending:
        for dependency in graph.get(pending.pop(), []):
            if dependency not in result:
                result.add(dependency)
                pending.append(dependency)
    return result


def order(ids, graph):
    result, visiting, done = [], set(), set()
    def visit(item):
        if item in visiting:
            raise QualityError(f"Dependency cycle at {item}")
        if item in done:
            return
        visiting.add(item)
        for dependency in sorted(graph.get(item, [])):
            visit(dependency)
        visiting.remove(item)
        done.add(item)
        result.append(item)
    for item in sorted(ids):
        visit(item)
    return result


def validate(manifest):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise QualityError(".quality.json schema_version must be 1")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", manifest.get("repository", "")):
        raise QualityError("repository must be owner/name")
    if not isinstance(manifest.get("default_branch"), str) or not manifest["default_branch"]:
        raise QualityError("default_branch is required")
    checks, components = manifest.get("checks"), manifest.get("components")
    if not isinstance(checks, dict) or not checks or not isinstance(components, dict) or not components:
        raise QualityError("checks and components must be nonempty objects")
    strings(manifest.get("bootstrap", []), "bootstrap")
    fixture_env(manifest.get("bootstrap_env", {}))
    strings(manifest.get("global_inputs", []), "global_inputs")
    if not isinstance(manifest.get("runtimes", {}), dict):
        raise QualityError("runtimes must be an object")
    for name, version in manifest.get("runtimes", {}).items():
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
            raise QualityError(f"runtimes.{name} must pin an exact version")
    for name, check in checks.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not isinstance(check, dict):
            raise QualityError(f"Invalid check id {name}")
        if not isinstance(check.get("command"), str) or not check["command"].strip():
            raise QualityError(f"{name}: command is required")
        if check.get("kind") not in KINDS or check.get("runner") not in RUNNERS:
            raise QualityError(f"{name}: unsupported kind or runner")
        if type(check.get("timeout")) is not int or not 1 <= check["timeout"] <= 21600:
            raise QualityError(f"{name}: timeout must be 1..21600 seconds")
        strings(check.get("inputs", []), f"{name}.inputs")
        strings(check.get("depends_on", []), f"{name}.depends_on")
        for dependency in check.get("depends_on", []):
            if dependency not in checks:
                raise QualityError(f"{name}: unknown check dependency {dependency}")
            if checks[dependency].get("runner") != check["runner"]:
                raise QualityError(f"{name}: dependency {dependency} must use the same runner for artifact reuse")
        fixture_env(check.get("env", {}))
    order(checks, {k: v.get("depends_on", []) for k, v in checks.items()})
    for name, component in components.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not isinstance(component, dict):
            raise QualityError(f"Invalid component id {name}")
        strings(component.get("paths"), f"{name}.paths", True)
        strings(component.get("checks"), f"{name}.checks", True)
        strings(component.get("critical_behaviors"), f"{name}.critical_behaviors", True)
        strings(component.get("depends_on", []), f"{name}.depends_on")
        if set(component["checks"]) - checks.keys():
            raise QualityError(f"{name}: unknown checks")
        if set(component.get("depends_on", [])) - components.keys():
            raise QualityError(f"{name}: unknown component dependencies")
    order(components, {k: v.get("depends_on", []) for k, v in components.items()})
    docs = manifest.get("docs", {})
    if not isinstance(docs, dict):
        raise QualityError("docs must be an object")
    strings(docs.get("paths", []), "docs.paths")
    strings(docs.get("checks", []), "docs.checks")
    for name in docs.get("checks", []):
        if name not in checks or checks[name]["kind"] != "docs":
            raise QualityError("docs.checks must name documentation checks")
    if docs.get("paths") and not docs.get("checks"):
        raise QualityError("Allowlisted documentation requires a real documentation check")
    release = manifest.get("release", {})
    if not isinstance(release, dict) or release.get("kind") not in {"none", "vercel", "fly", "desktop", "package"}:
        raise QualityError("release.kind must declare the actual distribution target")
    for name, capability in release.get("capabilities", {}).items():
        if not isinstance(capability, dict) or capability.get("status") not in {"available", "unavailable", "not_applicable"} or not capability.get("reason"):
            raise QualityError(f"release.capabilities.{name} must declare status and reason")
    return manifest


def load_manifest(root, ref=None):
    try:
        raw = git(root, "show", f"{ref}:.quality.json") if ref else (Path(root) / ".quality.json").read_bytes()
        return validate(json.loads(raw))
    except (OSError, ValueError) as error:
        raise QualityError(f"Invalid .quality.json: {error}") from error


def tree(root, ref):
    result = {}
    for entry in git(root, "ls-tree", "-rz", "--full-tree", ref).split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            result[os.fsdecode(path)] = metadata.decode()
    return result


def changed(root, base, head):
    # NUL delimiters preserve spaces, tabs and newlines. Keep both sides of a rename.
    items = git(root, "diff", "--name-status", "-z", "--find-renames", base, head, "--").split(b"\0")
    paths, i = set(), 0
    while i < len(items) and items[i]:
        status = items[i].decode()
        i += 1
        count = 2 if status.startswith(("R", "C")) else 1
        for _ in range(count):
            if i >= len(items) or not items[i]:
                raise QualityError("Incomplete git diff")
            paths.add(os.fsdecode(items[i]))
            i += 1
    return sorted(paths)


def safe_docs(path, manifest):
    if not matches(path, manifest.get("docs", {}).get("paths", [])):
        return False
    if matches(path, POLICY_PATHS + manifest.get("global_inputs", [])):
        return False
    if any(matches(path, c["paths"]) for c in manifest["components"].values()):
        return False
    return not any(c["kind"] != "docs" and matches(path, c.get("inputs", [])) for c in manifest["checks"].values())


def fingerprints(root, manifest, head):
    entries = tree(root, head)
    policy = {p: h for p, h in entries.items() if matches(p, POLICY_PATHS + manifest.get("global_inputs", []))}
    policy_hash = digest({"engine_version": VERSION, "manifest": manifest, "policy": policy})
    relevant_hash = digest({p: h for p, h in entries.items() if not safe_docs(p, manifest)})
    known_patterns = [p for c in manifest["components"].values() for p in c["paths"]]
    known_patterns += [p for c in manifest["checks"].values() for p in c.get("inputs", [])]
    unknown = {p: h for p, h in entries.items() if not safe_docs(p, manifest) and not matches(p, known_patterns)}
    graph = {k: v.get("depends_on", []) for k, v in manifest["components"].items()}
    check_graph = {k: v.get("depends_on", []) for k, v in manifest["checks"].items()}
    hashes = {}
    for name in order(manifest["checks"], check_graph):
        check = manifest["checks"][name]
        owners = {k for k, component in manifest["components"].items() if name in closure(component["checks"], check_graph)}
        upstream = closure(owners, graph)
        patterns = list(check.get("inputs", []))
        patterns += [p for component in upstream for p in manifest["components"][component]["paths"]]
        patterns += [p for component in upstream for check_id in manifest["components"][component]["checks"] for p in manifest["checks"][check_id].get("inputs", [])]
        if check["kind"] == "docs":
            patterns += manifest.get("docs", {}).get("paths", [])
        inputs = {p: h for p, h in entries.items() if matches(p, patterns)}
        if check["kind"] == "full":
            inputs = {p: h for p, h in entries.items() if not safe_docs(p, manifest)}
        hashes[name] = digest({"policy": policy_hash, "inputs": inputs, "unknown": unknown,
                               "command": check, "dependencies": {x: hashes[x] for x in check_graph[name]}})
    return policy_hash, relevant_hash, hashes


def select(root, manifest, base, head, full=False):
    head = revision(root, head)
    base = revision(root, base)
    paths = changed(root, base, head)
    affected, selected, reasons = set(), set(), []
    components, checks = manifest["components"], manifest["checks"]
    reverse = {k: set() for k in components}
    for name, component in components.items():
        for dependency in component.get("depends_on", []):
            reverse[dependency].add(name)
    for path in paths:
        if matches(path, POLICY_PATHS + manifest.get("global_inputs", [])):
            full = True
            reasons.append(f"verification or shared input changed: {path}")
            continue
        direct = {k for k, component in components.items() if matches(path, component["paths"])}
        check_matches = {k for k, check in checks.items() if matches(path, check.get("inputs", []))}
        direct.update(k for k, component in components.items() if set(component["checks"]) & check_matches)
        affected.update(direct)
        selected.update(check_matches)
        if safe_docs(path, manifest):
            selected.update(manifest.get("docs", {}).get("checks", []))
        elif not direct and not check_matches:
            full = True
            reasons.append(f"unknown impact: {path}")
    affected = closure(affected, reverse)
    if full:
        if not reasons:
            reasons.append("full regression requested")
        affected = set(components)
        selected = set(checks)
    else:
        selected.update(k for component in affected for k in components[component]["checks"] if checks[k]["kind"] != "full")
        # A changed full-suite test or fixture must actually run, even in daytime.
    check_graph = {k: v.get("depends_on", []) for k, v in checks.items()}
    selected = order(selected, check_graph)
    policy_hash, relevant_hash, hashes = fingerprints(root, manifest, head)
    result = {"schema_version": 1, "engine_version": VERSION, "repository": manifest["repository"],
              "base": base, "head": head, "full": full, "changed_paths": paths,
              "components": sorted(affected), "reasons": reasons or (["affected components and their consumers"] if affected else ["allowlisted documentation only" if paths else "no changed inputs"]),
              "policy_hash": policy_hash, "relevant_hash": relevant_hash,
              "checks": [{"id": name, "runner": checks[name]["runner"], "kind": checks[name]["kind"],
                          "depends_on": checks[name].get("depends_on", []), "input_hash": hashes[name],
                          "command_hash": digest(checks[name])} for name in selected],
              "skipped": [{"id": name, "reason": "selector-confirmed unaffected"} for name in sorted(set(checks) - set(selected))]}
    result["selection_hash"] = digest(result)
    return result


def fixture_env(values):
    if not isinstance(values, dict):
        raise QualityError("check env must be an object of test-only strings")
    for key, value in values.items():
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or not isinstance(value, str):
            raise QualityError("check env must contain uppercase names and string literals")
        if key in {"PATH", "HOME", "USERPROFILE", "NODE_OPTIONS", "PYTHONPATH", "BASH_ENV", "ENV", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "GH_TOKEN", "GITHUB_TOKEN", "AWS_PROFILE", "GOOGLE_APPLICATION_CREDENTIALS", "CARGO_HOME", "RUSTUP_HOME", "PNPM_HOME", "COREPACK_HOME"}:
            raise QualityError(f"Unsafe fixture environment key: {key}")
        if any(marker in value for marker in ("${", "$(", "`")):
            raise QualityError(f"Fixture environment must be literal: {key}")
        if re.search(r"(SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY|CREDENTIAL)", key) and value and not re.search(r"(?i)(test|fixture|dummy|mock|local|postgres|anon)", value):
            raise QualityError(f"{key} must use an obvious fixture value")
        if re.search(r"(DATABASE|POSTGRES|REDIS|MONGO|SUPABASE).*URL", key):
            host = urlsplit(value).hostname
            if value and host not in {"localhost", "127.0.0.1", "::1", "postgres", "redis", "db"} and not value.startswith(("file:", "memory:", "pglite:")):
                raise QualityError(f"{key} must point to disposable local storage")
    return values


def safe_environment(home, values=None, root=None):
    # Never inherit arbitrary environment, credentials, npm config, .netrc or user config.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "TERM", "RUNNER_OS", "RUNNER_ARCH", "PNPM_HOME", "COREPACK_HOME", "NVM_DIR", "PLAYWRIGHT_BROWSERS_PATH") if key in os.environ}
    env.update({"HOME": str(home), "USERPROFILE": str(home), "TMPDIR": str(home / "tmp"),
                "TMP": str(home / "tmp"), "TEMP": str(home / "tmp"),
                "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
                "XDG_CACHE_HOME": str(home / "cache"), "CI": "true", "QUALITY_TEST_MODE": "1",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "AWS_EC2_METADATA_DISABLED": "true", "DO_NOT_TRACK": "1", "NEXT_TELEMETRY_DISABLED": "1"})
    for folder in ("tmp", "config", "data", "cache"):
        (home / folder).mkdir(parents=True, exist_ok=True)
    env.update(fixture_env(values or {}))
    if root is not None:
        cache = Path(root).resolve() / ".quality-cache"
        for key, name in {"npm_config_cache": "npm", "PIP_CACHE_DIR": "pip", "PLAYWRIGHT_BROWSERS_PATH": "ms-playwright", "COREPACK_HOME": "corepack"}.items():
            env[key] = str(cache / name)
        cargo_home = cache / "cargo"
        # Cache only Cargo's registry/git downloads, never user configuration or credentials.
        for name in ("config", "config.toml", "credentials", "credentials.toml"):
            if (cargo_home / name).exists() or (cargo_home / name).is_symlink():
                raise QualityError("Cargo test cache contains forbidden configuration or credentials")
        env["CARGO_HOME"] = str(cargo_home)
        toolchains = Path(os.environ.get("RUSTUP_HOME", str(Path.home() / ".rustup")))
        if toolchains.is_absolute() and toolchains.is_dir():
            # rustup contains toolchain installations; Cargo's credential/config home is separate.
            env["RUSTUP_HOME"] = str(toolchains.resolve())
    return env


def check_workspace(root):
    if platform.system() == "Darwin":
        forbidden = {"Documents", "Desktop", "Downloads"}
        if forbidden & set(Path(root).resolve().parts):
            raise QualityError("macOS test cwd must be outside Documents, Desktop and Downloads")
    # Include ignored env files, which frameworks may load despite a scrubbed environment.
    bad = []
    excluded = {".git", "node_modules", ".venv", "venv", "target", ".next", ".quality-results"}
    for directory, folders, files in os.walk(root, followlinks=False):
        folders[:] = [x for x in folders if x not in excluded]
        for name in files:
            if (name == ".env" or name.startswith(".env.")) and not name.endswith((".example", ".sample", ".template")):
                bad.append(str((Path(directory) / name).relative_to(root)))
    if bad:
        raise QualityError("Tests refuse automatically loaded environment files; use a clean worktree and fixture literals: " + ", ".join(sorted(bad)))


def provenance(manifest):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return {"provider": "local"}
    return {"provider": "github-actions", "repository": os.environ.get("GITHUB_REPOSITORY"),
            "run_id": os.environ.get("GITHUB_RUN_ID"), "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "workflow": os.environ.get("GITHUB_WORKFLOW_REF", "").split("@", 1)[0].removeprefix(manifest["repository"] + "/"),
            "sha": os.environ.get("GITHUB_SHA"), "event": os.environ.get("GITHUB_EVENT_NAME"),
            "ref": os.environ.get("GITHUB_REF")}


def execute(command, root, env, timeout, log):
    start = time.monotonic()
    with open(log, "wb") as stream:
        proc = subprocess.Popen(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command], cwd=root, env=env,
                                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
            status = PASS if code == 0 else "failure"
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            code, status = None, "timed_out"
        except BaseException:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise
    return status, code, round(time.monotonic() - start, 3)


def run_checks(root, manifest, selection, runner, evidence_dir):
    if revision(root, "HEAD") != selection["head"]:
        raise QualityError("Check execution must use the selected HEAD")
    # Dirty tracked files would make commit-based input fingerprints dishonest.
    if git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise QualityError("Commit tracked input changes before recording quality evidence")
    untracked = [os.fsdecode(p) for p in git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0") if p]
    unsafe = [p for p in untracked if not matches(p, [".quality-results/**", ".quality-history/**", ".quality-cache/**"])]
    if unsafe:
        raise QualityError("Untracked source inputs cannot receive commit-based evidence: " + ", ".join(unsafe))
    check_workspace(root)
    evidence_dir = Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    selected = [c for c in selection["checks"] if not runner or c["runner"] == runner]
    if not runner and len({c["runner"] for c in selected}) > 1:
        raise QualityError("Multiple operating systems selected; run each --runner separately")
    if selected:
        expected_os = "Darwin" if selected[0]["runner"].startswith("macos") else "Linux"
        if platform.system() != expected_os:
            raise QualityError(f"Selected runner needs {expected_os}; current OS is {platform.system()}")
    record = {k: selection[k] for k in ("schema_version", "engine_version", "repository", "base", "head", "selection_hash", "policy_hash", "relevant_hash")}
    record.update({"mode": "full" if selection["full"] else "affected", "started_at": now(), "runner": runner or (selected[0]["runner"] if selected else "none"), "checks": [], "provenance": provenance(manifest)})
    start = time.monotonic()
    statuses = {}
    evidence_file = evidence_dir / f"evidence-{record['runner']}.json"
    # Persist running status first. Cancellation or host loss cannot leave a green file.
    record["status"] = "running"
    output(record, evidence_file)
    with tempfile.TemporaryDirectory(prefix="quality-home-") as temporary:
        home = Path(temporary)
        for item in selected:
            name = item["id"]
            check = manifest["checks"][name]
            result = {k: item[k] for k in ("id", "input_hash", "command_hash")}
            result.update({"attempt": 1, "started_at": now(), "status": "running"})
            record["checks"].append(result)
            output(record, evidence_file)
            if any(statuses.get(dependency) != PASS for dependency in item["depends_on"]):
                status, code, duration = "blocked", None, 0
            else:
                env = safe_environment(home, check.get("env", {}), root)
                env.update({"QUALITY_BASE": selection["base"], "QUALITY_HEAD": selection["head"]})
                status, code, duration = execute(check["command"], root, env, check["timeout"], evidence_dir / f"{name}.log")
            result.update({"status": status, "exit_code": code, "duration_seconds": duration, "completed_at": now()})
            statuses[name] = status
            print(f"{name}: {status} ({duration}s)", file=sys.stderr)
            output(record, evidence_file)
    record["completed_at"] = now()
    mutated = bool(git(root, "status", "--porcelain", "--untracked-files=no").strip())
    if mutated:
        record["errors"] = ["Check commands modified tracked inputs; commit-based verification evidence is invalid"]
    record["status"] = PASS if all(value == PASS for value in statuses.values()) and not mutated else "failure"
    record["metrics"] = {"duration_seconds": round(time.monotonic() - start, 3),
                         "first_attempt_failures": sum(value != PASS for value in statuses.values()) + int(mutated),
                         "checks_executed": sum(value != "blocked" for value in statuses.values()), "retry_count": 0,
                         "dependency_cache_hit": {"true": True, "false": False}.get(os.environ.get("QUALITY_CACHE_HIT"))}
    output(record, evidence_file)
    return record


def aggregate(selection, records, needs=None):
    errors, results = [], {}
    if selection.get("selection_hash") != digest({k: v for k, v in selection.items() if k != "selection_hash"}):
        errors.append("Selection integrity mismatch")
    expected = {item["id"]: item for item in selection["checks"]}
    if needs is not None:
        if not isinstance(needs, dict) or not needs:
            errors.append("Missing CI job results")
        else:
            for name, job in needs.items():
                selector = needs.get("select", needs.get("selector", {}))
                allowed_empty = not expected and name == "checks" and isinstance(job, dict) and job.get("result") == "skipped" and selector.get("result") == PASS
                if not allowed_empty and (not isinstance(job, dict) or job.get("result") != PASS):
                    errors.append(f"CI job {name}: {job.get('result', 'missing') if isinstance(job, dict) else 'missing'}")
    for record in records:
        for key in ("repository", "head", "base", "selection_hash", "policy_hash", "relevant_hash"):
            if record.get(key) != selection.get(key):
                errors.append(f"Evidence {key} mismatch")
        if record.get("status") != PASS:
            errors.append(f"Evidence run is {record.get('status', 'missing')}")
        for item in record.get("checks", []):
            name = item.get("id")
            if name not in expected or name in results:
                errors.append(f"Unexpected or duplicate result: {name}")
                continue
            results[name] = item
            wanted = expected[name]
            if item.get("status") != PASS or item.get("attempt") != 1 or item.get("exit_code") != 0:
                errors.append(f"Check {name}: {item.get('status', 'missing')} (first attempt required)")
            if item.get("input_hash") != wanted["input_hash"] or item.get("command_hash") != wanted["command_hash"]:
                errors.append(f"Check {name}: inputs or command mismatch")
            if record.get("runner") != wanted["runner"]:
                errors.append(f"Check {name}: wrong runner")
    for name in sorted(set(expected) - results.keys()):
        errors.append(f"Missing selected check: {name}")
    return {"schema_version": 1, "status": "failure" if errors else PASS, "errors": errors,
            "head": selection["head"], "repository": selection["repository"], "selection_hash": selection["selection_hash"],
            "checks": list(results.values()), "completed_at": now()}


def trusted(record, raw_hash, runs, manifest):
    p = record.get("provenance", {})
    if p.get("provider") != "github-actions":
        raise QualityError("Local/self-asserted evidence is not release provenance")
    for run in runs:
        if str(run.get("id")) != str(p.get("run_id")) or str(run.get("run_attempt")) != str(p.get("run_attempt")):
            continue
        if raw_hash not in run.get("evidence_sha256", []):
            raise QualityError("Evidence file is not bound to a downloaded trusted artifact")
        if run.get("repository", {}).get("full_name") != manifest["repository"] or p.get("repository") != manifest["repository"]:
            raise QualityError("Evidence repository mismatch")
        if run.get("status") != "completed" or run.get("conclusion") != PASS:
            raise QualityError("Trusted Actions run did not succeed")
        if run.get("event") not in {"push", "schedule", "workflow_dispatch"} or run.get("event") != p.get("event"):
            raise QualityError("Release evidence must come from a trusted default-branch event")
        if run.get("head_branch") != manifest["default_branch"] or p.get("ref") != f"refs/heads/{manifest['default_branch']}":
            raise QualityError("Release evidence must come from the default branch")
        allowed = manifest.get("trusted_workflows", [".github/workflows/quality.yml"])
        if run.get("path") not in allowed or run.get("path") != p.get("workflow"):
            raise QualityError("Untrusted workflow path")
        if run.get("head_sha") != record.get("head") or run.get("head_sha") != p.get("sha"):
            raise QualityError("Evidence does not match the trusted run commit")
        started = timestamp(record["started_at"])
        completed = timestamp(record["completed_at"])
        if not timestamp(run["created_at"]) <= started <= completed <= timestamp(run["updated_at"]) + dt.timedelta(minutes=1):
            raise QualityError("Evidence timestamps fall outside the trusted run")
        return run
    raise QualityError("No trusted Actions run for evidence")


def release_verify(root, manifest, candidate, baseline_records, affected_records, trusted_runs, at=None, max_age_hours=24):
    candidate = revision(root, candidate)
    current_time = at or dt.datetime.now(UTC)
    fallback = {"status": "full_required", "candidate": candidate, "eligible": False, "reasons": []}
    try:
        if not baseline_records:
            raise QualityError("No successful full baseline supplied")
        bases = {record[0].get("head") for record in baseline_records}
        if len(bases) != 1:
            raise QualityError("Baseline runner evidence must refer to one commit")
        base = revision(root, bases.pop())
        if not ancestor(root, base, candidate):
            raise QualityError("Baseline is not an ancestor of candidate")
        baseline_manifest = load_manifest(root, base)
        baseline_selection = select(root, baseline_manifest, base, base, full=True)
        latest_baseline = max(timestamp(record[0]["completed_at"]) for record in baseline_records)
        for run in trusted_runs:
            if run.get("quality_mode") != "full" or run.get("conclusion") == PASS:
                continue
            if run.get("status") == "completed" and run.get("repository", {}).get("full_name") == manifest["repository"] and run.get("head_branch") == manifest["default_branch"] and run.get("path") in manifest.get("trusted_workflows", [".github/workflows/quality.yml"]):
                if timestamp(run["updated_at"]) >= latest_baseline and ancestor(root, run["head_sha"], candidate):
                    raise QualityError("A newer full regression failed, was cancelled, or timed out")
        # Selection base affects identity but never the full coverage requirement.
        collected = []
        for record, raw_hash in baseline_records:
            trusted(record, raw_hash, trusted_runs, manifest)
            if record.get("mode") != "full" or record.get("status") != PASS:
                raise QualityError("Baseline is not a successful full regression")
            age = current_time - timestamp(record["completed_at"])
            if age < dt.timedelta(0) or (max_age_hours is not None and age >= dt.timedelta(hours=max_age_hours)):
                raise QualityError("Full baseline is missing, future-dated, or at least 24 hours old")
            actual_plan = select(root, baseline_manifest, record["base"], base, full=True)
            if record.get("selection_hash") != actual_plan["selection_hash"]:
                raise QualityError("Baseline selection is inconsistent with its commit")
            normalized = dict(record, base=base, selection_hash=baseline_selection["selection_hash"])
            collected.append(normalized)
        result = aggregate(baseline_selection, collected)
        if result["status"] != PASS:
            raise QualityError("Invalid full baseline: " + "; ".join(result["errors"]))
        candidate_plan = select(root, manifest, base, candidate)
        if baseline_selection["policy_hash"] != candidate_plan["policy_hash"]:
            raise QualityError("Verification, runtime or dependency inputs changed; full fallback required")
        wanted = {item["id"]: item for item in candidate_plan["checks"]}
        # Accept matching relevant-input evidence from any successful trusted ancestor.
        # Consider failed attempts too: a later green may not conceal a regression.
        available = {}
        for record, raw_hash in [*baseline_records, *affected_records]:
            trusted(record, raw_hash, trusted_runs, manifest)
            if not ancestor(root, record["head"], candidate) or not ancestor(root, base, record["head"]):
                raise QualityError("Affected evidence is outside baseline-to-candidate ancestry")
            if record.get("status") != PASS:
                raise QualityError("Unresolved regression failure in supplied evidence")
            recorded_manifest = load_manifest(root, record["head"])
            recorded_plan = select(root, recorded_manifest, record["base"], record["head"], record.get("mode") == "full")
            if any(record.get(key) != recorded_plan[key] for key in ("selection_hash", "policy_hash", "relevant_hash")):
                raise QualityError("Affected evidence does not match its committed selection and inputs")
            recorded_checks = {item["id"]: item for item in recorded_plan["checks"] if item["runner"] == record.get("runner")}
            if set(recorded_checks) != {item.get("id") for item in record.get("checks", [])} or len(recorded_checks) != len(record.get("checks", [])):
                raise QualityError("Affected evidence has missing, unexpected or duplicate checks")
            for item in record.get("checks", []):
                if item.get("status") != PASS or item.get("attempt") != 1 or item.get("exit_code") != 0:
                    raise QualityError("Failed, retried or skipped check in release evidence")
                name = item.get("id")
                if any(item.get(key) != recorded_checks[name][key] for key in ("input_hash", "command_hash")):
                    raise QualityError("Affected evidence contains a forged input fingerprint")
                if name in wanted and all(item.get(key) == wanted[name].get(key) for key in ("input_hash", "command_hash")) and record.get("runner") == wanted[name]["runner"]:
                    available[name] = item
        missing = sorted(wanted.keys() - available.keys())
        if missing:
            raise QualityError("Missing passing affected coverage since full baseline: " + ", ".join(missing))
        return {"status": "eligible", "eligible": True, "candidate": candidate, "baseline": base,
                "checked_changes": candidate_plan["changed_paths"], "checks": sorted(wanted), "reasons": ["Fresh trusted full baseline and matching affected evidence cover all subsequent changes"]}
    except (QualityError, KeyError, ValueError, TypeError) as error:
        fallback["reasons"].append(str(error))
        return fallback


def nightly(root, manifest, head, previous, trusted_runs, at=None, slot=0):
    at = at or dt.datetime.now(UTC)
    from zoneinfo import ZoneInfo
    local = at.astimezone(ZoneInfo("America/Denver"))
    # The Actions schedule owns timezone/staggering. Do not skip a delayed scheduled
    # invocation, and do not expire unchanged inputs merely because a day elapsed.
    eligible = release_verify(root, manifest, head, previous, [], trusted_runs, at, max_age_hours=None)
    _, relevant_hash, _ = fingerprints(root, manifest, revision(root, head))
    unchanged = bool(previous) and all(item[0].get("relevant_hash") == relevant_hash for item in previous)
    if eligible["eligible"] and unchanged:
        return {"run_full": False, "reason": "unchanged relevant inputs with a successful trusted full baseline", "local_time": local.isoformat()}
    return {"run_full": True, "reason": "changed inputs or unavailable successful full baseline", "local_time": local.isoformat()}


def docs_check(root, manifest):
    files = git(root, "ls-files", "-z").split(b"\0")
    errors, count = [], 0
    for raw in files:
        path = os.fsdecode(raw)
        if not path or not matches(path, manifest.get("docs", {}).get("paths", [])) or not (Path(root) / path).is_file():
            continue
        count += 1
        try:
            content = (Path(root) / path).read_text(encoding="utf-8")
        except (UnicodeError, OSError):
            errors.append(f"{path}: cannot read UTF-8 text")
            continue
        if "\x00" in content:
            errors.append(f"{path}: NUL byte in documentation")
        if path.endswith((".md", ".mdx")):
            content = re.sub(r"```.*?```", "", content, flags=re.S)
            for target in re.findall(r"(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)", content):
                target = target.strip().split(' "', 1)[0].strip("<>")
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/") or any(c in target for c in "{}$"):
                    continue
                destination = (Path(root) / path).parent / unquote(parsed.path)
                try:
                    destination.resolve().relative_to(Path(root).resolve())
                except ValueError:
                    errors.append(f"{path}: link escapes repository: {target}")
                    continue
                if not destination.exists():
                    errors.append(f"{path}: missing relative link {target}")
    return {"status": "failure" if errors else PASS, "files_checked": count, "errors": errors}


def read_records(paths):
    result = []
    for path in paths or []:
        raw = Path(path).read_bytes()
        result.append((json.loads(raw), digest(raw)))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("docs")
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--runner", choices=sorted(RUNNERS))
    for name in ("select", "run", "full"):
        command = sub.add_parser(name)
        command.add_argument("--base", default="HEAD")
        command.add_argument("--head", default="HEAD")
        command.add_argument("--output")
        command.add_argument("--runner", choices=sorted(RUNNERS))
        command.add_argument("--evidence-dir", default=".quality-results")
        if name == "select":
            command.add_argument("--full", action="store_true")
    aggregate_parser = sub.add_parser("aggregate")
    aggregate_parser.add_argument("--selection", required=True)
    aggregate_parser.add_argument("--evidence", action="append", default=[])
    aggregate_parser.add_argument("--needs-json")
    aggregate_parser.add_argument("--output")
    release = sub.add_parser("release")
    release_sub = release.add_subparsers(dest="release_command", required=True)
    verify = release_sub.add_parser("verify")
    verify.add_argument("--candidate", default="HEAD")
    verify.add_argument("--baseline", action="append", default=[])
    verify.add_argument("--evidence", action="append", default=[])
    verify.add_argument("--trusted-runs", required=True)
    verify.add_argument("--output")
    scheduled = sub.add_parser("nightly")
    scheduled.add_argument("--head", default="HEAD")
    scheduled.add_argument("--previous", action="append", default=[])
    scheduled.add_argument("--trusted-runs")
    scheduled.add_argument("--slot", type=int, default=0)
    scheduled.add_argument("--now")
    scheduled.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        root = Path(git(args.root, "rev-parse", "--show-toplevel").decode().strip())
        manifest = load_manifest(root)
        if args.command == "doctor":
            missing = [name for name in ("git", "bash", "python3") if not shutil.which(name)]
            missing.extend(name for name in manifest.get("prerequisites", []) if not shutil.which(name))
            mismatches = []
            binaries = {"python": "python3", "node": "node", "pnpm": "pnpm", "npm": "npm", "rust": "rustc", "go": "go"}
            for runtime, wanted in manifest.get("runtimes", {}).items():
                binary = binaries.get(runtime, runtime)
                if not shutil.which(binary):
                    missing.append(binary)
                    continue
                actual = subprocess.run([binary, "--version"], capture_output=True, text=True).stdout
                if not re.search(r"(?<!\d)" + re.escape(wanted) + r"(?![\d.])", actual):
                    mismatches.append(f"{runtime}: needs {wanted}, found {actual.strip()}")
            result = {"status": "failure" if missing or mismatches else PASS, "repository": manifest["repository"], "engine_version": VERSION,
                      "missing": missing, "runtime_mismatches": mismatches, "release": manifest["release"]}
        elif args.command == "docs":
            result = docs_check(root, manifest)
        elif args.command == "bootstrap":
            check_workspace(root)
            results = []
            with tempfile.TemporaryDirectory(prefix="quality-bootstrap-") as temporary:
                home = Path(temporary)
                env = safe_environment(home, manifest.get("bootstrap_env", {}), root)
                for index, command in enumerate(manifest.get("bootstrap", [])):
                    status, code, duration = execute(command, root, env, 1800, home / f"bootstrap-{index}.log")
                    if status != PASS:
                        print((home / f"bootstrap-{index}.log").read_text(encoding="utf-8", errors="replace"), file=sys.stderr)
                        raise QualityError(f"Bootstrap command {index + 1} {status} (exit {code})")
                    results.append({"step": index + 1, "status": status, "duration_seconds": duration})
            result = {"status": PASS, "steps": results}
        elif args.command in {"select", "run", "full"}:
            selection = select(root, manifest, args.base, args.head, full=args.command == "full" or getattr(args, "full", False))
            if args.command == "select":
                result = selection
            else:
                result = run_checks(root, manifest, selection, args.runner, args.evidence_dir)
        elif args.command == "aggregate":
            selection = json.loads(Path(args.selection).read_text(encoding="utf-8"))
            expected_selection = select(root, manifest, selection["base"], selection["head"], selection["full"])
            if expected_selection != selection:
                raise QualityError("Selection does not match the current adapter and committed inputs")
            records = [record for record, _ in read_records(args.evidence)]
            needs = json.loads(Path(args.needs_json).read_text(encoding="utf-8")) if args.needs_json else None
            result = aggregate(selection, records, needs)
        elif args.command == "release":
            runs = json.loads(Path(args.trusted_runs).read_text(encoding="utf-8"))
            result = release_verify(root, manifest, args.candidate, read_records(args.baseline), read_records(args.evidence), runs)
        else:
            runs = json.loads(Path(args.trusted_runs).read_text(encoding="utf-8")) if args.trusted_runs else []
            result = nightly(root, manifest, args.head, read_records(args.previous), runs,
                             timestamp(args.now) if args.now else None, args.slot)
        output(result, getattr(args, "output", None))
        return 0 if result.get("status", PASS) in {PASS, "eligible"} else (3 if result.get("status") == "full_required" else 1)
    except (QualityError, OSError, ValueError, TypeError, KeyError) as error:
        output({"status": "failure", "error": str(error)}, getattr(args, "output", None))
        return 1


if __name__ == "__main__":
    sys.exit(main())
