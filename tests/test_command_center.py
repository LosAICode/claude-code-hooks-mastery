import json

import pytest

import command_center as cc
import prompt_center as pc
from conftest import cfg, git
from test_prompt_join import NOW, session, write_sessions

MARK_BLOCK = "<!-- status:begin -->\nold\n<!-- status:end -->"


@pytest.fixture
def center(proj, tmp_path, monkeypatch):
    """A project with a few more prompts, a module README with markers, a board, and two sessions."""
    for name, text in {
        "funnels/Funnels-B12-4-Webinar.md": "---\nstatus: pending\norder: 9d\n---\n# B12-4: webinar honesty\n",
        "funnels/Funnels-B11-Perf.md": "---\nstatus: deployed\norder: 7\ncommit: c4201b3d3\n---\n# B11: performance\n",
        "funnels/Funnels-B8-Sec.md": "---\nstatus: blocked\norder: 5\nneeds_owner: PSL = owner\n---\n# B8: security sweep\n",
        "history/History-H7-Polish.md": "---\nstatus: running\nsession_id: gone0000-0000\norder: 7\n---\n# H7: feed polish\n",
    }.items():
        (proj / "docs/Prompts" / name).write_text(text)
    (proj / "docs/Prompts/funnels/Funnels-00-README.md").write_text(f"# Funnels\n\nintro\n\n{MARK_BLOCK}\n\n## Order (frozen)\n| a |\n")
    (proj / "docs/Prompts/README.md").write_text(f"# Prompts board\n\nintro\n\n{MARK_BLOCK}\n")
    tracker = tmp_path / "tracker"
    b4 = "docs/Prompts/funnels/Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md"
    write_sessions(tracker, [
        session("d60450f0-1111", prompt_file=b4, last_commit="9718648ea Merge B12-3", branch="main", worktree="/x/funnels-b4"),
        session("c5588aac-2222", state="waiting_on_owner", seen=41, task="Run docs/Old-2026-10-28/History-H8b-Paid-Paths-Without-Rows.md"),
    ])
    c = cfg(proj)
    c["prompts"]["board"] = "docs/Prompts/README.md"
    c["prompts"]["html"] = str(tmp_path / "out" / "command-center.html")
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    (tmp_path / "config.json").write_text(json.dumps(c))
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    return proj


def run(*args):
    return cc.main(["--now", NOW.isoformat(timespec="seconds"), *args])


def test_terminal_summary(center, capsys):
    assert run() == 0
    out = capsys.readouterr().out
    assert out.startswith("Prompt command center · 2026-10-30 16:00 · 2 running · 1 stalled · 1 blocked on owner")
    assert "funnels       1/5 done · 1 on main (held) · 1 running (d60450f0, 5m) · 1 pending · 1 blocked" in out
    assert "history       0/2 done · 1 running (c5588aac, 41m) · 1 stalled" in out
    assert "Running now" in out and "B4" in out and "d60450f0  working" in out
    assert "Stalled" in out and "H7" in out and "gone0000" in out
    assert "Blocked on owner" in out and "B8" in out and "PSL = owner" in out
    assert "Next up" in out and "B12-4" in out


def test_terminal_module_rows(center, capsys):
    assert run("--module", "funnels") == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0].startswith("funnels       1/5 done")
    assert any(line.startswith("order  id      status      ") for line in lines)
    rows = [line for line in lines if line.startswith(("9c", "9d", "7 ", "5 ", "B4"))]
    assert any("9c     B12-3   on-main" in row for row in rows)
    assert any("B4      running     " in row and "d60450f0" in row for row in rows)
    assert not any("H7" in line for line in lines)


def test_unknown_module_fails(center, capsys):
    assert run("--module", "nope") == 2
    assert "no prompts in module 'nope'" in capsys.readouterr().err


def test_json_output(center, capsys):
    assert run("--json") == 0
    data = json.loads(capsys.readouterr().out)
    funnels = next(m for m in data["modules"] if m["module"] == "funnels")
    assert funnels["finished"] == 1 and funnels["total"] == 5
    assert data["stalled"][0]["id"] == "H7" and data["running"][0]["session"] == "d60450f0"


def test_replace_between_markers():
    text = "a\n<!-- status:begin -->\nold\n<!-- status:end -->\nb\n"
    assert cc.replace_between_markers(text, "new\n") == "a\n<!-- status:begin -->\nnew\n<!-- status:end -->\nb\n"
    assert cc.replace_between_markers("no markers\n", "new\n") is None


def test_board_regenerates_readme_and_board_only_when_changed(center, capsys):
    assert run("--board") == 0
    out = capsys.readouterr().out
    readme = (center / "docs/Prompts/funnels/Funnels-00-README.md").read_text()
    assert "intro\n" in readme and "## Order (frozen)\n| a |\n" in readme
    assert "| # | Prompt | Status | Owner | Commit | Issue | Updated | Notes |" in readme
    assert "| 9c | `Funnels-B12-3-Step-Analytics-CSV.md` | 🔵 on-main |" in readme
    assert "| 7 | `Funnels-B11-Perf.md` | 🟡 deployed |  | `c4201b3d3` |" in readme
    assert "▶ running (d60450f0)" in readme
    board = (center / "docs/Prompts/README.md").read_text()
    assert "| Module | Done | Progress | Running | Pending | Blocked |" in board
    assert "| [funnels](funnels/Funnels-00-README.md) | 1/5 |" in board
    assert "Funnels-00-README.md" in out and "README.md" in out
    run("--board")
    assert capsys.readouterr().out.strip() == "nothing changed"


