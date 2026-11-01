"""Verify gate (owner 11-01): no restart ships code that no /verify has covered.

A restart ships all of main. /verify ends by recording main's sha (scripts/verify_record.py); a restart is allowed
while no code path (board_gate.code_paths) changed on main since the newest record. An outage restart and
NEUROGEN_VERIFY_OVERRIDE="<reason>" (logged) always pass.
"""
import json
import os
import subprocess
import sys

import pytest

import session_tracker as st
from conftest import ROOT, commit_file, git

SID = "ver1fy00-0000-0000-0000-000000000001"


@pytest.fixture
def repo(tmp_path, tracker_env):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, "modules/app.py", "x = 1\n", "feat: app")
    return r


def cfg_for(repo):
    return {"board_gate": {"repo": str(repo), "branch": "main", "code_paths": ["modules/"]}}


def gate(repo, cmd, active=lambda unit: "active"):
    payload = {"session_id": SID, "tool_name": "Bash", "tool_input": {"command": cmd}}
    return st.verify_gate(payload, cfg_for(repo), is_active=active)


def head(repo):
    return git(repo, "rev-parse", "main")


RESTARTS = [
    "flock /run/lock/neurogen-aa-deploy.lock scripts/restart_web.sh",
    "systemctl restart neurogenserver-celery",
    "bash -c 'systemctl restart neurogenserver-channels'",
    "python3 /opt/claude-code-hooks-mastery/scripts/command_center.py deploy-run --owner-ok 'yes'",
]


@pytest.mark.parametrize("cmd", RESTARTS)
def test_a_restart_with_no_verify_is_blocked(repo, cmd):
    reason = gate(repo, cmd)
    assert reason and "/verify" in reason and "verify_record.py" in reason


@pytest.mark.parametrize("cmd", RESTARTS)
def test_a_verify_of_main_lets_the_restart_through(repo, cmd):
    st.record_verify(head(repo), "verify: PASS", SID)
    assert gate(repo, cmd) is None


def test_docs_after_the_verify_still_pass(repo):
    st.record_verify(head(repo), "verify: PASS", SID)
    commit_file(repo, "Documentation/notes.md", "n\n", "docs: notes")
    assert gate(repo, RESTARTS[0]) is None


def test_code_landed_after_the_verify_blocks_and_names_it(repo):
    st.record_verify(head(repo), "verify: PASS", SID)
    commit_file(repo, "modules/new.py", "y = 2\n", "feat: new")
    reason = gate(repo, RESTARTS[1])
    assert reason and "modules/new.py" in reason


def test_a_verify_of_a_side_branch_does_not_count(repo):
    git(repo, "checkout", "-q", "-b", "side")
    side = commit_file(repo, "modules/side.py", "z = 3\n", "feat: side")
    git(repo, "checkout", "-q", "main")
    st.record_verify(side, "verify: PASS", SID)
    assert gate(repo, RESTARTS[0])


def test_a_record_of_an_unknown_sha_is_skipped_not_a_pass(repo):
    """A sha this repo lacks (another clone, a garbage line) must not make the gate fail open."""
    st.record_verify("deadbeef" * 5, "verify: PASS", SID)
    assert gate(repo, RESTARTS[0])


def test_the_newest_record_that_covers_main_wins(repo):
    old = head(repo)
    commit_file(repo, "modules/b.py", "b = 1\n", "feat: b")
    st.record_verify(old, "verify: PASS (old)", SID)
    assert gate(repo, RESTARTS[0])
    st.record_verify(head(repo), "verify: PASS", SID)
    assert gate(repo, RESTARTS[0]) is None


@pytest.mark.parametrize("cmd", [
    "systemctl show -p ActiveEnterTimestamp neurogenserver-celery",
    "systemctl is-active neurogenserver",
    "echo scripts/restart_web.sh",
    "grep restart_web.sh docs/runbook.md",
    "python3 scripts/command_center.py deploy-run --owner-ok 'yes' --dry-run",
    "python3 scripts/command_center.py deploy-plan",
])
def test_reads_dry_runs_and_mentions_are_never_blocked(repo, cmd):
    assert gate(repo, cmd) is None


def test_an_outage_restart_always_passes(repo):
    assert gate(repo, "systemctl restart neurogenserver-celery", active=lambda u: "failed") is None
    assert gate(repo, "scripts/restart_web.sh", active=lambda u: "inactive") is None


def test_the_override_passes_and_is_logged(repo, tracker_env):
    cmd = 'NEUROGEN_VERIFY_OVERRIDE="hotfix, owner said" systemctl restart neurogenserver-celery'
    assert gate(repo, cmd) is None
    rows = [json.loads(x) for x in (tracker_env / "deploys" / "overrides.jsonl").read_text().splitlines()]
    assert rows[-1]["kind"] == "verify" and rows[-1]["reason"] == "hotfix, owner said"


def test_no_repo_configured_means_no_gate(tracker_env):
    payload = {"session_id": SID, "tool_name": "Bash", "tool_input": {"command": RESTARTS[0]}}
    assert st.verify_gate(payload, {}) is None


def test_the_record_script_writes_mains_sha(repo, tracker_env, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(cfg_for(repo)))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_record.py"), "--summary", "verify: PASS"],
                       capture_output=True, text=True, env=os.environ, timeout=30)
    assert r.returncode == 0, r.stderr
    rec = json.loads((tracker_env / "deploys" / "verified.jsonl").read_text().splitlines()[-1])
    assert rec["sha"] == head(repo) and rec["summary"] == "verify: PASS"
    assert gate(repo, RESTARTS[0]) is None


def test_the_record_script_refuses_an_empty_summary(repo, tracker_env, tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(cfg_for(repo)))
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_record.py"), "--summary", "  "],
                       capture_output=True, text=True, env=os.environ, timeout=30)
    assert r.returncode != 0 and not (tracker_env / "deploys" / "verified.jsonl").exists()


def test_pre_tool_use_hook_blocks_an_unverified_restart(repo, tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "systemctl").write_text("#!/bin/sh\necho active\n")
    (fake / "systemctl").chmod(0o755)
    (tmp_path / "cfg.json").write_text(json.dumps(cfg_for(repo)))
    env = {**os.environ, "SESSION_TRACKER_DIR": str(tmp_path / "tracker"),
           "SESSION_TRACKER_CONFIG": str(tmp_path / "cfg.json"), "PATH": f"{fake}:{os.environ['PATH']}"}

    def run(cmd):
        payload = {"session_id": SID, "tool_name": "Bash", "tool_input": {"command": cmd}}
        return subprocess.run([sys.executable, str(ROOT / ".claude" / "hooks" / "pre_tool_use.py")],
                              input=json.dumps(payload), capture_output=True, text=True, env=env, cwd=tmp_path,
                              timeout=60)

    r = run("systemctl restart neurogenserver-celery")
    assert r.returncode == 2 and "/verify" in r.stderr
    assert run("systemctl show -p ActiveEnterTimestamp neurogenserver-celery").returncode == 0
