"""CC7: tracking that can't drift: hook-recorded commits, unrecorded-commit audit, test verdicts, stale notes,
per-commit migrations, progress, proof coverage."""
# ruff: noqa: F811  (the tests take the CC6 `plan` fixture, imported by name)
import os

import pytest

import command_center as cc
import prompt_center as pc
import prompt_deploy as pd
import session_tracker as st
from conftest import commit_file, git, write_config
from test_command_center_plan import out_json, plan, prompt, run, ts  # noqa: F401  (plan is a fixture)
from test_session_tracker import _run_hook

SID = "cc7cc7cc-0000-0000-0000-000000000001"
REL = "docs/Prompts/ops/Ops-CC9-Thing.md"
BODY = "# CC9\n\n## Do\n1. **One.** a\n2. **Two.** b\n3. **Three.** c\n\n## Done when\n- x\n"


def meta_of(path):
    return pc.parse_frontmatter(path.read_text())[0]


# --- 1. the hooks record a session's commits on its prompt ----------------------------------------------

@pytest.fixture
def hooked(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, REL, "---\nmodule: ops\nid: CC9\nstatus: pending\n---\n" + BODY, "prompt", when="2026-10-30T08:00:00")
    write_config(tmp_path, {"prompts": {"repo": str(r), "root": "docs/Prompts", "gate": True, "commit_claims": True}})
    env = {**os.environ, "SESSION_TRACKER_DIR": str(tmp_path / "tracker"),
           "SESSION_TRACKER_CONFIG": str(tmp_path / "config.json")}
    return r, env, tmp_path


def _hook_bash(env, cwd, command, tid, make):
    data = {"session_id": SID, "cwd": str(cwd), "tool_name": "Bash", "tool_use_id": tid, "tool_input": {"command": command}}
    assert _run_hook("pre_tool_use.py", data, env, cwd).returncode == 0
    out = make()
    r = _run_hook("post_tool_use.py", data, env, cwd)
    assert r.returncode == 0, r.stderr
    return out


def test_the_hooks_append_a_claimed_sessions_commits_to_its_prompt(hooked):
    r, env, cwd = hooked
    claim = {"session_id": SID, "cwd": str(cwd), "prompt": f"run {REL}", "hook_event_name": "UserPromptSubmit"}
    assert _run_hook("user_prompt_submit.py", claim, env, cwd, "--log-only").returncode == 0
    assert meta_of(r / REL)["status"] == "running"
    a = _hook_bash(env, cwd, f"git -C {r} commit -m 'feat: a (CC9)'", "t1",
                   lambda: commit_file(r, "modules/a.py", "a", "feat: a (CC9)"))
    b = _hook_bash(env, cwd, f"git -C {r} commit -m 'fix: b'", "t2", lambda: commit_file(r, "modules/b.py", "b", "fix: b"))
    # a command_center status commit and a commit touching only the prompts tree are not the prompt's work
    # (`set` also commits the Status Board, outside the prompts tree)
    _hook_bash(env, cwd, f"git -C {r} commit -m 'docs(prompts): ops CC9 → running'", "t3",
               lambda: commit_file(r, "docs/Status-Board.md", "n", "docs(prompts): ops CC9 → running"))
    _hook_bash(env, cwd, f"git -C {r} commit -m 'docs: prompt text'", "t4",
               lambda: commit_file(r, "docs/Prompts/ops/Ops-CC9-More.md", "m", "docs: prompt text"))
    meta = meta_of(r / REL)
    assert meta["commits"] == [a[:9], b[:9]]
    assert meta["status"] == "running"  # status untouched
    assert git(r, "status", "--porcelain", "--", REL) == ""  # committed with a pathspec
    assert f"docs(prompts): ops CC9 commits +{b[:9]}" in git(r, "log", "--format=%s").splitlines()


def test_an_unclaimed_session_writes_no_commits(hooked):
    r, env, cwd = hooked
    _hook_bash(env, cwd, f"git -C {r} commit -m 'feat: a'", "t1", lambda: commit_file(r, "modules/a.py", "a", "feat: a"))
    assert "commits" not in meta_of(r / REL)


