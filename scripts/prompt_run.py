"""deploy-run: the one acting path. The plan's restarts, one step at a time, each proven before the next.

Owner 10-31: "all deployments and restarts should be coordinated from here when /cc is triggered". It runs only with
the owner's OK quoted (``--owner-ok``), only when deploy-ready says OK, never a migration (it stops and prints the
line to apply by hand), and under the deploy lock (``deploy.lock``; a ``flock <that lock>`` prefix on a restart
command is dropped, since this process holds it). Each step: its commands, then the proofs as separate reads (the unit
started after the step began, its ready line, ``/api/health`` ×3 = 200 200 200). A failed command or proof stops the
run. The record ``<tracker>/deploys/<id>.json`` is written before the first step and after each one; it has ``end``
only once the run is over, so a file without ``end`` is a deploy-run in progress (the restart hook reads that).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import prompt_deploy as pd
import prompt_ready as pr

DEPLOY_LOCK = "/run/lock/neurogen-aa-deploy.lock"
PROOF_WAIT_S = 120
POLL_S = 5
_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")


def lock_path(umap: dict[str, Any]) -> str:
    return str((umap.get("deploy") or {}).get("lock") or DEPLOY_LOCK)


def strip_lock(cmd: str, lock: str) -> str:
    """``flock <lock> X`` → ``X`` when <lock> is the one this run holds (flock'ing it again would wait on ourselves)."""
    m = re.match(r"\s*flock\s+(\S+)\s+(.+)$", cmd)
    return m.group(2) if m and m.group(1) == lock else cmd


def proof_commands(step: dict[str, Any], umap: dict[str, Any]) -> list[tuple[str, str, str]]:
    """[(kind, unit, command)] in the order they are read: start time per unit, ready lines, after_each."""
    dep = umap.get("deploy") or {}
    ready = dep.get("ready") or {}
    proof = list(dep.get("proof") or []) or ["systemctl show -p ActiveEnterTimestamp,ActiveState {unit}"]
    out = [("started", u, str(proof[0]).replace("{unit}", u)) for u in step["units"]]
    out += [("ready", u, str(ready[u])) for u in step["units"] if u in ready]
    out += [("after_each", "", str(c)) for c in dep.get("after_each") or []]
    return out


def dry_run_lines(plan: dict[str, Any], umap: dict[str, Any]) -> list[str]:
    lock = lock_path(umap)
    lines = ["Steps (dry run: every command in order; nothing runs)"]
    for i, st in enumerate(plan["steps"], 1):
        lines.append(f"{i}. {st['unit']}")
        lines += [f"  {strip_lock(c, lock)}" for c in st["commands"]]
        lines += [f"  proof: {c}" for _, _, c in proof_commands(st, umap)]
    if not plan["steps"]:
        lines.append("  nothing to restart")
    return lines


def _shell(cmd: str, run: Any, env: dict[str, str] | None = None, timeout: int = 60) -> tuple[int, str]:
    try:
        p = run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, type(e).__name__
    return p.returncode, (p.stdout or "") + (p.stderr or "" if p.returncode else "")


def _line_time(line: str) -> float | None:
    """A log line's local stamp in epoch seconds; in the hour a DST fall-back repeats, the later reading (live 11-01
    01:02 EST: read as EDT, a fresh ready line looked an hour old). The unit's start time is proven separately."""
    m = _STAMP.search(line or "")
    try:
        t = datetime.fromisoformat(m.group(1).replace(" ", "T")) if m else None
    except ValueError:
        return None
    return max(t.timestamp(), t.replace(fold=1).timestamp()) if t else None


def prove(step: dict[str, Any], umap: dict[str, Any], start: int, run: Any, sleep: Callable[[float], None],
          wait_s: float) -> list[dict[str, Any]]:
    """Each proof once it holds, or its last reading when ``wait_s`` runs out (start times and ready lines take a
    while after a restart; health is read once)."""
    out = []
    for kind, unit, cmd in proof_commands(step, umap):
        deadline = time.time() + wait_s
        while True:
            if kind == "started":
                t = pd.unit_started(unit)
                ok, got = t is not None and t >= start, f"started {datetime.fromtimestamp(t).isoformat()}" if t else "not running"
            else:
                rc, got = _shell(cmd, run)
                if kind == "ready":
                    when = _line_time(got.strip().splitlines()[-1] if got.strip() else "")
                    ok = rc == 0 and bool(got.strip()) and (when is None or when >= start - 1)
                else:
                    ok = rc == 0 and pr.health_ok(got)
            if ok or kind == "after_each" or time.time() >= deadline:
                break
            sleep(min(POLL_S, max(0.0, deadline - time.time())))
        out.append({"cmd": cmd, "out": got.strip()[:500], "ok": ok})
        if not ok:
            break
    return out


def tracker_deploys() -> Path:
    return Path(os.environ.get("SESSION_TRACKER_DIR") or Path.home() / ".claude" / "session-tracker") / "deploys"


def write_record(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=1, default=str))
    os.replace(tmp, path)


