#!/usr/bin/env python3
"""Record a /verify: the sha of main it covers. The last step of /verify before any restart (owner 11-01).

The PreToolUse verify gate (session_tracker.verify_gate) blocks a restart while code (board_gate.code_paths) changed
on main since the newest record. Records go to ~/.claude/session-tracker/deploys/verified.jsonl (override
SESSION_TRACKER_DIR); the repo and branch come from board_gate in the tracker config.

    python3 scripts/verify_record.py --summary "verify: PASS, no fixes (3644a7f70, 2325caa86)" [--sha SHA]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "utils"))
import session_tracker as st  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--summary", required=True, help="the verify result, one line")
    ap.add_argument("--sha", help="the commit verified (default: the tip of board_gate.branch)")
    args = ap.parse_args()
    summary = args.summary.strip()
    if not summary:
        print("--summary is empty: say what the verify found", file=sys.stderr)
        return 2
    gate = st.load_config().get("board_gate") or {}
    repo, branch = str(gate.get("repo") or ""), str(gate.get("branch") or "main")
    if not repo:
        print("no board_gate.repo in the tracker config: nothing to record against", file=sys.stderr)
        return 2
    try:
        _, sha = st.run_git(repo, "rev-parse", "--verify", f"{args.sha or branch}^{{commit}}")
    except st.GitError as e:
        print(f"not a commit in {repo}: {args.sha or branch} ({e})", file=sys.stderr)
        return 2
    rec = st.record_verify(sha.strip(), summary, os.environ.get("CLAUDE_SESSION_ID", ""))
    print(f"verified {rec['sha'][:9]} ({branch} in {repo}): {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
