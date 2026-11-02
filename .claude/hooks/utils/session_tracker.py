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
MAX_SKIPPED_COMMITS = 20
PENDING_MAX_AGE_S = 3600
OPEN_AGENT_MAX_AGE_S = 6 * 3600  # a subagent whose SubagentStop never arrived stops holding the Stop gate after this
LOG_MAX_BYTES = 1_000_000

DEFAULT_BLOCK_REASON = (
    "Update your row in {board} (state, last commit, next) and commit it with a pathspec."
)
DEFAULT_PROMPT_REASON = (
    "`{prompt}` is still `running`. Set its final status: `python3 {script} set {ref} "
    "done|deployed|on-main|on-branch|blocked --commit <sha> --notes '<one line>'` "
    "(it commits the prompt, its module README and the board with a pathspec)."
)
STATUS_COMMIT_PREFIX = "docs(prompts):"  # command_center.py's own commits: status, not a prompt's work
_PROMPT_CENTER_MODULE = "prompt_center"  # sibling module; imported lazily so a bug there never breaks tracking
DEFAULT_CLAIM_VERBS = ["run", "execute", "start"]

_PROMPT_FILE = re.compile(r"(?:~|\.{0,2}/|\b)[\w.~/-]*/[\w.-]+\.md\b")
_GIT_SUBCMD = re.compile(
    r"\bgit\s+(?:-C\s+(\S+)\s+|-c\s+\S+\s+)*(commit|merge|cherry-pick|revert|am)\b"
)
_COMMAND_CENTER = re.compile(r"\bcommand_center\.py\b")
_COMMITTING = ("commit", "merge", "cherry-pick", "revert", "am")
_HEREDOC_MESSAGE = re.compile(
    r"(?:-m|--message)[=\s]+[\"']?\$\(\s*cat\s*<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2[ \t]*(?=\n|\)|$)", re.S
)
SESSION_TRAILER = "Claude-Session"
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
    expect = _expected(command, cwd, data.get("worktree"), cfg)  # the worktree as PreToolUse saw it
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
    made = []
    if pending and pending.get("heads"):
        others = _other_live_trailers(data)
        for git_dir, before in pending["heads"].items():
            made += _record_commits(data, git_dir, before, git, expect.get(git_dir, []), others)
    if made and data.get("prompt_claimed") and data.get("prompt_file"):
        try:
            _credit_prompt(data, made, cfg, git)
        except Exception as e:
            log_error("credit_prompt", e)

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
        # CC14: only a git commit/merge/… lands; a command_center.py call (read-only, or its own status commit)
        # snapshots the prompts repo too, and deploy-ready read that as "X is landing" (live 11-02, ×3)
        landing = sorted(d for d, rules in _expected(command, cwd, data.get("worktree"), cfg).items()
                         if d in heads and any(not r.get("status") for r in rules))
        pending[_pending_key(payload, command)] = {"heads": heads, "landing": landing, "ts": time.time()}
    if pending or data.get("pending_heads"):
        data["pending_heads"] = pending


def _git_target(words: list[str], cur: str) -> tuple[str, str, list[str]] | None:
    """(dir, subcommand, its args) of a committing `git` run, else None."""
    if not words or os.path.basename(words[0]) != "git":
        return None
    i, git_dir = 1, cur
    while i < len(words) and words[i].startswith("-"):
        if words[i] in ("-C", "-c") and i + 1 < len(words):
            if words[i] == "-C":
                git_dir = _resolve(words[i + 1], git_dir)
            i += 2
        else:
            i += 1
    if i >= len(words) or words[i] not in _COMMITTING:
        return None
    return os.path.normpath(git_dir), words[i], words[i + 1:]


def _first_paragraph(message: str) -> str:
    """git's %s: the message's first paragraph, its lines joined by spaces (a newline reaches us as " ; ")."""
    lines = []
    for line in message.replace(" ; ", "\n").splitlines():
        if not line.strip():
            if lines:
                break
            continue
        lines.append(line.strip())
    return " ".join(lines)


