"""Tieru Capability Discovery and Tool Routing subsystem."""

from __future__ import annotations

from tieru.capabilities.catalog import build_capability_catalog
from tieru.capabilities.models import (
    Capability,
    CapabilityMatch,
    CapabilityRouterConfig,
    CapabilityRoutingResult,
)
from tieru.capabilities.router import CapabilityRouter

__all__ = [
    "Capability",
    "CapabilityMatch",
    "CapabilityRouter",
    "CapabilityRouterConfig",
    "CapabilityRoutingResult",
    "build_capability_catalog",
]
