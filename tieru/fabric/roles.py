"""Role taxonomy, capability profiles, and advisory model recommendations for M33."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ModelRole(StrEnum):
    """Explicit cognitive roles in Tieru's durable task architecture."""

    CONTRACT_BUILDER = "contract_builder"
    PLANNER = "planner"
    EXECUTOR = "executor"
    STEP_VERIFIER = "step_verifier"
    REPLANNER = "replanner"
    GOAL_VERIFIER = "goal_verifier"


class RecommendationDecision(StrEnum):
    KEEP_CURRENT = "KEEP_CURRENT"
    CONSIDER_SWITCH = "CONSIDER_SWITCH"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class RecommendationConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# Standard configuration mapping: associates each cognitive role with a default
# broad Settings role profile ('main', 'small', 'judge') for backwards compatibility.
DEFAULT_ROLE_CONFIG_MAP: dict[ModelRole, str] = {
    ModelRole.CONTRACT_BUILDER: "small",
    ModelRole.PLANNER: "small",
    ModelRole.EXECUTOR: "main",
    ModelRole.STEP_VERIFIER: "judge",
    ModelRole.REPLANNER: "small",
    ModelRole.GOAL_VERIFIER: "judge",
}


@dataclass(frozen=True)
class RoleCandidate:
    """An eligible candidate model for evaluation in one or more cognitive roles."""

    provider: str
    model: str
    protocol: str = "openai"
    base_url: str | None = None
    context_limit: int | None = None
    usage_telemetry: bool = True
    reachable: bool = True
    status: str = "ready"
    error: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "context_limit": self.context_limit,
            "usage_telemetry": self.usage_telemetry,
            "reachable": self.reachable,
            "status": self.status,
            "error": self.error,
        }


@dataclass(frozen=True)
class RoleCapabilityProfile:
    """Measured quality, safety, latency, and resource footprint for one role x candidate model."""

    role: str
    provider: str
    model: str
    cases: int
    runs: int
    success_rate: float
    malformed_output_rate: float
    safety_violation_rate: float
    average_latency_seconds: float
    p95_latency_seconds: float
    average_input_tokens: float | None
    average_output_tokens: float | None
    telemetry_coverage: float
    role_metrics: dict[str, float] = field(default_factory=dict)
    safety_gate_passed: bool = True
    rejection_reasons: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["rejection_reasons"] = list(self.rejection_reasons)
        return data


@dataclass(frozen=True)
class RoleRecommendation:
    """Advisory recommendation for a cognitive role based on multi-dimensional evaluation evidence."""

    role: str
    current_model: str
    recommended_model: str
    recommendation: RecommendationDecision
    confidence: RecommendationConfidence
    reason: str
    safety_gate_passed: bool = True

    def public(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "current_model": self.current_model,
            "recommended_model": self.recommended_model,
            "recommendation": self.recommendation.value,
            "confidence": self.confidence.value,
            "reason": self.reason,
            "safety_gate_passed": self.safety_gate_passed,
        }


@dataclass(frozen=True)
class ModelRoleBaseline:
    """Versioned model role capability baseline artifact."""

    schema_version: int = 1
    roles: dict[str, str] = field(default_factory=dict)
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    profiles: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ==============================================================================
# M34 Evidence-Gated Role-Aware Model Routing
# ==============================================================================


class SelectionSource(StrEnum):
    """Origin of a model assignment for a cognitive role."""

    EXPLICIT_CONFIG = "explicit_config"
    EVIDENCE_POLICY = "evidence_policy"
    DEFAULT = "default"
    FALLBACK = "fallback"


