"""CC10: owner holds, a deploy-request queue, the cross-prompt commit finding.

Reuses the CC9 box (two on-main prompts, fake systemctl/curl/psql first on PATH, a tmp tracker dir and deploy lock):
nothing real restarts, and every request file lives in the test's own tracker dir."""
# ruff: noqa: F811  (the tests take fixtures imported by name)
import json
import threading
from pathlib import Path

import pytest

import command_center as cc
import prompt_center as pc
try:
    import prompt_requests as pq
except ImportError:  # before CC10: the tests fail one by one, not at collection
    pq = None
from conftest import commit_file, git
from test_command_center_cc9 import box, fakebin, restarted, run, write_prompt  # noqa: F401

A1 = "docs/Prompts/agents/Agents-A1-X.md"
PEER = "bbbb2222-0000-0000-0000-000000000002"


def meta(box, rel=A1):
    return pc.parse_frontmatter((box / rel).read_text())[0]


def reqs(tmp_path):
    f = tmp_path / "tracker" / "deploys" / "requests.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()] if f.exists() else []


def last_subject(box):
    return git(box, "log", "-1", "--format=%s")


# --- 1. owner holds ----------------------------------------------------------------------------------------------

def test_hold_writes_the_quote_and_commits_and_unhold_removes_it(box, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "cccc3333-0000-0000-0000-000000000003")
    assert run("hold", "agents/A1", "--owner", "Please don't schedule it from this request") == 0
    hold = meta(box)["hold"]
    assert hold["owner"] == "Please don't schedule it from this request"
    assert hold["by"].startswith("cccc3333") and hold["at"]
    assert meta(box)["status"] == "on-main"  # a hold is not a status
    assert last_subject(box) == "docs(prompts): agents A1 held by the owner"
    assert git(box, "status", "--porcelain", "--", A1) == ""
    assert run("unhold", "agents/A1") == 0
    assert "hold" not in meta(box) and last_subject(box) == "docs(prompts): agents A1 unheld"


def test_hold_needs_the_owners_words(box, capsys):
    assert run("hold", "agents/A1", "--owner", "  ") == 2
    assert "owner" in capsys.readouterr().err and "hold" not in meta(box)
    assert run("unhold", "agents/A1") == 2  # nothing to remove
    assert run("hold", "agents/NOPE", "--owner", "x") == 2


def test_a_hold_round_trips_through_the_frontmatter(box):
    text = pc.dump_frontmatter({"id": "A1", "hold": {"owner": 'say "no": not now', "by": "x", "at": "t"}})
    assert pc.parse_frontmatter(text + "body")[0]["hold"] == {"owner": 'say "no": not now', "by": "x", "at": "t"}


def test_deploy_ready_waits_on_a_held_prompt_it_would_ship(box, capsys):
    run("hold", "agents/A1", "--owner", "not today", "--no-commit")
    capsys.readouterr()
    assert run("deploy-ready") == 1
    out = capsys.readouterr().out
    assert 'WAIT: agents/A1 held by the owner ("not today")\n' in out and "Deploy: WAIT (1 reasons)" in out
    assert run("deploy-ready", "--json") == 1
    assert json.loads(capsys.readouterr().out)["waits"] == ['agents/A1 held by the owner ("not today")']
    run("unhold", "agents/A1", "--no-commit")
    capsys.readouterr()
    assert run("deploy-ready") == 0


def test_a_hold_on_a_prompt_that_does_not_ship_is_no_wait(box, capsys):
    write_prompt(box, "agents/Agents-A3-X.md", {"id": "A3", "status": "pending"})
    run("hold", "agents/A3", "--owner", "later", "--no-commit")
    capsys.readouterr()
    assert run("deploy-ready") == 0


def test_deploy_run_refuses_a_held_prompt_even_with_the_owners_ok(box, capsys, tmp_path):
    run("hold", "agents/A2", "--owner", "hold A2", "--no-commit")
    capsys.readouterr()
    assert run("deploy-run", "--owner-ok", "go", "--no-commit") == 1
    out = capsys.readouterr().out
    assert 'WAIT: agents/A2 held by the owner ("hold A2")' in out and "Nothing restarted" in out
    assert restarted(tmp_path) == [] and not list((tmp_path / "tracker" / "deploys").glob("*.json"))


def test_view_lists_holds_under_blocked_on_owner(box, capsys):
    run("hold", "agents/A1", "--owner", "not today", "--no-commit")
    capsys.readouterr()
    assert run() == 0
    out = capsys.readouterr().out
    section = out.split("Blocked on owner", 1)[1]
    assert "A1" in section.split("\n\n", 1)[0] and 'held by the owner ("not today")' in section


