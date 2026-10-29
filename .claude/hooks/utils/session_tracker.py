"""Live session list + Status Board stop gate, fed by the Claude Code hooks.

Each hook calls ``track(event, payload)`` once. The tracker keeps one JSON file per session in
``~/.claude/session-tracker/<session_id>.json`` (override: ``SESSION_TRACKER_DIR``), written atomically.
``scripts/sessions.py`` prints them as a table.

``stop_gate(payload)`` runs from the Stop hook. When the session committed code to the configured repo's
branch, or ran a configured deploy command, and the configured board file has no commit since the session
started, it returns a block decision once. It also refreshes a private copy of a status log.

Every repo path, board path and deploy pattern comes from the config file
(``~/.claude/session-tracker/config.json``, override: ``SESSION_TRACKER_CONFIG``); nothing about a
particular project lives in this file. See ``session_tracker.config.example.json``.

Fail open: no function here raises. Errors go to ``tracker.log`` in the tracker dir and the hook goes on.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

STALE_AFTER_S = 2 * 3600
HIDE_ENDED_AFTER_S = 24 * 3600
MAX_COMMITS = 200
MAX_DEPLOYS = 100
LOG_MAX_BYTES = 1_000_000

DEFAULT_BLOCK_REASON = (
    "Update your row in {board} (state, last commit, next) and commit it with a pathspec."
)

_PROMPT_FILE = re.compile(r"(?:~|\.{0,2}/|\b)[\w.~/-]*/[\w.-]+\.md\b")
_GIT_SUBCMD = re.compile(
    r"\bgit\s+(?:-C\s+(\S+)\s+|-c\s+\S+\s+)*(commit|merge|cherry-pick|revert|am)\b"
)
_CD = re.compile(r"(?:^|[;&|(]\s*)cd\s+([^\s;&|)]+)")
_WORKTREE_ADD = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+)?worktree\s+add\s+(?:(?:-b|-B)\s+\S+\s+|--?[\w-]+\s+)*([^\s;&|-][^\s;&|]*)"
)


# --- paths, config, log ---------------------------------------------------------------------------


def tracker_dir() -> Path:
    return Path(os.environ.get("SESSION_TRACKER_DIR") or Path.home() / ".claude" / "session-tracker")


def config_path() -> Path:
    env = os.environ.get("SESSION_TRACKER_CONFIG")
    return Path(env) if env else tracker_dir() / "config.json"


def load_config() -> dict[str, Any]:
    try:
        with open(config_path()) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log_error("config", e)
        return {}


def log_error(where: str, err: BaseException | str) -> None:
    try:
        d = tracker_dir()
        d.mkdir(parents=True, exist_ok=True)
        log = d / "tracker.log"
        if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
            os.replace(log, d / "tracker.log.1")
        with open(log, "a") as f:
            f.write(f"{now_iso()} {where}: {type(err).__name__ if isinstance(err, BaseException) else ''} {err}\n")
    except Exception:
        pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def session_file(session_id: str) -> Path:
    safe = re.sub(r"[^\w.-]", "_", session_id)
    return tracker_dir() / f"{safe}.json"


def load_session(session_id: str) -> dict[str, Any] | None:
    try:
        with open(session_file(session_id)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None


def write_session(data: dict[str, Any]) -> None:
    path = session_file(data["session_id"])
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


class _SessionLock:
    """flock on a side file: hooks of one session can run at the same time (parallel tool calls)."""

    def __init__(self, session_id: str) -> None:
        locks = tracker_dir() / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
        self.path = locks / session_file(session_id).with_suffix(".lock").name
        self.fd = -1

    def __enter__(self) -> _SessionLock:
        import fcntl

        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        os.close(self.fd)


# --- git ------------------------------------------------------------------------------------------


class GitError(Exception):
    pass


def run_git(cwd: str, *args: str, ok_codes: tuple[int, ...] = (0,)) -> tuple[int, str]:
    import subprocess

    try:
        p = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise GitError(f"git {args[0]}: {e}") from e
    if p.returncode not in ok_codes:
        raise GitError(f"git {args[0]} exit {p.returncode}: {p.stderr.strip()[:200]}")
    return p.returncode, p.stdout.strip()


GitRunner = Callable[..., "tuple[int, str]"]


def repo_id(path: str, git: GitRunner = run_git) -> str:
    """The repo's common git dir: the same for the main tree and all its worktrees."""
    _, out = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return os.path.normpath(out)


def branch_of(path: str, git: GitRunner = run_git) -> str:
    _, out = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if out == "HEAD":
        _, sha = git(path, "rev-parse", "--short=9", "HEAD")
        return f"detached@{sha}"
    return out


# --- payload -> fields ----------------------------------------------------------------------------


def task_from_prompt(prompt: str) -> str | None:
    text = (prompt or "").strip()
    if not text or text.startswith("<"):
        return None
    m = _PROMPT_FILE.search(text)
    if m:
        return m.group(0)
    flat = " ".join(text.split())
    return flat[:80] + ("…" if len(flat) > 80 else "")


