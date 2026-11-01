"""CC8: every session tracked: one Sessions list, claims that can't miss, the unclaimed hint, a claim gate, Parked,
`set --commit` ×2 / `--proof`, migrations prod already has, reference files and case-twin folders."""
# ruff: noqa: F811  (the tests take fixtures imported by name)
import json
import os
from pathlib import Path

import pytest

import prompt_center as pc
import session_tracker as st
from conftest import commit_file, git, write_config
from test_command_center import center, run  # noqa: F401  (center is a fixture)
from test_command_center_cc7 import _hook_bash, hooked  # noqa: F401  (hooked is a fixture)
from test_command_center_plan import plan  # noqa: F401  (plan is a fixture)
from test_command_center_plan import run as plan_run
from test_prompt_join import session, write_sessions
from test_session_tracker import _run_hook

A04 = "Documentation/Prompts/Assistants/Pending/Assistants-A04-Integrations-Everywhere-Attachments.md"
A04_TEXT = "run Documentation/Prompts/Assistants/Pending/Assistants-A04-Integrations-Everywhere-Attach\n  ments.md"
SID = "cc8cc8cc-0000-0000-0000-000000000001"


def meta_of(path):
    return pc.parse_frontmatter(Path(path).read_text())[0]


# --- 2. claims that can't miss -----------------------------------------------------------------------

@pytest.fixture
def claims(tmp_path, tracker_env):
    """A git repo with the A04 prompt (pending), a second prompt, and the gate on."""
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, A04, "---\nmodule: assistants\nid: A04\nstatus: pending\n---\n# A04\n", "a04", when="2026-10-31T08:00:00")
    commit_file(r, "Documentation/Prompts/Assistants/Pending/Assistants-A06-List-Workspace-Save-Honesty.md",
                "---\nmodule: assistants\nid: A06\nstatus: pending\n---\n# A06\n", "a06", when="2026-10-31T08:00:01")
    commit_file(r, "Documentation/Prompts/Assistants/Pending/Assistants-A07-Integrations-Everywhere-Bis.md",
                "---\nmodule: assistants\nid: A07\nstatus: pending\n---\n# A07\n", "a07", when="2026-10-31T08:00:02")
    write_config(tmp_path, {"prompts": {"repo": str(r), "root": "Documentation/Prompts", "gate": True,
                                        "commit_claims": True, "claim_verbs": ["run", "execute", "start"]}})
    return r


def say(text, sid=SID):
    st.track("UserPromptSubmit", {"session_id": sid, "cwd": "/elsewhere", "prompt": text})
    return st.load_session(sid)


def test_a_path_wrapped_across_a_newline_claims(claims):
    cfg = st.load_config()
    assert pc.resolve_prompt(A04_TEXT, cfg, pc.scan(cfg)) == A04
    assert pc.claims_prompt(A04_TEXT, ["run"])
    # wrapped early in the name: only the rejoin finds it (no token is a 20-character prefix)
    early = "run Documentation/Prompts/Assistants/Pending/Assist\n  ants-A04-Integrations-Everywhere-Attachments.md"
    assert pc.resolve_prompt(early, cfg, pc.scan(cfg)) == A04
    s = say(A04_TEXT)  # the 33a7199c first message exactly
    assert s["prompt_file"] == A04 and s["prompt_claimed"] is True
    assert meta_of(claims / A04)["status"] == "running" and meta_of(claims / A04)["session_id"] == SID


def test_a_wrapped_line_never_glues_two_real_paths(claims):
    cfg = st.load_config()
    text = "run Documentation/Prompts/Assistants/Pending/Assistants-A06-List-Workspace-Save-Honesty.md\nAssistants-A04.md"
    assert pc.resolve_prompt(text, cfg, pc.scan(cfg)).endswith("Assistants-A06-List-Workspace-Save-Honesty.md")


def test_a_unique_prefix_of_a_prompt_file_name_resolves(claims):
    cfg = st.load_config()
    index = pc.scan(cfg)
    assert pc.resolve_prompt("run Documentation/Prompts/Assistants/Pending/Assistants-A04-Integrations-Every", cfg, index) == A04
    assert pc.resolve_prompt("run Assistants-A06-List-Workspace.md", cfg, index).endswith("A06-List-Workspace-Save-Honesty.md")
    # shared by A04 and A07: ambiguous; under 20 characters: too short
    assert pc.resolve_prompt("run Documentation/Prompts/Assistants/Pending/Assistants-A0", cfg, index) is None
    assert pc.resolve_prompt("run x/Assistants-A04-Int.md", cfg, index) is None


