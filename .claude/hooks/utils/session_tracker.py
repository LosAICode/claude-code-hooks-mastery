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
MAX_COMMITS_PER_COMMAND = 20
PENDING_MAX_AGE_S = 3600
LOG_MAX_BYTES = 1_000_000

DEFAULT_BLOCK_REASON = (
    "Update your row in {board} (state, last commit, next) and commit it with a pathspec."
)
DEFAULT_PROMPT_REASON = (
    "`{prompt}` is still `running`. Set its final status: `python3 {script} set {ref} "
    "done|deployed|on-main|on-branch|blocked --commit <sha> --notes '<one line>'` "
    "(it commits the prompt, its module README and the board with a pathspec)."
)
_PROMPT_CENTER_MODULE = "prompt_center"  # sibling module; imported lazily so a bug there never breaks tracking
DEFAULT_CLAIM_VERBS = ["run", "execute", "start"]

_PROMPT_FILE = re.compile(r"(?:~|\.{0,2}/|\b)[\w.~/-]*/[\w.-]+\.md\b")
_GIT_SUBCMD = re.compile(
    r"\bgit\s+(?:-C\s+(\S+)\s+|-c\s+\S+\s+)*(commit|merge|cherry-pick|revert|am)\b"
)
_COMMAND_CENTER = re.compile(r"\bcommand_center\.py\b")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?(?:\n\s*\2[ \t]*(?=\n|$)|\Z)", re.S)
_SEPARATOR_CHARS = set(";&|()")
_SHELL_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "{"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
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


def _commit_dirs(command: str, cwd: str, worktree: str | None, cfg: dict[str, Any]) -> list[str]:
    """Repos this command may commit in: a git commit/merge/… target, and the prompts repo for command_center.py."""
    dirs = []
    git_dir = _git_dir_for_commit(command, cwd, worktree)
    if git_dir:
        dirs.append(git_dir)
    spec = cfg.get("prompts")
    if isinstance(spec, dict) and spec.get("repo") and _COMMAND_CENTER.search(command):
        dirs.append(os.path.normpath(str(spec["repo"])))
    return list(dict.fromkeys(dirs))


def _segments(command: str) -> list[list[str]]:
    """The command's simple commands as word lists: heredoc bodies dropped, split on ; && || | & ( ), `$(` and newlines."""
    import shlex

    # A newline ends a command; inside a quoted string the ";" stays literal text.
    text = _HEREDOC.sub("\n", command).replace("\\\n", " ").replace("\n", " ; ")
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:  # an unclosed quote: split on the separators by hand
        tokens = re.findall(r"[;&|()]+|[^\s;&|()]+", text)
    out: list[list[str]] = [[]]
    for tok in tokens:
        if set(tok) <= _SEPARATOR_CHARS:  # shlex glues a run: `);`, `&&`, `|&`
            out.append([])
        else:
            out[-1].append(tok)
    return [seg for seg in out if seg]


def _skip_opts(words: list[str], i: int, with_value: tuple[str, ...] = ()) -> int:
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        i += 2 if words[i] in with_value else 1
    return i + 1 if i < len(words) and words[i] == "--" else i


def _executed(words: list[str], depth: int = 0) -> list[list[str]]:
    """The words from the command word on, after env assignments and wrappers (sudo, flock, nice, bash -c, …)."""
    i = 0
    while i < len(words):
        w = os.path.basename(words[i])
        if _ENV_ASSIGN.match(words[i]) or words[i] in _SHELL_KEYWORDS:
            i += 1
        elif w in ("sudo", "env", "nohup", "exec", "time", "command"):
            i = _skip_opts(words, i + 1, ("-u", "-g"))
        elif w == "nice":
            i = _skip_opts(words, i + 1, ("-n",))
        elif w == "timeout":
            i = _skip_opts(words, i + 1, ("-s", "-k")) + 1  # + the duration
        elif w == "flock":
            i = _skip_opts(words, i + 1, ("-w", "-E", "--timeout", "--conflict-exit-code")) + 1  # + the lock file
        elif w in ("bash", "sh"):
            if "-c" in words[i + 1:] and depth < 3:
                j = words.index("-c", i + 1)
                return [x for seg in _segments(" ".join(words[j + 1:j + 2])) for x in _executed(seg, depth + 1)]
            i = _skip_opts(words, i + 1, ("-o", "-O"))
        else:
            return [words[i:]]
    return []


