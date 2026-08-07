"""Unified, local-first personal memory over Tieru's fact and episode tables."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from tieru.memory.semantic.store import _fts_query

MemoryKind = Literal["fact", "episode"]
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|password|secret|token)\s*[:=]\s*\S+", re.IGNORECASE
    ),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


class UnsafeMemoryError(ValueError):
    """Raised when content looks like a credential or secret."""


class MemoryCapacityError(RuntimeError):
    """Raised instead of silently growing memory without bound."""


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    content: str
    kind: MemoryKind
    source: str
    provenance: str
    created_at: str
    updated_at: str
    importance: float
    trusted: bool
    subject: str = ""
    happened_at: str = ""


def contains_secret(text: str) -> bool:
    return any(pattern.search(text or "") for pattern in _SECRET_PATTERNS)


def redact_secrets(text: str) -> str:
    """Keep chat continuity without persisting credential-shaped values."""
    if not text:
        return text
    if _SECRET_PATTERNS[0].search(text):
        return "[REDACTED SECRET]"
    for pattern in _SECRET_PATTERNS[1:]:
        text = pattern.sub("[REDACTED SECRET]", text)
    return text


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _fingerprint(kind: str, subject: str, content: str, happened_at: str) -> str:
    material = "|".join((kind, _normalize(subject), _normalize(content), happened_at[:10]))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class PersonalMemoryStore:
    """CRUD/search/export parity across semantic facts and dated episodes.

    IDs are stable, typed references (``fact:12`` / ``episode:7``). FTS5 is
    always the lexical source of truth. Optional semantic stores may augment
    fact search, but a failure never removes deterministic lexical retrieval.
    """

    def __init__(self, conn: sqlite3.Connection, max_records: int = 10000):
        self.conn = conn
        self.max_records = max_records
        self._backfill_fingerprints()

    def _backfill_fingerprints(self) -> None:
        for row in self.conn.execute(
            "SELECT id, subject, content FROM facts WHERE fingerprint = ''"
        ).fetchall():
            self.conn.execute(
                "UPDATE facts SET fingerprint=? WHERE id=?",
                (_fingerprint("fact", row["subject"], row["content"], ""), row["id"]),
            )
        for row in self.conn.execute(
            "SELECT id, happened_at, summary FROM episodes WHERE fingerprint = ''"
        ).fetchall():
            self.conn.execute(
                "UPDATE episodes SET fingerprint=? WHERE id=?",
                (
                    _fingerprint("episode", "", row["summary"], row["happened_at"]),
                    row["id"],
                ),
            )
        self.conn.commit()

    @staticmethod
    def _parts(memory_id: str) -> tuple[MemoryKind, int]:
        try:
            kind, raw_id = str(memory_id).split(":", 1)
            row_id = int(raw_id)
        except (ValueError, TypeError) as exc:
            raise ValueError("memory id must look like fact:12 or episode:7") from exc
        if kind not in ("fact", "episode"):
            raise ValueError("memory id must start with fact: or episode:")
        return kind, row_id  # type: ignore[return-value]

    def _count(self) -> int:
        facts = self.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        episodes = self.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        return int(facts) + int(episodes)

    def add(
        self,
        *,
        content: str,
        kind: MemoryKind = "fact",
        source: str = "user",
        provenance: str = "explicit user request",
        importance: float = 0.5,
        trusted: bool = True,
        subject: str = "user",
        happened_at: str = "",
    ) -> tuple[MemoryRecord, bool]:
        content = content.strip()
        if not content:
            raise ValueError("memory content must not be empty")
        if contains_secret(content):
            raise UnsafeMemoryError("refusing to store content that looks like a secret")
        if kind not in ("fact", "episode"):
            raise ValueError("kind must be fact or episode")
        if source.lower() in {"browser", "web", "search_web"}:
            trusted = False
        importance = min(1.0, max(0.0, float(importance)))
        if kind == "episode" and not happened_at:
            happened_at = datetime.now(UTC).date().isoformat()
        fingerprint = _fingerprint(kind, subject, content, happened_at)
        table = "facts" if kind == "fact" else "episodes"
        existing = self.conn.execute(
            f"SELECT id FROM {table} WHERE fingerprint=? LIMIT 1", (fingerprint,)
        ).fetchone()
        if existing:
            return self.get(f"{kind}:{existing['id']}"), False
        if self._count() >= self.max_records:
            raise MemoryCapacityError(
                f"memory limit ({self.max_records}) reached; delete an item before adding another"
            )
        if kind == "fact":
            cur = self.conn.execute(
                """INSERT INTO facts
                   (subject, content, source, provenance, importance, trusted, fingerprint)
                   VALUES (?,?,?,?,?,?,?)""",
                (subject.lower().strip(), content, source, provenance, importance,
                 int(trusted), fingerprint),
            )
        else:
            cur = self.conn.execute(
                """INSERT INTO episodes
                   (happened_at, summary, source, provenance, importance, trusted, fingerprint)
                   VALUES (?,?,?,?,?,?,?)""",
                (happened_at, content, source, provenance, importance, int(trusted), fingerprint),
            )
        self.conn.commit()
        return self.get(f"{kind}:{cur.lastrowid}"), True

    def get(self, memory_id: str) -> MemoryRecord:
        kind, row_id = self._parts(memory_id)
        if kind == "fact":
            row = self.conn.execute("SELECT * FROM facts WHERE id=?", (row_id,)).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM episodes WHERE id=?", (row_id,)).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return self._record(kind, row)

    @staticmethod
    def _record(kind: MemoryKind, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=f"{kind}:{row['id']}",
            content=row["content"] if kind == "fact" else row["summary"],
            kind=kind,
            source=row["source"] or "user",
            provenance=row["provenance"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or row["created_at"] or "",
            importance=float(row["importance"]),
            trusted=bool(row["trusted"]),
            subject=row["subject"] if kind == "fact" else "",
            happened_at=row["happened_at"] if kind == "episode" else "",
        )

    def list(self, *, kind: MemoryKind | None = None, limit: int = 200) -> list[MemoryRecord]:
        kinds = (kind,) if kind else ("fact", "episode")
        records: list[MemoryRecord] = []
        for item_kind in kinds:
            table = "facts" if item_kind == "fact" else "episodes"
            rows = self.conn.execute(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            records.extend(self._record(item_kind, row) for row in rows)
        records.sort(key=lambda record: (record.created_at, record.id), reverse=True)
        return records[:limit]

    def search(
        self, query: str, *, top_k: int = 8, trusted_only: bool = False
    ) -> list[MemoryRecord]:
        fts = _fts_query(query)
        if not fts:
            records = self.list(limit=top_k * 2)
            return [record for record in records if record.trusted or not trusted_only][:top_k]
        trust_fact = " AND f.trusted = 1" if trusted_only else ""
        trust_episode = " AND e.trusted = 1" if trusted_only else ""
        facts = self.conn.execute(
            "SELECT f.* FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid "
            f"WHERE facts_fts MATCH ?{trust_fact} ORDER BY rank, f.id LIMIT ?",
            (fts, top_k),
        ).fetchall()
        episodes = self.conn.execute(
            "SELECT e.* FROM episodes_fts JOIN episodes e ON e.id=episodes_fts.rowid "
            f"WHERE episodes_fts MATCH ?{trust_episode} ORDER BY rank, e.happened_at DESC LIMIT ?",
            (fts, top_k),
        ).fetchall()
        records = [self._record("fact", row) for row in facts]
        records += [self._record("episode", row) for row in episodes]
        return records[:top_k]

    def update(self, memory_id: str, **changes) -> MemoryRecord:
        current = self.get(memory_id)
        allowed = {"content", "subject", "happened_at", "importance", "trusted", "provenance"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported memory fields: {', '.join(sorted(unknown))}")
        content = str(changes.get("content", current.content)).strip()
        if contains_secret(content):
            raise UnsafeMemoryError("refusing to store content that looks like a secret")
        subject = str(changes.get("subject", current.subject))
        happened_at = str(changes.get("happened_at", current.happened_at))
        importance = min(1.0, max(0.0, float(changes.get("importance", current.importance))))
        trusted = int(bool(changes.get("trusted", current.trusted)))
        provenance = str(changes.get("provenance", current.provenance))
        fingerprint = _fingerprint(current.kind, subject, content, happened_at)
        now = datetime.now(UTC).isoformat(timespec="seconds")
        kind, row_id = self._parts(memory_id)
        if kind == "fact":
            self.conn.execute(
                """UPDATE facts SET content=?, subject=?, importance=?, trusted=?, provenance=?,
                   fingerprint=?, updated_at=? WHERE id=?""",
                (content, subject.lower().strip(), importance, trusted, provenance,
                 fingerprint, now, row_id),
            )
        else:
            self.conn.execute(
                """UPDATE episodes SET summary=?, happened_at=?, importance=?, trusted=?,
                   provenance=?, fingerprint=?, updated_at=? WHERE id=?""",
                (content, happened_at, importance, trusted, provenance, fingerprint, now, row_id),
            )
        self.conn.commit()
        return self.get(memory_id)

    def delete(self, memory_id: str) -> bool:
        kind, row_id = self._parts(memory_id)
        table = "facts" if kind == "fact" else "episodes"
        cur = self.conn.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def export(self, path: Path | None = None) -> str:
        payload = json.dumps([asdict(record) for record in self.list(limit=self.max_records)],
                             indent=2, ensure_ascii=False)
        if path is not None:
            path.write_text(payload + "\n", encoding="utf-8")
        return payload
