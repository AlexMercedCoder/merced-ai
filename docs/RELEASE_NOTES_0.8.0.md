# merced-ai 0.8.0

Release notes, September 28, 2026. The full list of changes is in the
[changelog](../CHANGELOG.md#080--2026-09-28).

## Highlights

- **Harness adapter plugins.** Every built-in adapter is now a `HarnessSpec`, and installed
  packages can add adapters through the `merced_ai.harnesses` entry-point group. A contract-test kit
  (`merced_ai.testing.contract`) checks third-party adapters. See [plugins](PLUGINS.md).
- **Agent Client Protocol.** Claude Code, Gemini CLI, Goose, and OpenCode run as ACP agents when
  their launchers are installed: replies stream, permission requests go through the AAIS approval
  dialog or a terminal prompt, and three of the four resume their own sessions. `merced-ai acp`
  serves a bot or a room to editors such as Zed.
- **Worktree rooms.** `group ask/chat --worktrees` gives each write-capable bot its own git
  worktree, with `group diff`, `group apply`, and `group cleanup` (and matching web UI views).
  Without worktrees, write-capable bots sharing a workspace now take turns.
- **Cross-harness evals.** `merced-ai eval run` sends one profile and prompt to several harnesses,
  scores deterministic checks first, and reports an optional judge separately. See
  [evals](EVALS.md).
- **Reviewed OAP state inbox.** State deltas are validated on arrival and applied only after
  approval. See [inbox](INBOX.md).
- **Terminal approvals.** When MagAgent, Loro, or an ACP agent asks for approval during `ask`,
  `chat`, or a group command, the CLI shows the request and takes one key; Deny is the default.
  Before this, those requests were denied silently on the command line.
- **Prompts off the command line.** Most harnesses now read the prompt from stdin or a private
  file, so long conversations no longer fail with `E2BIG` or a Windows command-line limit.
- **Experimental A2A endpoint** on the UI server. See [serving](SERVING.md).
- **Web UI first run.** A guide lists every detected harness with its state, offers a starter
  bot, keeps drafts typed before a bot exists, and explains a missing or stale token. Fonts are
  self-hosted.

## Behavior changes

- `merced-ai ui` defaults to port **8773** (was 8765, which collided with `loro web`). Update
  bookmarks and A2A client configuration that assumed 8765.
- Requires `agent-approval-interchange>=0.2.0,<0.3`. Approval-presenter and run-record owners are
  recorded as `aais.liveness` owner identities; existing files are read and upgraded in place.
- Harness descriptors report `harness_supports` and `broker_implements` separately, and only
  broker-implemented capabilities are shown as supported. Probe JSON keeps a deprecated
  `capabilities` field for this release.
- Projection reports list every profile section a prompt-context projection drops.
- A security self-review of the new surfaces (see [threat model](THREAT_MODEL.md)) led to fixes for
  worktree path trust, symlinks in applied patches, plugins loaded from the working directory, DNS
  rebinding and request size limits on the UI server, and approval labels chosen by the harness.

## MagAgent and Loro compatibility

Merced AI 0.8.0 works with earlier MagAgent and Loro releases and uses newer features when the
installed version has them. Features are detected from each harness's own help and version output.

| Harness | With this version or later | Older versions |
| --- | --- | --- |
| MagAgent 1.4.0 | Prompt through a private file (`ask --prompt-file`); `shell: ask` asks for every command and `shell: deny` removes the shell tools (reported as `mapped`) | Prompt as an argument, subject to the size guard; `shell` is `narrowed` |
| Loro 0.22.0 | Prompt through a private file (`run --prompt-file`) and `run --json` output | Prompt as an argument, subject to the size guard |

A development build of MagAgent that still reports 1.3.x is treated as 1.3.

## Upgrading

Back up `.merced-ai` before upgrading. Conversation, run, and presenter files from 0.7.0 load
without manual steps: 0.7.0 run journals are converted to the append-only JSON Lines format and
bare-PID owners are upgraded on first use. AGS, OAP, and AAIS document and wire formats are
unchanged; conformance results are in `docs/oap-conformance.json` and `docs/ags-conformance.json`.
