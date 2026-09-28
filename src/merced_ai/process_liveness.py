"""Non-destructive process liveness checks for recovery ownership.

Ownership records use :class:`aais.liveness.OwnerIdentity` (PID, process start time, and host),
which survives PID reuse. This PID-only helper remains for callers that have nothing more than a
PID; it never signals or disturbs the process (on Windows, ``os.kill(pid, 0)`` would terminate it).
"""

from aais.liveness import Liveness, pid_liveness


def process_alive(pid: int | None) -> bool:
    """True only when the PID is known to belong to a running process."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    return pid_liveness(pid) is Liveness.ALIVE