def test_board_leaves_readme_without_markers_alone(center, capsys):
    p = center / "docs/Prompts/history/History-00-README.md"
    p.write_text("# History\nno markers\n")
    run("--board")
    assert p.read_text() == "# History\nno markers\n"
    assert "History-00-README.md: no status markers" in capsys.readouterr().out


def test_html_dashboard(center, capsys):
    assert run("--html") == 0
    path = capsys.readouterr().out.strip()
    html = open(path).read()
    assert html.startswith("<!doctype html>") and "<script src=" not in html and "<link" not in html
    assert "funnels" in html and "B12-3" in html and "d60450f0" in html and "progress" in html


def test_set_updates_prompt_readme_board_and_commits(center, capsys):
    git(center, "init", "-q", "-b", "main")
    git(center, "add", "-A")
    git(center, "commit", "-q", "-m", "init")
    (center / "docs/unrelated.md").write_text("dirty\n")
    assert run("set", "B12-4", "on-main", "--commit", "abc1234", "--notes", "held for B13") == 0
    text = (center / "docs/Prompts/funnels/Funnels-B12-4-Webinar.md").read_text()
    assert "status: on-main\n" in text and "commit: abc1234\n" in text and "notes: held for B13\n" in text
    assert "# B12-4: webinar honesty\n" in text
    assert "| 9d | `Funnels-B12-4-Webinar.md` | 🔵 on-main |" in (center / "docs/Prompts/funnels/Funnels-00-README.md").read_text()
    assert git(center, "log", "-1", "--format=%s") == "docs(prompts): funnels B12-4 → on-main"
    assert git(center, "status", "--porcelain") == "?? docs/unrelated.md"
    assert "committed" in capsys.readouterr().out


def test_set_by_file_name_and_no_commit(center, capsys):
    assert run("set", "Funnels-B8-Sec.md", "pending", "--no-commit") == 0
    assert "status: pending\n" in (center / "docs/Prompts/funnels/Funnels-B8-Sec.md").read_text()


def test_set_ambiguous_or_unknown_id(center, capsys):
    (center / "docs/Prompts/history/History-B12-4-Dup.md").write_text("# dup\n")
    assert run("set", "B12-4", "done", "--no-commit") == 2
    assert "ambiguous" in capsys.readouterr().err
    assert run("set", "funnels/B12-4", "done", "--no-commit") == 0
    assert run("set", "Z99", "done", "--no-commit") == 2


def test_set_clears_todo_confirm(center):
    p = center / "docs/Prompts/funnels/Funnels-B8-Sec.md"
    p.write_text("---\nstatus: pending\norder: 5\ntodo: confirm\n---\n# B8: security sweep\n")
    assert run("set", "funnels/B8", "deployed", "--no-commit") == 0
    text = p.read_text()
    assert "todo" not in text and "status: deployed\n" in text and text.endswith("# B8: security sweep\n")


def test_write_meta_none_removes_key(tmp_path):
    p = tmp_path / "P.md"
    p.write_text("---\nstatus: pending\ntodo: confirm\n---\n# T\n")
    assert pc.write_meta(p, {"todo": None}) == {"status": "pending"}
    assert p.read_text() == "---\nstatus: pending\n---\n# T\n"


def test_claim_attaches_a_live_session_and_arms_the_gate(center, capsys):
    import session_tracker as st

    assert run("claim", "funnels/B12-4", "--session", "d60450f0", "--no-commit") == 0
    m = pc.parse_frontmatter((center / "docs/Prompts/funnels/Funnels-B12-4-Webinar.md").read_text())[0]
    assert m["status"] == "running" and m["session_id"] == "d60450f0-1111"
    s = st.load_session("d60450f0-1111")
    assert s["prompt_file"] == "docs/Prompts/funnels/Funnels-B12-4-Webinar.md" and s["prompt_claimed"] is True
    assert s["prompt_prev_status"] == "pending"
    assert "funnels B12-4: pending → running (d60450f0)" in capsys.readouterr().out


def test_claim_refuses_a_gone_session_or_a_finished_prompt(center, capsys):
    assert run("claim", "history/H7", "--session", "gone0000-0000", "--no-commit") == 2
    assert "not a live session" in capsys.readouterr().err
    assert run("claim", "funnels/B11", "--session", "d60450f0-1111", "--no-commit") == 2
    assert "deployed" in capsys.readouterr().err
    assert "status: deployed" in (center / "docs/Prompts/funnels/Funnels-B11-Perf.md").read_text()


def test_claim_takes_over_a_stalled_prompt(center):
    assert run("claim", "history/H7", "--session", "c5588aac-2222", "--no-commit") == 0
    m = pc.parse_frontmatter((center / "docs/Prompts/history/History-H7-Polish.md").read_text())[0]
    assert m["status"] == "running" and m["session_id"] == "c5588aac-2222"


def test_module_table_has_an_issue_column(center):
    p = center / "docs/Prompts/funnels/Funnels-B12-4-Webinar.md"
    p.write_text("---\nstatus: pending\norder: 9d\nissue: \"612\"\n---\n# B12-4: webinar honesty\n")
    view = cc.View(pc.prompts_cfg and __import__("session_tracker").load_config(), NOW.timestamp())
    m = next(m for m in view.modules if m["module"] == "funnels")
    table = cc.render_module_table(view, m)
    assert table.startswith("| # | Prompt | Status | Owner | Commit | Issue | Updated | Notes |")
    assert "| 9d | `Funnels-B12-4-Webinar.md` | ⚪ pending |  |  | #612 |" in table
