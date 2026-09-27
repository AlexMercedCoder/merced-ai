"""Serve Merced AI as an Agent Client Protocol (ACP) agent over stdio.

An ACP client such as Zed launches ``merced-ai acp --bot reviewer`` (or several ``--bot`` options
for a room) and talks JSON-RPC 2.0 on stdin/stdout. Each ACP session is a durable Merced AI
conversation, so the same room can be continued from the CLI or web UI.

Authority stays where it was: the client's user answers permission requests (Merced AI forwards
each harness's AAIS or ACP request as ``session/request_permission``), and launching a
write-capable bot needs the user's consent once per session. The transport is the stdio pipe the
client created, so no network port is opened.

Sessions belong to the agent process that created them. Any conversation in the workspace can be
loaded (its history is replayed), but only sessions this process started can be prompted. With
``allow_resume`` (``--allow-resume``), a loaded conversation can be continued too, provided every
bot in it is one this process serves, so a server started for a read-only bot can never drive a
write-capable one.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, Literal, cast

from aais import create_decision

from merced_ai import __version__
from merced_ai.application import (
    PreparedRun,
    RoutingError,
    is_write_capable,
    participant_from_run,
    prepare_group,
    prepare_run,
)
from merced_ai.bots import BotError
from merced_ai.harnesses import default_registry
from merced_ai.harnesses.registry import HarnessRegistry
from merced_ai.models import SessionRecord
from merced_ai.profiles import ProfileError
from merced_ai.sessions import SessionStore
from merced_ai.turns import execute_turn, plan_turn

PROTOCOL_VERSION = 1
MAX_PROMPT_CHARS = 1_000_000
MAX_LINE_CHARS = 8 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = 16
PARSE_ERROR, INVALID_PARAMS, METHOD_NOT_FOUND, INTERNAL_ERROR = -32700, -32602, -32601, -32603
DECISION_OPTIONS = {
    ("approve", "once"): ("allow_once", "Allow once"),
    ("approve", "session"): ("allow_always", "Always allow in this session"),
    ("approve", "persistent"): ("allow_always", "Always allow"),
    ("deny", "once"): ("reject_once", "Deny"),
}


def _one_line(text: str, limit: int = 300) -> str:
    """Harness-supplied text for a permission title: one line, no control characters, bounded."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in str(text))
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


class RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class MercedAcpAgent:
    """JSON-RPC 2.0 agent over a pair of text streams."""

    def __init__(
        self,
        workspace: Path,
        bots: tuple[str, ...],
        *,
        mode: str = "all",
        isolation: Literal["shared", "worktree"] = "shared",
        registry: HarnessRegistry | None = None,
        reader: IO[str] | None = None,
        writer: IO[str] | None = None,
        allow_resume: bool = False,
    ) -> None:
        if not bots:
            raise ValueError("choose at least one bot with --bot")
        self.workspace = workspace.resolve()
        self.bots = bots
        self.mode = mode
        self.isolation = isolation
        self.registry = registry or default_registry()
        self.reader = reader or sys.stdin
        self.writer = writer or sys.stdout
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._arrived = threading.Condition()
        self._cancellations: dict[str, threading.Event] = {}
        self._consented: set[str] = set()
        self.allow_resume = allow_resume
        # Sessions this process may prompt: the ones it created, plus loaded ones it may resume.
        self._owned: set[str] = set()
        self._workers: list[threading.Thread] = []
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)

    # ---- transport -----------------------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            self.writer.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.writer.flush()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request_client(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._arrived:
            self._next_id += 1
            request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        with self._arrived:
            while request_id not in self._pending:
                self._arrived.wait()
            reply = self._pending.pop(request_id)
        if "error" in reply:
            raise RpcError(INTERNAL_ERROR, str(reply["error"]))
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def serve(self) -> None:
        """Read messages until the client closes stdin."""
        while True:
            line = self.reader.readline(MAX_LINE_CHARS + 1)
            if not line:
                break
            if len(line) > MAX_LINE_CHARS:
                while line and not line.endswith("\n"):
                    line = self.reader.readline(MAX_LINE_CHARS)
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": INVALID_PARAMS, "message": "message is too large"},
                    }
                )
                continue
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": PARSE_ERROR, "message": "parse error"},
                    }
                )
                continue
            if not isinstance(message, dict):
                continue
            if "method" not in message:
                if isinstance(message.get("id"), int):
                    with self._arrived:
                        self._pending[message["id"]] = message
                        self._arrived.notify_all()
                continue
            if "id" not in message:
                self._dispatch(message)  # Notifications (session/cancel) are cheap; run inline.
                continue
            if not self._slots.acquire(blocking=False):
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": INTERNAL_ERROR,
                            "message": "too many concurrent requests",
                        },
                    }
                )
                continue
            worker = threading.Thread(target=self._dispatch_slot, args=(message,), daemon=True)
            self._workers = [item for item in self._workers if item.is_alive()]
            self._workers.append(worker)
            worker.start()
        for event in self._cancellations.values():
            event.set()
        for worker in self._workers:
            worker.join(timeout=10)

    def _dispatch_slot(self, message: dict[str, Any]) -> None:
        try:
            self._dispatch(message)
        finally:
            self._slots.release()

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = str(message["method"])
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        handler: Callable[[dict[str, Any]], dict[str, Any] | None] | None = {
            "initialize": self.initialize,
            "authenticate": lambda _params: {},
            "session/new": self.new_session,
            "session/load": self.load_session,
            "session/prompt": self.prompt,
            "session/cancel": self.cancel,
        }.get(method)
        is_request = "id" in message
        try:
            if handler is None:
                raise RpcError(METHOD_NOT_FOUND, f"{method} is not supported")
            result = handler(params)
            if is_request:
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": result or {}})
        except RpcError as error:
            if is_request:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": error.code, "message": str(error)},
                    }
                )
        except Exception as error:  # Never leave the client waiting.
            if is_request:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": INTERNAL_ERROR, "message": str(error)[:500]},
                    }
                )

    # ---- protocol methods ------------------------------------------------------------------

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": True},
            },
            "authMethods": [],
            "agentInfo": {"name": "merced-ai", "title": "Merced AI", "version": __version__},
        }

    def _workspace(self, params: dict[str, Any]) -> Path:
        cwd = params.get("cwd")
        if cwd and Path(cwd).resolve() != self.workspace:
            raise RpcError(
                INVALID_PARAMS,
                f"this Merced AI agent serves {self.workspace}; start it with -C {cwd} to use "
                "that project",
            )
        return self.workspace

    def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace = self._workspace(params)
        prepared: tuple[PreparedRun, ...]
        try:
            if len(self.bots) == 1:
                prepared = (prepare_run(self.bots[0], "Start.", workspace, registry=self.registry),)
            else:
                prepared = prepare_group(self.bots, workspace, registry=self.registry)
        except (BotError, ProfileError, RoutingError, ValueError) as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        session = SessionStore(workspace).create_group(
            tuple(participant_from_run(item) for item in prepared),
            mode=cast(Literal["mentions", "all", "round_robin"], self.mode),
            title=f"ACP · {', '.join(self.bots)}",
            isolation=self.isolation if len(prepared) > 1 else "shared",
        )
        self._owned.add(session.id)
        return {"sessionId": session.id}

    def _resume_refusal(self, session: SessionRecord) -> str | None:
        """Why this process may not prompt ``session``, or None when it may."""
        if session.id in self._owned:
            return None
        if not self.allow_resume:
            return (
                "This conversation was not started by this Merced AI agent, so it is read-only "
                "here. Start a new conversation, or start `merced-ai acp` with --allow-resume to "
                "continue conversations from other clients."
            )
        foreign = sorted({item.bot_name for item in session.participants} - set(self.bots))
        if foreign:
            return (
                f"This conversation includes {', '.join(foreign)}, which this agent does not "
                f"serve (it serves {', '.join(self.bots)}), so it is read-only here."
            )
        return None

    def load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace = self._workspace(params)
        try:
            session = SessionStore(workspace).load(str(params.get("sessionId", "")))
        except ValueError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        for turn in session.turns:
            kind = "user_message_chunk" if turn.role == "user" else "agent_message_chunk"
            text = (
                turn.content
                if turn.role == "user" or len(session.participants) == 1
                else (f"**{turn.bot_name}:** {turn.content}")
            )
            self._update(
                session.id, {"sessionUpdate": kind, "content": {"type": "text", "text": text}}
            )
        refusal = self._resume_refusal(session)
        if refusal is None:
            self._owned.add(session.id)
        else:
            self._text(session.id, f"\n\n_{refusal}_")
        return {}

    def cancel(self, params: dict[str, Any]) -> None:
        event = self._cancellations.get(str(params.get("sessionId", "")))
        if event is not None:
            event.set()

    def _update(self, session_id: str, update: dict[str, Any]) -> None:
        self.notify("session/update", {"sessionId": session_id, "update": update})

    def _text(self, session_id: str, text: str) -> None:
        self._update(
            session_id,
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
        )

    def _consent(self, session: SessionRecord, writers: list[str]) -> bool:
        """Ask once per session before running bots that may edit files or run commands."""
        if not writers or session.id in self._consented:
            return True
        reply = self.request_client(
            "session/request_permission",
            {
                "sessionId": session.id,
                "toolCall": {
                    "toolCallId": f"consent-{session.id}",
                    "title": f"Run {', '.join(writers)} with permission to edit files or run "
                    f"commands in {self.workspace}",
                    "kind": "other",
                    "status": "pending",
                },
                "options": [
                    {"optionId": "allow", "name": "Allow for this session", "kind": "allow_always"},
                    {"optionId": "reject", "name": "Don't run them", "kind": "reject_once"},
                ],
            },
        )
        outcome = reply.get("outcome") or {}
        allowed = outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow"
        if allowed:
            self._consented.add(session.id)
        return allowed

    def _relay_approval(
        self, session_id: str
    ) -> Callable[[dict[str, Any], threading.Event | None], dict[str, Any]]:
        def handler(envelope: dict[str, Any], _cancel: threading.Event | None) -> dict[str, Any]:
            request = envelope["request"]
            options = []
            by_option: dict[str, tuple[str, str]] = {}
            for choice in request["choices"]:
                key = (choice["decision"], choice["scope"])
                if key not in DECISION_OPTIONS:
                    continue
                kind, label = DECISION_OPTIONS[key]
                option_id = f"{choice['decision']}-{choice['scope']}"
                options.append(
                    # Our label, never the harness's: it could call an "always allow" "Deny".
                    {"optionId": option_id, "name": label, "kind": kind}
                )
                by_option[option_id] = key
            reply = self.request_client(
                "session/request_permission",
                {
                    "sessionId": session_id,
                    "toolCall": {
                        "toolCallId": request["id"],
                        "title": _one_line(
                            f"{request['origin']['harness']}: {request['action']['summary']}"
                        ),
                        "kind": "other",
                        "status": "pending",
                        "rawInput": request["action"].get("arguments", {}),
                    },
                    "options": options,
                },
            )
            outcome = reply.get("outcome") or {}
            decision, scope = ("cancel", "once")
            if outcome.get("outcome") == "selected" and outcome.get("optionId") in by_option:
                decision, scope = by_option[outcome["optionId"]]
            return create_decision(
                envelope,
                decision=decision,
                scope=scope,
                actor={"id": "acp-client-user", "type": "human", "authenticated_by": "acp-client"},
                sequence=int(envelope.get("sequence", 1)),
                stream="merced-ai.acp-server",
            )

        return handler

    def prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId", ""))
        blocks = params.get("prompt")
        text = _prompt_text(blocks if isinstance(blocks, list) else [])
        if len(text) > MAX_PROMPT_CHARS:
            raise RpcError(
                INVALID_PARAMS, f"the prompt is too large (max {MAX_PROMPT_CHARS} chars)"
            )
        store = SessionStore(self.workspace)
        try:
            session = store.load(session_id)
        except ValueError as error:
            raise RpcError(INVALID_PARAMS, str(error)) from error
        if session_id not in self._owned:
            # Loading is how a session becomes resumable; prompting blind is always refused.
            raise RpcError(
                INVALID_PARAMS,
                self._resume_refusal(session)
                or "load this conversation with session/load before prompting it",
            )
        if not text.strip():
            raise RpcError(INVALID_PARAMS, "the prompt has no text")
        cancellation = threading.Event()
        self._cancellations[session_id] = cancellation
        try:
            try:
                plan = plan_turn(
                    session, text, self.workspace, registry=self.registry, dispatch=None
                )
            except (BotError, ProfileError, RoutingError, ValueError) as error:
                self._text(session_id, f"Merced AI could not route this turn: {error}")
                return {"stopReason": "end_turn"}
            writers = [item.bot.name for item in plan.prepared if is_write_capable(item.profile)]
            if not self._consent(session, writers):
                self._text(session_id, "Not run: permission to edit or run commands was declined.")
                return {"stopReason": "end_turn"}
            group = len(session.participants) > 1
            started: set[str] = set()
            lock = threading.Lock()

            def on_event(bot: str, event: dict[str, Any]) -> None:
                kind = event.get("type")
                if kind == "assistant_delta":
                    with lock:
                        prefix = "" if not group or bot in started else f"\n\n**{bot}:** "
                        started.add(bot)
                    self._text(session_id, prefix + str(event.get("text", "")))
                elif kind == "tool_call":
                    call = dict(event.get("tool_call") or {})
                    call["toolCallId"] = f"{bot}:{call.get('toolCallId', '')}"
                    self._update(session_id, {"sessionUpdate": "tool_call_update", **call})

            outcome = execute_turn(
                session,
                text,
                plan,
                self.workspace,
                registry=self.registry,
                cancellation=cancellation,
                approval_handler=self._relay_approval(session_id),
                on_event=on_event,
            )
            for bot, reply in outcome.replies:
                if bot not in started:  # Non-streaming harnesses: send the whole reply now.
                    self._text(session_id, f"\n\n**{bot}:** {reply}" if group else reply)
            for bot, failure in outcome.failures:
                self._text(session_id, f"\n\n**{bot}** failed: {failure}")
            return {"stopReason": "cancelled" if cancellation.is_set() else "end_turn"}
        finally:
            self._cancellations.pop(session_id, None)


def _prompt_text(blocks: list[Any]) -> str:
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        elif block.get("type") == "resource":
            resource = block.get("resource") or {}
            if isinstance(resource.get("text"), str):
                parts.append(f"\n\n[{resource.get('uri', 'context')}]\n{resource['text']}")
        elif block.get("type") == "resource_link":
            parts.append(f"\n\n[Linked resource: {block.get('uri', '')}]")
    return "".join(parts)
