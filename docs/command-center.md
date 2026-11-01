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
| `needs_owner`, `cost`, `depends_on` | what the owner must decide (`inbox`; "answered …" closes it), what the run costs (`inbox`, `proofs` rank it: free · unknown · paid), ids that must be done first |
| `session_id`, `commits`, `deployed`, `updated`, `notes` | who claimed it, every commit it shipped (`[sha, …]`; `set --commit` appends; the older single `commit:` is still read and folded in on the next `set`), when it went live, last change, one line |
| `progress` | how far a running prompt is: `"3/6"` or `"3/6 tests red"` (`set … running --progress 3/6`; a bare `3` takes the numbered items under `## Do` as its total). Shown in the view, `--module`, the module tables, the HTML and `--json` |
| `tests` | the verdict behind a landing: `"<SUMMARY line> · <results file> · <sha tested>"` (`set --tests`), `"<text> · <sha>"` (a value that is not an existing file is text, whatever its length; its first 300 characters are kept, CC11), or `"none: <why>"` (`--no-tests`, a docs/config landing). `deploy-plan` lists what it would ship without one |
| `proof` | recommended for a new prompt (optional for old ones): what proves it live (script, probe account, cost); else `proofs` uses the first "live" bullet under `## Done when` |
| `after`, `run_last` | optional: `[ID, …]` that must be done first besides `depends_on`; `true` = not while another prompt of the module runs or is on a branch. Without them `next` reads "Run after <ID>" / "Run last" in the body (a quoted mention is not a hint) |
| `todo: confirm` | written by `migrate` when it could not read the old README row: check the status by hand |
| `issue` | the prompt's GitHub issue (an int, a quoted number or a list); one issue per prompt |

A file without frontmatter is `pending`. Progress = done + deployed over everything except skipped and superseded.
The view also derives two states: a `pending` prompt with a live session on it shows as `running`, and a `running`
prompt whose session is gone shows as `stalled`.

## Commands

```
python3 scripts/command_center.py                 # summary per module, then Sessions / Stalled / Blocked on owner / Parked / Next up
python3 scripts/command_center.py --module M      # every prompt of one module (order, id, status, owner, session, commit, updated, title)
python3 scripts/command_center.py --all           # every module, including skipped and superseded
python3 scripts/command_center.py --json          # for scripts (ops checks); --json --prompts adds every prompt with its frontmatter
python3 scripts/command_center.py --board         # rewrite the status block of every module README + the root board; prints what changed
python3 scripts/command_center.py --html          # write the HTML dashboard (prompts.html, default ~/.claude/session-tracker/command-center.html)
python3 scripts/command_center.py set <ID|module/ID|file> <status> [--commit SHA]… [--deployed TS] [--notes TEXT] [--needs-owner TEXT] [--proof TEXT]
                                  [--tests FILE|TEXT [--tested SHA] | --no-tests WHY] [--progress N[/M] [text]]
                                  [--clear KEY]… [--comment] [--close] [--no-commit]
python3 scripts/command_center.py claim <ref> --session SID [--no-commit]   # attach a live session by hand (running + session_id + its Stop gate)
python3 scripts/command_center.py deploy-check [--module M] [--json] [--apply [--no-commit]]
python3 scripts/command_center.py issues [--module M] [--fix [--no-commit]]
python3 scripts/command_center.py audit [--module M]      # exit 1 when it finds something
python3 scripts/command_center.py board-check             # the hand-written Status Board vs the prompts; exit 1 on a mismatch
python3 scripts/command_center.py board-markers [--no-commit]   # once: a generated Prompts: line in each module's board row
python3 scripts/command_center.py sort [--dry-run] [--no-commit]   # file every prompt under Completed/ or Pending/
python3 scripts/command_center.py inbox [--module M] [--json]        # every open owner ask, longest waiting first
python3 scripts/command_center.py deploy-plan [--module M] [--json]  # guards, migrations, restart order, proofs: prints, runs nothing
python3 scripts/command_center.py deploy-ready [--module M] [--json] # OK or WAIT with every reason, and what would ship; exit 0/1
python3 scripts/command_center.py deploy-run --owner-ok "<the owner's words>" [--only STEP] [--dry-run] [--no-commit]
python3 scripts/command_center.py proofs [--module M] [--json]       # deployed prompts and what proves them, free first
python3 scripts/command_center.py next [--parallel] [--json]         # pending prompts that can start now, and why the rest wait
python3 scripts/command_center.py migrate [--module M] [--dry-run] [--force]
```

