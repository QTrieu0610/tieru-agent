"""K3-as-referee quality scoring for the Compare arena.

Completion (tieru.ops.scoring) is deterministic — did the right tool fire. Quality
is the other half: *how good was the answer*, for the open-ended part a checklist
can't see. There's no single right answer, so we do what the market does for that
axis (MT-Bench / Chatbot-Arena style): an LLM grades the transcript against a
rubric, 0-10 + a one-line reason.

The referee must be a model that ISN'T racing — otherwise it grades itself, which
is neither fair nor credible (you can't test K3 with K3 as the judge). Default is
**gpt-5.6-sol**: a strong reasoning model that happens to be a poor *contestant*
here (it can't call tools on the chat endpoint) but a fine *judge* (grading is
pure text, no tools). Switchable per-race from the arena, or via TIERU_JUDGE_*.
Any provider works — Tieru's OpenAI-compat client exposes the same
`.messages.create` shape as the anthropic wire, so the judge is provider-agnostic.
"""

from __future__ import annotations

import json
import threading
import time

from tieru.config import load_settings, with_role
from tieru.context import ContextBuilder
from tieru.loop.models import ModelRouter

_DEFAULT_JUDGE = load_settings().role("judge")
JUDGE_PROVIDER = _DEFAULT_JUDGE.provider
JUDGE_MODEL = _DEFAULT_JUDGE.model

# A race grades every column at once — 8 judge calls hitting one endpoint
# simultaneously gets some 429'd, and those columns show "—". Cap how many judge
# calls run concurrently (shared across the race's threads) so the referee isn't
# stampeded; the rest queue and still get graded.
_JUDGE_SEMAPHORES: dict[int, threading.Semaphore] = {}


def _judge_semaphore(limit: int) -> threading.Semaphore:
    return _JUDGE_SEMAPHORES.setdefault(limit, threading.Semaphore(limit))

_RUBRIC = """You are a strict, fair judge scoring an AI assistant's reply.

The user asked:
{task}

The assistant replied:
{reply}
{actions}
Score how well the reply serves the user's request on a 0-10 scale:
- 9-10: fully addresses the request, correct, concise, honest about any limits.
- 5-8: mostly addresses it, minor gaps, padding, or small errors.
- 1-4: partial, vague, or partly wrong.
- 0: ignores the request, or claims an action that is NOT in the tool list above.

IMPORTANT: the tools listed above REALLY ran — this assistant can take those
actions. Do NOT penalize the reply for saying it did something that appears in
that list; those claims are true. Only "hallucinating" counts against it when it
claims an action with no matching tool call.

Reply with ONLY a JSON object, no prose:
{{"score": <int 0-10>, "reason": "<one short sentence>"}}"""


def judge_reply(task: str, reply: str, provider: str | None = None,
                model: str | None = None, tools: list | None = None) -> dict | None:
    """Grade one reply. `tools` is the list of tool names that ACTUALLY fired this
    turn — passed to the judge as ground truth so a truthful "I saved that" (with
    save_note in the list) isn't mistaken for a hallucination. Returns
    {"score": 0-10, "reason": str, "judge": model} or None if there's nothing to
    grade or the judge is unreachable (a judge hiccup must never fail a race)."""
    if not (reply or "").strip():
        return None
    settings = load_settings()
    current = settings.role("judge")
    if provider or model:
        target_provider = provider or current.provider
        target = settings.providers.get(target_provider)
        if target is None:
            return None
        settings = with_role(
            settings,
            "judge",
            provider=target_provider,
            protocol=target.protocol,
            model=model or (current.model if target_provider == current.provider
                            else target.default_model),
            base_url=target.base_url,
            api_key_env=target.api_key_env,
        )
    judge_role = settings.role("judge")
    actions = (f"Tools the assistant actually ran this turn: {', '.join(tools)}."
               if tools else "The assistant ran no tools this turn.")
    builder = ContextBuilder(max_block_bytes=8192, max_data_bytes=16_000)
    builder.add_control(
        _RUBRIC.format(task="[TASK DATA]", reply="[REPLY DATA]", actions="[ACTION DATA]"),
        source="eval_judge",
    )
    builder.add_data(task[:2000], source="eval_task")
    builder.add_data(reply[:4000], source="eval_reply")
    builder.add_data(actions, source="eval_actions")
    assembly = builder.build()
    # A race judges every column at once, so the endpoint sees a burst and may
    # 429. Retry ONLY the API call (with growing backoff); the semaphore caps how
    # many run concurrently. A response that arrives but won't parse isn't
    # transient — don't waste retries on it.
    resp = None
    for attempt in range(4):
        try:
            router = ModelRouter(settings)
            client = router.client("judge")
            with _judge_semaphore(settings.judge_concurrency):
                resp = client.messages.create(
                    model=judge_role.model, max_tokens=300,
                    system=assembly.system, messages=list(assembly.messages), tools=[])
            break
        except Exception:
            if attempt < 3:
                time.sleep(1.2 * (attempt + 1))   # 1.2s, 2.4s, 3.6s — let a 429 clear
    if resp is None:
        return None
    try:
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        obj = json.loads(text[text.index("{"): text.rindex("}") + 1])
        score = max(0, min(10, int(obj["score"])))
        return {
            "score": score,
            "reason": str(obj.get("reason", ""))[:200],
            "judge": judge_role.model,
        }
    except Exception:
        return None   # got a response, just not valid JSON — retrying won't help
