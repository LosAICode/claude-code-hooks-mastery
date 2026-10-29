#!/usr/bin/env python3
"""Every Claude Code session the hooks have seen, newest first: what it is doing and whether it waits on you.

Reads the files written by .claude/hooks/utils/session_tracker.py (~/.claude/session-tracker/, override
SESSION_TRACKER_DIR). Ended sessions older than 24 h are hidden (--all shows them); a session not seen for
2 h and not ended shows as "stale?".

    python3 scripts/sessions.py [--all]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "utils"))
from session_tracker import (  # noqa: E402
    HIDE_ENDED_AFTER_S,
    STALE_AFTER_S,
    parse_iso,
    tracker_dir,
)

COLUMNS = [("id", 8), ("state", 16), ("seen", 5), ("task", 44), ("worktree / branch", 40), ("last commit", 40)]


def load_all(directory: Path) -> list[dict[str, Any]]:
    sessions = []
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("session_id"):
            sessions.append(data)
    return sessions


def display_state(s: dict[str, Any], now: float) -> str:
    state = str(s.get("state") or "?")
    seen = parse_iso(s.get("last_seen")) or 0
    if state != "ended" and now - seen > STALE_AFTER_S:
        return "stale?"
    if state == "waiting_on_owner":
        return "WAITING ON YOU"
    return state


def visible(sessions: list[dict[str, Any]], now: float, show_all: bool) -> list[dict[str, Any]]:
    keep = [
        s for s in sessions
        if show_all or s.get("state") != "ended" or now - (parse_iso(s.get("last_seen")) or 0) <= HIDE_ENDED_AFTER_S
    ]
    return sorted(keep, key=lambda s: parse_iso(s.get("last_seen")) or 0, reverse=True)


def age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def short_path(path: str | None) -> str:
    if not path:
        return ""
    parts = Path(path).parts
    return "/".join(parts[-2:]) if len(parts) > 2 else path


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def render(sessions: list[dict[str, Any]], now: float) -> str:
    waiting = sum(1 for s in sessions if display_state(s, now) == "WAITING ON YOU")
    live = sum(1 for s in sessions if s.get("state") != "ended")
    lines = [f"{live} live session{'s' * (live != 1)} · {waiting} waiting on you", ""]
    lines.append("  ".join(name.ljust(w) for name, w in COLUMNS).rstrip())
    for s in sessions:
        where = short_path(s.get("worktree") or s.get("cwd"))
        if s.get("branch"):
            where += f" @{s['branch']}"
        cells = [
            str(s.get("session_id", ""))[:8],
            display_state(s, now),
            age(now - (parse_iso(s.get("last_seen")) or now)),
            short_path(s.get("task")) if "/" in str(s.get("task") or "") else str(s.get("task") or ""),
            where,
            str(s.get("last_commit") or ""),
        ]
        lines.append("  ".join(fit(c, w).ljust(w) for c, (_, w) in zip(cells, COLUMNS)).rstrip())
    if not sessions:
        lines.append("(no sessions)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--all", action="store_true", help="include sessions that ended more than 24 h ago")
    args = ap.parse_args(argv)
    directory = tracker_dir()
    if not directory.is_dir():
        print(f"No tracker files yet ({directory}).")
        return 0
    now = time.time()
    print(render(visible(load_all(directory), now, args.all), now))
    return 0


if __name__ == "__main__":
    sys.exit(main())
