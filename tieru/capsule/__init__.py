"""Tieru Capsule: portable, selective, offline identity snapshots."""

from tieru.capsule.manifest import FORMAT, FORMAT_VERSION, inspect_capsule
from tieru.capsule.models import CapsuleInspection, CapsuleLimits, ImportPlan
from tieru.capsule.security import CapsuleError
from tieru.capsule.service import CapsuleService

__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "CapsuleError",
    "CapsuleInspection",
    "CapsuleLimits",
    "CapsuleService",
    "ImportPlan",
    "inspect_capsule",
]
