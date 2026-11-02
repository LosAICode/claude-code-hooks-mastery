#!/usr/bin/env python3
"""Prompt command center: every session prompt per module, its status, and who is running it.

Reads the prompt files (frontmatter status) and the session tracker files, and joins them.

    python3 scripts/command_center.py                     # summary per module + running / stalled / blocked / next
    python3 scripts/command_center.py --module funnels    # every prompt of one module
    python3 scripts/command_center.py --all               # every prompt of every module
    python3 scripts/command_center.py --json
    python3 scripts/command_center.py --board             # regenerate the module README tables + the root board
    python3 scripts/command_center.py --html              # write the HTML dashboard, print its path
    python3 scripts/command_center.py set <id|module/id|file> <status> [--commit SHA] [--deployed TS]
                                          [--notes TEXT] [--needs-owner TEXT] [--comment] [--close] [--no-commit]
    python3 scripts/command_center.py issues [--module M] [--fix [--no-commit]]  # prompts × GitHub issues
    python3 scripts/command_center.py audit [--module M]       # exit 1 when it finds something
    python3 scripts/command_center.py map-check [--unit U] [--map FILE] [--json]  # imports no unit-map rule covers (exit 1)
    python3 scripts/command_center.py board-check             # the hand-written Status Board vs the prompts (read-only)
    python3 scripts/command_center.py board-markers [--no-commit]   # once: a generated Prompts: line per module row
    python3 scripts/command_center.py claim <ref> --session SID [--no-commit]   # attach a live session by hand
    python3 scripts/command_center.py deploy-check [--module M] [--json] [--apply [--no-commit]]
    python3 scripts/command_center.py inbox [--module M] [--json]        # every open owner ask, oldest first
    python3 scripts/command_center.py deploy-plan [--module M] [--json]  # guards, migrations, restart order, proofs (prints only)
    python3 scripts/command_center.py deploy-ready [--module M] [--json] # is a deploy safe now (reads only; exit 1 = WAIT)
    python3 scripts/command_center.py deploy-run --owner-ok "<quote>" [--only STEP] [--dry-run]  # the restarts, proven
    python3 scripts/command_center.py proofs [--module M] [--json]       # deployed prompts and what proves them, free first
    python3 scripts/command_center.py next [--parallel] [--json]         # pending prompts that can start now, and why not the rest
    python3 scripts/command_center.py migrate [--module M] [--dry-run] [--force]
    python3 scripts/command_center.py hold <module/ID> --owner "<quote>" [--no-commit]  # deploy-ready WAITs, deploy-run refuses
    python3 scripts/command_center.py unhold <module/ID> [--no-commit]
    python3 scripts/command_center.py request <module/ID> [--note TEXT]   # a peer asks for a deploy, then messages /cc
    python3 scripts/command_center.py requests [--all] [--json]           # the deploy requests (--all: done ones too)
    python3 scripts/command_center.py deploy-ready --answer <request id>  # records the verdict on that request

Config: the ``prompts`` block of the session tracker config (see docs/command-center.md).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "utils"))
import prompt_center as pc  # noqa: E402
import prompt_issues as pi  # noqa: E402
from session_tracker import attach_prompt, load_config, release_prompt, tracker_dir  # noqa: E402

BEGIN, END = "<!-- status:begin -->", "<!-- status:end -->"
COLUMNS = [("order", 5), ("id", 6), ("status", 16), ("needs owner", 18), ("session", 16), ("commit", 10),
           ("issue", 9), ("updated", 10), ("title", 40)]
TABLE_HEADER = "| # | Prompt | Status | Owner | Commit | Issue | Updated | Notes |\n|---|---|---|---|---|---|---|---|\n"
BOARD_HEADER = "| Module | Done | Progress | Running | Pending | Blocked |\n|---|---|---|---|---|---|\n"


# --- helpers ---------------------------------------------------------------------------------------


def age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def meta_str(p: pc.Prompt, key: str) -> str:
    v = p.meta.get(key)
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v or "")


def commit_cell(p: pc.Prompt) -> str:
    """The ``commits`` list, else the older ``commit`` text as written."""
    return meta_str(p, "commits") or meta_str(p, "commit")


def run_cell(runs: list[dict[str, Any]]) -> str:
    live = [r for r in runs if r.get("live")]
    if live:
        return f"{live[0]['sid8']} {age(live[0]['age_s'])}"
    return f"{runs[0]['sid8']} {runs[0]['state']}" if runs else ""


class View:
    """Everything the renderers need, loaded once."""

    def __init__(self, cfg: dict[str, Any], now: float) -> None:
        self.cfg, self.now = cfg, now
        self.spec = pc.prompts_cfg(cfg)
        self.prompts = pc.scan(cfg)
        d = tracker_dir()
        self.sessions = pc.load_sessions(d) if d.is_dir() else []
        self.runs = pc.join(self.prompts, self.sessions, cfg, now)
        self.modules = pc.progress(self.prompts, self.runs)

    def runs_of(self, p: pc.Prompt) -> list[dict[str, Any]]:
        return self.runs.get(p.rel, [])

    def status_of(self, p: pc.Prompt) -> str:
        return pc.effective_status(p, self.runs_of(p))


# --- terminal ----------------------------------------------------------------------------------------


def module_line(m: dict[str, Any]) -> str:
    c = m["counts"]
    parts = [f"{m['finished']}/{m['total']} done"]
    if c.get("on-main"):
        parts.append(f"{c['on-main']} on main (held)")
    if c.get("on-branch"):
        parts.append(f"{c['on-branch']} on branch")
    if m["running"]:
        who = ", ".join(f"{r['sid8']}, {age(r['age_s'])}" if r else "?" for _, r in m["running"])
        parts.append(f"{len(m['running'])} running ({who})")
    if m.get("active"):
        who = ", ".join(f"{r['sid8']}, {age(r['age_s'])}" for _, r in m["active"])
        parts.append(f"{len(m['active'])} active ({who})")
    if c.get("pending"):
        parts.append(f"{c['pending']} pending")
    if c.get("blocked"):
        parts.append(f"{c['blocked']} blocked")
    if c.get("stalled"):
        parts.append(f"{c['stalled']} stalled")
    return f"{m['module']:<14}" + " · ".join(parts)


def _row(module: str, p: pc.Prompt, tail: str) -> str:
    return f"  {module:<14}{(p.id or ''):<7}{fit(p.title or p.name, 36):<36}  {tail}".rstrip()


def session_rows(view: View) -> list[dict[str, Any]]:
    """One row per live session, WAITING ON YOU first, then newest activity first (CC8: every live session is on the
    command center, whatever its prompt's status, or with a hint to claim one)."""
    live = pc.live_session_ids(view.sessions, view.now)
    by_rel = {p.rel: p for p in view.prompts}
    prompt_of: dict[str, pc.Prompt] = {}
    for p in view.prompts:
        for r in view.runs_of(p):
            prompt_of.setdefault(r["sid"], p)
    rows = []
    for s in view.sessions:
        sid = str(s["session_id"])
        if sid not in live:
            continue
        own = str(s.get("prompt_file") or "")  # its claimed prompt first, when the join kept it
        p = by_rel[own] if any(r["sid"] == sid for r in view.runs.get(own, [])) else prompt_of.get(sid)
        state = pc.session_display_state(s, view.now)
        seen = pc._parse_iso(s.get("last_seen")) or view.now
        row: dict[str, Any] = {
            "sid": sid, "sid8": sid[:8], "state": state, "age_s": max(0, int(view.now - seen)),
            "waiting_on": (s.get("waiting_reason") or s.get("last_tool")) if state == "WAITING ON YOU" else None,
            "prompt": None, "unclaimed": None, "branch": s.get("branch"), "worktree": s.get("worktree"),
            "last_commit": s.get("last_commit"), "task": s.get("task")}
        if p:
            row["prompt"] = {"module": p.module, "id": p.id, "rel": p.rel, "title": p.title or p.name,
                             "status": view.status_of(p), "progress": p.meta.get("progress")}
        else:
            rel = s.get("prompt_mentioned")
            rel = rel if rel in by_rel else pc.resolve_prompt(str(s.get("task") or ""), view.cfg, view.prompts)
            q = by_rel.get(rel) if rel else None
            if q and q.status in ("pending", "blocked") and rel not in (s.get("released_prompts") or []):
                ref = f"{q.module}/{q.id}" if q.id else q.name
                row["unclaimed"] = {"module": q.module, "id": q.id, "rel": q.rel,
                                    "claim": f"command_center.py claim {ref} --session {sid[:8]}"}
        rows.append(row)
    rows.sort(key=lambda r: r["age_s"])
    rows.sort(key=lambda r: r["state"] != "WAITING ON YOU")
    return rows


def session_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {"live": len(rows), "working": sum(r["state"] == "working" for r in rows),
            "waiting": sum(r["state"] == "WAITING ON YOU" for r in rows), "idle": sum(r["state"] == "idle" for r in rows),
            "no_prompt": sum(r["prompt"] is None for r in rows)}


def session_what(r: dict[str, Any]) -> str:
    """The prompt part of a Sessions row: the prompt and its status, the claim to paste, or ``no prompt``."""
    p, u = r["prompt"], r["unclaimed"]
    if p:
        tail = f" {p['progress']}" if p.get("progress") else ""
        return f"{p['module']} {p['id'] or p['rel'].rsplit('/', 1)[-1]} {fit(p['title'], 36)} · {p['status']}{tail}"
    if u:
        ref = f"{u['module']}/{u['id']}" if u["id"] else u["rel"].rsplit("/", 1)[-1]
        return f"unclaimed: {ref} → {u['claim']}"
    return "no prompt" + (f" · {fit(str(r['task']), 40)}" if r.get("task") else "")


def session_line(r: dict[str, Any]) -> str:
    where = r.get("branch") or os.path.basename(str(r.get("worktree") or "")) or ""
    extras = [f"waits on: {r['waiting_on']}" if r.get("waiting_on") else "", f"@{where}" if where else "",
              f"last {r['last_commit']}" if r.get("last_commit") else ""]
    return f"  {r['sid8']}  {r['state']} {age(r['age_s'])}  {session_what(r)}" + "".join(f" · {x}" for x in extras if x)


PARKED_AFTER_S = 24 * 3600


def parked(view: View) -> list[tuple[str, pc.Prompt, str, int]]:
    """(module, prompt, branch tip, age) per on-branch prompt no live session holds, unmoved for 24 h (report only)."""
    out = []
    for m in view.modules:
        for p, status in m["prompts"]:
            if status != "on-branch" or any(r.get("live") for r in view.runs_of(p)):
                continue
            since = pc._parse_iso(p.meta.get("updated"))
            if since is None:
                try:
                    since = p.path.stat().st_mtime
                except OSError:
                    continue
            if view.now - since >= PARKED_AFTER_S:
                out.append((m["module"], p, (pc.commits_of(p.meta) or ["-"])[-1], int(view.now - since)))
    return sorted(out, key=lambda x: -x[3])


def render_terminal(view: View) -> str:
    mods = view.modules
    running = sum(len(m["running"]) for m in mods)
    stalled = sum(len(m["stalled"]) for m in mods)
    blocked = sum(len(m["blocked"]) for m in mods)
    rows = session_rows(view)
    n = session_counts(rows)
    stamp = datetime.fromtimestamp(view.now).strftime("%Y-%m-%d %H:%M")
    lines = [f"Prompt command center · {stamp} · {running} running · {stalled} stalled · {blocked} blocked on owner"
             f" · {n['live']} live · {n['working']} working · {n['waiting']} waiting on you · {n['idle']} idle"
             f" · {n['no_prompt']} without a prompt", ""]
    lines += [module_line(m) for m in mods]
    if not mods:
        lines.append("(no prompts found; check the prompts block of the tracker config)")
    deploy = deploy_line(view)
    if deploy:
        lines += ["", deploy]
    if rows:
        lines += ["", "Sessions"] + [session_line(r) for r in rows]
    lines += request_lines(view)
    sections = [
        ("Stalled (frontmatter says running, session gone)",
         [(m["module"], p, _last_run(view.runs_of(p), p)) for m in mods for p in m["stalled"]]),
        ("Blocked on owner", blocked_rows(view, [(m["module"], p, meta_str(p, "needs_owner") or meta_str(p, "notes"))
                                                for m in mods for p in m["blocked"]])),
        ("Parked (on-branch, no live session, unmoved 24 h+)",
         [(mod, p, f"tip {tip} · {age(secs)}") for mod, p, tip, secs in parked(view)]),
        ("Next up", [(m["module"], m["next"], f"⚑ needs owner: {owner}" if owner else "")
                     for m, owner in sorted(((m, owner_ask(m["next"])) for m in mods if m["next"]),
                                            key=lambda mo: bool(mo[1]))]),
    ]
    for title, rows in sections:
        if rows:
            lines += ["", title] + [_row(mod, p, tail) for mod, p, tail in rows]
    return "\n".join(lines)


def blocked_rows(view: View, rows: list[tuple[str, pc.Prompt, str]]) -> list[tuple[str, pc.Prompt, str]]:
    """The blocked prompts, plus every open prompt the owner holds, each with the owner's words (CC10)."""
    import prompt_ready as pr

    rows = list(rows)
    at = {p.rel: i for i, (_, p, _) in enumerate(rows)}
    for p in view.prompts:
        quote = pr.hold_quote(p.meta)
        if quote is None or p.status in pc.CLOSED:
            continue
        tail = f'held by the owner ("{quote}")'
        if p.rel in at:
            mod, q, was = rows[at[p.rel]]
            rows[at[p.rel]] = (mod, q, f"{tail} · {was}" if was else tail)
        else:
            rows.append((p.module, p, tail))
    return rows


def request_lines(view: View) -> list[str]:
    """The view's Inbox: open deploy requests, oldest first (CC10)."""
    import prompt_requests as pq

    rows = [r for r in pq.load() if r.get("state") == "open"]
    if not rows:
        return []
    out = ["", f"Inbox: deploy requests ({len(rows)} open, oldest first; `deploy-ready --answer <id>` answers one)"]
    for r in rows:
        since = pc._parse_iso(r.get("ts"))
        waited = f"{age(view.now - since)} ago" if since is not None else "?"
        out.append(f"  {r['id']}  {str(r.get('prompt')):<22}{str(r.get('session') or '-')[:8]} · {waited}"
                   + (f" · {r['note']}" if r.get("note") else ""))
    return out


def deploy_line(view: View) -> str | None:
    """``Deploy: OK | WAIT (<first reason>) · last deploy <age>`` from the cheap checks only (freeze file, locks,
    merge state, memory): the view runs no psql and no systemctl. None without a unit map."""
    import prompt_deploy as pd
    import prompt_ready as pr
    import prompt_run as prun

    try:
        umap = pd.load_map(view.spec)
    except (OSError, ValueError):
        return None
    if umap is None:
        return None
    ok, waits = pr.verdict(pr.cheap(umap, str(view.spec["repo"])))
    last = prun.last_deploy()
    return ("Deploy: OK" if ok else f"Deploy: WAIT ({waits[0]})") + (
        f" · last deploy {age(view.now - last)} ago" if last else " · last deploy never")


def owner_ask(p: pc.Prompt) -> str:
    """What the prompt waits on the owner for, or "" when it needs nothing (``no``, empty, absent)."""
    v = meta_str(p, "needs_owner").strip()
    return "" if v.lower() in ("", "no", "none", "false", "-", "—") else v


def _last_run(runs: list[dict[str, Any]], p: pc.Prompt) -> str:
    if runs:
        r = runs[0]
        return f"{r['sid8']}  {r['state']} {age(r['age_s'])} ago"
    return f"{str(p.meta.get('session_id') or '')[:8]}  no tracker file"


def render_module(view: View, modules: list[dict[str, Any]], show_all: bool) -> str:
    out = []
    for m in modules:
        out += [module_line(m), ""]
        out.append("  ".join(name.ljust(w) for name, w in COLUMNS).rstrip())
        for p, status in m["prompts"]:
            if status in pc.NOT_COUNTED and not show_all:
                continue
            cells = [p.order or "", p.id or "", status + progress_tail(p).replace("  ", " "), meta_str(p, "needs_owner"), run_cell(view.runs_of(p)),
                     commit_cell(p), ", ".join(str(n) for n in pi.issues_of(p.meta)), meta_str(p, "updated")[:10],
                     p.title or p.name]
            out.append("  ".join(fit(c, w).ljust(w) for c, (_, w) in zip(cells, COLUMNS)).rstrip())
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_json(view: View, with_prompts: bool = False) -> str:
    mods = view.modules

    live = pc.live_session_ids(view.sessions, view.now)

    def brief(p: pc.Prompt) -> dict[str, Any]:
        sid = p.meta.get("session_id")
        return {"module": p.module, "id": p.id, "name": p.name, "rel": p.rel, "session_id": sid,
                "session_live": bool(sid) and str(sid) in live, "updated": p.meta.get("updated"),
                "progress": p.meta.get("progress")}

    data = {
        "now": datetime.fromtimestamp(view.now).astimezone().isoformat(timespec="seconds"),
        "modules": [{"module": m["module"], "total": m["total"], "finished": m["finished"], "counts": m["counts"],
                     "next": m["next"].id if m["next"] else None} for m in mods],
        "running": [{**brief(p), "session": r["sid8"] if r else None, "state": r["state"] if r else None,
                     "age_s": r["age_s"] if r else None} for m in mods for p, r in m["running"]],
        "stalled": [brief(p) for m in mods for p in m["stalled"]],
        "blocked": [{**brief(p), "needs_owner": meta_str(p, "needs_owner")} for m in mods for p in m["blocked"]],
        "sessions": session_rows(view),
        "parked": [{**brief(p), "tip": tip, "age_s": secs} for _, p, tip, secs in parked(view)],
    }
    data["session_counts"] = session_counts(data["sessions"])
    if with_prompts:
        data["prompts"] = [{"module": p.module, "id": p.id, "name": p.name, "rel": p.rel, "title": p.title,
                            "status": p.status, "effective": view.status_of(p), "legacy": bool(p.legacy),
                            "meta": p.meta} for p in view.prompts]
    return json.dumps(data, indent=1, default=str)


# --- markdown: module README tables + the root board ---------------------------------------------------


def replace_between_markers(text: str, block: str) -> str | None:
    """The text with everything between the markers replaced, or None when the markers are missing."""
    a, b = text.find(BEGIN), text.find(END)
    if a == -1 or b == -1 or b < a:
        return None
    return text[: a + len(BEGIN)] + "\n" + block.rstrip("\n") + "\n" + text[b:]


def render_module_table(view: View, m: dict[str, Any]) -> str:
    rows = [TABLE_HEADER.rstrip("\n")]
    for p, status in m["prompts"]:
        runs = view.runs_of(p)
        live = [r for r in runs if r.get("live")]
        st = f"{pc.MARKS.get(status, '')} {status}".strip()
        if status in ("running", "stalled"):
            who = live[0]["sid8"] if live else str(p.meta.get("session_id") or (runs[0]["sid8"] if runs else ""))[:8]
            st += f" ({who})" if who else ""
        st += progress_tail(p).replace("  ", " ")
        commit = commit_cell(p)
        cells = [p.order or "", f"`{p.name}`", st, meta_str(p, "needs_owner"), f"`{commit}`" if commit else "",
                 ", ".join(f"#{n}" for n in pi.issues_of(p.meta)), meta_str(p, "updated")[:10], _cell(meta_str(p, "notes") + (" · todo: confirm" if p.meta.get("todo") else ""))]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip(" ·")


def module_readme(view: View, module: str) -> Path | None:
    folder = pc.module_folder(view.spec, module)
    if folder is None:
        return None
    for path in sorted(folder.iterdir()):
        if pc.INDEX_NAME.match(path.name) and path.is_file():
            return path
    return None


def render_board(view: View) -> str:
    stamp = datetime.fromtimestamp(view.now).strftime("%Y-%m-%d %H:%M")
    lines = [f"Updated {stamp} by `command_center.py --board`. Status marks: " +
             " ".join(f"{pc.MARKS[s]} {s}" for s in ("pending", "running", "on-branch", "on-main", "deployed", "done", "blocked")) + ".",
             "", BOARD_HEADER.rstrip("\n")]
    board_dir = (Path(str(view.spec["repo"])) / str(view.spec.get("board") or "")).parent
    for m in view.modules:
        readme = module_readme(view, m["module"])
        name = m["module"]
        if readme:
            try:
                name = f"[{m['module']}]({os.path.relpath(readme, board_dir)})"
            except ValueError:
                pass
        pct = int(round(100 * m["finished"] / m["total"])) if m["total"] else 0
        bar = "█" * (pct // 20) + "░" * (5 - pct // 20)
        running = ", ".join(f"{r['sid8']} {p.id or p.name}" if r else (p.id or p.name) for p, r in m["running"])
        stalled = ", ".join(f"⚠ {p.id or p.name}" for p in m["stalled"])
        blocked = ", ".join(p.id or p.name for p in m["blocked"])
        cells = [name, f"{m['finished']}/{m['total']}", f"{bar} {pct}%", " · ".join(x for x in (running, stalled) if x),
                 str(m["counts"].get("pending", 0)) + (f" (next {m['next'].id})" if m["next"] and m["next"].id else ""), blocked]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text() == text:
            return False
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
    return True


def regenerate(view: View, modules: list[str] | None = None) -> tuple[list[Path], list[str]]:
    """Rewrite the status block of each module README (and the board). Returns (changed paths, notes)."""
    changed, notes = [], []
    for m in view.modules:
        if modules and m["module"] not in modules:
            continue
        readme = module_readme(view, m["module"])
        if not readme:
            continue
        new = replace_between_markers(readme.read_text(), render_module_table(view, m))
        if new is None:
            notes.append(f"{readme.name}: no status markers ({BEGIN} … {END}); left alone")
        elif write_if_changed(readme, new):
            changed.append(readme)
    board = view.spec.get("board")
    if board:
        path = Path(str(view.spec["repo"])) / str(board)
        if path.is_file():
            new = replace_between_markers(path.read_text(), render_board(view))
            if new is None:
                notes.append(f"{path.name}: no status markers; left alone")
            elif write_if_changed(path, new):
                changed.append(path)
        else:
            notes.append(f"{board}: missing (create it with the markers to get the board)")
    sb, note = refresh_status_board(view)
    changed += [sb] if sb else []
    notes += [note] if note else []
    return changed, notes


# --- html ----------------------------------------------------------------------------------------------

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f24;--muted:#6b7280;--line:#e5e7eb;--bar:#e5e7eb;
--done:#15803d;--deployed:#ca8a04;--onmain:#2563eb;--onbranch:#7c3aed;--running:#0891b2;--pending:#9ca3af;--blocked:#dc2626;--stalled:#ea580c}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--ink:#e6e8ec;--muted:#9aa3b2;--line:#2a2f3a;--bar:#2a2f3a}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 16px}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.card h2{font-size:16px;margin:0 0 6px;display:flex;justify-content:space-between}.card h2 small{color:var(--muted);font-weight:400}
.progress{height:8px;background:var(--bar);border-radius:4px;overflow:hidden;margin:6px 0 10px}.progress i{display:block;height:100%;background:var(--done)}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:3px 6px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;border-top:0}.s{white-space:nowrap;font-weight:600}
.s-done{color:var(--done)}.s-deployed{color:var(--deployed)}.s-on-main{color:var(--onmain)}.s-on-branch{color:var(--onbranch)}
.s-running{color:var(--running)}.s-pending{color:var(--pending)}.s-blocked{color:var(--blocked)}.s-stalled{color:var(--stalled)}
.s-skipped,.s-superseded{color:var(--muted);text-decoration:line-through}.muted{color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 16px}.chip{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:13px}
"""


def render_html(view: View) -> str:
    e = html.escape
    stamp = datetime.fromtimestamp(view.now).strftime("%Y-%m-%d %H:%M")
    running = [(m["module"], p, r) for m in view.modules for p, r in m["running"]]
    stalled = [(m["module"], p) for m in view.modules for p in m["stalled"]]
    blocked = [(m["module"], p) for m in view.modules for p in m["blocked"]]
    chips = [f"<span class=chip>▶ {len(running)} running</span>", f"<span class=chip>⚠ {len(stalled)} stalled</span>",
             f"<span class=chip>🔴 {len(blocked)} blocked on owner</span>"]
    rows = session_rows(view)
    n = session_counts(rows)
    chips.append(f"<span class=chip>{n['live']} live · {n['waiting']} waiting on you · {n['no_prompt']} without a prompt</span>")
    cards = []
    if rows:
        cards.append("<section class=card><h2>Sessions <small>waiting on you first</small></h2><table>"
                     + "".join(f"<tr><td><code>{e(r['sid8'])}</code></td><td>{e(r['state'])} {e(age(r['age_s']))}</td>"
                               f"<td>{e(session_what(r))}</td><td>{e(r.get('waiting_on') or '')}</td>"
                               f"<td>{e(r.get('branch') or '')}</td><td><code>{e(r.get('last_commit') or '')}</code></td></tr>"
                               for r in rows) + "</table></section>")
    held = parked(view)
    if held:
        cards.append("<section class=card><h2>Parked <small>on-branch, no live session, unmoved 24 h+</small></h2><table>"
                     + "".join(f"<tr><td>{e(mod)} <b>{e(p.id or '')}</b> {e(p.title or p.name)}</td><td><code>{e(tip)}</code></td>"
                               f"<td>{e(age(secs))}</td></tr>" for mod, p, tip, secs in held) + "</table></section>")
    for m in view.modules:
        pct = int(round(100 * m["finished"] / m["total"])) if m["total"] else 0
        rows = []
        for p, status in m["prompts"]:
            runs = view.runs_of(p)
            who = run_cell(runs)
            if status == "stalled" and not who:
                who = str(p.meta.get("session_id") or "")[:8]
            rows.append(
                f"<tr><td class=muted>{e(p.order or '')}</td><td><b>{e(p.id or '')}</b> {e(p.title or p.name)}"
                f"<br><span class=muted>{e(p.name)}</span></td>"
                f"<td class='s s-{e(status)}'>{e(pc.MARKS.get(status, ''))} {e(status)}{e(progress_tail(p).replace('  ', ' '))}</td>"
                f"<td>{e(who)}</td><td>{e(meta_str(p, 'needs_owner'))}</td>"
                f"<td><code>{e(commit_cell(p))}</code></td><td>{e(meta_str(p, 'notes'))}</td></tr>")
        cards.append(
            f"<section class=card><h2>{e(m['module'])} <small>{m['finished']}/{m['total']} done · {pct}%</small></h2>"
            f"<div class=progress role=progressbar aria-valuenow='{pct}' aria-valuemin=0 aria-valuemax=100><i style='width:{pct}%'></i></div>"
            f"<table><tr><th>#</th><th>Prompt</th><th>Status</th><th>Session</th><th>Owner</th><th>Commit</th><th>Notes</th></tr>"
            + "".join(rows) + "</table></section>")
    return ("<!doctype html>\n<html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Prompt command center</title><style>{_CSS}</style></head><body>"
            f"<h1>Prompt command center</h1><p class=sub>{e(stamp)} · {len(view.prompts)} prompts · {len(view.modules)} modules · "
            f"regenerate with <code>command_center.py --html</code></p><div class=chips>{''.join(chips)}</div>"
            f"<div class=grid>{''.join(cards)}</div></body></html>\n")


# --- set ------------------------------------------------------------------------------------------------


def find_prompt(view: View, ref: str) -> pc.Prompt:
    """By ``module/ID``, ``ID`` or file name. Raises LookupError (unknown or ambiguous)."""
    ref = ref.strip().strip("`")
    module = None
    if "/" in ref and not ref.endswith(".md"):
        module, ref = ref.split("/", 1)
        module = pc._alias(module, view.spec.get("module_aliases") or {}) or module.lower()
    hits = [p for p in view.prompts if (p.name == ref or p.rel == ref or (p.id or "").lower() == ref.lower())
            and (module is None or p.module == module)]
    if not hits:
        raise LookupError(f"no prompt matches {ref!r} (use module/ID or the file name)")
    if len(hits) > 1:
        raise LookupError(f"{ref!r} is ambiguous: " + ", ".join(f"{p.module}/{p.id}" for p in hits) + " (use module/ID)")
    return hits[0]


def cmd_set(view: View, args: argparse.Namespace) -> int:
    try:
        p = find_prompt(view, args.ref)
    except LookupError as e:
        print(e, file=sys.stderr)
        return 2
    if args.close and args.status not in pc.CLOSED:
        print(f"--close closes the issue of a finished prompt ({', '.join(pc.CLOSED)}), not {args.status}", file=sys.stderr)
        return 2
    tests = tests_value(p, args)
    if tests is False:
        return 2
    progress = progress_value(p, args.progress)
    if args.tests is None and args.no_tests is None and args.status == "on-main" and not p.meta.get("tests"):
        print(f"warning: no --tests: {p.module} {p.id or p.name} lands on main with no test verdict "
              "(--tests <results file> or --no-tests \"<why>\"); deploy-plan lists it as not proven tested", file=sys.stderr)
    try:
        clear = list(args.clear or []) + (["needs_owner"] if args.needs_owner == "" else [])
        pc.set_status(view.spec, p.rel, args.status, clear=clear, commit=args.commit, deployed=args.deployed,
                      notes=args.notes, needs_owner=args.needs_owner or None, tests=tests, progress=progress,
                      proof=args.proof)
        moved = pc.place(view.spec, p.rel, args.status)[1] if view.spec.get("status_folders") else []
    except (ValueError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    if args.notes is None and p.meta.get("notes"):
        print(f"notes kept: {meta_str(p, 'notes')}  (--notes replaces them)")
    if args.status in ("pending", "blocked"):
        # The session that held it lets go, or the view still shows it running (live 10-31, A12)
        for sid in {r["sid"] for r in view.runs_of(p)}:
            release_prompt(sid, p.rel)
    if moved:
        print(f"moved to {moved[-2]}")
    print(f"{p.module} {p.id or p.name}: {p.status} → {args.status}")
    commit_change(view, [p.module], [p.rel, *moved], f"docs(prompts): {p.module} {p.id or p.name} → {args.status}",
                  args.no_commit)
    if args.comment or args.close:
        return post_progress(view, p, args)
    return 0


def tests_value(p: pc.Prompt, args: argparse.Namespace) -> str | None | bool:
    """The ``tests:`` line to store: "<SUMMARY> · <results file> · <sha tested>", "<text> · <sha>", "none: <why>";
    None = leave it; False = refused (printed). The sha: --tested, else --commit, else the prompt's last commit."""
    import prompt_plan as pp

    if args.no_tests is not None:
        return f"none: {args.no_tests}"
    if args.tests is None:
        return None
    sha = args.tested or (args.commit or [None])[-1] or (pc.commits_of(p.meta) or [None])[-1]
    if not sha:
        print(f"--tests: {p.module} {p.id or p.name} names no commit; pass --tested <sha> (or --commit)", file=sys.stderr)
        return False
    path = Path(args.tests).expanduser()
    try:
        is_file = path.is_file()
    except OSError:  # live 11-01: a long text was stat'ed as a file name (ENAMETOOLONG)
        is_file = False
    if not is_file:
        looks_like_path = not re.search(r"\s", args.tests) and len(args.tests) < 256
        if looks_like_path and ("/" in args.tests or path.suffix in (".txt", ".json")):
            print(f"--tests refused: {args.tests} is not a file", file=sys.stderr)  # a missing results file
            return False
        return f"{args.tests[:300]} · {sha[:9]}"
    summary, why = pp.read_verdict(path)
    if summary is None:
        print(f"--tests refused: {why}", file=sys.stderr)
        return False
    return f"{summary} · {path.resolve()} · {sha[:9]}"


def progress_value(p: pc.Prompt, value: str | None) -> str | None:
    """``3/6``, ``3/6 tests red`` as given; a bare ``3`` gets the numbered items under ``## Do`` as its total."""
    import prompt_plan as pp

    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        total = pc.do_items(pp.body_of(p))
        return f"{value}/{total}" if total else value
    return value


def progress_tail(p: pc.Prompt) -> str:
    return f"  {meta_str(p, 'progress')}" if p.meta.get("progress") else ""


def post_progress(view: View, p: pc.Prompt, args: argparse.Namespace) -> int:
    """One comment on the prompt's (first) issue: the status change, commit, deploy time and notes; --close closes it."""
    numbers = pi.issues_of(p.meta)
    if not numbers:
        print(f"{pi.label(p)} has no issue in its frontmatter; nothing posted (`issues` lists its candidates)")
        return 0
    body = " · ".join([f"{pi.label(p)}: {p.status} → {args.status}"] + [x for x in (
        f"commit `{', '.join(args.commit)}`" if args.commit else "", f"deployed {args.deployed}" if args.deployed else "",
        args.notes or "") if x])
    verb = ("close", "--comment") if args.close else ("comment", "--body")
    try:
        pi.gh(str(view.spec["repo"]), "issue", verb[0], str(numbers[0]), verb[1], body)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"NOT posted to #{numbers[0]} ({type(e).__name__}); post it yourself: {body}", file=sys.stderr)
        return 1
    print(f"#{numbers[0]}: " + ("closed with" if args.close else "commented") + f" \"{body}\"")
    return 0


