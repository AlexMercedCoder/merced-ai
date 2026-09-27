# Group conversations

Merced AI can place two to twelve existing OAP bots in one durable conversation. Each participant
keeps its own profile, preferred/fallback routing decision, harness, provider/model request, and
permission boundary. Merced AI coordinates user-requested turns; it does not create an autonomous
bot-to-bot loop.

## CLI

Start an interactive room:

```bash
merced-ai group chat reviewer builder tester
```

The default `mentions` mode sends an unmentioned message to the first participant. Mention one or
more bots to select them in participant order:

```text
you> @reviewer @tester inspect this proposal
```

Use `/all MESSAGE` for one fan-out turn or `/round-robin MESSAGE` for the next participant. Select
a different default for the room with `--mode all` or `--mode round_robin`.

For automation, create a room and run one fan-out turn:

```bash
merced-ai group ask reviewer builder tester \
  --prompt "Give independent assessments" \
  --json
```

Resume any room with `merced-ai session resume SESSION_ID`. `session list`, `session show --json`,
and the stored JSON expose the participants and attributed turns.

## Web UI

Launch `merced-ai ui`, then select **Group**. Search, select, and reorder two or more bots, name the
room, and choose its default dispatch mode. The composer offers `@mention` completion and can target mentioned bots, everyone, the next
round-robin participant, or one named participant. Participant chips show each bot's pinned
harness. Exported Markdown includes the bot and harness on every assistant response.

Rooms can be renamed. **Participants** starts a derived conversation where bots can be added,
removed, or reordered without mutating the historical room. Stable bot colors and initials
preserve attribution across transcripts and inspector cards.

## Dispatch and ordering

- `mentions`: exact, case-insensitive `@bot-name` matches select recipients. With no mention, the
  first participant responds.
- `all`: every participant responds.
- `round_robin`: one participant responds; persisted assistant-turn count chooses the next bot.
- a bot name: only that participant responds (web API/UI).

Multi-recipient runs execute concurrently, with one exception described in
[Shared-workspace writes](#shared-workspace-writes). Prompts are isolated per bot and identify the
intended speaker. Queued/running/completed/failed state and responses update progressively. Durable turns
are committed in original participant order, keeping resumed/exported transcripts deterministic.

## Shared-workspace writes

Every participant in a room works in the same project directory. A bot is **write-capable** when
its OAP profile does not deny both `edit` and `shell`; such a bot can change files in that
directory. When a turn selects two or more write-capable bots that share a workspace, Merced AI
runs those bots one at a time, in participant order, so they cannot edit the same files at once.
Read-only participants (both `edit` and `shell` denied) still run concurrently with everyone.

The room tells you when this happens:

- **CLI:** `group ask`, `group chat`, and `session resume` print a warning to stderr naming the
  bots. `group ask --json` adds `write_serialization: {"serialized": true, "bots": [...]}`.
- **Web UI:** an amber notice above the composer names the bots and says they take turns. Queued
  writers show "Queued until ... finishes (shared workspace)" in the run activity list, and the
  stream carries `write_serialization` and `participant_queued` events.

To let them run at the same time, pass `--allow-concurrent-writes` to `group ask`, `group chat`,
or `session resume`, or tick **Run at the same time** in the web notice (remembered per
conversation in this browser; the API field is `allow_concurrent_writes`). Only do this when the
bots will not touch the same files; worktree isolation (below) is the safer way.

## Worktree isolation

A room can instead give every write-capable bot its own `git worktree`:

```bash
merced-ai group chat builder fixer reviewer --worktrees
```

In the web UI, tick **Give each write-capable bot its own git worktree** when creating the room.

- The first time a write-capable bot runs, Merced AI creates a worktree on branch
  `merced/<conversation>/<bot>` from the commit checked out at that moment, under the Merced AI
  user directory (not inside your project, so it never appears as untracked files). The bot runs
  in the same subdirectory of that worktree as your workspace. Later turns reuse it.
- Isolated bots run at the same time; read-only bots keep using your workspace.
- Your uncommitted changes are not copied into the worktrees; commit first if a bot needs them.
- Compare: `merced-ai group diff SESSION` (a per-bot table) or `group diff SESSION BOT` (the
  patch), `--json` for both; in the web UI, **Compare changes** opens a side-by-side view with
  each bot's files, insertions and deletions, and colored diff.
- Apply: `merced-ai group apply SESSION BOT` (asks first; `--yes` to skip) or **Apply to
  workspace**. The patch must apply cleanly to your files as they are now; otherwise nothing is
  written and the message says which file conflicts, so you can commit or stash your edits or
  merge the bot's branch yourself.
- Discard: `merced-ai group cleanup SESSION` or **Discard all worktrees** removes the worktrees and
  branches. Deleting the conversation in the web UI does the same.
- A workspace that is not in a git repository cannot be isolated; the room says so and falls back
  to running write-capable bots one at a time.

## Safety and failure behavior

The user message is persisted once. Each successful answer is stored with `bot_name` and
`harness_id`; a failed participant does not discard successful answers from other bots. Approval
preflight lists every selected participant whose profile may permit editing or shell access. One
run ID controls the fan-out, and cancellation signals every active participant process.

Harness policy remains authoritative for each process. Merced AI never forwards one bot's answer
as a new request unless the user explicitly sends another turn. This prevents recursive agent
loops, surprise spend, and uncontrolled workspace mutation.

A failed participant exposes **Retry only this bot**. The named-recipient retry does not rerun
successful participants, avoiding duplicate spend or workspace actions.

## Session compatibility

Older session JSON remains valid. At load time, Merced AI derives a one-item `participants` list
from the legacy bot, harness, and profile snapshot fields. Files are rewritten only on the next
ordinary save. New files retain the legacy primary-bot fields so existing integrations can migrate
incrementally.
