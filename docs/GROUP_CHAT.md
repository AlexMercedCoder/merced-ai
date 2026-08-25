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

Launch `merced-ai ui`, then select **Group**. Enter two or more existing bot names and choose the
room's default dispatch mode. The composer can target mentioned bots, everyone, the next
round-robin participant, or one named participant. Participant chips show each bot's pinned
harness. Exported Markdown includes the bot and harness on every assistant response.

## Dispatch and ordering

- `mentions`: exact, case-insensitive `@bot-name` matches select recipients. With no mention, the
  first participant responds.
- `all`: every participant responds.
- `round_robin`: one participant responds; persisted assistant-turn count chooses the next bot.
- a bot name: only that participant responds (web API/UI).

Multi-recipient runs execute concurrently. Prompts are isolated per bot and identify the intended
speaker. Results, tool events, and persisted assistant turns are emitted in the original
participant order, making transcripts deterministic even when a later bot finishes first.

## Safety and failure behavior

The user message is persisted once. Each successful answer is stored with `bot_name` and
`harness_id`; a failed participant does not discard successful answers from other bots. Approval
preflight lists every selected participant whose profile may permit editing or shell access. One
run ID controls the fan-out, and cancellation signals every active participant process.

Harness policy remains authoritative for each process. Merced AI never forwards one bot's answer
as a new request unless the user explicitly sends another turn. This prevents recursive agent
loops, surprise spend, and uncontrolled workspace mutation.

## Session compatibility

Older session JSON remains valid. At load time, Merced AI derives a one-item `participants` list
from the legacy bot, harness, and profile snapshot fields. Files are rewritten only on the next
ordinary save. New files retain the legacy primary-bot fields so existing integrations can migrate
incrementally.
