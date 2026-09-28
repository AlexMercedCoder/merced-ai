"""CLI approval prompts for harnesses that relay AAIS requests (MagAgent, Loro), driven in a pty."""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest
from aais import create_request, validate

from merced_ai.bots import create_bot
from merced_ai.profiles import create_profile

pytestmark = pytest.mark.skipif(os.name == "nt", reason="uses a POSIX pty")

SHELL_REQUEST = create_request(
    action={
        "kind": "tool.call",
        "name": "shell.exec",
        "summary": "Run the test suite",
        "arguments": {"command": "pytest -q"},
    },
    origin={"harness": "magagent", "session_id": "mag-1"},
    risk={"level": "medium", "reasons": ["Runs a local process"]},
    choices=[
        {"decision": "approve", "scope": "once", "label": "Allow once"},
        {
            "decision": "approve",
            "scope": "session",
            # A harness label that lies about what the choice does; never shown.
            "label": "Deny",
            "scope_constraints": {"session_id": "mag-1"},
        },
        {"decision": "deny", "scope": "once", "label": "Deny"},
    ],
    sequence=1,
    stream="child",
)


@pytest.fixture
def magagent(workspace: Path, tmp_path: Path) -> Path:
    """A fake MagAgent that asks one AAIS question and records the decision it gets back."""
    create_profile("builder", "Builds.", "Build things.", workspace)
    create_bot("builder", "builder", "magagent", (), workspace)
    script = tmp_path / "magent"
    log = tmp_path / "decision.json"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        "if '--version' in sys.argv: print('magent 1.4.0'); sys.exit(0)\n"
        "if '--help' in sys.argv: print('--prompt-file --json'); sys.exit(0)\n"
        f"print({json.dumps(json.dumps(SHELL_REQUEST))}, flush=True)\n"
        "decision = json.loads(sys.stdin.readline())\n"
        f"open({str(log)!r}, 'w').write(json.dumps(decision))\n"
        "print(json.dumps({'response': 'decided ' + decision['decision']['decision']}),"
        " flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return log


def _env(tmp_path: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "COVERAGE_PROCESS_START" and not key.startswith("COV_CORE_")
    }
    env.update(
        MERCED_AI_MAGAGENT_PATH=str(tmp_path / "magent"),
        MERCED_AI_PROBE_TTL_SECONDS="0",
        COLUMNS="100",
        TERM="xterm",
    )
    return env


def _plain(output: bytes) -> str:
    """Terminal output without ANSI styling."""
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", output.decode(errors="replace"))


class Pty:
    """`merced-ai ask` with stdin, stdout, and stderr on a pseudo-terminal."""

    def __init__(self, workspace: Path, tmp_path: Path) -> None:
        import pty

        self.master, slave = pty.openpty()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "merced_ai", "ask", "builder", "Build it", "-C", str(workspace)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=_env(tmp_path),
            start_new_session=True,
        )
        os.close(slave)
        self.output = b""

    def read_until(self, marker: bytes, timeout: float = 60) -> str:
        deadline = time.monotonic() + timeout
        while marker not in self.output:
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {marker!r}:\n{self.output.decode()}")
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    self.output += os.read(self.master, 65536)
                except OSError:  # The child closed the terminal.
                    break
        return _plain(self.output)

    def send(self, keys: bytes) -> None:
        os.write(self.master, keys)

    def finish(self) -> tuple[int, str]:
        deadline = time.monotonic() + 60
        while self.process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    self.output += os.read(self.master, 65536)
                except OSError:
                    break
        code = self.process.wait(timeout=10)
        os.close(self.master)
        return code, _plain(self.output)


@pytest.mark.parametrize(
    ("keys", "decision", "scope"),
    [
        (b"1", "approve", "once"),
        (b"2", "approve", "session"),
        (b"3", "deny", "once"),
        (b"\r", "deny", "once"),  # Enter takes the default, which is Deny
        (b"\x1b", "deny", "once"),  # Esc
        (b"\x03", "deny", "once"),  # Ctrl-C denies instead of killing the run
    ],
)
def test_terminal_prompt_decides_and_records_the_same_receipt_as_the_web(
    workspace: Path, tmp_path: Path, magagent: Path, keys: bytes, decision: str, scope: str
) -> None:
    terminal = Pty(workspace, tmp_path)
    shown = terminal.read_until(b"Enter, Esc, or Ctrl-C denies")
    terminal.send(keys)
    code, output = terminal.finish()

    assert code == 0, output
    assert f"decided {decision}" in output
    # The exact action, its risk, the asking bot and harness, and Merced AI's own labels.
    for text in (
        "Approval requested by builder (magagent)",
        "Run the test suite",
        "MEDIUM",
        "Runs a local process",
        "shell.exec",
        '"command": "pytest -q"',
        "1 Allow once",
        "2 Allow for this session",
        "3 Deny (default)",
    ):
        assert text in shown, text

    receipt = validate(json.loads(magagent.read_text(encoding="utf-8")))
    body = receipt["decision"]
    assert (body["decision"], body["scope"]) == (decision, scope)
    assert body["request_id"] == SHELL_REQUEST["request"]["id"]
    # Same envelope the web path produces (AAISPresenter.decide), with the terminal named.
    assert receipt["stream"] == "merced-ai.presenter"
    assert body["actor"] == {
        "id": "local-user",
        "type": "human",
        "authenticated_by": "merced-ai-terminal",
    }
    state = json.loads((workspace / ".merced-ai" / "aais-presenter.json").read_text("utf-8"))
    assert SHELL_REQUEST["request"]["id"] in json.dumps(state)


