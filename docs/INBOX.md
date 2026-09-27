# OAP state inbox

Profiles in the Open Agent Profile format carry learned `state` (facts, preferences, glossary,
open threads). Sessions do not rewrite their own profile; they emit an `AgentStateDelta` and the
process that owns the file decides whether to apply it. Merced AI is that reviewing process for
the profiles in your workspace: deltas wait in an inbox until you decide.

## Getting deltas into the inbox

- **Import** a delta a harness produced: `merced-ai inbox add session.delta.yaml`. JSON, YAML, and
  Loro's proposal records (which wrap a delta) are accepted, or upload the document to
  `POST /api/inbox`.
- **Remember something yourself**: `merced-ai inbox remember reviewer "Use pytest, not unittest"
  --kind preference` (or **Inbox → Remember something** in the web UI). This records your explicit
  statement as a one-operation delta; it still waits for approval like any other.

Every delta is validated with the OAP reference validator on arrival: schema, the `/state`-only
scope of operations, literal-secret checks, and the target profile must exist.

## Reviewing

`merced-ai inbox list`, `inbox show ID` (or the **Inbox** page) show each delta's summary, target
revision, state operations with their reasons, and any proposals with a computed risk.

- **Apply state changes** (`merced-ai inbox approve ID`): the operations are applied with the OAP
  reference applicator as one atomic change: revision check, `target.digest` check, retention
  (pinned entries are never evicted), revision bump, and a history entry naming you as approver,
  written with a temporary file, fsync, and rename. The result is validated before it is written.
- **Conflicts**: if the profile changed since the delta was made, nothing is written and the item
  is marked `conflict`. When every operation appends or addresses entries by id, **Rebase and
  apply** (`--rebase`) re-targets it at the current revision; otherwise reject it.
- **Proposals** (requests to change `/metadata` or `/spec`, such as instructions, tools, or
  permissions) are never applied with the state operations, whatever `lifecycle.writeback` says.
  Each is decided on its own with `merced-ai inbox proposal ID N --approve` or **Apply this
  change**, and anything touching tools, permissions, memory, or subagents is shown as high risk
  regardless of what the delta claims. Applicator-owned fields (`name`, `revision`, `updated_at`,
  `trust`) cannot be proposed.
- **Reject** (`merced-ai inbox reject ID`) leaves the profile untouched.
- A profile with `lifecycle.writeback: off` refuses every delta; Markdown-encoded profiles must be
  updated by their owning harness.

Items live in `.merced-ai/inbox/` as JSON with their review history.

## Conformance

The inbox implements the OAP 1.0 Level 2 applicator requirements L2-A1 to L2-A14 and the
Level 2 behavioral tests 7 to 10 (atomicity, conflict, proposal gate, retention) in
`tests/test_inbox.py`. Merced AI does not generate deltas from sessions on its own and does not
enforce state-injection budgets (L2-S4), so it still claims OAP Level 1.
