# Hook Development Guide

How to write, test, and register custom Claude Code hooks.

## Hook Anatomy

Every hook is a UV single-file Python script:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.8"
# dependencies = ["some-package"]  # optional
# ///

import sys
import json

def main():
    # Read the JSON payload from stdin
    payload = json.loads(sys.stdin.read())

    # Your logic here
    tool_name = payload.get("tool_name", "")

    # Exit 0 = allow, non-zero = block
    if should_block(tool_name):
        # Output a message explaining the block
        result = {"error": "Blocked: reason here"}
        print(json.dumps(result))
        sys.exit(1)

    # Allow with optional modifications
    result = {"continue": True}
    print(json.dumps(result))
    sys.exit(0)

if __name__ == "__main__":
    main()
```

## Hook Types and Payloads

### PreToolUse

Fires before every tool call. Can block or modify the call.

**Payload:**
```json
{
  "tool_name": "Bash",
  "tool_input": {
    "command": "ls -la",
    "description": "List files"
  },
  "session_id": "abc123"
}
```

**Response (block):**
```json
{"error": "BLOCKED: Dangerous command detected"}
```

**Response (allow):**
```json
{"continue": true}
```

### PostToolUse

Fires after tool execution. Can transform the result.

**Payload:**
```json
{
  "tool_name": "Bash",
  "tool_input": {"command": "ls"},
  "tool_result": "file1.txt\nfile2.txt",
  "session_id": "abc123"
}
```

### UserPromptSubmit

Fires when the user submits a message.

**Payload:**
```json
{
  "prompt": "Fix the login bug",
  "session_id": "abc123"
}
```

### SessionStart / SessionEnd

Fires on session lifecycle events.

**Payload:**
```json
{
  "type": "startup",
  "session_id": "abc123"
}
```

Types: `startup`, `resume`, `clear` (SessionStart) or `exit`, `sigint`, `error` (SessionEnd).

### SubagentStart / SubagentStop

Fires when sub-agents spawn or complete.

### Notification

Fires on notification events. Use with `--notify` flag.

### Stop

Fires when Claude stops responding. Use with `--chat` flag.

### PreCompact

Fires before context compaction.

### PermissionRequest

Fires when a permission prompt appears.

### PostToolUseFailure

Fires when a tool call fails.

### Setup

Fires during project init/maintenance.

## Registering a Hook

Add to `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "uv run $CLAUDE_PROJECT_DIR/.claude/hooks/my_hook.py"
          }
        ]
      }
    ]
  }
}
```

### Matcher Patterns

| Matcher | Scope |
|---------|-------|
| `""` | All tool calls |
| `"Bash"` | Only Bash tool calls |
| `"Write"` | Only Write tool calls |
| `"Edit"` | Only Edit tool calls |

## Testing a Hook

```bash
# Test locally by piping a payload
echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf /"}}' | \
  uv run .claude/hooks/pre_tool_use.py

# Check exit code
echo $?  # 1 = blocked, 0 = allowed
```

## Best Practices

1. **Fail open** for non-critical hooks (exit 0 on unexpected input)
2. **Fail closed** for security hooks (exit 1 on unparseable input)
3. Keep hooks fast — they run on every tool call
4. Log to files, not stdout (stdout is the response channel)
5. Use `--log-only` flags for observability hooks that shouldn't block
6. Add dependencies in the UV `# /// script` metadata block
