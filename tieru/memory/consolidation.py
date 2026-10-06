"""Opt-in consolidation — distilling chats into durable memory, but only sometimes.

The whiteboard's diamond: "only consolidate after N new chats". Running a
summarizer after every message is wasteful and noisy; batching N exchanges
gives the summarizer enough context to extract facts worth keeping. The app calls
this path only when ``memory_write_policy=consolidate``; the default is explicit.

A cheap model reads the unconsolidated chat log and produces:
  - facts   → semantic memory ("Alex prefers morning meetings")
  - episode → episodic memory ("2026-07-10: planned the Acme demo with Alex")
"""

from __future__ import annotations

import json
import re
from datetime import date

import anthropic

from tieru.context import ContextBuilder
from tieru.memory.episodic.store import SqliteEpisodeStore
from tieru.memory.semantic.store import SqliteFactStore

SUMMARIZER_PROMPT = """\
You distill a personal assistant's recent conversation into long-term memory.

From the exchanges below, extract:
1. durable facts about the user, their people, projects, or preferences —
   only things worth remembering in a month; skip chit-chat and one-offs.
2. one single-sentence episode summarizing what happened in this conversation.

Never extract the assistant's own operating state: account balances, credits,
billing or spend, rate limits, tool errors or status codes, token counts, or
how the assistant or its tools work. It changes by the hour and says nothing
about the user.

Reply with ONLY this JSON:
{{"facts": [{{"subject": "<who/what>", "content": "<one sentence>"}}], "episode": "<one sentence>"}}

Exchanges:
{log}"""

RECALL_RULE = """
These exchanges answered from memory that is already kept, quoted below. Do NOT
extract a fact it already holds, even reworded. Keep only what is new: what
the user said, decided or asked to note.
Memory read this turn:
{recalled}"""

RECALL_PROMPT_CHARS = 6000

_MONEYISH = re.compile(
    r"\$|\busd\b|\bcredits?\b|\bmicro\b|\baccount\b|\bwallet\b|"
    r"\btop(?:ped)?[- ]?up\b|\binsufficient\b|\btreg\b|\bapi\b",
    re.IGNORECASE,
)
_OPS_STATE = re.compile(
    r"\binsufficient\b|"
    r"\b(?:out of|no|remaining|left in)\s+(?:\w+\s+)?credits?\b|\bcredits?\s+(?:left|remaining|balance)\b|"
    r"\d[\d,.]*\s*micro(?:-?usd)?\b|"
    r"\b(?:http|status(?: code)?|error(?: code)?)\s*[45]\d\d\b|"
    r"\b(?:returned|got|hit|with|at)\s+(?:an?\s+)?(?:http\s+)?(?:402|429)\b|"
    r"\brate[- ]limit|\btoo many requests\b|\bquota (?:exceeded|hit|reached|used)\b|"
    r"\b\d[\d,.]*\s*k?\s*(?:input |output |prompt |completion )?tokens\b|"
    r"\b(?:timed out|returned an error|failed with|tool error)\b|"
    r"\b(?:billing|billed|charged|spend|spent)\b.*\b(?:api|platform|tieru|waku|per call|account)\b|"
    r"\b(?:tieru|waku|api|platform)\b.*\b(?:billing|billed|charged)\b|"
    r"\b(?:system prompt|context window|tool calls?)\b",
    re.IGNORECASE,
)


def is_ops_state(fact: dict) -> bool:
    """True when a proposed fact is about the assistant's own operating state."""
    text = f"{fact.get('subject', '')} {fact.get('content', '')}"
    if re.search(r"\bbalance\b", text, re.IGNORECASE) and _MONEYISH.search(text):
        return True
    return _OPS_STATE.search(text) is not None


def _words(text: str) -> set[str]:
    text = re.sub(r"\\[nrt]", " ", text).replace("\\u2019", "'").replace("\u2019", "'")
    found = set()
    for word in re.findall(r"[\w$'.,%-]+", text):
        word = word.strip("'.,-").lstrip("$").lower()
        word = word.removesuffix("'s")
        if word:
            found.add(word)
    return found


def restates(fact: dict, recalled: str) -> bool:
    """True when the fact only repeats memory the turn read."""
    if not recalled:
        return False
    content = str(fact.get("content", "")).replace("\u2019", "'")
    key = _words(" ".join(w for w in content.split() if any(c.isupper() or c.isdigit() for c in w)))
    has_number = any(w[0].isdigit() for w in key)
    return has_number and key <= _words(recalled)


def consolidate_if_due(
    conn,
    client: anthropic.Anthropic,
    small_model: str,
    every_n: int,
    facts: SqliteFactStore,
    episodes: SqliteEpisodeStore,
    recalled: str = "",
) -> int:
    """Returns how many new facts were written (0 = not due or nothing worth keeping)."""
    rows = conn.execute(
        "SELECT id, role, content FROM chat_log WHERE consolidated = 0 ORDER BY id"
    ).fetchall()
    if len(rows) < every_n * 2:  # each exchange = 2 rows (user + assistant)
        return 0

    log = "\n".join(f"{r['role']}: {r['content']}" for r in rows)
    prompt = SUMMARIZER_PROMPT.replace("Exchanges:\n{log}", "")
    if recalled:
        prompt += RECALL_RULE.format(recalled=recalled[:RECALL_PROMPT_CHARS])
    try:
        builder = ContextBuilder(max_block_bytes=12_000, max_data_bytes=16_000)
        builder.add_control(
            prompt,
            source="memory_consolidation",
        )
        builder.add_data(log, source="conversation_history")
        assembly = builder.build()
        response = client.messages.create(
            model=small_model,
            max_tokens=600,
            system=assembly.system,
            messages=list(assembly.messages),
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        distilled = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except Exception:
        return 0  # never lose the log — it stays unconsolidated for next time

    written = 0
    for fact in distilled.get("facts", []):
        if not (fact.get("subject") and fact.get("content")):
            continue
        if is_ops_state(fact):
            continue
        if recalled and restates(fact, recalled):
            continue
        try:
            facts.add(fact["subject"], fact["content"], source="consolidation")
            written += int(getattr(facts, "last_created", True))
        except ValueError:
            pass  # malformed or credential-shaped memory is never persisted
    if distilled.get("episode"):
        try:
            episodes.add(distilled["episode"], happened_at=date.today().isoformat())
        except ValueError:
            pass

    conn.execute(
        f"UPDATE chat_log SET consolidated = 1 WHERE id IN ({','.join('?' * len(rows))})",
        [r["id"] for r in rows],
    )
    conn.commit()
    return written
