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
| `last_commit`, `commits[]` | Commits this session's own Bash command made (see "What counts"); each entry records its repo |
| `deploys[]` | Bash commands that run a `deploy_patterns` match as a command (see "What counts") |
| `last_tool` | The tool name plus the Bash `description` or the file path. Never the command or its output. |
| `prompt_file`, `prompt_claimed`, `prompt_prev_status` | The prompt the first message said to run, and whether the tracker marked it `running` (see docs/command-center.md) |

### What counts

- **A commit** is credited only when THIS command made it. On PreToolUse the tracker notes HEAD of each repo the
  command may commit in: the target of a `git commit|merge|cherry-pick|revert|am` (after `-C` or the last `cd`), plus
  the `prompts.repo` when the command runs `command_center.py` (`set`, `claim`, `board-markers`, … commit there). On
  PostToolUse it credits the commits HEAD moved through (`--first-parent before..after`, at most 20, oldest first), so a
  fast-forward merge and two commits in one command both count. HEAD moved by another session or the auto-sync, a
  command that commits nothing, or a command with no PreToolUse snapshot credits nothing. One race is left: another
  session committing in the same repo during the same seconds as a commit of yours that failed.
- **A deploy** is a `deploy_patterns` match that starts in the command word of a simple command: commands are split on
  `; && || | & ( )` and newlines, heredoc bodies dropped, and env assignments, `sudo`, `env`, `nohup`, `exec`, `time`,
  `nice [-n N]`, `timeout N`, `flock [opts] <lock>`, `bash|sh <script>`, `bash|sh -c '<cmd>'` and `do`/`then`/… skipped.
  So `flock … scripts/restart_web.sh`, `cd x && scripts/restart_web.sh` and `systemctl restart <unit>` are deploys;
  `grep|cat|sed|tail … restart_web.sh`, `git show …`, `echo "… restart_web.sh"` and `systemctl status|show` are not.
  A restart started from inside a script or a heredoc (`python3 - <<EOF … subprocess.Popen([...restart_web.sh])`) is
  not seen either. Replayed over 541 transcript commands that matched the old way (10-31): 439 kept, 102 dropped, all
  reads, docs or heredoc text but that one.

Run `python3 scripts/sessions.py [--all]` or `/sessions` to see the table, newest first:
- A session that hasn't ended and hasn't been seen for 2 h shows as `stale?`.
- Ended sessions older than 24 h are hidden unless you pass `--all`.
- The `prompt` column shows the claimed prompt as `<module>/<ID>` (through `_moved.json` when it was filed
  under `Completed/` or `Pending/` since), else its file name.
- Sessions only appear once these hooks run, so a session started in another project is not listed.

## Stop gates

`stop_gate(payload)` runs from `stop.py`. Each gate fires **once per session** (`board_gate_blocked`, `prompt_gate_blocked`)
and their reasons are joined into one block decision. The board gate blocks when all of these hold:
- the session committed or merged into the configured repo's branch a commit that touches `code_paths`, or it ran a
  command matching `deploy_patterns`;
- the board file has no commit on that branch since the session started (from any author), and none among the
  session's own commits;
- `stop_hook_active` is false.

The prompt gate (`prompts.gate`) blocks when the session claimed a prompt, has commits or deploys, and the prompt's
frontmatter still says `running` under this session id (docs/command-center.md). A prompt moved since the claim is
found through `_moved.json`.

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
