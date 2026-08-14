"""Deterministic coverage for the Telegram transport boundary."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from tieru.config import parse_telegram_allowed_users
from tieru.gateway.telegram import (
    TelegramAgentError,
    TelegramAgentManager,
    _allowed_ids,
    authorized_agent_response,
    get_session_id,
    split_message,
)


def test_allowed_users_merge_legacy_and_multiple_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER", "123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "456, 789,invalid,-1")
    assert _allowed_ids() == {"123", "456", "789"}
    assert parse_telegram_allowed_users("123", "456") == {"123", "456"}


def test_telegram_session_id_is_scoped_to_user():
    assert get_session_id(123) == "telegram:123"
    assert get_session_id(456) != get_session_id(123)


def test_long_message_is_split_without_losing_text():
    text = ("paragraph line\n" * 1000) + "tail"
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 4000 for chunk in chunks)
    assert "".join(chunks) == text


def test_split_prefers_not_to_cut_a_fenced_code_block():
    prefix = "intro\n" * 30
    code = "```python\nprint('hello')\n```\n"
    text = prefix + code + ("after\n" * 30)
    chunks = split_message(text, max_length=220)
    assert "".join(chunks) == text
    assert any(code in chunk for chunk in chunks)


def test_unauthorized_user_never_reaches_agent():
    class RecordingManager:
        calls = 0

        async def respond(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(reply="should not happen")

    manager = RecordingManager()
    result = asyncio.run(
        authorized_agent_response(manager, {"123"}, "999", "hello")
    )
    assert result is None
    assert manager.calls == 0


def test_new_resets_only_the_users_session():
    durable_memory = object()
    created = {}

    class FakeSession:
        def __init__(self, session_id):
            self.session_id = session_id
            self.history = [{"role": "user", "content": "old"}]

        def start_new(self, session_id):
            self.session_id = session_id
            self.history = []

    class FakeAgent:
        def __init__(self, session_id):
            self.session = FakeSession(session_id)
            self.memory = durable_memory
            self.conn = None

        def close(self):
            pass

    def factory(session_id):
        agent = FakeAgent(session_id)
        created[session_id] = agent
        return agent

    async def scenario():
        manager = TelegramAgentManager(
            settings=SimpleNamespace(), agent_factory=factory
        )
        agent = await manager.get_agent(123)
        fresh = await manager.reset(123, "telegram:123:new")
        assert fresh == "telegram:123:new"
        assert agent.session.session_id == fresh
        assert agent.session.history == []
        assert agent.memory is durable_memory
        await manager.close()

    asyncio.run(scenario())


def test_different_users_receive_different_tieru_instances():
    class FakeAgent:
        def __init__(self, session_id):
            self.session = SimpleNamespace(session_id=session_id)
            self.conn = None

        def close(self):
            pass

    async def scenario():
        manager = TelegramAgentManager(
            settings=SimpleNamespace(), agent_factory=FakeAgent
        )
        first, second = await asyncio.gather(
            manager.get_agent(123), manager.get_agent(456)
        )
        assert first is not second
        assert first.session.session_id == "telegram:123"
        assert second.session.session_id == "telegram:456"
        await manager.close()

    asyncio.run(scenario())


def test_same_users_turns_are_serialized():
    state_lock = threading.Lock()
    active = 0
    maximum = 0

    class FakeAgent:
        def __init__(self, session_id):
            self.session = SimpleNamespace(session_id=session_id)
            self.conn = None

        def respond(self, message, **kwargs):
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with state_lock:
                active -= 1
            return SimpleNamespace(reply=message)

        def close(self):
            pass

    async def scenario():
        manager = TelegramAgentManager(
            settings=SimpleNamespace(), agent_factory=FakeAgent
        )
        await asyncio.gather(
            manager.respond(123, "first"),
            manager.respond(123, "second"),
        )
        await manager.close()

    asyncio.run(scenario())
    assert maximum == 1


def test_new_before_first_turn_does_not_initialize_the_model():
    created = []

    class FakeAgent:
        def __init__(self, session_id):
            self.session = SimpleNamespace(session_id=session_id)
            self.conn = None

        def close(self):
            pass

    def factory(session_id):
        created.append(session_id)
        return FakeAgent(session_id)

    async def scenario():
        manager = TelegramAgentManager(
            settings=SimpleNamespace(), agent_factory=factory
        )
        fresh = await manager.reset(123, "telegram:123:new")
        assert fresh == "telegram:123:new"
        assert created == []
        agent = await manager.get_agent(123)
        assert agent.session.session_id == fresh
        assert created == [fresh]
        await manager.close()

    asyncio.run(scenario())


def test_cli_style_system_exit_cannot_terminate_telegram_polling():
    def factory(_session_id):
        raise SystemExit("No API key configured")

    async def scenario():
        manager = TelegramAgentManager(
            settings=SimpleNamespace(), agent_factory=factory
        )
        with pytest.raises(TelegramAgentError, match="No API key configured"):
            await manager.get_agent(123)
        await manager.close()

    asyncio.run(scenario())