def tool_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Name + a short human summary. Never the command itself or any output (either can hold secrets)."""
    detail = ""
    if tool_name == "Bash":
        detail = str(tool_input.get("description") or "")
    else:
        for key in ("file_path", "notebook_path", "pattern", "skill", "description", "subagent_type"):
            if tool_input.get(key):
                detail = str(tool_input[key])
                break
    detail = " ".join(detail.split())
    if len(detail) > 60:
        detail = detail[:59] + "…"
    return f"{tool_name}: {detail}" if detail else tool_name


def _resolve(path: str, cwd: str) -> str:
    path = path.strip("'\"")
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd or "/", path)
    return os.path.normpath(path)


def worktree_from_command(command: str, cwd: str) -> str | None:
    """The last `git worktree add <path>` target, else the last `cd <path>`, else None."""
    adds = _WORKTREE_ADD.findall(command)
    if adds:
        return _resolve(adds[-1], cwd)
    cds = [c for c in _CD.findall(command) if c not in ("-", "~-")]
    if cds:
        return _resolve(cds[-1], cwd)
    return None


def _git_dir_for_commit(command: str, cwd: str, worktree: str | None) -> str | None:
    m = None
    for m in _GIT_SUBCMD.finditer(command):
        pass
    if not m:
        return None
    if m.group(1):
        return _resolve(m.group(1), cwd)
    head = command[: m.start()]
    cds = [c for c in _CD.findall(head) if c not in ("-", "~-")]
    if cds:
        return _resolve(cds[-1], cwd)
    return worktree or cwd


def deploy_matches(command: str, patterns: list[str]) -> list[str]:
    hits = []
    for pat in patterns:
        try:
            m = re.search(pat, command)
        except re.error:
            continue
        if m:
            hits.append(m.group(0)[:60])
    return hits


# --- track ----------------------------------------------------------------------------------------


def _new_session(payload: dict[str, Any]) -> dict[str, Any]:
    cwd = str(payload.get("cwd") or os.getcwd())
    return {
        "session_id": str(payload["session_id"]),
        "cwd": cwd,
        "started": now_iso(),
        "last_seen": now_iso(),
        "task": None,
        "worktree": cwd,
        "branch": None,
        "last_cwd": cwd,
        "last_commit": None,
        "commits": [],
        "deploys": [],
        "state": "idle",
        "waiting_since": None,
        "waiting_reason": None,
        "last_tool": None,
    }


def _set_state(data: dict[str, Any], state: str, reason: str | None = None) -> None:
    if state == "waiting_on_owner":
        if data.get("state") != "waiting_on_owner":
            data["waiting_since"] = now_iso()
        data["waiting_reason"] = reason
    else:
        data["waiting_since"] = None
        data["waiting_reason"] = None
    data["state"] = state


def _refresh_branch(data: dict[str, Any], git: GitRunner) -> None:
    try:
        data["branch"] = branch_of(data["worktree"], git)
    except GitError:
        data["branch"] = None


def _on_bash(data: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any], git: GitRunner) -> None:
    command = str((payload.get("tool_input") or {}).get("command") or "")
    cwd = str(payload.get("cwd") or data.get("last_cwd") or "")
    moved = worktree_from_command(command, cwd)
    if moved and moved != data.get("worktree") and os.path.isdir(moved):
        try:  # only a git checkout counts as a worktree (`cd /tmp` does not)
            data["branch"] = branch_of(moved, git)
            data["worktree"] = moved
        except GitError:
            pass
    elif cwd and cwd != data.get("last_cwd"):
        data["worktree"] = cwd
        _refresh_branch(data, git)
    data["last_cwd"] = cwd

    git_dir = _git_dir_for_commit(command, cwd, data.get("worktree"))
    if git_dir:
        _record_commit(data, git_dir, git)

    hits = deploy_matches(command, list(cfg.get("deploy_patterns") or []))
    if hits:
        data["deploys"] = (data.get("deploys") or [])[-(MAX_DEPLOYS - 1):] + [
            {"ts": now_iso(), "match": hits[0]}
        ]


def _record_commit(data: dict[str, Any], git_dir: str, git: GitRunner) -> None:
    try:
        _, out = git(git_dir, "log", "-1", "--format=%H%x00%ct%x00%s")
        sha, ct, subject = out.split("\x00", 2)
        rid = repo_id(git_dir, git)
    except (GitError, ValueError) as e:
        log_error("commit", e)
        return
    if time.time() - int(ct) > 900:
        return  # HEAD is older than this command: the commit/merge did not happen
    commits = data.get("commits") or []
    if any(c.get("sha") == sha for c in commits):
        return
    commits.append({"sha": sha, "subject": subject[:80], "repo": rid, "dir": git_dir, "ts": now_iso()})
    data["commits"] = commits[-MAX_COMMITS:]
    data["last_commit"] = f"{sha[:9]} {subject[:60]}"
    if os.path.normpath(git_dir) == os.path.normpath(data.get("worktree") or ""):
        _refresh_branch(data, git)


def apply_event(
    data: dict[str, Any], event: str, payload: dict[str, Any], cfg: dict[str, Any], git: GitRunner = run_git
) -> None:
    data["last_seen"] = now_iso()
    if event == "SessionStart":
        if data.get("state") in (None, "ended"):
            _set_state(data, "idle")
        if data.get("branch") is None:
            _refresh_branch(data, git)
    elif event == "UserPromptSubmit":
        if not data.get("task"):
            data["task"] = task_from_prompt(str(payload.get("prompt") or ""))
        _set_state(data, "working")
    elif event in ("PreToolUse", "PermissionRequest"):
        _set_state(data, "waiting_on_owner", tool_summary(str(payload.get("tool_name") or ""), {}))
    elif event == "PostToolUse":
        _set_state(data, "working")
        name = str(payload.get("tool_name") or "")
        data["last_tool"] = tool_summary(name, dict(payload.get("tool_input") or {}))
        if name == "Bash":
            _on_bash(data, payload, cfg, git)
    elif event == "Notification":
        msg = " ".join(str(payload.get("message") or "").split())[:80]
        _set_state(data, "waiting_on_owner", msg or None)
    elif event == "Stop":
        _set_state(data, "idle")
    elif event == "SessionEnd":
        _set_state(data, "ended")
        data["end_reason"] = payload.get("reason")


def track(event: str, payload: dict[str, Any]) -> None:
    """Record one hook event for its session. Never raises."""
    try:
        sid = payload.get("session_id")
        if not sid:
            return
        cfg = load_config()
        with _SessionLock(str(sid)):
            data = load_session(str(sid)) or _new_session(payload)
            apply_event(data, event, payload, cfg)
            write_session(data)
    except Exception as e:
        log_error(f"track {event}", e)


# --- Stop gate ------------------------------------------------------------------------------------


def _touches(paths: list[str], prefixes: list[str]) -> bool:
    return any(p.startswith(tuple(prefixes)) for p in paths)


def _changed_files(repo: str, sha: str, git: GitRunner) -> list[str]:
    rc, out = git(repo, "rev-parse", "--verify", "--quiet", f"{sha}^1", ok_codes=(0, 1))
    if rc != 0:
        return []  # root commit
    _, out = git(repo, "diff", "--name-only", f"{sha}^1", sha)
    return out.splitlines()


def gate_reason(
    payload: dict[str, Any], session: dict[str, Any] | None, cfg: dict[str, Any], git: GitRunner = run_git
) -> str | None:
    """The block reason, or None to let the session stop. Raises GitError on git trouble (caller allows)."""
    gate = cfg.get("board_gate")
    if not gate or not session or payload.get("stop_hook_active"):
        return None
    if session.get("board_gate_blocked"):
        return None
    repo = str(gate["repo"])
    branch = str(gate.get("branch") or "main")
    board = str(gate["board"])
    code_paths = list(gate.get("code_paths") or [""])
    if not os.path.isdir(repo):
        raise GitError(f"repo missing: {repo}")
    rid = repo_id(repo, git)
    mine = [c for c in session.get("commits") or [] if c.get("repo") == rid]

    shipped = bool(session.get("deploys"))
    for c in mine:
        if shipped:
            break
        rc, _ = git(repo, "merge-base", "--is-ancestor", c["sha"], branch, ok_codes=(0, 1))
        if rc == 0 and _touches(_changed_files(repo, c["sha"], git), code_paths):
            shipped = True
    if not shipped:
        return None

    _, log = git(repo, "log", branch, f"--since={session.get('started')}", "--format=%H", "--", board)
    if log:
        return None
    if any(board in _changed_files(repo, c["sha"], git) for c in mine):
        return None
    return str(gate.get("message") or DEFAULT_BLOCK_REASON).format(board=board)


def sync_session_log(cfg: dict[str, Any]) -> bool:
    """Copy the status log over its private copy when it changed, keeping the copy's leading <!-- --> header.

    Never commits. Returns True when it wrote the copy.
    """
    spec = cfg.get("session_log")
    if not spec:
        return False
    src, dest = Path(spec["source"]), Path(spec["dest"])
    if not src.is_file() or not dest.is_file():
        return False
    body = src.read_text()
    current = dest.read_text()
    header = ""
    if current.startswith("<!--"):
        end = current.find("-->")
        if end != -1:
            header = current[: end + 3] + "\n"
    if current == header + body:
        return False
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    tmp.write_text(header + body)
    os.replace(tmp, dest)
    return True


def stop_gate(payload: dict[str, Any]) -> dict[str, str] | None:
    """Run from the Stop hook. Returns {"decision": "block", "reason": ...} or None. Never raises."""
    cfg = load_config()
    try:
        sync_session_log(cfg)
    except Exception as e:
        log_error("session_log", e)
    try:
        sid = str(payload.get("session_id") or "")
        if not sid or not cfg.get("board_gate"):
            return None
        with _SessionLock(sid):
            session = load_session(sid)
            reason = gate_reason(payload, session, cfg)
            if reason and session is not None:
                session["board_gate_blocked"] = now_iso()
                write_session(session)
        return {"decision": "block", "reason": reason} if reason else None
    except Exception as e:
        log_error("stop_gate", e)
        return None
