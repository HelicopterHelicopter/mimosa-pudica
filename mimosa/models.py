"""Core data models for mimosa."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class RefType(str, Enum):
    READ = "read"
    GREP = "grep"
    GLOB = "glob"
    WRITE = "write"
    BASH = "bash"
    MENTION = "mention"  # Heuristically extracted from text (Cursor)


class Source(str, Enum):
    CLAUDE_CODE = "claude-code"
    CURSOR = "cursor"
    OPENCODE = "opencode"


@dataclass
class Session:
    id: str
    source: Source
    project_path: str
    started_at: Optional[datetime]
    branch: Optional[str]
    indexed_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        if isinstance(self.source, str):
            self.source = Source(self.source)
        if isinstance(self.started_at, str):
            self.started_at = _parse_dt(self.started_at)
        if isinstance(self.indexed_at, str):
            self.indexed_at = _parse_dt(self.indexed_at)


@dataclass
class Reference:
    session_id: str
    file_path: str
    ref_type: RefType
    timestamp: Optional[datetime] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    # Additional context: grep pattern, glob pattern, bash command snippet, etc.
    context: Optional[str] = None
    tool_name: Optional[str] = None
    id: Optional[int] = None  # Set by DB after insert

    def __post_init__(self) -> None:
        if isinstance(self.ref_type, str):
            self.ref_type = RefType(self.ref_type)
        if isinstance(self.timestamp, str):
            self.timestamp = _parse_dt(self.timestamp)


@dataclass
class FileStats:
    """Aggregated stats for a file across references."""
    file_path: str
    ref_count: int
    session_count: int
    sources: list[str]
    first_seen: Optional[datetime]
    last_seen: Optional[datetime]
    ref_types: dict[str, int]  # ref_type -> count


@dataclass
class FunctionRef:
    """A reference resolved to a specific function/class within a file."""
    file_path: str
    symbol_name: str
    symbol_type: str  # "function", "class", "method"
    line_start: int
    line_end: int
    ref_count: int
    session_count: int


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None
