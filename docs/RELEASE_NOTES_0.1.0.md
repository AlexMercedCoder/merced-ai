# Merced AI 0.1.0 release notes

Merced AI 0.1.0 introduces a local-first OAP broker for collaborating through agent harnesses that
are already installed and authenticated. Merced provides portable profiles, bot bindings, session
records, a normalized CLI, and an optional loopback web UI while leaving model access, tools,
approvals, and sandbox enforcement with the selected harness.

## Highlights

- Discovery and executable adapters for Codex, Claude Code, Gemini CLI, OpenCode, Goose, Loro,
  MagAgent, Anton, DSH, AGY, Pi, Prime Agent, OpenClaw, and Kimi Code CLI.
- OAP validation, portable profile discovery, profile/spec digests, and explicit projection reports.
- Project and user bot bindings with conservative availability-only fallback.
- One-shot asks, interactive chat, durable normalized sessions, JSON automation, and local UI.
- Multi-provider DSH routing and configurable Kimi provider files without storing keys in Merced.
- Bounded shell-free child execution, timeout/cancellation handling, output limits, and structured
  failure normalization.
- Cross-platform detection overrides and bounded Linux, macOS, and Windows installer locations.

## Qualification

All fourteen installed harnesses returned unique exact-token responses through Merced AI in a
disposable workspace. The automated suite contains 46 tests with 75.27% branch-aware coverage. The
wheel and source distribution pass Twine checks, and a clean wheel installation independently
discovered all fourteen local harnesses.

See [MVP validation](MVP_VALIDATION.md) for provider/model details and the Anton atomic-turn safety
finding.

## Important boundaries

- Merced AI is not a sandbox and does not supersede harness policy.
- `installed` inventory status does not verify authentication or model availability.
- Capability declarations are provisional until runtime handshakes are implemented.
- ACP streaming, normalized tool/approval events, native-session resume parity, and OAP Level 2
  writeback are planned beyond the MVP.
- Native OAP schema differences can still require translation, particularly richer permission
  documents consumed directly by older MagAgent releases.

## Before publishing

The repository and package are release-ready locally. Maintainers must still choose the final
version/tag, run the configured cross-platform CI on the public remote, publish artifacts, and
perform the post-PyPI clean-install checks in [the release guide](RELEASING.md).
