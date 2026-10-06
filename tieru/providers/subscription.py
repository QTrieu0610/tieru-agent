"""Subscription-backed AI providers through locally installed CLIs.

Supports Codex (ChatGPT Plus/Pro), Claude Code (Claude Pro/Team), and Antigravity.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

from tieru.providers.cli_transport import (
    CliError,
    CodexRpc,
    account_environment,
    executable,
    run_cli,
    stop_process,
)

PROVIDERS = {
    "codex": "Codex / ChatGPT",
    "antigravity": "Antigravity",
    "claude": "Claude Code",
}


def parse_object(text: str) -> dict:
    if not isinstance(text, str):
        raise CliError("Model không trả về nội dung văn bản.")
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise CliError("Model không trả về JSON hợp lệ.") from exc
    if not isinstance(result, dict):
        raise CliError("Model phải trả về một JSON object.")
    return result


def cli_result(code: int, output: str, error: str) -> str:
    if code:
        raise CliError(f"CLI thất bại (mã {code}). Kiểm tra đăng nhập, quyền dùng model và hạn mức trong CLI.")
    if not output.strip():
        raise CliError("CLI không trả về dữ liệu. Kiểm tra phiên đăng nhập hoặc cập nhật CLI.")
    return output


class SubscriptionManager:
    """Manages local subscription-backed CLI providers."""

    def __init__(self, data_dir: Path | None = None):
        if data_dir is None:
            home = Path(os.getenv("TIERU_HOME", Path.home() / ".tieru"))
            self.root = home / "ai"
        else:
            self.root = data_dir / "ai"
        self.root.mkdir(parents=True, exist_ok=True)
        self._workspace = tempfile.TemporaryDirectory(prefix="tieru-ai-")
        self.work = Path(self._workspace.name)
        self.selection_file = self.root / "selection.json"
        self.session_file = self.root / "session.json"
        self.lock = threading.RLock()
        self.auth_lock = threading.RLock()
        self.codex_auth: CodexRpc | None = None
        self.logins: dict[str, dict] = {}
        self.cache: dict[str, tuple[float, dict]] = {}
        self.ai_timeout = int(os.getenv("TIERU_AI_TIMEOUT", "120"))

        self.codex_binary = os.getenv("CODEX_BINARY", "codex")
        self.claude_binary = os.getenv("CLAUDE_BINARY", "claude")
        self.antigravity_binary = os.getenv("ANTIGRAVITY_BINARY", "agy")

        active = None
        if self.session_file.exists():
            try:
                saved = json.loads(self.session_file.read_text(encoding="utf-8"))
                if saved.get("provider") in PROVIDERS:
                    active = saved["provider"]
            except (OSError, ValueError, AttributeError):
                pass
        else:
            selected = self.read_selection()
            active = selected["provider"] if selected else None
        self.session = {"provider": active, "pending_provider": None}
        self._write_json(self.session_file, self.session)

    def _write_json(self, destination: Path, value: dict) -> None:
        temporary = self.root / f"{destination.stem}-{uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _set_session(self, provider: str | None = None, pending: str | None = None) -> None:
        session = {"provider": provider, "pending_provider": pending}
        self._write_json(self.session_file, session)
        self.session = session

    def _activate(self, provider: str, state: dict) -> None:
        selected = self.read_selection()
        models = state.get("models", [])
        if models:
            model = next(
                (m for m in models if selected and selected["provider"] == provider and m["id"] == selected["model"]),
                None,
            )
            effort = selected.get("effort") if model and selected else None
            model = model or next((m for m in models if m.get("default")), models[0])
            try:
                normalized = self._selection(provider, model, effort)
            except ValueError:
                normalized = self._selection(provider, model, None)
            if selected != normalized:
                self._write_json(self.selection_file, normalized)
        if self.session != {"provider": provider, "pending_provider": None}:
            self._set_session(provider)

    def command(self, provider: str) -> list[str]:
        if provider not in PROVIDERS:
            raise ValueError(f"Provider '{provider}' không hợp lệ.")
        binary = {
            "codex": self.codex_binary,
            "claude": self.claude_binary,
            "antigravity": self.antigravity_binary,
        }[provider]
        package = {
            "codex": "@openai/codex/bin/codex.js",
            "claude": "@anthropic-ai/claude-code/cli.js",
        }.get(provider)
        return executable(binary, package)

    def read_selection(self) -> dict | None:
        with self.lock:
            try:
                result = json.loads(self.selection_file.read_text(encoding="utf-8"))
                if result.get("provider") in PROVIDERS and isinstance(result.get("model"), str):
                    selected = {"provider": result["provider"], "model": result["model"]}
                    if isinstance(result.get("effort"), str):
                        selected["effort"] = result["effort"]
                    return selected
            except (OSError, ValueError, AttributeError):
                pass
        return None

    def _selection(self, provider: str, model: dict, effort: str | None) -> dict:
        allowed = {row["id"] for row in model.get("efforts", [])}
        if effort is not None and effort not in allowed:
            raise ValueError("Effort không được model/provider này hỗ trợ. Làm mới và chọn lại.")
        if effort is None and model.get("default_effort") in allowed:
            effort = model["default_effort"]
        selected = {"provider": provider, "model": model["id"]}
        if effort is not None:
            selected["effort"] = effort
        return selected

    def _codex(self) -> CodexRpc:
        if self.codex_auth is None or self.codex_auth.proc.poll() is not None:
            self.codex_auth = CodexRpc(self.codex_binary, self.work)
        return self.codex_auth

    def _codex_status(self) -> dict:
        with self.auth_lock:
            rpc = self._codex()
            account = rpc.request("account/read", {"refreshToken": False}).get("account") or {}
            login = self.logins.get("codex")
            if login and login.get("login_id") in rpc.login_outcomes:
                self.logins.pop("codex", None)
                if not rpc.login_outcomes.pop(login["login_id"]):
                    return {
                        "connected": False,
                        "models": [],
                        "message": "Đăng nhập ChatGPT chưa hoàn tất hoặc đã hết hạn. Hủy và đăng nhập lại.",
                    }
            if account.get("type") != "chatgpt":
                return {
                    "connected": False,
                    "models": [],
                    "message": "Đăng nhập bằng tài khoản ChatGPT để dùng Codex.",
                }
            models, cursor = [], None
            while True:
                page = rpc.request("model/list", {"limit": 100, "cursor": cursor, "includeHidden": False})
                models.extend(
                    {
                        "id": row["model"],
                        "name": row.get("displayName", row["model"]),
                        "default": row.get("isDefault", False),
                        "default_effort": row.get("defaultReasoningEffort"),
                        "efforts": [
                            {
                                "id": effort["reasoningEffort"],
                                "name": effort["reasoningEffort"],
                                "description": effort.get("description", ""),
                            }
                            for effort in row.get("supportedReasoningEfforts", [])
                        ],
                    }
                    for row in page.get("data", [])
                    if not row.get("hidden")
                )
                cursor = page.get("nextCursor")
                if not cursor:
                    break
            return {
                "connected": True,
                "account": account.get("email"),
                "plan": account.get("planType"),
                "models": models,
            }

    def _claude_status(self, command: list[str]) -> dict:
        code, output, _ = run_cli([*command, "auth", "status"], cwd=self.work)
        account = parse_object(output)
        if (
            code
            or not account.get("loggedIn")
            or account.get("authMethod") not in {"oauth", "claudeai"}
            or account.get("apiProvider") != "firstParty"
        ):
            return {
                "connected": False,
                "models": [],
                "message": "Đăng nhập tài khoản Claude; phiên API key không được sử dụng.",
            }
        message = {"type": "control_request", "request_id": "catalog", "request": {"subtype": "initialize"}}
        code, output, error = run_cli(
            [
                *command,
                "-p",
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
            ],
            cwd=self.work,
            input_text=json.dumps(message) + "\n",
        )
        cli_result(code, output, error)
        models = []
        for line in output.splitlines():
            response = parse_object(line).get("response", {})
            if response.get("request_id") == "catalog" and response.get("subtype") == "success":
                models = [
                    {
                        "id": row["value"],
                        "name": row["displayName"],
                        "default": row["value"] == "default",
                        "efforts": (
                            [{"id": level, "name": level} for level in row.get("supportedEffortLevels", [])]
                            if row.get("supportsEffort")
                            else []
                        ),
                    }
                    for row in response.get("response", {}).get("models", [])
                ]
        if not models:
            raise CliError("Claude CLI chưa cung cấp danh sách model. Cập nhật Claude Code rồi làm mới.")
        return {
            "connected": True,
            "account": account.get("email"),
            "plan": account.get("subscriptionType"),
            "models": models,
        }

    def _check_antigravity_account_mode(self) -> None:
        path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
        if path.exists():
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise CliError("Không đọc được cấu hình Antigravity CLI.") from exc
            if config.get("modelProvider"):
                raise CliError("Antigravity CLI đang ở chế độ custom provider. Chuyển về tài khoản mặc định để dùng.")

    def _antigravity_status(self, command: list[str]) -> dict:
        self._check_antigravity_account_mode()
        code, output, _ = run_cli([*command, "auth", "status"], cwd=self.work)
        account = parse_object(output) if code == 0 else {}
        connected = bool(account.get("loggedIn") or account.get("user") or code == 0)
        models = [
            {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro", "default": True, "efforts": []},
            {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "default": False, "efforts": []},
            {"id": "claude-3-7-sonnet", "name": "Claude 3.7 Sonnet (Antigravity)", "default": False, "efforts": []},
        ]
        return {
            "connected": connected,
            "account": account.get("email") or account.get("user"),
            "plan": account.get("tier", "Google Cloud / Antigravity"),
            "models": models if connected else [],
            "message": None if connected else "Chạy agy đăng nhập trước khi kết nối.",
        }

    def _native_status(self, provider: str, refresh: bool = False) -> dict:
        with self.provider_locks[provider]:
            if not refresh and provider in self.cache:
                timestamp, cached = self.cache[provider]
                if time.monotonic() - timestamp < 30:
                    return cached
            result = {
                "id": provider,
                "name": PROVIDERS[provider],
                "installed": False,
                "connected": False,
                "models": [],
                "account": None,
                "plan": None,
                "message": None,
            }
            try:
                command = self.command(provider)
                result["installed"] = True
                if provider == "codex":
                    result.update(self._codex_status())
                elif provider == "claude":
                    result.update(self._claude_status(command))
                else:
                    result.update(self._antigravity_status(command))
                if result["connected"]:
                    with self.auth_lock:
                        login = self.logins.pop(provider, None)
                        if login and login.get("process"):
                            stop_process(login["process"])
            except (CliError, OSError, ValueError) as exc:
                result["message"] = str(exc)
            self.cache[provider] = (time.monotonic(), result)
            return result

    def status(self, provider: str, refresh: bool = False) -> dict:
        if provider not in PROVIDERS:
            raise ValueError(f"Provider '{provider}' không hợp lệ.")
        with self.lock:
            owner = self.session["provider"] or self.session["pending_provider"]
            if owner == provider:
                result = dict(self._native_status(provider, refresh))
                if result["connected"]:
                    self._activate(provider, result)
                result.update(blocked=False, pending=self.session["pending_provider"] == provider)
                return result
            result = {
                "id": provider,
                "name": PROVIDERS[provider],
                "installed": False,
                "connected": False,
                "models": [],
                "account": None,
                "plan": None,
                "blocked": bool(owner),
                "pending": False,
                "message": (
                    f"Đăng xuất {PROVIDERS[owner]} trước khi kết nối tài khoản này."
                    if owner
                    else "Kết nối tài khoản để tải model. Chỉ một provider được kích hoạt tại một thời điểm."
                ),
            }
            try:
                self.command(provider)
                result["installed"] = True
            except (CliError, OSError, ValueError) as exc:
                result["message"] = str(exc)
            return result

    def overview(self, refresh: bool = False) -> dict:
        with self.lock:
            providers = [self.status(key, refresh) for key in PROVIDERS]
            selected = self.read_selection()
            if selected and selected["provider"] != self.session["provider"]:
                selected = None
            ready = bool(
                selected
                and any(
                    p["id"] == selected["provider"]
                    and p["connected"]
                    and any(m["id"] == selected["model"] for m in p["models"])
                    for p in providers
                )
            )
            return {
                "providers": providers,
                "selection": selected,
                "ready": ready,
                "active_provider": self.session["provider"],
                "pending_provider": self.session["pending_provider"],
            }

    def resolve(self, provider: str | None = None, model: str | None = None, effort: str | None = None) -> dict:
        with self.lock:
            if (provider is None) != (model is None):
                raise ValueError("Cần chọn cả provider và model.")
            selected = {"provider": provider, "model": model} if provider else self.read_selection()
            if not selected:
                raise CliError("Kết nối tài khoản AI trước khi gửi tác vụ.")
            if effort is not None:
                selected["effort"] = effort
            if selected["provider"] not in PROVIDERS:
                raise ValueError(f"Provider '{selected['provider']}' không hợp lệ.")
            if selected["provider"] != self.session["provider"]:
                raise CliError(
                    "Chỉ được dùng provider đang kết nối trong Tieru. Đăng xuất provider hiện tại để đổi tài khoản."
                )
            state = self.status(selected["provider"])
            if not state["connected"]:
                raise CliError(state.get("message") or "Phiên đăng nhập đã hết hạn. Hãy kết nối lại provider.")
            selected_model = next((row for row in state["models"] if row["id"] == selected["model"]), None)
            if not selected_model:
                raise ValueError("Model không nằm trong danh sách của provider đã đăng nhập. Làm mới và chọn lại.")
            return self._selection(selected["provider"], selected_model, selected.get("effort"))

    def select(self, provider: str, model: str, effort: str | None = None) -> dict:
        with self.lock:
            selected = self.resolve(provider, model, effort)
            self._write_json(self.selection_file, selected)
            return selected

    def start_login(self, provider: str, device_code: bool = False) -> dict:
        if provider not in PROVIDERS:
            raise ValueError(f"Provider '{provider}' không hợp lệ.")
        with self.lock:
            owner = self.session["provider"] or self.session["pending_provider"]
            if owner and owner != provider:
                raise CliError(f"Chỉ được kết nối một provider. Đăng xuất {PROVIDERS[owner]} trước.")
            command = self.command(provider)
            state = self._native_status(provider, refresh=True)
            if state["connected"]:
                self._activate(provider, state)
                return {
                    "provider": provider,
                    "mode": "connected",
                    "message": f"Đã kết nối {PROVIDERS[provider]} và sẵn sàng sử dụng.",
                }
            existing = self.logins.get(provider)
            if existing and (
                existing.get("login_id")
                or (existing.get("process") and existing["process"].poll() is None)
                or existing.get("public", {}).get("mode") == "manual"
            ):
                return existing["public"]
            self._set_session(pending=provider)
            try:
                return self._start_native_login(provider, command, device_code)
            except (CliError, OSError, ValueError):
                self._set_session()
                raise

    def _start_native_login(self, provider: str, command: list[str], device_code: bool) -> dict:
        with self.auth_lock:
            if provider == "codex":
                result = self._codex().request(
                    "account/login/start", {"type": "chatgptDeviceCode" if device_code else "chatgpt"}
                )
                public = {
                    "provider": provider,
                    "mode": "device" if device_code else "browser",
                    "login_id": result.get("loginId"),
                    "url": result.get("authUrl") or result.get("verificationUrl"),
                    "user_code": result.get("userCode"),
                    "message": "Hoàn tất đăng nhập ChatGPT trong trình duyệt, sau đó bấm kiểm tra kết nối.",
                }
                self.logins[provider] = {"login_id": result.get("loginId"), "public": public}
            else:
                if provider == "antigravity":
                    self._check_antigravity_account_mode()
                if os.name != "nt":
                    public = {
                        "provider": provider,
                        "mode": "manual",
                        "message": (
                            "Chạy claude auth login --claudeai trong terminal rồi kiểm tra kết nối."
                            if provider == "claude"
                            else "Chạy agy trong terminal, đăng nhập Google rồi kiểm tra kết nối."
                        ),
                    }
                    self.logins[provider] = {"public": public}
                    return public
                args = [*command, "auth", "login", "--claudeai"] if provider == "claude" else command
                proc = subprocess.Popen(
                    args,
                    cwd=self.work,
                    env=account_environment(),
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
                public = {
                    "provider": provider,
                    "mode": "native",
                    "message": "Hoàn tất đăng nhập trong cửa sổ CLI vừa mở, sau đó bấm kiểm tra kết nối.",
                }
                self.logins[provider] = {"process": proc, "public": public}
            self.cache.pop(provider, None)
            return public

    def cancel_login(self, provider: str) -> None:
        if provider not in PROVIDERS:
            raise ValueError(f"Provider '{provider}' không hợp lệ.")
        with self.lock:
            if self.session["pending_provider"] == provider:
                self._set_session(self.session["provider"])
            try:
                self._cancel_native_login(provider)
            except (CliError, OSError):
                pass
            self.cache.pop(provider, None)

    def _cancel_native_login(self, provider: str) -> None:
        with self.auth_lock:
            login = self.logins.pop(provider, None)
            if login and login.get("login_id"):
                self._codex().request("account/login/cancel", {"loginId": login["login_id"]})
            if login and login.get("process"):
                stop_process(login["process"])
            self.cache.pop(provider, None)

    def logout(self, provider: str) -> dict:
        if provider not in PROVIDERS:
            raise ValueError(f"Provider '{provider}' không hợp lệ.")
        with self.lock:
            owner = self.session["provider"] or self.session["pending_provider"]
            if owner == provider:
                self._set_session()
                self.selection_file.unlink(missing_ok=True)
            try:
                self._cancel_native_login(provider)
            except (CliError, OSError):
                pass
            self.cache.pop(provider, None)
            if provider == "codex" and self.codex_auth:
                self.codex_auth.close()
                self.codex_auth = None
        return {"message": f"Đã đăng xuất {PROVIDERS[provider]}. Bạn có thể kết nối provider khác."}

    def complete_text(
        self,
        system: str,
        user: str,
        provider: str,
        model: str,
        effort: str | None = None,
    ) -> str:
        selected = self.resolve(provider, model, effort)
        effort = selected.get("effort") if selected else effort
        if provider == "codex":
            return self._complete_codex_text(system, user, model, effort)
        command = self.command(provider)
        if provider == "claude":
            args = [
                *command,
                "-p",
                "--model",
                model,
                "--output-format",
                "json",
                "--tools",
                "",
                "--max-turns",
                "1",
                "--no-session-persistence",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--system-prompt",
                system,
            ]
            if effort is not None:
                args += ["--effort", effort]
            code, output, error = run_cli(
                args,
                cwd=self.work,
                input_text=user,
                timeout=self.ai_timeout,
            )
            data = parse_object(cli_result(code, output, error))
            if data.get("is_error"):
                raise CliError("Claude không hoàn tất tác vụ. Kiểm tra phiên đăng nhập, model và hạn mức.")
            return str(data.get("result", ""))

        self._check_antigravity_account_mode()
        prompt = f"{system}\n\n{user}" if system else user
        code, output, error = run_cli(
            [*command, "--model", model, "-p", prompt],
            cwd=self.work,
            timeout=self.ai_timeout + 10,
            terminal=True,
        )
        return cli_result(code, output, error)

    def _complete_codex_text(self, system: str, user: str, model: str, effort: str | None = None) -> str:
        rpc = CodexRpc(self.codex_binary, self.work)
        thread_id = turn_id = None
        completed = False
        try:
            thread = rpc.request(
                "thread/start",
                {
                    "model": model,
                    "cwd": str(self.work),
                    "sandbox": "read-only",
                    "approvalPolicy": "never",
                    "developerInstructions": system,
                    "ephemeral": True,
                },
            )
            thread_id = thread["thread"]["id"]
            params = {"threadId": thread_id, "input": [{"type": "text", "text": user}]}
            if effort is not None:
                params["effort"] = effort
            turn = rpc.request("turn/start", params)
            turn_id = turn["turn"]["id"]
            deadline, messages = time.monotonic() + self.ai_timeout, {}
            while True:
                event = rpc.receive(deadline - time.monotonic())
                params = event.get("params", {})
                if params.get("threadId") != thread_id:
                    continue
                if event.get("method") == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        messages[item["id"]] = item
                if event.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                    final = params["turn"]
                    completed = True
                    if final.get("status") != "completed":
                        raise CliError((final.get("error") or {}).get("message") or "Codex không hoàn tất tác vụ.")
                    for item in final.get("items", []):
                        if item.get("type") == "agentMessage":
                            messages[item["id"]] = item
                    answers = [item for item in messages.values() if item.get("phase") != "commentary"]
                    if not answers:
                        raise CliError("Codex không trả về nội dung kết quả.")
                    return str(answers[-1].get("text", ""))
        finally:
            if not completed and thread_id and turn_id and rpc.proc.poll() is None:
                try:
                    rpc.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=3)
                except CliError:
                    pass
            rpc.close()

    def close(self) -> None:
        with self.lock, self.auth_lock:
            for login in self.logins.values():
                if login.get("process"):
                    stop_process(login["process"])
            self.logins.clear()
            if self.codex_auth:
                self.codex_auth.close()
                self.codex_auth = None
            self._workspace.cleanup()


subscription_manager = SubscriptionManager()
