# Architecture Overview

Claude Code Hooks Mastery demonstrates five integration patterns for extending Claude Code with deterministic control.

## Integration Patterns

### 1. Hook-Based Control (Deterministic)

Hooks are Python scripts that execute at specific lifecycle points. They receive JSON payloads via stdin and control execution via exit codes.

```
User Input → UserPromptSubmit hook → Claude processes → PreToolUse hook
    → Tool executes → PostToolUse hook → Claude responds → Stop hook
```

**Key hooks:**
- `pre_tool_use.py` blocks dangerous commands (`rm -rf`, `.env` access, sensitive file writes)
- `post_tool_use.py` logs all tool activity and can transform outputs
- `user_prompt_submit.py` pre-processes user input, stores prompts, names agents

### 2. Sub-Agent System (Parallel Workers)

Agents are specialized Claude instances with scoped tools and model selection. Defined as markdown files with YAML frontmatter.

```
Main Claude → spawns Agent (code-reviewer.md)
           → spawns Agent (ux-ui-reviewer.md)
           → both run in parallel
           → results merged by main Claude
```

### 3. Status Line Rendering (Real-Time)

Python scripts that output ANSI-formatted text for the Claude Code terminal bar. Updated on each interaction.

### 4. Output Style System (Response Formatting)

Markdown templates that control how Claude formats its responses: tables, YAML, bullet points, HTML, or ultra-concise.

### 5. Plugin Integration (MCP Servers)

External tools connected via Model Context Protocol:
- **context-mode** — Context window protection, large output indexing
- **claude-mem** — Persistent memory across conversations

## Data Flow

```
┌─────────────────────────────────────────────────────┐
│                    Claude Code                       │
├─────────────┬───────────────┬───────────────────────┤
│   Hooks     │    Agents     │     Plugins (MCP)     │
│  (13 py)    │   (8+ md)     │  context-mode         │
│             │               │  claude-mem           │
├─────────────┴───────────────┴───────────────────────┤
│              Status Lines (9 versions)               │
├─────────────────────────────────────────────────────┤
│              Output Styles (8 formats)               │
└─────────────────────────────────────────────────────┘
```

## Security Model

- **PreToolUse hook** validates every tool call before execution
- Comprehensive `rm` pattern detection (blocks `rm -rf`, `rm -r`, `rm --recursive`, etc.)
- `.env` file protection (blocks reads and writes to environment files)
- Whitelist-based Bash permissions in settings.json
- Hook inputs are JSON-validated; malformed payloads are rejected

## NeurogenServer Integration

This project includes agents and tooling specifically designed for the NeurogenServer platform:

- **Code Reviewer agent** — Specialized for Flask/SQLAlchemy patterns
- **Magnus Pipeline Reviewer** — Reviews the Magnus AI orchestration module (36K+ LoC)
- **UX/UI Reviewer** — Checks CMS editor and template deliverables
- **Printing Press integration** — Converts NeurogenServer's 72 OpenAPI specs into token-efficient CLIs for the Magnus AG2 pipeline

See [Printing Press Setup](Printing-Press-Setup.md) for details.