def test_a_later_message_claims_when_the_session_holds_no_prompt(claims):
    s = say("/cc")
    assert not s.get("prompt_file") and s["task"]
    s = say("why is A04 stalled? see Documentation/Prompts/Assistants/Pending/Assistants-A04-Integrations-Everywhere-Attachments.md")
    assert not s.get("prompt_file") and s["prompt_mentioned"] == A04  # a question names it, never claims
    s = say(f"ok, run {A04}")
    assert s["prompt_file"] == A04 and s["prompt_claimed"] is True and s["task"] == "/cc"
    assert meta_of(claims / A04)["status"] == "running"


def test_a_session_that_holds_a_prompt_is_never_re_pointed(claims):
    a06 = "Documentation/Prompts/Assistants/Pending/Assistants-A06-List-Workspace-Save-Honesty.md"
    say(f"run {a06}")
    s = say(f"run {A04}")
    assert s["prompt_file"] == a06
    assert meta_of(claims / A04)["status"] == "pending"


# --- 4. the claim gate ---------------------------------------------------------------------------------

def _stop(env, cwd, sid=None):
    from test_command_center_cc7 import SID as HOOK_SID

    r = _run_hook("stop.py", {"session_id": sid or HOOK_SID, "cwd": str(cwd), "stop_hook_active": False}, env, cwd)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def _say_hook(env, cwd, text):
    from test_command_center_cc7 import SID as HOOK_SID

    data = {"session_id": HOOK_SID, "cwd": str(cwd), "prompt": text, "hook_event_name": "UserPromptSubmit"}
    assert _run_hook("user_prompt_submit.py", data, env, cwd, "--log-only").returncode == 0


def test_claim_gate_asks_once_after_a_code_commit_with_no_prompt(hooked):
    r, env, cwd = hooked
    _say_hook(env, cwd, "fix the typo in modules/a.py")
    _hook_bash(env, cwd, f"git -C {r} commit -m 'fix: a'", "t1", lambda: commit_file(r, "modules/a.py", "a", "fix: a"))
    out = _stop(env, cwd)
    assert out and out["decision"] == "block"
    assert "claim <module>/<ID> --session cc7cc7cc" in out["reason"]
    assert "Documentation/Prompts/README.md" in out["reason"] and "command_center.py" in out["reason"]
    assert _stop(env, cwd) is None  # once


def test_claim_gate_names_the_prompt_the_session_mentioned(hooked):
    r, env, cwd = hooked
    _say_hook(env, cwd, "look at docs/Prompts/ops/Ops-CC9-Thing.md")
    _hook_bash(env, cwd, f"git -C {r} commit -m 'fix: a'", "t1", lambda: commit_file(r, "modules/a.py", "a", "fix: a"))
    assert "claim Ops-CC9-Thing.md --session cc7cc7cc" in _stop(env, cwd)["reason"]


def test_claim_gate_never_asks_a_session_that_only_viewed(hooked):
    r, env, cwd = hooked
    _say_hook(env, cwd, "/cc")
    _hook_bash(env, cwd, "python3 scripts/command_center.py", "t1", lambda: None)
    assert _stop(env, cwd) is None


def test_claim_gate_leaves_a_claimed_session_to_the_other_gates(hooked):
    r, env, cwd = hooked
    _say_hook(env, cwd, "run docs/Prompts/ops/Ops-CC9-Thing.md")
    _hook_bash(env, cwd, f"git -C {r} commit -m 'fix: a'", "t1", lambda: commit_file(r, "modules/a.py", "a", "fix: a"))
    out = _stop(env, cwd)
    assert "--session" not in out["reason"]  # the status gate asks, not the claim gate


# --- 1, 3, 5, 6. one Sessions list, the unclaimed hint, waiting first, Parked --------------------------