# --- 2. the deploy-request queue -----------------------------------------------------------------------------------

def test_request_appends_an_open_request_with_the_session(box, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", PEER)
    assert run("request", "agents/A1", "--note", "web only") == 0
    out = capsys.readouterr().out
    [r] = reqs(tmp_path)
    assert r["prompt"] == "agents/A1" and r["note"] == "web only" and r["state"] == "open"
    assert r["session"] == PEER and r["id"] and r["ts"]
    assert r["id"] in out and "SendMessage" in out
    assert run("request", "agents/NOPE") == 2 and len(reqs(tmp_path)) == 1


def test_requests_lists_open_and_all_includes_closed(box, capsys, tmp_path):
    run("request", "agents/A1")
    run("request", "agents/A2", "--note", "celery too")
    capsys.readouterr()
    first, second = reqs(tmp_path)
    pq.update(lambda r: r["id"] == first["id"], {"state": "done", "deploy": "x"})
    assert run("requests") == 0
    out = capsys.readouterr().out
    assert second["id"] in out and "celery too" in out and first["id"] not in out
    assert run("requests", "--all") == 0
    out = capsys.readouterr().out
    assert first["id"] in out and second["id"] in out and "done" in out
    assert run("requests", "--json") == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)["requests"]] == [second["id"]]


def test_deploy_ready_answer_records_the_verdict(box, capsys, tmp_path):
    run("request", "agents/A1")
    run("request", "agents/A2")
    capsys.readouterr()
    a, b = reqs(tmp_path)
    assert run("deploy-ready", "--answer", a["id"]) == 0
    assert f"request {a['id']} answered: OK" in capsys.readouterr().out
    (tmp_path / "state" / "builds").write_text("2\n")
    assert run("deploy-ready", "--json", "--answer", b["id"]) == 1
    capsys.readouterr()
    got = {r["id"]: r for r in reqs(tmp_path)}
    assert got[a["id"]]["state"] == "answered" and got[a["id"]]["verdict"] == "OK"
    assert got[b["id"]]["state"] == "answered" and got[b["id"]]["verdict"].startswith("WAIT: no build running")
    assert run("deploy-ready", "--answer", "nope") == 2 and "nope" in capsys.readouterr().err


def test_hold_marks_the_prompts_requests_held_and_unhold_reopens_them(box, capsys, tmp_path):
    run("request", "agents/A1")
    run("request", "agents/A2")
    run("request", "agents/A1", "--note", "again")
    capsys.readouterr()
    a, b, c = reqs(tmp_path)
    run("deploy-ready", "--answer", c["id"])
    run("hold", "agents/A1", "--owner", "not today", "--no-commit")
    got = {r["id"]: r["state"] for r in reqs(tmp_path)}
    assert got == {a["id"]: "held", b["id"]: "open", c["id"]: "held"}
    run("unhold", "agents/A1", "--no-commit")
    assert {r["id"]: r["state"] for r in reqs(tmp_path)} == {a["id"]: "open", b["id"]: "open", c["id"]: "open"}


def test_deploy_run_marks_the_requests_it_shipped_done(box, capsys, tmp_path):
    write_prompt(box, "agents/Agents-A3-X.md", {"id": "A3", "status": "pending"})
    for ref in ("agents/A1", "agents/A2", "agents/A3"):
        run("request", ref)
    assert run("deploy-run", "--owner-ok", "go", "--dry-run") == 0
    assert all(r["state"] == "open" for r in reqs(tmp_path))  # a dry run closes nothing
    assert run("deploy-run", "--owner-ok", "go", "--no-commit") == 0, capsys.readouterr()
    rec = json.loads(next((tmp_path / "tracker" / "deploys").glob("*.json")).read_text())
    got = {r["prompt"]: r for r in reqs(tmp_path)}
    assert got["agents/A1"]["state"] == "done" and got["agents/A1"]["deploy"] == rec["id"]
    assert got["agents/A2"]["state"] == "done" and got["agents/A3"]["state"] == "open"


