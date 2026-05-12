# Agent Development Guide

How to create, configure, and use Claude Code sub-agents.

## What Are Sub-Agents?

Sub-agents are specialized Claude instances that run as child processes. They have:
- Scoped tool access (only the tools you specify)
- Independent context windows (don't pollute the main conversation)
- Optional model selection (use cheaper models for simple tasks)
- Parallel execution capability

## Agent Definition Format

Create a markdown file in `.claude/agents/`:

```markdown
---
name: my-agent
description: One-line description shown in agent selection
tools: Bash, Read, Write, Edit, Grep, Glob
model: sonnet
---

# Agent Title

System prompt instructions go here. This is what the agent sees
as its mission. Be specific about:

1. What the agent should do
2. What tools to use and how
3. What output format to produce
4. What constraints to follow
```

### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier (kebab-case) |
| `description` | Yes | When to use this agent. Include trigger patterns. |
| `tools` | Yes | Comma-separated list of allowed tools |
| `model` | No | Model override: `sonnet`, `opus`, `haiku` |

### Available Tools

```
Bash, Read, Write, Edit, Grep, Glob, LS,
WebFetch, WebSearch, Agent (for sub-sub-agents)
```

## Examples from This Project

### Code Reviewer (Read-Only)

```markdown
---
name: code-reviewer
description: Performs comprehensive code reviews. Specialized for Flask/SQLAlchemy.
tools: Read, Grep, Glob, LS, Bash
---
```

Uses only read-only tools — can't modify code, only analyze it.

### Printing Press Builder (Full Access)

```markdown
---
name: printing-press-builder
description: Generates CLIs from OpenAPI specs using CLI Printing Press factory.
tools: Bash, Read, Write, Edit, Grep, Glob, WebFetch, WebSearch
model: sonnet
---
```

Full tool access + web capabilities. Uses Sonnet for cost efficiency.

### Work Completion Summary (Specialized)

```markdown
---
name: work-completion-summary
description: Provides concise audio summaries via TTS.
tools: Bash, mcp__ElevenLabs__text_to_speech, mcp__ElevenLabs__play_audio
---
```

Minimal tools — only Bash and ElevenLabs MCP for audio output.

## Using Agents

### From Claude Code

Agents are invoked automatically when their description matches the task, or explicitly:

```
Use the code-reviewer agent to review my changes
```

### Parallel Execution

Multiple agents can run simultaneously:

```
Run code-reviewer and ux-ui-reviewer on the current branch
```

Claude Code launches both agents and merges their results.

### The Meta-Agent

The `meta-agent` generates new agent configurations from a description:

```
Create a new agent that specializes in database migration review
```

It writes a complete `.md` file with appropriate frontmatter and system prompt.

## Team Agents

Team agents in `.claude/agents/team/` support orchestrated workflows:

- **builder.md** — Generic engineering agent for implementation
- **validator.md** — Read-only verification of completed work

## Multi-Model Agents

The crypto agents demonstrate the multi-model pattern — same agent logic at different cost/quality points:

```
crypto/
├── crypto-coin-analyzer-haiku.md    # Fast, cheap
├── crypto-coin-analyzer-sonnet.md   # Balanced
└── crypto-coin-analyzer-opus.md     # Maximum quality
```

## Best Practices

1. **Scope tools narrowly** — reviewers shouldn't have Write access
2. **Use model overrides** — Sonnet for routine tasks, Opus for complex analysis
3. **Write clear trigger descriptions** — Claude Code uses these to auto-select agents
4. **Keep system prompts focused** — one agent, one job
5. **Include output format specs** — tell the agent exactly what to produce
