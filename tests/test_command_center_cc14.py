"""CC14 (2026-11-02): handovers on the board, `done` only when proven, clean-up on done, one restart window,
sandbox copies named after their prompt, the nightly disk report in /cc."""
import json
from datetime import datetime

import pytest

import command_center as cc
import prompt_center as pc
from conftest import git
from test_command_center_cc9 import LIVE_SID, NOW, box, fakebin, run, write_prompt  # noqa: F401
from test_prompt_join import session, write_sessions

NEW_SID = "bbbb2222-0000-0000-0000-000000000002"


def meta_of(box, rel):
    return pc.parse_frontmatter((box / "docs/Prompts" / rel).read_text())[0]


def view():
    return cc.View(cc.load_config(), datetime.fromisoformat(NOW).timestamp())


def sessions(box, *rows):
    write_sessions(box.parent / "tracker", list(rows))


# --- Do 6: handovers ----------------------------------------------------------------------------------------


def test_claim_takes_over_an_on_main_prompt_and_keeps_its_status(box, capsys):
    sessions(box, session(LIVE_SID, seen=300), session(NEW_SID, name="a12b-taker", seen=1))  # A1's holder is gone
    assert run("claim", "agents/A1", "--session", NEW_SID[:8], "--no-commit") == 0
    m = meta_of(box, "agents/Agents-A1-X.md")
    assert m["status"] == "on-main" and m["session_id"] == NEW_SID
    assert m["previous_sessions"] == [LIVE_SID]
    assert "taken over" in capsys.readouterr().out
    lines = cc.tell_lines(view(), ["docs/Prompts/agents/Agents-A1-X.md"])
    assert lines and "a12b-taker" in lines[0]


def test_claim_refuses_a_takeover_while_the_holder_is_live(box, capsys):
    sessions(box, session(LIVE_SID, seen=1), session(NEW_SID, seen=1))
    assert run("claim", "agents/A1", "--session", NEW_SID, "--no-commit") == 2
    assert meta_of(box, "agents/Agents-A1-X.md")["session_id"] == LIVE_SID
    assert "live" in capsys.readouterr().err


def test_claim_takes_over_a_prompt_with_no_session(box):
    sessions(box, session(NEW_SID, seen=1))
    assert run("claim", "agents/A2", "--session", NEW_SID, "--no-commit") == 0
    m = meta_of(box, "agents/Agents-A2-X.md")
    assert m["status"] == "on-main" and m["session_id"] == NEW_SID and "previous_sessions" not in m


def test_claim_never_takes_over_a_done_prompt(box):
    write_prompt(box, "agents/Agents-A3-X.md", {"id": "A3", "status": "done"})
    sessions(box, session(NEW_SID, seen=1))
    assert run("claim", "agents/A3", "--session", NEW_SID, "--no-commit") == 2


# --- Do 6: done only when deployed (or --no-deploy) and proven ---------------------------------------------------


def test_done_is_refused_for_an_on_main_prompt(box, capsys):
    assert run("set", "agents/A1", "done", "--no-commit", "--keep-worktree") == 2
    assert "deployed" in capsys.readouterr().err
    assert meta_of(box, "agents/Agents-A1-X.md")["status"] == "on-main"


def test_done_with_no_deploy_needs_a_reason_and_a_proof(box, capsys):
    assert run("set", "agents/A2", "done", "--no-deploy", "docs only", "--no-commit", "--keep-worktree") == 2
    assert "proof" in capsys.readouterr().err
    assert run("set", "agents/A2", "done", "--no-deploy", "docs only", "--proof", "README reads right",
               "--no-commit", "--keep-worktree") == 0
    m = meta_of(box, "agents/Agents-A2-X.md")
    assert m["status"] == "done" and m["deployed"] == "none: docs only"


def test_done_after_deployed_with_its_proof_line(box):
    pc.set_status(pc.prompts_cfg(cc.load_config()), "docs/Prompts/agents/Agents-A1-X.md", "deployed")
    assert run("set", "agents/A1", "done", "--no-commit", "--keep-worktree") == 0


def test_skipped_needs_neither(box):
    assert run("set", "agents/A2", "skipped", "--no-commit", "--keep-worktree") == 0


# --- Do 1: clean up on done ------------------------------------------------------------------------------


