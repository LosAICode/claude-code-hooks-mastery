import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".claude" / "hooks" / "utils"))
sys.path.insert(0, str(ROOT / "scripts"))


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
