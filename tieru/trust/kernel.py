"""Central, fail-closed authorization boundary for every registered tool."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tieru.trust.fingerprint import action_fingerprint
from tieru.trust.models import ActionRequest, ApprovalRequest, RiskLevel, TrustDecision
from tieru.trust.policy import PolicyEvaluator
from tieru.trust.risk import RiskClassifier

ApprovalHandler = Callable[[ApprovalRequest], bool]
Observer = Callable[[str, dict[str, Any]], None]


class TrustKernel:
    def __init__(
        self,
        policy: dict[str, Any] | None = None,
        legacy_policy: dict[str, Any] | None = None,
        approval_handler: ApprovalHandler | None = None,
        *,
        context: dict[str, Any] | None = None,
        classifier: RiskClassifier | None = None,
    ) -> None:
        self.classifier = classifier or RiskClassifier()
        self.policy = PolicyEvaluator(policy, legacy_policy, context)
        self.approval_handler = approval_handler
        self._denied_fingerprints: set[str] = set()

    @staticmethod
    def _event(action: ActionRequest, decision: TrustDecision) -> dict[str, Any]:
        return {
            "tool": action.tool_name,
            "capability": [item.value for item in action.capabilities],
            "operation": action.operation,
            "risk": decision.risk.value,
            "allowed": decision.allowed,
            "approval_required": decision.approval_required,
            "reason_codes": list(decision.reason_codes),
            "fingerprint": decision.action_fingerprint,
        }

    def _decision(
        self,
        action: ActionRequest,
        *,
        allowed: bool,
        risk: RiskLevel,
        approval_required: bool,
        reason_codes: tuple[str, ...],
        matched_policy: str,
        fingerprint: str,
        observer: Observer | None,
    ) -> TrustDecision:
        decision = TrustDecision(
            allowed=allowed,
            risk=risk,
            approval_required=approval_required,
            reason_codes=reason_codes,
            explanation=self.explain_parts(allowed, risk, reason_codes),
            matched_policy=matched_policy,
            action_fingerprint=fingerprint,
        )
        if observer:
            observer("trust_decision", self._event(action, decision))
        return decision

    def authorize(
        self,
        action: ActionRequest,
        *,
        default_policy: str,
        legacy_capabilities: tuple[str, ...] = (),
        approval_args: dict[str, Any] | None = None,
        observer: Observer | None = None,
    ) -> TrustDecision:
        fingerprint = action_fingerprint(action)
        if observer:
            observer(
                "trust_request",
                {
                    "tool": action.tool_name,
                    "capability": [item.value for item in action.capabilities],
                    "operation": action.operation,
                    "fingerprint": fingerprint,
                },
            )
        try:
            risk, risk_reasons = self.classifier.classify(action)
        except Exception:
            return self._decision(
                action,
                allowed=False,
                risk=RiskLevel.CRITICAL,
                approval_required=False,
                reason_codes=("classifier_error", "fail_closed"),
                matched_policy="classifier_error",
                fingerprint=fingerprint,
                observer=observer,
            )
        if not action.capabilities:
            return self._decision(
                action,
                allowed=False,
                risk=risk,
                approval_required=False,
                reason_codes=("unclassified_action", "fail_closed"),
                matched_policy="unclassified",
                fingerprint=fingerprint,
                observer=observer,
            )
        try:
            policy = self.policy.evaluate(
                action,
                risk,
                default_policy=default_policy,
                legacy_capabilities=legacy_capabilities,
            )
        except Exception:
            return self._decision(
                action,
                allowed=False,
                risk=RiskLevel.CRITICAL,
                approval_required=False,
                reason_codes=("policy_error", "fail_closed"),
                matched_policy="policy_error",
                fingerprint=fingerprint,
                observer=observer,
            )
        reasons = tuple(dict.fromkeys((*risk_reasons, *policy.reason_codes)))
        if (
            risk is RiskLevel.CRITICAL
            and policy.mode == "allow"
            and policy.matched_policy
            in {
                "tool.default_policy",
                "trust.default",
                "tool_permissions.defaults.read_only",
                "tool_permissions.defaults.side_effect",
            }
        ):
            policy = type(policy)(
                "deny", policy.matched_policy, ("critical_requires_explicit_allow",)
            )
            reasons = tuple(dict.fromkeys((*risk_reasons, *policy.reason_codes)))
        if policy.mode == "deny":
            self._denied_fingerprints.add(fingerprint)
            return self._decision(
                action,
                allowed=False,
                risk=risk,
                approval_required=False,
                reason_codes=reasons,
                matched_policy=policy.matched_policy,
                fingerprint=fingerprint,
                observer=observer,
            )
        if policy.mode == "allow":
            return self._decision(
                action,
                allowed=True,
                risk=risk,
                approval_required=False,
                reason_codes=reasons,
                matched_policy=policy.matched_policy,
                fingerprint=fingerprint,
                observer=observer,
            )
        if fingerprint in self._denied_fingerprints:
            return self._decision(
                action,
                allowed=False,
                risk=risk,
                approval_required=True,
                reason_codes=(*reasons, "repeated_denial"),
                matched_policy=policy.matched_policy,
                fingerprint=fingerprint,
                observer=observer,
            )
        if self.approval_handler is None:
            self._denied_fingerprints.add(fingerprint)
            return self._decision(
                action,
                allowed=False,
                risk=risk,
                approval_required=True,
                reason_codes=(*reasons, "approval_unavailable"),
                matched_policy=policy.matched_policy,
                fingerprint=fingerprint,
                observer=observer,
            )
        request = ApprovalRequest(
            tool=action.tool_name,
            risk=risk.value,
            read_only=action.read_only,
            capabilities=tuple(item.value for item in action.capabilities),
            args=dict(approval_args or {}),
            argument_hash=fingerprint,
            reason=self.explain_parts(False, risk, reasons),
            operation=action.operation,
            target=action.target,
            scope=action.scope,
            resource_type=action.resource_type,
        )
        try:
            approved = bool(self.approval_handler(request))
            approval_code = "approval_granted" if approved else "approval_denied"
        except Exception:
            approved = False
            approval_code = "approval_error"
        if observer:
            observer(
                "trust_approval",
                {
                    "tool": action.tool_name,
                    "risk": risk.value,
                    "approved": approved,
                    "reason_code": approval_code,
                    "fingerprint": fingerprint,
                },
            )
        if not approved:
            self._denied_fingerprints.add(fingerprint)
        return self._decision(
            action,
            allowed=approved,
            risk=risk,
            approval_required=True,
            reason_codes=(*reasons, approval_code),
            matched_policy=policy.matched_policy,
            fingerprint=fingerprint,
            observer=observer,
        )

    @staticmethod
    def explain_parts(
        allowed: bool, risk: RiskLevel, reason_codes: tuple[str, ...]
    ) -> str:
        verdict = "ALLOWED" if allowed else "DENIED"
        reasons = ", ".join(code.replace("_", " ") for code in reason_codes)
        return f"{verdict} — {risk.value} risk: {reasons or 'no matching allow rule'}"

    def explain(self, decision: TrustDecision) -> str:
        return decision.explanation