def cmd_issues(view: View, args: argparse.Namespace) -> int:
    """Prompts × GitHub issues: missing, unlinked, duplicates, state mismatches. --fix writes the keys it can match."""
    try:
        issues = pi.fetch_issues(str(view.spec["repo"]))
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print(f"gh issue list failed ({type(e).__name__}); is gh logged in for this repo?", file=sys.stderr)
        return 2
    prompts = [p for p in view.prompts if not args.module or p.module == args.module]
    found = pi.audit(prompts, issues, view.cfg)
    print(render_issues_header(view, prompts, issues))
    print(pi.render(found))
    if not args.fix:
        return 0
    fixable = [(p, hits[0]) for p, hits in found["missing"] if len(hits) == 1]
    if not fixable:
        print("\n--fix: nothing to write (a prompt needs exactly one issue naming it)")
        return 0
    written = []
    for p, issue in fixable:
        if pc.write_meta(p.path, {"issue": str(issue["number"])}, expect={"issue": (None, "")}) is None:
            print(f"{pi.label(p)}: changed since the scan; left alone")
            continue
        written.append((p, issue))
        print(f"{pi.label(p)}: issue #{issue['number']}")
    fixable = written
    if not fixable:
        return 0
    labels: dict[str, list[str]] = {}
    for p, _ in fixable:
        labels.setdefault(p.module, []).append(p.id or p.name)
    what = "; ".join(f"{m} {', '.join(ids)}" for m, ids in labels.items())
    commit_change(view, list(labels), [p.rel for p, _ in fixable],
                  f"docs(prompts): issues --fix: {len(fixable)} issue keys ({what})", args.no_commit)
    return 0


