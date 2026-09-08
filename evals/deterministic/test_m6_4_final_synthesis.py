"""Deterministic M6.4 final-answer synthesis contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evals.helpers import make_waku, response, text_block, tool_block
from tieru.loop.agent import _validate_synthesis
from tieru.tools import search


class RecordingClient:
    def __init__(self, script):
        self._script = list(script)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._script.pop(0)


def _mock_fetch(monkeypatch, url: str, title: str, content: str):
    monkeypatch.setattr(search, "_validate_public_url", lambda value: value)
    monkeypatch.setattr(
        search,
        "_read",
        lambda _request, _limit: (
            f"<html><head><title>{title}</title></head><body>{content}</body></html>".encode(),
            {"Content-Type": "text/html; charset=utf-8"},
            url,
            False,
        ),
    )


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        ("", "empty"),
        ("Hello!", "greeting"),
        ('{"tool_use": "web_fetch"}', "tool_residue"),
        ("Sources: https://example.com/source", "source_only"),
    ],
)
def test_synthesis_validation_rejects_invalid_shapes(reply, error):
    assert error in _validate_synthesis(reply, {"https://example.com/source"})


def test_synthesis_validation_rejects_fake_url():
    errors = _validate_synthesis(
        "Python 3.14.7. Source: https://fake.example/release",
        {"https://python.org/downloads/"},
    )

    assert "unverified_url" in errors


def test_verified_python_release_is_synthesized_with_content_and_real_url(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(search, "_current_year", lambda: 2026)
    url = "https://python.org/downloads/"
    fake_url = "https://fake.example/release"
    monkeypatch.setattr(
        search,
        "_search_once",
        lambda _query, _limit: (
            [("Python release 3.14.7 in 2026", "Latest stable version", url)],
            "test",
        ),
    )
    _mock_fetch(
        monkeypatch,
        url,
        "Download Python",
        "The latest stable Python release in 2026 is Python 3.14.7.",
    )
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    script = [
        gate,
        response([tool_block("web_search", {"query": "latest Python release"})], "tool_use"),
        response([tool_block("web_fetch", {"url": url})], "tool_use"),
        response([text_block("Hello! What can I do for you?")]),
        response([text_block(f"Python 3.14.7. Source: {fake_url}")]),
        response([text_block(f"Python 3.14.7 is the latest verified release. Source: {url}")]),
    ]
    client = RecordingClient(script)
    app = make_waku(tmp_path / "home", client=client)

    result = app.respond("What is the latest Python release?")

    assert "Python 3.14.7" in result.reply
    assert url in result.reply and fake_url not in result.reply
    assert not result.reply.lower().startswith("hello")
    assert client.calls[-1]["tools"] == []
    synthesis_messages = client.calls[-1]["messages"]
    assert len(synthesis_messages) == 2
    assert synthesis_messages[0]["content"].startswith("TIERU_UNTRUSTED_DATA_V1")
    assert synthesis_messages[1] == {
        "role": "user", "content": "What is the latest Python release?"
    }


def test_python_homepage_greeting_only_synthesis_retries(monkeypatch, tmp_path):
    url = "https://python.org/"
    monkeypatch.setattr(
        search,
        "_search_once",
        lambda _query, _limit: (
            [("Welcome to Python.org", "Official Python homepage", url)],
            "test",
        ),
    )
    _mock_fetch(monkeypatch, url, "Welcome to Python.org", "Python official homepage.")
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    script = [
        gate,
        response([tool_block("web_search", {"query": "official Python homepage"})], "tool_use"),
        response([tool_block("web_fetch", {"url": url})], "tool_use"),
        response([text_block("Sources only")]),
        response([text_block("Hello! I found the page.")]),
        response([text_block(f"The official homepage title is Welcome to Python.org. Source: {url}")]),
    ]
    client = RecordingClient(script)
    app = make_waku(tmp_path / "home", client=client)

    result = app.respond("What is the official Python homepage title?")

    assert "Welcome to Python.org" in result.reply
    assert result.reply.count(url) == 1
    assert client.calls[-2]["tools"] == client.calls[-1]["tools"] == []


def test_insufficient_freshness_uses_deterministic_safe_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(search, "_current_year", lambda: 2026)
    monkeypatch.setattr(
        search,
        "_search_once",
        lambda _query, _limit: (
            [("Acme", "Product information", "https://acme.example/")],
            "test",
        ),
    )
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    client = RecordingClient(
        [
            gate,
            response([tool_block("web_search", {"query": "current Acme release"})], "tool_use"),
            response([text_block("Acme 99 is current at https://fake.example/")]),
        ]
    )
    app = make_waku(tmp_path / "home", client=client)

    result = app.respond("What is the current Acme release?")

    assert result.reply == "I could not verify the requested information from the available evidence."
    assert "99" not in result.reply and "http" not in result.reply
    assert len(client.calls) == 3
