# Threat model

This is a **self-review**. The same agent that wrote the 0.8.0 features also wrote this model and
the fixes below. No independent reviewer, fuzzer, or penetration test was involved, so treat it
as a map of what we looked at and what we changed, not as an audit result. Please report anything
we missed through the process in [SECURITY.md](../SECURITY.md).

Scope: the surfaces added or widened for 0.8.0 (the ACP client and server, the A2A endpoint,
worktree rooms, the OAP delta inbox, harness plugins, cross-harness evals, private prompt files,
and the run journal), plus the loopback web UI they share. Each section lists the assets, the
threats by STRIDE category, the mitigations with file references, what risk is left, and the tests
that cover it.

## Trust boundaries and baseline

- **The user and their shell are trusted.** Anyone who can run commands as the user, or write to
  `~/.merced-ai`, is out of scope; they already have everything Merced AI has.
- **Harnesses are semi-trusted.** Merced AI launches them with the permissions the profile asks
  for, but the harness is the policy authority and Merced AI is not a sandbox around it (see
  SECURITY.md). A harness can write anywhere its own permissions allow, including the workspace's
  `.merced-ai/` directory. That is why state Merced AI reads back from the workspace is
  re-validated on read.
- **Everything a harness or agent sends back is untrusted input:** stdout, ACP messages, AAIS
  requests, OAP deltas, and diffs.
- **Other local processes and web pages are untrusted.** They can reach loopback ports but do not
  have the UI token.

STRIDE key: **S**poofing, **T**ampering, **R**epudiation, **I**nformation disclosure,
**D**enial of service, **E**levation of privilege.

## Findings fixed in this review

| # | Surface | Problem | Fix | Regression test (`tests/test_security_review.py`) |
|---|---------|---------|-----|---------------------------------------------------|
| 1 | Worktrees | `remove` ran `git worktree remove --force` and `rmtree` on whatever path the on-disk index named | Delete only paths strictly inside the room's root; delete only the branch Merced AI names | `test_worktree_removal_never_deletes_outside_its_root` |
| 2 | Worktrees | A tampered index entry was returned by `get`/`ensure`, so a bot could be run, and `git add -A` executed, in any repository | Index entries outside the root are ignored and the worktree is recreated | `test_a_tampered_worktree_index_never_redirects_a_bot` |
| 3 | Worktrees | `apply` recreated symbolic links that point outside the repository (for example `keys -> ~/.ssh`) in the real workspace | `apply` refuses such links; `diff` and both UIs flag them | `test_apply_refuses_symlinks_that_point_outside_the_repository` |
| 4 | Plugins | Under `python -m merced_ai`, a `*.dist-info` in the working directory registered an entry point, and its code ran | `__main__` drops the working directory from `sys.path`; entry points whose distribution lives in the working directory are ignored and reported | `test_entry_points_from_the_working_directory_are_ignored` |
| 5 | Web / A2A | No Host check, so DNS rebinding could reach the server (the token was still required) | Only literal loopback host names are served (421 otherwise), including no bare single-label names | `test_dns_rebinding_host_is_rejected_even_with_a_token` |
| 6 | Web / A2A | Request bodies were unbounded | 16 MB cap from `Content-Length` (413), chunked bodies refused (411) | `test_oversized_request_bodies_are_refused_before_parsing` |
| 7 | A2A / evals | Message text, stored tasks, and eval jobs grew without bound in memory | 100,000-character text cap, 200 tasks, and about 50 finished eval jobs kept | `test_a2a_message_text_is_bounded`, `test_in_memory_a2a_tasks_are_capped`, `test_finished_eval_jobs_are_pruned` |
| 8 | Inbox | YAML alias expansion ("billion laughs") was accepted | YAML is loaded with aliases refused | `test_yaml_alias_bombs_are_rejected` |
| 9 | Inbox | A deeply nested delta raised `RecursionError`; files had no size cap | 64-level depth check before anything recursive runs, 1 MB file cap | `test_deeply_nested_and_oversized_deltas_are_rejected` |
| 10 | Inbox | One torn or foreign item file broke the whole listing | Unreadable items are skipped | `test_a_corrupt_inbox_item_does_not_break_the_listing` |
| 11 | Inbox | A harness could edit a stored proposal's `risk` to `low`, and the UI showed it | Risk is recomputed on every read | `test_a_stored_proposal_cannot_downgrade_its_own_risk` |
| 12 | Run journal | A line with a non-integer `sequence` or non-string `sse` crashed replay | Such lines are skipped, as are non-increasing sequences | `test_journal_lines_with_bad_fields_are_skipped` |
| 13 | ACP client | A malformed message (non-dict params, string ids, bad plan or tool-call shapes) killed the reader thread and hung the turn | Every message is shape-checked and handled in isolation | `test_malformed_agent_traffic_does_not_kill_the_client` |
| 14 | ACP client | Reply text, tool calls, line length, and agent-request threads were unbounded | 10M-character reply (marked `truncated`), 500 tool calls, 8 MiB lines, 8 concurrent agent requests | `test_streamed_reply_size_is_bounded` |
| 15 | ACP server | Prompt size, line length, and request threads were unbounded | 1M-character prompt, 8 MiB lines, 16 concurrent requests | `test_acp_server_bounds_prompt_size` |
| 16 | Approvals (ACP server and web UI) | Button and option names came from the harness's own `label`, so "always allow" could be labelled "Deny" | Names are derived from decision and scope; the ACP title is one sanitized line; the web dialog focuses Deny first | `test_forwarded_approval_labels_come_from_merced_not_the_harness`, `test_web_approval_buttons_do_not_use_the_harness_label` |