def _commit_rule(args: list[str], heredocs: list[str]) -> dict[str, Any]:
    """What one `git commit` can make: {"subject": first -m's subject or None, "paths": its pathspec or None}."""
    message: str | None = None
    unparsable = False
    paths = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            paths = args[i + 1:]
            break
        nxt = args[i + 1] if i + 1 < len(args) else None
        value = None
        if a == "--message" or a.startswith("--message="):
            value, i = (a.split("=", 1)[1], i) if "=" in a else (nxt, i + 1)
        elif a in ("--file", "-F", "-C", "-c", "--reuse-message", "--reedit-message") or a.startswith(
                ("--file=", "--reuse-message=", "--reedit-message=")):
            unparsable = True  # the message comes from a file or another commit
            i += 0 if "=" in a else 1
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            for k, ch in enumerate(a[1:], 1):
                if ch == "m":
                    value, i = (a[k + 1:], i) if a[k + 1:] else (nxt, i + 1)
                    break
                if ch in "FCc":
                    unparsable = True
                    i += 0 if a[k + 1:] else 1
                    break
        if value is not None and message is None:
            message = value
        i += 1
    subject = None
    if message is not None and not unparsable:
        if "$(" in message:
            message = heredocs.pop(0) if heredocs else None
        if message is not None and "$" not in message and "`" not in message:
            subject = _first_paragraph(message) or None
    return {"subject": subject, "paths": paths}


