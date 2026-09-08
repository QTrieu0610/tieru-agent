"""Bounded same-role verification for DEEP turns; never changes the answer."""

from __future__ import annotations

import json
import time

from tieru.context import ContextBuilder

_PROMPT = """Review the answer for obvious incompleteness or contradiction.
Return only JSON: {{"verified": true/false, "issue_count": <integer>}}.
Do not provide hidden reasoning.

Request: {message}
Answer: {answer}"""


def verify_result(
    model_router, decision, message: str, answer: str, notify, *, candidate=None
) -> dict:
    if not decision.profile.verification_enabled:
        return {"status": "skipped", "verified": None, "issue_count": 0}
    started = time.perf_counter()
    try:
        notify("model_call_started", {
            "role": decision.role, "model": decision.model,
            "provider": decision.provider, "purpose": "fabric_verification",
        })
        client = model_router.client_for(candidate, decision.role)
        builder = ContextBuilder(max_block_bytes=8192, max_data_bytes=10_000)
        builder.add_control(
            _PROMPT.split("Request:", 1)[0], source="fabric_verifier"
        )
        builder.add_user(message[:2000], source="user")
        builder.add_data(answer[:4000], source="model_output")
        assembly = builder.build()
        response = client.messages.create(
            model=decision.model,
            max_tokens=min(256, decision.profile.max_tokens),
            system=assembly.system,
            messages=list(assembly.messages),
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        value = json.loads(text[text.index("{"):text.rindex("}") + 1])
        result = {
            "status": "completed", "verified": bool(value.get("verified")),
            "issue_count": max(0, int(value.get("issue_count", 0))),
        }
        usage = getattr(response, "usage", None)
        notify("model_call_completed", {
            "role": decision.role, "model": decision.model,
            "provider": decision.provider, "purpose": "fabric_verification",
            "usage": {"in": getattr(usage, "input_tokens", 0),
                      "out": getattr(usage, "output_tokens", 0)},
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "stop_reason": getattr(response, "stop_reason", ""),
        })
    except Exception as exc:  # noqa: BLE001 - preserve original safe answer
        result = {
            "status": "degraded", "verified": None, "issue_count": 0,
            "error_code": type(exc).__name__,
        }
    notify("fabric_verification", result)
    return result