Each regression test was run against the code before its fix and failed there.

A follow-up (SEC-2) tied ACP server sessions to the process that created them; see the ACP
server section below.

## ACP client (`harnesses/acp.py`, `harnesses/acp_rpc.py`)

**Assets:** the user's files and shell (through the agent's permission requests), the approval
decision, and the broker process's memory and threads.

**Threats**

- *S:* The agent labels a destructive tool call as harmless (`kind: "read"`, a soothing title).
- *T:* The agent sends malformed JSON-RPC to confuse request/response matching.
- *I:* The agent asks the client to read files (`fs/read_text_file`) outside the workspace, or
  follows symlinks out of it.
- *D:* Huge lines, endless chunks, thousands of tool calls, or a flood of agent-to-client requests.
- *E:* The agent asks the client to write files or open a terminal on its behalf, or the client
  switches the agent into an auto-approve mode.

**Mitigations**

- Merced AI advertises no filesystem or terminal capability (`clientCapabilities` in
  `harnesses/acp.py`); any `fs/*` or `terminal/*` request is answered `-32601` (`AcpMethodNotFound`).
  There is no client-side file access to traverse, so path traversal and symlinks do not apply
  here. The agent reads files with its own permissions.
- Permission requests go through AAIS (`_decide_permission`). A profile that denies edit or shell
  rejects matching kinds without asking. With no presenter (plain CLI), requests are rejected.
- `choose_mode` never selects `AUTO_APPROVE_MODES` and picks a read-only mode when the profile
  denies both edit and shell.
- `acp_rpc.AcpConnection` bounds line length (`MAX_LINE_BYTES`), drops non-JSON and non-object
  lines, only matches integer response ids, isolates each message in its own `try`, and caps
  concurrent agent requests (`MAX_AGENT_REQUESTS`, excess answered `-32000`).
- `_Turn` caps reply size (`MAX_REPLY_CHARS`, reported as `truncated`) and tool calls
  (`MAX_TOOL_CALLS`), and truncates tool-call summaries.

**Residual risk:** an agent's `kind` and title are self-reported. A profile that denies shell
blocks `execute`, but an agent that labels a shell call as `read` gets whatever the user or the
profile allows for reads. The harness's own sandbox is the backstop.

