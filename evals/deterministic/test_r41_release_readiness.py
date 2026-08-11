from __future__ import annotations

import tomllib
from pathlib import Path

import tieru

REPO = Path(__file__).resolve().parents[2]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_beta_candidate_version_is_consistent() -> None:
    with (REPO / "pyproject.toml").open("rb") as handle:
        package_version = tomllib.load(handle)["project"]["version"]

    assert package_version == "0.3.0b1"
    assert tieru.__version__ == package_version
    assert package_version in _read(REPO / "docs" / "RELEASING.md")


def test_beta_release_notes_are_truthful_about_distribution_and_ci() -> None:
    notes = _read(REPO / "docs" / "RELEASE_NOTES_BETA.md")

    assert "local-first personal AI runtime" in notes
    assert "Candidate package version: `0.3.0b1`" in notes
    assert "hosted Windows, Ubuntu, and macOS GitHub" in notes
    assert "Actions evidence remains pending" in notes
    assert "not being published" in notes
    assert "PyPI as part of this beta" in notes
    assert "pip install tieru-agent` is not a supported" in notes
    assert "production ready" not in notes.lower()


def test_legacy_demo_seed_contains_only_synthetic_people() -> None:
    seed = _read(REPO / "scripts" / "demo_seed.py")

    assert "Demo User" in seed
    assert "fictional friend" in seed
    for personal_name in ("Vo Quang Trieu", "Raj", "Sergey"):
        assert personal_name not in seed


def test_final_candidate_acceptance_is_recorded_without_rewriting_r31() -> None:
    acceptance = _read(REPO / "PUBLIC_BETA_ACCEPTANCE.md")

    assert "wheel: Tieru 0.2.0" in acceptance
    assert "sdist: Tieru 0.2.0" in acceptance
    assert "wheel: Tieru 0.3.0b1" in acceptance
    assert "sdist: Tieru 0.3.0b1" in acceptance
    assert "Hosted GitHub Actions evidence remains pending" in acceptance
