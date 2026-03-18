"""mimosa CLI entry point."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table
from rich import box
from rich.text import Text

from .config import Config, find_repo_root
from .db import Database
from .models import Source

console = Console()
err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Shared context helpers
# ---------------------------------------------------------------------------

def _resolve_repo(repo_override: Optional[str]) -> tuple[Optional[Path], Config]:
    """Find repo root and build a Config. Exits with a helpful message if the
    command requires a repo but none is found."""
    if repo_override:
        repo_root = Path(repo_override).resolve()
    else:
        repo_root = find_repo_root()
    return repo_root, Config(repo_root=repo_root)


def _require_repo(repo_override: Optional[str]) -> tuple[Path, Config]:
    """Like _resolve_repo but aborts if no repo root is found."""
    repo_root, config = _resolve_repo(repo_override)
    if repo_root is None:
        console.print(
            "[red]Error:[/red] not inside a git repository.\n"
            "Run [bold]mimosa[/bold] from within a repo, or use "
            "[bold]--repo PATH[/bold] to specify one.\n"
            "If this is a new repo, run [bold]git init[/bold] first."
        )
        raise SystemExit(1)
    return repo_root, config


def _get_db(config: Config) -> Database:
    config.mimosa_dir.mkdir(parents=True, exist_ok=True)
    return Database(config.db_path)


_SOURCE_CHOICES = ["cursor", "claude-code", "opencode"]


def _source_opt(source_str: Optional[str]) -> Optional[Source]:
    if source_str is None:
        return None
    try:
        return Source(source_str)
    except ValueError:
        raise click.BadParameter(
            f"Unknown source '{source_str}'. Choose: {', '.join(_SOURCE_CHOICES)}"
        )


def _repo_header(repo_root: Path) -> str:
    return f"[dim]repo:[/dim] [bold cyan]{repo_root}[/bold cyan]"


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option("0.1.0", prog_name="mimosa")
@click.option(
    "--repo",
    default=None,
    metavar="PATH",
    help="Override the repo root (default: walk up from cwd to find .git).",
    is_eager=True,
)
@click.pass_context
def cli(ctx: click.Context, repo: Optional[str]) -> None:
    """Track which files your AI coding agents reference most.

    mimosa is repo-aware: it stores data in <repo>/.mimosa/ and only tracks
    files belonging to that repo. Run 'mimosa init' once per repo, then
    'mimosa index' to scan agent sessions.
    """
    ctx.ensure_object(dict)
    ctx.obj["repo"] = repo


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def init(ctx: click.Context) -> None:
    """Initialise mimosa for the current repo.

    Creates a .mimosa/ directory at the repo root and adds it to .gitignore.
    You only need to run this once per repository.
    """
    repo_root, config = _resolve_repo(ctx.obj.get("repo"))
    if repo_root is None:
        console.print(
            "[red]Error:[/red] not inside a git repository.\n"
            "Run [bold]git init[/bold] first, then re-run [bold]mimosa init[/bold]."
        )
        raise SystemExit(1)

    ok, msg = config.init_repo()
    if ok:
        console.print(
            Panel(
                f"{msg}\n\n"
                f"[dim]DB will be stored at:[/dim] {config.db_path}\n\n"
                "Next step: run [bold]mimosa index[/bold] to scan agent sessions.",
                title="mimosa initialised",
                border_style="green",
            )
        )
    else:
        console.print(f"[yellow]{msg}[/yellow]")


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

@cli.command()
@click.option(
    "--source",
    type=click.Choice(_SOURCE_CHOICES),
    default=None,
    help="Limit indexing to one source (default: all).",
)
@click.option(
    "--reindex",
    is_flag=True,
    default=False,
    help="Re-index already-seen sessions (clears and re-inserts their refs).",
)
@click.pass_context
def index(ctx: click.Context, source: Optional[str], reindex: bool) -> None:
    """Scan agent session logs and store file references in the repo database.

    Only references to files within this repo are recorded. The database is
    stored at <repo>/.mimosa/mimosa.db.

    Supported sources: Cursor, Claude Code, OpenCode.
    """
    repo_root, config = _require_repo(ctx.obj.get("repo"))
    config.init_repo()  # ensure .mimosa/ exists
    db = _get_db(config)

    from .parsers.claude_code import ClaudeCodeParser
    from .parsers.cursor import CursorParser
    from .parsers.opencode import OpenCodeParser

    sources_to_run: list[str] = (
        _SOURCE_CHOICES if source is None else [source]
    )

    console.print(_repo_header(repo_root))

    total_sessions = 0
    total_refs = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=True,
    ) as progress:
        if "claude-code" in sources_to_run:
            files = list(config.claude_code_session_files())
            task = progress.add_task(
                f"[cyan]Claude Code[/cyan] – scanning {len(files)} sessions…",
                total=len(files),
            )
            parser = ClaudeCodeParser(config)
            for sf in files:
                progress.advance(task)
                session_id = sf.stem
                if not reindex and db.session_exists(session_id):
                    continue
                try:
                    session, refs = parser.parse(sf, repo_root=repo_root)
                except Exception as exc:
                    err_console.print(f"[yellow]Warning:[/yellow] {sf.name}: {exc}")
                    continue
                if not refs and not reindex:
                    continue
                with db.transaction():
                    if reindex:
                        db.delete_session_refs(session_id)
                    db.upsert_session(session)
                    db.insert_refs(refs)
                total_sessions += 1
                total_refs += len(refs)

        if "cursor" in sources_to_run:
            files = list(config.cursor_transcript_files())
            task = progress.add_task(
                f"[magenta]Cursor[/magenta] – scanning {len(files)} transcripts…",
                total=len(files),
            )
            parser_c = CursorParser(config)
            for sf in files:
                progress.advance(task)
                session_id = sf.stem
                if not reindex and db.session_exists(session_id):
                    continue
                try:
                    session, refs = parser_c.parse(sf, repo_root=repo_root)
                except Exception as exc:
                    err_console.print(f"[yellow]Warning:[/yellow] {sf.name}: {exc}")
                    continue
                if not refs and not reindex:
                    continue
                with db.transaction():
                    if reindex:
                        db.delete_session_refs(session_id)
                    db.upsert_session(session)
                    db.insert_refs(refs)
                total_sessions += 1
                total_refs += len(refs)

        if "opencode" in sources_to_run:
            parser_oc = OpenCodeParser(config)
            if not parser_oc.is_available():
                if source == "opencode":
                    err_console.print(
                        f"[yellow]OpenCode DB not found:[/yellow] {config.opencode_db_path}\n"
                        "[dim]Install OpenCode or set MIMOSA_OPENCODE_DB to point to your DB.[/dim]"
                    )
            else:
                oc_sessions = parser_oc.sessions_for_repo(repo_root)
                task = progress.add_task(
                    f"[yellow]OpenCode[/yellow] – scanning {len(oc_sessions)} sessions…",
                    total=len(oc_sessions),
                )
                for sess_row in oc_sessions:
                    progress.advance(task)
                    session_id = sess_row["id"]
                    if not reindex and db.session_exists(session_id):
                        continue
                    try:
                        session, refs = parser_oc.parse_session(sess_row, repo_root=repo_root)
                    except Exception as exc:
                        err_console.print(f"[yellow]Warning:[/yellow] {session_id[:12]}: {exc}")
                        continue
                    if not refs and not reindex:
                        continue
                    with db.transaction():
                        if reindex:
                            db.delete_session_refs(session_id)
                        db.upsert_session(session)
                        db.insert_refs(refs)
                    total_sessions += 1
                    total_refs += len(refs)

    stats = db.summary_stats()
    console.print(
        Panel(
            f"[bold green]Indexed[/bold green] {total_sessions} new sessions, "
            f"{total_refs} new references.\n"
            f"Totals: [bold]{stats.get('total_sessions', 0)}[/bold] sessions · "
            f"[bold]{stats.get('unique_files', 0)}[/bold] files · "
            f"[bold]{stats.get('total_refs', 0)}[/bold] refs\n"
            f"[dim]{config.db_path}[/dim]",
            title="mimosa index complete",
            border_style="green",
        )
    )


# ---------------------------------------------------------------------------
# top
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--days", "-d", default=None, type=int, help="Restrict to last N days.")
@click.option("--limit", "-n", default=20, show_default=True, help="Number of results.")
@click.option(
    "--source",
    type=click.Choice(_SOURCE_CHOICES),
    default=None,
    help="Filter by source.",
)
@click.option(
    "--granularity",
    type=click.Choice(["file", "function"]),
    default="file",
    show_default=True,
    help="Show results at file or function level.",
)
@click.option(
    "--exclude-writes",
    is_flag=True,
    default=False,
    help="Exclude write-type references (only count reads/greps/mentions).",
)
@click.pass_context
def top(
    ctx: click.Context,
    days: Optional[int],
    limit: int,
    source: Optional[str],
    granularity: str,
    exclude_writes: bool,
) -> None:
    """Show the most-referenced files (or functions) by AI agents."""
    repo_root, config = _require_repo(ctx.obj.get("repo"))
    db = _get_db(config)
    src = _source_opt(source)
    period = f"last {days} days" if days else "all time"

    console.print(_repo_header(repo_root))

    if granularity == "function":
        _show_top_functions(db, days=days, limit=limit, source=source, period=period)
        return

    from .analyzers.ranking import top_files
    file_stats = top_files(
        db,
        days=days,
        limit=limit,
        source=src,
        exclude_writes=exclude_writes,
    )

    if not file_stats:
        console.print(
            "[yellow]No references found.[/yellow] "
            "Run [bold]mimosa index[/bold] to scan agent sessions."
        )
        return

    table = Table(
        title=f"Most-Referenced Files  [{period}]",
        box=box.ROUNDED,
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("File", style="bold", overflow="fold")
    table.add_column("Refs", justify="right", style="green")
    table.add_column("Sessions", justify="right", style="yellow")
    table.add_column("Types", style="dim")
    table.add_column("Last Seen", style="dim")
    table.add_column("Sources", style="dim")

    for i, fs in enumerate(file_stats, 1):
        table.add_row(
            str(i),
            fs.file_path,
            str(fs.ref_count),
            str(fs.session_count),
            _format_ref_types(fs.ref_types),
            _fmt_dt(fs.last_seen),
            ", ".join(sorted(set(fs.sources))),
        )

    console.print(table)


def _show_top_functions(
    db: Database,
    *,
    days: Optional[int],
    limit: int,
    source: Optional[str],
    period: str,
) -> None:
    from .analyzers.functions import top_functions

    with console.status("Analysing function-level references…"):
        func_refs = top_functions(
            db,
            days=days,
            limit=limit,
            source_filter=source,
        )

    if not func_refs:
        console.print("[yellow]No function-level references found.[/yellow]")
        console.print(
            "[dim]Tip: function-level analysis requires refs with line ranges "
            "(Claude Code Read/Grep, or Cursor code-citation blocks).[/dim]"
        )
        return

    table = Table(
        title=f"Most-Referenced Functions/Classes  [{period}]",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("File", overflow="fold")
    table.add_column("Symbol", style="bold")
    table.add_column("Type", style="dim")
    table.add_column("Lines", style="dim", justify="right")
    table.add_column("Refs", justify="right", style="green")
    table.add_column("Sessions", justify="right", style="yellow")

    for i, fr in enumerate(func_refs, 1):
        table.add_row(
            str(i),
            fr.file_path,
            fr.symbol_name,
            fr.symbol_type,
            f"{fr.line_start}–{fr.line_end}",
            str(fr.ref_count),
            str(fr.session_count),
        )

    console.print(table)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("file_path")
@click.option("--days", "-d", default=None, type=int, help="Restrict to last N days.")
@click.option("--limit", "-n", default=50, show_default=True, help="Max refs to show.")
@click.pass_context
def show(ctx: click.Context, file_path: str, days: Optional[int], limit: int) -> None:
    """Show reference history for a specific file.

    FILE_PATH can be relative to the repo root (e.g. src/utils/api.py) or
    an absolute path.
    """
    repo_root, config = _require_repo(ctx.obj.get("repo"))
    db = _get_db(config)

    # Normalise to repo-relative
    target = _to_repo_relative(file_path, repo_root)
    history = db.file_history(target, days=days, limit=limit)

    # Partial match fallback
    if not history:
        all_stats = db.top_files(limit=5000)
        candidates = [fs.file_path for fs in all_stats if file_path in fs.file_path]
        if candidates:
            target = candidates[0]
            history = db.file_history(target, days=days, limit=limit)
            console.print(f"[dim]Resolved → [bold]{target}[/bold][/dim]\n")

    if not history:
        console.print(
            f"[yellow]No references found for[/yellow] [bold]{file_path}[/bold]\n"
            "[dim]Tip: paths are stored relative to the repo root.[/dim]"
        )
        return

    period = f"last {days} days" if days else "all time"
    console.print(_repo_header(repo_root))
    console.print(
        Panel(
            f"[bold]{target}[/bold]  ·  "
            f"[green]{len(history)}[/green] references ({period})",
            border_style="cyan",
        )
    )

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("Time", style="dim", width=20)
    table.add_column("Type", width=8)
    table.add_column("Tool", style="dim", width=18)
    table.add_column("Lines", justify="right", style="dim", width=12)
    table.add_column("Source", style="dim", width=12)
    table.add_column("Session", style="dim")
    table.add_column("Context", overflow="fold", style="dim")

    for ref in history:
        ref_type = ref.get("ref_type", "")
        color = _ref_type_color(ref_type)
        ls, le = ref.get("line_start"), ref.get("line_end")
        lines_str = (f"{ls}–{le}" if le else str(ls)) if ls else ""
        table.add_row(
            _fmt_dt(ref.get("timestamp")),
            Text(ref_type, style=color),
            ref.get("tool_name") or "",
            lines_str,
            ref.get("source") or "",
            (ref.get("session_id") or "")[:12],
            (ref.get("context") or "")[:60],
        )

    console.print(table)


# ---------------------------------------------------------------------------
# stale
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--days", "-d", default=30, show_default=True, help="Reference window in days.")
@click.option("--min-refs", default=3, show_default=True, help="Minimum reference count.")
@click.option("--limit", "-n", default=20, show_default=True, help="Number of results.")
@click.option(
    "--source",
    type=click.Choice(_SOURCE_CHOICES),
    default=None,
)
@click.option(
    "--staleness-days",
    default=30,
    show_default=True,
    help="Days since last git commit to consider a file stale.",
)
@click.pass_context
def stale(
    ctx: click.Context,
    days: int,
    min_refs: int,
    limit: int,
    source: Optional[str],
    staleness_days: int,
) -> None:
    """Find heavily-referenced files that haven't been updated recently in git."""
    repo_root, config = _require_repo(ctx.obj.get("repo"))
    db = _get_db(config)
    src = _source_opt(source)

    console.print(_repo_header(repo_root))

    with console.status("Checking git history for stale files…"):
        from .analyzers.staleness import get_stale_files
        stale_files = get_stale_files(
            db,
            days=days,
            min_refs=min_refs,
            limit=limit,
            source=src,
            staleness_days=staleness_days,
            repo_root=repo_root,
        )

    if not stale_files:
        console.print(
            "[green]No stale files found.[/green] "
            "All heavily-referenced files appear to be recently updated."
        )
        return

    table = Table(
        title=(
            f"Stale Files  "
            f"[referenced ≥{min_refs}× in last {days}d · "
            f"not updated in {staleness_days}+ days]"
        ),
        box=box.ROUNDED,
        header_style="bold red",
    )
    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("File", overflow="fold")
    table.add_column("Refs", justify="right", style="green")
    table.add_column("Sessions", justify="right", style="yellow")
    table.add_column("Last Ref", style="dim")
    table.add_column("Last Git Update", style="dim")
    table.add_column("Days Stale", justify="right")
    table.add_column("Status")

    for i, sf in enumerate(stale_files, 1):
        days_str = str(sf.days_since_update) if sf.days_since_update is not None else "—"
        table.add_row(
            str(i),
            sf.file_path,
            str(sf.ref_count),
            str(sf.session_count),
            _fmt_dt(sf.last_referenced),
            _fmt_dt(sf.last_git_updated),
            days_str,
            _stale_status(sf),
        )

    console.print(table)
    console.print(
        "\n[dim]Run [bold]mimosa show <file>[/bold] for the full reference history.[/dim]"
    )


