# Merced AI Product Requirements Document

Status: MVP implemented; post-MVP milestones active
Product name: Merced AI
CLI command: `merced-ai`
Initial package: `merced_ai`

## 1. Summary

Merced AI is a local-first broker for existing AI agent harnesses. It does not implement its own
model loop, tool runtime, or provider layer. Instead, it discovers installed harnesses, normalizes
their capabilities behind adapters, and lets users create durable bots from Open Agent Profile
(OAP) documents. Users can chat and collaborate with one or several bots through a CLI and an
optional local web UI.

OAP describes who a bot is and what it has learned. The selected harness remains responsible for
reasoning, tool execution, authentication, sandboxing, and final permission enforcement.

## 2. Problem

Users often have several capable agent harnesses installed, such as Codex, Claude Code, Gemini
CLI, OpenCode, Goose, Loro, and MagAgent. Each has a different invocation model, event format,
session lifecycle, permission surface, and agent-definition format. A named assistant created in
one harness is difficult to reuse in another, and users cannot easily see how faithfully a portable
profile will run on a given harness.

Building another full harness would duplicate mature agent loops and tool ecosystems. Merced AI
instead supplies the missing client and portability layer.

## 3. Goals

1. Discover supported harnesses already available on the machine.
2. Report installation, compatibility, authentication readiness, transport, and capabilities
   without leaking credentials.
3. Normalize harness lifecycle operations behind a stable adapter interface.
4. Discover, validate, resolve, and run OAP profiles without changing OAP semantics.
5. Define bots as bindings between portable profiles and local harness preferences.
6. Provide interactive chat, one-shot tasks, session resume, cancellation, streamed events, and
   approvals through one CLI contract.
7. Report every profile field that is mapped, narrowed, substituted, dropped, or unsupported.
8. Support reviewed OAP state writeback without allowing a profile to widen its own authority.
9. Offer an optional loopback-first web UI using the same application service as the CLI.
10. Make third-party harness adapters testable and installable without modifying the core.
11. Let users convene multiple independently routed OAP bots in an attributed, bounded room.

## 4. Non-goals

- Implementing a new LLM provider abstraction or agent loop.
- Reimplementing tools already owned by a harness.
- Silently translating a profile when semantic equivalence cannot be established.
- Circumventing a harness's authentication, approvals, sandbox, or policy.
- Treating OAP as an agent-to-agent wire protocol.
- Installing harnesses or network packages automatically during discovery.
- Autonomous or recursive bot-to-bot orchestration; every group turn remains user-triggered.
- Remote, multi-user hosting in the first release.

## 5. Users and primary stories

### Individual developer

- I can see which harnesses are installed and ready.
- I can create an OAP-backed reviewer bot and run it with Codex today and Claude tomorrow.
- I can see when the second harness cannot honor part of the profile.
- I can resume a previous conversation when the harness supports it.

### Harness and adapter author

- I can implement one typed adapter contract.
- I can validate it with reusable fixtures and conformance tests.
- I can preserve harness-specific events without weakening the portable core.

### OAP profile author

- I can validate and preview a profile against each detected harness.
- I can review proposed state changes before they are written.
- I can keep a profile portable while choosing local harness preferences separately.

## 6. Product concepts

### Profile

An OAP `AgentProfile` document. It owns portable identity, instructions, requested capabilities,
context, state, and revision history.

### Harness

An external agent runtime such as MagAgent, Codex, or Gemini CLI. The harness is the final authority
for model access, tools, permissions, and execution.

### Adapter

A translation layer between Merced AI's normalized lifecycle and one harness or protocol.

### Bot

A local binding containing an OAP profile reference, preferred harness, optional fallbacks,
workspace defaults, and session preferences. Harness bindings are not embedded into the portable
profile by default.

### Session

A durable Merced AI record linked to one or more ordered bot participants. Each participant pins
its routed harness, OAP profile revision, profile digest, and spec digest when the session starts.
Assistant turns retain bot and harness attribution. Legacy one-bot fields remain readable.

### Group conversation

A session with two to twelve bots. Explicit mentions, ask-everyone, named-recipient, and round-robin
dispatch choose participants. Multi-recipient work runs concurrently but results are committed in
participant order. Participant failure is isolated, cancellation is shared, and no assistant
response automatically triggers another bot.

## 7. Core architecture

```text
CLI / optional Web UI
          |
   Application Service
          |
  Bots / Sessions / OAP
          |
     Harness Router
          |
    Adapter Registry
          |
 ACP | native RPC/SDK | structured subprocess | degraded text subprocess
```

Transport preference:

1. Native harness API or SDK.
2. Agent Client Protocol (ACP).
3. Structured noninteractive subprocess output.
4. Plain-text subprocess compatibility mode, only when explicitly enabled.