@pytest.fixture
def busy(center):
    root = center / "docs/Prompts/funnels"
    (root / "Funnels-B13-Branch.md").write_text(
        "---\nstatus: on-branch\nprogress: 2/5\ncommits: [\"abcdef123\"]\nupdated: 2026-10-27T09:00:00-04:00\n---\n# B13: branch work\n")
    (root / "Funnels-B14-Parked.md").write_text(
        "---\nstatus: on-branch\ncommits: [\"1234567ab\", \"fedcba987\"]\nupdated: 2026-10-28T09:00:00-04:00\n---\n# B14: parked\n")
    (root / "Funnels-B15-Fresh.md").write_text(
        "---\nstatus: on-branch\ncommits: [\"aaaa11111\"]\nupdated: 2026-10-30T15:00:00-04:00\n---\n# B15: fresh\n")
    write_sessions(center.parent / "tracker", [
        session("a6a6a6a6-3333", seen=1, prompt_file="docs/Prompts/funnels/Funnels-B13-Branch.md",
                worktree="/w/b13", branch="b13", last_commit="abcdef123 feat: b13"),
        session("f2f2f2f2-4444", state="waiting_on_owner", seen=2, task="look at the logs",
                waiting_reason="Bash: run the sandbox"),
        session("a4a4a4a4-5555", seen=3, task="look at docs/Prompts/funnels/Funnels-B12-4-Webinar.md",
                prompt_mentioned="docs/Prompts/funnels/Funnels-B12-4-Webinar.md"),
        session("1d1e1d1e-6666", state="idle", seen=4, task="/cc"),
        session("ended000-7777", state="ended", prompt_file="docs/Prompts/funnels/Funnels-B11-Perf.md"),
    ])
    return center