@pytest.fixture
def left(box, tmp_path, monkeypatch):
    """A1 deployed, landed with a --no-ff merge of `a1-work`; worktrees: merged+noise, merged+stray file,
    an unmerged one its session worked in; a sandbox copy sbx-agents-A1-1."""
    import prompt_cleanup as pcl

    monkeypatch.setattr(pcl, "busy_paths", lambda proc="/proc": set())
    r = box
    wts = tmp_path / "wts"
    git(r, "commit", "-q", "--allow-empty", "-m", "base")
    git(r, "worktree", "add", "-q", "-b", "a1-work", str(wts / "merged"))
    (wts / "merged" / "a1.py").write_text("a1\n")
    git(wts / "merged", "add", "a1.py")
    git(wts / "merged", "commit", "-q", "-m", "a1 work")
    git(r, "merge", "-q", "--no-ff", "-m", "Merge a1-work", "a1-work")
    merge = git(r, "rev-parse", "HEAD")
    git(r, "worktree", "add", "-q", "-b", "a1-stray", str(wts / "stray"), merge)
    git(r, "worktree", "add", "-q", "-b", "a1-more", str(wts / "unmerged"), merge)
    (wts / "unmerged" / "more.py").write_text("more\n")
    git(wts / "unmerged", "add", "more.py")
    git(wts / "unmerged", "commit", "-q", "-m", "more work")
    # noise, and a file that is untracked here but identical on main: removable
    (wts / "merged" / "__pycache__").mkdir()
    (wts / "merged" / "__pycache__" / "a1.cpython-312.pyc").write_bytes(b"\0")
    (wts / "merged" / "venv").symlink_to("/nonexistent/venv")
    (wts / "stray" / "notes.txt").write_text("my notes\n")  # not on main: keep
    sbx = tmp_path / "sbx"
    (sbx / "sbx-agents-A1-1" / "modules").mkdir(parents=True)
    (sbx / "sbx-agents-B9-1").mkdir()
    rel = "docs/Prompts/agents/Agents-A1-X.md"
    pc.set_status(pc.prompts_cfg(cc.load_config()), rel, "deployed", commit=merge[:9])
    sessions(box, session(LIVE_SID, seen=300, prompt_file=rel, worktree=str(wts / "unmerged")),
             session(NEW_SID, seen=1, prompt_file=rel, worktree=str(wts / "stray")))
    c = json.loads((tmp_path / "config.json").read_text())
    c["prompts"]["sbx_root"] = str(sbx)
    (tmp_path / "config.json").write_text(json.dumps(c))
    return r, wts, sbx


def test_done_dry_run_shows_what_goes_and_removes_nothing(left, capsys):
    r, wts, sbx = left
    assert run("set", "agents/A1", "done", "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"WOULD REMOVE worktree {wts / 'merged'} (a1-work): merged and clean" in out
    assert f"WOULD REMOVE sandbox copy {sbx / 'sbx-agents-A1-1'}" in out
    assert f"KEEP   worktree {wts / 'unmerged'} (a1-more): 1 commit(s) not on main" in out
    assert f"KEEP   worktree {wts / 'stray'} (a1-stray): untracked file(s) not on main: notes.txt" in out
    assert (wts / "merged").is_dir() and (sbx / "sbx-agents-A1-1").is_dir()
    assert "status: deployed" in (r / "docs/Prompts/agents/Agents-A1-X.md").read_text()


def test_done_removes_merged_clean_worktrees_branches_and_sbx_copies(left, capsys):
    r, wts, sbx = left
    assert run("set", "agents/A1", "done", "--no-commit") == 0
    out = capsys.readouterr().out
    assert not (wts / "merged").exists() and "REMOVE branch a1-work" in out
    assert "a1-work" not in git(r, "branch", "--list", "a1-work")
    assert not (sbx / "sbx-agents-A1-1").exists() and (sbx / "sbx-agents-B9-1").is_dir()
    assert (wts / "unmerged" / "more.py").is_file() and (wts / "stray" / "notes.txt").is_file()
    assert "a1-more" in git(r, "branch", "--list", "a1-more")


def test_keep_worktree_skips_the_clean_up(left):
    _r, wts, sbx = left
    assert run("set", "agents/A1", "done", "--no-commit", "--keep-worktree") == 0
    assert (wts / "merged").is_dir() and (sbx / "sbx-agents-A1-1").is_dir()


def test_a_worktree_in_use_or_named_by_a_prompt_stays(left, monkeypatch, capsys):
    import prompt_cleanup as pcl

    r, wts, sbx = left
    monkeypatch.setattr(pcl, "busy_paths", lambda proc="/proc": {str(sbx / "sbx-agents-A1-1" / "modules")})
    write_prompt(r, "agents/Agents-A4-X.md", {"id": "A4", "status": "on-main",
                                              "notes": f"\"results in {wts / 'merged'}/results.json\""})
    assert run("set", "agents/A1", "done", "--no-commit") == 0
    out = capsys.readouterr().out
    assert (wts / "merged").is_dir() and "named by agents/A4 notes:" in out
    assert (sbx / "sbx-agents-A1-1").is_dir() and "a process has its cwd" in out


def test_untracked_noise_and_files_identical_on_main_do_not_keep_a_worktree(tmp_path):
    import prompt_cleanup as pcl

    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("a\n")
    git(r, "add", "a.txt")
    git(r, "commit", "-q", "-m", "a")
    git(r, "worktree", "add", "-q", "--detach", str(tmp_path / "w"), "HEAD~0")
    (r / "tests").mkdir()
    (r / "tests" / "t.py").write_text("t\n")
    git(r, "add", "tests/t.py")
    git(r, "commit", "-q", "-m", "t on main")
    w = tmp_path / "w"
    (w / "tests").mkdir()
    (w / "tests" / "t.py").write_text("t\n")                   # identical on main
    (w / "discovery_reports").mkdir()
    (w / "discovery_reports" / "r.json").write_text("{}")      # noise
    (w / "tests" / "browser" / "artifacts").mkdir(parents=True)
    (w / "tests" / "browser" / "artifacts" / "s.png").write_bytes(b"x")
    assert pcl.untracked_not_on_main(str(w)) == []
    (w / "tests" / "t.py").write_text("changed\n")
    assert pcl.untracked_not_on_main(str(w)) == ["tests/t.py"]