def deploy_matches(command: str, patterns: list[str]) -> list[str]:
    """Patterns that match where a command is EXECUTED: at the command word, never in an argument or a heredoc."""
    hits = []
    runs = [run for seg in _segments(command) for run in _executed(seg)]
    for pat in patterns:
        for run in runs:
            try:
                m = re.search(pat, " ".join(run))
            except re.error:
                break
            if m and m.start() < len(run[0]):
                hits.append(m.group(0)[:60])
                break
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

    pending = (data.get("pending_heads") or {}).pop(_pending_key(payload, command), None)
    for git_dir, before in ((pending or {}).get("heads") or {}).items():
        _record_commits(data, git_dir, before, git)

    hits = deploy_matches(command, list(cfg.get("deploy_patterns") or []))
    if hits:
        data["deploys"] = (data.get("deploys") or [])[-(MAX_DEPLOYS - 1):] + [
            {"ts": now_iso(), "match": hits[0]}
        ]


def _pending_key(payload: dict[str, Any], command: str) -> str:
    import hashlib

    return str(payload.get("tool_use_id") or "cmd:" + hashlib.sha1(command.encode()).hexdigest()[:16])


def _snapshot_heads(data: dict[str, Any], payload: dict[str, Any], cfg: dict[str, Any], git: GitRunner) -> None:
    """PreToolUse: HEAD of each repo this command may commit in, so PostToolUse credits only what it moved."""
    pending = {k: v for k, v in (data.get("pending_heads") or {}).items()
               if time.time() - float(v.get("ts") or 0) < PENDING_MAX_AGE_S}
    command = str((payload.get("tool_input") or {}).get("command") or "")
    cwd = str(payload.get("cwd") or data.get("last_cwd") or "")
    heads = {}
    for git_dir in _commit_dirs(command, cwd, data.get("worktree"), cfg):
        try:
            heads[git_dir] = git(git_dir, "rev-parse", "HEAD")[1]
        except GitError:
            pass  # not a checkout (yet): nothing to compare against
    if heads:
        pending[_pending_key(payload, command)] = {"heads": heads, "ts": time.time()}
    if pending or data.get("pending_heads"):
        data["pending_heads"] = pending


def _record_commits(data: dict[str, Any], git_dir: str, before: str, git: GitRunner) -> None:
    """Credit the commits HEAD moved through during this command (first parent, oldest first); none if it did not move."""
    try:
        _, after = git(git_dir, "rev-parse", "HEAD")
        if after == before:
            return
        _, out = git(git_dir, "log", "--first-parent", f"-{MAX_COMMITS_PER_COMMAND}", "--format=%H%x00%s",
                     f"{before}..{after}")
        rid = repo_id(git_dir, git)
    except GitError as e:
        log_error("commit", e)
        return
    rows = [line.split("\x00", 1) for line in reversed(out.splitlines()) if "\x00" in line]
    commits = data.get("commits") or []
    known = {c.get("sha") for c in commits}
    for sha, subject in rows:
        if sha in known:
            continue
        commits.append({"sha": sha, "subject": subject[:80], "repo": rid, "dir": git_dir, "ts": now_iso()})
        data["last_commit"] = f"{sha[:9]} {subject[:60]}"
    data["commits"] = commits[-MAX_COMMITS:]
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
            try:
                _claim_prompt(data, str(payload.get("prompt") or ""), cfg)
            except Exception as e:
                log_error("claim_prompt", e)
        _set_state(data, "working")
    elif event in ("PreToolUse", "PermissionRequest"):
        _set_state(data, "waiting_on_owner", tool_summary(str(payload.get("tool_name") or ""), {}))
        if event == "PreToolUse" and payload.get("tool_name") == "Bash":
            _snapshot_heads(data, payload, cfg, git)
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


