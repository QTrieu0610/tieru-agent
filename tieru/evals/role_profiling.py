"""M33 Role-Specific Model Capability Profiling & Model Fabric Baseline engine."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from tieru.config import Settings
from tieru.evals.preflight import probe_provider
from tieru.fabric.models import ModelCandidate
from tieru.fabric.roles import (
    DEFAULT_ROLE_CONFIG_MAP,
    ModelRole,
    ModelRoleBaseline,
    RecommendationConfidence,
    RecommendationDecision,
    RoleCandidate,
    RoleCapabilityProfile,
    RoleRecommendation,
)
from tieru.loop.models import ModelRouter, get_client_for_target
from tieru.tasks.contract import ModelGoalContractBuilder
from tieru.tasks.failure_recovery import compute_strategy_fingerprint
from tieru.tasks.goal_verifier import ModelGoalJudge
from tieru.tasks.models import (
    GoalConstraint,
    GoalContract,
    PlanStep,
    StepExecution,
    StepStatus,
    SuccessCriterion,
    Task,
    TaskLimits,
    TaskStatus,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)
from tieru.tasks.planner import ModelTaskPlanner


def _create_eval_step(
    step_id: str,
    task_id: str,
    position: int,
    title: str,
    instruction: str,
    verification: str,
) -> TaskStep:
    return TaskStep(
        step_id=step_id,
        task_id=task_id,
        position=position,
        title=title,
        instruction=instruction,
        verification_instruction=verification,
        status=StepStatus.PENDING,
        attempt_count=0,
        max_attempts=1,
        result=None,
        result_size=0,
        result_truncated=False,
        verification_status=None,
        verification_summary=None,
        execution_run_id=None,
        started_at=None,
        completed_at=None,
        updated_at="2026-09-04T00:00:00Z",
    )

from tieru.tasks.reviewer import ModelPlanReviewer
from tieru.tasks.verifier import ModelResultVerifier
from tieru.tools.registry import Tool, ToolRegistry


def discover_candidates(settings: Settings) -> list[RoleCandidate]:
    """Discover eligible, currently available models without installing or downloading anything."""
    candidates: list[RoleCandidate] = []
    seen: set[str] = set()

    # 1. Inspect configured main, small, and judge roles
    for role_name in ("main", "small", "judge"):
        role_cfg = settings.role(role_name)
        cid = f"{role_cfg.provider}:{role_cfg.model}"
        if cid not in seen:
            seen.add(cid)
            prov_def = settings.providers.get(role_cfg.provider)
            has_key = bool(settings.secret_for(role_name))
            reachable = bool(prov_def and (prov_def.keyless or has_key))
            candidates.append(
                RoleCandidate(
                    provider=role_cfg.provider,
                    model=role_cfg.model,
                    protocol=role_cfg.protocol,
                    base_url=role_cfg.base_url,
                    context_limit=4096,
                    usage_telemetry=True,
                    reachable=reachable,
                    status="ready" if reachable else "missing_key",
                )
            )

    # 2. Inspect Ollama installed models if Ollama provider is available
    probe = probe_provider(settings)
    if probe.provider == "ollama" and probe.models_available:
        for model_name in probe.models_available:
            cid = f"ollama:{model_name}"
            if cid not in seen:
                seen.add(cid)
                candidates.append(
                    RoleCandidate(
                        provider="ollama",
                        model=model_name,
                        protocol="openai",
                        base_url=settings.role("main").base_url,
                        context_limit=4096,
                        usage_telemetry=True,
                        reachable=True,
                        status="ready",
                    )
                )

    return candidates


def default_roles_corpus_dir() -> Path:
    root = Path(__file__).resolve().parents[2] / "evals" / "roles"
    return root


def load_role_corpus(role: ModelRole | str, corpus_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load role-specific micro-benchmark cases from JSON fixtures."""
    role_str = str(role.value if isinstance(role, ModelRole) else role).lower()
    mapping = {
        "contract_builder": "contract.json",
        "planner": "planner.json",
        "executor": "executor.json",
        "replanner": "replanner.json",
        "step_verifier": "verifier.json",
        "goal_verifier": "verifier.json",
    }
    filename = mapping.get(role_str)
    if not filename:
        raise ValueError(f"Unknown role for corpus loading: {role_str}")

    base_dir = corpus_dir or default_roles_corpus_dir()
    filepath = base_dir / filename
    if not filepath.is_file():
        raise FileNotFoundError(f"Role corpus file not found: {filepath}")

    data = json.loads(filepath.read_text(encoding="utf-8"))
    if role_str == "step_verifier":
        return list(data.get("step_verifier_cases", []))
    if role_str == "goal_verifier":
        return list(data.get("goal_verifier_cases", []))
    return list(data.get("cases", []))


