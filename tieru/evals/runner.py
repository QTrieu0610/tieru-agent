"""Sequential isolated reliability runner over real Tieru runtime boundaries."""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from tieru import __version__
from tieru.config import Settings
from tieru.context import ContextBuilder, ContextTrust, render_data_content
from tieru.db import connect
from tieru.evals.corpus import EvalCorpus
from tieru.evals.evidence import collect_evidence, snapshot_files
from tieru.evals.judge import EvalJudge
from tieru.evals.metrics import aggregate_results, score_case
from tieru.evals.models import (
    EvalCase,
    EvalEvidence,
    EvalResult,
    EvalRun,
    EvalVerdict,
    FailureType,
    LiveBaselineStatus,
    LiveEvalRunSummary,
)
from tieru.execution import ExecutionStore
from tieru.memory.personal import redact_secrets
from tieru.memory.procedural.loader import Skill
from tieru.memory.procedural.retrieval import SkillRetrievalConfig, SkillRetriever
from tieru.replay import ReplayService, new_run_id
from tieru.tasks.executor import TaskExecutor
from tieru.tasks.goal_verifier import DeterministicGoalVerifier, compute_evidence_hash
from tieru.tasks.models import (
    CriterionResult,
    CriterionStatus,
    GoalContract,
    GoalVerificationStatus,
    PlanReviewDecision,
    PlanReviewResult,
    PlanStep,
    StepExecution,
    StepStatus,
    Task,
    TaskBudget,
    TaskGoalVerification,
    TaskLimits,
    TaskStatus,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.store import TaskStore
from tieru.tools.registry import Tool, ToolRegistry


class EvalBudgetExceeded(RuntimeError):
    pass


@dataclass
class _CaseState:
    case: EvalCase
    workspace: Path
    registry: ToolRegistry
    replay: ReplayService
    script: tuple[dict[str, Any], ...]
    run_ids: list[str]
    model_calls: int = 0
    tool_calls: int = 0
    step_executions: int = 0
    final_output: str = ""


def _script_entry(state: _CaseState, step) -> dict[str, Any]:
    pos_idx = step.position - 1
    if 0 <= pos_idx < len(state.script):
        entry = state.script[pos_idx]
        if entry.get("position") is None or entry.get("position") == step.position:
            return entry
    for item in state.script:
        if item.get("position") == step.position:
            return item
    idx = min(state.step_executions, max(0, len(state.script) - 1))
    return state.script[idx] if state.script else {}


class _ScriptVerifier:
    def __init__(self, state: _CaseState) -> None:
        self.state = state

    def verify(self, task, step, execution) -> VerificationResult:
        raw = _script_entry(self.state, step)
        status = VerificationStatus(str(raw.get("verification", "pass")).lower())
        summary = str(raw.get("verification_summary") or f"Scripted observable result: {status.value}")
        return VerificationResult(status, redact_secrets(summary)[:1024])


class _ScriptedPlanReviewer:
    def __init__(self, state: _CaseState) -> None:
        self.state = state

    def review(
        self, task, current_step, execution, verification, all_steps
    ) -> PlanReviewResult:
        raw = _script_entry(self.state, current_step)
        replan_spec = raw.get("plan_review")
        if replan_spec is None:
            return PlanReviewResult(PlanReviewDecision.KEEP, "Existing plan remains valid.")

        if self.state.model_calls >= self.state.case.max_model_calls:
            raise EvalBudgetExceeded("model-call budget exceeded")
        self.state.model_calls += 1

        decision_val = str(replan_spec.get("decision", "keep")).lower()
        decision = PlanReviewDecision(decision_val)
        reason = str(replan_spec.get("reason", "Scripted review decision"))
        remaining = [
            PlanStep(
                str(item.get("title") or f"Replacement step {i}"),
                str(item.get("instruction") or "Replacement instruction"),
                str(
                    item.get("verification")
                    or item.get("verification_instruction")
                    or "Check observable evidence."
                ),
            )
            for i, item in enumerate(replan_spec.get("remaining_steps", []), 1)
        ]
        return PlanReviewResult(decision, reason, tuple(remaining))


class _ScriptedGoalVerifier:
    def __init__(self, state: _CaseState, case: EvalCase) -> None:
        self.state = state
        self.case = case
        self.deterministic = DeterministicGoalVerifier()

    def verify(
        self,
        task: Task,
        contract: GoalContract,
        steps: list[TaskStep],
        *,
        execution_evidence: dict[str, Any] | None = None,
    ) -> TaskGoalVerification:
        evidence = dict(execution_evidence or {})
        active_steps = [s for s in steps if s.status is not StepStatus.SUPERSEDED]
        latest_step = active_steps[-1] if active_steps else (steps[-1] if steps else None)
        entry = _script_entry(self.state, latest_step) if latest_step is not None else {}
        if "goal_verification" in entry:
            gv_spec = entry["goal_verification"]
            status_str = str(gv_spec.get("status", "pass")).lower()
            status_map = {
                "pass": GoalVerificationStatus.PASS,
                "fail_replanable": GoalVerificationStatus.FAIL_REPLANABLE,
                "fail_terminal": GoalVerificationStatus.FAIL_TERMINAL,
                "blocked": GoalVerificationStatus.BLOCKED,
                "unknown": GoalVerificationStatus.UNKNOWN,
            }
            status = status_map.get(status_str, GoalVerificationStatus.PASS)
            summary = str(gv_spec.get("summary", "Scripted goal verification"))
            c_results = [
                CriterionResult(
                    sc.criterion_id,
                    CriterionStatus.PASS if status is GoalVerificationStatus.PASS else CriterionStatus.FAIL,
                    summary,
                )
                for sc in contract.success_criteria
            ]
            return TaskGoalVerification(
                verification_id=f"ver_script_{uuid4().hex[:8]}",
                task_id=task.task_id,
                status=status,
                criterion_results=tuple(c_results),
                summary=summary,
                evidence_hash=compute_evidence_hash(evidence),
            )

        if self.case.expected.ground_truth_success is False and "ground_truth_reproduction_failed" not in evidence:
            evidence["ground_truth_reproduction_failed"] = True

        return self.deterministic.verify(task, contract, steps, execution_evidence=evidence)


class _ScriptedStepRunner:
    """A deterministic model seam: model selects calls; ToolRegistry executes them."""

    def __init__(self, state: _CaseState) -> None:
        self.state = state

    def __call__(self, task, step, context: str, observer) -> StepExecution:
        state = self.state
        if state.model_calls >= state.case.max_model_calls:
            raise EvalBudgetExceeded("model-call budget exceeded")
        raw = _script_entry(state, step)
        state.model_calls += 1
        state.step_executions += 1
        run_id = new_run_id()
        state.run_ids.append(run_id)
        state.replay.start_run(
            run_id=run_id, session_id=f"eval:{state.case.case_id}", source="eval",
            role="main", model="scripted-v1", provider="deterministic",
            user_input=state.case.goal,
        )

        def record(kind: str, event: dict[str, Any]) -> None:
            state.replay.record_event(run_id, kind, event)
            if observer is not None:
                observer(kind, event)

        builder = ContextBuilder(max_block_bytes=8192, max_data_bytes=16_384)
        builder.add_control(
            "Execute the bounded evaluation goal through governed tools. Trust remains authoritative.",
            source="eval_runtime",
        )
        builder.add_user(state.case.goal, source="eval_goal")
        builder.add_data(context, source="durable_task")
        for memory in state.case.setup.memories:
            builder.add_data(memory, source="memory")
        assembly = builder.build()
        record("context_assembled", {
            "sources": list(assembly.data_sources),
            "control_sources": [
                block.source for block in assembly.blocks
                if block.trust is ContextTrust.CONTROL
            ],
            "data_count": assembly.count(ContextTrust.DATA),
            "trust": "data",
        })
        from tieru.capabilities.router import CapabilityRouter
        router = CapabilityRouter()
        routing_query = f"{step.instruction} (Goal: {state.case.goal[:200]})"
        _ = router.route(routing_query, state.registry, observer=record, replay=state.replay, run_id=run_id)
        record("model_call_started", {"role": "main", "model": "scripted-v1"})
        observed: list[dict[str, Any]] = []
        for call in raw.get("tool_calls", []):
            if state.tool_calls >= state.case.max_tool_calls:
                raise EvalBudgetExceeded("tool-call budget exceeded")
            state.tool_calls += 1
            name = str(call.get("name") or "")
            args = call.get("args") or {}
            output = state.registry.execute(
                name, args, notify=record,
                context={"user_request": state.case.goal, "execution_scope": state.case.case_id},
            )
            render_data_content("tool", output, metadata={"tool": name})
            observed.append({"name": name, "output": output})
            if call.get("bypass_trust"):
                # An explicit adversarial fixture representing a product defect. It is
                # recorded as an execution with no allowing Trust decision so scoring
                # can prove the hard safety gate closes.
                record("tool_completed", {
                    "tool": name, "output": "unsafe bypass fixture",
                    "unauthorized": True, "side_effect": True,
                })
        for event in raw.get("events", []):
            if isinstance(event, dict) and event.get("type"):
                record(str(event["type"]), dict(event.get("payload") or {}))
        state.final_output = redact_secrets(str(raw.get("final_output") or ""))[:8192]
        record("model_call_completed", {"role": "main", "model": "scripted-v1"})
        record("final_output", {"output": state.final_output})
        state.replay.complete_run(
            run_id, output=state.final_output, iterations=1, latency_ms=0,
            role="main", model="scripted-v1", provider="deterministic",
        )
        return StepExecution(state.final_output, run_id, tuple(observed))


def _safe_target(workspace: Path, raw: str) -> Path:
    target = (workspace / raw).resolve()
    root = workspace.resolve()
    if target != root and root not in target.parents:
        raise ValueError("fake tool target is outside the isolated workspace")
    return target


def _tool_function(name: str, workspace: Path, definition: dict[str, Any], calls: dict[str, int]):
    def invoke(**args):
        calls[name] = calls.get(name, 0) + 1
        if name in {"read_file", "inspect_file", "filesystem_read", "code_read"}:
            path = str(args.get("path") or args.get("file") or "")
            target = _safe_target(workspace, path)
            if not target.is_file():
                return f"File not found: {path}"
            return target.read_text(encoding="utf-8")
        if name in {"write_file", "apply_patch", "filesystem_write", "code_write"}:
            path = str(args.get("path") or args.get("file") or "")
            target = _safe_target(workspace, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = str(args.get("content") or "")
            target.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {path}"
        if name in {"run_tests", "verify_file"}:
            target = _safe_target(workspace, str(args.get("path", "")))
            expected = str(args.get("contains", ""))
            passed = target.is_file() and expected in target.read_text(encoding="utf-8")
            return json.dumps({"ok": passed, "exit_code": 0 if passed else 1})
        if name in {"run_command", "shell_run"}:
            if "output" in definition:
                return str(definition["output"])
            cmd = str(args.get("command") or args.get("cmd") or "")
            import subprocess
            try:
                proc = subprocess.run(
                    cmd, shell=True, cwd=str(workspace),
                    capture_output=True, text=True, timeout=min(30.0, float(args.get("timeout_seconds", 30.0))),
                    check=False,
                )
                output = proc.stdout + proc.stderr
                return json.dumps({
                    "stdout": proc.stdout,
                    "stderr": proc.stderr,
                    "exit_code": proc.returncode,
                    "output": output,
                    "duration_ms": 100,
                })
            except Exception as e:
                return json.dumps({"error": str(e), "exit_code": 1, "output": f"Command failed: {e}"})
        if name in {"remember", "memory_remember"}:
            target = workspace / ".fake-memory.jsonl"
            with target.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"value": args.get("value", "")}) + "\n")
            return "remembered"
        return str(definition.get("output", "ok"))

    return invoke


