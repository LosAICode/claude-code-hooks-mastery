"""CC10 item 6: the Stop gate waits while background subagents run (SubagentStart opens one, SubagentStop closes it)."""
import json
import os
import time

import prompt_center as pc
import session_tracker as st
from conftest import write_config
from test_command_center_cc7 import BODY, REL, SID, _commit_in_session, gated  # noqa: F401  (gated is a fixture)
from test_session_tracker import _run_hook

STOP = {"session_id": SID, "stop_hook_active": False}


def agent(event, aid, sid=SID):
    return {"session_id": sid, "cwd": "/elsewhere", "agent_id": aid, "agent_type": "builder", "hook_event_name": event}


def test_start_opens_and_stop_closes_an_agent(tracker_env):
    st.track("SubagentStart", agent("SubagentStart", "a1"))
    st.track("SubagentStart", agent("SubagentStart", "a2"))
    assert sorted(st.open_agents(st.load_session(SID))) == ["a1", "a2"]
    st.track("SubagentStop", agent("SubagentStop", "a1"))
    assert st.open_agents(st.load_session(SID)) == ["a2"]


def test_a_stop_with_no_start_is_harmless(tracker_env):
    # live 11-01: SubagentStop fires for agents the session never saw start (compaction's summariser, agent_type "")
    st.track("SubagentStop", agent("SubagentStop", "ghost"))
    st.track("SubagentStop", agent("SubagentStop", "ghost"))
    assert st.open_agents(st.load_session(SID)) == []


def test_an_agent_older_than_the_cap_counts_as_closed(tracker_env):
    st.track("SubagentStart", agent("SubagentStart", "lost"))
    s = st.load_session(SID)
    old = time.time() - st.OPEN_AGENT_MAX_AGE_S - 60
    s["open_agents"]["lost"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(old))
    st.write_session(s)
    assert st.open_agents(st.load_session(SID)) == []


def test_status_and_progress_gates_wait_while_an_agent_runs(gated):
    r = gated
    pc.write_meta(r / REL, {"updated": "2026-10-30T09:00:00-04:00"})
    _commit_in_session(r, "feat: one", "t1")
    st.track("SubagentStart", agent("SubagentStart", "b1"))
    assert st.stop_gate(STOP) is None                      # the builder is still working: no ask, nothing spent
    s = st.load_session(SID)
    assert not s.get("prompt_gate_blocked") and not s.get("progress_gate_blocked")
    st.track("SubagentStop", agent("SubagentStop", "b1"))
    first = st.stop_gate(STOP)
    assert first and "still `running`" in first["reason"]   # the status ask, now that the agents are done
    second = st.stop_gate(STOP)
    assert second and "--progress" in second["reason"]


def test_progress_gate_waits_too_after_the_status_ask(gated):
    r = gated
    pc.write_meta(r / REL, {"updated": "2026-10-30T09:00:00-04:00"})
    _commit_in_session(r, "feat: one", "t1")
    assert "still `running`" in st.stop_gate(STOP)["reason"]
    st.track("SubagentStart", agent("SubagentStart", "b2"))
    assert st.stop_gate(STOP) is None
    assert not st.load_session(SID).get("progress_gate_blocked")
    st.track("SubagentStop", agent("SubagentStop", "b2"))
    assert "--progress" in st.stop_gate(STOP)["reason"]


def test_claim_gate_still_asks_while_an_agent_runs(tmp_path, tracker_env):
    # a claim is wanted as early as possible; it does not depend on the work being finished
    write_config(tmp_path, {"prompts": {"repo": str(tmp_path), "root": "docs/Prompts", "gate": True}})
    st.track("UserPromptSubmit", {"session_id": SID, "cwd": str(tmp_path), "prompt": "fix it"})
    s = st.load_session(SID)
    s["commits"] = [{"sha": "abc1234", "subject": "feat: x"}]
    st.write_session(s)
    st.track("SubagentStart", agent("SubagentStart", "b3"))
    out = st.stop_gate(STOP)
    assert out and "holds no prompt" in out["reason"]


def test_the_subagent_hooks_record_open_agents(tmp_path):
    d = tmp_path / "tracker"
    env = {**os.environ, "SESSION_TRACKER_DIR": str(d), "SESSION_TRACKER_CONFIG": str(tmp_path / "none.json")}
    assert _run_hook("subagent_start.py", agent("SubagentStart", "h1"), env, tmp_path).returncode == 0
    assert list(json.loads((d / f"{SID}.json").read_text())["open_agents"]) == ["h1"]
    r = _run_hook("subagent_stop.py", {**agent("SubagentStop", "h1"), "stop_hook_active": False}, env, tmp_path)
    assert r.returncode == 0
    assert json.loads((d / f"{SID}.json").read_text())["open_agents"] == {}
