# Agents Reference

Sub-agents are specialized Claude Code workers defined as markdown files in `.claude/agents/`.

## Core Agents

| Agent | File | Model | Purpose |
|-------|------|-------|---------|
| Code Reviewer | `code-reviewer.md` | — | Code review for NeurogenServer Flask/SQLAlchemy codebase |
| UX/UI Reviewer | `ux-ui-reviewer.md` | — | Reviews HTML/CSS for UX quality, accessibility, design compliance |
| Magnus Pipeline Reviewer | `magnus-pipeline-reviewer.md` | — | Deep architectural review of the Magnus pipeline (36K+ LoC) |
| Meta Agent | `meta-agent.md` | — | Generates new sub-agent configuration files from descriptions |
| Printing Press Builder | `printing-press-builder.md` | sonnet | Generates CLIs from OpenAPI specs using CLI Printing Press factory |
| LLM/AI Research | `llm-ai-agents-and-eng-research.md` | — | AI research specialist for latest LLM/agent news |
| Hello World | `hello-world-agent.md` | — | Simple greeting agent for testing |
| Work Completion Summary | `work-completion-summary.md` | — | TTS audio summaries of completed work |

## Team Agents

Located in `.claude/agents/team/`:

| Agent | Purpose |
|-------|---------|
| `builder.md` | Generic engineering agent for implementation tasks |
| `validator.md` | Read-only validation agent for verifying completed work |

## Crypto Agents

Located in `.claude/agents/crypto/`. Each has haiku/opus/sonnet variants:

- **Crypto Coin Analyzer** — Real-time market analysis for specific tickers
- **Crypto Investment Plays** — Actionable investment opportunities with entry/exit strategies
- **Crypto Market Agent** — Top N cryptocurrencies by market cap
- **Crypto Movers** — Biggest gainers and losers over timeframes
- **Macro Crypto Correlation Scanner** — Macro-economic event correlation with crypto

## Creating a New Agent

Create a markdown file in `.claude/agents/` with this frontmatter:

```markdown
---
name: my-agent
description: What this agent does and when to use it
tools: Bash, Read, Write, Edit, Grep, Glob
model: sonnet
---

# Agent Instructions

Your agent's system prompt goes here.
```

Use the **Meta Agent** to auto-generate new agents: invoke it with a description and it writes the full config file.
