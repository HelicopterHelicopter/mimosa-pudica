"""Configuration and path discovery for mimosa."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, Optional


_GLOBAL_HOME = Path.home() / ".mimosa"
_SETTINGS_FILE = "settings.json"
MIMOSA_DIR = ".mimosa"


def find_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from start (default: cwd) to find the nearest .git directory."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        if (directory / ".git").exists():
            return directory
    return None


class Config:
    """Runtime configuration for mimosa.

    DB is stored at <repo_root>/.mimosa/mimosa.db (per-repo).
    Global tool settings (agent tool paths) are at ~/.mimosa/settings.json.
    """

    def __init__(self, repo_root: Optional[Path] = None) -> None:
        self.repo_root = repo_root

        # Global home for shared settings
        self._global_home = Path(
            os.environ.get("MIMOSA_HOME") or str(_GLOBAL_HOME)
        )
        self._global_home.mkdir(parents=True, exist_ok=True)
        self._settings: dict = self._load_settings()

        # Per-repo .mimosa dir (or fall back to global home if not in a repo)
        self.mimosa_dir: Path = (
            repo_root / MIMOSA_DIR if repo_root is not None else self._global_home
        )

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        override = os.environ.get("MIMOSA_DB_PATH") or self._settings.get("db_path")
        if override:
            return Path(override)
        return self.mimosa_dir / "mimosa.db"

    @property
    def claude_code_base(self) -> Path:
        return Path(
            os.environ.get("MIMOSA_CLAUDE_CODE_BASE")
            or self._settings.get("claude_code_base")
            or str(Path.home() / ".claude")
        )

    @property
    def cursor_base(self) -> Path:
        return Path(
            os.environ.get("MIMOSA_CURSOR_BASE")
            or self._settings.get("cursor_base")
            or str(Path.home() / ".cursor")
        )

    @property
    def opencode_db_path(self) -> Path:
        return Path(
            os.environ.get("MIMOSA_OPENCODE_DB")
            or self._settings.get("opencode_db_path")
            or str(Path.home() / ".local" / "share" / "opencode" / "opencode.db")
        )

    # ------------------------------------------------------------------
    # Session file discovery (scoped to repo when possible)
    # ------------------------------------------------------------------

    def claude_code_session_files(self) -> Iterator[Path]:
        """Yield Claude Code session JSONL files scoped to the current repo."""
        projects_dir = self.claude_code_base / "projects"
        if not projects_dir.exists():
            return

        if self.repo_root is not None:
            # Try the exact encoded directory first
            encoded = _encode_path_for_claude(self.repo_root)
            specific_dir = projects_dir / encoded
            if specific_dir.exists():
                yield from specific_dir.glob("sessions/*.jsonl")
                return

        # Fallback: scan all sessions (e.g. not in a repo, or encoding didn't match)
        yield from projects_dir.glob("*/sessions/*.jsonl")

    def cursor_transcript_files(self) -> Iterator[Path]:
        """Yield Cursor transcript JSONL files scoped to the current repo."""
        transcripts_dir = self.cursor_base / "projects"
        if not transcripts_dir.exists():
            return

        if self.repo_root is not None:
            encoded = _encode_path_for_cursor(self.repo_root)
            specific_dir = transcripts_dir / encoded
            if specific_dir.exists():
                yield from specific_dir.glob("agent-transcripts/**/*.jsonl")
                return

        yield from transcripts_dir.glob("*/agent-transcripts/**/*.jsonl")

    # ------------------------------------------------------------------
    # Repo initialisation
    # ------------------------------------------------------------------

    def init_repo(self) -> tuple[bool, str]:
        """Create .mimosa/ in repo_root and add it to .gitignore."""
        if self.repo_root is None:
            return False, "Not in a git repository"

        self.mimosa_dir.mkdir(parents=True, exist_ok=True)

        gitignore = self.repo_root / ".gitignore"
        added_gitignore = False
        entry = ".mimosa/"
        if gitignore.exists():
            content = gitignore.read_text()
            if entry not in content and ".mimosa" not in content:
                gitignore.write_text(content.rstrip() + f"\n{entry}\n")
                added_gitignore = True
        else:
            gitignore.write_text(f"{entry}\n")
            added_gitignore = True

        suffix = " and added to .gitignore" if added_gitignore else ""
        return True, f"Initialized {self.mimosa_dir}{suffix}"

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------

    def _settings_path(self) -> Path:
        return self._global_home / _SETTINGS_FILE

    def _load_settings(self) -> dict:
        p = self._settings_path()
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def set(self, key: str, value: str) -> None:
        self._settings[key] = value
        self._settings_path().write_text(
            json.dumps(self._settings, indent=2) + "\n"
        )

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._settings.get(key, default)

    def as_dict(self) -> dict:
        return {
            "repo_root": str(self.repo_root) if self.repo_root else "(not in a git repo)",
            "mimosa_dir": str(self.mimosa_dir),
            "db_path": str(self.db_path),
            "claude_code_base": str(self.claude_code_base),
            "cursor_base": str(self.cursor_base),
            "opencode_db": str(self.opencode_db_path),
            "global_settings": str(self._settings_path()),
        }

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def to_repo_relative(self, path: str) -> Optional[str]:
        """Convert an absolute path to repo-relative if it's under repo_root.

        Returns None if the path is outside the repo (should be skipped).
        Returns the path unchanged if it's already relative.
        """
        if not path or not Path(path).is_absolute():
            return path  # relative path – keep as-is
        if self.repo_root is None:
            return path
        try:
            return str(Path(path).relative_to(self.repo_root))
        except ValueError:
            return None  # outside repo – skip

    @staticmethod
    def decode_claude_project_path(encoded: str) -> str:
        if encoded.startswith("-"):
            return encoded.replace("-", "/", 1).replace("-", "/")
        return encoded

    @staticmethod
    def decode_cursor_project_path(encoded: str) -> str:
        if not encoded.startswith("/"):
            return "/" + encoded.replace("-", "/")
        return encoded


# ------------------------------------------------------------------
# Path encoding helpers
# ------------------------------------------------------------------

def _encode_path_for_claude(repo_root: Path) -> str:
    """Generate the Claude Code encoded directory name for a repo root.

    /Users/foo/bar → -Users-foo-bar
    """
    return str(repo_root).replace("/", "-")


def _encode_path_for_cursor(repo_root: Path) -> str:
    """Generate the Cursor encoded directory name for a repo root.

    /Users/foo/bar → Users-foo-bar
    """
    return str(repo_root).lstrip("/").replace("/", "-")


def get_config(repo_root: Optional[Path] = None) -> Config:
    return Config(repo_root=repo_root)
