"""Status folders: `Completed/` reads as done, `set` and `sort` keep files in `Completed/` or `Pending/`."""
import json

import pytest

import command_center as cc
import prompt_center as pc
import session_tracker as st
from conftest import commit_file, git, write_config
from test_prompt_join import NOW, session, write_sessions

ROOT = "docs/Prompts"


def spec_of(r, **kw):
    return {"repo": str(r), "root": ROOT, "module_aliases": {"Communications": "comms"}, **kw}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A git repo with capitalized module folders, one prompt per status, a README with markers and a board."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    files = {
        f"{ROOT}/Alpha/Alpha-00-README.md": "# Alpha\n\n<!-- status:begin -->\nold\n<!-- status:end -->\n",
        f"{ROOT}/Alpha/Alpha-A1-First.md": "---\nstatus: pending\norder: 1\n---\n# A1: first\n",
        f"{ROOT}/Alpha/Alpha-A2-Second.md": "---\nstatus: done\norder: 2\n---\n# A2: second\n",
        f"{ROOT}/Alpha/Pending/Alpha-A3-Third.md": "---\nstatus: on-main\norder: 3\n---\n# A3: third\n",
        f"{ROOT}/Alpha/Completed/Alpha-A4-Bare.md": "# A4: no frontmatter, filed as done\n",
        f"{ROOT}/Alpha/Completed/Alpha-A5-Skip.md": "---\nstatus: skipped\norder: 5\n---\n# A5\n",
        f"{ROOT}/Alpha/Alpha-A6-Live.md": "---\nstatus: running\nsession_id: live0000-1111\norder: 6\n---\n# A6\n",
        f"{ROOT}/Communications/Comms-C1-Dial.md": "---\nstatus: deployed\norder: 1\n---\n# C1\n",
        f"{ROOT}/Communications/Comms-00-README.md": "# Comms\n\n<!-- status:begin -->\nold\n<!-- status:end -->\n",
        f"{ROOT}/README.md": "# board\n\n<!-- status:begin -->\nold\n<!-- status:end -->\n",
        f"{ROOT}/_moved.json": json.dumps({"docs/Old/Alpha-A1-First.md": f"{ROOT}/Alpha/Alpha-A1-First.md"}, indent=1) + "\n",
    }
    for rel, text in files.items():
        commit_file(r, rel, text, f"add {rel}", when="2020-01-01T00:00:00")
    tracker = tmp_path / "tracker"
    write_sessions(tracker, [session("live0000-1111")])
    cfg = {"prompts": {**spec_of(r, status_folders=True), "board": f"{ROOT}/README.md"}}
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    write_config(tmp_path, cfg)
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    return r


def run(*args):
    return cc.main(["--now", NOW.isoformat(timespec="seconds"), *args])


def by_name(r):
    return {p.name: p for p in pc.scan({"prompts": spec_of(r)})}


def moved(r):
    return json.loads((r / ROOT / "_moved.json").read_text())


# --- reading ---------------------------------------------------------------------------------------------


def test_completed_folder_reads_as_done_unless_frontmatter_closes_it(tree):
    ps = by_name(tree)
    assert ps["Alpha-A4-Bare.md"].status == "done"
    assert ps["Alpha-A5-Skip.md"].status == "skipped"
    assert ps["Alpha-A3-Third.md"].status == "on-main"  # Pending/ overrides nothing
    assert ps["Alpha-A3-Third.md"].module == "alpha" and ps["Comms-C1-Dial.md"].module == "comms"


def test_module_folder_finds_the_existing_folder_by_key_or_alias(tree):
    s = spec_of(tree)
    assert pc.module_folder(s, "alpha") == tree / ROOT / "Alpha"
    assert pc.module_folder(s, "comms") == tree / ROOT / "Communications"
    assert pc.module_folder(s, "nope") is None


def test_status_folder():
    assert [pc.status_folder(s) for s in ("done", "skipped", "superseded")] == ["Completed"] * 3
    assert {pc.status_folder(s) for s in ("pending", "running", "on-main", "deployed", "blocked")} == {"Pending"}


def test_board_finds_readmes_in_capitalized_and_aliased_folders(tree, capsys):
    assert run("--board") == 0
    assert "| 1 | `Comms-C1-Dial.md` |" in (tree / ROOT / "Communications/Comms-00-README.md").read_text()
    assert "[comms](Communications/Comms-00-README.md)" in (tree / ROOT / "README.md").read_text()


# --- place / set ------------------------------------------------------------------------------------------


def test_place_moves_with_git_and_keeps_moved_map_one_hop(tree):
    s = spec_of(tree)
    old = f"{ROOT}/Alpha/Alpha-A1-First.md"
    new, paths = pc.place(s, old, "done")
    assert new == f"{ROOT}/Alpha/Completed/Alpha-A1-First.md" and (tree / new).is_file() and not (tree / old).exists()
    assert old in paths and new in paths and f"{ROOT}/_moved.json" in paths
    m = moved(tree)
    assert m["docs/Old/Alpha-A1-First.md"] == new and m[old] == new  # the older entry is repointed
    back, _ = pc.place(s, new, "pending")
    m = moved(tree)
    assert back == f"{ROOT}/Alpha/Pending/Alpha-A1-First.md"
    assert set(m.values()) == {back} and back not in m  # no chain, no self-map


def test_place_leaves_readmes_legacy_and_already_placed_files_alone(tree):
    s = spec_of(tree)
    for rel, status in ((f"{ROOT}/Alpha/Alpha-00-README.md", "done"), (f"{ROOT}/Alpha/Pending/Alpha-A3-Third.md", "on-main"),
                        ("docs/Elsewhere.md", "done")):
        assert pc.place(s, rel, status) == (rel, [])