The four planning commands read only (files, git, `systemctl show`): none restarts a unit, applies a migration,
spends credits, writes a file or starts a session.

`--json` lists `running`, `stalled` and `blocked` prompts with `updated`, `session_id` and `session_live`, so a check can
age a stalled prompt or spot a blocked one whose session is gone. `sessions` holds the Sessions rows (below) as
`{sid, sid8, state, age_s, waiting_on, prompt, unclaimed, branch, worktree, last_commit, task}`, `session_counts` the
header counts, and `parked` the Parked prompts with their `tip` and `age_s`.

**Sessions** (CC8: every live session, one list). Every live session (`pc.live_session_ids`: not ended, seen in the
last 2 h), WAITING ON YOU first (with the tool or message it waits on: `waiting_reason`, else `last_tool`), then the
newest activity first. Each row shows its prompt (module, id, title) and that prompt's status, whatever it is
(`running`, `on-branch`, `deployed`, …), with `progress:`, the branch (or worktree) and the last commit. A session
with no prompt that named one (`prompt_mentioned`, or a task that resolves to a `pending`/`blocked` prompt) shows
`unclaimed: <module>/<ID> → command_center.py claim <module>/<ID> --session <sid8>`; one with none shows `no prompt`.
The header counts `N live · N working · N waiting on you · N idle · N without a prompt`; a module line counts a live
session on a prompt that is not `running` (A06, on-branch, 10-31) as `N active (<sid8>, <age>)`. `--html` has the
same card. A session is joined to a prompt by its `prompt_file`, by the prompt's `session_id`, or by a task whose
words claim it (`run X`); a task that only mentions a prompt (`look at X`) is no run.

