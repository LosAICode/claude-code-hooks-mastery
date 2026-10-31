"""deploy-check: which units each on-main/deployed prompt's commit needs, and which of them restarted after it landed.

The path → unit map is the project's own file (``prompts.unit_map``, repo-relative; NeuroGenServer
``scripts/deploy_units.json``, which ``ops_drift.py`` reads too). A file needs the units of every rule it matches;
a ``live_at_merge`` rule is live when it lands. Patterns are git ``:(glob)`` pathspecs.

A commit lands when main's first-parent line first contains it (a branch commit: the merge, not its author time).
A unit is live for a prompt when it started after every commit of the prompt that needs it landed. Unit start times
are read with ``systemctl show`` (read-only: nothing here restarts anything).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

CHECKED = ("on-main", "deployed")
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")


def glob_regex(pattern: str) -> re.Pattern[str]:
    """git ``:(glob)``: ``*`` and ``?`` stay inside one folder, ``**/`` is any folders, a trailing ``/**`` all inside."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("/**", i) and i + 3 == len(pattern):
            out.append("/.*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i + 2:]:
            j = pattern.index("]", i + 2)
            body = pattern[i + 1:j]
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body).replace("\\", "\\\\") + "]")
            i = j + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def load_map(spec: dict[str, Any]) -> dict[str, Any] | None:
    rel = spec.get("unit_map")
    if not rel:
        return None
    with open(Path(str(spec["repo"])) / str(rel)) as f:
        data = json.load(f)
    groups = data.get("groups") or {}
    for rule in data.get("rules") or []:
        rule["_in"] = [glob_regex(p) for p in rule.get("paths") or []]
        rule["_out"] = [glob_regex(p) for p in rule.get("exclude") or []]
        rule["_units"] = [u for name in rule.get("units") or [] for u in groups.get(name, [name])]
    data["order"] = list(dict.fromkeys(u for rule in data.get("rules") or [] for u in rule["_units"]))
    return data


def needs(files: list[str], umap: dict[str, Any]) -> tuple[set[str], bool]:
    """(the units these files need, whether any of them is live at merge)."""
    units: set[str] = set()
    at_merge = False
    for f in files:
        for rule in umap.get("rules") or []:
            if any(r.fullmatch(f) for r in rule["_in"]) and not any(r.fullmatch(f) for r in rule["_out"]):
                units.update(rule["_units"])
                at_merge = at_merge or bool(rule.get("live_at_merge"))
    return units, at_merge


def compact(units: list[str], umap: dict[str, Any]) -> list[str]:
    """A group whose units are all listed shows as its name (``web``)."""
    out = list(units)
    for name, members in (umap.get("groups") or {}).items():
        if members and all(m in out for m in members):
            at = out.index(members[0])
            out = [u for u in out if u not in members]
            out.insert(min(at, len(out)), name)
    return out


def unit_started(unit: str) -> int | None:
    """Epoch seconds the unit became active, or None when it is not running."""
    try:
        out = subprocess.run(["systemctl", "show", "--timestamp=unix", "-p", "ActiveState,ActiveEnterTimestamp", unit],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    stamp = props.get("ActiveEnterTimestamp", "").lstrip("@")
    if props.get("ActiveState") != "active" or not stamp.isdigit():
        return None
    return int(stamp)


class History:
    """main's first-parent line, read once, and where a commit landed on it."""

    def __init__(self, repo: str, branch: str = "main") -> None:
        self.repo, self.branch = repo, branch
        out = self._git("log", "--first-parent", "--format=%H %ct", branch) or ""
        rows = [line.split() for line in out.splitlines() if line]
        self.first_parent = {sha: int(ct) for sha, ct in rows}
        self.position = {sha: i for i, (sha, _) in enumerate(rows)}  # 0 = main's tip; higher = older

    def _git(self, *args: str) -> str | None:
        try:
            p = subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return p.stdout if p.returncode == 0 else None

    def landing(self, ref: str) -> tuple[dict[str, Any] | None, str | None]:
        """({sha, landed, files}, None), or (None, why it can't be checked)."""
        full = (self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") or "").strip()
        if not full:
            return None, f"{ref} is not in this repo"
        if full in self.first_parent:
            landed = self.first_parent[full]
            base = (self._git("rev-parse", "--verify", "--quiet", f"{full}^1") or "").strip()
        else:
            if self._git("merge-base", "--is-ancestor", full, self.branch) is None:
                return None, f"{ref} is not on {self.branch}"
            # its descendants on the first-parent line; the oldest one merged it (--first-parent would also
            # restrict --ancestry-path to first-parent edges and find nothing)
            after = (self._git("rev-list", "--ancestry-path", f"{full}..{self.branch}") or "").split()
            merges = [sha for sha in after if sha in self.first_parent]
            if not merges:
                return None, f"{ref}: no landing commit found on {self.branch}"
            merge = max(merges, key=self.position.__getitem__)
            landed = self.first_parent[merge]
            base = (self._git("merge-base", f"{merge}^1", full) or "").strip()
        raw = (self._git("diff", "-z", "--name-only", "--no-renames", base, full) if base
               else self._git("show", "-z", "--name-only", "--format=", full)) or ""
        return {"sha": full, "landed": landed, "files": [f for f in raw.split("\0") if f.strip()]}, None


def commits_of(meta: dict[str, Any]) -> list[str]:
    value = meta.get("commit")
    text = " ".join(str(v) for v in value) if isinstance(value, list) else str(value or "")
    return list(dict.fromkeys(_SHA.findall(text)))


def check(prompts: list[Any], spec: dict[str, Any], umap: dict[str, Any], history: History) -> list[dict[str, Any]]:
    starts: dict[str, int | None] = {}

    def start(unit: str) -> int | None:
        if unit not in starts:
            starts[unit] = unit_started(unit)
        return starts[unit]

    out = []
    for p in prompts:
        if p.status not in CHECKED:
            continue
        e: dict[str, Any] = {"module": p.module, "id": p.id, "name": p.name, "rel": p.rel, "status": p.status,
                             "commits": [], "needs": [], "live": [], "waits": [], "starts": {}, "landed": None,
                             "live_since": None, "half_since": None, "reason": None}
        out.append(e)
        refs = commits_of(p.meta)
        skipped = []
        need_by: dict[str, int] = {}  # unit -> the latest landing among the commits that need it
        at_merge = False
        for ref in refs:
            got, why = history.landing(ref)
            if got is None:
                skipped.append(why)
                continue
            units, merge_live = needs(got["files"], umap)
            at_merge = at_merge or merge_live
            e["commits"].append({"sha": got["sha"][:9], "landed": got["landed"], "needs": sorted(units)})
            for u in units:
                need_by[u] = max(need_by.get(u, 0), got["landed"])
        if not e["commits"]:
            e["state"] = "unchecked"
            e["reason"] = "; ".join(skipped) if skipped else "no commit in its frontmatter"
            continue
        e["landed"] = max(c["landed"] for c in e["commits"])
        e["reason"] = "; ".join(skipped) or None
        order = umap.get("order") or []
        e["needs"] = sorted(need_by, key=lambda u: (order.index(u) if u in order else len(order), u))
        e["starts"] = {u: start(u) for u in e["needs"]}
        e["live"] = [u for u in e["needs"] if (start(u) or 0) >= need_by[u]]
        e["waits"] = [u for u in e["needs"] if u not in e["live"]]
        if not e["needs"]:
            e["state"] = "live" if at_merge else "no-unit"
            e["live_since"] = e["landed"] if at_merge else None
        elif not e["waits"]:
            e["state"] = "live"
            e["live_since"] = max(int(start(u) or 0) for u in e["needs"])
        elif e["live"]:
            e["state"] = "half"
            e["half_since"] = min(int(start(u) or 0) for u in e["live"])
        else:
            e["state"] = "waiting"
    return out
