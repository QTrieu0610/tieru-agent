"""Collect bounded evaluation evidence from Replay and durable SQLite state."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from tieru.evals.models import EvalEvidence, FailureStage
from tieru.memory.personal import redact_secrets


def snapshot_files(workspace: Path) -> dict[str, str]:
    """Return content hashes only; artifacts never need raw file content."""
    result: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(workspace).as_posix()
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def collect_evidence(
    *,
    conn,
    replay,
    run_ids: tuple[str, ...],
    task_id: str,
    workspace: Path,
    before_files: dict[str, str],
    selected_skills: tuple[str, ...] = (),
    final_output: str = "",
    duration_ms: int = 0,
    model_calls: int = 0,
    goal_verification_eligible: bool = False,
) -> EvalEvidence:
    task_row = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    task_status = str(task_row["status"]) if task_row is not None else None
    steps = tuple(
        dict(row) for row in conn.execute(
            """SELECT position, title, instruction, verification_instruction,
                      status, attempt_count, verification_status,
                      verification_summary, execution_run_id
               FROM task_steps WHERE task_id=? ORDER BY position""",
            (task_id,),
        ).fetchall()
    )
    events: list[dict[str, Any]] = []
    for run_id in run_ids:
        try:
            events.extend(replay.get_events(run_id))
        except (KeyError, ValueError):
            continue
    trust = tuple(
        event for event in events if event.get("event_type") == "trust_decision"
    )
    tool_requests = tuple(
        event for event in events if event.get("event_type") == "tool_requested"
    )
    tool_calls = tuple(
        event for event in events
        if event.get("category") == "tool"
        and event.get("event_type") in {
            "tool_completed", "tool_failed", "tool_denied", "tool_idempotency_hit",
            "tool_execution_in_progress", "tool_execution_uncertain",
        }
    )
    actions = tuple(
        dict(row) for row in conn.execute(
            """SELECT action_fingerprint, tool_name, status, attempt_count,
                      completion_source, retryable
               FROM tool_executions ORDER BY started_at, action_fingerprint"""
        ).fetchall()
    )
    verification = tuple(
        {
            "position": int(step["position"]),
            "status": step.get("verification_status"),
            "summary": redact_secrets(str(step.get("verification_summary") or "")),
        }
        for step in steps
        if step.get("verification_status") is not None
    )
    after_files = snapshot_files(workspace)
    changed = tuple(
        sorted(path for path in set(before_files) | set(after_files)
               if before_files.get(path) != after_files.get(path))
    )
    retries = sum(max(0, int(item.get("attempt_count") or 0) - 1) for item in actions)
    recovery_count = sum(1 for event in events if event.get("category") == "recovery")
    revisions: tuple[dict[str, Any], ...] = ()
    table_names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "task_plan_revisions" in table_names:
        revisions = tuple(
            dict(row)
            for row in conn.execute(
                """SELECT revision_id, task_id, revision_number, reason, trigger_step_id, created_at
                   FROM task_plan_revisions WHERE task_id=? ORDER BY revision_number""",
                (task_id,),
            ).fetchall()
        )
    replan_count = sum(1 for r in revisions if int(r.get("revision_number", 0)) > 0)
    replan_limit_blocked = any(
        event.get("event_type") == "task_replan_blocked"
        and (event.get("safe_payload") or {}).get("reason") == "replan_limit_exceeded"
        for event in events
    )
    goal_contract: dict[str, Any] | None = None
    checkpoints: tuple[dict[str, Any], ...] = ()
    if "task_execution_checkpoints" in table_names:
        checkpoints = tuple(
            dict(row)
            for row in conn.execute(
                """SELECT checkpoint_id, task_id, step_id, kind, source, evidence_hash,
                          created_at, run_id, tool_name, exit_code, timed_out, duration_ms,
                          path, before_hash, after_hash, exists_flag, action_ledger_id,
                          consumed_by_verifier, summary
                   FROM task_execution_checkpoints WHERE task_id=? ORDER BY created_at ASC""",
                (task_id,),
            ).fetchall()
        )
    if "task_goal_contracts" in table_names:
        c_row = conn.execute(
            "SELECT contract_id, task_id, goal, constraints_json, created_at FROM task_goal_contracts WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if c_row is not None:
            goal_contract = dict(c_row)

    goal_verifications: tuple[dict[str, Any], ...] = ()
    if "task_goal_verifications" in table_names:
        goal_verifications = tuple(
            dict(row)
            for row in conn.execute(
                """SELECT verification_id, task_id, status, summary, plan_revision_number, evidence_hash, created_at
                   FROM task_goal_verifications WHERE task_id=? ORDER BY rowid ASC""",
                (task_id,),
            ).fetchall()
        )
    goal_verification_status = (
        str(goal_verifications[-1]["status"]) if goal_verifications else None
    )
    capability_events = [
        e for e in events
        if e.get("event_type") == "capability_routed"
        or (e.get("category") == "routing" and e.get("event_type") == "capability_routed")
    ]
    visible_tools = tuple(dict.fromkeys(
        str(tool)
        for e in capability_events
        for tool in (e.get("safe_payload", {}).get("selected_tools") or [])
    ))
    routed_capabilities = tuple(dict.fromkeys(
        str(cap)
        for e in capability_events
        for cap in (e.get("safe_payload", {}).get("selected_capabilities") or [])
    ))
    candidate_tool_count = max(
        (int((e.get("safe_payload", {}).get("candidate_count") or 0)) for e in capability_events),
        default=0,
    )
    input_tokens = None
    output_tokens = None
    active_runtime_seconds = 0.0
    command_runtime_seconds = 0.0
    if "task_budget_usages" in table_names:
        u_row = conn.execute(
            """SELECT input_tokens, output_tokens, model_calls, retries,
                      active_runtime_seconds, command_runtime_seconds
               FROM task_budget_usages WHERE task_id=?""",
            (task_id,),
        ).fetchone()
        if u_row is not None:
            raw_in = u_row["input_tokens"]
            raw_out = u_row["output_tokens"]
            raw_models = u_row["model_calls"]
            if raw_in is not None and raw_in > 0:
                input_tokens = int(raw_in)
            if raw_out is not None and raw_out > 0:
                output_tokens = int(raw_out)
            if model_calls == 0 and raw_models is not None and raw_models > 0:
                model_calls = int(raw_models)
            retries = max(retries, int(u_row["retries"] or 0))
            active_runtime_seconds = float(u_row["active_runtime_seconds"] or 0.0)
            command_runtime_seconds = float(u_row["command_runtime_seconds"] or 0.0)
    budget_events = [
        event
        for event in events
        if event.get("event_type") == "task_budget_exhausted"
    ]
    budget_resource = None
    if budget_events:
        payload = budget_events[-1].get("safe_payload") or {}
        budget_resource = str(payload.get("resource") or payload.get("code") or "unknown")
    if budget_resource is None:
        for step in reversed(steps):
            summary = str(step.get("verification_summary") or "")
            if "budget_exhausted:" in summary:
                budget_resource = summary.split("budget_exhausted:", 1)[1].split()[0].rstrip(".,;:")
                break
    event_types = {str(event.get("event_type") or "") for event in events}
    goal_recorded = bool(goal_verifications)
    goal_started = ("goal_verification_started" in event_types) or goal_recorded
    m_contract = 0
    m_planner = 0
    m_agent = 0
    m_step_ver = 0
    m_replanner = 0
    m_goal_ver = 0
    m_other = 0
    for event in events:
        if event.get("event_type") == "model_call_started":
            payload = event.get("safe_payload") or {}
            phase = str(payload.get("phase") or "")
            role = str(payload.get("role") or event.get("role") or "")
            if phase in {"contract_builder", "contract"} or role == "contract":
                m_contract += 1
            elif phase in {"planner", "task_planner"} or role == "planner":
                m_planner += 1
            elif phase in {"step_verifier", "task_verifier"}:
                m_step_ver += 1
            elif phase in {"task_plan_reviewer", "reviewer", "replanner"}:
                m_replanner += 1
            elif phase in {"goal_judge", "goal_verifier"}:
                m_goal_ver += 1
            elif phase in {"turn", "agent", "final_synthesis"} or role in {"main", "agent"}:
                m_agent += 1
            else:
                m_other += 1
    if (m_contract + m_planner + m_agent + m_step_ver + m_replanner + m_goal_ver + m_other) == 0 and model_calls > 0:
        m_agent = model_calls

    step_v_det = False
    step_v_sem = False
    step_fb_used = False
    step_fb_succ = False
    step_insufficient = False
    det_eligible = False

    for s in steps:
        v_status = str(s.get("verification_status") or "").lower()
        v_summary = str(s.get("verification_summary") or "").lower()
        instr = f"{s.get('title', '')} {s.get('instruction', '')}".lower()
        if any(k in instr for k in ("read", "inspect", "check file", "write", "create", "modify", "edit", "run", "pytest", "command")):
            det_eligible = True
        if v_status == "unknown" or "insufficient" in v_summary:
            step_insufficient = True
        if (
            "[deterministic]" in v_summary
            or "step_verification_deterministic" in event_types
            or (v_status == "pass" and any(e.get("event_type") == "tool_completed" for e in events))
        ):
            step_v_det = True
        if "[semantic]" in v_summary or "step_verification_semantic" in event_types:
            step_v_sem = True
        if "fallback" in v_summary or "semantic_verifier_unavailable" in v_summary or "step_verification_fallback_used" in event_types:
            step_fb_used = True
            if v_status == "pass":
                step_fb_succ = True

    det_avoided = det_eligible and not step_v_sem
    denied = any(
        event.get("event_type") == "trust_decision"
        and not bool((event.get("safe_payload") or {}).get("allowed"))
        for event in events
    )
    tool_failed = any(
        event.get("event_type") in {
            "tool_failed",
            "tool_execution_failed",
            "tool_execution_uncertain",
            "tool_execution_in_progress",
        }
        for event in events
    )
    terminal_stage: FailureStage | None = None
    if budget_resource is not None:
        terminal_stage = FailureStage.BUDGET
    elif goal_recorded or goal_started:
        terminal_stage = FailureStage.GOAL_VERIFICATION
    elif denied:
        terminal_stage = FailureStage.TRUST
    elif tool_failed:
        terminal_stage = FailureStage.TOOL_EXECUTION
    elif verification:
        terminal_stage = FailureStage.STEP_VERIFICATION
    elif tool_requests:
        terminal_stage = FailureStage.TOOL_SELECTION
    elif steps or goal_contract is not None:
        terminal_stage = FailureStage.PLANNING
    else:
        terminal_stage = FailureStage.CONTRACT

    terminal_reason = ""
    if verification:
        terminal_reason = str(verification[-1].get("summary") or "")
    if budget_resource is not None:
        terminal_reason = f"budget_exhausted:{budget_resource}"
    terminal_path = {
        "contract_created": goal_contract is not None,
        "plan_created": bool(steps),
        "step_claimed": "task_step_claimed" in event_types,
        "capabilities_selected": "capability_routed" in event_types,
        "model_requested_tool": bool(tool_requests),
        "trust_decision": bool(trust),
        "tool_execution": bool(tool_calls),
        "step_verification": bool(verification),
        "replan": replan_count > 0,
        "goal_verification": goal_recorded,
        "terminal_state": task_status,
    }
    return EvalEvidence(
        task_status=task_status,
        steps=steps,
        tool_calls=tool_calls,
        trust_decisions=trust,
        action_executions=actions,
        replay_events=tuple(events),
        selected_skills=selected_skills,
        verification_results=verification,
        artifacts=tuple(sorted(after_files)),
        changed_artifacts=changed,
        checkpoints=checkpoints,
        final_output=redact_secrets(final_output)[:8192],
        duration_ms=max(0, int(duration_ms)),
        model_calls=max(0, int(model_calls)),
        tool_call_count=len(tool_calls),
        retry_count=retries,
        recovery_count=recovery_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        plan_revisions=revisions,
        replan_count=replan_count,
        replan_limit_blocked=replan_limit_blocked,
        goal_contract=goal_contract,
        goal_verifications=goal_verifications,
        goal_verification_status=goal_verification_status,
        visible_tools=visible_tools,
        routed_capabilities=routed_capabilities,
        candidate_tool_count=candidate_tool_count,
        task_id=task_id,
        replay_run_ids=run_ids,
        active_runtime_seconds=active_runtime_seconds,
        command_runtime_seconds=command_runtime_seconds,
        budget_exhausted=budget_resource is not None,
        budget_exhausted_resource=budget_resource,
        planned_steps=tuple(
            {
                "position": int(step.get("position") or 0),
                "title": redact_secrets(str(step.get("title") or ""))[:256],
                "instruction": redact_secrets(str(step.get("instruction") or ""))[:4096],
                "verification_instruction": redact_secrets(
                    str(step.get("verification_instruction") or "")
                )[:4096],
            }
            for step in steps
        ),
        tool_requests=tool_requests,
        terminal_path=terminal_path,
        terminal_stage=terminal_stage,
        terminal_reason=redact_secrets(terminal_reason)[:512],
        goal_verification_eligible=bool(goal_verification_eligible),
        goal_verification_started=goal_started,
        goal_verification_recorded=goal_recorded,
        model_calls_contract=m_contract,
        model_calls_planner=m_planner,
        model_calls_agent=m_agent,
        model_calls_step_verifier=m_step_ver,
        model_calls_replanner=m_replanner,
        model_calls_goal_verifier=m_goal_ver,
        model_calls_other=m_other,
        step_verification_deterministic=step_v_det,
        step_verification_semantic=step_v_sem,
        offline_fallback_used=step_fb_used,
        offline_fallback_success=step_fb_succ,
        step_insufficient_evidence=step_insufficient,
        deterministic_eligible=det_eligible,
        deterministic_avoided_model_call=det_avoided,
        plan_evidence_valid=not any("plan_evidence_gap" in str(s.get("verification_summary", "")) for s in steps),
        required_evidence_produced=(
            bool(steps)
            and all(s.get("status") in {"succeeded", "skipped"} for s in steps)
            and not any("Required observable evidence missing" in str(s.get("verification_summary", "")) for s in steps)
        ),
        evidence_corrections_attempted=sum(1 for e in events if e.get("event_type") == "evidence_correction_started"),
        evidence_corrections_succeeded=sum(1 for e in events if e.get("event_type") == "evidence_correction_completed"),
        required_tool_omissions=sum(
            1
            for e in events
            if e.get("event_type") == "evidence_correction_started"
            and "required_tool_not_invoked" in str((e.get("safe_payload") or {}).get("reason", ""))
        ),
        plan_capability_mismatches=sum(
            1
            for e in events
            if "plan_capability_mismatch" in str(e.get("event_type", ""))
            or "plan_capability_mismatch" in str((e.get("safe_payload") or {}).get("reason", ""))
        ),
        execution_contract_complete=(
            bool(steps)
            and all(s.get("status") in {"succeeded", "skipped"} for s in steps)
            and not any(e.get("event_type") in {"step_continuation_exhausted", "evidence_correction_blocked"} for e in events)
        ),
        early_model_termination=any(e.get("event_type") == "step_continuation_requested" for e in events),
        partial_evidence_observed=any(e.get("event_type") == "step_evidence_partial" for e in events),
        continuation_turns=sum(1 for e in events if e.get("event_type") == "step_continuation_requested"),
        continuation_succeeded=any(
            e.get("event_type") == "step_evidence_complete"
            and int((e.get("safe_payload") or {}).get("turn_number") or 1) > 1
            for e in events
        ),
        continuation_exhausted=any(e.get("event_type") == "step_continuation_exhausted" for e in events),
        evidence_requirements_total=max(
            len(steps),
            sum(
                int((e.get("safe_payload") or {}).get("satisfied_count") or 0)
                + int((e.get("safe_payload") or {}).get("missing_count") or 0)
                for e in events
                if e.get("event_type") == "step_completion_assessed"
            ),
        ),
        evidence_requirements_satisfied=sum(
            int((e.get("safe_payload") or {}).get("satisfied_count") or 0)
            for e in events
            if e.get("event_type") in {"step_evidence_complete", "step_completion_assessed"}
        ) if any(e.get("event_type") == "step_completion_assessed" for e in events) else (
            len(steps) if all(s.get("status") in {"succeeded", "skipped"} for s in steps) else 0
        ),
        total_execution_turns=max(1, len(steps) + sum(1 for e in events if e.get("event_type") == "step_continuation_requested")),
        recoverable_failures=sum(
            1 for e in events
            if e.get("event_type") == "step_failure_classified"
            and (e.get("safe_payload") or {}).get("is_recoverable")
        ),
        recovery_replans_requested=sum(1 for e in events if e.get("event_type") == "task_recovery_replan_requested"),
        recovery_replans_applied=sum(1 for e in events if e.get("event_type") == "task_recovery_replan_applied"),
        recovery_strategies_rejected=sum(1 for e in events if e.get("event_type") == "task_recovery_strategy_rejected"),
        recovery_exhausted=any(e.get("event_type") == "task_recovery_exhausted" for e in events),
    )
