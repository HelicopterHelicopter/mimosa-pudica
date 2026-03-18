"""Parser for OpenCode sessions.

OpenCode stores all data in a single SQLite database:
    ~/.local/share/opencode/opencode.db

Schema (relevant tables):
    project  – id, worktree (absolute path), name
    session  – id, project_id, title, directory, time_created (unix ms)
    part     – id, message_id, session_id, time_created, data (JSON)

Each `part.data` JSON has a `type` field. We care about type="tool":
    {
      "type":   "tool",
      "callID": "...",
      "tool":   "<tool_name>",
      "state":  {
        "status": "completed" | "error" | ...,
        "input":  { ... },   ← tool arguments
        "output": "..."
      }
    }

Tool names → what we extract:
    read        → input.filePath                       (RefType.READ)
    write       → input.filePath                       (RefType.WRITE)
    edit        → input.filePath                       (RefType.WRITE)
    grep        → file paths from output text          (RefType.GREP)
    glob        → file paths from output text          (RefType.GLOB)
    list        → input.path or output file paths      (RefType.GLOB)
    bash        → heuristic file args from command     (RefType.BASH)
    codesearch  → skip (no concrete file path)
    task        → skip (subagent has its own session)
"""
from __future__ import annotations

import json
import re
import shlex
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Iterator, Optional

from ..models import Reference, RefType, Session, Source, _parse_dt
from ..config import Config

# ---- Regex to extract file paths from grep/glob output text ----------------
# OpenCode outputs lines like: "/absolute/path/to/file.py:"
_OUTPUT_FILE_LINE_RE = re.compile(r"^(/[^\n:]+\.[a-zA-Z0-9]{1,8})(?::|$)", re.MULTILINE)

# Bash file-argument extraction (reuse same logic as Claude Code parser)
_CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".rb", ".sh", ".yaml", ".yml",
    ".json", ".toml", ".md", ".txt", ".html", ".css", ".scss", ".sql",
}


def _extract_files_from_output(output: str) -> list[str]:
    """Pull absolute file paths out of grep/glob output text."""
    return [m.group(1) for m in _OUTPUT_FILE_LINE_RE.finditer(output or "")]


def _extract_files_from_bash(command: str) -> list[str]:
    paths: list[str] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if token.startswith("-") or token in {"&&", "||", "|", ";", ">", ">>", "<"}:
            continue
        if not token.startswith("$") and not token.startswith("http"):
            p = Path(token)
            if p.suffix in _CODE_EXTENSIONS or (token.startswith("/") and "/" in token):
                paths.append(token)
    return paths


