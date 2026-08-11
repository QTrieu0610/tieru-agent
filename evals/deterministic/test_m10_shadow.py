"""M10 deterministic acceptance coverage for passive Tieru Shadow."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from evals.helpers import ScriptedClient, make_waku, response, text_block
from tieru.__main__ import _parser
from tieru.config import Settings
from tieru.db import connect
from tieru.forge import ForgeService
from tieru.forge.extractor import WorkflowExtractor
from tieru.replay import ReplayService
from tieru.shadow import ShadowLifecycleError, ShadowService


def setup(tmp_path, *, enabled=True, minimum=3, evidence=5):
    home = tmp_path / "home"
    settings = Settings(
        home=home, trust_policy={"default": "allow"}, shadow_enabled=enabled,
        shadow_min_occurrences=minimum, shadow_max_evidence_runs=evidence,
    )
    settings.ensure_home()
    conn = connect(home)
    replay = ReplayService(conn, settings)
    return settings, conn, replay, ShadowService(conn, settings)


def replay_run(
    replay, *, tool="verify_repo", path="C:/work/repo", status="completed",
    denied=False, failed=False, secret="", complete_lifecycle=True,
):
    item = replay.start_run(
        session_id="session", source="test", role="main", model="model", provider="local",
        user_input="private conversation body that Shadow must never copy",
    )
    replay.record_event(item.run_id, "tool_requested", {
        "tool": tool,
        "args": {"cwd": path, "authorization": secret, "fixed_mode": "bounded"},
    })
    replay.record_event(item.run_id, "trust_decision", {
        "tool": tool, "capability": ["local_read", "process_execution"],
        "operation": f"inspect_{tool}", "allowed": not denied,
    })
    terminal = "tool_denied" if denied else "tool_failed" if failed else "tool_completed"
    replay.record_event(item.run_id, terminal, {
        "tool": tool, "output": "private output and Authorization: Bearer hidden-value",
    })
    if status == "completed" and complete_lifecycle:
        replay.complete_run(
            item.run_id, output="private final response", iterations=1, latency_ms=2,
            role="main", model="model", provider="local",
        )
    elif status == "failed":
        replay.fail_run(
            item.run_id, error_code="failed", error_summary="failure", latency_ms=2,
            role="main", model="model", provider="local",
        )
    return item.run_id


def observe_many(shadow, replay, count, **run_options):
    run_ids = []
    for index in range(count):
        run_id = replay_run(replay, path=f"C:/repo/{index}", **run_options)
        run_ids.append(run_id)
        shadow.observe(run_id)
    return run_ids


def ready_suggestion(shadow, replay):
    observe_many(shadow, replay, shadow.settings.shadow_min_occurrences)
    return shadow.store.list_suggestions()[0]


def test_completed_run_is_accepted_and_first_occurrence_observes(tmp_path):
    _settings, conn, replay, shadow = setup(tmp_path)
    run_id = replay_run(replay)
    result = shadow.observe(run_id)
    assert result["status"] == "observing"
    pattern = shadow.store.list_patterns()[0]
    assert pattern.occurrence_count == pattern.successful_count == 1
    assert pattern.source_run_ids == [run_id]
    assert shadow.suggestions() == []
    conn.close()


@pytest.mark.parametrize("status", ["failed", "running"])
def test_failed_and_running_runs_are_ignored(tmp_path, status):
    _settings, conn, replay, shadow = setup(tmp_path)
    result = shadow.observe(replay_run(replay, status=status))
    assert result["status"] == "ineligible"
    assert shadow.patterns() == []
    conn.close()


def test_corrupt_and_denied_or_failed_tool_workflows_are_non_suggestible(tmp_path):
    _settings, conn, replay, shadow = setup(tmp_path)
    corrupt = replay_run(replay, complete_lifecycle=False)
    replay.store.finish(
        corrupt, status="completed", iterations=0, latency_ms=0,
        role="main", model="model", provider="local",
    )
    assert shadow.observe(corrupt)["status"] == "ineligible"
    assert shadow.observe(replay_run(replay, denied=True))["status"] == "ineligible"
    assert shadow.observe(replay_run(replay, failed=True))["status"] == "ineligible"
    assert shadow.patterns() == []
    conn.close()


def test_m9_signature_is_reused_and_benign_parameters_group(tmp_path):
    _settings, conn, replay, shadow = setup(tmp_path)
    first = replay_run(replay, path="C:/one", secret="Bearer sk-abcdefghijklmnop")
    second = replay_run(replay, path="D:/two", secret="Bearer another-secret-value")
    expected = WorkflowExtractor(replay).extract([first]).workflow_signature
    shadow.observe(first)
    shadow.observe(second)
    pattern = shadow.store.list_patterns()[0]
    assert pattern.workflow_signature == expected
    assert pattern.occurrence_count == 2
    stored = json.dumps(pattern.public())
    assert "abcdefghijklmnop" not in stored and "another-secret" not in stored
    conn.close()


def test_materially_different_workflows_remain_separate(tmp_path):
    _settings, conn, replay, shadow = setup(tmp_path)
    shadow.observe(replay_run(replay, tool="verify_repo"))
    shadow.observe(replay_run(replay, tool="send_message"))
    patterns = shadow.store.list_patterns()
    assert len(patterns) == 2
    assert len({item.workflow_signature for item in patterns}) == 2
    conn.close()


def test_aggregation_threshold_timestamps_and_bounded_evidence(tmp_path):
    _settings, conn, replay, shadow = setup(tmp_path, evidence=2)
    ids = observe_many(shadow, replay, 3)
    pattern = shadow.store.list_patterns()[0]
    assert pattern.occurrence_count == 3
    assert pattern.source_run_ids == ids[-2:]
    assert pattern.first_seen_at <= pattern.last_seen_at
    suggestion = shadow.store.list_suggestions()[0]
    assert suggestion.status == "ready" and suggestion.occurrence_count == 3
    assert pattern.status == "suggestion_ready"
    shadow.observe(replay_run(replay))
    assert len(shadow.store.list_suggestions()) == 1
    conn.close()


def test_configurable_minimum_and_no_suggestion_below_it(tmp_path):
    _settings, conn, replay, shadow = setup(tmp_path, minimum=4)
    observe_many(shadow, replay, 3)
    assert shadow.suggestions() == []
    shadow.observe(replay_run(replay))
    assert len(shadow.suggestions()) == 1
    conn.close()


def test_confidence_is_deterministic_and_monotonic_without_model(tmp_path):
    _settings, conn, replay, shadow = setup(tmp_path)
    levels = {"low": 0, "medium": 1, "high": 2}
    observed = []
    for _ in range(5):
        shadow.observe(replay_run(replay, tool="verify_status"))
        observed.append(shadow.store.list_patterns()[0].confidence)
    assert observed[:2] == ["low", "low"]
    assert observed[2] == "medium" and observed[-1] == "high"
    assert [levels[item] for item in observed] == sorted(levels[item] for item in observed)
    assert not hasattr(shadow, "client")
    conn.close()


def test_ignore_suppresses_until_occurrences_materially_increase(tmp_path):
    _settings, conn, replay, shadow = setup(tmp_path)
    suggestion = ready_suggestion(shadow, replay)
    shadow.ignore(suggestion.suggestion_id)
    shadow.observe(replay_run(replay))
    assert shadow.store.get_pattern(suggestion.pattern_id).status == "ignored"
    assert shadow.suggestions() == []
    shadow.observe(replay_run(replay))
    shadow.observe(replay_run(replay))
    assert shadow.store.get_pattern(suggestion.pattern_id).status == "suggestion_ready"
    assert len(shadow.suggestions()) == 1
    conn.close()


def test_snooze_and_dismiss_suppress_deterministically(tmp_path):
    _settings, conn, replay, shadow = setup(tmp_path)
    suggestion = ready_suggestion(shadow, replay)
    snoozed = shadow.snooze(suggestion.suggestion_id, days=3)
    shadow.observe(replay_run(replay))
    assert snoozed.status == "snoozed"
    assert shadow.store.get_pattern(suggestion.pattern_id).status == "snoozed"
    shadow.dismiss(suggestion.suggestion_id)
    observe_many(shadow, replay, 4)
    assert shadow.store.get_pattern(suggestion.pattern_id).status == "dismissed"
    assert shadow.suggestions() == []
    conn.close()


def _write_skill(home: Path, name: str, signature: str = ""):
    path = home / "skills" / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: unrelated reviewed procedure\n---\n\nUse it.\n",
        encoding="utf-8",
    )
    if signature:
        (path / "forge-metadata.json").write_text(
            json.dumps({"workflow_signature": signature}), encoding="utf-8"
        )


def test_equivalent_installed_skill_suppresses_but_unrelated_does_not(tmp_path):
    settings, conn, replay, shadow = setup(tmp_path)
    source = replay_run(replay)
    signature = WorkflowExtractor(replay).extract([source]).workflow_signature
    _write_skill(settings.home, "covered-workflow", signature)
    shadow.observe(source)
    observe_many(shadow, replay, 2)
    pattern = shadow.store.list_patterns()[0]
    assert pattern.status == "covered" and shadow.suggestions() == []
    conn.close()

    settings, conn, replay, shadow = setup(tmp_path / "other")
    _write_skill(settings.home, "unrelated", "different-signature")
    observe_many(shadow, replay, 3)
    assert len(shadow.suggestions()) == 1
    conn.close()


def test_existing_compatible_forge_draft_prevents_duplicate_suggestion(tmp_path):
    settings, conn, replay, shadow = setup(tmp_path)
    source = replay_run(replay)
    forge = ForgeService(conn, settings, approval_handler=lambda _request: True)
    draft = forge.forge([source])
    shadow.observe(source)
    observe_many(shadow, replay, 2)
    pattern = shadow.store.list_patterns()[0]
    assert pattern.status == "forged" and pattern.forge_draft_id == draft.draft_id
    assert shadow.suggestions() == []
    conn.close()


def test_explicit_forge_handoff_uses_m9_and_creates_inactive_draft_only(tmp_path):
    settings, conn, replay, shadow = setup(tmp_path)
    suggestion = ready_suggestion(shadow, replay)
    forge = ForgeService(conn, settings, approval_handler=lambda _request: True)
    draft = shadow.forge(suggestion.suggestion_id, forge)
    assert draft.metadata["status"] == "draft"
    assert draft.workflow.source_run_ids == suggestion.representative_run_ids[-3:]
    assert not (settings.home / "skills" / draft.metadata["skill_id"]).exists()
    assert shadow.store.get_suggestion(suggestion.suggestion_id).status == "forged"
    with pytest.raises(ShadowLifecycleError):
        shadow.forge(suggestion.suggestion_id, forge)
    conn.close()


def test_repetition_never_changes_trust_or_grants_capabilities(tmp_path):
    settings, conn, replay, shadow = setup(tmp_path)
    before = copy.deepcopy(settings.trust_policy)
    observe_many(shadow, replay, 5, tool="deploy_service")
    suggestion = shadow.store.list_suggestions()[0]
    assert settings.trust_policy == before
    assert suggestion.required_capabilities == ["local_read", "process_execution"]
    assert "granted" not in json.dumps(suggestion.public()).lower()
    conn.close()


def test_privacy_tables_store_structure_not_private_content(tmp_path):
    _settings, conn, replay, shadow = setup(tmp_path)
    observe_many(
        shadow, replay, 3,
        secret="Authorization: Bearer sk-abcdefghijklmnopqrstuv",
    )
    rows = []
    for table in ("shadow_patterns", "shadow_suggestions", "shadow_observations"):
        rows.extend(dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall())
    stored = json.dumps(rows)
    for forbidden in ("private conversation", "private output", "abcdefghijklmnopqrstuv", "Authorization"):
        assert forbidden not in stored
    conn.close()


def test_disabled_shadow_does_not_aggregate_and_preserves_existing_metadata(tmp_path):
    _settings, conn, replay, shadow = setup(tmp_path)
    shadow.observe(replay_run(replay))
    assert len(shadow.patterns()) == 1
    shadow.set_enabled(False)
    assert shadow.observe(replay_run(replay))["status"] == "disabled"
    assert len(shadow.patterns()) == 1
    shadow.set_enabled(True)
    assert shadow.status()["enabled"] is True
    conn.close()


def test_default_is_disabled_for_existing_users(tmp_path):
    settings, conn, replay, shadow = setup(tmp_path, enabled=False)
    assert settings.shadow_enabled is False and shadow.enabled is False
    assert shadow.observe(replay_run(replay))["status"] == "disabled"
    assert shadow.patterns() == []
    conn.close()


def test_shadow_failure_does_not_block_response_or_corrupt_replay(tmp_path):
    app = make_waku(
        tmp_path / "home", client=ScriptedClient([
            response([text_block('{"retrieve": false, "query": "", "reason": "self-contained"}')]),
            response([text_block("answer")]),
        ]),
        shadow_enabled=True,
    )

    class BrokenShadow:
        observer = None

        def observe(self, _run_id):
            raise OSError("shadow database unavailable")

    policy = copy.deepcopy(app.settings.trust_policy)
    app.shadow = BrokenShadow()
    result = app.respond("hello", source="test")
    assert result.reply == "answer"
    assert app.replay.get_run(result.run_id)["status"] == "completed"
    assert app.settings.trust_policy == policy
    assert not (app.settings.home / "skills").exists()


def test_schema_migration_is_additive_idempotent_and_separate_from_memory(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    first, second = connect(home), connect(home)
    tables = {row[0] for row in second.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert {"shadow_patterns", "shadow_suggestions", "shadow_observations", "shadow_settings"} <= tables
    first.close()
    second.close()


def test_cli_dashboard_and_safe_events_are_exposed(tmp_path):
    args = _parser().parse_args(["shadow", "snooze", "suggest_123", "--days", "2"])
    assert args.shadow_command == "snooze" and args.days == 2
    html = Path("tieru/ops/static/index.html").read_text(encoding="utf-8")
    views = Path("tieru/ops/static/js/views.js").read_text(encoding="utf-8")
    dashboard = Path("tieru/ops/dashboard.py").read_text(encoding="utf-8")
    assert 'href="#shadow"' in html and "Why am I seeing this?" in views
    for action in ("Forge Skill", "Ignore", "Snooze", "Dismiss", "Enable Shadow"):
        assert action in views
    assert '"/api/shadow"' in dashboard and '"/api/shadow/"' in dashboard

    _settings, conn, replay, shadow = setup(tmp_path)
    events = []
    shadow.observer = lambda kind, payload: events.append((kind, payload))
    observe_many(shadow, replay, 3)
    assert {kind for kind, _payload in events} >= {
        "shadow_run_observed", "shadow_pattern_updated", "shadow_threshold_reached",
        "shadow_suggestion_created",
    }
    assert "private" not in json.dumps(events)
    conn.close()
