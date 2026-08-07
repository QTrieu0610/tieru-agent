"""Shared eval plumbing: a scripted fake LLM client for offline tests, and a
real Tieru factory for live ones."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from types import SimpleNamespace


def _has_key() -> bool:
    """True when the ACTIVE provider has its key set."""
    from tieru.config import load_settings
    from tieru.loop.models import PROVIDERS

    settings = load_settings()
    provider = PROVIDERS.get(settings.provider)
    return bool(settings.api_key or (provider and os.getenv(provider.key_env)))


HAS_KEY = _has_key()


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, args: dict, call_id: str = "tu_1"):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


def response(blocks, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        content=blocks,
    )


class ScriptedClient:
    """Plays back a fixed list of responses — the 'model' for offline tests."""

    def __init__(self, script: list):
        self._script = list(script)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        return self._script.pop(0)


def make_waku(home: Path, client=None, **settings_overrides):
    """Build a Tieru instance with an isolated home dir; optionally swap in a fake client."""
    from tieru.app import Tieru
    from tieru.config import Settings, load_settings

    known = {f.name for f in dataclasses.fields(Settings)}
    for switch in ("apple_calendar", "google_calendar", "apple_tools", "graph_workflows"):
        if switch in known:
            settings_overrides.setdefault(switch, False)
    settings_overrides.setdefault(
        "tool_permissions", {"tools": {"delegate_task": "confirm"}}
    )
    loader_overrides = {"home": home, **settings_overrides}
    if "provider" in loader_overrides:
        loader_overrides["main_provider"] = loader_overrides.pop("provider")
        loader_overrides.setdefault("small_provider", loader_overrides["main_provider"])
    if "model" in loader_overrides:
        loader_overrides["main_model"] = loader_overrides.pop("model")
    settings = load_settings(loader_overrides)
    if client is not None and not settings.api_key:
        settings.api_key = "offline"
    return Tieru(settings=settings, client=client, approval_handler=lambda request: True)
