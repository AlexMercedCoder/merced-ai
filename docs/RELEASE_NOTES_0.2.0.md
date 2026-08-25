# Merced AI 0.2.0 release notes

Merced AI 0.2.0 turns the original harness broker into a practical local collaboration workspace.
It adds durable multi-bot conversations and a complete optional web UI while preserving the core
boundary: installed harnesses continue to own model access, tools, credentials, sandboxing, and
final policy enforcement.

## Highlights

- Durable group sessions with ordered participants and attributed assistant turns.
- Exact `@mention`, ask-everyone, named-recipient, and round-robin dispatch from the CLI, API, and
  browser UI.
- Concurrent isolated fan-out with progressive per-bot status, deterministic persistence order,
  partial-failure containment, shared cancellation, and exact failed-bot retry.
- A functional loopback-only web workspace for profiles, bot bindings, single and group chats,
  routing, approvals, transcript search/resume/export, and harness-health inspection.
- Searchable ordered group setup, conversation naming and derivation, stable bot identities,
  mention completion, responsive mobile controls, and refreshed desktop/mobile documentation.
- OAP profile editing with revision-safe preservation of unedited fields, provider/model selection,
  and explicit edit/shell permission requests.
- Hardened fragment-token exchange, HTTP-only SameSite cookies, origin checks, CSP, framing and
  referrer protections, cache prevention, and loopback-only binding for the local UI.
- Immediate workspace bootstrap with cached harness health, progressive background detection,
  per-harness detecting/ready/failure states, and explicit refresh controls.

## Compatibility and validation

- Python 3.11 through 3.14 are exercised across Ubuntu, macOS, and Windows.
- The automated suite contains 66 tests: 65 local tests pass with 79.65% branch-aware coverage and
  one dedicated Chromium test validates the real desktop/mobile collaboration workflow.
- Chromium CI publishes current UI screenshots and preserves failure captures as an artifact.
- Wheel and source distributions pass build and Twine metadata checks.
- The existing fourteen-harness qualification matrix remains unchanged; group orchestration uses
  the same qualified adapter boundary and deterministic fake adapters for paid-provider-free tests.

See [MVP validation](MVP_VALIDATION.md), [group conversations](GROUP_CHAT.md), and the
[UI guide](UI.md) for detailed evidence and runtime boundaries.

## Upgrade notes

- Existing one-bot session JSON remains readable. New group fields have backward-compatible
  defaults.
- Existing OAP profiles and bot bindings require no migration.
- The browser UI remains optional: install `merced-ai[webui]` before running `merced-ai ui`.
- Approval prompts describe requested authority, but the selected harness remains authoritative.
- Token-level streaming, interactive harness approval forwarding, universal native-session resume,
  and OAP Level 2 writeback remain future work.
