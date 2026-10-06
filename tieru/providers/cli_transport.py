"""Local CLI transport for subscription-backed providers.

Prompts never pass through a shell or a remote proxy.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path


class CliError(RuntimeError):
    pass


def account_environment() -> dict[str, str]:
    env = os.environ.copy()
    # Inherited API credentials must never change subscription-backed requests.
    blocked = {
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY",
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_BASE_URL", "AGY_ADC_AUTH",
    }
    return {key: value for key, value in env.items() if key.upper() not in blocked}


def executable(binary: str, package: str | None = None) -> list[str]:
    path = shutil.which(binary)
    if not path:
        raise CliError(f"Không tìm thấy {binary}. Cài CLI trên máy chạy backend rồi làm mới kết nối.")
    # npm's Windows .cmd shims require a shell. Launch their Node entry directly.
    if Path(path).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        entry = Path(path).parent / "node_modules" / (package or "")
        node = shutil.which("node")
        if node and package and entry.is_file():
            return [node, str(entry)]
        raise CliError(f"Hãy cấu hình đường dẫn CLI native cho {binary}; ứng dụng không chạy prompt qua shell.")
    return [path]


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=10,
            check=False,
        )
    else:
        proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def strip_terminal(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "")


def run_cli(
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 30,
    input_text: str | None = None,
    terminal: bool = False,
) -> tuple[int, str, str]:
    if terminal and os.name == "nt":
        try:
            from winpty import PtyProcess

            if len(subprocess.list2cmdline(command)) > 30000:
                raise CliError("Prompt vượt giới hạn CLI Windows. Cập nhật Antigravity để dùng stdin.")
            proc = PtyProcess.spawn(command, cwd=str(cwd), env=account_environment(), dimensions=(50, 240))
            chunks: queue.Queue = queue.Queue()

            def read() -> None:
                try:
                    while True:
                        chunks.put(proc.read(8192))
                except (EOFError, OSError):
                    pass
                finally:
                    chunks.put(None)

            threading.Thread(target=read, daemon=True).start()
            output, size, deadline = [], 0, time.monotonic() + timeout
            try:
                if input_text is not None:
                    proc.write(input_text + "\n")
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CliError(f"CLI quá thời gian chờ ({timeout}s).")
                    try:
                        chunk = chunks.get(timeout=min(remaining, 1.0))
                    except queue.Empty:
                        continue
                    if chunk is None:
                        break
                    size += len(chunk)
                    if size > 2_000_000:
                        raise CliError("Phản hồi CLI vượt giới hạn kích thước.")
                    output.append(chunk)
                return proc.exitstatus or 0, strip_terminal("".join(output)), ""
            finally:
                if proc.isalive():
                    proc.terminate(force=True)
        except ImportError:
            pass

    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=account_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = proc.communicate(input_text, timeout=timeout)
        return proc.returncode, strip_terminal(stdout), strip_terminal(stderr)
    except subprocess.TimeoutExpired as exc:
        stop_process(proc)
        raise CliError(f"CLI quá thời gian chờ ({timeout}s).") from exc
    finally:
        if proc.poll() is None:
            stop_process(proc)


class CodexRpc:
    """One app-server connection; serialize requests with the owning adapter."""

    def __init__(self, binary: str, cwd: Path):
        command = executable(binary, "@openai/codex/bin/codex.js")
        self.proc = subprocess.Popen(
            [*command, "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"', "app-server"],
            cwd=cwd,
            env=account_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.messages: queue.Queue = queue.Queue()
        self.pending_events: deque[dict] = deque(maxlen=2048)
        self.login_outcomes: dict[str, bool] = {}
        self.next_id = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {"clientInfo": {"name": "tieru", "title": "TieruAgent", "version": "0.1.0"}})
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.messages.put(None)

    def send(self, message: dict) -> None:
        if self.proc.poll() is not None:
            raise CliError("Codex app-server đã dừng. Làm mới kết nối.")
        try:
            self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CliError("Mất kết nối Codex app-server.") from exc

    def receive(self, timeout: float, *, pending: bool = True) -> dict:
        if timeout <= 0:
            raise CliError("Codex quá thời gian chờ.")
        if pending and self.pending_events:
            return self.pending_events.popleft()
        try:
            message = self.messages.get(timeout=max(timeout, 0.01))
        except queue.Empty as exc:
            raise CliError("Codex quá thời gian chờ.") from exc
        if message is None:
            raise CliError("Codex app-server đã dừng.")
        if message.get("method") == "account/login/completed":
            params = message.get("params", {})
            if params.get("loginId"):
                self.login_outcomes[params["loginId"]] = bool(params.get("success"))
        if "method" in message and "id" in message:
            # Agent generation never grants untracked file-writing approvals.
            if "requestApproval" in message["method"]:
                self.send({"id": message["id"], "result": {"decision": "decline"}})
            else:
                self.send({"id": message["id"], "error": {"code": -32601, "message": "Unsupported client request"}})
        return message

    def request(self, method: str, params: dict | None = None, timeout: int = 30) -> dict:
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            message = self.receive(deadline - time.monotonic(), pending=False)
            if "method" in message and "id" not in message:
                self.pending_events.append(message)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise CliError(message["error"].get("message", "Codex request failed"))
            return message.get("result", {})

    def close(self) -> None:
        stop_process(self.proc)
        self.reader.join(timeout=3)
        if self.proc.stdin:
            self.proc.stdin.close()
        if self.proc.stdout:
            self.proc.stdout.close()
