"""THE LOOP — observe → reason → act → repeat. This file is the whole trick.

Every agent framework is ultimately this while-loop with more indirection:

    while not done:
        response = llm(messages, tools)          # reason
        if response asks for tools:
            results = run(tool_calls)            # act
            messages += results                  # observe
        else:
            done                                 # reply to the human

End-loop guardrails (the orange box's exit conditions):
  1. the model stops asking for tools  → natural end of turn
  2. max_iterations reached            → hard stop, never spin forever
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anthropic

from tieru.tools.registry import ToolRegistry

# Observers let the gateway show tool calls live and let ops/tracing record
# them — without either being wired into the loop's logic.
LoopEvent = dict[str, Any]
Observer = Callable[[str, LoopEvent], None]


@dataclass
class LoopResult:
    reply: str
    tool_calls: list[LoopEvent] = field(default_factory=list)
    iterations: int = 0
    run_id: str = ""


def run_loop(
    client: anthropic.Anthropic,
    model: str,
    system: str,
    messages: list[dict],
    tools: ToolRegistry,
    max_iterations: int = 10,
    max_tokens: int = 2048,
    observer: Observer | None = None,
    stream: bool = False,
    provider: str = "",
    role: str = "main",
) -> LoopResult:
    """Run one agent turn. `messages` is mutated in place — after the call it
    contains the full working memory of the turn (assistant thoughts, tool
    calls, tool results), which is exactly what gets traced.

    stream=True emits the assistant's text as it's generated (notify("text",
    {"delta": ...})) so a gateway can show it appear token by token — used by
    the dashboard. Falls back to a single call for clients without streaming."""
    notify = observer or (lambda kind, ev: None)
    result = LoopResult(reply="")
    can_stream = stream and hasattr(client.messages, "stream")
    denied_signatures: set[str] = set()

    for iteration in range(1, max_iterations + 1):
        result.iterations = iteration

        # ---- reason: one LLM call with the current working memory
        response = None
        call_started = time.perf_counter()
        notify("model_call_started", {"iteration": iteration, "model": model,
                                      "provider": provider, "role": role})
        if can_stream:
            try:
                with client.messages.stream(
                    model=model, system=system, messages=messages,
                    tools=tools.schemas(), max_tokens=max_tokens,
                ) as s:
                    for delta in s.text_stream:
                        notify("text", {"delta": delta})
                    response = s.get_final_message()
            except Exception:
                response = None  # any streaming hiccup → fall back to one call
        if response is None:
            response = client.messages.create(
                model=model,
                system=system,
                messages=messages,
                tools=tools.schemas(),
                max_tokens=max_tokens,
            )
        notify("llm", {"iteration": iteration, "stop_reason": response.stop_reason,
                       "usage": {"in": response.usage.input_tokens, "out": response.usage.output_tokens},
                       "duration_ms": int((time.perf_counter() - call_started) * 1000),
                       "model": model, "provider": provider, "role": role})

        # the assistant's turn (text and/or tool requests) joins working memory
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        # ---- guardrail 1: no tool calls → the model is talking to the human
        if not tool_uses:
            result.reply = "".join(b.text for b in response.content if b.type == "text")
            return result

        # ---- act: execute each requested tool; observe: feed results back
        tool_results = []
        for call in tool_uses:
            safe_args = tools.redact_args(call.name, call.input)
            notify("tool_requested", {"tool": call.name, "args": safe_args})
            output = tools.execute(call.name, call.input, notify=notify)
            event = {"tool": call.name, "args": safe_args, "output": output}
            result.tool_calls.append(event)
            notify("tool", event)
            try:
                parsed = json.loads(output)
            except (TypeError, json.JSONDecodeError):
                parsed = {}
            if (parsed.get("error") or {}).get("code") == "tool_permission_denied":
                signature = json.dumps([call.name, safe_args], sort_keys=True)
                if signature in denied_signatures:
                    result.reply = (
                        f"I could not run '{call.name}' because its permission was denied."
                    )
                    return result
                denied_signatures.add(signature)
            tool_results.append(
                {"type": "tool_result", "tool_use_id": call.id, "content": output}
            )
        messages.append({"role": "user", "content": tool_results})

    # ---- guardrail 2: ran out of iterations
    result.reply = "(I hit my iteration limit before finishing — try breaking the request into smaller steps.)"
    notify("error", {"error_code": "iteration_limit",
                     "error_summary": "Maximum loop iterations reached.",
                     "iteration": max_iterations})
    return result
