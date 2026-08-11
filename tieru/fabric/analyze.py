"""Fast deterministic-first task analysis; no model or network required."""

from __future__ import annotations

import re

from tieru.fabric.models import TaskProfile

_GREETING = re.compile(
    r"^(hi|hello|hey|hiya|good (morning|afternoon|evening)|thanks|thank you|ok|okay)[!. ]*$",
    re.IGNORECASE,
)
_DEEP = re.compile(
    r"\b(architecture|architectural|analysis|analy[sz]e|comprehensive|deep(?:ly)?|"
    r"repository-wide|root cause|debug(?:ging)?|system design|trade-?offs?|"
    r"multi-step analysis)\b",
    re.IGNORECASE,
)
_REPOSITORY = re.compile(
    r"\b(repo(?:sitory)?|codebase|project-wide|all files)\b", re.IGNORECASE
)
_ACTION = re.compile(
    r"\b(create|write|edit|modify|delete|remove|run|execute|install|deploy|send|"
    r"browse|open|commit|push|fix|implement|build|test|lint)\b",
    re.IGNORECASE,
)
_TOOL_TARGET = re.compile(
    r"\b(file|folder|repo(?:sitory)?|workspace|browser|website|shell|command|tests?|"
    r"calendar|message|email|database|package|code)\b",
    re.IGNORECASE,
)
_MEMORY = re.compile(
    r"\b(remember|recall|my (?:preference|project|meeting|note|schedule)|last time|"
    r"what did i|when did i)\b",
    re.IGNORECASE,
)
_PLANNING = re.compile(r"\b(plan|roadmap|strategy|steps|approach)\b", re.IGNORECASE)
_SUMMARY = re.compile(r"\b(summarize|summary|condense|tldr)\b", re.IGNORECASE)
_QUESTION = re.compile(
    r"^(what|why|when|where|who|how|which|can you explain)\b", re.IGNORECASE
)
_LOCAL_ONLY = re.compile(
    r"\b(local[- ]only|stay local|use (?:a )?local model|do not use (?:the )?cloud|"
    r"don't use (?:the )?cloud|offline only|never send .* cloud)\b",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"\b(api[-_ ]?key|access token|bearer token|password|credential|private key|"
    r"secret(?:s)?|\.env)\b",
    re.IGNORECASE,
)


class TaskAnalyzer:
    """Describe observable request shape without retaining the message."""

    def analyze(self, message: str) -> TaskProfile:
        text = " ".join(str(message or "").strip().split())
        low = text.lower()
        if _GREETING.fullmatch(text):
            return TaskProfile(
                "greeting", "low", signals=("short_conversational_input",),
                reason_codes=("greeting_or_acknowledgement",),
            )

        deep = bool(_DEEP.search(text))
        repository = bool(_REPOSITORY.search(text))
        action = bool(_ACTION.search(text) and _TOOL_TARGET.search(text))
        memory = bool(_MEMORY.search(text))
        local_only = bool(_LOCAL_ONLY.search(text) or _SENSITIVE.search(text))
        signals: list[str] = []
        reasons: list[str] = []

        if deep:
            signals.append("deep_analysis_language")
            reasons.append("complex_analysis_requested")
        if repository:
            signals.append("repository_scope")
            reasons.append("repository_scope")
        if action:
            signals.append("explicit_action_language")
            reasons.append("explicit_tool_or_file_task")
        if memory:
            signals.append("personal_memory_reference")
            reasons.append("memory_may_be_required")
        if local_only:
            signals.append("trusted_local_only_signal")
            reasons.append("local_only_required")

        if deep:
            return TaskProfile(
                "analysis", "high", requires_tools=action,
                requires_memory=memory, requires_deep_context=True,
                requires_verification=True, estimated_scope="multi_step",
                privacy="local_only" if local_only else "private",
                signals=tuple(signals), reason_codes=tuple(reasons),
            )
        if action:
            task_type = "coding" if re.search(r"\b(code|file|repo|test|lint|fix|implement)\b", low) else "tool_task"
            return TaskProfile(
                task_type, "normal", requires_tools=True, requires_memory=memory,
                estimated_scope="multi_step", signals=tuple(signals),
                privacy="local_only" if local_only else "private",
                reason_codes=tuple(reasons),
            )
        if _SUMMARY.search(text):
            return TaskProfile(
                "summarization", "normal", requires_memory=memory,
                privacy="local_only" if local_only else "private",
                signals=tuple(signals), reason_codes=tuple(reasons or ["summarization_request"]),
            )
        if _PLANNING.search(text):
            return TaskProfile(
                "planning", "normal", requires_memory=memory,
                privacy="local_only" if local_only else "private",
                signals=tuple(signals), reason_codes=tuple(reasons or ["planning_request"]),
            )
        if _QUESTION.search(text) or text.endswith("?"):
            return TaskProfile(
                "lookup", "normal", requires_memory=memory,
                privacy="local_only" if local_only else "private",
                signals=tuple(signals), reason_codes=tuple(reasons or ["ordinary_question"]),
            )
        if text and len(text.split()) <= 8 and re.search(r"\b(i|you|we|feel|think)\b", low):
            return TaskProfile(
                "chat", "normal", requires_memory=memory,
                privacy="local_only" if local_only else "private",
                signals=tuple(signals), reason_codes=tuple(reasons or ["conversational_request"]),
            )
        return TaskProfile(
            "unknown", "normal", requires_memory=memory,
            privacy="local_only" if local_only else "private",
            signals=tuple(signals), reason_codes=tuple(reasons or ["unknown_task_shape"]),
        )
