#!/usr/bin/env python3
"""Ask a question about big files without putting them in the session's context (RLM).

A port of NeuroGen's session RLM (`modules/services/session_rlm_service.py`), after Zhang, Kraska & Khattab,
"Recursive Language Models" (arXiv:2512.24601; reference implementation github.com/alexzhang13/rlm).

The content is held in a Python variable `context`, NOT in any prompt. A root model writes Python to search
it, sees only the (capped) print output, may call `llm(instruction, text)` (a cheap sub-model) over slices,
and ends with FINAL("answer"). Only the answer comes back, so a 600 KB document costs the calling session a
few hundred tokens instead of riding along on every later call.

Models run through the `claude` CLI (`claude -p`, the account's own login): no API key, no hooks, no tools,
no MCP, no CLAUDE.md, no saved session. Each call starts at about 600 tokens.

    python3 scripts/rlm.py "Which env vars does the voice unit read?" Documentation/Developer-Reference.md
    journalctl -u neurogenserver --since -1h | python3 scripts/rlm.py "Why did requests 500?" -
    python3 scripts/rlm.py "List every open item about KB" /opt/neurogenserver/Documentation/*.md --stats
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT_MODEL = "sonnet"
SUB_MODEL = "haiku"
MAX_ITERATIONS = 8
MAX_SUB_CALLS = 12
MAX_OUTPUT_CHARS = 2000
MAX_SUBCALL_CHARS = 8000
MAX_FILE_CHARS = 5_000_000
CALL_TIMEOUT = 180

SUB_SYSTEM = (
    "You are answering about one excerpt of a larger document. Answer only from the excerpt. "
    "Be concise and factual. If the excerpt does not contain the answer, say so plainly."
)


class Usage:
    def __init__(self):
        self.root_calls = 0
        self.sub_calls = 0
        self.cost_usd = 0.0

    def line(self) -> str:
        return f"[rlm] root calls {self.root_calls}, sub calls {self.sub_calls}, cost ${self.cost_usd:.4f}"


def claude_call(prompt: str, system: str, model: str, usage: Usage) -> str:
    """One stateless `claude -p` call. Raises RuntimeError on failure."""
    cmd = [
        "claude", "-p", "--model", model, "--system-prompt", system, "--tools", "",
        "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence", "--output-format", "json",
    ]
    # An empty cwd: nothing to auto-discover, and project hooks never see these calls.
    with tempfile.TemporaryDirectory(prefix="rlm-") as cwd:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=CALL_TIMEOUT, cwd=cwd)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude -p exited {proc.returncode}: {(proc.stdout or proc.stderr)[:300]}")
    usage.cost_usd += float(data.get("total_cost_usd") or 0)
    if data.get("is_error"):
        raise RuntimeError(f"claude -p error: {str(data.get('result'))[:300]}")
    return str(data.get("result") or "")


class SubCallBudget:
    def __init__(self, max_calls: int):
        self.max_calls = max_calls
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True


def make_sub_llm(model: str, budget: SubCallBudget, usage: Usage):
    """The `llm()` the REPL code calls. Never raises: a readable string lets the root model adapt."""

    def llm(instruction: str, text: str = "") -> str:
        if not budget.take():
            return (f"[llm() budget exhausted: {budget.max_calls} sub-calls used. "
                    f"Answer from what you already have, or process fewer items.]")
        payload = str(text or "")
        truncated = len(payload) > MAX_SUBCALL_CHARS
        payload = payload[:MAX_SUBCALL_CHARS]
        message = f"{instruction}\n\n---\n{payload}" if payload else str(instruction)
        usage.sub_calls += 1
        try:
            out = claude_call(message, SUB_SYSTEM, model, usage)
        except Exception as e:
            return f"[llm() failed: {type(e).__name__}]"
        if not out:
            return "[llm() returned nothing]"
        return f"{out}\n[note: excerpt was truncated to {MAX_SUBCALL_CHARS} chars]" if truncated else out

    return llm


# Same guard as NeuroGen's check_code_safety: the code is written by a model that has just read the files,
# so a poisoned file could steer it. Every CPython escape goes through a dunder attribute.
_DENIED_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input", "breakpoint", "globals", "locals", "vars", "dir",
    "getattr", "setattr", "delattr", "memoryview", "exit", "quit", "help", "license", "credits",
})

_SAFE_BUILTINS = {
    name: __builtins__[name] if isinstance(__builtins__, dict) else getattr(__builtins__, name)
    for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "frozenset", "int", "isinstance",
        "len", "list", "map", "max", "min", "print", "range", "repr", "reversed", "round", "set", "slice",
        "sorted", "str", "sum", "tuple", "zip", "Exception", "ValueError", "KeyError", "IndexError",
    )
}


def check_code_safety(code: str) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Syntax error: {e.msg} (line {e.lineno})"
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return f"Blocked: attribute '{node.attr}' — underscore attributes are not available."
        if isinstance(node, ast.Name):
            n = node.id
            if n.startswith("__"):
                return f"Blocked: name '{n}' — dunder names are not available."
            if n in _DENIED_NAMES:
                return f"Blocked: '{n}' is not available in this sandbox."
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "Blocked: imports are not available. `re`, `json`, `math`, `Counter`, `defaultdict` are in scope."
    return None


def execute(code: str, env: dict) -> str:
    error = check_code_safety(code)
    if error:
        return error
    lines: list[str] = []
    env["print"] = lambda *a, sep=" ", end="\n", **_: lines.append(sep.join(str(x) for x in a) + end)
    try:
        exec(compile(code, "<repl>", "exec"), env)  # noqa: S102 - guarded above, restricted builtins
    except Exception as e:
        lines.append(f"{type(e).__name__}: {e}\n")
    out = "".join(lines) or "(no output — use print())"
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + f"\n[output truncated at {MAX_OUTPUT_CHARS} chars]"
    return out


def extract_code(text: str) -> str | None:
    blocks = re.findall(r"```(?:python|repl|py)?\s*\n(.*?)```", text, re.DOTALL)
    if blocks:
        return "\n".join(blocks)
    # With no tools, the model sometimes writes a tool call instead; run only the first, since it then
    # imagines the output and writes more.
    m = re.search(r'<parameter name="code">(.*?)</parameter>', text, re.DOTALL)
    return m.group(1) if m else None


def extract_final(text: str, env: dict) -> str | None:
    m = re.search(r"FINAL_VAR\(\s*['\"]?(\w+)['\"]?\s*\)", text)
    if m and m.group(1) in env:
        return str(env[m.group(1)])
    for pattern in (r'FINAL\s*\(\s*"""(.*?)"""\s*\)', r"FINAL\s*\(\s*'''(.*?)'''\s*\)",
                    r'FINAL\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)', r"FINAL\s*\(\s*'((?:[^'\\]|\\.)*)'\s*\)"):
        m = re.search(pattern, text, re.DOTALL)
        if m and m.group(1).strip() not in {"", "...", "answer", "your answer here"}:
            return re.sub(r"\\([\"'\\])", r"\1", m.group(1).strip())
    return None


def system_prompt(context_size: int, files: list[str], max_sub_calls: int) -> str:
    listing = "\n".join(f"  - {f}" for f in files[:40]) + ("\n  - ..." if len(files) > 40 else "")
    return (
        "You are answering a question about files via a Python REPL.\n\n"
        f"The content is in the variable `context` ({context_size:,} characters). Files, each starting with a "
        f"line '=== FILE: <path> ===':\n{listing}\n"
        "You CANNOT see the content directly — write Python code to explore it.\n\n"
        "Available:\n"
        "- `context`: str — all file text; `files`: dict path -> text; `query`: str — the question\n"
        "- `line_of(pos)`: int — line number, within its file, of a character offset in `context` (for citations)\n"
        "- `re`, `json`, `math`, `Counter`, `defaultdict` (no imports)\n"
        "- `llm(instruction, text)`: str — ask a cheap model about a PIECE of content (≤8000 chars).\n"
        f"  Budget: {max_sub_calls} calls. Use it for judgement (summarise, classify, 'which parts MEAN x').\n"
        "  Prefer regex/string work for locating things — it is free.\n\n"
        "Write code in ```python blocks. Only print() output is shown to you, capped at 2000 chars.\n"
        "Variables persist between turns.\n"
        "STRATEGY: if a keyword search finds nothing, the text phrases it differently; switch to llm() over "
        "chunks rather than guessing more synonyms.\n\n"
        "When you have the answer, call FINAL(\"answer\") (or FINAL(variable)) in code or as plain text. "
        "Cite file paths (and line numbers where you found them). Do NOT guess — read the content first."
    )


def load(paths: list[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    for p in paths:
        if p == "-":
            files["<stdin>"] = sys.stdin.read()
            continue
        path = Path(p)
        for f in sorted(path.rglob("*")) if path.is_dir() else [path]:
            if f.is_file():
                try:
                    files[str(f)] = f.read_text(errors="replace")
                except OSError as e:
                    print(f"[rlm] skipped {f}: {e.strerror}", file=sys.stderr)
    return files


def line_in_file(context: str, pos: int) -> int:
    """Line number, within its own file, of a character offset in `context`."""
    header = context.rfind("=== FILE: ", 0, pos + 1)
    body = context.find("\n", header) + 1 if header >= 0 else 0
    return context.count("\n", body, pos) + 1


def run(query: str, files: dict[str, str], root_model: str, sub_model: str, max_iterations: int,
        max_sub_calls: int, usage: Usage, verbose: bool = False) -> str:
    context = "".join(f"=== FILE: {name} ===\n{text}\n" for name, text in files.items())
    if len(context) > MAX_FILE_CHARS:
        print(f"[rlm] content is {len(context):,} chars; only the first {MAX_FILE_CHARS:,} are searched",
              file=sys.stderr)
        context = context[:MAX_FILE_CHARS]
    env = {
        "__builtins__": dict(_SAFE_BUILTINS), "context": context, "files": dict(files), "query": query,
        "re": re, "json": json, "math": math, "Counter": Counter, "defaultdict": defaultdict,
        "llm": make_sub_llm(sub_model, SubCallBudget(max_sub_calls), usage),
        "line_of": lambda pos: line_in_file(context, pos),
    }
    answers: list[str] = []
    env["FINAL"] = lambda answer: answers.append(str(answer))
    env["FINAL_VAR"] = lambda value: answers.append(str(value))
    system = system_prompt(len(context), list(files), max_sub_calls)
    transcript = f"QUESTION: {query}\n\nStart by looking at the structure of `context`."
    grounded = False
    for turn in range(1, max_iterations + 1):
        usage.root_calls += 1
        reply = claude_call(transcript, system, root_model, usage)
        code = extract_code(reply)
        # A FINAL counts only once the model has seen output that read the content, on an earlier turn.
        was_grounded = grounded
        if code:
            result = execute(code, env)
            grounded = grounded or "context" in code or "files" in code
            if verbose:
                print(f"--- turn {turn} code ---\n{code}\n--- output ---\n{result}", file=sys.stderr)
            transcript += f"\n\nASSISTANT:\n{reply}\n\nREPL OUTPUT (turn {turn}/{max_iterations}):\n{result}"
            if 0 < max_iterations - turn <= 2:
                transcript += (f"\n\nREPL: {max_iterations - turn} turn(s) left. Finish now: "
                               "call FINAL(answer) with what you have, saying what you could not confirm.")
        final = answers.pop() if answers else extract_final(re.sub(r"```.*?```", "", reply, flags=re.DOTALL), env)
        answers.clear()
        if final and was_grounded:
            return final
        if final:
            transcript += ("" if code else f"\n\nASSISTANT:\n{reply}") + (
                "\n\nREPL: STOP — that FINAL came before you saw any output that read `context`. "
                "Look at the output above (or read the content), then answer.")
        elif not code:
            if verbose:
                print(f"--- turn {turn} reply (no code) ---\n{reply[:1500]}", file=sys.stderr)
            transcript += f"\n\nASSISTANT:\n{reply}\n\nREPL: no ```python block found. Write code or call FINAL()."
    usage.root_calls += 1
    return claude_call(transcript + "\n\nOut of turns. Give your best answer now from what you saw, "
                       "saying what you could not confirm. Plain text, no code.", system, root_model, usage)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query")
    ap.add_argument("paths", nargs="+", help="files, directories, or - for stdin")
    ap.add_argument("--root-model", default=ROOT_MODEL)
    ap.add_argument("--sub-model", default=SUB_MODEL)
    ap.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)
    ap.add_argument("--max-sub-calls", type=int, default=MAX_SUB_CALLS)
    ap.add_argument("--stats", action="store_true", help="print calls and cost to stderr")
    ap.add_argument("--verbose", action="store_true", help="print each turn's code and output to stderr")
    args = ap.parse_args()

    files = load(args.paths)
    if not files or not any(files.values()):
        print("[rlm] no readable content", file=sys.stderr)
        return 1
    usage = Usage()
    try:
        answer = run(args.query, files, args.root_model, args.sub_model, args.max_iterations,
                     args.max_sub_calls, usage, args.verbose)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"[rlm] failed: {e}", file=sys.stderr)
        return 1
    finally:
        if args.stats:
            print(usage.line(), file=sys.stderr)
    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
