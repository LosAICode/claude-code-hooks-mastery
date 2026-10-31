"""issues: one GitHub issue per prompt; the audit, --fix by title, and set --comment / --close."""
import json

import pytest

import command_center as cc
import prompt_center as pc
import prompt_issues as pi
from conftest import git
from test_prompt_join import NOW

CALLS: list = []  # the gh calls a test made (gh itself is faked)


@pytest.fixture
def issues_proj(tmp_path, monkeypatch):
    r = tmp_path / "proj"
    for rel, text in {
        "docs/Prompts/connections/Connections-R7-Zapier.md": "---\nstatus: pending\nissue: \"580\"\n---\n# R7: zapier\n",
        "docs/Prompts/connections/Connections-R8-Slack.md": "---\nstatus: done\nissue: 581\n---\n# R8: slack\n",
        "docs/Prompts/connections/Connections-R9-Meta.md": "---\nstatus: pending\n---\n# R9: meta\n",
        "docs/Prompts/connections/Connections-R2-Calendly.md": "---\nstatus: pending\nissue: [574, 999]\n---\n# R2: calendly\n",
        "docs/Prompts/ops/Ops-CC3-Center.md": "---\nmodule: ops\nid: CC3\nstatus: running\n---\n# CC3: center\n",
        "docs/Prompts/ops/Ops-CC9-Nothing.md": "---\nmodule: ops\nid: CC9\nstatus: pending\n---\n# CC9: nothing\n",
    }.items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    gh_issues = [
        {"number": 580, "state": "OPEN", "title": "[API Connections] R7: Zapier / Make", "body": "Prompt: x"},
        {"number": 584, "state": "CLOSED", "title": "[API Connections] R7: Zapier / Make", "body": "Prompt: x"},
        {"number": 581, "state": "OPEN", "title": "[API Connections] R8: Slack alerts", "body": ""},
        {"number": 582, "state": "OPEN", "title": "[API Connections] R9: Meta Lead Ads", "body": ""},
        {"number": 574, "state": "CLOSED", "title": "[API Connections] R2: Calendly and Zoom", "body": ""},
        {"number": 605, "state": "OPEN", "title": "[Ops] CC3: command center",
         "body": "Prompt: `docs/Prompts/ops/Ops-CC3-Center.md`\n\nmore"},
        {"number": 579, "state": "OPEN", "title": "[API Connections] Epic: audit", "body": "R7, R8, R9"},
    ]
    CALLS.clear()
    monkeypatch.setattr(pi, "fetch_issues", lambda repo: gh_issues)
    monkeypatch.setattr(pi, "gh", lambda repo, *args: CALLS.append(args) or "")
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    c = {"prompts": {"repo": str(r), "root": "docs/Prompts"}}
    (tmp_path / "config.json").write_text(json.dumps(c))
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    return r




def run(*args):
    return cc.main(["--now", NOW.isoformat(timespec="seconds"), *args])


def meta(r, name, module="connections"):
    return pc.parse_frontmatter((r / f"docs/Prompts/{module}/{name}").read_text())[0]


def test_issue_numbers_of_a_prompt():
    assert pi.issues_of({"issue": "605"}) == [605]
    assert pi.issues_of({"issue": [574, "999"]}) == [574, 999]
    assert pi.issues_of({"issue": "#12, #13"}) == [12, 13]
    assert pi.issues_of({}) == []


def test_issues_audit_lists_missing_unlinked_duplicates_and_state_mismatches(issues_proj, capsys):
    assert run("issues") == 0
    out = capsys.readouterr().out
    assert "No issue (3)" in out and "connections R9" in out and "ops CC3" in out and "ops CC9" in out
    assert "#584 names connections R7; its frontmatter lists #580" in out
    assert "#605 names ops CC3; its frontmatter lists none" in out
    assert "connections R7: #580 (open), #584 (closed)" in out
    assert "connections R8 is done but #581 is open" in out
    assert "connections R2 is pending but #574 is closed" in out
    assert "#999 (connections R2) is not on GitHub" in out
    assert "#579" not in out  # an epic names no single prompt


def test_issues_fix_writes_keys_it_can_match_and_commits(issues_proj, capsys):
    assert run("issues", "--fix") == 0
    assert meta(issues_proj, "Connections-R9-Meta.md")["issue"] == "582"
    assert meta(issues_proj, "Ops-CC3-Center.md", "ops")["issue"] == "605"
    assert "issue" not in meta(issues_proj, "Ops-CC9-Nothing.md", "ops")
    assert meta(issues_proj, "Connections-R7-Zapier.md")["issue"] == "580"  # never rewritten
    assert git(issues_proj, "log", "-1", "--format=%s") == "docs(prompts): issues --fix: 2 issue keys (connections R9; ops CC3)"
    assert CALLS == []  # nothing created or changed on GitHub


def test_set_comment_posts_one_progress_comment(issues_proj, capsys):
    assert run("set", "connections/R7", "on-main", "--commit", "abc1234", "--comment", "--no-commit") == 0
    assert CALLS == [("issue", "comment", "580", "--body",
                                  "connections R7: pending → on-main · commit `abc1234`")]


def test_set_done_closes_only_with_close(issues_proj, capsys):
    assert run("set", "connections/R9", "done", "--no-commit") == 0
    assert CALLS == []
    assert run("set", "connections/R7", "done", "--commit", "abc1234", "--notes", "live 10-30", "--close", "--no-commit") == 0
    assert CALLS == [("issue", "close", "580", "--comment",
                                  "connections R7: pending → done · commit `abc1234` · live 10-30")]


def test_close_needs_a_closing_status(issues_proj, capsys):
    assert run("set", "connections/R7", "on-main", "--close", "--no-commit") == 2
    assert "--close" in capsys.readouterr().err
    assert meta(issues_proj, "Connections-R7-Zapier.md")["status"] == "pending"


def test_comment_without_an_issue_says_so(issues_proj, capsys):
    assert run("set", "ops/CC9", "on-main", "--comment", "--no-commit") == 0
    assert "no issue" in capsys.readouterr().out and CALLS == []


def test_module_view_and_table_show_the_issue(issues_proj, capsys):
    assert run("--module", "connections") == 0
    out = capsys.readouterr().out
    assert "issue" in out.splitlines()[2] and "580" in out and "574, 999" in out
