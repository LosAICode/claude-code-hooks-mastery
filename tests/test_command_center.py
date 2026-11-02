import json
from pathlib import Path

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
    assert "\nSessions\n" in out and "B4" in out and "d60450f0  working" in out
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
    assert "status: on-main\n" in text and "commits: [abc1234]\n" in text and "notes: held for B13\n" in text
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
    assert run("set", "B12-4", "done", "--no-deploy", "fixture", "--proof", "fixture", "--no-commit") == 2
    assert "ambiguous" in capsys.readouterr().err
    assert run("set", "funnels/B12-4", "done", "--no-deploy", "fixture", "--proof", "fixture", "--no-commit") == 0
    assert run("set", "Z99", "done", "--no-deploy", "fixture", "--proof", "fixture", "--no-commit") == 2


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
    write_sessions(center.parent / "tracker", [session("ended000-3333", state="ended"), session("stale000-4444", seen=300)])
    for sid in ("ended000-3333", "stale000-4444"):
        assert run("claim", "funnels/B12-4", "--session", sid, "--no-commit") == 2
        assert "not a live session" in capsys.readouterr().err
    # CC14: a deployed prompt is taken over (status kept); a done one is never claimed
    assert run("claim", "funnels/B11", "--session", "d60450f0-1111", "--no-commit") == 0
    assert "taken over" in capsys.readouterr().out
    assert "status: deployed" in (center / "docs/Prompts/funnels/Funnels-B11-Perf.md").read_text()
    pc.set_status(pc.prompts_cfg(cc.load_config()), "docs/Prompts/funnels/Funnels-B11-Perf.md", "done")
    assert run("claim", "funnels/B11", "--session", "d60450f0-1111", "--no-commit") == 2
    assert "done" in capsys.readouterr().err


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


def test_json_prompts_lists_every_prompt_with_its_frontmatter(center, capsys):
    assert run("--json", "--prompts") == 0
    data = json.loads(capsys.readouterr().out)
    rows = {r["name"]: r for r in data["prompts"]}
    assert rows["Funnels-B11-Perf.md"]["meta"] == {"status": "deployed", "order": "7", "commit": "c4201b3d3"}
    assert rows["Funnels-B11-Perf.md"]["status"] == "deployed" and rows["History-H7-Polish.md"]["effective"] == "stalled"
    assert "modules" in data
    assert run("--json") == 0
    assert "prompts" not in json.loads(capsys.readouterr().out)


def test_set_clear_and_empty_needs_owner_remove_the_key(center):
    p = center / "docs/Prompts/funnels/Funnels-B8-Sec.md"
    assert run("set", "funnels/B8", "pending", "--needs-owner", "", "--clear", "order", "--no-commit") == 0
    m = pc.parse_frontmatter(p.read_text())[0]
    assert "needs_owner" not in m and "order" not in m and m["status"] == "pending"
    assert run("set", "funnels/B8", "pending", "--clear", "status", "--no-commit") == 2  # never the status


def test_audit_flags_data_problems(center, capsys):
    root = center / "docs/Prompts"
    (root / "Funnels" / "Completed").mkdir(parents=True)
    (root / "Funnels/Completed/Funnels-B2-Old.md").write_text("---\nstatus: pending\n---\n# B2: old\n")
    (root / "funnels/Funnels-B5-Shipped.md").write_text("---\nstatus: on-main\ntodo: confirm\n---\n# B5\n")
    (root / "funnels/Funnels-B11-Twin.md").write_text("---\nid: B11\nstatus: pending\n---\n# B11 twin\n")
    (root / "funnels/Notes-About-Things.md").write_text("---\nmodule: funnels\nstatus: pending\n---\n# notes\n")
    (root / "History").mkdir()
    (root / "History/History-H9-X.md").write_text("---\nstatus: pending\n---\n# H9\n")
    assert run("audit") == 1
    out = capsys.readouterr().out
    assert "funnels B5: on-main with no commit" in out
    assert "funnels B5: todo: confirm left over" in out
    assert "Funnels-B2-Old.md: status pending but filed under Completed/" in out
    assert "funnels B11: id doubled (Funnels-B11-Perf.md, Funnels-B11-Twin.md)" in out
    assert "Notes-About-Things.md: no id" in out
    assert "lower-case module folder docs/Prompts/funnels/ (capitalized twin: History/" not in out
    assert "lower-case module folder docs/Prompts/funnels/" in out


def test_audit_clean_tree_exits_0(center, capsys, tmp_path):
    root = center / "docs/Prompts"
    for f in list((center / "docs").rglob("*.md")):
        f.unlink()
    (root / "Funnels").mkdir()
    (root / "Funnels/Funnels-B1-X.md").write_text("---\nstatus: pending\n---\n# B1\n")
    assert run("audit") == 0
    assert "nothing to report" in capsys.readouterr().out


