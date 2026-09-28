"""Answer harness approval requests (AAIS) from the terminal.

MagAgent and Loro ask before running a command or editing a file; so do ACP agents. In the web UI
those requests appear in the approval dialog. On the command line:

- **Interactive** (stdin and stderr are terminals): the request is shown on stderr with the exact
  action, its risk, and the harness that asked, and the user picks a choice with one key. Enter,
  Esc, and Ctrl-C all refuse. The decision goes through the same ``AAISPresenter`` as the web UI,
  so the receipt is the same envelope, persisted the same way, except that it records
  ``authenticated_by: merced-ai-terminal``.
- **Non-interactive** (piped, CI, ``--json`` into a file): nothing can ask, so the request is
  denied, exactly as before, and one line on stderr says so and how to approve instead.

Prompts from bots running in parallel (group commands) are shown one at a time.
"""

from __future__ import annotations

import json
import os
import select
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

from aais import ConflictError, create_decision
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from merced_ai.aais_presenter import AAISPresenter
from merced_ai.application import ApprovalHandler

TERMINAL_AUTHENTICATION = "merced-ai-terminal"
CHOICE_LABELS = {
    ("approve", "once"): "Allow once",
    ("approve", "session"): "Allow for this session",
    ("approve", "persistent"): "Always allow",
    ("deny", "once"): "Deny",
    ("deny", "session"): "Deny for this session",
    ("deny", "persistent"): "Always deny",
}
MAX_ARGUMENT_LINES = 20
_PROMPT_LOCK = threading.Lock()  # One prompt on the terminal at a time.


def choice_label(decision: str, scope: str) -> str:
    """Merced AI's own wording for a choice; the harness's label is never shown."""
    return CHOICE_LABELS.get((decision, scope), f"{decision.title()} ({scope})")


def _clean(text: Any, limit: int = 300) -> str:
    """Harness text for the terminal: no control characters (so no escape sequences), bounded."""
    cleaned = "".join(ch if ch.isprintable() or ch == "\n" else " " for ch in str(text))
    cleaned = cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"
    return escape(cleaned)


def terminal_is_interactive(stdin: IO[str] | None = None, stderr: IO[str] | None = None) -> bool:
    stdin = stdin or sys.stdin
    stderr = stderr or sys.stderr
    try:
        return stdin.isatty() and stderr.isatty()
    except (AttributeError, ValueError):
        return False


def approval_handler(
    workspace: Path, *, bot: str | None = None, interactive: bool | None = None
) -> ApprovalHandler:
    """The handler the CLI passes to harnesses that relay approval requests."""
    if interactive is None:
        interactive = terminal_is_interactive()
    if interactive:
        return TerminalApprover(workspace, bot=bot)

    def deny(
        envelope: dict[str, Any], cancellation: threading.Event | None = None
    ) -> dict[str, Any]:
        return deny_without_terminal(envelope, cancellation, bot=bot)

    return deny


def _asker(request: dict[str, Any], bot: str | None) -> str:
    harness = _clean(request["origin"].get("harness", "the harness"), 40)
    return f"{_clean(bot, 64)} ({harness})" if bot else harness


def deny_without_terminal(
    envelope: dict[str, Any],
    _cancellation: threading.Event | None = None,
    *,
    bot: str | None = None,
) -> dict[str, Any]:
    request = envelope["request"]
    summary = _clean(request["action"].get("summary") or request["action"].get("name"), 120)
    Console(stderr=True, highlight=False, soft_wrap=True).print(
        f"[yellow]Denied[/yellow] {_asker(request, bot)} asked to {summary}: no terminal to ask "
        "on. Run the command in an interactive terminal to approve it there, or have the "
        "conversation in the web UI (merced-ai ui)."
    )
    return create_decision(
        envelope,
        decision="deny",
        scope="once",
        actor={"id": "merced-ai.no-presenter", "type": "policy", "authenticated_by": "adapter"},
        sequence=int(envelope.get("sequence", 1)),
        stream="merced-ai.presenter",
    )


