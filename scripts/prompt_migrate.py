"""One-off, idempotent: move legacy prompt folders into ``<prompts.root>/<module>/`` with status frontmatter.

Run through ``command_center.py migrate [--module M] [--dry-run] [--force]``. Steps, all repeatable:
1. ``git mv`` every prompt and module README out of each ``legacy_globs`` folder (file names unchanged; the
   batch ``00-README.md`` goes to ``_batches/<date>-00-README.md``).
2. Record old → new paths in ``<root>/_moved.json``.
3. Add frontmatter to each moved prompt that has none, read from its module README's table row
   (``infer_status``), else ``pending`` + ``todo: confirm``.
4. Put the status markers (and the generated table) into each module README above its old ``## Order``
   table, which is kept and marked frozen; point its handoff rule at ``command_center.py set``.
5. Rewrite the ``Paste:`` line of each prompt and the README's ``Run `…` `` example to the new path.
6. Leave a one-line redirect ``README.md`` in each old folder; create the root board if missing.
It never commits: it prints the pathspec commit command.
"""

from __future__ import annotations

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
from session_tracker import tracker_dir  # noqa: E402

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HEX = re.compile(r"`([0-9a-f]{7,12})`")
_ROW_FILE = re.compile(r"`([^`]+\.md)`")
_PASTE = re.compile(r"^(Paste:[^\n]*?)((?:[\w.~-]+/)+[^`\n]*?\.md)", re.MULTILINE)
_RULE = re.compile(r"mark (?:the|this README's) row (?:done|✅)[^.)]*", re.IGNORECASE)
_ORDER_HEADING = re.compile(r"^## Order\b.*$", re.MULTILINE)

BOARD_TEMPLATE = """# Session prompts: command center board

One folder per module; each prompt carries its status in frontmatter. Run `/command-center` (or
`python3 scripts/command_center.py` in claude-code-hooks-mastery) for the live view, `--board` to refresh the tables
below and in each module README, and `command_center.py set <module>/<ID> <status> --commit <sha>` in a handoff.

<!-- status:begin -->
<!-- status:end -->
"""


class Move:
    def __init__(self, old: Path, new: Path, module: str | None, kind: str) -> None:
        self.old, self.new, self.module, self.kind = old, new, module, kind  # kind: prompt | readme | batch


# --- status from a README row ------------------------------------------------------------------------------


def infer_status(row: str) -> tuple[str, str | None]:
    """(status, commit) from the free text of a legacy README row."""
    text = row or ""
    low = text.lower()
    after_main = text[low.find("main"):] if "main" in low else ""
    sha = (_HEX.search(after_main) or _HEX.search(text))
    commit = sha.group(1) if sha else None
    if "refuted" in low:
        return "skipped", commit
    if "✅" in text and re.search(r"\bDONE\b", text):
        return "done", commit
    if "✅" in text and re.search(r"\bdeployed\b|\blive\b", low) and not re.search(r"\bnot deployed\b", low):
        return "deployed", commit
    if "🔵" in text or re.search(r"\bON MAIN\b|\bHELD\b", text) or re.search(r"\bnot deployed\b", low) \
            or ("✅" in text and re.search(r"\bon main\b", low)):
        return "on-main", commit
    if "✅" in text:
        return "done", commit
    return "pending", None


def parse_rows(readme_text: str) -> list[dict[str, Any]]:
    """Each table row with a backticked ``.md`` cell: {name, order, needs_owner, text}."""
    rows, owner_col = [], None
    for line in readme_text.splitlines():
        if not line.startswith("|"):
            owner_col = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r"-*:?-*", c) for c in cells):
            continue
        if owner_col is None and not any(_ROW_FILE.search(c) for c in cells):
            owner_col = next((i for i, c in enumerate(cells) if "owner" in c.lower()), -1)
            continue
        m = next((_ROW_FILE.search(c) for c in cells if _ROW_FILE.search(c)), None)
        if not m:
            continue
        needs_owner = cells[owner_col] if owner_col not in (None, -1) and owner_col < len(cells) else ""
        rows.append({"name": os.path.basename(m.group(1)), "order": re.sub(r"[^\w.-]", "", cells[0]),
                     "needs_owner": needs_owner, "text": " | ".join(cells[1:])})
    return rows


