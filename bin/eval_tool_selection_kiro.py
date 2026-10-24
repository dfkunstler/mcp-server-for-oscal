#!/usr/bin/env python3
"""
Run the tool-selection prompts through kiro-cli instead of direct Bedrock calls.

Each version needs a kiro agent named ``oscal-eval-<version>`` in ``--workspace`` that loads
only that version's OSCAL MCP server as ``oscal`` and has a ``preToolUse`` hook that appends
the hook event to ``$EVAL_LOG`` and exits 2 (deny). The first logged event is the model's
first tool call; no tool runs. Arguments are validated against the version's exported
``tools/list`` snapshot (``bin/export_tools_list.py``). kiro-cli does not report token usage.

Not part of ``hatch run tests``: it uses Kiro credits.

    hatch run python bin/eval_tool_selection_kiro.py --workspace private/eval/kiro/ws \
        --tools head=private/eval/tools-branch.json --model claude-haiku-4.5 --out run.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess  # nosec B404 - runs the local kiro-cli binary with fixed arguments
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from eval_tool_selection import PROMPTS_PATH, load_snapshot, summarize
from jsonschema import Draft202012Validator

SERVER = "@oscal/"
KIRO_ARG_KEYS = {"__tool_use_purpose"}


def _decode_stream(text: str) -> list[dict]:
    """Decode concatenated JSON objects (parallel hook calls can share a line)."""
    decoder = json.JSONDecoder()
    events, i = [], 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return events
        obj, i = decoder.raw_decode(text, i)
        events.append(obj)


def run_one(workspace: Path, agent: str, model: str, snap: dict, prompt: dict) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        log = Path(f.name)
    start = time.perf_counter()
    proc = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
        [  # noqa: S607 - kiro-cli is resolved from PATH on purpose
            "kiro-cli",
            "chat",
            "--agent",
            agent,
            "--model",
            model,
            "--no-interactive",
            "--trust-all-tools",
            prompt["prompt"],
        ],
        cwd=workspace,
        env={**os.environ, "EVAL_LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    latency = time.perf_counter() - start
    events = _decode_stream(log.read_text())
    log.unlink(missing_ok=True)
    first = events[0] if events else None
    tool = args = None
    arg_errors: list[str] = []
    if first:
        name = first["tool_name"]
        tool = name.removeprefix(SERVER) if name.startswith(SERVER) else f"<builtin:{name}>"
        args = {k: v for k, v in (first.get("tool_input") or {}).items() if k not in KIRO_ARG_KEYS}
        schema = snap["schemas"].get(tool, {})
        arg_errors = [e.message for e in Draft202012Validator(schema).iter_errors(args)]
        arg_errors += [
            f"unknown argument {k!r}" for k in sorted(set(args) - set(schema.get("properties", {})))
        ]
    return {
        "tool": tool,
        "args": args,
        "argErrors": arg_errors,
        "toolCallsLogged": len(events),
        "exitCode": proc.returncode,
        "stderrTail": proc.stderr[-300:] if proc.returncode else "",
        "inputTokens": 0,
        "outputTokens": 0,
        "latency": latency,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--workspace", required=True, help="directory holding .kiro/agents")
    ap.add_argument("--tools", action="append", required=True, help="NAME=snapshot.json")
    ap.add_argument("--model", required=True, help="kiro-cli model id, e.g. claude-haiku-4.5")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", action="append", help="prompt id to run (repeatable)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    prompts = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
    if args.only:
        prompts = [p for p in prompts if p["id"] in set(args.only)]
    snaps = {}
    for spec in args.tools:
        name, path = spec.split("=", 1)
        snaps[name] = load_snapshot(path)
    workspace = Path(args.workspace).resolve()

    def work(job: tuple) -> dict:
        v, p, i = job
        snap = snaps[v]
        r = run_one(workspace, f"oscal-eval-{v}", args.model, snap, p)
        return {
            "version": v,
            "id": p["id"],
            "repeat": i,
            "applicable": any(e in snap["names"] for e in p["expect"]),
            "correct": r["tool"] in p["expect"],
            "acceptable": r["tool"] in p["expect"] + p.get("ok", []),
            **r,
        }

    jobs = [(v, p, i) for v in snaps for p in prompts for i in range(args.repeats)]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(work, jobs))

    out = {"runner": "kiro-cli", "model": args.model, "repeats": args.repeats, "results": results}
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    failed = [r for r in results if r["exitCode"]]
    print(  # noqa: T201 - CLI report
        f"kiro-cli model={args.model} repeats={args.repeats} runs={len(results)} "
        f"failed_runs={len(failed)} (input tokens not reported by kiro-cli)"
    )
    print(summarize(results, prompts, list(snaps)))  # noqa: T201 - CLI report


if __name__ == "__main__":
    main()
