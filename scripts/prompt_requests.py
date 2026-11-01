"""The deploy-request queue: ``<tracker>/deploys/requests.jsonl``, one request per line.

CC10: a peer's "deploy request assistants/A04b" arrived as a SendMessage line and left no record of who asked, what
was answered, or which requests were still open. A peer now runs ``command_center.py request <module>/<ID>`` (then
messages the /cc session); ``deploy-ready --answer <id>`` records the verdict, ``hold`` marks the prompt's requests
held, and ``deploy-run`` marks the ones it shipped done. States: open → answered → held | done (``unhold`` reopens a
held one). Every write takes ``requests.lock`` (fcntl); an update rewrites the file atomically (tmp + replace).
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

CLOSED = ("done",)


def deploys_dir() -> Path:
    return Path(os.environ.get("SESSION_TRACKER_DIR") or Path.home() / ".claude" / "session-tracker") / "deploys"


def requests_path() -> Path:
    return deploys_dir() / "requests.jsonl"


@contextmanager
def _locked() -> Iterator[Path]:
    path = requests_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name("requests.lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(path: Path) -> list[dict[str, Any]]:
    out = []
    try:
        text = path.read_text()
    except OSError:
        return []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:  # a torn or hand-mangled line is skipped, never fatal
            continue
        if isinstance(row, dict) and row.get("id"):
            out.append(row)
    return out


def load() -> list[dict[str, Any]]:
    """Every request, oldest first."""
    return sorted(_read(requests_path()), key=lambda r: (str(r.get("ts") or ""), str(r["id"])))


def add(prompt: str, note: str | None, session: str | None, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    row = {"id": datetime.fromtimestamp(now).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(2),
           "ts": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"), "session": session,
           "prompt": prompt, "note": note or "", "state": "open"}
    with _locked() as path:
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
    return row


def update(match: Callable[[dict[str, Any]], bool], changes: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge ``changes`` into every request ``match`` picks; the rows changed."""
    with _locked() as path:
        rows = _read(path)
        hit = [r for r in rows if match(r)]
        if not hit:
            return []
        for r in hit:
            r.update(changes)
        tmp = path.with_name(f".requests.{os.getpid()}.tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        os.replace(tmp, path)
    return hit
