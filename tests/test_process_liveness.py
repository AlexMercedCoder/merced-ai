"""Recovery inspection must never terminate its owner, particularly on Windows."""

import os
import subprocess
import sys

from merced_ai.process_liveness import process_alive


def test_liveness_preserves_running_child_and_detects_exit():
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("COV_CORE_") and k != "COVERAGE_PROCESS_START"
    }
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env) as child:
        try:
            assert process_alive(child.pid)
            assert child.poll() is None
        finally:
            child.terminate()
            child.wait(timeout=10)
        assert not process_alive(child.pid)
    assert not process_alive(None)
    assert not process_alive(-1)
    assert not process_alive(0)
