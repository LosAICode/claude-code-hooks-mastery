import json
import subprocess
from datetime import datetime, timedelta

import prompt_center as pc
from conftest import git
from test_prompt_scan import cfg, proj  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 30, 16, 0).astimezone()


def iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def session(sid, state="working", seen=5, **kw):
    return {"session_id": sid, "state": state, "last_seen": iso(seen), "task": None, "branch": "main", **kw}


def write_sessions(d, sessions):
    d.mkdir(parents=True, exist_ok=True)
    for s in sessions:
        (d / f"{s['session_id']}.json").write_text(json.dumps(s))


def prompts(proj):
    return pc.scan(cfg(proj))


def rel(proj, name):
    return next(p.rel for p in prompts(proj) if p.name == name)


def test_load_sessions_skips_junk(tmp_path):
    write_sessions(tmp_path, [session("a1"), session("b2")])
    (tmp_path / "junk.json").write_text("{not json")
    (tmp_path / "config.json").write_text("{}")
    assert sorted(s["session_id"] for s in pc.load_sessions(tmp_path)) == ["a1", "b2"]


def test_join_by_prompt_file_and_by_task(proj, tmp_path):
    b12 = rel(proj, "Funnels-B12-3-Step-Analytics-CSV.md")
    h8b = rel(proj, "History-H8b-Paid-Paths-Without-Rows.md")
    sessions = [
        session("aaaa1111-x", prompt_file=b12, last_commit="9718648ea merge"),
        session("bbbb2222-x", state="waiting_on_owner", task="run and execute from /docs History-H8b-Paid-Paths-Without-Rows.md"),
        session("cccc3333-x", task="why are there 63 app files?"),
    ]
    runs = pc.join(prompts(proj), sessions, cfg(proj), NOW.timestamp())
    assert [r["sid8"] for r in runs[b12]] == ["aaaa1111"]
    assert runs[b12][0]["live"] is True and runs[b12][0]["last_commit"] == "9718648ea merge"
    assert [(r["sid8"], r["state"]) for r in runs[h8b]] == [("bbbb2222", "WAITING ON YOU")]
    assert sum(len(v) for v in runs.values()) == 2


def test_live_means_not_ended_and_seen_recently(proj):
    b12 = rel(proj, "Funnels-B12-3-Step-Analytics-CSV.md")
    sessions = [session("a", state="ended", seen=10, prompt_file=b12), session("b", seen=200, prompt_file=b12)]
    runs = pc.join(prompts(proj), sessions, cfg(proj), NOW.timestamp())
    assert [(r["sid8"], r["live"], r["state"]) for r in runs[b12]] == [("a", False, "ended"), ("b", False, "stale?")]


def test_effective_status(proj):
    ps = {p.name: p for p in prompts(proj)}
    pending = ps["History-H8b-Paid-Paths-Without-Rows.md"]
    assert pc.effective_status(pending, []) == "pending"
    assert pc.effective_status(pending, [{"live": True}]) == "running"
    pending.status = "running"
    assert pc.effective_status(pending, [{"live": False}]) == "stalled"
    assert pc.effective_status(pending, []) == "stalled"
    assert pc.effective_status(pending, [{"live": True}]) == "running"
    done = ps["Funnels-B12-3-Step-Analytics-CSV.md"]
    done.status = "done"
    assert pc.effective_status(done, [{"live": True}]) == "done"


def test_progress_counts_and_next(proj):
    for name, text in {
        "funnels/Funnels-B13-Suites.md": "---\nstatus: pending\norder: 10\ndepends_on: [B12-4]\n---\n# B13\n",
        "funnels/Funnels-B12-4-Webinar.md": "---\nstatus: pending\norder: 9d\n---\n# B12-4\n",
        "funnels/Funnels-B11-Perf.md": "---\nstatus: deployed\norder: 7\n---\n# B11\n",
        "funnels/Funnels-B10-KB.md": "---\nstatus: done\norder: 8\n---\n# B10\n",
        "funnels/Funnels-B9-Old.md": "---\nstatus: skipped\norder: 6\n---\n# B9\n",
        "funnels/Funnels-B8-Sec.md": "---\nstatus: blocked\norder: 5\nneeds_owner: PSL = owner\n---\n# B8\n",
    }.items():
        (proj / "docs/Prompts" / name).write_text(text)
    ps = prompts(proj)
    b4 = next(p for p in ps if p.id == "B4")
    runs = {b4.rel: [{"sid8": "d60450f0", "live": True, "state": "working", "age_s": 60}]}
    mods = {m["module"]: m for m in pc.progress(ps, runs)}
    f = mods["funnels"]
    assert (f["total"], f["finished"]) == (7, 2)          # 8 prompts minus the skipped one; done + deployed
    assert f["counts"]["on-main"] == 1 and f["counts"]["running"] == 1 and f["counts"]["blocked"] == 1
    assert [p.id for p, _ in f["running"]] == ["B4"]
    assert [p.id for p in f["blocked"]] == ["B8"]
    assert f["next"].id == "B12-4"                        # B13 waits for B12-4; B4 is running
    assert mods["history"]["next"].id == "H8b"
    assert [m["module"] for m in pc.progress(ps, runs)] == sorted(mods)


def test_mark_running_claims_only_claimable(proj, tmp_path):
    spec = cfg(proj)
    h8b = rel(proj, "History-H8b-Paid-Paths-Without-Rows.md")
    assert pc.mark_running(spec, h8b, "sid-1", live_sids={"sid-1"}) == "pending"
    meta, _, _ = pc.parse_frontmatter((proj / h8b).read_text())
    assert meta["status"] == "running" and meta["session_id"] == "sid-1" and meta["updated"]
    assert pc.mark_running(spec, h8b, "sid-2", live_sids={"sid-1", "sid-2"}) is None    # sid-1 still live
    assert pc.mark_running(spec, h8b, "sid-2", live_sids={"sid-2"}) == "running"         # sid-1 gone: re-claim
    b12 = rel(proj, "Funnels-B12-3-Step-Analytics-CSV.md")                               # on-main: never claimed
    assert pc.mark_running(spec, b12, "sid-3", live_sids=set()) is None
    assert "on-main" in (proj / b12).read_text()


def test_set_status_and_commit(proj):
    git(proj, "init", "-q", "-b", "main")
    git(proj, "add", "-A")
    git(proj, "commit", "-q", "-m", "init")
    spec = cfg(proj)
    h8b = rel(proj, "History-H8b-Paid-Paths-Without-Rows.md")
    meta = pc.set_status(spec, h8b, "on-main", commit="abc1234", notes="deploy held")
    assert meta["status"] == "on-main" and meta["commit"] == "abc1234" and meta["notes"] == "deploy held"
    (proj / "docs/other.md").write_text("unrelated dirty file\n")
    sha = pc.commit_prompt_files(spec, [h8b], "docs(prompts): H8b on main")
    assert sha and git(proj, "log", "-1", "--format=%s") == "docs(prompts): H8b on main"
    assert git(proj, "status", "--porcelain") == "?? docs/other.md"   # pathspec: the unrelated file stays


def test_set_status_rejects_unknown_status(proj):
    try:
        pc.set_status(cfg(proj), rel(proj, "F3-Planner-Cap.md"), "finished")
    except ValueError as e:
        assert "finished" in str(e)
    else:
        raise AssertionError("expected ValueError")
