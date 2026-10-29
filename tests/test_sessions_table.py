import time
from datetime import datetime

import sessions


def iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


NOW = time.time()


def s(sid, state, seen_ago, **kw):
    return {"session_id": sid, "state": state, "last_seen": iso(NOW - seen_ago), **kw}


def test_newest_first_and_old_ended_hidden():
    rows = [s("old-ended", "ended", 25 * 3600), s("a-recent", "working", 10), s("b-older", "idle", 600),
            s("c-ended", "ended", 3600)]
    shown = [r["session_id"] for r in sessions.visible(rows, NOW, show_all=False)]
    assert shown == ["a-recent", "b-older", "c-ended"]
    shown_all = [r["session_id"] for r in sessions.visible(rows, NOW, show_all=True)]
    assert shown_all[-1] == "old-ended"


def test_stale_when_not_seen_for_2h_and_not_ended():
    assert sessions.display_state(s("x", "working", 2 * 3600 + 5), NOW) == "stale?"
    assert sessions.display_state(s("x", "ended", 5 * 3600), NOW) == "ended"
    assert sessions.display_state(s("x", "waiting_on_owner", 60), NOW) == "WAITING ON YOU"


def test_render_has_summary_task_branch_and_commit():
    rows = [s("12345678abcdef", "waiting_on_owner", 30, task="/opt/p/Documentation/Prompts/Task-A.md",
              worktree="/var/lib/wt/feature-x", branch="feature-x", last_commit="abc123def feat: thing"),
            s("87654321", "idle", 90)]
    out = sessions.render(rows, NOW)
    assert out.splitlines()[0] == "2 live sessions · 1 waiting on you"
    line = out.splitlines()[3]
    assert line.startswith("12345678  WAITING ON YOU")
    assert "Prompts/Task-A.md" in line and "wt/feature-x @feature-x" in line and "abc123def feat: thing" in line


def test_empty_table():
    assert "(no sessions)" in sessions.render([], NOW)
