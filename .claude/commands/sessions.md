---
description: List every Claude Code session the hooks have seen (state, task, worktree, last commit)
allowed-tools: Bash(python3 /opt/claude-code-hooks-mastery/scripts/sessions.py:*)
---

Run `python3 /opt/claude-code-hooks-mastery/scripts/sessions.py $ARGUMENTS` and show its output as is, in a code
block. Then, in one line, name the sessions in state "WAITING ON YOU" or "stale?" (if any). Do nothing else.
