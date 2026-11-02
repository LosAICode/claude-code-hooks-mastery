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


# --- Do 4: one restart window ----------------------------------------------------------------------------

from conftest import commit_file  # noqa: E402
from test_command_center_cc9 import FAKE_RESTART_WEB, checks_of, fake_of, iso, set_map  # noqa: E402

OTHER = "dddd4444-0000-0000-0000-000000000004"


def test_a_docs_only_commit_never_blocks_a_restart_but_a_code_one_does(box, capsys, tmp_path):
    # whatever the map says: here a catch-all rule sends Documentation/ to web too
    set_map(box, tmp_path, lambda m: m["rules"].append({"paths": ["**"], "units": ["web"]}))
    doc = commit_file(box, "Documentation/Security/Register.md", "row", "security: register row M18")
    rows = [{"sha": doc, "subject": "security: register row M18", "repo": "r", "dir": str(box), "ts": iso(2)}]
    write_sessions(tmp_path / "tracker", [session(OTHER, seen=1, commits=rows)])
    _code, checks, _ = checks_of(capsys)
    assert checks["no session landing"]["ok"], checks["no session landing"]
    code_sha = commit_file(box, "modules/services/w.py", "w", "fix: w")
    rows.append({"sha": code_sha, "subject": "fix: w", "repo": "r", "dir": str(box), "ts": iso(1)})
    write_sessions(tmp_path / "tracker", [session(OTHER, seen=1, commits=rows)])
    _code, checks, _ = checks_of(capsys)
    assert not checks["no session landing"]["ok"] and code_sha[:9] in checks["no session landing"]["detail"]


def test_a_command_center_snapshot_is_not_a_landing_a_git_merge_one_is(box, capsys, tmp_path):
    t = datetime.fromisoformat(NOW).timestamp() - 60
    head = git(box, "rev-parse", "HEAD")
    claim = {"heads": {str(box): head}, "landing": [], "ts": t}
    write_sessions(tmp_path / "tracker", [session(OTHER, seen=1, pending_heads={"t1": claim})])
    _code, checks, _ = checks_of(capsys)
    assert checks["no session landing"]["ok"], checks["no session landing"]
    merge = {"heads": {str(box): head}, "landing": [str(box)], "ts": t}
    write_sessions(tmp_path / "tracker", [session(OTHER, seen=1, pending_heads={"t2": merge})])
    _code, checks, _ = checks_of(capsys)
    assert not checks["no session landing"]["ok"] and "is landing" in checks["no session landing"]["detail"]


def test_the_snapshot_names_only_git_landings(box):
    import session_tracker as st

    cfg = st.load_config()
    for command, landing in (("python3 /x/scripts/command_center.py deploy-ready", []),
                             (f"cd {box} && git merge --no-ff a1-work", [str(box)])):
        data = {"session_id": OTHER}
        st._snapshot_heads(data, {"tool_input": {"command": command}, "cwd": str(box), "tool_use_id": "t"}, cfg,
                           st.run_git)
        assert data["pending_heads"]["t"]["landing"] == landing, command


def test_a_failed_command_closes_its_snapshot(box):
    import session_tracker as st

    cfg = st.load_config()
    payload = {"session_id": OTHER, "tool_name": "Bash", "tool_use_id": "t9", "cwd": str(box),
               "tool_input": {"command": f"cd {box} && git merge --no-ff nope"}}
    data = {"session_id": OTHER}
    st.apply_event(data, "PreToolUse", payload, cfg)
    assert "t9" in data["pending_heads"]
    st.apply_event(data, "PostToolUseFailure", payload, cfg)
    assert "t9" not in (data.get("pending_heads") or {})


@pytest.fixture
def window(box, tmp_path):
    """The live tree `box` on main, the window file configured in tmp and open."""
    import datetime as dt

    path = tmp_path / "restart-window"
    c = json.loads((tmp_path / "config.json").read_text())
    c["prompts"]["restart_window"] = str(path)
    (tmp_path / "config.json").write_text(json.dumps(c))
    until = (dt.datetime.now().astimezone() + dt.timedelta(minutes=10)).isoformat(timespec="seconds")
    path.write_text(json.dumps({"owner_ok": "ship A09", "units": ["app"], "expires": until}))
    return path


def guard(box, command, cwd=None):
    import session_tracker as st

    return st.landing_guard({"tool_input": {"command": command}, "cwd": str(cwd or box)}, st.load_config())


def test_the_guard_refuses_a_code_merge_or_commit_to_main_while_the_window_is_open(box, window, tmp_path):
    assert "restart window is open" in guard(box, f"cd {box} && git merge --no-ff a1-work")
    (box / "modules" / "x.py").parent.mkdir(exist_ok=True)
    (box / "modules" / "x.py").write_text("x")
    git(box, "add", "modules/x.py")
    assert guard(box, 'git commit -m "fix: x"')
    assert guard(box, 'git commit -m "fix: x" -- modules/x.py')