The application layer must not contain harness-specific command construction or event parsing.

## 8. Harness discovery

Discovery is driven by registered adapter descriptors. Each descriptor declares executable names,
supported transports, version requirements, and a bounded probe implementation.

The discovery pipeline must:

1. Resolve configured executable paths.
2. Search `PATH` for known executable names.
3. Run a noninteractive version probe with a timeout and output bound.
4. Attempt a transport-specific capability handshake where available.
5. Check authentication readiness without persisting secrets.
6. Return a structured inventory record.
7. Cache results with explicit refresh and version/path invalidation.

Harness states:

- `not_installed`
- `installed`
- `incompatible`
- `needs_auth`
- `ready`
- `degraded`
- `probe_failed`

Discovery must not scan the full filesystem, invoke a shell, auto-install bridges, contact the
network without an explicit adapter action, or treat the presence of an environment variable as
proof of successful authentication.

## 9. Adapter contract

Every adapter must support metadata and probing. Session methods may report an explicit unsupported
capability during early qualification.

Required conceptual methods:

- `probe()`
- `capabilities()`
- `list_models()`
- `project_profile(resolved_profile, broker_policy)`
- `create_session(request)`
- `resume_session(native_session_id)`
- `send(session, message)` as a stream of normalized events
- `cancel(session)`
- `close(session)`
- `respond_to_approval(request_id, decision)`

Profile projection returns:

- effective profile
- harness configuration
- support level: `native`, `projected`, `degraded`, or `unsupported`
- mapped fields
- narrowed fields
- substituted fields
- dropped fields
- warnings and evidence

Normalized event types initially include:

- `session.started`
- `assistant.text.delta`
- `assistant.thinking.delta`
- `tool.started`
- `tool.output`
- `tool.completed`
- `approval.requested`
- `usage.updated`
- `artifact.created`
- `session.completed`
- `session.failed`

Unknown native events are retained under namespaced metadata when safe.

## 10. OAP requirements

Merced AI should consume the reference `open-agent-profile` package rather than fork its schema or
writeback algorithms.

The OAP layer owns discovery, parsing, validation, inheritance, digest verification, prompt
assembly, state injection, delta validation, conflict detection, history, and atomic writes.

Effective authority is always an intersection:

```text
resolved profile request
  intersection broker policy
  intersection adapter capabilities
  intersection actual harness policy
```

The actual harness has the final decision. If an adapter cannot verify the result, Merced AI labels
the projection provisional. It must not claim OAP conformance from schema validation alone.

Profile projection strategies, in descending quality:

1. Native OAP support.
2. Native session system instructions and policy controls.
3. Ephemeral harness-native agent configuration.
4. Labeled prompt-prefix compatibility mode with explicit user consent.

OAP Level 1 is required before general release. Level 2 state writeback follows after real-world
profile execution is stable. Level 3 features are added incrementally and advertised individually.

## 11. Bot storage and precedence

Merced AI supports both user-global and project-local bots.

- User root: platform-appropriate Merced AI config directory.
- Project root: `<workspace>/.merced-ai/bots/`.
- Portable OAP profiles remain discoverable from `.agents/` and other roots defined by OAP.

Project-local bot bindings override same-named user bindings with a visible warning. Duplicate names
within one root are errors.

Example binding:

```yaml
kind: BotBinding
apiVersion: merced.ai/v1alpha1
metadata:
  name: reviewer
spec:
  profile: .agents/reviewer.agent.yaml
  harness:
    preferred: codex
    fallbacks: [claude, gemini]
  workspace: .
  session:
    resume: true
```

## 12. CLI requirements

Initial command groups:

```text
merced-ai init
merced-ai get-started
merced-ai status
merced-ai doctor

merced-ai harness list|show|probe|refresh|configure|set-default|register
merced-ai profile list|show|validate|create|edit|clone|import|export|diff|effective
merced-ai bot list|show|create|edit|delete|test
merced-ai chat <bot>
merced-ai ask <bot> <prompt>
merced-ai group chat <bot> <bot> [<bot>...]
merced-ai group ask <bot> <bot> [<bot>...] --prompt <prompt> [--json]
merced-ai run <bot> <prompt>
merced-ai session list|show|resume|cancel|export
merced-ai approval list|allow|deny
merced-ai state inbox|show|apply|reject
merced-ai ui
```

CLI rules:

- Read commands support `--json`.
- Streaming commands support JSON Lines output.
- `--dry-run` resolves routing and projection without launching a harness.
- `--explain` shows routing and projection decisions.
- Exit codes distinguish configuration, authentication, compatibility, policy, and runtime errors.
- Destructive state operations identify the exact path and revision.

## 13. Optional UI

