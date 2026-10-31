"""Prompt command center: the prompts of a project, their status, and the sessions running them.

A prompt is a Markdown file under ``<repo>/<prompts.root>/<module>/``. Its status lives in a small flat
frontmatter block (``key: value`` lines, YAML-compatible); the body is never touched. The session tracker
claims a prompt when a session's first message says to run it, and ``scripts/command_center.py`` renders
the whole picture. Nothing about a particular project lives here: paths come from the tracker config
(``prompts`` block, see ``session_tracker.config.example.json``).

Standard library only (the hooks run with no third-party packages). Functions the hooks call never raise.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

KNOWN_KEYS = (
    "module", "id", "order", "title", "status", "needs_owner", "cost", "depends_on",
    "session_id", "commit", "deployed", "updated", "notes", "todo", "issue",
)
MAX_FRONTMATTER_LINES = 60
_NEEDS_QUOTE = re.compile(r"""^[\[{"'!&*>|%@#`]|: | #|^$|^\s|\s$""")
# Scalars a YAML reader would turn into a number, bool, null or date: a commit `0123456`, `123e4567`, `no`.
_YAML_TYPED = re.compile(
    r"^(?:[-+]?[\d._]*\d[\d._]*(?:[eE][-+]?\d+)?|0x[0-9a-fA-F_]+|[-+]?\.(?:inf|nan)|\d{4}-\d{2}-\d{2}.*"
    r"|true|false|yes|no|on|off|y|n|null|~)$",
    re.IGNORECASE,
)


# --- frontmatter ----------------------------------------------------------------------------------


def _parse_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_parse_value(x) for x in inner.split(",")] if inner else []
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        if raw[0] == '"':
            try:
                return json.loads(raw)
            except ValueError:
                pass
        return raw[1:-1]
    return raw


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str, bool]:
    """(meta, body, had_frontmatter). Needs ``---`` on line 1 and a closing ``---`` within 60 lines."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text, False
    lines = text.splitlines(keepends=True)
    for i in range(1, min(len(lines), MAX_FRONTMATTER_LINES + 1)):
        if lines[i].rstrip("\r\n") == "---":
            meta: dict[str, Any] = {}
            for line in lines[1:i]:
                line = line.rstrip("\r\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                key, sep, value = line.partition(":")
                if not sep or not re.fullmatch(r"[A-Za-z_][\w-]*", key.strip()):
                    return {}, text, False
                meta[key.strip()] = _parse_value(value)
            return meta, "".join(lines[i + 1:]), True
    return {}, text, False


def _dump_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_dump_value(v) for v in value) + "]"
    if value is None:
        return ""
    s = str(value)
    return json.dumps(s) if _NEEDS_QUOTE.search(s) or _YAML_TYPED.match(s) else s


def dump_frontmatter(meta: dict[str, Any]) -> str:
    keys = [k for k in KNOWN_KEYS if k in meta] + [k for k in meta if k not in KNOWN_KEYS]
    lines = ["---"] + [f"{k}: {_dump_value(meta[k])}".rstrip() for k in keys] + ["---"]
    return "\n".join(lines) + "\n"


def _mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns


def write_meta(path: Path, updates: dict[str, Any], expect: dict[str, tuple] | None = None) -> dict[str, Any] | None:
    """Merge ``updates`` into the file's frontmatter; the body is kept byte for byte.

    ``expect`` maps a key to the values it must currently have (``None`` = absent). An update whose value is
    None removes that key. Returns the new meta, or None when nothing was written (expectation failed, or
    the file changed underneath).
    """
    path = Path(path)
    raw = path.read_bytes()
    before = _mtime_ns(path)
    head, body = _split_bytes(raw)
    meta, _, _ = parse_frontmatter(head.decode("utf-8", "replace") + ("\n" if head else ""))
    for key, allowed in (expect or {}).items():
        if meta.get(key) not in allowed:
            return None
    for key, value in updates.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    out = dump_frontmatter(meta).encode("utf-8") + body
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(out)
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            pass
        if _mtime_ns(path) != before:
            return None
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return meta


def _split_bytes(raw: bytes) -> tuple[bytes, bytes]:
    """(frontmatter bytes without the closing newline, body bytes). No frontmatter → (b'', raw)."""
    text = raw.decode("utf-8", "replace")
    _, body, had = parse_frontmatter(text)
    if not had:
        return b"", raw
    body_bytes = body.encode("utf-8")
    return raw[: len(raw) - len(body_bytes)], body_bytes


# --- the prompts of a project -----------------------------------------------------------------------

STATUSES = ("pending", "running", "on-branch", "on-main", "deployed", "done", "blocked", "skipped", "superseded")
MARKS = {"pending": "⚪", "running": "▶", "stalled": "▶?", "on-branch": "◐", "on-main": "🔵", "deployed": "🟡",
         "done": "🟢", "blocked": "🔴", "skipped": "—", "superseded": "—"}
FINISHED = ("done", "deployed")
NOT_COUNTED = ("skipped", "superseded")
CLOSED = ("done", "skipped", "superseded")  # a prompt nobody runs again: filed under COMPLETED_FOLDER
COMPLETED_FOLDER, PENDING_FOLDER = "Completed", "Pending"
INDEX_NAME = re.compile(r"^(?:[A-Za-z-]+-)?00-README\.md$|^README\.md$", re.IGNORECASE)
_ID = re.compile(r"^[A-Z]{0,2}\d+[a-z]?$")
_TITLE_PREFIX = re.compile(r"^(?:Session prompt\b[^:]*:\s*)?(?:(?:[A-Za-z]+\s+)?[A-Z]{0,2}\d+[a-z]?(?:-\d+)?(?:\s*\([^)]*\))?:\s*)?")
HEAD_BYTES = 4096


class Prompt:
    __slots__ = ("path", "rel", "module", "id", "order", "title", "meta", "status", "has_frontmatter", "legacy")

    def __init__(self, **kw: Any) -> None:
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def name(self) -> str:
        return self.path.name

    def __repr__(self) -> str:
        return f"Prompt({self.module}/{self.id or '?'} {self.name} {self.status})"


def prompts_cfg(cfg: dict[str, Any]) -> dict[str, Any] | None:
    spec = cfg.get("prompts") if isinstance(cfg, dict) else None
    if not isinstance(spec, dict) or not spec.get("repo") or not spec.get("root"):
        return None
    return spec


def _alias(token: str, aliases: dict[str, str]) -> str | None:
    for key in (token, token.lower()):
        if key in aliases:
            return str(aliases[key])
    for key, value in aliases.items():
        if key.lower() == token.lower():
            return str(value)
    return None


def infer_module_and_id(name: str, folder_module: str | None, aliases: dict[str, str]) -> tuple[str, str | None]:
    """From ``Funnels-B12-3-Title.md``: ('funnels', 'B12-3'); ``01-Sock-…``: (alias(Sock), '01'); ``F3-…``: (alias(F3), 'F3')."""
    stem = re.sub(r"\.md$", "", name, flags=re.IGNORECASE)
    tokens = [t for t in re.split(r"[-\s]+", stem) if t]
    module: str | None = folder_module
    pos = 0
    if tokens and not module:
        two = "-".join(tokens[:2])
        if _alias(two, aliases):
            module, pos = _alias(two, aliases), 2
        elif _alias(tokens[0], aliases):
            module, pos = _alias(tokens[0], aliases), 1 if not _ID.match(tokens[0]) else 0
        elif _ID.match(tokens[0]) and len(tokens) > 1:  # `01-Sock-…`: the id first, then the module
            two = "-".join(tokens[1:3])
            module = _alias(two, aliases) or _alias(tokens[1], aliases) or tokens[1].lower()
            return module, tokens[0]
        else:
            module, pos = tokens[0].lower(), 1
    elif tokens and module and _alias(tokens[0], aliases) == module:
        pos = 0 if _ID.match(tokens[0]) else 1
    elif tokens and module and tokens[0].lower() == module.lower():
        pos = 1
    ident = None
    if pos < len(tokens) and _ID.match(tokens[pos]):
        ident = tokens[pos]
        if pos + 1 < len(tokens) and re.fullmatch(r"\d+", tokens[pos + 1]):
            ident += "-" + tokens[pos + 1]
    return (module or "misc"), ident


def _read_head(path: Path) -> str:
    with open(path, "rb") as f:
        return f.read(HEAD_BYTES).decode("utf-8", "replace")


def _title(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return _TITLE_PREFIX.sub("", line[2:].strip(), count=1)
    return ""


def load_prompt(path: Path, rel: str, module: str | None, legacy: bool, aliases: dict[str, str]) -> Prompt:
    meta, body, had = parse_frontmatter(_read_head(path))
    mod, ident = infer_module_and_id(path.name, module, aliases)
    mod = str(meta.get("module") or mod)
    ident = str(meta.get("id") or ident or "") or None
    status = str(meta.get("status") or "pending")
    if status not in STATUSES:
        status = "pending"
    if path.parent.name.lower() == COMPLETED_FOLDER.lower() and status not in CLOSED:
        status = "done"  # filed under Completed/: done, whatever an older frontmatter says
    return Prompt(path=path, rel=rel, module=mod, id=ident, order=str(meta.get("order") or ident or ""), legacy=legacy,
                  title=str(meta.get("title") or _title(body)), meta=meta, status=status, has_frontmatter=had)


_DATE_SUFFIX = re.compile(r"(?:-sent)?\s*-\s*\d{1,2}-\d{2}(?:-\d{2})?$", re.IGNORECASE)


def _twin_key(name: str) -> str:
    """One key for the same prompt saved twice: case folded, a trailing `-10-28` / `-Sent-10-28-26` dropped."""
    stem = re.sub(r"\.md$", "", name, flags=re.IGNORECASE).strip().lower()
    return _DATE_SUFFIX.sub("", stem).strip(" -")


def scan(cfg: dict[str, Any]) -> list[Prompt]:
    """Every prompt under the root's module folders (one level of sub-folders too) plus the legacy globs.

    The same prompt saved twice (a legacy copy, a `Funnels/` twin of `funnels/`, a `-10-28` dated copy) counts once:
    the copy with frontmatter wins, then a root copy over a legacy one, then the lower-case folder, then the shallower.
    """
    spec = prompts_cfg(cfg)
    if not spec:
        return []
    repo = Path(str(spec["repo"]))
    aliases = dict(spec.get("module_aliases") or {})
    seen: dict[str, tuple[tuple, Prompt]] = {}

    def add(path: Path, module: str | None, legacy: bool, depth: int = 0, top: str | None = None) -> None:
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".md":
            return
        if INDEX_NAME.match(path.name) or path.name.startswith("_"):
            return
        try:
            prompt = load_prompt(path, str(path.relative_to(repo)), module, legacy, aliases)
        except OSError:
            return
        folder = top or path.parent.name
        rank = (prompt.has_frontmatter, not legacy, folder == folder.lower(), -depth)
        key = _twin_key(path.name)
        if key not in seen or rank > seen[key][0]:
            seen[key] = (rank, prompt)

    def module_of(folder: Path) -> str:
        return _alias(folder.name, aliases) or folder.name.lower()

    root = repo / str(spec["root"])
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            if folder.is_dir() and not folder.name.startswith("_") and not folder.is_symlink():
                for path in sorted(folder.iterdir()):
                    if path.is_dir() and not path.name.startswith("_") and not path.is_symlink():
                        for sub in sorted(path.iterdir()):
                            add(sub, module_of(folder), False, depth=1, top=folder.name)
                    else:
                        add(path, module_of(folder), False, top=folder.name)
    for pattern in spec.get("legacy_globs") or []:
        for folder in sorted(repo.glob(str(pattern))):
            if folder.is_dir():
                for path in sorted(folder.iterdir()):
                    add(path, None, True)
    return sorted((v[1] for v in seen.values()), key=lambda p: (p.module, _order_key(p.order), p.name))


def _order_key(order: str | None) -> tuple:
    parts = re.findall(r"\d+|[A-Za-z]+", order or "")
    return tuple((0, int(x)) if x.isdigit() else (1, x.lower()) for x in parts) or ((2, ""),)


# --- which prompt does a message name? ---------------------------------------------------------------

_PATH_TOKEN = re.compile(r"(?:~|\.{0,2}/|\b)[\w.~/-]*/[\w.-]+\.md\b")


def module_folder(spec: dict[str, Any], module: str) -> Path | None:
    """The existing folder under the root for a module key (``Agents/`` for ``agents``, an alias folder), or None.

    Several twins: the capitalized one wins (the owner names folders; tools follow).
    """
    spec = _spec(spec)
    root = Path(str(spec["repo"])) / str(spec["root"])
    aliases = dict(spec.get("module_aliases") or {})
    if not root.is_dir():
        return None
    hits = [f for f in sorted(root.iterdir()) if f.is_dir() and not f.is_symlink() and not f.name.startswith("_")
            and (_alias(f.name, aliases) or f.name.lower()) == module]
    return min(hits, key=lambda f: f.name == f.name.lower()) if hits else None


def status_folder(status: str) -> str:
    return COMPLETED_FOLDER if status in CLOSED else PENDING_FOLDER


def record_move(spec: dict[str, Any], old: str, new: str) -> str:
    """Add ``old → new`` to ``_moved.json`` and repoint every entry that led to ``old`` (lookups stay one hop).

    Returns the map's repo-relative path.
    """
    spec = _spec(spec)
    rel = str(Path(str(spec["root"])) / "_moved.json")
    data = moved_map(spec)
    data = {k: (new if v == old else v) for k, v in data.items()}
    data[old] = new
    data = {k: v for k, v in data.items() if k != v and k != new}
    path = Path(str(spec["repo"])) / rel
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(dict(sorted(data.items())), indent=1, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return rel


def place(spec: dict[str, Any], rel: str, status: str) -> tuple[str, list[str]]:
    """File a prompt under ``<module folder>/Completed/`` or ``/Pending/`` by status (``git mv`` when tracked).

    Returns (its path now, the repo-relative paths a commit must name). Module READMEs, files outside the
    root's module folders and files already in place are left alone: (rel, []). Raises OSError when the
    target exists.
    """
    import subprocess

    spec = _spec(spec)
    repo = Path(str(spec["repo"]))
    root = repo / str(spec["root"])
    path = repo / rel
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return rel, []
    if len(parts) < 2 or parts[0].startswith("_") or INDEX_NAME.match(path.name) or not path.is_file():
        return rel, []
    target = root / parts[0] / status_folder(status) / path.name
    if target == path:
        return rel, []
    if target.exists():
        raise OSError(f"{target.relative_to(repo)} already exists; not moving {rel}")
    target.parent.mkdir(parents=True, exist_ok=True)
    new = str(target.relative_to(repo))
    tracked = False
    try:
        tracked = subprocess.run(["git", "-C", str(repo), "mv", "--", rel, new], capture_output=True,
                                 timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    if not tracked:
        os.replace(path, target)
    moved_rel = record_move(spec, rel, new)
    return new, ([rel] if tracked else []) + [new, moved_rel]


def moved_map(spec: dict[str, Any]) -> dict[str, str]:
    try:
        with open(Path(str(spec["repo"])) / str(spec["root"]) / "_moved.json") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_prompt(text: str, cfg: dict[str, Any], index: list[Prompt]) -> str | None:
    """The repo-relative path of the prompt a message names: a path token, a moved old path, or a bare file name."""
    spec = prompts_cfg(cfg)
    if not spec or not text:
        return None
    repo = os.path.normpath(str(spec["repo"]))
    by_rel = {p.rel: p for p in index}
    by_name = {p.name: p for p in index}
    moved = None
    for m in _PATH_TOKEN.finditer(text):
        token = m.group(0)
        if os.path.isabs(token):
            token = os.path.normpath(token)
            if not token.startswith(repo + os.sep):
                continue
            token = os.path.relpath(token, repo)
        token = token.lstrip("./")
        if token in by_rel:
            return token
        if moved is None:
            moved = moved_map(spec)
        if token in moved and moved[token] in by_rel:
            return moved[token]
        base = os.path.basename(token)
        if base in by_name:
            return by_name[base].rel
    best = max((n for n in by_name if n in text), key=len, default=None)
    return by_name[best].rel if best else None


CLAIM_WINDOW = 6  # a claim verb this early says "run it": `analyze and execute X`, `Run plan and execute X`
_QUESTION_WORDS = {"why", "what", "when", "where", "who", "how", "did", "does", "do", "is", "was", "should"}
_NEGATIONS = {"not", "don't", "dont", "never", "without"}
_BARE_PATH = re.compile(r"[`'\"]*[^\s`'\"]+\.md[`'\"]*", re.IGNORECASE)


def claims_prompt(text: str, verbs: list[str]) -> bool:
    """True when the message says to run its prompt, not just mention it.

    A claim verb (``run``, ``execute``, …) among the first six words, or a message that is only a prompt path
    (backticks allowed). A question (``why is X stalled``) or a negation before the verb never claims.
    """
    head = (text or "").strip()[:400]
    if not head or head.startswith("<"):
        return False
    if _BARE_PATH.fullmatch(head):
        return True
    words = [w.strip("`'\"*_:.,;!?()").lower() for w in head.split()[:CLAIM_WINDOW]]
    if not words or words[0] in _QUESTION_WORDS:
        return False
    wanted = {str(v).lower() for v in verbs}
    for i, w in enumerate(words):
        if w in wanted:
            return not _NEGATIONS.intersection(words[:i])
    return False


# --- sessions × prompts ------------------------------------------------------------------------------

STALE_AFTER_S = 2 * 3600


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(value: Any) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value)).timestamp() if value else None
    except ValueError:
        return None


def load_sessions(directory: Path) -> list[dict[str, Any]]:
    """Every tracker file in the directory (the tracker's ``<session_id>.json`` files)."""
    sessions = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("session_id"):
            sessions.append(data)
    return sessions


def session_display_state(s: dict[str, Any], now: float, stale_after: float = STALE_AFTER_S) -> str:
    state = str(s.get("state") or "?")
    seen = _parse_iso(s.get("last_seen")) or 0
    if state != "ended" and now - seen > stale_after:
        return "stale?"
    return "WAITING ON YOU" if state == "waiting_on_owner" else state


def live_session_ids(sessions: list[dict[str, Any]], now: float, stale_after: float = STALE_AFTER_S) -> set[str]:
    return {str(s["session_id"]) for s in sessions
            if s.get("state") != "ended" and now - (_parse_iso(s.get("last_seen")) or 0) <= stale_after}


def join(prompts: list[Prompt], sessions: list[dict[str, Any]], cfg: dict[str, Any], now: float,
         stale_after: float = STALE_AFTER_S) -> dict[str, list[dict[str, Any]]]:
    """{prompt rel: [run, …]} newest first. A session matches by ``prompt_file``, else by its ``task`` text."""
    runs: dict[str, list[dict[str, Any]]] = {}
    by_rel = {p.rel for p in prompts}
    by_sid = {str(p.meta.get("session_id")): p.rel for p in prompts if p.status == "running" and p.meta.get("session_id")}

    def run_of(s: dict[str, Any], sid: str) -> dict[str, Any]:
        seen = _parse_iso(s.get("last_seen")) or now
        return {"sid": sid, "sid8": sid[:8], "state": session_display_state(s, now, stale_after),
                "age_s": max(0, int(now - seen)), "live": sid in live_session_ids([s], now, stale_after),
                "branch": s.get("branch"), "worktree": s.get("worktree"), "last_commit": s.get("last_commit")}

    for s in sorted(sessions, key=lambda s: _parse_iso(s.get("last_seen")) or 0, reverse=True):
        sid = str(s["session_id"])
        rel = s.get("prompt_file")
        if not rel or rel not in by_rel:
            rel = resolve_prompt(str(s.get("task") or ""), cfg, prompts)
        if rel:
            runs.setdefault(rel, []).append(run_of(s, sid))
        own = by_sid.get(sid)  # the prompt's frontmatter names this session (claimed later or by hand)
        if own and own != rel:
            runs.setdefault(own, []).append(run_of(s, sid))
    return runs


def effective_status(p: Prompt, runs: list[dict[str, Any]]) -> str:
    live = any(r.get("live") for r in runs)
    if p.status == "running":
        return "running" if live else "stalled"
    if p.status in ("pending", "blocked") and live:
        return "running"
    return p.status


def progress(prompts: list[Prompt], runs: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """One summary per module, sorted by module name."""
    mods: dict[str, dict[str, Any]] = {}
    for p in prompts:
        m = mods.setdefault(p.module, {"module": p.module, "total": 0, "finished": 0, "counts": {},
                                       "running": [], "stalled": [], "blocked": [], "next": None, "prompts": []})
        status = effective_status(p, runs.get(p.rel, []))
        m["prompts"].append((p, status))
        m["counts"][status] = m["counts"].get(status, 0) + 1
        if status not in NOT_COUNTED:
            m["total"] += 1
        if status in FINISHED:
            m["finished"] += 1
        if status == "running":
            live = [r for r in runs.get(p.rel, []) if r.get("live")]
            m["running"].append((p, live[0] if live else None))
        elif status == "stalled":
            m["stalled"].append(p)
        elif status == "blocked":
            m["blocked"].append(p)
    for m in mods.values():
        done_ids = {p.id for p, s in m["prompts"] if s in FINISHED}
        for p, s in sorted(m["prompts"], key=lambda ps: (_order_key(ps[0].order), ps[0].name)):
            deps = p.meta.get("depends_on") or []
            deps = deps if isinstance(deps, list) else [deps]
            if s == "pending" and all(str(d) in done_ids for d in deps):
                m["next"] = p
                break
    return [mods[k] for k in sorted(mods)]


# --- status changes ----------------------------------------------------------------------------------


class _PromptLock:
    def __init__(self, lock_dir: Path, rel: str) -> None:
        import hashlib

        lock_dir.mkdir(parents=True, exist_ok=True)
        self.path = lock_dir / f"prompt-{hashlib.sha1(rel.encode()).hexdigest()[:12]}.lock"
        self.fd = -1

    def __enter__(self) -> _PromptLock:
        import fcntl

        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> None:
        os.close(self.fd)


def _spec(cfg_or_spec: dict[str, Any]) -> dict[str, Any]:
    """Accept the whole tracker config or just its ``prompts`` block."""
    spec = prompts_cfg(cfg_or_spec) or cfg_or_spec
    if not spec.get("repo"):
        raise ValueError("prompts config needs 'repo' and 'root'")
    return spec


def _lock_dir(spec: dict[str, Any]) -> Path:
    base = os.environ.get("SESSION_TRACKER_DIR") or str(Path.home() / ".claude" / "session-tracker")
    return Path(str(spec.get("lock_dir") or base)) / ".locks"


def mark_running(spec: dict[str, Any], rel: str, sid: str, live_sids: set[str]) -> str | None:
    """Claim a prompt for a session. Returns the previous status, or None when it was not claimable.

    Claimable: ``pending``, ``blocked``, no frontmatter, or ``running`` for a session that is no longer live.
    A prompt never moves backwards from on-branch/on-main/deployed/done/skipped/superseded.
    """
    spec = _spec(spec)
    path = Path(str(spec["repo"])) / rel
    with _PromptLock(_lock_dir(spec), rel):
        meta, _, _ = parse_frontmatter(_read_head(path))
        prev = str(meta.get("status") or "pending")
        if prev == "running" and str(meta.get("session_id") or "") in live_sids:
            return None
        if prev not in ("pending", "blocked", "running"):
            return None
        new = write_meta(path, {"status": "running", "session_id": sid, "updated": _now_iso()},
                         expect={"status": (None, "", prev)})
    return prev if new is not None else None


def set_status(spec: dict[str, Any], rel: str, status: str, clear: list[str] | tuple[str, ...] = (),
               **fields: Any) -> dict[str, Any]:
    """Write ``status`` and the given fields (None = leave alone); ``clear`` names keys to remove."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; one of {', '.join(STATUSES)}")
    if {"status", "updated"} & set(clear):
        raise ValueError("--clear can't remove status or updated")
    updates: dict[str, Any] = {"status": status, "updated": _now_iso(), "todo": None}  # setting a status confirms it
    updates.update({k: v for k, v in fields.items() if v is not None})
    updates.update({k: None for k in clear})
    spec = _spec(spec)
    path = Path(str(spec["repo"])) / rel
    with _PromptLock(_lock_dir(spec), rel):
        meta = write_meta(path, updates)
    if meta is None:
        raise OSError(f"{rel} changed while writing; retry")
    return meta


def commit_prompt_files(spec: dict[str, Any], rels: list[str], message: str, attempts: int = 3) -> str | None:
    """``git commit -m <message> -- <rels>`` in the prompts repo (a pathspec commit, never ``add -A``).

    Retries on a busy index. Returns the new commit's sha, or None when nothing was committed.
    """
    import subprocess
    import time

    repo = str(_spec(spec)["repo"])
    for i in range(attempts):
        try:
            p = subprocess.run(["git", "-C", repo, "commit", "-q", "-m", message, "--", *rels],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        if p.returncode == 0:
            out = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
            return out.stdout.strip() or None
        if "index.lock" in (p.stderr or "") and i + 1 < attempts:
            time.sleep(0.5 * (i + 1))
            continue
        return None
    return None