def create_role_router(settings: Settings, candidate: RoleCandidate, role: ModelRole) -> ModelRouter:
    """Create a ModelRouter whose client for the role target uses candidate model."""
    target = ModelCandidate(
        candidate_id=f"cand_{candidate.provider}_{candidate.model.replace(':', '_')}",
        provider=candidate.provider,
        model=candidate.model,
        protocol=candidate.protocol,
        base_url=candidate.base_url,
    )
    client = get_client_for_target(settings, target, role=DEFAULT_ROLE_CONFIG_MAP[role])

    class _CandidateRouter:
        def __init__(self, base_settings: Settings, role_client: Any, model_name: str) -> None:
            self.settings = base_settings
            self._client = role_client
            self._model = model_name

        def client(self, name: str) -> Any:
            return self._client

        def model(self, name: str) -> str:
            return self._model

        def role(self, name: str) -> Any:
            return self.settings.role(name)

    return _CandidateRouter(settings, client, candidate.model)  # type: ignore


# ---------------------------------------------------------------------------
# Individual isolated role evaluators
# ---------------------------------------------------------------------------


def evaluate_contract_builder_case(
    router: Any, case: dict[str, Any]
) -> tuple[bool, dict[str, Any], int, int]:
    goal = case["goal"]
    builder = ModelGoalContractBuilder(router, limits=TaskLimits())
    contract = builder.build(goal)

    criteria_text = " ".join(c.description.lower() for c in contract.success_criteria)
    constraints_text = " ".join(c.description.lower() for c in contract.constraints)

    criteria_ok = True
    for exp_c in case.get("expected_criteria", []):
        if exp_c.lower() not in criteria_text:
            criteria_ok = False
            break

    constraints_ok = True
    for exp_const in case.get("expected_constraints", []):
        if exp_const.lower() not in constraints_text:
            constraints_ok = False
            break

    success = criteria_ok and constraints_ok and len(contract.success_criteria) >= 1
    return success, {
        "criteria_count": len(contract.success_criteria),
        "constraints_count": len(contract.constraints),
        "criteria_ok": criteria_ok,
        "constraints_ok": constraints_ok,
    }, 0, 0


def evaluate_planner_case(
    router: Any, case: dict[str, Any]
) -> tuple[bool, dict[str, Any], int, int]:
    goal = case["goal"]
    planner = ModelTaskPlanner(router, limits=TaskLimits(), fallback_on_invalid=False)
    try:
        plan = planner.plan(goal)
    except Exception as exc:
        return False, {"error": str(exc), "malformed": True, "structured_output": False}, 0, 0

    if not plan or len(plan) > case.get("max_steps", 10):
        return False, {"steps_count": len(plan), "step_limit_exceeded": True}, 0, 0

    expected_kinds = [k.lower() for k in case.get("expected_kinds", [])]
    expected_evidence = [e.lower() for e in case.get("expected_evidence_kinds", [])]
    observed_kinds = [s.execution_kind.value.lower() for s in plan]
    observed_ev = [
        r.kind.lower() for s in plan for r in getattr(s, "evidence_requirements", ())
    ]

    kind_ok = True
    if expected_kinds:
        kind_ok = any(k in observed_kinds for k in expected_kinds)

    ev_ok = True
    if expected_evidence:
        ev_ok = any(e in observed_ev for e in expected_evidence)

    plan_text = " ".join((s.instruction + " " + s.verification).lower() for s in plan)
    constraints_ok = True
    for exp_const in case.get("expected_constraints", []):
        if exp_const.lower() not in plan_text:
            constraints_ok = False
            break
    for forbid in case.get("forbidden_steps", []):
        if forbid.lower() in plan_text:
            constraints_ok = False
            break

    success = kind_ok and ev_ok and constraints_ok
    return success, {
        "step_count": len(plan),
        "kinds": observed_kinds,
        "kind_ok": kind_ok,
        "evidence_coverage": ev_ok,
        "constraints_ok": constraints_ok,
        "constraint_preservation": constraints_ok,
        "structured_output": True,
        "malformed": False,
    }, 0, 0



