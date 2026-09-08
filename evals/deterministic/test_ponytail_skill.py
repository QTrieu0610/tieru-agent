"""M37.1 deterministic verification for the Ponytail Coding Token Discipline skill."""

from __future__ import annotations

from pathlib import Path

from tieru.memory.procedural.loader import SkillLoader, _parse

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "skills" / "community" / "ponytail" / "SKILL.md"


def _load_ponytail():
    assert SKILL_PATH.is_file(), f"Missing {SKILL_PATH}"
    skill = _parse(SKILL_PATH)
    assert skill is not None, f"Failed to parse {SKILL_PATH}"
    return skill


def test_01_skill_md_parses():
    skill = _load_ponytail()
    assert skill.name == "ponytail"
    assert skill.body


def test_02_canonical_name_is_ponytail():
    skill = _load_ponytail()
    assert skill.name == "ponytail"


def test_03_description_contains_explicit_trigger_vocabulary():
    skill = _load_ponytail()
    desc = skill.description.casefold()
    for term in (
        "ponytail",
        "yagni",
        "minimal code",
        "simplest solution",
        "shortest diff",
        "less boilerplate",
        "avoid over-engineering",
        "do less",
    ):
        assert term in desc, f"Expected '{term}' in skill description"


def test_04_body_is_compact_under_1200_bytes():
    skill = _load_ponytail()
    body_bytes = len(skill.body.encode("utf-8"))
    assert body_bytes <= 1200, f"Ponytail body exceeds 1,200 bytes: {body_bytes}"
    raw_file_bytes = len(SKILL_PATH.read_bytes())
    assert raw_file_bytes <= 1200, f"Ponytail file exceeds 1,200 bytes: {raw_file_bytes}"


def test_05_explicit_ponytail_retrieves_ponytail():
    loader = SkillLoader([REPO_ROOT / "skills"])
    for query in ["ponytail", "please use ponytail", "apply ponytail on this bug"]:
        matches = loader.retrieve(query, top_k=2)
        assert matches, f"Failed to retrieve ponytail for explicit query: {query}"
        assert matches[0].skill.name == "ponytail"
        assert matches[0].final_score >= 0.30


def test_06_unrelated_non_coding_prompt_does_not_retrieve_it():
    loader = SkillLoader([REPO_ROOT / "skills"])
    unrelated_queries = [
        "weather",
        "what is the weather in Tokyo?",
        "translation",
        "translate this document into French",
        "casual conversation",
        "good morning, how are you doing?",
        "general factual questions",
        "what is the capital of France?",
    ]
    for query in unrelated_queries:
        matches = loader.retrieve(query, top_k=2)
        matching_names = [m.skill.name for m in matches]
        assert "ponytail" not in matching_names, (
            f"Unrelated query '{query}' erroneously retrieved ponytail (score: {[m.final_score for m in matches]})"
        )


def test_07_security_and_validation_constraints_are_preserved():
    skill = _load_ponytail()
    body = skill.body
    # Policy: Preserve security, validation, constraints, and required verification.
    assert "security" in body.casefold()
    assert "validation" in body.casefold()
    assert "constraints" in body.casefold()
    assert "verification" in body.casefold()


def test_08_required_verification_is_preserved():
    skill = _load_ponytail()
    body = skill.body
    # Policy: Run the smallest relevant existing check.
    assert "smallest relevant existing check" in body.casefold()
    # Must not encourage dropping tests or evidence
    assert "skip test" not in body.casefold()
    assert "ignore test" not in body.casefold()
    assert "bypass verification" not in body.casefold()


def test_09_skill_does_not_request_extra_model_or_tool_calls():
    skill = _load_ponytail()
    body = skill.body.casefold()
    for forbidden in ["delegate_task", "subagent", "spawn agent", "new model call", "ask_llm"]:
        assert forbidden not in body, f"Skill body mentions forbidden model call pattern: {forbidden}"


def test_10_no_always_on_system_prompt_injection():
    # Verify Ponytail is not baked into default system prompts
    session_py = (REPO_ROOT / "tieru" / "runtime" / "session.py").read_text(encoding="utf-8")
    agent_py = (REPO_ROOT / "tieru" / "loop" / "agent.py").read_text(encoding="utf-8")
    context_builder_py = (REPO_ROOT / "tieru" / "context" / "builder.py").read_text(encoding="utf-8")
    assert "ponytail" not in session_py.casefold()
    assert "ponytail" not in agent_py.casefold()
    assert "ponytail" not in context_builder_py.casefold()


def test_11_no_core_trust_action_ledger_goal_verification_changes():
    # Verify core Trust, Action Ledger, and Goal Verification remain authoritative
    kernel_py = (REPO_ROOT / "tieru" / "trust" / "kernel.py").read_text(encoding="utf-8")
    store_py = (REPO_ROOT / "tieru" / "tasks" / "store.py").read_text(encoding="utf-8")
    goal_verifier_py = (REPO_ROOT / "tieru" / "tasks" / "goal_verifier.py").read_text(encoding="utf-8")
    assert "ponytail" not in kernel_py.casefold()
    assert "ponytail" not in store_py.casefold()
    assert "ponytail" not in goal_verifier_py.casefold()
