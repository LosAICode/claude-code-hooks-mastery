# ruff: noqa: F811  (the tests take fixtures imported by name)
"""CC11: commits in a second repo, the main-line guard, a quieter cross-prompt audit, `set --tests` text, the
restart-gate wording and the board gate's subagent wait, the landing window's own and no-unit landings."""
import json
from datetime import datetime

import pytest

import command_center as cc
import prompt_center as pc
import prompt_deploy as pd
from conftest import commit_file, git, use_tz
from test_command_center_cc9 import NOW_DT, box, checks_of, fakebin, iso  # noqa: F401  (box, fakebin: fixtures)
from test_prompt_join import session, write_sessions

UNIT_MAP = {"groups": {"web": ["app", "app-web@2"]},
            "rules": [{"paths": ["modules/static/**"], "live_at_merge": True},
                      {"paths": ["modules/**"], "exclude": ["modules/static/**"], "units": ["web"]}]}
NOW = "2026-10-30T16:00:00"


def write_prompt(r, rel, meta, body="# x\n"):
    p = r / "docs/Prompts" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in meta.items()) + "---\n" + body)


def meta_of(path):
    return pc.parse_frontmatter(path.read_text())[0]


def run(*args):
    return cc.main(["--now", NOW, *args])


@pytest.fixture
def two(tmp_path, monkeypatch):
    """The project repo plus a hooks repo: CC9 lists a project commit and two hooks commits; CC8 only hooks ones."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, "README.md", "x", "init", when="2026-10-30T08:00:00")
    commit_file(r, "scripts/deploy_units.json", json.dumps(UNIT_MAP), "map", when="2026-10-30T08:00:01")
    app = commit_file(r, "modules/app.py", "a", "feat(ops): CC9 app side", when="2026-10-30T09:00:00")
    h = tmp_path / "hooks"
    h.mkdir()
    git(h, "init", "-q", "-b", "main")
    commit_file(h, "README.md", "h", "init", when="2026-10-30T08:00:00")
    h1 = commit_file(h, "scripts/command_center.py", "1", "feat(command-center): CC9 one", when="2026-10-30T10:00:00")
    h2 = commit_file(h, ".claude/hooks/utils/x.py", "2", "feat(session-tracker): CC9 two", when="2026-10-30T11:00:00")
    git(h, "checkout", "-q", "-b", "side")
    off = commit_file(h, "scripts/side.py", "s", "side work", when="2026-10-30T11:30:00")
    git(h, "checkout", "-q", "main")
    write_prompt(r, "ops/Ops-CC9-X.md", {"module": "ops", "id": "CC9", "status": "on-main",
                                         "commits": f"[{app[:9]}, {h1[:7]}, {h2[:7]}]"})
    write_prompt(r, "ops/Ops-CC8-X.md", {"module": "ops", "id": "CC8", "status": "on-main", "commits": f"[{h1[:7]}]"})
    write_prompt(r, "ops/Ops-CC7-X.md", {"module": "ops", "id": "CC7", "status": "on-main",
                                         "commits": f"[{off[:7]}, deadbee]"})
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "prompts", env={"GIT_AUTHOR_DATE": "2026-10-30T12:00:00",
                                                  "GIT_COMMITTER_DATE": "2026-10-30T12:00:00"})
    monkeypatch.setattr(pd, "unit_started", {"app": 1, "app-web@2": 1}.get)
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    c = {"prompts": {"repo": str(r), "root": "docs/Prompts", "unit_map": "scripts/deploy_units.json",
                     "repos": [{"name": "hooks", "path": str(h), "live_at_merge": True}]}}
    (tmp_path / "config.json").write_text(json.dumps(c))
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    return r, h, {"app": app, "h1": h1, "h2": h2, "off": off}


def check_json(capsys):
    assert run("deploy-check", "--json") == 0
    return {e["id"]: e for e in json.loads(capsys.readouterr().out)["prompts"]}


# --- 1. commits in a second repo ------------------------------------------------------------------------

def test_a_hooks_commit_lands_in_its_own_repo_and_is_live_at_merge(two, capsys):
    _, _, s = two
    r = check_json(capsys)
    cc9 = r["CC9"]
    assert [c["sha"] for c in cc9["commits"]] == [s["app"][:9], "hooks:" + s["h1"][:9], "hooks:" + s["h2"][:9]]
    assert cc9["reason"] is None  # no "is not in this repo"
    assert cc9["needs"] == ["app", "app-web@2"]
    cc8 = r["CC8"]
    assert cc8["state"] == "live" and cc8["needs"] == [] and cc8["commits"][0]["repo"] == "hooks"
    cc7 = r["CC7"]  # a hooks commit not on its main, and a sha in no repo
    assert cc7["state"] == "unchecked"
    assert "hooks:" in cc7["reason"] and "not on main" in cc7["reason"]
    assert "deadbee is not in any repo" in cc7["reason"]


def test_without_the_repos_list_a_hooks_commit_is_not_in_this_repo(two, capsys, tmp_path):
    c = json.loads((tmp_path / "config.json").read_text())
    del c["prompts"]["repos"]
    (tmp_path / "config.json").write_text(json.dumps(c))
    r = check_json(capsys)
    assert r["CC8"]["state"] == "unchecked" and "is not in this repo" in r["CC8"]["reason"]


def test_deploy_plan_shows_hooks_commits_by_repo(two, capsys):
    _, _, s = two
    assert run("deploy-plan") in (0, 1)
    out = capsys.readouterr().out
    assert "hooks:" + s["h1"][:9] in out
    assert "not in this repo" not in out


def test_the_tracker_credits_a_hooks_repo_commit_to_the_claimed_prompt(two, tmp_path):
    import session_tracker as st

    r, h, _ = two
    cfg = json.loads((tmp_path / "config.json").read_text())
    rel = "docs/Prompts/ops/Ops-CC8-X.md"
    sha = commit_file(h, "scripts/more.py", "m", "feat(command-center): CC8 more")
    # a commit that touches only a "docs/Prompts/" path in the hooks repo is still work there
    tree = commit_file(h, "docs/Prompts/notes.md", "n", "docs: hooks notes")
    data = {"prompt_claimed": True, "prompt_file": rel}
    rows = [{"sha": sha, "subject": "feat(command-center): CC8 more", "repo": st.repo_id(str(h), st.run_git),
             "dir": str(h)},
            {"sha": tree, "subject": "docs: hooks notes", "repo": st.repo_id(str(h), st.run_git), "dir": str(h)}]
    cfg["prompts"]["commit_claims"] = False
    st._credit_prompt(data, rows, cfg, st.run_git)
    assert pc.commits_of(meta_of(r / rel))[-2:] == [sha[:9], tree[:9]]


# --- 3. quieter cross-prompt audit ----------------------------------------------------------------------

@pytest.fixture
def cross(tmp_path, monkeypatch):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, "README.md", "x", "init", when="2026-10-30T08:00:00")
    m02 = commit_file(r, "Documentation/m02.md", "m", "docs(security): M02 billing deep audit: no P0")
    a04 = commit_file(r, "modules/a.py", "a", "feat(assistants): one reply dispatcher (A04)")
    c1 = commit_file(r, "modules/c.py", "c", "fix(connections): C1 every Connect button works")
    a01 = commit_file(r, "tests/t.py", "t", "test(assistants): the refusal test anchors on A01's payer check")
    b20 = commit_file(r, "modules/b.py", "b", "fix(funnels): B20 order bumps")
    git(r, "checkout", "-q", "-b", "websites-b13")
    commit_file(r, "modules/w.py", "w", "feat(websites): page editor")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--no-ff", "websites-b13", "-m", "Merge websites-b13: page editor (B13)")
    merge = git(r, "rev-parse", "HEAD")
    prompts = {
        "Connections/Connections-R9-X.md": {"module": "connections", "id": "R9", "status": "on-main", "commits": f"[{m02[:9]}]"},
        "Security/Security-M02-X.md": {"module": "security", "id": "M02", "status": "done"},
        "Assistants/Assistants-A04b-X.md": {"module": "assistants", "id": "A04b", "status": "on-main", "commits": f"[{a04[:9]}]"},
        "Assistants/Assistants-A04-X.md": {"module": "assistants", "id": "A04", "status": "done"},
        "Connections/Connections-01-X.md": {"module": "connections", "id": '"01"', "status": "deployed", "commit": c1[:9]},
        "Connections/Connections-C1-X.md": {"module": "connections", "id": "C1", "status": "deployed", "commit": c1[:9]},
        "Assistants/Assistants-A03-X.md": {"module": "assistants", "id": "A03", "status": "on-main", "commits": f"[{a01[:9]}]"},
        "Assistants/Assistants-A01-X.md": {"module": "assistants", "id": "A01", "status": "done"},
        "Agents/Agents-A20-X.md": {"module": "agents", "id": "A20", "status": "on-main", "aliases": "[B20]",
                                   "commits": f"[{b20[:9]}]"},
        "Funnels/Funnels-B20-X.md": {"module": "funnels", "id": "B20", "status": "done"},
        "Websites/Websites-W2-X.md": {"module": "websites", "id": "W2", "status": "on-main", "commits": f"[{merge[:9]}]"},
        "Funnels/Funnels-B13-X.md": {"module": "funnels", "id": "B13", "status": "done"},
    }
    for rel, meta in prompts.items():
        write_prompt(r, rel, meta)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "prompts")
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    (tmp_path / "config.json").write_text(json.dumps({"prompts": {"repo": str(r), "root": "docs/Prompts"}}))
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    return r


def test_cross_prompt_audit_keeps_only_the_wrong_entry(cross):
    view = cc.View(cc.load_config(), datetime.fromisoformat(NOW).timestamp())
    found = cc.cross_prompt_findings(view)
    assert len(found) == 1, found
    assert found[0].startswith("connections/R9:") and "names security/M02" in found[0]


def test_without_the_alias_skip_the_shared_commit_is_a_hit(cross, monkeypatch):
    monkeypatch.setattr(cc, "aliases_of", lambda p, index: set())
    view = cc.View(cc.load_config(), datetime.fromisoformat(NOW).timestamp())
    found = cc.cross_prompt_findings(view)
    # (connections/01 → C1 is also skipped by the own-scope rule: same module, `fix(connections):`)
    assert any(f.startswith("agents/A20:") for f in found)


# --- 4. set --tests text ---------------------------------------------------------------------------------

def test_set_tests_takes_long_text_as_text(two, capsys):
    r, _, s = two
    text = "targeted: " + "x" * 400
    assert run("set", "ops/CC9", "on-main", "--tests", text, "--no-commit") == 0, capsys.readouterr()
    stored = meta_of(r / "docs/Prompts/ops/Ops-CC9-X.md")["tests"]
    assert stored.startswith(text[:300]) and text[:301] not in stored
    assert stored.endswith(" · " + s["h2"][:7])
    # text with slashes is still text (live 11-01: "23/23 files … TZ=America/New_York" was refused as a missing file)
    assert run("set", "ops/CC9", "on-main", "--tests", "23/23 files pass under TZ=America/New_York", "--no-commit") == 0
    assert run("set", "ops/CC9", "on-main", "--tests", "/no/such/results.txt", "--no-commit") == 2


@pytest.fixture(autouse=True)
def _no_session_env(monkeypatch):
    """This session's own id would be skipped as the deploy-ready caller."""
    for k in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(k, raising=False)


