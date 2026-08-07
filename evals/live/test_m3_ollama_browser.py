"""Opt-in live M3 smoke: local Gemma calls and consumes a restricted browser tool."""

from __future__ import annotations

import contextlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tieru.app import Tieru
from tieru.config import load_settings
from tieru.providers.ollama import OllamaError, OllamaIntegration
from tieru.tools.browser import playwright_available


class _Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Tieru fixture fact: the verification code is COBALT-731."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@contextlib.contextmanager
def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_local_gemma_uses_restricted_browser_result(tmp_path):
    settings = load_settings(
        {
            "profile": "ollama-gemma4-e2b",
            "home": tmp_path,
            "browser_enabled": True,
            "browser_allowed_domains": ["127.0.0.1"],
            "browser_allow_local_fixture": True,
            "max_iterations": 5,
        }
    )
    try:
        doctor = OllamaIntegration(settings.role("main").base_url or "").doctor("gemma4:e2b")
    except OllamaError as exc:
        pytest.skip(f"local Ollama unavailable: {exc}")
    if not doctor["model_present"]:
        pytest.skip("local Ollama does not have gemma4:e2b")
    if not playwright_available():
        pytest.skip("Playwright Python package is not installed")

    with _server() as port:
        app = Tieru(settings=settings, approval_handler=lambda request: True)
        try:
            result = app.respond(
                f"Use browser_open on http://127.0.0.1:{port}, then browser_read. "
                "Reply with the exact verification code from the page."
            )
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium browser is not installed")
            raise
        finally:
            app.close()
    assert [call["tool"] for call in result.tool_calls][:2] == ["browser_open", "browser_read"]
    assert "COBALT-731" in result.reply
