"""R3.2 contracts for public GitHub community files."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
CONTRIBUTING = REPO / "CONTRIBUTING.md"
SECURITY = REPO / "SECURITY.md"
CODE_OF_CONDUCT = REPO / "CODE_OF_CONDUCT.md"
CHANGELOG = REPO / "CHANGELOG.md"
RELEASING = REPO / "docs" / "RELEASING.md"
COMMUNITY = REPO / "docs" / "COMMUNITY.md"
PR_TEMPLATE = REPO / ".github" / "PULL_REQUEST_TEMPLATE.md"
ISSUE_DIR = REPO / ".github" / "ISSUE_TEMPLATE"
ISSUE_TEMPLATES = (
    ISSUE_DIR / "bug_report.md",
    ISSUE_DIR / "feature_request.md",
    ISSUE_DIR / "model_provider.md",
    ISSUE_DIR / "documentation.md",
)
PUBLIC_COMMUNITY_FILES = (
    README,
    CONTRIBUTING,
    SECURITY,
    CODE_OF_CONDUCT,
    CHANGELOG,
    RELEASING,
    COMMUNITY,
    PR_TEMPLATE,
    *ISSUE_TEMPLATES,
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _frontmatter(path: Path) -> dict:
    text = _read(path)
    assert text.startswith("---\n"), f"missing frontmatter: {path}"
    raw = text.split("---\n", 2)[1]
    return yaml.safe_load(raw)


def _internal_links(path: Path) -> list[str]:
    return [
        target
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", _read(path))
        if not re.match(r"^[a-z][a-z0-9+.-]*://", target, re.IGNORECASE)
        and not target.startswith("#")
    ]


def test_required_community_files_and_markdown_links_exist():
    for path in PUBLIC_COMMUNITY_FILES:
        assert path.is_file(), path
        for target in _internal_links(path):
            resolved = (path.parent / target.split("#", 1)[0]).resolve()
            assert resolved.exists(), f"broken link in {path}: {target}"


def test_issue_template_frontmatter_and_config_are_valid():
    expected_labels = {
        "bug_report.md": "bug",
        "feature_request.md": "enhancement",
        "model_provider.md": "enhancement, provider",
        "documentation.md": "documentation",
    }
    for path in ISSUE_TEMPLATES:
        frontmatter = _frontmatter(path)
        assert {"name", "about", "title", "labels", "assignees"} <= set(frontmatter)
        assert frontmatter["labels"] == expected_labels[path.name]
    config = yaml.safe_load(_read(ISSUE_DIR / "config.yml"))
    assert config["blank_issues_enabled"] is True
    assert all(link["url"].startswith("https://github.com/QTrieu0610/tieru-agent/")
               for link in config.get("contact_links", []))


def test_contributing_covers_setup_checks_architecture_and_rules():
    text = _read(CONTRIBUTING)
    normalized = " ".join(text.split())
    for required in (
        "python -m venv .venv",
        ".\\.venv\\Scripts\\Activate.ps1",
        "source .venv/bin/activate",
        'python -m pip install -e ".[dev]"',
        "python -m pytest -q",
        "python -m tieru.ops.release_gate",
        "API keys",
        "live Ollama",
        "Trust remains the authority",
    ):
        assert required in normalized
    for subsystem in (
        "tieru/memory/", "tieru/trust/", "tieru/replay/", "tieru/forge/",
        "tieru/shadow/", "tieru/fabric/", "tieru/capsule/", "tieru/tools/",
        "tieru/ops/",
    ):
        assert subsystem in text


def test_contribution_footprint_and_good_first_issue_boundaries_are_explicit():
    text = _read(CONTRIBUTING)
    assert "## Contribution Footprint" in text
    assert all(size in text for size in ("**Small:**", "**Medium:**", "**Large:**"))
    assert "## Good first issues" in text
    for high_risk in ("Trust policy", "Capsule importer", "Memory Graph migrations"):
        assert high_risk in text


def test_bug_security_and_pr_guidance_discourage_sensitive_uploads():
    bug = _read(ISSUE_DIR / "bug_report.md")
    security = _read(SECURITY)
    pr = _read(PR_TEMPLATE)
    assert "tieru doctor --json" in bug and "complete `.tieru/`" in bug
    for forbidden in ("state.db", "Capsule", ".env", "API keys"):
        assert forbidden in bug
        assert forbidden in security
    assert "No credentials, tokens, cookies, private Memory" in pr
    assert "allow-all" in security and "allow-all" in pr


def test_feature_and_provider_templates_cover_tieru_boundaries():
    feature = _read(ISSUE_DIR / "feature_request.md")
    provider = _read(ISSUE_DIR / "model_provider.md")
    assert "../../CONTRIBUTING.md#contribution-footprint" in feature
    assert "## Contribution Footprint" in _read(CONTRIBUTING)
    for concept in ("Local-first", "Security and Trust", "Privacy", "Model or provider"):
        assert concept in feature
    for concept in (
        "Protocol compatibility", "Tool calling", "Structured output",
        "Context window", "Credential mechanism", "marketing text",
    ):
        assert concept in provider


def test_pull_request_template_is_concise_but_covers_review_impact():
    text = _read(PR_TEMPLATE)
    for heading in (
        "## What changed?", "## Why?", "## Type", "## Validation",
        "## Impact", "## Checklist",
    ):
        assert heading in text
    for impact in (
        "Security impact", "Privacy impact", "Trust impact",
        "Backward compatibility", "Documentation",
    ):
        assert impact in text
    assert text.count("- [ ]") <= 15


def test_security_route_is_conditional_and_privacy_safe():
    text = _read(SECURITY)
    normalized = " ".join(text.split())
    assert "If the repository Security page offers" in text
    assert "/security/advisories/new" in text
    assert "Private reporting availability is controlled" in text
    assert "minimal public issue" in text
    assert "Do not include the vulnerability" in normalized
    assert "tieru doctor --json" in text


def test_changelog_and_release_guidance_do_not_create_a_release():
    changelog = _read(CHANGELOG)
    releasing = _read(RELEASING)
    assert "## Unreleased" in changelog
    assert not re.search(r"(?m)^## \[?v?\d+\.\d+\.\d+", changelog)
    assert "package version `0.3.0b1`" in releasing
    assert "`v0.3.0-beta.1`" in releasing
    assert "R4.1 readiness review is PASS" in releasing
    assert "GitHub prerelease" in releasing
    with (REPO / "pyproject.toml").open("rb") as handle:
        assert tomllib.load(handle)["project"]["version"] == "0.3.0b1"


def test_repository_setting_recommendations_are_factual_and_bounded():
    text = _read(COMMUNITY)
    normalized = " ".join(text.split())
    assert "do not claim" in normalized
    assert "Local-first personal AI runtime with memory, safe tools" in text
    for topic in ("ai-agent", "local-ai", "personal-ai", "ollama", "privacy"):
        assert f"`{topic}`" in text
    for label in ("good first issue", "trust", "provider", "security", "breaking-change"):
        assert f"`{label}`" in text
    assert "Defer the CI badge" in text


def test_readme_exposes_the_public_community_contracts():
    text = _read(README)
    for target in (
        "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md", "LICENSE",
        "PUBLIC_BETA_ACCEPTANCE.md", "CHANGELOG.md",
    ):
        assert f"]({target})" in text


def test_no_stale_or_invalid_public_commands_were_introduced():
    text = "\n".join(_read(path) for path in PUBLIC_COMMUNITY_FILES)
    assert "tieru connect google" not in text
    assert re.search(r"(?m)^tieru\s+['\"]", text) is None
    assert "Do not disable Trust or use an allow-all policy" in " ".join(text.split())


def test_project_name_conduct_and_upstream_attribution_remain_correct():
    conduct = _read(CODE_OF_CONDUCT)
    license_text = _read(REPO / "LICENSE")
    contributing = _read(CONTRIBUTING)
    assert "QTrieu0610/tieru-agent" in conduct
    assert "https://github.com/QTrieu0610" in conduct
    assert "Sean Chen (ShenSeanChen)" in license_text
    assert "Vo Quang Trieu (QTrieu0610)" in license_text
    assert "ShenSeanChen/waku-agent" in contributing
