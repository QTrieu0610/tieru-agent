"""Production-owned authority types for model-facing context."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class ContextTrust(StrEnum):
    CONTROL = "control"
    REVIEWED = "reviewed"
    USER = "user"
    DATA = "data"


@dataclass(frozen=True)
class ContextBlock:
    source: str
    trust: ContextTrust
    content: str
    metadata: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    original_size: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class ContextAssembly:
    """Provider-neutral output: privileged prompt plus role-preserving messages."""

    system: str
    messages: tuple[dict, ...]
    blocks: tuple[ContextBlock, ...]

    def count(self, trust: ContextTrust) -> int:
        return sum(block.trust is trust for block in self.blocks)

    @property
    def data_sources(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                block.source for block in self.blocks if block.trust is ContextTrust.DATA
            )
        )
