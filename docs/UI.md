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
- Create, edit, and delete project-local OAP profiles, including provider/model and edit/shell
  permission requests. Provider and known-model choices are bounded dropdowns with an explicit
  harness-default option. Fields outside the editor are preserved and the revision increments
  atomically.
- Review root-derived profile trust adjustments and discovery-collision warnings before binding a
  profile to a bot.
- Create, edit, and delete project-local bot bindings with ordered fallback harnesses.
- Delete durable conversations after confirmation when no run is active.
- Attach up to twenty workspace files or browser uploads to a run. Small UTF-8 files are bounded
  and supplied inline; large, binary, and image assets are stored under the project-local Merced
  directory and supplied as explicit workspace paths for capable harnesses.
- Inspect durable run records with status, participants, context manifest, normalized tool events,
  elapsed time, and partial-failure counts. Run records survive browser refreshes independently of
  the conversation transcript.
- Copy a shell-quoted handoff command for the active harness and workspace without making Merced AI
  impersonate that harness's terminal or session model.
- Opt into browser-native completion notifications. The preference stays in browser storage and
  notification permission is requested only from an explicit operator action.
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

Workspace context is resolved beneath the workspace root before every run. Traversal, ignored
dependency/build directories, and internal Merced state other than the dedicated attachment area
are rejected. Upload names are reduced to their basename, payloads are bounded to 10 MB, and a
single prompt can inline at most 750 KB. The durable context manifest records paths, media types,
sizes, and whether each item was delivered inline or by workspace reference.

The UI does not turn Merced AI into a sandbox. Harness configuration, provider credentials,
workspace permissions, and the harness's effective policy remain authoritative.

Merced AI is a cross-harness router rather than a second implementation of each harness's graph,
memory, Skill, plugin, or MCP editor. Those resources remain owned by the selected harness; the UI
projects an OAP profile and routes the session without pretending it can safely mutate another
harness's private configuration format.

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
contracts, accessibility landmarks, reduced motion, packaged static assets, bounded context
listing, traversal rejection, upload persistence, run-record durability, and harness handoffs.

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

Profile generation keeps its dialog open, disables duplicate submission, and displays an elapsed “Generating and validating profile” state until the native harness returns a validated OAP proposal. Merced AI deliberately does not duplicate graph editors or extension configuration: it dispatches to installed native harnesses, so AGS authoring and plugin/skill/MCP lifecycle management remain in MagAgent, Loro, or Mag Command Center. The Harnesses screen identifies the selected native runtime and its capabilities so users know where to configure those facilities.
# Portable profile authoring

Manual and generated profile dialogs can save to the current project's portable `.agents`
directory, the universal `~/.agentprofiles` directory, or merced-ai's user profile directory.
Long-running generation displays elapsed operational health and explains that the selected harness
is authoring a bounded OAP draft before merced-ai validates it. This is lifecycle visibility, not
private model reasoning.

Merced AI remains a harness broker: provider credentials, provider discovery, image models, and
Agentic Graph execution belong to the selected harness. Their absence from this Settings surface is
an authority boundary, not an incomplete duplicate configuration system.
