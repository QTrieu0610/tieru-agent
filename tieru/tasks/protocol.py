"""Structured Executor Protocol & Tool-Use Reliability (M36).

Governs model action proposals, validates tool arguments before execution,
enforces structured action intent, defends against unknown tools, detects
and bounds no-progress turns, and measures checkpoint realization.

Mandatory Architectural Invariants:
1. A model may propose an action, but only the runtime determines whether that
   proposal is a valid executable tool request.
2. Tieru never executes an unavailable or malformed tool request merely because
   it resembles a valid action in model-generated text.
3. Executor protocol correction is bounded and does not bypass Trust, Action
   Ledger, Capability Routing, or Resource Budget controls.
4. Tool-selection quality and checkpoint-realization quality are measured separately.
5. A checkpoint can only be credited to the Executor when an actual governed
   execution produced it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from tieru.memory.personal import redact_secrets
from tieru.tasks.models import (
    StepExecutionKind,
    TaskStep,
)
from tieru.tools.registry import Tool, ToolRegistry

_PATH_KEYS = {"path", "file", "filepath", "filename", "destination", "source", "target"}
_TOOL_RESIDUE_PATTERN = re.compile(
    r"(?:<tool|tool[_ -]?use|tool[_ -]?call|web_search|web_fetch|browser_[a-z_]+|"
    r"filesystem_[a-z_]+|document_read|shell_run|run_command|git_[a-z_]+|code_[a-z_]+|"
    r"github_[a-z_]+|i will use)",
    re.IGNORECASE,
)


class ExecutorTurnOutcome(str, Enum):
    """Normalized outcome of a single Executor turn."""

    PROGRESS = "progress"
    COMPLETE_CANDIDATE = "complete_candidate"
    PROTOCOL_ERROR = "protocol_error"
    NO_PROGRESS = "no_progress"
    BLOCKED = "blocked"


class ToolActivationMode(str, Enum):
    """Execution scaffolding mode for model tool activation."""

    NONE = "none"
    REQUIRED = "required"
    PREFERRED = "preferred"


class ToolResourceDomain(str, Enum):
    """Semantic resource domain targeted by a tool."""

    FILESYSTEM = "filesystem"
    NOTES = "notes"
    CODE = "code"
    DOCUMENT = "document"
    PROCESS = "process"
    EXTERNAL = "external"


@dataclass(frozen=True)
class ToolChoicePolicy:
    """Provider-neutral tool choice policy passed to model adapters."""

    mode: ToolActivationMode = ToolActivationMode.NONE
    allowed_tools: tuple[str, ...] = ()


class ExecutorErrorClass(str, Enum):
    """Taxonomy of Executor protocol and tool-use failures."""

    UNKNOWN_TOOL = "executor_unknown_tool"
    INVALID_TOOL_ARGUMENTS = "executor_invalid_tool_arguments"
    TOOL_OMISSION = "executor_tool_omission"
    PREMATURE_FINAL = "executor_premature_final"
    TOOL_ACTIVATION_SIGNAL_IGNORED = "tool_activation_signal_ignored"
    NO_PROGRESS = "executor_no_progress"
    SEQUENCE_EXHAUSTED = "executor_sequence_exhausted"


@dataclass(frozen=True)
class ExecutorActionIntent:
    """Validated intermediate representation of an Executor's proposed action.

    This structure normalizes model output; it does NOT authorize actions.
    ToolRegistry and TrustKernel remain authoritative.
    """

    kind: str  # execution kind of step, e.g. "read", "write", "command", "reasoning"
    tool_name: str | None = None
    arguments: Mapping[str, Any] | None = None
    final_text: str | None = None
    intent_type: Literal[
        "tool_call",
        "reasoning_output",
        "cannot_proceed",
    ] = "reasoning_output"
    raw_response: str | None = None
    validation_error: str | None = None
    validation_error_class: str | None = None
    is_tool_like_prose: bool = False

    @property
    def is_valid(self) -> bool:
        return self.validation_error is None


@dataclass(frozen=True)
class ArgumentValidationResult:
    """Detailed result of schema-based tool argument validation."""

    is_valid: bool
    error_class: str | None = None  # missing_required_argument, unknown_argument, wrong_argument_type, invalid_path_shape
    error_message: str | None = None
    field_name: str | None = None


def validate_tool_arguments(tool: Tool, args: Any) -> ArgumentValidationResult:
    """Validate tool arguments against tool input schema before execution.

    Distinguishes:
    - missing_required_argument
    - unknown_argument
    - wrong_argument_type
    - invalid_path_shape
    """
    if not isinstance(args, dict):
        return ArgumentValidationResult(
            is_valid=False,
            error_class="wrong_argument_type",
            error_message="tool arguments must be a JSON object",
            field_name="arguments",
        )

    schema = tool.input_schema or {}
    required = schema.get("required", [])
    if isinstance(required, list):
        for req in required:
            if isinstance(req, str) and req not in args:
                return ArgumentValidationResult(
                    is_valid=False,
                    error_class="missing_required_argument",
                    error_message=f"missing required field: {req}",
                    field_name=req,
                )

    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False and isinstance(properties, dict):
        for key in args:
            if key not in properties:
                return ArgumentValidationResult(
                    is_valid=False,
                    error_class="unknown_argument",
                    error_message=f"unknown argument: {key}",
                    field_name=key,
                )

    # Validate property types
    if isinstance(properties, dict):
        for key, val in args.items():
            prop_spec = properties.get(key)
            if isinstance(prop_spec, dict):
                declared_type = prop_spec.get("type")
                if declared_type:
                    type_err = _check_value_type(val, declared_type, key)
                    if type_err:
                        return ArgumentValidationResult(
                            is_valid=False,
                            error_class="wrong_argument_type",
                            error_message=type_err,
                            field_name=key,
                        )

            # Path shape validation
            if key in _PATH_KEYS:
                path_err = _check_path_shape(val, key)
                if path_err:
                    return ArgumentValidationResult(
                        is_valid=False,
                        error_class="invalid_path_shape",
                        error_message=path_err,
                        field_name=key,
                    )

    # General schema validation
    raw_errors = tool.validate_arguments(args)
    if raw_errors:
        first_err = raw_errors[0]
        err_cls = "wrong_argument_type"
        if "required" in first_err:
            err_cls = "missing_required_argument"
        elif "must be" in first_err:
            err_cls = "wrong_argument_type"
        return ArgumentValidationResult(
            is_valid=False,
            error_class=err_cls,
            error_message=first_err,
        )

    return ArgumentValidationResult(is_valid=True)


def _check_value_type(val: Any, expected: str, field_name: str) -> str | None:
    if expected == "string" and not isinstance(val, str):
        return f"field '{field_name}' must be string, got {type(val).__name__}"
    if expected == "integer" and (not isinstance(val, int) or isinstance(val, bool)):
        return f"field '{field_name}' must be integer, got {type(val).__name__}"
    if expected == "number" and (not isinstance(val, (int, float)) or isinstance(val, bool)):
        return f"field '{field_name}' must be number, got {type(val).__name__}"
    if expected == "boolean" and not isinstance(val, bool):
        return f"field '{field_name}' must be boolean, got {type(val).__name__}"
    if expected == "array" and not isinstance(val, list):
        return f"field '{field_name}' must be array, got {type(val).__name__}"
    if expected == "object" and not isinstance(val, dict):
        return f"field '{field_name}' must be object, got {type(val).__name__}"
    return None


def _check_path_shape(val: Any, field_name: str) -> str | None:
    if not isinstance(val, str):
        return f"path field '{field_name}' must be a string"
    stripped = val.strip()
    if not stripped:
        return f"path field '{field_name}' cannot be empty"
    if "\0" in val:
        return f"path field '{field_name}' cannot contain null bytes"
    return None


def parse_action_intent(
    raw_text: str | None = None,
    tool_name: str | None = None,
    arguments: Mapping[str, Any] | None = None,
    step_kind: StepExecutionKind | str | None = None,
) -> ExecutorActionIntent:
    """Parse and normalize model output into an ExecutorActionIntent.

    Distinguishes native tool calls from plain prose.
    Does NOT auto-execute tool-like JSON found in prose.
    """
    kind_str = (
        step_kind.value
        if isinstance(step_kind, StepExecutionKind)
        else str(step_kind or "reasoning").lower()
    )

    if tool_name:
        return ExecutorActionIntent(
            kind=kind_str,
            tool_name=tool_name,
            arguments=arguments or {},
            final_text=None,
            intent_type="tool_call",
            raw_response=raw_text,
        )

    text = (raw_text or "").strip()
    is_tool_like = False
    if text and (("{" in text and "}" in text and ('"tool"' in text or '"name"' in text)) or _TOOL_RESIDUE_PATTERN.search(text)):
        is_tool_like = True

    # Detect cannot_proceed indicators
    low_text = text.lower()
    cannot_proceed = any(
        phrase in low_text
        for phrase in (
            "cannot proceed",
            "unable to proceed",
            "i cannot complete",
            "impossible to complete",
        )
    )

    return ExecutorActionIntent(
        kind=kind_str,
        tool_name=None,
        arguments=None,
        final_text=text,
        intent_type="cannot_proceed" if cannot_proceed else "reasoning_output",
        raw_response=raw_text,
        is_tool_like_prose=is_tool_like,
    )


def validate_action_intent_for_step(
    intent: ExecutorActionIntent,
    step: TaskStep,
    visible_tools: set[str],
    tool_registry: ToolRegistry | None = None,
    has_checkpoints: bool = False,
) -> tuple[ExecutorTurnOutcome, str | None, str | None]:
    """Validate whether an action intent complies with the step contract.

    Returns:
        (outcome, error_class, error_message)
    """
    kind = step.execution_kind or StepExecutionKind.REASONING

    # 1. Unknown tool check
    if intent.intent_type == "tool_call":
        t_name = intent.tool_name or ""
        if t_name not in visible_tools:
            return (
                ExecutorTurnOutcome.PROTOCOL_ERROR,
                ExecutorErrorClass.UNKNOWN_TOOL.value,
                f"Tool '{t_name}' is not available.",
            )

        # 2. Tool argument validation
        if tool_registry is not None:
            tool = tool_registry.get(t_name)
            if tool is not None:
                val_res = validate_tool_arguments(tool, intent.arguments or {})
                if not val_res.is_valid:
                    return (
                        ExecutorTurnOutcome.PROTOCOL_ERROR,
                        ExecutorErrorClass.INVALID_TOOL_ARGUMENTS.value,
                        val_res.error_message,
                    )
        return ExecutorTurnOutcome.PROGRESS, None, None

    # 3. Reasoning steps accept reasoning candidates
    if kind is StepExecutionKind.REASONING:
        if intent.intent_type == "cannot_proceed":
            return (
                ExecutorTurnOutcome.BLOCKED,
                "executor_cannot_proceed",
                intent.final_text or "Executor cannot proceed.",
            )
        if intent.final_text and len(intent.final_text.strip()) >= 5:
            return ExecutorTurnOutcome.COMPLETE_CANDIDATE, None, None
        return (
            ExecutorTurnOutcome.NO_PROGRESS,
            ExecutorErrorClass.NO_PROGRESS.value,
            "Reasoning step requires a substantive answer candidate.",
        )

    # 4. Tool-required steps reject prose completion if required evidence is missing
    if kind in {
        StepExecutionKind.READ,
        StepExecutionKind.WRITE,
        StepExecutionKind.COMMAND,
        StepExecutionKind.EXTERNAL_ACTION,
    } and not has_checkpoints:
        return (
            ExecutorTurnOutcome.NO_PROGRESS,
            ExecutorErrorClass.PREMATURE_FINAL.value,
            f"Step requires observable '{kind.value}' tool execution, but only prose was provided.",
        )

    return ExecutorTurnOutcome.NO_PROGRESS, None, None


def format_schema_feedback(tool_name: str, error_message: str) -> str:
    """Format safe model-facing feedback for invalid tool arguments."""
    return (
        f"Tool request was not executable.\n\n"
        f"Tool:\n{tool_name}\n\n"
        f"Validation:\n{error_message}\n\n"
        f"Use only the available tool schema."
    )


def format_unknown_tool_feedback(tool_name: str, visible_tools: Sequence[str]) -> str:
    """Format safe model-facing feedback for unknown/unavailable tool proposals."""
    tool_list = "\n".join(f"- {t}" for t in sorted(visible_tools)) if visible_tools else "- none"
    return (
        f"unknown_tool: Tool '{tool_name}' is not available.\n\n"
        f"Available tools:\n{tool_list}\n\n"
        f"Use only one of the currently available tools."
    )


PROSE_PROMISE_PATTERNS = (
    "i will", "i'll", "let me", "we will", "going to", "now i will",
    "i shall", "i plan to", "next, i", "i am going to", "i'm going to",
    "promise", "will now",
)


def is_prose_promise(text: str) -> bool:
    """Detect whether text is a prose promise rather than actual progress."""
    t = (text or "").strip().lower()
    return any(p in t for p in PROSE_PROMISE_PATTERNS)


def format_no_progress_feedback(missing_requirements: Sequence[str], execution_kind: str = "tool") -> str:
    """Format bounded feedback when an Executor turn produces no progress."""
    req_lines = "\n".join(f"- {r}" for r in missing_requirements) if missing_requirements else f"- observable {execution_kind} execution"
    return (
        f"No observable progress was produced.\n\n"
        f"The current step still requires:\n{req_lines}\n\n"
        f"Use one of the currently available tools if execution is required."
    )


def compact_continuation_context(
    step: TaskStep,
    missing_requirements: Sequence[str],
    satisfied_requirements: Sequence[str],
    recent_tool_summary: str | None = None,
) -> str:
    """Build a compact, Context Firewall-safe continuation directive.

    Omits satisfied requirements so the model does not re-read or repeat work.
    """
    missing_lines = "\n".join(f"- {req}" for req in missing_requirements)
    satisfied_info = ""
    if satisfied_requirements:
        sat_list = ", ".join(satisfied_requirements)
        satisfied_info = f"\nAlready satisfied evidence (do not repeat): {sat_list}\n"

    summary_part = ""
    if recent_tool_summary:
        trimmed = recent_tool_summary[:500]
        summary_part = f"\nRecent tool observation summary:\n{trimmed}\n"

    directive = (
        "CONTINUATION DIRECTIVE:\n"
        "The current step is NOT complete because required observable evidence has not yet been produced.\n"
        f"{satisfied_info}"
        f"{summary_part}"
        f"Missing required evidence:\n{missing_lines}\n\n"
        f"Continue working on the SAME step:\n"
        f"STEP OBJECTIVE: {step.position}. {step.title} — {step.instruction}\n\n"
        "EXECUTION RULES:\n"
        "1. Stay on this current step. Do not repeat already satisfied work.\n"
        "2. Do not merely state or summarize that the step succeeded.\n"
        "3. You must invoke the relevant permitted tool now to produce the required evidence."
    )
    return redact_secrets(directive)


def get_tool_resource_domain(tool_name: str, tool: Tool | None = None) -> ToolResourceDomain:
    """Map a tool to its canonical semantic resource domain."""
    if tool is not None and getattr(tool, "resource_domain", None) is not None:
        custom_domain = tool.resource_domain
        if isinstance(custom_domain, ToolResourceDomain):
            return custom_domain
        try:
            return ToolResourceDomain(str(custom_domain).lower())
        except ValueError:
            pass

    name = tool_name.lower()
    if name.startswith("filesystem_"):
        return ToolResourceDomain.FILESYSTEM
    if name.startswith(("notes_", "note_")):
        return ToolResourceDomain.NOTES
    if name.startswith(("code_", "git_")):
        return ToolResourceDomain.CODE
    if name.startswith("document_"):
        return ToolResourceDomain.DOCUMENT
    if name in {"run_command", "shell_run", "command_run"}:
        return ToolResourceDomain.PROCESS
    if name.startswith(("web_", "browser_", "github_")):
        return ToolResourceDomain.EXTERNAL
    return ToolResourceDomain.FILESYSTEM


def filter_compatible_tools(
    step: TaskStep,
    visible_tools: Sequence[str] | set[str],
    *,
    missing_requirements: Sequence[Any] = (),
    tool_registry: ToolRegistry | None = None,
) -> tuple[str, ...]:
    """Derive semantically compatible visible tools for the current step.

    INVARIANT: compatible_tools ⊆ visible_tools (never expands visibility!).
    """
    visible_set = set(visible_tools)
    kind = step.execution_kind or StepExecutionKind.REASONING
    if kind is StepExecutionKind.REASONING and not missing_requirements:
        return ()

    instruction = (step.instruction or "").lower()
    title = (step.title or "").lower()
    combined_target_text = f"{title} {instruction}"

    is_workspace_file_target = any(
        ext in combined_target_text
        for ext in (".py", ".json", ".sql", ".txt", ".ini", ".md", ".yaml", ".yml", ".sh", "file", "path", "directory")
    )
    is_notes_target = any(
        kw in combined_target_text for kw in ("note", "notes", "personal note", "tieru notes")
    )

    candidates: list[str] = []
    for name in visible_tools:
        tool = tool_registry.get(name) if tool_registry else None
        domain = get_tool_resource_domain(name, tool)

        # Resource-domain check: exclude notes tools if target is explicitly workspace files and not notes
        if domain is ToolResourceDomain.NOTES and is_workspace_file_target and not is_notes_target:
            continue

        if kind is StepExecutionKind.READ:
            if domain in {ToolResourceDomain.FILESYSTEM, ToolResourceDomain.CODE, ToolResourceDomain.DOCUMENT}:
                candidates.append(name)
            elif domain is ToolResourceDomain.NOTES:
                if is_notes_target:
                    candidates.append(name)
            elif domain is ToolResourceDomain.EXTERNAL and any(w in combined_target_text for w in ("web", "url", "http", "search")):
                candidates.append(name)
        elif kind is StepExecutionKind.WRITE:
            if domain in {ToolResourceDomain.FILESYSTEM, ToolResourceDomain.CODE}:
                if name not in {"filesystem_read", "code_read", "code_search", "filesystem_search", "filesystem_list"}:
                    candidates.append(name)
            elif (domain is ToolResourceDomain.NOTES and is_notes_target) or domain is ToolResourceDomain.PROCESS:
                candidates.append(name)
        elif kind is StepExecutionKind.COMMAND:
            if domain is ToolResourceDomain.PROCESS:
                candidates.append(name)
        elif kind is StepExecutionKind.EXTERNAL_ACTION:
            if domain in {ToolResourceDomain.EXTERNAL, ToolResourceDomain.PROCESS}:
                candidates.append(name)
        else:  # MIXED or unclassified
            candidates.append(name)

    # Filter strictly to visible_tools to guarantee invariant
    compatible = tuple(name for name in candidates if name in visible_set)
    # If filtered set is empty, fall back to all visible tools to avoid blocking if kind is not reasoning
    if not compatible and kind is not StepExecutionKind.REASONING:
        compatible = tuple(name for name in visible_tools if name in visible_set)
    return compatible


def determine_tool_activation_mode(
    step: TaskStep,
    *,
    missing_requirements: Sequence[Any] = (),
    compatible_tools: Sequence[str] = (),
    has_trust_block: bool = False,
    has_uncertain_action: bool = False,
    remaining_budget: float | None = None,
    satisfied_all_evidence: bool = False,
) -> ToolActivationMode:
    """Determine whether structured tool proposal is required on this turn.

    REQUIRED is only set when ALL 5 conditions are true:
    1. Current step is tool-required (kind != REASONING or missing evidence exists);
    2. Required observable execution evidence is missing;
    3. At least one compatible visible tool exists;
    4. No hard Trust/block/uncertain state already exists;
    5. Remaining budget permits another execution turn.
    """
    kind = step.execution_kind or StepExecutionKind.REASONING
    if kind is StepExecutionKind.REASONING and not missing_requirements:
        return ToolActivationMode.NONE

    if satisfied_all_evidence or (bool(step.evidence_requirements) and not missing_requirements):
        return ToolActivationMode.NONE

    if has_trust_block or has_uncertain_action:
        return ToolActivationMode.NONE

    if remaining_budget is not None and remaining_budget <= 0:
        return ToolActivationMode.NONE

    if not compatible_tools:
        return ToolActivationMode.NONE

    is_tool_kind = kind in {
        StepExecutionKind.READ,
        StepExecutionKind.WRITE,
        StepExecutionKind.COMMAND,
        StepExecutionKind.EXTERNAL_ACTION,
        StepExecutionKind.MIXED,
    }
    if is_tool_kind or bool(missing_requirements):
        return ToolActivationMode.REQUIRED

    return ToolActivationMode.NONE


def format_first_turn_scaffold(
    step: TaskStep,
    compatible_tools: Sequence[str],
    missing_requirements: Sequence[str] = (),
) -> str:
    """Format the bounded first-turn execution scaffold contract."""
    if len(compatible_tools) > 5:
        shown = sorted(compatible_tools)[:5]
        tools_str = "\n".join(f"- {t}" for t in shown) + f"\n- ... ({len(compatible_tools) - 5} more)"
    elif compatible_tools:
        tools_str = "\n".join(f"- {t}" for t in sorted(compatible_tools))
    else:
        tools_str = "- none"
    missing_str = "\n".join(f"- {r}" for r in missing_requirements) if missing_requirements else "- observable execution evidence"
    directive = (
        f"CURRENT STEP EXECUTION CONTRACT\n\n"
        f"Objective:\n{step.instruction}\n\n"
        f"Execution:\nTOOL ACTION REQUIRED\n\n"
        f"Missing observable evidence:\n{missing_str}\n\n"
        f"Available compatible tools:\n{tools_str}\n\n"
        f"Return a structured tool action. Do not claim completion from prose."
    )
    return redact_secrets(directive)


@dataclass
class ExecutorMetricsTracker:
    """Tracks structured Executor protocol reliability metrics across task runs."""

    total_turns: int = 0
    valid_actions: int = 0
    tool_call_required_turns: int = 0
    required_tool_invocations: int = 0
    unknown_tool_proposals: int = 0
    invalid_argument_proposals: int = 0
    premature_final_proposals: int = 0
    no_progress_turns: int = 0
    protocol_corrections_attempted: int = 0
    protocol_corrections_succeeded: int = 0
    sequence_completions: int = 0
    sequence_total: int = 0
    first_turn_successes: int = 0
    total_steps: int = 0
    verified_steps: int = 0
    executor_turns_for_verified: int = 0
    input_tokens_executor: int = 0
    output_tokens_executor: int = 0
    checkpoints_produced: int = 0
    checkpoints_expected: int = 0

    def record_turn(
        self,
        *,
        is_valid_action: bool = True,
        is_tool_required: bool = False,
        required_tool_invoked: bool = False,
        is_unknown_tool: bool = False,
        is_invalid_argument: bool = False,
        is_premature_final: bool = False,
        is_no_progress: bool = False,
        turn_number: int = 1,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        self.total_turns += 1
        if is_valid_action:
            self.valid_actions += 1
        if is_tool_required:
            self.tool_call_required_turns += 1
            if required_tool_invoked:
                self.required_tool_invocations += 1
        if is_unknown_tool:
            self.unknown_tool_proposals += 1
        if is_invalid_argument:
            self.invalid_argument_proposals += 1
        if is_premature_final:
            self.premature_final_proposals += 1
        if is_no_progress:
            self.no_progress_turns += 1
        if turn_number == 1 and is_valid_action and not is_no_progress:
            self.first_turn_successes += 1
        if input_tokens is not None:
            self.input_tokens_executor += input_tokens
        if output_tokens is not None:
            self.output_tokens_executor += output_tokens

    def record_correction(self, succeeded: bool) -> None:
        self.protocol_corrections_attempted += 1
        if succeeded:
            self.protocol_corrections_succeeded += 1

    def record_step_completion(self, verified: bool, turns_used: int) -> None:
        self.total_steps += 1
        if verified:
            self.verified_steps += 1
            self.executor_turns_for_verified += turns_used

    def record_sequence(self, completed: bool) -> None:
        self.sequence_total += 1
        if completed:
            self.sequence_completions += 1

    def record_checkpoints(self, produced: int, expected: int) -> None:
        self.checkpoints_produced += produced
        self.checkpoints_expected += expected

    def compute_metrics(self) -> dict[str, Any]:
        return {
            "executor_valid_action_rate": (
                self.valid_actions / self.total_turns if self.total_turns > 0 else 1.0
            ),
            "executor_tool_call_required_rate": (
                self.tool_call_required_turns / self.total_turns if self.total_turns > 0 else 0.0
            ),
            "executor_required_tool_invocation_rate": (
                self.required_tool_invocations / self.tool_call_required_turns
                if self.tool_call_required_turns > 0
                else 1.0
            ),
            "executor_unknown_tool_rate": (
                self.unknown_tool_proposals / self.total_turns if self.total_turns > 0 else 0.0
            ),
            "executor_invalid_argument_rate": (
                self.invalid_argument_proposals / self.total_turns if self.total_turns > 0 else 0.0
            ),
            "executor_premature_final_rate": (
                self.premature_final_proposals / self.total_turns if self.total_turns > 0 else 0.0
            ),
            "executor_no_progress_rate": (
                self.no_progress_turns / self.total_turns if self.total_turns > 0 else 0.0
            ),
            "executor_protocol_correction_rate": (
                self.protocol_corrections_attempted / self.total_turns if self.total_turns > 0 else 0.0
            ),
            "executor_protocol_correction_success_rate": (
                self.protocol_corrections_succeeded / self.protocol_corrections_attempted
                if self.protocol_corrections_attempted > 0
                else None
            ),
            "executor_sequence_completion_rate": (
                self.sequence_completions / self.sequence_total if self.sequence_total > 0 else None
            ),
            "executor_checkpoint_realization_rate": (
                self.checkpoints_produced / max(1, self.checkpoints_expected)
                if self.checkpoints_expected > 0
                else None
            ),
            "executor_first_turn_success_rate": (
                self.first_turn_successes / max(1, self.total_steps) if self.total_steps > 0 else 1.0
            ),
            "average_executor_turns_per_verified_step": (
                self.executor_turns_for_verified / max(1, self.verified_steps)
                if self.verified_steps > 0
                else 0.0
            ),
            "input_tokens_executor": self.input_tokens_executor,
            "output_tokens_executor": self.output_tokens_executor,
            "tokens_per_verified_executor_step": (
                (self.input_tokens_executor + self.output_tokens_executor) / max(1, self.verified_steps)
                if self.verified_steps > 0
                else 0.0
            ),
        }
