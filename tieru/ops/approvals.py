"""Dashboard transport for M3 tool confirmations (not a policy engine)."""

from __future__ import annotations

import contextlib
import secrets
import threading
import time
from dataclasses import dataclass, field

from tieru.tools.registry import PermissionRequest


@dataclass
class _PendingApproval:
    id: str
    session_id: str
    request: PermissionRequest
    created_at: float
    expires_at: float
    event: threading.Event = field(default_factory=threading.Event)
    decision: bool = False
    resolved: bool = False

    def public(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "tool": self.request.tool,
            "risk": self.request.risk,
            "read_only": self.request.read_only,
            "capabilities": list(self.request.capabilities),
            "args": self.request.args,
            "argument_hash": self.request.argument_hash,
            "reason": self.request.reason,
            "expires_in": max(0, int(self.expires_at - time.monotonic())),
        }


class DashboardApprovalService:
    """Bridge a synchronous gate callback to an authenticated dashboard tab."""

    def __init__(self, ttl_seconds: float = 30.0, heartbeat_seconds: float = 2.0) -> None:
        self.ttl_seconds = ttl_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._local = threading.local()
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingApproval] = {}
        self._heartbeat = 0.0

    @contextlib.contextmanager
    def session(self, session_id: str):
        previous = getattr(self._local, "session_id", None)
        self._local.session_id = session_id
        try:
            yield
        finally:
            self._local.session_id = previous

    def confirm(self, request: PermissionRequest) -> bool:
        session_id = getattr(self._local, "session_id", "")
        if not session_id:
            return False
        now = time.monotonic()
        pending = _PendingApproval(
            id=secrets.token_urlsafe(24),
            session_id=session_id,
            request=request,
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._expire_locked(now)
            self._pending[pending.id] = pending
        while not pending.event.wait(min(0.25, max(0.0, pending.expires_at - time.monotonic()))):
            now = time.monotonic()
            with self._lock:
                connected = self._heartbeat and now - self._heartbeat <= self.heartbeat_seconds
                # Give a freshly created request one heartbeat window to appear in the UI.
                if now - pending.created_at > self.heartbeat_seconds and not connected:
                    pending.resolved = True
                    pending.decision = False
                    pending.event.set()
            if now >= pending.expires_at:
                break
        with self._lock:
            current = self._pending.pop(pending.id, None)
            if current is None or not current.resolved or time.monotonic() > current.expires_at:
                return False
            return current.decision

    def list_pending(self) -> list[dict]:
        with self._lock:
            self._expire_locked(time.monotonic())
            return [item.public() for item in self._pending.values() if not item.resolved]

    def heartbeat(self) -> None:
        with self._lock:
            self._heartbeat = time.monotonic()

    def resolve(self, payload: dict) -> bool:
        if not isinstance(payload, dict) or "id" not in payload:
            return False
        with self._lock:
            now = time.monotonic()
            self._expire_locked(now)
            item = self._pending.get(str(payload["id"]))
            if item is None or item.resolved or now > item.expires_at:
                return False
            required = {"id", "session_id", "tool", "argument_hash", "decision"}
            valid = required <= payload.keys() and payload.get("decision") in {"approve", "deny"}
            valid = valid and secrets.compare_digest(item.session_id, str(payload.get("session_id", "")))
            valid = valid and secrets.compare_digest(item.request.tool, str(payload.get("tool", "")))
            valid = valid and secrets.compare_digest(
                item.request.argument_hash, str(payload.get("argument_hash", ""))
            )
            if not valid:
                item.resolved = True
                item.decision = False
                item.event.set()
                return False
            item.decision = payload["decision"] == "approve"
            item.resolved = True
            item.event.set()
            return True

    def deny_session(self, session_id: str) -> None:
        with self._lock:
            for item in self._pending.values():
                if item.session_id == session_id and not item.resolved:
                    item.decision = False
                    item.resolved = True
                    item.event.set()

    def _expire_locked(self, now: float) -> None:
        for key, item in list(self._pending.items()):
            if now > item.expires_at and not item.resolved:
                item.event.set()
                self._pending.pop(key, None)


dashboard_approvals = DashboardApprovalService()
