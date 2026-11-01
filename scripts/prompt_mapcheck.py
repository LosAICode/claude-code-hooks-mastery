"""map-check: the files a unit's process loads that no unit-map rule for that unit matches.

The map's ``deploy.entry`` block names each unit's entry files and import roots (repo-relative)::

    "entry": {"app-channels": {"files": ["modules/channel_listeners_server.py"], "roots": ["modules"]}}

From the entry files the walk follows every ``import X`` and ``from X import Y`` (``Y`` too when it is a submodule
of package ``X``), relative imports, ``importlib.import_module("<constant>")``, at any depth (imports inside
functions count: lazy imports are the norm). A module is followed only when it resolves to a file under one of the
roots, and a package's ``__init__.py`` counts as loaded with it. Nothing is imported or run: each import statement
is parsed with ``ast``. Files under a ``tests/`` folder are skipped. Modules loaded by name from data (``"pkg.mod:Class"`` strings)
are invisible to the walk: list them as entry files.

A reached file is a gap when ``prompt_deploy.needs`` on it alone does not give the unit: a change to that file
would not restart the process that runs it.
"""

from __future__ import annotations

import ast
import os
import re
from collections import deque
from pathlib import Path
from typing import Any

import prompt_deploy as pd

_STMT = re.compile(r"^[ \t]*(?:import[ \t]+[\w.]|from[ \t]+\.*[\w.]*[ \t]+import\b).*$", re.MULTILINE)
_DYNAMIC = re.compile(r"""\b(?:import_module|__import__)\(\s*['"]([\w.]+)['"]""")


