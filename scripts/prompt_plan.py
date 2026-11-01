"""deploy-plan, proofs and next: what to do next, read from the prompts, the tracker, git and the unit map.

Read-only: it reads files, git, (through ``prompt_deploy``) ``systemctl show`` and the config's read-only
``prod_revision`` query (``alembic_version``); it never restarts a unit,
applies a migration, writes a file or starts a session. The project's own deploy steps (guards, restart commands,
proofs, the migrations folder) come from the ``deploy`` block of the unit map, never from this code.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import prompt_center as pc
import prompt_deploy as pd
import prompt_issues as pi

BUSY = ("running", "on-branch")
_FREE = re.compile(r"\b(?:no|0|zero)\s+(?:AI\s+)?credits?\b|\bno\b[^;,.()]*\bbuilds?\b", re.IGNORECASE)
_PAID = re.compile(r"\$\s?\d|\bcredits?\b|\bbuilds?\b|\bpaid\b", re.IGNORECASE)
_DOLLARS = re.compile(r"\$\s?(\d+(?:\.\d+)?)")
RANKS = ("free", "unknown", "paid")
_ID = r"[A-Z]{0,3}\d+[a-z]?(?:-\d+)?"
# "Run last." / "Run after A01/A02" in a prompt's body; a quoted mention ("run last") is not a hint
_RUN_LAST = re.compile(r"(?<![\"“'\w])Run last\b")
_RUN_AFTER = re.compile(rf"(?<![\"“'\w])Run after\s+({_ID}(?:\s*(?:/|,|and|&)\s*{_ID})*)")
_TICKED = re.compile(r"`([^`\s]+)`")


def _git(repo: str | Path, *args: str) -> str | None:
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def body_of(p: pc.Prompt) -> str:
    try:
        return pc.parse_frontmatter(p.path.read_text(errors="replace"))[1]
    except OSError:
        return ""


def _as_list(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value] if value not in (None, "") else []
    return [str(v).strip() for v in values if str(v).strip()]


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "yes", "1", "y")


# --- proofs ---------------------------------------------------------------------------------------------


def cost_rank(text: Any) -> tuple[int, float]:
    """(0 free | 1 unknown | 2 paid, the largest $ amount named). "no AI credits", "0 credits", "NO build" are free."""
    t = str(text or "").strip()
    if not t:
        return 1, 0.0
    rest = _FREE.sub("", t)
    if not _PAID.search(rest):
        return 0, 0.0
    return 2, max((float(x) for x in _DOLLARS.findall(rest)), default=0.0)


def done_when_live(body: str) -> str:
    """The first top-level bullet of the ``Done when`` section that says "live", joined onto one line."""
    m = re.search(r"^#+\s*Done when\b.*$", body, re.MULTILINE | re.IGNORECASE)
    if not m:
        return ""
    section = body[m.end():]
    nxt = re.search(r"^#+\s", section, re.MULTILINE)
    bullets: list[list[str]] = []
    cur: list[str] | None = None
    for line in (section[:nxt.start()] if nxt else section).splitlines():
        if re.match(r"[-*]\s+", line):
            cur = [line[1:].strip()]
            bullets.append(cur)
        elif cur is not None and line[:1] in (" ", "\t") and line.strip():
            cur.append(line.strip())
        elif not line.strip():
            cur = None
    return next((" ".join(b) for b in bullets if re.search(r"\blive\b", " ".join(b), re.IGNORECASE)), "")


def proof_row(p: pc.Prompt) -> dict[str, Any]:
    """What proves the prompt: its ``proof:`` line, else the Done-when live bullet; ranked by ``cost`` + ``proof``."""
    proof = str(p.meta.get("proof") or "").strip()
    cost = str(p.meta.get("cost") or "").strip()
    rank, dollars = cost_rank(" ; ".join(x for x in (cost, proof) if x))
    return {"module": p.module, "id": p.id, "name": p.name, "rel": p.rel, "status": p.status,
            "proof": proof or done_when_live(body_of(p)), "source": "proof" if proof else "done-when",
            "cost": cost, "rank": RANKS[rank], "dollars": dollars, "deployed": p.meta.get("deployed")}


def proofs(prompts: list[pc.Prompt]) -> list[dict[str, Any]]:
    """Deployed prompts (live, not proven yet), free first, then the cheapest, then the longest deployed."""
    rows = [proof_row(p) for p in prompts if p.status == "deployed"]
    return sorted(rows, key=lambda r: (RANKS.index(r["rank"]), r["dollars"], pc._parse_iso(r["deployed"]) or 0, r["rel"]))


# --- deploy-plan -----------------------------------------------------------------------------------------


_REVISION = re.compile(r"^revision\s*(?::[^=\n]*)?=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_DOWN = re.compile(r"^down_revision\s*(?::[^=\n]*)?=\s*(.+)$", re.MULTILINE)
_QUOTED = re.compile(r"['\"]([^'\"]+)['\"]")


def revision_of(text: str) -> tuple[str | None, list[str]]:
    """(revision, down_revisions) of an Alembic file, parsed (never imported); a merge has several downs."""
    rev, down = _REVISION.search(text or ""), _DOWN.search(text or "")
    return (rev.group(1) if rev else None), (_QUOTED.findall(down.group(1)) if down else [])


def applied_revisions(files: dict[str, str], heads: str | list[str]) -> set[str]:
    """Every revision at or below the heads (``alembic_version``), walking ``down_revision`` through ``files``
    ({name: text}). A head no file defines: nothing (the walk can't vouch for anything)."""
    graph = {}
    for text in files.values():
        rev, downs = revision_of(text)
        if rev:
            graph[rev] = downs
    todo = [h for h in ([heads] if isinstance(heads, str) else heads) if h in graph]
    seen: set[str] = set()
    while todo:
        rev = todo.pop()
        if rev not in seen:
            seen.add(rev)
            todo += [d for d in graph.get(rev, []) if d in graph]
    return seen


def prod_revision(command: str | None, run: Any = subprocess.run) -> list[str] | None:
    """The heads ``alembic_version`` holds now, from the config's read-only query; None when unset or it fails."""
    if not command:
        return None
    try:
        p = run(command, shell=True, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    heads = [line.strip() for line in (p.stdout or "").splitlines() if line.strip()]
    return heads if p.returncode == 0 and heads else None


def migration_files(repo: str | Path, mig_dir: str) -> dict[str, str]:
    folder = Path(repo) / mig_dir
    out = {}
    for f in sorted(folder.glob("*.py")) if folder.is_dir() else []:
        try:
            out[str(f.relative_to(repo))] = f.read_text(errors="replace")
        except OSError:
            pass
    return out


def deploy_plan(prompts: list[pc.Prompt], umap: dict[str, Any], history: pd.History,
                prod: list[str] | None = None, repo: str | Path | None = None) -> dict[str, Any]:
    """Prompts to ship (on-main, or deployed with a commit not live yet), their units, migrations, guards, steps.

    ``prod``: the heads ``alembic_version`` holds; a migration at or below them is marked ``in_prod`` (live 10-31:
    the R4b/R5 migrations were listed to apply by hand while prod was already at r5)."""
    results = pd.check(prompts, umap, history)
    picked = sorted((e for e in results if e["status"] == "on-main" or e["state"] in ("half", "waiting")),
                    key=lambda e: e["rel"])
    dep = umap.get("deploy") or {}
    groups = umap.get("groups") or {}
    units = {u for e in picked for u in e["waits"]}
    mig_dir = str(dep.get("migrations") or "").rstrip("/")
    # a migration ran at boot once every unit ITS commit needs started after that commit landed (live 10-31: R4's
    # migration ran at the 10:53 boot while a later fix of R4 still waited on web): listed apart, not to apply again
    def booted(e: dict[str, Any], c: dict[str, Any]) -> bool:
        return e["state"] == "live" or bool(c["needs"]) and all((e["starts"].get(u) or 0) >= c["landed"] for u in c["needs"])

    files = migration_files(repo, mig_dir) if prod and repo and mig_dir else {}
    applied = applied_revisions(files, prod) if files else set()
    migrations = [{"module": e["module"], "id": e["id"], "sha": c["sha"], "file": f, "applied_at_boot": booted(e, c),
                   "in_prod": revision_of(files.get(f, ""))[0] in applied if applied else False}
                  for e in picked for c in e["commits"] for f in c.get("files") or []
                  if mig_dir and f.startswith(mig_dir + "/")]
    restart, ready = dep.get("restart") or {}, dep.get("ready") or {}
    order = list(dep.get("order") or pd.compact(list(umap.get("order") or []), umap))
    order += [u for u in sorted(units) if u not in {m for n in order for m in groups.get(n, [n])}]
    steps = []
    for name in order:
        members = groups.get(name, [name])
        if not any(u in units for u in members):
            continue
        own = restart.get(name)
        default = str(restart.get("*") or "systemctl restart {unit}")
        commands = [str(own).replace("{unit}", name)] if own else [default.replace("{unit}", u) for u in members]
        proof = ([str(c).replace("{unit}", u) for u in members for c in dep.get("proof") or []]
                 + [str(ready[u]) for u in members if u in ready] + [str(c) for c in dep.get("after_each") or []])
        steps.append({"unit": name, "units": members, "commands": commands, "proof": proof,
                      "guards": [g for g in dep.get("guards") or [] if name in (g.get("before") or [])
                                 or any(u in (g.get("before") or []) for u in members)]})
    by_rel = {p.rel: p for p in prompts}
    untested = [{"module": e["module"], "id": e["id"], "rel": e["rel"], "why": why}
                for e in picked if e["rel"] in by_rel for why in [untested_why(by_rel[e["rel"]], e, history)] if why]
    return {"prompts": picked, "units": sorted(units), "migrations": migrations, "migrate": dep.get("migrate"),
            "untested": untested,
            "guards": list(dep.get("guards") or []), "steps": steps,
            "proofs": [proof_row(by_rel[e["rel"]]) for e in picked if e["rel"] in by_rel],
            "note": None if dep else "no `deploy` block in the unit map: the map's own unit order, plain "
                                     "`systemctl restart`, no guards or proofs (docs/command-center.md)"}


def untested_why(p: pc.Prompt, e: dict[str, Any], history: pd.History) -> str | None:
    """Why the prompt's ``tests:`` does not prove what ships, or None. ``none: <why>`` is a docs/config landing."""
    tests = str(p.meta.get("tests") or "").strip()
    if not tests:
        return "no tests: line (`set … --tests <results file>`)"
    if tests.startswith("none:"):
        return None
    shas = pc._SHA.findall(tests.rsplit(" · ", 1)[-1])
    if not shas:
        return f"tests: names no sha ({tests[:60]})"
    got, why = history.landing(shas[-1])
    if got is None:
        return f"the tested {shas[-1]} is not what ships ({why})"
    later = [c["sha"] for c in e["commits"] if c["landed"] > got["landed"]]
    return f"{', '.join(later)} landed after the tested {shas[-1]}" if later else None


def read_verdict(path: Path) -> tuple[str | None, str | None]:
    """(the ``SUMMARY`` line of a ``scripts/run_tests.py`` results file, None) or (None, why it is no pass).

    A ``.txt`` is the printed run (its last SUMMARY line); a ``.json`` is ``--out`` (its tally)."""
    import json

    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        return None, f"{path}: unreadable ({type(e).__name__})"
    if path.suffix == ".json":
        try:
            data = json.loads(text)
            tally = {str(k): int(v) for k, v in data["tally"].items()}
            files = len(data.get("results") or [])
        except (ValueError, KeyError, TypeError, AttributeError):
            return None, f"{path}: not a run_tests.py --out file"
        bad = tally.get("NEW-FAILURES", 0) + tally.get("BROKEN", 0)
        summary = ("SUMMARY " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))
                   + f" | files={files} | {'FAIL' if bad else 'OK'}")
    else:
        lines = [ln.strip() for ln in text.splitlines() if ln.startswith("SUMMARY ")]
        if not lines:
            return None, f"{path}: no SUMMARY line (not a run_tests.py results file?)"
        summary = lines[-1]
    if not summary.endswith("| OK"):
        return None, f"{path}: {summary} (a failing run proves nothing)"
    return summary, None


