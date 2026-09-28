# Serving Merced AI to other clients

Merced AI can be the agent another tool talks to, so an editor or another agent can drive one of
your bots or a whole room. Both surfaces are new in 0.8.0.

## As an ACP agent (stdio)

```bash
merced-ai acp --bot reviewer -C ~/code/project            # one bot
merced-ai acp --bot builder --bot tester --worktrees -C .  # a room
```

An Agent Client Protocol client starts this command and speaks JSON-RPC over its stdin and stdout.
For example, in Zed's `settings.json`:

```json
{
  "agent_servers": {
    "Merced AI reviewer": {
      "command": "merced-ai",
      "args": ["acp", "--bot", "reviewer", "-C", "/path/to/project"]
    }
  }
}
```

What the client gets:

- **Sessions are Merced AI conversations.** `session/new` creates one (a room when you pass more
  than one `--bot`); `session/load` replays it. The same conversation shows up in
  `merced-ai session list` and the web UI.
- **Sessions belong to the process that started them.** A conversation started by another client,
  by an earlier run of this command, or in the CLI or web UI loads read-only: its history is
  replayed with a note, and prompting it is refused. To continue such conversations (for example
  when your editor reopens its threads after a restart), add `--allow-resume`. Even then, only
  conversations whose bots are all served by this command can be continued, so an agent started
  for a read-only bot can never drive a room with write-capable bots.
- **Streaming.** Replies from harnesses that run over ACP stream as `agent_message_chunk`
  updates; other harnesses send their reply when they finish. In a room each reply starts with
  the bot's name.
- **Consent and approvals.** Before a bot that may edit files or run commands runs for the first
  time in a session, the client is asked (`session/request_permission`) whether to allow it for
  the session. Permission requests from the harness itself (AAIS from MagAgent and Loro, or ACP
  from an ACP harness) are forwarded the same way, and the user's choice goes back to the harness.
- **The rest of the room rules apply:** write-capable bots take turns or get worktrees, profile
  drift is refused, and failures of one bot do not hide the others' replies.
- `session/cancel` stops the running harnesses. The agent advertises no file-system or terminal
  capability and serves only the `-C` workspace; a `cwd` elsewhere is refused.

There is no network listener: the security boundary is the process that launched the command.

## As an A2A endpoint (experimental, HTTP)

`merced-ai ui` also serves an [A2A](https://a2a-protocol.org) JSON-RPC endpoint on the same
loopback address and token as the UI:

- Agent card: `GET /.well-known/agent-card.json` (one skill per bot).
- Endpoint: `POST /a2a` with `Authorization: Bearer <token>`, where the token is the one in the
  URL `merced-ai ui` prints.
- `message/send` runs a turn. Pick the bot with `metadata.bot` or a room with `metadata.bots`
  (otherwise the first bot); continue a conversation by sending its `contextId`. Each bot's reply
  is an artifact named after the bot.
- Bots that may edit files or run commands need `metadata.approved: true`; without it the task
  comes back `input-required` with an explanation. Harness permission requests appear in the web
  UI approval dialog, where a person decides.
- `tasks/get` and `tasks/cancel` work for tasks served by the running process. The process keeps
  the latest 200 tasks; older ones answer "task not found".
- Limits: requests must use a loopback host name (`127.0.0.1`, `localhost`, or `[::1]`; other
  `Host` headers get 421, which blocks DNS rebinding), bodies are capped at 16 MB and must carry a
  `Content-Length` (chunked bodies get 411), and message text is capped at 100,000 characters.
- `message/stream`, resubscribe, and push notifications are not implemented and return the A2A
  "unsupported operation" error; the agent card says `streaming: false`.

Example:

```bash
curl -s http://127.0.0.1:8773/a2a -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"message/send","params":{"message":{"role":"user",
  "messageId":"m1","parts":[{"kind":"text","text":"Review README.md"}],"metadata":{"bot":"reviewer"}}}}'
```