def _claim_prompt(data: dict[str, Any], prompt: str, cfg: dict[str, Any]) -> None:
    """First message names a prompt file: remember it; if it says to run it, mark the prompt `running`."""
    spec = cfg.get("prompts")
    if not isinstance(spec, dict) or not spec.get("repo") or ".md" not in prompt:
        return
    import importlib

    pc = importlib.import_module(_PROMPT_CENTER_MODULE)
    index = pc.scan(cfg)
    rel = pc.resolve_prompt(prompt, cfg, index)
    if not rel:
        return
    if not pc.claims_prompt(prompt, list(spec.get("claim_verbs") or DEFAULT_CLAIM_VERBS)):
        data["prompt_mentioned"] = rel
        return
    sid = str(data["session_id"])
    data["prompt_file"] = rel
    data["released_prompts"] = [r for r in data.get("released_prompts") or [] if r != rel]
    d = tracker_dir()
    live = pc.live_session_ids(pc.load_sessions(d) if d.is_dir() else [], time.time()) - {sid}
    prev = pc.mark_running(spec, rel, sid, live)
    data["prompt_claimed"] = prev is not None
    data["prompt_prev_status"] = prev
    if prev is not None and spec.get("commit_claims", True):
        p = next((p for p in index if p.rel == rel), None)
        label = f"{p.module} {p.id or p.name}" if p else rel
        data["prompt_claim_commit"] = pc.commit_prompt_files(spec, [rel], f"docs(prompts): {label} running ({sid[:8]})")


def attach_prompt(session_id: str, rel: str, prev: str | None) -> bool:
    """Record a hand claim (``command_center.py claim``) in the session's file, so its Stop gate applies."""
    with _SessionLock(session_id):
        data = load_session(session_id)
        if data is None:
            return False
        data.update(prompt_file=rel, prompt_claimed=True, prompt_prev_status=prev,
                    released_prompts=[r for r in data.get("released_prompts") or [] if r != rel])
        write_session(data)
    return True


def release_prompt(session_id: str, rel: str) -> bool:
    """Drop the session's hold on a prompt set back to pending or blocked (``command_center.py set``).

    The prompt goes into ``released_prompts``, so the view no longer matches the session to it by its task text
    either; a later claim of the same prompt takes it back.
    """
    with _SessionLock(session_id):
        data = load_session(session_id)
        if data is None:
            return False
        if rel not in (data.get("released_prompts") or []):
            data["released_prompts"] = [*(data.get("released_prompts") or []), rel]
        if data.get("prompt_file") == rel:
            data.update(prompt_file=None, prompt_claimed=False, prompt_prev_status=None)
        write_session(data)
    return True


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


def prompt_gate_reason(payload: dict[str, Any], session: dict[str, Any] | None, cfg: dict[str, Any]) -> str | None:
    """Block once when the session claimed a prompt, shipped commits or deploys, and left it `running`."""
    spec = cfg.get("prompts")
    if not isinstance(spec, dict) or not spec.get("gate") or not session or payload.get("stop_hook_active"):
        return None
    if session.get("prompt_gate_blocked") or not session.get("prompt_claimed") or not session.get("prompt_file"):
        return None
    if not session.get("commits") and not session.get("deploys"):
        return None
    import importlib

    pc = importlib.import_module(_PROMPT_CENTER_MODULE)
    rel = str(session["prompt_file"])
    if not (Path(str(spec["repo"])) / rel).exists():  # filed under Completed/ or Pending/ since the claim
        rel = pc.moved_map(spec).get(rel, rel)
    meta, _, _ = pc.parse_frontmatter(pc._read_head(Path(str(spec["repo"])) / rel))
    if meta.get("status") != "running" or str(meta.get("session_id") or "") != str(session.get("session_id")):
        return None
    script = Path(__file__).resolve().parents[3] / "scripts" / "command_center.py"
    return str(spec.get("message") or DEFAULT_PROMPT_REASON).format(
        prompt=rel, ref=os.path.basename(rel).replace(" ", "\\ "), script=script)


GATES = (("board_gate", gate_reason), ("prompt_gate", prompt_gate_reason))


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
    """Run from the Stop hook. Returns {"decision": "block", "reason": ...} or None. Never raises.

    Each gate runs on its own and fires at most once per session (``<gate>_blocked``); their reasons
    are joined into one block decision.
    """
    cfg = load_config()
    try:
        sync_session_log(cfg)
    except Exception as e:
        log_error("session_log", e)
    try:
        sid = str(payload.get("session_id") or "")
        if not sid:
            return None
        reasons = []
        with _SessionLock(sid):
            session = load_session(sid)
            if session is None:
                return None
            for name, gate in GATES:
                try:
                    reason = gate(payload, session, cfg)
                except Exception as e:
                    log_error(name, e)
                    reason = None
                if reason:
                    reasons.append(reason)
                    session[f"{name}_blocked"] = now_iso()
            if reasons:
                write_session(session)
        return {"decision": "block", "reason": "\n".join(reasons)} if reasons else None
    except Exception as e:
        log_error("stop_gate", e)
        return None
