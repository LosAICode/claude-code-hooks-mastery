"""CC6: the planning commands (commits list, inbox, deploy-plan, proofs, next --parallel). All read-only."""
import json
import subprocess
from datetime import datetime

import pytest

import command_center as cc
import prompt_center as pc
import prompt_deploy as pd
import prompt_plan as pp
from conftest import commit_file, git
from test_prompt_join import session, write_sessions

NOW = "2026-10-30T16:00:00"
UNIT_MAP = {
    "groups": {"web": ["app", "app-web@2"]},
    "rules": [
        {"paths": ["modules/static/**"], "live_at_merge": True},
        {"paths": ["modules/templates/**"], "units": ["web"]},
        {"paths": ["modules/**"], "exclude": ["modules/static/**", "modules/templates/**"], "units": ["web", "app-celery"]},
        {"paths": ["modules/tasks/**"], "units": ["app-celery-beat"]},
        {"paths": ["modules/voice_server.py"], "units": ["app-voice"]},
    ],
    "parallel": {"shared_units": [], "ignore": ["Documentation/notes.md"]},
    "deploy": {
        "migrations": "modules/migrations/versions/",
        "migrate": "scripts/vault_exec.sh python apply_migrations.py",
        "order": ["web", "app-celery", "app-celery-beat", "app-voice"],
        "restart": {"web": "flock /run/lock/deploy.lock scripts/restart_web.sh", "*": "systemctl restart {unit}"},
        "guards": [
            {"name": "no build running", "command": "psql -tAc 'SELECT count(*) FROM builds'", "expect": "0"},
            {"name": "no deploy freeze", "command": "test ! -e /run/lock/deploy-freeze && echo none", "expect": "none"},
            {"name": "0 live calls", "command": "ss -Htn state established '( sport = :5026 )' | wc -l", "expect": "0",
             "before": ["app-voice"]},
        ],
        "proof": ["systemctl show -p ActiveEnterTimestamp {unit}"],
        "ready": {"app-celery": "grep -a ' ready\\.' /var/log/celery-error.log | tail -1"},
        "after_each": ["curl -s -o /dev/null -w '%{http_code}' localhost/api/health  # x3"],
    },
}


def ts(text):
    return int(datetime.fromisoformat(text).timestamp())


def prompt(r, rel, meta, body="# x\n"):
    p = r / "docs/Prompts" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in meta.items()) + "---\n" + body)


