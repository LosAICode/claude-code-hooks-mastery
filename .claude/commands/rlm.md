---
allowed-tools: Bash(python3 /opt/claude-code-hooks-mastery/scripts/rlm.py:*)
description: Answer a question about big files or logs without loading them into context (RLM)
argument-hint: "<question>" <file|dir|-> [more paths]
---

# RLM

Answer the question in `$ARGUMENTS` with `scripts/rlm.py`, a Recursive Language Model loop (as in NeuroGen's
`services/session_rlm_service.py`). The files stay in a Python variable. A Sonnet root model searches them in a
sandboxed REPL, Haiku sub-calls read slices, and only the answer reaches this session.

## When

- The question needs reading across a file over 50 KB, many files, or a long log, and a Grep would not settle it.
- Use Grep plus a ranged Read when you already know the string you need: that is free.

## Run

```bash
python3 /opt/claude-code-hooks-mastery/scripts/rlm.py "<question>" <paths...> --stats
# a log or command output:
<command> | python3 /opt/claude-code-hooks-mastery/scripts/rlm.py "<question>" - --stats
```

Each run costs about $0.03-0.20 and takes 20-60 s (the `--stats` line shows calls and cost). Check a cited line
with a ranged Read before you act on it. If an answer says it ran out of turns, ask a narrower question or pass
`--max-iterations 12`.
