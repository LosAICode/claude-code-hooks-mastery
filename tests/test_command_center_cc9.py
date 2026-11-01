"""CC9: the command center coordinates deploys: deploy-ready, rebased commits, deploy-run, Tell lines, the /cc line.

Nothing real restarts: `systemctl`, `curl` and `psql` are fake scripts first on PATH (asserted per test), the web
restart is a fake script in the fixture map, and the deploy lock is a tmp file. /tmp is mounted noexec here, so the
fakes live in a per-test dir under /dev/shm (removed after)."""
# ruff: noqa: F811  (the tests take fixtures imported by name)
import fcntl
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import command_center as cc
import prompt_center as pc
import prompt_deploy as pd
import prompt_ready as pr
from conftest import commit_file, git
from test_prompt_join import session, write_sessions

NOW = "2026-10-30T16:00:00"
NOW_DT = datetime.fromisoformat(NOW).astimezone()
LIVE_SID = "aaaa1111-0000-0000-0000-000000000001"

FAKE_SYSTEMCTL = """#!/bin/sh
S="$FAKE_STATE"
for unit; do :; done
case "$1" in
  restart) T="${FAKE_NOW:-$(date +%s)}"; echo "$T" > "$S/start-$unit"; echo "$unit" >> "$S/restarted"
           echo "[$(date -d "@$T" '+%Y-%m-%d %H:%M:%S'),000: INFO/MainProcess] w@h ready." >> "$S/celery.log" ;;
  show) t=$(cat "$S/start-$unit" 2>/dev/null)
        if [ -n "$t" ]; then echo ActiveState=active; echo "ActiveEnterTimestamp=@$t"; else echo ActiveState=inactive; fi ;;
  *) exit 1 ;;
esac
"""
FAKE_CURL = """#!/bin/sh
printf '%s ' "$(cat "$FAKE_STATE/health" 2>/dev/null || echo 200)"
"""
FAKE_PSQL = """#!/bin/sh
cat "$FAKE_STATE/builds" 2>/dev/null || echo 0
"""
FAKE_RESTART_WEB = """#!/bin/sh
systemctl restart app && systemctl restart app-web@2
"""


def ts(text):
    return int(datetime.fromisoformat(text).timestamp())


def iso(minutes_ago):
    return (NOW_DT - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def write_prompt(r, rel, meta, body="# x\n"):
    p = r / "docs/Prompts" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in meta.items()) + "---\n" + body)


def unit_map(tmp_path):
    fake = Path((tmp_path / "fakebin").read_text())
    return {
        "groups": {"web": ["app", "app-web@2"]},
        "rules": [
            {"paths": ["modules/static/**"], "live_at_merge": True},
            {"paths": ["modules/templates/**"], "units": ["web"]},
            {"paths": ["modules/**"], "exclude": ["modules/static/**", "modules/templates/**"], "units": ["web", "app-celery"]},
            {"paths": ["modules/voice_server.py"], "units": ["app-voice"]},
        ],
        "deploy": {
            "migrations": "modules/migrations/versions/",
            "migrate": "scripts/vault_exec.sh python apply_migrations.py",
            "order": ["web", "app-celery", "app-voice"],
            "restart": {"web": f"flock {tmp_path / 'deploy.lock'} {fake / 'restart_web.sh'}", "*": "systemctl restart {unit}"},
            "guards": [
                {"name": "no build running", "command": "psql -tAc 'SELECT count(*) FROM builds'", "expect": "0"},
                {"name": "0 live calls", "command": f"cat {tmp_path / 'state' / 'calls'}", "expect": "0",
                 "before": ["app-voice"]},
            ],
            "proof": ["systemctl show -p ActiveEnterTimestamp {unit}"],
            "ready": {"app-celery": f"grep -a ' ready\\.' {tmp_path / 'state' / 'celery.log'} | tail -1"},
            "after_each": ["for i in 1 2 3; do curl -s -o /dev/null -w '%{http_code} ' http://127.0.0.1:5025/api/health; done; echo"],
            "lock": str(tmp_path / "deploy.lock"),
            "freeze_file": str(tmp_path / "freeze"),
            "proof_wait_s": 1,
            "ready_checks": {"min_available_mb": 1500, "locks": [str(tmp_path / "sandbox.lock")], "landing_window_min": 10},
        },
    }


