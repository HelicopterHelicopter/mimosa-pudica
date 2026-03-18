"""File ranking analyzer – wraps DB queries with additional filtering."""
from __future__ import annotations

from typing import Optional

from ..db import Database
from ..models import FileStats, RefType, Source


def top_files(
    db: Database,
    *,
    days: Optional[int] = None,
    limit: int = 20,
    source: Optional[Source] = None,
    exclude_writes: bool = False,
) -> list[FileStats]:
    """Return the most-referenced files, sorted by reference count.

    Since the DB is scoped to a single repo, no project_path filter is needed.
    """
    ref_types: Optional[list[RefType]] = None
    if exclude_writes:
        ref_types = [rt for rt in RefType if rt is not RefType.WRITE]

    return db.top_files(
        days=days,
        limit=limit,
        source=source,
        ref_types=ref_types,
    )
