"""What a finished prompt left behind, and whether it may go (CC14, 2026-11-02).

A worktree goes only when nothing in it would be lost: its HEAD is an ancestor of main, ``git status
--untracked-files=no`` is empty, every untracked file is noise (venv link, caches, walk artifacts) or byte-identical
on main, no process has its cwd or an open file inside it, and no prompt names its path (``proof:``, ``tests:``,
``notes:``). A branch goes with ``git branch -d`` only (git refuses an unmerged one). Sandbox copies
``/var/tmp/sbx-<module>-<ID>[-<n>]`` go when no process uses them. Everything else is a KEEP with its reason.

Used by ``command_center.py set … done|skipped|superseded`` (Do 1) and, by path, by neurogenserver's
``scripts/cleanup_dev_copies.py`` (Do 2). Never ``-D``; ``worktree remove --force`` only on a tree just found
clean (its untracked files are noise, which plain ``remove`` refuses).
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Iterable

NOISE_NAMES = {"venv", "__pycache__", ".pytest_cache", "discovery_reports", "node_modules", ".mypy_cache", ".ruff_cache"}
NOISE_PREFIXES = ("tests/browser/artifacts/",)
SBX_ROOT = "/var/tmp"


def git(repo: str | Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def worktrees(repo: str | Path) -> list[dict]:
    """``git worktree list --porcelain`` as dicts: path, head, branch (short name or None), locked, main."""
    out, cur = [], None
    for line in git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[9:], "head": "", "branch": None, "locked": False, "main": not out}
            out.append(cur)
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].removeprefix("refs/heads/")
        elif line.startswith("locked"):
            cur["locked"] = True
    return out


def is_noise(rel: str) -> bool:
    rel = rel.rstrip("/")
    return bool(NOISE_NAMES & set(rel.split("/"))) or any((rel + "/").startswith(p) for p in NOISE_PREFIXES)


def untracked_not_on_main(wt: str, main: str = "main") -> list[str]:
    """Untracked files that are neither noise nor byte-identical to the same path on ``main``."""
    out = git(wt, "ls-files", "--others", "--exclude-standard", "--directory", "-z").stdout
    left = []
    for entry in filter(None, out.split("\0")):
        if is_noise(entry):
            continue
        full = os.path.join(wt, entry)
        files = [entry] if not os.path.isdir(full) or os.path.islink(full) else [
            os.path.relpath(os.path.join(d, f), wt) for d, dirs, fs in os.walk(full)
            for f in fs if not is_noise(os.path.relpath(os.path.join(d, f), wt))]
        for rel in files:
            on_main = subprocess.run(["git", "-C", wt, "cat-file", "blob", f"{main}:{rel}"], capture_output=True)
            try:
                same = on_main.returncode == 0 and Path(wt, rel).read_bytes() == on_main.stdout
            except OSError:
                same = False
            if not same:
                left.append(rel)
    return left


def busy_paths(proc: str = "/proc") -> set[str]:
    """Every cwd and open file of every process (what a removal would pull from under it)."""
    seen = set()
    for pid in os.listdir(proc) if os.path.isdir(proc) else []:
        if not pid.isdigit():
            continue
        for link in [f"{proc}/{pid}/cwd"] + glob.glob(f"{proc}/{pid}/fd/*"):
            try:
                seen.add(os.readlink(link))
            except OSError:
                pass
    return seen


def in_use(path: str, busy: Iterable[str]) -> bool:
    path = path.rstrip("/")
    return any(b == path or b.startswith(path + "/") for b in busy)


def keep_reason(repo: str | Path, wt: dict, busy: Iterable[str], refs: dict[str, str], main: str = "main") -> str | None:
    """Why this worktree must stay, or None when nothing in it would be lost."""
    path = wt["path"]
    if wt.get("main"):
        return "the main tree"
    if wt.get("locked"):
        return "locked"
    if not os.path.isdir(path):
        return None  # already gone; `worktree prune` drops the entry
    if not wt.get("head") or git(repo, "merge-base", "--is-ancestor", wt["head"], main).returncode != 0:
        n = git(repo, "rev-list", "--count", f"{main}..{wt.get('head') or 'HEAD'}").stdout.strip() or "?"
        return f"{n} commit(s) not on {main}"
    dirty = [l for l in git(path, "status", "--porcelain", "--untracked-files=no").stdout.splitlines() if l.strip()]
    if dirty:
        return f"{len(dirty)} uncommitted change(s)"
    extra = untracked_not_on_main(path, main)
    if extra:
        return f"untracked file(s) not on {main}: " + ", ".join(extra[:3]) + (" …" if len(extra) > 3 else "")
    if in_use(path, busy):
        return "a process has its cwd or a file open in it"
    for who, text in refs.items():
        if path.rstrip("/") in text:
            return f"named by {who}"
    return None


def prompt_refs(prompts: Iterable) -> dict[str, str]:
    """``module/ID field`` → text of every prompt's proof/tests/notes (paths a prompt still needs)."""
    out = {}
    for p in prompts:
        for key in ("proof", "tests", "notes"):
            v = p.meta.get(key)
            if v:
                out[f"{p.module}/{p.id or p.name} {key}:"] = str(v)
    return out