**Parked** (report only): an `on-branch` prompt no live session holds whose `updated` (else the file's mtime) is 24 h
old or more, with its branch tip (the last sha in `commits:`) and age. Rerun its session or `set` its real status.

`set` updates the frontmatter, regenerates that module's table and the board, and commits those files with a
pathspec (`docs(prompts): <module> <ID> → <status>`), because an uncommitted edit in a shared tree blocks any later
merge that touches the file. Setting a prompt `pending` or `blocked` also releases it from every session that held
it (its tracker `prompt_file` is cleared and the prompt goes into `released_prompts`), so the view stops showing it
running; a later claim takes it back. `--commit` repeats append every sha (`--commit A --commit B`); `--proof
"<text>"` writes `proof:`. `--no-commit` leaves the files for your own commit. `--needs-owner ""` and `--clear KEY`
remove a key (never `status` or `updated`). `--comment` posts one comment to the prompt's issue (`<module> <ID>: <old> →
<new> · commit · deployed · notes`); `--close` closes the issue with that comment and works only with `done`,
`skipped` or `superseded`. "Next up" is the first `pending` prompt per module by `order` whose `depends_on` are all done;
prompts that need nothing from the owner come first, the others are marked `⚑ needs owner: …`.

`claim` attaches a session that is live in the tracker (full id or a unique prefix) to a prompt: it writes `running` and
`session_id`, records the claim in the session's tracker file so its Stop gate applies, and commits. It refuses a gone
session, a prompt another live session holds, and anything past `blocked` (a prompt never moves backwards).

**`deploy-check`** answers "is it live?" per unit. For each `on-main` and `deployed` prompt it reads every commit in
`commits:` (and the older `commit:`); it is live only when all of them are. It finds where each landed on main (the first first-parent commit that contains it: a branch commit lands at
its merge, not at its author time; CC12: a first-parent commit lands at the earliest time main's reflog held a tip
that contains it, so a commit that came in by a fast-forward lands then, not at its committer date: live 11-01 R3's
`6ba6cc1e1` read as landed 10-30 23:15; no reflog = the committer date), lists the files that landing brought (a merge named by its sha: its branch; a plain commit: only its own change, CC10, since
live 11-01 a main merged into a branch and fast-forwarded put main's own commits off the first-parent line), and maps them to units with the project's
map (`unit_map`; NeuroGenServer `scripts/deploy_units.json`, which `ops_drift.py` reads too: one map, owner decision
10-30). A file needs the units of every rule it matches; a `live_at_merge` rule (static files) is live when it lands;
a file no rule matches needs no unit. A unit counts when it started (`systemctl show`, read-only) after the latest
landing that needs it. States: **live** (every needed unit), **half** (some), **waiting** (none), **no-unit** (docs,
tests, scripts), **unchecked** (no commit, or a commit not on main, e.g. another repo's). `--apply` sets an `on-main`
prompt that is live to `deployed` with `deployed:` = the last needed unit's start, and commits; it never touches a
`deployed` prompt and never moves one back. A start time is the unit's latest start, so `live since` can be later than
the real moment. `--json` is what `ops_drift.py` reads for `drift-prompt-half-live-<module>-<id>` (half live > 24 h).

**`inbox`** lists every prompt with a `needs_owner` that is not done/skipped/superseded (an ask that starts with
"answered" is closed), longest waiting first: `module/ID`, status, how long it waited (since `updated`, else since the
file last changed), its `cost`, the ask, and the line to paste (`run <path>` for a pending/blocked prompt;
`deploy-plan --module M` for an on-main one; `proofs --module M` for a deployed one; the holding session for a running
or on-branch one). It then lists, report only, Status Board clauses that say "owner" and name a prompt with no
`needs_owner`.

**`deploy-plan`** takes every `on-main` prompt and every `deployed` one that is half live or waiting (a later commit
not live yet), runs `deploy-check`'s logic on them and prints, in order: the prompts and the units each still waits
on; the migrations among their commits (files under `deploy.migrations`, one row per file from its newest commit,
CC12) to apply BY HAND first, and one at or below prod's `alembic_version` is shown apart as "already in
prod" (the config's `prod_revision` query; read-only; any error = "alembic_version unread" and the whole list stays:
R4b/R5 were listed 10-31 while prod was already at r5). Only while `alembic_version` is unread, one whose commit's
units all started after it landed is guessed to have run at boot and is shown apart (R4, 10-31); with prod read,
prod decides (CC12: live 11-01 R3's migration read as both "to apply" and "ran at boot" while prod lacked it). The walk parses `revision`/`down_revision` (merge tuples too)
in the migrations folder, never imports a file; "Not proven tested": every prompt it ships with no `tests:`, a tested sha that is
not on main, or a commit that landed after the tested sha; the guards; one restart step per unit in `deploy.order` that is needed
(a group's own command, e.g. `restart_web.sh` under the deploy lock, else `systemctl restart <unit>` per member), each
with the guards that must hold right before it (`before:`, e.g. 0 live calls before `-voice`) and its proofs; then
`deploy-check --apply` and the proofs owed. The project's steps live in the unit map's `deploy` block, not in code:

| `deploy` key | meaning |
|---|---|
| `migrations`, `migrate` | the migrations folder; how to apply one by hand (printed) |
| `guards` | `[{name, command, expect, before?}]`; no `before` = before the first restart |
| `order` | unit or group names in restart order; a needed unit not listed goes last |
| `restart` | `{group or unit: command, "*": "systemctl restart {unit}"}` |
| `proof`, `ready`, `after_each` | per unit (`{unit}` filled in); `{unit: its ready-line grep}`; after every step (health ×3) |

| `lock`, `freeze_file`, `proof_wait_s` | `deploy-run`'s lock (default `/run/lock/neurogen-aa-deploy.lock`); the freeze file the `/cc` line checks (default `/run/lock/neurogen-deploy-freeze`); how long a unit's start-time and ready-line proofs poll (default 120 s) |
| `ready_checks` | `deploy-ready`'s own checks: `min_available_mb` (default 1500), `locks` (files that must not be held, e.g. the sandbox lock; default none), `merge_state`, `dirty_units`, `migration_chain`, `main_line`, `verify` (booleans, default on), `landing_window_min` (default 10) |

Without a `deploy` block it uses the map's own unit order and plain `systemctl restart`, with a note.

**Commits in a second repo** (CC11). `commits:` may hold shas from any repo in `prompts.repos`
(`[{"name": "hooks", "path": "/opt/claude-code-hooks-mastery", "live_at_merge": true}]`). A sha that is not in the
project repo is looked up on each listed repo's own main line; it lands there, needs none of the map's units and, for a
`live_at_merge` repo (its hooks and scripts run from the tree), is live when it lands. `deploy-check`, `deploy-plan`
and `audit` show it as `hooks:<sha>`; only a sha found in no repo says `<sha> is not in any repo` (live 11-01: CC9 and
CC10 listed `c1c6ea0`, `fa7279d` … and could never read as live). The tracker credits a claimed prompt with its
session's commits in a listed repo too (the "only the prompts tree" skip applies to the project repo only).

**Rebased commits** (CC9). A sha in `commits:` that is not on main (the commit before a rebase, which the hook
recorded: R9's `0fa179e48`, 10-31) resolves to the main commit with the same `git patch-id`, else the same subject and
author. `deploy-check` and `deploy-plan` use the new sha and print `<old> → <new> (rebased)`; a resolved sha the prompt
already lists counts once. `audit` gives the `set … --commit <new>` line; nothing rewrites `commits:` on its own.

**`deploy-ready`** (CC9, read-only) is the answer to "is a deploy safe?". It runs every `deploy.guards` entry (a
`before:` guard only when its unit ships) and the `ready_checks`: free memory (`MemAvailable`) at or above the floor;
no `locks` file held (`flock -n`; a suite in the sandbox); no merge or rebase in progress in the live tree; no
uncommitted change the unit map maps to a unit or that is live at merge (static JS); every migration to apply chains
from prod's `alembic_version` or another shipped migration, with one head (prod unread = WAIT); no other live session
landing now (a pending git commit/merge snapshot in the repo, or a code commit recorded within `landing_window_min`;
the calling session and `docs(prompts):` status commits are skipped; CC11: so are, with `--answer <id>`, the requesting
session, and a commit whose files need no unit, such as tests or docs: live 11-01 07:42 A03's request waited on its own
one-file test commit); and (CC11, `main_line`, on by default) main's newest landing did not put a branch on its
first-parent line (see `audit`'s main-line check); and (CC12, `verify`, on by default, only while `board_gate.repo`
is set) a `/verify` record covers main's code, so the verify gate would let the restart through. One line per check (`OK  <name>` or
`WAIT: <name>: <detail>`), the verdict `Deploy: OK` / `Deploy: WAIT (<n> reasons)`, then what would ship (prompts,
units, migrations to apply, not proven tested). Exit 0 OK, 1 WAIT, 2 unit map unreadable.

**`deploy-run`** (CC9) is the one command that acts, run from the `/cc` session after the owner's OK, quoted in
`--owner-ok` (never a peer session's message). It refuses without the quote and when `deploy-ready` says WAIT; with
migrations to apply it prints `deploy.migrate` and the files and exits 3 (apply them by hand, then rerun; it never
runs a migration). Under the deploy lock (held elsewhere = exit 1) it runs each step of `deploy-plan` as its own
process with `NEUROGEN_DEPLOY_RUN=<id>` (a step's own `flock <lock>` prefix dropped: it already holds that lock), then
that step's proofs as separate reads: every member unit's `ActiveEnterTimestamp` at or after the step began, its ready
line (newer than the step when it carries a timestamp; a local stamp in the hour a DST fall-back repeats reads as the
later time, CC10), and `after_each` (`/api/health` ×3, all 200). A failed command
or proof stops it: it prints what is live and what is not, and `--only <step>` reruns one step. At the end it runs
`deploy-check --apply` and prints the `Tell:` lines. `--dry-run` prints every command in order and runs nothing (exit 1
on WAIT, 3 when migrations would stop it).

Each run writes `~/.claude/session-tracker/deploys/<id>.json` after every step: the owner's OK, the session, the plan,
per step its start, end, commands with exit codes and proofs with their output, the result and the prompts set
deployed. A file with no `end` is a run in progress (the restart gate below reads exactly that). `/cc` shows one line:
`Deploy: OK | WAIT (<first reason>) · last deploy <age>`, from the cheap checks only (freeze file, locks, merge state,
memory: no psql, no systemctl).

**`Tell:`**. After `deploy-run`, and after `deploy-check --apply` alone, one line per prompt set deployed:
`Tell: <session> · <module>/<ID> · owes: <its proof>` (`(no live session)` when its session ended). The `/cc` session
sends them with SendMessage; the script cannot.

**`proofs`** lists `deployed` prompts (live, proof open) with their `proof:` line, else the first top-level bullet
under `## Done when` that says "live". Ranked by `cost` + `proof`: free ("session tokens only", "no AI credits",
"0 credits", "NO … build") first, then unknown (no cost stated), then paid (credits, `$`, a build; the larger `$` later),
then the longest deployed.

