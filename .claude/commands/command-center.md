---
description: Prompt progress per module (done, on main, running and by whom, pending, blocked on owner); --module M, --all, --board, --html, set, migrate
allowed-tools: Bash(python3 /opt/claude-code-hooks-mastery/scripts/command_center.py:*)
---

Run `python3 /opt/claude-code-hooks-mastery/scripts/command_center.py $ARGUMENTS` and show its output as is, in a
code block. Then, in one line, name anything listed under "Stalled" or "Blocked on owner" (if any). Do nothing else.
