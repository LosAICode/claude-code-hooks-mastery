import json
from datetime import datetime
import os

import pytest

import prompt_center as pc
import prompt_migrate as pm
from conftest import git
from test_prompt_join import session, write_sessions

FUNNELS_README = """# Funnels backlog B4-B12: session prompts (written 2026-10-28)

One prompt = one fresh session. Paste the prompt file's path, e.g. "Run `docs/Old-2026-10-28/Funnels-B4-Publish-Wiring-Guarantee.md`".

## Order (and why)
| # | File | Needs owner | Notes |
|---|---|---|---|
| 1 | ✅ `Funnels-B4-Publish-Wiring-Guarantee.md` | no | ✅ DEPLOYED 10-28 19:23 (main `ef51dbda4`), live proof PASS. |
| 2 | ✅ `Funnels-B5-Public-Chatbot-Phones.md` | PSL = owner | 🔵 ON MAIN `032667a02` 10-30, deploy HELD by owner. |
| 3 | `Funnels-B12-4-Webinar-Honesty.md` | no | Remove fake `spots_remaining`. |
| 4 | `Funnels-B7-Refuted.md` | no | refuted by A00: nothing to do |

## Rules for every Funnels session
- **Handoff:** the doc's §7 row, and this README's table (mark the row done).
"""

