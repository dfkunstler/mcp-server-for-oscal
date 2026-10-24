#!/usr/bin/env python3
"""
Export the live MCP ``tools/list`` and server ``instructions`` to JSON.

Used by ``bin/eval_tool_selection.py`` to compare tool selection across versions. To export
an older checkout, put its ``src`` first on ``PYTHONPATH`` so that checkout's package is
imported instead of the installed one:

    PYTHONPATH=../other-checkout/src hatch run python bin/export_tools_list.py out.json

Works with servers that have ``main.build_server()`` and with older ones that only have the
module-level ``main.mcp`` and a no-argument ``_setup_tools()``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


def export() -> dict:
    from mcp_server_for_oscal import main

    if hasattr(main, "build_server"):
        server = main.build_server()
        main._setup_tools(server)
    else:
        server = main.mcp
        main._setup_tools()
    tools = asyncio.run(server.list_tools())
    dumped = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]
    return {
        "source": str(Path(main.__file__).resolve().parent),
        "instructions": server.instructions or "",
        "tools": dumped,
        "toolsListChars": len(json.dumps(dumped, separators=(",", ":"), ensure_ascii=False)),
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:  # noqa: PLR2004 - one output path
        raise SystemExit("usage: export_tools_list.py OUTPUT.json")
    data = export()
    Path(sys.argv[1]).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"{data['source']}: {len(data['tools'])} tools, {data['toolsListChars']} chars")  # noqa: T201 - CLI report
