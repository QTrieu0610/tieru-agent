"""Offline Ollama provider tests plus one explicitly opt-in live smoke test."""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from tieru.config import VERIFIED_GEMMA_MODEL, Settings
from tieru.loop import models
from tieru.providers.ollama import OllamaError, OllamaIntegration


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_ollama_health_and_discovery_are_encapsulated(monkeypatch):
    def fake_urlopen(request, timeout):
        assert request.full_url.startswith("http://127.0.0.1:11434/")
        if request.full_url.endswith("/api/version"):
            return _Response(json.dumps({"version": "0.32.5"}).encode())
        return _Response(json.dumps({"models": [{
            "name": VERIFIED_GEMMA_MODEL,
            "digest": "7fbdbf8f5e45",
            "size": 7_200_000_000,
        }]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    integration = OllamaIntegration("http://127.0.0.1:11434/v1")
    assert integration.health()["version"] == "0.32.5"
    assert integration.models()[0].name == VERIFIED_GEMMA_MODEL
    assert integration.doctor(VERIFIED_GEMMA_MODEL)["model_present"] is True


def test_ollama_connection_error_is_actionable(monkeypatch):
    def unavailable(*args, **kwargs):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    with pytest.raises(OllamaError, match="ollama serve"):
        OllamaIntegration("http://127.0.0.1:11434/v1").health()


def test_ollama_is_keyless_and_uses_openai_adapter(monkeypatch, tmp_path):
    captured = {}

    class StubAdapter:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.messages = SimpleNamespace()

    monkeypatch.setattr(models, "OpenAICompatClient", StubAdapter)
    settings = Settings(provider="ollama", model=VERIFIED_GEMMA_MODEL, home=tmp_path)
    client = models.get_client(settings)
    assert isinstance(client, StubAdapter)
    assert captured["api_key"] == "ollama-local"
    assert captured["base_url"] == "http://127.0.0.1:11434/v1"


def test_openai_stream_normalizes_text_and_tool_calls():
    from tieru.loop.adapters import OpenAIChatAdapter, _OpenAIStream

    client = OpenAIChatAdapter.__new__(OpenAIChatAdapter)
    chunks = [
        SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content="hello ",
                tool_calls=[SimpleNamespace(
                    index=0,
                    id="call-1",
                    function=SimpleNamespace(name="save_note", arguments='{"content":'),
                )],
            ))],
        ),
        SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content="world",
                tool_calls=[SimpleNamespace(
                    index=0,
                    id=None,
                    function=SimpleNamespace(name=None, arguments='"safe"}'),
                )],
            ))],
        ),
    ]
    client._call = lambda kwargs, **extra: chunks
    stream = _OpenAIStream(client, {})
    assert "".join(stream.text_stream) == "hello world"
    final = stream.get_final_message()
    assert final.stop_reason == "tool_use"
    tool = next(block for block in final.content if block.type == "tool_use")
    assert tool.name == "save_note"
    assert tool.input == {"content": "safe"}
    assert final.usage.input_tokens == 3


def test_live_ollama_smoke_is_explicitly_opt_in():
    if os.getenv("TIERU_RUN_OLLAMA_LIVE") != "1":
        pytest.skip("set TIERU_RUN_OLLAMA_LIVE=1 to run against local Ollama")
    result = OllamaIntegration("http://127.0.0.1:11434/v1").doctor(VERIFIED_GEMMA_MODEL)
    assert result["model_present"] is True
