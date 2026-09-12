"""Bounded subprocess capture with process-tree cancellation and an independent control channel."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_FRAME_BYTES = 1_000_000


class ChildProcessError(RuntimeError):
    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class ChildResult:
    stdout: str
    stderr: str
    returncode: int
    truncated: bool


class Capture:
    def __init__(self, limit: int):
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        available = max(0, self.limit - len(self.data))
        self.data.extend(chunk[:available])
        self.truncated |= len(chunk) > available

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


def stop_process(process: subprocess.Popen) -> None:
    if os.name == "nt":  # pragma: no cover - Windows CI
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    else:
        # Only signal a group that this child leads; never signal the broker's own group.
        with suppress(ProcessLookupError):
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    if os.name != "nt":
        # The group can outlive its leader; descendants may ignore SIGTERM.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def run_child(
    command: list[str],
    *,
    workspace: Path,
    env: dict[str, str],
    timeout: float,
    cancellation: threading.Event | None,
    limit: int,
    stdin_payload: str | None = None,
    control: Callable[[dict[str, Any], threading.Event], dict[str, Any] | None] | None = None,
) -> ChildResult:
    # Every child is an external harness boundary, including capability probes.
    # Do not let test instrumentation alter its startup or coverage database.
    env = {
        key: value
        for key, value in env.items()
        if key != "COVERAGE_PROCESS_START" and not key.startswith("COV_CORE_")
    }
    stopped = threading.Event()
    error: list[Exception] = []
    out, err = Capture(limit), Capture(limit)
    process = subprocess.Popen(
        command,
        cwd=workspace,
        env=env,
        shell=False,
        stdin=subprocess.PIPE if control or stdin_payload is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )

    def handle_line(line: bytes) -> None:
        try:
            value = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            value = None
        if (
            control
            and isinstance(value, dict)
            and str(value.get("type", "")).startswith("approval.")
        ):
            reply = control(value, stopped)
            if reply is not None and not stopped.is_set():
                assert process.stdin is not None
                process.stdin.write(json.dumps(reply).encode() + b"\n")
                process.stdin.flush()
        else:
            out.append(line)

    def read(pipe: Any, target: Capture, frames: bool) -> None:
        pending = bytearray()
        try:
            while chunk := os.read(pipe.fileno(), 16_384):
                if not frames:
                    target.append(chunk)
                    continue
                pending.extend(chunk)
                while (index := pending.find(b"\n")) >= 0:
                    if index > MAX_FRAME_BYTES:
                        raise ValueError("Harness control frame exceeds the maximum size")
                    line = bytes(pending[: index + 1])
                    del pending[: index + 1]
                    handle_line(line)
                if len(pending) > MAX_FRAME_BYTES:
                    raise ValueError("Harness control frame exceeds the maximum size")
            if pending:
                handle_line(bytes(pending))
        except Exception as exc:
            error.append(exc)
            stopped.set()

    readers = [
        threading.Thread(target=read, args=(process.stdout, out, bool(control)), daemon=True),
        threading.Thread(target=read, args=(process.stderr, err, False), daemon=True),
    ]
    for reader in readers:
        reader.start()

    def write_input() -> None:
        try:
            assert process.stdin is not None
            process.stdin.write((stdin_payload or "").encode())
            process.stdin.flush()
            if not control:
                process.stdin.close()
        except OSError as exc:
            error.append(exc)

    if stdin_payload is not None:
        writer = threading.Thread(target=write_input, daemon=True)
        readers.append(writer)
        writer.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None or any(reader.is_alive() for reader in readers):
            if cancellation is not None and cancellation.is_set():
                raise ChildProcessError("was cancelled", 130)
            if time.monotonic() >= deadline:
                raise ChildProcessError(f"timed out after {timeout}s", 5)
            if error:
                raise ChildProcessError(f"control channel failed: {error[0]}", 5)
            time.sleep(0.02)
        if error:
            raise ChildProcessError(f"control channel failed: {error[0]}", 5)
        return ChildResult(
            out.text(), err.text(), process.returncode, out.truncated or err.truncated
        )
    except KeyboardInterrupt as exc:
        raise ChildProcessError("was cancelled", 130) from exc
    finally:
        stopped.set()
        if process.poll() is None or any(reader.is_alive() for reader in readers):
            stop_process(process)
        for reader in readers:
            reader.join(timeout=2)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                with suppress(OSError):
                    pipe.close()