def render_issues_header(view: View, prompts: list[pc.Prompt], issues: list[dict[str, Any]]) -> str:
    linked = sum(1 for p in prompts if pi.issues_of(p.meta))
    needing = sum(1 for p in prompts if pi.issues_of(p.meta) or p.status not in pc.NOT_COUNTED)
    return (f"Issues · {datetime.fromtimestamp(view.now).strftime('%Y-%m-%d %H:%M')} · {linked}/{needing} prompts "
            f"have an issue (skipped/superseded need none) · {len(issues)} issues on GitHub")


def commit_change(view: View, modules: list[str], rels: list[str], message: str, no_commit: bool) -> str | None:
    """Regenerate the modules' tables and the board, then commit those files and ``rels`` with a pathspec."""
    changed, notes = regenerate(View(view.cfg, view.now), modules)
    for n in notes:
        print(n)
    repo = Path(str(view.spec["repo"]))
    rels = list(dict.fromkeys(rels + [str(c.relative_to(repo)) for c in changed]))
    if no_commit:
        print("not committed (--no-commit); files: " + ", ".join(rels))
        return None
    sha = pc.commit_prompt_files(view.spec, rels, message)
    print(f"committed {sha[:9]}: " + ", ".join(rels) if sha else "NOT committed (git refused; commit the files yourself): " + ", ".join(rels))
    return sha


