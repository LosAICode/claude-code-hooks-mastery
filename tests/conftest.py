import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".claude" / "hooks" / "utils"))
sys.path.insert(0, str(ROOT / "scripts"))


# Every test runs in one known zone: the one the run was started with (``TZ=America/New_York pytest …``), else UTC
# (CC11: at 01:02 EST, the repeated DST hour, three deploy-run tests failed on an unchanged main). Set at import, so
# the test modules' own module-level times (``NOW_DT``) are read in it too.
os.environ["TZ"] = os.environ.get("TZ") or "UTC"
time.tzset()
PINNED_TZ = os.environ["TZ"]


@pytest.fixture(autouse=True)
def pinned_tz():
    """Back to the pinned zone after a test that switched it (``use_tz``)."""
    old = PINNED_TZ
    yield
    os.environ["TZ"] = old
    time.tzset()


def use_tz(monkeypatch, name: str) -> None:
    """Switch this test (and the processes it starts) to ``name``; the pin restores the zone after it."""
    monkeypatch.setenv("TZ", name)
    time.tzset()


@pytest.fixture
def tracker_env(tmp_path, monkeypatch):
    """A private tracker dir + config for one test."""
    d = tmp_path / "tracker"
    monkeypatch.setenv("SESSION_TRACKER_DIR", str(d))
    monkeypatch.setenv("SESSION_TRACKER_CONFIG", str(tmp_path / "config.json"))
    return d


def write_config(tmp_path: Path, cfg: dict) -> None:
    (tmp_path / "config.json").write_text(json.dumps(cfg))


def git(repo: Path, *args: str, env: dict | None = None) -> str:
    full_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", **(env or {})}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=full_env).stdout.strip()


def commit_file(repo: Path, rel: str, text: str, msg: str, when: str | None = None) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(repo, "add", rel)
    env = {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else None
    git(repo, "commit", "-q", "-m", msg, env=env)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_file(r, "README.md", "x", "init", when="2020-01-01T00:00:00")
    commit_file(r, "docs/Board.md", "board v1", "board v1", when="2020-01-01T00:00:01")
    return r


@pytest.fixture
def proj(tmp_path):
    """A prompts tree: a root with module folders plus two legacy folders (shared by the prompt tests)."""
    root = tmp_path / "proj"
    for rel, text in {
        "docs/Prompts/funnels/Funnels-00-README.md": "# index\n",
        "docs/Prompts/funnels/Funnels-B12-3-Step-Analytics-CSV.md": "---\nstatus: on-main\norder: 9c\n---\n# B12-3: per-step conversions\n",
        "docs/Prompts/funnels/Funnels-B4-Publish-Wiring-Guarantee-Sent - 10-28.md": "# B4 publish wiring\n",
        "docs/Prompts/history/History-H8b-Paid-Paths-Without-Rows.md": "# H8b\n",
        "docs/Prompts/README.md": "board\n",
        "docs/Prompts/_moved.json": "{}",
        "docs/Prompts/_batches/2026-10-26-00-README.md": "# batch\n",
        "docs/Old-2026-10-26/01-Sock-PDF-Security-Land.md": "# Sock\n",
        "docs/Old-2026-10-27/F3-Planner-Cap.md": "# F3\n",
        "docs/Old-2026-10-27/Comms-3-Dialer-Walk.md": "# Comms 3\n",
        "docs/Old-2026-10-28/Websites-List-Server-Time.md": "# Websites list\n",
        "docs/Old-2026-10-28/Security-Audit-00-README.md": "# sec index\n",
        "docs/Old-2026-10-28/History-H8b-Paid-Paths-Without-Rows.md": "# OLD COPY\n",
        "docs/Old-2026-10-28/Uddd2-05-Booking-Add-Step.md": "# odd\n",
        "docs/Old-2026-10-26/02-Org-Roles-P2-Finish.md": "# org roles\n",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def cfg(root):
    """A tracker config whose prompts block points at that tree."""
    return {"prompts": {"repo": str(root), "root": "docs/Prompts", "legacy_globs": ["docs/Old-*"],
                        "module_aliases": {"Sock": "security", "F3": "engine", "Security-Audit": "security", "Org-Roles": "org-roles",
                                           "Uddd2": "misc"}}}