# --- plan ---------------------------------------------------------------------------------------------------


def plan_moves(spec: dict[str, Any], module: str | None) -> list[Move]:
    repo, root = Path(str(spec["repo"])), Path(str(spec["repo"])) / str(spec["root"])
    aliases = dict(spec.get("module_aliases") or {})
    moves: list[Move] = []
    for pattern in spec.get("legacy_globs") or []:
        for folder in sorted(repo.glob(str(pattern))):
            if not folder.is_dir():
                continue
            date = (_DATE.search(folder.name) or re.search(r"$", folder.name)).group(0) or folder.name
            for path in sorted(folder.iterdir()):
                if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".md" or path.name == "README.md":
                    continue
                if re.fullmatch(r"00-README\.md", path.name, re.IGNORECASE):
                    mv = Move(path, root / "_batches" / f"{date}-{path.name}", None, "batch")
                elif pc.INDEX_NAME.match(path.name):
                    token = re.sub(r"-00-README\.md$", "", path.name, flags=re.IGNORECASE)
                    mod, _ = pc.infer_module_and_id(token + "-x.md", None, aliases)
                    mv = Move(path, root / mod / path.name, mod, "readme")
                else:
                    mod, _ = pc.infer_module_and_id(path.name, None, aliases)
                    mv = Move(path, root / mod / path.name, mod, "prompt")
                if module and mv.module != module:
                    continue
                if mv.new.exists() and not mv.new.samefile(path):  # name collision: keep both, date-prefixed
                    mv.new = mv.new.with_name(f"{date}-{mv.new.name}")
                moves.append(mv)
    return moves


def preflight(spec: dict[str, Any], moves: list[Move], cfg: dict[str, Any]) -> list[str]:
    repo = Path(str(spec["repo"]))
    problems = []
    folders = sorted({str(m.old.parent.relative_to(repo)) for m in moves})
    if folders:
        p = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", *folders], capture_output=True, text=True)
        if p.stdout.strip():
            problems.append("uncommitted changes under the legacy folders:\n  " + "\n  ".join(p.stdout.strip().splitlines()))
    d = tracker_dir()
    sessions = pc.load_sessions(d) if d.is_dir() else []
    now = time.time()
    index = pc.scan(cfg)
    moving = {str(m.old.relative_to(repo)) for m in moves}
    live = []
    for s in sessions:
        if str(s["session_id"]) not in pc.live_session_ids([s], now):
            continue
        rel = s.get("prompt_file") or pc.resolve_prompt(str(s.get("task") or ""), cfg, index)
        if rel in moving:
            live.append(f"{str(s['session_id'])[:8]} ({rel})")
    if live:
        problems.append("live sessions are running prompts that would move: " + ", ".join(live))
    lock = spec.get("sync_lock")
    if lock and Path(str(lock)).exists():
        problems.append(f"the auto-sync lock {lock} exists (a sync is running)")
    every = int(spec.get("sync_every_hours") or 0)
    if every:
        t = datetime.now()
        near = t.hour % every == 0 and t.minute <= 5 or (t.hour + 1) % every == 0 and t.minute >= 55
        if near:
            problems.append(f"within 5 minutes of the auto-sync run (every {every} h on the hour); wait")
    return problems


# --- apply ----------------------------------------------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()[:300]}")
    return p.stdout


def apply_moves(spec: dict[str, Any], moves: list[Move]) -> list[str]:
    repo, root = Path(str(spec["repo"])), Path(str(spec["repo"])) / str(spec["root"])
    actions = []
    for mv in moves:
        mv.new.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", str(mv.old.relative_to(repo)), str(mv.new.relative_to(repo)))
        actions.append(f"git mv {mv.old.relative_to(repo)} {mv.new.relative_to(repo)}")
    moved_path = root / "_moved.json"
    try:
        moved = json.loads(moved_path.read_text()) if moved_path.exists() else {}
    except ValueError:
        moved = {}
    for mv in moves:
        moved[str(mv.old.relative_to(repo))] = str(mv.new.relative_to(repo))
    if moves:
        root.mkdir(parents=True, exist_ok=True)
        moved_path.write_text(json.dumps(dict(sorted(moved.items())), indent=1) + "\n")
        actions.append(f"updated {moved_path.relative_to(repo)}")
    return actions


