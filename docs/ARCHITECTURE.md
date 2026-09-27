# Architecture

Merced AI is a broker, not an agent runtime. It turns portable identity and policy intent into a
bounded invocation of an existing harness while preserving clear ownership boundaries.

```text
OAP profile(s) + bot binding(s) + user prompt
                 |
                 v
        profile discovery/validation
                 |
                 v
       harness registry and detection
                 |
                 v
    participant selection and routing
                 |
                 v
         adapter profile projection(s)
                 |
                 v
    shell-free bounded subprocess(es)
                 |
                 v
       normalized result + session log
```

AGS ingestion is a separate read-only path: a graph is validated with the published support
library, normalized to a deterministic dependency plan, and returned without entering the harness
routing or subprocess pipeline.

## Components

- `profiles.py`: OAP discovery, validation, digests, authoring, and prompt assembly.
- `graphs.py`: AGS validation, RFC 8785 graph identity, dependency ordering, reachability, bounded
  work/cost summaries, and unsupported-feature reporting; it never executes graph nodes.
- `bots.py`: project/user bot bindings and harness preference resolution.
- `harnesses/registry.py`: fixed supported-harness metadata and adapter registration.
- `harnesses/detection.py`: bounded executable resolution and version probing.
- `harnesses/adapters/command.py`: provider-aware profile projection, argv construction, execution,
  cancellation, and output normalization.
- `application.py`: routing and run preparation.
- `sessions.py`: atomic normalized session persistence, exact mention selection, and deterministic
  group dispatch.
- `cli.py`: human and JSON automation surfaces.
- `web/`: the optional loopback-first UI over the same application records.
  - `app.py` composes the FastAPI app (security headers, static assets, routers) and
    `run_web_ui` enforces loopback binding and the one-time token.
  - `context.py` holds per-server state (`WebContext`) and the authentication dependencies:
    every endpoint takes `ReadContext` (cookie or token) or `WriteContext` (also same-origin).
  - `probes.py` is the cached, background-refreshed harness detection service.
  - `runs.py` is the run service: it plans a turn, asks launch consent, and owns the supervised
    run whose events every HTTP stream only observes.
  - `routers/` groups endpoints by resource: `workspace` (auth, bootstrap, harnesses, context,
    history), `catalog` (profiles, bots, projection), and `conversations` (sessions, messages,
    replay, cancel, approvals).
  - `webui_server.py` remains as a compatibility import path.

The UI bootstrap path never probes executables. It returns profiles, bots, sessions, and a cached
or placeholder harness snapshot, then a separate authenticated endpoint starts one bounded
background probe sequence. Results become visible per harness and a complete snapshot is written
atomically for later launches. Routing still performs its own current probe before execution, so
the UI cache cannot authorize a stale route.

## Adapter contract

An adapter must probe availability, describe profile projection, build a noninteractive command,
execute it without a shell, normalize output, bound time/output, and expose honest degradation.
Native OAP support is used only when a harness actually consumes the discovered profile. Other
harnesses receive a delimited prompt or a dedicated system-prompt flag.

### Capabilities: what the harness offers versus what Merced AI delivers

Each harness descriptor carries two capability sets:

- `harness_supports`: features the harness documents for its own interactive or protocol surface,
  such as streaming, native session resume, or model listing. Merced AI records these for
  reference and does not use them yet.
- `broker_implements`: what Merced AI actually delivers through its adapter today. This is the
  only set the CLI (`harness list`, `harness show`) and the web UI present as a capability.

Today every adapter runs one noninteractive subprocess per turn. Output arrives when the process
exits (no streaming), each turn replays a bounded transcript instead of resuming a native session,
and selected workspace files are inlined into the prompt. MagAgent and Loro additionally relay AAIS
approvals over stdio, receive project OAP profiles by name, and can satisfy WebMCP routing once
their readiness report verifies. No built-in descriptor claims the ACP transport, because Merced AI
has no ACP client yet. Probe JSON still contains a `capabilities` field equal to
`broker_implements` for one release so existing automation keeps working.

Permission projection is advisory and may only narrow intent. The harness remains responsible for
credentials, provider traffic, approvals, sandboxing, tools, and final policy enforcement.

## Run journal

Web runs are owned by the broker, not by the HTTP request. Every server-sent event is appended to
`.merced-ai/run-events/<run-id>.jsonl` (a `header` line, one `event` line per event with its
sequence number, and a `complete` line). Reconnecting clients replay from a sequence number; the
replay window keeps the last 1,000 events (at most 24 MB) and reports a `replay_gap` when a client
asks for something older. Appends are flushed immediately and fsynced at most four times a second
plus once at completion, so appending stays constant-cost however long a run gets. 0.7.0 journals
(`<run-id>.json`) are converted the first time they are read.

Routing probes each candidate harness before a turn. Within one CLI chat, one group room, or one
UI server, a probe result is reused for `MERCED_AI_PROBE_TTL_SECONDS` (30 s by default), so a turn
no longer pays for spawning version and capability commands every time.

## Data ownership

- OAP profiles own portable identity, instructions, model preference, and bounded learned state.
- Bot bindings own local harness preference and fallback metadata.
- Sessions own normalized conversational history and pinned profile/spec digests.
- Group sessions own an ordered participant list. Each participant pins its bot, routed harness,
  and profile/spec snapshot; assistant turns carry bot and harness attribution.
- Harness-native state, credentials, model catalogs, and provider logs remain harness-owned.
- Cached UI probe snapshots contain only executable paths, bounded version output, both capability
  sets, status, and timestamps; they never contain provider credentials. The cache schema is
  versioned, and a snapshot written by an older release is ignored and re-probed.

## Failure model

Discovery failures are isolated. A run is attempted only against an available adapter. Timeout,
interrupt, process-start, nonzero-exit, and structured in-stream failures become bounded
`HarnessRunError` results. Automatic fallback stops once a request has begun to prevent duplicate
paid or mutating work.

Group fan-out prepares an isolated prompt and route for each selected participant, executes the
bounded harness processes concurrently, streams lifecycle/results as each completes, then saves
successful results in participant order. Failures are isolated and named retry addresses only the
failed bot. A shared cancellation signal reaches all processes in that
user-triggered fan-out. There is deliberately no automatic assistant-to-assistant turn scheduler.

## Security boundaries

Merced AI rejects plaintext profile credentials, never interpolates commands into a shell string,
limits captured output, and keeps UI access loopback/token constrained. It is not a sandbox. Users
must treat each installed harness and provider configuration as trusted executable code.
