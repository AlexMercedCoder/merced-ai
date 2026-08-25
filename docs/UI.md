# Local web UI

Merced AI ships a responsive, loopback-only collaboration UI as the optional `webui` extra.

```bash
python -m pip install 'merced-ai[webui]'
merced-ai ui -C /path/to/workspace
```

## Product surfaces

- Start, search, select, resume, and export normalized conversations.
- Create group conversations from existing bots; target exact `@mentions`, everyone, the next
  round-robin bot, or one named participant.
- Search, select, and reorder participants; name/rename rooms; derive a new room when changing
  participants; and use keyboard-accessible mention completion.
- Select a bot and, before the first turn, override its installed harness route.
- Send requests through the same routing and OAP projection layer used by the CLI.
- Receive normalized Server-Sent Events for run start, structured harness activity, completion,
  cancellation, and failures.
- See participant-attributed messages, routes, activity, partial failures, and Markdown exports.
- Follow queued/running/completed/failed state progressively, cancel the shared run, and retry only
  a failed participant.
- Review projection adjustments, effective authority, route health, versions, and capabilities.
- Load workspace data immediately while bounded harness probes update independently in the
  background; reuse cached health on later launches and refresh detection explicitly when needed.
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

The browser receives a normalized live lifecycle stream. Group starts include every selected bot
and harness; participant-start, message, tool, error, and cancellation events include participant
identity. Selected participants run concurrently and responses appear as they complete, while
persisted answers are normalized in participant order. Subprocess adapters currently emit the
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

Group coverage includes legacy session loading, mention/all/direct/round-robin selection,
deterministic result order, attribution, approval aggregation, partial failures, export, and shared
run cancellation contracts. Dedicated Chromium CI validates ordered group creation, progressive
responses, mention completion, derived-room controls, and desktop/mobile layouts, then publishes
screenshots as a workflow artifact. See [Group conversations](GROUP_CHAT.md).

Group creation and participant derivation render from the mutation response immediately. They do
not wait for a second harness discovery pass, which keeps the interface responsive on machines with
many installed harnesses. Adapter or routing failures remain inside the open dialog for correction.

Initial bootstrap follows the same rule: profiles, bots, sessions, and cached or placeholder
harness entries render before any executable is invoked. The browser starts an authenticated
background refresh, polls progressive results, and announces detecting/completed state. Each
completed snapshot is stored atomically in the user Merced directory; results older than five
minutes are labeled as previous while revalidation runs. The inspector and Harnesses view both
provide an explicit refresh control.
