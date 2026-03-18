"""Parser for Cursor agent transcript JSONL files.

Cursor transcripts live at:
    ~/.cursor/projects/<encoded>/agent-transcripts/<uuid>/<uuid>.jsonl
    ~/.cursor/projects/<encoded>/agent-transcripts/<uuid>/subagents/<uuid>.jsonl

Unlike Claude Code, Cursor transcripts do NOT expose raw tool calls.
We extract file references heuristically from user and assistant message text:

1. <attached_files> XML blocks  → path= attributes
2. <code_selection path="…">   → path= attribute
3. <terminal_selection path="…"> → path= attribute (skip – terminal files)
4. @/absolute/path:lines        → @-references in user text
5. ```startLine:endLine:filepath code citation blocks in assistant text
6. Backtick-quoted paths         → `src/utils/api.ts` in assistant prose
7. Plain file path mentions      → /absolute/paths in text
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models import Reference, RefType, Session, Source, _parse_dt
from ..config import Config

# ---- Compiled regexes -------------------------------------------------------

# Matches path= attributes inside XML-like tags
_XML_PATH_RE = re.compile(r'\bpath="([^"]+)"')

# Matches @/path:line-range or @/path references
_AT_REF_RE = re.compile(
    r'@(/[^\s"\'<>]+?)(?::(\d+)(?:-(\d+))?)?(?=[\s"\'<>,\)]|$)'
)

# Matches code citation blocks: ```startLine:endLine:filepath or ```start:end:filepath
_CITATION_RE = re.compile(
    r"```(\d+):(\d+):([^\s`\n]+)"
)

# Matches backtick-quoted paths that look like file paths (contain / or . with
# a known extension)
_BACKTICK_PATH_RE = re.compile(
    r"`(/[^`\n]+\.[a-zA-Z0-9]{1,6}|[a-zA-Z0-9_./-]+\.[a-zA-Z0-9]{1,6})`"
)

# Matches absolute paths in plain text (not preceded by = or ")
_ABS_PATH_RE = re.compile(
    r"(?<![=\"'/])(/(?:Users|home|workspace|app|src|repo|project)/[^\s\"'<>,\)\]\}]+)"
)

# Common code file extensions – used to filter false-positive backtick matches
_CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".rb", ".sh", ".yaml", ".yml",
    ".json", ".toml", ".md", ".txt", ".html", ".css", ".scss",
    ".sql", ".proto", ".graphql",
}

# Paths that are clearly not source files
_SKIP_PREFIXES = (
    "/Users/JHEEL/.cursor/",
    "/var/folders/",
    "/tmp/",
    "/private/tmp/",
    "/System/",
    "/Library/Developer/",
)

# Regex for patterns that look like terminal files or internal Cursor paths
_TERMINAL_PATH_RE = re.compile(r"terminals/\d+(?:\.txt)?$")


def _is_code_path(p: str) -> bool:
    if not p:
        return False
    # Reject paths with colons (not valid in Unix paths – likely doc examples like /path:line)
    if ":" in p:
        return False
    # Reject paths ending with backtick or quote (doc artifacts)
    if p.endswith(("`", "'", '"', ")")):
        return False
    for prefix in _SKIP_PREFIXES:
        if p.startswith(prefix):
            return False
    # Reject Cursor-internal terminal files (e.g. /Users-foo-bar/terminals/5)
    if _TERMINAL_PATH_RE.search(p):
        return False
    # Reject paths that look like encoded Cursor project paths without a file extension
    # (e.g. /Users-JHEEL-new-repos-communication-service/5 — no extension, ends in digit)
    path = Path(p)
    if not path.suffix and path.name.isdigit():
        return False
    # Accept if has a code extension OR is a deep enough path (3+ components)
    if path.suffix in _CODE_EXTENSIONS:
        return True
    parts = [c for c in p.split("/") if c]
    return len(parts) >= 3 and not p.endswith((".", ".."))


class CursorParser:
    """Parses a single Cursor agent transcript JSONL file."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self._config = config or Config()

    def parse(
        self,
        session_file: Path,
        repo_root: Optional[Path] = None,
    ) -> tuple[Session, list[Reference]]:
        """Parse a transcript file. If repo_root is given, absolute paths are
        normalised to repo-relative and out-of-repo paths are discarded."""
        session_id = session_file.stem
        project_path = self._infer_project_path(session_file)
        started_at: Optional[datetime] = None
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

            role = record.get("role", "")
            message = record.get("message", {})
            content = message.get("content", [])

            if isinstance(content, str):
                content = [{"type": "text", "text": content}]

            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text", "")
                if not text:
                    continue

                # Use a slightly later timestamp heuristic for Cursor –
                # transcripts don't embed per-message timestamps, so we
                # capture None and let the session-level started_at fill it.
                new_refs = self._extract_from_text(
                    text, session_id, role, timestamp=None
                )
                refs.extend(new_refs)

        deduped = _deduplicate_refs(refs)

        # Normalise to repo-relative; drop refs outside the repo
        if repo_root is not None:
            normalised: list[Reference] = []
            for r in deduped:
                norm_path = _normalise_to_repo(r.file_path, repo_root)
                if norm_path is not None:
                    r.file_path = norm_path
                    normalised.append(r)
            deduped = normalised

        session = Session(
            id=session_id,
            source=Source.CURSOR,
            project_path=project_path,
            started_at=started_at,
            branch=None,
        )
        return session, deduped

    def _extract_from_text(
        self,
        text: str,
        session_id: str,
        role: str,
        timestamp: Optional[datetime],
    ) -> list[Reference]:
        refs: list[Reference] = []
        seen: set[str] = set()

        def add(file_path: str, ref_type: RefType, line_start=None, line_end=None, context=None):
            norm = _normalize_path(file_path)
            if not norm or not _is_code_path(norm):
                return
            key = f"{norm}:{line_start}:{line_end}"
            if key in seen:
                return
            seen.add(key)
            refs.append(
                Reference(
                    session_id=session_id,
                    file_path=norm,
                    ref_type=ref_type,
                    timestamp=timestamp,
                    line_start=line_start,
                    line_end=line_end,
                    context=context,
                    tool_name="cursor_heuristic",
                )
            )

        # 1. XML path= attributes (attached_files, code_selection, etc.)
        for match in _XML_PATH_RE.finditer(text):
            p = match.group(1)
            # Skip Cursor internal paths (terminal files, etc.)
            if ".cursor/projects" in p and "terminals" in p:
                continue
            add(p, RefType.MENTION)

        # 2. @-references
        for match in _AT_REF_RE.finditer(text):
            p = match.group(1)
            ls = int(match.group(2)) if match.group(2) else None
            le = int(match.group(3)) if match.group(3) else None
            add(p, RefType.MENTION, line_start=ls, line_end=le)

        # 3. Code citation blocks: ```startLine:endLine:filepath
        for match in _CITATION_RE.finditer(text):
            ls, le, p = int(match.group(1)), int(match.group(2)), match.group(3)
            add(p, RefType.READ, line_start=ls, line_end=le)

        # 4. Backtick-quoted paths in assistant text (only for assistant role to
        #    avoid double-counting user attached files already handled above)
        if role == "assistant":
            for match in _BACKTICK_PATH_RE.finditer(text):
                p = match.group(1)
                add(p, RefType.MENTION)

        # 5. Absolute paths in text (both roles, conservative filter)
        for match in _ABS_PATH_RE.finditer(text):
            p = match.group(1).rstrip(".,;:!?)")
            add(p, RefType.MENTION)

        return refs

    def _infer_project_path(self, session_file: Path) -> str:
        """Decode the Cursor project path from the directory name."""
        parts = session_file.parts
        try:
            cursor_idx = parts.index(".cursor")
            projects_idx = parts.index("projects", cursor_idx)
            encoded = parts[projects_idx + 1]
            return Config.decode_cursor_project_path(encoded)
        except (ValueError, IndexError):
            return ""


def _normalize_path(p: str) -> str:
    if not p:
        return p
    p = p.strip().rstrip("/")
    if p.startswith("~"):
        p = str(Path(p).expanduser())
    return p


def _normalise_to_repo(path: str, repo_root: Path) -> Optional[str]:
    """Return repo-relative path, or None if outside the repo."""
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        return path  # already relative – keep
    try:
        return str(p.relative_to(repo_root))
    except ValueError:
        return None  # outside repo – discard


def _deduplicate_refs(refs: list[Reference]) -> list[Reference]:
    """Remove exact duplicates (same file, type, line range)."""
    seen: set[tuple] = set()
    result: list[Reference] = []
    for r in refs:
        key = (r.file_path, r.ref_type, r.line_start, r.line_end)
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result