class OpenCodeParser:
    """Reads OpenCode sessions from its SQLite database."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self._config = config or Config()

    @property
    def db_path(self) -> Path:
        return self._config.opencode_db_path

    def is_available(self) -> bool:
        return self.db_path.exists()

    # ------------------------------------------------------------------
    # Session discovery
    # ------------------------------------------------------------------

    def sessions_for_repo(self, repo_root: Path) -> list[dict]:
        """Return all OpenCode sessions whose project worktree matches repo_root."""
        if not self.is_available():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.title, s.directory, s.time_created,
                       p.worktree, p.id AS project_id
                FROM session s
                JOIN project p ON p.id = s.project_id
                WHERE p.worktree = ?
                ORDER BY s.time_created DESC
                """,
                (str(repo_root),),
            ).fetchall()
        return [dict(r) for r in rows]

    def all_sessions(self) -> list[dict]:
        """Return every session in the DB (used when no repo filter is available)."""
        if not self.is_available():
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.title, s.directory, s.time_created,
                       p.worktree, p.id AS project_id
                FROM session s
                JOIN project p ON p.id = s.project_id
                ORDER BY s.time_created DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_session(
        self,
        session_row: dict,
        repo_root: Optional[Path] = None,
    ) -> tuple[Session, list[Reference]]:
        """Parse one OpenCode session into a (Session, [Reference]) pair."""
        session_id = session_row["id"]
        worktree = session_row.get("worktree", "")
        time_ms = session_row.get("time_created") or 0
        started_at = datetime.utcfromtimestamp(time_ms / 1000) if time_ms else None

        session = Session(
            id=session_id,
            source=Source.OPENCODE,
            project_path=worktree,
            started_at=started_at,
            branch=None,
        )

        refs = self._extract_refs(session_id, repo_root)
        return session, refs

    def _extract_refs(
        self,
        session_id: str,
        repo_root: Optional[Path],
    ) -> list[Reference]:
        """Query `part` rows for this session and extract file references."""
        if not self.is_available():
            return []

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.data, p.time_created
                FROM part p
                WHERE p.session_id = ?
                  AND json_extract(p.data, '$.type') = 'tool'
                ORDER BY p.time_created ASC
                """,
                (session_id,),
            ).fetchall()

        refs: list[Reference] = []
        for row in rows:
            ts_ms = row["time_created"] or 0
            timestamp = datetime.utcfromtimestamp(ts_ms / 1000) if ts_ms else None
            try:
                data = json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                continue

            new_refs = self._extract_from_tool_part(data, session_id, timestamp)
            refs.extend(new_refs)

        # Normalise to repo-relative paths, drop out-of-repo refs
        if repo_root is not None:
            normalised: list[Reference] = []
            for r in refs:
                norm = _normalise_to_repo(r.file_path, repo_root)
                if norm is not None:
                    r.file_path = norm
                    normalised.append(r)
            refs = normalised

        return _deduplicate(refs)

    def _extract_from_tool_part(
        self,
        data: dict,
        session_id: str,
        timestamp: Optional[datetime],
    ) -> list[Reference]:
        tool_name: str = data.get("tool", "")
        state: dict = data.get("state") or {}
        inp: dict = state.get("input") or {}
        output: str = state.get("output") or ""
        refs: list[Reference] = []

        def add(path: str, ref_type: RefType, context: str = None, ls=None, le=None):
            if path:
                refs.append(
                    Reference(
                        session_id=session_id,
                        file_path=path.rstrip("/"),
                        ref_type=ref_type,
                        timestamp=timestamp,
                        line_start=ls,
                        line_end=le,
                        context=context,
                        tool_name=tool_name,
                    )
                )

        if tool_name == "read":
            add(inp.get("filePath", ""), RefType.READ,
                ls=inp.get("startLine"), le=inp.get("endLine"))

        elif tool_name in {"write", "edit"}:
            add(inp.get("filePath", ""), RefType.WRITE)

        elif tool_name == "grep":
            pattern = inp.get("pattern", "")
            # Extract files from output (richer signal than just the search path)
            for fp in _extract_files_from_output(output):
                add(fp, RefType.GREP, context=pattern)
            # Fallback: if output is empty but there's a cwd/path argument
            if not output and inp.get("path"):
                add(inp["path"], RefType.GREP, context=pattern)

        elif tool_name in {"glob", "list"}:
            pattern = inp.get("pattern", inp.get("glob", ""))
            for fp in _extract_files_from_output(output):
                add(fp, RefType.GLOB, context=pattern)
            # Also capture the base path if provided
            if inp.get("path") and not output:
                add(inp["path"], RefType.GLOB, context=pattern)

        elif tool_name == "bash":
            command: str = inp.get("command", "")
            for fp in _extract_files_from_bash(command):
                add(fp, RefType.BASH, context=command[:200])

        # codesearch, task, todoread, todowrite, question, webfetch – no file paths
        return refs

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _normalise_to_repo(path: str, repo_root: Path) -> Optional[str]:
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        return path
    try:
        return str(p.relative_to(repo_root))
    except ValueError:
        return None


def _deduplicate(refs: list[Reference]) -> list[Reference]:
    seen: set[tuple] = set()
    result: list[Reference] = []
    for r in refs:
        key = (r.file_path, r.ref_type, r.line_start, r.line_end)
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result