def _expected(command: str, cwd: str, worktree: str | None, cfg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Per repo dir, what this command can make: one rule per committing word it executes (CC9).

    A rule is {"status": True} (command_center.py: its own status commits only), {"subject", "paths"} (a `git
    commit`: the first -m's subject, the pathspec after `--`; None = no such rule) or {} (merge, cherry-pick,
    revert, am, a message from a file: what it brings in). A commit is this command's when one rule of its dir
    accepts it. Live 10-31: Security M02's docs commit landed in the shared tree while Connections ran `command_center
    .py set connections/R9`, and the bare before/after snapshot put it in R9's ``commits:``."""
    rules: dict[str, list[dict[str, Any]]] = {}
    heredocs = [_first_paragraph(m.group(3).replace("\n", " ; ")) for m in _HEREDOC_MESSAGE.finditer(command)]
    cur = worktree or cwd
    spec = cfg.get("prompts")
    for seg in _segments(command):
        for run in _executed(seg):
            if run[0] == "cd" and len(run) > 1 and run[1] not in ("-", "~-"):
                cur = _resolve(run[1], cur)
                continue
            if isinstance(spec, dict) and spec.get("repo") and any(
                    os.path.basename(w) == "command_center.py" for w in run[:2]):
                rules.setdefault(os.path.normpath(str(spec["repo"])), []).append({"status": True})
                continue
            target = _git_target(run, cur)
            if not target:
                continue
            git_dir, sub, args = target
            rule = _commit_rule(args, heredocs) if sub == "commit" else {}
            if rule.get("paths") is not None:
                rule["paths"] = [_resolve(p, git_dir) for p in rule["paths"]]
            rules.setdefault(git_dir, []).append(rule)
    return rules


def _covered(files: list[str], paths: list[str], top: str) -> bool:
    """Every file (repo-relative) lies under one of the pathspec's absolute paths (a directory covers its files)."""
    import fnmatch

    def ok(f: str) -> bool:
        full = os.path.join(top, f)
        return any(full == p or full.startswith(p.rstrip("/") + "/") or fnmatch.fnmatchcase(full, p) for p in paths)

    return bool(files) and all(ok(f) for f in files)


def _accepts(rule: dict[str, Any], subject: str, files: Callable[[], list[str]], top: Callable[[], str]) -> bool:
    if rule.get("status"):
        return _is_status_commit(subject)
    if rule.get("subject") is not None and subject != rule["subject"]:
        return False
    return rule.get("paths") is None or _covered(files(), rule["paths"], top())


def _other_live_trailers(data: dict[str, Any]) -> set[str]:
    """The Claude-Session trailers of the other live sessions (not ended, seen within STALE_AFTER_S)."""
    out = set()
    try:
        for f in tracker_dir().glob("*.json"):
            try:
                other = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(other, dict) or other.get("session_id") == data.get("session_id"):
                continue
            seen = parse_iso(other.get("last_seen"))
            if other.get("claude_session") and other.get("state") != "ended" and seen and \
                    time.time() - seen < STALE_AFTER_S:
                out.add(str(other["claude_session"]))
    except OSError as e:
        log_error("live_trailers", e)
    out.discard(str(data.get("claude_session") or ""))
    return out


def _trailer(body: str) -> str:
    found = re.findall(rf"^{SESSION_TRAILER}:\s*(\S+)\s*$", body, re.M)
    return found[-1] if found else ""


def _record_commits(data: dict[str, Any], git_dir: str, before: str, git: GitRunner,
                    rules: list[dict[str, Any]] | None = None, others: set[str] | None = None) -> list[dict[str, Any]]:
    """Credit the commits HEAD moved through during this command (first parent, oldest first) that the command
    could have made (``_expected``); none if it did not move. A commit whose Claude-Session trailer is another live
    session's is that session's. The rest go to ``skipped_commits`` with the reason.

    Returns the rows it added."""
    try:
        _, after = git(git_dir, "rev-parse", "HEAD")
        if after == before:
            return []
        _, out = git(git_dir, "log", "--first-parent", f"-{MAX_COMMITS_PER_COMMAND}", "--format=%H%x00%s%x00%B%x1e",
                     f"{before}..{after}")
        rid = repo_id(git_dir, git)
    except GitError as e:
        log_error("commit", e)
        return []
    rows = [rec.strip("\n").split("\x00", 2) for rec in out.split("\x1e") if rec.count("\x00") >= 2]
    commits = data.get("commits") or []
    known = {c.get("sha") for c in commits}
    added, skipped = [], []
    top_cache: list[str] = []

    def top() -> str:
        if not top_cache:
            top_cache.append(git(git_dir, "rev-parse", "--show-toplevel")[1])
        return top_cache[0]

    for sha, subject, body in reversed(rows):
        if sha in known:
            continue
        trailer = _trailer(body)
        files_cache: list[list[str]] = []

        def files(sha: str = sha) -> list[str]:
            if not files_cache:
                files_cache.append(_changed_files(git_dir, sha, git))
            return files_cache[0]

        reason = None
        if trailer and trailer in (others or set()):
            reason = "another live session's commit"
        else:
            try:
                hit = next((r for r in rules or [] if _accepts(r, subject, files, top)), None)
            except GitError as e:
                log_error("commit_rule", e)
                hit = None
            if hit is None:
                reason = "not made by this command"
            elif hit.get("subject") is not None and trailer and not data.get("claude_session"):
                data["claude_session"] = trailer
        if reason:
            skipped.append({"sha": sha, "subject": subject[:80], "reason": reason, "ts": now_iso()})
            continue
        added.append({"sha": sha, "subject": subject[:80], "repo": rid, "dir": git_dir, "ts": now_iso()})
        data["last_commit"] = f"{sha[:9]} {subject[:60]}"
    data["commits"] = (commits + added)[-MAX_COMMITS:]
    if skipped:
        data["skipped_commits"] = ((data.get("skipped_commits") or []) + skipped)[-MAX_SKIPPED_COMMITS:]
    if os.path.normpath(git_dir) == os.path.normpath(data.get("worktree") or ""):
        _refresh_branch(data, git)
    return added


def _is_status_commit(subject: str) -> bool:
    return str(subject or "").startswith(STATUS_COMMIT_PREFIX)


def _credit_prompt(data: dict[str, Any], rows: list[dict[str, Any]], cfg: dict[str, Any], git: GitRunner) -> None:
    """Append the claimed prompt's work to its ``commits:``: commits in the prompts repo, not a status commit and
    not one that touches only the prompts tree (CC7: R4's fix landed unrecorded, so deploy-check called R4 live),
    and commits in a repo listed in ``prompts.repos`` (CC11: CC9/CC10's hooks-repo work)."""
    spec = cfg.get("prompts")
    if not isinstance(spec, dict) or not spec.get("repo"):
        return
    repo = str(spec["repo"])
    rid = repo_id(repo, git)
    extra = set()
    for r in spec.get("repos") or []:
        path = str(r.get("path") or "") if isinstance(r, dict) else ""
        if path and os.path.isdir(path):
            try:
                extra.add(repo_id(path, git))
            except GitError:
                pass
    root = str(spec.get("root") or "").strip("/") + "/"
    shas = []
    for c in rows:
        if c.get("repo") not in {rid, *extra} or _is_status_commit(c.get("subject") or ""):
            continue
        if c.get("repo") == rid:
            files = _changed_files(str(c.get("dir") or repo), c["sha"], git)
            if files and all(f.startswith(root) for f in files):
                continue
        shas.append(c["sha"])
    if not shas:
        return
    import importlib

    pc = importlib.import_module(_PROMPT_CENTER_MODULE)
    rel = str(data["prompt_file"])
    added = pc.add_commits(spec, rel, shas)
    if not added or not spec.get("commit_claims", True):
        return
    if not (Path(repo) / rel).exists():
        rel = pc.moved_map(spec).get(rel, rel)
    meta, _, _ = pc.parse_frontmatter(pc._read_head(Path(repo) / rel))
    label = " ".join(str(x) for x in (meta.get("module"), meta.get("id")) if x) or os.path.basename(rel)
    pc.commit_prompt_files(spec, [rel], f"{STATUS_COMMIT_PREFIX} {label} commits +" + " +".join(added))


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
        if not data.get("prompt_file"):  # any message claims while the session holds none (CC8), never re-points
            try:
                _claim_prompt(data, str(payload.get("prompt") or ""), cfg)
            except Exception as e:
                log_error("claim_prompt", e)
        _set_state(data, "working")
    elif event == "PreToolUse" and payload.get("tool_name") == "Bash":
        _snapshot_heads(data, payload, cfg, git)  # not a wait: a permission dialog arrives as PermissionRequest
    elif event in ("PreToolUse", "PermissionRequest"):
        _set_state(data, "waiting_on_owner", tool_summary(str(payload.get("tool_name") or ""), {}))
    elif event in ("PostToolUse", "PostToolUseFailure"):  # CC14: a failed command's snapshot read as "landing" 10 min
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
    elif event in ("SubagentStart", "SubagentStop"):
        _on_subagent(data, event, str(payload.get("agent_id") or ""))


def _on_subagent(data: dict[str, Any], event: str, agent_id: str) -> None:
    """Keep ``open_agents`` {agent_id: started} for the parent session (both events carry the parent's session_id).
    A stop with no start (compaction's summariser) is a no-op; expired entries are dropped on every event."""
    agents = {a: ts for a, ts in (data.get("open_agents") or {}).items() if a in open_agents(data)}
    if agent_id and event == "SubagentStart":
        agents[agent_id] = now_iso()
    elif agent_id:
        agents.pop(agent_id, None)
    data["open_agents"] = agents


def open_agents(session: dict[str, Any] | None) -> list[str]:
    """Subagents started and not yet stopped, younger than OPEN_AGENT_MAX_AGE_S."""
    cutoff = time.time() - OPEN_AGENT_MAX_AGE_S
    return [a for a, ts in ((session or {}).get("open_agents") or {}).items() if (parse_iso(ts) or 0) > cutoff]


def _claim_prompt(data: dict[str, Any], prompt: str, cfg: dict[str, Any]) -> None:
    """A message names a prompt file: remember it; if it says to run it, mark the prompt `running`."""
    spec = cfg.get("prompts")
    if not isinstance(spec, dict) or not spec.get("repo") or (".md" not in prompt and "/" not in prompt):
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


# --- restart gate (CC9) -------------------------------------------------------------------------

DEFAULT_RESTART_PATTERNS = [r"scripts/restart_web\.sh", r"systemctl\s+(?:try-)?(?:restart|stop|start)\s+neurogenserver"]
WEB_UNITS = ["neurogenserver", "neurogenserver-web@5027", "neurogenserver-web@5028"]
RESTART_BLOCK_REASON = (
    "BLOCKED: restarts are coordinated by the Command Center. Queue a request: run `python3 "
    "/opt/claude-code-hooks-mastery/scripts/command_center.py request <module>/<ID>` (it prints the request id), then "
    "SendMessage the /cc session `deploy request <module>/<ID> <request id>`; it answers with `deploy-ready`. "
    "(Outage: a unit that is down can always be restarted; override: NEUROGEN_DEPLOY_OVERRIDE=\"<reason>\", logged.)"
)
_DEPLOY_ID = re.compile(r"^[\w.-]+$")