def test_the_guard_passes_docs_commits_branches_and_closed_windows(box, window, tmp_path):
    (box / "Documentation").mkdir(exist_ok=True)
    (box / "Documentation" / "r.md").write_text("row")
    git(box, "add", "Documentation/r.md")
    assert guard(box, 'git commit -m "docs: row"') is None
    assert guard(box, 'git commit -m "docs: row" -- Documentation/r.md') is None
    (box / "modules").mkdir(exist_ok=True)
    (box / "modules" / "y.py").write_text("y")
    git(box, "add", "modules/y.py")
    git(box, "commit", "-q", "-m", "y")
    (box / "modules" / "y.py").write_text("y2")                  # a tracked code edit, not staged
    (box / "Documentation" / "r.md").write_text("row 2")
    git(box, "add", "Documentation/r.md")
    assert guard(box, 'git commit -m "docs: row"') is None       # commits only what is staged
    assert guard(box, 'git commit -am "docs: row"')              # -a takes the code edit too
    assert guard(box, 'git commit --all -m "docs: row"')
    assert guard(box, "python3 /x/scripts/command_center.py set agents/A1 deployed") is None
    git(box, "worktree", "add", "-q", "-b", "side", str(tmp_path / "side"))
    assert guard(box, 'git commit -m "fix: y"', cwd=tmp_path / "side") is None
    window.write_text(json.dumps({"owner_ok": "x", "expires": "2026-01-01T00:00:00+00:00"}))  # expired
    assert guard(box, f"cd {box} && git merge --no-ff a1-work") is None
    window.unlink()
    assert guard(box, f"cd {box} && git merge --no-ff a1-work") is None


def test_the_guard_is_off_without_the_config_key(box, tmp_path):
    assert guard(box, f"cd {box} && git merge --no-ff a1-work") is None


def test_deploy_run_opens_the_window_for_its_restarts_and_closes_it(box, window, capsys, tmp_path):
    window.unlink()
    (fake_of(tmp_path) / "restart_web.sh").write_text(
        FAKE_RESTART_WEB + f'cat {window} > {tmp_path / "state" / "window_seen"}\n')
    sessions(box, session(LIVE_SID, name="a04b-lander", seen=1), session(OTHER, name="assistants", seen=1))
    assert run("deploy-run", "--owner-ok", "owner: ship A1", "--no-commit") == 0
    out = capsys.readouterr().out
    seen = json.loads((tmp_path / "state" / "window_seen").read_text())
    assert seen["owner_ok"] == "owner: ship A1" and "app" in seen["units"]
    assert not window.exists()
    assert "Tell: assistants (dddd4444) · restart window open until" in out
    assert "Tell: assistants (dddd4444) · deploy done" in out


def test_a_failed_deploy_run_still_closes_the_window(box, window, capsys, tmp_path):
    window.unlink()
    (fake_of(tmp_path) / "restart_web.sh").write_text("#!/bin/sh\nexit 1\n")
    sessions(box, session(OTHER, name="assistants", seen=1))
    assert run("deploy-run", "--owner-ok", "go", "--no-commit") != 0
    assert not window.exists()
    assert "deploy stopped" in capsys.readouterr().out


# --- Do 3: sandbox copies named after their prompt --------------------------------------------------------


def test_sbx_path_names_the_sessions_prompt_and_counts_up(box, capsys, tmp_path, monkeypatch):
    c = json.loads((tmp_path / "config.json").read_text())
    c["prompts"]["sbx_root"] = str(tmp_path / "sbx")
    (tmp_path / "config.json").write_text(json.dumps(c))
    (tmp_path / "sbx" / "sbx-agents-A1-1").mkdir(parents=True)
    (tmp_path / "sbx" / "sbx-agents-A1-3").mkdir()
    monkeypatch.setenv("CLAUDE_SESSION_ID", LIVE_SID)
    assert run("sbx-path") == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "sbx" / "sbx-agents-A1-4")
    assert run("sbx-path", "agents/A2") == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "sbx" / "sbx-agents-A2-1")
    monkeypatch.setenv("CLAUDE_SESSION_ID", NEW_SID)
    assert run("sbx-path") == 2


# --- Do 2: the nightly disk report in /cc, park --archive -----------------------------------------------


def report_rows(tmp_path):
    return {"generated": "2026-10-30T04:30:00", "df": {"/": {"pct": 74.2}, "/tmp": {"pct": 96.0}},
            "kept": [
                {"path": "/w/a12-old", "kind": "worktree", "prompt": "docs/Prompts/agents/Agents-A1-X.md",
                 "branch": "a12", "age_days": 40, "size": 2_000_000_000, "why": "3 commit(s) not on main", "unmerged": 3},
                {"path": "/w/a12-newer", "kind": "worktree", "prompt": "docs/Prompts/agents/Agents-A1-X.md",
                 "branch": "a12b", "age_days": 9, "size": 500_000_000, "why": "1 commit(s) not on main", "unmerged": 1},
                {"path": "/w/lost", "kind": "worktree", "prompt": None, "branch": None, "age_days": 20,
                 "size": 1_000_000_000, "why": "2 commit(s) not on main", "unmerged": 2},
                {"path": "/w/fresh", "kind": "worktree", "prompt": None, "branch": "f", "age_days": 2,
                 "size": 9_000_000_000, "why": "1 commit(s) not on main", "unmerged": 1},
                {"path": "/w/dirty", "kind": "worktree", "prompt": None, "branch": "d", "age_days": 30,
                 "size": 9_000_000_000, "why": "2 uncommitted change(s)", "unmerged": 0},
            ]}


