# Quality in the user fork

This adapter belongs to `Extra-Medium-Human/hermes-agent`. It does not publish
to Nous Research, operate its infrastructure, install Hermes, or change the
operator's running application, profiles, conversations, or workspaces.

Use the shared `.quality/quality.py` entrypoint after the pinned engine is
vendored: `doctor`, `bootstrap`, then `run --base <sha> --head <sha>` for the
affected graph, or `full` for the overnight offline regression. The adapter pins
Python, uv, Node and npm and installs `uv.lock` and `package-lock.json` frozen.
The existing Python per-file runner and eight duration-balanced slices remain
authoritative. Retries are disabled for quality evidence so a failing attempt
is not converted into a pass.

Python checks and Electron launches receive disposable homes with no credentials.
Python subprocesses reject remote network connections. Desktop tests use the
real local backend and synthetic inference server, never a paid provider. The
macOS desktop build is reused for the boot/chat smoke; that run disables
screenshots, video and traces and never exercises microphone, screen capture,
installed-app update or permission setup. It fixes portable binary resolution,
inherited Electron mode and runtime-home contamination, and waits for the built
renderer instead of reading the initial blank page's title.

Known exclusions are explicit: the upstream runner excludes integration, live
E2E and Docker directories; SSH and paid-provider evaluations also remain out
of ordinary checks. Full means the declared offline Python/JS regression plus
the nonvisual macOS smoke, not validation of external credentials or all upstream
release platforms. The fork's release capability remains unavailable until an
authorized distribution target has staged activation and recovery evidence.

The required `Quality` check owns automatic selection and evidence. The implementing
agent self-reviews the affected behavior and reuses unchanged passing evidence.
Independent review, review labels, receipt files, frozen-commit verdicts and
upstream infographic/comment rituals are not delivery gates in this user fork.
Runtime safeguards for user data and external effects still apply.
