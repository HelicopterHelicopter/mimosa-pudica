"""Staleness analyzer: find files that are frequently referenced but rarely updated."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from ..db import Database
from ..models import FileStats, Source


@dataclass
class StaleFile:
    file_path: str
    ref_count: int
    session_count: int
    last_referenced: Optional[datetime]
    last_git_updated: Optional[datetime]
    days_since_update: Optional[int]
    exists_on_disk: bool


def get_stale_files(
    db: Database,
    *,
    days: int = 30,
    min_refs: int = 3,
    limit: int = 30,
    source: Optional[Source] = None,
    project_path: Optional[str] = None,
    staleness_days: int = 30,
    repo_root: Optional[Path] = None,
) -> list[StaleFile]:
    """Return files that are heavily referenced but not recently updated in git."""
    file_stats = db.top_files(
        days=days,
        limit=limit * 3,
        source=source,
        project_path=project_path,
    )

    result: list[StaleFile] = []
    for fs in file_stats:
        if fs.ref_count < min_refs:
            continue

        # Resolve to absolute path for disk/git checks
        file_path_str = fs.file_path
        if repo_root is not None and not Path(file_path_str).is_absolute():
            abs_path = repo_root / file_path_str
        else:
            abs_path = Path(file_path_str)

        p = abs_path
        exists = p.exists()
        last_git_updated = _git_last_modified(str(p), repo_root=repo_root)

        days_since: Optional[int] = None
        if last_git_updated:
            days_since = (datetime.utcnow() - last_git_updated).days

        # Consider stale if: file hasn't been modified in git for staleness_days
        # OR file no longer exists on disk
        is_stale = (
            not exists
            or (days_since is not None and days_since >= staleness_days)
            or (last_git_updated is None and exists)
        )

        if is_stale:
            result.append(
                StaleFile(
                    file_path=fs.file_path,
                    ref_count=fs.ref_count,
                    session_count=fs.session_count,
                    last_referenced=fs.last_seen,
                    last_git_updated=last_git_updated,
                    days_since_update=days_since,
                    exists_on_disk=exists,
                )
            )

        if len(result) >= limit:
            break

    return result


def _git_last_modified(
    file_path: str,
    repo_root: Optional[Path] = None,
) -> Optional[datetime]:
    """Get the last commit date for a file via git log."""
    p = Path(file_path)

    # Determine the directory to run git from
    if repo_root is not None:
        cwd = repo_root
    elif p.is_absolute():
        cwd = p.parent if p.parent.exists() else None
    else:
        return None

    if cwd is None:
        return None

    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%aI", "--", str(p)],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=5,
        )
        output = result.stdout.strip()
        if not output:
            return None
        output = output[:19]  # strip timezone: "2024-03-15T14:23:00"
        from ..models import _parse_dt
        return _parse_dt(output)
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        return None
