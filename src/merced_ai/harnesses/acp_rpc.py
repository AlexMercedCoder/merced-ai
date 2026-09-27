"""JSON-RPC 2.0 client connection to one ACP agent process (newline-delimited stdio).

Everything the agent sends is untrusted: lines are length-bounded, non-JSON and malformed
messages are dropped without stopping the reader, and concurrent agent-to-client requests are
capped so an agent cannot spawn unbounded threads.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from merced_ai.harnesses.adapters.command import HarnessRunError
from merced_ai.harnesses.process import Capture, stop_process

__all__ = [
    "MAX_AGENT_REQUESTS",
    "MAX_LINE_BYTES",
    "MAX_STDERR",
    "AcpConnection",
    "AcpError",
    "AcpMethodNotFound",
]

MAX_STDERR = 64_000
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_AGENT_REQUESTS = 8


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
        # Agent-to-client requests are answered on threads; bound how many run at once.
        self._request_slots = threading.BoundedSemaphore(MAX_AGENT_REQUESTS)
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
            while True:
                line = self.process.stdout.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    # Drain the rest of an oversized line and drop it.
                    while line and not line.endswith(b"\n"):
                        line = self.process.stdout.readline(MAX_LINE_BYTES)
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # Agents sometimes print banners; JSON-RPC lines are what count.
                if not isinstance(message, dict):
                    continue
                params = message.get("params")
                params = params if isinstance(params, dict) else {}
                try:
                    if "method" in message and "id" in message:
                        if not self._request_slots.acquire(blocking=False):
                            self._reject_busy(message)
                            continue
                        threading.Thread(target=self._answer, args=(message,), daemon=True).start()
                    elif "method" in message:
                        self._on_notification(str(message["method"]), params)
                    elif isinstance(message.get("id"), int):
                        with self._arrived:
                            self._pending[message["id"]] = message
                            self._arrived.notify_all()
                except Exception:  # A malformed message from the agent must not stop the reader.
                    continue
        finally:
            self.closed.set()
            with self._arrived:
                self._arrived.notify_all()

    def _reject_busy(self, message: dict[str, Any]) -> None:
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32000, "message": "too many concurrent requests"},
                }
            )
        except AcpError:
            pass

    def _answer(self, message: dict[str, Any]) -> None:
        try:
            self._answer_request(message)
        finally:
            self._request_slots.release()

    def _answer_request(self, message: dict[str, Any]) -> None:
        try:
            params = message.get("params")
            result = self._on_request(
                str(message["method"]), params if isinstance(params, dict) else {}
            )
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