def _systemctl_is_active(unit: str) -> str:
    import subprocess

    try:
        return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        log_error("restart_gate is-active", e)
        return "unknown"


def _restart_units(run: list[str]) -> list[str]:
    """The neurogenserver units a matched run acts on; [] when it can't tell (a `$u` in the name)."""
    if os.path.basename(run[0]) != "systemctl":
        return list(WEB_UNITS) if any(w.endswith("restart_web.sh") for w in run[:2]) else []
    names = [w for w in run[1:] if not w.startswith("-")][1:]  # the words after the verb
    units = [w.removesuffix(".service") for w in names if w.startswith("neurogenserver")]
    return [] if any("$" in u for u in units) else units


def _deploy_open(run_id: str) -> bool:
    if not _DEPLOY_ID.match(run_id) or run_id.startswith("."):
        return False
    try:
        with open(tracker_dir() / "deploys" / f"{run_id}.json") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(meta, dict) and "end" not in meta


def _log_override(session_id: str, reason: str, match: str, kind: str = "deploy") -> None:
    d = tracker_dir() / "deploys"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "overrides.jsonl", "a") as f:
        f.write(json.dumps({"ts": now_iso(), "session_id": session_id, "reason": reason, "match": match,
                            "kind": kind}) + "\n")


def restart_gate(payload: dict[str, Any], cfg: dict[str, Any],
                 is_active: Callable[[str], str] = _systemctl_is_active) -> str | None:
    """PreToolUse Bash: the block message for a restart that skips the Command Center, else None. Never raises.

    Off unless ``prompts.deploy_coordinator.session`` names a session (id or a prefix of 8+). Allowed: the
    coordinator; ``NEUROGEN_DEPLOY_RUN=<id>`` on the restart with ``deploys/<id>.json`` still open (deploy-run);
    ``NEUROGEN_DEPLOY_OVERRIDE=<reason>`` on it (logged to ``deploys/overrides.jsonl``); an outage (no unit it names
    is active). Before CC9 any session could restart a unit, and every restart ships all of main (10-31 01:14: an
    auto-restart shipped untested main during the freeze)."""
    try:
        spec = cfg.get("prompts")
        coord = spec.get("deploy_coordinator") if isinstance(spec, dict) else None
        coord_sid = str(coord.get("session") or "") if isinstance(coord, dict) else ""
        if len(coord_sid) < 8:
            return None
        command = str((payload.get("tool_input") or {}).get("command") or "")
        patterns = list(cfg.get("restart_patterns") or DEFAULT_RESTART_PATTERNS)
        if not deploy_matches(command, patterns):
            return None
        sid = str(payload.get("session_id") or "")
        if sid.startswith(coord_sid):
            return None
        for seg in _segments(command):
            for run in _executed(seg):
                match = deploy_matches(" ".join(run), patterns)
                if not match:
                    continue
                env = {}
                for w in seg:
                    if w == run[0]:
                        break
                    if _ENV_ASSIGN.match(w):
                        k, v = w.split("=", 1)
                        env[k] = v
                if env.get("NEUROGEN_DEPLOY_RUN") and _deploy_open(env["NEUROGEN_DEPLOY_RUN"]):
                    continue
                if env.get("NEUROGEN_DEPLOY_OVERRIDE", "").strip():
                    _log_override(sid, env["NEUROGEN_DEPLOY_OVERRIDE"].strip(), match[0])
                    continue
                units = _restart_units(run)
                if units and all(is_active(u) != "active" for u in units):
                    continue  # an outage: the unit is down, restarting it can't ship anything over a live one
                return RESTART_BLOCK_REASON
        return None
    except Exception as e:
        log_error("restart_gate", e)
        return None


