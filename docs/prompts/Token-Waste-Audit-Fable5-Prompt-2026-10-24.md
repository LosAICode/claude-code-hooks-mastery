# Token-waste audit of past Claude Code sessions — Fable 5 prompt

**Written**: 2026-10-24 | **Run with**: Fable 5.1 (`claude --model claude-fable-5-1`, or `/model` in a fresh
session) in `/opt/claude-code-hooks-mastery` | **Cost**: read-only. The audit's own tokens are the only cost; it starts
no builds and calls no billed provider.

Paste everything below the line into a fresh session.

---

You are auditing token waste in this team's past Claude Code sessions. The goal is a ranked, reproducible account of
where tokens were spent without moving the work forward, and of what would have prevented it, with the savings
measured, not guessed.

## Context you need

- **The bill:** 31.05B tokens and $12,832 in six weeks. 97.2% of it was cache reads: each API call re-sends the whole
  conversation, so cost ≈ context size × number of calls. The 30-day report on 2026-10-24 showed:
  - 29.25B cache reads; average context 374k per call
  - 140 sessions past 300k context, holding 94% of reads
  - subagents at 48% of reads
  - Read tool results of 54.1M tokens (725 of them over 10k tokens) and Bash results of 34.1M
- **Changes made on 2026-10-24** (sessions after that date should look different; measure before and after):
  - auto-compaction at 400k (`autoCompactWindow`)
  - one task per session (NeuroGenServer `CLAUDE.md` v54)
  - Sonnet for routine helpers
  - 14 agents parked
  - `/rlm` (`scripts/rlm.py`, see `docs/rlm.md`) and a PreToolUse guard that blocks whole-file Reads over 50 KB
- **Data:** `~/.claude/projects/` holds 3 project directories (`-opt`, `-opt-claude-code-hooks-mastery`,
  `-opt-neurogenserver`), about 494 JSONL transcripts and 3.1 GB, from 2026-09-09 on. Subagent transcripts sit under
  `<session>/subagents/` (about 478 files).
- **Fields seen so far** (verify before relying on them):
  - assistant entries carry `message.usage` (`input_tokens`, `cache_read_input_tokens`,
    `cache_creation_input_tokens`, `output_tokens`), `message.id` (repeated across lines of one response: dedupe on
    it) and `message.model`
  - user entries carry `tool_result` content
  - `cost-state` entries carry `totalCostUSD` and `modelUsage`
  - `system` entries with subtype `compact_boundary` mark compactions (24 files have one)
- **Existing tool:** `scripts/token_usage_report.py` already computes totals, context per call, long sessions,
  subagent share and big tool results. Reuse its parsing; don't rewrite it.

## Rules (they protect this session's own context)

1. **Never Read or `cat` a transcript, or print tool-result text.** Transcripts hold secrets, customer data and huge
   tool outputs. Do all analysis in Python scripts that print aggregates only: counts, sizes, ids, paths, timestamps.
   Keep each command's output under about 150 lines.
2. **Scripts and intermediate files go in the scratchpad**, one script per question, saved so every number in the
   report can be re-run. Run them with `nice -n 10`.
