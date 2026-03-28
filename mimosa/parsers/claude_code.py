"""Parser for Claude Code session JSONL files.

Claude Code stores sessions at:
    ~/.claude/projects/<encoded-project-path>/sessions/<uuid>.jsonl

Each line is a JSON record. We look for assistant records that contain
tool_use blocks and extract file references from:
    - Read     → input.file_path
    - Grep     → input.path  (directory/file searched)
    - Glob     → input.target_directory + input.glob_pattern
    - Write    → input.file_path
    - Bash     → input.command  (heuristic file arg extraction)
    - Task     → subagent prompt (skip – subagent has its own session)
    - MCP tool calls that reference file paths (best-effort)
"""
from __future__ import annotations

import json
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models import Reference, RefType, Session, Source, _parse_dt
from ..config import Config

# Regex to find file-like arguments in bash commands (absolute or relative paths
# with common code-file extensions or directory separators).
_BASH_FILE_RE = re.compile(
    r"""(?:^|\s)(/[\w./\-]+|(?:\.{1,2}/[\w./\-]+))"""
)

# Known tools that take a primary file_path argument
_FILE_PATH_TOOLS = {"Read", "Write", "Edit", "EditNotebook"}
# Tools that take a directory/path argument
_PATH_TOOLS = {"Grep", "SemanticSearch"}
# Glob tool
_GLOB_TOOLS = {"Glob"}


def _extract_file_paths_from_bash(command: str) -> list[str]:
    """Heuristically extract file paths from a bash command string."""
    paths: list[str] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    for token in tokens:
        # Skip flags and common non-path tokens
        if token.startswith("-") or token in ("&&", "||", "|", ";", ">", ">>", "<"):
            continue
        p = Path(token)
        # Accept tokens that look like file paths: contain a slash or have a
        # common code extension, and don't look like URLs.
        if (
            ("/" in token or "." in token)
            and not token.startswith("http")
            and not token.startswith("$")
        ):
            if p.suffix in {
                ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
                ".java", ".c", ".cpp", ".h", ".hpp", ".rb", ".sh",
                ".yaml", ".yml", ".json", ".toml", ".md", ".txt",
                ".html", ".css", ".scss", ".sql",
            } or "/" in token:
                paths.append(token)
    return paths


