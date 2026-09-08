"""Goal Contract Builder subsystem for M23 — Goal Contract & Task-Level Success Verification."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any, Protocol
from uuid import uuid4

from tieru.context.builder import ContextBuilder
from tieru.tasks.models import (
    GoalConstraint,
    GoalContract,
    GoalContractError,
    SuccessCriterion,
    TaskLimits,
)


def _text_from_response(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(getattr(block, "text", ""))
            for block in content
            if getattr(block, "type", "text") == "text"
        )
    return str(content or "")

CONTRACT_BUILDER_SYSTEM_PROMPT = """You are Tieru's Goal Contract Builder.
Given the user's task goal and explicit instructions, your job is to extract:
1. Success Criteria: concrete observable outcomes that must be true for the goal to be considered completely achieved.
2. Constraints: explicit restrictions, boundaries, or forbidden outcomes that must not be violated.

Return ONLY a valid JSON object matching this schema:
{
  "success_criteria": [
    {
      "id": "sc1",
      "description": "Concrete observable condition",
      "verification_kind": "deterministic",
      "required_evidence": "test result, file diff, exit code, etc."
    }
  ],
  "constraints": [
    {
      "id": "c1",
      "description": "Explicit restriction that must not be violated",
      "constraint_kind": "user_intent"
    }
  ]
}

