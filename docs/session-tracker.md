# Session tracker and Status Board stop gate

When you run several Claude Code sessions at once, `/sessions` shows each one's state, task, worktree and last commit.
A Stop hook also reminds a session that shipped code to update the shared status board before it finishes.

## Live session list

The hooks (`session_start`, `user_prompt_submit`, `pre_tool_use` for AskUserQuestion only, `permission_request`,
`post_tool_use`, `notification`, `stop`, `session_end`) each call `track(event, payload)` from
`.claude/hooks/utils/session_tracker.py`. It keeps one JSON file per session in `~/.claude/session-tracker/`, which you
can override with `SESSION_TRACKER_DIR`. Claude Code's own `~/.claude/sessions/` is never touched.

| Field | Comes from |
|---|---|
| `task` | The first `.md` path in the session's first prompt, else that prompt's first 80 characters |
| `state` | `working` (prompt or tool), `waiting_on_owner` (Notification, permission dialog, AskUserQuestion), `idle` (Stop), `ended` (SessionEnd) |
| `worktree` / `branch` | The last `git worktree add` or `cd` into a git checkout, else the payload's `cwd` |
| `last_commit`, `commits[]` | A Bash `git commit`/`merge`/`cherry-pick`/`revert`/`am` whose HEAD is new; each entry records its repo |
| `deploys[]` | Bash commands that match `deploy_patterns` in the config |
| `last_tool` | The tool name plus the Bash `description` or the file path. Never the command or its output. |

Run `python3 scripts/sessions.py [--all]` or `/sessions` to see the table, newest first:
- A session that hasn't ended and hasn't been seen for 2 h shows as `stale?`.
- Ended sessions older than 24 h are hidden unless you pass `--all`.
- Sessions only appear once these hooks run, so a session started in another project is not listed.

## Stop gate

`stop_gate(payload)` runs from `stop.py` and blocks the stop **once per session** with the configured message when all of these hold:
- the session committed or merged into the configured repo's branch a commit that touches `code_paths`, or it ran a
  command matching `deploy_patterns`;
- the board file has no commit on that branch since the session started (from any author), and none among the
  session's own commits;
- `stop_hook_active` is false.

It also copies `session_log.source` over `session_log.dest` when they differ, keeping `dest`'s leading `<!-- -->`
header. It never commits.

## Config

The config lives in `~/.claude/session-tracker/config.json`, or wherever `SESSION_TRACKER_CONFIG` points. Start from
`.claude/hooks/utils/session_tracker.config.example.json`. Project paths and unit names belong in that file, not in
this repo.

## Failure behaviour

Nothing here raises. Errors go to `~/.claude/session-tracker/tracker.log` and the hook exits 0, so a tracker bug never
blocks a tool call or a stop. A hook falls back to a no-op if the module cannot be imported. A git error or a missing
repo in the gate lets the stop through and is logged.

## Cost

Measured in-process, `track()` takes about 0.5 ms and the module adds about 5 ms of imports. git runs only on
SessionStart, on a move into a new checkout and on a commit. Each hook's wall time (100–250 ms) is mostly `uv run`
startup and varies with machine load.

Tests: `python3 -m pytest tests -q`.