# --- restart window (CC14) ----------------------------------------------------------------------------------

RESTART_WINDOW_FILE = "/run/lock/neurogen-restart-window"  # the live config's prompts.restart_window
DOCS_ONLY_PREFIXES = ("Documentation/", "reports/")
_COMMIT_ALL = re.compile(r"\bcommit\b[^;&|]*?\s(?:-[a-zA-Z]*a[a-zA-Z]*|--all)(?=\s|$)")
LANDING_BLOCK_REASON = (
    "BLOCKED: a restart window is open ({why}; until {until}): no code commit or merge to main until the /cc session "
    "says 'deploy done'. Docs-only commits ({docs}) still pass. Commit on your branch and land after. A stale window "
    "(the deploy-run died) expires by itself; the /cc session can remove {path}."
)


def restart_window(cfg: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """The open window ``deploy-run`` wrote (owner quote, units, expires), or None (no file, unreadable, expired)."""
    spec = cfg.get("prompts")
    path = str(spec.get("restart_window") or "") if isinstance(spec, dict) else ""
    if not path:
        return None  # off until the config names the file (tests never touch the live one)
    try:
        with open(path) as f:
            win = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(win, dict) or (parse_iso(str(win.get("expires") or "")) or 0) <= (now or time.time()):
        return None
    return {**win, "path": path}


def docs_only(files: list[str], prefixes: tuple[str, ...] = DOCS_ONLY_PREFIXES) -> bool:
    return bool(files) and all(os.path.normpath(f).startswith(prefixes) for f in files)


def landing_guard(payload: dict[str, Any], cfg: dict[str, Any], git: GitRunner = run_git) -> str | None:
    """PreToolUse Bash: the block message for a code commit or merge to the prompts repo's main while a restart window
    is open, else None. Never raises (fails open). Docs-only commits and branches other than main pass."""
    try:
        spec = cfg.get("prompts")
        if not isinstance(spec, dict) or not spec.get("repo"):
            return None
        win = restart_window(cfg)
        if not win:
            return None
        repo = os.path.realpath(str(spec["repo"]))
        command = str((payload.get("tool_input") or {}).get("command") or "")
        cwd = str(payload.get("cwd") or "")
        prefixes = tuple(spec.get("docs_only_prefixes") or DOCS_ONLY_PREFIXES)
        for git_dir, rules in _expected(command, cwd, None, cfg).items():
            if os.path.realpath(git_dir) != repo or branch_of(git_dir, git) != "main":
                continue
            for rule in rules:
                if rule.get("status"):
                    continue  # command_center.py's own docs(prompts) commits
                if "paths" in rule:  # a git commit: its pathspec, else what is staged
                    files = [os.path.relpath(os.path.join(git_dir, f), repo) for f in rule.get("paths") or []]
                    if not files:
                        files = git(git_dir, "diff", "--cached", "--name-only")[1].splitlines()
                        if _COMMIT_ALL.search(command):  # -a / --all also commits tracked edits not yet staged
                            files += git(git_dir, "diff", "--name-only")[1].splitlines()
                    if docs_only(files, prefixes):
                        continue
                why = str(win.get("owner_ok") or "deploy-run")[:80]
                return LANDING_BLOCK_REASON.format(why=f"owner: {why}", until=win.get("expires"),
                                                   docs=", ".join(prefixes), path=win["path"])
        return None
    except Exception as e:
        log_error("landing_guard", e)
        return None


# --- verify gate (owner 11-01) --------------------------------------------------------------------

VERIFY_RECORD = "verified.jsonl"
VERIFY_BLOCK_REASON = (
    "BLOCKED: no /verify covers what this restart ships ({why}). Run /verify on main's code since the last "
    "verified restart, then record it: python3 /opt/claude-code-hooks-mastery/scripts/verify_record.py "
    "--summary \"<verify result>\". (Outage: a unit that is down can always be restarted; override: "
    "NEUROGEN_VERIFY_OVERRIDE=\"<reason>\", logged.)"
)


def record_verify(sha: str, summary: str, session_id: str = "") -> dict[str, Any]:
    """Append one /verify record: the sha of main it covers. The verify gate reads the newest ones."""
    rec = {"ts": now_iso(), "session_id": session_id, "sha": sha, "summary": summary}
    d = tracker_dir() / "deploys"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / VERIFY_RECORD, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def _verify_records(limit: int = 20) -> list[dict[str, Any]]:
    try:
        with open(tracker_dir() / "deploys" / VERIFY_RECORD) as f:
            lines = f.read().splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):  # newest first
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and re.fullmatch(r"[0-9a-f]{7,40}", str(rec.get("sha") or "")):
            out.append(rec)
    return out


