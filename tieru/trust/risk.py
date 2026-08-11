"""Deterministic risk classification for normalized action requests."""

from __future__ import annotations

from tieru.trust.models import ActionRequest, Capability, RiskLevel

_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def _max_risk(*levels: RiskLevel) -> RiskLevel:
    return max(levels, key=_ORDER.__getitem__)


class RiskClassifier:
    """Code-driven classification; no model call or prose interpretation."""

    def classify(self, action: ActionRequest) -> tuple[RiskLevel, tuple[str, ...]]:
        if not action.capabilities:
            return RiskLevel.CRITICAL, ("unclassified_capability",)

        capabilities = set(action.capabilities)
        risk = RiskLevel.LOW
        reasons: list[str] = []
        if Capability.LOCAL_READ in capabilities:
            reasons.append("local_read")
        if Capability.NETWORK_READ in capabilities:
            reasons.append("network_read")
        if Capability.LOCAL_WRITE in capabilities:
            risk = _max_risk(risk, RiskLevel.MEDIUM)
            reasons.append("local_write")
        if Capability.EXTERNAL_WRITE in capabilities or action.external_write:
            risk = _max_risk(risk, RiskLevel.HIGH)
            reasons.append("external_write")
        if Capability.PROCESS_EXECUTION in capabilities or action.process_execution:
            risk = _max_risk(risk, RiskLevel.HIGH)
            reasons.append("process_execution")
        if Capability.BROWSER_AUTOMATION in capabilities or action.browser_action:
            level = RiskLevel.LOW if action.read_only else RiskLevel.HIGH
            risk = _max_risk(risk, level)
            reasons.append("browser_automation")
        if Capability.DESTRUCTIVE in capabilities or action.destructive:
            risk = _max_risk(risk, RiskLevel.HIGH)
            reasons.append("destructive")
            if action.scope in {"broad", "unrestricted"} or action.target in {"*", "/", "\\"}:
                risk = RiskLevel.CRITICAL
                reasons.append("broad_destructive_scope")
        if action.metadata.get("credential_related"):
            risk = RiskLevel.CRITICAL
            reasons.append("credential_handling")
        if not action.reversible and risk is RiskLevel.MEDIUM:
            risk = RiskLevel.HIGH
            reasons.append("not_reversible")

        declared = str(action.metadata.get("declared_risk", "")).upper()
        if declared in RiskLevel.__members__:
            risk = _max_risk(risk, RiskLevel[declared])
        return risk, tuple(dict.fromkeys(reasons or ["bounded_read"]))