def cmd_claim(view: View, args: argparse.Namespace) -> int:
    """Attach a live session to a prompt by hand: ``running`` + ``session_id``, and the session's Stop gate applies."""
    try:
        p = find_prompt(view, args.ref)
    except LookupError as e:
        print(e, file=sys.stderr)
        return 2
    live = pc.live_session_ids(view.sessions, view.now)
    sids = sorted(s for s in live if s == args.session or s.startswith(args.session))
    if len(sids) != 1:
        why = "is ambiguous: " + ", ".join(sids) if sids else "is not a live session (no tracker file active in the last 2 h)"
        print(f"{args.session!r} {why}", file=sys.stderr)
        return 2
    sid = sids[0]
    prev = pc.mark_running(view.spec, p.rel, sid, live - {sid})
    if prev is None:
        holder = str(p.meta.get("session_id") or "")[:8]
        print(f"{p.module} {p.id or p.name} is {p.status}" + (f" ({holder}, live)" if p.status == "running" else "")
              + ": only pending, blocked or a gone session's running prompt can be claimed", file=sys.stderr)
        return 2
    attach_prompt(sid, p.rel, prev)
    moved = pc.place(view.spec, p.rel, "running")[1] if view.spec.get("status_folders") else []
    print(f"{p.module} {p.id or p.name}: {prev} → running ({sid[:8]})")
    commit_change(view, [p.module], [p.rel, *moved], f"docs(prompts): {p.module} {p.id or p.name} running ({sid[:8]}, by hand)",
                  args.no_commit)
    return 0


STALE_NOTES = re.compile(r"\b(?:queued|pending|waiting|todo|blocked)\b", re.IGNORECASE)


def audit_findings(view: View, module: str | None = None) -> list[str]:
    import prompt_deploy as pd
    import prompt_plan as pp

    prompts = [p for p in view.prompts if not module or p.module == module]
    out = []
    for p in prompts:
        raw = str(p.meta.get("status") or "pending")
        if raw in ("on-main", "deployed", "done") and not (pc.commits_of(p.meta) or p.meta.get("commit")):
            out.append(f"{pi.label(p)}: {raw} with no commit ({p.rel})")
        if p.meta.get("todo"):
            out.append(f"{pi.label(p)}: todo: {p.meta['todo']} left over ({p.rel})")
        folder = p.path.parent.name
        if folder in (pc.COMPLETED_FOLDER, pc.PENDING_FOLDER) and folder != pc.status_folder(raw):
            out.append(f"{p.name}: status {raw} but filed under {folder}/ (`command_center.py sort` moves it)")
        if (folder == pc.PENDING_FOLDER and "id" not in p.meta and p.status not in pc.NOT_COUNTED
                and not re.search(r"^##\s+Do\b", pp.body_of(p), re.MULTILINE)):
            out.append(f"{p.name}: no id and no ## Do: a reference file? add `kind: reference` to its frontmatter "
                       f"(the owner's file: report, don't move it) ({p.rel})")
        elif not p.id and p.status not in pc.NOT_COUNTED:
            out.append(f"{p.name}: no id, so `set <module>/<ID>` can't reach it (add `id:` to its frontmatter)")
        stale = STALE_NOTES.search(meta_str(p, "notes"))
        if raw in ("on-main", "deployed", "done") and stale:
            out.append(f"{pi.label(p)}: {raw} but its notes still say \"{stale.group(0)}\" (stale notes? "
                       f"`set {p.module}/{p.id or p.name} {raw} --notes '<now>'`)")
        if raw == "deployed" and not pp.proof_row(p)["proof"]:
            out.append(f"{pi.label(p)}: deployed with no proof (no `proof:` line, no live bullet under Done when), "
                       "so `proofs` can't say how to close it")
    history = pd.history_for(view.spec)
    for p in prompts:
        if str(p.meta.get("status") or "") not in ("on-main", "deployed", "done") or not p.id:
            continue
        refs = pc.commits_of(p.meta)
        for ref in refs:
            got, why = history.landing(ref)
            new = history.resolve(ref) if got is None and str(why).endswith(f"not on {history.branch}") else None
            if not new:
                continue
            if any(new.startswith(r) for r in refs if r != ref):
                out.append(f"{p.module}/{p.id}: {ref[:9]} was rebased to {new[:9]}, which its commits already list")
            else:
                out.append(f"{p.module}/{p.id}: {ref[:9]} was rebased to {new[:9]}: set {p.module}/{p.id} --commit {new[:9]}")
    out += cross_prompt_findings(view, module)
    if not module:
        import prompt_mainline as ml

        days = float(view.spec.get("main_line_days") or 3)
        out += [ml.line_text(f) for f in ml.findings(str(view.spec["repo"]), str(view.spec.get("main_branch") or "main"),
                                                     view.now - days * 86400)]
    try:
        umap = pd.load_map(view.spec)
    except (OSError, ValueError):
        umap = None
    if umap and not module:
        import prompt_mapcheck as mc

        for unit, res in mc.check(str(view.spec["repo"]), umap).items():
            line = mc.finding(unit, res)
            if line:
                out.append(line)
    for r in pp.unrecorded_commits(view.prompts, str(view.spec["repo"]),
                                   str(view.spec.get("main_branch") or "main"), umap):
        q = r["prompt"]
        if module and q.module != module:
            continue
        sha = r["sha"][:9]
        out.append(f"{pi.label(q)}: commit {sha} \"{fit(r['subject'], 70)}\" is in no prompt's commits: → "
                   f"command_center.py set {q.module}/{q.id} {q.meta.get('status') or 'pending'} --commit {sha}")
    by_id: dict[tuple[str, str], list[pc.Prompt]] = {}
    for p in prompts:
        if p.id and p.status not in pc.NOT_COUNTED:
            by_id.setdefault((p.module, p.id.lower()), []).append(p)
    for (mod, _), ps in sorted(by_id.items()):
        if len(ps) > 1:
            out.append(f"{mod} {ps[0].id}: id doubled ({', '.join(sorted(p.name for p in ps))}); "
                       "give one a distinct id or set it superseded")
    refs: list[str] = []
    pc.scan(view.cfg, references=refs)
    out += [f"reference: {rel} (kind: reference, not a prompt)" for rel in refs
            if not module or f"/{module}/" in f"/{rel.lower()}"]
    root = Path(str(view.spec["repo"])) / str(view.spec["root"])
    if root.is_dir() and not module:
        twins: dict[str, list[str]] = {}
        for folder in sorted(root.iterdir()):
            if folder.is_dir() and not folder.is_symlink() and not folder.name.startswith("_") and any(folder.rglob("*.md")):
                twins.setdefault(folder.name.lower(), []).append(str(folder.relative_to(Path(str(view.spec["repo"])))) + "/")
        for names in twins.values():
            if len(names) > 1:
                out.append(f"{' and '.join(sorted(names, reverse=True))} differ only by case (one module, two folders; "
                           "report only: the owner's files stay where they are)")
        for folder in sorted(root.iterdir()):
            if (folder.is_dir() and not folder.is_symlink() and not folder.name.startswith("_")
                    and folder.name == folder.name.lower() and any(folder.rglob("*.md"))):
                out.append(f"lower-case module folder {folder.relative_to(Path(str(view.spec['repo'])))}/ "
                           "(module folders are capitalized; move its prompts with `set` or `sort`)")
    return out


_PROMPT_ID = re.compile(r"^[A-Z]+\d+[a-z]?(?:-\d+)?$")