def _stale_status(sf) -> Text:
    if not sf.exists_on_disk:
        return Text("deleted", style="bold red")
    if sf.days_since_update is None:
        return Text("untracked", style="yellow")
    if sf.days_since_update >= 90:
        return Text("very stale", style="bold red")
    if sf.days_since_update >= 30:
        return Text("stale", style="red")
    return Text("recently updated", style="green")


# ---------------------------------------------------------------------------
# annotate
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("file_path")
@click.option(
    "--window",
    default=120,
    show_default=True,
    help="Session match window in minutes around each commit.",
)
@click.option("--limit", "-n", default=10, show_default=True, help="Max commits to show.")
@click.pass_context
def annotate(ctx: click.Context, file_path: str, window: int, limit: int) -> None:
    """Show which files the agent consulted when each commit was made.

    Uses git blame to identify commits, then matches commit timestamps to
    indexed agent sessions to find what files were being referenced.
    """
    repo_root, config = _require_repo(ctx.obj.get("repo"))
    db = _get_db(config)

    # Resolve to absolute path for git blame
    target_rel = _to_repo_relative(file_path, repo_root)
    target_abs = str(repo_root / target_rel)

    if not Path(target_abs).exists():
        console.print(f"[yellow]File not found:[/yellow] {target_abs}")
        return

    console.print(_repo_header(repo_root))

    with console.status(f"Running git blame on {Path(target_abs).name}…"):
        from .git.annotate import annotate_file
        annotations = annotate_file(db, target_abs, session_window_minutes=window)

    if not annotations:
        console.print(
            f"[yellow]No git blame data for[/yellow] [bold]{target_rel}[/bold].\n"
            "[dim]The file must have at least one commit.[/dim]"
        )
        return

    annotations = annotations[:limit]

    console.print(
        Panel(
            f"[bold]{target_rel}[/bold]  ·  "
            f"[green]{len(annotations)}[/green] commits annotated  "
            f"[dim](±{window} min window)[/dim]",
            title="Agent Annotation",
            border_style="blue",
        )
    )

    for ann in annotations:
        header = (
            f"[bold]{ann.commit_hash}[/bold]  "
            f"[dim]{_fmt_dt(ann.commit_time)}[/dim]  "
            f"{ann.author or ''}  "
            f"[italic]{ann.summary or ''}[/italic]"
        )
        meta = (
            f"[dim]lines {ann.line_range[0]}–{ann.line_range[1]}[/dim]  "
            + (
                f"[dim]session {ann.session_id[:12]}[/dim]"
                if ann.session_id
                else "[dim]no session matched[/dim]"
            )
        )

        if ann.referenced_files:
            refs_text = "\n".join(
                f"  [cyan]→[/cyan] {fp}" for fp in ann.referenced_files[:15]
            )
            if len(ann.referenced_files) > 15:
                refs_text += f"\n  [dim]… and {len(ann.referenced_files) - 15} more[/dim]"
        else:
            refs_text = "  [dim](no session refs found)[/dim]"

        console.print(Panel(f"{header}\n{meta}\n\n{refs_text}", border_style="dim"))
        console.print()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@cli.command("config")
