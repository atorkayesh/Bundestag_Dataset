"""
utils.py
========
Shared utilities for the Bundestag pipeline scripts.
"""

import sys
from pathlib import Path


def append_log(log_path: Path, entry: str) -> None:
    """
    Append one line to a pipeline log file.

    Uses fcntl.flock on Linux/macOS for NFS-safe atomic writes when multiple
    SLURM array tasks write to the same shared log file concurrently.
    Falls back to a plain append on Windows (local dev — no concurrent writes).
    """
    with open(log_path, "a", encoding="utf-8") as lg:
        if sys.platform != "win32":
            import fcntl
            fcntl.flock(lg, fcntl.LOCK_EX)
        lg.write(entry + "\n")
        if sys.platform != "win32":
            import fcntl
            fcntl.flock(lg, fcntl.LOCK_UN)
