# Hooks Reference

Quick reference for all 13 Claude Code hook lifecycle events implemented in this project.

## Hook Scripts

All hooks are UV single-file Python scripts in `.claude/hooks/`. They receive JSON payloads via stdin and control execution flow via exit codes.

| Script | Hook Event | Purpose |
|--------|-----------|---------|
| `setup.py` | Setup | Init/maintenance tasks at project setup |
| `session_start.py` | SessionStart | Runs on startup, resume, or `/clear` |
| `session_end.py` | SessionEnd | Cleanup on exit, SIGINT, or error |
| `pre_tool_use.py` | PreToolUse | Validates tool calls before execution. Blocks dangerous commands (`rm -rf`, `.env` access) and whole-file Reads over 50 KB (use `/rlm`, see [rlm.md](rlm.md)) |
| `post_tool_use.py` | PostToolUse | Logs and formats tool results after execution |
| `post_tool_use_failure.py` | PostToolUseFailure | Handles tool execution failures |
| `user_prompt_submit.py` | UserPromptSubmit | Processes user input. Flags: `--log-only`, `--store-last-prompt`, `--name-agent` |
| `notification.py` | Notification | Dispatches notifications. Flag: `--notify` |
| `stop.py` | Stop | Runs when Claude stops responding. Flag: `--chat` |
| `subagent_start.py` | SubagentStart | Fires when a sub-agent is spawned |
| `subagent_stop.py` | SubagentStop | Fires when a sub-agent completes. Flag: `--notify` |
| `pre_compact.py` | PreCompact | Runs before context compaction |
| `permission_request.py` | PermissionRequest | Logs permission prompts. Flag: `--log-only` |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Allow / continue |
| Non-zero | Block / fail the tool call |

## Configuration

Hooks are registered in `.claude/settings.json` under the `hooks` key:

```json
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "uv run $CLAUDE_PROJECT_DIR/.claude/hooks/pre_tool_use.py"
      }]
    }]
  }
}
```

An empty `matcher` means the hook runs for all tool calls. Use specific matchers like `"Bash"` or `"Write"` to scope hooks to specific tools.

## Adding a New Hook

1. Create a new `.py` file in `.claude/hooks/`
2. Use the UV single-file script header:
   ```python
   #!/usr/bin/env -S uv run --script
   # /// script
   # requires-python = ">=3.8"
   # ///
   ```
3. Read JSON payload from stdin, write response to stdout
4. Register in `.claude/settings.json`
