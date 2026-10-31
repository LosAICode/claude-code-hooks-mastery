# Prompt command center

`/sessions` shows the live Claude Code sessions. `/command-center` (alias `/cc`) shows the other half: every session
prompt per module, which are pending, which are running right now and by whom, which landed, which are deployed, and
which wait on the owner. The data is the prompt files themselves plus the session tracker.

## Layout

```
<repo>/<prompts.root>/                 e.g. docs/Prompts/
  README.md                            the board: intro + a generated block between <!-- status:begin/end -->
  _moved.json                          old path → new path for every prompt moved (`migrate`, `set`, `sort`), one hop
  <Module>/<Module>-00-README.md       the module index: free text + a generated status table between the markers
  <Module>/<Module>-<ID>-<Title>.md    one prompt = one session; its status lives in frontmatter
  <Module>/Pending/…  <Module>/Completed/…   with `status_folders` on: open prompts / done, skipped, superseded
  _batches/<date>-00-README.md         cross-module batch indexes moved in by `migrate`
```

Module = the folder, case-folded and passed through `module_aliases` (`Organization/` → `org-roles`), including one level
of sub-folders (`funnels/Completed/B1.md` is still a funnels prompt). For a prompt in a legacy folder (`legacy_globs`),
the module comes from the file-name prefix through `module_aliases` (`Sock → security`, `F3 → engine`, …) and the id from
the next token (`Funnels-B12-3-…` → `B12-3`, `01-Sock-…` → `01`). A file name is never changed by the tools, so links and
`find -name` keep working.

The same prompt saved twice counts once: a legacy copy, a `Funnels/` twin of `funnels/`, or a dated copy
(`…-Standard-10-28.md` next to `…-Standard.md`). The copy with frontmatter wins, then a root copy over a legacy one, then
the lower-case folder, then the shallower one; `set` edits that copy.

**Status folders.** A prompt filed under a `Completed/` sub-folder reads as `done` unless its frontmatter says `done`,
`skipped` or `superseded` (someone can file a finished prompt by hand). `Pending/` overrides nothing. With
`status_folders: true`, `set` also `git mv`s the prompt into `<Module>/Completed/` (done, skipped, superseded) or
`<Module>/Pending/` (every other status), records the move in `_moved.json` and commits old path, new path and the map
together. `sort` does the same for every prompt at once, leaving a `running` prompt whose session is live where it is
(its session's stored path stays valid until its own `set`). The Stop gate follows `_moved.json`, so a moved prompt still
gets its reminder. Tools find a module's folder through `module_folder` (case-folded name or alias; a capitalized
twin wins), so `Agents/` serves the `agents` module and nothing recreates a lower-case folder. Two different prompts that share an id but not a
name (`Comms-4-Consent-…` and `Comms-4-Verify-…`) are both kept.

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
| `issue` | the prompt's GitHub issue (an int, a quoted number or a list); one issue per prompt |

A file without frontmatter is `pending`. Progress = done + deployed over everything except skipped and superseded.
The view also derives two states: a `pending` prompt with a live session on it shows as `running`, and a `running`
prompt whose session is gone shows as `stalled`.

## Commands

```
python3 scripts/command_center.py                 # summary per module, then Running now / Other live sessions / Stalled / Blocked on owner / Next up
python3 scripts/command_center.py --module M      # every prompt of one module (order, id, status, owner, session, commit, updated, title)
python3 scripts/command_center.py --all           # every module, including skipped and superseded
python3 scripts/command_center.py --json          # for scripts (ops checks); --json --prompts adds every prompt with its frontmatter
python3 scripts/command_center.py --board         # rewrite the status block of every module README + the root board; prints what changed
python3 scripts/command_center.py --html          # write the HTML dashboard (prompts.html, default ~/.claude/session-tracker/command-center.html)
python3 scripts/command_center.py set <ID|module/ID|file> <status> [--commit SHA] [--deployed TS] [--notes TEXT] [--needs-owner TEXT]
                                  [--clear KEY]… [--comment] [--close] [--no-commit]
python3 scripts/command_center.py claim <ref> --session SID [--no-commit]   # attach a live session by hand (running + session_id + its Stop gate)
python3 scripts/command_center.py deploy-check [--module M] [--json] [--apply [--no-commit]]
python3 scripts/command_center.py issues [--module M] [--fix [--no-commit]]
python3 scripts/command_center.py audit [--module M]      # exit 1 when it finds something
python3 scripts/command_center.py board-check             # the hand-written Status Board vs the prompts; exit 1 on a mismatch
python3 scripts/command_center.py board-markers [--no-commit]   # once: a generated Prompts: line in each module's board row
python3 scripts/command_center.py sort [--dry-run] [--no-commit]   # file every prompt under Completed/ or Pending/
python3 scripts/command_center.py migrate [--module M] [--dry-run] [--force]
```

`--json` lists `running`, `stalled` and `blocked` prompts with `updated`, `session_id` and `session_live`, so a check can
age a stalled prompt or spot a blocked one whose session is gone. `other_sessions` (and a card in `--html`) holds the
live sessions "Running now" leaves out, as in the terminal view: `{prompt, session}` text, newest first.

