"""CC9: a commit is credited only to the session whose Bash call made it; restarts route through the Command Center."""
import json
import os
import subprocess
import sys

import pytest

import prompt_center as pc
import session_tracker as st
from conftest import ROOT, commit_file, git, write_config

SID = "cc9cc9cc-0000-0000-0000-000000000001"
OTHER = "0th3r000-0000-0000-0000-000000000002"
MINE_URL = "https://claude.ai/code/session_MINE"
OTHER_URL = "https://claude.ai/code/session_OTHER"
REL = "docs/Prompts/connections/Connections-R9-Meta-Lead-Ads.md"


def meta_of(path):
    return pc.parse_frontmatter(path.read_text())[0]


def bash(cmd, tid="t1", sid=SID, cwd="/tmp"):
    return {"session_id": sid, "cwd": str(cwd), "tool_name": "Bash", "tool_use_id": tid, "tool_input": {"command": cmd}}


def around(cmd, make, tid="t1", cwd="/tmp"):
    """PreToolUse snapshot, then whatever lands while the command runs, then PostToolUse."""
    st.track("PreToolUse", bash(cmd, tid, cwd=cwd))
    out = make()
    st.track("PostToolUse", bash(cmd, tid, cwd=cwd))
    return out


def shas():
    return [c["sha"] for c in st.load_session(SID)["commits"]]