class Walker:
    """Resolves and parses files under one repo; the parse of each file is cached across units."""

    def __init__(self, repo: str | Path):
        self.repo = Path(repo)
        self._imports: dict[str, list[tuple[str, int, str | None]]] = {}
        self._resolved: dict[tuple[str, tuple[str, ...]], list[str]] = {}

    def imports(self, rel: str) -> list[tuple[str, int, str | None]]:
        """(module, relative level, imported name or None) for every import in the file, at any depth.

        Parsing whole files is too slow on a large tree (a full ``ast.parse`` + walk of the -channels closure took
        26 s), so each import statement is cut out by line (with its parenthesized or ``\\`` continuation) and
        only that statement is parsed. An ``import`` line inside a docstring may count: that over-reports, never
        under-reports."""
        if rel in self._imports:
            return self._imports[rel]
        out: list[tuple[str, int, str | None]] = []
        try:
            text = (self.repo / rel).read_text(errors="replace")
        except OSError:
            text = ""
        for m in _STMT.finditer(text):
            stmt = [m.group(0).split("#")[0].strip()]
            depth = stmt[0].count("(") - stmt[0].count(")")
            end = m.end()
            while (depth > 0 or stmt[-1].endswith("\\")) and end < len(text):
                nl = text.find("\n", end + 1)
                nl = len(text) if nl < 0 else nl
                stmt.append(text[end + 1:nl].split("#")[0].strip())
                depth += stmt[-1].count("(") - stmt[-1].count(")")
                end = nl
            src = " ".join(s.rstrip("\\") for s in stmt)
            try:
                tree = ast.parse(src)
            except SyntaxError:
                try:
                    tree = ast.parse(src.split(";")[0])     # ``import x; if y:``
                except SyntaxError:
                    continue
            for node in tree.body:
                if isinstance(node, ast.Import):
                    out += [(a.name, 0, None) for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    out += [(node.module or "", node.level, a.name) for a in node.names]
        out += [(m.group(1), 0, None) for m in _DYNAMIC.finditer(text)]
        self._imports[rel] = out
        return out

    def resolve(self, dotted: str, roots: list[str]) -> list[str]:
        """The files ``import dotted`` loads under the first root that has it: each package's __init__.py, then the
        module. [] when it is not under any root (stdlib, site-packages)."""
        key = (dotted, tuple(roots))
        if key not in self._resolved:
            self._resolved[key] = self._resolve(dotted, roots)
        return self._resolved[key]

    def _resolve(self, dotted: str, roots: list[str]) -> list[str]:
        parts = [p for p in dotted.split(".") if p]
        for root in roots:
            files, ok, d = [], bool(parts), root
            for i, part in enumerate(parts):
                d = f"{d}/{part}"
                last = i == len(parts) - 1
                if os.path.isfile(os.path.join(self.repo, d, "__init__.py")):
                    files.append(f"{d}/__init__.py")
                elif last and os.path.isfile(os.path.join(self.repo, d + ".py")):
                    files.append(d + ".py")
                elif not last and os.path.isdir(os.path.join(self.repo, d)):
                    continue        # a namespace package: nothing to load at this level
                else:
                    ok = False
                    break
            if ok and files:
                return files
        return []

    def is_package(self, rel: str) -> bool:
        return rel.endswith("/__init__.py")

    def package_of(self, rel: str, roots: list[str]) -> list[str] | None:
        """The dotted package parts a file sits in, relative to the root that holds it."""
        for root in roots:
            prefix = root.rstrip("/") + "/"
            if rel.startswith(prefix):
                parts = rel[len(prefix):].split("/")[:-1]
                return parts
        return None

    def targets(self, rel: str, roots: list[str]) -> list[str]:
        """Every file under the roots that one file's imports load."""
        out: list[str] = []
        for module, level, name in self.imports(rel):
            if level:
                pkg = self.package_of(rel, roots)
                if pkg is None or level - 1 > len(pkg):
                    continue
                base = pkg[:len(pkg) - (level - 1)]
                dotted = ".".join(base + ([module] if module else []))
                if not dotted:
                    if name and name != "*":
                        out += self.resolve(name, roots)
                    continue
            else:
                dotted = module
            got = self.resolve(dotted, roots)
            out += got
            if got and name and name != "*" and self.is_package(got[-1]):
                out += self.resolve(f"{dotted}.{name}", roots)
        return out

    def walk(self, files: list[str], roots: list[str]) -> dict[str, str | None]:
        """BFS from the entry files: every reached file → the file that first imported it (None for an entry)."""
        parent: dict[str, str | None] = {}
        queue: deque[str] = deque()
        for f in files:
            if f not in parent and (self.repo / f).is_file():
                parent[f] = None
                queue.append(f)
        while queue:
            cur = queue.popleft()
            for t in self.targets(cur, roots):
                if t not in parent and not _in_tests(t):
                    parent[t] = cur
                    queue.append(t)
        return parent


def _in_tests(rel: str) -> bool:
    return "tests" in rel.split("/")[:-1]


def chain(parent: dict[str, str | None], rel: str) -> list[str]:
    out: list[str] = [rel]
    while parent.get(out[-1]) is not None:
        out.append(str(parent[out[-1]]))
    return out[::-1]


def entries(umap: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The units in ``deploy.entry``; its ``about`` note (and any other non-object value) is not one."""
    return {k: v for k, v in (((umap.get("deploy") or {}).get("entry")) or {}).items() if isinstance(v, dict)}


def check(repo: str | Path, umap: dict[str, Any], unit: str | None = None,
          walker: Walker | None = None) -> dict[str, dict[str, Any]]:
    """{unit: {"reached": n, "missing": [entry files not found], "gaps": [{"file", "chain"}]}} for each entry unit."""
    walker = walker or Walker(repo)
    out: dict[str, dict[str, Any]] = {}
    for name, spec in entries(umap).items():
        if unit and name != unit:
            continue
        files = [str(f) for f in spec.get("files") or []]
        roots = [str(r).rstrip("/") for r in spec.get("roots") or []]
        parent = walker.walk(files, roots)
        gaps = [{"file": f, "chain": chain(parent, f)} for f in sorted(parent)
                if name not in pd.needs([f], umap)[0]]
        out[name] = {"reached": len(parent), "missing": [f for f in files if f not in parent], "gaps": gaps}
    return out


def finding(unit: str, result: dict[str, Any], shown: int = 3) -> str | None:
    """The one audit line for a unit with gaps (None when it has none)."""
    gaps = [g["file"] for g in result["gaps"]] + [f"{f} (entry file missing)" for f in result["missing"]]
    if not gaps:
        return None
    more = ", …" if len(gaps) > shown else ""
    return f"map-check: {unit} reaches {len(gaps)} file(s) no rule maps: {', '.join(gaps[:shown])}{more}"
