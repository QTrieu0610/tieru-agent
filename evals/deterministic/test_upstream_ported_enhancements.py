from unittest.mock import MagicMock

from tieru.loop.agent import LoopResult, run_loop
from tieru.loop.trim import chain, shrink_seen
from tieru.memory.consolidation import is_ops_state, restates
from tieru.tools.registry import Tool, ToolRegistry


def test_shrink_seen_trims_prior_large_tool_outputs():
    # Last tool message should NOT be trimmed even if long
    # Prior tool messages exceeding LONG_CHARS should be trimmed in-place
    long_text = "A" * 5000
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "search", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": long_text}],
        },
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t2", "name": "search", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t2", "content": long_text}],
        },
    ]

    shrink_seen(messages)
    # The first tool result block should be trimmed (prior tool message)
    first_tool_block = messages[2]["content"][0]["content"]
    assert len(first_tool_block) < 5000
    assert "Cut from 5,000 characters" in first_tool_block
    assert first_tool_block.startswith("A" * 2000)

    # The second (latest) tool result block must remain untouched
    second_tool_block = messages[4]["content"][0]["content"]
    assert second_tool_block == long_text


def test_chain_combiner():
    def f1(msgs):
        msgs.append({"role": "user", "content": "step1"})

    def f2(msgs):
        msgs.append({"role": "user", "content": "step2"})

    composed = chain(f1, f2)
    msgs = [{"role": "user", "content": "start"}]
    composed(msgs)
    assert len(msgs) == 3
    assert msgs[1]["content"] == "step1"
    assert msgs[2]["content"] == "step2"


def test_is_ops_state_filters_noise():
    # Noise/ops phrases should return True
    assert is_ops_state({"subject": "treg", "content": "balance has reached $0.00"})
    assert is_ops_state({"subject": "error", "content": "status 429 too many requests"})
    assert is_ops_state({"subject": "billing", "content": "HTTP 402 payment required"})
    assert is_ops_state({"subject": "usage", "content": "Remaining tokens count: 1200 prompt tokens"})
    assert is_ops_state({"subject": "provider", "content": "Rate limit exceeded"})
    assert is_ops_state({"subject": "tool", "content": "Tool failed with internal exception"})

    # Real user/project facts should return False
    assert not is_ops_state({"subject": "preference", "content": "User prefers Python over JS"})
    assert not is_ops_state({"subject": "project", "content": "User is building a CV pipeline"})
    assert not is_ops_state({"subject": "architecture", "content": "Uses PostgreSQL with pgvector"})


def test_restates_detects_duplicate_facts():
    recalled = "- Candidate has 5 years of Python experience and scored 8 points\n- Project built in 2024"

    # Duplicates with numbers that match recalled memory
    assert restates({"content": "Candidate has 5 years in Python"}, recalled)
    assert restates({"content": "Project built in 2024"}, recalled)

    # New facts (numbers not in recalled)
    assert not restates({"content": "Candidate has 10 years of backend experience"}, recalled)
    assert not restates({"content": "Uses Docker in 2025"}, recalled)


def test_final_answer_on_iteration_limit():
    client = MagicMock()
    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.id = "call_1"
    tool_use_block.name = "dummy_tool"
    tool_use_block.input = {"q": "data"}

    tool_resp = MagicMock()
    tool_resp.content = [tool_use_block]
    tool_resp.stop_reason = "tool_use"
    tool_resp.usage = MagicMock(input_tokens=10, output_tokens=10)

    final_text_block = MagicMock()
    final_text_block.type = "text"
    final_text_block.text = "Here is what I synthesized before limit."

    final_resp = MagicMock()
    final_resp.content = [final_text_block]
    final_resp.stop_reason = "end_turn"
    final_resp.usage = MagicMock(input_tokens=10, output_tokens=10)

    # First call: loop iteration 1 returns tool_use
    # Next call exceeds max_iterations=1, triggers _final_answer with tool_choice={"type": "none"}
    def mock_create(*args, **kwargs):
        if kwargs.get("tool_choice") == {"type": "none"}:
            return final_resp
        return tool_resp

    client.messages.create.side_effect = mock_create

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="dummy_tool",
            description="dummy tool",
            input_schema={
                "type": "object",
                "properties": {"q": {"type": "string"}},
            },
            fn=lambda q: "partial query result",
            default_policy="allow",
            capabilities=("lookup",),
        )
    )

    result: LoopResult = run_loop(
        client=client,
        model="claude-3-haiku-20240307",
        system="System prompt",
        messages=[{"role": "user", "content": "find something"}],
        tools=registry,
        max_iterations=1,
    )

    assert result.limit_reached is True
    assert "Here is what I synthesized before limit." in result.reply
