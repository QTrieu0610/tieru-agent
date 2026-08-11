"""Tieru — a local-first personal AI runtime.

Current subsystem map:
  Runtime      → tieru/runtime + tieru/gateway + tieru/loop
  Memory       → tieru/memory
  Skills       → tieru/memory/procedural
  Model Layer  → tieru/loop/models.py + tieru/providers
  Trust/Tools  → tieru/tools/registry.py + tieru/tools
  Replay       → tieru/replay (local, normalized, read-only run inspection)
  Operations   → tieru/ops + evals (tracing and evaluation)
"""

from tieru.app import Tieru

__version__ = "0.3.0b1"
__all__ = ["Tieru", "__version__"]