def sbx_copies(module: str, pid: str, root: str = SBX_ROOT) -> list[str]:
    base = os.path.join(root, f"sbx-{module.lower()}-{pid}")
    return sorted(p for p in glob.glob(base) + glob.glob(base + "-*") if os.path.isdir(p))


def matches(wt: dict, repo: str | Path, shas: list[str], paths: set[str]) -> bool:
    """A worktree is the prompt's when a session that held it worked there, or its HEAD/branch tip is one of the
    prompt's commits, or is the branch side (second parent) of one of them (the ``--no-ff`` landing merge)."""
    if wt.get("main"):
        return False
    if wt["path"].rstrip("/") in paths:
        return True
    head = wt.get("head") or ""
    if not head:
        return False
    for sha in shas:
        if len(sha) >= 7 and head.startswith(sha):
            return True
        second = git(repo, "rev-parse", "--verify", "-q", f"{sha}^2").stdout.strip()
        if second and head == second:
            return True
    return False


def plan(repos: list[str], shas: list[str], session_paths: set[str], module: str, pid: str, refs: dict[str, str],
         busy: Iterable[str] | None = None, sbx_root: str = SBX_ROOT) -> list[dict]:
    """Every worktree, branch and sandbox copy the prompt left, each with action ``remove`` or ``keep`` + reason."""
    busy = busy_paths() if busy is None else set(busy)
    rows = []
    wt_paths = set()
    for repo in repos:
        for wt in worktrees(repo):
            wt_paths.add(wt["path"])
            if not matches(wt, repo, shas, session_paths):
                continue
            why = keep_reason(repo, wt, busy, refs)
            rows.append({"kind": "worktree", "repo": str(repo), "path": wt["path"], "branch": wt.get("branch"),
                         "action": "keep" if why else "remove", "why": why or "merged and clean"})
    for path in sbx_copies(module, pid, sbx_root):
        if path in wt_paths:
            continue  # a registered worktree: judged above (if it is this prompt's)
        why = "a process has its cwd or a file open in it" if in_use(path, busy) else None
        rows.append({"kind": "sandbox copy", "repo": None, "path": path, "branch": None,
                     "action": "keep" if why else "remove", "why": why or "this prompt's sandbox copy"})
    return rows


def size_of(path: str) -> int:
    out = subprocess.run(["du", "-xsb", path], capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else 0


def apply(rows: list[dict], say: Callable[[str], None] = print) -> int:
    """Remove the ``remove`` rows (worktree, then its branch with ``-d``). Returns bytes freed."""
    freed = 0
    for r in rows:
        if r["action"] != "remove":
            continue
        size = size_of(r["path"]) if os.path.exists(r["path"]) else 0
        if r["kind"] == "worktree":
            res = git(r["repo"], "worktree", "remove", "--force", r["path"])
            if res.returncode != 0:
                say(f"KEEP   {r['path']}: git worktree remove refused: {res.stderr.strip()[:200]}")
                continue
            if r.get("branch"):
                b = git(r["repo"], "branch", "-d", r["branch"])
                say(f"REMOVE branch {r['branch']}" if b.returncode == 0
                    else f"KEEP   branch {r['branch']}: {b.stderr.strip()[:200]}")
        else:
            shutil.rmtree(r["path"], ignore_errors=True)
        freed += size
        say(f"REMOVE {r['kind']} {r['path']} ({size / 1e9:.2f} GB)")
    return freed