def test_a_broken_prompt_center_never_breaks_commit_tracking(hooked, monkeypatch):
    r, env, cwd = hooked
    monkeypatch.setenv("SESSION_TRACKER_DIR", env["SESSION_TRACKER_DIR"])
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", env["SESSION_TRACKER_CONFIG"])
    st.track("UserPromptSubmit", {"session_id": SID, "cwd": str(cwd), "prompt": f"run {REL}"})
    monkeypatch.setattr(pc, "add_commits", lambda *a, **k: 1 / 0)
    bash = {"session_id": SID, "cwd": str(cwd), "tool_name": "Bash", "tool_use_id": "t1",
            "tool_input": {"command": f"git -C {r} commit -m 'feat: a'"}}
    st.track("PreToolUse", bash)
    sha = commit_file(r, "modules/a.py", "a", "feat: a")
    st.track("PostToolUse", bash)
    assert [c["sha"] for c in st.load_session(SID)["commits"]] == [sha]


# --- 2. audit: commits nobody recorded ------------------------------------------------------------------

def test_audit_names_a_commit_missing_from_its_prompts_commits(plan, capsys):
    first = commit_file(plan, "modules/connections/drive.py", "1", "feat(connections): import (R4, #576)",
                        when="2026-10-30T12:00:00")
    git(plan, "checkout", "-q", "-b", "r4b")
    side = commit_file(plan, "modules/connections/n.py", "n", "fix: notion (R4, #576)", when="2026-10-30T12:10:00")
    git(plan, "checkout", "-q", "main")
    git(plan, "merge", "-q", "--no-ff", "-m", "Merge r4b (R4, #576)", "r4b",
        env={"GIT_COMMITTER_DATE": "2026-10-30T12:20:00", "GIT_AUTHOR_DATE": "2026-10-30T12:20:00"})
    merge = git(plan, "rev-parse", "HEAD")
    missed = commit_file(plan, "modules/connections/cap.py", "c", "fix(connections): KB cap (R4, #576)",
                         when="2026-10-30T13:00:00")
    named = commit_file(plan, "modules/connections/x.py", "x", "fix: api R4 follow-up", when="2026-10-30T13:10:00")
    commit_file(plan, "docs/Prompts/api/notes.md", "n", "docs(prompts): api R4 → on-main (#576)", when="2026-10-30T13:20:00")
    docs = commit_file(plan, "Documentation/r4.md", "d", "docs: R4 landed (R4, #576)", when="2026-10-30T13:30:00")
    prompt(plan, "api/Api-R4-X.md", {"module": "api", "id": "R4", "status": "deployed", "issue": "\"576\"",
                                     "commits": f"[\"{first[:9]}\", \"{merge[:9]}\"]"})
    run("audit")
    out = capsys.readouterr().out
    assert f"set api/R4 deployed --commit {missed[:9]}" in out
    assert named[:9] in out
    assert side[:9] not in out and first[:9] not in out  # merged in by a recorded merge / recorded
    assert docs[:9] not in out  # needs no unit: deploy-check would not miss it
    assert out.count("is in no prompt's commits") == 2


# --- 3. test verdicts -------------------------------------------------------------------------------------

