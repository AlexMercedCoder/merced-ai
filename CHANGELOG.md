# Changelog

## Unreleased

- Replaced comma-separated group setup with searchable, ordered bot selection; added stable bot
  identities, mention autocomplete, group-aware inspection, conversation naming, derived
  participant sets, and mobile group controls.
- Group runs now expose progressive per-participant status and responses while committing durable
  results in deterministic participant order. Failed-bot retries target only that bot.
- Added dedicated Chromium UI validation and desktop/mobile group-chat screenshot artifacts to CI.
- Group creation now renders the returned session immediately instead of blocking on a second full
  harness probe; group dialogs keep adapter errors visible and completed refreshes clear stale
  loading text.
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