Rules:
- Do NOT hallucinate criteria that contradict the user goal.
- Explicit user constraints (e.g., 'without changing public API', 'do not delete tests') MUST be preserved.
- Keep criteria bounded and concise.
- Output ONLY valid JSON.
"""


_CONSTRAINT_MARKERS: tuple[tuple[str, str], ...] = (
    # Vietnamese Negative (multi-word first)
    (r"\bkhông\s+được\s+phép\b", "không được phép"),
    (r"\bkhông\s+được\b", "không được"),
    (r"\bkhông\s+làm\s+thay\s+đổi\b", "không làm thay đổi"),
    (r"\bkhông\s+thay\s+đổi\b", "không thay đổi"),
    (r"\bkhông\s+sửa\b", "không sửa"),
    (r"\bkhông\s+xóa\b", "không xóa"),
    (r"\bkhông\s+bỏ\b", "không bỏ"),
    (r"\bkhông\s+skip\b", "không skip"),
    (r"\bđừng\b", "đừng"),
    (r"\bkhông\s+(?:chạm|đụng|can\s+thiệp|vi\s+phạm|tác\s+động)\b", "không"),
    # Vietnamese Positive / Scope
    (r"\bchỉ\s+được\s+phép\b", "chỉ được phép"),
    (r"\bchỉ\s+được\b", "chỉ được"),
    (r"\bchỉ\s+(?:sửa|thay\s+đổi|tác\s+động|chạy|đọc|ghi|can\s+thiệp|chọn|file|module|trong)\b", "chỉ"),
    (r"\bphải\s+giữ\s+nguyên\b", "phải giữ nguyên"),
    (r"\bphải\s+giữ\b", "phải giữ"),
    (r"\bgiữ\s+nguyên\b", "giữ nguyên"),
    # English Negative
    (r"\bwithout\b", "without"),
    (r"\bdo\s+not\b", "do not"),
    (r"\bdon't\b", "do not"),
    (r"\bnever\b", "never"),
    (r"\bmust\s+not\b", "must not"),
    # English Positive / Scope
    (r"\bonly\s+(?:modify|change|edit|touch|update|files?|under|in|use)\b", "only"),
    (r"\bkeep\b", "keep"),
    (r"\bpreserve\b", "preserve"),
)


def _canonical_constraint_key(text: str) -> str:
    """Deterministic Unicode-normalized case-folded key for exact/canonical deduplication."""
    nfc = unicodedata.normalize("NFC", text).strip().lower()
    return re.sub(r"[\s\.,;!]+$", "", nfc)


def extract_explicit_constraints(goal: str) -> list[GoalConstraint]:
    """Deterministically extract explicit user constraints from prompt phrasing in English & Vietnamese."""
    goal_norm = unicodedata.normalize("NFC", goal).strip()
    if not goal_norm:
        return []

    matches: list[tuple[int, int, str]] = []
    for pat, canonical_name in _CONSTRAINT_MARKERS:
        for m in re.finditer(pat, goal_norm, re.IGNORECASE):
            matches.append((m.start(), m.end(), canonical_name))

    # Sort matches by start position, then longest match first
    matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    filtered_spans: list[tuple[int, int, str]] = []
    for m in matches:
        if not any(f[0] <= m[0] and m[1] <= f[1] for f in filtered_spans):
            filtered_spans.append(m)

    constraints: list[GoalConstraint] = []
    seen_keys: set[str] = set()
    idx = 1

    for i, (start, end, cname) in enumerate(filtered_spans):
        limit = len(goal_norm)
        if i + 1 < len(filtered_spans):
            limit = filtered_spans[i + 1][0]

        clause = goal_norm[start:limit]
        clause = re.split(r"[,;.\n\r!?]", clause)[0]
        clause = re.split(r"\s+(?:và|nhưng|đồng\s+thời|sau\s+đó|and|but|then)\s+", clause)[0]
        clause = clause.strip()

        ckey = _canonical_constraint_key(clause)
        if len(clause) > 5 and ckey not in seen_keys:
            seen_keys.add(ckey)
            constraints.append(
                GoalConstraint(
                    constraint_id=f"c_exp_{idx}",
                    description=clause,
                    constraint_kind="user_intent",
                    required=True,
                )
            )
            idx += 1

    return constraints


def parse_goal_contract_output(
    goal: str,
    text: str,
    limits: TaskLimits,
    *,
    user_constraints: tuple[GoalConstraint, ...] = (),
) -> GoalContract:
    """Parse and validate structured JSON output from a goal contract builder."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)
    except Exception as exc:
        raise GoalContractError(f"Malformed goal contract JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise GoalContractError("Goal contract output must be a JSON object")

    raw_criteria = data.get("success_criteria")
    if not isinstance(raw_criteria, list):
        raise GoalContractError("'success_criteria' must be a list")

    criteria: list[SuccessCriterion] = []
    for idx, item in enumerate(raw_criteria, 1):
        if not isinstance(item, dict):
            raise GoalContractError("Each success criterion must be a dict")
        cid = str(item.get("id") or f"sc{idx}")[:64]
        desc = str(item.get("description") or "").strip()
        vkind = str(item.get("verification_kind") or "deterministic")[:32]
        ev = str(item.get("required_evidence") or "").strip()[:160]
        if not desc:
            raise GoalContractError(f"Criterion {cid} missing description")
        if len(desc.encode("utf-8")) > limits.max_criterion_description_bytes:
            raise GoalContractError(f"Criterion {cid} description exceeds size limit")
        criteria.append(
            SuccessCriterion(
                criterion_id=cid,
                description=desc,
                verification_kind=vkind,
                required_evidence=ev,
                required=bool(item.get("required", True)),
            )
        )

    if len(criteria) > limits.max_criteria_per_contract:
        raise GoalContractError(
            f"Criteria count {len(criteria)} exceeds limit {limits.max_criteria_per_contract}"
        )
    if not criteria:
        criteria.append(
            SuccessCriterion(
                criterion_id="sc1",
                description=f"Observable completion of: {goal[:100]}",
                verification_kind="deterministic",
                required=True,
            )
        )

    raw_constraints = data.get("constraints", [])
    if not isinstance(raw_constraints, list):
        raise GoalContractError("'constraints' must be a list")

    explicit = extract_explicit_constraints(goal)
    if len(explicit) > limits.max_constraints_per_contract:
        raise GoalContractError(
            f"Explicit user constraints count ({len(explicit)}) exceeds configured limit "
            f"({limits.max_constraints_per_contract}). Contract creation blocked for safety."
        )
    for ec in explicit:
        if len(ec.description.encode("utf-8")) > limits.max_constraint_description_bytes:
            raise GoalContractError(
                f"Explicit user constraint description exceeds limit ({limits.max_constraint_description_bytes}): "
                f"{ec.description[:50]}..."
            )

    constraints_map: dict[str, GoalConstraint] = {}
    # Explicit user constraints are authoritative and unconditionally preserved
    for ec in explicit:
        constraints_map[_canonical_constraint_key(ec.description)] = ec

    for uc in user_constraints:
        key = _canonical_constraint_key(uc.description)
        if key not in constraints_map and len(constraints_map) < limits.max_constraints_per_contract:
            constraints_map[key] = uc

    for idx, c_item in enumerate(raw_constraints, len(constraints_map) + 1):
        if isinstance(c_item, str):
            c_desc = c_item.strip()
            c_id = f"c{idx}"
            c_kind = "user_intent"
        elif isinstance(c_item, dict):
            c_desc = str(c_item.get("description") or "").strip()
            c_id = str(c_item.get("id") or f"c{idx}")[:64]
            c_kind = str(c_item.get("constraint_kind") or "user_intent")[:32]
        else:
            continue
        key = _canonical_constraint_key(c_desc)
        if c_desc and key not in constraints_map:
            if len(c_desc.encode("utf-8")) > limits.max_constraint_description_bytes:
                raise GoalContractError("Constraint description exceeds size limit")
            if len(constraints_map) < limits.max_constraints_per_contract:
                constraints_map[key] = GoalConstraint(
                    constraint_id=c_id,
                    description=c_desc,
                    constraint_kind=c_kind,
                    required=True,
                )

    final_constraints = tuple(constraints_map.values())

    return GoalContract(
        contract_id=f"contract_{uuid4().hex[:12]}",
        task_id="",
        goal=goal,
        success_criteria=tuple(criteria),
        constraints=final_constraints,
    )


class GoalContractBuilder(Protocol):
    def build(
        self,
        goal: str,
        *,
        constraints: tuple[GoalConstraint, ...] = (),
    ) -> GoalContract:
        """Derive an immutable GoalContract from user goal and constraints."""
        ...


class DeterministicGoalContractBuilder:
    def __init__(self, limits: TaskLimits | None = None) -> None:
        self.limits = limits or TaskLimits()

    def build(
        self,
        goal: str,
        *,
        constraints: tuple[GoalConstraint, ...] = (),
    ) -> GoalContract:
        explicit = extract_explicit_constraints(goal)
        if len(explicit) > self.limits.max_constraints_per_contract:
            raise GoalContractError(
                f"Explicit user constraints count ({len(explicit)}) exceeds configured limit "
                f"({self.limits.max_constraints_per_contract}). Contract creation blocked for safety."
            )
        for ec in explicit:
            if len(ec.description.encode("utf-8")) > self.limits.max_constraint_description_bytes:
                raise GoalContractError(
                    f"Explicit user constraint description exceeds limit ({self.limits.max_constraint_description_bytes}): "
                    f"{ec.description[:50]}..."
                )

        all_constraints_map: dict[str, GoalConstraint] = {}
        for ec in explicit:
            all_constraints_map[_canonical_constraint_key(ec.description)] = ec

        for uc in constraints:
            key = _canonical_constraint_key(uc.description)
            if key not in all_constraints_map and len(all_constraints_map) < self.limits.max_constraints_per_contract:
                all_constraints_map[key] = uc

        criteria = [
            SuccessCriterion(
                criterion_id="sc1",
                description=f"Observable execution and verification of: {goal.strip()[:200]}",
                verification_kind="deterministic",
                required_evidence="step_verification",
                required=True,
            )
        ]
        return GoalContract(
            contract_id=f"contract_{uuid4().hex[:12]}",
            task_id="",
            goal=goal,
            success_criteria=tuple(criteria),
            constraints=tuple(all_constraints_map.values()),
        )


class ModelGoalContractBuilder:
    """Tool-free LLM contract builder enforcing M19 Context Firewall invariants."""

    def __init__(
        self,
        model_router: Any,
        role: str = "small",
        limits: TaskLimits | None = None,
    ) -> None:
        self.model_router = model_router
        self.role = role
        self.limits = limits or TaskLimits()

    def build(
        self,
        goal: str,
        *,
        constraints: tuple[GoalConstraint, ...] = (),
    ) -> GoalContract:
        explicit_from_text = extract_explicit_constraints(goal)
        combined_user_constraints = tuple(
            {c.description.lower(): c for c in (*constraints, *explicit_from_text)}.values()
        )

        builder = ContextBuilder(max_block_bytes=self.limits.max_goal_bytes)
        builder.add_control(CONTRACT_BUILDER_SYSTEM_PROMPT, source="contract_builder")

        user_content = f"Task Goal:\n{goal}"
        if combined_user_constraints:
            user_content += "\n\nExplicit User Constraints:\n" + "\n".join(
                f"- {c.description}" for c in combined_user_constraints
            )
        builder.add_user(user_content, source="goal_contract_user")
        assembly = builder.build()

        try:
            client = self.model_router.client(self.role)
            response = client.messages.create(
                model=self.model_router.model(self.role),
                system=assembly.system,
                messages=list(assembly.messages),
                tools=[],
                max_tokens=1200,
            )
            text = _text_from_response(getattr(response, "content", response))
            contract = parse_goal_contract_output(
                goal,
                text,
                self.limits,
                user_constraints=combined_user_constraints,
            )
            return contract
        except GoalContractError:
            raise
        except Exception:
            return DeterministicGoalContractBuilder(self.limits).build(
                goal, constraints=combined_user_constraints
            )


class CallableGoalContractBuilder:
    """Test injector for deterministic or scripted contract outputs."""

    def __init__(self, fn: Callable[[str], GoalContract]) -> None:
        self._fn = fn

    def build(
        self,
        goal: str,
        *,
        constraints: tuple[GoalConstraint, ...] = (),
    ) -> GoalContract:
        contract = self._fn(goal)
        if constraints:
            existing = {c.description.lower(): c for c in contract.constraints}
            for uc in constraints:
                if uc.description.lower() not in existing:
                    existing[uc.description.lower()] = uc
            return GoalContract(
                contract_id=contract.contract_id,
                task_id=contract.task_id,
                goal=contract.goal,
                success_criteria=contract.success_criteria,
                constraints=tuple(existing.values()),
                created_at=contract.created_at,
            )
        return contract
