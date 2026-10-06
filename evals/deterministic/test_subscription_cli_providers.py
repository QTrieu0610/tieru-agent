from unittest.mock import MagicMock

from tieru.config import load_settings
from tieru.loop.cli_adapters import SubscriptionCliAdapter
from tieru.loop.models import get_client
from tieru.ops.dashboard import connections_action
from tieru.providers.cli_transport import CliError
from tieru.providers.subscription import cli_result, parse_object


def test_parse_object_and_cli_result():
    assert parse_object('{"key": "value"}') == {"key": "value"}
    assert parse_object('```json\n{"foo": 123}\n```') == {"foo": 123}

    try:
        parse_object("not json")
        assert False, "Should raise CliError"
    except CliError:
        pass

    assert cli_result(0, "output text", "") == "output text"
    try:
        cli_result(1, "", "error")
        assert False, "Should raise CliError"
    except CliError:
        pass


def test_subscription_cli_adapter_tool_parsing():
    adapter = SubscriptionCliAdapter("codex")

    # Plain text output
    text_blocks = adapter._parse_output("Here is the answer.")
    assert len(text_blocks) == 1
    assert text_blocks[0].type == "text"
    assert text_blocks[0].text == "Here is the answer."

    # Output with tool call
    raw_tool = 'Thinking...\n```json\n{"tool": "filesystem_read", "args": {"path": "test.txt"}}\n```'
    tool_blocks = adapter._parse_output(raw_tool)
    assert len(tool_blocks) == 2
    assert tool_blocks[0].type == "text"
    assert tool_blocks[0].text == "Thinking..."
    assert tool_blocks[1].type == "tool_use"
    assert tool_blocks[1].name == "filesystem_read"
    assert tool_blocks[1].input == {"path": "test.txt"}


def test_subscription_cli_adapter_create_mock():
    mock_manager = MagicMock()
    mock_manager.complete_text.return_value = '```json\n{"tool": "web_search", "args": {"query": "python"}}\n```'

    adapter = SubscriptionCliAdapter("codex", manager=mock_manager)
    res = adapter.messages.create(
        model="gpt-4o",
        system="You are helpful",
        messages=[{"role": "user", "content": "search something"}],
        tools=[{"name": "web_search", "description": "search web"}],
    )

    assert res.stop_reason == "tool_use"
    assert len(res.content) == 1
    assert res.content[0].type == "tool_use"
    assert res.content[0].name == "web_search"
    assert res.content[0].input == {"query": "python"}
    mock_manager.complete_text.assert_called_once()


def test_get_client_resolves_subscription_providers(tmp_path):
    settings = load_settings({
        "main_provider": "codex",
        "main_model": "gpt-4o",
    })
    client = get_client(settings, role="main")
    assert isinstance(client, SubscriptionCliAdapter)
    assert client.provider == "codex"

    settings_claude = load_settings({
        "main_provider": "claude_code",
        "main_model": "claude-3-7-sonnet",
    })
    client_claude = get_client(settings_claude, role="main")
    assert isinstance(client_claude, SubscriptionCliAdapter)
    assert client_claude.provider == "claude"

    settings_ag = load_settings({
        "main_provider": "antigravity",
        "main_model": "gemini-2.5-pro",
    })
    client_ag = get_client(settings_ag, role="main")
    assert isinstance(client_ag, SubscriptionCliAdapter)
    assert client_ag.provider == "antigravity"


def test_connections_action_dashboard():
    overview = connections_action({"action": "overview"})
    assert "providers" in overview
    provider_ids = [p["id"] for p in overview["providers"]]
    assert "codex" in provider_ids
    assert "claude" in provider_ids
    assert "antigravity" in provider_ids

    status = connections_action({"action": "status", "provider": "codex"})
    assert status["id"] == "codex"
    assert "installed" in status

    unknown = connections_action({"action": "nonexistent"})
    assert "error" in unknown