3. **For a qualitative question** ("what was this session doing when it re-read X 14 times?"), extract a small,
   redacted excerpt (tool names, file paths, the user prompt's first 200 characters) to a scratch file, then ask
   `python3 scripts/rlm.py "<question>" <excerpt file> --stats`. Never pipe raw transcripts to rlm.
4. **Never print a secret.** If a path or excerpt might hold one, print its presence or length only.
5. **Read-only.** Change no settings, hooks, CLAUDE.md or memory. The output is a report with recommendations; the
   owner decides.
6. **Work inline.** If you must use a subagent, give it one bounded question and `model: "opus"` (owner rule); never
   a long-lived helper.
7. **Stay small.** If your own context passes 250k, write what you have to the report and stop. The report is the
   handoff.

## Step 0 — verify the data model (do this first, briefly)

On 3 transcripts (one main session, one subagent, one with a `compact_boundary`), print only the set of entry
`type`/`subtype` values and the key names of a usage block, a tool_use and a tool_result. Confirm:

- how to dedupe usage
- how to tell a compaction
- how to tie a tool_result to its tool_use (name and input path)
- where timestamps live

Reconcile your 30-day totals with `token_usage_report.py --days 30` to within 1% before going on.

## Step 1 — measure each kind of waste

For each category, compute tokens, the share of the total, the top 10 offenders (session id, project, date), and a
dollar figure:

- **Dollar figures:** use the `cost-state` `totalCostUSD`/`modelUsage` where present. Otherwise use current list
  prices; look them up with the `claude-api` skill, never from memory.
- **Mark every number** as measured or estimated.

| # | Category | How to measure it |
|---|---|---|
| A | **Oversized context** | For each call, context = input + cache_read + cache_create. Count the tokens above 150k, summed over calls, and the sessions that ran past 400k and 967k. Find which share would have been saved by compacting at 400k, and by a 200k cap (`CLAUDE_CODE_DISABLE_1M_CONTEXT=1`). |
| B | **Carried tool results** | For each tool_result over 10k tokens, take its size × the calls that followed it in the same session, up to the next compaction. Rank by tool (Read, Bash, Grep, MCP, Agent) and by file path or command. That product is the real cost of a big read. |
| C | **Repeated reads** | The same path Read 2+ times in one session (note when ranges differ), and the same path read across many sessions: candidates for a summary file or `/rlm`. |
| D | **Cache re-creation** | The time gap before each call against its `cache_creation_input_tokens`. Test the open hypothesis that daily cache writes jumping from 2-6M to 45-128M from 2026-10-10 came from re-caching after waits longer than the cache TTL (5 min / 1 h). Bucket by gap length. If confirmed, estimate what `promptCacheTtl` / `subagentPromptCacheTtl` would save. |
| E | **Subagents** | Long-lived subagents near 966k; subagents that read the same files as their parent; large Agent results returned into the parent; how many subagents per task. |
| F | **Polling and loops** | Runs of near-identical tool calls (the same Bash command, `sleep`, status checks, the same failing command retried), and calls whose output is under 200 characters, in streaks. |
| G | **Failures and blocks** | Tool errors, permission denials and hook blocks, and the calls spent recovering from each (until the next successful call of the same tool). |
| H | **Fixed startup overhead** | The first-call context per session: system prompt, tool and agent lists, CLAUDE.md and the memory index (about 21 KB) × calls. What would trimming the memory index to 10 KB, or parking more agents, save? |
| I | **Model fit** | Opus or Fable calls on mechanical work (only Grep/Glob/Read/Bash with no Edit/Write in that subagent) that Sonnet or Haiku could have done, and the price difference. |

## Step 2 — check yourself

- Every headline number comes from a saved script. Re-run the three largest and confirm they reproduce.
- Hand-check three findings with ranged reads of metadata only (ids, sizes, timestamps), never tool-result text.
- Categories overlap (a big Read inside an oversized session). Say how you avoided double counting, or present the
  shares as non-additive.
- Split every metric at 2026-10-24: before vs after. If there is too little "after" data yet, say so.

## Step 3 — the report

Write `docs/reports/Token-Waste-Audit-<YYYY-MM-DD>.md`:

1. **Summary**: the top five waste sources ranked by dollars, each with one sentence of evidence and one fix.
2. **Category table**: tokens, share, dollars, measured/estimated, top offender.
3. **The 10 costliest sessions**: what they were doing (from the first user prompt, 200 characters, secrets
   redacted), peak context, calls, which categories hit them.
4. **Recommendations**, each with:
   - the exact change (settings key and value, hook, CLAUDE.md line, workflow habit)
   - its expected saving
   - its risk
   - how to verify it in next week's `token_usage_report.py`
5. **What stays unproven**, and the data that would settle it.
6. **How to re-run**: the list of scripts and commands.

Then reply with five lines: the top three findings with dollar figures, the report path, and the one change you would
make first. Do not commit the report or apply any change; ask the owner.