class ClaudeCodeParser:
    """Parses a single Claude Code session JSONL file."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self._config = config or Config()

    def parse(
        self,
        session_file: Path,
        repo_root: Optional[Path] = None,
    ) -> tuple[Session, list[Reference]]:
        """Parse a session file. If repo_root is given, paths are normalised to
        repo-relative and refs outside the repo are discarded."""
        session_id = session_file.stem
        project_path = self._infer_project_path(session_file)
        started_at: Optional[datetime] = None
        branch: Optional[str] = None
        refs: list[Reference] = []

        try:
            lines = session_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Capture metadata from any record
            ts = _parse_dt(record.get("timestamp", ""))
            if ts and started_at is None:
                started_at = ts
            if record.get("gitBranch") and branch is None:
                branch = record["gitBranch"]

            record_type = record.get("type", "")

            if record_type == "assistant":
                message = record.get("message", {})
                content = message.get("content", [])
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    new_refs = self._extract_from_tool_use(block, session_id, ts)
                    refs.extend(new_refs)

        # Normalise paths relative to repo root and drop out-of-repo refs
        if repo_root is not None:
            normalised: list[Reference] = []
            for r in refs:
                norm_path = _normalise_to_repo(r.file_path, repo_root)
                if norm_path is not None:
                    r.file_path = norm_path
                    normalised.append(r)
            refs = normalised

        session = Session(
            id=session_id,
            source=Source.CLAUDE_CODE,
            project_path=project_path,
            started_at=started_at,
            branch=branch,
        )
        return session, refs

    def _extract_from_tool_use(
        self,
        block: dict,
        session_id: str,
        timestamp: Optional[datetime],
    ) -> list[Reference]:
        tool_name: str = block.get("name", "")
        inp: dict = block.get("input", {}) or {}
        refs: list[Reference] = []

        if tool_name in _FILE_PATH_TOOLS:
            file_path = inp.get("file_path") or inp.get("path")
            if file_path:
                line_start = inp.get("offset") or inp.get("line_start")
                line_end = inp.get("limit")
                # For Write, the limit is content length – not a useful line_end
                if tool_name == "Write":
                    line_end = None
                ref_type = RefType.WRITE if tool_name in {"Write", "Edit", "EditNotebook"} else RefType.READ
                refs.append(
                    Reference(
                        session_id=session_id,
                        file_path=_normalize_path(file_path),
                        ref_type=ref_type,
                        timestamp=timestamp,
                        line_start=_int_or_none(line_start),
                        line_end=_int_or_none(line_end),
                        tool_name=tool_name,
                    )
                )

        elif tool_name in _PATH_TOOLS:
            search_path = inp.get("path") or inp.get("target_directories")
            pattern = inp.get("pattern") or inp.get("query")
            if isinstance(search_path, list):
                for p in search_path:
                    if p:
                        refs.append(
                            Reference(
                                session_id=session_id,
                                file_path=_normalize_path(p),
                                ref_type=RefType.GREP,
                                timestamp=timestamp,
                                context=pattern,
                                tool_name=tool_name,
                            )
                        )
            elif search_path:
                refs.append(
                    Reference(
                        session_id=session_id,
                        file_path=_normalize_path(search_path),
                        ref_type=RefType.GREP,
                        timestamp=timestamp,
                        context=pattern,
                        tool_name=tool_name,
                    )
                )

        elif tool_name in _GLOB_TOOLS:
            target_dir = inp.get("target_directory") or inp.get("path") or ""
            glob_pattern = inp.get("glob_pattern") or inp.get("pattern") or ""
            if target_dir or glob_pattern:
                refs.append(
                    Reference(
                        session_id=session_id,
                        file_path=_normalize_path(target_dir or "."),
                        ref_type=RefType.GLOB,
                        timestamp=timestamp,
                        context=glob_pattern or None,
                        tool_name=tool_name,
                    )
                )

        elif tool_name in {"Bash", "Shell"}:
            command: str = inp.get("command", "") or ""
            for fp in _extract_file_paths_from_bash(command):
                refs.append(
                    Reference(
                        session_id=session_id,
                        file_path=_normalize_path(fp),
                        ref_type=RefType.BASH,
                        timestamp=timestamp,
                        context=command[:200] if command else None,
                        tool_name=tool_name,
                    )
                )

        elif tool_name == "StrReplace":
            file_path = inp.get("path")
            if file_path:
                refs.append(
                    Reference(
                        session_id=session_id,
                        file_path=_normalize_path(file_path),
                        ref_type=RefType.WRITE,
                        timestamp=timestamp,
                        tool_name=tool_name,
                    )
                )

        return refs

    def _infer_project_path(self, session_file: Path) -> str:
        """Best-effort decode of the Claude project path from the directory name."""
        # session_file = ~/.claude/projects/<encoded>/sessions/<uuid>.jsonl
        parts = session_file.parts
        try:
            projects_idx = parts.index("projects")
            encoded = parts[projects_idx + 1]
            return Config.decode_claude_project_path(encoded)
        except (ValueError, IndexError):
            return ""


def _normalize_path(p: str) -> str:
    if not p:
        return p
    p = p.rstrip("/")
    if p.startswith("~"):
        p = str(Path(p).expanduser())
    return p


def _normalise_to_repo(path: str, repo_root: Path) -> Optional[str]:
    """Return repo-relative path, or None if outside the repo."""
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        return path  # already relative
    try:
        return str(p.relative_to(repo_root))
    except ValueError:
        return None  # outside repo – discard


def _int_or_none(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
