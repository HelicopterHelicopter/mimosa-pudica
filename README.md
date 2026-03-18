# mimosa

Track which files your AI coding agents reference most — per repo, with staleness detection and git-blame-style attribution.

## What it does

As coding agents work on your projects, they constantly read files, grep for patterns, and reference code. **mimosa** indexes those sessions and answers:

- Which files does the agent reference most in the past N days?
- Which heavily-referenced files haven't been updated recently and may be outdated?
- What was the agent consulting when it made a specific git commit?
- Which functions/classes inside a file get referenced most often?

mimosa is **repo-local** — each repo stores its own data in `.mimosa/mimosa.db` (next to `.git/`), similar to how git itself works. Running `mimosa top` in `project-a` shows nothing about `project-b`.

## Supported agents

| Agent | Data source | Quality |
|---|---|---|
| **Claude Code** | `~/.claude/projects/*/sessions/*.jsonl` | Structured tool calls — exact file paths and line ranges |
| **OpenCode** | `~/.local/share/opencode/opencode.db` | Structured tool calls — exact file paths and line ranges |
| **Cursor** | `~/.cursor/projects/*/agent-transcripts/**/*.jsonl` | Heuristic extraction — Cursor transcripts do not expose raw tool calls |

## Installation

Requires Python 3.11+.

```bash
git clone https://github.com/HelicopterHelicopter/mimosa-pudica
cd mimosa-pudica
pip install -e .
```

> **Note:** The `mimosa` script is installed to `~/Library/Python/3.x/bin/` on macOS. Add it to your PATH:
> ```bash
> export PATH="$HOME/Library/Python/$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')/bin:$PATH"
> ```
> Add the above line to your `~/.zshrc` or `~/.bashrc` to make it permanent.

## Getting started

Run these once per repository you want to track:

```bash
cd your-project

# 1. Initialise — creates .mimosa/ and adds it to .gitignore
mimosa init

# 2. Index all agent sessions for this repo
mimosa index
```

Then explore:

```bash
# Top 20 most-referenced files (all time)
mimosa top

# Top files in the last 7 days
mimosa top --days 7

# Reference history for a specific file
mimosa show src/utils/api.py

# Find files the agent references a lot but that haven't been updated recently
mimosa stale

# Git-blame-style: what was the agent reading when it edited this file?
mimosa annotate src/services/auth.py

# Re-scan after new agent sessions
mimosa index
```

## Commands

### `mimosa init`

Creates `.mimosa/` in the repo root and adds it to `.gitignore`. Run once per repo.

```bash
mimosa init
```

---

### `mimosa index`

Scans all agent session logs, extracts file references, and stores them in `.mimosa/mimosa.db`. Only references to files within the current repo are kept.

```bash
mimosa index                        # All sources (Claude Code, OpenCode, Cursor)
mimosa index --source opencode      # One source only
mimosa index --reindex              # Force re-scan of already-indexed sessions
```

| Flag | Description |
|---|---|
| `--source` | `cursor`, `claude-code`, or `opencode` |
| `--reindex` | Clear and re-insert refs for already-seen sessions |

---

### `mimosa top`

Show the most-referenced files or functions.

```bash
mimosa top                          # Top 20 files, all time
mimosa top --days 7                 # Last 7 days
mimosa top --days 30 --limit 50     # Last 30 days, top 50
mimosa top --source opencode        # Filter to one source
mimosa top --exclude-writes         # Reads and greps only (ignore writes)
mimosa top --granularity function   # Function/class level breakdown
```

| Flag | Default | Description |
|---|---|---|
| `--days`, `-d` | all time | Restrict to the last N days |
| `--limit`, `-n` | 20 | Number of results |
| `--source` | all | `cursor`, `claude-code`, or `opencode` |
| `--granularity` | `file` | `file` or `function` |
| `--exclude-writes` | off | Exclude write-type references |

> **Function-level analysis** (`--granularity function`) requires references that include line ranges. These come from Claude Code and OpenCode `read` tool calls, and from Cursor code citation blocks. It also requires the referenced files to still exist on disk.

---

### `mimosa show FILE`

Full reference history for a single file.

```bash
mimosa show src/utils/api.py
mimosa show src/utils/api.py --days 14
mimosa show api.py                  # Partial name match works
```

| Flag | Default | Description |
|---|---|---|
| `--days`, `-d` | all time | Restrict to the last N days |
| `--limit`, `-n` | 50 | Max references to show |

---

### `mimosa stale`

Find files that are frequently referenced by agents but haven't been updated in git recently. These are good candidates for a review — the agent may be working from outdated information.

```bash
mimosa stale                        # Default: referenced ≥3× in 30 days, not updated in 30+ days
mimosa stale --min-refs 5           # Require at least 5 references
mimosa stale --days 60              # Wider reference window
mimosa stale --staleness-days 14    # Flag files not updated in 14+ days
```