ROLE_REQUIREMENTS: dict[ModelRole, dict[str, bool]] = {
    ModelRole.CONTRACT_BUILDER: {"tools_must_be_empty": True, "tool_calling_required": False},
    ModelRole.PLANNER: {"tools_must_be_empty": True, "tool_calling_required": False},
    ModelRole.EXECUTOR: {"tools_must_be_empty": False, "tool_calling_required": True},
    ModelRole.STEP_VERIFIER: {"tools_must_be_empty": True, "tool_calling_required": False},
    ModelRole.REPLANNER: {"tools_must_be_empty": True, "tool_calling_required": False},
    ModelRole.GOAL_VERIFIER: {"tools_must_be_empty": True, "tool_calling_required": False},
}


@dataclass(frozen=True)
class RoleModelAssignment:
    """Explicit model assignment for one cognitive role with bounded fallback and evidence."""

    role: ModelRole
    primary_provider: str
    primary_model: str
    fallback_provider: str | None = None
    fallback_model: str | None = None
    fallback_used: bool = False
    evidence_source: str = ""
    confidence: str = "LOW"
    tool_calling_required: bool = False
    tools_must_be_empty: bool = False
    selection_source: str = SelectionSource.DEFAULT.value
    configured_model: str = ""
    recommended_model: str = ""
    effective_model: str = ""
    actually_executed_model: str = ""
    profile_matches_effective_model: bool = True

    def public(self) -> dict[str, Any]:
        eff_m = self.effective_model or self.primary_model
        cfg_m = self.configured_model or self.primary_model
        rec_m = self.recommended_model or ""
        act_m = self.actually_executed_model or (self.fallback_model if self.fallback_used else self.primary_model)
        return {
            "role": self.role.value if isinstance(self.role, ModelRole) else str(self.role),
            "primary_provider": self.primary_provider,
            "primary_model": self.primary_model,
            "fallback_provider": self.fallback_provider,
            "fallback_model": self.fallback_model,
            "fallback_used": self.fallback_used,
            "evidence_source": self.evidence_source,
            "confidence": self.confidence,
            "tool_calling_required": self.tool_calling_required,
            "tools_must_be_empty": self.tools_must_be_empty,
            "selection_source": self.selection_source,
            "configured_model": cfg_m,
            "recommended_model": rec_m,
            "effective_model": eff_m,
            "actually_executed_model": act_m,
            "profile_matches_effective_model": self.profile_matches_effective_model,
        }


@dataclass(frozen=True)
class ModelRolePolicy:
    """Versioned evidence-backed role policy artifact."""

    schema_version: int = 1
    generated_from_baseline: str = ""
    baseline_hash: str = ""
    corpus_hash: str = ""
    created_at: str = ""
    assignments: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelRolePolicy:
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            generated_from_baseline=str(data.get("generated_from_baseline", "")),
            baseline_hash=str(data.get("baseline_hash", "")),
            corpus_hash=str(data.get("corpus_hash", "")),
            created_at=str(data.get("created_at", "")),
            assignments=dict(data.get("assignments", {})),
            evidence_summary=dict(data.get("evidence_summary", {})),
        )


