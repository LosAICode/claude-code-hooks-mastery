# RLM in Claude Code

**Updated**: 2026-10-24 | **Code**: `scripts/rlm.py`, `/rlm` (`.claude/commands/rlm.md`), the large-Read guard in
`.claude/hooks/pre_tool_use.py` | **Commits**: `585e18d` (this repo), `97b24bf52` (NeuroGenServer `CLAUDE.md`)

RLM (Recursive Language Model) answers a question about big files or logs without loading them into the session.
It is the same design NeuroGen uses for chat file analysis (`modules/services/session_rlm_service.py`), after
Zhang, Kraska & Khattab, *Recursive Language Models* (arXiv:2512.24601). The paper author's reference
implementation is [alexzhang13/rlm](https://github.com/alexzhang13/rlm).

## 1. Why: where the cache reads come from

Each step of a session is one API call, and each call re-sends the whole conversation. The cost is roughly
**context size × number of calls**. Anything that enters the context stays there until the next compaction and
is billed again on every later call.

From `scripts/token_usage_report.py --days 30` (run 2026-10-24):

| Measure | Value |
|---|---|
| Cache reads | 29.25B tokens (output: 30.8M) |
| Average context per call | 374k |
| Sessions past 300k context | 140, holding 94% of cache reads |
| Tool output that entered contexts | Read 54.1M tokens, Bash 34.1M tokens |
| Read results over 10k tokens each | 725 |
| Docs in `Documentation/` over 50 KB | 35 (`magnus_upgrades.md` 634 KB, `UX-Standard-Guide.md` 113 KB, the security register 190 KB) |

A whole-file Read of `magnus_upgrades.md` adds about 160k tokens. Two things follow:

- **It is re-sent on every later call.** Over 500 later calls, that is about 80M cache-read tokens.
- **It fills the 400k compaction budget sooner.** At 40% of the window, compaction (and its re-cache) comes much
  earlier.

RLM keeps that file out of the session. Only the answer comes back, typically 300-800 tokens.

## 2. How it works

```
your session ──question + paths──▶ scripts/rlm.py
                                      │  files → Python variable `context` (never in a prompt)
                                      ▼
                         root model (Sonnet) writes Python ──▶ sandboxed exec ──▶ print output (≤2,000 chars)
                                      ▲                              │
                                      └──────── next turn ◀──────────┘
                         llm(instruction, slice) ──▶ sub-model (Haiku), ≤8,000 chars per slice, ≤12 calls
                         FINAL("answer") ──▶ only this returns to your session
```

- **Models run through `claude -p`** on the account's own login, so no API key is needed. Each call runs with no
  tools, no MCP servers, no settings or hooks, no CLAUDE.md and no saved session, from an empty temporary
  directory. A call starts at about 600 tokens.
- **The sandbox is NeuroGen's `check_code_safety`.** It blocks imports, `open`/`eval`/`exec`/`getattr`, and any
  underscore attribute or dunder name, and runs with a small set of built-ins. The code is written by a model
  that has just read the files, so a poisoned file is the threat it guards against.
- **The answer has to be grounded.** A `FINAL()` only counts once the model has seen output that read the
  content. With two turns left, the model is told to finish. If it runs out, one last call asks for its best
  answer and what it could not confirm.
- **REPL helpers:** `context`, `files` (path → text), `query`, `line_of(pos)` (a line number within its file, for
  citations), `re`, `json`, `math`, `Counter`, `defaultdict`, `llm()`.

## 3. Using it

### In a session

```
/rlm "Which upgrades changed billing for team steps?" /opt/neurogenserver/Documentation/magnus_upgrades.md
```

### From Bash

```bash
RLM=/opt/claude-code-hooks-mastery/scripts/rlm.py

# one big doc
python3 $RLM "What did U246 change, and when?" /opt/neurogenserver/Documentation/magnus_upgrades.md --stats

# many files, or a directory (read recursively)
python3 $RLM "Which docs describe the KB search fee, and what do they say?" /opt/neurogenserver/Documentation --stats

# a log or any command output: '-' reads stdin
journalctl -u neurogenserver --since -2h | python3 $RLM "Group the errors by cause, with counts" - --stats
```

| Flag | Default | Meaning |
|---|---|---|
| `--stats` | off | Print root calls, sub calls and cost (USD) to stderr |
| `--verbose` | off | Print each turn's code and output to stderr (debugging) |
| `--max-iterations` | 8 | Root-model turns |
| `--max-sub-calls` | 12 | `llm()` budget |
| `--root-model` / `--sub-model` | `sonnet` / `haiku` | Any `claude --model` alias |

Content over 5M characters is cut, with a warning on stderr.

### When to use what

| Situation | Use |
|---|---|
| You know the string (a function, an env var, an id) | `Grep`, then `Read` with `offset`/`limit`. It's free. |
| The question needs reading across a big doc, many files or a long log | `/rlm` |
| You will edit the file | `Grep` + ranged `Read` of the part you edit |
| You truly need the whole file in context | `Read` with `allow_large: true` (deliberately) |

**Before acting on a cited line, check it with a ranged Read.** RLM is a reader, not a source of truth.

### The guard

`.claude/hooks/pre_tool_use.py` blocks a `Read` with no `offset`/`limit` of any text file over 50 KB (exit 2). The
message names the file size and points to Grep + ranged Read or `rlm.py`. These still go through:

- a Read with `offset` or `limit`
- `allow_large: true`
- images, PDFs and notebooks

It applies to every session started in this repo, which covers all NeuroGenServer work.

## 4. Test results (2026-10-24)

| Question | Content | Result | Root / sub calls | Cost | Time |
|---|---|---|---|---|---|
| What did U246 change, and when? | `magnus_upgrades.md`, 634 KB | Right date (2026-08-13) and title, matching grep | 9 / 0 | $0.13 | 46 s |
| Docker env var, and which section says so | Developer-Reference + CLAUDE.md | `AG2_CODE_USE_DOCKER`; both cited lines exact (CLAUDE.md:103, Developer-Reference.md:290-291) | 4 / 0 | $0.03 | ~25 s |
| Team-step billing changes | `magnus_upgrades.md` | Found U216, U237 and U3a1a-21/-22; missed that one section belongs to U246 | 7 / 0 | $0.14 | 46 s |
| Group warnings and errors by cause | 3,000 lines of `celery-beat-error.log` via stdin | 8 groups with counts and line indexes | 6 / 0 | $0.17 | ~50 s |

Fixes made during testing:

- **`FINAL()` inside code.** The model called it there and lost turns, so `FINAL` is now a REPL function.
- **Tool-call markup.** Sonnet with no tools sometimes writes tool-call markup instead of a code block, so the
  first such block now runs.
- **Escaped quotes** in answers are unescaped.
- **Line numbers** are now per file.

Sandbox, `FINAL` parsing and the guard were checked directly.

## 5. Costs and limits

- **Each run costs $0.03-0.20 and takes 20-60 s,** billed to the same Claude account. `rlm.py` runs with
  `--no-session-persistence`, so its calls do **not** appear in `token_usage_report.py`; `--stats` is their
  record.
- **Haiku sub-calls were never used in testing.** Sonnet answered every question with regex and slicing. NeuroGen
  saw the same (2026-08-01) and added the STRATEGY block, which is ported here. `llm()` matters when the wording
  varies.
- **Answers can be incomplete.** An answer that says it ran out of turns needs a narrower question or
  `--max-iterations 12`.
- **RLM is not the main lever.** Most of the bill is long sessions. Compaction at 400k (`autoCompactWindow`) and one
  task per session (CLAUDE.md v54) address that. RLM and the guard address the 725 large Reads and long log or
  command outputs.

## 6. Status and next steps

| Item | Status |
|---|---|
| `scripts/rlm.py` (port of NeuroGen session RLM, `claude -p` backend) | Done, committed `585e18d` |
| `/rlm` command | Done |
| PreToolUse guard: whole-file Read over 50 KB | Done; applies to sessions started after 2026-10-24 |
| NeuroGenServer `CLAUDE.md` §7 points here | Done (`97b24bf52`, v54.1) |
| Measure: whole-file Reads over 50 KB and "Read results over 10k tokens" should fall toward 0 in `token_usage_report.py --days 7` | Check 2026-10-31 |
| Log each `--stats` line to a file, so RLM's own cost shows in the weekly report | Open |
| Depth-2 recursion (a sub-call with its own `llm()`) | Not planned; the paper's gains come from depth 1 |
