"""Isolated deterministic M10-Lite bounded controller contracts."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from evals.helpers import response, text_block, tool_block
from tieru.loop.agent import run_bounded_multi_step, run_loop
from tieru.tools.registry import Tool, ToolRegistry


class RecordingClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.script.pop(0)


def fixture_tool(
    name,
    fn,
    *,
    properties=None,
    required=None,
    read_only=True,
    default_policy="allow",
):
    return Tool(
        name=name,
        description=f"M10-Lite fixture tool {name}.",
        input_schema={
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
        fn=fn,
        risk="low" if read_only else "medium",
        read_only=read_only,
        capabilities=("local_read",) if read_only else ("local_write",),
        default_policy=default_policy,
    )


def registry(*items, approve=True):
    tools = ToolRegistry(approval_handler=lambda _request: approve)
    for item in items:
        tools.register(item)
    return tools


def run(script, tools, goal="Complete the multi-step goal.", **limits):
    client = RecordingClient(script)
    result = run_bounded_multi_step(
        client,
        "model",
        "system",
        [{"role": "user", "content": goal}],
        tools,
        require_plan=True,  # legacy keyword must not enable a planner
        **limits,
    )
    return result, client


def decision_context(call):
    return json.loads(call["messages"][0]["content"])


def test_m10_lite_is_not_active_in_the_normal_agent_path():
    client = RecordingClient([response([text_block("direct reply")])])
    original_messages = [{"role": "user", "content": "normal goal"}]

    result = run_loop(client, "model", "normal system", original_messages, registry())

    assert result.reply == "direct reply" and result.state == {}
    assert client.calls[0]["system"] == "normal system"
    assert client.calls[0]["messages"] is original_messages


def test_a_three_plus_tool_calls_complete_without_upfront_plan():
    seen = []
    tools = registry(
        *[
            fixture_tool(name, lambda name=name: seen.append(name) or json.dumps({"value": name}))
            for name in ("first", "second", "third")
        ]
    )
    result, client = run(
        [
            response([tool_block("first", {}, "1")], "tool_use"),
            response([tool_block("second", {}, "2")], "tool_use"),
            response([tool_block("third", {}, "3")], "tool_use"),
            response([text_block("done")]),
        ],
        tools,
    )

    assert seen == ["first", "second", "third"]
    assert result.reply == "done" and result.state["step_count"] == 3
    assert set(result.state) == {"goal", "observations", "tool_calls", "errors", "step_count"}
    assert all("plan_task" not in [tool["name"] for tool in call["tools"]] for call in client.calls)
    for index, call in enumerate(client.calls):
        context = decision_context(call)
        assert context["original_goal"] == "Complete the multi-step goal."
        assert context["current_step_count"] == index
        assert {"verified_observations", "available_tools"} <= set(context)


def test_b_file_search_read_final_uses_real_file_observation():
    tools = registry(
        fixture_tool(
            "filesystem_search",
            lambda query: json.dumps(
                {
                    "results": [{"path": "pyproject.toml"}],
                    "next_action": {"tool": "filesystem_read", "path": "pyproject.toml"},
                }
            ),
            properties={"query": {"type": "string"}},
            required=["query"],
        ),
        fixture_tool(
            "filesystem_read",
            lambda path: json.dumps({"path": path, "content": 'version = "1.2.3"'}),
            properties={"path": {"type": "string"}},
            required=["path"],
        ),
    )
    result, _client = run(
        [
            response([tool_block("filesystem_search", {"query": "version"}, "s")], "tool_use"),
            response([tool_block("filesystem_read", {"path": "candidate"}, "r")], "tool_use"),
            response([text_block("ready")]),
            response([text_block("The project version is 1.2.3 in pyproject.toml.")]),
        ],
        tools,
        goal="Find the project version declaration and explain it.",
    )

    assert [event["tool"] for event in result.tool_calls] == [
        "filesystem_search", "filesystem_read"
    ]
    assert result.tool_calls[1]["args"]["path"] == "pyproject.toml"
    assert result.reply == "The project version is 1.2.3 in pyproject.toml."


def test_c_web_search_fetch_final_uses_only_fetched_source():
    url = "https://example.com/research"
    tools = registry(
        fixture_tool(
            "web_search",
            lambda query: json.dumps({"query": query, "results": [{"url": url}]}),
            properties={"query": {"type": "string"}},
            required=["query"],
        ),
        fixture_tool(
            "web_fetch",
            lambda url: json.dumps(
                {"url": url, "status": "ok", "content": "Verified research finding."}
            ),
            properties={"url": {"type": "string"}},
            required=["url"],
        ),
    )
    result, _client = run(
        [
            response([tool_block("web_search", {"query": "research"}, "s")], "tool_use"),
            response([tool_block("web_fetch", {"url": url}, "f")], "tool_use"),
            response([text_block("ready")]),
            response([text_block(f"Verified research finding. Source: {url}")]),
        ],
        tools,
        goal="Research the finding and cite the source.",
    )

    assert [event["tool"] for event in result.tool_calls] == ["web_search", "web_fetch"]
    assert url in result.reply


def test_d_code_search_patch_test_diff_final():
    tools = registry(
        fixture_tool(
            "code_search", lambda query: json.dumps({"results": [{"path": "sample.py"}]}),
            properties={"query": {"type": "string"}}, required=["query"],
        ),
        fixture_tool(
            "code_patch", lambda **_args: json.dumps({"path": "sample.py", "changed": True}),
            properties={
                "path": {"type": "string"}, "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            required=["path", "old_text", "new_text"], read_only=False,
        ),
        fixture_tool(
            "shell_run",
            lambda command: json.dumps({"exit_code": 0, "stdout": "1 passed", "command": command}),
            properties={"command": {"type": "string"}}, required=["command"], read_only=False,
        ),
        fixture_tool("git_diff", lambda: json.dumps({"content": "+VALUE = 2"})),
    )
    result, _client = run(
        [
            response([tool_block("code_search", {"query": "VALUE"}, "s")], "tool_use"),
            response(
                [tool_block("code_patch", {"path": "sample.py", "old_text": "1", "new_text": "2"}, "p")],
                "tool_use",
            ),
            response([tool_block("shell_run", {"command": "pytest -q"}, "t")], "tool_use"),
            response([tool_block("git_diff", {}, "d")], "tool_use"),
            response([text_block("ready")]),
            response([text_block("sample.py changed to VALUE = 2; 1 test passed and the diff confirms it.")]),
        ],
        tools,
        goal="Find and update VALUE, run the test, inspect git_diff, then explain.",
    )

    assert [event["tool"] for event in result.tool_calls] == [
        "code_search", "code_patch", "shell_run", "git_diff"
    ]
    assert "1 test passed" in result.reply


def test_e_duplicate_normalized_call_is_blocked_before_execute():
    calls = []
    tools = registry(
        fixture_tool(
            "read_value", lambda value: calls.append(value) or json.dumps({"value": value}),
            properties={"value": {"type": "string"}}, required=["value"],
        )
    )
    result, _client = run(
        [
            response([tool_block("read_value", {"value": "same"}, "1")], "tool_use"),
            response([tool_block("read_value", {"value": "same"}, "2")], "tool_use"),
        ],
        tools,
    )

    assert calls == ["same"] and result.state["step_count"] == 1
    assert result.state["errors"][-1]["code"] == "duplicate_tool_call"


def test_f_repeated_no_progress_safe_stops():
    calls = []
    tools = registry(
        fixture_tool(
            "probe", lambda value: calls.append(value) or json.dumps({"unchanged": True}),
            properties={"value": {"type": "integer"}}, required=["value"],
        )
    )
    result, _client = run(
        [
            response([tool_block("probe", {"value": 1}, "1")], "tool_use"),
            response([tool_block("probe", {"value": 2}, "2")], "tool_use"),
            response([tool_block("probe", {"value": 3}, "3")], "tool_use"),
        ],
        tools,
    )

    assert calls == [1, 2, 3]
    assert result.state["errors"][-1]["code"] == "no_progress"


def test_g_max_steps_blocks_the_next_execution():
    calls = []
    tools = registry(
        fixture_tool(
            "step", lambda value: calls.append(value) or json.dumps({"value": value}),
            properties={"value": {"type": "integer"}}, required=["value"],
        )
    )
    result, _client = run(
        [
            response([tool_block("step", {"value": 1}, "1")], "tool_use"),
            response([tool_block("step", {"value": 2}, "2")], "tool_use"),
            response([tool_block("step", {"value": 3}, "3")], "tool_use"),
        ],
        tools,
        max_steps=2,
    )

    assert calls == [1, 2] and result.state["step_count"] == 2
    assert result.state["errors"][-1]["code"] == "max_steps"


def test_h_tool_failure_has_at_most_two_retries():
    calls = []

    def fail(value):
        calls.append(value)
        return json.dumps({"error": {"code": "fixture_failure", "retryable": True}})

    tools = registry(
        fixture_tool(
            "fragile", fail,
            properties={"value": {"type": "integer"}}, required=["value"],
        )
    )
    result, _client = run(
        [
            response([tool_block("fragile", {"value": 1}, "1")], "tool_use"),
            response([tool_block("fragile", {"value": 2}, "2")], "tool_use"),
            response([tool_block("fragile", {"value": 3}, "3")], "tool_use"),
        ],
        tools,
    )

    assert calls == [1, 2, 3]
    assert result.state["errors"][-1]["code"] == "tool_failure_limit"


def test_i_permission_deny_is_observed_and_shell_bypass_is_not_run():
    patch_calls = []
    shell_calls = []
    tools = registry(
        fixture_tool(
            "code_patch", lambda **args: patch_calls.append(args) or "wrong",
            properties={"path": {"type": "string"}}, required=["path"],
            read_only=False, default_policy="confirm",
        ),
        fixture_tool(
            "shell_run", lambda command: shell_calls.append(command) or "wrong",
            properties={"command": {"type": "string"}}, required=["command"],
            read_only=False, default_policy="confirm",
        ),
        approve=False,
    )
    result, _client = run(
        [
            response([tool_block("code_patch", {"path": "sample.py"}, "p")], "tool_use"),
            response([tool_block("shell_run", {"command": "write sample.py"}, "s")], "tool_use"),
        ],
        tools,
        goal="Change sample.py even if permission is denied.",
    )

    assert patch_calls == [] and shell_calls == []
    assert result.state["errors"][-1]["code"] == "permission_bypass_blocked"
    assert "bypass" in result.reply


def test_j_fake_observation_and_source_do_not_enter_final():
    fake = "https://fake.example/source"
    tools = registry(
        fixture_tool(
            "filesystem_read",
            lambda path: json.dumps({"path": path, "content": "verified version 4"}),
            properties={"path": {"type": "string"}}, required=["path"],
        )
    )
    result, _client = run(
        [
            response([tool_block("filesystem_read", {"path": "version.txt"}, "r")], "tool_use"),
            response([text_block(f"Version 99. Source: {fake}")]),
            response([text_block(f"Version 99. Source: {fake}")]),
            response([text_block("The verified local version is 4.")]),
        ],
        tools,
    )

    assert result.reply == "The verified local version is 4."
    assert fake not in result.reply and "99" not in result.reply


def test_only_one_tool_is_executed_per_iteration():
    calls = []
    tools = registry(
        fixture_tool("one", lambda: calls.append("one") or "one"),
        fixture_tool("two", lambda: calls.append("two") or "two"),
    )
    result, _client = run(
        [
            response([tool_block("one", {}, "1"), tool_block("two", {}, "2")], "tool_use"),
            response([text_block("safe final")]),
        ],
        tools,
    )

    assert calls == ["one"] and result.reply == "safe final"
    assert result.state["errors"][-1]["code"] == "multiple_tool_calls"


def test_global_timeout_is_a_controlled_stop():
    class SlowClient:
        def __init__(self):
            self.messages = SimpleNamespace(create=self.create)

        @staticmethod
        def create(**_kwargs):
            time.sleep(0.02)
            return response([text_block("late")])

    result = run_bounded_multi_step(
        SlowClient(),
        "model",
        "system",
        [{"role": "user", "content": "timed goal"}],
        registry(),
        task_timeout_seconds=0.005,
    )

    assert result.state["errors"][-1]["code"] == "task_timeout"