def _deploy_run(run: list[str]) -> bool:
    """`command_center.py deploy-run` without --dry-run: it restarts units itself, out of this hook's sight."""
    for i, w in enumerate(run[:3]):
        if w.endswith("command_center.py") and run[i + 1:i + 2] == ["deploy-run"]:
            return "--dry-run" not in run
    return False


def _unverified_code(repo: str, branch: str, paths: list[str], git: GitRunner) -> str | None:
    """None when the newest verify record covers the code on `branch`, else why not."""
    records = _verify_records()
    if not records:
        return "no verify recorded yet"
    for rec in records:
        rc, _ = git(repo, "merge-base", "--is-ancestor", rec["sha"], branch, ok_codes=(0, 1, 128))
        if rc != 0:
            continue  # a branch sha, or one this repo doesn't have
        _, out = git(repo, "diff", "--name-only", rec["sha"], branch, "--", *paths)
        changed = out.split()
        if not changed:
            return None
        more = f" and {len(changed) - 3} more" if len(changed) > 3 else ""
        return (f"code changed on {branch} since the verify of {rec['sha'][:9]}: {', '.join(changed[:3])}{more}; "
                f"what to verify: git -C {repo} diff {rec['sha'][:9]}..{branch} -- {' '.join(paths)}")
    return f"no verify record is on {branch}"


