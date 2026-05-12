# Getting Started

This guide walks you through setting up Claude Code Hooks Mastery from scratch.

## Prerequisites

### Required

1. **[Astral UV](https://docs.astral.sh/uv/getting-started/installation/)** - All hook scripts use UV single-file format
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** - Anthropic's CLI
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

### Optional

- **[Go 1.26+](https://go.dev/dl/)** - Required for CLI Printing Press
- **[ElevenLabs MCP](https://github.com/elevenlabs/elevenlabs-mcp)** - Text-to-speech notifications
- **[Firecrawl MCP](https://www.firecrawl.dev/mcp)** - Web scraping
- **[OpenAI API key](https://openai.com/)** - For LLM provider hook utilities
- **[Ollama](https://ollama.com/)** - Local LLM for hook processing

## Installation

```bash
git clone https://github.com/yourusername/claude-code-hooks-mastery.git
cd claude-code-hooks-mastery
```

Claude Code automatically detects the `.claude/settings.json` and loads all hooks, agents, and configurations.

## Verify Setup

```bash
# Start Claude Code in the project
claude

# Check hooks are loaded
/health

# Check agents are available
# Type a message and observe hook activity in the status line
```

## Project Structure

```
claude-code-hooks-mastery/
├── .claude/
│   ├── settings.json          # Main config: hooks, permissions, plugins
│   ├── hooks/                 # 13 hook lifecycle scripts (Python/UV)
│   │   ├── pre_tool_use.py    # Validates + blocks dangerous commands
│   │   ├── post_tool_use.py   # Logs + formats tool results
│   │   ├── session_start.py   # Session initialization
│   │   ├── session_end.py     # Cleanup on exit
│   │   ├── notification.py    # Notification dispatch
│   │   ├── stop.py            # Stop event handler
│   │   ├── user_prompt_submit.py
│   │   ├── subagent_start.py
│   │   ├── subagent_stop.py
│   │   ├── pre_compact.py
│   │   ├── permission_request.py
│   │   ├── post_tool_use_failure.py
│   │   └── setup.py
│   ├── agents/                # Sub-agent definitions (Markdown)
│   │   ├── code-reviewer.md
│   │   ├── meta-agent.md
│   │   ├── printing-press-builder.md
│   │   ├── magnus-pipeline-reviewer.md
│   │   ├── ux-ui-reviewer.md
│   │   ├── work-completion-summary.md
│   │   ├── team/              # Team orchestration agents
│   │   └── crypto/            # Crypto analysis agents (multi-model)
│   ├── status_lines/          # 9 status line versions (v1-v9)
│   └── output-styles/         # 8 response formatting styles
├── docs/                      # Quick reference docs
├── Documentation/             # Detailed guides
├── apps/                      # Example applications
├── specs/                     # API specifications
└── README.md
```

## Next Steps

- Read [Hook Development Guide](Hook-Development-Guide.md) to write custom hooks
- Read [Agent Development Guide](Agent-Development-Guide.md) to create sub-agents
- Read [Printing Press Setup](Printing-Press-Setup.md) for CLI generation
- Check [Architecture Overview](Architecture-Overview.md) for system design
