"""Portable, explicit subprocess argument construction."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Any

# Tieru provider id -> pi's built-in provider id (see ``pi --list-models``).
# This is shared by the production delegation tool and the coding eval harness.
PI_PROVIDER = {
    "anthropic": "anthropic",
    "openai": "openai",
    "gemini": "google",
    "kimi": "moonshotai",
    "xai": "xai",
    "glm": "zai",
    "deepseek": "deepseek",
    "minimax": "minimax",
    "openrouter": "openrouter",
    "opencode_zen": "opencode_zen",
    "opencode_go": "opencode_go",
}


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


def credential_environment(variable: str, credential: str) -> dict[str, str]:
    """Build a child-only environment carrying one configured credential."""
    env = os.environ.copy()
    if variable and credential:
        env[variable] = credential
    return env


def safe_command_for_log(
    argv: list[Any] | tuple[Any, ...], sensitive_options: tuple[str, ...] = ("--api-key",)
) -> str:
    """Render argv for diagnostics while omitting sensitive option/value pairs."""
    safe: list[str] = []
    skip_value = False
    for item in argv:
        value = str(item)
        if skip_value:
            skip_value = False
            continue
        if value in sensitive_options:
            skip_value = True
            continue
        if any(value.startswith(f"{option}=") for option in sensitive_options):
            continue
        safe.append(value)
    return shlex.join(safe)


def verify_argv(command: str | list[Any] | tuple[Any, ...]) -> list[str]:
    """Parse a trusted eval command without a shell and expand Python markers."""
    argv = shlex.split(command, posix=True) if isinstance(command, str) else [str(x) for x in command]
    if not argv:
        raise ValueError("verify command must not be empty")
    if argv[0] in {"{python}", "python", "python3", "py"}:
        argv[0] = sys.executable
    return argv