The UI is desirable but follows the stable CLI/application contract. `merced-ai ui` starts it only
when explicitly requested.

Initial surfaces:

- bot and profile navigation
- conversation workspace
- single- and multi-bot conversation creation with participant/dispatch controls
- harness and model selection
- effective-authority and profile-projection review
- streamed tool activity
- approval requests
- profile editor and validation
- state-delta inbox
- harness health and session history

The server binds only to loopback and uses an ephemeral access token. UI code cannot widen broker
or harness permissions.

## 14. Persistence

Use SQLite for normalized session metadata, events, routing decisions, and projection records.
Store OAP profiles and bot bindings as reviewable files. Do not duplicate profile state into SQLite
as an authoritative copy.

Native harness transcripts may be referenced by identifier. Merced AI stores only the normalized
events required for continuity, audit, and OAP reconciliation, subject to configurable retention.

## 15. Security requirements

- Spawn processes without a shell.
- Bound probe and session startup time and output.
- Use a minimized, allowlisted environment for adapters where practical.
- Never log credentials or full environments.
- Treat harness output and OAP state as untrusted data.
- Keep OAP state delimited from authoritative instructions.
- Never apply an OAP delta outside `/state`.
- Revision-check and atomically apply all profile writes.
- Do not auto-launch profile-declared MCP commands from imported or project profiles without review.
- Pin the profile revision, spec digest, adapter version, executable path, and harness version per
  session.
- Make permission ownership visible; no layer may represent a broader grant than the harness gives.

## 16. Initial adapter roadmap

1. Mock adapter for contract development.
2. Generic ACP adapter.
3. MagAgent native OAP adapter.
4. Codex App Server adapter.
5. Loro native OAP adapter.
6. Gemini ACP qualification.
7. Claude Code structured-stream qualification.
8. OpenCode and Goose qualification.
9. Third-party adapter plugin API.

## 17. Delivery milestones

The shipped MVP cuts a usable vertical slice across M0 through M3. It uses qualified structured
subprocess transports for the first release; generic ACP streaming, live approval forwarding, and
native harness-session resume remain the next M3 increments. Local Merced AI sessions are durable
and resumable now by replaying a bounded attributed transcript. M5's delivered UI also includes
bounded multi-bot rooms without autonomous agent loops.

### M0: Scaffold

- Python package and CLI entry point.
- Typed harness descriptor, probe, capability, and event records.
- Adapter protocol and registry.
- Safe executable discovery.
- Mock adapter and foundational tests.

### M1: Inventory and doctor

- Built-in descriptors for the initial harness set.
- Bounded version and capability probes.
- Structured `harness list`, `show`, `probe`, and `doctor` output.
- Cache and explicit refresh.

Exit: local inventory is deterministic, fast, cross-platform, and secret-safe.

### M2: OAP Level 1 and bots

- Reference package integration.
- Profile discovery, validation, resolution, and projection.
- Bot binding schema and lifecycle.
- Dry-run routing and effective-authority report.

Exit: a user can prove what a bot would become on each detected harness before running it.

### M3: First real sessions

- Generic ACP, MagAgent, and Codex adapters.
- Chat, ask, streaming events, approvals, cancellation, and durable sessions.

Exit: one OAP bot runs through all three adapters using the same client contract.

### M4: OAP Level 2

- State injection and evidence-based delta generation.
- Delta inbox, approval, conflict handling, and atomic application.

Exit: OAP conformance and hostile-input suites pass for implemented Level 2 behaviors.

### M5: Optional local UI

- Thin browser client over the application service.
- Profile/bot management, chat, approvals, projections, health, and history.
- Attributed group rooms with deterministic concurrent fan-out and participant-level failures.

Exit: CLI and UI produce equivalent application-level results.

### M6: Ecosystem

- Additional qualified harnesses.
- Adapter packaging and compatibility contract.
- Generated compatibility matrix and integration test kit.

## 18. Quality and acceptance criteria

- Unit tests do not require installed harnesses or network access.
- Each adapter has captured protocol fixtures and contract tests.
- Live qualification tests are opt-in and never run in ordinary CI.
- Detection of one broken harness does not prevent listing other harnesses.
- Unknown or unsupported capabilities fail closed.
- No adapter may silently discard an OAP field that affects behavior or authority.
- Core startup and cached inventory should complete in under 300 ms on a typical development
  machine; live refresh may be slower and reports per-probe timing.
- Supported platforms are Linux, macOS, and Windows; platform qualification is tracked per adapter.

## 19. Open product decisions

These can be settled after M0 without invalidating the architecture:

- Final rules for automatic fallback between harnesses.
- Whether the web UI ships inside the Python wheel or as a separately versioned artifact.
- Adapter distribution through Python entry points, a signed registry, or both.
- Default retention duration for normalized conversation events.
