# Architecture

Merced AI is a broker, not an agent runtime. It turns portable identity and policy intent into a
bounded invocation of an existing harness while preserving clear ownership boundaries.

```text
OAP profile + bot binding + user prompt
                 |
                 v
        profile discovery/validation
                 |
                 v
       harness registry and detection
                 |
                 v
         adapter profile projection
                 |
                 v
      shell-free bounded subprocess
                 |
                 v
       normalized result + session log
```

## Components

- `profiles.py`: OAP discovery, validation, digests, authoring, and prompt assembly.
- `bots.py`: project/user bot bindings and harness preference resolution.
- `harnesses/registry.py`: fixed supported-harness metadata and adapter registration.
- `harnesses/detection.py`: bounded executable resolution and version probing.
- `harnesses/adapters/command.py`: provider-aware profile projection, argv construction, execution,
  cancellation, and output normalization.
- `application.py`: routing and run preparation.
- `sessions.py`: atomic normalized session persistence.
- `cli.py`: human and JSON automation surfaces.
- `webui_server.py`: loopback-first optional UI over the same application records.

## Adapter contract

An adapter must probe availability, describe profile projection, build a noninteractive command,
execute it without a shell, normalize output, bound time/output, and expose honest degradation.
Native OAP support is used only when a harness actually consumes the discovered profile. Other
harnesses receive a delimited prompt or a dedicated system-prompt flag.

Permission projection is advisory and may only narrow intent. The harness remains responsible for
credentials, provider traffic, approvals, sandboxing, tools, and final policy enforcement.

## Data ownership

- OAP profiles own portable identity, instructions, model preference, and bounded learned state.
- Bot bindings own local harness preference and fallback metadata.
- Sessions own normalized conversational history and pinned profile/spec digests.
- Harness-native state, credentials, model catalogs, and provider logs remain harness-owned.

## Failure model

Discovery failures are isolated. A run is attempted only against an available adapter. Timeout,
interrupt, process-start, nonzero-exit, and structured in-stream failures become bounded
`HarnessRunError` results. Automatic fallback stops once a request has begun to prevent duplicate
paid or mutating work.

## Security boundaries

Merced AI rejects plaintext profile credentials, never interpolates commands into a shell string,
limits captured output, and keeps UI access loopback/token constrained. It is not a sandbox. Users
must treat each installed harness and provider configuration as trusted executable code.
