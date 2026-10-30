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
                                          [--notes TEXT] [--needs-owner TEXT] [--no-commit]
    python3 scripts/command_center.py migrate [--module M] [--dry-run] [--force]

Config: the ``prompts`` block of the session tracker config (see docs/command-center.md).
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "utils"))
import prompt_center as pc  # noqa: E402
from session_tracker import load_config, tracker_dir  # noqa: E402

BEGIN, END = "<!-- status:begin -->", "<!-- status:end -->"
COLUMNS = [("order", 5), ("id", 6), ("status", 10), ("needs owner", 18), ("session", 16), ("commit", 10),
           ("updated", 10), ("title", 40)]
TABLE_HEADER = "| # | Prompt | Status | Owner | Commit | Updated | Notes |\n|---|---|---|---|---|---|---|\n"
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
    if c.get("pending"):
        parts.append(f"{c['pending']} pending")
    if c.get("blocked"):
        parts.append(f"{c['blocked']} blocked")
    if c.get("stalled"):
        parts.append(f"{c['stalled']} stalled")
    return f"{m['module']:<14}" + " · ".join(parts)


def _row(module: str, p: pc.Prompt, tail: str) -> str:
    return f"  {module:<14}{(p.id or ''):<7}{fit(p.title or p.name, 36):<36}  {tail}".rstrip()


def render_terminal(view: View) -> str:
    mods = view.modules
    running = sum(len(m["running"]) for m in mods)
    stalled = sum(len(m["stalled"]) for m in mods)
    blocked = sum(len(m["blocked"]) for m in mods)
    stamp = datetime.fromtimestamp(view.now).strftime("%Y-%m-%d %H:%M")
    lines = [f"Prompt command center · {stamp} · {running} running · {stalled} stalled · {blocked} blocked on owner", ""]
    lines += [module_line(m) for m in mods]
    if not mods:
        lines.append("(no prompts found; check the prompts block of the tracker config)")
    sections = [
        ("Running now", [(m["module"], p, f"{r['sid8']}  {r['state']}  {age(r['age_s'])}" + (f"  @{r['branch']}" if r.get("branch") else "")
                         if r else "?") for m in mods for p, r in m["running"]]),
        ("Stalled (frontmatter says running, session gone)",
         [(m["module"], p, _last_run(view.runs_of(p), p)) for m in mods for p in m["stalled"]]),
        ("Blocked on owner", [(m["module"], p, meta_str(p, "needs_owner") or meta_str(p, "notes")) for m in mods for p in m["blocked"]]),
        ("Next up", [(m["module"], m["next"], f"needs owner: {meta_str(m['next'], 'needs_owner') or 'no'}") for m in mods if m["next"]]),
    ]
    for title, rows in sections:
        if rows:
            lines += ["", title] + [_row(mod, p, tail) for mod, p, tail in rows]
    return "\n".join(lines)


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
            cells = [p.order or "", p.id or "", status, meta_str(p, "needs_owner"), run_cell(view.runs_of(p)),
                     meta_str(p, "commit"), meta_str(p, "updated")[:10], p.title or p.name]
            out.append("  ".join(fit(c, w).ljust(w) for c, (_, w) in zip(cells, COLUMNS)).rstrip())
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_json(view: View) -> str:
    mods = view.modules

    live = pc.live_session_ids(view.sessions, view.now)

    def brief(p: pc.Prompt) -> dict[str, Any]:
        sid = p.meta.get("session_id")
        return {"module": p.module, "id": p.id, "name": p.name, "rel": p.rel, "session_id": sid,
                "session_live": bool(sid) and str(sid) in live, "updated": p.meta.get("updated")}

    data = {
        "now": datetime.fromtimestamp(view.now).astimezone().isoformat(timespec="seconds"),
        "modules": [{"module": m["module"], "total": m["total"], "finished": m["finished"], "counts": m["counts"],
                     "next": m["next"].id if m["next"] else None} for m in mods],
        "running": [{**brief(p), "session": r["sid8"] if r else None, "state": r["state"] if r else None,
                     "age_s": r["age_s"] if r else None} for m in mods for p, r in m["running"]],
        "stalled": [brief(p) for m in mods for p in m["stalled"]],
        "blocked": [{**brief(p), "needs_owner": meta_str(p, "needs_owner")} for m in mods for p in m["blocked"]],
    }
    return json.dumps(data, indent=1)


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
        commit = meta_str(p, "commit")
        cells = [p.order or "", f"`{p.name}`", st, meta_str(p, "needs_owner"), f"`{commit}`" if commit else "",
                 meta_str(p, "updated")[:10], _cell(meta_str(p, "notes") + (" · todo: confirm" if p.meta.get("todo") else ""))]
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
    cards = []
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
                f"<td class='s s-{e(status)}'>{e(pc.MARKS.get(status, ''))} {e(status)}</td>"
                f"<td>{e(who)}</td><td>{e(meta_str(p, 'needs_owner'))}</td>"
                f"<td><code>{e(meta_str(p, 'commit'))}</code></td><td>{e(meta_str(p, 'notes'))}</td></tr>")
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
    try:
        pc.set_status(view.spec, p.rel, args.status, commit=args.commit, deployed=args.deployed, notes=args.notes,
                      needs_owner=args.needs_owner)
        moved = pc.place(view.spec, p.rel, args.status)[1] if view.spec.get("status_folders") else []
    except (ValueError, OSError) as e:
        print(e, file=sys.stderr)
        return 2
    fresh = View(view.cfg, view.now)
    changed, notes = regenerate(fresh, [p.module])
    for n in notes:
        print(n)
    repo = Path(str(view.spec["repo"]))
    rels = list(dict.fromkeys([p.rel, *moved] + [str(c.relative_to(repo)) for c in changed]))
    if moved:
        print(f"moved to {moved[-2]}")
    print(f"{p.module} {p.id or p.name}: {p.status} → {args.status}")
    if args.no_commit:
        print("not committed (--no-commit); files: " + ", ".join(rels))
        return 0
    sha = pc.commit_prompt_files(view.spec, rels, f"docs(prompts): {p.module} {p.id or p.name} → {args.status}")
    print(f"committed {sha[:9]}: " + ", ".join(rels) if sha else "NOT committed (git refused; commit the files yourself): " + ", ".join(rels))
    return 0


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
        parser.add_argument("--board", action="store_true", help="regenerate the module README tables and the board")
        parser.add_argument("--html", action="store_true", help="write the HTML dashboard and print its path")
    s = sub.add_parser("set", help="set a prompt's status")
    s.add_argument("ref", help="ID, module/ID or file name")
    s.add_argument("status", choices=pc.STATUSES)
    s.add_argument("--commit")
    s.add_argument("--deployed")
    s.add_argument("--notes")
    s.add_argument("--needs-owner", dest="needs_owner")
    s.add_argument("--no-commit", action="store_true")
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
        print(render_json(view))
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