# --- 2. main-line guard ----------------------------------------------------------------------------------

@pytest.fixture
def ffd(tmp_path, monkeypatch):
    """A branch merges main, then main is fast-forwarded to it (live 11-01 00:48, A03)."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, "README.md", "x", "init", when="2026-10-30T08:00:00")
    commit_file(r, "scripts/deploy_units.json", json.dumps({**UNIT_MAP, "deploy": {"ready_checks": {
        "min_available_mb": 0, "merge_state": False, "dirty_units": False, "migration_chain": False,
        "landing_window_min": 0}}}), "map", when="2026-10-30T08:00:01")
    git(r, "checkout", "-q", "-b", "a03")
    commit_file(r, "modules/a.py", "a", "feat(assistants): A03 widget", when="2026-10-30T09:00:00")
    git(r, "checkout", "-q", "main")
    commit_file(r, "modules/b.py", "b", "fix(x): main work", when="2026-10-30T09:30:00")
    git(r, "checkout", "-q", "a03")
    env = {"GIT_AUTHOR_DATE": "2026-10-30T10:00:00", "GIT_COMMITTER_DATE": "2026-10-30T10:00:00"}
    git(r, "merge", "-q", "--no-ff", "main", "-m", "Merge branch 'main' into a03", env=env)
    bad = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--ff-only", "a03")
    write_prompt(r, "assistants/Assistants-A03-X.md", {"module": "assistants", "id": "A03", "status": "on-main",
                                                     "commits": f"[{bad[:9]}]"})
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "docs(prompts): assistants A03 → on-main",
        env={"GIT_AUTHOR_DATE": "2026-10-30T10:01:00", "GIT_COMMITTER_DATE": "2026-10-30T10:01:00"})
    monkeypatch.setattr(pd, "unit_started", {"app": 1, "app-web@2": 1}.get)
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    (tmp_path / "config.json").write_text(json.dumps({"prompts": {"repo": str(r), "root": "docs/Prompts",
                                                                  "unit_map": "scripts/deploy_units.json"}}))
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    return r, bad


def test_audit_names_main_fast_forwarded_to_a_branch_that_merged_main(ffd, capsys):
    _, bad = ffd
    run("audit")
    out = capsys.readouterr().out
    assert f"main line: {bad[:9]}" in out and "--no-ff" in out


def test_a_custom_merge_message_is_caught_by_the_reflog(ffd, capsys):
    import prompt_mainline as ml

    r, bad = ffd
    git(r, "checkout", "-q", "-b", "b2", "HEAD~1")
    commit_file(r, "modules/c.py", "c", "feat: c")
    git(r, "checkout", "-q", "main")
    commit_file(r, "modules/d.py", "d", "fix: d")
    git(r, "checkout", "-q", "b2")
    git(r, "merge", "-q", "--no-ff", "main", "-m", "sync up")
    sync = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--ff-only", "b2")
    shas = [f["sha"] for f in ml.findings(str(r))]
    assert sync in shas and bad in shas


def test_merging_origin_main_into_main_keeps_the_line(ffd):
    import prompt_mainline as ml

    r, bad = ffd
    git(r, "checkout", "-q", "-b", "origin-main", "HEAD~1")
    commit_file(r, "Documentation/owner.md", "o", "docs: owner's GitHub edit")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--no-ff", "origin-main", "-m", "Merge remote-tracking branch 'origin/main' into HEAD")
    assert [f["sha"] for f in ml.findings(str(r))] == [bad]


def test_deploy_ready_waits_while_the_fast_forward_is_the_newest_landing(ffd, capsys):
    r, bad = ffd
    assert run("deploy-ready", "--json") == 1
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert not checks["main line"]["ok"] and bad[:9] in checks["main line"]["detail"]
    git(r, "checkout", "-q", "-b", "next")
    commit_file(r, "modules/e.py", "e", "feat: e", when="2026-10-30T11:00:00")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--no-ff", "next", "-m", "Merge next",
        env={"GIT_AUTHOR_DATE": "2026-10-30T11:05:00", "GIT_COMMITTER_DATE": "2026-10-30T11:05:00"})
    run("deploy-ready", "--json")
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["main line"]["ok"]


# --- 6. gate message + board gate ------------------------------------------------------------------------

def test_the_restart_gate_says_request_first_then_sendmessage():
    import session_tracker as st

    msg = st.RESTART_BLOCK_REASON
    assert "command_center.py request <module>/<ID>" in msg and "SendMessage" in msg
    assert msg.index("command_center.py request") < msg.index("SendMessage")


def test_the_board_gate_waits_while_subagents_run(repo):
    from datetime import datetime, timedelta

    import session_tracker as st

    sha = commit_file(repo, "modules/a.py", "x", "code")
    cfg = {"board_gate": {"repo": str(repo), "branch": "main", "board": "docs/Board.md", "code_paths": ["modules/"]}}
    started = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat(timespec="seconds")
    s = {"session_id": "g", "started": started, "deploys": [], "commits": [{"sha": sha, "repo": st.repo_id(str(repo))}],
         "open_agents": {"a1": st.now_iso()}}
    stop = {"session_id": "g", "stop_hook_active": False}
    assert st.gate_reason(stop, s, cfg) is None
    s["open_agents"] = {}
    assert st.gate_reason(stop, s, cfg) is not None


# --- 6b. landing window: the requester's own landings and no-unit commits --------------------------------



def test_a_test_only_landing_does_not_wait(box, capsys, tmp_path):
    other = "dddd4444-0000-0000-0000-000000000004"
    t = commit_file(box, "tests/test_widget.py", "t", "test(assistants): anchors on A01's payer check")
    rows = [{"sha": t, "subject": "test(assistants): x", "repo": "r", "dir": str(box), "ts": iso(2)}]
    write_sessions(tmp_path / "tracker", [session(other, seen=1, commits=rows)])
    code, checks, _ = checks_of(capsys)
    assert checks["no session landing"]["ok"], checks["no session landing"]
    code_sha = commit_file(box, "modules/services/w.py", "w", "fix(assistants): w")
    rows.append({"sha": code_sha, "subject": "fix(assistants): w", "repo": "r", "dir": str(box), "ts": iso(1)})
    write_sessions(tmp_path / "tracker", [session(other, seen=1, commits=rows)])
    code, checks, _ = checks_of(capsys)
    assert not checks["no session landing"]["ok"] and code_sha[:9] in checks["no session landing"]["detail"]


def test_answering_a_request_skips_the_requesters_own_landing(box, capsys, tmp_path):
    import prompt_requests as pq

    asker = "eeee5555-0000-0000-0000-000000000005"
    code_sha = commit_file(box, "modules/services/w.py", "w", "fix(assistants): w")
    rows = [{"sha": code_sha, "subject": "fix(assistants): w", "repo": "r", "dir": str(box), "ts": iso(2)}]
    write_sessions(tmp_path / "tracker", [session(asker, seen=1, commits=rows)])
    code, checks, _ = checks_of(capsys)
    assert not checks["no session landing"]["ok"]
    req = pq.add("agents/A1", None, asker, NOW_DT.timestamp())
    code, checks, _ = checks_of(capsys, "--answer", req["id"])
    assert checks["no session landing"]["ok"], checks["no session landing"]


# --- 5. clock-proof deploy-run ---------------------------------------------------------------------------

@pytest.mark.parametrize("moment", ["2026-11-01T01:30:00", "2026-03-08T02:30:00"])  # the repeated hour; the gap
def test_deploy_run_proves_its_units_in_new_york_across_dst(box, capsys, tmp_path, monkeypatch, moment):
    import prompt_run as prun

    use_tz(monkeypatch, "America/New_York")
    at = int(datetime.fromisoformat(moment).replace(fold=1).timestamp())
    monkeypatch.setattr(prun, "clock", lambda: at)
    monkeypatch.setenv("FAKE_NOW", str(at))  # the fake units start and log "ready" on the same clock
    assert cc.main(["--now", moment, "deploy-run", "--owner-ok", "owner: ship", "--no-commit"]) == 0, capsys.readouterr()
    rec = json.loads(next((tmp_path / "tracker" / "deploys").glob("*.json")).read_text())
    assert rec["result"] == "ok" and rec["started"] == at
    assert all(p["ok"] for st in rec["steps"] for p in st["proofs"])
