# Local web UI

Merced AI ships a responsive, loopback-only collaboration UI as the optional `webui` extra.

```bash
python -m pip install 'merced-ai[webui]'
merced-ai ui -C /path/to/workspace
```

## Product surfaces

- Start, search, select, resume, and export normalized conversations.
- Select a bot and, before the first turn, override its installed harness route.
- Send requests through the same routing and OAP projection layer used by the CLI.
- Receive normalized Server-Sent Events for run start, structured harness activity, completion,
  cancellation, and failures.
- Cancel a running subprocess and retry failed requests.
- Review projection adjustments, effective authority, route health, versions, and capabilities.
- Create and edit project-local OAP profiles, including provider/model and edit/shell permission
  requests. Fields outside the editor are preserved and the revision increments atomically.
- Create bot bindings with ordered fallback harnesses.
- Use safe Markdown/code rendering, code copying, light/dark themes, keyboard submission, reduced
  motion, mobile navigation, and keyboard-visible focus.

## Approval model

When a profile does not explicitly deny both editing and shell access, the UI requires a one-run
confirmation before launching the harness. This is a Merced AI preflight, not a replacement for
the harness's own approval and sandbox policy. The inspector labels that boundary explicitly.

## Local security model

The server accepts only `127.0.0.1`, `localhost`, or `::1`. Each launch generates an ephemeral
token in the URL fragment. Browser JavaScript exchanges it for an HTTP-only, SameSite cookie and
immediately removes the fragment from the address and history entry. API mutations reject
cross-origin requests.

Responses use a restrictive Content Security Policy, deny framing and MIME sniffing, disable
unneeded browser permissions, omit referrers, and disable caching. Profile and bot names continue
through the same path-safe validators used by the CLI.

The UI does not turn Merced AI into a sandbox. Harness configuration, provider credentials,
workspace permissions, and the harness's effective policy remain authoritative.

## Streaming boundary

The browser receives a normalized live lifecycle stream. Subprocess adapters currently emit the
final assistant response once the harness process completes. Harnesses that return structured
native events expose those events through the stream after normalization; token-level and
interactive approval bridging depend on future transport-specific streaming adapters.

## Validation

Automated coverage includes authentication/cookie exchange, security headers, origin rejection,
profile and bot management, routing and projection, session creation and export, approval
preflight, normalized SSE success/error/tool lifecycles, persistence, cancellation, responsive
contracts, accessibility landmarks, reduced motion, and packaged static assets.

The release workflow also starts the real Uvicorn server from an installed build and runs a
deterministic executable through profile creation, bot binding, session creation, and the streamed
message endpoint.
