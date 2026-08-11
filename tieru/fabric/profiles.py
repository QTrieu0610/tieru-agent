"""Immutable per-turn execution profiles layered over validated Settings."""

from __future__ import annotations

from tieru.fabric.models import ExecutionMode, ExecutionProfile

_MAX_TOKENS = 131_072
_MAX_ITERATIONS = 50
_MAX_HISTORY_TURNS = 100


def _bounded(value: int, maximum: int) -> int:
    return max(1, min(int(value), maximum))


def profiles_for(settings) -> dict[ExecutionMode, ExecutionProfile]:
    return {
        ExecutionMode.QUICK: ExecutionProfile(
            ExecutionMode.QUICK, "small",
            _bounded(settings.fabric_quick_max_tokens, _MAX_TOKENS),
            _bounded(settings.fabric_quick_max_iterations, _MAX_ITERATIONS),
            _bounded(settings.fabric_quick_history_turns, _MAX_HISTORY_TURNS),
            False, False, False, False,
        ),
        ExecutionMode.STANDARD: ExecutionProfile(
            ExecutionMode.STANDARD, "main", _bounded(settings.max_tokens, _MAX_TOKENS),
            _bounded(settings.max_iterations, _MAX_ITERATIONS),
            _bounded(settings.history_turns, _MAX_HISTORY_TURNS),
            True, True, True, False,
        ),
        ExecutionMode.AGENT: ExecutionProfile(
            ExecutionMode.AGENT, "main",
            _bounded(max(settings.max_tokens, settings.fabric_agent_max_tokens), _MAX_TOKENS),
            _bounded(
                max(settings.max_iterations, settings.fabric_agent_max_iterations),
                _MAX_ITERATIONS,
            ),
            _bounded(settings.history_turns, _MAX_HISTORY_TURNS),
            True, True, True, False,
        ),
        ExecutionMode.DEEP: ExecutionProfile(
            ExecutionMode.DEEP, "main",
            _bounded(max(settings.max_tokens, settings.fabric_deep_max_tokens), _MAX_TOKENS),
            _bounded(
                max(settings.max_iterations, settings.fabric_deep_max_iterations),
                _MAX_ITERATIONS,
            ),
            _bounded(
                max(settings.history_turns, settings.fabric_deep_history_turns),
                _MAX_HISTORY_TURNS,
            ),
            True, True, True, settings.fabric_deep_verification,
        ),
    }
