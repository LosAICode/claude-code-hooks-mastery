# Status Lines Reference

Status lines display real-time context in the Claude Code terminal. 9 versions are available in `.claude/status_lines/`.

## Active Status Line

Configured in `.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "uv run $CLAUDE_PROJECT_DIR/.claude/status_lines/status_line_v6.py",
    "padding": 0
  }
}
```

## Versions

| Version | File | Highlights |
|---------|------|------------|
| v1 | `status_line.py` | Basic status display |
| v2 | `status_line_v2.py` | Added git branch info |
| v3 | `status_line_v3.py` | Color-coded output |
| v4 | `status_line_v4.py` | ANSI formatting |
| v5 | `status_line_v5.py` | Context window tracking |
| **v6** | `status_line_v6.py` | **Currently active.** GSD integration |
| v7 | `status_line_v7.py` | Extended metrics |
| v8 | `status_line_v8.py` | Compact layout |
| v9 | `status_line_v9.py` | Latest iteration |

## Switching Versions

Edit the `statusLine.command` path in `.claude/settings.json` to point to a different version, or use the `update_status_line` skill.

## Writing a Custom Status Line

Status line scripts must:
1. Be a UV single-file Python script
2. Output ANSI-formatted text to stdout
3. Complete within the time budget (no long-running operations)
4. Use the `#!/usr/bin/env -S uv run --script` shebang