@pytest.fixture
def fakebin(tmp_path):
    d = Path(tempfile.mkdtemp(prefix="cc9-fake-", dir="/dev/shm"))
    (tmp_path / "fakebin").write_text(str(d))  # where unit_map() finds it
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def box(tmp_path, monkeypatch, fakebin):
    """A repo with two on-main prompts (web; web + app-celery), fake systemctl/curl/psql first on PATH."""
    fake, state = fakebin, tmp_path / "state"
    state.mkdir()
    for name, text in (("systemctl", FAKE_SYSTEMCTL), ("curl", FAKE_CURL), ("psql", FAKE_PSQL),
                       ("restart_web.sh", FAKE_RESTART_WEB)):
        (fake / name).write_text(text)
        (fake / name).chmod(0o755)
    (state / "calls").write_text("0\n")
    for u in ("app", "app-web@2", "app-celery", "app-voice"):
        (state / f"start-{u}").write_text(str(ts("2026-10-30T09:00:00")))
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_STATE", str(state))
    mem = tmp_path / "meminfo"
    mem.write_text("MemTotal:       16000000 kB\nMemAvailable:    4000000 kB\n")
    monkeypatch.setattr(pr, "MEMINFO", str(mem))
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, "README.md", "x", "init", when="2026-10-30T08:00:00")
    commit_file(r, "scripts/deploy_units.json", json.dumps(unit_map(tmp_path)), "map", when="2026-10-30T08:00:01")
    tpl = commit_file(r, "modules/templates/a.html", "t", "tpl", when="2026-10-30T10:00:00")
    svc = commit_file(r, "modules/services/x.py", "s", "svc", when="2026-10-30T10:30:00")
    write_prompt(r, "agents/Agents-A1-X.md", {"id": "A1", "status": "on-main", "commit": f"\"{tpl[:9]}\"",
                                              "session_id": LIVE_SID, "proof": "\"probe 320 sees 200\""})
    write_prompt(r, "agents/Agents-A2-X.md", {"id": "A2", "status": "on-main", "commit": f"\"{svc[:9]}\"",
                                              "tests": "\"none: fixture\""})
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "prompts", env={"GIT_AUTHOR_DATE": "2026-10-30T11:00:00", "GIT_COMMITTER_DATE": "2026-10-30T11:00:00"})
    tracker = tmp_path / "tracker"
    write_sessions(tracker, [session(LIVE_SID, name="a04b-lander", seen=5)])
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    c = {"prompts": {"repo": str(r), "root": "docs/Prompts", "unit_map": "scripts/deploy_units.json"}}
    (tmp_path / "config.json").write_text(json.dumps(c))
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert shutil.which("systemctl") == str(fake / "systemctl")
    assert os.environ["PATH"].split(os.pathsep)[0] == str(fake)
    return r


def fake_of(tmp_path):
    return Path((tmp_path / "fakebin").read_text())


def run(*args):
    return cc.main(["--now", NOW, *args])


def restarted(tmp_path):
    f = tmp_path / "state" / "restarted"
    return f.read_text().split() if f.exists() else []


def set_map(r, tmp_path, edit):
    umap = unit_map(tmp_path)
    edit(umap)
    (r / "scripts/deploy_units.json").write_text(json.dumps(umap))
    git(r, "commit", "-q", "-am", "map edit")


def checks_of(capsys, *args):
    code = run("deploy-ready", "--json", *args)
    data = json.loads(capsys.readouterr().out)
    return code, {c["name"]: c for c in data["checks"]}, data


# --- 1. deploy-ready -------------------------------------------------------------------------------------------

def test_deploy_ready_ok_lists_what_would_ship(box, capsys):
    assert run("deploy-ready") == 0
    out = capsys.readouterr().out
    assert "OK  no build running" in out and "Deploy: OK" in out
    assert "0 live calls" not in out  # its unit does not ship
    assert out.index("Deploy: OK") < out.index("Prompts") < out.index("Migrations") < out.index("Not proven tested")
    assert "agents A1" in out and "agents A2" in out
    code, checks, data = checks_of(capsys)
    assert code == 0 and data["verdict"] == "OK" and all(c["ok"] for c in checks.values())
    assert {"memory", "locks", "merge state", "uncommitted changes", "migration chain", "no session landing"} <= set(checks)
    assert restarted(box.parent) == []