def last_deploy(directory: Path | None = None) -> float | None:
    """When the newest deploy-run ended (or started, if it has not)."""
    d = directory or tracker_deploys()
    for f in sorted(d.glob("*.json"), reverse=True) if d.is_dir() else []:
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        t = rec.get("end") or rec.get("started")
        if t:
            return float(t)
    return None


def execute(plan: dict[str, Any], umap: dict[str, Any], repo: str, owner_ok: str, out: Callable[[str], None] = print,
            run: Any = subprocess.run, sleep: Callable[[float], None] = time.sleep) -> tuple[int, dict[str, Any] | None]:
    """Run the steps under the deploy lock. (exit code, the record) — 1 with no record when the lock is held."""
    dep = umap.get("deploy") or {}
    lock = lock_path(umap)
    try:
        fh = open(lock, "a")
    except OSError as e:
        out(f"deploy lock {lock} unusable ({type(e).__name__}): nothing restarted")
        return 1, None
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            out(f"deploy lock held: {lock} (another deploy runs): nothing restarted")
            return 1, None
        rid = time.strftime("%Y%m%dT%H%M%S")
        path = tracker_deploys() / f"{rid}.json"
        rec: dict[str, Any] = {
            "id": rid, "owner_ok": owner_ok, "session": pr.own_session(), "started": int(time.time()),
            "plan": {"prompts": [f"{e['module']}/{e['id'] or e['name']}" for e in plan["prompts"]], "units": plan["units"],
                     "steps": [{"unit": s["unit"], "commands": s["commands"]} for s in plan["steps"]]},
            "steps": [], "result": None, "deployed": [], "end": None}
        write_record(path, rec)
        env = {**os.environ, "NEUROGEN_DEPLOY_RUN": rid}
        wait_s = float(dep.get("proof_wait_s") or PROOF_WAIT_S)
        for i, st in enumerate(plan["steps"]):
            start = int(time.time())
            row: dict[str, Any] = {"unit": st["unit"], "start": start, "end": None, "commands": [], "proofs": []}
            rec["steps"].append(row)
            failed = None
            for c in st["commands"]:
                cmd = strip_lock(c, lock)
                out(f"{st['unit']}: {cmd}")
                try:
                    rc = subprocess.run(cmd, shell=True, cwd=repo, env=env).returncode
                except OSError:
                    rc = 127
                row["commands"].append({"cmd": cmd, "rc": rc})
                if rc != 0:
                    failed = f"{cmd} exited {rc}"
                    break
            if not failed:
                row["proofs"] = prove(st, umap, start, run, sleep, wait_s)
                bad = [p for p in row["proofs"] if not p["ok"]]
                failed = f"proof failed: {bad[0]['cmd']} → {bad[0]['out'][:120] or '(nothing)'}" if bad else None
            row["end"] = int(time.time())
            if failed:
                rec["result"], rec["end"] = f"failed at {st['unit']}: {failed}", int(time.time())
                write_record(path, rec)
                live = [s["unit"] for s in plan["steps"][:i]]
                out(f"STOPPED at {st['unit']}: {failed}")
                out("Live (restarted and proven): " + (", ".join(live) or "none"))
                out("Not live: " + ", ".join(s["unit"] for s in plan["steps"][i:]))
                return 1, rec
            started = [p["out"] for p in row["proofs"] if p["out"].startswith("started ")]
            out(f"{st['unit']}: proven ({'; '.join(started) or 'no start time read'})")
            write_record(path, rec)
        rec["result"] = "ok"
        write_record(path, rec)
        rec["_path"] = str(path)
        return 0, rec
    finally:
        fh.close()


def finish(rec: dict[str, Any], deployed: list[str]) -> None:
    path = Path(rec.pop("_path"))
    rec["deployed"], rec["end"] = deployed, int(time.time())
    write_record(path, rec)
