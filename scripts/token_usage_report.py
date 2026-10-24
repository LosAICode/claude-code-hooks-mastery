#!/usr/bin/env python3
"""Where Claude Code tokens go: context size per call, long sessions, subagents, big reads.

Read-only. Scans the session transcripts under ~/.claude/projects and prints the numbers that drive cost:
every API call re-sends the whole context, so cache reads = context size x number of calls.

    python3 scripts/token_usage_report.py [--days 7] [--top 10]
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
LONG_CONTEXT = 300_000
BIG_RESULT_CHARS = 40_000  # ~10k tokens


@dataclass
class Session:
    path: Path
    subagent: bool
    calls: int = 0
    cache_read: int = 0
    cache_create: int = 0
    output: int = 0
    max_context: int = 0
    first_context: int = 0


def scan(
    path: Path,
    reads: Counter[str],
    result_chars: Counter[str],
    big_results: Counter[str],
) -> Session:
    session = Session(
        path, subagent="subagents" in path.parts or path.name.startswith("agent-")
    )
    seen: set[str] = set()
    tool_names: dict[str, str] = {}
    with path.open(errors="ignore") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if entry.get("type") == "assistant":
                usage = message.get("usage")
                msg_id = message.get("id")
                if usage and msg_id not in seen:
                    seen.add(msg_id)
                    context = (
                        usage.get("input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                    )
                    session.calls += 1
                    session.cache_read += usage.get("cache_read_input_tokens", 0)
                    session.cache_create += usage.get("cache_creation_input_tokens", 0)
                    session.output += usage.get("output_tokens", 0)
                    session.max_context = max(session.max_context, context)
                    session.first_context = session.first_context or context
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_names[block.get("id", "")] = block.get("name", "?")
                        tool_input = block.get("input") or {}
                        if (
                            block.get("name") == "Read"
                            and "offset" not in tool_input
                            and "limit" not in tool_input
                        ):
                            reads[str(tool_input.get("file_path", ""))] += 1
            elif entry.get("type") == "user" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        name = tool_names.get(block.get("tool_use_id", ""), "?")
                        size = len(json.dumps(block.get("content")))
                        result_chars[name] += size
                        if size > BIG_RESULT_CHARS:
                            big_results[name] += 1
    return session


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    cutoff = time.time() - args.days * 86400
    files = [p for p in PROJECTS.rglob("*.jsonl") if p.stat().st_mtime > cutoff]
    reads: Counter[str] = Counter()
    result_chars: Counter[str] = Counter()
    big_results: Counter[str] = Counter()
    sessions = [
        s for s in (scan(p, reads, result_chars, big_results) for p in files) if s.calls
    ]
    if not sessions:
        print(
            f"No transcripts with usage in the last {args.days} days under {PROJECTS}"
        )
        return

    total_read = sum(s.cache_read for s in sessions)
    total_calls = sum(s.calls for s in sessions)
    subagents = [s for s in sessions if s.subagent]
    long_ones = [s for s in sessions if s.max_context > LONG_CONTEXT]
    firsts = sorted(s.first_context for s in sessions if not s.subagent)

    print(
        f"Last {args.days} days: {len(sessions)} sessions ({len(subagents)} subagents), {total_calls:,} API calls"
    )
    print(
        f"  cache read     {total_read / 1e9:6.2f}B   avg context/call {total_read / total_calls / 1e3:5.0f}k"
    )
    print(f"  cache create   {sum(s.cache_create for s in sessions) / 1e6:6.0f}M")
    print(f"  output         {sum(s.output for s in sessions) / 1e6:6.1f}M")
    print(
        f"  subagent share of reads  {sum(s.cache_read for s in subagents) / total_read:4.0%}"
    )
    print(
        f"  sessions past {LONG_CONTEXT // 1000}k context: {len(long_ones)}, "
        f"{sum(s.cache_read for s in long_ones) / total_read:4.0%} of reads"
    )
    if firsts:
        print(
            f"  base context (first call, median main session) {firsts[len(firsts) // 2] / 1e3:.0f}k"
        )

    print(f"\nTop {args.top} sessions by cache read:")
    for s in sorted(sessions, key=lambda s: s.cache_read, reverse=True)[: args.top]:
        kind = "sub " if s.subagent else "main"
        print(
            f"  {s.cache_read / 1e6:7.0f}M  {s.calls:5d} calls  max {s.max_context / 1e3:4.0f}k  {kind} {s.path.stem[:40]}"
        )

    print("\nTool results by size (~tokens):")
    for name, chars in result_chars.most_common(6):
        print(
            f"  {name:12s} {chars / 4 / 1e6:6.1f}M   results >10k tokens: {big_results[name]}"
        )

    print("\nMost whole-file Reads (no offset/limit):")
    for path, count in reads.most_common(args.top):
        print(f"  {count:4d}  {path}")


if __name__ == "__main__":
    main()
