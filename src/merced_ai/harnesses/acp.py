"""Agent Client Protocol (ACP) client adapter.

Runs one ACP agent process per turn over JSON-RPC 2.0 on stdio (newline-delimited) and gives
Merced AI what the one-shot subprocess adapters cannot:

- **Streaming.** ``agent_message_chunk`` updates are forwarded as they arrive.
- **Approvals.** ``session/request_permission`` requests are presented through the same AAIS
  presenter the web UI uses for MagAgent and Loro, and the user's decision is mapped back onto
  the agent's own permission options. Without a presenter (the CLI), requests are rejected.
- **Native resume.** When the agent supports ``session/load`` and the conversation recorded the
  agent's session ID, the next turn loads that session and sends only the new message instead
  of replaying a flattened transcript.

The harness stays the policy authority: Merced AI only answers the permission requests the agent
chooses to send, never switches an agent into an auto-approve mode, and selects a read-only mode
when the profile denies both editing and shell access.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aais import create_request

from merced_ai.harnesses.acp_launch import AcpLaunch
from merced_ai.harnesses.adapters.command import CommandHarnessAdapter, HarnessRunError
from merced_ai.harnesses.api import HarnessSpec, prefixed_prompt
from merced_ai.harnesses.detection import locate_executable
from merced_ai.harnesses.process import Capture, stop_process
from merced_ai.models import (
    HarnessCapabilities,
    HarnessProbe,
    ProfileProjection,
    ProfileRecord,
    ProjectionAdjustment,
    RunRequest,
    RunResult,
    TransportKind,
)

ACP_PROTOCOL_VERSION = 1
MAX_STDERR = 64_000
# Modes that approve tool calls without asking; Merced AI never selects them.
AUTO_APPROVE_MODES = {"auto", "yolo", "bypassPermissions", "acceptEdits", "autoEdit"}
READ_ONLY_MODES = ("plan", "chat", "read-only", "readonly")
ASKING_MODES = ("default", "approve", "manual", "smart_approve")
EDIT_KINDS = {"edit", "delete", "move"}
SHELL_KINDS = {"execute"}

ApprovalHandler = Callable[[dict[str, Any], threading.Event | None], dict[str, Any]]
EventHandler = Callable[[dict[str, Any]], None]


class AcpError(RuntimeError):
    pass


class AcpConnection:
    """A JSON-RPC 2.0 client for one ACP agent process."""

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        on_notification: Callable[[str, dict[str, Any]], None],
        on_request: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> None:
        self._on_notification = on_notification
        self._on_request = on_request
        self._next_id = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._arrived = threading.Condition()
        self._write_lock = threading.Lock()
        self.stderr = Capture(MAX_STDERR)
        self.closed = threading.Event()
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        data = (json.dumps(message) + "\n").encode()
        with self._write_lock:
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except OSError as error:
                raise AcpError(f"agent closed its input: {error}") from error

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        descriptor = self.process.stderr.fileno()
        try:
            while chunk := os.read(descriptor, 16_384):
                self.stderr.append(chunk)
        except OSError:
            return

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # Agents sometimes print banners; JSON-RPC lines are what count.
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    threading.Thread(target=self._answer, args=(message,), daemon=True).start()
                elif "method" in message:
                    self._on_notification(str(message["method"]), message.get("params") or {})
                elif "id" in message:
                    with self._arrived:
                        self._pending[int(message["id"])] = message
                        self._arrived.notify_all()
        finally:
            self.closed.set()
            with self._arrived:
                self._arrived.notify_all()

    def _answer(self, message: dict[str, Any]) -> None:
        try:
            result = self._on_request(str(message["method"]), message.get("params") or {})
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except AcpMethodNotFound:
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": f"{message['method']} is not supported"},
            }
        except Exception as error:  # Report instead of leaving the agent waiting forever.
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32603, "message": str(error)[:500]},
            }
        try:
            self._send(reply)
        except AcpError:
            pass

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        cancellation: threading.Event | None = None,
        on_cancel: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        cancel_sent = False
        with self._arrived:
            while request_id not in self._pending:
                if self.closed.is_set():
                    raise AcpError(f"agent exited during {method}")
                if cancellation is not None and cancellation.is_set() and not cancel_sent:
                    cancel_sent = True
                    if on_cancel is not None:
                        on_cancel()
                    # Give the agent a moment to stop cleanly and answer "cancelled".
                    deadline = min(deadline, time.monotonic() + 5)
                if time.monotonic() >= deadline:
                    if cancel_sent:
                        raise HarnessRunError("ACP run was cancelled", exit_code=130)
                    raise HarnessRunError(
                        f"ACP {method} timed out after {timeout:.0f}s", exit_code=5
                    )
                self._arrived.wait(0.05)
            reply = self._pending.pop(request_id)
        if "error" in reply:
            error = reply["error"] or {}
            raise AcpError(f"{method} failed: {error.get('message', error)}")
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            stop_process(self.process)


class AcpMethodNotFound(Exception):
    pass


def choose_mode(modes: dict[str, Any] | None, *, read_only: bool) -> str | None:
    """Pick a mode that keeps approvals with the user; never an auto-approve mode."""
    if not isinstance(modes, dict):
        return None
    available = [
        str(item.get("id")) for item in modes.get("availableModes", []) if isinstance(item, dict)
    ]
    preferences = (READ_ONLY_MODES + ASKING_MODES) if read_only else ASKING_MODES
    for candidate in preferences:
        if candidate in available:
            return candidate
    current = modes.get("currentModeId")
    if current in AUTO_APPROVE_MODES:
        raise HarnessRunError(
            f"The agent starts in auto-approve mode {current!r} and offers no mode that asks "
            "first; Merced AI will not run it over ACP.",
            exit_code=4,
        )
    return None


@dataclass
class _Turn:
    request: RunRequest
    harness_id: str
    session_id: str | None = None
    replaying: bool = False
    # Updates before our prompt (session replay, mode-change notices) are not part of the reply.
    prompting: bool = False
    text: list[str] = field(default_factory=list)
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    permissions: list[dict[str, Any]] = field(default_factory=list)
    sequence: int = 0


class AcpHarnessAdapter(CommandHarnessAdapter):
    """A harness that speaks ACP, with its one-shot subprocess adapter as the fallback.

    ACP is used when the launcher is installed and ``MERCED_AI_ACP`` is not ``0``; otherwise
    every call goes to the spec's subprocess adapter.
    """

    streams_output = True

    def __init__(self, spec: HarnessSpec, launch: AcpLaunch) -> None:
        super().__init__(spec)
        self.launch = launch

    @property
    def relays_approvals(self) -> bool:
        return self.acp_available() or self.spec.aais_control

    def acp_argv(self) -> list[str] | None:
        if os.environ.get("MERCED_AI_ACP") == "0":
            return None
        if self.launch.executable_names:
            executable = locate_executable(
                self.descriptor.model_copy(
                    update={
                        "id": f"{self.descriptor.id}-acp",
                        "executable_names": self.launch.executable_names,
                    }
                )
            )
        else:
            executable = locate_executable(self.descriptor)
        return [str(executable), *self.launch.args] if executable else None

    def acp_available(self) -> bool:
        return self.acp_argv() is not None

    def probe(self, workspace: Path | None = None) -> HarnessProbe:
        probe = super().probe() if workspace is None else super().probe(workspace=workspace)
        if probe.path is None or not self.acp_available():
            return probe.model_copy(update={"transport": TransportKind.STRUCTURED_SUBPROCESS})
        return probe.model_copy(
            update={
                "transport": TransportKind.ACP_STDIO,
                "broker_implements": probe.broker_implements.model_copy(
                    update={**ACP_BROKER, "resume": self.launch.resumes}
                ),
            }
        )

    def project_profile(self, profile: ProfileRecord) -> ProfileProjection:
        projection = super().project_profile(profile)
        if not self.acp_available():
            return projection
        requested = profile.document.get("spec", {}).get("model", {}).get("id")
        adjustments = [
            item for item in projection.adjustments if not item.field.startswith("spec.model")
        ]
        adjustments.append(
            ProjectionAdjustment(
                field="spec.permissions",
                action="mapped",
                reason=(
                    "Over ACP the agent asks before tool calls and Merced AI relays each request "
                    "for approval; edit/shell denial selects a read-only mode and rejects "
                    "matching requests."
                ),
            )
        )
        if requested:
            adjustments.append(
                ProjectionAdjustment(
                    field="spec.model",
                    action="dropped",
                    reason="ACP sessions use the model configured in the harness.",
                )
            )
        return projection.model_copy(
            update={
                "support_level": "degraded",
                "model": None,
                "adjustments": tuple(adjustments),
            }
        )

    def run_cancellable(
        self,
        request: RunRequest,
        cancellation: threading.Event | None,
        approval_handler: ApprovalHandler | None = None,
        approval_event_handler: EventHandler | None = None,
        on_event: EventHandler | None = None,
    ) -> RunResult:
        argv = self.acp_argv()
        if argv is None:
            return super().run_cancellable(
                request, cancellation, approval_handler, approval_event_handler
            )
        return self._run_acp(argv, request, cancellation, approval_handler, on_event)

    def _run_acp(
        self,
        argv: list[str],
        request: RunRequest,
        cancellation: threading.Event | None,
        approval_handler: ApprovalHandler | None,
        on_event: EventHandler | None,
    ) -> RunResult:
        started = time.monotonic()
        turn = _Turn(request, self.descriptor.id)
        emit = on_event or (lambda _event: None)
        permissions = request.profile.document.get("spec", {}).get("permissions", {})
        edit_denied = permissions.get("edit") == "deny"
        shell_denied = permissions.get("shell") == "deny"

        def on_notification(method: str, params: dict[str, Any]) -> None:
            if method != "session/update" or turn.replaying or not turn.prompting:
                return
            update = params.get("update") or {}
            kind = update.get("sessionUpdate")
            if kind == "agent_message_chunk":
                content = update.get("content") or {}
                if content.get("type") == "text" and isinstance(content.get("text"), str):
                    turn.text.append(content["text"])
                    emit({"type": "assistant_delta", "text": content["text"]})
            elif kind in {"tool_call", "tool_call_update"}:
                call_id = str(update.get("toolCallId", ""))
                merged = {**turn.tool_calls.get(call_id, {}), **_tool_summary(update)}
                turn.tool_calls[call_id] = merged
                emit({"type": "tool_call", "tool_call": merged})
            elif kind == "plan":
                emit({"type": "plan", "entries": update.get("entries", [])[:50]})

        def on_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "session/request_permission":
                return self._decide_permission(
                    turn, params, approval_handler, cancellation, edit_denied, shell_denied, emit
                )
            # Merced AI advertises no filesystem or terminal capability.
            raise AcpMethodNotFound(method)

        env = {**self.environment(request, request.workspace), **self.launch.env}
        connection = AcpConnection(
            argv,
            cwd=request.workspace,
            env=env,
            on_notification=on_notification,
            on_request=on_request,
        )
        try:
            init = connection.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "merced-ai", "title": "Merced AI", "version": "0.8.0"},
                },
                timeout=60,
            )
            can_load = bool((init.get("agentCapabilities") or {}).get("loadSession"))
            session_args = {"cwd": str(request.workspace), "mcpServers": []}
            resumed = False
            load_error: str | None = None
            modes: dict[str, Any] | None = None
            if request.native_session_id and can_load:
                turn.replaying = True
                try:
                    loaded = connection.request(
                        "session/load",
                        {"sessionId": request.native_session_id, **session_args},
                        timeout=120,
                    )
                    turn.session_id, resumed = request.native_session_id, True
                    modes = loaded.get("modes")
                except AcpError as error:
                    # The agent no longer has it (or cannot load it); start fresh below.
                    resumed, load_error = False, str(error)[:300]
                finally:
                    turn.replaying = False
            if not resumed:
                created = connection.request("session/new", session_args, timeout=120)
                turn.session_id = str(created["sessionId"])
                modes = created.get("modes")
            mode = choose_mode(modes, read_only=edit_denied and shell_denied)
            if mode and mode != (modes or {}).get("currentModeId"):
                connection.request(
                    "session/set_mode", {"sessionId": turn.session_id, "modeId": mode}, timeout=30
                )
            if resumed and request.turn_prompt:
                text = request.turn_prompt
            else:
                text = prefixed_prompt(request.projection.system_prompt, request.prompt)
            emit({"type": "acp_session", "session_id": turn.session_id, "resumed": resumed})
            turn.prompting = True
            result = connection.request(
                "session/prompt",
                {"sessionId": turn.session_id, "prompt": [{"type": "text", "text": text}]},
                timeout=request.timeout_seconds,
                cancellation=cancellation,
                on_cancel=lambda: connection.notify(
                    "session/cancel", {"sessionId": turn.session_id}
                ),
            )
        except AcpError as error:
            tail = connection.stderr.text().strip().splitlines()[-1:] or [""]
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} (ACP) failed: {error}. {tail[0][:300]}".strip(),
                exit_code=1,
                stderr=connection.stderr.text(),
            ) from error
        finally:
            connection.close()
        stop_reason = str(result.get("stopReason", ""))
        if stop_reason == "cancelled" or (cancellation is not None and cancellation.is_set()):
            raise HarnessRunError(f"Harness {self.descriptor.id!r} was cancelled.", exit_code=130)
        return RunResult(
            harness_id=self.descriptor.id,
            output="".join(turn.text).strip(),
            exit_code=0,
            native_session_id=turn.session_id,
            raw={
                "transport": "acp",
                "stop_reason": stop_reason,
                "resumed": resumed,
                "load_session": can_load,
                "load_error": load_error,
                "tool_calls": list(turn.tool_calls.values())[-50:],
                "permissions": turn.permissions,
            },
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    def _decide_permission(
        self,
        turn: _Turn,
        params: dict[str, Any],
        approval_handler: ApprovalHandler | None,
        cancellation: threading.Event | None,
        edit_denied: bool,
        shell_denied: bool,
        emit: EventHandler,
    ) -> dict[str, Any]:
        options = [item for item in params.get("options", []) if isinstance(item, dict)]
        tool = params.get("toolCall") or {}
        kind = str(tool.get("kind") or "other")
        title = str(tool.get("title") or kind)
        denied_by_profile = (kind in EDIT_KINDS and edit_denied) or (
            kind in SHELL_KINDS and shell_denied
        )
        decision, scope = "deny", "once"
        if not denied_by_profile and approval_handler is not None:
            turn.sequence += 1
            choices: list[dict[str, Any]] = [
                {"decision": "approve", "scope": "once", "label": "Allow once"}
            ]
            if _option(options, "allow_always"):
                choices.append(
                    {
                        "decision": "approve",
                        "scope": "session",
                        "label": "Always allow",
                        # The agent applies "always" within its own session and tool kind.
                        "scope_constraints": {
                            "acp_session_id": turn.session_id or "acp",
                            "tool_kind": kind,
                        },
                    }
                )
            choices.append({"decision": "deny", "scope": "once", "label": "Deny"})
            envelope = create_request(
                action={
                    "kind": "tool.call",
                    "name": kind,
                    "summary": title[:200],
                    "arguments": _safe_json(tool.get("rawInput") or {}),
                    "working_directory": str(turn.request.workspace),
                },
                origin={
                    "harness": turn.harness_id,
                    "session_id": turn.session_id or "acp",
                    "project": str(turn.request.workspace),
                },
                risk={
                    "level": "high" if kind in EDIT_KINDS | SHELL_KINDS else "medium",
                    "reasons": [f"The agent wants to run a {kind} tool call over ACP."],
                },
                choices=choices,
                sequence=turn.sequence,
                stream=f"acp.{turn.harness_id}",
            )
            emit({"type": "approval_pending", "summary": title[:200], "kind": kind})
            body = approval_handler(envelope, cancellation).get("decision", {})
            decision, scope = str(body.get("decision", "deny")), str(body.get("scope", "once"))
        turn.permissions.append(
            {
                "title": title[:200],
                "kind": kind,
                "decision": decision,
                "scope": scope,
                "by_profile": denied_by_profile,
            }
        )
        if decision == "cancel":
            return {"outcome": {"outcome": "cancelled"}}
        wanted = (
            ("allow_always", "allow_once")
            if decision == "approve" and scope != "once"
            else ("allow_once",)
            if decision == "approve"
            else ("reject_once", "reject_always")
        )
        for option_kind in wanted:
            if option := _option(options, option_kind):
                return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        return {"outcome": {"outcome": "cancelled"}}


ACP_BROKER = {"streaming": True, "resume": True, "approvals": True}


def _option(options: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((item for item in options if item.get("kind") == kind), None)


def _tool_summary(update: dict[str, Any]) -> dict[str, Any]:
    return {
        key: update[key]
        for key in ("toolCallId", "title", "kind", "status")
        if key in update and isinstance(update[key], str)
    }


def _safe_json(value: Any) -> dict[str, Any]:
    try:
        text = json.dumps(value)[:4000]
        loaded = json.loads(text) if len(text) < 4000 else {"truncated": text}
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {"value": loaded}


def acp_capabilities() -> HarnessCapabilities:
    return HarnessCapabilities(attachments=True, **ACP_BROKER)


__all__ = ["AcpHarnessAdapter", "AcpLaunch", "acp_capabilities", "choose_mode"]