@pytest.fixture
def live(tmp_path, tracker_env):
    """A live tree with a prompts tree, and this session holding R9."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, REL, "---\nmodule: connections\nid: R9\nstatus: pending\n---\n# R9\n", "prompt",
                when="2026-10-31T08:00:00")
    write_config(tmp_path, {"prompts": {"repo": str(r), "root": "docs/Prompts", "commit_claims": False}})
    st.track("UserPromptSubmit", {"session_id": SID, "cwd": str(r), "prompt": f"run {REL}"})
    assert st.load_session(SID)["prompt_claimed"]
    return r


def other_session(tracker_env, url, state="working", last_seen=None):
    tracker_env.mkdir(parents=True, exist_ok=True)
    (tracker_env / f"{OTHER}.json").write_text(json.dumps({
        "session_id": OTHER, "state": state, "last_seen": last_seen or st.now_iso(), "claude_session": url}))


# --- 8. credit a commit only to the session that made it ----------------------------------------------------------

def test_r9_m02_a_foreign_commit_during_a_command_center_set_is_not_credited(live):
    # Live 10-31: Connections ran `command_center.py set connections/R9 …` in the shared tree; Security M02 committed
    # 280389595 there meanwhile; the before/after snapshot saw HEAD move through it and put it in R9's `commits:`.
    r = live
    cmd = f"cd {r} && python3 /x/scripts/command_center.py set connections/R9 on-main --commit cda15d991"

    def land():
        foreign = commit_file(r, "Documentation/security/M02.md", "m02", "docs(security): M02 audit")
        status = commit_file(r, "docs/Status-Board.md", "r9", "docs(prompts): connections R9 → on-main")
        return foreign, status

    foreign, status = around(cmd, land, cwd=r)
    assert foreign not in shas() and status in shas()
    assert not meta_of(r / REL).get("commits")
    skipped = st.load_session(SID)["skipped_commits"]
    assert [s["sha"] for s in skipped] == [foreign]


def test_a_pathspec_commit_records_only_its_own_commit(live):
    r = live
    cmd = f"cd {r} && git commit -m 'fix(x): y' -- a.py"

    def land():
        commit_file(r, "b.py", "b", "fix(other): z")
        commit_file(r, "b2.py", "b", "fix(x): y")  # the same subject, a file outside the pathspec
        return commit_file(r, "a.py", "a", "fix(x): y")

    own = around(cmd, land, cwd=r)
    assert shas() == [own]
    assert meta_of(r / REL)["commits"] == [own[:9]]
    assert len(st.load_session(SID)["skipped_commits"]) == 2


def test_a_directory_pathspec_covers_files_under_it(live):
    r = live
    own = around(f"git -C {r} commit -m 'feat: dir' -- modules/", lambda: commit_file(r, "modules/x/a.py", "a", "feat: dir"))
    assert shas() == [own]


@pytest.mark.parametrize("cmd_tail,subject", [
    ("commit -m 'feat: s' -m 'the body'", "feat: s"),
    ("commit -am 'feat: c'", "feat: c"),
    ("commit --message='feat: e'", "feat: e"),
    ("commit -m \"$(cat <<'EOF'\nfeat: h\n\nbody\nEOF\n)\" -- a.py", "feat: h"),
])
def test_the_message_forms_match_the_subject(live, cmd_tail, subject):
    r = live
    cmd = f"git -C {r} {cmd_tail}"

    def land():
        foreign = commit_file(r, "b.py", "b", "fix(other): z")
        return foreign, commit_file(r, "a.py", "a", subject)

    foreign, own = around(cmd, land, cwd=r)
    assert shas() == [own]


def test_a_message_from_a_file_has_no_subject_rule(live):
    r = live
    cmd = f"git -C {r} commit -F - <<'EOF'\nfeat: f\nEOF"
    own = around(cmd, lambda: commit_file(r, "a.py", "a", "feat: f"), cwd=r)
    assert shas() == [own]


def test_a_merge_brings_in_its_commits(live):
    r = live
    git(r, "checkout", "-q", "-b", "feat")
    sha = commit_file(r, "m.py", "m", "feat: m")
    git(r, "checkout", "-q", "main")
    around(f"git -C {r} merge --ff-only feat", lambda: git(r, "merge", "-q", "--ff-only", "feat"), cwd=r)
    assert shas() == [sha]


def test_a_command_that_only_mentions_git_commit_records_nothing(live):
    r = live
    around(f"cd {r} && echo 'git commit -m x'", lambda: commit_file(r, "a.py", "a", "x"), cwd=r)
    assert st.load_session(SID)["commits"] == []


def test_another_live_sessions_trailer_is_skipped_and_mine_is_remembered(live, tracker_env):
    r = live
    other_session(tracker_env, OTHER_URL)

    def land():
        commit_file(r, "b.py", "b", f"feat: same\n\nClaude-Session: {OTHER_URL}")
        return commit_file(r, "a.py", "a", f"feat: same\n\nClaude-Session: {MINE_URL}")

    own = around(f"git -C {r} commit -m 'feat: same'", land, cwd=r)
    assert shas() == [own]
    assert st.load_session(SID)["claude_session"] == MINE_URL
    # a merge has no subject rule: the trailer alone keeps the other session's commit out
    git(r, "checkout", "-q", "-b", "feat")
    theirs = commit_file(r, "c.py", "c", f"feat: c\n\nClaude-Session: {OTHER_URL}")
    git(r, "checkout", "-q", "main")
    around(f"git -C {r} merge --ff-only feat", lambda: git(r, "merge", "-q", "--ff-only", "feat"), "t2", cwd=r)
    assert theirs not in shas()


def test_an_ended_sessions_trailer_is_not_skipped(live, tracker_env):
    r = live
    other_session(tracker_env, OTHER_URL, state="ended")
    git(r, "checkout", "-q", "-b", "feat")
    theirs = commit_file(r, "c.py", "c", f"feat: c\n\nClaude-Session: {OTHER_URL}")
    git(r, "checkout", "-q", "main")
    around(f"git -C {r} merge --ff-only feat", lambda: git(r, "merge", "-q", "--ff-only", "feat"), cwd=r)
    assert shas() == [theirs]


# --- 5. restarts route through the Command Center -------------------------------------------------------------------

COORD = "c00rd1na-0000-0000-0000-000000000009"
ON = {"prompts": {"deploy_coordinator": {"session": COORD[:8]}}}


def gate(cmd, cfg=ON, sid=SID, active=lambda unit: "active"):
    return st.restart_gate(bash(cmd, sid=sid), cfg, is_active=active)


def test_restart_gate_is_off_by_default(tracker_env):
    assert gate("systemctl restart neurogenserver-celery", cfg={}) is None
    assert gate("systemctl restart neurogenserver-celery", cfg={"prompts": {"deploy_coordinator": {}}}) is None


@pytest.mark.parametrize("cmd", [
    "systemctl restart neurogenserver-celery",
    "flock /run/lock/x scripts/restart_web.sh",
    "sudo systemctl stop neurogenserver-voice",
    "systemctl start neurogenserver-channels.service",
    "systemctl try-restart neurogenserver-celery-beat",
])
def test_restart_gate_blocks_a_non_coordinator(tracker_env, cmd):
    reason = gate(cmd)
    assert reason and "deploy request" in reason and "NEUROGEN_DEPLOY_OVERRIDE" in reason


@pytest.mark.parametrize("cmd", [
    "systemctl show -p ActiveEnterTimestamp neurogenserver-celery",
    "systemctl status neurogenserver-celery",
    "systemctl is-active neurogenserver",
    "grep -n x scripts/restart_web.sh",
    'echo "run systemctl restart neurogenserver"',
])
def test_restart_gate_never_blocks_a_read(tracker_env, cmd):
    assert gate(cmd) is None


def test_restart_gate_lets_the_coordinator_restart(tracker_env):
    assert gate("scripts/restart_web.sh", sid=COORD) is None


def test_restart_gate_allows_an_open_deploy_run_only(tracker_env):
    deploys = tracker_env / "deploys"
    deploys.mkdir(parents=True)
    (deploys / "20261101-0100.json").write_text(json.dumps({"start": "x"}))
    (deploys / "20261101-0000.json").write_text(json.dumps({"start": "x", "end": "y"}))
    assert gate("NEUROGEN_DEPLOY_RUN=20261101-0100 systemctl restart neurogenserver-celery") is None
    assert gate("NEUROGEN_DEPLOY_RUN=20261101-0000 systemctl restart neurogenserver-celery")  # closed
    assert gate("NEUROGEN_DEPLOY_RUN=nope systemctl restart neurogenserver-celery")  # missing
    assert gate("NEUROGEN_DEPLOY_RUN=../x systemctl restart neurogenserver-celery")  # not a deploy id
    # the id must sit on the restart itself, not on an earlier command
    assert gate("export NEUROGEN_DEPLOY_RUN=20261101-0100; systemctl restart neurogenserver-celery")


def test_restart_gate_override_is_allowed_and_logged(tracker_env):
    assert gate('NEUROGEN_DEPLOY_OVERRIDE="celery wedged, owner on the phone" systemctl restart neurogenserver-celery') is None
    rows = [json.loads(x) for x in (tracker_env / "deploys" / "overrides.jsonl").read_text().splitlines()]
    assert rows[0]["reason"] == "celery wedged, owner on the phone" and rows[0]["session_id"] == SID
    assert rows[0]["match"] and rows[0]["ts"]
    assert gate('NEUROGEN_DEPLOY_OVERRIDE="" systemctl restart neurogenserver-celery')  # an empty reason is no override


def test_restart_gate_never_stops_an_outage_restart(tracker_env):
    asked = []

    def down(unit):
        asked.append(unit)
        return "failed" if unit != "neurogenserver-web@5028" else "inactive"

    assert gate("scripts/restart_web.sh", active=down) is None
    assert asked == ["neurogenserver", "neurogenserver-web@5027", "neurogenserver-web@5028"]
    assert gate("systemctl restart neurogenserver-celery", active=lambda u: "failed") is None
    # one unit still up: not an outage
    assert gate("scripts/restart_web.sh", active=lambda u: "active" if u == "neurogenserver" else "failed")


def test_pre_tool_use_hook_blocks_a_restart_when_switched_on(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "systemctl").write_text("#!/bin/sh\necho active\n")
    (fake / "systemctl").chmod(0o755)
    (tmp_path / "on.json").write_text(json.dumps(ON))
    env = {**os.environ, "SESSION_TRACKER_DIR": str(tmp_path / "tracker"),
           "SESSION_TRACKER_CONFIG": str(tmp_path / "on.json"), "PATH": f"{fake}:{os.environ['PATH']}"}

    def run(cmd):
        return subprocess.run([sys.executable, str(ROOT / ".claude" / "hooks" / "pre_tool_use.py")],
                              input=json.dumps(bash(cmd)), capture_output=True, text=True, env=env, cwd=tmp_path,
                              timeout=60)

    r = run("systemctl restart neurogenserver-celery")
    assert r.returncode == 2 and "deploy request" in r.stderr
    assert run("systemctl show -p ActiveEnterTimestamp neurogenserver-celery").returncode == 0