`set` updates the frontmatter, regenerates that module's table and the board, and commits those files with a
pathspec (`docs(prompts): <module> <ID> → <status>`), because an uncommitted edit in a shared tree blocks any later
merge that touches the file. Setting a prompt `pending` or `blocked` also releases it from every session that held
it (its tracker `prompt_file` is cleared and the prompt goes into `released_prompts`), so the view stops showing it
running; a later claim takes it back. The header counts live sessions too: "Other live sessions" lists each one that
Running now leaves out (its prompt is on-branch, done, …, or it has none). `--no-commit` leaves the files for your own commit. `--needs-owner ""` and `--clear KEY`
remove a key (never `status` or `updated`). `--comment` posts one comment to the prompt's issue (`<module> <ID>: <old> →
<new> · commit · deployed · notes`); `--close` closes the issue with that comment and works only with `done`,
`skipped` or `superseded`. "Next up" is the first `pending` prompt per module by `order` whose `depends_on` are all done;
prompts that need nothing from the owner come first, the others are marked `⚑ needs owner: …`.

`claim` attaches a session that is live in the tracker (full id or a unique prefix) to a prompt: it writes `running` and
`session_id`, records the claim in the session's tracker file so its Stop gate applies, and commits. It refuses a gone
session, a prompt another live session holds, and anything past `blocked` (a prompt never moves backwards).

**`deploy-check`** answers "is it live?" per unit. For each `on-main` and `deployed` prompt it reads the commits in
`commit:`, finds where each landed on main (the first first-parent commit that contains it: a branch commit lands at
its merge, not at its author time), lists the files that landing brought, and maps them to units with the project's
map (`unit_map`; NeuroGenServer `scripts/deploy_units.json`, which `ops_drift.py` reads too: one map, owner decision
10-30). A file needs the units of every rule it matches; a `live_at_merge` rule (static files) is live when it lands;
a file no rule matches needs no unit. A unit counts when it started (`systemctl show`, read-only) after the latest
landing that needs it. States: **live** (every needed unit), **half** (some), **waiting** (none), **no-unit** (docs,
tests, scripts), **unchecked** (no commit, or a commit not on main, e.g. another repo's). `--apply` sets an `on-main`
prompt that is live to `deployed` with `deployed:` = the last needed unit's start, and commits; it never touches a
`deployed` prompt and never moves one back. A start time is the unit's latest start, so `live since` can be later than
the real moment. `--json` is what `ops_drift.py` reads for `drift-prompt-half-live-<module>-<id>` (half live > 24 h).

**`issues`** reads `gh issue list --state all` and lists: prompts with no issue (with the issues whose title names
them), issues that name a prompt whose `issue:` doesn't list them, prompts named by several issues, state mismatches
(a done/skipped/superseded prompt with an open issue; a pending/running/blocked/on-branch one with a closed issue)
and `issue:` numbers GitHub doesn't have. An issue names a prompt by its body's `Prompt: <path>` line (old paths
resolve through `_moved.json`), else by its title, `[Module] ID: …` (`[API Connections] R7:` → connections R7).
`--fix` writes `issue:` only for a prompt without one that exactly one issue names. Nothing here creates an issue:
creating them is outward-facing, so the owner OKs the list first.

**`audit`** flags `on-main`/`deployed`/`done` with no commit, a `todo:` left over, a file whose `Pending/` or
`Completed/` folder disagrees with its status, a prompt with no id (so `set <module>/<ID>` can't reach it), an id two
live prompts share, and a lower-case module folder holding prompts (module folders are capitalized).

**`board-check`** reads the hand-written board (`status_board`) and, for each table row whose first cell names a
module, splits its State cell into clauses (at `·`, `;`, `|`, parentheses; code spans dropped). A clause that names
a prompt id with only HELD/ON MAIN/merged/🔵 words, while the prompt is `deployed`/`done`, is flagged, and so is one
with only DEPLOYED/LIVE/🟢/🟡 words while the prompt is not live yet. Ranges (`H1-H6`) and bare numbers are skipped;
"live proof/walk/check" is not a status. It prints the rows and never edits the board's prose.

**`board-markers`** (owner OK 10-30) puts `<!-- prompts:<module> --><!-- /prompts -->` at the start of the State cell
of the first board row per module. From then on every `set`, `claim`, `deploy-check --apply` and `--board` rewrites
only the text between those markers: `**Prompts:** 🟢 9/12 done or deployed (… done, … deployed) · ▶ running … ·
🔵 on-main … · 🔴 blocked … · ⚪ N pending (next …).` It has no time stamp, so it changes only when a status does. It is
never written while the board has uncommitted edits (another session's lines would ride along in the pathspec commit);
the command then says so and the next run catches up. `board-check` ignores the generated text.

## What the hooks do

- **Claim.** On a session's first message that names a prompt file, the tracker marks that prompt `running` with the
  session id (and commits it with a pathspec when `commit_claims` is on) when a claim verb (`run`, `execute`, `start`)
  is among the first six words (`analyze and execute X`, `Run plan and execute X`) or the message is only a prompt
  path (backticks allowed). A question (`why is X stalled`, first word why/what/how/did/is/…) or a negation before the
  verb (`do not run X`) never claims. Only `pending`, `blocked` and `running`-with-a-dead-session prompts can be
  claimed; a prompt never moves backwards. A first message that merely mentions a prompt ("look at X.md", "review
  X.md") records `prompt_mentioned` and claims nothing; `claim` attaches such a session by hand. The claim costs about 60 ms once per session (113 prompts, 80 tracker files).
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
| `status_folders` | `true`: `set` files prompts under `Completed/` / `Pending/` (see Layout) |
| `module_aliases` | file-name prefix or folder name → module (`Sock → security`, `Organization → org-roles`), case-insensitive |
| `sync_lock`, `sync_every_hours` | an auto-commit daemon's lock file and cadence; `migrate` refuses to run near it |
| `unit_map`, `main_branch` | `deploy-check`: the project's path → unit map (repo-relative JSON) and the branch it lands on (default `main`) |
| `status_board` | `board-check`: the hand-written Status Board (repo-relative) |

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
