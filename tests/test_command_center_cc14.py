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