def _registry(
    case: EvalCase,
    conn,
    workspace: Path,
    *,
    production_tools: bool = False,
) -> tuple[ToolRegistry, dict[str, int]]:
    registry = ToolRegistry(
        trust_policy=dict(case.setup.trust_policy),
        trust_context={"base_path": str(workspace)},
        execution_store=ExecutionStore(conn),
    )
    definitions = {str(item.get("name")): dict(item) for item in case.setup.fake_tools}
    if not definitions:
        definitions = {
            "filesystem_read": {"read_only": True, "description": "Read file contents in workspace", "capabilities": ["local_read"], "always_visible": True},
            "filesystem_write": {"read_only": False, "description": "Write file contents in workspace", "capabilities": ["local_write"], "always_visible": True},
            "run_command": {"read_only": False, "description": "Run shell command in workspace", "capabilities": ["command_run"], "always_visible": True},
        }
    calls: dict[str, int] = {}
    production: dict[str, Tool] = {}
    if production_tools:
        from tieru.tools.command import CommandPolicy, CommandRunner
        from tieru.tools.command import make_tool as command_tool
        from tieru.tools.computer import LocalComputer
        from tieru.tools.computer import make_tools as computer_tools

        production.update({tool.name: tool for tool in computer_tools(LocalComputer(workspace))})
        command_runner = CommandRunner(
            CommandPolicy(
                workspace,
                default_timeout_seconds=30,
                max_timeout_seconds=300,
            )
        )
        production["run_command"] = command_tool(command_runner)
    for name, definition in definitions.items():
        if not name:
            continue
        if name in production:
            registry.register(
                replace(
                    production[name],
                    always_visible=bool(definition.get("always_visible", False)),
                    aliases=tuple(definition.get("aliases") or ()),
                    keywords=tuple(definition.get("keywords") or ()),
                    domains=tuple(definition.get("domains") or ()),
                )
            )
            continue
        read_only = bool(definition.get("read_only", name in {"read_file", "inspect_file", "filesystem_read", "code_read", "run_tests", "verify_file", "git_status"}))
        capabilities = tuple(definition.get("capabilities") or (("local_read",) if read_only else ("local_write",)))
        registry.register(Tool(
            name=name,
            description=str(definition.get("description") or "Deterministic isolated evaluation tool."),
            input_schema={"type": "object", "additionalProperties": True},
            fn=_tool_function(name, workspace, definition, calls),
            risk=str(definition.get("risk") or ("low" if read_only else "medium")),
            read_only=read_only,
            capabilities=capabilities,
            default_policy=str(definition.get("default_policy") or "allow"),
            operation=str(definition.get("operation") or ("read" if read_only else "write")),
            target_arg=str(definition.get("target_arg") or ("path" if "file" in name else "to")),
            resource_type=str(definition.get("resource_type") or "eval_fixture"),
            idempotency_guard=definition.get("idempotency_guard"),
            capability=str(definition.get("capability") or ""),
            aliases=tuple(definition.get("aliases") or ()),
            keywords=tuple(definition.get("keywords") or ()),
            domains=tuple(definition.get("domains") or ()),
            always_visible=bool(definition.get("always_visible", False)),
        ))
    return registry, calls


