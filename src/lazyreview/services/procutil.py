"""Subprocess helpers that do not leak pipe file descriptors."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, TextIO

logger = logging.getLogger(__name__)


def close_stream(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except Exception:
        pass


def close_popen_pipes(proc: subprocess.Popen[Any]) -> None:
    """Release stdin/stdout/stderr FDs held by a Popen after pumps finish."""
    close_stream(getattr(proc, "stdin", None))
    close_stream(getattr(proc, "stdout", None))
    close_stream(getattr(proc, "stderr", None))


def pump_stream_to_file(
    stream: TextIO | None,
    path: Path,
    *,
    log_label: str = "log pump",
) -> None:
    """Copy a subprocess pipe to a file, always closing the pipe afterward."""
    if stream is None:
        return
    try:
        with path.open("w", encoding="utf-8", buffering=1) as out:
            for line in stream:
                out.write(line)
                out.flush()
    except Exception:
        logger.exception("%s failed for %s", log_label, path)
    finally:
        close_stream(stream)
