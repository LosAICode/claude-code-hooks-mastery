import json
import os
import subprocess
import sys

import pytest

import session_tracker as st
from conftest import ROOT, commit_file, git, write_config

SID = "abcd1234-0000-0000-0000-000000000000"


def payload(**kw):
    return {"session_id": SID, "cwd": "/tmp", **kw}


def state():
    return st.load_session(SID)


def test_session_start_creates_file_atomically(tracker_env):
    st.track("SessionStart", payload(source="startup"))
    data = state()
    assert data["state"] == "idle"
    assert data["cwd"] == "/tmp" and data["worktree"] == "/tmp"
    assert data["started"] and data["last_seen"]
    assert not list(tracker_env.glob(".*.tmp"))


def test_first_prompt_sets_task_from_prompt_file_path(tracker_env):
    st.track("UserPromptSubmit", payload(prompt="Run /opt/p/Documentation/Next-Session-Prompts-2026-10-28/X-Y.md now"))
    st.track("UserPromptSubmit", payload(prompt="Run /opt/p/Other.md instead"))
    data = state()
    assert data["task"] == "/opt/p/Documentation/Next-Session-Prompts-2026-10-28/X-Y.md"
    assert data["state"] == "working"


def test_task_falls_back_to_first_80_chars():
    text = "fix the thing " * 20
    task = st.task_from_prompt(text)
    assert task is not None and task.startswith("fix the thing fix") and len(task) == 81
    assert st.task_from_prompt("<local-command-caveat>x</local-command-caveat>") is None


def test_notification_and_ask_user_question_mean_waiting(tracker_env):
    st.track("UserPromptSubmit", payload(prompt="hi"))
    st.track("Notification", payload(message="Claude needs your permission to use Bash"))
    data = state()
    assert data["state"] == "waiting_on_owner"
    assert data["waiting_reason"] == "Claude needs your permission to use Bash"
    since = data["waiting_since"]
    st.track("PreToolUse", payload(tool_name="AskUserQuestion"))
    assert state()["waiting_since"] == since  # still the same wait
    st.track("PostToolUse", payload(tool_name="AskUserQuestion", tool_input={}))
    assert state()["state"] == "working" and state()["waiting_since"] is None


def test_stop_idle_and_session_end_ended(tracker_env):
    st.track("UserPromptSubmit", payload(prompt="hi"))
    st.track("Stop", payload(stop_hook_active=False))
    assert state()["state"] == "idle"
    st.track("SessionEnd", payload(reason="prompt_input_exit"))
    assert state()["state"] == "ended" and state()["end_reason"] == "prompt_input_exit"


def test_last_tool_never_stores_the_command(tracker_env):
    cmd = "API_TOKEN=sk-live-0123456789 curl https://x.invalid"
    st.track("PostToolUse", payload(tool_name="Bash", tool_input={"command": cmd, "description": "Call the API"},
                                    tool_response={"stdout": "secret-output"}))
    raw = st.session_file(SID).read_text()
    assert "sk-live" not in raw and "secret-output" not in raw
    assert state()["last_tool"] == "Bash: Call the API"
    st.track("PostToolUse", payload(tool_name="Read", tool_input={"file_path": "/a/b.py"}))
    assert state()["last_tool"] == "Read: /a/b.py"


def test_worktree_and_branch_follow_cd_and_worktree_add(tracker_env, repo, tmp_path):
    wt = tmp_path / "wt1"
    git(repo, "worktree", "add", "-q", "-b", "feat", str(wt))
    st.track("PostToolUse", payload(tool_name="Bash", tool_input={"command": f"git worktree add --detach {wt} && ls"}))
    assert state()["worktree"] == str(wt) and state()["branch"] == "feat"
    st.track("PostToolUse", payload(tool_name="Bash", tool_input={"command": f"cd {repo} && git status"}))
    assert state()["worktree"] == str(repo) and state()["branch"] == "main"
    # A later command with no cd and an unchanged cwd keeps the worktree; so does a cd out of any checkout.
    st.track("PostToolUse", payload(tool_name="Bash", tool_input={"command": "ls"}))
    st.track("PostToolUse", payload(tool_name="Bash", tool_input={"command": f"cd {tmp_path} && ls"}))
    assert state()["worktree"] == str(repo) and state()["branch"] == "main"


def pre(cmd, tid="t1"):
    st.track("PreToolUse", payload(tool_name="Bash", tool_use_id=tid, tool_input={"command": cmd}))


