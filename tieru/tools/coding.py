"""Bounded repository inspection, exact code patches, and read-only GitHub tools."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from tieru.tools.computer import (
    MAX_FILE_BYTES,
    MAX_SEARCH_BYTES,
    MAX_SEARCH_FILES,
    SECRET_ENV,
    SKIP_SEARCH_DIRS,
    LocalComputer,
    WorkspacePolicyError,
    _decode,
)
from tieru.tools.registry import Tool

MAX_GIT_CHARS = 50_000
MAX_PATCH_CHARS = 50_000
MAX_CODE_CHARS = 100_000
GIT_TIMEOUT = 20
GH_TIMEOUT = 20
CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".css", ".go", ".h", ".hpp", ".html", ".java", ".js", ".jsx",
    ".json", ".kt", ".md", ".php", ".py", ".rb", ".rs", ".sh", ".sql", ".swift", ".toml",
    ".ts", ".tsx", ".vue", ".xml", ".yaml", ".yml",
}
REPO_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*$"
)


def _json(**fields: Any) -> str:
    return json.dumps(fields, ensure_ascii=False, sort_keys=True)


def _bounded(text: str, limit: int = MAX_GIT_CHARS) -> tuple[str, bool]:
    return text[:limit], len(text) > limit


def _safe_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not SECRET_ENV.search(key)
    }
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"})
    return environment


class RepositoryTools:
    def __init__(self, computer: LocalComputer):
        self.computer = computer
        self.root = computer.root

    def _run_git(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.root,
                env=_safe_environment(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT,
                check=False,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("git is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"git timed out after {GIT_TIMEOUT} seconds") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "git failed").strip()[:1000]
            raise RuntimeError(message)
        return result.stdout

    def _ensure_repo(self) -> None:
        top = Path(self._run_git(["rev-parse", "--show-toplevel"]).strip()).resolve()
        if top != self.root:
            raise WorkspacePolicyError(
                "Git repository root must equal the configured workspace root"
            )

    def git_status(self) -> str:
        self._ensure_repo()
        output = self._run_git(["status", "--short", "--branch"])
        content, truncated = _bounded(output)
        return _json(status="ok", repository=".", content=content, truncated=truncated)

    def git_diff(
        self,
        path: str = "",
        staged: bool = False,
        include_untracked: bool = True,
    ) -> str:
        self._ensure_repo()
        relative = ""
        if path:
            target = self.computer._path(path)
            relative = self.computer._relative(target)
        args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        if staged:
            args.append("--cached")
        if relative:
            args += ["--", relative]
        output = self._run_git(args)
        if include_untracked and not staged:
            output += self._untracked_diff(relative)
        content, truncated = _bounded(output)
        return _json(
            status="ok",
            repository=".",
            path=relative,
            staged=staged,
            content=content,
            truncated=truncated,
        )

    def _untracked_diff(self, relative: str) -> str:
        args = ["ls-files", "--others", "--exclude-standard", "-z"]
        if relative:
            args += ["--", relative]
        names = [name for name in self._run_git(args).split("\0") if name]
        chunks = []
        remaining = MAX_GIT_CHARS
        for name in names[:100]:
            try:
                target = self.computer._path(name)
                if not target.is_file() or target.stat().st_size > MAX_FILE_BYTES:
                    continue
                with target.open("rb") as handle:
                    data = handle.read(MAX_FILE_BYTES + 1)
                if b"\x00" in data:
                    continue
                text, _encoding = _decode(data)
            except (OSError, UnicodeError, WorkspacePolicyError):
                continue
            diff = "".join(
                difflib.unified_diff(
                    [],
                    text.splitlines(keepends=True),
                    fromfile="/dev/null",
                    tofile=f"b/{name}",
                )
            )
            chunks.append(diff[:remaining])
            remaining -= len(chunks[-1])
            if remaining <= 0:
                break
        return "".join(chunks)

    def git_log(self, limit: int = 10) -> str:
        self._ensure_repo()
        count = max(1, min(int(limit), 50))
        raw = self._run_git(
            ["log", f"-n{count}", "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1e"]
        )
        commits = []
        for record in raw.strip("\n\x1e").split("\x1e"):
            fields = record.strip().split("\x1f", 4)
            if len(fields) == 5:
                commits.append(
                    {
                        "hash": fields[0],
                        "short_hash": fields[1],
                        "author": fields[2],
                        "date": fields[3],
                        "subject": fields[4],
                    }
                )
        return _json(status="ok", repository=".", commits=commits, truncated=False)

    def code_search(
        self,
        query: str,
        path: str = ".",
        max_results: int = 50,
    ) -> str:
        if not query:
            raise ValueError("query must not be empty")
        target = self.computer._path(path)
        limit = max(1, min(int(max_results), 200))
        lowered = query.casefold()
        results: list[dict[str, Any]] = []
        scanned_files = 0
        scanned_bytes = 0

        def candidates():
            if target.is_file():
                yield target
                return
            for current, dirs, files in os.walk(target, followlinks=False):
                dirs[:] = sorted(name for name in dirs if name not in SKIP_SEARCH_DIRS)
                for name in sorted(files):
                    item = Path(current) / name
                    if item.suffix.lower() in CODE_SUFFIXES:
                        yield item

        for item in candidates():
            if len(results) >= limit or scanned_files >= MAX_SEARCH_FILES:
                break
            try:
                resolved = self.computer._path(str(item))
                size = resolved.stat().st_size
                if size > MAX_FILE_BYTES or scanned_bytes + size > MAX_SEARCH_BYTES:
                    continue
                with resolved.open("rb") as handle:
                    data = handle.read(MAX_FILE_BYTES + 1)
                if b"\x00" in data:
                    continue
                text, _encoding = _decode(data)
            except (OSError, UnicodeError, WorkspacePolicyError):
                continue
            scanned_files += 1
            scanned_bytes += len(data)
            relative = self.computer._relative(resolved)
            for line_number, line in enumerate(text.splitlines(), 1):
                if lowered in line.casefold():
                    results.append({"path": relative, "line": line_number, "text": line[:500]})
                    if len(results) >= limit:
                        break
        paths = list(dict.fromkeys(item["path"] for item in results))
        return _json(
            query=query,
            path=self.computer._relative(target),
            results=results,
            candidate_paths=paths,
            next_action=(
                {"tool": "code_read", "path": paths[0]} if len(paths) == 1 else None
            ),
            scanned_files=scanned_files,
            truncated=len(results) >= limit or scanned_files >= MAX_SEARCH_FILES,
        )

    def code_read(
        self,
        path: str,
        start_line: int = 1,
        end_line: int = 400,
    ) -> str:
        target = self.computer._path(path)
        if not target.is_file():
            raise IsADirectoryError(f"workspace path is not a file: {path}")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise WorkspacePolicyError(f"code file exceeds the {MAX_FILE_BYTES}-byte limit")
        with target.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if b"\x00" in data:
            raise ValueError("binary files are not supported by code_read")
        text, encoding = _decode(data)
        lines = text.splitlines()
        start = max(1, int(start_line))
        end = max(start, min(int(end_line), start + 999))
        selected = lines[start - 1 : end]
        content = "\n".join(f"{number:>6}  {line}" for number, line in enumerate(selected, start))
        content, size_truncated = _bounded(content, MAX_CODE_CHARS)
        return _json(
            path=self.computer._relative(target),
            start_line=start,
            end_line=min(end, len(lines)),
            total_lines=len(lines),
            encoding=encoding,
            content=content,
            truncated=end < len(lines) or size_truncated,
        )

    def code_patch(self, path: str, old_text: str, new_text: str) -> str:
        if not old_text:
            raise ValueError("old_text must not be empty")
        if len(old_text) + len(new_text) > MAX_PATCH_CHARS:
            raise WorkspacePolicyError(
                f"patch exceeds the {MAX_PATCH_CHARS}-character limit"
            )
        target = self.computer._path(path)
        if not target.is_file():
            raise IsADirectoryError(f"workspace path is not a file: {path}")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise WorkspacePolicyError(f"code file exceeds the {MAX_FILE_BYTES}-byte limit")
        with target.open("rb") as handle:
            original_bytes = handle.read(MAX_FILE_BYTES + 1)
        if b"\x00" in original_bytes:
            raise ValueError("binary files cannot be patched")
        original, encoding = _decode(original_bytes)
        occurrences = original.count(old_text)
        if occurrences == 0:
            raise ValueError("patch context did not match the current file")
        if occurrences > 1:
            raise ValueError(
                f"patch context is ambiguous: expected one match, found {occurrences}"
            )
        updated = original.replace(old_text, new_text, 1)
        before_hash = hashlib.sha256(original_bytes).hexdigest()
        encoded = updated.encode(encoding)
        original_mode = target.stat().st_mode
        handle_id, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tieru-patch", dir=target.parent
        )
        try:
            with os.fdopen(handle_id, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, original_mode)
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise
        return _json(
            status="ok",
            path=self.computer._relative(target),
            changed=True,
            replacements=1,
            before_sha256=before_hash,
            after_sha256=hashlib.sha256(encoded).hexdigest(),
            bytes=len(encoded),
        )


class GitHubReader:
    """Fixed read-only gh argv; model input can never become a command or flag."""

    def __init__(self, root: Path, default_repo: str = ""):
        self.root = root
        self.default_repo = default_repo

    @staticmethod
    def _repo(value: str) -> str:
        if value and not REPO_RE.fullmatch(value):
            raise ValueError("repo must use the owner/name form")
        return value

    def _run(self, args: list[str]) -> Any:
        try:
            result = subprocess.run(
                ["gh", *args],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GH_TIMEOUT,
                check=False,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("gh CLI is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"gh timed out after {GH_TIMEOUT} seconds") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "gh failed").strip()[:1000]
            if "auth" in message.casefold() or "logged in" in message.casefold():
                raise RuntimeError("gh is not authenticated; run gh auth login")
            raise RuntimeError(message)
        if len(result.stdout) > MAX_GIT_CHARS:
            raise RuntimeError("GitHub response exceeded the bounded output limit")
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("gh returned invalid JSON") from exc

    @staticmethod
    def _source(payload: Any) -> str:
        if isinstance(payload, dict):
            return str(payload.get("url", ""))
        return ""

    def repo(self, repo: str = "") -> str:
        selected = self._repo(repo or self.default_repo)
        args = ["repo", "view"]
        if selected:
            args.append(selected)
        args += [
            "--json",
            "nameWithOwner,description,url,defaultBranchRef,isPrivate,updatedAt",
        ]
        payload = self._run(args)
        return _json(status="ok", repo=selected, url=self._source(payload), data=payload)

    def issue(self, number: int, repo: str = "") -> str:
        selected = self._repo(repo or self.default_repo)
        if int(number) <= 0:
            raise ValueError("issue number must be positive")
        args = ["issue", "view", str(number)]
        if selected:
            args += ["--repo", selected]
        args += ["--json", "number,title,body,state,author,url,updatedAt,labels"]
        payload = self._run(args)
        return _json(status="ok", repo=selected, url=self._source(payload), data=payload)

    def pr(self, number: int, repo: str = "") -> str:
        selected = self._repo(repo or self.default_repo)
        if int(number) <= 0:
            raise ValueError("PR number must be positive")
        args = ["pr", "view", str(number)]
        if selected:
            args += ["--repo", selected]
        args += [
            "--json",
            "number,title,body,state,author,url,updatedAt,baseRefName,headRefName,statusCheckRollup",
        ]
        payload = self._run(args)
        return _json(status="ok", repo=selected, url=self._source(payload), data=payload)

    def ci(self, repo: str = "", limit: int = 10) -> str:
        selected = self._repo(repo or self.default_repo)
        args = ["run", "list"]
        if selected:
            args += ["--repo", selected]
        args += [
            "--limit",
            str(max(1, min(int(limit), 50))),
            "--json",
            "databaseId,name,status,conclusion,event,headBranch,url,updatedAt",
        ]
        payload = self._run(args)
        url = ""
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            url = str(payload[0].get("url", ""))
        return _json(status="ok", repo=selected, url=url, data=payload)


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    result = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        result["required"] = required
    return result


def make_tools(repo: RepositoryTools, github: GitHubReader) -> list[Tool]:
    path = {"type": "string", "maxLength": 4096}
    local_read = {
        "read_only": True,
        "default_policy": "allow",
        "risk": "low",
        "capabilities": ("filesystem.read", "process.read"),
        "scope": "workspace",
        "resource_type": "repository",
    }
    code_read = {
        "read_only": True,
        "default_policy": "allow",
        "risk": "low",
        "capabilities": ("filesystem.read",),
        "target_arg": "path",
        "scope": "workspace",
    }
    github_read = {
        "read_only": True,
        "default_policy": "allow",
        "risk": "low",
        "capabilities": ("network.read", "process.read"),
        "target_arg": "repo",
        "resource_type": "repository",
    }
    repo_name = {
        "type": "string",
        "maxLength": 200,
        "description": "GitHub owner/name; omit to use configured/current repository",
    }
    return [
        Tool(
            "git_status",
            "Read bounded Git working-tree and branch status. Never stages or changes files.",
            _schema(),
            repo.git_status,
            operation="status",
            fixed_target="current workspace repository",
            **local_read,
        ),
        Tool(
            "git_diff",
            "Read the real bounded workspace diff, including untracked text files by default.",
            _schema(
                {
                    "path": path,
                    "staged": {"type": "boolean"},
                    "include_untracked": {"type": "boolean"},
                }
            ),
            repo.git_diff,
            operation="diff",
            target_arg="path",
            fixed_target="current workspace repository",
            **local_read,
        ),
        Tool(
            "git_log",
            "Read bounded recent Git commit metadata. Never changes refs or branches.",
            _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
            repo.git_log,
            operation="log",
            fixed_target="current workspace repository",
            **local_read,
        ),
        Tool(
            "code_search",
            "Search bounded source files in the workspace. Read a selected match before editing.",
            _schema(
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "path": path,
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                ["query"],
            ),
            repo.code_search,
            operation="search_code",
            resource_type="code",
            **code_read,
        ),
        Tool(
            "code_read",
            "Read a bounded line-numbered source range inside the workspace.",
            _schema(
                {
                    "path": path,
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                ["path"],
            ),
            repo.code_read,
            operation="read_code",
            resource_type="code",
            **code_read,
        ),
        Tool(
            "code_patch",
            "Replace one exact unique code context inside one workspace file; mismatch or ambiguity fails safely.",
            _schema(
                {
                    "path": path,
                    "old_text": {"type": "string", "minLength": 1, "maxLength": MAX_PATCH_CHARS},
                    "new_text": {"type": "string", "maxLength": MAX_PATCH_CHARS},
                },
                ["path", "old_text", "new_text"],
            ),
            repo.code_patch,
            read_only=False,
            default_policy="confirm",
            risk="medium",
            capabilities=("filesystem.write",),
            sensitive_args=("old_text", "new_text"),
            operation="patch",
            target_arg="path",
            scope="workspace",
            resource_type="code",
        ),
        Tool(
            "github_repo",
            "Read public/configured GitHub repository metadata through fixed gh CLI arguments.",
            _schema({"repo": repo_name}),
            github.repo,
            operation="read_repo",
            fixed_target="GitHub repository",
            **github_read,
        ),
        Tool(
            "github_issue",
            "Read one GitHub issue by number. Read-only: cannot edit, comment, close, or label.",
            _schema(
                {"number": {"type": "integer", "minimum": 1}, "repo": repo_name},
                ["number"],
            ),
            github.issue,
            operation="read_issue",
            fixed_target="GitHub issue",
            **github_read,
        ),
        Tool(
            "github_pr",
            "Read one GitHub pull request and its check summary. Cannot merge, edit, or comment.",
            _schema(
                {"number": {"type": "integer", "minimum": 1}, "repo": repo_name},
                ["number"],
            ),
            github.pr,
            operation="read_pr",
            fixed_target="GitHub pull request",
            **github_read,
        ),
        Tool(
            "github_ci",
            "Read bounded recent GitHub Actions runs. Cannot rerun, cancel, or dispatch workflows.",
            _schema(
                {
                    "repo": repo_name,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                }
            ),
            github.ci,
            operation="read_ci",
            fixed_target="GitHub Actions",
            **github_read,
        ),
    ]