**`next`** lists the pending prompts that can start now: every `depends_on`/`after` id done or deployed; not run-last
while another prompt of its module runs or is on a branch; no file shared with a running/on-branch prompt (its commits'
files and its live session's worktree changes, committed, uncommitted and untracked; the live tree itself is skipped)
or with a prompt the list already starts; no unit shared with one. A prompt's files are the backticked repo files its
body names (a bare file name counts when one tracked file has it; folders and globs do not). The unit map's `parallel`
block says what sharing is no conflict: `shared_units` (units nearly every change restarts: NeuroGenServer `web`,
`-celery`) and `ignore` (globs everyone edits or only reads: docs, `tests/known_failures.txt`). Without `--parallel`
it lists the first startable prompt per module. Every other pending prompt is listed with why it waits.

**`issues`** reads `gh issue list --state all` and lists: prompts with no issue (with the issues whose title names
them), issues that name a prompt whose `issue:` doesn't list them, prompts named by several issues, state mismatches
(a done/skipped/superseded prompt with an open issue; a pending/running/blocked/on-branch one with a closed issue)
and `issue:` numbers GitHub doesn't have. An issue names a prompt by its body's `Prompt: <path>` line (old paths
resolve through `_moved.json`), else by its title, `[Module] ID: …` (`[API Connections] R7:` → connections R7).
`--fix` writes `issue:` only for a prompt without one that exactly one issue names. Nothing here creates an issue:
creating them is outward-facing, so the owner OKs the list first.

**`audit`** flags `on-main`/`deployed`/`done` with no commit, a `todo:` left over, a file whose `Pending/` or
`Completed/` folder disagrees with its status, a prompt with no id (so `set <module>/<ID>` can't reach it), an id two
live prompts share, a lower-case module folder holding prompts (module folders are capitalized), (CC8) two module
folders whose names differ only by case (`connections/` beside `Connections/`), every `kind: reference` file (skipped
by `scan`: the owner's audit report is no prompt), a `Pending/` file with no `id:` and no `## Do` (add `kind:
reference`?): all report only, it never moves or deletes the owner's files; and (CC7):
- an `on-main`/`deployed`/`done` prompt whose `notes:` still say queued/pending/waiting/todo/blocked;
- a `deployed` prompt with neither `proof:` nor a "live" bullet under `## Done when` (`proofs` can't say how to close it);
- a commit on main whose subject names a prompt (`#<issue>` or `<module> <ID>`), made since that prompt's first commit,
  that no prompt's `commits:` lists and no recorded merge brought in, with the `set … --commit <sha>` line that records
  it (report only: the prompt's owner runs it). Status commits (`docs(prompts):`) and, with a unit map, commits that
  need no unit (docs, tests) are skipped: deploy-check would not miss them. Live 10-31: R4's fix `766d43595` was never
  recorded, so deploy-check called R4 live while the fix waited on web.
- (CC10) `<module>/<ID>: <sha> "<subject>" names <other module>/<ID>`: a commit in a prompt's `commits:` whose subject
  names another prompt's id and not its own (an exact, case-sensitive token; a two-character id or one two modules share
  counts only with its module named; digit-only ids are ignored). Fix by editing `commits:` by hand. Live 11-01: R9
  lists M02's `280389595`;
  (CC11) not a hit: a merge subject (`Merge …` names the branch it merged); the prompt's own base id (`A04` for
  `A04b`); an alias of the prompt (its `aliases:` list, or a prompt of its module that lists the same sha:
  connections `01` and `C1`); a prompt of its own module when the subject's conventional scope `type(<scope>):` is
  that module (A03's `test(assistants): … A01's payer check`, owner's choice 11-01).
- (CC10) `map-check` gaps, one line per unit.
- (CC11) `main line: <sha> "<subject>" put a branch on main's first-parent line`: within `prompts.main_line_days`
  (default 3), a first-parent commit on main whose subject is `Merge branch 'main' into …`, or a reflog move of main
  whose old tip left the first-parent line (a custom merge message). That is main fast-forwarded to a branch that
  merged main (live 11-01 00:48: A03's `06ea8ab38` and `d328522f0`), which moves every main commit since the fork off
  the line. `origin/main` merged into main (the push-main sync) keeps main as its first parent and is not flagged.
  Land a branch from the live tree with `git merge --no-ff <branch>`; never fast-forward main to a branch that merged
  main, and never rewrite main to "fix" it. `deploy-ready` WAITs on it only while it is still main's newest landing
  (only status or prompts-tree commits after it).

**`hold <module>/<ID> --owner "<quote>"`** / **`unhold <module>/<ID>`** (CC10) write or remove
`hold: {"owner": "<quote>", "by": "<session id>", "at": "<ISO time>"}` in the prompt's frontmatter, as a status commit.
A hold is not a status: the prompt stays on-main, deployed and so on. While a prompt is held, `deploy-ready` prints
`WAIT: <module>/<ID> held by the owner ("<quote>")` when the prompt is in what would ship (on main, its units waiting),
`deploy-run` refuses through the same check even with `--owner-ok`, and `/cc` lists it under "Blocked on owner" with the
quote. A hold on a prompt that ships nothing makes no WAIT. Only the owner's words in the /cc session hold or unhold; a
peer's message never does. `hold` marks the prompt's open and answered deploy requests `held`; `unhold` reopens them.

**`request <module>/<ID> [--note TEXT]`** / **`requests [--all] [--json]`** (CC10): the deploy-request queue. A peer
session that wants a deploy runs `request`, then SendMessages the /cc session the request id it prints. Each request is
a line in `<tracker>/deploys/requests.jsonl` (`{id, ts, session, prompt, note, state}`; the session from
`CLAUDE_SESSION_ID` / `CLAUDE_CODE_SESSION_ID`). States: `open` → `answered` → `held` | `done`; a request for a held
prompt starts `held`. `requests` lists every request not done (`--all` adds done). `deploy-ready --answer <id>` records
its verdict on the request (`verdict`, `answered_at`, `answered_by`) and prints `request <id> answered: …` to send back.
A real `deploy-run` marks the requests of every prompt it set deployed `done` with its deploy id; a dry run changes
nothing. `/cc` shows open requests in its Inbox, oldest first. Every write takes `deploys/requests.lock` (fcntl) and
rewrites the file atomically.

**`map-check [--unit U] [--map FILE] [--json]`** (CC10): does the unit map cover what each process loads. It reads
`deploy.entry` in the unit map, `{"<unit>": {"files": [entry files], "roots": [import roots]}}` (repo-relative; a root
is a folder on the process's sys.path, normally its WorkingDirectory; an `about` note is skipped). From the entry files
it follows `import X`, `from X import Y` (Y too when it is a submodule), relative imports and
`importlib.import_module("<constant>")` at any depth (imports inside functions count) to files under a root; a
package's `__init__.py` counts as loaded. Nothing is imported or run, and `tests/` is skipped. It prints every reached
file that no rule giving that unit matches, with one import chain (entry → … → file): a change there would not restart
the process that runs it. Modules loaded by name from data (`"pkg.mod:Class"` strings) are invisible to it: list them
as entry files. Exit 0 no gaps, 1 gaps or a missing entry file, 2 map unreadable / no `deploy.entry` / unknown `--unit`.
`--map` checks another map file (a proposed one) without editing the project's. `audit` adds one line per unit with
gaps. Live 11-01: `-channels` and `-voice` each reach ~970 backend files (models/** loads at startup) while their rules
covered 13 and 4; the owner put both on the `modules/**` rule.

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

- **Claim.** On any message that names a prompt file while the session holds no prompt (CC8: not only the first; a
  session that holds one is never re-pointed by a later mention), the tracker marks that prompt `running` with the
  session id (and commits it with a pathspec when `commit_claims` is on) when a claim verb (`run`, `execute`, `start`)
  is among the first six words (`analyze and execute X`, `Run plan and execute X`) or the message is only a prompt
  path (backticks allowed). A question (`why is X stalled`, first word why/what/how/did/is/…) or a negation before the
  verb (`do not run X`) never claims. Only `pending`, `blocked` and `running`-with-a-dead-session prompts can be
  claimed; a prompt never moves backwards. A first message that merely mentions a prompt ("look at X.md", "review
  X.md") records `prompt_mentioned` and claims nothing; `claim` attaches such a session by hand. A path a terminal
  wrapped (`…-Attach⏎  ments.md`, A04 10-31) is joined back before matching, and a token that still matches nothing but
  is a unique prefix (≥ 20 characters) of one prompt's file name resolves to it. A claim scan costs about 60 ms (113
  prompts, 80 tracker files), once per message only while the session holds no prompt.
- **Commits.** Each commit a session's own command made in the prompts repo (or one of its worktrees) is appended to
  the claimed prompt's `commits:` (`pc.add_commits`: status and `updated` untouched; a prompt filed elsewhere since the
  claim is found through `_moved.json`), then committed with a pathspec as `docs(prompts): <module> <ID> commits +<sha>`
  when `commit_claims` is on. Skipped: status commits (`docs(prompts):`), commits touching only the prompts tree, and
  commits in other repos (deploy-check reads only the prompts repo; a hooks-repo prompt still uses `set --commit`).
  A failure is logged, never raised. Only what this session's command could make counts (CC9; live 10-31: M02's docs
  commit `280389595` landed in the shared tree while Connections ran `command_center.py set` and was credited to R9): a
  `command_center.py` call makes only status commits; `git commit -m <msg>` (also `-am`, `--message=`, a
  `$(cat <<'EOF' …)` heredoc) makes a commit with that subject, and with a `--` pathspec only files under those paths;
  merge, cherry-pick, revert, am and `-F` have no subject rule. A commit whose `Claude-Session:` trailer belongs to
  another live session is skipped; a session learns its own trailer from its first subject-matched commit. Rejected
  commits go to the session's `skipped_commits` (last 20, with the reason). A command that only mentions `git commit`
  (in an echo or a grep) records nothing.
- **Restart gate** (CC9, off until `prompts.deploy_coordinator` is set). A PreToolUse Bash hook stops
  `scripts/restart_web.sh` and `systemctl restart|stop|start neurogenserver*` at the command word (top-level `restart_patterns`, beside `deploy_patterns`),
  so `systemctl show`/`status`/`is-active` and a mention in an argument pass. It lets the call through when the session
  is the coordinator; when the restart's own segment sets `NEUROGEN_DEPLOY_RUN=<id>` and `deploys/<id>.json` has no
  `end`; when it sets `NEUROGEN_DEPLOY_OVERRIDE="<reason>"` (appended to `deploys/overrides.jsonl`); or when every unit
  it names is down (an outage restart is never stopped; `restart_web.sh` names the three web units). An error reading
  `systemctl is-active` lets it through (logged). Otherwise (CC11 wording): "Queue a request: run
  `command_center.py request <module>/<ID>` (it prints the request id), then SendMessage the /cc session
  `deploy request <module>/<ID> <request id>`; it answers with `deploy-ready`."
- **Verify gate** (owner 11-01, on whenever `board_gate.repo` is set). The same restarts, plus
  `command_center.py deploy-run` without `--dry-run` (it restarts units from Python, out of the hook's sight), are
  stopped while a `board_gate.code_paths` file changed on `board_gate.branch` since the newest record in
  `deploys/verified.jsonl` that the repo has. `/verify` ends with `scripts/verify_record.py --summary "<result>"`, which
  records the branch tip. The message names the files and the `git diff <sha>..main -- modules/` to verify. A down
  unit and `NEUROGEN_VERIFY_OVERRIDE="<reason>"` (logged with `kind: verify`) always pass; a hook error lets the call
  through (logged). Seeded 11-01 with a BASELINE record at `771071574` (main as live since the 07:43 restart).
- **Gate.** With `prompts.gate` on, a Stop is blocked once per session when the session claimed a prompt, has commits
  or deploys, and the prompt is still `running` under its id. The message names the `set` command. It never blocks a
  session that only stopped to ask a question (no commits), and a read error lets the stop through (logged).
  (CC10) While a background subagent is open (SubagentStart adds its `agent_id` to the session's `open_agents`,
  SubagentStop removes it; an entry older than 6 h counts as closed), the status and progress gates do not fire and
  do not spend their one block: a turn end while builders run is not the end of the work (live 11-01). (CC11) The
  board gate waits the same way; the claim gate is unchanged.
- **Progress gate.** After the status gate has had its one block, a later Stop is blocked once more when the prompt is
  still `running` under the session and the session committed code (not a status commit) after the prompt's `updated`
  (its claim or last `set`): it asks for `set … running --progress <done>/<total>`. The two never ask in the same stop.
  Message override: `prompts.progress_message`.
- **Claim gate** (CC8, last). A Stop is blocked once when the session holds no claimed prompt and made a code commit
  (not a status commit) or ran a deploy command: it asks to `claim <module>/<ID> --session <sid8>` (naming the prompt
  it mentioned, when it did) or to write one from the template in `Documentation/Prompts/README.md` and claim that. A
  session that only read, viewed (`/cc`, `/sessions`) or answered questions is never asked. Override:
  `prompts.claim_message`.

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
| `deploy_coordinator` | `{"session": "<id or ≥8-char prefix>"}`: the `/cc` session whose restarts pass the restart gate; unset = the gate is off |
| `repos` | (CC11) other repos a prompt's `commits:` may name: `[{"name", "path", "live_at_merge", "branch"}]` (see Commits in a second repo) |
| `main_line_days` | (CC11) how far back `audit` and `deploy-ready` look for main fast-forwarded to a branch that merged main (default 3) |
| `prod_revision` | `deploy-plan`: a read-only shell query printing prod's `alembic_version` head(s), e.g. `psql -h 127.0.0.1 -U postgres -d neurogenserver -tAc 'SELECT version_num FROM alembic_version'`; unset = "alembic_version unread" |

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