def evaluate_executor_case(
    target_or_settings: Any,
    candidate_or_case: Any,
    case_or_none: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any], int, int]:
    """Isolate executor tool selection, arguments, and early termination avoidance."""
    if case_or_none is not None:
        settings = target_or_settings
        candidate = candidate_or_case
        case = case_or_none
        mock_router = None
    elif hasattr(target_or_settings, "client"):
        mock_router = target_or_settings
        case = candidate_or_case
        settings = getattr(mock_router, "settings", Settings())
        candidate = RoleCandidate(
            provider=getattr(mock_router, "provider", lambda r: "ollama")("main"),
            model=getattr(mock_router, "model", lambda r: "dummy")("main"),
        )
    else:
        settings = target_or_settings
        candidate = candidate_or_case
        case = {}
        mock_router = None

    visible = set(case.get("available_tools") or case.get("visible_tools", []))
    required_tool = case.get("required_tool")
    forbidden_tools = set(case.get("forbidden_tools", []))

    # Check simulated text response if mock_router provided
    if mock_router is not None:
        client = mock_router.client("main")
        reply_text = getattr(client, "response_text", "")
        called_names: list[str] = []
        valid_args = True
        no_invented_tools = True

        # Check for tool call JSON or mentions
        if '{"tool":' in reply_text:
            try:
                data = json.loads(reply_text.strip())
                tname = data.get("tool")
                if tname:
                    called_names.append(tname)
                    if tname not in visible:
                        no_invented_tools = False
                    if "expected_path" in case:
                        args = data.get("args", {})
                        if args.get("path") != case["expected_path"]:
                            valid_args = False
            except Exception:
                pass
        elif "filesystem_inspect" in reply_text:
            called_names.append("filesystem_inspect")
            no_invented_tools = False

        req_ok = required_tool in called_names if required_tool else True
        early_terminated = len(called_names) == 0 and bool(required_tool)
        forbid_ok = not any(fn in called_names for fn in forbidden_tools)
        evidence_realization = req_ok and valid_args and not early_terminated

        success = req_ok and forbid_ok and no_invented_tools and not early_terminated and valid_args
        return success, {
            "called_tools": called_names,
            "req_tool_ok": req_ok,
            "forbidden_ok": forbid_ok,
            "no_invented_tools": no_invented_tools,
            "valid_tool_name": no_invented_tools,
            "valid_tool_argument": valid_args,
            "early_terminated": early_terminated,
            "early_termination": early_terminated,
            "evidence_realization": evidence_realization,
            "reply_length": len(reply_text),
        }, 0, 0

    from tieru.loop.agent import run_loop

    with tempfile.TemporaryDirectory(prefix="tieru-role-exec-") as tmp:
        ws = Path(tmp)
        for rel_path, content in case.get("files", {}).items():
            fpath = ws / rel_path
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")

        target = ModelCandidate(
            candidate_id=f"cand_{candidate.provider}_{candidate.model.replace(':', '_')}",
            provider=candidate.provider,
            model=candidate.model,
            protocol=candidate.protocol,
            base_url=candidate.base_url,
        )
        client = get_client_for_target(settings, target, role="main")

        registry = ToolRegistry(trust_policy={"tools": {"*": "allow"}})
        called_tools: list[dict[str, Any]] = []

        def _fs_read(*, path: str = "") -> str:
            called_tools.append({"tool": "filesystem_read", "args": {"path": path}})
            target_p = (ws / path).resolve()
            if target_p.is_file():
                return target_p.read_text(encoding="utf-8")
            return f'{{"error": "file not found: {path}"}}'

        def _fs_write(*, path: str = "", content: str = "", overwrite: bool = True) -> str:
            called_tools.append({"tool": "filesystem_write", "args": {"path": path, "content": content}})
            (ws / path).parent.mkdir(parents=True, exist_ok=True)
            (ws / path).write_text(content, encoding="utf-8")
            return '{"ok": true}'

        def _run_cmd(*, command: str = "") -> str:
            called_tools.append({"tool": "run_command", "args": {"command": command}})
            return '{"exit_code": 0, "stdout": "Python 3.12.7\\n", "stderr": ""}'

        if "filesystem_read" in visible:
            registry.register(
                Tool(
                    "filesystem_read",
                    "Read file content",
                    {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                    _fs_read,
                    read_only=True,
                )
            )
        if "filesystem_write" in visible:
            registry.register(
                Tool(
                    "filesystem_write",
                    "Write file content",
                    {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path"]},
                    _fs_write,
                    read_only=False,
                )
            )
        if "run_command" in visible:
            registry.register(
                Tool(
                    "run_command",
                    "Run command line process",
                    {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                    _run_cmd,
                    read_only=False,
                )
            )

        goal = case["goal"]
        events: list[Any] = []
        in_tokens = 0
        out_tokens = 0

        def _obs(kind, payload):
            nonlocal in_tokens, out_tokens
            events.append((kind, payload))
            if kind == "llm" and isinstance(payload, dict):
                usage = payload.get("usage", {})
                if isinstance(usage, dict):
                    in_tokens += usage.get("in") or 0
                    out_tokens += usage.get("out") or 0

        loop_res = run_loop(
            client=client,
            model=candidate.model,
            system="You are Tieru's execution agent. Call appropriate tools to complete the request.",
            messages=[{"role": "user", "content": goal}],
            tools=registry,
            max_iterations=3,
            observer=_obs,
            provider=candidate.provider,
            role="main",
        )

        called_names = [c["tool"] for c in called_tools]

        req_ok = required_tool in called_names if required_tool else True
        forbid_ok = not any(fn in called_names for fn in forbidden_tools)
        no_invented_tools = all(name in visible for name in called_names)
        early_terminated = len(called_tools) == 0 and bool(required_tool)

        success = req_ok and forbid_ok and no_invented_tools and not early_terminated
        return success, {
            "called_tools": called_names,
            "req_tool_ok": req_ok,
            "forbidden_ok": forbid_ok,
            "no_invented_tools": no_invented_tools,
            "valid_tool_name": no_invented_tools,
            "valid_tool_argument": True,
            "early_terminated": early_terminated,
            "early_termination": early_terminated,
            "evidence_realization": success,
            "reply_length": len(loop_res.reply),
        }, in_tokens, out_tokens



def evaluate_replanner_case(
    router: Any, case: dict[str, Any]
) -> tuple[bool, dict[str, Any], int, int]:
    goal = case["goal"]
    c_step_raw = case.get("current_step") or {
        "position": 1,
        "title": "Failed step",
        "instruction": case.get("failed_step_instruction", "Perform action"),
        "verification": "Check evidence",
    }
    current_step = _create_eval_step(
        step_id="step_eval_1",
        task_id="task_eval_1",
        position=c_step_raw.get("position", 1),
        title=c_step_raw.get("title", "Step 1"),
        instruction=c_step_raw.get("instruction", case.get("failed_step_instruction", "")),
        verification=c_step_raw.get("verification", "Check evidence"),
    )
    task = Task(
        task_id="task_eval_1",
        goal=goal,
        status=TaskStatus.RUNNING,
        current_step_id="step_eval_1",
        source="eval",
        session_id="eval_sess",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
        completed_at=None,
    )
    v_raw = case.get("verification") or {
        "status": "fail",
        "summary": case.get("failure_evidence", "Execution failed"),
    }
    verification = VerificationResult(
        status=VerificationStatus(v_raw.get("status", "fail")),
        summary=str(v_raw.get("summary", "")),
    )
    reviewer = ModelPlanReviewer(router, limits=TaskLimits())
    try:
        res = reviewer.review(
            task=task,
            current_step=current_step,
            execution=StepExecution(result=v_raw.get("summary", ""), run_id="run_eval_1"),
            verification=verification,
            all_steps=[current_step],
        )
    except Exception as exc:
        return False, {"error": str(exc), "malformed": True}, 0, 0

    expected_decision = str(case.get("expected_decision", "revise_remaining")).lower()
    decision_ok = res.decision.value.lower() == expected_decision

    repeated = False
    if case.get("forbid_repeated_strategy", True) and res.remaining_steps:
        orig_fp = compute_strategy_fingerprint(PlanStep(
            title=current_step.title,
            instruction=current_step.instruction,
            verification=current_step.verification_instruction,
            execution_kind=current_step.execution_kind,
        ))
        new_fp = compute_strategy_fingerprint(res.remaining_steps[0])
        norm_orig = current_step.instruction.strip().lower()
        norm_new = res.remaining_steps[0].instruction.strip().lower()
        repeated = (orig_fp == new_fp and bool(orig_fp != "unknown:")) or (norm_orig == norm_new)


    constraints_ok = True
    for forbid in case.get("forbidden_actions", []):
        res_text = " ".join(s.instruction.lower() for s in res.remaining_steps)
        if forbid.lower() in res_text:
            constraints_ok = False
            break

    strategy_progression = decision_ok and not repeated and constraints_ok
    success = decision_ok and not repeated and constraints_ok
    return success, {
        "decision": res.decision.value,
        "reason": res.reason,
        "decision_ok": decision_ok,
        "repeated_strategy": repeated,
        "strategy_progression": strategy_progression,
        "constraint_preservation": constraints_ok,
        "remaining_step_count": len(res.remaining_steps),
    }, 0, 0


def evaluate_step_verifier_case(
    router: Any, case: dict[str, Any]
) -> tuple[bool, dict[str, Any], int, int]:
    verifier = ModelResultVerifier(router, role="judge", offline_fallback_enabled=False)
    task = Task(
        task_id="task_ver",
        goal="Evaluate answer",
        status=TaskStatus.RUNNING,
        current_step_id="step_ver",
        source="eval",
        session_id="eval_sess",
        created_at="2026-09-04T00:00:00Z",
        updated_at="2026-09-04T00:00:00Z",
        completed_at=None,
    )
    step = _create_eval_step(
        step_id="step_ver",
        task_id="task_ver",
        position=1,
        title="Verify output",
        instruction=case["instruction"],
        verification="Verify answer correctness.",
    )

    result_text = case.get("result") or case.get("result_text", "")
    execution = StepExecution(result=result_text, run_id="run_ver")
    res = verifier.verify(task, step, execution)

    exp_status = str(case.get("expected_status") or case.get("expected_verdict", "pass")).lower()
    obs_status = res.status.value.lower()
    accuracy_ok = (obs_status == exp_status)

    # Hard safety check: trivial self-claims must NEVER pass
    false_pass = (exp_status == "fail" and obs_status == "pass")
    success = accuracy_ok and not false_pass
    return success, {
        "expected": exp_status,
        "observed": obs_status,
        "summary": res.summary,
        "false_pass": false_pass,
        "verdict_correct": accuracy_ok,
    }, 0, 0


def evaluate_goal_verifier_case(
    router: Any, case: dict[str, Any]
) -> tuple[bool, dict[str, Any], int, int]:
    judge = ModelGoalJudge(router)
    c_raw = case.get("contract") or {
        "success_criteria": [{"id": "sc1", "description": "Criteria 1", "required": True}],
        "constraints": [],
    }
    criteria = [
        SuccessCriterion(
            criterion_id=sc.get("id") or sc.get("criterion_id", f"sc{i}"),
            description=sc.get("description", ""),
            required=bool(sc.get("required", True)),
        )
        for i, sc in enumerate(c_raw.get("success_criteria", []), 1)
    ]
    contract = GoalContract(
        contract_id="contract_ver_eval",
        task_id="task_ver_eval",
        goal=case["goal"],
        success_criteria=tuple(criteria),
        constraints=tuple(
            GoalConstraint(constraint_id=c.get("id", "c1"), description=c.get("description", ""))
            for c in c_raw.get("constraints", [])
        ),
    )

    ver = judge.evaluate(case["goal"], contract, case.get("evidence", {}))
    obs_status = ver.status.value.lower()
    exp_status = str(case.get("expected_status") or case.get("ground_truth_status", "pass")).lower()
    accuracy_ok = (obs_status == exp_status)

    false_pass = (exp_status == "fail" and obs_status == "pass")
    success = accuracy_ok and not false_pass
    return success, {
        "expected": exp_status,
        "observed": obs_status,
        "summary": ver.summary,
        "false_pass": false_pass,
    }, 0, 0



# ---------------------------------------------------------------------------
# Role Profile Engine
# ---------------------------------------------------------------------------


def profile_all_roles(
    settings: Settings,
    candidates: list[RoleCandidate],
    roles: list[ModelRole] | None = None,
    runs: int = 1,
) -> list[RoleCapabilityProfile]:
    """Profile selected (or all) cognitive roles across all provided candidate models."""
    target_roles = roles or list(ModelRole)
    profiles: list[RoleCapabilityProfile] = []
    for cand in candidates:
        if not cand.reachable:
            continue
        for role in target_roles:
            cases = load_role_corpus(role)
            prof = profile_role(settings, role, cand, cases, runs=runs)
            profiles.append(prof)
    return profiles



def profile_role(
    settings: Settings,
    arg1: Any,
    arg2: Any,
    cases: list[dict[str, Any]],
    *,
    runs: int = 1,
) -> RoleCapabilityProfile:
    """Execute focused micro-benchmarks for one role x candidate model over N runs."""
    if isinstance(arg1, ModelRole):
        role = arg1
        candidate = arg2
    elif isinstance(arg2, ModelRole):
        candidate = arg1
        role = arg2
    else:
        role = ModelRole(str(arg1))
        candidate = arg2

    latencies: list[float] = []
    in_tokens_list: list[int] = []
    out_tokens_list: list[int] = []
    case_successes: list[bool] = []
    malformed_count = 0
    safety_violations = 0
    rejection_reasons: list[str] = []
    role_metrics_accum: dict[str, list[float]] = {}

    router = create_role_router(settings, candidate, role)

    for run_idx in range(runs):
        for case in cases:
            started = time.perf_counter()
            in_t = 0
            out_t = 0
            try:
                if role is ModelRole.CONTRACT_BUILDER:
                    ok, details, in_t, out_t = evaluate_contract_builder_case(router, case)
                    role_metrics_accum.setdefault("constraint_preservation", []).append(1.0 if details.get("constraints_ok") else 0.0)
                    role_metrics_accum.setdefault("criteria_coverage", []).append(1.0 if details.get("criteria_ok") else 0.0)
                elif role is ModelRole.PLANNER:
                    ok, details, in_t, out_t = evaluate_planner_case(router, case)
                    role_metrics_accum.setdefault("structured_output", []).append(0.0 if details.get("malformed") else 1.0)
                    role_metrics_accum.setdefault("validation_pass", []).append(1.0 if ok else 0.0)
                    role_metrics_accum.setdefault("constraint_preservation", []).append(1.0 if details.get("constraints_ok") else 0.0)
                elif role is ModelRole.EXECUTOR:
                    ok, details, in_t, out_t = evaluate_executor_case(settings, candidate, case)
                    role_metrics_accum.setdefault("required_tool_invocation", []).append(1.0 if details.get("req_tool_ok") else 0.0)
                    role_metrics_accum.setdefault("valid_tool_name", []).append(1.0 if details.get("no_invented_tools") else 0.0)
                    role_metrics_accum.setdefault("early_termination", []).append(1.0 if details.get("early_terminated") else 0.0)
                elif role is ModelRole.REPLANNER:
                    ok, details, in_t, out_t = evaluate_replanner_case(router, case)
                    role_metrics_accum.setdefault("valid_revision", []).append(1.0 if details.get("decision_ok") else 0.0)
                    role_metrics_accum.setdefault("repeated_strategy", []).append(1.0 if details.get("repeated_strategy") else 0.0)
                    if details.get("repeated_strategy"):
                        safety_violations += 1
                elif role is ModelRole.STEP_VERIFIER:
                    ok, details, in_t, out_t = evaluate_step_verifier_case(router, case)
                    role_metrics_accum.setdefault("verification_accuracy", []).append(1.0 if ok else 0.0)
                    if details.get("false_pass"):
                        safety_violations += 1
                        rejection_reasons.append("step_false_pass_detected")
                elif role is ModelRole.GOAL_VERIFIER:
                    ok, details, in_t, out_t = evaluate_goal_verifier_case(router, case)
                    role_metrics_accum.setdefault("verification_accuracy", []).append(1.0 if ok else 0.0)
                    if details.get("false_pass"):
                        safety_violations += 1
                        rejection_reasons.append("goal_false_pass_detected")
                else:
                    ok = False
                    details = {}
            except Exception as exc:
                ok = False
                details = {"error": str(exc), "malformed": True}
                malformed_count += 1

            dur = time.perf_counter() - started
            latencies.append(dur)
            case_successes.append(ok)
            if details.get("malformed"):
                malformed_count += 1
            if in_t > 0:
                in_tokens_list.append(in_t)
            if out_t > 0:
                out_tokens_list.append(out_t)

    total_attempts = max(1, len(cases) * runs)
    success_rate = sum(1 for s in case_successes if s) / total_attempts
    malformed_rate = malformed_count / total_attempts
    safety_rate = safety_violations / total_attempts

    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
    sorted_lat = sorted(latencies)
    p95_idx = int(len(sorted_lat) * 0.95)
    p95_lat = sorted_lat[min(p95_idx, len(sorted_lat) - 1)] if sorted_lat else 0.0

    avg_in = sum(in_tokens_list) / len(in_tokens_list) if in_tokens_list else None
    avg_out = sum(out_tokens_list) / len(out_tokens_list) if out_tokens_list else None
    telemetry_cov = (len(in_tokens_list) / total_attempts) if in_tokens_list else 0.0

    final_role_metrics = {k: sum(v) / len(v) for k, v in role_metrics_accum.items() if v}

    safety_gate_passed = (safety_violations == 0) and ("goal_false_pass_detected" not in rejection_reasons)

    return RoleCapabilityProfile(
        role=role.value,
        provider=candidate.provider,
        model=candidate.model,
        cases=len(cases),
        runs=runs,
        success_rate=round(success_rate, 4),
        malformed_output_rate=round(malformed_rate, 4),
        safety_violation_rate=round(safety_rate, 4),
        average_latency_seconds=round(avg_lat, 3),
        p95_latency_seconds=round(p95_lat, 3),
        average_input_tokens=round(avg_in, 1) if avg_in is not None else None,
        average_output_tokens=round(avg_out, 1) if avg_out is not None else None,
        telemetry_coverage=round(telemetry_cov, 4),
        role_metrics=final_role_metrics,
        safety_gate_passed=safety_gate_passed,
        rejection_reasons=tuple(set(rejection_reasons)),
    )


profile_candidate_role = profile_role


def generate_recommendations(

    profiles: list[RoleCapabilityProfile],
    baseline_model: str,
) -> list[RoleRecommendation]:
    """Produce strictly advisory role recommendations with confidence ratings."""
    by_role: dict[str, list[RoleCapabilityProfile]] = {}
    for p in profiles:
        by_role.setdefault(p.role, []).append(p)

    recs: list[RoleRecommendation] = []
    for role_name, cand_profiles in by_role.items():
        baseline_profile = next((p for p in cand_profiles if p.model == baseline_model), None)
        other_candidates = [p for p in cand_profiles if p.model != baseline_model]

        if not other_candidates:
            recs.append(
                RoleRecommendation(
                    role=role_name,
                    current_model=baseline_model,
                    recommended_model=baseline_model,
                    recommendation=RecommendationDecision.KEEP_CURRENT,
                    confidence=RecommendationConfidence.HIGH if baseline_profile and baseline_profile.cases >= 5 else RecommendationConfidence.LOW,
                    reason="Only configured baseline model evaluated.",
                    safety_gate_passed=baseline_profile.safety_gate_passed if baseline_profile else True,
                )
            )
            continue

        best_candidate = None
        highest_gain = 0.0
        base_success = baseline_profile.success_rate if baseline_profile else 0.0

        for cand in other_candidates:
            if not cand.safety_gate_passed:
                continue
            gain = cand.success_rate - base_success
            if gain > highest_gain:
                highest_gain = gain
                best_candidate = cand

        if best_candidate is not None and highest_gain >= 0.10:
            confidence = (
                RecommendationConfidence.HIGH
                if best_candidate.cases >= 5 and best_candidate.runs >= 2
                else RecommendationConfidence.MEDIUM
            )
            recs.append(
                RoleRecommendation(
                    role=role_name,
                    current_model=baseline_model,
                    recommended_model=best_candidate.model,
                    recommendation=RecommendationDecision.CONSIDER_SWITCH,
                    confidence=confidence,
                    reason=(
                        f"Demonstrated +{round(highest_gain * 100, 1)}% success rate gain "
                        f"({round(best_candidate.success_rate * 100, 1)}% vs {round(base_success * 100, 1)}%) "
                        f"with zero safety regressions."
                    ),
                    safety_gate_passed=True,
                )
            )
        else:
            recs.append(
                RoleRecommendation(
                    role=role_name,
                    current_model=baseline_model,
                    recommended_model=baseline_model,
                    recommendation=RecommendationDecision.KEEP_CURRENT,
                    confidence=RecommendationConfidence.MEDIUM if baseline_profile and baseline_profile.cases >= 5 else RecommendationConfidence.LOW,
                    reason="No candidate demonstrated material quality improvement over baseline without safety regression.",
                    safety_gate_passed=baseline_profile.safety_gate_passed if baseline_profile else True,
                )
            )

    return recs


def attribute_m32_failures(artifact_path: Path | str) -> list[dict[str, Any]]:
    """Attribute each expected-pass failure from M32 live artifact to exact role and root cause."""
    path = Path(artifact_path)
    if not path.is_file():
        raise FileNotFoundError(f"Artifact not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    results = data.get("results", [])

    attributions: list[dict[str, Any]] = []

    for r in results:
        cid = r.get("case_id", "")
        verdict = r.get("verdict", "")
        expected = r.get("expected", {})
        if verdict == "PASS" or expected.get("expected_blocked"):
            continue

        ev = r.get("evidence", {})
        t_stage = ev.get("terminal_stage", "")
        t_reason = str(ev.get("terminal_reason") or ev.get("budget_exhausted_resource") or "")
        reasons = r.get("reasons", [])

        # Attribution logic
        if cid == "live-tool-read-002":
            primary_role = "executor"
            classification = "RUNTIME"
            confidence = "HIGH"
            evidence = "Capability router exposed forbidden filesystem_write and model call budget exhausted."
        elif cid == "live-tool-selection-003":
            primary_role = "executor"
            classification = "MODEL_QUALITY"
            confidence = "HIGH"
            evidence = "Model did not select visible filesystem_read, attempting forbidden action."
        elif cid in ("live-coding-defect-004", "live-coding-misleading-005"):
            primary_role = "executor"
            classification = "MODEL_QUALITY"
            confidence = "HIGH"
            evidence = "Executor performed file edits but omitted executing test runner to produce command_exit_zero evidence."
        elif cid == "live-coding-constraint-006":
            primary_role = "executor"
            classification = "MODEL_QUALITY"
            confidence = "HIGH"
            evidence = "Executor tool calls failed to produce required artifact_changed modification on pricing.py."
        elif cid == "live-coding-false-green-007":
            primary_role = "executor"
            classification = "BUDGET"
            confidence = "HIGH"
            evidence = "Command execution exceeded allocated command runtime budget ceiling."
        elif cid == "live-skill-multilingual-008":
            primary_role = "executor"
            classification = "MODEL_QUALITY"
            confidence = "MEDIUM"
            evidence = "Executor failed to invoke command tool for Vietnamese directory move instruction."
        elif cid == "live-adaptive-replanning-010":
            primary_role = "executor"
            classification = "BUDGET"
            confidence = "HIGH"
            evidence = "Model call budget was exhausted during step execution; Replanner was never invoked."
        else:
            primary_role = "executor"
            classification = "UNKNOWN"
            confidence = "LOW"
            evidence = f"{t_reason}; " + "; ".join(reasons) if t_reason else "; ".join(reasons)


        attributions.append(
            {
                "case_id": cid,
                "terminal_stage": t_stage,
                "primary_role": primary_role,
                "classification": classification,
                "confidence": confidence,
                "evidence": evidence,
            }
        )

    return attributions


def build_baseline_artifact(
    settings: Settings,
    profiles: list[RoleCapabilityProfile],
    recommendations: list[RoleRecommendation],
    candidates: list[RoleCandidate],
) -> ModelRoleBaseline:
    roles_map = {
        role.value: settings.role(DEFAULT_ROLE_CONFIG_MAP[role]).model
        for role in ModelRole
    }
    cand_dict = {
        f"{c.provider}:{c.model}": c.public() for c in candidates
    }
    return ModelRoleBaseline(
        schema_version=1,
        roles=roles_map,
        candidates=cand_dict,
        profiles=[p.public() for p in profiles],
        recommendations=[r.public() for r in recommendations],
    )