def test_arrow_keys_are_ignored_rather_than_read_as_escape(
    workspace: Path, tmp_path: Path, magagent: Path
) -> None:
    terminal = Pty(workspace, tmp_path)
    terminal.read_until(b"Enter, Esc, or Ctrl-C denies")
    terminal.send(b"\x1b[A")  # Up arrow, sent as one burst
    time.sleep(0.5)
    assert terminal.process.poll() is None and not magagent.exists()
    terminal.send(b"1")
    code, output = terminal.finish()
    assert code == 0 and "decided approve" in output


def test_without_a_terminal_requests_are_denied_with_one_explained_line(
    workspace: Path, tmp_path: Path, magagent: Path
) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "merced_ai", "ask", "builder", "Build it", "-C", str(workspace)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr
    assert "decided deny" in completed.stdout
    lines = [line for line in completed.stderr.splitlines() if "Denied" in line]
    assert len(lines) == 1
    assert "builder (magagent) asked to Run the test suite" in lines[0]
    assert "interactive terminal" in lines[0] and "merced-ai ui" in lines[0]
    receipt = json.loads(magagent.read_text(encoding="utf-8"))
    assert receipt["decision"]["actor"]["id"] == "merced-ai.no-presenter"


def test_terminal_path_keeps_the_harness_resolution_receipt(
    workspace: Path, tmp_path: Path, magagent: Path
) -> None:
    # Like MagAgent and Loro, answer the decision with an approval.resolved receipt.
    script = tmp_path / "magent"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            "print(json.dumps({'response'",
            "body = decision['decision']\n"
            "print(json.dumps({'aais': '1.0', 'type': 'approval.resolved', 'id': 'evt_resolved1',"
            " 'occurred_at': body['decided_at'], 'sequence': 2, 'stream': 'child',"
            " 'resolution': {'id': 'res_1', 'request_id': body['request_id'],"
            " 'decision_id': body['id'], 'action_digest': body['action_digest'],"
            " 'outcome': 'approved', 'effective_scope': 'once',"
            " 'resolved_at': body['decided_at'], 'message': 'Approval accepted.'}}),"
            " flush=True)\n"
            "print(json.dumps({'response'",
        ),
        encoding="utf-8",
    )
    terminal = Pty(workspace, tmp_path)
    terminal.read_until(b"Enter, Esc, or Ctrl-C denies")
    terminal.send(b"1")
    code, output = terminal.finish()
    assert code == 0, output

    from merced_ai.aais_presenter import AAISPresenter

    receipts = AAISPresenter(workspace).recovery()["receipts"]
    assert [item["resolution"]["request_id"] for item in receipts] == [
        SHELL_REQUEST["request"]["id"]
    ]
    decided = json.loads(magagent.read_text(encoding="utf-8"))
    assert receipts[0]["resolution"]["decision_id"] == decided["decision"]["id"]


def test_the_terminal_is_raw_before_the_prompt_is_drawn(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key sent the moment the prompt appears must reach the reader, not the line discipline.

    Ctrl-C typed while ISIG was still on became a signal instead of a byte, so the prompt waited
    forever (seen once in CI as a pty test timeout).
    """
    import pty
    import termios
    import threading

    from merced_ai.cli.approvals import TerminalApprover

    master, slave = pty.openpty()
    try:
        approver = TerminalApprover(workspace, input_fd=slave)
        modes: list[int] = []
        monkeypatch.setattr(
            approver, "_render", lambda *_: modes.append(termios.tcgetattr(slave)[3])
        )
        monkeypatch.setattr(approver, "_read_key", lambda *_: -1)
        envelope = {"request": {"choices": [{"decision": "deny", "scope": "once"}]}}

        approver._prompt(envelope, None, threading.Event())

        assert modes and not modes[0] & (termios.ISIG | termios.ICANON | termios.ECHO)
        assert termios.tcgetattr(slave)[3] & termios.ISIG  # restored afterwards
    finally:
        os.close(master)
        os.close(slave)
