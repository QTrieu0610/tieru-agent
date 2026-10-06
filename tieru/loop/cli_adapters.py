"""Adapter bridging local subscription CLI providers to Tieru Messages interface."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from uuid import uuid4

from tieru.loop.adapters import ModelError
from tieru.providers.subscription import SubscriptionManager, subscription_manager

_TOOL_CALL_REGEX = re.compile(
    r"```(?:json)?\s*\{\s*\"tool\"\s*:\s*\"([A-Za-z0-9_-]+)\"\s*,\s*\"args\"\s*:\s*(\{.*?\})\s*\}\s*```",
    re.DOTALL,
)


class SubscriptionCliAdapter:
    """Adapts a local subscription CLI (Codex, Claude, Antigravity) to Tieru Messages."""

    protocol = "cli"

    def __init__(
        self,
        provider: str,
        manager: SubscriptionManager | None = None,
        effort: str | None = None,
    ):
        self.provider = provider
        self.manager = manager or subscription_manager
        self.effort = effort
        self.messages = SimpleNamespace(create=self._create, stream=self._create)

    @property
    def supports_required_tool_choice(self) -> bool:
        return True

    @property
    def supports_tool_calling(self) -> bool:
        return True

    def _format_conversation(
        self,
        messages: list[dict],
        system: str | None,
        tools: list[dict] | None,
    ) -> tuple[str, str]:
        """Convert system, history and tools into system instructions and a consolidated user turn."""
        sys_parts = []
        if system:
            sys_parts.append(system)

        if tools:
            tool_guide = (
                "\nYou have access to the following tools:\n"
                + json.dumps(tools, ensure_ascii=False, indent=2)
                + "\n\nIf you need to call a tool, respond ONLY with a codeblock in this format:\n"
                "```json\n"
                '{"tool": "tool_name", "args": {"param": "value"}}\n'
                "```\n"
                "Otherwise, answer normally in plain text."
            )
            sys_parts.append(tool_guide)

        system_text = "\n\n".join(sys_parts)

        # Build prompt from messages
        user_parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str):
                user_parts.append(f"[{role.upper()}]:\n{content}")
            elif isinstance(content, list):
                # list of blocks
                blocks_str = []
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "tool_result":
                            blocks_str.append(f"[TOOL RESULT for {b.get('tool_use_id', '')}]:\n{b.get('content', '')}")
                        elif b.get("type") == "text":
                            blocks_str.append(str(b.get("text", "")))
                    else:
                        text = getattr(b, "text", "")
                        if text:
                            blocks_str.append(text)
                if blocks_str:
                    user_parts.append(f"[{role.upper()}]:\n" + "\n".join(blocks_str))

        user_text = "\n\n".join(user_parts)
        return system_text, user_text

    def _parse_output(self, raw_text: str) -> list[SimpleNamespace]:
        raw = raw_text.strip()
        # Check if the output contains a structured tool call
        match = _TOOL_CALL_REGEX.search(raw)
        if match:
            tool_name = match.group(1)
            raw_args = match.group(2)
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
            # Before match text if any
            prefix = raw[: match.start()].strip()
            blocks = []
            if prefix:
                blocks.append(SimpleNamespace(type="text", text=prefix))
            blocks.append(
                SimpleNamespace(
                    type="tool_use",
                    id=f"call_{uuid4().hex[:8]}",
                    name=tool_name,
                    input=args,
                )
            )
            return blocks

        # Plain text answer
        return [SimpleNamespace(type="text", text=raw)]

    def _create(self, **kwargs) -> SimpleNamespace:
        model = kwargs.get("model", "")
        system = kwargs.get("system")
        messages = kwargs.get("messages", [])
        tools = kwargs.get("tools")
        tool_choice = kwargs.get("tool_choice")

        # If tool_choice is none (e.g. final answer synthesis), omit tools from prompt
        active_tools = tools if tool_choice != {"type": "none"} else None

        sys_prompt, user_prompt = self._format_conversation(messages, system, active_tools)

        try:
            output = self.manager.complete_text(
                system=sys_prompt,
                user=user_prompt,
                provider=self.provider,
                model=model,
                effort=self.effort,
            )
        except Exception as exc:
            raise ModelError(f"Subscription provider '{self.provider}' failed: {exc}") from exc

        blocks = self._parse_output(output)
        has_tools = any(getattr(b, "type", "") == "tool_use" for b in blocks)

        return SimpleNamespace(
            stop_reason="tool_use" if has_tools else "end_turn",
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
            content=blocks,
        )