def verify_gate(payload: dict[str, Any], cfg: dict[str, Any], is_active: Callable[[str], str] = _systemctl_is_active,
                git: GitRunner = run_git) -> str | None:
    """PreToolUse Bash: the block message for a restart that ships code no /verify has covered, else None.

    Off unless ``board_gate.repo`` is set. A restart is ``restart_patterns`` (default: restart_web.sh, systemctl
    restart/stop/start neurogenserver*) at the command word, or ``command_center.py deploy-run`` without
    --dry-run. It passes while no ``board_gate.code_paths`` file changed on the branch since the newest
    ``deploys/verified.jsonl`` record on it (scripts/verify_record.py, the last step of /verify). Always allowed:
    an outage (every unit it names is down) and ``NEUROGEN_VERIFY_OVERRIDE=<reason>`` (logged). Never raises.
    """
    try:
        gate = cfg.get("board_gate")
        repo = str(gate.get("repo") or "") if isinstance(gate, dict) else ""
        if not repo:
            return None
        command = str((payload.get("tool_input") or {}).get("command") or "")
        patterns = list(cfg.get("restart_patterns") or DEFAULT_RESTART_PATTERNS)
        sid = str(payload.get("session_id") or "")
        for seg in _segments(command):
            for run in _executed(seg):
                match = deploy_matches(" ".join(run), patterns)
                if not match and not _deploy_run(run):
                    continue
                env = {}
                for w in seg:  # the assignments before the command word (a `bash -c` run has none in seg)
                    if w == run[0]:
                        break
                    if _ENV_ASSIGN.match(w):
                        k, v = w.split("=", 1)
                        env[k] = v
                if env.get("NEUROGEN_VERIFY_OVERRIDE", "").strip():
                    _log_override(sid, env["NEUROGEN_VERIFY_OVERRIDE"].strip(), (match or ["deploy-run"])[0],
                                  kind="verify")
                    continue
                units = _restart_units(run) if match else []
                if units and all(is_active(u) != "active" for u in units):
                    continue  # an outage: nothing live to ship over
                why = _unverified_code(repo, str(gate.get("branch") or "main"),
                                       list(gate.get("code_paths") or []), git)
                if why:
                    return VERIFY_BLOCK_REASON.format(why=why)
        return None
    except Exception as e:
        log_error("verify_gate", e)
        return None


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
    if session.get("board_gate_blocked") or open_agents(session):  # CC11: like the status and progress gates
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
    if open_agents(session):  # CC10: background agents still working; ask when they are done, not mid-work
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


