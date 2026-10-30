"""The tracker claims a prompt on the first message and reminds the session once to set its final status."""
import json

import pytest

import prompt_center as pc
import session_tracker as st
from conftest import commit_file, git, write_config

SID = "claim000-0000-0000-0000-000000000001"
PROMPT = "docs/Prompts/funnels/Funnels-B12-4-Webinar.md"


@pytest.fixture
def proj(tmp_path, tracker_env):
    """A git repo with one pending prompt, a tracker dir, and a config with the prompts block (gate on)."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, PROMPT, "---\nstatus: pending\norder: 9d\n---\n# B12-4\n", "prompt", when="2020-01-01T00:00:00")
    commit_file(r, "docs/Prompts/funnels/Funnels-B4-Done.md", "---\nstatus: done\n---\n# B4\n", "prompt2", when="2020-01-01T00:00:01")
    commit_file(r, "docs/Board.md", "board", "board", when="2020-01-01T00:00:02")
    write_config(tmp_path, {
        "board_gate": {"repo": str(r), "branch": "main", "board": "docs/Board.md", "code_paths": ["modules/"]},
        "prompts": {"repo": str(r), "root": "docs/Prompts", "gate": True, "commit_claims": True,
                    "claim_verbs": ["run", "execute", "start"]},
    })
    return r


def prompt_event(text, sid=SID):
    return {"session_id": sid, "cwd": "/elsewhere", "prompt": text, "hook_event_name": "UserPromptSubmit"}


def meta(r, rel=PROMPT):
    return pc.parse_frontmatter((r / rel).read_text())[0]


def test_first_run_message_claims_and_commits(proj):
    st.track("UserPromptSubmit", prompt_event(f"Run `{PROMPT}`"))
    m = meta(proj)
    assert m["status"] == "running" and m["session_id"] == SID
    s = st.load_session(SID)
    assert s["prompt_file"] == PROMPT and s["prompt_claimed"] is True and s["prompt_prev_status"] == "pending"
    assert git(proj, "log", "-1", "--format=%s") == "docs(prompts): funnels B12-4 running (claim000)"
    assert git(proj, "status", "--porcelain") == ""


def test_mention_without_verb_does_not_claim(proj):
    st.track("UserPromptSubmit", prompt_event(f"Look at {PROMPT} and fix the typo"))
    assert meta(proj)["status"] == "pending"
    s = st.load_session(SID)
    assert s.get("prompt_claimed") is None and s["prompt_mentioned"] == PROMPT


def test_done_prompt_is_never_claimed(proj):
    st.track("UserPromptSubmit", prompt_event("Run docs/Prompts/funnels/Funnels-B4-Done.md"))
    assert meta(proj, "docs/Prompts/funnels/Funnels-B4-Done.md")["status"] == "done"
    s = st.load_session(SID)
    assert s["prompt_file"] == "docs/Prompts/funnels/Funnels-B4-Done.md" and s["prompt_claimed"] is False


def test_only_the_first_message_claims(proj):
    st.track("UserPromptSubmit", prompt_event("fix the tests"))
    st.track("UserPromptSubmit", prompt_event(f"Run {PROMPT}"))
    assert meta(proj)["status"] == "pending" and st.load_session(SID).get("prompt_file") is None


def test_claim_survives_a_broken_prompt_module(proj, monkeypatch):
    monkeypatch.setattr(st, "_PROMPT_CENTER_MODULE", "no_such_module_xyz")
    st.track("UserPromptSubmit", prompt_event(f"Run {PROMPT}"))
    s = st.load_session(SID)
    assert s["task"] == PROMPT and s["state"] == "working"
    assert "no_such_module_xyz" in (st.tracker_dir() / "tracker.log").read_text()


def test_no_commit_when_commit_claims_is_off(proj, tmp_path):
    cfg = json.loads((tmp_path / "config.json").read_text())
    cfg["prompts"]["commit_claims"] = False
    write_config(tmp_path, cfg)
    st.track("UserPromptSubmit", prompt_event(f"Run {PROMPT}"))
    assert meta(proj)["status"] == "running"
    assert git(proj, "status", "--porcelain") == "M docs/Prompts/funnels/Funnels-B12-4-Webinar.md"


# --- the Stop gate -------------------------------------------------------------------------------------------


def claimed_session(proj, commits=(), deploys=()):
    st.track("UserPromptSubmit", prompt_event(f"Run {PROMPT}"))
    s = st.load_session(SID)
    rid = st.repo_id(str(proj))
    s["commits"] = [{"sha": c, "repo": rid} for c in commits]
    s["deploys"] = list(deploys)
    st.write_session(s)
    return s


def stop(**kw):
    return {"session_id": SID, "stop_hook_active": False, **kw}


def test_gate_blocks_once_when_prompt_still_running_after_a_commit(proj):
    claimed_session(proj, commits=[commit_file(proj, "docs/notes.md", "x", "docs")])
    out = st.stop_gate(stop())
    assert out["decision"] == "block" and PROMPT in out["reason"] and "command_center.py set" in out["reason"]
    assert st.load_session(SID)["prompt_gate_blocked"]
    assert st.stop_gate(stop()) is None


def test_gate_passes_without_commits_or_deploys(proj):
    claimed_session(proj)
    assert st.stop_gate(stop()) is None


def test_gate_passes_once_status_changed(proj):
    claimed_session(proj, commits=[commit_file(proj, "docs/notes.md", "x", "docs")])
    pc.set_status({"repo": str(proj), "root": "docs/Prompts"}, PROMPT, "on-main", commit="abc1234")
    assert st.stop_gate(stop()) is None


def test_gate_passes_for_another_sessions_claim(proj):
    claimed_session(proj, commits=[commit_file(proj, "docs/notes.md", "x", "docs")])
    pc.write_meta(proj / PROMPT, {"session_id": "other-session"})
    assert st.stop_gate(stop()) is None


def test_gate_respects_stop_hook_active_and_unclaimed(proj):
    claimed_session(proj, commits=[commit_file(proj, "docs/notes.md", "x", "docs")])
    assert st.stop_gate(stop(stop_hook_active=True)) is None
    st.track("UserPromptSubmit", prompt_event("just chatting", sid="other000-0000-0000-0000-000000000002"))
    assert st.stop_gate({"session_id": "other000-0000-0000-0000-000000000002", "stop_hook_active": False}) is None


def test_gate_off_in_config(proj, tmp_path):
    cfg = json.loads((tmp_path / "config.json").read_text())
    cfg["prompts"]["gate"] = False
    write_config(tmp_path, cfg)
    claimed_session(proj, commits=[commit_file(proj, "docs/notes.md", "x", "docs")])
    assert st.stop_gate(stop()) is None


def test_both_gates_combine(proj):
    sha = commit_file(proj, "modules/a.py", "code", "code")      # code on main, board untouched
    claimed_session(proj, commits=[sha])
    out = st.stop_gate(stop())
    assert "docs/Board.md" in out["reason"] and PROMPT in out["reason"]
    s = st.load_session(SID)
    assert s["board_gate_blocked"] and s["prompt_gate_blocked"]
    assert st.stop_gate(stop()) is None


def test_prompt_gate_without_board_gate_config(proj, tmp_path):
    cfg = json.loads((tmp_path / "config.json").read_text())
    del cfg["board_gate"]
    write_config(tmp_path, cfg)
    claimed_session(proj, commits=[commit_file(proj, "docs/notes.md", "x", "docs")])
    assert st.stop_gate(stop())["decision"] == "block"


def test_unreadable_prompt_file_lets_the_stop_through(proj):
    claimed_session(proj, commits=[commit_file(proj, "docs/notes.md", "x", "docs")])
    (proj / PROMPT).unlink()
    assert st.stop_gate(stop()) is None
    assert "prompt_gate" in (st.tracker_dir() / "tracker.log").read_text()