def test_view_inbox_shows_open_requests_oldest_first(box, capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", PEER)
    run("--now", "2026-10-30T15:00:00", "request", "agents/A2", "--note", "first")
    run("--now", "2026-10-30T15:30:00", "request", "agents/A1", "--note", "second")
    run("--now", "2026-10-30T15:40:00", "request", "agents/A1", "--note", "closed")
    pq.update(lambda r: r["note"] == "closed", {"state": "done"})
    capsys.readouterr()
    assert run() == 0
    out = capsys.readouterr().out
    inbox = out.split("Inbox", 1)[1].split("\n\n", 1)[0]
    assert inbox.index("first") < inbox.index("second") and "closed" not in inbox
    assert "agents/A2" in inbox and PEER[:8] in inbox


def test_concurrent_writers_lose_nothing(box, tmp_path):
    def add(n):
        for i in range(10):
            pq.add(f"agents/A{n}", note=f"{n}-{i}", session=None, now=1_000_000.0 + i)

    def flip():
        for _ in range(20):
            pq.update(lambda r: r["note"].endswith("-0"), {"state": "answered", "verdict": "OK"})

    threads = [threading.Thread(target=add, args=(n,)) for n in range(4)] + [threading.Thread(target=flip)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    got = reqs(tmp_path)
    assert len(got) == 40 and len({r["id"] for r in got}) == 40
    assert sorted(r["note"] for r in got) == sorted(f"{n}-{i}" for n in range(4) for i in range(10))


def test_cc_command_files_name_the_new_commands():
    root = Path(__file__).resolve().parent.parent / ".claude" / "commands"
    a, b = (root / "cc.md").read_text(), (root / "command-center.md").read_text()
    assert a.split("---", 2)[2] == b.split("---", 2)[2]
    for word in ("hold", "unhold", "request", "requests", "--answer"):
        assert word in a
    assert "then SendMessage the /cc session" in a


# --- 5. the cross-prompt commit finding --------------------------------------------------------------------------

@pytest.fixture
def crossed(box):
    """R9 lists a docs commit whose subject names security/M02 (live: M02's 280389595 in R9's commits)."""
    m02 = commit_file(box, "docs/m02.md", "x", "docs(security): M02 audit report", when="2026-10-30T12:00:00")
    own = commit_file(box, "modules/r9.py", "x", "feat(connections): R9 Meta leads, after M02 review", when="2026-10-30T12:01:00")
    s3 = commit_file(box, "modules/s3.py", "x", "fix: upload to S3 with A1-style retry", when="2026-10-30T12:02:00")
    write_prompt(box, "connections/Connections-R9-X.md",
                 {"id": "R9", "status": "on-main", "commits": f"[\"{m02[:9]}\", \"{own[:9]}\", \"{s3[:9]}\", \"0123abcd9\"]"})
    write_prompt(box, "security/Security-M02-X.md", {"id": "M02", "status": "done", "commit": f"\"{m02[:9]}\""})
    write_prompt(box, "storage/Storage-S3-X.md", {"id": "S3", "status": "pending"})
    git(box, "add", "-A")
    git(box, "commit", "-q", "-m", "prompts 2")
    return m02, own, s3


def test_audit_flags_a_commit_naming_another_prompt(crossed, box, capsys):
    m02, own, s3 = crossed
    run("audit")
    out = capsys.readouterr().out
    assert f'connections/R9: {m02[:9]} "docs(security): M02 audit report" names security/M02' in out
    assert f"{own[:9]} \"feat" not in out  # names its own id too
    assert f"{s3[:9]} \"fix" not in out    # S3 / A1 unqualified short ids are no prompt reference
    assert "security/M02: " + m02[:9] not in out  # its own commit
    for rel in ("docs/Prompts/connections/Connections-R9-X.md", "docs/Prompts/security/Security-M02-X.md"):
        assert git(box, "status", "--porcelain", "--", rel) == ""  # read-only


def test_a_qualified_short_id_is_flagged(box, capsys):
    sha = commit_file(box, "modules/y.py", "x", "fix(storage): S3 bucket names", when="2026-10-30T12:00:00")
    write_prompt(box, "agents/Agents-A5-X.md", {"id": "A5", "status": "on-main", "commit": f"\"{sha[:9]}\""})
    write_prompt(box, "storage/Storage-S3-X.md", {"id": "S3", "status": "pending"})
    run("audit")
    assert f'agents/A5: {sha[:9]} "fix(storage): S3 bucket names" names storage/S3' in capsys.readouterr().out


def test_cross_prompt_finding_is_a_function_of_the_index(crossed, box):
    cfg = json.loads(Path(__import__("os").environ["SESSION_TRACKER_CONFIG"]).read_text())
    view = cc.View(cfg, 0)
    found = cc.cross_prompt_findings(view, "security")
    assert found == []
