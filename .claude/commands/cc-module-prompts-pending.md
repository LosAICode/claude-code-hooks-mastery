---
description: Full paths of every prompt filed in a module's Pending/ folder, by module (optional module name; --status S narrows); flags any prompt whose status belongs in the other folder
allowed-tools: Bash(python3 /opt/claude-code-hooks-mastery/scripts/command_center.py:*)
---

Run `python3 /opt/claude-code-hooks-mastery/scripts/command_center.py prompts pending`, adding `--module <name>` when
$ARGUMENTS starts with a bare module name (e.g. `assistants`) and passing any other flags in $ARGUMENTS as is:

$ARGUMENTS

Show the output as is, in a code block. If it ends with prompts out of place (exit 1), say so in one line and offer
`command_center.py sort` (it moves them by status and commits); do not run it unasked.

Do nothing else.