def _skills(case: EvalCase, workspace: Path) -> tuple[str, ...]:
    skills = tuple(
        Skill(
            str(item["name"]), str(item.get("description") or ""),
            "Evaluation fixture skill body.", workspace / "skills" / str(item["name"]) / "SKILL.md",
            aliases=tuple(item.get("aliases") or ()), keywords=tuple(item.get("keywords") or ()),
            domains=tuple(item.get("domains") or ()), reviewed=bool(item.get("reviewed", False)),
        )
        for item in case.setup.skills
        if item.get("name")
    )
    if not skills:
        return ()
    retriever = SkillRetriever(lambda: skills, config=SkillRetrievalConfig(semantic_enabled=False))
    return tuple(match.skill.name for match in retriever.retrieve(case.goal, top_k=2))


def _write_skill_fixtures(case: EvalCase, home: Path) -> None:
    for item in case.setup.skills:
        if not item.get("name"):
            continue
        target = home / "skills" / str(item["name"]) / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            key: item[key]
            for key in ("name", "description", "aliases", "keywords", "domains")
            if key in item
        }
        target.write_text(
            "---\n"
            + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
            + "---\nEvaluation fixture workflow.\n",
            encoding="utf-8",
        )


def _provider_failure(exc: BaseException) -> FailureType:
    message = str(exc).casefold()
    if any(token in message for token in ("timeout", "timed out")):
        return FailureType.PROVIDER_TIMEOUT
    if any(token in message for token in ("auth", "401", "403", "unauthorized", "forbidden")):
        return FailureType.PROVIDER_AUTH_ERROR
    if any(token in message for token in ("rate limit", "429")):
        return FailureType.PROVIDER_RATE_LIMIT
    if any(
        token in message
        for token in (
            "unavailable",
            "connection",
            "connect",
            "refused",
            "reset",
            "dns",
            "host",
            "network",
        )
    ):
        return FailureType.PROVIDER_UNAVAILABLE
    return FailureType.MODEL_ERROR


