"""Deterministic M9 code, Git, GitHub, permission, and Replay contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals.helpers import ScriptedClient, make_waku, response, text_block, tool_block
from tieru.tools.coding import GitHubReader, RepositoryTools, make_tools
from tieru.tools.computer import LocalComputer
from tieru.tools.registry import ToolRegistry


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout


def init_repo(root: Path) -> None:
    git(root, "init", "-q")
    git(root, "config", "user.email", "tieru-tests@example.invalid")
    git(root, "config", "user.name", "Tieru Tests")
    (root / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(root, "add", "sample.py")
    git(root, "commit", "-q", "-m", "initial fixture")


def registry_for(root: Path, *, approve=True):
    computer = LocalComputer(root)
    repository = RepositoryTools(computer)
    github = GitHubReader(root)
    registry = ToolRegistry(
        approval_handler=lambda _request: approve,
        trust_context={"base_path": str(root), "path_aliases": {"workspace": str(root)}},
    )
    for tool in make_tools(repository, github):
        registry.register(tool)
    return repository, github, registry


def execute(registry: ToolRegistry, name: str, args: dict) -> dict:
    return json.loads(registry.execute(name, args))


def test_git_status_diff_and_log(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
    repository, _github, _registry = registry_for(tmp_path)

    status = json.loads(repository.git_status())
    diff = json.loads(repository.git_diff())
    log = json.loads(repository.git_log(limit=1))

    assert "sample.py" in status["content"]
    assert "-VALUE = 1" in diff["content"] and "+VALUE = 2" in diff["content"]
    assert log["commits"][0]["subject"] == "initial fixture"


def test_git_diff_includes_untracked_text_file(tmp_path):
    init_repo(tmp_path)
    (tmp_path / "new.py").write_text("NEW = True\n", encoding="utf-8")
    repository, _github, _registry = registry_for(tmp_path)

    diff = json.loads(repository.git_diff(path="new.py"))

    assert "--- /dev/null" in diff["content"]
    assert "+++ b/new.py" in diff["content"]
    assert "+NEW = True" in diff["content"]


def test_code_search_and_read(tmp_path):
    (tmp_path / "module.py").write_text(
        "def answer():\n    return 42\n", encoding="utf-8"
    )
    repository, _github, _registry = registry_for(tmp_path)

    searched = json.loads(repository.code_search("return 42"))
    read = json.loads(repository.code_read("module.py", start_line=1, end_line=2))

    assert searched["results"] == [
        {"path": "module.py", "line": 2, "text": "    return 42"}
    ]
    assert searched["next_action"] == {"tool": "code_read", "path": "module.py"}
    assert "     1  def answer():" in read["content"]
    assert "     2      return 42" in read["content"]


def test_code_patch_success_and_context_mismatch_safe_failure(tmp_path):
    target = tmp_path / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    _repository, _github, registry = registry_for(tmp_path)

    patched = execute(
        registry,
        "code_patch",
        {"path": "module.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
    )
    mismatch = execute(
        registry,
        "code_patch",
        {"path": "module.py", "old_text": "VALUE = 9", "new_text": "VALUE = 3"},
    )

    assert patched["changed"] is True and patched["replacements"] == 1
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert mismatch["error"]["code"] == "tool_execution_error"
    assert "context did not match" in mismatch["error"]["message"]
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"


def test_code_patch_outside_workspace_is_blocked(tmp_path):
    outside = tmp_path.parent / "outside-m9.py"
    outside.write_text("SAFE = True\n", encoding="utf-8")
    _repository, _github, registry = registry_for(tmp_path)

    result = execute(
        registry,
        "code_patch",
        {"path": "../outside-m9.py", "old_text": "True", "new_text": "False"},
    )

    assert result["error"]["code"] == "tool_execution_error"
    assert "outside the workspace root" in result["error"]["message"]
    assert outside.read_text(encoding="utf-8") == "SAFE = True\n"


def test_code_patch_symlink_escape_is_blocked(tmp_path):
    outside = tmp_path.parent / "outside-m9-link.py"
    outside.write_text("SAFE = True\n", encoding="utf-8")
    link = tmp_path / "link.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable in this environment")
    _repository, _github, registry = registry_for(tmp_path)

    result = execute(
        registry,
        "code_patch",
        {"path": "link.py", "old_text": "True", "new_text": "False"},
    )

    assert result["error"]["code"] == "tool_execution_error"
    assert outside.read_text(encoding="utf-8") == "SAFE = True\n"


def test_code_patch_requires_permission(tmp_path):
    target = tmp_path / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    _repository, _github, registry = registry_for(tmp_path, approve=False)

    result = execute(
        registry,
        "code_patch",
        {"path": "module.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
    )

    assert result["error"]["code"] == "tool_permission_denied"
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_github_repo_issue_pr_and_ci_are_fixed_read_only_calls(monkeypatch, tmp_path):
    reader = GitHubReader(tmp_path)
    calls = []

    def mocked(args):
        calls.append(args)
        if args[:2] == ["repo", "view"]:
            return {"nameWithOwner": "owner/repo", "url": "https://github.com/owner/repo"}
        if args[:2] == ["issue", "view"]:
            return {"number": 7, "title": "Issue", "url": "https://github.com/owner/repo/issues/7"}
        if args[:2] == ["pr", "view"]:
            return {"number": 8, "title": "PR", "url": "https://github.com/owner/repo/pull/8"}
        return [{"status": "completed", "url": "https://github.com/owner/repo/actions/runs/9"}]

    monkeypatch.setattr(reader, "_run", mocked)

    repo = json.loads(reader.repo("owner/repo"))
    issue = json.loads(reader.issue(7, "owner/repo"))
    pr = json.loads(reader.pr(8, "owner/repo"))
    ci = json.loads(reader.ci("owner/repo", 3))

    assert repo["url"].endswith("/owner/repo")
    assert issue["data"]["number"] == 7
    assert pr["data"]["number"] == 8
    assert ci["data"][0]["status"] == "completed"
    assert [call[0] for call in calls] == ["repo", "issue", "pr", "run"]
    assert not any(word in json.dumps(calls) for word in ("merge", "comment", "close"))


def test_github_secret_output_is_redacted(monkeypatch, tmp_path):
    _repository, github, registry = registry_for(tmp_path)
    monkeypatch.setattr(
        github,
        "_run",
        lambda _args: {
            "number": 7,
            "body": "api_key=sk-super-secret-value",
            "url": "https://github.com/owner/repo/issues/7",
        },
    )

    output = registry.execute("github_issue", {"number": 7, "repo": "owner/repo"})

    assert "sk-super-secret-value" not in output
    assert "[REDACTED SECRET]" in output


def test_patch_then_test_then_diff_and_replay_edit(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    init_repo(tmp_path)
    (tmp_path / "test_target.py").write_text(
        "from sample import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    git(tmp_path, "add", "test_target.py")
    git(tmp_path, "commit", "-q", "-m", "add test")
    command = f'"{sys.executable}" -m pytest -q test_target.py'
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    client = ScriptedClient(
        [
            gate,
            response([tool_block("code_read", {"path": "sample.py"}, "read")], "tool_use"),
            response(
                [
                    tool_block(
                        "code_patch",
                        {"path": "sample.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"},
                        "patch",
                    )
                ],
                "tool_use",
            ),
            response(
                [tool_block("shell_run", {"command": command}, "test")], "tool_use"
            ),
            response([tool_block("git_diff", {}, "diff")], "tool_use"),
            response([text_block("done")]),
            response(
                [
                    text_block(
                        "Changed sample.py from VALUE = 1 to VALUE = 2. "
                        "Test result: 1 passed. The Git diff confirms only sample.py changed."
                    )
                ]
            ),
        ]
    )
    app = make_waku(tmp_path / "home", client=client)

    result = app.respond("Update VALUE to 2, run its test, review the diff, and summarize.")
    events = app.replay.get_events(result.run_id)
    completed = {
        event["tool"] for event in events if event["event_type"] == "tool_completed"
    }
    outputs = {event["tool"]: json.loads(event["output"]) for event in result.tool_calls}

    assert {"code_read", "code_patch", "shell_run", "git_diff"} <= completed
    assert outputs["shell_run"]["exit_code"] == 0
    assert "1 passed" in outputs["shell_run"]["stdout"]
    assert "+VALUE = 2" in outputs["git_diff"]["content"]
    assert "sample.py" in result.reply and "1 passed" in result.reply
    assert git(tmp_path, "rev-list", "--count", "HEAD").strip() == "2"
    app.close()


def test_single_structured_code_search_candidate_is_read_before_final(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "module.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    gate = response([text_block('{"retrieve": false, "query": "", "reason": "test"}')])
    client = ScriptedClient(
        [
            gate,
            response(
                [tool_block("code_search", {"query": "return 42"}, "search")],
                "tool_use",
            ),
            response([text_block("")]),
            response([text_block("done")]),
            response([text_block("module.py returns the integer 42 from answer().")]),
        ]
    )
    app = make_waku(tmp_path / "home", client=client)

    result = app.respond("Find the answer implementation and explain it.")

    assert [event["tool"] for event in result.tool_calls] == ["code_search", "code_read"]
    assert "42" in result.reply
    app.close()


def test_no_commit_push_merge_or_branch_mutation_capability(tmp_path):
    _repository, _github, registry = registry_for(tmp_path)
    names = set(registry._tools)

    assert {"git_status", "git_diff", "git_log"} <= names
    assert not any(
        forbidden in names
        for forbidden in (
            "git_add", "git_commit", "git_push", "git_merge", "git_reset", "git_checkout"
        )
    )

    computer_registry = ToolRegistry(approval_handler=lambda _request: True)
    from tieru.tools.computer import make_tools as make_computer_tools

    for tool in make_computer_tools(LocalComputer(tmp_path)):
        computer_registry.register(tool)
    for command in ("git commit -m nope", "git push", "git merge main", "git checkout other"):
        result = execute(computer_registry, "shell_run", {"command": command})
        assert result["error"]["code"] == "tool_execution_error"
        assert "Git mutation is unavailable" in result["error"]["message"]