DEFAULT_PROGRESS_REASON = (
    "You committed code on `{prompt}` since its last update. Say how far it is: `python3 {script} set {ref} "
    "running --progress <done>/{total}` (or the final status, as above)."
)


def progress_gate_reason(payload: dict[str, Any], session: dict[str, Any] | None, cfg: dict[str, Any]) -> str | None:
    """Ask once for ``--progress`` when the claimed prompt is still running and the session committed code after the
    prompt's ``updated`` (claim, last ``set``). It waits until the status gate has had its one turn (it runs first,
    so the two never ask in the same stop)."""
    spec = cfg.get("prompts")
    if not isinstance(spec, dict) or not spec.get("gate") or not session or payload.get("stop_hook_active"):
        return None
    if session.get("progress_gate_blocked") or not session.get("prompt_gate_blocked"):
        return None
    if not session.get("prompt_claimed") or not session.get("prompt_file") or open_agents(session):
        return None
    import importlib

    pc = importlib.import_module(_PROMPT_CENTER_MODULE)
    repo = Path(str(spec["repo"]))
    rel = str(session["prompt_file"])
    if not (repo / rel).exists():
        rel = pc.moved_map(spec).get(rel, rel)
    text = (repo / rel).read_text(errors="replace")
    meta, body, _ = pc.parse_frontmatter(text)
    if meta.get("status") != "running" or str(meta.get("session_id") or "") != str(session.get("session_id")):
        return None
    since = parse_iso(str(meta.get("updated") or "")) or 0
    code = [c for c in session.get("commits") or []
            if not _is_status_commit(c.get("subject") or "") and (parse_iso(c.get("ts")) or 0) > since]
    if not code:
        return None
    total = pc.do_items(body) or "<total>"
    script = Path(__file__).resolve().parents[3] / "scripts" / "command_center.py"
    return str(spec.get("progress_message") or DEFAULT_PROGRESS_REASON).format(
        prompt=rel, ref=os.path.basename(rel).replace(" ", "\\ "), script=script, total=total)


DEFAULT_CLAIM_REASON = (
    "This session committed code or ran a deploy, but holds no prompt, so the command center can't track it. Claim "
    "its prompt: `python3 {script} claim {ref} --session {sid8}`, or write one from the template in "
    "`Documentation/Prompts/README.md` (§ New prompt) and claim that."
)


def claim_gate_reason(payload: dict[str, Any], session: dict[str, Any] | None, cfg: dict[str, Any]) -> str | None:
    """Ask once for a claim when a session with no claimed prompt made a code commit or ran a deploy (CC8: every
    session belongs on the command center). Reading, viewing (`/cc`) and answering never ask."""
    spec = cfg.get("prompts")
    if not isinstance(spec, dict) or not spec.get("gate") or not session or payload.get("stop_hook_active"):
        return None
    if session.get("claim_gate_blocked") or session.get("prompt_claimed"):
        return None
    code = [c for c in session.get("commits") or [] if not _is_status_commit(c.get("subject") or "")]
    if not code and not session.get("deploys"):
        return None
    named = session.get("prompt_file") or session.get("prompt_mentioned")
    script = Path(__file__).resolve().parents[3] / "scripts" / "command_center.py"
    return str(spec.get("claim_message") or DEFAULT_CLAIM_REASON).format(
        script=script, ref=os.path.basename(str(named)).replace(" ", "\\ ") if named else "<module>/<ID>",
        sid8=str(session.get("session_id"))[:8])


# progress_gate runs before prompt_gate: it asks only after the status gate's one block, never in the same stop;
# claim_gate last: it asks only a session the other two leave alone (no claimed prompt)
GATES = (("board_gate", gate_reason), ("progress_gate", progress_gate_reason), ("prompt_gate", prompt_gate_reason),
         ("claim_gate", claim_gate_reason))


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