ENGINE_README = """# Engine prompts
| Order | Prompt | Item | Size | Can start |
|---|---|---|---|---|
| 1 | `Engine-F3-Land-Planner-Cap.md` | F3 planner cap | ✅ DONE: landed + deployed 10-28 (main `8a4b62e2c`); do not run | — |
"""


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    files = {
        "docs/Old-2026-10-26/00-README.md": "# batch 10-26\n| 01 | `01-Sock-PDF-Security-Land.md` | sec |\n",
        "docs/Old-2026-10-26/01-Sock-PDF-Security-Land.md": "# Sock\n\nbody ---\nkeep `docs/Old-2026-10-26/x.md` mention\n",
        "docs/Old-2026-10-27/F3-Planner-Cap.md": "# F3 old\n",
        "docs/Old-2026-10-28/Funnels-00-README.md": FUNNELS_README,
        "docs/Old-2026-10-28/Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md":
            "# B4: publish wiring\n\nPaste: `Run docs/Old-2026-10-28/Funnels-B4-Publish-Wiring-Guarantee.md`\n\nbody\n",
        "docs/Old-2026-10-28/Funnels-B5-Public-Chatbot-Phones-10-28.md": "# B5\n",
        "docs/Old-2026-10-28/Funnels-B12-4-Webinar-Honesty.md": "# B12-4\n",
        "docs/Old-2026-10-28/Funnels-B7-Refuted.md": "# B7\n",
        "docs/Old-2026-10-28/Funnels-B9-No-Row.md": "---\nstatus: done\n---\n# B9 already has frontmatter\n",
        "docs/Old-2026-10-28/Engine-00-README.md": ENGINE_README,
        "docs/Old-2026-10-28/Engine-F3-Land-Planner-Cap.md": "# F3 new\n",
        "docs/Old-2026-10-30/Security-Audit-00-README.md": "# Security audit index\n",
        "docs/Old-2026-10-30/Security-S1-Logins.md": "# S1\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(tracker))
    cfg = {"prompts": {"repo": str(root), "root": "docs/Prompts", "legacy_globs": ["docs/Old-*"],
                       "board": "docs/Prompts/README.md",
                       "module_aliases": {"Sock": "security", "F3": "engine", "Security-Audit": "security"}}}
    return root, cfg, tracker


def test_dry_run_moves_nothing(legacy, capsys):
    root, cfg, _ = legacy
    assert pm.main(cfg, None, dry_run=True, force=False) == 0
    out = capsys.readouterr().out
    assert "git mv docs/Old-2026-10-28/Funnels-B12-4-Webinar-Honesty.md docs/Prompts/funnels/Funnels-B12-4-Webinar-Honesty.md" in out
    assert "docs/Old-2026-10-26/00-README.md docs/Prompts/_batches/2026-10-26-00-README.md" in out
    assert not (root / "docs/Prompts").exists()
    assert git(root, "status", "--porcelain") == ""


def test_apply_moves_and_annotates(legacy):
    root, cfg, _ = legacy
    assert pm.main(cfg, None, dry_run=False, force=False) == 0
    assert (root / "docs/Prompts/funnels/Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md").is_file()
    assert (root / "docs/Prompts/security/01-Sock-PDF-Security-Land.md").is_file()
    assert (root / "docs/Prompts/security/Security-Audit-00-README.md").is_file()
    assert (root / "docs/Prompts/engine/F3-Planner-Cap.md").is_file()
    assert (root / "docs/Prompts/_batches/2026-10-26-00-README.md").is_file()
    status = git(root, "status", "--porcelain")
    assert "docs/Old-2026-10-28/Funnels-B12-4-Webinar-Honesty.md -> docs/Prompts/funnels/Funnels-B12-4-Webinar-Honesty.md" in status
    moved = json.loads((root / "docs/Prompts/_moved.json").read_text())
    assert moved["docs/Old-2026-10-28/Funnels-B5-Public-Chatbot-Phones-10-28.md"] == "docs/Prompts/funnels/Funnels-B5-Public-Chatbot-Phones-10-28.md"

    def meta(rel):
        return pc.parse_frontmatter((root / rel).read_text())[0]

    b4 = meta("docs/Prompts/funnels/Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md")
    assert (b4["module"], b4["id"], b4["order"], b4["status"], b4["commit"], b4["needs_owner"]) == ("funnels", "B4", "1", "deployed", "ef51dbda4", "no")
    assert "todo" not in b4
    b5 = meta("docs/Prompts/funnels/Funnels-B5-Public-Chatbot-Phones-10-28.md")
    assert (b5["status"], b5["commit"], b5["needs_owner"]) == ("on-main", "032667a02", "PSL = owner")
    assert meta("docs/Prompts/funnels/Funnels-B12-4-Webinar-Honesty.md")["status"] == "pending"
    assert meta("docs/Prompts/funnels/Funnels-B7-Refuted.md")["status"] == "skipped"
    b9 = meta("docs/Prompts/funnels/Funnels-B9-No-Row.md")
    assert b9 == {"status": "done"}                       # had frontmatter: untouched
    f3_old = meta("docs/Prompts/engine/F3-Planner-Cap.md")
    assert (f3_old["status"], f3_old["todo"]) == ("superseded", "confirm")
    assert meta("docs/Prompts/engine/Engine-F3-Land-Planner-Cap.md")["status"] == "done"
    sock = meta("docs/Prompts/security/01-Sock-PDF-Security-Land.md")
    assert (sock["status"], sock["todo"], sock["id"]) == ("pending", "confirm", "01")
    s1 = meta("docs/Prompts/security/Security-S1-Logins.md")
    assert (s1["status"], s1["todo"]) == ("pending", "confirm")


def test_apply_rewrites_only_paste_and_example_lines(legacy):
    root, cfg, _ = legacy
    pm.main(cfg, None, dry_run=False, force=False)
    b4 = (root / "docs/Prompts/funnels/Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md").read_text()
    assert "Paste: `Run docs/Prompts/funnels/Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md`" in b4
    assert b4.endswith("\nbody\n")
    sock = (root / "docs/Prompts/security/01-Sock-PDF-Security-Land.md").read_text()
    assert sock.endswith("# Sock\n\nbody ---\nkeep `docs/Old-2026-10-26/x.md` mention\n")   # body untouched
    readme = (root / "docs/Prompts/funnels/Funnels-00-README.md").read_text()
    assert 'e.g. "Run `docs/Prompts/funnels/Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md`"' in readme
    assert "<!-- status:begin -->" in readme and readme.index("<!-- status:end -->") < readme.index("## Order (frozen")
    assert "| 1 | ✅ `Funnels-B4-Publish-Wiring-Guarantee.md` | no |" in readme      # old table kept
    assert "| 1 | `Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md` | 🟡 deployed | no | `ef51dbda4` |" in readme
    assert "mark the row done" not in readme and "command_center.py set" in readme
    stub = (root / "docs/Old-2026-10-28/README.md").read_text()
    assert "docs/Prompts/<module>/" in stub and "_moved.json" in stub
    assert sorted(os.listdir(root / "docs/Old-2026-10-28")) == ["README.md"]
    board = (root / "docs/Prompts/README.md").read_text()
    assert "<!-- status:begin -->" in board and "| [funnels](funnels/Funnels-00-README.md) |" in board


def test_second_run_changes_nothing(legacy, capsys):
    root, cfg, _ = legacy
    pm.main(cfg, None, dry_run=False, force=False)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "migrated")
    assert pm.main(cfg, None, dry_run=False, force=False) == 0
    assert git(root, "status", "--porcelain") == ""
    assert "nothing to migrate" in capsys.readouterr().out


