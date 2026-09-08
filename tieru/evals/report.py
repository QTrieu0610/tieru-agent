"""Secret-safe JSON artifacts and compact human reliability scorecards."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tieru.evals.baseline import BaselineComparison
from tieru.evals.models import EvalRun
from tieru.memory.personal import redact_secrets


def _safe(value: Any, key: str = "") -> Any:
    lowered = key.lower().replace("-", "_")
    usage_keys = {"input_tokens", "output_tokens", "total_tokens"}
    if lowered not in usage_keys and any(
        part in lowered
        for part in ("api_key", "authorization", "password", "secret", "access_token", "cookie")
    ):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe(item) for item in value]
    return value


def run_json(run: EvalRun | Mapping[str, Any]) -> str:
    value = run.to_dict() if isinstance(run, EvalRun) else dict(run)
    return json.dumps(_safe(value), indent=2, sort_keys=True, ensure_ascii=False)


def write_result(run: EvalRun, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(run_json(run) + "\n", encoding="utf-8")
    return target


def _pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.1f}%"


def render_scorecard(run: EvalRun | Mapping[str, Any]) -> str:
    value = run.to_dict() if isinstance(run, EvalRun) else dict(run)
    metrics = value.get("metrics") or {}
    is_live = str(value.get("mode") or "") == "live"
    lines = ["Tieru Live Reliability" if is_live else "Tieru M21 Reliability", "-" * 36]
    if is_live:
        lines.extend(
            [
                f"Baseline status{metrics.get('status', 'NOT_RUN')!s:>21}",
                f"Scope{metrics.get('scope', 'unknown')!s:>31}",
                f"Selected cases{int(metrics.get('selected_cases', 0)):>22}",
                f"Attempted cases{int(metrics.get('attempted_cases', 0)):>21}",
                f"Completed cases{int(metrics.get('completed_cases', 0)):>21}",
                f"Live attempt rate{_pct(metrics.get('live_case_attempt_rate')):>19}",
                f"Expected case-runs{int(metrics.get('expected_case_runs', 0)):>18}",
                f"Attempted case-runs{int(metrics.get('attempted_case_runs', 0)):>17}",
                f"Goal verification reach{_pct(metrics.get('goal_verification_reach_rate')):>13}",
                f"Pre-goal blocks{_pct(metrics.get('pre_goal_block_rate')):>20}",
                f"Unexpected Trust blocks{_pct(metrics.get('unexpected_trust_block_rate')):>12}",
            ]
        )
    for label, key in (
        ("Cases", "cases"), ("Passed", "passed"), ("Failed", "failed"),
        ("Blocked expected", "expected_blocked"),
    ):
        lines.append(f"{label:<28}{metrics.get(key, 0):>8}")
    for label, key in (
        ("Task completion", "task_completion_rate"),
        ("Expected-pass completion", "expected_pass_completion_rate"),
        ("Evidence realization", "evidence_realization_rate"),
        ("Execution contract complete", "execution_contract_completion_rate"),
        ("Verification accuracy", "verification_accuracy"),
        ("False success", "false_success_rate"),
        ("Tool selection", "tool_selection_accuracy"),
        ("Trust violations", "trust_violation_rate"),
        ("Duplicate writes", "duplicate_side_effect_rate"),
        ("Recovery success", "recovery_success_rate"),
        ("Prompt injection escapes", "prompt_injection_escape_rate"),
        ("Early model termination", "early_model_termination_rate"),
        ("Continuation turns", "continuation_turn_rate"),
        ("Continuation success", "continuation_success_rate"),
        ("Token telemetry coverage", "token_telemetry_coverage"),
        ("Skill Recall@1", "skill_recall_at_1"),
        ("Skill Recall@2", "skill_recall_at_2"),
    ):
        lines.append(f"{label:<28}{_pct(metrics.get(key)):>8}")
    if is_live:
        lines.extend(
            [
                f"Known input tokens{int(metrics.get('known_input_tokens', 0)):>18}",
                f"Known output tokens{int(metrics.get('known_output_tokens', 0)):>17}",
                f"Average duration (s){float(metrics.get('average_duration_seconds', 0.0)):>15.2f}",
                f"P95 duration (s){float(metrics.get('p95_duration_seconds', 0.0)):>19.2f}",
                f"Execution turns/step{float(metrics.get('average_execution_turns_per_step', 1.0)):>16.2f}",
                "",
                "Completion funnel:",
                f"- contract created: {int(metrics.get('contract_created_count', 0))}",
                f"- plan created: {int(metrics.get('plan_created_count', 0))}",
                f"- plan evidence valid: {int(metrics.get('plan_evidence_valid_count', 0))}",
                f"- execution started: {int(metrics.get('execution_started_count', 0))}",
                f"- model turn completed: {int(metrics.get('model_turn_completed_count', 0))}",
                f"- required evidence partial: {int(metrics.get('required_evidence_partial_count', 0))}",
                f"- required evidence complete: {int(metrics.get('required_evidence_complete_count', 0))}",
                f"- step ready to verify: {int(metrics.get('step_ready_to_verify_count', 0))}",
                f"- step verification: {int(metrics.get('step_verification_reached_count', 0))}",
                f"- recoverable failure: {int(metrics.get('recoverable_failure_count', 0))}",
                f"- recovery replan applied: {int(metrics.get('recovery_replans_applied_count', 0))}",
                f"- goal verification started: {int(metrics.get('goal_verification_started_count', 0))}",
                f"- goal verification recorded: {int(metrics.get('goal_verification_recorded_count', 0))}",
                f"- task completed: {int(metrics.get('task_completed_count', 0))}",
            ]
        )
    lines.append(f"Reliability gates{'PASS' if value.get('reliability_pass') else 'FAIL':>19}")
    categories = value.get("categories") or {}
    if categories:
        lines.extend(["", "Categories:"])
        for name, category in sorted(categories.items()):
            lines.append(
                f"- {name}: completion={_pct(category.get('task_completion_rate'))}, "
                f"tool_selection={_pct(category.get('tool_selection_accuracy'))}"
            )
    failures = value.get("failures") or {}
    if failures:
        lines.extend(["", "Failures:"])
        lines.extend(f"- {name}: {count}" for name, count in sorted(failures.items()))
    return "\n".join(lines)


def render_comparison(comparison: BaselineComparison) -> str:
    lines = [
        "Metric                       baseline    current      delta",
        "-" * 62,
    ]
    for row in comparison.rows:
        lines.append(
            f"{row['metric']:<29}{float(row['baseline']):>10.3f}"
            f"{float(row['current']):>11.3f}{float(row['delta']):>11.3f}"
        )
    lines.append(f"Regression gates: {'PASS' if comparison.passed else 'FAIL'}")
    lines.extend(f"- {item}" for item in comparison.regressions)
    return "\n".join(lines)
