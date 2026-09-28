"""Atomic writes and bounded, cross-process local storage transactions."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

# How long a writer waits for another process's lock before reporting "Storage is busy".
LOCK_TIMEOUT_SECONDS = 30.0


@contextmanager
def file_lock(path: Path, timeout: float | None = None) -> Iterator[None]:
    if timeout is None:
        timeout = LOCK_TIMEOUT_SECONDS
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    if path.is_symlink() or lock_path.is_symlink():
        raise ValueError("Store and lock paths cannot be symbolic links")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    deadline = time.monotonic() + timeout
    locked = False
    try:
        if sys.platform == "win32":  # pragma: no cover - Windows CI
            import msvcrt

            # Windows lets a process lock a byte past end-of-file, so the lock file
            # stays empty. Writing a placeholder byte first raced with a process
            # that already held byte 0 locked and failed with PermissionError.
        else:
            import fcntl
        while not locked:
            try:
                if sys.platform == "win32":  # pragma: no cover
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise ValueError(
                        f"Storage is busy; retry the operation: {path.name}"
                    ) from error
                time.sleep(0.02)
        yield
    finally:
        if locked:
            if sys.platform == "win32":  # pragma: no cover
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        with suppress(OSError):
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