def post(cmd, tid="t1"):
    st.track("PostToolUse", payload(tool_name="Bash", tool_use_id=tid, tool_input={"command": cmd}))


def test_git_commit_records_sha_subject_and_repo(tracker_env, repo):
    cmd = f"cd {repo} && git commit -m 'feat: a' -- modules/a.py"
    pre(cmd)
    sha = commit_file(repo, "modules/a.py", "x = 1", "feat: a")
    post(cmd)
    data = state()
    assert data["last_commit"] == f"{sha[:9]} feat: a"
    assert data["commits"][0]["sha"] == sha
    assert data["commits"][0]["repo"] == st.repo_id(str(repo))
    assert not data.get("pending_heads")
    # The same command again (nothing new committed) adds no duplicate.
    pre(f"git -C {repo} commit -m again", "t2")
    post(f"git -C {repo} commit -m again", "t2")
    assert len(state()["commits"]) == 1


def test_old_head_is_not_recorded_as_a_commit(tracker_env, repo):
    pre(f"git -C {repo} commit -m nothing")
    post(f"git -C {repo} commit -m nothing")
    assert state()["commits"] == [] and state()["last_commit"] is None


def test_another_sessions_fresh_commit_is_not_credited(tracker_env, repo):
    commit_file(repo, "modules/b.py", "y = 1", "docs(prompts): someone else")  # seconds ago, by another session
    pre(f"cd {repo} && git commit -m mine")  # nothing to commit
    post(f"cd {repo} && git commit -m mine")
    # A command that commits nothing, while another session commits in the same tree, credits nothing either.
    pre(f"git -C {repo} status", "t2")
    commit_file(repo, "modules/c.py", "z = 1", "auto-sync")
    post(f"git -C {repo} status", "t2")
    assert state()["commits"] == [] and state()["last_commit"] is None


def test_post_without_pre_credits_nothing(tracker_env, repo):
    commit_file(repo, "modules/a.py", "x = 1", "feat: a")
    post(f"git -C {repo} commit -m 'feat: a'")
    assert state()["commits"] == []


def test_fast_forward_merge_is_credited(tracker_env, repo):
    git(repo, "checkout", "-q", "-b", "feat")
    sha = commit_file(repo, "modules/m.py", "m = 1", "feat: m")
    git(repo, "checkout", "-q", "main")
    cmd = f"git -C {repo} merge --ff-only feat"
    pre(cmd)
    git(repo, "merge", "-q", "--ff-only", "feat")
    post(cmd)
    assert [c["sha"] for c in state()["commits"]] == [sha]


def test_command_center_commit_in_the_prompts_repo_is_credited(tracker_env, repo, tmp_path):
    write_config(tmp_path, {"prompts": {"repo": str(repo), "root": "docs/Prompts"}})
    code = f"cd {repo} && git commit -m 'feat: code' -- modules/a.py"
    cmd = f"{code} && python3 /x/scripts/command_center.py set ops/CC5 done --commit abc"
    pre(cmd)
    a = commit_file(repo, "modules/a.py", "x = 1", "feat: code")
    b = commit_file(repo, "docs/Prompts/ops/CC5.md", "done", "docs(prompts): ops CC5 done")
    post(cmd)
    data = state()
    assert [c["sha"] for c in data["commits"]] == [a, b]  # both commits of the one command, oldest first
    assert data["last_commit"].startswith(b[:9])
    # A command_center read (no commit) credits nothing.
    pre("python3 /x/scripts/command_center.py --module ops", "t2")
    post("python3 /x/scripts/command_center.py --module ops", "t2")
    assert len(state()["commits"]) == 2


@pytest.mark.parametrize("cmd", [
    "grep -n neurogen-deploy-freeze scripts/restart_web.sh",
    "cat scripts/restart_web.sh",
    "sed -n 1,5p /opt/p/scripts/restart_web.sh",
    'echo "then run scripts/restart_web.sh"',
    "git show HEAD -- scripts/restart_web.sh",
    "git diff HEAD -- scripts/restart_web.sh | head",
    "tail -50 scripts/restart_web.sh; ls scripts",
    "cat <<'EOF' > notes.md\nscripts/restart_web.sh\nsystemctl restart neurogenserver\nEOF",
    "systemctl status neurogenserver-celery",
    "systemctl show neurogenserver -p ActiveEnterTimestamp && systemctl is-active neurogenserver",
    "grep 'systemctl restart neurogenserver' docs/x.md",
    "grep x 2>&1 scripts/restart_web.sh",
    'echo "unbalanced scripts/restart_web.sh',
    "grep -n x \\\n  scripts/restart_web.sh",
    "ls\n  grep y scripts/restart_web.sh\n# scripts/restart_web.sh",
    "python3 - <<'PY'\nimport os\nos.system('scripts/restart_web.sh')\nPY",
])
def test_reading_a_deploy_script_is_not_a_deploy(cmd):
    assert st.deploy_matches(cmd, PATTERNS) == []


