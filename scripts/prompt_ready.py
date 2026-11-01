"""deploy-ready: is a deploy safe now? Every check is a read; nothing here restarts, applies or writes anything.

The answer to a peer's "can I deploy?" (CC9: on 11-01 00:05 the /cc session checked by hand: freeze, builds, calls,
memory, locks, merge state, dirty modules/, deploy-plan, unit start times). The guards are the map's
``deploy.guards``; the rest take their parameters from ``deploy.ready_checks`` (defaults below):

- ``min_available_mb``: ``MemAvailable`` floor (10-31 01:14: two suites at once OOM-killed ``-celery``).
- ``locks``: lock files a running job holds (``flock -n <f> true``: exit 1 = held; a missing file is free).
- ``merge_state``: no merge or rebase half done in the live tree.
- ``dirty_units``: no uncommitted file a unit (or the merge) would load.
- ``migration_chain``: each migration to apply chains from prod's ``alembic_version`` or another shipped one, and
  the files have one head.
- ``landing_window_min``: no other live session mid-commit into the repo, or landed code there in the window.

Always: no prompt that would ship is held by the owner (``hold:`` in its frontmatter, ``command_center.py hold``).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import prompt_center as pc
import prompt_deploy as pd
import prompt_plan as pp

MEMINFO = "/proc/meminfo"
FREEZE_FILE = "/run/lock/neurogen-deploy-freeze"
DEFAULTS = {"min_available_mb": 1500, "locks": [], "merge_state": True, "dirty_units": True, "migration_chain": True,
            "landing_window_min": 10, "main_line": True, "verify": True}
STATUS_COMMIT_PREFIX = "docs(prompts):"  # command_center.py's own commits: they change no unit


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def options(umap: dict[str, Any]) -> dict[str, Any]:
    return {**DEFAULTS, **((umap.get("deploy") or {}).get("ready_checks") or {})}


def own_session() -> str | None:
    return os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def verdict(checks: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """(every check OK, the reasons to wait: ``<name>: <detail>``)."""
    waits = [f"{c['name']}: {c['detail']}" if c["detail"] else c["name"] for c in checks if not c["ok"]]
    return not waits, waits


def health_ok(out: str) -> bool:
    """``/api/health`` ×3 reads ``200 200 200``: three codes, every one 200."""
    codes = (out or "").split()
    return len(codes) == 3 and all(c == "200" for c in codes)


# --- the checks ----------------------------------------------------------------------------------------------------


def guards(umap: dict[str, Any], plan: dict[str, Any], run: Any = subprocess.run) -> list[dict[str, Any]]:
    """Every ``deploy.guards`` entry; a ``before:`` guard only when one of its units ships."""
    groups = umap.get("groups") or {}
    shipping = set(plan.get("units") or [])
    for st in plan.get("steps") or []:
        shipping |= {st["unit"], *st.get("units", [])}
    out = []
    for g in (umap.get("deploy") or {}).get("guards") or []:
        before = g.get("before") or []
        if before and not any(b in shipping or any(m in shipping for m in groups.get(b, [])) for b in before):
            continue
        try:
            p = run(str(g["command"]), shell=True, capture_output=True, text=True, timeout=30)
            got = (p.stdout or "").strip()
        except (OSError, subprocess.SubprocessError) as e:
            out.append(_check(g["name"], False, f"could not run ({type(e).__name__})"))
            continue
        want = str(g.get("expect", ""))
        out.append(_check(g["name"], got == want, "" if got == want else f"reads {got[:80]!r}, expected {want!r}"))
    return out


def memory(min_mb: int, meminfo: str | None = None) -> dict[str, Any]:
    try:
        text = Path(meminfo or MEMINFO).read_text()
        kb = int(re.search(r"^MemAvailable:\s+(\d+)", text, re.MULTILINE).group(1))  # type: ignore[union-attr]
    except (OSError, AttributeError, ValueError):
        return _check("memory", False, "MemAvailable unread")
    mb = kb // 1024
    return _check("memory", mb >= int(min_mb), f"{mb} MB available" + ("" if mb >= int(min_mb) else f", needs {min_mb}"))


def locks(files: list[str], run: Any = subprocess.run) -> dict[str, Any]:
    held = []
    for f in files:
        if not os.path.exists(f):  # nobody holds a lock file that is not there (and flock would create it)
            continue
        try:
            rc = run(["flock", "-n", f, "true"], capture_output=True, text=True, timeout=10).returncode
        except (OSError, subprocess.SubprocessError):
            rc = 1
        if rc != 0:
            held.append(f)
    return _check("locks", not held, f"held: {', '.join(held)}" if held else "")


def merge_state(repo: str, run: Any = subprocess.run) -> dict[str, Any]:
    found = []
    for name in ("MERGE_HEAD", "rebase-merge", "rebase-apply"):
        try:
            p = run(["git", "-C", repo, "rev-parse", "--git-path", name], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return _check("merge state", False, "git unread")
        path = (p.stdout or "").strip()
        if path and os.path.exists(path if os.path.isabs(path) else os.path.join(repo, path)):
            found.append(name)
    return _check("merge state", not found, f"{', '.join(found)} in {repo}: a merge or rebase is half done" if found else "")


def dirty_units(repo: str, umap: dict[str, Any], run: Any = subprocess.run) -> dict[str, Any]:
    try:
        p = run(["git", "-C", repo, "status", "--porcelain", "-z"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return _check("uncommitted changes", False, "git status unread")
    if p.returncode != 0:
        return _check("uncommitted changes", False, "git status unread")
    tokens, files = (p.stdout or "").split("\0"), []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if len(entry) < 4:
            continue
        files.append(entry[3:])
        if entry[0] in "RC":  # a rename's second token is its old path
            i += 1
    hits = [f for f in files if any(pd.needs([f], umap))]
    if not hits:
        return _check("uncommitted changes", True)
    more = f" (+{len(hits) - 5} more)" if len(hits) > 5 else ""
    return _check("uncommitted changes", False, f"{len(hits)} uncommitted unit files: {', '.join(hits[:5])}{more}")


def migration_chain(plan: dict[str, Any], prod: list[str] | None, repo: str, umap: dict[str, Any]) -> dict[str, Any]:
    mig_dir = str((umap.get("deploy") or {}).get("migrations") or "").rstrip("/")
    files = pp.migration_files(repo, mig_dir) if mig_dir else {}
    todo = [m for m in plan.get("migrations") or [] if not m.get("applied_at_boot") and not m.get("in_prod")]
    problems = []
    if todo and not prod:
        return _check("migration chain", False, "alembic_version unread: cannot tell what prod has")
    graph = {rev: downs for rev, downs in (pp.revision_of(t) for t in files.values()) if rev}
    applied = pp.applied_revisions(files, prod) if prod else set()
    shipped = {pp.revision_of(files.get(m["file"], ""))[0] for m in plan.get("migrations") or []}
    for m in todo:
        rev, downs = pp.revision_of(files.get(m["file"], ""))
        if not rev:
            problems.append(f"{m['file']}: no revision")
            continue
        loose = [d for d in downs if d not in applied and d not in shipped]
        if loose or not downs and applied:
            problems.append(f"{rev} chains from {', '.join(loose) or 'nothing'}, not prod ({', '.join(prod or [])})")
    heads = sorted(set(graph) - {d for downs in graph.values() for d in downs})
    if graph and len(heads) != 1:
        problems.append(f"{len(heads)} heads ({', '.join(heads)})")
    return _check("migration chain", not problems, "; ".join(problems))


def _needs_no_unit(repo: str, sha: str, umap: dict[str, Any] | None) -> bool:
    """A commit whose files the map sends to no unit (tests, docs); unreadable or unknown = it may need one."""
    if not umap:
        return False
    try:
        p = subprocess.run(["git", "-C", repo, "show", "-z", "--name-only", "--format=", sha], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    files = [f for f in p.stdout.split("\0") if f.strip()] if p.returncode == 0 else []
    return bool(files) and pd.needs(files, umap) == (set(), False)


def landing(sessions: list[dict[str, Any]], repo: str, now: float, window_min: float,
            own: str | None = None, also: list[str] | None = None,
            umap: dict[str, Any] | None = None) -> dict[str, Any]:
    """Another live session committing into the repo now (a HEAD snapshot not yet closed), or landed code there in
    the window (the tracker stores no commands: only the pending snapshot and the commit rows).

    Not counted (CC11, live 11-01 07:42: A03's request waited on its own one-file test commit): the sessions in
    ``also`` (the request's own session) and, with the map, a commit that needs no unit."""
    target = os.path.normpath(repo)
    window = float(window_min) * 60
    found = []
    live = pc.live_session_ids(sessions, now)
    skip = [x[:8] for x in [own, *(also or [])] if x]
    for s in sessions:
        sid = str(s["session_id"])
        if sid not in live or sid[:8] in skip:  # its own deploy-ready call snapshots HEAD too
            continue
        who = str(s.get("name") or sid[:8])
        for snap in (s.get("pending_heads") or {}).values():
            if (any(os.path.normpath(d) == target for d in (snap.get("heads") or {}))
                    and 0 <= now - float(snap.get("ts") or 0) <= window):
                found.append(f"{who} is landing")
                break
        for c in s.get("commits") or []:
            when = pc._parse_iso(c.get("ts"))
            if (when is None or os.path.normpath(str(c.get("dir") or "")) != target
                    or str(c.get("subject") or "").startswith(STATUS_COMMIT_PREFIX)):
                continue
            if 0 <= now - when <= window and not _needs_no_unit(repo, str(c.get("sha")), umap):
                found.append(f"{who} landed {str(c.get('sha'))[:9]} {int((now - when) // 60)} min ago")
    return _check("no session landing", not found, "; ".join(dict.fromkeys(found)))


def main_line(repo: str, root: str, now: float, branch: str = "main", days: float = 3) -> dict[str, Any]:
    """WAIT while main's newest landing put a branch on its first-parent line (CC11: A03's fast-forward, 11-01)."""
    import prompt_mainline as ml

    found = ml.findings(repo, branch, now - float(days) * 86400)
    bad = [f for f in found[:1] if ml.newest_landing(repo, f["sha"], root, branch)]
    return _check("main line", not bad, ml.line_text(bad[0]) if bad else "")


def verify(cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """WAIT while no /verify record covers main's code: the verify gate would block the restart (CC12; Assistants 2
    asked, 11-01). None while the gate is off (no ``board_gate.repo``)."""
    import session_tracker as st

    gate = (st.load_config() if cfg is None else cfg).get("board_gate")
    repo = str(gate.get("repo") or "") if isinstance(gate, dict) else ""
    if not repo:
        return None
    why = st._unverified_code(repo, str(gate.get("branch") or "main"), list(gate.get("code_paths") or []), st.run_git)
    return _check("verify", not why, why or "")


def hold_quote(meta: dict[str, Any]) -> str | None:
    """The owner's words a prompt is held with (``hold: {owner, by, at}``, or a hand-written string), else None."""
    h = meta.get("hold")
    if not h:
        return None
    return str(h.get("owner") or "") if isinstance(h, dict) else str(h)


def holds(view: Any, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """One WAIT per held prompt the plan would ship (on main, its units waiting): CC10, the owner held A04b's
    restart and only the conversation knew."""
    by_rel = {p.rel: p for p in view.prompts}
    out = []
    for e in plan.get("prompts") or []:
        p = by_rel.get(e["rel"])
        quote = hold_quote(p.meta) if p is not None and e.get("waits") else None
        if quote is not None:
            label = f"{e['module']}/{e['id'] or e['name']}"
            out.append(_check(f'{label} held by the owner ("{quote}")', False))
    return out


def ready(view: Any, umap: dict[str, Any], plan: dict[str, Any], prod: list[str] | None,
          run: Any = subprocess.run, meminfo: str | None = None, also: list[str] | None = None) -> list[dict[str, Any]]:
    """[{name, ok, detail}]: the guards, then the ready checks the map turns on. ``also``: sessions whose landings
    don't count (the request being answered)."""
    opt = options(umap)
    repo = str(view.spec["repo"])
    out = guards(umap, plan, run) + holds(view, plan)
    if opt.get("min_available_mb"):
        out.append(memory(int(opt["min_available_mb"]), meminfo))
    out.append(locks([str(f) for f in opt.get("locks") or []], run))
    if opt.get("merge_state"):
        out.append(merge_state(repo, run))
    if opt.get("dirty_units"):
        out.append(dirty_units(repo, umap, run))
    if opt.get("migration_chain"):
        out.append(migration_chain(plan, prod, repo, umap))
    if opt.get("main_line"):
        out.append(main_line(repo, str(view.spec.get("root") or ""), view.now,
                             str(view.spec.get("main_branch") or "main"), float(view.spec.get("main_line_days") or 3)))
    if opt.get("verify"):
        out += [c for c in [verify()] if c]
    if opt.get("landing_window_min"):
        out.append(landing(view.sessions, repo, view.now, float(opt["landing_window_min"]), own_session(), also, umap))
    return out


def cheap(umap: dict[str, Any], repo: str, run: Any = subprocess.run, meminfo: str | None = None) -> list[dict[str, Any]]:
    """The /cc line's checks: the freeze file, locks, merge state, memory (no psql, no systemctl, no git status)."""
    opt = options(umap)
    freeze = str((umap.get("deploy") or {}).get("freeze_file") or FREEZE_FILE)
    out = []
    if os.path.exists(freeze):
        try:
            why = Path(freeze).read_text(errors="replace").strip().splitlines()[0][:60]
        except (OSError, IndexError):
            why = ""
        out.append(_check("deploy freeze", False, why or freeze))
    else:
        out.append(_check("deploy freeze", True))
    out.append(locks([str(f) for f in opt.get("locks") or []], run))
    if opt.get("merge_state"):
        out.append(merge_state(repo, run))
    if opt.get("min_available_mb"):
        out.append(memory(int(opt["min_available_mb"]), meminfo))
    return out
