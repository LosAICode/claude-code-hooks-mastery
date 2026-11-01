"""deploy-check: the units each on-main/deployed prompt's commit needs, and which restarted after it landed."""
import json
from datetime import datetime

import pytest

import command_center as cc
import prompt_center as pc
import prompt_deploy as pd
from conftest import commit_file, git

UNIT_MAP = {
    "groups": {"web": ["app", "app-web@2"]},
    "rules": [
        {"paths": ["modules/static/**"], "live_at_merge": True},
        {"paths": ["modules/templates/**"], "units": ["web"]},
        {"paths": ["modules/**"], "exclude": ["modules/static/**", "modules/templates/**"], "units": ["web", "app-celery"]},
        {"paths": ["modules/services/agent_runtime*", "modules/channel_listeners_server.py"], "units": ["app-channels"]},
        {"paths": ["modules/services/magnus_*", "modules/services/magnus_*/**"], "units": ["app-celery-magnus"]},
    ],
}


def ts(text):
    return int(datetime.fromisoformat(text).timestamp())


@pytest.fixture
def deploy(tmp_path, monkeypatch):
    """main with: a branch commit authored 10:00 but merged 12:00, a static change, a docs-only change, a magnus fix."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, "README.md", "x", "init", when="2026-10-30T08:00:00")
    commit_file(r, "scripts/deploy_units.json", json.dumps(UNIT_MAP), "map", when="2026-10-30T08:00:01")
    git(r, "checkout", "-q", "-b", "feat")
    branch_sha = commit_file(r, "modules/services/agent_runtime.py", "a", "feat: agents", when="2026-10-30T10:00:00")
    commit_file(r, "modules/app.py", "a", "feat: app", when="2026-10-30T10:05:00")
    git(r, "checkout", "-q", "main")
    commit_file(r, "modules/static/js/x.js", "js", "js", when="2026-10-30T11:00:00")
    git(r, "merge", "-q", "--no-ff", "feat", "-m", "Merge feat", env={"GIT_AUTHOR_DATE": "2026-10-30T12:00:00",
                                                                       "GIT_COMMITTER_DATE": "2026-10-30T12:00:00"})
    merge_sha = git(r, "rev-parse", "HEAD")
    static_sha = git(r, "rev-parse", "HEAD^1")
    docs_sha = commit_file(r, "Documentation/x.md", "d", "docs", when="2026-10-30T12:30:00")
    magnus_sha = commit_file(r, "modules/services/magnus_skills/s.md", "m", "magnus", when="2026-10-30T13:00:00")
    tpl_sha = commit_file(r, "modules/templates/a.html", "t", "tpl", when="2026-10-30T13:30:00")
    prompts = {
        "A05": ("on-main", branch_sha[:9]),        # needs web + celery + channels; landed 12:00 (not 10:00)
        "A06": ("on-main", merge_sha[:9]),         # same change, named by its merge
        "A07": ("on-main", static_sha[:9]),        # static only: live at merge
        "A08": ("on-main", docs_sha[:9]),          # no unit
        "A09": ("deployed", magnus_sha[:9]),       # code too (web + celery live), waits on celery-magnus
        "A10": ("on-main", tpl_sha[:9]),           # web only: live
        "A11": ("on-main", ""),                    # no commit
        "A12": ("on-main", "deadbeef1"),           # not in this repo
        "A13": ("pending", tpl_sha[:9]),           # not checked
    }
    for ident, (status, sha) in prompts.items():
        commit_line = f"commit: \"{sha}\"\n" if sha else ""
        (r / "docs/Prompts/agents").mkdir(parents=True, exist_ok=True)
        (r / f"docs/Prompts/agents/Agents-{ident}-X.md").write_text(f"---\nstatus: {status}\n{commit_line}---\n# {ident}: x\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "prompts", env={"GIT_AUTHOR_DATE": "2026-10-30T14:00:00", "GIT_COMMITTER_DATE": "2026-10-30T14:00:00"})
    starts = {"app": ts("2026-10-30T15:00:00"), "app-web@2": ts("2026-10-30T15:01:00"),
              "app-celery": ts("2026-10-30T15:02:00"), "app-channels": ts("2026-10-29T09:00:00"),
              "app-celery-magnus": None}
    monkeypatch.setattr(pd, "unit_started", starts.get)
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    c = {"prompts": {"repo": str(r), "root": "docs/Prompts", "unit_map": "scripts/deploy_units.json"}}
    (tmp_path / "config.json").write_text(json.dumps(c))
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    return r


def run(*args):
    return cc.main(["--now", "2026-10-30T16:00:00", *args])


def results(capsys):
    assert run("deploy-check", "--json") == 0
    return {e["id"]: e for e in json.loads(capsys.readouterr().out)["prompts"]}


def test_needs_landing_time_and_states(deploy, capsys):
    r = results(capsys)
    assert "A13" not in r
    a05 = r["A05"]
    assert a05["needs"] == ["app", "app-web@2", "app-celery", "app-channels"]
    assert a05["landed"] == ts("2026-10-30T12:00:00")  # the merge on main, not the 10:00 author time
    assert a05["state"] == "half" and a05["waits"] == ["app-channels"] and a05["live"] == ["app", "app-web@2", "app-celery"]
    assert a05["half_since"] == ts("2026-10-30T15:00:00")
    assert {k: r["A06"][k] for k in ("needs", "landed", "state")} == {k: a05[k] for k in ("needs", "landed", "state")}
    assert r["A07"]["state"] == "live" and r["A07"]["needs"] == [] and r["A07"]["live_since"] == ts("2026-10-30T11:00:00")
    assert r["A08"]["state"] == "no-unit"
    assert r["A09"]["state"] == "half" and r["A09"]["waits"] == ["app-celery-magnus"]
    assert r["A10"]["state"] == "live" and r["A10"]["needs"] == ["app", "app-web@2"]
    assert r["A10"]["live_since"] == ts("2026-10-30T15:01:00")  # the last needed unit's start
    assert r["A11"]["state"] == "unchecked" and "no commit" in r["A11"]["reason"]
    assert r["A12"]["state"] == "unchecked" and "deadbeef1" in r["A12"]["reason"]


def test_text_report_groups_units(deploy, capsys):
    assert run("deploy-check") == 0
    out = capsys.readouterr().out
    assert "Half live (3)" in out and "agents A05" in out and "waits: app-channels (started 10-29 09:00)" in out
    assert "live: web, app-celery" in out
    assert "app-celery-magnus (not running)" in out


def test_apply_sets_deployed_only_when_every_unit_restarted_and_never_backwards(deploy, capsys):
    assert run("deploy-check", "--apply") == 0
    out = capsys.readouterr().out

    def meta(ident):
        return pc.parse_frontmatter((deploy / f"docs/Prompts/agents/Agents-{ident}-X.md").read_text())[0]

    assert meta("A10")["status"] == "deployed"
    assert datetime.fromisoformat(meta("A10")["deployed"]).timestamp() == ts("2026-10-30T15:01:00")
    assert meta("A07")["status"] == "deployed"
    for ident in ("A05", "A06", "A08", "A11", "A12"):
        assert meta(ident)["status"] == "on-main", ident
    assert meta("A09")["status"] == "deployed" and "deployed" not in meta("A09")  # untouched
    assert git(deploy, "log", "-1", "--format=%s") == "docs(prompts): deploy-check: 2 prompts deployed (agents A07, A10)"
    assert "committed" in out


def test_without_a_unit_map_it_says_so(deploy, capsys, tmp_path):
    c = json.loads((tmp_path / "config.json").read_text())
    del c["prompts"]["unit_map"]
    (tmp_path / "config.json").write_text(json.dumps(c))
    assert run("deploy-check") == 2
    assert "unit_map" in capsys.readouterr().err


@pytest.mark.parametrize("pattern,path,hit", [
    ("modules/services/magnus_*", "modules/services/magnus_x.py", True),
    ("modules/services/magnus_*", "modules/services/magnus_skills/a.md", False),  # git: a folder match is no file match
    ("modules/services/magnus_*/**", "modules/services/magnus_skills/sub/a.md", True),
    ("modules/**", "modules/app.py", True),
    ("modules/**", "top.py", False),
    ("modules/*.py", "modules/sub/a.py", False),
    ("**/a.md", "modules/x/a.md", True),
    ("**/a.md", "a.md", True),
    ("modules/tasks/**", "modules/tasks/t.py", True),
    ("modules/celery_config.py", "modules/celery_config.py", True),
    ("modules/?.py", "modules/a.py", True),
])
def test_glob_matches_like_git(pattern, path, hit):
    assert bool(pd.glob_regex(pattern).fullmatch(path)) is hit


def test_glob_agrees_with_git_ls_files(tmp_path):
    r = tmp_path / "g"
    r.mkdir()
    git(r, "init", "-q")
    files = ["modules/services/magnus_skills/a.md", "modules/services/magnus_skills/sub/b.md", "modules/services/magnus_x.py",
             "modules/services/xmagnus_y.py", "modules/static/js/a.js", "modules/tasks/t.py", "modules/app.py", "top.py"]
    for f in files:
        commit_file(r, f, "x", f)
    for pattern in ("modules/services/magnus_*", "modules/**", "modules/*", "modules/*.py", "**/a.md", "modules/s*/**"):
        expect = set(git(r, "ls-files", "--", f":(glob){pattern}").splitlines())
        assert {f for f in files if pd.glob_regex(pattern).fullmatch(f)} == expect, pattern


def test_unreadable_files_are_unchecked_not_no_unit(deploy, monkeypatch, capsys):
    real = pd.History._git

    def broken(self, *args):
        return None if args[0] in ("diff", "show") else real(self, *args)

    monkeypatch.setattr(pd.History, "_git", broken)
    r = results(capsys)
    assert r["A10"]["state"] == "unchecked" and "files" in r["A10"]["reason"]


def test_apply_leaves_a_prompt_another_session_moved_since_the_scan(deploy, monkeypatch, capsys):
    real = pd.check
    a10 = deploy / "docs/Prompts/agents/Agents-A10-X.md"

    def check_then_someone_sets_done(*args):
        out = real(*args)
        a10.write_text(a10.read_text().replace("status: on-main", "status: done"))
        return out

    monkeypatch.setattr(pd, "check", check_then_someone_sets_done)
    assert run("deploy-check", "--apply") == 0
    assert "status: done" in a10.read_text() and "deployed:" not in a10.read_text()
    assert "agents A10: left alone" in capsys.readouterr().out


def test_a_main_commit_after_main_was_merged_into_a_branch_needs_only_its_own_files(deploy, capsys):
    """Live 11-01 00:48: A03 merged main into a03_widget and main fast-forwarded to it, so CC9's map-only commit
    left the first-parent line and was charged every file the branch changed since its fork (web, celery, ...)."""
    r = deploy
    git(r, "checkout", "-q", "-b", "side", "HEAD~1")
    commit_file(r, "modules/side.py", "s", "side: code", when="2026-10-30T14:10:00")
    git(r, "checkout", "-q", "main")
    map_sha = commit_file(r, "scripts/other.json", "{}", "map only", when="2026-10-30T14:20:00")
    (r / "docs/Prompts/agents/Agents-A14-X.md").write_text(f"---\nstatus: on-main\ncommit: \"{map_sha[:9]}\"\n---\n# A14: x\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "prompt A14", env={"GIT_AUTHOR_DATE": "2026-10-30T14:21:00", "GIT_COMMITTER_DATE": "2026-10-30T14:21:00"})
    git(r, "checkout", "-q", "side")
    git(r, "merge", "-q", "--no-ff", "main", "-m", "Merge branch 'main' into side")
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--ff-only", "side")
    a14 = results(capsys)["A14"]
    assert a14["commits"][0]["files"] == ["scripts/other.json"]
    assert a14["state"] == "no-unit" and a14["needs"] == []
