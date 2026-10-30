# Prompt command center

`/sessions` shows the live Claude Code sessions. `/command-center` (alias `/cc`) shows the other half: every session
prompt per module, which are pending, which are running right now and by whom, which landed, which are deployed, and
which wait on the owner. The data is the prompt files themselves plus the session tracker.

## Layout

```
<repo>/<prompts.root>/                 e.g. docs/Prompts/
  README.md                            the board: intro + a generated block between <!-- status:begin/end -->
  _moved.json                          old path → new path for prompts moved in by `migrate`
  <module>/<Module>-00-README.md       the module index: free text + a generated status table between the markers
  <module>/<Module>-<ID>-<Title>.md    one prompt = one session; its status lives in frontmatter
  _batches/<date>-00-README.md         cross-module batch indexes moved in by `migrate`
```

Module = the folder. For a prompt in a legacy folder (`legacy_globs`), the module comes from the file-name prefix through
`module_aliases` (`Sock → security`, `F3 → engine`, …) and the id from the next token (`Funnels-B12-3-…` → `B12-3`,
`01-Sock-…` → `01`). A file name is never changed by the tools, so links and `find -name` keep working.

## Frontmatter

Flat `key: value` lines (YAML-compatible; lists as `[a, b]`; a value a YAML reader would mistype, such as a commit
`0123456` or `no`, is quoted). The body below the closing `---` is never touched. Keys, in the order they are written:

| key | meaning |
|---|---|
| `module`, `id`, `order` | folder, short id, sort key in the module table (defaults: folder, from the name, the id) |
| `status` | `pending` ⚪ · `running` ▶ · `on-branch` ◐ (built, not landed) · `on-main` 🔵 (landed, deploy held) · `deployed` 🟡 (live, proof open) · `done` 🟢 · `blocked` 🔴 (owner) · `skipped` (refuted / do not run) · `superseded` (`notes: by …`) |
| `needs_owner`, `cost`, `depends_on` | what the owner must decide, what the run costs, ids that must be done first |
| `session_id`, `commit`, `deployed`, `updated`, `notes` | who claimed it, the landing commit, when it went live, last change, one line |
| `todo: confirm` | written by `migrate` when it could not read the old README row: check the status by hand |

A file without frontmatter is `pending`. Progress = done + deployed over everything except skipped and superseded.
The view also derives two states: a `pending` prompt with a live session on it shows as `running`, and a `running`
prompt whose session is gone shows as `stalled`.

## Commands

```
python3 scripts/command_center.py                 # summary per module, then Running now / Stalled / Blocked on owner / Next up
python3 scripts/command_center.py --module M      # every prompt of one module (order, id, status, owner, session, commit, updated, title)
python3 scripts/command_center.py --all           # every module, including skipped and superseded
python3 scripts/command_center.py --json          # for scripts (ops checks)
python3 scripts/command_center.py --board         # rewrite the status block of every module README + the root board; prints what changed
python3 scripts/command_center.py --html          # write the HTML dashboard (prompts.html, default ~/.claude/session-tracker/command-center.html)
python3 scripts/command_center.py set <ID|module/ID|file> <status> [--commit SHA] [--deployed TS] [--notes TEXT] [--needs-owner TEXT] [--no-commit]
python3 scripts/command_center.py migrate [--module M] [--dry-run] [--force]
```

`set` updates the frontmatter, regenerates that module's table and the board, and commits those files with a
pathspec (`docs(prompts): <module> <ID> → <status>`), because an uncommitted edit in a shared tree blocks any later
merge that touches the file. `--no-commit` leaves the files for your own commit. "Next up" is the first `pending`
prompt by `order` whose `depends_on` are all done.

## What the hooks do

- **Claim.** On a session's first message, when it names a prompt file and starts with a claim verb (`run`, `execute`,
  `start`), the tracker marks that prompt `running` with the session id (and commits it with a pathspec when
  `commit_claims` is on). Only `pending`, `blocked` and `running`-with-a-dead-session prompts can be claimed; a prompt
  never moves backwards. A first message that merely mentions a prompt ("look at X.md") records `prompt_mentioned` and
  claims nothing. The claim costs about 60 ms once per session (113 prompts, 80 tracker files).
- **Gate.** With `prompts.gate` on, a Stop is blocked once per session when the session claimed a prompt, has commits
  or deploys, and the prompt is still `running` under its id. The message names the `set` command. It never blocks a
  session that only stopped to ask a question (no commits), and a read error lets the stop through (logged).

## Config

The `prompts` block of `~/.claude/session-tracker/config.json` (template: `.claude/hooks/utils/session_tracker.config.example.json`):

| key | meaning |
|---|---|
| `repo`, `root` | the project and the prompts folder inside it (required) |
| `legacy_globs` | folders still holding prompts not yet moved in (also what `migrate` moves) |
| `board`, `html` | the root board file; where `--html` writes |
| `claim_verbs`, `commit_claims`, `gate`, `message` | the claim and gate behaviour above |
| `module_aliases` | file-name prefix → module, for legacy names |
| `sync_lock`, `sync_every_hours` | an auto-commit daemon's lock file and cadence; `migrate` refuses to run near it |

## Migration

`migrate` is idempotent. It `git mv`s every prompt and module README out of each legacy folder into `<root>/<module>/`
(batch `00-README.md` files go to `_batches/`), records the moves in `_moved.json`, adds frontmatter to each moved
prompt from its module README's table row (✅ DEPLOYED/LIVE → `deployed`, ✅ DONE or bare ✅ → `done`, 🔵/ON MAIN/HELD →
`on-main`, refuted → `skipped`, the first backticked sha after "main" → `commit`), marks a bare-id twin of a
module-prefixed prompt `superseded`, and writes `pending` + `todo: confirm` where no row matched. It inserts the markers
above the old `## Order` table (kept, marked frozen), points the README's handoff rule at `set`, rewrites each prompt's
`Paste:` line and the README's `Run `…`` example to the new path, leaves a one-line redirect `README.md` in each old
folder, and creates the board if missing. Pre-flight refuses (override with `--force`) on uncommitted changes under the
old folders, on live sessions running prompts that would move, and near the auto-sync. It never commits: it prints the
pathspec commit command. Old paths keep resolving through `_moved.json` and by file name, so sessions started before
the move still match.

Tests: `python3 -m pytest tests -q` (pure; fixtures are synthetic).