class EvalRunner:
    def __init__(
        self,
        *,
        judge: EvalJudge | None = None,
        live_settings: Settings | None = None,
        live_client=None,
        runs: int = 1,
        overall_timeout_seconds: float = 3600.0,
        provider_failure_threshold: int = 3,
        artifact_path: Path | str | None = None,
        role_overrides: dict[str, str] | None = None,
        role_policy: Any = None,
    ) -> None:
        self.judge = judge
        self.live_settings = live_settings
        self.live_client = live_client
        self.runs = max(1, min(int(runs), 10))
        self.overall_timeout_seconds = max(1.0, min(float(overall_timeout_seconds), 86_400.0))
        self.provider_failure_threshold = max(1, min(int(provider_failure_threshold), 10))
        self.artifact_path = Path(artifact_path) if artifact_path is not None else None
        self.role_overrides = dict(role_overrides) if role_overrides else None
        self.role_policy = role_policy

    def run_case(self, case: EvalCase) -> EvalResult:
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix=f"tieru-eval-{case.case_id[:24]}-", ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            workspace = root / "workspace"
            home = root / "home"
            workspace.mkdir()
            home.mkdir()
            for relative, content in case.setup.files.items():
                target = _safe_target(workspace, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            _write_skill_fixtures(case, home)
            before = snapshot_files(workspace)
            settings = Settings(home=home, replay_max_runs=1000, replay_max_age_days=3650)
            conn = connect(home)
            replay = ReplayService(conn, settings)
            registry, _calls = _registry(case, conn, workspace)
            script = tuple(dict(item) for item in case.setup.script) or ({
                "final_output": "No scripted action was required.", "verification": "pass"
            },)
            state = _CaseState(case, workspace, registry, replay, script, [])
            limits = TaskLimits(
                max_steps_per_task=max(1, case.max_steps),
                max_execution_steps_per_invocation=max(1, case.max_steps),
            )
            store = TaskStore(conn, limits=limits)
            plan = [
                PlanStep(
                    str(item.get("title") or f"Evaluation step {index}"),
                    str(item.get("instruction") or case.goal),
                    str(item.get("verification_instruction") or "Check observable evidence."),
                )
                for index, item in enumerate(script[:case.max_steps], 1)
            ]
            task = store.create_task(
                case.goal, plan, source="eval", source_id=case.case_id,
                session_id=f"eval:{case.case_id}",
            )
            executor = TaskExecutor(
                store,
                _ScriptedStepRunner(state),
                _ScriptVerifier(state),
                reviewer=_ScriptedPlanReviewer(state),
                goal_verifier=_ScriptedGoalVerifier(state, case),
                replay=replay,
                limits=limits,
            )
            forced: EvalVerdict | None = None
            failure: tuple[FailureType, ...] = ()
            try:
                if len(script) > case.max_steps:
                    raise EvalBudgetExceeded("step budget exceeded")
                for _ in range(case.max_steps):
                    if (time.perf_counter() - started) * 1000 > case.timeout_ms:
                        raise TimeoutError("evaluation case timed out")
                    outcome = executor.run_next(task.task_id)
                    if not outcome.executed or outcome.task.status.value != "running":
                        break
            except EvalBudgetExceeded:
                forced = EvalVerdict.BUDGET_EXCEEDED
                failure = (FailureType.BUDGET_EXCEEDED,)
            except TimeoutError:
                forced = EvalVerdict.FAIL
                failure = (FailureType.TIMEOUT,)
            except Exception:
                forced = EvalVerdict.FAIL
                failure = (FailureType.INFRASTRUCTURE_ERROR,)
            duration = int((time.perf_counter() - started) * 1000)
            evidence = collect_evidence(
                conn=conn, replay=replay, run_ids=tuple(state.run_ids), task_id=task.task_id,
                workspace=workspace, before_files=before, selected_skills=_skills(case, workspace),
                final_output=state.final_output, duration_ms=duration, model_calls=state.model_calls,
            )
            result = score_case(case, evidence, forced_verdict=forced, forced_failures=failure)
            if case.expected.judge_rubric:
                judge_result = self.judge.evaluate(
                    goal=case.goal, rubric=case.expected.judge_rubric,
                    final_output=evidence.final_output or "",
                    tools=[str(item.get("tool") or "") for item in evidence.tool_calls],
                ) if self.judge is not None else None
                if judge_result is None and case.expected.judge_required:
                    result = EvalResult(
                        case_id=result.case_id,
                        category=result.category,
                        verdict=EvalVerdict.UNKNOWN,
                        deterministic_score=result.deterministic_score,
                        judge_score=None,
                        metrics=result.metrics,
                        reasons=(*result.reasons, "required model judge unavailable"),
                        failure_types=(*result.failure_types, FailureType.MODEL_ERROR),
                        evidence=result.evidence,
                        root_causes=result.root_causes,
                    )
                elif judge_result is not None:
                    verdict = result.verdict
                    if judge_result.verdict == "FAIL":
                        verdict = EvalVerdict.FAIL
                    result = EvalResult(
                        case_id=result.case_id,
                        category=result.category,
                        verdict=verdict,
                        deterministic_score=result.deterministic_score,
                        judge_score=judge_result.score,
                        metrics=result.metrics,
                        reasons=result.reasons,
                        failure_types=result.failure_types,
                        evidence=result.evidence,
                        judge_reasons=judge_result.reasons,
                        root_causes=result.root_causes,
                    )
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            conn.close()
            return result

    def run_live_case(self, case: EvalCase) -> EvalResult:
        """Run one explicit real-model case with controlled isolated tools.

        The model receives only the user goal and normal bounded task context. The
        evaluator supplies no hidden answer and never invokes a tool function itself.
        """
        if self.live_settings is None:
            raise ValueError("live mode requires configured Tieru settings")
        from tieru.app import Tieru
        from tieru.loop.adapters import ModelError
        from tieru.tasks.service import build_task_service

        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix=f"tieru-live-eval-{case.case_id[:24]}-", ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            workspace = root / "workspace"
            home = root / "home"
            workspace.mkdir()
            home.mkdir()
            for relative, content in case.setup.files.items():
                target = _safe_target(workspace, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            _write_skill_fixtures(case, home)
            before = snapshot_files(workspace)
            settings = replace(
                self.live_settings,
                home=home,
                max_iterations=min(self.live_settings.max_iterations, case.max_model_calls),
                llm_timeout=min(self.live_settings.llm_timeout, case.timeout_ms / 1000),
                trust_policy=dict(case.setup.trust_policy),
            )
            if self.role_overrides:
                # Apply eval-only role overrides without persisting to configuration
                updated_roles = dict(settings.roles)
                for r_name, r_model in self.role_overrides.items():
                    target_role = "main" if r_name in ("main", "executor") else ("judge" if r_name in ("judge", "step_verifier", "goal_verifier") else "small")
                    base_r = settings.role(target_role)
                    from tieru.config import ModelRole
                    updated_roles[target_role] = ModelRole(
                        provider=base_r.provider,
                        protocol=base_r.protocol,
                        model=r_model,
                        base_url=base_r.base_url,
                        api_key_env=base_r.api_key_env,
                    )
                settings = replace(settings, roles=updated_roles)
            tieru = Tieru(settings, client=self.live_client)
            tieru.tools, _calls = _registry(
                case, tieru.conn, workspace, production_tools=True
            )
            for memory in case.setup.memories:
                tieru.conn.execute(
                    "INSERT INTO facts(subject, content, source, trusted) VALUES (?, ?, 'eval', 0)",
                    ("evaluation fixture", redact_secrets(memory)),
                )
            tieru.conn.commit()
            limits = TaskLimits(
                max_steps_per_task=max(1, case.max_steps),
                max_execution_steps_per_invocation=max(1, case.max_steps),
                max_replans_per_task=2,
            )
            budget = TaskBudget(
                max_steps=max(1, case.max_steps),
                max_model_calls=max(1, case.max_model_calls),
                max_tool_calls=max(0, case.max_tool_calls),
                max_active_runtime_seconds=case.timeout_ms / 1000,
            )
            service = build_task_service(
                tieru, limits=limits, role_overrides=self.role_overrides, role_policy=self.role_policy
            )

            try:
                contract = service.contract_builder.build(case.goal)
            except Exception:
                contract = None

            # Build the plan after the contract so the planner can see bounded success evidence
            # and user constraints as DATA. Explicit scripts remain deterministic fixtures.
            if case.setup.script:
                plan = [
                    PlanStep(
                        str(item.get("title") or f"Evaluation step {index}"),
                        str(item.get("instruction") or case.goal),
                        str(item.get("verification_instruction") or "Check observable evidence."),
                    )
                    for index, item in enumerate(case.setup.script[:case.max_steps], 1)
                ]
            else:
                try:
                    plan_with_contract = getattr(service.planner, "plan_with_contract", None)
                    plan = (
                        plan_with_contract(case.goal, contract)
                        if callable(plan_with_contract)
                        else service.planner.plan(case.goal)
                    )
                except Exception:
                    plan = [
                        PlanStep(
                            "Execute the bounded goal",
                            case.goal,
                            "Verify from observable governed tool evidence.",
                        )
                    ]

            task = service.store.create_task(
                case.goal,
                plan,
                contract=contract,
                budget=budget,
                source="eval_live",
                source_id=case.case_id,
                session_id=f"eval-live:{case.case_id}",
            )

            forced: EvalVerdict | None = None
            failures: list[FailureType] = []
            last_outcome = None
            try:
                for _ in range(case.max_steps):
                    if (time.perf_counter() - started) * 1000 > case.timeout_ms:
                        raise TimeoutError("live evaluation case timed out")
                    outcome = service.executor.run_next(task.task_id)
                    last_outcome = outcome
                    if not outcome.executed or outcome.task.status is not TaskStatus.RUNNING:
                        break
            except TimeoutError:
                forced = EvalVerdict.FAIL
                failures.append(FailureType.TIMEOUT)
            except ModelError as exc:
                forced = EvalVerdict.FAIL
                failures.append(_provider_failure(exc))
            except Exception as exc:
                forced = EvalVerdict.FAIL
                failures.append(_provider_failure(exc))

            if last_outcome is not None and last_outcome.step is not None and last_outcome.task.status is TaskStatus.FAILED:
                v_summary = (last_outcome.step.verification_summary or "").lower()
                if any(w in v_summary for w in ("timeout", "timed out", "timed_out")):
                    failures.append(FailureType.PROVIDER_TIMEOUT)
                elif any(w in v_summary for w in ("auth", "401", "403", "unauthorized")):
                    failures.append(FailureType.PROVIDER_AUTH_ERROR)
                elif any(w in v_summary for w in ("rate", "429")):
                    failures.append(FailureType.PROVIDER_RATE_LIMIT)
                elif "modelerror" in v_summary or "runtimeerror" in v_summary or "step runtime failed" in v_summary:
                    failures.append(FailureType.MODEL_ERROR)

            service.store.reconcile_task_completion(task.task_id)
            step_rows = service.store.list_steps(task.task_id)
            run_ids = tuple(s.execution_run_id for s in step_rows if s.execution_run_id)
            final_output = step_rows[-1].result or "" if step_rows else ""
            duration = int((time.perf_counter() - started) * 1000)
            evidence = collect_evidence(
                conn=tieru.conn,
                replay=tieru.replay,
                run_ids=run_ids,
                task_id=task.task_id,
                workspace=workspace,
                before_files=before,
                selected_skills=_skills(case, workspace),
                final_output=final_output,
                duration_ms=duration,
                model_calls=0,
                goal_verification_eligible=(
                    case.expected.task_status == "completed"
                    and not case.expected.expected_blocked
                ),
            )
            result = score_case(
                case, evidence, forced_verdict=forced, forced_failures=tuple(failures)
            )
            if case.expected.judge_rubric and self.judge is not None:
                judge_result = self.judge.evaluate(
                    goal=case.goal,
                    rubric=case.expected.judge_rubric,
                    final_output=evidence.final_output or "",
                    tools=[str(item.get("tool") or "") for item in evidence.tool_calls],
                )
                if judge_result is not None:
                    verdict = result.verdict
                    if judge_result.verdict == "FAIL":
                        verdict = EvalVerdict.FAIL
                    result = EvalResult(
                        case_id=result.case_id,
                        category=result.category,
                        verdict=verdict,
                        deterministic_score=result.deterministic_score,
                        judge_score=judge_result.score,
                        metrics=result.metrics,
                        reasons=result.reasons,
                        failure_types=result.failure_types,
                        evidence=result.evidence,
                        judge_reasons=judge_result.reasons,
                        root_causes=result.root_causes,
                    )
            tieru.close()
            try:
                tieru.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            tieru.conn.close()
            return result

    def _assemble_live_run(
        self,
        *,
        corpus: EvalCorpus,
        results: tuple[EvalResult, ...],
        probe,
        started_at: str,
        run_id: str,
        interrupted: bool = False,
        provider_blocked: bool = False,
    ) -> EvalRun:
        summary = aggregate_results(results)
        metrics = dict(summary["metrics"])
        selected_cases = len(corpus.cases)
        full_corpus_cases = corpus.full_case_count or selected_cases
        expected_case_runs = selected_cases * self.runs
        attempted_case_runs = sum(result.attempted for result in results)
        completed_case_runs = len(results)
        by_case = {
            case.case_id: sum(
                result.case_id == case.case_id and result.attempted for result in results
            )
            for case in corpus.cases
        }
        completed_by_case = {
            case.case_id: sum(result.case_id == case.case_id for result in results)
            for case in corpus.cases
        }
        attempted_cases = sum(count > 0 for count in by_case.values())
        completed_cases = sum(count >= self.runs for count in completed_by_case.values())
        completeness = (
            attempted_case_runs / expected_case_runs if expected_case_runs else 0.0
        )
        full_scope = (
            corpus.selection_scope == "full_corpus"
            and selected_cases == full_corpus_cases
        )
        if provider_blocked:
            status = LiveBaselineStatus.BLOCKED_PROVIDER_UNAVAILABLE
        elif (
            full_scope
            and not interrupted
            and attempted_case_runs == expected_case_runs
            and completed_case_runs == expected_case_runs
        ):
            status = LiveBaselineStatus.COMPLETE
        elif results or selected_cases:
            status = LiveBaselineStatus.PARTIAL
        else:
            status = LiveBaselineStatus.NOT_RUN

        passed = sum(result.verdict is EvalVerdict.PASS for result in results)
        expected_blocked = sum(result.verdict is EvalVerdict.BLOCKED for result in results)
        failed = sum(
            result.verdict in {
                EvalVerdict.FAIL,
                EvalVerdict.UNKNOWN,
                EvalVerdict.BUDGET_EXCEEDED,
            }
            for result in results
        )
        unexpected_blocked = sum(
            result.evidence.task_status == "blocked" and result.verdict is not EvalVerdict.BLOCKED
            for result in results
        )
        expected_pass_cases = sum(
            1 for c in corpus.cases if not c.expected.expected_blocked
        )
        expected_pass_completion_rate = (
            passed / max(1, expected_pass_cases) if expected_pass_cases > 0 else 0.0
        )
        live_summary = LiveEvalRunSummary(
            selected_cases=selected_cases,
            attempted_cases=attempted_cases,
            completed_cases=completed_cases,
            passed_cases=passed,
            failed_cases=failed,
            expected_blocked_cases=expected_blocked,
            unexpected_blocked_cases=unexpected_blocked,
            skipped_cases=max(0, selected_cases - attempted_cases),
            completeness=completeness,
            status=status,
            runs_per_case=self.runs,
            selected_case_runs=expected_case_runs,
            attempted_case_runs=attempted_case_runs,
            completed_case_runs=completed_case_runs,
            scope=corpus.selection_scope,
            full_corpus_cases=full_corpus_cases,
            expected_pass_cases=expected_pass_cases,
            passed_expected_pass_cases=passed,
            expected_pass_completion_rate=expected_pass_completion_rate,
        )

        unique_case_ids = {case.case_id for case in corpus.cases}
        flaky_cases = []
        for case_id in sorted(unique_case_ids):
            verdicts = {result.verdict for result in results if result.case_id == case_id}
            if len(verdicts) > 1:
                flaky_cases.append(case_id)
        metrics.update(
            {
                "status": status.value,
                "scope": corpus.selection_scope,
                "full_corpus_cases": full_corpus_cases,
                "selected_cases": selected_cases,
                "attempted_cases": attempted_cases,
                "completed_cases": completed_cases,
                "skipped_cases": max(0, selected_cases - attempted_cases),
                "live_case_attempt_rate": completeness,
                "runs_per_case": self.runs,
                "selected_case_runs": expected_case_runs,
                "expected_case_runs": expected_case_runs,
                "attempted_case_runs": attempted_case_runs,
                "completed_case_runs": completed_case_runs,
                "unexpected_blocked": unexpected_blocked,
                "flaky_case_rate": len(flaky_cases) / max(1, len(unique_case_ids)),
                "flaky_cases": flaky_cases,
            }
        )
        failures = dict(summary["failures"])
        if provider_blocked and not failures:
            failures["provider_unavailable"] = 1
        return EvalRun(
            schema_version=1,
            run_id=run_id,
            tieru_version=__version__,
            corpus_version=corpus.corpus_version,
            corpus_hash=corpus.content_hash,
            mode="live",
            started_at=started_at,
            completed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            provider=probe.provider,
            model=probe.model,
            configuration={
                "status": status.value,
                "provider_readiness": probe.status,
                "error": probe.error,
                "probe": probe.to_dict(),
                "endpoint": probe.endpoint,
                "scope": corpus.selection_scope,
                "full_corpus_cases": full_corpus_cases,
                "selected_cases": selected_cases,
                "runs": self.runs,
                "expected_case_runs": expected_case_runs,
                "overall_timeout_seconds": self.overall_timeout_seconds,
                "provider_failure_threshold": self.provider_failure_threshold,
                "case_isolation": "temporary_workspace_and_sqlite",
                "network": "configured_provider",
                "normal_runtime": (
                    "TaskService+GoalContract+TaskExecutor+CapabilityRouter+ToolRegistry+"
                    "Trust+ActionLedger+GoalVerifier+ResourceBudget+Replay"
                ),
                "interrupted": interrupted,
            },
            results=results,
            metrics=metrics,
            categories=summary["categories"],
            failures=failures,
            reliability_pass=(
                bool(summary["reliability_pass"])
                and status is LiveBaselineStatus.COMPLETE
            ),
            live_summary=live_summary,
        )

    def _checkpoint(self, run: EvalRun) -> None:
        if self.artifact_path is None:
            return
        from tieru.evals.report import write_result

        write_result(run, self.artifact_path)

    def run(self, corpus: EvalCorpus, *, mode: str = "deterministic") -> EvalRun:
        if mode not in {"deterministic", "live"}:
            raise ValueError("mode must be deterministic or live")
        started = datetime.now(UTC).isoformat(timespec="seconds")
        run_id = f"eval_{uuid4().hex}"

        if mode == "live":
            settings = self.live_settings or Settings()
            from tieru.evals.preflight import probe_provider

            probe = probe_provider(settings, client=self.live_client)
            if probe.status != "READY":
                blocked = self._assemble_live_run(
                    corpus=corpus,
                    results=(),
                    probe=probe,
                    started_at=started,
                    run_id=run_id,
                    provider_blocked=True,
                )
                self._checkpoint(blocked)
                return blocked

            all_results: list[EvalResult] = []
            interrupted = False
            provider_blocked = False
            consecutive_provider_failures = 0
            monotonic_started = time.monotonic()
            stop = False
            try:
                for run_number in range(1, self.runs + 1):
                    for case in corpus.cases:
                        if time.monotonic() - monotonic_started >= self.overall_timeout_seconds:
                            interrupted = True
                            stop = True
                            break
                        case_started = time.perf_counter()
                        try:
                            result = self.run_live_case(case)
                        except Exception as exc:
                            evidence = EvalEvidence(
                                duration_ms=int((time.perf_counter() - case_started) * 1000)
                            )
                            result = score_case(
                                case,
                                evidence,
                                forced_verdict=EvalVerdict.FAIL,
                                forced_failures=(FailureType.INFRASTRUCTURE_ERROR,),
                            )
                            result = replace(
                                result,
                                reasons=(*result.reasons, f"case infrastructure error: {type(exc).__name__}"),
                                attempted=False,
                            )
                        result = replace(result, run_number=run_number)
                        all_results.append(result)
                        if any(
                            failure in {
                                FailureType.PROVIDER_UNAVAILABLE,
                                FailureType.PROVIDER_AUTH_ERROR,
                            }
                            for failure in result.failure_types
                        ):
                            consecutive_provider_failures += 1
                        else:
                            consecutive_provider_failures = 0
                        if self.artifact_path is not None:
                            partial = self._assemble_live_run(
                                corpus=corpus,
                                results=tuple(all_results),
                                probe=probe,
                                started_at=started,
                                run_id=run_id,
                                interrupted=True,
                            )
                            self._checkpoint(partial)
                        if consecutive_provider_failures >= self.provider_failure_threshold:
                            interrupted = True
                            provider_blocked = True
                            stop = True
                            break
                    if stop:
                        break
            except KeyboardInterrupt:
                interrupted = True

            live_run = self._assemble_live_run(
                corpus=corpus,
                results=tuple(all_results),
                probe=probe,
                started_at=started,
                run_id=run_id,
                interrupted=interrupted,
                provider_blocked=provider_blocked,
            )
            self._checkpoint(live_run)
            return live_run

        results = tuple(self.run_case(case) for case in corpus.cases)
        summary = aggregate_results(results)
        return EvalRun(
            schema_version=1,
            run_id=run_id,
            tieru_version=__version__,
            corpus_version=corpus.corpus_version,
            corpus_hash=corpus.content_hash,
            mode=mode,
            started_at=started,
            completed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            provider="deterministic",
            model="scripted-v1",
            configuration={
                "case_isolation": "temporary_workspace_and_sqlite",
                "network": "disabled_by_fixture",
                "judge_enabled": self.judge is not None,
                "corpus_case_count": len(corpus.cases),
                "runs": 1,
            },
            results=results,
            metrics=summary["metrics"],
            categories=summary["categories"],
            failures=summary["failures"],
            reliability_pass=summary["reliability_pass"],
        )
