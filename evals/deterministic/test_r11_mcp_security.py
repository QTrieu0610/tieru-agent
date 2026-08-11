"""R1.1 regressions for Trust-authorized MCP startup and bounded results."""

from __future__ import annotations

import json
from types import SimpleNamespace

from tieru.tools.mcp_client import MCPBridge, _bounded_result
from tieru.trust import TrustKernel


def _config(tmp_path, **server):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": [{
        "name": "demo", "command": "python", "args": ["server.py"], **server,
    }]}), encoding="utf-8")
    return path


def test_mcp_configuration_without_process_permission_never_starts_thread(tmp_path, monkeypatch):
    bridge = MCPBridge(_config(tmp_path), kernel=TrustKernel())
    started = []
    monkeypatch.setattr(bridge._thread, "start", lambda: started.append(True))
    assert bridge.start() == []
    assert started == []
    bridge.close()


def test_explicit_process_policy_allows_mcp_connect_path(tmp_path, monkeypatch):
    kernel = TrustKernel({
        "capabilities": {"process_execution": {"mode": "allow", "commands": ["python"]}}
    })
    bridge = MCPBridge(_config(tmp_path), kernel=kernel)
    connected = []

    async def fake_connect(servers):
        connected.extend(servers)
        return {}

    monkeypatch.setattr(bridge, "_connect_all", fake_connect)
    assert bridge.start() == []
    bridge.close()
    assert [item["name"] for item in connected] == ["demo"]


def test_mcp_startup_approval_metadata_excludes_args_and_environment(tmp_path):
    requests = []
    kernel = TrustKernel(approval_handler=lambda request: requests.append(request) or True)
    bridge = MCPBridge(
        _config(tmp_path, env={"API_KEY": "secret-value"}, args=["--token", "secret-value"]),
        kernel=kernel,
    )
    allowed = bridge._authorized_servers(json.loads(bridge.config_path.read_text())["servers"])
    bridge.close()
    assert len(allowed) == 1
    assert requests[0].operation == "start_mcp_server"
    assert requests[0].capabilities == ("process_execution",)
    serialized = json.dumps(requests[0].args)
    assert "secret-value" not in serialized and "--token" not in serialized


def test_mcp_output_is_redacted_bounded_and_status_preserving():
    result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text="api_key=sk-abcdefghijklmnop " + "x" * 4096)],
    )
    rendered = _bounded_result(result, 512)
    assert len(rendered.encode("utf-8")) <= 512
    assert "status=ok" in rendered
    assert "truncated=true" in rendered
    assert "original_size_bytes=" in rendered
    assert "sk-abcdefghijklmnop" not in rendered


def test_mcp_error_and_unsupported_content_are_safe():
    rendered = _bounded_result(
        SimpleNamespace(isError=True, content=[SimpleNamespace(data=b"binary-secret")]),
        512,
    )
    assert "status=error" in rendered
    assert "unsupported MCP content omitted" in rendered
    assert "original_size_bytes=13" in rendered
    assert "binary-secret" not in rendered
