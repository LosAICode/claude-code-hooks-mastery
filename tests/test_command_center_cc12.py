# ruff: noqa: F811  (the tests take fixtures imported by name)
"""CC12: landing times from main's reflog, one migration row per file with prod first, the verify line in
deploy-ready."""
import json

import pytest

import command_center as cc
import prompt_deploy as pd
import session_tracker as st
from conftest import commit_file, git
from test_command_center_cc9 import box, checks_of, fakebin, ts  # noqa: F401  (box, fakebin: fixtures)
from test_command_center_cc11 import write_prompt

NOW = "2026-10-30T16:00:00"
MIG = "modules/migrations/versions"
UNIT_MAP = {"groups": {"web": ["app", "app-web@2"]},
            "rules": [{"paths": ["modules/**"], "units": ["web"]}],
            "deploy": {"migrations": MIG + "/"}}


def run(*args):
    return cc.main(["--now", NOW, *args])


def set_prod(tmp_path, cmd):
    path = tmp_path / "config.json"
    c = json.loads(path.read_text())
    c["prompts"]["prod_revision"] = cmd
    path.write_text(json.dumps(c))


@pytest.fixture
def ffwd(tmp_path, monkeypatch):
    """R3's case: a branch adds a migration (09:00) and fixes it (13:00), merges main (13:30), and main is
    fast-forwarded to it at 14:00. The units started at 12:00: after the first commit's date, before it landed."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, "README.md", "x", "init", when="2026-10-30T08:00:00")
    commit_file(r, "scripts/deploy_units.json", json.dumps(UNIT_MAP), "map", when="2026-10-30T08:00:01")
    commit_file(r, f"{MIG}/r1.py", "revision = 'r1'\ndown_revision = None\n", "r1", when="2026-10-30T08:30:00")
    git(r, "checkout", "-q", "-b", "feat")
    add = commit_file(r, f"{MIG}/r2.py", "revision = 'r2'\ndown_revision = 'r1'\n", "feat(x): r2",
                      when="2026-10-30T09:00:00")
    fix = commit_file(r, f"{MIG}/r2.py", "revision = 'r2'\ndown_revision = 'r1'\n# fix\n", "fix(x): r2",
                      when="2026-10-30T13:00:00")
    git(r, "checkout", "-q", "main")
    commit_file(r, "docs/other.md", "o", "docs: other", when="2026-10-30T10:00:00")
    git(r, "checkout", "-q", "feat")
    at = {"GIT_AUTHOR_DATE": "2026-10-30T13:30:00", "GIT_COMMITTER_DATE": "2026-10-30T13:30:00"}
    git(r, "merge", "-q", "--no-edit", "main", env=at)
    git(r, "checkout", "-q", "main")
    git(r, "merge", "-q", "--ff-only", "feat", env={"GIT_COMMITTER_DATE": "2026-10-30T14:00:00"})
    write_prompt(r, "connections/Connections-R3-X.md", {"module": "connections", "id": "R3", "status": "on-main",
                                                        "commits": f"[{add[:9]}, {fix[:9]}]"})
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "prompts", env={"GIT_AUTHOR_DATE": "2026-10-30T14:10:00",
                                                  "GIT_COMMITTER_DATE": "2026-10-30T14:10:00"})
    monkeypatch.setattr(pd, "unit_started", {"app": ts("2026-10-30T12:00:00"),
                                             "app-web@2": ts("2026-10-30T12:00:00")}.get)
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    c = {"prompts": {"repo": str(r), "root": "docs/Prompts", "unit_map": "scripts/deploy_units.json"}}
    (tmp_path / "config.json").write_text(json.dumps(c))
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    return r, {"add": add, "fix": fix}


def plan_json(capsys):
    assert run("deploy-plan", "--json") == 0
    return json.loads(capsys.readouterr().out)


# --- 1. landing times from main's reflog -------------------------------------------------------------------

def test_a_fast_forwarded_commit_lands_when_main_got_it(ffwd):
    r, sha = ffwd
    h = pd.History(str(r))
    got, why = h.landing(sha["add"])
    assert why is None and got["landed"] == ts("2026-10-30T14:00:00")  # not its 09:00 committer date
    first, _ = h.landing(git(r, "rev-list", "--max-parents=0", "main"))
    assert first["landed"] == ts("2026-10-30T08:00:00")  # a commit made on main lands at its own time


def test_without_a_reflog_the_committer_date_stands(ffwd):
    r, sha = ffwd
    git(r, "reflog", "expire", "--expire=all", "--all")
    got, _ = pd.History(str(r)).landing(sha["add"])
    assert got["landed"] == ts("2026-10-30T09:00:00")


def test_units_started_before_the_fast_forward_still_wait(ffwd, monkeypatch, capsys):
    monkeypatch.setattr(pd, "unit_started", {"app": ts("2026-10-30T13:15:00"),
                                             "app-web@2": ts("2026-10-30T13:15:00")}.get)  # after both dates
    assert run("deploy-check", "--json") == 0
    e = {x["id"]: x for x in json.loads(capsys.readouterr().out)["prompts"]}["R3"]
    assert e["state"] == "waiting" and set(e["waits"]) == {"app", "app-web@2"}


# --- 2. one migration row per file, prod first ---------------------------------------------------------------

def test_one_row_per_migration_and_no_boot_guess_without_prod(ffwd, capsys):
    rows = plan_json(capsys)["migrations"]
    assert [(m["file"], m["applied_at_boot"]) for m in rows] == [(f"{MIG}/r2.py", False)]


def test_prod_below_the_migration_means_to_apply_whatever_the_unit_times(ffwd, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pd, "unit_started", {"app": ts("2026-10-30T15:00:00"),
                                             "app-web@2": ts("2026-10-30T15:00:00")}.get)  # after the landing
    set_prod(tmp_path, "echo r1")
    rows = plan_json(capsys)["migrations"]
    assert [(m["file"], m["applied_at_boot"], m["in_prod"]) for m in rows] == [(f"{MIG}/r2.py", False, False)]
    assert run("deploy-plan") == 0
    text = capsys.readouterr().out.split("Migrations (apply BY HAND", 1)[1].split("\n\n", 1)[0]
    assert f"{MIG}/r2.py  (connections R3" in text and "ran at boot" not in text


def test_a_migration_prod_holds_is_listed_once(ffwd, tmp_path, capsys):
    set_prod(tmp_path, "echo r2")
    assert run("deploy-plan") == 0
    text = capsys.readouterr().out.split("Migrations (apply BY HAND", 1)[1].split("\n\n", 1)[0]
    assert text.count(f"{MIG}/r2.py") == 1 and "already in prod (alembic_version r2)" in text


# --- 3. the verify line in deploy-ready -----------------------------------------------------------------------

def gate_on(tmp_path):
    path = tmp_path / "config.json"
    c = json.loads(path.read_text())
    c["board_gate"] = {"repo": c["prompts"]["repo"], "branch": "main", "code_paths": ["modules/"]}
    path.write_text(json.dumps(c))
    return c["prompts"]["repo"]


def test_deploy_ready_waits_until_a_verify_covers_main(box, tmp_path, capsys):
    repo = gate_on(tmp_path)
    code, checks, _ = checks_of(capsys)
    assert code == 1 and checks["verify"]["ok"] is False and "no verify recorded yet" in checks["verify"]["detail"]
    st.record_verify(git(repo, "rev-parse", "main"), "verify: PASS")
    code, checks, _ = checks_of(capsys)
    assert code == 0 and checks["verify"]["ok"] is True


def test_no_board_gate_no_verify_line(box, capsys):
    _, checks, _ = checks_of(capsys)
    assert "verify" not in checks
