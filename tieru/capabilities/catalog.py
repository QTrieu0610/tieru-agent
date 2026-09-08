"""Dynamic capability catalog construction from ToolRegistry metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tieru.capabilities.models import Capability

if TYPE_CHECKING:
    from tieru.tools.registry import Tool, ToolRegistry

# Canonical capability blueprints for standard Tieru tools.
_CANONICAL_BLUEPRINTS: tuple[dict, ...] = (
    {
        "capability_id": "coding_execution",
        "name": "Command Execution",
        "description": "Execute terminal commands, run tests, compile and evaluate code in the local workspace.",
        "aliases": ("run command", "terminal", "bash", "shell", "execute command", "run tests", "run_command", "shell_run", "chạy lệnh", "thực thi lệnh", "lệnh"),
        "keywords": ("command", "execute", "shell", "terminal", "test", "tests", "run", "bash", "pytest", "pwd", "lệnh", "chạy", "thực thi", "kiểm thử", "thư mục"),
        "domains": ("coding", "system"),
        "operations": ("execute",),
        "tool_patterns": ("run_command", "shell_run", "run_tests", "test_runner"),
        "risk": "high",
        "read_only": False,
    },
    {
        "capability_id": "calendar",
        "name": "Calendar & Scheduling",
        "description": "View, list, create, update, and manage calendar events and meetings.",
        "aliases": ("calendar", "meetings", "events", "schedule meeting", "list events", "show meetings"),
        "keywords": ("calendar", "event", "events", "meeting", "meetings", "schedule", "appointment"),
        "domains": ("productivity", "calendar"),
        "operations": ("read", "create", "update", "delete"),
        "tool_patterns": ("list_events", "create_event", "calendar", "list_calendar_events", "update_event", "delete_event"),
        "risk": "medium",
        "read_only": False,
    },
    {
        "capability_id": "messaging",
        "name": "Messaging & Communication",
        "description": "Read, draft, and send messages, emails, chats, and SMS to recipients.",
        "aliases": ("messaging", "messages", "email", "sms", "chat", "send message", "send_message"),
        "keywords": ("message", "messages", "email", "sms", "chat", "send", "recipient"),
        "domains": ("communication",),
        "operations": ("read", "create"),
        "tool_patterns": ("send_message", "messages"),
        "risk": "medium",
        "read_only": False,
    },
    {
        "capability_id": "notes",
        "name": "Notes & Scratchpad",
        "description": "Read and write scratchpad notes, memos, and quick reference items.",
        "aliases": ("notes", "scratchpad", "note", "take note", "memo", "save note"),
        "keywords": ("notes", "note", "scratchpad", "memo", "jot", "save"),
        "domains": ("productivity", "notes"),
        "operations": ("read", "create", "update"),
        "tool_patterns": ("save_note", "notes", "list_notes", "append_note"),
        "risk": "low",
        "read_only": False,
    },
    {
        "capability_id": "search",
        "name": "Web Search & Research",
        "description": "Search the public web and fetch content from specific web URLs.",
        "aliases": ("web search", "search", "google", "browse", "web fetch", "web_search", "web_fetch"),
        "keywords": ("search", "web", "fetch", "url", "browse", "internet", "google"),
        "domains": ("research", "web"),
        "operations": ("read",),
        "tool_patterns": ("search", "web_search", "web_fetch"),
        "risk": "low",
        "read_only": True,
    },
    {
        "capability_id": "weather",
        "name": "Weather Information",
        "description": "Check current weather forecasts and conditions for locations.",
        "aliases": ("weather", "forecast", "temperature"),
        "keywords": ("weather", "forecast", "temperature", "rain", "climate", "conditions"),
        "domains": ("utilities",),
        "operations": ("read",),
        "tool_patterns": ("weather",),
        "risk": "low",
        "read_only": True,
    },
    {
        "capability_id": "filesystem",
        "name": "Local Filesystem Operations",
        "description": "Inspect, read, write, edit, and list files and directories in the workspace.",
        "aliases": ("filesystem", "files", "file", "directory", "read file", "write file", "inspect file", "verify file", "read_file", "write_file", "tập tin", "thư mục", "đọc file", "ghi file"),
        "keywords": ("file", "files", "directory", "read", "write", "path", "folder", "inspect", "fix", "patch", "change", "code", "repo", "repository", "tập tin", "thư mục", "đường dẫn", "đọc", "ghi", "xác định"),
        "domains": ("filesystem", "coding"),
        "operations": ("read", "create", "update"),
        "tool_patterns": (
            "filesystem_read", "filesystem_write", "filesystem_search", "filesystem_mkdir",
            "document_read", "read_file", "write_file", "list_dir", "edit_file", "view_file",
            "inspect_file", "verify_file",
        ),
        "risk": "medium",
        "read_only": False,
    },
    {
        "capability_id": "repository",
        "name": "Repository & Git Operations",
        "description": "Inspect git status, git diff, code symbols, commits, and repository structure.",
        "aliases": ("git", "repository", "repo", "github", "code symbols", "git status", "git diff"),
        "keywords": ("git", "repo", "repository", "commit", "diff", "status", "symbols", "branch"),
        "domains": ("coding", "git"),
        "operations": ("read",),
        "tool_patterns": (
            "git_status", "git_diff", "git_log", "code_symbols", "code_search",
            "code_read", "code_patch", "github", "github_read",
        ),
        "risk": "low",
        "read_only": True,
    },
    {
        "capability_id": "delegation",
        "name": "Task Delegation & Subagents",
        "description": "Delegate tasks and complex subproblems to specialized worker subagents.",
        "aliases": ("delegate", "delegate task", "subagent", "worker"),
        "keywords": ("delegate", "task", "subagent", "worker", "agent"),
        "domains": ("orchestration", "coding"),
        "operations": ("execute",),
        "tool_patterns": ("delegate_task",),
        "risk": "medium",
        "read_only": False,
    },
    {
        "capability_id": "memory_recall",
        "name": "Memory Recall",
        "description": "Query personal and episodic memory to recall previous conversations, facts, and context.",
        "aliases": ("memory", "recall", "memory recall", "remember", "what did I tell you", "memory query"),
        "keywords": ("memory", "remember", "recall", "search", "knowledge", "fact", "past"),
        "domains": ("memory",),
        "operations": ("read",),
        "tool_patterns": ("memory_search", "memory_query", "recall_memory", "remember", "memory_remember"),
        "risk": "low",
        "read_only": True,
    },
    {
        "capability_id": "memory_admin",
        "name": "Memory Administration",
        "description": "Manage, forget, update core memory soul rules, and author procedural skills.",
        "aliases": ("memory admin", "forget memory", "update soul", "create skill", "manage memory"),
        "keywords": ("memory", "admin", "forget", "soul", "skill", "delete memory", "learn"),
        "domains": ("memory",),
        "operations": ("update", "delete", "create"),
        "tool_patterns": ("manage_memory", "update_soul", "create_skill"),
        "risk": "high",
        "read_only": False,
    },
    {
        "capability_id": "scheduler",
        "name": "Task Scheduling",
        "description": "Schedule recurring or one-shot background tasks, list schedules, and cancel schedules.",
        "aliases": ("scheduler", "schedule", "cron", "recurring", "cancel schedule", "every morning"),
        "keywords": ("schedule", "cron", "recurring", "interval", "cancel", "timer"),
        "domains": ("scheduler",),
        "operations": ("read", "create", "delete"),
        "tool_patterns": ("schedule_task", "list_schedules", "cancel_schedule"),
        "risk": "medium",
        "read_only": False,
    },
    {
        "capability_id": "browser",
        "name": "Restricted Web Browser",
        "description": "Interact with allowed web domains using a restricted browser automation engine.",
        "aliases": ("browser", "playwright", "web automation", "open browser"),
        "keywords": ("browser", "click", "page", "navigate", "web", "automation"),
        "domains": ("browser", "web"),
        "operations": ("read", "execute"),
        "tool_patterns": (
            "browser_open", "browser_click", "browser_type", "browser_close",
            "browser_select", "browser_screenshot",
        ),
        "risk": "medium",
        "read_only": False,
    },
)


def _infer_operations(tool: Tool) -> tuple[str, ...]:
    if tool.operation:
        return (tool.operation.lower(),)
    low_name = tool.name.lower()
    if any(k in low_name for k in ("list", "read", "get", "query", "status", "diff", "inspect", "fetch")):
        return ("read",)
    if any(k in low_name for k in ("create", "add", "new", "schedule")):
        return ("create",)
    if any(k in low_name for k in ("update", "edit", "modify")):
        return ("update",)
    if any(k in low_name for k in ("delete", "remove", "cancel", "wipe", "destroy", "forget")):
        return ("delete",)
    if any(k in low_name for k in ("run", "exec", "delegate")):
        return ("execute",)
    return ("read",) if tool.read_only else ("write",)


def build_capability_catalog(registry: ToolRegistry) -> dict[str, Capability]:
    """Dynamically construct a capability catalog from all registered tools."""
    registered_tools = getattr(registry, "_tools", {})
    if not registered_tools:
        return {}

    catalog: dict[str, Capability] = {}
    claimed_tools: set[str] = set()

    # 1. Custom tool-declared capabilities
    custom_groups: dict[str, list[Tool]] = {}
    for tool in registered_tools.values():
        if tool.capability:
            custom_groups.setdefault(tool.capability, []).append(tool)

    for cap_id, tools in custom_groups.items():
        tool_names = tuple(t.name for t in tools)
        claimed_tools.update(tool_names)
        aliases = tuple(dict.fromkeys(a for t in tools for a in t.aliases))
        keywords = tuple(dict.fromkeys(k for t in tools for k in t.keywords))
        domains = tuple(dict.fromkeys(d for t in tools for d in t.domains))
        all_ops = tuple(dict.fromkeys(op for t in tools for op in _infer_operations(t)))
        always_visible = any(t.always_visible for t in tools)
        read_only = all(bool(t.read_only) for t in tools)
        catalog[cap_id] = Capability(
            capability_id=cap_id,
            name=tools[0].name.replace("_", " ").title(),
            description=tools[0].description,
            aliases=aliases,
            keywords=keywords,
            domains=domains,
            tool_names=tool_names,
            risk=tools[0].risk or "medium",
            read_only=read_only,
            operations=all_ops,
            always_visible=always_visible,
        )

    # 2. Canonical blueprints matching remaining registered tools
    for blueprint in _CANONICAL_BLUEPRINTS:
        patterns = blueprint["tool_patterns"]
        matching_tools = [
            t for name, t in registered_tools.items()
            if name in patterns and name not in claimed_tools
        ]
        if matching_tools:
            tool_names = tuple(t.name for t in matching_tools)
            claimed_tools.update(tool_names)
            # Merge tool-level aliases and keywords if present
            extra_aliases = tuple(a for t in matching_tools for a in t.aliases)
            extra_keywords = tuple(k for t in matching_tools for k in t.keywords)
            extra_domains = tuple(d for t in matching_tools for d in t.domains)
            all_ops = tuple(dict.fromkeys(op for t in matching_tools for op in _infer_operations(t)))
            always_visible = any(t.always_visible for t in matching_tools)
            catalog[blueprint["capability_id"]] = Capability(
                capability_id=blueprint["capability_id"],
                name=blueprint["name"],
                description=blueprint["description"],
                aliases=tuple(dict.fromkeys((*blueprint["aliases"], *extra_aliases))),
                keywords=tuple(dict.fromkeys((*blueprint["keywords"], *extra_keywords))),
                domains=tuple(dict.fromkeys((*blueprint["domains"], *extra_domains))),
                tool_names=tool_names,
                risk=blueprint["risk"],
                read_only=blueprint["read_only"],
                operations=all_ops or blueprint["operations"],
                always_visible=always_visible,
            )

    # 3. Any remaining unassigned tools get single-tool capabilities
    for name, tool in registered_tools.items():
        if name in claimed_tools:
            continue
        ops = _infer_operations(tool)
        catalog[f"tool.{name}"] = Capability(
            capability_id=f"tool.{name}",
            name=name.replace("_", " ").title(),
            description=tool.description or f"Tool {name}",
            aliases=tool.aliases or (name, name.replace("_", " ")),
            keywords=tool.keywords or tuple(name.split("_")),
            domains=tool.domains or (),
            tool_names=(name,),
            risk=tool.risk or ("low" if tool.read_only else "medium"),
            read_only=bool(tool.read_only),
            operations=ops,
            always_visible=tool.always_visible,
        )

    return catalog
