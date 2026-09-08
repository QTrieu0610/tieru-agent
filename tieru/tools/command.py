"""Workspace-confined, policy-governed, bounded foreground command execution."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from tieru.memory.personal import redact_secrets
from tieru.runtime.subprocesses import executable_argv
from tieru.tools.registry import Tool

DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300
DEFAULT_OUTPUT_BYTES = 16_384
MAX_ARGV_ITEMS = 128
MAX_ARG_BYTES = 4096
MAX_ARGV_BYTES = 16_384

DENIED_SHELLS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "fish",
        "ksh",
        "dash",
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
    }
)
DENIED_PRIVILEGE_WRAPPERS = frozenset(
    {"sudo", "sudo.exe", "su", "doas", "runas", "runas.exe"}
)
DENIED_REMOTE_OR_CONTAINER = frozenset(
    {"ssh", "ssh.exe", "scp", "scp.exe", "sftp", "sftp.exe", "docker", "podman"}
)
_SAFE_ENV_KEYS = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "SYSTEMROOT",
    "WINDIR",
    "PATHEXT",
)


class CommandPolicyError(ValueError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True)
class CommandPolicy:
    workspace_root: Path
    default_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_timeout_seconds: int = MAX_TIMEOUT_SECONDS
    max_stdout_bytes: int = DEFAULT_OUTPUT_BYTES
    max_stderr_bytes: int = DEFAULT_OUTPUT_BYTES

    def __post_init__(self) -> None:
        root = self.workspace_root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError("command workspace root must be an existing directory")
        if not 1 <= self.default_timeout_seconds <= self.max_timeout_seconds:
            raise ValueError("default command timeout is outside the allowed bound")
        if self.max_timeout_seconds > MAX_TIMEOUT_SECONDS:
            raise ValueError(f"command timeout maximum cannot exceed {MAX_TIMEOUT_SECONDS}")
        if self.max_stdout_bytes < 128 or self.max_stderr_bytes < 128:
            raise ValueError("command output limits must be at least 128 bytes")
        object.__setattr__(self, "workspace_root", root)


@dataclass(frozen=True)
class CommandRequest:
    argv: tuple[str, ...]
    cwd: Path
    cwd_alias: str
    timeout_seconds: int


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    error_code: str = ""
    error_message: str = ""

    def as_json(self) -> str:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": redact_secrets(self.stdout),
            "stderr": redact_secrets(self.stderr),
            "duration_ms": max(0, self.duration_ms),
            "timed_out": self.timed_out,
            "truncated": self.truncated,
        }
        if self.error_code:
            payload["error"] = {
                "code": self.error_code,
                "message": redact_secrets(self.error_message)[:1024],
                "retryable": False,
            }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.total = 0

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def pump(self, stream: BinaryIO | None) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                self.total += len(chunk)
                remaining = self.limit - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def text(self) -> str:
        data = bytes(self.data)
        if self.truncated:
            marker = b"\n[TRUNCATED]"
            data = data[: max(0, self.limit - len(marker))] + marker
        value = redact_secrets(data.decode("utf-8", errors="replace"))
        encoded = value.encode("utf-8")
        if len(encoded) <= self.limit:
            return value
        marker = b"\n[TRUNCATED]"
        return (
            encoded[: max(0, self.limit - len(marker))].decode("utf-8", errors="ignore")
            + marker.decode()
        )


def build_child_env(source: os._Environ[str] | dict[str, str] | None = None) -> dict[str, str]:
    """Build a child-only minimum environment without provider/gateway secrets."""
    parent = source if source is not None else os.environ
    environment: dict[str, str] = {}
    for key in _SAFE_ENV_KEYS:
        value = parent.get(key)
        if value:
            environment[key] = str(value)
    environment.setdefault("PATH", os.defpath)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["NO_COLOR"] = "1"
    return environment


class CommandRunner:
    def __init__(self, policy: CommandPolicy) -> None:
        self.policy = policy

    def _cwd(self, value: object) -> tuple[Path, str]:
        raw = str(value or ".")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.policy.workspace_root / candidate
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(self.policy.workspace_root)
        except ValueError as exc:
            raise CommandPolicyError(
                "cwd_outside_workspace", "Working directory is outside the allowed workspace."
            ) from exc
        if not resolved.exists():
            raise CommandPolicyError("cwd_not_found", "Working directory does not exist.")
        if not resolved.is_dir():
            raise CommandPolicyError("cwd_not_directory", "Working directory is not a directory.")
        alias = "." if not relative.parts else relative.as_posix()
        return resolved, alias

    @staticmethod
    def _argv(value: object) -> list[str]:
        if not isinstance(value, list) or not value:
            raise CommandPolicyError("command_invalid", "argv must be a non-empty list of strings.")
        if len(value) > MAX_ARGV_ITEMS:
            raise CommandPolicyError("command_invalid", "argv contains too many items.")
        argv: list[str] = []
        total = 0
        for item in value:
            if not isinstance(item, str) or not item or "\x00" in item:
                raise CommandPolicyError(
                    "command_invalid", "Every argv item must be a non-empty string without NUL."
                )
            size = len(item.encode("utf-8"))
            if size > MAX_ARG_BYTES:
                raise CommandPolicyError("command_invalid", "An argv item exceeds its byte limit.")
            total += size
            argv.append(item)
        if total > MAX_ARGV_BYTES:
            raise CommandPolicyError("command_invalid", "argv exceeds its total byte limit.")
        return argv

    @staticmethod
    def _denied_executable(argv0: str) -> None:
        name = Path(argv0).name.casefold()
        if name in DENIED_SHELLS:
            raise CommandPolicyError(
                "executable_denied", "Shell interpreters are unavailable in run_command."
            )
        if name in DENIED_PRIVILEGE_WRAPPERS:
            raise CommandPolicyError(
                "executable_denied", "Privilege escalation wrappers are unavailable."
            )
        if name in DENIED_REMOTE_OR_CONTAINER:
            raise CommandPolicyError(
                "executable_denied", "Remote and container execution wrappers are unavailable."
            )

    @staticmethod
    def _deny_package_install(argv: list[str]) -> None:
        lowered = [item.casefold() for item in argv]
        executable = Path(lowered[0]).name
        direct_managers = {
            "pip", "pip.exe", "pip3", "pip3.exe", "npm", "npm.cmd", "pnpm",
            "yarn", "uv", "uv.exe", "apt", "apt-get", "winget", "choco", "brew",
        }
        installs = {"install", "add", "sync", "ensurepip"}
        direct_install = executable in direct_managers and bool(installs & set(lowered[1:]))
        python_install = (
            len(lowered) >= 3
            and lowered[1] == "-m"
            and lowered[2] in {"pip", "ensurepip"}
            and bool(installs & set(lowered[2:]))
        )
        if direct_install or python_install:
            raise CommandPolicyError(
                "executable_denied", "Package installation is unavailable in run_command."
            )

    def _external_executable_allowed(self, path: Path, environment: dict[str, str]) -> bool:
        if path == Path(sys.executable).resolve():
            return True
        found = shutil.which(path.name, path=environment.get("PATH"))
        if not found:
            return False
        try:
            return Path(found).resolve() == path
        except OSError:
            return False

    def _resolve_executable(
        self, argv: list[str], cwd: Path, environment: dict[str, str]
    ) -> list[str]:
        self._denied_executable(argv[0])
        self._deny_package_install(argv)
        token = argv[0]
        token_path = Path(token).expanduser()
        path_like = token_path.is_absolute() or any(
            separator and separator in token for separator in (os.sep, os.altsep)
        )
        if path_like:
            candidate = token_path if token_path.is_absolute() else cwd / token_path
            resolved = candidate.resolve()
            try:
                resolved.relative_to(self.policy.workspace_root)
                in_workspace = True
            except ValueError:
                in_workspace = False
            if not in_workspace and not self._external_executable_allowed(resolved, environment):
                raise CommandPolicyError(
                    "executable_denied",
                    "Executable paths must be workspace-local or resolve to an installed PATH executable.",
                )
        else:
            found = shutil.which(token, path=environment.get("PATH"))
            if not found:
                raise CommandPolicyError("command_not_found", "Executable was not found on PATH.")
            resolved = Path(found).resolve()
        if not resolved.exists() or not resolved.is_file():
            raise CommandPolicyError("command_not_found", "Executable does not exist or is not a file.")
        self._denied_executable(resolved.name)
        return [*executable_argv(resolved), *argv[1:]]

    def prepare(self, arguments: dict[str, Any]) -> dict[str, Any]:
        argv = self._argv(arguments.get("argv"))
        cwd, _alias = self._cwd(arguments.get("cwd", "."))
        try:
            timeout = int(arguments.get("timeout_seconds") or self.policy.default_timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise CommandPolicyError("command_invalid", "timeout_seconds must be an integer.") from exc
        if not 1 <= timeout <= self.policy.max_timeout_seconds:
            raise CommandPolicyError(
                "command_invalid",
                f"timeout_seconds must be between 1 and {self.policy.max_timeout_seconds}.",
            )
        environment = build_child_env()
        resolved_argv = self._resolve_executable(argv, cwd, environment)
        return {"argv": resolved_argv, "cwd": str(cwd), "timeout_seconds": timeout}

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                proc.terminate()
        else:
            proc.terminate()
        try:
            proc.wait(timeout=0.75)
            return
        except subprocess.TimeoutExpired:
            pass
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                proc.kill()
        else:
            proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return

    def run(
        self,
        argv: list[str],
        cwd: str,
        timeout_seconds: int,
        _notify=None,
    ) -> str:
        del _notify
        started = time.perf_counter()
        environment = build_child_env()
        stdout = _BoundedCapture(self.policy.max_stdout_bytes)
        stderr = _BoundedCapture(self.policy.max_stderr_bytes)
        kwargs: dict[str, Any] = {
            "args": list(argv),
            "cwd": str(cwd),
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            proc = subprocess.Popen(**kwargs)
        except FileNotFoundError:
            return CommandResult(
                False,
                None,
                "",
                "",
                int((time.perf_counter() - started) * 1000),
                False,
                False,
                "command_not_found",
                "Executable was not found.",
            ).as_json()
        except OSError as exc:
            return CommandResult(
                False,
                None,
                "",
                "",
                int((time.perf_counter() - started) * 1000),
                False,
                False,
                "command_launch_error",
                f"Process could not be launched: {type(exc).__name__}.",
            ).as_json()

        out_thread = threading.Thread(target=stdout.pump, args=(proc.stdout,), daemon=True)
        err_thread = threading.Thread(target=stderr.pump, args=(proc.stderr,), daemon=True)
        out_thread.start()
        err_thread.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(proc)
        out_thread.join(timeout=2)
        err_thread.join(timeout=2)
        duration_ms = int((time.perf_counter() - started) * 1000)
        stdout_text = stdout.text()
        stderr_text = stderr.text()
        truncated = stdout.truncated or stderr.truncated
        if timed_out:
            return CommandResult(
                False,
                proc.returncode,
                stdout_text,
                stderr_text,
                duration_ms,
                True,
                truncated,
                "command_timeout",
                f"Command exceeded the {timeout_seconds}-second timeout and was stopped.",
            ).as_json()
        if proc.returncode != 0:
            return CommandResult(
                False,
                proc.returncode,
                stdout_text,
                stderr_text,
                duration_ms,
                False,
                truncated,
                "command_failed",
                f"Command exited with status {proc.returncode}.",
            ).as_json()
        return CommandResult(
            True,
            proc.returncode,
            stdout_text,
            stderr_text,
            duration_ms,
            False,
            truncated,
        ).as_json()


def make_tool(runner: CommandRunner) -> Tool:
    return Tool(
        name="run_command",
        description=(
            "Run one argv-only foreground process in the configured workspace. "
            "Shell interpreters, privilege wrappers, background execution, and inherited "
            "credentials are unavailable."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1, "maxLength": MAX_ARG_BYTES},
                },
                "cwd": {"type": "string", "maxLength": 4096},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": runner.policy.max_timeout_seconds,
                },
            },
            "required": ["argv"],
            "additionalProperties": False,
        },
        fn=runner.run,
        wants_notify=True,
        risk="high",
        read_only=False,
        capabilities=("process.execute",),
        default_policy="confirm",
        operation="run",
        target_arg="cwd",
        fixed_target="configured workspace foreground process",
        scope="workspace",
        resource_type="process",
        reversible=False,
        idempotency_scope="run",
        prepare_args=runner.prepare,
    )
