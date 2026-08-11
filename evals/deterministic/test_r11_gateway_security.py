"""R1.1 regressions for fail-closed external gateways and bounded webhooks."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from tieru.gateway.discord import should_answer
from tieru.gateway.telegram import sender_allowed as telegram_sender_allowed
from tieru.gateway.whatsapp import (
    RequestBodyError,
    _build_handler,
    _read_bounded_body,
)
from tieru.gateway.whatsapp import sender_allowed as whatsapp_sender_allowed


class RecordingStream:
    def __init__(self, payload: bytes = b""):
        self.payload = payload
        self.reads = []

    def read(self, size: int) -> bytes:
        self.reads.append(size)
        return self.payload[:size]


def test_empty_gateway_allowlists_deny_every_sender():
    assert not telegram_sender_allowed(set(), "123")
    assert not whatsapp_sender_allowed("", "15551234567")
    assert not should_answer(
        is_dm=True,
        author_id="123",
        channel_id="dm",
        mentioned=False,
        allowed_users=set(),
        allowed_channels=set(),
        require_mention=True,
    )


def test_explicit_gateway_identities_accept_only_the_matching_sender():
    assert telegram_sender_allowed({"123"}, "123")
    assert not telegram_sender_allowed({"123"}, "999")
    assert whatsapp_sender_allowed("15551234567", "15551234567")
    assert not whatsapp_sender_allowed("15551234567", "15557654321")
    base = {
        "is_dm": True,
        "channel_id": "dm",
        "mentioned": False,
        "allowed_users": {"123"},
        "allowed_channels": set(),
        "require_mention": True,
    }
    assert should_answer(author_id="123", **base)
    assert not should_answer(author_id="999", **base)


def test_whatsapp_body_limit_is_checked_before_reading():
    stream = RecordingStream(b"small")
    assert _read_bounded_body({"Content-Length": "5"}, stream, 10) == b"small"
    assert stream.reads == [5]

    for value, status in (("11", 413), ("not-a-number", 400), ("-1", 400), (None, 411)):
        stream = RecordingStream(b"must not be read")
        headers = {} if value is None else {"Content-Length": value}
        with pytest.raises(RequestBodyError) as exc:
            _read_bounded_body(headers, stream, 10)
        assert exc.value.status == status
        assert stream.reads == []


def _handler(monkeypatch, tmp_path, *, allowed="15551234567"):
    class FakeSettings:
        home = tmp_path

        def ensure_home(self):
            self.home.mkdir(parents=True, exist_ok=True)

    class FakeTieru:
        def __init__(self, *args, **kwargs):
            self.session = SimpleNamespace(session_id="")

        def respond(self, text, **kwargs):
            return SimpleNamespace(reply=f"reply:{text}")

    import tieru.config
    import tieru.db
    from tieru.gateway import whatsapp

    monkeypatch.setattr(tieru.config, "load_settings", lambda: FakeSettings())
    monkeypatch.setattr(tieru.db, "connect", lambda *args, **kwargs: object())
    monkeypatch.setattr(whatsapp, "Tieru", FakeTieru)
    sent = []
    monkeypatch.setattr(
        whatsapp,
        "_send_message",
        lambda token, phone_id, to, text: sent.append((to, text)) or True,
    )
    return _build_handler("access-token", "phone-id", "verify-secret", "app-secret", allowed), sent


def _serve_once(handler, request):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        request(connection)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_small_signed_whatsapp_body_preserves_allowed_sender_path(monkeypatch, tmp_path):
    handler, sent = _handler(monkeypatch, tmp_path)
    body = json.dumps({
        "entry": [{"changes": [{"value": {"messages": [
            {"from": "15551234567", "text": {"body": "hello"}}
        ]}}]}]
    }).encode()
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()

    def request(connection):
        connection.request(
            "POST", "/webhook", body=body,
            headers={"Content-Length": str(len(body)), "X-Hub-Signature-256": signature},
        )
        assert connection.getresponse().status == 200

    _serve_once(handler, request)
    assert sent == [("15551234567", "reply:hello")]


def test_whatsapp_verification_secret_never_appears_in_logs(monkeypatch, tmp_path, capsys):
    handler, _ = _handler(monkeypatch, tmp_path)

    def request(connection):
        connection.request(
            "GET",
            "/webhook?hub.mode=subscribe&hub.challenge=ok&hub.verify_token=leak-me-never",
        )
        assert connection.getresponse().status == 403

    _serve_once(handler, request)
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "leak-me-never" not in output
    assert "verify-secret" not in output
