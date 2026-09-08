"""Strict JSON corpus loading with stable IDs and reproducible hashing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tieru.evals.models import EvalCase, EvalExpectation, EvalSetup

MAX_CORPUS_BYTES = 2 * 1024 * 1024
MAX_CASES = 500


class CorpusError(ValueError):
    """The checked-in evaluation corpus violates the versioned schema."""


@dataclass(frozen=True)
class EvalCorpus:
    schema_version: int
    corpus_version: str
    cases: tuple[EvalCase, ...]
    content_hash: str
    source_paths: tuple[Path, ...] = ()
    full_case_count: int = 0
    selection_scope: str = "full_corpus"

    def select(self, *, category: str | None = None, case_id: str | None = None) -> EvalCorpus:
        selected = tuple(
            case for case in self.cases
            if (category is None or case.category == category)
            and (case_id is None or case.case_id == case_id)
        )
        if case_id is not None and not selected:
            raise CorpusError(f"unknown evaluation case: {case_id}")
        if category is not None and not selected:
            raise CorpusError(f"unknown or empty evaluation category: {category}")
        full_count = self.full_case_count or len(self.cases)
        if case_id is not None:
            scope = "single_case"
        elif category is not None:
            scope = "category_subset"
        else:
            scope = self.selection_scope
        return EvalCorpus(
            self.schema_version,
            self.corpus_version,
            selected,
            self.content_hash,
            self.source_paths,
            full_count,
            scope,
        )


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CorpusError(f"{field} must be an array of strings")
    if len(value) > 64 or any(not item.strip() or len(item) > 256 for item in value):
        raise CorpusError(f"{field} contains an invalid item")
    return tuple(item.strip() for item in value)


def _setup(raw: Any) -> EvalSetup:
    if raw is None:
        return EvalSetup()
    if not isinstance(raw, dict):
        raise CorpusError("setup must be an object")
    files = raw.get("files") or {}
    if not isinstance(files, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in files.items()
    ):
        raise CorpusError("setup.files must map paths to text")
    memories = _strings(raw.get("memories"), "setup.memories")
    skills = raw.get("skills") or []
    tools = raw.get("fake_tools") or []
    script = raw.get("script") or []
    trust = raw.get("trust_policy") or {}
    if not all(isinstance(item, dict) for item in (*skills, *tools, *script)):
        raise CorpusError("setup skills, fake_tools, and script must contain objects")
    if not isinstance(trust, dict):
        raise CorpusError("setup.trust_policy must be an object")
    return EvalSetup(
        dict(files), memories, tuple(skills), dict(trust), tuple(tools), tuple(script),
        str(raw["clock"]) if raw.get("clock") is not None else None,
    )


def _expectation(raw: Any) -> EvalExpectation:
    if not isinstance(raw, dict):
        raise CorpusError("expected must be an object")
    allowed = set(EvalExpectation.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise CorpusError(f"unknown expectation fields: {', '.join(sorted(unknown))}")
    values = dict(raw)
    for name in (
        "required_tools", "forbidden_tools", "required_events", "forbidden_events",
        "expected_artifacts", "unchanged_artifacts", "required_evidence",
    ):
        values[name] = _strings(raw.get(name), f"expected.{name}")
    try:
        return EvalExpectation(**values)
    except (TypeError, ValueError) as exc:
        raise CorpusError(f"invalid expectation: {exc}") from exc


def _case(raw: Any) -> EvalCase:
    if not isinstance(raw, dict):
        raise CorpusError("each case must be an object")
    required = {"case_id", "category", "goal", "expected"}
    if missing := required - set(raw):
        raise CorpusError(f"case missing fields: {', '.join(sorted(missing))}")
    try:
        return EvalCase(
            case_id=str(raw["case_id"]), category=str(raw["category"]),
            goal=str(raw["goal"]), setup=_setup(raw.get("setup")),
            expected=_expectation(raw["expected"]),
            max_steps=int(raw.get("max_steps", 8)),
            max_tool_calls=int(raw.get("max_tool_calls", 12)),
            max_model_calls=int(raw.get("max_model_calls", 8)),
            timeout_ms=int(raw.get("timeout_ms", 30_000)),
            tags=_strings(raw.get("tags"), "tags"),
        )
    except (TypeError, ValueError) as exc:
        raise CorpusError(f"invalid case {raw.get('case_id', '<unknown>')}: {exc}") from exc


def load_corpus(path: Path | str | tuple[Path | str, ...] | list[Path | str]) -> EvalCorpus:
    paths = (path,) if isinstance(path, (str, Path)) else tuple(path)
    if not paths:
        raise CorpusError("at least one corpus path is required")
    cases: list[EvalCase] = []
    versions: list[str] = []
    digest = hashlib.sha256()
    schema_version = 1
    resolved: list[Path] = []
    for item in paths:
        source = Path(item).resolve()
        raw = source.read_bytes()
        if len(raw) > MAX_CORPUS_BYTES:
            raise CorpusError(f"corpus exceeds {MAX_CORPUS_BYTES} bytes: {source}")
        digest.update(source.name.encode("utf-8") + b"\0" + raw)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorpusError(f"invalid JSON corpus {source}: {exc}") from exc
        if not isinstance(value, dict) or int(value.get("schema_version", 0)) != 1:
            raise CorpusError("corpus schema_version must be 1")
        version = value.get("corpus_version")
        raw_cases = value.get("cases")
        if not isinstance(version, str) or not version.strip():
            raise CorpusError("corpus_version must be a non-empty string")
        if not isinstance(raw_cases, list):
            raise CorpusError("cases must be an array")
        versions.append(version.strip())
        cases.extend(_case(case) for case in raw_cases)
        resolved.append(source)
    if len(cases) > MAX_CASES:
        raise CorpusError(f"corpus exceeds {MAX_CASES} cases")
    ids = [case.case_id for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise CorpusError(f"duplicate case IDs: {', '.join(duplicates)}")
    version = "+".join(dict.fromkeys(versions))
    return EvalCorpus(
        schema_version,
        version,
        tuple(cases),
        digest.hexdigest(),
        tuple(resolved),
        len(cases),
        "full_corpus",
    )


def default_corpus_paths() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parents[2] / "evals" / "cases"
    if not root.is_dir():
        root = Path(__file__).resolve().parent / "cases"
    return tuple(sorted(root.glob("*.json")))


def default_live_corpus_paths() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parents[2] / "evals" / "live"
    if not root.is_dir():
        root = Path(__file__).resolve().parent / "live"
    return tuple(sorted(root.glob("*.json")))