def test_place_refuses_to_overwrite(tree):
    commit_file(tree, f"{ROOT}/Alpha/Completed/Alpha-A1-First.md", "# other\n", "clash")
    with pytest.raises(OSError):
        pc.place(spec_of(tree), f"{ROOT}/Alpha/Alpha-A1-First.md", "done")


def test_set_done_moves_into_completed_and_commits_every_path(tree, capsys):
    assert run("set", "alpha/A1", "done", "--commit", "abc1234") == 0
    new = tree / ROOT / "Alpha/Completed/Alpha-A1-First.md"
    assert "status: done\n" in new.read_text()
    assert git(tree, "status", "--porcelain") == ""
    assert git(tree, "log", "-1", "--format=%s") == "docs(prompts): alpha A1 → done"
    assert "| 1 | `Alpha-A1-First.md` | 🟢 done |" in (tree / ROOT / "Alpha/Alpha-00-README.md").read_text()


def test_set_takes_the_module_as_the_folder_names_it(tree, capsys):
    """`Alpha/A3` (the folder's case) and `Communications/C1` (an aliased folder) find their prompts."""
    assert run("set", "Alpha/A3", "deployed", "--no-commit") == 0
    assert "status: deployed\n" in (tree / ROOT / "Alpha/Pending/Alpha-A3-Third.md").read_text()
    assert run("set", "Communications/C1", "done", "--no-commit") == 0
    assert "status: done\n" in (tree / ROOT / "Communications/Completed/Comms-C1-Dial.md").read_text()


def test_set_back_to_pending_moves_out_of_completed(tree, capsys):
    assert run("set", "alpha/A4", "pending") == 0
    assert (tree / ROOT / "Alpha/Pending/Alpha-A4-Bare.md").is_file()
    assert by_name(tree)["Alpha-A4-Bare.md"].status == "pending"
    assert git(tree, "status", "--porcelain") == ""


def test_set_without_status_folders_does_not_move(tree, tmp_path, capsys):
    write_config(tmp_path, {"prompts": spec_of(tree)})
    assert run("set", "alpha/A1", "done") == 0
    assert (tree / ROOT / "Alpha/Alpha-A1-First.md").is_file()


# --- sort -------------------------------------------------------------------------------------------------


def test_sort_dry_run_changes_nothing(tree, capsys):
    assert run("sort", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "Alpha-A1-First.md → docs/Prompts/Alpha/Pending/" in out and "Alpha-A2-Second.md → docs/Prompts/Alpha/Completed/" in out
    assert git(tree, "status", "--porcelain") == ""


def test_sort_places_everything_but_a_live_running_prompt(tree, capsys):
    assert run("sort") == 0
    out = capsys.readouterr().out
    a = tree / ROOT / "Alpha"
    assert (a / "Pending/Alpha-A1-First.md").is_file() and (a / "Completed/Alpha-A2-Second.md").is_file()
    assert (a / "Alpha-A6-Live.md").is_file() and "skipped Alpha-A6-Live.md (running, live0000)" in out
    assert (tree / ROOT / "Communications/Pending/Comms-C1-Dial.md").is_file()
    assert (a / "Alpha-00-README.md").is_file()
    assert git(tree, "status", "--porcelain") == ""
    assert git(tree, "log", "-1", "--format=%s").startswith("docs(prompts): sort")


# --- gate + json --------------------------------------------------------------------------------------------


def test_gate_follows_a_moved_prompt(tree, tmp_path):
    rel = f"{ROOT}/Alpha/Alpha-A1-First.md"
    cfg = {"prompts": {**spec_of(tree), "gate": True, "claim_verbs": ["run"]}}
    write_config(tmp_path, cfg)
    sid = "gate0000-0000"
    st.track("UserPromptSubmit", {"session_id": sid, "cwd": "/x", "prompt": f"Run {rel}", "hook_event_name": "UserPromptSubmit"})
    s = st.load_session(sid)
    s["commits"] = [{"sha": commit_file(tree, "docs/n.md", "x", "n"), "repo": st.repo_id(str(tree))}]
    st.write_session(s)
    pc.place(cfg["prompts"], rel, "running")  # moved to Pending/ while still running for this session
    out = st.stop_gate({"session_id": sid, "stop_hook_active": False})
    assert out and out["decision"] == "block" and "Pending/Alpha-A1-First.md" in out["reason"]


def test_json_carries_updated_and_session_liveness(tree, capsys):
    p = tree / ROOT / "Alpha/Alpha-A1-First.md"
    p.write_text("---\nstatus: running\nsession_id: gone0000-0000\nupdated: \"2020-01-02T00:00:00+00:00\"\n---\n# A1\n")
    p2 = tree / ROOT / "Alpha/Pending/Alpha-A3-Third.md"
    p2.write_text("---\nstatus: blocked\nsession_id: live0000-1111\n---\n# A3\n")
    assert run("--json") == 0
    data = json.loads(capsys.readouterr().out)
    stalled = {e["id"]: e for e in data["stalled"]}
    assert stalled["A1"]["updated"] == "2020-01-02T00:00:00+00:00" and stalled["A1"]["session_live"] is False
    blocked = {e["id"]: e for e in data["blocked"]}
    assert blocked["A3"]["session_id"] == "live0000-1111" and blocked["A3"]["session_live"] is True


def test_migrate_files_into_the_existing_capitalized_folder(tree, tmp_path):
    import prompt_migrate

    commit_file(tree, "docs/Old-2026-10-26/Alpha-A9-Legacy.md", "# A9\n", "legacy")
    s = {**spec_of(tree), "legacy_globs": ["docs/Old-*"]}
    [mv] = prompt_migrate.plan_moves(s, None)
    assert mv.new == tree / ROOT / "Alpha" / "Alpha-A9-Legacy.md"