def test_one_sessions_list_every_live_session_waiting_first(busy, capsys):
    assert run() == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == ("Prompt command center · 2026-10-30 16:00 · 2 running · 1 stalled · 1 blocked on owner"
                                   " · 6 live · 3 working · 2 waiting on you · 1 idle · 3 without a prompt")
    assert "Running now" not in out and "Other live sessions" not in out
    block = out.split("\nSessions\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert [line.split()[0] for line in block] == ["f2f2f2f2", "c5588aac", "a6a6a6a6", "a4a4a4a4", "1d1e1d1e", "d60450f0"]
    rows = {line.split()[0]: line for line in block}
    assert "WAITING ON YOU" in rows["f2f2f2f2"] and "waits on: Bash: run the sandbox" in rows["f2f2f2f2"]
    assert "no prompt" in rows["f2f2f2f2"]
    assert "B13" in rows["a6a6a6a6"] and "on-branch 2/5" in rows["a6a6a6a6"] and "@b13" in rows["a6a6a6a6"]
    assert "abcdef123 feat: b13" in rows["a6a6a6a6"]
    assert ("unclaimed: funnels/B12-4 → command_center.py claim funnels/B12-4 --session a4a4a4a4") in rows["a4a4a4a4"]
    assert "idle" in rows["1d1e1d1e"] and "no prompt" in rows["1d1e1d1e"]
    assert "B4" in rows["d60450f0"] and "running" in rows["d60450f0"]
    assert "ended000" not in out
    assert "1 active (a6a6a6a6, 1m)" in next(line for line in out.splitlines() if line.startswith("funnels"))


def test_sessions_in_json_and_html(busy, capsys):
    assert run("--json") == 0
    data = json.loads(capsys.readouterr().out)
    rows = data["sessions"]
    assert [r["sid8"] for r in rows] == ["f2f2f2f2", "c5588aac", "a6a6a6a6", "a4a4a4a4", "1d1e1d1e", "d60450f0"]
    assert rows[0]["waiting_on"] == "Bash: run the sandbox" and rows[0]["prompt"] is None
    assert rows[2]["prompt"]["id"] == "B13" and rows[2]["prompt"]["status"] == "on-branch"
    assert rows[3]["unclaimed"]["claim"] == "command_center.py claim funnels/B12-4 --session a4a4a4a4"
    assert data["session_counts"] == {"live": 6, "working": 3, "waiting": 2, "idle": 1, "no_prompt": 3}
    assert [p["id"] for p in data["parked"]] == ["B14"]
    assert run("--html") == 0
    page = Path(capsys.readouterr().out.strip().splitlines()[-1]).read_text()
    card = page.split("<h2>Sessions", 1)[1].split("</section>", 1)[0]
    assert "f2f2f2f2" in card and "unclaimed" in card and "Parked" in page
    assert "ended000" not in card  # (the B11 row's Session column still names its last run)


def test_parked_lists_old_on_branch_prompts_nobody_holds(busy, capsys):
    assert run() == 0
    out = capsys.readouterr().out
    parked = out.split("\nParked", 1)[1].split("\n\n", 1)[0]
    assert "B14" in parked and "fedcba987" in parked and "2d" in parked
    assert "B13" not in parked, "a live session holds it"
    assert "B15" not in parked, "moved an hour ago"


# --- 7. set --commit ×2, --proof ----------------------------------------------------------------------------

def test_set_keeps_every_commit_and_writes_proof(center, capsys):
    path = center / "docs/Prompts/funnels/Funnels-B12-4-Webinar.md"
    assert run("set", "funnels/B12-4", "on-branch", "--commit", "aaa111222", "--commit", "bbb333444",
               "--proof", "probe 320 sees 403", "--no-commit") == 0
    m = meta_of(path)
    assert m["commits"] == ["aaa111222", "bbb333444"] and m["proof"] == "probe 320 sees 403"


# --- 8. migrations prod already has ----------------------------------------------------------------------

def _with_revisions(plan, cmd):
    versions = plan / "modules/migrations/versions"
    (versions / "r9_add_col.py").write_text('"""r9"""\nrevision = "r9_add_col"\ndown_revision = "r8_base"\n')
    (versions / "r8_base.py").write_text("revision: str = 'r8_base'\ndown_revision: Union[str, None] = None\n")
    cfg_path = Path(os.environ["SESSION_TRACKER_CONFIG"])
    c = json.loads(cfg_path.read_text())
    c["prompts"]["prod_revision"] = cmd
    cfg_path.write_text(json.dumps(c))


def test_deploy_plan_drops_migrations_prod_already_has(plan, capsys):
    _with_revisions(plan, "echo r9_add_col")
    assert plan_run("deploy-plan") == 0
    text = capsys.readouterr().out
    todo = text.split("Migrations (apply BY HAND", 1)[1].split("\n\n", 1)[0]
    assert "  none" in todo and "already in prod (alembic_version r9_add_col): modules/migrations/versions/r9_add_col.py" in todo
    assert plan_run("deploy-plan", "--json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["prod_revision"] == "r9_add_col" and data["migrations"][0]["in_prod"] is True


def test_deploy_plan_keeps_the_list_when_alembic_version_is_unread(plan, capsys):
    _with_revisions(plan, "exit 2")
    assert plan_run("deploy-plan") == 0
    text = capsys.readouterr().out
    assert "alembic_version unread" in text
    todo = text.split("Migrations (apply BY HAND", 1)[1].split("\n\n", 1)[0]
    assert "modules/migrations/versions/r9_add_col.py  (agents C02" in todo


def test_applied_revisions_walks_merges():
    import prompt_plan as pp

    files = {"a.py": "revision = 'a'\ndown_revision = None\n", "b.py": "revision = 'b'\ndown_revision = 'a'\n",
             "c.py": "revision = 'c'\ndown_revision = 'a'\n", "m.py": "revision = 'm'\ndown_revision = ('b', 'c')\n",
             "n.py": "revision = 'n'\ndown_revision = 'm'\n"}
    assert pp.applied_revisions(files, "m") == {"a", "b", "c", "m"}
    assert pp.applied_revisions(files, "zz") == set()


# --- 9. not-a-prompt files ------------------------------------------------------------------------------

def test_reference_files_are_skipped_and_case_twin_folders_flagged(center, capsys):
    sec = center / "docs/Prompts/Security/Pending"
    sec.mkdir(parents=True)
    (sec / "Security-Audit-2026-10-30.md").write_text("---\nkind: reference\n---\n# The audit report\n")
    (sec / "Security-Report-Notes.md").write_text("# Notes\n\nNo task list here.\n")
    (center / "docs/Prompts/Funnels").mkdir()
    (center / "docs/Prompts/Funnels/Funnels-B99-X.md").write_text("---\nid: B99\nstatus: pending\n---\n# B99\n")
    names = [p.name for p in pc.scan(st.load_config())]
    assert "Security-Audit-2026-10-30.md" not in names
    assert run("audit") == 1
    out = capsys.readouterr().out
    assert "reference: docs/Prompts/Security/Pending/Security-Audit-2026-10-30.md" in out
    assert "Security-Report-Notes.md: no id and no ## Do: a reference file? add `kind: reference`" in out
    assert "docs/Prompts/funnels/ and docs/Prompts/Funnels/ differ only by case" in out
    assert (sec / "Security-Audit-2026-10-30.md").exists() and (sec / "Security-Report-Notes.md").exists()
