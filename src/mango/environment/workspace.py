"""Keep concurrent commands from mutating the same generated workspace."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def workspace_lock(workspace: Path):
    workspace.mkdir(parents=True, exist_ok=True)
    with (workspace / ".harness.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                f"Workspace is already in use: {workspace}; "
                "wait for that command or select a different --workspace"
            ) from error
        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