# --- audit: commits nobody recorded --------------------------------------------------------------------


def unrecorded_commits(prompts: list[pc.Prompt], repo: str, branch: str = "main",
                       umap: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Commits on ``branch`` whose subject names a prompt (``#<issue>`` or ``<module> <ID>``), made since that
    prompt's first commit, that no prompt's ``commits:`` lists, nor a recorded merge brought in (live 10-31:
    R4's fix ``766d43595`` "(R4, #576)" was never recorded). Status commits (``docs(prompts):``) are skipped, and
    with a unit map so is a commit that needs no unit (docs, tests): deploy-check would not miss it."""
    out = _git(repo, "log", branch, "--format=%H%x00%P%x00%ct%x00%s")
    if not out:
        return []
    rows = [line.split("\x00", 3) for line in out.splitlines() if line.count("\x00") == 3]
    info = {sha: (parents.split(), int(ct), subject) for sha, parents, ct, subject in rows}
    by7: dict[str, list[str]] = {}
    for sha in info:
        by7.setdefault(sha[:7], []).append(sha)

    def full(short: str) -> str | None:
        hits = [s for s in by7.get(short[:7], []) if s.startswith(short)]
        return hits[0] if len(hits) == 1 else None

    brought: dict[str, set[str]] = {}

    def merged_in(sha: str) -> set[str]:
        if sha not in brought:
            listed = _git(repo, "rev-list", f"{sha}^1..{sha}") if len(info[sha][0]) > 1 else ""
            brought[sha] = set((listed or "").split()) - {sha}
        return brought[sha]

    recorded = {f for p in prompts for s in pc.commits_of(p.meta) for f in [full(s)] if f}
    covered = set(recorded)
    for sha in recorded:
        covered |= merged_in(sha)
    found = []
    for p in prompts:
        firsts = [f for s in pc.commits_of(p.meta) for f in [full(s)] if f]
        if not p.id or not firsts:
            continue
        since = min(info[f][1] for f in firsts)
        names = [rf"#{n}\b" for n in pi.issues_of(p.meta)] + [rf"\b{re.escape(p.module)}\s+{re.escape(p.id)}\b"]
        pattern = re.compile("|".join(names), re.IGNORECASE)
        for sha, (_, ct, subject) in info.items():
            if ct < since or sha in covered or subject.startswith("docs(prompts):") or not pattern.search(subject):
                continue
            if merged_in(sha) & recorded:
                continue  # a merge that brought a recorded commit in
            if umap is not None:
                units, at_merge = pd.needs((_git(repo, "diff", "--name-only", f"{sha}^1", sha) or "").split(), umap)
                if not units and not at_merge:
                    continue
            found.append({"prompt": p, "sha": sha, "subject": subject, "ts": ct})
    return sorted(found, key=lambda r: (r["prompt"].rel, r["ts"]))


# --- next --------------------------------------------------------------------------------------------------


def order_hints(meta: dict[str, Any], body: str) -> tuple[bool, list[str]]:
    """(run last?, the ids it runs after). Explicit ``run_last`` / ``after`` keys win; the body is the fallback."""
    run_last = _truthy(meta["run_last"]) if "run_last" in meta else bool(_RUN_LAST.search(body))
    if "after" in meta:
        after = _as_list(meta["after"])
    else:
        after = [i for m in _RUN_AFTER.finditer(body) for i in re.findall(_ID, m.group(1))]
    return run_last, list(dict.fromkeys(after))


def _same_repo(wt: str, repo: str) -> bool:
    """A worktree of the prompts repo, and not the repo's own (shared, live) tree."""
    try:
        if Path(wt).resolve() == Path(repo).resolve():
            return False
    except OSError:
        return False
    common = [(_git(x, "rev-parse", "--path-format=absolute", "--git-common-dir") or "").strip() for x in (wt, repo)]
    return bool(common[0]) and common[0] == common[1]


def touched(p: pc.Prompt, runs: list[dict[str, Any]], repo: str, branch: str) -> set[str]:
    """Paths a running/on-branch prompt changes: its commits' files, and its live session's worktree changes."""
    files: set[str] = set()
    for sha in pc.commits_of(p.meta):
        files.update(f for f in (_git(repo, "show", "-z", "--name-only", "--format=", sha) or "").split("\0") if f.strip())
    for r in runs:
        wt = str(r.get("worktree") or "")
        if not r.get("live") or not wt or not _same_repo(wt, repo):
            continue
        base = (_git(wt, "merge-base", "HEAD", branch) or "").strip()
        if base:
            files.update(f for f in (_git(wt, "diff", "-z", "--name-only", base) or "").split("\0") if f.strip())
        files.update(f for f in (_git(wt, "ls-files", "-z", "--others", "--exclude-standard") or "").split("\0") if f.strip())
    return files


def mentioned_paths(body: str, repo: str, root: str, tracked: list[str]) -> set[str]:
    """Backticked repo paths in a prompt's body that exist (a bare file name: its one tracked match)."""
    by_name: dict[str, list[str]] = {}
    for f in tracked:
        by_name.setdefault(f.rsplit("/", 1)[-1], []).append(f)
    out = set()
    for tok in _TICKED.findall(body):
        tok = re.sub(r"[:#]\d+$", "", tok.strip().rstrip(".,;:)"))
        if not tok or tok.startswith(("/", "~", "-")) or any(c in tok for c in "*?<>{}$=") or tok.startswith(root + "/"):
            continue
        if "/" not in tok:
            hits = by_name.get(tok, [])
            if len(hits) == 1:
                out.add(hits[0])
            continue
        if (Path(repo) / tok).is_file():  # a folder (`tests`) says where to look, not what changes
            out.add(tok)
    return out


def _overlap(a: set[str], b: set[str]) -> list[str]:
    return sorted({x for x in a for y in b if x == y or y.startswith(x + "/") or x.startswith(y + "/")})


def next_prompts(view: Any, umap: dict[str, Any] | None, parallel: bool) -> dict[str, list[dict[str, Any]]]:
    """Pending prompts that can start now, and why each other pending one waits.

    Held: a ``depends_on``/``after`` prompt not finished; run last while another prompt of its module is running or
    on a branch; a path (or, with a unit map, a unit) shared with a running/on-branch prompt, or a path shared with a
    prompt this list already starts. The map's ``parallel`` block names what sharing is no conflict: ``shared_units``
    (units nearly every change restarts) and ``ignore`` (glob paths everyone edits or only reads, like docs).
    A prompt's paths are the backticked files its body names. Without ``parallel``: the first startable per module.
    """
    spec = view.spec
    repo, root = str(spec["repo"]), str(spec["root"]).strip("/")
    par = (umap or {}).get("parallel") or {}
    groups = (umap or {}).get("groups") or {}
    shared_units = {u for name in par.get("shared_units") or [] for u in groups.get(name, [name])}
    ignore = [pd.glob_regex(g) for g in par.get("ignore") or []]

    def keep(files: set[str]) -> set[str]:
        return {f for f in files if not any(r.fullmatch(f) for r in ignore)}

    def units_of(files: set[str]) -> set[str]:
        return (pd.needs(sorted(files), umap)[0] - shared_units) if umap else set()

    branch = str(spec.get("main_branch") or "main")
    status = {p.rel: view.status_of(p) for p in view.prompts}
    finished = {(p.module, (p.id or "").lower()) for p in view.prompts if status[p.rel] in pc.FINISHED}
    busy = [(p, keep(touched(p, view.runs_of(p), repo, branch))) for p in view.prompts if status[p.rel] in BUSY]
    busy = [(p, files, units_of(files)) for p, files in busy]
    tracked = (_git(repo, "ls-files", "-z") or "").split("\0")
    cands = sorted((p for p in view.prompts if status[p.rel] == "pending" and not p.legacy),
                   key=lambda p: (p.module, pc._order_key(p.order), p.name))
    start: list[dict[str, Any]] = []
    held: list[dict[str, Any]] = []
    starting: list[tuple[pc.Prompt, set[str]]] = []
    started_modules: set[str] = set()
    for p in cands:
        body = body_of(p)
        why = []
        deps = _as_list(p.meta.get("depends_on"))
        run_last, after = order_hints(p.meta, body)
        waits = [d for d in dict.fromkeys(deps + after) if (p.module, d.lower()) not in finished]
        if waits:
            why.append("waits on " + ", ".join(waits) + " (not done)")
        mates = [q.id or q.name for q, _, _ in busy if q.module == p.module]
        if run_last and mates:
            why.append(f"run last, and {', '.join(mates)} of {p.module} is running or on a branch")
        paths = keep(mentioned_paths(body, repo, root, tracked))
        units = units_of(paths)
        for q, files, q_units in busy:
            shared = _overlap(paths, files)
            if shared:
                why.append(f"overlaps {q.module} {q.id or q.name} ({status[q.rel]}): {', '.join(shared[:4])}")
            elif units & q_units:
                why.append(f"shares units with {q.module} {q.id or q.name} ({status[q.rel]}): "
                           + ", ".join(pd.compact(sorted(units & q_units), umap or {})))
        for q, q_paths in starting:
            shared = _overlap(paths, q_paths)
            if shared:
                why.append(f"overlaps {q.module} {q.id or q.name} (listed to start): {', '.join(shared[:4])}")
        if not why and not parallel and p.module in started_modules:
            continue
        row = {"module": p.module, "id": p.id, "name": p.name, "rel": p.rel}
        if why:
            held.append({**row, "why": "; ".join(why)})
            continue
        start.append({**row, "paste": f"run {p.rel}", "paths": sorted(paths)})
        starting.append((p, paths))
        started_modules.add(p.module)
    return {"start": start, "held": held}
