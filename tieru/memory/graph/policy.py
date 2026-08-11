"""Deterministic relation policy for Tieru Memory Graph."""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_SINGLE_VALUE_PREDICATES = frozenset(
    {
        "DEFAULT_MODEL",
        "USES_DEFAULT_MODEL",
        "CURRENT_ROLE",
        "PRIMARY_PROJECT",
    }
)


def normalize_predicate(value: str) -> str:
    normalized = "_".join((value or "").strip().upper().replace("-", " ").split())
    if not normalized or not all(ch.isalnum() or ch == "_" for ch in normalized):
        raise ValueError("predicate must be a machine-readable identifier")
    return normalized


@dataclass(frozen=True)
class GraphPolicy:
    """Predicates with one current value supersede older active relations."""

    single_value_predicates: frozenset[str] = field(
        default_factory=lambda: DEFAULT_SINGLE_VALUE_PREDICATES
    )

    def __post_init__(self) -> None:
        normalized = frozenset(normalize_predicate(item) for item in self.single_value_predicates)
        object.__setattr__(self, "single_value_predicates", normalized)

    def is_single_value(self, predicate: str) -> bool:
        return normalize_predicate(predicate) in self.single_value_predicates
