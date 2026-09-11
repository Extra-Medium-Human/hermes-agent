# Desktop development map

Desktop is a native Electron + React chat surface using a JSON-RPC agent backend. Electron owns native process/filesystem/window capabilities; the backend owns sessions, tools and model calls; the renderer owns presentation. Native access crosses a narrow typed bridge.

## State and identity contracts

- Backend data is cached by the renderer. Merge refreshes with live/pinned rows, reject stale responses, roll back failed optimistic writes visibly, and keep foreground publication separate from background cache updates.
- Durable navigation uses stored session identity; streaming uses runtime identity; state surviving compression uses lineage identity. Translate these explicitly at boundaries.
- Connection/mode apply keeps the shell mounted and wipes gateway-bound stores before reconnecting. Runtime `HERMES_HOME` changes reload the window. Live profile swaps keep background streams and merge lists; only user selection creates a foreground draft. Socket, active profile and connection state agree after each switch.
- Persisted keys declare global/window/connection/profile/session/project scope. Shared state uses small feature-owned stores, request data uses the query layer, local interaction stays in the component, and non-painting coordination uses refs.
- Background events do not steal focus/navigation. Keyboard ownership follows focus; hidden terminals/live surfaces retain lifecycle. Distinguish loading, empty, reconnecting, degraded and exhausted recovery states.

## Runtime and transport contracts

Use one resolver per precedence policy. Validate a candidate at its actual boundary; failed reads may fall through, while failed authoritative writes surface or roll back. Distinguish missing capability from transient failure. Retries are bounded with a recovery action.

OAuth reconnects mint a fresh WebSocket ticket on every dial. Only confirmed 401/403 or tagged auth rejection means reauthentication; timeout, malformed response and server failures remain connectivity failures. Cached URLs are fallback candidates only for long-lived-token/local auth. A connection probe establishes the actual WebSocket/auth path, not merely HTTP status.

Agent-callable Desktop tools use the session source (`source: 'desktop'`), including remote/cloud gateways. Keep compatibility tied to identified older-runtime capability. Keep hot interactions narrowly subscribed, coalesce cosmetic updates, and publish terminal turn transitions promptly.

## Nous free tier

Free-tier state is pulled from the backend and never latched in the renderer. `free_tier.status` reads local auth state without network access; `free_tier.ack_notice` persists the one-time notice on the identity. The ready screen and own-key strip render the same `notice_pending` state. Sign-in uses the existing Nous OAuth start/poll path, and every entry point opens the same sign-in dialog. Branch on explicit `free_tier_row` / `free_tier` payload fields, never provider display names.

## Focused references and commands

- Backend lifecycle, slash palette and Bot Mode: [src/AGENTS.md](src/AGENTS.md).
- Visual or interaction changes: [DESIGN.md](DESIGN.md).
- The repository root contains work/verification policy. There is no extra Desktop checklist, reviewer gate or full-suite requirement here. Choose relevant commands from [package.json](package.json); `npx vitest run <affected-file>` runs a focused test when useful.