def test_deploy_ready_waits_on_a_failing_guard(box, capsys, tmp_path):
    (tmp_path / "state" / "builds").write_text("2\n")
    code, checks, data = checks_of(capsys)
    assert code == 1 and data["verdict"] == "WAIT"
    assert not checks["no build running"]["ok"] and "2" in checks["no build running"]["detail"]
    assert run("deploy-ready") == 1
    out = capsys.readouterr().out
    assert "WAIT: no build running" in out and "Deploy: WAIT (1 reasons)" in out


def test_a_before_guard_runs_when_its_unit_ships(box, capsys, tmp_path):
    v = commit_file(box, "modules/voice_server.py", "v", "voice", when="2026-10-30T11:30:00")
    write_prompt(box, "agents/Agents-A3-X.md", {"id": "A3", "status": "on-main", "commit": f"\"{v[:9]}\""})
    git(box, "add", "-A")
    git(box, "commit", "-q", "-m", "a3")
    (tmp_path / "state" / "calls").write_text("1\n")
    code, checks, _ = checks_of(capsys)
    assert code == 1 and not checks["0 live calls"]["ok"]


def test_deploy_ready_waits_on_low_memory(box, capsys, tmp_path):
    (tmp_path / "meminfo").write_text("MemAvailable:     900000 kB\n")
    code, checks, _ = checks_of(capsys)
    assert code == 1 and not checks["memory"]["ok"] and "878" in checks["memory"]["detail"]