@pytest.mark.parametrize("cmd", [
    "flock /run/lock/neurogen-aa-deploy.lock scripts/restart_web.sh",
    "cd /opt/p && scripts/restart_web.sh",
    "systemctl restart neurogenserver-celery",
    "systemctl try-restart neurogenserver-channels",
    "sudo nice -n 10 /opt/p/scripts/restart_web.sh 2>&1 | tail -5",
    "bash -c 'scripts/restart_web.sh'",
    "X=1 timeout 600 bash scripts/restart_web.sh",
    "cat scripts/restart_web.sh; flock -w 30 /run/lock/x.lock scripts/restart_web.sh",
    "scripts/restart_web.sh 2>&1 | tail -5",
    "ls\nscripts/restart_web.sh",
    "L=x.log; before=$(wc -l < $L); systemctl restart neurogenserver-celery-beat.service; echo done",
    "R=$(psql -c \"select 1\"); [ \"$R\" = 0 ] || { echo busy; exit 3; }; systemctl restart neurogenserver.service; "
    "case \"$s\" in *'\"ok\"'*) echo",  # an unclosed quote further on
    "for u in celery channels; do systemctl restart neurogenserver-$u; done",
])
def test_executing_a_deploy_command_is_a_deploy(cmd):
    assert st.deploy_matches(cmd, PATTERNS)


PATTERNS = [r"scripts/restart_web\.sh", r"systemctl\s+(?:try-)?restart\s+neurogenserver"]


def test_deploy_patterns_come_from_config(tracker_env, tmp_path):
    write_config(tmp_path, {"deploy_patterns": [r"scripts/restart_app\.sh"]})
    st.track("PostToolUse", payload(tool_name="Bash", tool_input={"command": "flock /x scripts/restart_app.sh"}))
    st.track("PostToolUse", payload(tool_name="Bash", tool_input={"command": "ls scripts"}))
    st.track("PostToolUse", payload(tool_name="Bash", tool_input={"command": "grep x scripts/restart_app.sh"}))
    deploys = state()["deploys"]
    assert len(deploys) == 1 and deploys[0]["match"] == "scripts/restart_app.sh"


def test_a_raising_writer_never_raises(tracker_env, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(st.os, "replace", boom)
    st.track("SessionStart", payload())  # must not raise
    assert "disk full" in (tracker_env / "tracker.log").read_text()


def _run_hook(name, data, env, cwd, *args):
    return subprocess.run([sys.executable, str(ROOT / ".claude" / "hooks" / name), *args],
                          input=json.dumps(data), capture_output=True, text=True, env=env, cwd=cwd, timeout=60)


def test_hooks_write_the_tracker_file(tmp_path):
    d = tmp_path / "tracker"
    env = {**os.environ, "SESSION_TRACKER_DIR": str(d), "SESSION_TRACKER_CONFIG": str(tmp_path / "none.json")}
    r = _run_hook("post_tool_use.py", payload(tool_name="Read", tool_input={"file_path": "/a"}), env, tmp_path)
    assert r.returncode == 0
    assert json.loads((d / f"{SID}.json").read_text())["last_tool"] == "Read: /a"
    r = _run_hook("stop.py", payload(stop_hook_active=False), env, tmp_path, "--chat")
    assert r.returncode == 0 and r.stdout == ""
    assert json.loads((d / f"{SID}.json").read_text())["state"] == "idle"


def test_hooks_exit_0_when_the_tracker_cannot_write(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")  # mkdir under a file fails
    env = {**os.environ, "SESSION_TRACKER_DIR": str(blocker / "sub"),
           "SESSION_TRACKER_CONFIG": str(tmp_path / "none.json")}
    for name, args in [("post_tool_use.py", ()), ("stop.py", ("--chat",)), ("session_start.py", ()),
                       ("session_end.py", ()), ("notification.py", ()),
                       ("user_prompt_submit.py", ("--log-only",))]:
        r = _run_hook(name, payload(tool_name="Read", tool_input={}, prompt="hi"), env, tmp_path, *args)
        assert r.returncode == 0, (name, r.stderr)
