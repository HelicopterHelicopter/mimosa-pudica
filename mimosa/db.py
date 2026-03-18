"""SQLite database layer for mimosa."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Iterator, Optional

from .models import FileStats, Reference, RefType, Session, Source


DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    project_path TEXT NOT NULL DEFAULT '',
    started_at  TEXT,
    branch      TEXT,
    indexed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    file_path   TEXT NOT NULL,
    ref_type    TEXT NOT NULL,
    timestamp   TEXT,
    line_start  INTEGER,
    line_end    INTEGER,
    context     TEXT,
    tool_name   TEXT
);

CREATE INDEX IF NOT EXISTS idx_refs_file_path  ON refs(file_path);
CREATE INDEX IF NOT EXISTS idx_refs_timestamp  ON refs(timestamp);
CREATE INDEX IF NOT EXISTS idx_refs_session_id ON refs(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_path);
"""


def _dt_str(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(DDL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def session_exists(self, session_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return row is not None

    def upsert_session(self, session: Session) -> None:
        self._conn.execute(
            """
            INSERT INTO sessions (id, source, project_path, started_at, branch, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                indexed_at   = excluded.indexed_at,
                branch       = excluded.branch
            """,
            (
                session.id,
                session.source.value,
                session.project_path,
                _dt_str(session.started_at),
                session.branch,
                _dt_str(session.indexed_at),
            ),
        )

    def delete_session_refs(self, session_id: str) -> None:
        """Remove all refs for a session so it can be re-indexed cleanly."""
        self._conn.execute("DELETE FROM refs WHERE session_id = ?", (session_id,))

    # ------------------------------------------------------------------
    # References
    # ------------------------------------------------------------------

    def insert_refs(self, refs: list[Reference]) -> None:
        self._conn.executemany(
            """
            INSERT INTO refs (session_id, file_path, ref_type, timestamp, line_start, line_end, context, tool_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r.session_id,
                    r.file_path,
                    r.ref_type.value,
                    _dt_str(r.timestamp),
                    r.line_start,
                    r.line_end,
                    r.context,
                    r.tool_name,
                )
                for r in refs
            ],
        )

    # ------------------------------------------------------------------
    # Analytics queries
    # ------------------------------------------------------------------

    def top_files(
        self,
        *,
        days: Optional[int] = None,
        limit: int = 20,
        source: Optional[Source] = None,
        project_path: Optional[str] = None,
        ref_types: Optional[list[RefType]] = None,
    ) -> list[FileStats]:
        conditions = []
        params: list = []

        if days is not None:
            cutoff = datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            from datetime import timedelta
            cutoff = cutoff - timedelta(days=days - 1)
            conditions.append("(r.timestamp >= ? OR r.timestamp IS NULL)")
            params.append(_dt_str(cutoff))

        if source is not None:
            conditions.append("s.source = ?")
            params.append(source.value)

        if project_path is not None:
            conditions.append("s.project_path LIKE ?")
            params.append(f"%{project_path}%")

        if ref_types:
            placeholders = ",".join("?" * len(ref_types))
            conditions.append(f"r.ref_type IN ({placeholders})")
            params.extend(rt.value for rt in ref_types)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        rows = self._conn.execute(
            f"""
            SELECT
                r.file_path,
                COUNT(*)                                AS ref_count,
                COUNT(DISTINCT r.session_id)            AS session_count,
                GROUP_CONCAT(DISTINCT s.source)         AS sources,
                MIN(r.timestamp)                        AS first_seen,
                MAX(r.timestamp)                        AS last_seen,
                GROUP_CONCAT(r.ref_type)                AS ref_types_csv
            FROM refs r
            JOIN sessions s ON s.id = r.session_id
            {where}
            GROUP BY r.file_path
            ORDER BY ref_count DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()

        result = []
        for row in rows:
            type_counts: dict[str, int] = {}
            if row["ref_types_csv"]:
                for rt in row["ref_types_csv"].split(","):
                    type_counts[rt] = type_counts.get(rt, 0) + 1

            result.append(
                FileStats(
                    file_path=row["file_path"],
                    ref_count=row["ref_count"],
                    session_count=row["session_count"],
                    sources=row["sources"].split(",") if row["sources"] else [],
                    first_seen=_parse_dt_str(row["first_seen"]),
                    last_seen=_parse_dt_str(row["last_seen"]),
                    ref_types=type_counts,
                )
            )
        return result

    def file_history(
        self,
        file_path: str,
        *,
        days: Optional[int] = None,
        limit: int = 100,
    ) -> list[dict]:
        conditions = ["r.file_path = ?"]
        params: list = [file_path]

        if days is not None:
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=days)
            conditions.append("(r.timestamp >= ? OR r.timestamp IS NULL)")
            params.append(_dt_str(cutoff))

        where = "WHERE " + " AND ".join(conditions)

        rows = self._conn.execute(
            f"""
            SELECT
                r.id, r.session_id, r.ref_type, r.timestamp,
                r.line_start, r.line_end, r.context, r.tool_name,
                s.source, s.project_path, s.branch
            FROM refs r
            JOIN sessions s ON s.id = r.session_id
            {where}
            ORDER BY r.timestamp DESC
            LIMIT ?
            """,
            params + [limit],
        ).fetchall()

        return [dict(row) for row in rows]

    def get_sessions_for_file(self, file_path: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT session_id FROM refs WHERE file_path = ?",
            (file_path,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_refs_for_session(self, session_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM refs WHERE session_id = ?", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def all_sessions(self, source: Optional[Source] = None) -> list[dict]:
        if source:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE source = ? ORDER BY started_at DESC",
                (source.value,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def summary_stats(self) -> dict:
        row = self._conn.execute(
            """
            SELECT
                COUNT(DISTINCT s.id)         AS total_sessions,
                COUNT(r.id)                  AS total_refs,
                COUNT(DISTINCT r.file_path)  AS unique_files,
                MIN(r.timestamp)             AS earliest,
                MAX(r.timestamp)             AS latest
            FROM sessions s
            LEFT JOIN refs r ON r.session_id = s.id
            """
        ).fetchone()
        return dict(row) if row else {}


def _parse_dt_str(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    from .models import _parse_dt
    return _parse_dt(s)
