"""MCP connector — plug any Model Context Protocol server into Tieru's tools.

Tieru's loop is synchronous; the MCP SDK is async. The bridge below runs one
asyncio event loop on a daemon thread, holds every server's session on that loop
via a single AsyncExitStack (anyio requires the stack be entered/exited on the
same task), and lets the sync loop call tools via run_coroutine_threadsafe.

Config: TIERU_HOME/mcp.json
  {"servers": [{"name": "fs", "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                "env": {}}]}

Each server's tools register as `<server>_<tool>` on the ToolRegistry. A server
that fails to connect is skipped with a warning — Tieru still starts.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from tieru.memory.personal import redact_secrets
from tieru.tools.registry import Tool
from tieru.trust import ActionRequest, Capability, TrustKernel


def _bounded_result(result: Any, max_output_bytes: int) -> str:
    """Render one MCP result into redacted, model-facing bounded text."""
    status = "error" if bool(getattr(result, "isError", False)) else "ok"
    if isinstance(result, str):
        status = "error"
        raw = result
        original_size = len(raw.encode("utf-8"))
    else:
        parts = []
        original_size = 0
        for block in getattr(result, "content", ()):
            text = getattr(block, "text", None)
            if isinstance(text, str):
                original_size += len(text.encode("utf-8"))
                parts.append(text)
                continue
            data = getattr(block, "data", b"")
            size = len(data if isinstance(data, bytes) else str(data).encode("utf-8"))
            original_size += size
            parts.append(f"[unsupported MCP content omitted: {size} bytes]")
        original_size += max(0, len(parts) - 1)
        raw = "\n".join(parts) or "(no output)"
    safe = redact_secrets(raw)
    limit = max(128, int(max_output_bytes))
    base = f"[MCP result: status={status}; original_size_bytes={original_size}; truncated="
    complete_header = base + "false]"
    complete = complete_header + "\n" + safe
    if len(complete.encode("utf-8")) <= limit:
        return complete
    truncated_header = base + "true]"
    budget = max(0, limit - len((truncated_header + "\n").encode("utf-8")))
    preview = safe.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return truncated_header + "\n" + preview


class MCPBridge:
    def __init__(
        self,
        config_path: Path,
        timeout: float = 30.0,
        *,
        kernel: TrustKernel | None = None,
        max_output_bytes: int = 2048,
    ):
        self.config_path = config_path
        self.timeout = timeout
        self.kernel = kernel
        self.max_output_bytes = max_output_bytes
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._started = False
        self._stack: AsyncExitStack | None = None
        self._sessions: dict = {}

    def start(self) -> list[Tool]:
        """Connect every configured server and return their tools (as Tools)."""
        configured = json.loads(self.config_path.read_text(encoding="utf-8")).get("servers", [])
        servers = self._authorized_servers(configured)
        if not servers:
            return []
        self._thread.start()
        self._started = True
        specs = {str(item.get("name", "")): item for item in servers if isinstance(item, dict)}
        fut = asyncio.run_coroutine_threadsafe(self._connect_all(servers), self._loop)
        try:
            listed = fut.result(self.timeout * 2)  # {server: [tool metas]}
        except Exception:
            self.close()
            raise
        tools: list[Tool] = []
        for srv, metas in listed.items():
            for meta in metas:
                # Remote descriptions are not authorization metadata. A user may
                # explicitly classify individual MCP tools in mcp.json; absent
                # that local declaration, the action stays unclassified/denied.
                configured = (specs.get(srv, {}).get("tool_policies") or {}).get(
                    meta["name"], {}
                )
                classified = isinstance(configured, dict) and bool(
                    configured.get("capabilities")
                )
                tools.append(Tool(
                    name=f"{srv}_{meta['name']}",
                    description=f"[MCP:{srv}] {meta.get('description','') or ''}",
                    input_schema=meta.get("inputSchema") or {"type": "object", "properties": {}},
                    fn=(lambda srv=srv, tname=meta["name"], **kw: self.call(srv, tname, kw)),
                    risk=str(configured.get("risk", "high" if classified else "critical")),
                    read_only=bool(configured.get("read_only", False)),
                    capabilities=tuple(configured.get("capabilities", ("unclassified",))),
                    default_policy=str(configured.get("policy", "deny")),
                    operation=str(configured.get("operation", meta["name"])),
                    fixed_target=f"{srv}/{meta['name']}",
                    scope=str(configured.get("scope", f"mcp:{srv}")),
                    resource_type="mcp",
                    reversible=bool(configured.get("reversible", False)),
                    explicit_policy=classified,
                ))
        return tools

    def _authorized_servers(self, servers: Any) -> list[dict[str, Any]]:
        """Authorize each configured process before the event loop can spawn it."""
        if not isinstance(servers, list):
            print("MCP configuration ignored: 'servers' must be a list")
            return []
        allowed = []
        for spec in servers:
            if not isinstance(spec, dict):
                continue
            name = str(spec.get("name", "")).strip()
            command = str(spec.get("command", "")).strip()
            if not name or not command:
                print("MCP server configuration skipped: name and command are required")
                continue
            if self.kernel is None:
                print(f"MCP server '{name}' denied: Trust authorization is unavailable")
                continue
            action = ActionRequest(
                tool_name="mcp_start_server",
                capabilities=(Capability.PROCESS_EXECUTION,),
                operation="start_mcp_server",
                target=command,
                scope=str(self.config_path.parent.resolve()),
                resource_type="process",
                local=True,
                process_execution=True,
                reversible=True,
                read_only=False,
                metadata={"server_name": name},
            )
            decision = self.kernel.authorize(
                action,
                default_policy="confirm",
                legacy_capabilities=("process.execute",),
                approval_args={
                    "server": name,
                    "command": command,
                    "config_directory": str(self.config_path.parent.resolve()),
                },
            )
            if not decision.allowed:
                print(f"MCP server '{name}' denied by Trust policy; process was not started")
                continue
            allowed.append(spec)
        return allowed

    async def _connect_all(self, servers) -> dict:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stack = AsyncExitStack()
        listed: dict = {}
        for spec in servers:
            name = spec["name"]
            try:
                params = StdioServerParameters(
                    command=spec["command"], args=spec.get("args", []), env=spec.get("env") or None
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session
                tools = (await session.list_tools()).tools
                listed[name] = [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools]
            except Exception as exc:  # one bad server shouldn't stop the rest
                print(f"MCP server '{name}' failed to connect: {exc}")
        return listed

    def call(self, server: str, tool: str, args: dict) -> str:
        try:
            fut = asyncio.run_coroutine_threadsafe(self._acall(server, tool, args), self._loop)
            return _bounded_result(fut.result(self.timeout), self.max_output_bytes)
        except Exception as exc:
            return _bounded_result(
                f"MCP call {server}_{tool} failed: {redact_secrets(str(exc))}",
                self.max_output_bytes,
            )

    async def _acall(self, server: str, tool: str, args: dict) -> Any:
        session = self._sessions.get(server)
        if session is None:
            return f"MCP server '{server}' is not connected."
        return await session.call_tool(tool, args)

    def close(self) -> None:
        if not self._started:
            if not self._loop.is_closed():
                self._loop.close()
            return
        if self._stack is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._stack.aclose(), self._loop).result(10)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._started = False
        if not self._loop.is_closed():
            self._loop.close()