def annotate_module(spec: dict[str, Any], module: str, aliases: dict[str, str]) -> list[str]:
    """Frontmatter for every bare prompt of a module, from its README rows; superseded twins; Paste lines."""
    repo, folder = Path(str(spec["repo"])), Path(str(spec["repo"])) / str(spec["root"]) / module
    if not folder.is_dir():
        return []
    actions = []
    readme = next((p for p in sorted(folder.iterdir()) if pc.INDEX_NAME.match(p.name) and p.is_file()), None)
    rows = parse_rows(readme.read_text()) if readme else []
    prompts = [p for p in sorted(folder.iterdir()) if p.is_file() and not p.is_symlink() and p.suffix == ".md"
               and not pc.INDEX_NAME.match(p.name)]
    by_id: dict[str, list[Path]] = {}
    for p in prompts:
        _, ident = pc.infer_module_and_id(p.name, module, aliases)
        if ident:
            by_id.setdefault(ident, []).append(p)
    for p in prompts:
        meta, _, had = pc.parse_frontmatter(pc._read_head(p))
        if had:
            continue
        _, ident = pc.infer_module_and_id(p.name, module, aliases)
        row = match_row(rows, p.name, ident, module, aliases)
        fm: dict[str, Any] = {"module": module, "id": ident, "order": (row or {}).get("order") or ident}
        twins = by_id.get(ident or "", [])
        if ident and len(twins) > 1 and p.name.lower().startswith(ident.lower() + "-"):
            fm.update({"status": "superseded", "notes": "by " + ", ".join(t.name for t in twins if t != p), "todo": "confirm"})
        elif row:
            status, commit = infer_status(row["text"])
            fm["status"] = status
            if commit:
                fm["commit"] = commit
            if row["needs_owner"]:
                fm["needs_owner"] = row["needs_owner"]
        else:
            fm.update({"status": "pending", "todo": "confirm"})
        fm = {k: v for k, v in fm.items() if v not in (None, "")}
        fm["updated"] = pc._now_iso()
        pc.write_meta(p, fm)
        actions.append(f"frontmatter {p.relative_to(repo)}: {fm['status']}")
    for p in prompts:
        text = p.read_text()
        new = _PASTE.sub(lambda m: m.group(1) + str(p.relative_to(repo)), text, count=1) if _PASTE.search(text) else text
        if new != text:
            p.write_text(new)
            actions.append(f"paste line {p.relative_to(repo)}")
    return actions


def match_row(rows: list[dict[str, Any]], name: str, ident: str | None, module: str, aliases: dict[str, str]) -> dict[str, Any] | None:
    for row in rows:
        if row["name"] == name:
            return row
    if ident:
        for row in rows:
            _, rid = pc.infer_module_and_id(row["name"], module, aliases)
            if rid == ident:
                return row
    best, best_len = None, 0
    for row in rows:
        n = len(os.path.commonprefix([row["name"].lower(), name.lower()]))
        if n > best_len and n >= 12:
            best, best_len = row, n
    return best


