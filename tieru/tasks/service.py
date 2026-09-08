"""User-facing orchestration service for explicit durable-task operations."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from tieru.fabric.roles import SelectionSource
from tieru.tasks.contract import GoalContractBuilder, ModelGoalContractBuilder
from tieru.tasks.executor import TaskExecutor, TieruStepRunner
from tieru.tasks.goal_verifier import LayeredTaskGoalVerifier, ModelGoalJudge
from tieru.tasks.models import TaskBudget, TaskLimits, TaskRunResult, TaskStatus
from tieru.tasks.planner import ModelTaskPlanner, TaskPlanner
from tieru.tasks.store import TaskStore
from tieru.tasks.verifier import LayeredTaskVerifier, ModelResultVerifier


class TaskService:
    def __init__(
        self,
        store: TaskStore,
        planner: TaskPlanner,
        executor: TaskExecutor,
        *,
        contract_builder: GoalContractBuilder | None = None,
    ) -> None:
        self.store = store
        self.planner = planner
        self.executor = executor
        self.contract_builder = contract_builder

    def create(
        self,
        *,
        goal: str,
        budget: TaskBudget | None = None,
        source: str = "cli",
        source_id: str | None = None,
        session_id: str | None = None,
        observer=None,
    ):
        existing = (
            self.store.get_task_by_source(source, source_id) if source_id is not None else None
        )
        if existing is not None:
            return existing
        contract = self.contract_builder.build(goal) if self.contract_builder is not None else None
        plan_with_contract = getattr(type(self.planner), "plan_with_contract", None)
        plan = (
            plan_with_contract(self.planner, goal, contract)
            if callable(plan_with_contract)
            else self.planner.plan(goal)
        )
        task = self.store.create_task(
            goal,
            plan,
            budget=budget,
            contract=contract,
            source=source,
            source_id=source_id,
            session_id=session_id,
        )
        actual_contract = self.store.get_goal_contract(task.task_id)
        actual_budget = self.store.get_task_budget(task.task_id)
        if observer is not None:
            observer(
                "task_created",
                {"task_id": task.task_id, "status": task.status.value, "step_count": len(plan)},
            )
            observer(
                "task_budget_created",
                {"task_id": task.task_id, "limits": asdict(actual_budget)},
            )
            observer(
                "goal_contract_created",
                {
                    "task_id": task.task_id,
                    "contract_id": actual_contract.contract_id,
                    "criteria_count": len(actual_contract.success_criteria),
                    "constraints_count": len(actual_contract.constraints),
                },
            )
        return task

    def run(
        self,
        task_id: str,
        *,
        max_steps: int = 1,
        observer=None,
        resume: bool = False,
    ) -> list[TaskRunResult]:
        limit = max(
            1,
            min(int(max_steps), self.store.limits.max_execution_steps_per_invocation),
        )
        results: list[TaskRunResult] = []
        for index in range(limit):
            result = self.executor.run_next(
                task_id,
                observer=observer,
                recover_orphaned=resume and index == 0,
            )
            results.append(result)
            if not result.executed or result.task.status is not TaskStatus.RUNNING:
                break
        return results

    def resume(self, task_id: str, *, max_steps: int = 1, observer=None):
        self.store.reconcile_task_completion(task_id)
        return self.run(task_id, max_steps=max_steps, observer=observer, resume=True)

    def cancel(self, task_id: str, *, observer=None):
        task = self.store.cancel(task_id)
        if observer is not None:
            observer("task_cancelled", {"task_id": task.task_id, "status": task.status.value})
        return task

    def recover(self, task_id: str, *, note: str = "", source: str = "api", observer=None):
        """Explicitly prepare or verify one blocked step; never execute it."""
        from tieru.recovery import RecoveryService, RecoveryStore

        recovery = RecoveryService(RecoveryStore(self.store.conn), replay=self.executor.replay)
        return recovery.recover_task(
            task_id,
            self,
            note=note,
            source=source,
            observer=observer,
        )

    def extend_budget(
        self,
        task_id: str,
        *,
        actor: str = "operator",
        reason: str = "",
        model_calls: float = 0,
        tool_calls: float = 0,
        steps: float = 0,
        replans: float = 0,
        verification_calls: float = 0,
        retries: float = 0,
        command_runtime_seconds: float = 0,
        active_runtime_seconds: float = 0,
        input_tokens: float = 0,
        output_tokens: float = 0,
        observer=None,
    ) -> TaskBudget:
        increments = {
            "max_model_calls": model_calls,
            "max_tool_calls": tool_calls,
            "max_steps": steps,
            "max_replans": replans,
            "max_verification_calls": verification_calls,
            "max_retries": retries,
            "max_command_runtime_seconds": command_runtime_seconds,
            "max_active_runtime_seconds": active_runtime_seconds,
            "max_input_tokens": input_tokens,
            "max_output_tokens": output_tokens,
        }
        increments = {k: v for k, v in increments.items() if v > 0}
        new_budget = self.store.extend_budget(
            task_id,
            actor=actor,
            reason=reason,
            increments=increments,
        )
        if observer is not None:
            observer(
                "task_budget_extended",
                {
                    "task_id": task_id,
                    "actor": actor,
                    "reason": reason,
                    "increments": increments,
                },
            )
        return new_budget

    def show(self, task_id: str) -> dict:
        contract = self.store.get_goal_contract(task_id)
        latest_ver = self.store.get_latest_goal_verification(task_id)
        budget = self.store.get_task_budget(task_id)
        usage = self.store.get_task_budget_usage(task_id)
        remaining = self.store.get_budget_remaining(task_id)
        allocations = self.store.list_budget_allocations(task_id)
        return {
            "task": asdict(self.store.get_task(task_id)),
            "steps": [asdict(step) for step in self.store.list_steps(task_id)],
            "revisions": [asdict(rev) for rev in self.store.list_revisions(task_id)],
            "goal_contract": asdict(contract),
            "goal_verification": asdict(latest_ver) if latest_ver is not None else None,
            "budget": {
                "limits": asdict(budget),
                "usage": asdict(usage),
                "remaining": remaining,
                "allocations": [asdict(a) for a in allocations],
            },
        }

    def list(self, *, status: TaskStatus | None = None, limit: int = 50):
        return self.store.list_tasks(status=status, limit=limit)


def build_task_service(
    tieru,
    *,
    limits: TaskLimits | None = None,
    role_overrides: dict[str, str] | None = None,
    role_policy: Any = None,
) -> TaskService:
    bounds = limits or TaskLimits()
    store = TaskStore(tieru.conn, limits=bounds)

    from tieru.fabric.models import ModelCandidate
    from tieru.fabric.roles import resolve_effective_role_assignment
    from tieru.loop.models import get_client_for_target

    class _RoleAwareTaskRouter:
        def __init__(self, base_router, overrides: dict[str, str] | None, policy: Any, tieru_ref: Any) -> None:
            self._base = base_router
            self._overrides = dict(overrides or {})
            self._policy = policy
            self.tieru = tieru_ref
            self.settings = base_router.settings
            self._scoped_clients: dict[str, Any] = {}
            self.routing_history: list[dict[str, Any]] = []

        def assignment(self, role_name: str):
            return resolve_effective_role_assignment(
                role_name,
                self.settings,
                policy=self._policy,
                eval_overrides=self._overrides,
            )

        def model(self, role_name: str) -> str:
            return self.assignment(role_name).primary_model

        def provider(self, role_name: str) -> str:
            return self.assignment(role_name).primary_provider

        def role(self, role_name: str) -> Any:
            assign = self.assignment(role_name)
            from tieru.config import ModelRole
            p_cfg = self.settings.providers.get(assign.primary_provider)
            proto = p_cfg.protocol if p_cfg else "openai"
            base_u = p_cfg.base_url if p_cfg else None
            key_env = p_cfg.api_key_env if p_cfg else ""
            return ModelRole(
                provider=assign.primary_provider,
                protocol=proto,
                model=assign.primary_model,
                base_url=base_u,
                api_key_env=key_env,
            )

        def client(self, role_name: str) -> Any:
            assign = self.assignment(role_name)
            client_key = f"{role_name}:{assign.primary_provider}:{assign.primary_model}"
            if client_key not in self._scoped_clients:
                if getattr(self._base, "_shared_client", None) is not None:
                    raw_client = self._base._shared_client
                elif assign.selection_source == SelectionSource.DEFAULT.value:
                    from tieru.fabric.roles import DEFAULT_ROLE_CONFIG_MAP, ModelRole
                    if role_name in ("main", "small", "judge"):
                        broad = role_name
                    elif any(role_name == m.value for m in ModelRole):
                        broad = DEFAULT_ROLE_CONFIG_MAP.get(ModelRole(role_name), "main")
                    else:
                        broad = "main"
                    raw_client = self._base.client(broad)
                else:
                    cand = ModelCandidate(
                        candidate_id=f"scoped_{role_name}_{assign.primary_model.replace(':', '_')}",
                        provider=assign.primary_provider,
                        model=assign.primary_model,
                        protocol=self.role(role_name).protocol,
                        base_url=self.role(role_name).base_url,
                    )
                    raw_client = get_client_for_target(self.settings, cand, role=role_name)
                self._scoped_clients[client_key] = self._wrap_client(raw_client, assign)
            return self._scoped_clients[client_key]

        def client_for(self, target, role: str = "main"):
            return self._base.client_for(target, role)

        def _record_event(self, assign, fallback_used: bool = False, routing_error: str = ""):
            p = assign.fallback_provider if fallback_used and assign.fallback_provider else assign.primary_provider
            m = assign.fallback_model if fallback_used and assign.fallback_model else assign.primary_model
            source = "fallback" if fallback_used else assign.selection_source
            role_val = assign.role.value if hasattr(assign.role, "value") else str(assign.role)
            event = {
                "role": role_val,
                "provider": p,
                "model": m,
                "selection_source": source,
                "fallback_used": fallback_used,
            }
            if routing_error:
                event["routing_error"] = routing_error
            self.routing_history.append(event)
            if self.tieru is not None and getattr(self.tieru, "tracer", None):
                self.tieru.tracer.event("model_role_routed", event)

        def _wrap_client(self, raw_client: Any, assign) -> Any:
            router = self

            class _Wrapper:
                def __init__(self, client):
                    self._client = client
                    self.messages = _MessagesWrapper(client.messages)

            class _MessagesWrapper:
                def __init__(self, messages):
                    self._messages = messages

                def create(self, *args, **kwargs):
                    router._record_event(assign, fallback_used=False)
                    try:
                        return self._messages.create(*args, **kwargs)
                    except Exception as exc:
                        if router._is_infrastructure_failure(exc) and assign.fallback_model and assign.fallback_provider:
                            if assign.tool_calling_required and not router._supports_tool_calling(assign.fallback_model):
                                router._record_event(assign, fallback_used=False, routing_error="fallback_lacks_tool_support")
                                raise
                            fb_cand = ModelCandidate(
                                candidate_id=f"fb_{assign.role}_{assign.fallback_model.replace(':', '_')}",
                                provider=assign.fallback_provider,
                                model=assign.fallback_model,
                                protocol=router.role(str(assign.role)).protocol,
                                base_url=router.role(str(assign.role)).base_url,
                            )
                            fb_client = get_client_for_target(router.settings, fb_cand, role=str(assign.role))
                            fb_kwargs = dict(kwargs)
                            fb_kwargs["model"] = assign.fallback_model
                            router._record_event(assign, fallback_used=True)
                            return fb_client.messages.create(*args, **fb_kwargs)
                        raise

                def stream(self, *args, **kwargs):
                    router._record_event(assign, fallback_used=False)
                    return self._messages.stream(*args, **kwargs)

            return _Wrapper(raw_client)

        def _is_infrastructure_failure(self, exc: Exception) -> bool:
            from tieru.loop.adapters import ModelError
            err_str = str(exc).lower()
            if isinstance(exc, (TimeoutError, ConnectionError, ModelError)):
                return True
            return any(w in err_str for w in ("timeout", "connection refused", "network unreachable", "503", "502"))

        def _supports_tool_calling(self, model: str) -> bool:
            low = model.lower()
            return not ("text-only" in low or "no-tools" in low or "unsupported_tools" in low)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._base, name)

        def __setattr__(self, name: str, value: Any) -> None:
            if name in {"_base", "_overrides", "_policy", "tieru", "settings", "routing_history", "_scoped_clients"}:
                super().__setattr__(name, value)
            elif hasattr(self, "_base") and (hasattr(self._base, name) or not hasattr(self, name)):
                setattr(self._base, name, value)
            else:
                super().__setattr__(name, value)

    router = _RoleAwareTaskRouter(tieru.model_router, role_overrides, role_policy, tieru)
    tieru.model_router = router

    planner = ModelTaskPlanner(router, role="planner", limits=bounds)
    verifier = LayeredTaskVerifier(
        ModelResultVerifier(
            router,
            role="step_verifier",
            offline_fallback_role="small",
            offline_fallback_enabled=True,
            store=store,
        )
    )
    from tieru.tasks.reviewer import ModelPlanReviewer

    reviewer = ModelPlanReviewer(router, role="replanner", limits=bounds, store=store)
    contract_builder = ModelGoalContractBuilder(router, role="contract_builder", limits=bounds)
    goal_verifier = LayeredTaskGoalVerifier(judge=ModelGoalJudge(router, role="goal_verifier", store=store))
    executor = TaskExecutor(
        store,
        TieruStepRunner(tieru),
        verifier,
        reviewer=reviewer,
        goal_verifier=goal_verifier,
        replay=tieru.replay,
        limits=bounds,
    )
    return TaskService(store, planner, executor, contract_builder=contract_builder)

