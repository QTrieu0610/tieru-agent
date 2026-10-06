import sys
from pathlib import Path

# evals/ sits next to waku/, not inside it — make both importable when
# running `pytest evals` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os

# Sanitize NO_PROXY if httpx cannot parse bare IPv6 entries like '::1' on Windows
if "NO_PROXY" in os.environ and "::1" in os.environ["NO_PROXY"]:
    cleaned = [p.strip() for p in os.environ["NO_PROXY"].split(",") if p.strip() not in ("::1", "::1/128")]
    os.environ["NO_PROXY"] = ",".join(cleaned)

# Provide mock keys for offline/deterministic testing if not set in environment
os.environ.setdefault("ANTHROPIC_API_KEY", "mock-anthropic-key-for-deterministic-tests")
os.environ.setdefault("OPENAI_API_KEY", "mock-openai-key-for-deterministic-tests")

