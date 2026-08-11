"""Policy-first model filtering and deterministic selection."""

from __future__ import annotations

from dataclasses import dataclass, replace

from tieru.fabric.models import (
    CandidateEvaluation,
    ExecutionMode,
    ModelSelection,
    PrivacyPolicy,
)


class ModelSelectionError(RuntimeError):
    def __init__(self, message: str, evaluations=()):
        super().__init__(message)
        self.evaluations = tuple(evaluations)


@dataclass(frozen=True)
class RoutingOverrides:
    force_local: bool = False
    preferred_model: str = ""
    force_mode: str = ""


class ModelSelector:
    def __init__(self, settings, registry, availability, scorer):
        self.settings = settings
        self.registry = registry
        self.availability = availability
        self.scorer = scorer

    def select(self, task, profile, *, overrides: RoutingOverrides | None = None):
        overrides = overrides or RoutingOverrides()
        policy = PrivacyPolicy(self.settings.fabric_routing_policy)
        local_only = (
            policy is PrivacyPolicy.LOCAL_ONLY
            or overrides.force_local
            or task.privacy == "local_only"
        )
        required = self.required_capabilities(task, profile.mode)
        evaluations: list[CandidateEvaluation] = []
        survivors = []
        for candidate in self.registry.all():
            status = self.availability.check(candidate)
            reasons: list[str] = []
            if not status.available:
                reasons.append(status.reason)
            if not candidate.enabled:
                reasons.append("candidate_disabled")
            if local_only and not candidate.local:
                reasons.append("local_only_policy")
            if profile.role not in candidate.role_compatibility:
                reasons.append("role_incompatible")
            if overrides.preferred_model and candidate.candidate_id != overrides.preferred_model:
                reasons.append("not_preferred_model")
            for capability in required:
                supported = candidate.capabilities.get(capability) is True
                if (
                    capability == "long_context"
                    and candidate.context_limit is not None
                    and candidate.context_limit >= profile.max_tokens
                ):
                    supported = True
                if not supported:
                    reasons.append(f"{capability}_unsupported_or_unknown")
            if candidate.output_limit is not None and candidate.output_limit < profile.max_tokens:
                reasons.append("output_limit_insufficient")
            if (task.requires_deep_context and candidate.context_limit is not None
                    and candidate.context_limit < profile.max_tokens):
                reasons.append("context_limit_insufficient")
            unique = tuple(dict.fromkeys(reasons))
            evaluation = CandidateEvaluation(
                candidate.candidate_id, candidate.provider, candidate.model,
                not unique, unique, availability=status,
            )
            evaluations.append(evaluation)
            if not unique:
                survivors.append(candidate)

        # local_first is a policy stage, not a score that cloud can outbid.
        if policy is PrivacyPolicy.LOCAL_FIRST and not local_only:
            local_survivors = [candidate for candidate in survivors if candidate.local]
            if local_survivors:
                cloud_ids = {candidate.candidate_id for candidate in survivors if not candidate.local}
                survivors = local_survivors
                evaluations = [
                    replace(item, eligible=False,
                            exclusion_reasons=("suitable_local_candidate_available",))
                    if item.candidate_id in cloud_ids else item
                    for item in evaluations
                ]

        ranked = []
        evaluation_by_id = {item.candidate_id: item for item in evaluations}
        for candidate in survivors:
            total, breakdown, history = self.scorer.score(
                candidate, required=required, policy=policy.value,
                execution_mode=profile.mode.value, task_type=task.task_type,
            )
            reasons = ["available", "policy_allowed", "required_capabilities_supported"]
            reasons.append(
                "history_sufficient" if history["sufficient_samples"]
                else "history_neutral_insufficient_samples"
            )
            ranked.append((total, candidate.candidate_id, candidate, breakdown, tuple(reasons)))
            evaluation_by_id[candidate.candidate_id] = replace(
                evaluation_by_id[candidate.candidate_id], total_score=total,
                score_breakdown=breakdown,
            )
        evaluations = [evaluation_by_id[item.candidate_id] for item in evaluations]
        ranked.sort(key=lambda item: (-item[0], item[1]))
        if not ranked:
            preferred = f" preferred candidate {overrides.preferred_model!r}" if overrides.preferred_model else ""
            raise ModelSelectionError(
                f"No eligible model candidate for {profile.mode.value}{preferred}", evaluations
            )
        total, _alias, selected, breakdown, reasons = ranked[0]
        fallback_chain = tuple(item[2].candidate_id for item in ranked[1:])
        return ModelSelection(
            selected.candidate_id, selected.provider, selected.model, profile.role,
            total, breakdown, tuple(evaluations), reasons, fallback_chain,
            initial_candidate_id=selected.candidate_id,
        )

    @staticmethod
    def required_capabilities(task, mode: ExecutionMode) -> tuple[str, ...]:
        required = ["text"]
        if mode is ExecutionMode.AGENT or task.requires_tools:
            required.append("tool_calling")
        if mode is ExecutionMode.DEEP and task.requires_deep_context:
            required.append("long_context")
        return tuple(required)