def compute_baseline_hash(data: dict[str, Any] | ModelRoleBaseline) -> str:
    """Compute deterministic SHA-256 hash of baseline artifact content."""
    import hashlib
    import json

    raw = data.to_dict() if isinstance(data, ModelRoleBaseline) else data
    clean = {
        "schema_version": raw.get("schema_version", 1),
        "roles": raw.get("roles", {}),
        "candidates": raw.get("candidates", {}),
        "profiles": raw.get("profiles", []),
        "recommendations": raw.get("recommendations", []),
    }
    encoded = json.dumps(clean, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_corpus_hash(corpus_dir: Any = None) -> str:
    """Compute deterministic hash of the role benchmark evaluation corpus."""
    import hashlib
    import json
    from pathlib import Path

    target_dir = Path(corpus_dir) if corpus_dir else Path("evals/corpus/role_benchmarks")
    if not target_dir.is_dir():
        return hashlib.sha256(b"empty_corpus").hexdigest()

    files = sorted(target_dir.glob("*.json"))
    hasher = hashlib.sha256()
    for file_path in files:
        hasher.update(file_path.name.encode("utf-8"))
        try:
            content = json.loads(file_path.read_text(encoding="utf-8"))
            hasher.update(json.dumps(content, sort_keys=True).encode("utf-8"))
        except Exception:
            hasher.update(file_path.read_bytes())
    return hasher.hexdigest()


def generate_policy_from_baseline(
    baseline: ModelRoleBaseline | dict[str, Any],
    *,
    baseline_path: str = "evals/baselines/model_role_baseline.json",
    corpus_dir: Any = None,
    created_at: str | None = None,
) -> ModelRolePolicy:
    """Generate an evidence-gated role policy artifact strictly adhering to safety & evidence gates."""
    from datetime import UTC, datetime

    raw = baseline.to_dict() if isinstance(baseline, ModelRoleBaseline) else baseline
    profiles = raw.get("profiles", [])
    recommendations = raw.get("recommendations", [])
    roles_map = raw.get("roles", {})
    candidates_map = raw.get("candidates", {})

    b_hash = compute_baseline_hash(raw)
    c_hash = compute_corpus_hash(corpus_dir)
    timestamp = created_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    profiles_by_role: dict[str, dict[str, dict[str, Any]]] = {}
    for p in profiles:
        profiles_by_role.setdefault(p["role"], {})[p["model"]] = p

    recs_by_role = {r["role"]: r for r in recommendations}

    assignments: dict[str, dict[str, Any]] = {}
    eligible_changes: list[dict[str, Any]] = []
    rejected_changes: list[dict[str, Any]] = []

    for role in ModelRole:
        role_key = role.value
        curr_model = roles_map.get(role_key, "gemma4:e2b")
        role_profiles = profiles_by_role.get(role_key, {})
        curr_profile = role_profiles.get(curr_model)
        rec = recs_by_role.get(role_key, {})

        # Default provider lookup from candidates map
        curr_provider = "ollama"
        for cand_info in candidates_map.values():
            if cand_info.get("model") == curr_model:
                curr_provider = cand_info.get("provider", "ollama")
                break

        reqs = ROLE_REQUIREMENTS.get(role, {"tools_must_be_empty": False, "tool_calling_required": False})

        # Evaluate candidate models for this role
        selected_candidate: dict[str, Any] | None = None
        best_gain = 0.0

        for cand_model, cand_profile in role_profiles.items():
            if cand_model == curr_model:
                continue

            # Hard safety gates
            safety_passed = cand_profile.get("safety_gate_passed", False)
            safety_viol_rate = cand_profile.get("safety_violation_rate", 0.0)
            curr_viol_rate = curr_profile.get("safety_violation_rate", 0.0) if curr_profile else 0.0
            rejection_reasons = list(cand_profile.get("rejection_reasons", []))

            # Quality delta
            cand_success = cand_profile.get("success_rate", 0.0)
            curr_success = curr_profile.get("success_rate", 0.0) if curr_profile else 0.0
            quality_delta = cand_success - curr_success

            # Case count and confidence check
            cases = cand_profile.get("cases", 0)
            confidence = rec.get("confidence", "LOW")
            rec_decision = rec.get("recommendation", "KEEP_CURRENT")

            # Invariant checks: false passes or constraint losses are immediate disqualifiers
            role_metrics = cand_profile.get("role_metrics", {})
            has_false_pass = (
                "step_false_pass_detected" in rejection_reasons
                or role_metrics.get("step_false_pass", 0.0) > 0.0
                or role_metrics.get("goal_false_pass", 0.0) > 0.0
                or role_metrics.get("false_pass", 0.0) > 0.0
            )
            has_constraint_loss = role_metrics.get("constraint_loss", 0.0) > 0.0

            eligible = (
                safety_passed
                and not has_false_pass
                and not has_constraint_loss
                and safety_viol_rate <= curr_viol_rate
                and quality_delta > 0.001
                and cases >= 3
                and confidence in ("HIGH", "MEDIUM")
                and rec_decision != "INSUFFICIENT_EVIDENCE"
            )

            if eligible and quality_delta > best_gain:
                best_gain = quality_delta
                selected_candidate = cand_profile
            else:
                reject_reason = "insufficient_quality_delta" if quality_delta <= 0.001 else (
                    "hard_safety_failure" if (not safety_passed or has_false_pass or has_constraint_loss) else (
                        "confidence_gate_failed" if confidence not in ("HIGH", "MEDIUM") else "insufficient_evidence"
                    )
                )
                rejected_changes.append({
                    "role": role_key,
                    "current_model": curr_model,
                    "candidate_model": cand_model,
                    "quality_delta": round(quality_delta, 4),
                    "safety_gate_passed": safety_passed,
                    "confidence": confidence,
                    "rejection_reason": reject_reason,
                })

        if selected_candidate is not None:
            cand_model = selected_candidate["model"]
            cand_provider = selected_candidate.get("provider", "ollama")
            eligible_changes.append({
                "role": role_key,
                "current_model": curr_model,
                "selected_model": cand_model,
                "quality_delta": round(best_gain, 4),
            })
            assignment = RoleModelAssignment(
                role=role,
                primary_provider=cand_provider,
                primary_model=cand_model,
                fallback_provider=curr_provider,
                fallback_model=curr_model,
                evidence_source=baseline_path,
                confidence=rec.get("confidence", "MEDIUM"),
                tool_calling_required=reqs.get("tool_calling_required", False),
                tools_must_be_empty=reqs.get("tools_must_be_empty", False),
                selection_source=SelectionSource.EVIDENCE_POLICY.value,
            )
        else:
            assignment = RoleModelAssignment(
                role=role,
                primary_provider=curr_provider,
                primary_model=curr_model,
                fallback_provider=None,
                fallback_model=None,
                evidence_source=baseline_path,
                confidence=rec.get("confidence", "LOW"),
                tool_calling_required=reqs.get("tool_calling_required", False),
                tools_must_be_empty=reqs.get("tools_must_be_empty", False),
                selection_source=SelectionSource.DEFAULT.value,
            )

        assignments[role_key] = assignment.public()

    evidence_summary = {
        "eligible_role_changes": eligible_changes,
        "rejected_role_changes": rejected_changes,
        "stale_evidence": False,
        "baseline_artifact": baseline_path,
        "roles_profiled": [r.value for r in ModelRole],
    }

    return ModelRolePolicy(
        schema_version=1,
        generated_from_baseline=baseline_path,
        baseline_hash=b_hash,
        corpus_hash=c_hash,
        created_at=timestamp,
        assignments=assignments,
        evidence_summary=evidence_summary,
    )


def validate_policy_artifact(
    data: dict[str, Any],
    available_models: set[str] | None = None,
) -> tuple[bool, list[str]]:
    """Validate policy artifact schema, role identifiers, provider types, and stale model inventory."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return False, ["Policy data must be a dictionary"]

    schema_version = data.get("schema_version")
    if schema_version != 1:
        errors.append(f"Unsupported schema_version '{schema_version}'; expected 1")

    assignments = data.get("assignments")
    if not isinstance(assignments, dict):
        errors.append("Policy missing 'assignments' dictionary")
        return False, errors

    known_roles = {r.value for r in ModelRole}
    known_providers = {"ollama", "openai", "anthropic"}

    for role_name, assign_info in assignments.items():
        if role_name not in known_roles:
            errors.append(f"Unknown cognitive role '{role_name}' in policy assignments")
            continue
        if not isinstance(assign_info, dict):
            errors.append(f"Assignment for role '{role_name}' must be a dictionary")
            continue

        primary_model = assign_info.get("primary_model")
        primary_provider = assign_info.get("primary_provider")

        if not primary_model or not isinstance(primary_model, str):
            errors.append(f"Role '{role_name}' missing primary_model")
        if not primary_provider or not isinstance(primary_provider, str):
            errors.append(f"Role '{role_name}' missing primary_provider")
        elif primary_provider not in known_providers:
            errors.append(f"Role '{role_name}' references unknown provider '{primary_provider}'")

        # Check stale model inventory if available models provided
        if available_models is not None and primary_model:
            model_key = f"{primary_provider}:{primary_model}"
            if primary_model not in available_models and model_key not in available_models:
                errors.append(f"Model '{primary_model}' for role '{role_name}' is not in available model inventory (stale policy)")

    return (len(errors) == 0, errors)


def load_role_policy(
    policy_path: Any,
    available_models: set[str] | None = None,
) -> ModelRolePolicy | None:
    """Safely load and validate a ModelRolePolicy from disk, failing safe on errors."""
    import json
    from pathlib import Path

    path = Path(policy_path)
    if not path.is_file():
        return None
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
        is_valid, _ = validate_policy_artifact(content, available_models=available_models)
        if not is_valid:
            return None
        return ModelRolePolicy.from_dict(content)
    except Exception:
        return None


def resolve_effective_role_assignment(
    role: ModelRole | str,
    settings: Any,
    *,
    policy: ModelRolePolicy | None = None,
    eval_overrides: dict[str, str] | None = None,
    available_models: set[str] | None = None,
) -> RoleModelAssignment:
    """Resolve the effective model assignment for a cognitive role following strict precedence.

    Precedence:
    1. eval-only override (--role-model or eval fixture override)
    2. explicit user configuration (settings.cognitive_roles / settings.roles)
    3. enabled evidence-backed role policy (if not stale/unavailable)
    4. Model Fabric default mapping (DEFAULT_ROLE_CONFIG_MAP -> broad settings role)
    """
    role_str = str(role.value if isinstance(role, ModelRole) else role).lower()
    broad_to_cognitive = {
        "main": ModelRole.EXECUTOR,
        "small": ModelRole.PLANNER,
        "judge": ModelRole.GOAL_VERIFIER,
    }
    if role_str in broad_to_cognitive:
        role_obj = broad_to_cognitive[role_str]
    elif any(role_str == m.value for m in ModelRole):
        role_obj = ModelRole(role_str)
    else:
        role_obj = ModelRole.EXECUTOR
    role_name = role_obj.value
    reqs = ROLE_REQUIREMENTS.get(role_obj, {"tools_must_be_empty": False, "tool_calling_required": False})
    broad_role = DEFAULT_ROLE_CONFIG_MAP.get(role_obj, "main")
    default_role = settings.role(broad_role)
    base_configured_model = default_role.model

    # 1. Eval-only override
    if eval_overrides:
        override_model = eval_overrides.get(role_name)
        if not override_model:
            # Check broad role equivalent
            override_model = eval_overrides.get(broad_role)
        if override_model:
            base_r = settings.role(broad_role)
            return RoleModelAssignment(
                role=role_obj,
                primary_provider=base_r.provider,
                primary_model=override_model,
                evidence_source="eval_override",
                confidence="HIGH",
                tool_calling_required=reqs.get("tool_calling_required", False),
                tools_must_be_empty=reqs.get("tools_must_be_empty", False),
                selection_source=SelectionSource.EXPLICIT_CONFIG.value,
                configured_model=base_configured_model,
                recommended_model="",
                effective_model=override_model,
                actually_executed_model=override_model,
                profile_matches_effective_model=True,
            )

    # Helper to parse configured role entry
    def _parse_role_entry(entry: Any) -> tuple[str, str, str | None, str | None]:
        if isinstance(entry, dict):
            return (
                entry.get("provider", "ollama"),
                entry.get("model", ""),
                entry.get("fallback_provider"),
                entry.get("fallback_model"),
            )
        return (
            getattr(entry, "provider", "ollama"),
            getattr(entry, "model", ""),
            getattr(entry, "fallback_provider", None),
            getattr(entry, "fallback_model", None),
        )

    def _is_model_avail(prov: str, mod: str) -> bool:
        if available_models is None:
            return True
        if isinstance(available_models, dict):
            prov_mods = available_models.get(prov, [])
            return mod in prov_mods
        model_key = f"{prov}:{mod}"
        return mod in available_models or model_key in available_models

    # 2. Explicit user configuration
    cognitive_roles = getattr(settings, "cognitive_roles", {})
    user_roles = getattr(settings, "roles", {})
    configured_entry = cognitive_roles.get(role_name) or user_roles.get(role_name)

    if configured_entry is not None:
        prov, mod, fb_prov, fb_mod = _parse_role_entry(configured_entry)
        if _is_model_avail(prov, mod):
            return RoleModelAssignment(
                role=role_obj,
                primary_provider=prov,
                primary_model=mod,
                fallback_provider=fb_prov,
                fallback_model=fb_mod,
                evidence_source="user_config",
                confidence="HIGH",
                tool_calling_required=reqs.get("tool_calling_required", False),
                tools_must_be_empty=reqs.get("tools_must_be_empty", False),
                selection_source=SelectionSource.EXPLICIT_CONFIG.value,
                configured_model=mod,
                recommended_model="",
                effective_model=mod,
                actually_executed_model=mod,
                profile_matches_effective_model=True,
            )
        elif fb_mod and _is_model_avail(fb_prov or prov, fb_mod):
            return RoleModelAssignment(
                role=role_obj,
                primary_provider=fb_prov or prov,
                primary_model=fb_mod,
                evidence_source="user_config_fallback",
                confidence="HIGH",
                tool_calling_required=reqs.get("tool_calling_required", False),
                tools_must_be_empty=reqs.get("tools_must_be_empty", False),
                selection_source=SelectionSource.FALLBACK.value,
                fallback_used=True,
                configured_model=mod,
                recommended_model="",
                effective_model=fb_mod,
                actually_executed_model=fb_mod,
                profile_matches_effective_model=True,
            )
        else:
            # Model is unavailable and no valid fallback
            return RoleModelAssignment(
                role=role_obj,
                primary_provider=prov,
                primary_model=mod,
                evidence_source="user_config_unavailable",
                confidence="HIGH",
                tool_calling_required=reqs.get("tool_calling_required", False),
                tools_must_be_empty=reqs.get("tools_must_be_empty", False),
                selection_source=SelectionSource.EXPLICIT_CONFIG.value,
                configured_model=mod,
                recommended_model="",
                effective_model=mod,
                actually_executed_model=mod,
                profile_matches_effective_model=True,
            )

    # 3. Enabled evidence-backed role policy
    active_policy = policy
    if active_policy is None and getattr(settings, "role_routing_enabled", False):
        policy_path = getattr(settings, "role_policy_path", None)
        if policy_path:
            active_policy = load_role_policy(policy_path, available_models=available_models)

    if active_policy is not None and role_name in active_policy.assignments:
        assign_data = active_policy.assignments[role_name]
        primary_model = assign_data.get("primary_model", "")
        primary_provider = assign_data.get("primary_provider", "ollama")

        # Stale check: verify model is available
        is_available = True
        if available_models is not None and primary_model:
            model_key = f"{primary_provider}:{primary_model}"
            if primary_model not in available_models and model_key not in available_models:
                is_available = False

        if is_available and primary_model:
            # Check profile match
            profile_model = assign_data.get("profile_model") or primary_model
            profile_ok = profile_evidence_matches_effective_assignment(profile_model, primary_model)
            return RoleModelAssignment(
                role=role_obj,
                primary_provider=primary_provider,
                primary_model=primary_model,
                fallback_provider=assign_data.get("fallback_provider"),
                fallback_model=assign_data.get("fallback_model"),
                evidence_source=active_policy.generated_from_baseline or "model_role_policy.json",
                confidence=assign_data.get("confidence", "LOW"),
                tool_calling_required=reqs.get("tool_calling_required", False),
                tools_must_be_empty=reqs.get("tools_must_be_empty", False),
                selection_source=assign_data.get("selection_source", SelectionSource.EVIDENCE_POLICY.value),
                configured_model=base_configured_model,
                recommended_model=primary_model,
                effective_model=primary_model,
                actually_executed_model=primary_model,
                profile_matches_effective_model=profile_ok,
            )

    # 4. Default Model Fabric routing
    return RoleModelAssignment(
        role=role_obj,
        primary_provider=default_role.provider,
        primary_model=default_role.model,
        fallback_provider=None,
        fallback_model=None,
        evidence_source="default_fabric_mapping",
        confidence="HIGH",
        tool_calling_required=reqs.get("tool_calling_required", False),
        tools_must_be_empty=reqs.get("tools_must_be_empty", False),
        selection_source=SelectionSource.DEFAULT.value,
        configured_model=base_configured_model,
        recommended_model="",
        effective_model=base_configured_model,
        actually_executed_model=base_configured_model,
        profile_matches_effective_model=True,
    )


resolve_role_assignment = resolve_effective_role_assignment


def profile_evidence_matches_effective_assignment(
    profile_model: str, effective_model: str
) -> bool:
    """Check if profiled baseline model matches the effective runtime model for a role."""
    def _norm(m: str) -> str:
        s = str(m or "").strip().lower()
        for prov in ("ollama:", "openai:", "anthropic:"):
            if s.startswith(prov):
                s = s[len(prov):]
                break
        return s

    p = _norm(profile_model)
    e = _norm(effective_model)
    return bool(p and e and p == e)


def compute_role_provenance_metrics(events: list[dict[str, Any]]) -> dict[str, float]:
    """Compute role assignment hit rate, effective match rate, drift rate, and fallback rate."""
    if not events:
        return {
            "effective_model_match_rate": 1.0,
            "unexpected_fallback_rate": 0.0,
            "role_assignment_drift_rate": 0.0,
            "profiled_model_equals_effective_model_rate": 1.0,
        }
    total = len(events)
    matches = sum(
        1
        for e in events
        if e.get("effective_model")
        == e.get("actually_executed_model", e.get("model", e.get("effective_model")))
    )
    fallbacks = sum(
        1
        for e in events
        if e.get("fallback_used") and not e.get("expected_fallback")
    )
    drift = sum(1 for e in events if e.get("role_drift"))
    profile_matches = sum(
        1 for e in events if e.get("profile_matches_effective_model", True)
    )

    return {
        "effective_model_match_rate": round(matches / max(1, total), 4),
        "unexpected_fallback_rate": round(fallbacks / max(1, total), 4),
        "role_assignment_drift_rate": round(drift / max(1, total), 4),
        "profiled_model_equals_effective_model_rate": round(
            profile_matches / max(1, total), 4
        ),
    }



def compute_routing_metrics(events: list[dict[str, Any]]) -> dict[str, float]:
    """Compute role assignment hit rate, fallback rate, and routing error rate."""
    if not events:
        return {
            "role_assignment_hit_rate": 1.0,
            "role_model_fallback_rate": 0.0,
            "role_model_routing_error_rate": 0.0,
        }
    total = len(events)
    fallbacks = sum(1 for e in events if e.get("fallback_used"))
    errors = sum(1 for e in events if e.get("routing_error"))
    hits = sum(
        1
        for e in events
        if e.get("model") == e.get("expected_model", e.get("model"))
        and not e.get("routing_error")
    )
    return {
        "role_assignment_hit_rate": round(hits / total, 4),
        "role_model_fallback_rate": round(fallbacks / total, 4),
        "role_model_routing_error_rate": round(errors / total, 4),
    }