def test_deploy_ready_waits_on_a_held_lock(box, capsys, tmp_path):
    with open(tmp_path / "sandbox.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, checks, _ = checks_of(capsys)
    assert code == 1 and not checks["locks"]["ok"] and "sandbox.lock" in checks["locks"]["detail"]
    code, checks, _ = checks_of(capsys)
    assert code == 0 and checks["locks"]["ok"]


def test_a_missing_lock_file_is_free(box, capsys, tmp_path):
    assert not (tmp_path / "sandbox.lock").exists()
    code, checks, _ = checks_of(capsys)
    assert checks["locks"]["ok"] and not (tmp_path / "sandbox.lock").exists()


def test_deploy_ready_waits_on_a_merge_in_progress(box, capsys):
    (box / ".git" / "MERGE_HEAD").write_text(git(box, "rev-parse", "HEAD") + "\n")
    code, checks, _ = checks_of(capsys)
    assert code == 1 and not checks["merge state"]["ok"] and "MERGE_HEAD" in checks["merge state"]["detail"]


def test_deploy_ready_waits_on_uncommitted_unit_files(box, capsys):
    (box / "modules/services/new.py").write_text("x")
    (box / "modules/templates/a.html").write_text("changed")
    (box / "Documentation").mkdir()
    (box / "Documentation/notes.md").write_text("docs need no unit")
    code, checks, _ = checks_of(capsys)
    d = checks["uncommitted changes"]
    assert code == 1 and not d["ok"] and "modules/services/new.py" in d["detail"] and "modules/templates/a.html" in d["detail"]
    assert "notes.md" not in d["detail"]


def test_uncommitted_docs_only_is_ok(box, capsys):
    (box / "notes.md").write_text("x")
    code, checks, _ = checks_of(capsys)
    assert code == 0 and checks["uncommitted changes"]["ok"]


def _migration(r, name, rev, down, when):
    text = f"revision = '{rev}'\ndown_revision = {repr(down)}\n"
    return commit_file(r, f"modules/migrations/versions/{name}", text, f"migration {rev}", when=when)


def test_migration_chain_from_prod(box, capsys, tmp_path):
    _migration(box, "r1.py", "r1", None, "2026-10-29T08:00:00")
    m2 = _migration(box, "r2.py", "r2", "r1", "2026-10-30T11:10:00")
    write_prompt(box, "agents/Agents-A4-X.md", {"id": "A4", "status": "on-main", "commit": f"\"{m2[:9]}\""})
    git(box, "add", "-A")
    git(box, "commit", "-q", "-m", "a4")
    c = json.loads((tmp_path / "config.json").read_text())
    c["prompts"]["prod_revision"] = "echo r1"
    (tmp_path / "config.json").write_text(json.dumps(c))
    code, checks, _ = checks_of(capsys)
    assert checks["migration chain"]["ok"], checks["migration chain"]
    c["prompts"]["prod_revision"] = "false"
    (tmp_path / "config.json").write_text(json.dumps(c))
    code, checks, _ = checks_of(capsys)
    assert code == 1 and "alembic_version unread" in checks["migration chain"]["detail"]


def test_migration_chain_broken_or_two_heads(box, capsys, tmp_path):
    _migration(box, "r1.py", "r1", None, "2026-10-29T08:00:00")
    m3 = _migration(box, "r3.py", "r3", "rX", "2026-10-30T11:10:00")
    write_prompt(box, "agents/Agents-A4-X.md", {"id": "A4", "status": "on-main", "commit": f"\"{m3[:9]}\""})
    git(box, "add", "-A")
    git(box, "commit", "-q", "-m", "a4")
    c = json.loads((tmp_path / "config.json").read_text())
    c["prompts"]["prod_revision"] = "echo r1"
    (tmp_path / "config.json").write_text(json.dumps(c))
    code, checks, _ = checks_of(capsys)
    d = checks["migration chain"]["detail"]
    assert code == 1 and "r3" in d and "rX" in d and "2 heads" in d


def test_a_session_landing_now_waits(box, capsys, tmp_path):
    other = "bbbb2222-0000-0000-0000-000000000002"
    write_sessions(tmp_path / "tracker", [session(other, name="lander", seen=1,
                                                  pending_heads={"k": {"heads": {str(box): "abc"}, "ts": NOW_DT.timestamp() - 60}})])
    code, checks, _ = checks_of(capsys)
    assert code == 1 and "lander is landing" in checks["no session landing"]["detail"]


def test_a_commit_landed_minutes_ago_waits_but_not_a_status_commit(box, capsys, tmp_path):
    other = "cccc3333-0000-0000-0000-000000000003"
    rows = [{"sha": "d" * 40, "subject": "docs(prompts): x on-main", "repo": "r", "dir": str(box), "ts": iso(2)}]
    write_sessions(tmp_path / "tracker", [session(other, seen=1, commits=rows)])
    code, checks, _ = checks_of(capsys)
    assert code == 0 and checks["no session landing"]["ok"]
    rows.append({"sha": "e" * 40, "subject": "fix(x): y", "repo": "r", "dir": str(box), "ts": iso(3)})
    rows.append({"sha": "f" * 40, "subject": "fix(x): old", "repo": "r", "dir": str(box), "ts": iso(30)})
    write_sessions(tmp_path / "tracker", [session(other, seen=1, commits=rows)])
    code, checks, _ = checks_of(capsys)
    d = checks["no session landing"]["detail"]
    assert code == 1 and "cccc3333 landed eeeeeeeee 3 min ago" in d and "fffffffff" not in d


def test_deploy_ready_without_a_map_is_2(box, capsys):
    (box / "scripts/deploy_units.json").write_text("{not json")
    assert run("deploy-ready") == 2


def test_ready_verdict_is_the_checks(box):
    view = cc.View(cc.load_config(), datetime.fromisoformat(NOW).timestamp())
    umap = pd.load_map(view.spec)
    fails = [{"name": "x", "ok": False, "detail": "y"}]
    assert pr.verdict(fails) == (False, ["x: y"]) and pr.verdict([{"name": "x", "ok": True, "detail": ""}]) == (True, [])
    plan = {"units": [], "steps": [], "migrations": [], "guards": umap["deploy"]["guards"]}
    names = [c["name"] for c in pr.ready(view, umap, plan, None)]
    assert names[0] == "no build running" and "0 live calls" not in names


# --- 2. rebased commits ------------------------------------------------------------------------------------------

@pytest.fixture
def rebased(box):
    git(box, "checkout", "-q", "-b", "r9")
    old = commit_file(box, "modules/services/r9.py", "r9", "feat(r9): meta leads", when="2026-10-30T11:20:00")
    git(box, "checkout", "-q", "main")
    commit_file(box, "modules/services/other.py", "o", "other", when="2026-10-30T11:25:00")
    git(box, "checkout", "-q", "r9")
    git(box, "rebase", "-q", "main", env={"GIT_COMMITTER_DATE": "2026-10-30T11:30:00"})
    new = git(box, "rev-parse", "HEAD")
    git(box, "checkout", "-q", "main")
    git(box, "merge", "-q", "--no-ff", "-m", "merge r9", "r9", env={"GIT_COMMITTER_DATE": "2026-10-30T11:31:00"})
    git(box, "branch", "-q", "-D", "r9")
    assert old != new
    write_prompt(box, "connections/Connections-R9-X.md", {"id": "R9", "status": "on-main", "commit": f"\"{old[:9]}\""})
    git(box, "add", "-A")
    git(box, "commit", "-q", "-m", "r9 prompt")
    return old, new


def test_history_resolves_a_rebased_commit(rebased, box):
    old, new = rebased
    h = pd.History(str(box))
    assert h.landing(old)[0] is None and h.resolve(old) == new
    assert h.resolve(new) == new


def test_resolve_falls_back_to_subject_and_author(box):
    git(box, "checkout", "-q", "-b", "b")
    old = commit_file(box, "modules/services/q.py", "q1", "fix(q): same subject", when="2026-10-30T11:20:00")
    git(box, "checkout", "-q", "main")
    new = commit_file(box, "modules/services/q.py", "q2 resolved differently", "fix(q): same subject", when="2026-10-30T11:40:00")
    git(box, "branch", "-q", "-D", "b")
    assert pd.History(str(box)).resolve(old) == new


def test_deploy_check_and_plan_show_the_rebased_sha(rebased, box, capsys):
    old, new = rebased
    run("deploy-check", "--json")
    r9 = next(e for e in json.loads(capsys.readouterr().out)["prompts"] if e["id"] == "R9")
    assert r9["state"] != "unchecked" and r9["commits"][0]["sha"] == new[:9] and r9["commits"][0]["rebased_from"] == old[:9]
    assert r9["waits"] == ["app", "app-web@2", "app-celery"]
    run("deploy-check")
    assert f"{old[:9]} → {new[:9]} (rebased)" in capsys.readouterr().out
    run("deploy-plan")
    assert f"{old[:9]} → {new[:9]} (rebased)" in capsys.readouterr().out


def test_audit_says_to_set_the_new_sha(rebased, box, capsys):
    old, new = rebased
    run("audit")
    out = capsys.readouterr().out
    assert f"connections/R9: {old[:9]} was rebased to {new[:9]}: set connections/R9 --commit {new[:9]}" in out
    assert old[:9] in (box / "docs/Prompts/connections/Connections-R9-X.md").read_text()  # never rewritten


# --- 3. deploy-run -------------------------------------------------------------------------------------------------

def test_deploy_run_refuses_without_the_owners_ok(box, capsys, tmp_path):
    assert run("deploy-run", "--owner-ok", "  ") == 2
    assert "owner" in capsys.readouterr().err
    assert restarted(tmp_path) == []


def test_deploy_run_refuses_on_wait(box, capsys, tmp_path):
    (tmp_path / "state" / "builds").write_text("1\n")
    assert run("deploy-run", "--owner-ok", "go") == 1
    out = capsys.readouterr().out
    assert "WAIT: no build running" in out and restarted(tmp_path) == []
    assert not (tmp_path / "tracker" / "deploys").exists()


def test_deploy_run_dry_run_prints_every_command_and_runs_nothing(box, capsys, tmp_path):
    assert run("deploy-run", "--owner-ok", "go", "--dry-run") == 0
    out = capsys.readouterr().out
    assert restarted(tmp_path) == [] and not (tmp_path / "tracker" / "deploys").exists()
    web = str(fake_of(tmp_path) / "restart_web.sh")
    assert f"\n  {web}\n" in out + "\n" and "flock" not in out.split("Steps", 1)[1]
    assert out.index(web) < out.index("systemctl restart app-celery") < out.index("ready\\.")
    assert "systemctl show -p ActiveEnterTimestamp app-web@2" in out and "curl" in out


def test_deploy_run_dry_run_on_wait_still_prints_the_plan(box, capsys, tmp_path):
    (tmp_path / "state" / "builds").write_text("1\n")
    assert run("deploy-run", "--owner-ok", "go", "--dry-run") == 1
    out = capsys.readouterr().out
    assert "systemctl restart app-celery" in out and "WAIT" in out and restarted(tmp_path) == []


def test_deploy_run_restarts_proves_records_and_sets_deployed(box, capsys, tmp_path):
    assert run("deploy-run", "--owner-ok", "owner: ship A1 and A2", "--no-commit") == 0, capsys.readouterr()
    out = capsys.readouterr().out
    assert restarted(tmp_path) == ["app", "app-web@2", "app-celery"]
    files = list((tmp_path / "tracker" / "deploys").glob("*.json"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text())
    assert rec["owner_ok"] == "owner: ship A1 and A2" and rec["result"] == "ok" and rec["end"] >= rec["started"]
    assert [s["unit"] for s in rec["steps"]] == ["web", "app-celery"]
    assert all(c["rc"] == 0 for s in rec["steps"] for c in s["commands"])
    assert all(p["ok"] for s in rec["steps"] for p in s["proofs"])
    assert rec["steps"][0]["commands"][0]["cmd"] == str(fake_of(tmp_path) / "restart_web.sh")
    assert sorted(rec["deployed"]) == ["docs/Prompts/agents/Agents-A1-X.md", "docs/Prompts/agents/Agents-A2-X.md"]
    for rel in rec["deployed"]:
        assert pc.parse_frontmatter((box / rel).read_text())[0]["status"] == "deployed"
    assert "Tell: a04b-lander (aaaa1111) · agents/A1 · owes: probe 320 sees 200" in out
    assert "Tell: (no live session) · agents/A2" in out
    assert "web" in out and "started" in out


def test_deploy_run_passes_its_id_to_each_command(box, capsys, tmp_path):
    (fake_of(tmp_path) / "restart_web.sh").write_text(
        FAKE_RESTART_WEB + f'echo "$NEUROGEN_DEPLOY_RUN" > {tmp_path / "state" / "run_id"}\n')
    assert run("deploy-run", "--owner-ok", "go", "--no-commit") == 0
    rec = json.loads(next((tmp_path / "tracker" / "deploys").glob("*.json")).read_text())
    assert (tmp_path / "state" / "run_id").read_text().strip() == rec["id"]


def test_a_failing_health_proof_stops_before_the_next_step(box, capsys, tmp_path):
    (tmp_path / "state" / "health").write_text("502")
    assert run("deploy-run", "--owner-ok", "go", "--no-commit") == 1
    out = capsys.readouterr().out
    assert restarted(tmp_path) == ["app", "app-web@2"]  # app-celery untouched
    rec = json.loads(next((tmp_path / "tracker" / "deploys").glob("*.json")).read_text())
    assert rec["result"].startswith("failed") and rec["end"] and rec["deployed"] == []
    assert "Not live" in out and "app-celery" in out.split("Not live", 1)[1]
    assert pc.parse_frontmatter((box / "docs/Prompts/agents/Agents-A1-X.md").read_text())[0]["status"] == "on-main"


def test_a_failing_restart_command_stops(box, capsys, tmp_path):
    (fake_of(tmp_path) / "restart_web.sh").write_text("#!/bin/sh\nexit 4\n")
    assert run("deploy-run", "--owner-ok", "go", "--no-commit") == 1
    assert restarted(tmp_path) == []
    rec = json.loads(next((tmp_path / "tracker" / "deploys").glob("*.json")).read_text())
    assert rec["steps"][0]["commands"][0]["rc"] == 4 and rec["end"]


def test_deploy_run_stops_at_migrations(box, capsys, tmp_path):
    _migration(box, "r1.py", "r1", None, "2026-10-29T08:00:00")
    m2 = _migration(box, "r2.py", "r2", "r1", "2026-10-30T11:10:00")
    write_prompt(box, "agents/Agents-A4-X.md", {"id": "A4", "status": "on-main", "commit": f"\"{m2[:9]}\""})
    git(box, "add", "-A")
    git(box, "commit", "-q", "-m", "a4")
    c = json.loads((tmp_path / "config.json").read_text())
    c["prompts"]["prod_revision"] = "echo r1"
    (tmp_path / "config.json").write_text(json.dumps(c))
    assert run("deploy-run", "--owner-ok", "go") == 3
    out = capsys.readouterr().out
    assert "scripts/vault_exec.sh python apply_migrations.py" in out and "modules/migrations/versions/r2.py" in out
    assert "rerun" in out and restarted(tmp_path) == []


def test_deploy_run_only_one_step(box, capsys, tmp_path):
    assert run("deploy-run", "--owner-ok", "go", "--only", "app-celery", "--no-commit") == 0
    assert restarted(tmp_path) == ["app-celery"]
    assert run("deploy-run", "--owner-ok", "go", "--only", "nope") == 2


def test_deploy_run_refuses_while_the_deploy_lock_is_held(box, capsys, tmp_path):
    with open(tmp_path / "deploy.lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run("deploy-run", "--owner-ok", "go") == 1
    assert "lock" in capsys.readouterr().out and restarted(tmp_path) == []


def test_nothing_to_restart_still_applies(box, capsys, tmp_path):
    for u in ("app", "app-web@2", "app-celery"):
        (tmp_path / "state" / f"start-{u}").write_text(str(ts("2026-10-30T12:00:00")))
    assert run("deploy-run", "--owner-ok", "go", "--no-commit") == 0
    out = capsys.readouterr().out
    assert "nothing to restart" in out and restarted(tmp_path) == [] and "Tell:" in out
    assert pc.parse_frontmatter((box / "docs/Prompts/agents/Agents-A1-X.md").read_text())[0]["status"] == "deployed"


def test_health_tokens_must_be_three_200s():
    assert pr.health_ok("200 200 200 \n") and not pr.health_ok("200 200") and not pr.health_ok("200 502 200")
    assert not pr.health_ok("")


# --- 4. Tell after deploy-check --apply ----------------------------------------------------------------------

def test_deploy_check_apply_prints_tell_lines(box, capsys, tmp_path):
    for u in ("app", "app-web@2"):
        (tmp_path / "state" / f"start-{u}").write_text(str(ts("2026-10-30T12:00:00")))
    assert run("deploy-check", "--apply", "--no-commit") == 0
    out = capsys.readouterr().out
    assert "Tell: a04b-lander (aaaa1111) · agents/A1 · owes: probe 320 sees 200" in out and "A2" not in out.split("Tell", 1)[1]


# --- 6. the /cc line --------------------------------------------------------------------------------------------

def test_view_shows_the_deploy_line(box, capsys, tmp_path, monkeypatch):
    calls = []
    real = __import__("subprocess").run

    def watched(cmd, *a, **kw):
        calls.append(cmd)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(__import__("subprocess"), "run", watched)
    assert run() == 0
    out = capsys.readouterr().out
    assert "Deploy: OK · last deploy never" in out
    assert not any("psql" in str(c) or "systemctl" in str(c) for c in calls)
    (tmp_path / "freeze").write_text("suites running")
    d = tmp_path / "tracker" / "deploys"
    d.mkdir()
    (d / "20261030T150000.json").write_text(json.dumps({"id": "x", "started": ts("2026-10-30T14:58:00"),
                                                        "end": ts("2026-10-30T15:00:00")}))
    assert run() == 0
    out = capsys.readouterr().out
    assert "Deploy: WAIT (deploy freeze: suites running) · last deploy 1h ago" in out


def test_cc_command_files_match():
    root = Path(__file__).resolve().parent.parent / ".claude" / "commands"
    a, b = (root / "cc.md").read_text(), (root / "command-center.md").read_text()
    body = lambda t: t.split("---", 2)[2]  # noqa: E731
    assert body(a) == body(b)
    assert "deploy-run --owner-ok" in a and "SendMessage" in a and "deploy-ready" in a and "never a peer" in a


def test_a_multi_line_merge_down_revision_is_read():
    """Live 11-01: NeuroGenServer's reconcile_20260520 lists its downs over several lines; read as none, deploy-ready
    saw 4 heads where alembic has one."""
    import prompt_plan as pp
    text = ("revision: str = 'reconcile_20260520'\ndown_revision: Union[str, None] = (\n    '20260429_add_user_files_thumbnail',\n"
            "    \"20260505_ad_track\",  # ads\n    '7f431a6a7b6e',\n)\nbranch_labels = None\n")
    assert pp.revision_of(text) == ("reconcile_20260520", ["20260429_add_user_files_thumbnail", "20260505_ad_track", "7f431a6a7b6e"])
    assert pp.revision_of("revision = 'b'\ndown_revision = 'a'\nx = ('z',)\n") == ("b", ["a"])
    assert pp.revision_of("revision = 'a'\ndown_revision = None\n") == ("a", [])


def test_a_rebased_sha_listed_with_its_new_sha_counts_once(rebased, box, capsys):
    old, new = rebased
    path = box / "docs/Prompts/connections/Connections-R9-X.md"
    path.write_text(path.read_text().replace(f'commit: "{old[:9]}"', f'commits: ["{old[:9]}", "{new[:9]}"]'))
    run("deploy-check", "--json")
    r9 = next(e for e in json.loads(capsys.readouterr().out)["prompts"] if e["id"] == "R9")
    assert [c["sha"] for c in r9["commits"]] == [new[:9]] and r9["commits"][0]["rebased_from"] == old[:9]
    run("audit")
    out = capsys.readouterr().out
    assert f"connections/R9: {old[:9]} was rebased to {new[:9]}, which its commits already list" in out
