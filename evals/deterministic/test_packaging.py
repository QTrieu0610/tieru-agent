"""DETERMINISTIC EVAL — what actually ships, and what a test is allowed to read.

Two failures found on 2026-07-31, both invisible to every other test because
every other test runs from a git checkout on the maintainer's machine.

1. THE WHEEL SHIPPED NO SKILLS. `pyproject.toml` packages `waku`, but the
   bundled skills live in `skills/` at the REPO ROOT, so `pip install
   waku-agent` produced a Waku with zero procedural memory — one of the four
   pillars, silently absent. The dashboard shipped fine (it lives under
   `waku/ops/static`), which is exactly why nobody noticed: the thing you can
   see worked. Nothing here can catch a broken wheel by inspecting a checkout,
   so these tests pin the two halves of the fix instead — the build config that
   copies the folder in, and the lookup that finds it once it's there.

2. THE TEST SUITE READ THE DEVELOPER'S `.env`. `waku/config.py` calls
   load_dotenv() at import, so a stale `WAKU_GRAPH_WORKFLOWS=1` routed every
   scripted turn through the triage graph, spent one extra model call, and
   failed 8 tests that were green in CI. A suite whose result depends on an
   untracked file is not deterministic — it is a suite that lies in whichever
   direction the local machine happens to point.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tieru.memory import bundled_skill_dirs
from tieru.memory.procedural.loader import SkillLoader

REPO = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    with (REPO / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_the_bundled_skills_are_findable():
    """Whatever the install shape, Waku must find the skills it ships with."""
    dirs = bundled_skill_dirs()
    assert dirs, (
        "bundled_skill_dirs() found nothing — Waku would start with no "
        "procedural memory and never say so"
    )
    for d in dirs:
        assert d.is_dir()
    names = {s.name for s in SkillLoader(dirs).skills}
    assert {"schedule-meeting", "weekly-brief"} <= names, sorted(names)


def test_the_wheel_carries_the_skills_folder():
    """The build config half of the fix. `packages = ["waku"]` alone leaves the
    repo-root skills/ out of the wheel, which is the bug — force-include copies
    it to waku/skills so an installed Waku finds it beside the code."""
    wheel = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel.get("force-include", {}).get("skills") == "tieru/skills", (
        "pyproject no longer force-includes skills/ into the wheel — a "
        "`pip install waku-agent` would ship with zero skills again"
    )


def test_the_wheel_carries_dashboard_and_configuration_example():
    wheel = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel.get("force-include", {}).get("tieru/tieru.example.yaml") == (
        "tieru/tieru.example.yaml"
    )
    static = REPO / "tieru" / "ops" / "static"
    assert (static / "index.html").is_file() and (static / "js" / "main.js").is_file()


def test_release_gate_pins_every_shipped_subsystem_and_runtime_asset():
    from tieru.ops.release_gate import SHIPPED_SDIST_FILES, SHIPPED_WHEEL_FILES

    subsystem_files = {
        "tieru/trust/kernel.py",
        "tieru/replay/service.py",
        "tieru/forge/service.py",
        "tieru/shadow/service.py",
        "tieru/fabric/service.py",
        "tieru/capsule/service.py",
        "tieru/memory/graph/service.py",
    }
    assert subsystem_files <= SHIPPED_WHEEL_FILES
    assert subsystem_files <= SHIPPED_SDIST_FILES
    assert {
        "tieru/tieru.example.yaml",
        "tieru/ops/static/index.html",
        "tieru/ops/static/js/main.js",
        "tieru/ops/doctor.py",
        "tieru/ops/init.py",
    } <= SHIPPED_WHEEL_FILES
    assert "tieru/ops/init.py" in SHIPPED_SDIST_FILES
    assert any(path.endswith("/SKILL.md") for path in SHIPPED_WHEEL_FILES)


def test_release_gate_forbids_runtime_and_temporary_artifacts():
    from tieru.ops.release_gate import _forbidden_artifact_path

    for path in (
        "tieru_agent/.tieru/state.db",
        "tieru_agent/traces/.tieru/run.tieru",
        "tieru_agent/tieru/__pycache__/app.pyc",
        "tieru_agent/capsule.tmp",
        "tieru_agent/google-token.json",
        "tieru_agent/traces/run.jsonl",
        "tieru_agent/.env",
    ):
        assert _forbidden_artifact_path(path), path
    assert not _forbidden_artifact_path("tieru_agent/tieru/ops/tracing.py")


def test_personal_agent_template_is_excluded_from_source_distribution():
    sdist = _pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "template_Agent.md" in sdist.get("exclude", [])


def test_public_examples_ship_in_sdist_but_not_runtime_wheel():
    targets = _pyproject()["tool"]["hatch"]["build"]["targets"]
    sdist = targets["sdist"]
    wheel = targets["wheel"]
    assert "examples" not in sdist.get("exclude", [])
    assert sdist.get("force-include", {}).get("examples") == "examples"
    assert "examples" not in wheel.get("force-include", {})

    from tieru.ops.release_gate import SHIPPED_SDIST_FILES, SHIPPED_WHEEL_FILES

    expected = {
        "examples/README.md",
        "examples/local-only/config.yaml",
        "examples/local-multi-model/config.yaml",
        "examples/local-plus-cloud/config.yaml",
        "examples/privacy-first/config.yaml",
        "examples/repository-agent/config.yaml",
    }
    assert expected <= SHIPPED_SDIST_FILES
    assert not any(path.startswith("examples/") for path in SHIPPED_WHEEL_FILES)


def test_lookup_covers_both_install_shapes():
    """The lookup half. A checkout has skills/ at the repo root; a wheel has it
    at waku/skills. Exactly one exists at a time, so the function must look in
    both — checking only one is how this broke."""
    import inspect

    from tieru import memory

    src = inspect.getsource(memory.bundled_skill_dirs)
    assert 'parents[1] / "skills"' in src, "lost the installed-wheel location"
    assert 'parents[2] / "skills"' in src, "lost the repo-checkout location"


@pytest.mark.parametrize("field", ["urls", "keywords", "classifiers"])
def test_pypi_metadata_is_present(field):
    """A bare PyPI page is a bad first impression for a teaching repo, and the
    project URLs are the only navigation a pip user gets back to the source."""
    assert _pyproject()["project"].get(field), f"pyproject is missing {field}"


def test_evals_never_inherit_the_developers_env():
    """THE regression for #2. `make_waku` must pin every switch that changes
    what a turn DOES, so the suite describes its own world instead of the
    maintainer's .env. Deliberately NOT pinned: `experimental`, because
    test_delegate.py drives it via monkeypatch.setenv to prove the env var
    reaches Settings at all."""
    import inspect

    from evals import helpers

    src = inspect.getsource(helpers.make_waku)
    for switch in ("apple_calendar", "google_calendar", "apple_tools", "graph_workflows"):
        assert switch in src, (
            f"make_waku no longer pins {switch!r} — a stale value in the "
            "maintainer's .env can now change what these tests measure"
        )


def test_a_scripted_turn_ignores_the_graph_flag(tmp_path, monkeypatch):
    """The behavioural half of the same regression: with the flag set in the
    environment, a scripted turn must still take the plain loop. When this
    broke, the triage graph ate one queued response and the loop reported one
    iteration where the test expected two."""
    import dataclasses

    from evals.helpers import ScriptedClient, make_waku, response, text_block
    from tieru.config import Settings

    if "graph_workflows" not in {f.name for f in dataclasses.fields(Settings)}:
        pytest.skip("graph workflows not built on this branch")

    monkeypatch.setenv("WAKU_GRAPH_WORKFLOWS", "1")
    # Exactly two responses: the retrieval gate, then the answer. That count IS
    # the assertion — a triage graph would spend a third on classification and
    # this client would raise IndexError instead of answering.
    gate = response([text_block('{"retrieve": false, "reason": "greeting"}')])
    app = make_waku(tmp_path / "home",
                    client=ScriptedClient([gate, response([text_block("hi")])]))
    assert app.settings.graph_workflows is False
    assert app.respond("hello").reply == "hi"
