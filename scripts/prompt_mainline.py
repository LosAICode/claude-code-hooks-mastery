"""The main-line guard (CC11): main fast-forwarded to a branch that had merged main.

Live 11-01 00:48: A03 ran ``git merge main`` in ``a03_widget`` and main was fast-forwarded to it, so the merges
``06ea8ab38`` and ``d328522f0`` ("Merge branch 'main' into a03_widget") sit on main's first-parent line and every main
commit since the fork left it (landing times moved to 00:48). Two signals, read-only:

- a first-parent commit whose subject is git's own "Merge branch 'main' into …";
- a reflog move of main whose old tip is not on main's first-parent line now (a custom merge message).

Landing a branch: from the live tree, ``git merge --no-ff <branch>``; never fast-forward main to a branch that merged
main.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

# the local main merged into a branch; not ``origin/main`` merged into main itself (the push-main sync: main stays
# its first parent)
MERGE_OF_MAIN = r"^Merge branch '{branch}' into "
STATUS_COMMIT_PREFIX = "docs(prompts):"


def _git(repo: str, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def findings(repo: str, branch: str = "main", since: float | None = None) -> list[dict[str, Any]]:
    """[{sha, subject, ct, how}] newest first: the first-parent commits that put a branch on main's line, since
    ``since`` (epoch seconds; None = all)."""
    out = _git(repo, "log", "--first-parent", "--format=%H%x00%ct%x00%s", branch) or ""
    rows = [line.split("\0", 2) for line in out.splitlines() if line.count("\0") == 2]
    line = {sha: (int(ct), subject) for sha, ct, subject in rows}
    pattern = re.compile(MERGE_OF_MAIN.format(branch=re.escape(branch)))
    found: dict[str, dict[str, Any]] = {}
    for sha, ct, subject in rows:
        if (since is None or int(ct) >= since) and pattern.match(subject):
            found[sha] = {"sha": sha, "subject": subject, "ct": int(ct), "how": "merge of main on the main line"}
    log = _git(repo, "log", "-g", "--date=unix", "--format=%H%x00%gd", f"refs/heads/{branch}") or ""
    moves = [r.split("\0", 1) for r in log.splitlines() if "\0" in r]
    for (new, sel), (old, _) in zip(moves, moves[1:]):
        when = re.search(r"@\{(\d+)\}", sel)
        if since is not None and (not when or int(when.group(1)) < since):
            continue
        if new in line and old not in line and new not in found and _git(
                repo, "merge-base", "--is-ancestor", old, branch) is not None:
            ct, subject = line[new]
            found[new] = {"sha": new, "subject": subject, "ct": ct, "how": f"main moved past {old[:9]}, which left "
                                                                           "its first-parent line"}
    return sorted(found.values(), key=lambda f: -f["ct"])


def newest_landing(repo: str, sha: str, root: str, branch: str = "main") -> bool:
    """Whether ``sha`` is still main's newest landing: every first-parent commit after it is a status commit or
    touches only the prompts tree."""
    out = _git(repo, "log", "--first-parent", "--format=%x01%s", "--name-only", f"{sha}..{branch}")
    if out is None:
        return False
    prefix = root.strip("/") + "/"
    for chunk in out.split("\x01")[1:]:
        subject, *files = [x for x in chunk.splitlines() if x.strip()] or [""]
        if subject.startswith(STATUS_COMMIT_PREFIX) or (files and all(f.startswith(prefix) for f in files)):
            continue
        return False
    return True


def line_text(f: dict[str, Any]) -> str:
    return (f"main line: {f['sha'][:9]} \"{f['subject'][:70]}\" put a branch on main's first-parent line "
            f"({f['how']}): land with `git merge --no-ff <branch>`, never fast-forward main to a branch that merged it")