@pytest.fixture
def report(box, tmp_path):
    path = tmp_path / "disk_report.json"
    path.write_text(json.dumps(report_rows(tmp_path)))
    c = json.loads((tmp_path / "config.json").read_text())
    c["prompts"]["disk_report"] = str(path)
    (tmp_path / "config.json").write_text(json.dumps(c))
    return path


def test_cc_shows_the_disk_header_and_parked_gb_by_prompt_oldest_first(report, capsys):
    assert run() == 0
    out = capsys.readouterr().out
    assert out.splitlines()[1] == "Disk · / 74% · /tmp 96% ⚠ · report 10-30 04:30"
    assert "Parked, 3.5 GB (worktrees with unmerged commits, untouched 7 d+;" in out
    tail = out[out.index("Parked, 3.5 GB"):]
    assert tail.index("Agents-A1-X.md") < tail.index("/w/a12-old") < tail.index("/w/a12-newer") < tail.index(
        "(no prompt names it)") < tail.index("/w/lost")
    assert "/w/fresh" not in tail and "/w/dirty" not in tail


def test_cc_json_carries_the_disk_block(report, capsys):
    assert run("--json") == 0
    disk = json.loads(capsys.readouterr().out)["disk"]
    assert disk["parked_bytes"] == 3_500_000_000 and [r["path"] for r in disk["parked"]] == ["/w/a12-old", "/w/a12-newer", "/w/lost"]


def test_a_stale_report_says_so_and_no_config_key_shows_nothing(box, report, capsys, tmp_path):
    rep = report_rows(tmp_path)
    rep["generated"] = "2026-10-28T04:30:00"
    report.write_text(json.dumps(rep))
    run()
    assert "stale" in capsys.readouterr().out.splitlines()[1]
    c = json.loads((tmp_path / "config.json").read_text())
    del c["prompts"]["disk_report"]
    (tmp_path / "config.json").write_text(json.dumps(c))
    run()
    assert "Disk ·" not in capsys.readouterr().out


@pytest.fixture
def parked_wt(box, tmp_path, monkeypatch):
    import prompt_cleanup as pcl

    monkeypatch.setattr(pcl, "busy_paths", lambda proc="/proc": set())
    c = json.loads((tmp_path / "config.json").read_text())
    c["prompts"]["archive_dir"] = str(tmp_path / "archive")
    (tmp_path / "config.json").write_text(json.dumps(c))
    w = tmp_path / "parked"
    git(box, "worktree", "add", "-q", "-b", "old-idea", str(w))
    for i in range(2):
        (w / f"idea{i}.py").write_text(f"idea {i}\n")
        git(w, "add", f"idea{i}.py")
        git(w, "commit", "-q", "-m", f"idea {i}")
    return w


def test_park_without_archive_only_says_what_it_would_do(box, parked_wt, capsys, tmp_path):
    assert run("park", str(parked_wt)) == 0
    assert "would archive 2 commit(s) of old-idea" in capsys.readouterr().out
    assert parked_wt.is_dir() and not (tmp_path / "archive").exists()


def test_park_archive_writes_a_patch_that_rebuilds_the_branch_then_removes_it(box, parked_wt, capsys, tmp_path):
    tip_tree = git(box, "rev-parse", "old-idea^{tree}")
    assert run("park", str(parked_wt), "--archive") == 0
    patch = tmp_path / "archive" / "old-idea.patch"
    assert patch.is_file() and json.loads((tmp_path / "archive" / "old-idea.json").read_text())["commits"] == 2
    assert not parked_wt.exists() and "old-idea" not in git(box, "branch", "--list", "old-idea")
    base = json.loads((tmp_path / "archive" / "old-idea.json").read_text())["base"]
    git(box, "worktree", "add", "-q", "-b", "restored", str(tmp_path / "restored"), base)
    git(tmp_path / "restored", "am", "-q", str(patch))
    assert git(tmp_path / "restored", "rev-parse", "HEAD^{tree}") == tip_tree


def test_park_refuses_uncommitted_edits_or_stray_files(box, parked_wt, capsys):
    (parked_wt / "idea0.py").write_text("edited\n")
    assert run("park", str(parked_wt), "--archive") == 2
    assert "uncommitted" in capsys.readouterr().err
    git(parked_wt, "checkout", "--", "idea0.py")
    (parked_wt / "scratch.txt").write_text("mine\n")
    assert run("park", str(parked_wt), "--archive") == 2
    assert "not on main" in capsys.readouterr().err and parked_wt.is_dir()
