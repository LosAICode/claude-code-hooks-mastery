# Settings Reference

Project settings are in `.claude/settings.json`.

## Permissions

```json
{
  "permissions": {
    "allow": [
      "Bash(mkdir:*)", "Bash(uv:*)", "Bash(find:*)", "Bash(mv:*)",
      "Bash(grep:*)", "Bash(npm:*)", "Bash(ls:*)", "Bash(cp:*)",
      "Bash(chmod:*)", "Bash(touch:*)",
      "Bash(printing-press:*)", "Bash(pp-*:*)", "Bash(go:*)",
      "Write", "Edit"
    ]
  }
}
```

## Enabled Plugins

| Plugin | Source | Purpose |
|--------|--------|---------|
| `skill-creator` | claude-plugins-official | Create new skills |
| `superpowers` | claude-plugins-official | Extended skill workflows |
| `context-mode` | context-mode | Context window protection via MCP |
| `claude-mem` | thedotmack | Persistent memory via MCP |

## Modifying Settings

Use the `/update-config` skill for safe settings modification, or edit `.claude/settings.json` directly.