def test_set_tests_stores_summary_path_and_sha_and_refuses_a_fail(plan, capsys, tmp_path):
    ok = tmp_path / "ok.txt"
    ok.write_text("PASS            tests/a.py: 1 passed\nSUMMARY PASS=2 | files=2 | OK\n")
    path = plan / "docs/Prompts/agents/Agents-C10-X.md"
    sha = meta_of(path)["commit"]
    assert run("set", "agents/C10", "on-main", "--tests", str(ok), "--no-commit") == 0
    assert meta_of(path)["tests"] == f"SUMMARY PASS=2 | files=2 | OK · {ok} · {sha[:9]}"
    bad = tmp_path / "bad.txt"
    bad.write_text("SUMMARY NEW-FAILURES=1, PASS=1 | files=2 | FAIL\n")
    before = path.read_text()
    assert run("set", "agents/C10", "on-main", "--tests", str(bad), "--no-commit") == 2
    assert "FAIL" in capsys.readouterr().err and path.read_text() == before
    assert run("set", "agents/C11", "on-main", "--tests", str(ok), "--no-commit") == 2  # no commit: no sha to name
    assert run("set", "agents/C10", "on-main", "--tests", str(tmp_path / "missing.txt"), "--no-commit") == 2
    assert "not a file" in capsys.readouterr().err
    assert run("set", "agents/C11", "on-main", "--no-tests", "docs only", "--no-commit") == 0
    assert meta_of(plan / "docs/Prompts/agents/Agents-C11-X.md")["tests"] == "none: docs only"


def test_set_on_main_without_tests_warns(plan, capsys):
    assert run("set", "agents/C10", "on-main", "--no-commit") == 0
    assert "no --tests" in capsys.readouterr().err
    assert run("set", "agents/C10", "on-main", "--no-tests", "docs only", "--no-commit") == 0
    capsys.readouterr()
    assert run("set", "agents/C10", "on-main", "--no-commit") == 0
    assert "no --tests" not in capsys.readouterr().err  # it has a verdict already


def test_deploy_plan_lists_prompts_not_proven_tested(plan, capsys, tmp_path):
    ok = tmp_path / "ok.txt"
    ok.write_text("SUMMARY PASS=1 | files=1 | OK\n")
    c01 = meta_of(plan / "docs/Prompts/agents/Agents-C01-X.md")["commits"]
    assert run("set", "agents/C10", "on-main", "--tests", str(ok), "--no-commit") == 0
    assert run("set", "agents/C01", "on-main", "--tests", str(ok), "--tested", c01[0], "--no-commit") == 0
    capsys.readouterr()
    data = out_json(capsys, "deploy-plan")
    untested = {u["id"]: u["why"] for u in data["untested"]}
    assert "C10" not in untested
    assert "C02" in untested and "no tests" in untested["C02"]
    assert "C01" in untested and c01[1] in untested["C01"]  # a commit landed after the tested sha
    assert run("deploy-plan") == 0
    assert "Not proven tested" in capsys.readouterr().out


# --- 4. stale notes ---------------------------------------------------------------------------------------

def test_a_status_change_without_notes_says_the_notes_were_kept(plan, capsys):
    assert run("set", "agents/C11", "on-main", "--notes", "sandbox queued", "--no-commit") == 0
    capsys.readouterr()
    assert run("set", "agents/C11", "deployed", "--no-commit") == 0
    assert "notes kept: sandbox queued" in capsys.readouterr().out
    run("audit")
    out = capsys.readouterr().out
    assert any("C11" in line and "stale notes" in line for line in out.splitlines())


# --- 5. per-commit migrations ------------------------------------------------------------------------------

def test_a_migration_whose_commit_is_live_is_not_listed_to_apply(plan, capsys, monkeypatch):
    mig = git(plan, "log", "-1", "--format=%H", "--", "modules/migrations")
    fix = commit_file(plan, "modules/templates/b.html", "b", "fix (R4)", when="2026-10-30T12:30:00")
    prompt(plan, "api/Api-R4-X.md", {"id": "R4", "status": "deployed", "commits": f"[\"{mig[:9]}\", \"{fix[:9]}\"]"})
    boot = ts("2026-10-30T12:00:00")  # after the migration landed (11:00), before the fix (12:30)
    monkeypatch.setattr(pd, "unit_started", {"app": boot, "app-web@2": boot, "app-celery": boot,
                                             "app-celery-beat": boot, "app-voice": None}.get)
    data = out_json(capsys, "deploy-plan")
    r4 = [m for m in data["migrations"] if m["id"] == "R4"]
    assert r4 and all(m["applied_at_boot"] for m in r4)
    assert [p["id"] for p in data["prompts"] if p["id"] == "R4"] == ["R4"]  # the fix still waits on web
    run("deploy-plan")
    text = capsys.readouterr().out
    todo = text[text.index("Migrations"):text.index("Guards")]
    assert "R4, " not in todo.split("(ran at boot")[0]


