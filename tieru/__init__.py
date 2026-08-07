"""Tieru — a minimal, transparent, local-first personal AI agent.

Four pillars, one module each:
  harness  → tieru/runtime + tieru/gateway  (scaffolding around the raw LLM)
  loop     → tieru/loop                     (observe → reason → act → repeat)
             tieru/graph                    (opt-in structure around the loop)
  memory   → tieru/memory                   (procedural / semantic / episodic)
  ops      → tieru/ops + evals/             (trace → eval → gate → release)
"""

from tieru.app import Tieru

__version__ = "0.2.0"
__all__ = ["Tieru", "__version__"]