STATUS_BOARD = """# Status Board

| Workstream | State | Last landed (main) | Next | Detail |
|---|---|---|---|---|
| Funnels backlog B1-B12 | 🟢 B4-B9 live; 🔵 **B11 ON MAIN, deploy HELD** · B12-4 merged, held | `abc` | B13 | x |
| History module pass | 🟢 **H7 DEPLOYED** 10-30 | `def` | H8 | x |
| Magnus build reliability | 🟢 B11 live | `123` | — | x |
"""


def test_board_check_flags_rows_that_disagree_with_prompt_status(center, capsys, tmp_path):
    (center / "docs/Status-Board.md").write_text(STATUS_BOARD)
    c = json.loads((tmp_path / "config.json").read_text())
    c["prompts"]["status_board"] = "docs/Status-Board.md"
    (tmp_path / "config.json").write_text(json.dumps(c))
    before = (center / "docs/Status-Board.md").read_text()
    assert run("board-check") == 1
    out = capsys.readouterr().out
    assert 'Funnels backlog B1-B12: says HELD/ON MAIN for funnels B11, which is deployed' in out
    assert 'History module pass: says DEPLOYED/LIVE for history H7, which is running' in out
    assert "B12-4" not in out  # pending prompt, "held" claim: not a contradiction of a deployed/done one
    assert "Magnus" not in out  # a row that names no module is skipped
    assert (center / "docs/Status-Board.md").read_text() == before  # never edits the prose


def test_next_up_ranks_prompts_needing_no_owner_first(center, capsys):
    (center / "docs/Prompts/history/History-H9-Next.md").write_text(
        "---\nstatus: pending\norder: 9\nneeds_owner: cost OK\n---\n# H9: next\n")
    (center / "docs/Prompts/history/History-H7-Polish.md").write_text("---\nstatus: done\norder: 7\n---\n# H7\n")
    (center / "docs/Prompts/history/History-H8b-Paid-Paths-Without-Rows.md").write_text("---\nstatus: done\n---\n# H8b\n")
    (center / "docs/Prompts/funnels/Funnels-B12-4-Webinar.md").write_text(
        "---\nstatus: pending\norder: 9d\nneeds_owner: \"no\"\n---\n# B12-4: webinar honesty\n")
    assert run() == 0
    out = capsys.readouterr().out
    nxt = out.split("Next up", 1)[1].strip().splitlines()
    b12, h9 = (next(i for i, line in enumerate(nxt) if ident in line) for ident in ("B12-4", "H9"))
    assert b12 < h9 and h9 == len(nxt) - 1  # the one that needs the owner comes last
    assert "⚑ needs owner: cost OK" in nxt[h9] and "needs owner" not in nxt[b12]


def _with_status_board(center, tmp_path):
    (center / "docs/Status-Board.md").write_text(STATUS_BOARD)
    c = json.loads((tmp_path / "config.json").read_text())
    c["prompts"]["status_board"] = "docs/Status-Board.md"
    (tmp_path / "config.json").write_text(json.dumps(c))
    return center / "docs/Status-Board.md"


def test_board_markers_go_in_once_per_module_and_set_refreshes_them(center, capsys, tmp_path):
    board = _with_status_board(center, tmp_path)
    assert run("board-markers", "--no-commit") == 0
    lines = board.read_text().splitlines()
    funnels = next(line for line in lines if line.startswith("| Funnels backlog"))
    assert funnels.startswith("| Funnels backlog B1-B12 | <!-- prompts:funnels -->**Prompts:** ")
    assert funnels.endswith("<!-- /prompts --> 🟢 B4-B9 live; 🔵 **B11 ON MAIN, deploy HELD** · B12-4 merged, held | `abc` | B13 | x |")
    assert "🔴 blocked B8" in funnels and "⚪ 1 pending (next B12-4)" in funnels
    assert "<!-- prompts:history -->" in next(line for line in lines if line.startswith("| History"))
    assert "prompts:" not in next(line for line in lines if line.startswith("| Magnus"))
    once = board.read_text()
    assert run("board-markers", "--no-commit") == 0 and board.read_text() == once  # idempotent
    assert run("set", "funnels/B12-4", "on-main", "--commit", "abc1234", "--no-commit") == 0
    assert "🔵 on-main B12-3, B12-4" in board.read_text()


def test_board_check_ignores_the_generated_lines(center, capsys, tmp_path):
    board = _with_status_board(center, tmp_path)
    assert run("board-markers", "--no-commit") == 0
    # a stale generated line (refresh skipped while the board was dirty) is not a hand-written claim
    board.write_text(board.read_text() + "| Funnels later | <!-- prompts:funnels -->🔵 on-main B12-3 · 🔵 B11 ON MAIN<!-- /prompts --> | x | x | x |\n")
    capsys.readouterr()
    run("board-check")
    out = capsys.readouterr().out
    assert out.count("funnels B11") == 1 and "Prompts:" not in out


