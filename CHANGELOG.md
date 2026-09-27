# Changelog

## Unreleased

Targets 0.8.0.

### Fixed

- Prompts no longer ride on the command line for harnesses that accept another channel. Codex,
  Claude Code, Gemini CLI, OpenCode, Goose, Pi, Prime Agent, and Kimi read the prompt from stdin;
  OpenClaw reads it from `--message-file`; Claude Code, Pi, and Prime Agent read the profile system
  prompt from a private temporary file. Before this change a long conversation plus attached
  context (up to about 850 KB) was passed as one argument and could fail with `E2BIG` on Linux or a
  command-line length error on Windows.
- MagAgent, Loro, DSH, and Antigravity still take the prompt as an argument (MagAgent and Loro use
  stdin for AAIS approvals). A new guard refuses any command line over 100 KB on POSIX or 24 KB on
  Windows with a clear message and exit status 7, instead of failing to start the process.
- A corrupt `.merced-ai/aais-presenter.json` no longer stops `merced-ai ui` from starting. The
  file is moved aside as `aais-presenter.corrupt-<timestamp>.json`, the presenter starts with empty
  state, `merced-ai ui` prints a warning, and the web UI shows a dismissible banner naming the kept
  file. State is applied only after the whole file validates, so a bad file never leaves partial
  state, and an invalid AAIS envelope in the file is treated as corruption instead of escaping as an
  unhandled library error.
- Web UI: the light theme no longer renders dark buttons, inputs, and cards with dark text (for
  example "New conversation", the route picker, and secondary buttons); surfaces now use theme
  tokens. Bot identity colors were silently blocked by the page's own Content Security Policy
  (inline `style` attributes) and now apply through the CSSOM. Management cards stack on phones
  instead of squeezing their text into a narrow column, and small secondary text meets a higher
  contrast ratio in both themes.
- Output normalization found by the live smoke suite: Goose returned the user's own message as
  the reply (its JSON transcript lists the user turn first); OpenCode 1.18 returned an empty reply
  (it streams `text` parts rather than role-tagged messages); MagAgent 1.3 returned an empty reply
  (it prints a status line before its JSON and tags turns as `assistant_message` events). Explicit
  top-level answers (`result`, `response`) now win, then the assistant's own turn; a user turn is
  never returned. `loro run` summaries now yield only the "Model response" text, and a Loro
  `provider_error` stop is reported as a failed run instead of as the reply.
- A harness that exits without reading all of stdin is judged by its exit status and output, not
  reported as a broker control-channel failure.

### Added

