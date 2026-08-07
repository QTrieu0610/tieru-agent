"""Portable, explicit subprocess argument construction."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any


def executable_argv(executable: str | Path) -> list[str]:
    """Return an argv prefix that also launches Python scripts on Windows."""
    value = str(executable)
    path = Path(value)
    is_python = path.suffix.lower() in {".py", ".pyw"}
    if path.is_file():
        try:
            with path.open("rb") as stream:
                head = stream.read(256).lower()
        except OSError:
            head = b""
        is_python = is_python or (head.startswith(b"#!") and b"python" in head.splitlines()[0])
    return [sys.executable, value] if is_python else [value]


def verify_argv(command: str | list[Any] | tuple[Any, ...]) -> list[str]:
    """Parse a trusted eval command without a shell and expand Python markers."""
    argv = shlex.split(command, posix=True) if isinstance(command, str) else [str(x) for x in command]
    if not argv:
        raise ValueError("verify command must not be empty")
    if argv[0] in {"{python}", "python", "python3", "py"}:
        argv[0] = sys.executable
    return argv
