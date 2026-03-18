"""Git blame + agent reference annotation.

For a given file, we:
1. Run `git blame --porcelain` to get per-line commit hashes and timestamps.
2. Cross-reference commit timestamps against indexed sessions to find which
   session was likely active when each commit was made.
3. Look up what other files that session was referencing.
4. Return an annotated view: for each unique commit in the file, show the
   agent session context (referenced files).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..db import Database
from ..models import _parse_dt


@dataclass
class LineAnnotation:
    line_number: int
    line_content: str
    commit_hash: Optional[str]
    commit_time: Optional[datetime]
    author: Optional[str]
    session_id: Optional[str]
    session_refs: list[str] = field(default_factory=list)


@dataclass
class CommitAnnotation:
    commit_hash: str
    commit_time: Optional[datetime]
    author: Optional[str]
    summary: Optional[str]
    line_range: tuple[int, int]  # (first_line, last_line) in the file
    session_id: Optional[str]
    referenced_files: list[str] = field(default_factory=list)


def annotate_file(
    db: Database,
    file_path: str,
    *,
    session_window_minutes: int = 120,
) -> list[CommitAnnotation]:
    """Return commit-level annotations showing which files the agent referenced.

    ``session_window_minutes`` is how far before/after a commit we search for
    a matching session (default: 2 hours).
    """
    p = Path(file_path)
    if not p.exists():
        return []

    blame_entries = _git_blame(p)
    if not blame_entries:
        return []

    # Group blame entries by commit hash
    commits: dict[str, list[dict]] = {}
    for entry in blame_entries:
        ch = entry.get("commit")
        if ch:
            commits.setdefault(ch, []).append(entry)

    window = timedelta(minutes=session_window_minutes)
    all_sessions = db.all_sessions()

    result: list[CommitAnnotation] = []
    for commit_hash, entries in commits.items():
        commit_time = entries[0].get("committer_time") or entries[0].get("author_time")
        author = entries[0].get("author")
        summary = entries[0].get("summary")
        line_numbers = [e["line"] for e in entries]
        line_range = (min(line_numbers), max(line_numbers))

        # Find sessions whose started_at is within window of commit time
        matched_session = _find_session_near_time(
            all_sessions, commit_time, window
        )

        referenced: list[str] = []
        if matched_session:
            refs = db.get_refs_for_session(matched_session)
            referenced = _deduplicate_paths(refs, exclude=file_path)

        result.append(
            CommitAnnotation(
                commit_hash=commit_hash[:8],
                commit_time=commit_time,
                author=author,
                summary=summary,
                line_range=line_range,
                session_id=matched_session,
                referenced_files=referenced,
            )
        )

    result.sort(key=lambda c: c.commit_time or datetime.min, reverse=True)
    return result


def _git_blame(file_path: Path) -> list[dict]:
    """Run git blame --porcelain and parse the output."""
    try:
        result = subprocess.run(
            ["git", "blame", "--porcelain", str(file_path)],
            capture_output=True,
            text=True,
            cwd=str(file_path.parent),
            timeout=30,
        )
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    return _parse_blame_porcelain(result.stdout)


def _parse_blame_porcelain(output: str) -> list[dict]:
    """Parse git blame --porcelain output into a list of line dicts."""
    entries: list[dict] = []
    current: dict = {}

    for line in output.splitlines():
        if not line:
            continue

        # Commit line: 40-char hash followed by orig_line final_line [count]
        if len(line) >= 40 and line[:40].isalnum() and " " in line:
            parts = line.split()
            if len(parts) >= 3 and len(parts[0]) == 40:
                if current:
                    entries.append(current)
                current = {"commit": parts[0], "line": int(parts[2])}
                continue

        if line.startswith("\t"):
            current["content"] = line[1:]
            continue

        # Metadata lines
        if line.startswith("author "):
            current["author"] = line[7:]
        elif line.startswith("author-time "):
            ts = int(line[12:])
            current["author_time"] = datetime.utcfromtimestamp(ts)
        elif line.startswith("committer-time "):
            ts = int(line[15:])
            current["committer_time"] = datetime.utcfromtimestamp(ts)
        elif line.startswith("summary "):
            current["summary"] = line[8:]

    if current:
        entries.append(current)

    return entries


def _find_session_near_time(
    sessions: list[dict],
    target: Optional[datetime],
    window: timedelta,
) -> Optional[str]:
    """Find the session whose start time is closest to target within window."""
    if target is None:
        return None

    best_id: Optional[str] = None
    best_delta = window

    for session in sessions:
        ts_str = session.get("started_at") or session.get("indexed_at")
        if not ts_str:
            continue
        ts = _parse_dt(ts_str)
        if ts is None:
            continue
        delta = abs(ts - target)
        if delta <= window and delta < best_delta:
            best_delta = delta
            best_id = session["id"]

    return best_id


def _deduplicate_paths(refs: list[dict], exclude: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for r in refs:
        fp = r.get("file_path", "")
        if fp and fp != exclude and fp not in seen:
            seen.add(fp)
            result.append(fp)
    return result
