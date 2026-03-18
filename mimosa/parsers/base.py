"""Abstract base for session parsers."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator, Protocol

from ..models import Reference, Session


class SessionParser(Protocol):
    """Protocol that all session parsers must implement."""

    def parse(self, session_file: Path) -> tuple[Session, list[Reference]]:
        """Parse a session file and return (Session, [Reference]).

        Implementations should be idempotent – repeated parsing of the
        same file must produce the same output.
        """
        ...