def commit_subjects(repo: str, shas: list[str]) -> dict[str, str]:
    """{sha as listed: its subject}, one git call; a sha not in the repo is left out."""
    shas = list(dict.fromkeys(shas))
    if not shas:
        return {}
    try:
        p = subprocess.run(["git", "-C", repo, "log", "--no-walk=unsorted", "--ignore-missing", "--format=%H%x09%s", *shas],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    full = [line.split("\t", 1) for line in (p.stdout or "").splitlines() if "\t" in line]
    return {s: subject for s in shas for h, subject in full if h.startswith(s)}


def names_prompt(subject: str, pid: str, module: str, aliases: dict[str, str], qualified: bool) -> bool:
    """The subject names ``pid`` as a token (``A04b`` not in ``A04b-2`` or ``xA04b``); with ``qualified``, its module
    (or an alias of it) is named too: a short id (``S3``, ``A1``) or one two modules share means nothing alone."""
    if not re.search(rf"(?<![\w-]){re.escape(pid)}(?![\w-])", subject):
        return False
    if not qualified:
        return True
    names = [module] + [k for k, v in aliases.items() if str(v) == module and not _PROMPT_ID.match(k)]
    return any(re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", subject, re.IGNORECASE) for n in names)


_SCOPE = re.compile(r"^\w+\(([^)]+)\)!?:")


def aliases_of(p: pc.Prompt, prompts: list[pc.Prompt]) -> set[str]:
    """Ids that name this prompt too: its ``aliases:`` and any prompt of its module listing one of its commits
    (connections ``01`` and ``C1`` are one audit, both list ``01071ccc7``)."""
    raw = p.meta.get("aliases") or []
    out = {str(a).lower() for a in (raw if isinstance(raw, list) else str(raw).replace(",", " ").split())}
    mine = set(pc.commits_of(p.meta))
    for q in prompts:
        if q is not p and q.id and q.module == p.module and mine & set(pc.commits_of(q.meta)):
            out.add(q.id.lower())
    return out


def cross_prompt_findings(view: View, module: str | None = None) -> list[str]:
    """A commit in a prompt's ``commits:`` whose subject names another prompt and not its own (CC10: M02's docs commit
    280389595 sat in R9's list). Read-only: the fix is a hand edit of ``commits:``.

    Not a hit (CC11): a merge (``Merge <branch>`` names the branch), the prompt's own base id (``A04`` for ``A04b``),
    one of its aliases, or a prompt of its own module when the subject's scope is that module (A03's
    ``test(assistants): … A01's payer check``)."""
    aliases = dict(view.spec.get("module_aliases") or {})
    index: dict[str, list[pc.Prompt]] = {}
    for p in view.prompts:
        if p.id and _PROMPT_ID.match(p.id) and p.status not in pc.NOT_COUNTED:
            index.setdefault(p.id, []).append(p)
    mine = [p for p in view.prompts if (not module or p.module == module) and p.id and pc.commits_of(p.meta)]
    shas = [s for p in mine for s in pc.commits_of(p.meta)]
    subjects = commit_subjects(str(view.spec["repo"]), shas)
    for r in view.spec.get("repos") or []:
        if isinstance(r, dict) and r.get("path"):
            subjects = {**commit_subjects(str(r["path"]), [s for s in shas if s not in subjects]), **subjects}
    out = []
    for p in mine:
        base = re.sub(r"[a-z]+$", "", p.id)
        also = aliases_of(p, view.prompts)
        for sha in pc.commits_of(p.meta):
            subject = subjects.get(sha)
            if not subject or subject.startswith("Merge ") or names_prompt(subject, p.id, p.module, aliases, len(p.id) <= 2):
                continue
            scope = _SCOPE.match(subject)
            own_scope = bool(scope) and scope.group(1).lower() in {p.module.lower(), *(
                str(k).lower() for k, v in aliases.items() if str(v) == p.module)}
            for pid, qs in sorted(index.items()):
                if pid.lower() == p.id.lower() or pid == base or pid.lower() in also:
                    continue
                for q in qs:
                    if own_scope and q.module == p.module:
                        continue
                    if names_prompt(subject, pid, q.module, aliases, len(pid) <= 2 or len(qs) > 1):
                        out.append(f'{p.module}/{p.id}: {sha[:9]} "{fit(subject, 70)}" names {q.module}/{q.id}')
    return out


def cmd_audit(view: View, args: argparse.Namespace) -> int:
    found = audit_findings(view, args.module)
    print("\n".join(found) if found else "audit: nothing to report")
    return 1 if found else 0


def cmd_map_check(view: View, args: argparse.Namespace) -> int:
    import prompt_deploy as pd
    import prompt_mapcheck as mc

    spec = {**view.spec, "unit_map": args.map} if args.map else view.spec
    try:
        umap = pd.load_map(spec)
    except (OSError, ValueError) as e:
        print(f"unit map unreadable: {e}", file=sys.stderr)
        return 2
    if umap is None or not mc.entries(umap):
        print("no deploy.entry block in the unit map (see docs/command-center.md)", file=sys.stderr)
        return 2
    if args.unit and args.unit not in mc.entries(umap):
        print(f"no deploy.entry for {args.unit!r}; entries: " + ", ".join(mc.entries(umap)), file=sys.stderr)
        return 2
    results = mc.check(str(view.spec["repo"]), umap, args.unit)
    found = any(r["gaps"] or r["missing"] for r in results.values())
    if args.json:
        print(json.dumps({"units": results}, indent=1))
        return 1 if found else 0
    lines = []
    for unit, r in results.items():
        bad = len(r["gaps"]) + len(r["missing"])
        lines.append(f"{unit}: reaches {r['reached']} file(s), " + (f"{bad} no rule maps" if bad else "no gaps"))
        lines += [f"  {f} (entry file missing)" for f in r["missing"]]
        for g in r["gaps"]:
            lines += [f"  {g['file']}", f"    {' → '.join(g['chain'])}"]
    print("\n".join(lines))
    return 1 if found else 0


_CLAUSE = re.compile(r"\s·\s|;|\||\s—\s|\.\s|[()]")
_HELD = re.compile(r"\bheld\b|\bon main\b|\bmerged\b|🔵", re.IGNORECASE)
_LIVE = re.compile(r"\bdeployed\b|\blive\b(?!\s+(?:proof|walk|check|probe|data|call|test|socket))|🟢|🟡", re.IGNORECASE)
NOT_LIVE = ("pending", "running", "on-branch", "on-main", "blocked")
PROMPTS_MARK = re.compile(r"(<!-- prompts:(?P<mod>[\w-]+) -->)(.*?)(<!-- /prompts -->)")


def row_module(name: str, modules: set[str], aliases: dict[str, str]) -> str | None:
    """The first module a Status Board row's first cell names (``Funnels backlog B1-B12`` → funnels)."""
    for w in re.findall(r"[A-Za-z][\w-]*", name):
        m = pc._alias(w, aliases) or w.lower()
        if m in modules:
            return m
    return None


def prompts_line(m: dict[str, Any]) -> str:
    """The generated one-line summary a Status Board row carries between its prompts markers (no time stamp:
    it changes only when a status does)."""
    by: dict[str, list[pc.Prompt]] = {}
    for p, s in sorted(m["prompts"], key=lambda ps: (pc._order_key(ps[0].order), ps[0].name)):
        by.setdefault(s, []).append(p)
    c = m["counts"]
    parts = [f"🟢 {m['finished']}/{m['total']} done or deployed ({c.get('done', 0)} done, {c.get('deployed', 0)} deployed)"]
    for status in ("running", "stalled", "on-branch", "on-main", "blocked"):
        if by.get(status):
            parts.append(f"{pc.MARKS[status]} {status} " + ", ".join(p.id or p.name for p in by[status]))
    if by.get("pending"):
        parts.append(f"⚪ {len(by['pending'])} pending" + (f" (next {m['next'].id})" if m["next"] and m["next"].id else ""))
    return "**Prompts:** " + " · ".join(parts) + "."


def status_board_clean(view: View) -> bool:
    """False when the hand-written board has uncommitted edits (another session's lines we must not commit)."""
    repo = str(view.spec["repo"])
    try:
        if subprocess.run(["git", "-C", repo, "rev-parse", "--git-dir"], capture_output=True, timeout=20).returncode != 0:
            return True  # not a git repo: no one else's commit to protect (and `git diff` would compare without an index)
        return subprocess.run(["git", "-C", repo, "diff", "--quiet", "HEAD", "--", str(view.spec["status_board"])],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def fill_markers(view: View, text: str) -> str:
    mods = {m["module"]: m for m in view.modules}
    return PROMPTS_MARK.sub(lambda mt: mt.group(1) + prompts_line(mods[mt.group("mod")]) + mt.group(4)
                            if mt.group("mod") in mods else mt.group(0), text)


def refresh_status_board(view: View) -> tuple[Path | None, str | None]:
    """Rewrite the text between each ``<!-- prompts:<module> -->`` / ``<!-- /prompts -->`` pair; nothing else."""
    rel = view.spec.get("status_board")
    path = Path(str(view.spec["repo"])) / str(rel or "")
    if not rel or not path.is_file():
        return None, None
    text = path.read_text()
    if not PROMPTS_MARK.search(text):
        return None, None
    new = fill_markers(view, text)
    if new == text:
        return None, None
    if not status_board_clean(view):
        return None, f"{rel}: has uncommitted edits (another session's?); its Prompts: lines not refreshed"
    write_if_changed(path, new)
    return path, None


def cmd_board_markers(view: View, args: argparse.Namespace) -> int:
    """Put a prompts marker at the start of the State cell of the first row per module that lacks one."""
    rel = view.spec.get("status_board")
    if not rel:
        print("no status board: set prompts.status_board in the tracker config", file=sys.stderr)
        return 2
    if not status_board_clean(view):
        print(f"{rel} has uncommitted edits (another session's?); commit or wait, then rerun", file=sys.stderr)
        return 2
    path = Path(str(view.spec["repo"])) / str(rel)
    lines = path.read_text().split("\n")
    modules = {m["module"] for m in view.modules}
    have = {mt.group("mod") for mt in PROMPTS_MARK.finditer("\n".join(lines))}
    added = []
    for i, line in enumerate(lines):
        cells = line.strip().strip("|").split("|")
        if not line.startswith("|") or len(cells) < 2:
            continue
        module = row_module(cells[0].strip(), modules, dict(view.spec.get("module_aliases") or {}))
        if module and module not in have and cells[0].strip() not in ("Workstream", "Module"):
            at = line.index("|", 1) + 1
            lines[i] = line[:at] + f" <!-- prompts:{module} --><!-- /prompts -->" + line[at:]
            have.add(module)
            added.append(module)
    if not added:
        print("markers added: none (every module row has one)")
        return 0
    # filled here: once written, the board is dirty and the refresh in commit_change would skip it
    write_if_changed(path, fill_markers(view, "\n".join(lines)))
    print("markers added: " + ", ".join(added))
    commit_change(view, [], [str(rel)], f"docs(status-board): generated Prompts: line per module row ({', '.join(added)})",
                  args.no_commit)
    return 0


def board_rows(text: str) -> list[tuple[str, str]]:
    """[(first cell, the State cell)] of every Markdown table row but headers and separators.

    Only the State (second) cell is a status claim; Next and Detail hold plans ("then the held deploys …").
    """
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 2 and cells[0] and not set(cells[0]) <= set("-:") \
                and cells[0] not in ("Workstream", "Module"):
            rows.append((cells[0], re.sub(r"`[^`]*`", "", PROMPTS_MARK.sub("", cells[1]))))
    return rows


def board_mismatches(view: View, text: str) -> list[str]:
    """A row that says HELD/ON MAIN for a deployed/done prompt, or DEPLOYED/LIVE for one that is not live yet.

    A row belongs to the first module its first cell names; a claim is a clause of its State cell (split at ``·``,
    ``;``, ``—``, ``. ``, parentheses) that names the prompt's id and only one kind of status word. Ranges
    (``H1-H6``) and bare numbers are skipped.
    """
    aliases = dict(view.spec.get("module_aliases") or {})
    by_module: dict[str, list[pc.Prompt]] = {}
    for p in view.prompts:
        if p.id and not p.id.isdigit() and p.status not in pc.NOT_COUNTED:
            by_module.setdefault(p.module, []).append(p)
    out = []
    for name, row in board_rows(text):
        module = row_module(name, set(by_module), aliases)
        if module is None:
            continue
        for clause in _CLAUSE.split(row):
            held, live = bool(_HELD.search(clause)), bool(_LIVE.search(clause))
            if held == live:
                continue
            for p in by_module[module]:
                if not re.search(rf"(?<![\w-]){re.escape(p.id)}(?![\w-])", clause):
                    continue
                if held and p.status in ("deployed", "done"):
                    claim = "HELD/ON MAIN"
                elif live and p.status in NOT_LIVE:
                    claim = "DEPLOYED/LIVE"
                else:
                    continue
                out.append(f"{name}: says {claim} for {module} {p.id}, which is {p.status}: «{fit(clause.strip(), 100)}»")
    return list(dict.fromkeys(out))


def cmd_board_check(view: View, args: argparse.Namespace) -> int:
    rel = view.spec.get("status_board")
    if not rel:
        print("no status board: set prompts.status_board in the tracker config", file=sys.stderr)
        return 2
    try:
        text = (Path(str(view.spec["repo"])) / str(rel)).read_text()
    except OSError as e:
        print(f"{rel}: {e}", file=sys.stderr)
        return 2
    found = board_mismatches(view, text)
    print("\n".join(found) if found else f"board-check: every {rel} row agrees with its prompts")
    return 1 if found else 0


def cmd_sort(view: View, args: argparse.Namespace) -> int:
    """File every prompt under its module's Completed/ or Pending/ folder; a live running prompt stays put."""
    live = pc.live_session_ids(view.sessions, view.now)
    repo = Path(str(view.spec["repo"]))
    paths: list[str] = []
    count = 0
    for p in view.prompts:
        if p.legacy:
            continue
        sid = str(p.meta.get("session_id") or "")
        target = p.path.parent if p.path.parent.name in (pc.COMPLETED_FOLDER, pc.PENDING_FOLDER) else None
        want = pc.status_folder(p.status)
        if target is not None and target.name == want:
            continue
        if p.status == "running" and sid in live:
            print(f"skipped {p.name} (running, {sid[:8]})")
            continue
        if args.dry_run:
            parts = Path(p.rel).parts
            root_parts = Path(str(view.spec["root"])).parts
            top = Path(*parts[: len(root_parts) + 1])
            if len(parts) > len(root_parts) + 1:
                print(f"{p.name} → {top / want / p.name}")
            continue
        try:
            new, touched = pc.place(view.spec, p.rel, p.status)
        except OSError as e:
            print(e, file=sys.stderr)
            continue
        if touched:
            print(f"{p.name} → {new}")
            paths.extend(touched)
            count += 1
    if args.dry_run or not paths:
        print("dry run: nothing moved" if args.dry_run else "nothing to move")
        return 0
    changed, notes = regenerate(View(view.cfg, view.now))
    for n in notes:
        print(n)
    rels = list(dict.fromkeys(paths + [str(c.relative_to(repo)) for c in changed]))
    if args.no_commit:
        print(f"not committed (--no-commit); {len(rels)} paths")
        return 0
    sha = pc.commit_prompt_files(view.spec, rels, f"docs(prompts): sort {count} prompts into Completed/ and Pending/")
    print(f"committed {sha[:9]}" if sha else "NOT committed (git refused; commit the files yourself)")
    return 0


def cmd_prompts(view: View, args: argparse.Namespace) -> int:
    """Every prompt filed in Pending/ or Completed/, full paths by module; one whose status belongs in the other
    folder (or in no folder yet) is flagged. 0 every file in its place, 1 something for `sort` to move."""
    want = pc.PENDING_FOLDER if args.folder == "pending" else pc.COMPLETED_FOLDER
    if args.module and args.module not in {m["module"] for m in view.modules}:
        print(f"no prompts in module {args.module!r}; modules: " + ", ".join(m["module"] for m in view.modules), file=sys.stderr)
        return 2
    rows: dict[str, list[str]] = {}
    misfiled = 0
    live = pc.live_session_ids(view.sessions, view.now)
    for p in view.prompts:
        if p.legacy or (args.module and p.module != args.module) or (args.status and p.status not in args.status):
            continue
        filed = p.path.parent.name if p.path.parent.name in (pc.COMPLETED_FOLDER, pc.PENDING_FOLDER) else None
        belongs = pc.status_folder(p.status)
        if (filed or belongs) != want:
            continue
        note = ""
        if filed != belongs:
            if p.status == "running" and str(p.meta.get("session_id") or "") in live:
                note = "  (live session: `sort` moves it later)"  # sort leaves it too
            else:
                note = f"  ⚠ not filed (belongs in {belongs}/)" if filed is None else f"  ⚠ belongs in {belongs}/"
                misfiled += 1
        rows.setdefault(p.module, []).append(f"  {pc.MARKS.get(p.status, '?')} {p.id or '-':<6} {p.status:<10} "
                                             f"{p.path.resolve()}{note}")
    total = sum(len(r) for r in rows.values())
    print(f"{want}/ prompts · {total} in {len(rows)} modules" + (f" · {misfiled} misfiled" if misfiled else ""))
    for module in sorted(rows):
        print(f"\n{module} ({len(rows[module])})")
        print("\n".join(rows[module]))
    if misfiled:
        print(f"\n{misfiled} prompt(s) out of place: `command_center.py sort` files them by status.")
    return 1 if misfiled else 0


# --- deploy-check ---------------------------------------------------------------------------------------

DEPLOY_SECTIONS = (("live", "Live in every unit it needs"), ("half", "Half live"),
                   ("waiting", "Waiting (no needed unit restarted since it landed)"),
                   ("no-unit", "No unit needed (docs, tests, scripts)"), ("unchecked", "Not checked"))


def _hm(epoch: float | None) -> str:
    return datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M") if epoch else "?"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def sha_cell(commits: list[dict[str, Any]]) -> str:
    """The commits as shas; a rebased one as ``old → new (rebased)`` (CC9: R9's shas changed in a rebase)."""
    return ", ".join(f"{c['rebased_from']} → {c['sha']} (rebased)" if c.get("rebased_from") else c["sha"] for c in commits)


def render_deploy(results: list[dict[str, Any]], umap: dict[str, Any], now: float) -> str:
    import prompt_deploy as pd

    starts = {u: s for e in results for u, s in e["starts"].items()}
    units = " · ".join(f"{u} {_hm(s) if s else 'not running'}" for u, s in
                       sorted(starts.items(), key=lambda us: (umap["order"].index(us[0]) if us[0] in umap["order"] else 99)))
    lines = [f"Deploy check · {datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M')} · {len(results)} on-main/deployed prompts",
             f"unit starts: {units or '(none needed)'}"]
    for state, title in DEPLOY_SECTIONS:
        rows = [e for e in results if e["state"] == state]
        if not rows:
            continue
        lines += ["", f"{title} ({len(rows)})"]
        for e in rows:
            label = f"{e['module']} {e['id'] or e['name'].removesuffix('.md')}"
            head = f"  {fit(label, 27):<28}{e['status']:<10}"
            if state == "unchecked":
                lines.append(head + str(e["reason"]))
                continue
            shas = sha_cell(e["commits"])
            tail = [f"{shas} landed {_hm(e['landed'])}"]
            if state == "live":
                tail.append(f"live since {_hm(e['live_since'])}" + (f" ({', '.join(pd.compact(e['needs'], umap))})"
                                                                    if e["needs"] else " (at merge)"))
            elif state in ("half", "waiting"):
                if e["live"]:
                    tail.append("live: " + ", ".join(pd.compact(e["live"], umap)))
                groups = umap.get("groups") or {}

                def started(unit: str) -> str:
                    times = [e["starts"].get(u) for u in groups.get(unit, [unit])]
                    return f"started {_hm(min(times))}" if all(times) else "not running"

                tail.append("waits: " + ", ".join(f"{u} ({started(u)})" for u in pd.compact(e["waits"], umap)))
            if state == "live" and e["status"] == "on-main":
                tail.append("→ deployed with --apply")
            if e.get("reason"):
                tail.append(f"skipped {e['reason']}")
            lines.append(head + " · ".join(tail))
    return "\n".join(lines)


def cmd_deploy_check(view: View, args: argparse.Namespace) -> int:
    import prompt_deploy as pd

    try:
        umap = pd.load_map(view.spec)
    except (OSError, ValueError) as e:
        print(f"unit map unreadable: {e}", file=sys.stderr)
        return 2
    if umap is None:
        print("no unit map: set prompts.unit_map in the tracker config (see docs/command-center.md)", file=sys.stderr)
        return 2
    prompts = [p for p in view.prompts if not args.module or p.module == args.module]
    history = pd.history_for(view.spec)
    results = pd.check(prompts, umap, history)
    if args.json:
        print(json.dumps({"now": _iso(view.now), "units": {u: s for e in results for u, s in e["starts"].items()},
                          "prompts": results}, indent=1))
        return 0
    print(render_deploy(results, umap, view.now))
    if not args.apply:
        return 0
    rels = apply_live(view, results, args.no_commit)
    if rels:
        print("\n".join(tell_lines(view, rels)))
    return 0


def apply_live(view: View, results: list[dict[str, Any]], no_commit: bool) -> list[str]:
    """``--apply``: every on-main prompt live in each unit it needs → deployed (one commit). The rels it set."""
    ready = [e for e in results if e["state"] == "live" and e["status"] == "on-main"]
    if not ready:
        print("\n--apply: nothing to set (only an on-main prompt live in every unit it needs moves to deployed)")
        return []
    by_rel = {p.rel: p for p in view.prompts}
    rels: list[str] = []
    applied = []
    for e in ready:
        p = by_rel[e["rel"]]
        try:  # only if still on-main: never over a status another session set since the scan
            pc.set_status(view.spec, p.rel, "deployed", expect={"status": ("on-main",)}, deployed=_iso(e["live_since"]))
        except OSError as err:
            print(f"{p.module} {p.id or p.name}: left alone ({err})")
            continue
        applied.append(e)
        rels += [p.rel, *(pc.place(view.spec, p.rel, "deployed")[1] if view.spec.get("status_folders") else [])]
        print(f"{p.module} {p.id or p.name}: on-main → deployed ({_hm(e['live_since'])})")
    ready = applied
    if not ready:
        return []
    labels: dict[str, list[str]] = {}
    for e in ready:
        labels.setdefault(e["module"], []).append(e["id"] or e["name"])
    what = "; ".join(f"{m} {', '.join(ids)}" for m, ids in labels.items())
    commit_change(view, list(labels), rels, f"docs(prompts): deploy-check: {len(ready)} prompts deployed ({what})",
                  no_commit)
    return [e["rel"] for e in ready]


def tell_lines(view: View, rels: list[str]) -> list[str]:
    """Who to tell that their prompt is live, and what proof it owes: the session in its frontmatter, if live.
    The /cc session sends each line with SendMessage (CC9: peers asked "is it live?" by hand)."""
    import prompt_plan as pp

    live = pc.live_session_ids(view.sessions, view.now)
    by_sid = {str(s["session_id"]): s for s in view.sessions}
    by_rel = {p.rel: p for p in view.prompts}
    out = []
    for rel in rels:
        p = by_rel.get(rel)
        if p is None:
            continue
        sid = str(p.meta.get("session_id") or "")
        owes = pp.proof_row(p)["proof"] or "(no proof: line)"
        ref = f"{p.module}/{p.id or p.name}"
        if sid and sid in live:
            name = by_sid[sid].get("name")
            who = f"{name} ({sid[:8]})" if name else sid[:8]
        else:
            who = "(no live session)"
        out.append(f"Tell: {who} · {ref} · owes: {owes}")
    return out


# --- planning: inbox, deploy-plan, proofs, next (read-only) ------------------------------------------------


ANSWERED = re.compile(r"answered\b", re.IGNORECASE)


def paste_for(p: pc.Prompt) -> str:
    """The line that moves an owner ask on: run it, or the plan for the deploy/proof it waits on."""
    me = f"python3 {Path(__file__).resolve()}"
    if p.status == "on-main":
        return f"{me} deploy-plan --module {p.module}"
    if p.status == "deployed":
        return f"{me} proofs --module {p.module}"
    if p.status in ("running", "on-branch"):
        return f"(answer in the session running it: {str(p.meta.get('session_id') or '')[:8] or 'see the view'})"
    return f"run {p.rel}"


def board_owner_rows(view: View, text: str, module: str | None = None) -> list[dict[str, Any]]:
    """Status Board clauses that say "owner" and name a prompt with no ``needs_owner`` (report only)."""
    aliases = dict(view.spec.get("module_aliases") or {})
    by_module: dict[str, list[pc.Prompt]] = {}
    for p in view.prompts:
        if p.id and not p.id.isdigit() and p.status not in pc.CLOSED and (not module or p.module == module):
            by_module.setdefault(p.module, []).append(p)
    out = []
    for name, row in board_rows(text):
        mod = row_module(name, set(by_module), aliases)
        if mod is None:
            continue
        for clause in _CLAUSE.split(row):
            if not re.search(r"\bowner\b", clause, re.IGNORECASE):
                continue
            for p in by_module[mod]:
                if not owner_ask(p) and re.search(rf"(?<![\w-]){re.escape(p.id)}(?![\w-])", clause):
                    out.append({"module": mod, "id": p.id, "status": p.status, "row": name, "clause": clause.strip()})
    return list({(r["module"], r["id"], r["clause"]): r for r in out}.values())


def cmd_inbox(view: View, args: argparse.Namespace) -> int:
    asks = []
    for p in view.prompts:
        ask = owner_ask(p)
        if (not ask or ANSWERED.match(ask) or p.status in pc.CLOSED or (args.module and p.module != args.module)):
            continue
        since = pc._parse_iso(p.meta.get("updated"))
        if since is None:  # no updated: key yet, so since the file last changed
            try:
                since = p.path.stat().st_mtime
            except OSError:
                pass
        asks.append({"module": p.module, "id": p.id, "name": p.name, "rel": p.rel, "status": view.status_of(p), "ask": ask,
                     "cost": meta_str(p, "cost"), "updated": p.meta.get("updated"),
                     "waited_s": int(view.now - since) if since else None, "paste": paste_for(p)})
    asks.sort(key=lambda a: (-(a["waited_s"] or 0), a["rel"]))
    board: list[dict[str, Any]] = []
    rel = view.spec.get("status_board")
    if rel:
        try:
            board = board_owner_rows(view, (Path(str(view.spec["repo"])) / str(rel)).read_text(), args.module)
        except OSError:
            pass
    if args.json:
        print(json.dumps({"now": _iso(view.now), "asks": asks, "board": board}, indent=1, default=str))
        return 0
    lines = [f"Owner inbox · {datetime.fromtimestamp(view.now).strftime('%Y-%m-%d %H:%M')} · {len(asks)} open asks, oldest first"]
    for a in asks:
        waited = f"waited {age(a['waited_s'])}" if a["waited_s"] is not None else "waited ?"
        lines += ["", f"  {a['module']}/{a['id'] or a['name']}  {a['status']} · {waited}" + (f" · cost: {a['cost']}" if a["cost"] else ""),
                  f"    asks: {a['ask']}", f"    {a['paste']}"]
    if board:
        lines += ["", f"Status Board clauses naming the owner for a prompt with no needs_owner ({len(board)}, report only)"]
        lines += [f"  {b['module']} {b['id']} ({b['status']}): «{fit(b['clause'], 100)}»" for b in board]
    print("\n".join(lines))
    return 0


def _plan_inputs(view: View, args: argparse.Namespace) -> tuple[list[pc.Prompt], dict[str, Any], Any] | None:
    import prompt_deploy as pd

    try:
        umap = pd.load_map(view.spec)
    except (OSError, ValueError) as e:
        print(f"unit map unreadable: {e}", file=sys.stderr)
        return None
    if umap is None:
        print("no unit map: set prompts.unit_map in the tracker config (see docs/command-center.md)", file=sys.stderr)
        return None
    prompts = [p for p in view.prompts if not args.module or p.module == args.module]
    return prompts, umap, pd.history_for(view.spec)


def _proof_line(r: dict[str, Any]) -> str:
    what = r["proof"] or "(no proof: line and no live bullet under Done when)"
    return f"  {fit(r['module'] + ' ' + (r['id'] or r['name']), 22):<23}{r['rank']:<8}{fit(what, 110)}" + (
        f"  · cost: {r['cost']}" if r["cost"] else "")


def ship_lines(plan: dict[str, Any], umap: dict[str, Any], prod: list[str] | None, query: str | None) -> list[str]:
    """What would ship: Prompts, Migrations, Not proven tested (deploy-plan and deploy-ready print the same)."""
    import prompt_deploy as pd

    head = ", ".join(prod or [])
    lines = ["", "Prompts"]
    for e in plan["prompts"]:
        label = f"{e['module']} {e['id'] or e['name'].removesuffix('.md')}"
        shas = sha_cell(e["commits"]) or "-"
        tail = (f"waits: {', '.join(pd.compact(e['waits'], umap))}" if e["waits"] else
                "live: deploy-check --apply sets it deployed" if e["state"] == "live" else e["state"])
        lines.append(f"  {fit(label, 27):<28}{e['status']:<10}{shas} · {tail}" + (f" · {e['reason']}" if e.get("reason") else ""))
    lines += ["", "Migrations (apply BY HAND first, under the guards" + (f": {plan['migrate']}" if plan["migrate"] else "") + ")"]
    if plan["migrations"] and not prod:
        lines.append("  (alembic_version unread: " + ("its query failed" if query else "no `prod_revision` query in the "
                     "tracker config") + "; every migration that ships is listed)")
    todo = [m for m in plan["migrations"] if not m["applied_at_boot"] and not m["in_prod"]]
    lines += [f"  {m['file']}  ({m['module']} {m['id']}, {m['sha']})" for m in todo] or ["  none"]
    have = [m["file"] for m in plan["migrations"] if m["in_prod"] and not m["applied_at_boot"]]
    if have:
        lines.append(f"  (already in prod (alembic_version {head}): {', '.join(have)})")
    lines += [f"  (ran at boot, its units started after it landed: {m['file']}, {m['module']} {m['id']})"
              for m in plan["migrations"] if m["applied_at_boot"]]
    lines += ["", "Not proven tested (no passing verdict for what ships; `set … --tests <results file>`)"]
    lines += [f"  {fit(u['module'] + ' ' + (u['id'] or u['rel']), 22):<23}{u['why']}" for u in plan["untested"]] or ["  none"]
    return lines


def _build_plan(view: View, args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], list[str] | None, Any] | None:
    """(plan, unit map, prod heads, the prod query), or None when the map is unreadable (printed)."""
    import prompt_plan as pp

    got = _plan_inputs(view, args)
    if got is None:
        return None
    prompts, umap, history = got
    query = view.spec.get("prod_revision")
    prod = pp.prod_revision(query)
    return pp.deploy_plan(prompts, umap, history, prod=prod, repo=str(view.spec["repo"])), umap, prod, query


def cmd_deploy_plan(view: View, args: argparse.Namespace) -> int:
    got = _build_plan(view, args)
    if got is None:
        return 2
    plan, umap, prod, query = got
    head = ", ".join(prod or [])
    if args.json:
        print(json.dumps({"now": _iso(view.now), "prod_revision": head or None, **plan}, indent=1, default=str))
        return 0
    lines = [f"Deploy plan · {datetime.fromtimestamp(view.now).strftime('%Y-%m-%d %H:%M')} · {len(plan['prompts'])} prompts to ship "
             "· it prints, it runs nothing"]
    if plan["note"]:
        lines.append(f"note: {plan['note']}")
    lines += ship_lines(plan, umap, prod, query)
    lines += ["", "Guards (each must read as expected before the first restart)"]
    lines += [f"  {g['name']}: {g['command']}  → expect {g.get('expect', '')}" for g in plan["guards"] if not g.get("before")] or ["  none in the map"]
    lines += ["", "Restarts (one at a time, each proven before the next)"]
    for i, st in enumerate(plan["steps"], 1):
        for g in st["guards"]:
            lines.append(f"     guard first, {g['name']}: {g['command']}  → expect {g.get('expect', '')}")
        lines.append(f"  {i}. {st['unit']}")
        lines += [f"       {c}" for c in st["commands"]]
        lines += [f"       proof: {c}" for c in st["proof"]]
    if not plan["steps"]:
        lines.append("  none: every needed unit already runs these commits")
    lines += ["", f"Then: python3 {Path(__file__).resolve()} deploy-check --apply, and the proofs owed:"]
    lines += [_proof_line(r) for r in plan["proofs"]] or ["  none"]
    print("\n".join(lines))
    return 0


def _ready_lines(checks: list[dict[str, Any]]) -> list[str]:
    import prompt_ready as pr

    ok, waits = pr.verdict(checks)
    lines = [f"OK  {c['name']}" + (f" ({c['detail']})" if c["detail"] else "") if c["ok"] else
             f"WAIT: {c['name']}" + (f": {c['detail']}" if c["detail"] else "") for c in checks]
    return lines + ["", "Deploy: OK" if ok else f"Deploy: WAIT ({len(waits)} reasons)"]


def cmd_deploy_ready(view: View, args: argparse.Namespace) -> int:
    """Is a deploy safe now: the guards and ready checks (all reads), then what would ship. 0 OK, 1 WAIT, 2 no map."""
    import prompt_ready as pr
    import prompt_requests as pq

    if args.answer and not any(r["id"] == args.answer for r in pq.load()):
        print(f"--answer {args.answer}: no such deploy request (`requests --all` lists them)", file=sys.stderr)
        return 2
    got = _build_plan(view, args)
    if got is None:
        return 2
    plan, umap, prod, query = got
    asker = next((r.get("session") for r in pq.load() if r["id"] == args.answer), None) if args.answer else None
    checks = pr.ready(view, umap, plan, prod, also=[str(asker)] if asker else None)
    ok, waits = pr.verdict(checks)
    answer = answer_request(view, args.answer, "OK" if ok else "WAIT: " + "; ".join(waits)) if args.answer else None
    if args.json:
        print(json.dumps({"now": _iso(view.now), "verdict": "OK" if ok else "WAIT", "waits": waits, "checks": checks,
                          "prod_revision": ", ".join(prod or []) or None, "plan": plan,
                          **({"answered": answer} if answer else {})}, indent=1, default=str))
        return 0 if ok else 1
    lines = [f"Deploy ready? · {datetime.fromtimestamp(view.now).strftime('%Y-%m-%d %H:%M')} · {len(plan['prompts'])} prompts "
             "to ship · reads only", ""]
    lines += _ready_lines(checks)
    lines += ship_lines(plan, umap, prod, query)
    lines += ["", "Units: " + (", ".join(st["unit"] for st in plan["steps"]) or "none to restart")]
    if answer:
        lines += ["", f"request {answer['id']} answered: {answer['verdict']}"
                  + (f" · tell {str(answer.get('session'))[:8]}" if answer.get("session") else "")]
    print("\n".join(lines))
    return 0 if ok else 1


def cmd_deploy_run(view: View, args: argparse.Namespace) -> int:
    """The plan's restarts, step by step, each proven (prompt_run.py). Owner-OK only; stops on WAIT or migrations."""
    import prompt_ready as pr
    import prompt_run as prun

    owner_ok = (args.owner_ok or "").strip()
    if not owner_ok:
        print('deploy-run needs --owner-ok "<the owner\'s words>": it quotes the owner\'s OK in this session, never a '
              "peer's message", file=sys.stderr)
        return 2
    got = _build_plan(view, args)
    if got is None:
        return 2
    plan, umap, prod, query = got
    if args.only:
        names = [st["unit"] for st in plan["steps"]]
        if args.only not in names:
            print(f"--only {args.only}: not a step of the plan (steps: {', '.join(names) or 'none'})", file=sys.stderr)
            return 2
        plan = {**plan, "steps": [st for st in plan["steps"] if st["unit"] == args.only]}
        plan["units"] = [u for st in plan["steps"] for u in st["units"] if u in plan["units"]]
    checks = pr.ready(view, umap, plan, prod)
    ok, waits = pr.verdict(checks)
    print("\n".join(_ready_lines(checks)))
    todo = [m for m in plan["migrations"] if not m["applied_at_boot"] and not m["in_prod"]]
    me = f"python3 {Path(__file__).resolve()}"
    rerun = f"{me} deploy-run --owner-ok {json.dumps(owner_ok)}" + (f" --only {args.only}" if args.only else "")
    if args.dry_run:
        print("\n".join(["", *prun.dry_run_lines(plan, umap)]))
        if todo:
            print(f"\nwould stop first: {len(todo)} migrations to apply by hand ({', '.join(m['file'] for m in todo)})")
        if not ok:
            print("\nwould not run: " + "; ".join(waits))
        return 1 if not ok else 3 if todo else 0
    if not ok:
        print("\nNothing restarted. Rerun when every check is OK.")
        return 1
    if todo:
        lines = ["", "Migrations to apply BY HAND first (deploy-run never runs one): " + str(plan["migrate"] or "")]
        lines += [f"  {m['file']}  ({m['module']} {m['id']}, {m['sha']})" for m in todo]
        lines += ["", f"Then rerun: {rerun}"]
        print("\n".join(lines))
        return 3
    if not plan["steps"]:
        print("\nnothing to restart: every needed unit already runs these commits")
        rec = None
    else:
        print("")
        code, rec = prun.execute(plan, umap, str(view.spec["repo"]), owner_ok)
        if code:
            print(f"\nThen: fix it, `{me} deploy-plan`, and rerun the rest with --only <step>.")
            return code
    rels: list[str] = []
    try:
        import prompt_deploy as pd

        prompts = [p for p in view.prompts if not args.module or p.module == args.module]
        history = pd.history_for(view.spec)
        rels = apply_live(view, pd.check(prompts, umap, history), args.no_commit)
    finally:
        if rec is not None:
            prun.finish(rec, rels)
    close_requests(view, rels, rec["id"] if rec else None)
    if rec is not None:
        print("")
        for st in rec["steps"]:
            starts = [p["out"] for p in st["proofs"] if p["out"].startswith("started ")]
            print(f"  {st['unit']}: {'; '.join(starts)}")
    lines = tell_lines(view, rels)
    if lines:
        print("\n" + "\n".join(lines))
    return 0


def cmd_proofs(view: View, args: argparse.Namespace) -> int:
    import prompt_plan as pp

    rows = pp.proofs([p for p in view.prompts if not args.module or p.module == args.module])
    if args.json:
        print(json.dumps({"now": _iso(view.now), "proofs": rows}, indent=1, default=str))
        return 0
    print(f"Proofs owed · {datetime.fromtimestamp(view.now).strftime('%Y-%m-%d %H:%M')} · {len(rows)} deployed prompts, free first")
    print("\n".join(_proof_line(r) for r in rows) if rows else "  none")
    return 0


def cmd_next(view: View, args: argparse.Namespace) -> int:
    import prompt_deploy as pd
    import prompt_plan as pp

    try:
        umap = pd.load_map(view.spec)
    except (OSError, ValueError):
        umap = None
    got = pp.next_prompts(view, umap, args.parallel)
    if args.json:
        print(json.dumps({"now": _iso(view.now), **got}, indent=1, default=str))
        return 0
    lines = [f"Can start now ({len(got['start'])}" + (", in parallel)" if args.parallel else ", first per module)")]
    lines += [f"  {fit(r['module'] + '/' + (r['id'] or r['name']), 22):<23}{r['paste']}" for r in got["start"]] or ["  none"]
    lines += ["", f"Held back ({len(got['held'])})"]
    lines += [f"  {fit(r['module'] + '/' + (r['id'] or r['name']), 22):<23}{r['why']}" for r in got["held"]] or ["  none"]
    if umap is None:
        lines += ["", "no unit map: unit overlap not checked"]
    print("\n".join(lines))
    return 0


# --- owner holds and deploy requests (CC10) ------------------------------------------------------------------


def cmd_hold(view: View, args: argparse.Namespace) -> int:
    """``hold``: write ``hold: {owner, by, at}``; ``unhold``: remove it. A status commit either way; the prompt's
    deploy requests follow (open/answered → held, held → open)."""
    import prompt_ready as pr
    import prompt_requests as pq

    holding = args.cmd == "hold"
    quote = (args.owner or "").strip() if holding else ""
    if holding and not quote:
        print('hold needs --owner "<the owner\'s words>": it quotes the owner in this session, never a peer\'s message',
              file=sys.stderr)
        return 2
    try:
        p = find_prompt(view, args.ref)
    except LookupError as e:
        print(e, file=sys.stderr)
        return 2
    if not holding and pr.hold_quote(p.meta) is None:
        print(f"{p.module} {p.id or p.name} is not held", file=sys.stderr)
        return 2
    value = {"owner": quote, "by": pr.own_session() or "unknown", "at": _iso(view.now)} if holding else None
    with pc._PromptLock(pc._lock_dir(view.spec), p.rel):
        written = pc.write_meta(p.path, {"hold": value})
    if written is None:
        print(f"{p.rel} changed while writing; not written (run it again)", file=sys.stderr)
        return 2
    ref = f"{p.module}/{p.id or p.name}"
    states, to = (("open", "answered"), "held") if holding else (("held",), "open")
    moved = pq.update(lambda r: r.get("prompt") == ref and r.get("state") in states, {"state": to})
    print(f'{p.module} {p.id or p.name}: ' + (f'held by the owner ("{quote}")' if holding else "unheld")
          + (f"; {len(moved)} deploy requests → {to}" if moved else ""))
    commit_change(view, [p.module], [p.rel], f"docs(prompts): {p.module} {p.id or p.name} "
                  + ("held by the owner" if holding else "unheld"), args.no_commit)
    return 0


def cmd_request(view: View, args: argparse.Namespace) -> int:
    import prompt_ready as pr
    import prompt_requests as pq

    try:
        p = find_prompt(view, args.ref)
    except LookupError as e:
        print(e, file=sys.stderr)
        return 2
    row = pq.add(f"{p.module}/{p.id or p.name}", args.note, pr.own_session(), view.now)
    quote = pr.hold_quote(p.meta)
    if quote is not None:
        row = pq.update(lambda r: r["id"] == row["id"], {"state": "held"})[0]
    print(f"request {row['id']}: {row['prompt']} ({p.status}) {row['state']}"
          + (f'; held by the owner ("{quote}")' if quote is not None else ""))
    print(f'Now SendMessage the /cc session: "deploy request {row["id"]} ({row["prompt"]})"')
    return 0


def cmd_requests(view: View, args: argparse.Namespace) -> int:
    import prompt_requests as pq

    rows = [r for r in pq.load() if args.all or r.get("state") not in pq.CLOSED]
    if args.json:
        print(json.dumps({"now": _iso(view.now), "requests": rows}, indent=1, default=str))
        return 0
    lines = [f"Deploy requests · {datetime.fromtimestamp(view.now).strftime('%Y-%m-%d %H:%M')} · {len(rows)}"
             + (" (all)" if args.all else " not done") + ", oldest first"]
    for r in rows:
        tail = [x for x in (r.get("note"), r.get("verdict") and f"answered: {r['verdict']}",
                            r.get("deploy") and f"deploy {r['deploy']}") if x]
        lines.append(f"  {r['id']}  {str(r.get('state')):<9}{str(r.get('prompt')):<22}{str(r.get('session') or '-')[:8]}"
                     f" · {r.get('ts')}" + "".join(f" · {x}" for x in tail))
    print("\n".join(lines if rows else lines + ["  none"]))
    return 0


def answer_request(view: View, rid: str, verdict: str) -> dict[str, Any] | None:
    """deploy-ready's verdict on one request: open/answered → answered; a held or done one keeps its state."""
    import prompt_ready as pr
    import prompt_requests as pq

    got = pq.update(lambda r: r["id"] == rid, {"verdict": verdict, "answered_at": _iso(view.now),
                                                "answered_by": pr.own_session()})
    if got and got[0].get("state") in ("open", "answered"):
        got = pq.update(lambda r: r["id"] == rid, {"state": "answered"})
    return got[0] if got else None


def close_requests(view: View, rels: list[str], deploy_id: str | None) -> None:
    """deploy-run: every request for a prompt it set deployed → done, with the deploy's id."""
    import prompt_requests as pq

    by_rel = {p.rel: p for p in view.prompts}
    refs = {f"{p.module}/{p.id or p.name}" for p in (by_rel.get(r) for r in rels) if p is not None}
    if not refs:
        return
    done = pq.update(lambda r: r.get("prompt") in refs and r.get("state") not in pq.CLOSED,
                     {"state": "done", "deploy": deploy_id, "done_at": _iso(time.time())})
    if done:
        print(f"deploy requests done: {', '.join(r['id'] + ' (' + str(r['prompt']) + ')' for r in done)}")


# --- main ------------------------------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--now", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd")
    view = sub.add_parser("view", help="(default) show the prompts")
    for parser in (ap, view):
        parser.add_argument("--module", help="one module's prompts")
        parser.add_argument("--all", action="store_true", help="every prompt of every module, incl. skipped")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--prompts", action="store_true", help="with --json: every prompt with its frontmatter")
        parser.add_argument("--board", action="store_true", help="regenerate the module README tables and the board")
        parser.add_argument("--html", action="store_true", help="write the HTML dashboard and print its path")
    s = sub.add_parser("set", help="set a prompt's status")
    s.add_argument("ref", help="ID, module/ID or file name")
    s.add_argument("status", choices=pc.STATUSES)
    s.add_argument("--commit", action="append", help="a sha (repeatable: every one is appended)")
    s.add_argument("--deployed")
    s.add_argument("--proof", help="what proves it live (writes `proof:`)")
    s.add_argument("--notes")
    verdict = s.add_mutually_exclusive_group()
    verdict.add_argument("--tests", metavar="FILE|TEXT", help="the run_tests.py results file (its SUMMARY must say OK), or a verdict")
    verdict.add_argument("--no-tests", dest="no_tests", metavar="WHY", help="a landing with nothing to test (docs, config)")
    s.add_argument("--tested", metavar="SHA", help="with --tests: the commit tested (default: --commit, else its last commit)")
    s.add_argument("--progress", metavar="N[/M] [text]", help="how far a running prompt is; M defaults to its ## Do items")
    s.add_argument("--needs-owner", dest="needs_owner")
    s.add_argument("--clear", action="append", metavar="KEY", help="remove a frontmatter key (repeatable)")
    s.add_argument("--no-commit", action="store_true")
    s.add_argument("--comment", action="store_true", help="post one progress comment (status + commit) to its issue")
    s.add_argument("--close", action="store_true", help="close its issue with that comment (done/skipped/superseded)")
    i = sub.add_parser("issues", help="prompts × GitHub issues: missing, unlinked, duplicates, state mismatches")
    i.add_argument("--module")
    i.add_argument("--fix", action="store_true", help="write the issue: keys it can match (never creates an issue)")
    i.add_argument("--no-commit", action="store_true")
    au = sub.add_parser("audit", help="data problems: no commit, todo left, folder vs status, ids, lower-case folders")
    au.add_argument("--module")
    mp = sub.add_parser("map-check", help="files a unit's entry imports that no unit-map rule for it matches; 0 none, 1 gaps")
    mp.add_argument("--unit", help="one deploy.entry unit")
    mp.add_argument("--map", help="another unit map file (repo-relative or absolute) instead of prompts.unit_map")
    mp.add_argument("--json", action="store_true")
    sub.add_parser("board-check", help="Status Board rows that say HELD/ON MAIN or DEPLOYED/LIVE against a prompt's status")
    bm = sub.add_parser("board-markers", help="add a generated Prompts: line to the first Status Board row per module")
    bm.add_argument("--no-commit", action="store_true")
    c = sub.add_parser("claim", help="attach a live session to a prompt (running + session_id; arms its Stop gate)")
    c.add_argument("ref", help="ID, module/ID or file name")
    c.add_argument("--session", required=True, help="the session id (or a unique prefix)")
    c.add_argument("--no-commit", action="store_true")
    d = sub.add_parser("deploy-check", help="the units each on-main/deployed prompt needs, and which restarted since it landed")
    d.add_argument("--module")
    d.add_argument("--json", action="store_true")
    d.add_argument("--apply", action="store_true", help="set deployed where every needed unit restarted after landing")
    d.add_argument("--no-commit", action="store_true")
    dr = sub.add_parser("deploy-ready", help="is a deploy safe now: guards + ready checks (reads only); 0 OK, 1 WAIT")
    dr.add_argument("--module")
    dr.add_argument("--json", action="store_true")
    dr.add_argument("--answer", metavar="REQUEST_ID", help="record the verdict on that deploy request")
    rn = sub.add_parser("deploy-run", help="the plan's restarts, one step at a time, each proven (owner's OK only)")
    rn.add_argument("--owner-ok", dest="owner_ok", required=True, metavar="QUOTE",
                    help="the owner's OK, quoted from this session (never a peer's message)")
    rn.add_argument("--only", metavar="STEP", help="one step of the plan (a unit or group name)")
    rn.add_argument("--dry-run", dest="dry_run", action="store_true", help="print every command in order; run nothing")
    rn.add_argument("--module")
    rn.add_argument("--no-commit", action="store_true")
    for name, text in (("inbox", "every open owner ask, oldest first, with its cost and the line to paste"),
                       ("deploy-plan", "guards, migrations, restart order and proofs for what is on main (prints only)"),
                       ("proofs", "deployed prompts and what proves them, free first")):
        pl = sub.add_parser(name, help=text)
        pl.add_argument("--module")
        pl.add_argument("--json", action="store_true")
    nx = sub.add_parser("next", help="pending prompts that can start now (deps done, no run-last, no overlap)")
    nx.add_argument("--parallel", action="store_true", help="every startable prompt, not just the first per module")
    nx.add_argument("--json", action="store_true")
    ho = sub.add_parser("hold", help="the owner holds a prompt: deploy-ready WAITs on it, deploy-run refuses it")
    ho.add_argument("ref", help="module/ID, ID or file name")
    ho.add_argument("--owner", required=True, metavar="QUOTE", help="the owner's words, quoted from this session")
    ho.add_argument("--no-commit", action="store_true")
    uh = sub.add_parser("unhold", help="remove the owner's hold (only on the owner's word)")
    uh.add_argument("ref", help="module/ID, ID or file name")
    uh.add_argument("--no-commit", action="store_true")
    rq = sub.add_parser("request", help="ask the /cc session for a deploy of a prompt (then SendMessage it)")
    rq.add_argument("ref", help="module/ID, ID or file name")
    rq.add_argument("--note", help="what you need (units, timing)")
    rs = sub.add_parser("requests", help="the deploy requests: open, answered, held (--all: done too)")
    rs.add_argument("--all", action="store_true")
    rs.add_argument("--json", action="store_true")
    pp = sub.add_parser("prompts", help="full paths of the prompts filed in Pending/ or Completed/; 1 if any is misfiled")
    pp.add_argument("folder", choices=("pending", "completed"))
    pp.add_argument("--module")
    pp.add_argument("--status", action="append", choices=pc.STATUSES, help="only this status (repeatable)")
    so = sub.add_parser("sort", help="file every prompt under its module's Completed/ or Pending/ folder")
    so.add_argument("--dry-run", action="store_true")
    so.add_argument("--no-commit", action="store_true")
    m = sub.add_parser("migrate", help="move legacy prompt folders into the root, one folder per module")
    m.add_argument("--module")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--force", action="store_true")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    now = datetime.fromisoformat(args.now).timestamp() if args.now else time.time()
    cfg = load_config()
    if not pc.prompts_cfg(cfg):
        print("No prompts config: add a 'prompts' block to the tracker config (see docs/command-center.md).", file=sys.stderr)
        return 2
    if args.cmd == "migrate":
        import prompt_migrate

        return prompt_migrate.main(cfg, args.module, args.dry_run, args.force)
    view = View(cfg, now)
    if args.cmd == "set":
        return cmd_set(view, args)
    if args.cmd == "sort":
        return cmd_sort(view, args)
    if args.cmd == "prompts":
        return cmd_prompts(view, args)
    if args.cmd == "claim":
        return cmd_claim(view, args)
    if args.cmd == "deploy-check":
        return cmd_deploy_check(view, args)
    if args.cmd == "inbox":
        return cmd_inbox(view, args)
    if args.cmd == "deploy-plan":
        return cmd_deploy_plan(view, args)
    if args.cmd == "deploy-ready":
        return cmd_deploy_ready(view, args)
    if args.cmd == "deploy-run":
        return cmd_deploy_run(view, args)
    if args.cmd == "proofs":
        return cmd_proofs(view, args)
    if args.cmd == "next":
        return cmd_next(view, args)
    if args.cmd == "issues":
        return cmd_issues(view, args)
    if args.cmd == "audit":
        return cmd_audit(view, args)
    if args.cmd == "map-check":
        return cmd_map_check(view, args)
    if args.cmd == "board-check":
        return cmd_board_check(view, args)
    if args.cmd == "board-markers":
        return cmd_board_markers(view, args)
    if args.cmd in ("hold", "unhold"):
        return cmd_hold(view, args)
    if args.cmd == "request":
        return cmd_request(view, args)
    if args.cmd == "requests":
        return cmd_requests(view, args)
    if args.board:
        changed, notes = regenerate(view)
        repo = Path(str(view.spec["repo"]))
        for n in notes:
            print(n)
        print("\n".join(str(c.relative_to(repo)) for c in changed) if changed else "nothing changed")
        return 0
    if args.html:
        out = Path(os.path.expanduser(str(view.spec.get("html") or (tracker_dir() / "command-center.html"))))
        out.parent.mkdir(parents=True, exist_ok=True)
        write_if_changed(out, render_html(view))
        print(out)
        return 0
    if args.json:
        print(render_json(view, args.prompts))
        return 0
    if args.module or args.all:
        mods = [m for m in view.modules if args.all or m["module"] == args.module]
        if not mods:
            print(f"no prompts in module {args.module!r}; modules: " + ", ".join(m["module"] for m in view.modules), file=sys.stderr)
            return 2
        print(render_module(view, mods, args.all), end="")
        return 0
    print(render_terminal(view))
    return 0


if __name__ == "__main__":
    sys.exit(main())
