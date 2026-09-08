"""Top-level Model Fabric v1 routing above the existing ModelRouter."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from tieru.context import ContextBuilder
from tieru.fabric.analyze import TaskAnalyzer
from tieru.fabric.availability import AvailabilityService
from tieru.fabric.candidates import CandidateRegistry
from tieru.fabric.models import ExecutionMode, RouteDecision, TaskProfile
from tieru.fabric.performance import ModelPerformanceService
from tieru.fabric.policy import FabricPolicy
from tieru.fabric.profiles import profiles_for
from tieru.fabric.scoring import CandidateScorer
from tieru.fabric.selection import ModelSelector, RoutingOverrides

_CLASSIFIER_PROMPT = """Classify only the observable task shape. Return JSON:
{{"task_type":"greeting|chat|lookup|summarization|coding|analysis|planning|tool_task|unknown",
"complexity":"low|normal|high"}}
Do not execute the task. Message: {message}"""
_TYPES = {"greeting", "chat", "lookup", "summarization", "coding", "analysis",
          "planning", "tool_task", "unknown"}


class ModelFabric:
    """Choose HOW one turn runs; ModelRouter still chooses configured clients."""

    def __init__(self, settings, model_router, *, analyzer=None, classifier=None,
                 replay=None, availability=None, performance=None):
        self.settings = settings
        self.model_router = model_router
        self.analyzer = analyzer or TaskAnalyzer()
        self.classifier = classifier
        self.policy = FabricPolicy(settings.fabric_default_mode)
        self.registry = CandidateRegistry(settings)
        self.availability = availability or AvailabilityService(
            settings,
            probe=(lambda _candidate: True)
            if getattr(model_router, "_shared_client", None) is not None else None,
            credential_override=getattr(model_router, "_shared_client", None) is not None,
        )
        self.performance = performance or ModelPerformanceService(
            replay, min_samples=settings.fabric_min_history_samples
        )
        self.scorer = CandidateScorer(settings.fabric_weights, self.performance)
        self.selector = ModelSelector(
            settings, self.registry, self.availability, self.scorer
        )

    def route(self, message: str, *, allow_classifier: bool = True,
              overrides: RoutingOverrides | None = None) -> RouteDecision:
        fallback = False
        source = "deterministic"
        extra_reasons: tuple[str, ...] = ()
        try:
            task = self.analyzer.analyze(message)
        except Exception as exc:  # noqa: BLE001 - orchestration must fail safe
            task = TaskProfile(
                "unknown", "normal", reason_codes=("analyzer_failure",)
            )
            fallback = True
            source = "deterministic_fallback"
            extra_reasons = (f"analyzer_{type(exc).__name__}",)

        if (allow_classifier and self.settings.fabric_use_small_classifier
                and task.task_type == "unknown"):
            try:
                assisted = self._classify(message)
                if assisted is not None:
                    task = assisted
                    source = "small_model_signal"
                else:
                    fallback = True
                    source = "deterministic_fallback"
                    extra_reasons += ("classifier_malformed",)
            except Exception as exc:  # noqa: BLE001 - optional signal only
                fallback = True
                source = "deterministic_fallback"
                extra_reasons += (f"classifier_{type(exc).__name__}",)

        mode, reasons = self.policy.select(task)
        overrides = overrides or RoutingOverrides()
        if overrides.force_mode:
            mode = ExecutionMode(overrides.force_mode)
            reasons = (*reasons, "user_forced_execution_mode")
        reasons = tuple(dict.fromkeys([*reasons, *extra_reasons]))
        profile = profiles_for(self.settings)[mode]
        selection = self.selector.select(task, profile, overrides=overrides)
        return self._decision(
            task, profile, reasons, fallback_used=fallback, classifier_source=source,
            selection=selection,
        )

    def fallback(self, reason: str = "fabric_failure") -> RouteDecision:
        task = TaskProfile("unknown", "normal", reason_codes=(reason,))
        profile = profiles_for(self.settings)[ExecutionMode.STANDARD]
        return self._decision(
            task, profile, (reason,), fallback_used=True,
            classifier_source="deterministic_fallback",
            selection=None,
        )

    def explain(self, message: str, *, overrides: RoutingOverrides | None = None) -> dict:
        """Deterministic inspection; never invokes the optional classifier."""
        return self.route(
            message, allow_classifier=False, overrides=overrides
        ).public()

    def modes(self) -> list[dict]:
        return [profile.public() for profile in profiles_for(self.settings).values()]

    def status(self) -> dict:
        return {
            "enabled": self.settings.fabric_enabled,
            "default_mode": self.settings.fabric_default_mode,
            "use_small_classifier": self.settings.fabric_use_small_classifier,
            "routing_policy": self.settings.fabric_routing_policy,
            "availability_ttl_seconds": self.settings.fabric_availability_ttl_seconds,
            "min_history_samples": self.settings.fabric_min_history_samples,
            "max_fallbacks": self.settings.fabric_max_fallbacks,
            "weights": dict(self.settings.fabric_weights),
            "models": self.models(),
            "performance": self.performance.stats(),
            "modes": self.modes(),
        }

    def models(self, *, available_only: bool = False, refresh: bool = False) -> list[dict]:
        result = []
        for candidate in self.registry.all():
            availability = self.availability.check(candidate, refresh=refresh)
            if available_only and not availability.available:
                continue
            result.append({**candidate.public(), "availability": availability.public()})
        return result

    def refresh(self) -> list[dict]:
        return [item.public() for item in self.availability.refresh(self.registry.all())]

    def candidate(self, candidate_id: str):
        return self.registry.get(candidate_id)

    def fallback_decision(self, decision: RouteDecision, reason: str) -> RouteDecision:
        selection = decision.model_selection
        if selection is None:
            raise RuntimeError("No Fabric selection is available for fallback")
        if selection.fallback_count >= self.settings.fabric_max_fallbacks:
            raise RuntimeError("Fabric fallback limit reached")
        failed = self.registry.get(selection.candidate_id)
        self.availability.mark_unavailable(failed, reason)
        next_selection = self.selector.select(decision.task_profile, decision.profile)
        next_selection = replace(
            next_selection,
            fallback_count=selection.fallback_count + 1,
            initial_candidate_id=selection.initial_candidate_id or selection.candidate_id,
            reasons=(*next_selection.reasons, "hard_infrastructure_fallback", reason),
        )
        return replace(
            decision,
            provider=next_selection.provider,
            model=next_selection.model,
            reason_codes=(*decision.reason_codes, reason),
            explanation=(
                f"{decision.explanation}; fallback to {next_selection.candidate_id}: "
                f"{reason.replace('_', ' ')}"
            ),
            fallback_used=True,
            model_selection=next_selection,
        )

    def _decision(
        self, task, profile, reasons, *, fallback_used: bool, classifier_source: str,
        selection,
    ) -> RouteDecision:
        role = profile.role
        explanation = f"{profile.mode.value.upper()} selected: " + "; ".join(
            item.replace("_", " ") for item in reasons
        )
        return RouteDecision(
            profile.mode, profile, task,
            selection.provider if selection else self.model_router.provider(role),
            selection.model if selection else self.model_router.model(role),
            role, reasons, explanation, fallback_used, classifier_source, selection,
        )

    def _classify(self, message: str) -> TaskProfile | None:
        if self.classifier is not None:
            raw: Any = self.classifier(message)
        else:
            builder = ContextBuilder(max_block_bytes=4096)
            builder.add_control(
                _CLASSIFIER_PROMPT.replace("Message: {message}", ""),
                source="fabric_classifier",
            )
            builder.add_user(message, source="user")
            assembly = builder.build()
            response = self.model_router.client("small").messages.create(
                model=self.model_router.model("small"), max_tokens=160,
                system=assembly.system, messages=list(assembly.messages),
            )
            raw = "".join(block.text for block in response.content if block.type == "text")
        if isinstance(raw, TaskProfile):
            return raw
        if isinstance(raw, dict):
            value = raw
        else:
            text = str(raw)
            if "{" not in text:
                return None
            value = json.loads(text[text.index("{"):text.rindex("}") + 1])
        task_type = str(value.get("task_type", "unknown"))
        complexity = str(value.get("complexity", "normal"))
        if task_type not in _TYPES or complexity not in {"low", "normal", "high"}:
            return None
        # A classifier is only consulted for deterministic UNKNOWN. Its output
        # can describe shape, but cannot grant tools, memory, or permissions.
        return TaskProfile(
            task_type, complexity,
            requires_deep_context=task_type == "analysis" and complexity == "high",
            requires_verification=task_type == "analysis" and complexity == "high",
            reason_codes=("optional_classifier_signal",),
        )
