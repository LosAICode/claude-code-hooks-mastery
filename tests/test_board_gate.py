import json
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

import session_tracker as st
from conftest import ROOT, commit_file, git, write_config

SID = "gate0000-0000-0000-0000-000000000000"


def cfg_for(repo):
    return {"board_gate": {"repo": str(repo), "branch": "main", "board": "docs/Board.md", "code_paths": ["modules/"]}}


def session(repo, commits=(), deploys=(), started=None):
    started = started or (datetime.now().astimezone() - timedelta(minutes=5)).isoformat(timespec="seconds")
    rid = st.repo_id(str(repo))
    return {"session_id": SID, "started": started, "deploys": list(deploys),
            "commits": [{"sha": c, "repo": rid} for c in commits]}


def stop(**kw):
    return {"session_id": SID, "stop_hook_active": False, **kw}


def test_no_code_change_allows(repo):
    assert st.gate_reason(stop(), session(repo), cfg_for(repo)) is None


def test_code_and_board_updated_allows(repo):
    sha = commit_file(repo, "modules/a.py", "x", "code")
    commit_file(repo, "docs/Board.md", "board v2", "board")  # any author: the gate is author-agnostic
    assert st.gate_reason(stop(), session(repo, [sha]), cfg_for(repo)) is None


def test_code_without_board_blocks(repo):
    sha = commit_file(repo, "modules/a.py", "x", "code")
    reason = st.gate_reason(stop(), session(repo, [sha]), cfg_for(repo))
    assert reason == "Update your row in docs/Board.md (state, last commit, next) and commit it with a pathspec."


def test_merge_commit_into_main_counts(repo):
    git(repo, "checkout", "-q", "-b", "feat")
    commit_file(repo, "modules/b.py", "y", "feat")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "-m", "merge feat", "feat")
    merge = git(repo, "rev-parse", "HEAD")
    assert st.gate_reason(stop(), session(repo, [merge]), cfg_for(repo)) is not None


def test_branch_commit_not_on_main_allows(repo):
    git(repo, "checkout", "-q", "-b", "feat")
    sha = commit_file(repo, "modules/b.py", "y", "feat")
    git(repo, "checkout", "-q", "main")
    assert st.gate_reason(stop(), session(repo, [sha]), cfg_for(repo)) is None


def test_docs_only_commit_allows(repo):
    sha = commit_file(repo, "docs/other.md", "z", "docs")
    assert st.gate_reason(stop(), session(repo, [sha]), cfg_for(repo)) is None


def test_deploy_without_board_blocks(repo):
    s = session(repo, deploys=[{"ts": "x", "match": "restart"}])
    assert st.gate_reason(stop(), s, cfg_for(repo)) is not None


def test_board_committed_before_session_started_does_not_count(repo):
    commit_file(repo, "docs/Board.md", "board v2", "board",
                when=(datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds"))
    sha = commit_file(repo, "modules/a.py", "x", "code")
    assert st.gate_reason(stop(), session(repo, [sha]), cfg_for(repo)) is not None


def test_second_stop_never_blocks(repo):
    sha = commit_file(repo, "modules/a.py", "x", "code")
    assert st.gate_reason(stop(stop_hook_active=True), session(repo, [sha]), cfg_for(repo)) is None


def test_other_repos_commits_are_ignored(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    sha = commit_file(other, "modules/a.py", "x", "elsewhere")
    s = session(repo)
    s["commits"] = [{"sha": sha, "repo": st.repo_id(str(other))}]
    assert st.gate_reason(stop(), s, cfg_for(repo)) is None


def test_no_config_allows(repo):
    sha = commit_file(repo, "modules/a.py", "x", "code")
    assert st.gate_reason(stop(), session(repo, [sha]), {}) is None


@pytest.fixture
def gate_setup(tracker_env, tmp_path, repo):
    write_config(tmp_path, cfg_for(repo))
    sha = commit_file(repo, "modules/a.py", "x", "code")
    tracker_env.mkdir(parents=True, exist_ok=True)
    st.write_session(session(repo, [sha]))
    return repo


def test_stop_gate_blocks_once_per_session(gate_setup):
    first = st.stop_gate(stop())
    assert first is not None and first["decision"] == "block"
    assert st.stop_gate(stop()) is None


def test_repo_missing_allows_and_logs(tracker_env, tmp_path, gate_setup):
    cfg = cfg_for(tmp_path / "gone")
    write_config(tmp_path, cfg)
    assert st.stop_gate(stop()) is None
    assert "repo missing" in (tracker_env / "tracker.log").read_text()


def test_git_error_allows_and_logs(tracker_env, tmp_path, gate_setup):
    not_git = tmp_path / "plain"
    not_git.mkdir()
    write_config(tmp_path, cfg_for(not_git))
    assert st.stop_gate(stop()) is None
    assert "GitError" in (tracker_env / "tracker.log").read_text()


def test_session_log_copy_keeps_header_and_never_rewrites_unchanged(tmp_path):
    src, dest = tmp_path / "STATUS.md", tmp_path / "Session-Log.md"
    src.write_text("# Session status\n\n## new entry\n")
    dest.write_text("<!-- private copy\n     of the log -->\n# Session status\n")
    cfg = {"session_log": {"source": str(src), "dest": str(dest)}}
    assert st.sync_session_log(cfg) is True
    assert dest.read_text() == "<!-- private copy\n     of the log -->\n# Session status\n\n## new entry\n"
    assert st.sync_session_log(cfg) is False


def test_session_log_missing_dest_is_not_created(tmp_path):
    src = tmp_path / "STATUS.md"
    src.write_text("x")
    cfg = {"session_log": {"source": str(src), "dest": str(tmp_path / "nope" / "Log.md")}}
    assert st.sync_session_log(cfg) is False
    assert not (tmp_path / "nope").exists()


def test_stop_hook_prints_the_block_decision(gate_setup, tmp_path, tracker_env):
    r = subprocess.run([sys.executable, str(ROOT / ".claude" / "hooks" / "stop.py"), "--chat"],
                       input=json.dumps(stop()), capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert r.returncode == 0
    assert json.loads(r.stdout)["decision"] == "block"