# --- 6. progress ------------------------------------------------------------------------------------------

def test_progress_is_written_shown_and_defaults_its_total(plan, capsys):
    prompt(plan, "engine/Engine-E1-X.md", {"id": "E1", "status": "running", "session_id": "e1e1e1e1-live"},
           "# E1\n\n## Do\n1. a\n2. b\n3. c\n4. d\n5. e\n6. f\n\n## Tests\n1. not an item\n")
    assert run("set", "engine/E1", "running", "--progress", "3", "--no-commit") == 0
    assert meta_of(plan / "docs/Prompts/engine/Engine-E1-X.md")["progress"] == "3/6"
    assert run("set", "engine/E1", "running", "--progress", "4/6 tests red", "--no-commit") == 0
    capsys.readouterr()
    data = out_json(capsys)
    assert [r["progress"] for r in data["running"] if r["id"] == "E1"] == ["4/6 tests red"]
    assert run() == 0
    assert "4/6 tests red" in capsys.readouterr().out
    assert run("--module", "engine") == 0
    assert "4/6" in capsys.readouterr().out
    view = cc.View(cc.load_config(), ts("2026-10-30T16:00:00"))
    m = next(m for m in view.modules if m["module"] == "engine")
    assert "4/6 tests red" in cc.render_module_table(view, m)
    assert "4/6 tests red" in cc.render_html(view)


@pytest.fixture
def gated(tmp_path, tracker_env):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, REL, "---\nmodule: ops\nid: CC9\nstatus: pending\n---\n" + BODY, "prompt", when="2026-10-30T08:00:00")
    write_config(tmp_path, {"prompts": {"repo": str(r), "root": "docs/Prompts", "gate": True, "commit_claims": False}})
    st.track("UserPromptSubmit", {"session_id": SID, "cwd": str(r), "prompt": f"run {REL}"})
    return r


def _commit_in_session(r, msg, tid):
    bash = {"session_id": SID, "cwd": str(r), "tool_name": "Bash", "tool_use_id": tid,
            "tool_input": {"command": f"git -C {r} commit -m '{msg}'"}}  # CC9: the subject must match
    st.track("PreToolUse", bash)
    commit_file(r, f"modules/{tid}.py", tid, msg)
    st.track("PostToolUse", bash)


def test_the_stop_gate_asks_once_for_progress_after_a_code_commit(gated):
    r = gated
    stop = {"session_id": SID, "stop_hook_active": False}
    pc.write_meta(r / REL, {"updated": "2026-10-30T09:00:00-04:00"})  # claimed (last updated) before the commit
    _commit_in_session(r, "feat: one", "t1")
    first = st.stop_gate(stop)
    assert first and "still `running`" in first["reason"] and "--progress" not in first["reason"]  # the status ask first
    second = st.stop_gate(stop)
    assert second and "--progress <done>/3" in second["reason"]
    _commit_in_session(r, "feat: two", "t2")
    assert st.stop_gate(stop) is None  # once


def test_no_progress_ask_when_progress_changed_after_the_last_commit(gated):
    r = gated
    stop = {"session_id": SID, "stop_hook_active": False}
    _commit_in_session(r, "feat: one", "t1")
    st.stop_gate(stop)
    pc.write_meta(r / REL, {"progress": "1/3", "updated": "2099-01-01T00:00:00-04:00"})
    assert st.stop_gate(stop) is None


# --- 7. proof coverage ------------------------------------------------------------------------------------

def test_audit_flags_a_deployed_prompt_with_no_proof(plan, capsys):
    run("audit")
    lines = capsys.readouterr().out.splitlines()
    assert any("C09" in line and "no proof" in line for line in lines)
    assert not any("C05" in line and "no proof" in line for line in lines)  # its Done-when has a live bullet
    assert not any("C03" in line and "no proof" in line for line in lines)  # proof: line