| Flag | Default | Description |
|---|---|---|
| `--days`, `-d` | 30 | Reference window in days |
| `--min-refs` | 3 | Minimum reference count to be included |
| `--limit`, `-n` | 20 | Number of results |
| `--staleness-days` | 30 | Days since last git commit to consider stale |
| `--source` | all | Filter to one source |

Status labels: `deleted` (file no longer exists), `untracked` (not in git), `stale` (30–89 days), `very stale` (90+ days).

---

### `mimosa annotate FILE`

Git-blame-style view: for each commit that touched a file, show which other files the agent was referencing during the same session. Useful for understanding the agent's context when it made a change.

```bash
mimosa annotate src/services/auth.py
mimosa annotate src/services/auth.py --window 60   # Match sessions within ±60 min
mimosa annotate src/services/auth.py --limit 5     # Show last 5 commits only
```

| Flag | Default | Description |
|---|---|---|
| `--window` | 120 | Session match window in minutes around each commit |
| `--limit`, `-n` | 10 | Max commits to annotate |

> This command requires the file to have at least one git commit. Session matching uses timestamp proximity — commits made within `--window` minutes of a session's start time are linked to it.

---

### `mimosa config`

Show the active configuration for the current repo, or set a persistent override.

```bash
mimosa config                           # Show current config
mimosa config --set claude_code_base /custom/path
mimosa config --set opencode_db_path /path/to/opencode.db
```

Overridable settings:

| Key | Default | Description |
|---|---|---|
| `claude_code_base` | `~/.claude` | Root of Claude Code data |
| `cursor_base` | `~/.cursor` | Root of Cursor data |
| `opencode_db_path` | `~/.local/share/opencode/opencode.db` | OpenCode SQLite DB |

All settings can also be set via environment variables: `MIMOSA_CLAUDE_CODE_BASE`, `MIMOSA_CURSOR_BASE`, `MIMOSA_OPENCODE_DB`.

---

### Global flags

These work on any command:

```bash
mimosa --repo /path/to/other-repo top   # Run against a different repo
mimosa --version                        # Show version
```

## How data is stored

Each repo gets its own isolated database at `<repo-root>/.mimosa/mimosa.db`. `mimosa init` adds `.mimosa/` to your `.gitignore` automatically so it is never committed.

Global settings (agent tool paths) are stored at `~/.mimosa/settings.json` and apply across all repos.

```
your-project/
├── .git/
├── .mimosa/
│   └── mimosa.db       ← all reference data for this repo
├── .gitignore          ← .mimosa/ is added here by `mimosa init`
└── src/
    └── ...
```

## Data source details

### Claude Code

Parses `~/.claude/projects/*/sessions/*.jsonl`. Each line is a JSON record; `assistant` records contain `tool_use` blocks with structured tool calls:

| Tool | What is captured |
|---|---|
| `Read` | `file_path`, `offset` (line start), `limit` |
| `Grep` / `SemanticSearch` | `path` (directory searched), `pattern` |
| `Glob` | `target_directory`, `glob_pattern` |
| `Write` / `StrReplace` / `EditNotebook` | `file_path` |
| `Bash` / `Shell` | Heuristic file argument extraction from `command` |

Session files are scoped by project: mimosa looks for the encoded directory matching the current repo path before falling back to scanning all sessions.

### OpenCode

Reads directly from `~/.local/share/opencode/opencode.db` (SQLite). Sessions are matched to the current repo via `project.worktree`. File references are extracted from the `part` table's `data` JSON:

| Tool | What is captured |
|---|---|
| `read` | `filePath`, `startLine`, `endLine` |
| `write`, `edit` | `filePath` |
| `grep` | File paths parsed from output text |
| `glob`, `list` | File paths parsed from output text |
| `bash` | Heuristic file argument extraction from `command` |

### Cursor

Parses `~/.cursor/projects/*/agent-transcripts/**/*.jsonl`. Cursor transcripts do not record raw tool calls, so extraction is heuristic from message text:

| Pattern | What is captured |
|---|---|
| `<attached_files path="...">` | Explicitly attached file paths |
| `@/path:line-range` | User @ references |
| `` ```start:end:filepath `` | Code citation blocks in assistant responses |
| Backtick-quoted paths | `` `src/utils/api.ts` `` in assistant prose |
| Absolute paths in text | `/absolute/path/to/file.py` mentions |

Because extraction is heuristic, Cursor refs have lower precision than Claude Code or OpenCode, and will not contain line ranges (so function-level analysis is limited to code citation blocks).

## Typical workflow

```bash
# Once per repo
cd my-project
mimosa init

# After each agent session (or add to a cron job)
mimosa index

# Weekly review
mimosa top --days 7
mimosa stale --min-refs 3

# When investigating why a file looks the way it does
mimosa show src/core/router.py
mimosa annotate src/core/router.py
```
