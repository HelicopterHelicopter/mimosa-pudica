"""Function-level reference breakdown using tree-sitter ASTs.

For each referenced file (with optional line range), we find the enclosing
function/class/method and accumulate reference counts at that symbol level.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..db import Database
from ..models import FunctionRef


@dataclass
class _Symbol:
    name: str
    sym_type: str  # "function", "class", "method"
    line_start: int
    line_end: int


def top_functions(
    db: Database,
    *,
    days: Optional[int] = None,
    limit: int = 30,
    source_filter: Optional[str] = None,
) -> list[FunctionRef]:
    """Return the most-referenced functions/classes across all files."""
    from .ranking import top_files
    from ..models import Source

    src = Source(source_filter) if source_filter else None
    file_stats = top_files(db, days=days, limit=50, source=src)

    # Accumulate symbol-level counts
    symbol_counts: dict[tuple[str, str, str], dict] = {}
    # key: (file_path, symbol_name, sym_type) → {ref_count, sessions, line_start, line_end}

    for fs in file_stats:
        file_path = fs.file_path
        p = Path(file_path)
        if not p.exists() or not p.is_file():
            continue

        symbols = _extract_symbols(p)
        if not symbols:
            continue

        refs = db.file_history(file_path, days=days, limit=1000)
        for ref in refs:
            ls = ref.get("line_start")
            le = ref.get("line_end")
            session_id = ref.get("session_id", "")

            enclosing = _find_enclosing_symbol(symbols, ls, le)
            if enclosing is None:
                continue

            key = (file_path, enclosing.name, enclosing.sym_type)
            if key not in symbol_counts:
                symbol_counts[key] = {
                    "ref_count": 0,
                    "sessions": set(),
                    "line_start": enclosing.line_start,
                    "line_end": enclosing.line_end,
                }
            symbol_counts[key]["ref_count"] += 1
            symbol_counts[key]["sessions"].add(session_id)

    results: list[FunctionRef] = []
    for (file_path, sym_name, sym_type), data in symbol_counts.items():
        results.append(
            FunctionRef(
                file_path=file_path,
                symbol_name=sym_name,
                symbol_type=sym_type,
                line_start=data["line_start"],
                line_end=data["line_end"],
                ref_count=data["ref_count"],
                session_count=len(data["sessions"]),
            )
        )

    results.sort(key=lambda r: r.ref_count, reverse=True)
    return results[:limit]


def _extract_symbols(file_path: Path) -> list[_Symbol]:
    """Extract function/class/method symbols from a source file.

    Attempts tree-sitter first; falls back to regex for common languages.
    """
    try:
        return _extract_with_treesitter(file_path)
    except Exception:
        pass
    return _extract_with_regex(file_path)


def _extract_with_treesitter(file_path: Path) -> list[_Symbol]:
    """Use tree-sitter to extract symbols with exact line ranges."""
    import tree_sitter  # noqa: F401 - presence check

    lang = _detect_language(file_path)
    if lang is None:
        return []

    parser = _get_parser(lang)
    if parser is None:
        return []

    source = file_path.read_bytes()
    tree = parser.parse(source)
    symbols: list[_Symbol] = []

    # Query varies by language; we walk the tree manually
    _walk_tree(tree.root_node, source, symbols, lang)
    return symbols


def _walk_tree(node, source: bytes, symbols: list[_Symbol], lang: str) -> None:
    """Recursively walk a tree-sitter node and collect named symbols."""
    FUNCTION_TYPES = {
        "python": {"function_definition", "async_function_definition"},
        "typescript": {"function_declaration", "method_definition", "arrow_function",
                       "function_expression", "abstract_method_signature"},
        "javascript": {"function_declaration", "method_definition", "arrow_function",
                       "function_expression"},
        "go": {"function_declaration", "method_declaration"},
        "rust": {"function_item", "impl_item"},
    }
    CLASS_TYPES = {
        "python": {"class_definition"},
        "typescript": {"class_declaration", "interface_declaration"},
        "javascript": {"class_declaration"},
        "go": {"type_spec"},
        "rust": {"struct_item", "enum_item", "trait_item"},
    }

    func_types = FUNCTION_TYPES.get(lang, set())
    class_types = CLASS_TYPES.get(lang, set())

    node_type = node.type
    if node_type in func_types or node_type in class_types:
        name = _get_node_name(node, source)
        sym_type = "class" if node_type in class_types else "function"
        if name:
            symbols.append(
                _Symbol(
                    name=name,
                    sym_type=sym_type,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                )
            )

    for child in node.children:
        _walk_tree(child, source, symbols, lang)


def _get_node_name(node, source: bytes) -> Optional[str]:
    """Extract the identifier name from a named definition node."""
    for child in node.children:
        if child.type in ("identifier", "name", "type_identifier", "field_identifier"):
            return source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
    return None


_parser_cache: dict[str, object] = {}


def _get_parser(lang: str):
    if lang in _parser_cache:
        return _parser_cache[lang]

    try:
        import tree_sitter_python
        import tree_sitter_typescript
        import tree_sitter_javascript
        import tree_sitter_go
        import tree_sitter_rust
        from tree_sitter import Language, Parser

        lang_map = {
            "python": tree_sitter_python.language(),
            "typescript": tree_sitter_typescript.language_typescript(),
            "javascript": tree_sitter_javascript.language(),
            "go": tree_sitter_go.language(),
            "rust": tree_sitter_rust.language(),
        }

        if lang not in lang_map:
            return None

        parser = Parser(Language(lang_map[lang]))
        _parser_cache[lang] = parser
        return parser
    except (ImportError, AttributeError, Exception):
        return None


def _detect_language(file_path: Path) -> Optional[str]:
    ext_map = {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".js": "javascript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
    }
    return ext_map.get(file_path.suffix.lower())


# ---- Regex fallback ---------------------------------------------------------

_REGEX_PATTERNS: dict[str, list[tuple[str, str]]] = {
    ".py": [
        (r"^(?:async\s+)?def\s+(\w+)\s*\(", "function"),
        (r"^class\s+(\w+)\s*[:\(]", "class"),
    ],
    ".ts": [
        (r"(?:^|\s)(?:async\s+)?function\s+(\w+)\s*[\(<]", "function"),
        (r"(?:^|\s)(?:export\s+)?class\s+(\w+)[\s{<]", "class"),
        (r"(?:^|\s)(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(", "function"),
        (r"^\s+(?:async\s+)?(\w+)\s*\([^)]*\)\s*(?::\s*\w+\s*)?\{", "method"),
    ],
    ".tsx": [
        (r"(?:^|\s)(?:async\s+)?function\s+(\w+)\s*[\(<]", "function"),
        (r"(?:^|\s)(?:export\s+)?class\s+(\w+)[\s{<]", "class"),
    ],
    ".js": [
        (r"(?:^|\s)(?:async\s+)?function\s+(\w+)\s*\(", "function"),
        (r"(?:^|\s)class\s+(\w+)[\s{]", "class"),
    ],
    ".go": [
        (r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(", "function"),
    ],
    ".rs": [
        (r"^(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*[<\(]", "function"),
        (r"^(?:pub\s+)?struct\s+(\w+)[\s{<]", "class"),
    ],
}


def _extract_with_regex(file_path: Path) -> list[_Symbol]:
    patterns = _REGEX_PATTERNS.get(file_path.suffix.lower(), [])
    if not patterns:
        return []

    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    symbols: list[_Symbol] = []
    for i, line in enumerate(lines):
        for pattern, sym_type in patterns:
            m = re.search(pattern, line)
            if m:
                name = m.group(1)
                # Estimate end as start of next same-level definition or EOF
                symbols.append(
                    _Symbol(
                        name=name,
                        sym_type=sym_type,
                        line_start=i + 1,
                        line_end=len(lines),  # placeholder; refined below
                    )
                )
                break

    # Refine line_end: each symbol ends just before the next one starts
    for idx in range(len(symbols) - 1):
        symbols[idx].line_end = symbols[idx + 1].line_start - 1

    return symbols


def _find_enclosing_symbol(
    symbols: list[_Symbol],
    line_start: Optional[int],
    line_end: Optional[int],
) -> Optional[_Symbol]:
    """Find the tightest enclosing symbol for a given line range."""
    if line_start is None:
        return None

    ref_line = line_start
    best: Optional[_Symbol] = None
    best_size = float("inf")

    for sym in symbols:
        if sym.line_start <= ref_line <= sym.line_end:
            size = sym.line_end - sym.line_start
            if size < best_size:
                best = sym
                best_size = size

    return best
