#!/usr/bin/env python3
"""Deterministic ACP agent for tests. Behavior is driven by keywords in the prompt text.

- PERMISSION:<kind> asks for permission for a tool call of that kind before answering.
- SLOW waits (up to 30 s) for session/cancel and then answers stopReason "cancelled".
- CRASH exits with an error message on stderr.
Every received message is appended to the file named by FAKE_ACP_LOG.
"""

import json
import os
import sys
import threading

LOG = os.environ.get("FAKE_ACP_LOG")
MODES = json.loads(os.environ.get("FAKE_ACP_MODES", "null") or "null")
LOAD = os.environ.get("FAKE_ACP_LOAD", "1") == "1"
cancelled = threading.Event()
answers = {}
answered = threading.Condition()
next_id = [1000]


def log(message):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(message) + "\n")


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def update(session_id, payload):
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": payload},
        }
    )


def ask(method, params):
    next_id[0] += 1
    request_id = next_id[0]
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    with answered:
        while request_id not in answers:
            answered.wait()
        return answers.pop(request_id)


def chunk(session_id, text):
    update(
        session_id,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
    )


def handle_prompt(message):
    params = message["params"]
    sid = params["sessionId"]
    text = "".join(block.get("text", "") for block in params["prompt"])
    if "CRASH" in text:
        sys.stderr.write("fatal: the fake agent crashed on purpose\n")
        sys.stderr.flush()
        os._exit(3)
    if "PERMISSION:" in text:
        kind = text.split("PERMISSION:", 1)[1].split()[0]
        update(
            sid,
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": f"Run {kind}",
                "kind": kind,
                "status": "pending",
            },
        )
        reply = ask(
            "session/request_permission",
            {
                "sessionId": sid,
                "toolCall": {
                    "toolCallId": "t1",
                    "title": f"Run {kind}",
                    "kind": kind,
                    "rawInput": {"path": "notes.md"},
                },
                "options": [
                    {"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "always", "name": "Always", "kind": "allow_always"},
                    {"optionId": "no", "name": "Reject", "kind": "reject_once"},
                ],
            },
        )
        outcome = reply.get("result", {}).get("outcome", {})
        chunk(sid, f"permission={outcome.get('optionId', outcome.get('outcome'))} ")
    if "SLOW" in text:
        cancelled.wait(30)
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": "cancelled"}})
        return
    chunk(sid, "Hello ")
    chunk(sid, "from ACP")
    send({"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": "end_turn"}})


for line in sys.stdin:
    message = json.loads(line)
    log(message)
    method = message.get("method")
    if method is None:
        with answered:
            answers[message["id"]] = message
            answered.notify_all()
        continue
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {"loadSession": LOAD},
                    "authMethods": [],
                },
            }
        )
    elif method == "session/new":
        result = {"sessionId": "sess-new"}
        if MODES:
            result["modes"] = MODES
        send({"jsonrpc": "2.0", "id": message["id"], "result": result})
    elif method == "session/load":
        sid = message["params"]["sessionId"]
        if sid == "gone":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32002, "message": "session not found"},
                }
            )
            continue
        chunk(sid, "REPLAYED HISTORY ")
        send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
    elif method == "session/set_mode":
        # Like Gemini CLI, announce the mode change as a message chunk before any prompt.
        chunk(message["params"]["sessionId"], "[MODE_UPDATE] " + message["params"]["modeId"])
        send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
    elif method == "session/prompt":
        threading.Thread(target=handle_prompt, args=(message,), daemon=True).start()
    elif method == "session/cancel":
        cancelled.set()