@pytest.fixture
def plan(tmp_path, monkeypatch):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, "README.md", "x", "init", when="2026-10-30T08:00:00")
    commit_file(r, "scripts/deploy_units.json", json.dumps(UNIT_MAP), "map", when="2026-10-30T08:00:01")
    commit_file(r, "modules/services/engine.py", "e", "engine", when="2026-10-30T08:00:02")
    commit_file(r, "Documentation/guide.md", "g", "guide", when="2026-10-30T08:00:03")
    tpl = commit_file(r, "modules/templates/a.html", "t", "tpl", when="2026-10-30T10:00:00")
    docs = commit_file(r, "Documentation/x.md", "d", "docs", when="2026-10-30T10:30:00")
    mig = commit_file(r, "modules/migrations/versions/r9_add_col.py", "m", "migration", when="2026-10-30T11:00:00")
    beat = commit_file(r, "modules/tasks/t.py", "b", "beat", when="2026-10-30T11:30:00")
    voice = commit_file(r, "modules/voice_server.py", "v", "voice", when="2026-10-30T11:40:00")
    # agents: deploy + inbox + proofs
    prompt(r, "agents/Agents-C01-X.md", {"id": "C01", "status": "on-main", "commits": f"[\"{tpl[:9]}\", \"{docs[:9]}\"]"})
    prompt(r, "agents/Agents-C02-X.md", {"id": "C02", "status": "on-main", "commit": f"\"{mig[:9]}\""})
    prompt(r, "agents/Agents-C09-X.md", {"id": "C09", "status": "deployed", "commits": f"[\"{tpl[:9]}\", \"{beat[:9]}\"]",
                                         "deployed": "2026-10-30T15:30:00-04:00"})
    prompt(r, "agents/Agents-C10-X.md", {"id": "C10", "status": "on-main", "commit": f"\"{voice[:9]}\""})
    prompt(r, "agents/Agents-C03-X.md", {"id": "C03", "status": "deployed", "cost": "session tokens; a few credits",
                                         "proof": "\"probe 320 chat; a few credits\""})
    prompt(r, "agents/Agents-C04-X.md", {"id": "C04", "status": "deployed", "cost": "one build ~$3"},
           "# C04\n\n## Done when\n- Tests pass.\n- Live: a build of the sample\n  site publishes.\n\n## Next\n- x\n")
    prompt(r, "agents/Agents-C05-X.md", {"id": "C05", "status": "deployed", "cost": "session tokens only (no AI credits)"},
           "# C05\n\n## Done when\n- Live: probe 318 sees 403.\n")
    prompt(r, "agents/Agents-C06-X.md", {"id": "C06", "status": "blocked", "needs_owner": "deploy OK", "cost": "\"$2 of credits\"",
                                         "updated": "2026-10-29T09:00:00-04:00"})
    prompt(r, "agents/Agents-C07-X.md", {"id": "C07", "status": "pending", "needs_owner": "Zoom key",
                                         "updated": "2026-10-28T09:00:00-04:00"})
    prompt(r, "agents/Agents-C08-X.md", {"id": "C08", "status": "done", "needs_owner": "old ask"})
    prompt(r, "agents/Agents-C12-X.md", {"id": "C12", "status": "deployed", "needs_owner": "answered 10-29"})
    prompt(r, "agents/Agents-C11-X.md", {"id": "C11", "status": "on-main", "needs_owner": "restart OK",
                                         "updated": "2026-10-30T09:00:00-04:00"})
    # engine: next --parallel
    prompt(r, "engine/Engine-E1-X.md", {"id": "E1", "status": "running", "session_id": "e1e1e1e1-live"})
    prompt(r, "engine/Engine-E2-X.md", {"id": "E2", "status": "pending"}, "# E2\nTouches `modules/services/engine.py`.\n")
    prompt(r, "engine/Engine-E3-X.md", {"id": "E3", "status": "pending", "run_last": "true"}, "# E3\n`Documentation/guide.md`\n")
    prompt(r, "engine/Engine-E4-X.md", {"id": "E4", "status": "pending"}, "# E4\n> Read the README. Run after E9 (same file).\n")
    prompt(r, "engine/Engine-E5-X.md", {"id": "E5", "status": "pending"}, "# E5\nOnly `Documentation/guide.md`; A12 (\"run last\").\n")
    prompt(r, "engine/Engine-E6-X.md", {"id": "E6", "status": "pending", "depends_on": "[E1]"}, "# E6\n")
    prompt(r, "engine/Engine-E7-X.md", {"id": "E7", "status": "pending"}, "# E7\nThe page `modules/templates/a.html`.\n")
    prompt(r, "engine/Engine-E8-X.md", {"id": "E8", "status": "pending"}, "# E8\n> Run last.\n")
    prompt(r, "engine/Engine-E9-X.md", {"id": "E9", "status": "pending", "after": "[E1]"}, "# E9\n")
    prompt(r, "ux/Ux-U1-X.md", {"id": "U1", "status": "pending", "run_last": "true"}, "# U1\n")
    commit_file(r, "Documentation/notes.md", "n", "notes", when="2026-10-30T08:00:04")
    prompt(r, "ux/Ux-U2-X.md", {"id": "U2", "status": "pending"}, "# U2\nNotes in `Documentation/notes.md`, tests in `modules`.\n")
    prompt(r, "ux/Ux-U3-X.md", {"id": "U3", "status": "pending"}, "# U3\nAlso `Documentation/notes.md`.\n")
    (r / "docs/Status-Board.md").write_text("| Workstream | State |\n|---|---|\n"
                                            "| Agents | C05 waits on the owner's walk · C06 owner OK |\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "prompts", env={"GIT_AUTHOR_DATE": "2026-10-30T14:00:00", "GIT_COMMITTER_DATE": "2026-10-30T14:00:00"})
    # E1's worktree: a branch that changed engine.py
    wt = tmp_path / "wt-e1"
    git(r, "worktree", "add", "-q", "-b", "e1", str(wt))
    commit_file(wt, "modules/services/engine.py", "e2", "e1 work", when="2026-10-30T15:00:00")
    (wt / "modules/services/new_helper.py").write_text("uncommitted")
    starts = {"app": ts("2026-10-30T09:00:00"), "app-web@2": ts("2026-10-30T09:00:00"),
              "app-celery": ts("2026-10-30T09:00:00"), "app-celery-beat": ts("2026-10-30T12:00:00"), "app-voice": None}
    monkeypatch.setattr(pd, "unit_started", starts.get)
    tracker = tmp_path / "tracker"
    write_sessions(tracker, [session("e1e1e1e1-live", worktree=str(wt), branch="e1",
                                     prompt_file="docs/Prompts/engine/Engine-E1-X.md")])
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    c = {"prompts": {"repo": str(r), "root": "docs/Prompts", "unit_map": "scripts/deploy_units.json",
                     "status_board": "docs/Status-Board.md"}}
    (tmp_path / "config.json").write_text(json.dumps(c))
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    return r


def run(*args):
    return cc.main(["--now", NOW, *args])


def out_json(capsys, *args):
    assert run(*args, "--json") == 0
    return json.loads(capsys.readouterr().out)


# --- 1. commits list ---------------------------------------------------------------------------------

def test_commits_of_reads_the_list_and_the_legacy_key():
    assert pc.commits_of({"commits": ["abc1234", "def5678"], "commit": "abc1234 9999999"}) == ["abc1234", "def5678", "9999999"]
    assert pc.commits_of({"commit": "abc1234 (merge 1234abcd)"}) == ["abc1234", "1234abcd"]
    assert pc.commits_of({}) == []
    assert pd.commits_of is pc.commits_of


def test_two_commits_one_needing_no_unit_still_wait_for_the_other(plan, capsys):
    r = {e["id"]: e for e in out_json(capsys, "deploy-check")["prompts"]}
    assert r["C01"]["state"] == "waiting" and len(r["C01"]["commits"]) == 2 and r["C01"]["waits"] == ["app", "app-web@2"]


def test_set_commit_appends_and_folds_the_legacy_key_in(plan, capsys):
    path = plan / "docs/Prompts/agents/Agents-C02-X.md"
    assert run("set", "agents/C02", "on-main", "--commit", "a11082006", "--no-commit") == 0
    meta = pc.parse_frontmatter(path.read_text())[0]
    first = pc.commits_of({"commit": git(plan, "log", "-1", "--format=%h", "--abbrev=9", "--", "modules/migrations")})[0]
    assert meta["commits"] == [first, "a11082006"] and "commit" not in meta
    assert run("set", "agents/C02", "deployed", "--commit", "a11082006", "--no-commit") == 0
    assert pc.parse_frontmatter(path.read_text())[0]["commits"] == [first, "a11082006"]


def test_audit_counts_a_commits_list_as_a_commit(plan, capsys):
    run("audit")
    out = capsys.readouterr().out
    assert "C01" not in out and any("C03" in line and "deployed with no commit" in line for line in out.splitlines())


# --- 2. inbox ----------------------------------------------------------------------------------------

def test_inbox_lists_owner_asks_oldest_first_with_cost_and_paste_line(plan, capsys):
    data = out_json(capsys, "inbox")
    asks = data["asks"]
    assert [a["id"] for a in asks] == ["C07", "C06", "C11"]
    assert asks[0]["ask"] == "Zoom key" and asks[0]["paste"] == "run docs/Prompts/agents/Agents-C07-X.md"
    assert asks[1]["cost"] == "$2 of credits" and asks[1]["waited_s"] == ts(NOW) - int(datetime.fromisoformat("2026-10-29T09:00:00-04:00").timestamp())
    assert "deploy-plan --module agents" in asks[2]["paste"]
    assert [b["id"] for b in data["board"]] == ["C05"]  # C06 already has a needs_owner
    assert run("inbox") == 0
    text = capsys.readouterr().out
    assert text.index("agents/C07") < text.index("agents/C06") and "waited 2d" in text and "C05" in text


# --- 3. deploy-plan --------------------------------------------------------------------------------------

def _snapshot(r, tmp_path):
    files = {p: p.read_bytes() for p in (tmp_path).rglob("*") if p.is_file() and ".git/" not in str(p)}
    return files, git(r, "status", "--porcelain"), git(r, "rev-parse", "HEAD")


def test_deploy_plan_orders_units_lists_migrations_first_and_guards(plan, capsys, tmp_path, monkeypatch):
    before = _snapshot(plan, tmp_path)
    real = subprocess.run

    def watched(cmd, *a, **kw):
        assert "restart" not in cmd and "systemctl" not in cmd, cmd
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", watched)
    data = out_json(capsys, "deploy-plan")
    assert [p["id"] for p in data["prompts"]] == ["C01", "C02", "C09", "C10", "C11"]
    assert data["migrations"][0]["file"] == "modules/migrations/versions/r9_add_col.py" and data["migrations"][0]["id"] == "C02"
    assert data["migrations"][0]["applied_at_boot"] is False
    steps = [s["unit"] for s in data["steps"]]
    assert steps == ["web", "app-celery", "app-voice"]  # beat already restarted after C09's commit
    assert data["steps"][0]["commands"] == ["flock /run/lock/deploy.lock scripts/restart_web.sh"]
    assert data["steps"][1]["commands"] == ["systemctl restart app-celery"]
    guards = {g["name"]: g for g in data["guards"]}
    assert set(guards) == {"no build running", "no deploy freeze", "0 live calls"}
    assert guards["0 live calls"]["before"] == ["app-voice"]
    assert run("deploy-plan") == 0
    text = capsys.readouterr().out
    assert text.index("Migrations") < text.index("Guards") < text.index("1. web") < text.index("2. app-celery")
    assert text.index("0 live calls") > text.index("2. app-celery")  # printed right before -voice
    assert data["steps"][1]["proof"] == ["systemctl show -p ActiveEnterTimestamp app-celery",
                                         "grep -a ' ready\\.' /var/log/celery-error.log | tail -1",
                                         "curl -s -o /dev/null -w '%{http_code}' localhost/api/health  # x3"]
    assert "ActiveEnterTimestamp app-celery" in text and "deploy-check --apply" in text and "runs nothing" in text
    assert _snapshot(plan, tmp_path) == before


def test_deploy_plan_without_a_deploy_block_uses_the_map_order(plan, capsys):
    umap = {k: v for k, v in UNIT_MAP.items() if k != "deploy"}
    (plan / "scripts/deploy_units.json").write_text(json.dumps(umap))
    data = out_json(capsys, "deploy-plan")
    assert [s["unit"] for s in data["steps"]] == ["web", "app-celery", "app-voice"]
    assert data["note"]


# --- 4. proofs ------------------------------------------------------------------------------------------

def test_proofs_lists_deployed_prompts_cheapest_first(plan, capsys):
    rows = out_json(capsys, "proofs")["proofs"]
    assert [r["id"] for r in rows] == ["C05", "C12", "C09", "C03", "C04"]
    by = {r["id"]: r for r in rows}
    assert by["C03"]["proof"] == "probe 320 chat; a few credits"
    assert by["C04"]["proof"] == "Live: a build of the sample site publishes." and by["C04"]["rank"] == "paid"
    assert by["C05"]["rank"] == "free" and by["C09"]["rank"] == "unknown" and by["C09"]["proof"] == ""


# --- 5. next --parallel -------------------------------------------------------------------------------------

def test_next_parallel_starts_only_what_overlaps_nothing(plan, capsys):
    data = out_json(capsys, "next", "--parallel")
    start = [p["id"] for p in data["start"]]
    held = {p["id"]: p["why"] for p in data["held"]}
    assert "E5" in start and "U1" in start  # U1: run last, but nothing else of ux runs
    assert data["start"][start.index("E5")]["paste"] == "run docs/Prompts/engine/Engine-E5-X.md"
    assert "modules/services/engine.py" in held["E2"] and "E1" in held["E2"]
    assert "run last" in held["E3"] and "run last" in held["E8"]
    assert "E9" in held["E4"]
    assert "E1" in held["E6"] and "E1" in held["E9"]
    assert "web" in held["E7"]  # shares units with E1's change
    assert "E1" not in start and "E1" not in held  # running: not a candidate
    assert "U2" in start and "U3" in start  # an ignored path and a folder mention are no overlap
    assert run("next", "--parallel") == 0
    text = capsys.readouterr().out
    assert "run docs/Prompts/engine/Engine-E5-X.md" in text and "E2" in text


def test_shared_units_are_no_conflict(plan, capsys):
    umap = json.loads((plan / "scripts/deploy_units.json").read_text())
    umap["parallel"]["shared_units"] = ["web", "app-celery"]
    (plan / "scripts/deploy_units.json").write_text(json.dumps(umap))
    data = out_json(capsys, "next", "--parallel")
    assert "E7" in [p["id"] for p in data["start"]]
    assert "E2" in {p["id"] for p in data["held"]}  # the same file still is


def test_inbox_waits_since_the_file_changed_without_updated(plan, capsys):
    import os
    path = plan / "docs/Prompts/agents/Agents-C07-X.md"
    path.write_text(path.read_text().replace("updated: 2026-10-28T09:00:00-04:00\n", ""))
    stamp = datetime.fromisoformat("2026-10-27T16:00:00").timestamp()
    os.utime(path, (stamp, stamp))
    asks = out_json(capsys, "inbox")["asks"]
    assert asks[0]["id"] == "C07" and asks[0]["waited_s"] == 3 * 86400
    assert "C12" not in [a["id"] for a in asks]  # "answered …" is no open ask


def test_body_hints():
    assert pp.order_hints({}, "> Read it. Run after A01/A02 (same file).") == (False, ["A01", "A02"])
    assert pp.order_hints({}, "**Run after H1 and H2**: it needs") == (False, ["H1", "H2"])
    assert pp.order_hints({}, "> Run last.\n") == (True, [])
    assert pp.order_hints({}, 'A12 ("run last") was started') == (False, [])
    assert pp.order_hints({"run_last": "true", "after": ["X1"]}, "") == (True, ["X1"])


def test_cost_rank():
    assert pp.cost_rank("session tokens only (no AI credits)")[0] == 0
    assert pp.cost_rank("session tokens only; 0 credits; NO Magnus build (canary rule)")[0] == 0
    assert pp.cost_rank("")[0] == 1
    assert pp.cost_rank("one build ~$3") > pp.cost_rank("a few credits") > pp.cost_rank("")
