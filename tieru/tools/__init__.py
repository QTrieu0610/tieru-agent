"""The agent's tools. Flagship-task tools (calendar/notes/messages), memory
self-management (manage_memory/update_soul/create_skill), and opt-in adapters:
Apple ecosystem (TIERU_APPLE_TOOLS=1) and MCP servers (.tieru/mcp.json)."""

from __future__ import annotations

import sqlite3

from tieru.config import Settings
from tieru.tools import calendar, memory_admin, messages, notes, search
from tieru.tools.registry import ApprovalHandler, ToolRegistry


def build_registry(
    conn: sqlite3.Connection,
    settings: Settings,
    memory=None,
    approval_handler: ApprovalHandler | None = None,
) -> ToolRegistry:
    registry = ToolRegistry(
        settings.tool_permissions,
        approval_handler,
        trust_policy=settings.trust_policy,
        trust_context={
            "base_path": str(settings.home.resolve().parent),
            "path_aliases": {
                "home": str(settings.home.resolve()),
                "workspace": str((settings.home / "workspace").resolve()),
            },
            "browser_domains": list(settings.browser_allowed_domains),
        },
    )
    registry.register(
        calendar.make_tool(
            conn,
            settings.home,
            apple_calendar=settings.apple_calendar,
            google_calendar=settings.google_calendar,
            google_calendar_id=settings.google_calendar_id,
        )
    )
    # Read side: "what's on my calendar?" — one tool across every connected
    # source (Google when signed in, plus tieru's own), so the model never has
    # to guess which calendar the user meant.
    registry.register(calendar.make_list_tool(conn, settings.home))
    registry.register(notes.make_tool(memory.store if memory is not None else conn))
    registry.register(messages.make_tool(settings.home))
    # Web search — pairs with create_event for the multi-tool loop demo
    # ("find the World Cup games left and add them to my calendar").
    registry.register(search.make_tool())

    # Memory self-management — the agent can correct/forget memory, learn rules,
    # and author its own skills (feels like a personal agent, not a black box).
    if memory is not None:
        registry.register(memory_admin.make_manage_memory_tool(memory))
        registry.register(memory_admin.make_update_soul_tool(settings))
        registry.register(memory_admin.make_create_skill_tool(settings, memory))

    # Experimental tools — off by default; opt in with TIERU_EXPERIMENTAL=1.
    # delegate_task (sub-agents via pi) is live; terminal/browser/cron are
    # still skeletons that report "coming soon".
    #
    # Trust settings.experimental ALONE. load_settings() already defaults it from
    # TIERU_EXPERIMENTAL, so re-checking the env here would let the global switch
    # override an explicit False — and the arena passes experimental=False for
    # every non-coding race. Once the dashboard could write TIERU_EXPERIMENTAL=1,
    # that OR silently forced delegate_task into races that never asked for it.
    if getattr(settings, "experimental", False):
        from tieru.tools import experimental

        for t in experimental.make_tools(settings):
            registry.register(t)

    # Apple ecosystem readers/writers (opt-in; first use triggers macOS prompts).
    if settings.apple_tools:
        from tieru.tools import apple

        for t in apple.make_tools():
            registry.register(t)

    # Read-only GitHub via the gh CLI (opt-in; uses gh's own auth, no token here).
    if getattr(settings, "gh_tool", False):
        from tieru.tools import github

        registry.register(github.make_tool(default_repo=getattr(settings, "gh_repo", "")))

    if settings.browser_enabled:
        from tieru.tools.browser import RestrictedBrowser, make_tools

        browser = RestrictedBrowser(settings)
        for tool in make_tools(browser):
            registry.register(tool)
        registry.browser = browser

    # MCP servers (opt-in via .tieru/mcp.json).
    mcp_config = settings.home / "mcp.json"
    if mcp_config.exists():
        try:
            from tieru.tools.mcp_client import MCPBridge

            bridge = MCPBridge(
                mcp_config,
                kernel=registry.kernel,
                max_output_bytes=settings.replay_max_tool_output_bytes,
            )
            for t in bridge.start():
                registry.register(t)
            registry.mcp_bridge = bridge  # so Tieru.close() can stop the servers
        except ImportError:
            print("mcp.json found but the 'mcp' package is missing — pip install 'tieru-agent[mcp]'")

    return registry