@click.option("--set", "set_kv", nargs=2, metavar="KEY VALUE", help="Set a config value.")
@click.pass_context
def config_cmd(ctx: click.Context, set_kv: Optional[tuple[str, str]]) -> None:
    """Show or set mimosa configuration."""
    repo_root, config = _resolve_repo(ctx.obj.get("repo"))

    if set_kv:
        key, value = set_kv
        config.set(key, value)
        console.print(f"[green]Set[/green] {key} = {value}")
        return

    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("Key", style="bold cyan")
    table.add_column("Value")
    for k, v in config.as_dict().items():
        table.add_row(k, v)

    console.print(Panel(table, title="mimosa configuration", border_style="cyan"))

    try:
        db = _get_db(config)
        stats = db.summary_stats()
        console.print(
            f"\n[dim]Database:[/dim] "
            f"{stats.get('total_sessions', 0)} sessions · "
            f"{stats.get('unique_files', 0)} files · "
            f"{stats.get('total_refs', 0)} refs"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_repo_relative(path: str, repo_root: Path) -> str:
    """Convert a path to repo-relative form. Accepts absolute or relative input."""
    p = Path(path)
    if p.is_absolute():
        try:
            return str(p.relative_to(repo_root))
        except ValueError:
            return path
    # Handle paths like ./src/foo.py
    return str(p)


def _fmt_dt(dt) -> str:
    if dt is None:
        return ""
    if isinstance(dt, str):
        from .models import _parse_dt
        dt = _parse_dt(dt)
    if dt is None:
        return ""
    now = datetime.utcnow()
    delta = now - dt
    if delta.days == 0:
        h = delta.seconds // 3600
        return f"{h}h ago" if h else "just now"
    if delta.days < 7:
        return f"{delta.days}d ago"
    if delta.days < 365:
        return dt.strftime("%b %d")
    return dt.strftime("%b %d %Y")


def _format_ref_types(ref_types: dict[str, int]) -> str:
    order = ["read", "grep", "glob", "write", "bash", "mention"]
    parts = [f"{rt}:{ref_types[rt]}" for rt in order if rt in ref_types]
    return " ".join(parts)


def _ref_type_color(ref_type: str) -> str:
    return {
        "read": "cyan",
        "grep": "yellow",
        "glob": "blue",
        "write": "red",
        "bash": "magenta",
        "mention": "dim",
    }.get(ref_type, "white")