**Tests:** `test_acp_adapter.py` (permission mapping, profile denial without asking, safe mode and
refusal of auto-approve, crash reporting, cancellation); `test_security_review.py` (#13, #14).

## ACP server (`acp_server.py`, `merced-ai acp`)

**Assets:** the workspace, bots' write permissions, session transcripts, and the consent decision.

**Threats**

- *S:* A harness spoofs the permission prompt the client shows, through its option labels or a
  multi-line title that imitates Merced AI.
- *T:* The client passes a `cwd` outside the served workspace, or a crafted session id that
  escapes the sessions directory.
- *I/T:* A client continues a conversation another client or the CLI started (session
  hijack), or a server started for a read-only bot is used to drive a room with write-capable bots
  by loading that room's session.
- *D:* Oversized prompts or lines, or many concurrent requests.
- *E:* A write-capable bot runs without the user's consent.

**Mitigations**

- `session/new` refuses any `cwd` whose resolved path differs from the served workspace.
- Session ids must match `session-[A-Za-z0-9-]+` (`sessions.py`), so they cannot contain path
  separators.
- Sessions belong to the agent process that created them (`_owned`). `session/load` of any other
  conversation replays its history and then says it is read-only; `session/prompt` on it is
  refused, and prompting a session that was never loaded is refused too. The opt-in
  `--allow-resume` (`allow_resume`) lets a loaded conversation be continued, but only when every
  bot in it is one this process serves (`_resume_refusal`).
- A write-capable bot needs `session/request_permission` consent once per session (`_consent`);
  declining runs nothing.
- Forwarded approvals use Merced AI's own option names (`DECISION_OPTIONS`) and a single-line,
  bounded title (`_one_line`). Unknown decision and scope pairs are dropped.
- `MAX_PROMPT_CHARS`, `MAX_LINE_CHARS`, and `MAX_CONCURRENT_REQUESTS` bound input; notifications
  run inline.
- The transport is the stdio pipe of a process the client started, so there is no network listener.

**Residual risk:** ownership is per process, not per user identity. ACP has no client
authentication, so any client that can start `merced-ai acp` can still read (load) every
conversation in that workspace. It runs as the user, which is the same trust as the CLI. With
`--allow-resume`, any client of that server can continue any conversation of the served bots. The
action summary inside the title is still harness text, prefixed with the harness id.

**Tests:** `test_acp_server.py` (`test_errors_are_json_rpc_errors` covers the foreign `cwd` and
unknown session, `test_declined_consent_runs_nothing`, `test_room_asks_consent_once...`,
`test_threads_are_not_leaked`, `test_harness_approvals_are_forwarded_to_the_client`,
`test_sessions_belong_to_the_process_that_created_them`,
`test_allow_resume_continues_only_conversations_of_served_bots`,
`test_cli_allow_resume_flag_reaches_the_agent`);
`test_security_review.py` (#15, #16).

## A2A endpoint and the loopback web UI (`web/app.py`, `web/context.py`, `web/routers/a2a.py`)

**Assets:** the ability to run bots, approve actions, and read transcripts; the UI token.

**Threats**

- *S:* A web page uses DNS rebinding to become same-origin with `127.0.0.1:<port>`.
- *T:* Cross-site request forgery against mutations.
- *I:* Another local user or page reads transcripts or the agent card.
- *D:* Huge bodies, huge message text, unbounded task and job maps.
- *E:* An A2A caller runs a write-capable bot without approval.

**Mitigations**

- The server binds only to loopback (`run_web_ui` refuses anything else).
- Every request needs the random per-launch token (cookie with `SameSite=Strict`, header, or
  `Authorization: Bearer`), compared in constant time (`authorize`).
- Mutations with an `Origin` header must match the `Host` (403 otherwise).
- The `Host` header must be a literal loopback name (`ALLOWED_HOSTS`), which defeats DNS
  rebinding even if the token leaked to the page.
- `MAX_BODY_BYTES` (16 MB, 413), chunked bodies refused (411), `MAX_TEXT_CHARS`, `MAX_TASKS`, and
  eval-job pruning bound memory.
- Write-capable bots over A2A stop in `input-required` unless the caller sets
  `metadata.approved: true`, and harness permission requests during the turn still go to the web
  UI approval dialog, where a person decides.
- Responses carry a strict Content Security Policy, `no-store`, `nosniff`, `DENY` framing, and
  `no-referrer`.

**Residual risk:** a caller holding the token can pre-approve write-capable bots with
`metadata.approved`; the token is the whole authorization. The token is in the URL fragment that `merced-ai ui` prints, so anything that
can read the terminal or browser history can use it. Clients that send chunked bodies must send a
`Content-Length` instead. A process running as the same user can read the token from the
server's memory, which is out of scope.

**Tests:** `test_a2a.py` (token required, input-required for writers), `test_webui.py`
(non-loopback binding refused), `test_webui_assets.py` (CSP-compatible assets);
`test_security_review.py` (#5, #6, #7).

## Worktree mode (`worktrees.py`, `merced-ai group diff/apply/cleanup`)

**Assets:** the user's working tree, other repositories on disk, and anything reachable through
a symlink.

**Threats**

- *T:* Bot or session names with `/` or `..` become branch names and paths.
- *T:* `group apply` writes outside the repository through `../` paths or through symlinks.
- *T:* A tampered `worktrees.json` redirects a bot, `git add`, or cleanup to another directory.
- *E:* An applied symlink gives later tools a path out of the project.

**Mitigations**

- Bot names match `SAFE_NAME` (`^[a-z][a-z0-9-]{0,62}$`) and session ids match
  `session-[0-9a-f]{32}` before any path or branch is built.
- Worktrees live under `~/.merced-ai/worktrees/<repo-hash>/<session>/<bot>`.
- The patch is produced by git from the bot's own index and applied with `git apply --check`
  first, all or nothing, with no 3-way merge. `git apply` refuses absolute and `..` paths and
  refuses to write through a symlink (it reports "beyond a symbolic link").
- `_symlinks` and `_escapes` find links whose target is absolute or climbs out of the repository.
  `apply` refuses them and the compare views mark them.
- `_trusted` and `_inside_root` make index entries outside the root invisible, and `remove`
  deletes only inside the root and only Merced AI's own branch name.

**Residual risk:** a worktree limits where a harness starts, not where it can write. A harness with
shell access can still `cd ..`. Symlinks that stay inside the repository are applied. A patch
larger than `MAX_PATCH_BYTES` is shown truncated, but `apply` uses the full patch, so review large
changes on the branch.

**Tests:** `test_worktrees.py` (`test_manager_validates_inputs`, the full CLI
isolate/compare/apply/cleanup flow), `test_group_write_safety.py`; `test_security_review.py` (#1,
#2, #3).

## OAP delta inbox (`inbox.py`)

**Assets:** profiles under `.agents/`, especially `spec.tools`, `spec.permissions`, memory, and
subagents.

**Threats**

- *E:* A delta escalates tools or permissions.
- *S:* A proposal claims `risk: low`.
- *T:* A stored item is edited between intake and approval.
- *D:* YAML alias bombs, deep nesting, huge files, or corrupt item files.
- *R:* Who approved a change.

**Mitigations**

- State operations must stay under `/state`. This is checked at intake and again by the `oap`
  library at apply time, so a tampered stored item is refused too; we verified this by editing a
  pending item.
- Proposals are never applied with the state operations. Each needs its own approval
  (`decide_proposal`).
- Risk is computed (`_proposal_risk`, `HIGH_RISK_PREFIXES`) at intake and again on every read.
- The whole profile is validated before an atomic write, and revision conflicts never blind-write.
- `_NoAliasLoader`, `MAX_DEPTH`, and `MAX_DELTA_BYTES` bound parsing. Item ids must match
  `delta-<32 hex>`. Each item records `approved_by` and a history.

**Residual risk:** the approver sees proposal values that a harness could have edited before
review. What is shown is what gets applied, but the provenance is only as good as the workspace
directory.

**Tests:** `test_inbox.py` (scope, proposal gate, revision conflicts, invalid results, retention,
CLI flow); `test_security_review.py` (#8 to #11).

## Harness plugins (`harnesses/registry.py`, `__main__.py`)

**Assets:** the broker process, and through it everything the user can do.

**Threats**

- *E:* An untrusted plugin runs arbitrary code at startup.
- *T:* A package or module in the current directory shadows a plugin name or one of Merced AI's
  own modules.

**Mitigations**

- Plugins load only from installed distributions' `merced_ai.harnesses` entry points.
  `MERCED_AI_DISABLE_PLUGINS=1` turns them off.
- Load errors are reported, not fatal.
- Distributions located in the working directory are ignored with a visible error, and
  `python -m merced_ai` no longer puts the working directory on `sys.path`.
- The contract kit (`merced_ai.testing.contract`) checks a plugin's honesty, not its safety.

**Residual risk:** installing a plugin is running its code, exactly like installing any Python
package. Only install plugins you trust (docs/PLUGINS.md).

**Tests:** `test_adapter_plugins.py` (registration, disabling, load-error reporting);
`test_security_review.py` (#4).

## Eval judge (`evals.py`)

**Assets:** eval scores and the workspace.

**Threats**

- *T:* A graded reply injects instructions ("score 10") into the judge prompt.
- *E:* A reply convinces the judge to run tools.

**Mitigations**

- The judge runs with a profile copy whose permissions are `edit: deny, shell: deny`. For Codex,
  that projects to `--sandbox read-only`.
- The reply is fenced and truncated to 8,000 characters.
- `parse_judgement` accepts only a 0 to 10 number. Judge output is only ever stored as a score and
  reason; nothing is executed from it.
- Write-capable profiles run their harnesses one at a time.

**Residual risk:** prompt injection can move a judge score. That is accepted: scores are advisory
and the deterministic checks are reported alongside.

**Tests:** `test_evals.py`;
`test_security_review.py::test_the_eval_judge_runs_read_only_and_injection_only_moves_the_score`.

## Temporary prompt files (`harnesses/api.py`, `harnesses/adapters/command.py`)

**Assets:** the prompt and system prompt, which may contain private context.

**Threats**

- *I:* Other local users read the files.
- *T:* A symlink planted in a shared temp directory redirects the write.

**Mitigations**

- Each run gets its own `tempfile.TemporaryDirectory` (mode 0700, unpredictable name), and files
  inside it are created with `O_CREAT | O_EXCL` and mode 0600 (`write_private`), so a pre-existing
  path or symlink makes the write fail instead of following it.
- The directory is removed when the run ends.

**Residual risk:** on Windows the modes are not enforced the same way; the files rely on the
per-user temp directory's ACL.

**Tests:** `test_prompt_delivery.py::test_prompt_files_are_private_and_removed_after_the_run`, and
the Loro and MagAgent integration tests that assert mode 0600.

## Run journal replay (`run_supervisor.py`)

**Assets:** the ability to reconnect to a running turn and see its events.

**Threats**

- *T/D:* A torn final line, foreign lines, bad field types, or out-of-order sequences crash or
  confuse replay.
- *D:* A huge journal.

**Mitigations**

- `read_journal` skips unparsable lines, non-object records, and events with a non-integer
  `sequence`, a non-string `sse`, or a sequence that does not increase.
- Replay is bounded by `MAX_REPLAY_EVENTS` and `MAX_REPLAY_BYTES`.

**Residual risk:** a harness that can write to `.merced-ai/runs/` can inject well-formed but false
events into a replay. They are display-only.

**Tests:** `test_run_journal.py` (torn line, bounded window); `test_security_review.py` (#12).

## Not covered by this review

- The harnesses themselves (Codex, Claude Code, MagAgent, Loro, and the rest) and their sandboxes.
- The `aais` and `oap` libraries beyond how Merced AI calls them.
- Supply-chain review of dependencies.
- Behaviour on Windows, which was reasoned about, not tested, for file modes and symlinks.