def test_refuses_dirty_tree_and_live_session(legacy, capsys):
    root, cfg, tracker = legacy
    (root / "docs/Old-2026-10-28/Funnels-B12-4-Webinar-Honesty.md").write_text("# edited\n")
    assert pm.main(cfg, None, dry_run=False, force=False) == 1
    err = capsys.readouterr().err
    assert "uncommitted" in err and "Funnels-B12-4" in err
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "edit")
    seen_now = datetime.now().astimezone().isoformat(timespec="seconds")  # migrate checks liveness on the wall clock
    write_sessions(tracker, [session("c5588aac-1", task="Run docs/Old-2026-10-28/Funnels-B12-4-Webinar-Honesty.md",
                                     last_seen=seen_now)])
    assert pm.main(cfg, "funnels", dry_run=False, force=False) == 1
    assert "c5588aac" in capsys.readouterr().err
    assert pm.main(cfg, "engine", dry_run=False, force=False) == 0          # another module moves fine
    assert (root / "docs/Prompts/engine/Engine-F3-Land-Planner-Cap.md").is_file()
    assert (root / "docs/Old-2026-10-28/Funnels-B12-4-Webinar-Honesty.md").is_file()
    assert pm.main(cfg, "funnels", dry_run=False, force=True) == 0


def test_refuses_when_sync_lock_present(legacy, tmp_path, capsys):
    root, cfg, _ = legacy
    lock = tmp_path / "sync.lock"
    lock.write_text("123")
    cfg["prompts"]["sync_lock"] = str(lock)
    assert pm.main(cfg, None, dry_run=False, force=False) == 1
    assert "sync" in capsys.readouterr().err


@pytest.mark.parametrize("row,expected", [
    ("✅ DEPLOYED 10-28 19:23 (main `ef51dbda4`), live proof PASS. `fnPublishOutcomeText` is on main (B6 unblocked)", ("deployed", "ef51dbda4")),
    ("After B12 scoping: the full suite once, every new failure attributed vs `e95be494c`, then the held B8 + B10 deploy", ("pending", None)),
    ("🔵 ON MAIN `4ee8c9392` 10-30, deploy HELD for B13", ("on-main", "4ee8c9392")),
    ("🔵 ON MAIN `032667a02` 10-30, deploy HELD by owner", ("on-main", "032667a02")),
    ("✅ LIVE since the 10-29 22:10 web restart (`4ca40f2ac`)", ("deployed", "4ca40f2ac")),
    ("✅ DONE 10-30: verdicts + new findings", ("done", None)),
    ("✅ `fd5fcb902` 2026-10-29, DEPLOYED + live-proven", ("deployed", "fd5fcb902")),
    ("✅ on main `01071ccc7` 2026-10-29, not deployed", ("on-main", "01071ccc7")),
    ("✅ 2026-10-29: `Documentation/Roadmap.md` (13 ranked)", ("done", None)),
    ("refuted by A00", ("skipped", None)),
    ("Per-step conversions, CSV. After 9b.", ("pending", None)),
    ("", ("pending", None)),
])
def test_infer_status(row, expected):
    assert pm.infer_status(row) == expected
