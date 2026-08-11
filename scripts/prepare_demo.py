"""Prepare isolated, synthetic Tieru public-demo state outside the checkout.

The default action copies text fixtures only. Optional deterministic seeding is
explicit and writes solely below a marker-owned demo root. It never contacts a
model, starts Ollama, grants Trust authority, or reads the user's Tieru home.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

MARKER = ".tieru-public-demo-root"
REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE = REPOSITORY / "examples" / "demo"
HOME_NAMES = ("local-agent", "shadow-forge", "capsule-a", "capsule-b")


class DemoSafetyError(RuntimeError):
    """Raised before an unsafe or ambiguous filesystem operation."""


def _resolved_root(value: str | Path) -> Path:
    root = Path(value).expanduser().resolve()
    repository = REPOSITORY.resolve()
    if root == repository or repository in root.parents:
        raise DemoSafetyError("demo root must be outside the Tieru repository")
    if root == Path(root.anchor):
        raise DemoSafetyError("demo root cannot be a filesystem root")
    return root


def _owned(root: Path) -> bool:
    return (root / MARKER).is_file()


def prepare(root: Path, *, replace: bool = False) -> None:
    root = _resolved_root(root)
    if root.exists() and any(root.iterdir()):
        if not replace:
            raise DemoSafetyError("demo root is not empty; use --replace only for a prior demo root")
        if not _owned(root):
            raise DemoSafetyError("refusing to replace an unmarked directory")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("Synthetic Tieru public demo root.\n", encoding="utf-8")
    shutil.copytree(FIXTURE / "project", root / "project")
    shutil.copytree(FIXTURE / "configs", root / "configs")
    for name in HOME_NAMES:
        (root / "homes" / name).mkdir(parents=True, exist_ok=True)
    (root / "artifacts").mkdir()
    print(f"Prepared synthetic demo root: {root}")


def cleanup(root: Path) -> None:
    root = _resolved_root(root)
    if not root.is_dir() or not _owned(root):
        raise DemoSafetyError("refusing to clean an absent or unmarked directory")
    shutil.rmtree(root)
    print(f"Removed marked demo root: {root}")


def _require_prepared(root: Path) -> Path:
    root = _resolved_root(root)
    if not root.is_dir() or not _owned(root):
        raise DemoSafetyError("prepare the marked demo root before seeding it")
    return root


def seed_capsule_a(root: Path) -> None:
    """Create synthetic portable state through Tieru's canonical local stores."""
    root = _require_prepared(root)
    home = root / "homes" / "capsule-a"
    config = root / "configs" / "local-agent.yaml"

    from tieru.config import load_settings
    from tieru.db import connect
    from tieru.memory import Memory
    from tieru.memory.graph import GraphService, GraphStore
    from tieru.memory.semantic.store import SqliteFactStore

    settings = load_settings({"home": home, "config_path": config})
    settings.ensure_home()
    conn = connect(home)
    try:
        if conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]:
            raise DemoSafetyError("Capsule A already contains memory; prepare a fresh root")
        SqliteFactStore(conn).add(
            "demo-project",
            "The reviewed local check is python -m pytest -q.",
            source="user",
        )
        GraphService(GraphStore(conn)).remember_relation(
            subject="Demo Project",
            predicate="uses_check",
            object="Pytest",
            subject_type="project",
            object_type="tool",
            source_type="explicit_user_save",
            source_ref="synthetic-demo-fixture",
        )
        skill_target = home / "skills" / "demo-project-check"
        skill_target.mkdir(parents=True)
        shutil.copy2(FIXTURE / "portable-skill" / "SKILL.md", skill_target / "SKILL.md")
        (home / "SOUL.md").write_text(
            "# Demo identity\n\nSynthetic identity for public Capsule footage.\n",
            encoding="utf-8",
        )
        Memory(conn, settings, None).export_markdown()
        counts = {
            "facts": conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
            "graph_entities": conn.execute("SELECT COUNT(*) FROM graph_entities").fetchone()[0],
            "graph_relations": conn.execute("SELECT COUNT(*) FROM graph_relations").fetchone()[0],
            "skills": 1,
        }
    finally:
        conn.close()
    print("Seeded Capsule A with synthetic portable state: " + json.dumps(counts, sort_keys=True))


def seed_shadow_fixture(root: Path) -> None:
    """Create three labeled model-free Replay runs and let Shadow observe them."""
    root = _require_prepared(root)
    home = root / "homes" / "shadow-forge"
    config = root / "configs" / "local-agent.yaml"

    from tieru.config import load_settings
    from tieru.db import connect
    from tieru.replay import ReplayService
    from tieru.shadow import ShadowService

    settings = load_settings({"home": home, "config_path": config})
    settings.shadow_enabled = True
    settings.ensure_home()
    conn = connect(home)
    try:
        if conn.execute("SELECT COUNT(*) FROM replay_runs").fetchone()[0]:
            raise DemoSafetyError("Shadow fixture already has Replay runs; prepare a fresh root")
        replay = ReplayService(conn, settings)
        shadow = ShadowService(conn, settings)
        run_ids: list[str] = []
        for index in range(1, settings.shadow_min_occurrences + 1):
            run = replay.start_run(
                session_id="deterministic-demo",
                source="deterministic_demo_fixture",
                role="main",
                model="fixture-no-model",
                provider="deterministic",
                user_input=f"Synthetic calendar workflow {index}",
            )
            replay.record_event(
                run.run_id,
                "tool_requested",
                {
                    "tool": "send_message",
                    "args": {
                        "to": f"demo-reviewer-{index}",
                        "body": f"Synthetic verification draft {index} is ready for review.",
                    },
                },
            )
            replay.record_event(
                run.run_id,
                "trust_decision",
                {
                    "tool": "send_message",
                    "capabilities": ["local_write"],
                    "operation": "draft",
                    "allowed": True,
                },
            )
            replay.record_event(
                run.run_id,
                "tool_completed",
                {"tool": "send_message", "output_preview": "Synthetic local draft created."},
            )
            replay.complete_run(
                run.run_id,
                output="Synthetic deterministic fixture completed.",
                iterations=1,
                latency_ms=1,
                role="main",
                model="fixture-no-model",
                provider="deterministic",
            )
            shadow.observe(run.run_id)
            run_ids.append(run.run_id)
        suggestion = shadow.suggestions()
    finally:
        conn.close()
    print(
        "Seeded labeled deterministic Shadow fixture: "
        + json.dumps({"run_ids": run_ids, "suggestions": suggestion}, sort_keys=True)
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="dedicated demo root outside this repository")
    parser.add_argument("--replace", action="store_true", help="replace only a marker-owned demo root")
    parser.add_argument("--cleanup", action="store_true", help="remove only a marker-owned demo root")
    parser.add_argument("--seed-capsule-a", action="store_true", help="seed synthetic Capsule A state")
    parser.add_argument(
        "--seed-shadow-fixture",
        action="store_true",
        help="seed clearly labeled model-free Replay runs for deterministic Shadow validation",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        root = _resolved_root(args.root)
        if args.cleanup:
            if args.replace or args.seed_capsule_a or args.seed_shadow_fixture:
                raise DemoSafetyError("--cleanup cannot be combined with prepare or seed options")
            cleanup(root)
            return 0
        prepare(root, replace=args.replace)
        if args.seed_capsule_a:
            seed_capsule_a(root)
        if args.seed_shadow_fixture:
            seed_shadow_fixture(root)
        return 0
    except DemoSafetyError as exc:
        print(f"Demo preparation refused: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