def test_a_status_board_with_someone_elses_edits_is_left_alone(center, capsys, tmp_path):
    board = _with_status_board(center, tmp_path)
    assert run("board-markers", "--no-commit") == 0
    git(center, "init", "-q", "-b", "main")
    git(center, "add", "-A")
    git(center, "commit", "-q", "-m", "init")
    board.write_text(board.read_text() + "\nanother session's line\n")
    before = board.read_text()
    assert run("set", "funnels/B12-4", "on-main", "--no-commit") == 0
    out = capsys.readouterr().out
    assert board.read_text() == before and "uncommitted edits" in out
    assert run("board-markers", "--no-commit") == 2


def test_board_markers_in_a_git_repo_fill_and_commit_in_one_go(center, capsys, tmp_path):
    board = _with_status_board(center, tmp_path)
    git(center, "init", "-q", "-b", "main")
    git(center, "add", "-A")
    git(center, "commit", "-q", "-m", "init")
    assert run("board-markers") == 0
    assert "<!-- prompts:funnels -->**Prompts:** 🟢 1/5" in board.read_text()
    assert git(center, "status", "--porcelain", "--", "docs/Status-Board.md") == ""
    assert git(center, "log", "-1", "--format=%s").startswith("docs(status-board): generated Prompts: line")


def test_set_status_with_expect_never_overwrites_a_status_that_moved(center):
    spec = pc.prompts_cfg(__import__("session_tracker").load_config())
    rel = "docs/Prompts/funnels/Funnels-B11-Perf.md"  # deployed
    with pytest.raises(OSError):
        pc.set_status(spec, rel, "deployed", expect={"status": ("on-main",)}, deployed="2026-10-30T16:50:00")
    assert "deployed:" not in (center / rel).read_text()
    pc.set_status(spec, rel, "done", expect={"status": ("deployed",)})
    assert "status: done" in (center / rel).read_text()


def test_board_markers_with_nothing_to_add_commits_nothing(center, capsys, tmp_path):
    _with_status_board(center, tmp_path)
    assert run("board-markers", "--no-commit") == 0
    capsys.readouterr()
    assert run("board-markers") == 0
    out = capsys.readouterr().out
    assert "none (every module row has one)" in out and "commit" not in out


def test_every_live_session_is_listed_even_when_its_prompt_is_past_running(center, capsys):
    # Live 10-31: 6 live sessions, 3 shown. A session whose prompt is on-branch or done (Security G running its
    # suites, Funnels B14 waiting on the owner) or that has no prompt dropped out of the view.
    write_sessions(center.parent / "tracker", [
        session("e1e1e1e1-5555", prompt_file="docs/Prompts/funnels/Funnels-B11-Perf.md", seen=3),
        session("f2f2f2f2-6666", state="waiting_on_owner", seen=2, task="look at the logs"),
        session("ended000-7777", state="ended", prompt_file="docs/Prompts/funnels/Funnels-B11-Perf.md"),
    ])
    assert run() == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].endswith("· 1 blocked on owner · 4 live · 2 working · 2 waiting on you · 0 idle"
                                        " · 1 without a prompt")
    rows = out.split("\nSessions\n", 1)[1].split("\n\n", 1)[0]
    assert "e1e1e1e1  working" in rows and "B11" in rows and "deployed" in rows
    assert "f2f2f2f2  WAITING ON YOU" in rows and "no prompt · look at the logs" in rows
    assert "d60450f0" in rows and "c5588aac" in rows, "one list: running prompts' sessions too"
    assert "ended000" not in out
    assert run("--json") == 0
    rows = json.loads(capsys.readouterr().out)["sessions"]
    assert [r["sid8"] for r in rows] == ["f2f2f2f2", "c5588aac", "e1e1e1e1", "d60450f0"]  # waiting first, then newest
    assert rows[0]["prompt"] is None and rows[2]["prompt"]["id"] == "B11"
    assert run("--html") == 0
    page = Path(capsys.readouterr().out.strip().splitlines()[-1]).read_text()
    assert "Sessions" in page and "f2f2f2f2" in page and "ended000" not in page


def test_set_pending_or_blocked_releases_the_sessions_hold_on_the_prompt(center, capsys):
    import session_tracker as st

    # Live 10-31: A12 was set back to pending, but the view kept it running: the claiming session's tracker
    # file still named it. A session that matched a prompt by its task text is released the same way.
    assert run("claim", "funnels/B12-4", "--session", "d60450f0", "--no-commit") == 0
    assert run("set", "funnels/B12-4", "pending", "--clear", "session_id", "--no-commit") == 0
    h8b = next(p for p in pc.scan(cfg(center)) if p.id == "H8b")
    assert run("set", "history/H8b", "blocked", "--no-commit") == 0
    capsys.readouterr()
    s = st.load_session("d60450f0-1111")
    assert not s.get("prompt_file") and not s.get("prompt_claimed")
    assert "docs/Prompts/funnels/Funnels-B12-4-Webinar.md" in s["released_prompts"]
    assert h8b.rel in st.load_session("c5588aac-2222")["released_prompts"]
    assert run("--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert not {"B12-4", "H8b"} & {r["id"] for r in data["running"]}
