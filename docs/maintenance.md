# Merced AI maintenance tracker

Audited 2026-11-24 against checkout `4d1f7ba`. The five bot/session tests passed. A separate temporary workspace contained one valid session and one damaged file.

Claim a row by entering your name or task ID and setting the status to **In progress**. Use **Blocked** with the missing dependency if needed. Set **Done** after the acceptance check passes and record the fix commit or PR, the command or manual check, and its result. If a newer revision already resolves the item, record that evidence and close it. Keep status in this table.

| ID | Priority | Status | Owner | Fix and verification |
| --- | --- | --- | --- | --- |
| MER-01 | P1 | Done | — | `5e6443f`. Six tests passed. The new test checks the real CLI and bootstrap response builder while preserving the damaged file. |

### MER-01 · Let healthy conversations open alongside a damaged session

**Reproduced at store level.** `SessionStore.list()` raised ValidationError when `session-damaged.json` contained an incomplete JSON object. The workspace bootstrap and CLI session list both call this method. See [src/merced_ai/sessions.py](../src/merced_ai/sessions.py) and [src/merced_ai/web/routers/workspace.py](../src/merced_ai/web/routers/workspace.py).

**Acceptance check.** Exercise the CLI list and /api/bootstrap with a valid session plus one unreadable or invalid session. Healthy sessions must remain usable, the affected filename must be identifiable, and neither file may be rewritten. Direct access to the damaged record should give a clear error.
