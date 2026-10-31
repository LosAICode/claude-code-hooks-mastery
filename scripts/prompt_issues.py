"""issues: one GitHub issue per prompt (owner rule 2026-10-30). The prompt's frontmatter ``issue:`` names it.

An issue names a prompt by its body's ``Prompt: <path>`` line (an old path resolves through ``_moved.json``), or else
by its title, ``[Module] ID: …`` / ``Module ID: …``. Read-only on GitHub except ``set --comment`` / ``--close``;
nothing here creates an issue.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import prompt_center as pc

_TITLE = re.compile(r"^\s*(?:\[(?P<label>[^\]]+)\]|(?P<word>[A-Za-z][\w-]*))\s*(?P<id>[A-Z]{0,3}\d+[a-z]?(?:-\d+)?)\s*:")
_PROMPT_LINE = re.compile(r"^\s*Prompt:\s*(.+)$", re.MULTILINE)
OPEN_WORK = ("pending", "running", "blocked", "on-branch")  # a closed issue here is a mismatch


def issues_of(meta: dict[str, Any]) -> list[int]:
    """The issue numbers of a prompt's ``issue:`` key (an int, a string, or a list; ``#`` allowed)."""
    value = meta.get("issue")
    text = " ".join(str(v) for v in value) if isinstance(value, list) else str(value or "")
    return [int(n) for n in re.findall(r"\d+", text)]


def gh(repo: str, *args: str) -> str:
    return subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True, timeout=60, check=True).stdout


def fetch_issues(repo: str) -> list[dict[str, Any]]:
    return json.loads(gh(repo, "issue", "list", "--state", "all", "--limit", "5000", "--json", "number,title,state,body"))


def _module_of(label: str, modules: set[str], aliases: dict[str, str]) -> str | None:
    for cand in [label, label.replace(" ", "-"), *reversed(label.split())]:
        m = pc._alias(cand, aliases) or cand.lower()
        if m in modules:
            return m
    return None


def prompt_of(issue: dict[str, Any], prompts: list[pc.Prompt], cfg: dict[str, Any]) -> pc.Prompt | None:
    by_rel = {p.rel: p for p in prompts}
    line = _PROMPT_LINE.search(str(issue.get("body") or ""))
    if line:
        rel = pc.resolve_prompt(line.group(1), cfg, prompts)
        if rel in by_rel:
            return by_rel[rel]
    m = _TITLE.match(str(issue.get("title") or ""))
    if not m:
        return None
    spec = pc.prompts_cfg(cfg) or {}
    module = _module_of(m.group("label") or m.group("word"), {p.module for p in prompts}, dict(spec.get("module_aliases") or {}))
    hits = [p for p in prompts if p.module == module and (p.id or "").lower() == m.group("id").lower()]
    return hits[0] if len(hits) == 1 else None


def label(p: pc.Prompt) -> str:
    return f"{p.module} {p.id or p.name}"


def audit(prompts: list[pc.Prompt], issues: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, list[Any]]:
    by_number = {int(i["number"]): i for i in issues}
    named: dict[str, list[dict[str, Any]]] = {}
    for issue in sorted(issues, key=lambda i: int(i["number"])):
        p = prompt_of(issue, prompts, cfg)
        if p is not None:
            named.setdefault(p.rel, []).append(issue)
    out: dict[str, list[Any]] = {"missing": [], "unlinked": [], "duplicates": [], "mismatches": [], "not_found": []}
    for p in prompts:
        listed = issues_of(p.meta)
        if not listed and p.status not in pc.NOT_COUNTED:
            out["missing"].append((p, named.get(p.rel, [])))
        for issue in named.get(p.rel, []):
            if int(issue["number"]) not in listed:
                out["unlinked"].append((issue, p, listed))
        if len(named.get(p.rel, [])) > 1:
            out["duplicates"].append((p, named[p.rel]))
        for n in listed:
            issue = by_number.get(n)
            if issue is None:
                out["not_found"].append((n, p))
            elif p.status in pc.CLOSED and issue["state"] == "OPEN":
                out["mismatches"].append(f"{label(p)} is {p.status} but #{n} is open")
            elif p.status in OPEN_WORK and issue["state"] == "CLOSED":
                out["mismatches"].append(f"{label(p)} is {p.status} but #{n} is closed")
    return out


def render(found: dict[str, list[Any]]) -> str:
    def state(i: dict[str, Any]) -> str:
        return f"#{i['number']} ({str(i['state']).lower()})"

    sections = [
        ("No issue", [f"  {label(p):<22}{p.name}" + (f"  ← {', '.join(state(i) for i in hits)} by title"
                                                       if hits else "") for p, hits in found["missing"]]),
        ("Issues naming a prompt its frontmatter doesn't list",
         [f"  #{i['number']} names {label(p)}; its frontmatter lists " + (", ".join(f"#{n}" for n in listed) or "none")
          for i, p, listed in found["unlinked"]]),
        ("Duplicates (one prompt, several issues)",
         [f"  {label(p)}: " + ", ".join(state(i) for i in hits) for p, hits in found["duplicates"]]),
        ("State mismatches", [f"  {m}" for m in found["mismatches"]]),
        ("Not on GitHub", [f"  #{n} ({label(p)}) is not on GitHub" for n, p in found["not_found"]]),
    ]
    lines = []
    for title, rows in sections:
        if rows:
            lines += ["", f"{title} ({len(rows)})", *rows]
    return "\n".join(lines).strip("\n") or "every prompt has its issue; nothing to report"
