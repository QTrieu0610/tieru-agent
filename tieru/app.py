"""Wiring — builds one Tieru from its parts. Gateways call `respond()`.

This file is the assembly diagram in code: config → db → tools → memory →
session → loop. If you want to understand the repo in one place, start here.
"""

from __future__ import annotations

from typing import Any

from tieru.config import Settings, load_settings
from tieru.context import ContextBlock, ContextBuilder
from tieru.db import connect
from tieru.fabric import ModelFabric, ModelSelectionError
from tieru.fabric.availability import AvailabilityService
from tieru.fabric.fallback import fallback_reason
from tieru.fabric.verify import verify_result
from tieru.loop.agent import LoopResult, Observer, run_loop
from tieru.loop.models import ModelRouter
from tieru.ops.tracing import Tracer, compose
from tieru.replay import ReplayRecorder, ReplayService, new_run_id
from tieru.runtime.session import Session
from tieru.tools import build_registry
from tieru.tools.registry import ToolRegistry


class Tieru:
    def __init__(self, settings: Settings | None = None, client=None, conn=None,
                 approval_handler=None):
        # `client` and `conn` are injectable: evals swap in a scripted model,
        # the dashboard injects a cross-thread connection. Same seam either way.
        self.settings = settings or load_settings()
        self.settings.ensure_home()
        self.conn = conn or connect(self.settings.home)
        self.model_router = ModelRouter(self.settings, shared_client=client)
        self.replay = ReplayService(self.conn, self.settings)
        self.fabric = ModelFabric(
            self.settings, self.model_router, replay=self.replay,
            availability=(AvailabilityService(
                self.settings, probe=lambda _candidate: True,
                credential_override=True,
            ) if client is not None else None),
        )
        bootstrap = None
        if self.settings.fabric_enabled and self.settings.fabric_models:
            candidates = sorted(
                self.fabric.registry.all(),
                key=lambda candidate: (not candidate.local, candidate.candidate_id),
            )
            for candidate in candidates:
                if not candidate.enabled or candidate.capabilities.get("text") is not True:
                    continue
                try:
                    self.model_router.client_for(candidate, "small")
                except (Exception, SystemExit):  # unavailable optional target
                    candidate_ready = False
                else:
                    candidate_ready = True
                if candidate_ready:
                    bootstrap = candidate
                    break
        if bootstrap is not None:
            self.client = self.model_router.client_for(bootstrap, "main")
            self.small_client = self.model_router.client_for(bootstrap, "small")
        else:
            self.client = self.model_router.client("main")
            self.small_client = self.model_router.client("small")

        # Memory first: the memory-management tools need it.
        from tieru.memory import Memory

        self.memory = Memory(self.conn, self.settings, self.small_client)
        if bootstrap is not None:
            self.memory.set_model(
                self.small_client, bootstrap.model, bootstrap.provider
            )
        self.tools = build_registry(
            self.conn, self.settings, self.memory, approval_handler=approval_handler
        )
        # QUICK receives no schemas. This separate empty view leaves the shared
        # registry and its Trust boundary untouched for every other mode.
        self.no_tools = ToolRegistry(trust_policy=self.settings.trust_policy)
        self.mcp_bridge = getattr(self.tools, "mcp_bridge", None)
        self.session = Session(self.settings, memory=self.memory)
        self.tracer = Tracer(self.settings)
        from tieru.shadow import ShadowService

        self.shadow = ShadowService(self.conn, self.settings)

        from tieru.tasks.service import build_task_service

        self.tasks = build_task_service(self)
        from tieru.capabilities import CapabilityRouter, CapabilityRouterConfig

        cap_config = CapabilityRouterConfig(
            max_visible_tools=getattr(self.settings, "capability_max_visible_tools", 8),
            min_score=getattr(self.settings, "capability_min_score", 0.30),
            top_k=getattr(self.settings, "capability_top_k", 4),
        )
        self.capability_router = CapabilityRouter(cap_config)

    def close(self) -> None:
        """Release external resources (MCP subprocesses). Called when the
        dashboard rebuilds the agent after a settings change."""
        if self.mcp_bridge is not None:
            self.mcp_bridge.close()
        browser = getattr(self.tools, "browser", None)
        if browser is not None:
            browser.close()

    def respond(self, user_message: str, observer: Observer | None = None,
                source: str = "cli", stream: bool = False,
                context_blocks: tuple[ContextBlock, ...] = (),
                routing_query: str | None = None,
                task_id: str | None = None,
                task_store: Any = None,
                role: str | None = None,
                tool_choice_policy: Any = None) -> LoopResult:
        """One full turn: assemble working memory → run the loop → persist.
        `source` tags which gateway the message arrived through (cli / voice /
        telegram / dashboard), so the unified chat can show its origin.
        `stream=True` streams the reply text token by token to the observer.
        Everything that happens is both shown (observer) and recorded (tracer)."""
        # capture the gate + graph decisions as they flow by, so we can persist
        # them with the turn (the reopened-thread telemetry the dashboard shows)
        import time
        captured: dict = {}
        run_id = new_run_id()
        recorder = ReplayRecorder(self.replay)
        effective_role = role or ("executor" if source == "task" else "main")
        active_role = self.model_router.role(effective_role)
        recorder.start(
            run_id=run_id,
            session_id=self.session.session_id,
            source=source,
            role=effective_role,
            model=active_role.model,
            provider=active_role.provider,
            user_input=user_message,
        )

        def _capture(kind, ev):
            if kind == "gate":
                captured["gate"] = {"decision": ev.get("decision"), "reason": ev.get("reason")}
            if kind == "route":
                captured["graph_route"] = {"target": ev.get("target"), "reason": ev.get("reason")}
            if kind == "triage":
                captured["triage_reason"] = ev.get("reason")
            if kind == "graph_end":
                captured["graph_path"] = ev.get("path")
            if kind in {
                "tool_requested", "tool_started", "tool_completed", "tool_failed",
                "tool_denied", "tool",
            }:
                captured["tool_activity"] = int(captured.get("tool_activity", 0)) + 1
        fanout = compose(observer, self.tracer.event, recorder.event, _capture)

        def notify(kind, event):
            fanout(kind, {**event, "run_id": run_id})

        decision = self.fabric.fallback("fabric_disabled")
        t0 = time.perf_counter()
        try:
            with self.tracer.turn(
                user_message,
                run_id=run_id,
                session_id=self.session.session_id,
                source=source,
            ):
                if self.settings.fabric_enabled:
                    try:
                        decision = self.fabric.route(user_message)
                    except ModelSelectionError:
                        # Policy/capability/availability exclusion is an honest
                        # stop. Falling through to the role target could bypass
                        # local_only or another hard rule.
                        raise
                    except Exception as exc:  # noqa: BLE001 - M11 compatibility fallback
                        if self.settings.fabric_routing_policy == "local_only":
                            raise
                        decision = self.fabric.fallback(
                            f"fabric_{type(exc).__name__}"
                        )
                    task = decision.task_profile
                    notify("fabric_analysis", {
                        "task_type": task.task_type,
                        "complexity": task.complexity,
                        "requires_tools": task.requires_tools,
                        "requires_memory": task.requires_memory,
                        "requires_deep_context": task.requires_deep_context,
                        "requires_verification": task.requires_verification,
                        "signals": list(task.signals),
                    })
                    notify("fabric_route", {
                        "task_type": task.task_type,
                        "complexity": task.complexity,
                        "execution_mode": decision.mode.value,
                        "role": decision.role,
                        "model": decision.model,
                        "provider": decision.provider,
                        "reason_codes": list(decision.reason_codes),
                        "classifier_source": decision.classifier_source,
                        "fallback_used": decision.fallback_used,
                        "max_tokens": decision.profile.max_tokens,
                        "max_iterations": decision.profile.max_iterations,
                        "history_turns": decision.profile.history_turns,
                        "tools_enabled": decision.profile.tools_enabled,
                        "memory_enabled": decision.profile.memory_enabled,
                        "verification_enabled": decision.profile.verification_enabled,
                    })
                    self._notify_selection(decision, notify)
                    if decision.fallback_used:
                        notify("fabric_fallback", {
                            "execution_mode": decision.mode.value,
                            "reason_codes": list(decision.reason_codes),
                        })
                # The graph front door is optional and can NEVER make Tieru worse:
                # flag off → this is exactly the old code path; flag on → the triage
                # graph decides quick vs full, and any failure anywhere falls open
                # to the plain loop below (same fail-open rule as the retrieval gate).
                if task_id is None and context_blocks:
                    for b in context_blocks:
                        if getattr(b, "metadata", None) and b.metadata.get("task_id"):
                            task_id = b.metadata["task_id"]
                            break
                if task_id is not None and task_store is None:
                    task_store = getattr(getattr(self, "tasks", None), "store", None)
                result = None
                if self.settings.fabric_enabled:
                    decision, result = self._run_profiled_with_fallback(
                        user_message, decision, notify, stream, context_blocks,
                        routing_query=routing_query,
                        task_id=task_id,
                        task_store=task_store,
                        tool_choice_policy=tool_choice_policy,
                    )
                elif self.settings.graph_workflows:
                    try:
                        result = self._respond_via_graph(
                            user_message, notify, stream, context_blocks
                        )
                    except Exception as exc:
                        notify("graph_end", {"workflow": "triage", "ms": 0, "steps": 0,
                                             "path": [], "error": repr(exc)})
                        result = None
                if result is None:
                    result = self._run_full_turn(
                        user_message, notify, stream, context_blocks,
                        routing_query=routing_query,
                        task_id=task_id,
                        task_store=task_store,
                        role=effective_role,
                        tool_choice_policy=tool_choice_policy,
                    )

                quick = captured.get("graph_route", {}).get("target") == "quick_reply"

                def _status(out: str) -> str:
                    low = (out or "").lower()
                    if "tool_permission_denied" in low:
                        return "denied"
                    return "error" if ("failed" in low or "timed out" in low
                                       or low.startswith("error")) else "ok"

                role = decision.role if self.settings.fabric_enabled else (
                    "small" if quick else "main"
                )
                verification = (
                    verify_result(
                        self.model_router, decision, user_message, result.reply, notify,
                        candidate=self._candidate_for(decision),
                    )
                    if self.settings.fabric_enabled
                    else {"status": "skipped"}
                )
                latency_ms = int((time.perf_counter() - t0) * 1000)
                meta = {
                    "run_id": run_id,
                    "gate": captured.get("gate"),
                    "graph": ({"workflow": "triage",
                               "route": "quick" if quick else "full",
                               "reason": captured.get("triage_reason", ""),
                               "path": captured.get("graph_path")}
                              if "graph_route" in captured else None),
                    "fabric": decision.public() if self.settings.fabric_enabled else None,
                    "fabric_verification": verification,
                    "iterations": result.iterations,
                    "latency_ms": latency_ms,
                    "tools": [{"tool": c["tool"], "status": _status(c["output"])}
                              for c in result.tool_calls],
                    "model": (
                        decision.model if self.settings.fabric_enabled
                        else self.model_router.model(role)
                    ),
                    "provider": (
                        decision.provider if self.settings.fabric_enabled
                        else self.model_router.provider(role)
                    ),
                }
                result.run_id = run_id
                self.session.add_exchange(user_message, result.reply, tool_calls=result.tool_calls,
                                          source=source, meta=meta)
                if (self.memory is not None
                        and (not self.settings.fabric_enabled
                             or decision.profile.memory_enabled)):
                    self.memory.maybe_consolidate(notify=notify)
                    self.memory.export_markdown()   # keep MEMORY.md in sync
                notify("final_output", {"output": result.reply, "reference": "chat_log"})

            self.tracer.end_turn(result.reply, result.iterations, run_id=run_id)
            recorder.complete(
                output=result.reply,
                iterations=result.iterations,
                latency_ms=latency_ms,
                role=role,
                model=meta["model"],
                provider=meta["provider"],
            )
            if recorder.degraded:
                self.tracer.event(
                    "replay_degraded", {"run_id": run_id, "errors": recorder.errors[:5]}
                )
            else:
                # Advisory post-processing only. A Shadow failure can never
                # change the completed Replay, response, Memory, tools, or Trust.
                try:
                    self.shadow.observer = lambda kind, event: self.tracer.event(
                        kind, {**event, "run_id": run_id}
                    )
                    self.shadow.observe(run_id)
                except Exception as exc:  # noqa: BLE001 - strict failure isolation
                    self.tracer.event(
                        "shadow_processing_error",
                        {"run_id": run_id, "error_code": type(exc).__name__},
                    )
            return result
        except Exception as exc:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            error_code = type(exc).__name__
            self.tracer.event(
                "run_failed",
                {"run_id": run_id, "error_code": error_code,
                 "error_summary": str(exc)[:500]},
            )
            failed_role = decision.role if self.settings.fabric_enabled else "main"
            recorder.fail(
                error_code=error_code,
                error_summary=str(exc),
                latency_ms=latency_ms,
                role=failed_role,
                model=(decision.model if self.settings.fabric_enabled
                       else self.model_router.model(failed_role)),
                provider=(decision.provider if self.settings.fabric_enabled
                          else self.model_router.provider(failed_role)),
            )
            raise

    def _run_full_turn(
        self, user_message: str, notify, stream: bool,
        context_blocks: tuple[ContextBlock, ...] = (),
        routing_query: str | None = None,
        task_id: str | None = None,
        task_store: Any = None,
        role: str = "main",
        tool_choice_policy: Any = None,
    ) -> LoopResult:
        """The classic turn: assemble working memory, run THE loop. Extracted
        verbatim so the graph's full_agent node calls the SAME code as the
        flag-off default — loop-as-a-node can never drift from loop-as-default."""
        # Working memory is a bounded window: only the last N turns (2 rows
        # each) enter the prompt, so context/cost/latency stay flat no matter
        # how long the conversation runs. Older turns live in state.db and
        # come back via the retrieval gate + episodic memory when relevant.
        window = self.settings.history_turns * 2
        assembly = self.session.build_context(
            user_message, notify=notify, history=self.session.history[-window:],
            extra_blocks=context_blocks,
        )

        if hasattr(self.model_router, "assignment"):
            assign = self.model_router.assignment(role)
            notify("model_role_routed", {
                "role": role,
                "provider": assign.primary_provider,
                "model": assign.primary_model,
                "selection_source": assign.selection_source,
                "fallback_used": False,
            })

        return run_loop(
            client=self.model_router.client(role),
            model=self.model_router.model(role),
            system=assembly.system,
            messages=list(assembly.messages),
            tools=self.tools,
            max_iterations=self.settings.max_iterations,
            max_tokens=self.settings.max_tokens,
            observer=notify,
            stream=stream,
            provider=self.model_router.provider(role),
            role=role,
            capability_router=self.capability_router,
            routing_query=routing_query,
            task_id=task_id,
            task_store=task_store,
            tool_choice_policy=tool_choice_policy,
        )

    def _run_profiled_turn(
        self, user_message, decision, notify, stream,
        context_blocks: tuple[ContextBlock, ...] = (),
        routing_query: str | None = None,
        task_id: str | None = None,
        task_store: Any = None,
        **kwargs: Any,
    ) -> LoopResult:
        """Run the unchanged loop once with an immutable Fabric profile."""
        profile = decision.profile
        candidate = self._candidate_for(decision)
        selected_client = self.model_router.client_for(candidate, decision.role)
        if self.memory is not None:
            self.memory.set_model(selected_client, decision.model, decision.provider)
        window = profile.history_turns * 2
        assembly = self.session.build_context(
            user_message, notify=notify, memory_enabled=profile.memory_enabled,
            role_model=decision.model, role_provider=decision.provider,
            history=self.session.history[-window:], extra_blocks=context_blocks,
        )
        return run_loop(
            client=selected_client,
            model=decision.model,
            system=assembly.system,
            messages=list(assembly.messages),
            tools=self.tools if profile.tools_enabled else self.no_tools,
            max_iterations=profile.max_iterations,
            max_tokens=profile.max_tokens,
            observer=notify,
            stream=stream,
            provider=decision.provider,
            role=decision.role,
            capability_router=self.capability_router if profile.tools_enabled else None,
            routing_query=routing_query,
            task_id=task_id,
            task_store=task_store,
            tool_choice_policy=kwargs.get("tool_choice_policy"),
        )

    def _candidate_for(self, decision):
        selection = decision.model_selection
        return self.fabric.candidate(selection.candidate_id) if selection else None

    @staticmethod
    def _notify_selection(decision, notify) -> None:
        selection = decision.model_selection
        if selection is None:
            return
        notify("fabric_candidates", {
            "task_type": decision.task_profile.task_type,
            "execution_mode": decision.mode.value,
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "provider": item.provider,
                    "model": item.model,
                    "eligible": item.eligible,
                }
                for item in selection.candidates
            ],
        })
        for item in selection.candidates:
            if not item.eligible:
                notify("fabric_filter", {
                    "candidate_id": item.candidate_id,
                    "provider": item.provider,
                    "model": item.model,
                    "exclusion_reasons": list(item.exclusion_reasons),
                })
            elif item.total_score is not None:
                notify("fabric_score", {
                    "candidate_id": item.candidate_id,
                    "provider": item.provider,
                    "model": item.model,
                    "total_score": item.total_score,
                    "score_breakdown": item.score_breakdown,
                })
        notify("fabric_selection", {
            "task_type": decision.task_profile.task_type,
            "execution_mode": decision.mode.value,
            "candidate_id": selection.candidate_id,
            "initial_candidate_id": selection.initial_candidate_id,
            "provider": selection.provider,
            "model": selection.model,
            "total_score": selection.total_score,
            "score_breakdown": selection.score_breakdown,
            "fallback_count": selection.fallback_count,
            "fallback_used": selection.fallback_count > 0,
        })

    def _run_profiled_with_fallback(
        self, user_message, decision, notify, stream,
        context_blocks: tuple[ContextBlock, ...] = (),
        routing_query: str | None = None,
        task_id: str | None = None,
        task_store: Any = None,
        tool_choice_policy: Any = None,
    ):
        while True:
            tool_activity = {"count": 0}

            def watched(kind, event, activity=tool_activity):
                if kind in {
                    "tool_requested", "tool_started", "tool_completed", "tool_failed",
                    "tool_denied", "tool",
                }:
                    activity["count"] += 1
                notify(kind, event)

            try:
                kwargs: dict[str, Any] = {}
                if routing_query is not None:
                    kwargs["routing_query"] = routing_query
                if task_id is not None:
                    kwargs["task_id"] = task_id
                if task_store is not None:
                    kwargs["task_store"] = task_store
                if tool_choice_policy is not None:
                    kwargs["tool_choice_policy"] = tool_choice_policy
                if context_blocks:
                    try:
                        result = self._run_profiled_turn(
                            user_message, decision, watched, stream, context_blocks,
                            **kwargs,
                        )
                    except TypeError:
                        result = self._run_profiled_turn(
                            user_message, decision, watched, stream, context_blocks,
                        )
                else:
                    try:
                        result = self._run_profiled_turn(
                            user_message, decision, watched, stream,
                            **kwargs,
                        )
                    except TypeError:
                        result = self._run_profiled_turn(
                            user_message, decision, watched, stream,
                        )
                return decision, result
            except Exception as exc:
                reason = fallback_reason(exc)
                safe_to_restart = not stream and tool_activity["count"] == 0
                if not reason or not safe_to_restart:
                    if reason and not safe_to_restart:
                        notify("fabric_fallback", {
                            "from_candidate": (
                                decision.model_selection.candidate_id
                                if decision.model_selection else ""
                            ),
                            "reason": reason,
                            "status": "blocked_after_tool_activity_or_stream",
                            "fallback_count": (
                                decision.model_selection.fallback_count
                                if decision.model_selection else 0
                            ),
                        })
                    raise
                previous = decision
                try:
                    decision = self.fabric.fallback_decision(decision, reason)
                except Exception:
                    raise exc
                notify("fabric_fallback", {
                    "from_candidate": previous.model_selection.candidate_id,
                    "to_candidate": decision.model_selection.candidate_id,
                    "reason": reason,
                    "status": "retrying",
                    "fallback_count": decision.model_selection.fallback_count,
                })
                self._notify_selection(decision, notify)

    def _respond_via_graph(
        self, user_message: str, notify, stream: bool,
        context_blocks: tuple[ContextBlock, ...] = (),
    ) -> LoopResult | None:
        """One turn through the triage graph workflow. Returns None whenever
        the graph didn't produce an answer — respond() then falls open to the
        plain loop, so this path can only ever ADD speed, never lose a reply."""
        from tieru.graph import run_graph
        from tieru.graph.workflows.triage import (
            QUICK_REPLY_PROMPT,
            build_triage_graph,
            classify_message,
            todays_events,
        )

        def quick_reply(state: dict) -> str:
            import time

            builder = ContextBuilder(max_block_bytes=8192)
            builder.add_control(
                QUICK_REPLY_PROMPT.split("Today's calendar:", 1)[0],
                source="graph_quick_reply",
            )
            builder.add_data(state.get("calendar", ""), source="calendar")
            for block in context_blocks:
                builder.add(
                    block.trust, block.content, source=block.source,
                    metadata=block.metadata,
                )
            builder.add_user(state["message"], source="user")
            assembly = builder.build()
            started = time.perf_counter()
            notify("model_call_started", {"role": "small",
                                          "model": self.model_router.model("small"),
                                          "provider": self.model_router.provider("small"),
                                          "purpose": "quick_reply"})
            response = self.small_client.messages.create(
                model=self.model_router.model("small"), max_tokens=600,
                system=assembly.system, messages=list(assembly.messages))
            usage = getattr(response, "usage", None)
            notify("model_call_completed", {"iteration": 1, "role": "small",
                                            "model": self.model_router.model("small"),
                                            "provider": self.model_router.provider("small"),
                                            "purpose": "quick_reply",
                                            "stop_reason": getattr(response, "stop_reason", ""),
                                            "usage": {"in": getattr(usage, "input_tokens", 0),
                                                      "out": getattr(usage, "output_tokens", 0)},
                                            "duration_ms": int((time.perf_counter() - started) * 1000)})
            return "".join(b.text for b in response.content if b.type == "text")

        def classify_with_events(message: str):
            import time

            started = time.perf_counter()
            notify("model_call_started", {"role": "small",
                                          "model": self.model_router.model("small"),
                                          "provider": self.model_router.provider("small"),
                                          "purpose": "graph_triage"})
            decision = classify_message(
                self.small_client, self.model_router.model("small"), message
            )
            notify("model_call_completed", {"role": "small",
                                            "model": self.model_router.model("small"),
                                            "provider": self.model_router.provider("small"),
                                            "purpose": "graph_triage",
                                            "duration_ms": int((time.perf_counter() - started) * 1000),
                                            "stop_reason": "decision"})
            return decision

        graph = build_triage_graph(
            classify_fn=classify_with_events,
            calendar_fn=lambda: todays_events(self.settings.home),
            quick_fn=quick_reply,
            # the full path is the SAME method the flag-off default runs; the
            # engine's tagged notifier stamps its inner events with node=
            full_fn=lambda state: self._run_full_turn(
                state["message"], state.get("_notify", notify), stream, context_blocks),
        )
        state = run_graph(graph, {"message": user_message}, observer=notify)
        if isinstance(state.get("result"), LoopResult):
            return state["result"]
        if state.get("reply"):
            return LoopResult(reply=state["reply"], tool_calls=[], iterations=1)
        return None  # graph produced nothing → caller falls open to the loop


# Source compatibility for applications that imported the old class name.
tieru = Tieru