class TerminalApprover:
    """Ask on the terminal and record the decision through the shared AAIS presenter."""

    def __init__(
        self,
        workspace: Path,
        *,
        bot: str | None = None,
        input_fd: int | None = None,
        console: Console | None = None,
    ) -> None:
        self.bot = bot
        self.presenter = AAISPresenter(workspace)
        self.input_fd = sys.stdin.fileno() if input_fd is None else input_fd
        self.console = console or Console(stderr=True, highlight=False)

    def record_event(self, envelope: dict[str, Any]) -> None:
        """Keep the harness's resolution receipts, so `recovery` shows them like the web UI."""
        self.presenter.record_event(envelope)

    def __call__(
        self, envelope: dict[str, Any], cancellation: threading.Event | None = None
    ) -> dict[str, Any]:
        request_id = str(envelope["request"]["id"])
        settled = threading.Event()

        def ask() -> None:
            with _PROMPT_LOCK:
                if settled.is_set():
                    return
                decision, scope = self._prompt(envelope, cancellation, settled)
            if decision is None:
                return  # Decided elsewhere (web UI) or cancelled; nothing to record.
            for _ in range(200):  # present() registers the request as pending first.
                try:
                    self.presenter.decide(
                        request_id,
                        decision,
                        scope,
                        actor_id="local-user",
                        authenticated_by=TERMINAL_AUTHENTICATION,
                    )
                    return
                except ConflictError:
                    return  # Someone decided first, or it expired.
                except ValueError:
                    time.sleep(0.05)

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        try:
            return self.presenter.present(envelope, cancellation)
        finally:
            settled.set()

    # ---- rendering and key handling ---------------------------------------------------------

    def _choices(self, request: dict[str, Any]) -> tuple[list[tuple[str, str]], tuple[str, str]]:
        choices = [
            (str(item["decision"]), str(item["scope"]))
            for item in request.get("choices") or []
            if isinstance(item, dict)
        ]
        refusal = next((item for item in choices if item == ("deny", "once")), None)
        refusal = refusal or next((item for item in choices if item[0] == "deny"), None)
        return choices, refusal or ("cancel", "once")

    def _render(self, request: dict[str, Any], choices: list[tuple[str, str]]) -> None:
        action = request.get("action") or {}
        risk = request.get("risk") or {}
        lines = [f"[bold]{_clean(action.get('summary') or action.get('name'), 200)}[/bold]"]
        reasons = " · ".join(_clean(reason, 120) for reason in risk.get("reasons") or [])
        level = _clean(str(risk.get("level", "unknown")).upper(), 20)
        lines.append(f"Risk: [bold]{level}[/bold]" + (f" · {reasons}" if reasons else ""))
        lines.append(f"Action: {_clean(action.get('name', ''), 120)}")
        for key in ("working_directory", "resource"):
            if action.get(key):
                lines.append(f"{key.replace('_', ' ').capitalize()}: {_clean(action[key], 300)}")
        if action.get("arguments"):
            shown = json.dumps(action["arguments"], indent=2, ensure_ascii=False).splitlines()
            if len(shown) > MAX_ARGUMENT_LINES:
                shown = shown[:MAX_ARGUMENT_LINES] + ["…"]
            lines.append("Arguments:\n" + _clean("\n".join(shown), 4000))
        digest = str(request.get("action_digest", ""))
        if digest:
            lines.append(f"[dim]Digest: {_clean(digest, 80)}[/dim]")
        _, refusal = self._choices(request)
        keys = "   ".join(
            f"[bold]{index}[/bold] {choice_label(*choice)}"
            + (" [dim](default)[/dim]" if choice == refusal else "")
            for index, choice in enumerate(choices, start=1)
        )
        lines.append("")
        lines.append(keys)
        lines.append("[dim]Press a number. Enter, Esc, or Ctrl-C denies.[/dim]")
        self.console.print(
            Panel(
                "\n".join(lines),
                title=f"Approval requested by {_asker(request, self.bot)}",
                title_align="left",
                border_style="yellow",
            )
        )

    def _prompt(
        self,
        envelope: dict[str, Any],
        cancellation: threading.Event | None,
        settled: threading.Event,
    ) -> tuple[str | None, str]:
        request = envelope["request"]
        choices, refusal = self._choices(request)
        self._render(request, choices)
        key = self._read_key(len(choices), cancellation, settled)
        if key is None:
            self.console.print("[dim]Decided elsewhere; the terminal prompt was withdrawn.[/dim]")
            return None, "once"
        decision, scope = choices[key] if key >= 0 else refusal
        verb = "[green]Approved[/green]" if decision == "approve" else "[yellow]Denied[/yellow]"
        self.console.print(f"{verb}: {choice_label(decision, scope)}.")
        return decision, scope

    def _read_key(
        self, count: int, cancellation: threading.Event | None, settled: threading.Event
    ) -> int | None:
        """Index of the chosen option, -1 for the refusal, or None if decided elsewhere."""
        if os.name == "nt":  # pragma: no cover - Windows console
            return _read_key_windows(count, cancellation, settled)
        with _raw_terminal(self.input_fd):
            while True:
                if settled.is_set() or (cancellation is not None and cancellation.is_set()):
                    return None
                ready, _, _ = select.select([self.input_fd], [], [], 0.1)
                if not ready:
                    continue
                data = os.read(self.input_fd, 1)
                if not data or data in (b"\r", b"\n", b"\x03", b"\x04"):
                    return -1  # Enter, Ctrl-C, Ctrl-D, or end of input: deny.
                if data == b"\x1b":
                    # A lone Esc denies; an arrow key (Esc [ A) is ignored.
                    more, _, _ = select.select([self.input_fd], [], [], 0.05)
                    if not more:
                        return -1
                    os.read(self.input_fd, 8)
                    continue
                if data.isdigit() and 1 <= int(data) <= count:
                    return int(data) - 1


def _read_key_windows(  # pragma: no cover - Windows console
    count: int, cancellation: threading.Event | None, settled: threading.Event
) -> int | None:
    import msvcrt

    while True:
        if settled.is_set() or (cancellation is not None and cancellation.is_set()):
            return None
        if not msvcrt.kbhit():  # type: ignore[attr-defined]
            time.sleep(0.1)
            continue
        key = msvcrt.getwch()  # type: ignore[attr-defined]
        if key in ("\r", "\n", "\x03", "\x1b"):
            return -1
        if key in ("\x00", "\xe0"):
            msvcrt.getwch()  # type: ignore[attr-defined]  # arrow and function keys
            continue
        if key.isdigit() and 1 <= int(key) <= count:
            return int(key) - 1


class _raw_terminal:  # noqa: N801 - used as a context manager
    """Read single keys without echo, with Ctrl-C delivered as a byte instead of SIGINT."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.saved: Any = None

    def __enter__(self) -> None:
        try:
            import termios
        except ImportError:  # pragma: no cover - Windows
            return
        try:
            self.saved = termios.tcgetattr(self.fd)
        except termios.error:
            return
        mode = termios.tcgetattr(self.fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, mode)

    def __exit__(self, *_exc: object) -> None:
        if self.saved is not None:
            import termios

            termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
