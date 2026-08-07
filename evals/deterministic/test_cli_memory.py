"""DETERMINISTIC EVAL - the CLI /memory command reads local SQLite state."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from tieru.db import connect
from tieru.gateway import cli


def test_memory_snapshot_reads_seeded_home(tmp_path):
    conn = connect(tmp_path)
    conn.execute(
        "INSERT INTO facts (subject, content, source) VALUES (?, ?, ?)",
        ("project", "Waku stays local-first", "user"),
    )
    conn.execute(
        "INSERT INTO facts (subject, content, source) VALUES (?, ?, ?)",
        ("alex", "Alex prefers morning meetings", "consolidation"),
    )
    conn.execute(
        "INSERT INTO episodes (happened_at, summary) VALUES (?, ?)",
        ("2026-07-16", "Planned the launch"),
    )
    conn.execute(
        "INSERT INTO episodes (happened_at, summary) VALUES (?, ?)",
        ("2026-07-17", "Reviewed the launch checklist"),
    )
    conn.execute("INSERT INTO chat_log (role, content, consolidated) VALUES ('user', 'old', 1)")
    conn.execute(
        "INSERT INTO chat_log (role, content, consolidated) VALUES ('user', 'new question', 0)"
    )
    conn.execute(
        "INSERT INTO chat_log (role, content, consolidated) VALUES ('assistant', 'new answer', 0)"
    )
    conn.commit()

    snapshot = cli._memory_snapshot(conn)

    assert "Semantic facts (2)" in snapshot
    assert "[alex] Alex prefers morning meetings" in snapshot
    assert "[project] Waku stays local-first" in snapshot
    assert "Recent episodes (2)" in snapshot
    assert "2026-07-17 - Reviewed the launch checklist" in snapshot
    assert "2026-07-16 - Planned the launch" in snapshot
    assert "Unconsolidated chat messages: 2" in snapshot


def test_memory_snapshot_handles_empty_home(tmp_path):
    snapshot = cli._memory_snapshot(connect(tmp_path))

    assert "Semantic facts (0)\n- none yet" in snapshot
    assert "Recent episodes (0)\n- none yet" in snapshot
    assert "Unconsolidated chat messages: 0" in snapshot


def test_cli_displays_tieru_brand(monkeypatch, tmp_path):
    output = StringIO()
    test_console = Console(file=output, force_terminal=False, color_system=None)
    inputs = iter(["hello", "/quit"])
    monkeypatch.setattr(test_console, "input", lambda _prompt: next(inputs))

    fake = SimpleNamespace(
        settings=SimpleNamespace(home=tmp_path, model="test-model"),
        session=SimpleNamespace(session_id=""),
        respond=lambda *_args, **_kwargs: SimpleNamespace(reply="hello back"),
    )
    monkeypatch.setattr(cli, "console", test_console)
    monkeypatch.setattr(cli, "Tieru", lambda **_kwargs: fake)

    cli.main()

    rendered = output.getvalue()
    assert "Tieru — local, yours, transparent." in rendered
    assert "Tieru › hello back" in rendered
    assert "Waku — local, yours, transparent." not in rendered