def update_readme(spec: dict[str, Any], module: str, aliases: dict[str, str], today: str) -> list[str]:
    repo, folder = Path(str(spec["repo"])), Path(str(spec["repo"])) / str(spec["root"]) / module
    readme = next((p for p in sorted(folder.iterdir()) if pc.INDEX_NAME.match(p.name) and p.is_file()), None) if folder.is_dir() else None
    if not readme:
        return []
    text = original = readme.read_text()
    if "<!-- status:begin -->" not in text:
        block = "<!-- status:begin -->\n<!-- status:end -->\n\n"
        m = _ORDER_HEADING.search(text)
        if m:
            heading = m.group(0)
            if "frozen" not in heading:
                heading = f"## Order (frozen {today}; live status is the table above)"
            text = text[: m.start()] + block + heading + text[m.end():]
        else:
            text = text.rstrip("\n") + "\n\n" + block
    text = _RULE.sub("run `command_center.py set " + module + "/<ID> <status> --commit <sha>` (it updates the status table)", text)
    prompts = {p.name: p for p in folder.iterdir() if p.is_file() and p.suffix == ".md" and not pc.INDEX_NAME.match(p.name)}

    def fix_example(m: re.Match) -> str:
        name = os.path.basename(m.group(2))
        target = prompts.get(name)
        if not target:
            _, ident = pc.infer_module_and_id(name, module, aliases)
            target = next((p for p in prompts.values() if ident and pc.infer_module_and_id(p.name, module, aliases)[1] == ident), None)
        return m.group(1) + (str(target.relative_to(repo)) if target else m.group(2)) + m.group(3)

    for glob in spec.get("legacy_globs") or []:  # only links into the legacy folders are rewritten
        folder = re.escape(str(glob)).replace(r"\*", r"[^/`]*")
        text = re.compile(r"(Run `)(" + folder + r"/[^`]*?\.md)(`)").sub(fix_example, text)
    if text != original:
        readme.write_text(text)
        return [f"readme {readme.relative_to(repo)}"]
    return []


def write_stubs(spec: dict[str, Any], moves: list[Move]) -> list[str]:
    repo, root = Path(str(spec["repo"])), str(spec["root"])
    actions = []
    for folder in sorted({m.old.parent for m in moves}):
        stub = folder / "README.md"
        if stub.exists():
            continue
        stub.write_text(f"Moved: these prompts now live in `{root}/<module>/` under the SAME file name "
                        f"(old → new paths: `{root}/_moved.json`). Run `command_center.py set <module>/<ID> <status>` for a handoff.\n")
        actions.append(f"stub {stub.relative_to(repo)}")
    return actions


def main(cfg: dict[str, Any], module: str | None, dry_run: bool, force: bool) -> int:
    spec = pc.prompts_cfg(cfg)
    if not spec:
        print("no prompts config", file=sys.stderr)
        return 2
    repo = Path(str(spec["repo"]))
    aliases = dict(spec.get("module_aliases") or {})
    moves = plan_moves(spec, module)
    if dry_run:
        for mv in moves:
            print(f"git mv {mv.old.relative_to(repo)} {mv.new.relative_to(repo)}")
        print(f"{len(moves)} files would move (dry run)")
        return 0
    problems = preflight(spec, moves, cfg) if moves else []
    if problems and not force:
        print("refusing (use --force to override):\n- " + "\n- ".join(problems), file=sys.stderr)
        return 1
    actions = apply_moves(spec, moves)
    today = datetime.now().date().isoformat()
    root = repo / str(spec["root"])
    present = {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")} if root.is_dir() else set()
    modules = sorted({mv.module for mv in moves if mv.module} | present)
    if module:
        modules = [m for m in modules if m == module]
    for mod in modules:
        actions += annotate_module(spec, mod, aliases)
        actions += update_readme(spec, mod, aliases, today)
    actions += write_stubs(spec, moves)
    board = spec.get("board")
    if board and not (repo / str(board)).exists():
        (repo / str(board)).parent.mkdir(parents=True, exist_ok=True)
        (repo / str(board)).write_text(BOARD_TEMPLATE)
        actions.append(f"board {board}")
    import command_center as cc

    changed, _ = cc.regenerate(cc.View(cfg, time.time()), modules or None)
    actions += [f"table {c.relative_to(repo)}" for c in changed]
    for a in actions:
        print(a)
    if not actions:
        print("nothing to migrate")
        return 0
    folders = sorted({str(m.old.parent.relative_to(repo)) for m in moves})
    print("\nNot committed. Review, then:\n  git -C " + str(repo) + " commit -m 'docs(prompts): move session prompts to "
          + f"{spec['root']}/<module>/ with status frontmatter' -- {spec['root']} " + " ".join(folders))
    return 0
