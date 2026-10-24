#!/usr/bin/env python3
"""
Compare first-call tool selection across exported tools/list snapshots (calls Bedrock).

Each snapshot comes from ``bin/export_tools_list.py``. For every prompt, version, and repeat,
this sends one Bedrock Converse request with the snapshot's tools and server instructions,
then records the first tool the model calls, whether its arguments validate against that
tool's inputSchema, input tokens, and latency. Tools are not executed.

Not part of ``hatch run tests``: it makes paid Bedrock calls.

    hatch run python bin/eval_tool_selection.py \
        --tools v063=private/eval/tools-v063.json --tools branch=private/eval/tools-branch.json \
        --model us.anthropic.claude-haiku-4-5-20251001-v1:0 --repeats 3 --out private/eval/run.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from jsonschema import Draft202012Validator

PROMPTS_PATH = Path(__file__).with_name("eval_tool_selection_prompts.json")
SYSTEM_SUFFIX = (
    "You are an assistant connected to an OSCAL MCP server. Answer the user's request by "
    "calling the single most appropriate tool."
)


def load_snapshot(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    tool_config = {
        "tools": [
            {
                "toolSpec": {
                    "name": t["name"],
                    "description": t.get("description") or t["name"],
                    "inputSchema": {"json": t["inputSchema"]},
                }
            }
            for t in data["tools"]
        ]
    }
    system = "\n\n".join(p for p in (data.get("instructions", ""), SYSTEM_SUFFIX) if p)
    return {
        "toolConfig": tool_config,
        "system": system,
        "schemas": {t["name"]: t["inputSchema"] for t in data["tools"]},
        "names": {t["name"] for t in data["tools"]},
        "toolsListChars": data["toolsListChars"],
    }


def run_one(client: Any, model: str, snap: dict, prompt: dict) -> dict[str, Any]:
    start = time.perf_counter()
    resp = client.converse(
        modelId=model,
        system=[{"text": snap["system"]}],
        messages=[{"role": "user", "content": [{"text": prompt["prompt"]}]}],
        toolConfig=snap["toolConfig"],
        inferenceConfig={"temperature": 0, "maxTokens": 512},
    )
    latency = time.perf_counter() - start
    uses = [c["toolUse"] for c in resp["output"]["message"]["content"] if "toolUse" in c]
    first = uses[0] if uses else None
    tool = first["name"] if first else None
    args = first["input"] if first else None
    arg_errors: list[str] = []
    if first:
        schema = snap["schemas"].get(tool, {})
        arg_errors = [e.message for e in Draft202012Validator(schema).iter_errors(args)]
        unknown = set(args) - set(schema.get("properties", {}))
        arg_errors += [f"unknown argument {k!r}" for k in sorted(unknown)]
    return {
        "tool": tool,
        "args": args,
        "argErrors": arg_errors,
        "inputTokens": resp["usage"]["inputTokens"],
        "outputTokens": resp["usage"]["outputTokens"],
        "latency": latency,
    }


def summarize(results: list[dict], prompts: list[dict], versions: list[str]) -> str:
    by = defaultdict(list)
    for r in results:
        by[(r["version"], r["id"])].append(r)
    lines = []
    header = f"{'version':<8} {'scored':>6} {'correct':>8} {'+ok step':>8} {'valid args':>10} "
    header += f"{'no tool':>8} "
    header += f"{'in tok':>8} {'p50 s':>6}"
    lines.append(header)
    for v in versions:
        rows = [r for r in results if r["version"] == v and r["applicable"]]
        if not rows:
            continue
        correct = sum(r["correct"] for r in rows)
        acceptable = sum(r["acceptable"] for r in rows)
        valid = sum(r["correct"] and not r["argErrors"] for r in rows)
        none = sum(r["tool"] is None for r in rows)
        tok = statistics.mean(r["inputTokens"] for r in rows)
        lat = statistics.median(r["latency"] for r in rows)
        lines.append(
            f"{v:<8} {len(rows):>6} {correct / len(rows):>8.0%} {acceptable / len(rows):>8.0%} "
            f"{valid / len(rows):>10.0%} "
            f"{none:>8} {tok:>8.0f} {lat:>6.2f}"
        )
    lines.append("")
    lines.append(
        "Per prompt (correct/repeats; '-' = no expected tool in that version; "
        "'~' marks an acceptable discovery step):"
    )
    lines.append(f"{'prompt':<26} " + " ".join(f"{v:>8}" for v in versions) + "  picks that differ")
    for p in prompts:
        cells, picks = [], []
        for v in versions:
            rs = by[(v, p["id"])]
            if not rs or not rs[0]["applicable"]:
                cells.append(f"{'-':>8}")
                continue
            cells.append(f"{sum(r['correct'] for r in rs)}/{len(rs):>6}")
            wrong = sorted(
                {str(r["tool"]) + ("~" if r["acceptable"] else "") for r in rs if not r["correct"]}
            )
            bad_args = sorted({e for r in rs if r["correct"] for e in r["argErrors"]})
            if wrong:
                picks.append(f"{v}: {','.join(wrong)}")
            if bad_args:
                picks.append(f"{v} args: {'; '.join(bad_args)[:80]}")
        lines.append(f"{p['id']:<26} " + " ".join(cells) + "  " + " | ".join(picks))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--tools", action="append", required=True, help="NAME=snapshot.json")
    ap.add_argument("--model", required=True)
    ap.add_argument("--region", default=None)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", action="append", help="prompt id to run (repeatable)")
    args = ap.parse_args()

    prompts = json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))
    if args.only:
        prompts = [p for p in prompts if p["id"] in set(args.only)]
    snaps = {}
    for spec in args.tools:
        name, path = spec.split("=", 1)
        snaps[name] = load_snapshot(path)
    client = boto3.client(
        "bedrock-runtime",
        region_name=args.region,
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}),
    )

    jobs = [(v, p, i) for v in snaps for p in prompts for i in range(args.repeats)]

    def work(job: tuple) -> dict:
        v, p, i = job
        snap = snaps[v]
        applicable = any(e in snap["names"] for e in p["expect"])
        r = run_one(client, args.model, snap, p)
        return {
            "version": v,
            "id": p["id"],
            "repeat": i,
            "applicable": applicable,
            "correct": r["tool"] in p["expect"],
            "acceptable": r["tool"] in p["expect"] + p.get("ok", []),
            **r,
        }

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(work, jobs))

    out = {
        "model": args.model,
        "repeats": args.repeats,
        "toolsListChars": {v: s["toolsListChars"] for v, s in snaps.items()},
        "results": results,
    }
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"model={args.model} repeats={args.repeats} calls={len(results)}")  # noqa: T201 - CLI report
    print("tools/list chars: " + ", ".join(f"{v}={c}" for v, c in out["toolsListChars"].items()))  # noqa: T201 - CLI report
    print(summarize(results, prompts, list(snaps)))  # noqa: T201 - CLI report


if __name__ == "__main__":
    main()