- Group turns now run write-capable bots that share a workspace one at a time, in participant
  order, and say so: a stderr warning in the CLI (plus `write_serialization` in
  `group ask --json`), and an amber notice above the web composer with queued-participant status.
  Read-only bots still run concurrently. Opt out with `--allow-concurrent-writes` on `group ask`,
  `group chat`, and `session resume`, or the **Run at the same time** toggle in the web UI. See
  [Shared-workspace writes](docs/GROUP_CHAT.md#shared-workspace-writes).
- Every `--workspace/-C` and `--json` option now has help text.
- An opt-in live smoke suite (`MERCED_AI_LIVE_SMOKE=1`, `tests/test_live_smoke.py`) checks that
  every adapter flag appears in the installed harness's help and sends one "Reply with OK" turn
  through each selected harness. Loro and MagAgent run against Nous Portal in a throwaway `HOME`.
  Results for 2026-09-27 are in [validation](docs/MVP_VALIDATION.md).
- A Windows CI test cancels a run whose child has spawned a grandchild and checks that the
  grandchild is gone (`taskkill /T`). It is skipped on other platforms with a reason; the POSIX
  process-group case keeps its own test.

- Harness adapter plugin API (version 1): an adapter is a `HarnessSpec` (descriptor, a build
  function, projection style, output format). Installed packages add adapters through the
  `merced_ai.harnesses` entry-point group; broken or conflicting plugins are skipped and reported
  by `harness list` and `doctor`, and `harness show` names the package that provided an adapter.
  `merced_ai.testing.contract.check_harness_spec` is a contract-test kit for adapter authors. All
  fourteen built-in adapters now use this API (the per-harness `if`/`elif` chain is gone) and pass
  the kit. `MERCED_AI_DISABLE_PLUGINS=1` loads only built-ins. See
  [Harness adapter plugins](docs/PLUGINS.md).

- Agent Client Protocol (ACP) client adapter. Claude Code (`claude-agent-acp`), Gemini CLI
  (`--acp`), Goose (`acp`), and OpenCode (`acp`) now run as ACP agents when the launcher is
  installed: replies stream into the web UI and terminal, permission requests go through the AAIS
  approval dialog (or a terminal prompt) and map to the agent's allow/reject options, a read-only
  profile selects the agent's read-only mode, auto-approve modes are never selected, and
  cancellation sends `session/cancel`. Claude Code, Goose, and OpenCode resume their own session
  on the next turn and receive only the new message (plus what other participants said since);
  Gemini CLI cannot load sessions across processes, so it still replays the transcript. Codex,
  Kimi, and Prime Agent launchers are opt-in (`MERCED_AI_ACP_EXPERIMENTAL`) because they were not
  verified; `MERCED_AI_ACP=0` turns ACP off. The ACP transport claim is back for the four verified
  harnesses only. Reply turns now record the harness session ID.

- Worktree-per-bot group rooms (`group ask/chat --worktrees`, or the option in the web group
  dialog): each write-capable bot works in its own `git worktree` and branch outside the project,
  so they run concurrently without touching your files. Compare their changes with
  `merced-ai group diff` or the web **Compare changes** view, apply one bot's patch with
  `merced-ai group apply` or **Apply to workspace** (only when it applies cleanly; nothing is
  written otherwise), and remove them with `merced-ai group cleanup` or **Discard all
  worktrees**. Non-git workspaces fall back to taking turns. See
  [Worktree isolation](docs/GROUP_CHAT.md#worktree-isolation).

- `merced-ai acp --bot NAME [--bot NAME ...]` serves a bot or a room as an ACP agent over stdio
  (for editors such as Zed): sessions are durable Merced AI conversations with `session/load`,
  ACP harness replies stream through, running a write-capable bot needs the client user's consent
  once per session, and harness permission requests are forwarded as `session/request_permission`.
- Experimental A2A endpoint on the UI server: agent card at `/.well-known/agent-card.json` and
  JSON-RPC `message/send`, `tasks/get`, `tasks/cancel` at `/a2a`, behind the same loopback
  binding and token (`Authorization: Bearer`). Write-capable bots need `metadata.approved`;
  streaming and push notifications are not implemented. See [Serving](docs/SERVING.md).

- Reviewed OAP state-delta inbox (`merced-ai inbox list/show/add/remember/approve/reject/proposal`
  and an **Inbox** page in the web UI). Deltas are validated on arrival, applied only after
  approval with the OAP reference applicator (atomic, revision- and digest-checked, retention,
  history with approver, validated before writing), conflicts never blind-write and id-addressed
  deltas can be rebased, and proposals to change a profile's metadata or spec are decided one at a
  time with computed risk. Implements the OAP Level 2 applicator requirements; the claimed level
  stays 1. See [OAP state inbox](docs/INBOX.md).

### Changed

- Harness descriptors and probes now report two capability sets: `harness_supports` (what the
  harness documents for itself) and `broker_implements` (what Merced AI delivers through its
  adapter). Previously every rich harness advertised streaming, resume, approvals, attachments,
  and model listing, although Merced AI returns output on completion, replays a transcript instead
  of resuming, and relays approvals only for MagAgent and Loro. `harness list`, `harness show`,
  and the web UI Harnesses screen show only broker-implemented capabilities; `harness show` and
  the UI name the unused harness features separately. Probe JSON keeps a deprecated
  `capabilities` field equal to `broker_implements` for this release.
- Removed the ACP transport claim from Gemini, OpenCode, Goose, Pi, Prime Agent, OpenClaw, and Kimi,
  and the `native` transport claim from Codex, Loro, and MagAgent. Every adapter runs a
  structured (or, for Anton, text) subprocess.
- `harness show`, `harness list --json`, and the web UI Harnesses screen report each harness's
  prompt delivery (`stdin`, `file`, or `argv`). The UI harness cache schema moved to 3, so
  snapshots written by 0.7.0 are ignored and re-probed.
- `harness list` shows a "Merced AI implements" column instead of a transport column, prints
  paths relative to `~`, and no longer shows a traceback line as the version of a failed probe.

### Documentation

- README opens with the project's one-line role and a shared "Which tool do I want?" table that
  points to Loro, MagAgent, Mag Command Center, and Merced AI.
- Fixed version drift: the README standards section no longer names 0.4.0, the conformance results
  name 0.7.0, the 0.7.0 changelog entry carries its release date, and the documentation index links
  the 0.7.0 release notes.

### Release engineering

- Added `scripts/check_release_metadata.py`, which checks that `pyproject.toml`, `__version__`,
  the README current-release line, the conformance results, the newest dated changelog heading,
  and the release-notes link agree. CI runs it on every push: advisory on branches, strict on `v*`
  tag builds (the workflow now also triggers on tags).

### Performance

- The run journal is append-only JSON Lines (`.merced-ai/run-events/<run-id>.jsonl`). 0.7.0
  rewrote and fsynced the whole event history on every event, so a long run got slower per event
  (up to 24 MB per write); appending is now constant-cost, reconnect and replay by sequence number
  are unchanged, fsync is batched (at most every 250 ms, always at completion), and 0.7.0 `.json`
  journals are converted on first read. `tests/test_run_journal.py` includes a benchmark that fails
  if the per-event cost grows with run length.
- Routing reuses a harness probe for 30 seconds within one chat, group room, or UI server instead
  of spawning the harness's version (and capability) command on every turn. Set
  `MERCED_AI_PROBE_TTL_SECONDS=0` to probe every turn.

### Internal structure

- The 1,000-line `webui_server.py` is now the `merced_ai.web` package: an app composer, a shared
  `WebContext` with typed read/write authentication dependencies, a harness probe cache service, a
  run service that owns turn planning and supervised execution, and three routers (workspace,
  catalog, conversations). No module is over 300 lines. `merced_ai.webui_server` still exports
  `create_web_app` and `run_web_ui`. The HTTP API is unchanged.

### Type checking

- mypy now runs in CI (Ubuntu) over all of `src/merced_ai` with a lenient baseline
  (`check_untyped_defs`, `no_implicit_optional`, `warn_unused_ignores`) and no excluded modules;
  `tests/test_typing_ratchet.py` fails if any module is excluded again. Fixing the baseline tightened a few real edges: bot edit/delete now reject a binding with
  no file path instead of failing on `None`, and platform-specific locking, liveness, and
  process-group code is gated on `sys.platform` so it type-checks per OS.

### Testing

- OAP and AGS fixture tests find the sibling `open-agent-profile` and `agentic-graph-spec` clones
  when `OAP_FIXTURE_REPO` or `AGS_FIXTURE_REPO` is unset, and skip with a reason that says how to
  enable them when neither exists. An explicitly configured path that is wrong fails collection.
  The OAP upstream fixtures previously resolved to the wrong parent directory and were silently
  skipped on developer machines.
- Approval-presenter and web run tests wait on generous deadlines instead of fixed short polls.
  Each presenter and journal step fsyncs, and under parallel disk load the old one-to-two-second
  budgets failed intermittently.

## 0.7.0 — 2026-09-12

See [release notes](docs/RELEASE_NOTES_0.7.0.md).

- Transactional conversation appends with stable turn IDs and explicit stale-save conflicts.
- Broker-owned runs with bounded replay, reconnect by run ID, and interrupted-run recovery.
- Process-group cancellation on POSIX, tree termination on Windows, and bounded output capture.
- Profile spec-digest checks before group and resumed dispatch.
- Durable approval presenter with idempotent decisions and orphaned-request recovery.

## 0.6.0 — 2026-09-06

- Added an explicit WebMCP capability to harness discovery and marked native MagAgent and Loro
  adapters as eligible providers.
- Added portable bot-level `requiresWebMCP` routing requirements across storage, CLI creation, the
  Web API, and the bot-management UI.
- Made routing fail over past installed but WebMCP-incapable harnesses instead of silently losing
  the requested browser-native capability.
- Exposed WebMCP readiness in CLI and Web UI inventories while keeping execution, credentials,
  approval, and audit policy inside the selected child harness.
- Added routing and persistence regression coverage plus an operational integration guide.

## 0.5.1 — 2026-08-31

- Added an AAIS 1.0 presenter for exact runtime approval requests emitted by MagAgent and Loro.
- Added a durable global Web UI permission modal and bidirectional child-process transport while
  preserving each child harness as the policy authority.
- Kept non-AAIS adapters fail-closed under their native safety modes and retained separate launch
  consent for profile-level preflight review.

## 0.5.0 — 2026-08-30

- Added confirmed conversation deletion and full create/edit/delete lifecycle controls for
  project-local OAP profiles and bot bindings.
- Replaced free-form profile provider/model fields with bounded provider and known-model choices,
  while retaining explicit harness-default options.
- Added API, storage, JavaScript syntax, and lifecycle regression coverage.
- Added prompt-driven OAP profile proposals across the CLI, UI, and agent tool surface, with
  universal `~/.agentprofiles` storage and review-first autonomous subagent behavior.
- Improved portable-profile source labeling, generation progress, provider selection, and complete
  profile/bot lifecycle documentation.

## 0.4.0 — 2026-08-29

- Added a secure workspace-context picker and bounded browser uploads for text, binary, and image
  context, with inline/path delivery manifests and internal-state traversal protections.
- Added durable normalized run telemetry, recent-run inspection, elapsed and partial-failure
  summaries, opt-in desktop completion notifications, and copyable active-harness handoffs.
- Expanded API, storage, security, asset, and browser-facing validation for the new workspace UI.

## 0.3.0 — 2026-08-27

- Raised the OAP support-library floor to 1.0.1 and added AGS 1.0.1 validation and
  deterministic planning support.
- Added conformance evidence and regression coverage for OAP prompt projection and AGS graph
  validation, ordering, reachability, and RFC 8785 graph digests.
- Displayed root-derived OAP trust adjustments and discovery collisions on Web UI profile cards.

## 0.2.0 — 2026-08-25

- Replaced comma-separated group setup with searchable, ordered bot selection; added stable bot
  identities, mention autocomplete, group-aware inspection, conversation naming, derived
  participant sets, and mobile group controls.
- Group runs now expose progressive per-participant status and responses while committing durable
  results in deterministic participant order. Failed-bot retries target only that bot.
- Added dedicated Chromium UI validation and desktop/mobile group-chat screenshot artifacts to CI.
- Group creation now renders the returned session immediately instead of blocking on a second full
  harness probe; group dialogs keep adapter errors visible and completed refreshes clear stale
  loading text.
- UI bootstrap now renders profiles, bots, sessions, and cached harness health without executing
  probes. Bounded detection refreshes progressively in the background with per-harness states and
  explicit refresh controls.
- Added durable multi-bot conversations across CLI, API, and web UI with exact mentions,
  ask-everyone, named-recipient, and round-robin dispatch.
- Added concurrent isolated fan-out with deterministic participant-order persistence, per-bot
  attribution/tool/error events, approval aggregation, partial-failure containment, shared
  cancellation, Markdown export attribution, and legacy session compatibility.
- Turned the optional web UI into a functional local collaboration workspace with bot and harness
  selection, conversation creation/resume/search/export, normalized SSE run lifecycles, safe
  Markdown/code rendering, retries, and cancellable harness subprocesses.
- Added OAP profile editing for instructions, model/provider choice, and edit/shell permission
  requests while preserving unedited profile fields and incrementing revisions atomically.
- Added bot creation with ordered fallbacks, accurate harness health, projection and authority
  inspection, approval preflight, responsive mobile navigation, light/dark themes, accessible
  keyboard behavior, reduced motion, and platform-aware shortcuts.
- Hardened local UI authentication with fragment-token exchange, HTTP-only SameSite cookies,
  cross-origin mutation rejection, CSP, cache prevention, framing/referrer/MIME protections, and
  loopback-only binding.
- Expanded automated UI, security, streaming, cancellation, packaging, accessibility-contract, and
  real-server validation.

## 0.1.0 — 2026-08-23

- Added OAP profile validation, discovery, creation, digests, and prompt assembly.
- Added project and user bot bindings with preferred and fallback harnesses.
- Added discovery for Codex, Claude Code, Gemini CLI, OpenCode, Goose, Loro, and MagAgent.
- Added executable MVP adapters for Codex, Claude Code, Gemini CLI, Loro, and MagAgent.
- Added profile projection and provider-aware model substitution reports.
- Added one-shot asks, interactive chat, durable local sessions, and session resume.
- Added shell-free bounded process execution with timeout and cancellation containment.
- Added JSON automation surfaces, packaging, CI, security guidance, and MVP validation evidence.
- Added an optional loopback-only responsive web UI with ephemeral-token API access.
- Added adapters and discovery for Anton, DSH, AGY, Pi, Prime Agent, OpenClaw, and Kimi Code CLI.
- Qualified OpenCode and Goose command adapters and hardened JSONL embedded-error detection.
- Generated new OAP profiles with explicit revision 1 for cross-harness compatibility.
- Live-qualified all 14 installed harnesses in an unsandboxed disposable workspace, including the
  repaired and atomic Anton bridge.
- Added DSH multi-provider guidance, Kimi alternate-config support, rootless executable discovery,
  and current OpenClaw/AGY invocation compatibility.
- Added cross-platform detection overrides and bounded Linux, macOS, Windows, uv, npm, Homebrew,
  Scoop, Chocolatey, and private-prefix search locations.
- Repaired and live-qualified Anton, made its REPL projection atomic, and normalized its final
  assistant response.
- Added release-grade installation, configuration, detection, architecture, troubleshooting,
  validation, contribution, and release documentation plus GitHub templates.
