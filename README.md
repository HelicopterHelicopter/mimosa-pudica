# mimosa

Track which files your AI coding agents reference most. Supports Cursor and Claude Code.

## What it does

As coding agents (Cursor, Claude Code) work on your projects, they read files, grep for patterns, and reference code constantly. **mimosa** indexes those sessions and tells you:

- Which files are referenced most in the past N days
- Which frequently-referenced files haven't been updated recently (staleness detection)
- What files an agent was consulting when it made a specific change (git-blame-style)
- Which functions/classes within a file are most often referenced (function-level granularity)

## Installation

```bash
pip install -e .
```

## Quick start

```bash
# Index your agent sessions (discovers Cursor + Claude Code automatically)
mimosa index

# Show top 20 most-referenced files in the past 7 days
mimosa top --days 7

# Show full reference history for a specific file
mimosa show src/utils/api.py

# Find files referenced often but not updated recently
mimosa stale --days 30 --min-refs 5

# Git-blame-style: which files did the agent consult when editing this file?
mimosa annotate src/services/auth.py

# Show most-referenced functions across all files
mimosa top --granularity function
```

## Data sources

### Claude Code
Parses `~/.claude/projects/*/sessions/*.jsonl`. Captures structured tool calls:
- **Read** — file reads with exact line ranges
- **Grep** — pattern searches with target paths
- **Glob** — file discovery patterns
- **Write** — files modified by the agent
- **Bash** — shell commands with file arguments

### Cursor
Parses `~/.cursor/projects/*/agent-transcripts/**/*.jsonl`. Cursor transcripts do not expose raw tool calls, so mimosa extracts references heuristically:
- `<attached_files>` XML blocks with `path=` attributes
- `@/path:line-range` user references
- Backtick-quoted file paths in assistant responses
- Code citation blocks (`` ```start:end:filepath ``)

### OpenCode
Reads directly from `~/.local/share/opencode/opencode.db` (SQLite). Captures structured tool calls with full file paths:
- **read** — exact file reads, with optional line ranges
- **write / edit** — files modified by the agent
- **grep** — file paths extracted from grep output
- **glob / list** — file paths extracted from glob output
- **bash** — heuristic file argument extraction from commands

## Commands

| Command | Description |
|---------|-------------|
| `mimosa index` | Scan and index agent session logs |
| `mimosa top` | Most-referenced files/functions |
| `mimosa show FILE` | Reference history for one file |
| `mimosa stale` | Frequently-referenced but stale files |
| `mimosa annotate FILE` | Agent context for each line change |
| `mimosa config` | View/set configuration |

## Options

```
mimosa index --source cursor          # Index only Cursor sessions
mimosa index --source claude-code     # Index only Claude Code sessions
mimosa index --source opencode        # Index only OpenCode sessions
mimosa index --project /path/to/repo  # Restrict to a specific project

mimosa top --days 7                   # Last 7 days
mimosa top --limit 30                 # Show top 30 files
mimosa top --source claude-code       # From a specific source
mimosa top --granularity function     # Function-level breakdown

mimosa stale --days 30 --min-refs 5   # Stale threshold + min reference count
```

## Database

mimosa stores all data in `~/.mimosa/mimosa.db` (SQLite). Run `mimosa config` to see the path.
