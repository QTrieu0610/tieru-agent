"""Same-origin session and CSRF protection for the loopback dashboard."""

from __future__ import annotations

import secrets
import threading
import time
from http.cookies import SimpleCookie
from urllib.parse import urlparse

COOKIE = "tieru_dashboard_session"


class DashboardSecurity:
    def __init__(self, ttl_seconds: int = 8 * 60 * 60) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[str, float]] = {}

    def issue(self, cookie_header: str = "") -> tuple[str, str, bool]:
        sid = self._cookie(cookie_header)
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            current = self._sessions.get(sid)
            if current is not None:
                return sid, current[0], False
            sid = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            self._sessions[sid] = (csrf, now + self.ttl_seconds)
            return sid, csrf, True

    def verify(self, headers) -> bool:
        sid = self._cookie(headers.get("Cookie", ""))
        csrf = headers.get("X-Tieru-CSRF", "")
        if not sid or not csrf or not self._same_origin(headers):
            return False
        with self._lock:
            self._expire(time.monotonic())
            current = self._sessions.get(sid)
            return current is not None and secrets.compare_digest(current[0], csrf)

    @staticmethod
    def _cookie(header: str) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(header)
        except Exception:
            return ""
        morsel = cookie.get(COOKIE)
        return morsel.value if morsel else ""

    @staticmethod
    def _same_origin(headers) -> bool:
        host = headers.get("Host", "")
        origin = headers.get("Origin", "")
        parsed = urlparse(origin)
        if parsed.scheme != "http" or parsed.path not in {"", "/"}:
            return False
        hostname = (parsed.hostname or "").lower()
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            return False
        return parsed.netloc.lower() == host.lower()

    def _expire(self, now: float) -> None:
        for sid, (_, expires) in list(self._sessions.items()):
            if expires < now:
                self._sessions.pop(sid, None)


dashboard_security = DashboardSecurity()
