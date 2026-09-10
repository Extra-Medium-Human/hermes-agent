# Hermes development map

Hermes shares an agent core across CLI, messaging gateways, TUI and Desktop. This file contains cross-cutting contracts; subsystem details stay with their owners.

## Product contracts

- Keep the conversation prompt and historical context stable; compression is the intentional exception. Skills/tool/memory changes default to the next session, with an explicit `--now` path for immediate invalidation. Preserve message-role alternation.
- Prefer existing code, CLI + skill, service-gated tools, or plugins before adding permanent core model-tool schemas. Plugins use generic extension contracts; third-party product integrations and new memory providers live outside the core tree.
- User state is profile-scoped through `get_hermes_home()`; user-facing paths use `display_hermes_home()` from `hermes_constants`. `_apply_profile_override()` runs before imports. Profile discovery itself is HOME-anchored so every active profile can list its peers.
- Behavioral settings belong in `config.yaml`; `.env` is for credentials. Preserve feature behavior and user data when fixing boundaries. Outbound telemetry remains opt-in.
- Dependencies have upper bounds; Git dependencies and Actions use commit SHAs. Regenerate `uv.lock` for dependency changes in `pyproject.toml`.

## Work and verification

Work to the requested outcome without adding mandatory reviewer verdicts, approval receipts, planning check-ins, test quotas, or full-suite completion rituals. Choose verification for the changed behavior; documentation-only edits need accurate claims and working references. Reuse passing evidence for unchanged code and stop once the requested behavior is established. Additional review and broad suites are optional task-specific tools, not recurring prerequisites.

When Python tests are useful, use `scripts/run_tests.sh <affected file or directory>`; the wrapper isolates credentials, environment and each test file. Frontend commands are in the relevant `package.json`; select the affected Vitest file or workspace script. This is command guidance, not an instruction to run tests on every edit. Keep disposable state outside real `~/.hermes`; profile checks isolate both `HERMES_HOME` and `Path.home()`. macOS GUI runs need disposable cwd and HOME outside Documents/Desktop/Downloads.

Preserve unrelated edits and shared history. Do not reset a worktree to integrate a branch. Read only references relevant to the files and behavior being changed; a reference map is not a read-everything checklist.

## Find the implementation

`run_agent.py`, `cli.py`, `gateway/run.py`, and `hermes_state.py` are facades with topical siblings. Search the sibling modules for the implementation; inspect the caller binding before patching. Avoid circular module-level facade/sibling imports. In-tree code imports defining modules rather than external plugin compatibility aliases. Keep new behavior in its owning module; update references affected by a symbol move.

Client capabilities are session-scoped: the session source/toolset selects desktop or messaging features across local, SSH and cloud backends. `check_fn` answers reachability/opt-in and is process-cached; it cannot stand in for per-session client identity. `HERMES_DESKTOP` identifies a spawned backend, not every GUI client.

Shared TypeScript state belongs in small feature-owned nanostores; components subscribe with `useStore`, actions read `.get()`. Persist state with its owning atom and explicit connection/profile/session scope. Keep route roots thin, helpers focused, and public props/shared object shapes expressed as interfaces.

| Changed area | Focused reference |
| --- | --- |
| Agent loop, providers, compression | [agent/AGENTS.md](agent/AGENTS.md) |
| CLI, config, update, profiles | [hermes_cli/AGENTS.md](hermes_cli/AGENTS.md) |
| Messaging, adapters, lifecycle | [gateway/AGENTS.md](gateway/AGENTS.md) |
| Tools, toolsets, delegation | [tools/AGENTS.md](tools/AGENTS.md) |
| Plugin loading and compatibility | [plugins/AGENTS.md](plugins/AGENTS.md) |
| TUI and JSON-RPC | [tui_gateway/AGENTS.md](tui_gateway/AGENTS.md) |
| Browser dashboard | [web/AGENTS.md](web/AGENTS.md) |
| Desktop shell and state | [apps/desktop/AGENTS.md](apps/desktop/AGENTS.md) |
| Skills and curator | [skills/AGENTS.md](skills/AGENTS.md) |
| Cron and kanban | [cron/AGENTS.md](cron/AGENTS.md) |

Long-form architecture is in `website/docs/developer-guide/`. `tools/registry.py` registers tools; `model_tools.py` discovers and dispatches them; `toolsets.py` controls agent exposure. State and logs use the active profile. `hermes logs` is the existing log browser.
