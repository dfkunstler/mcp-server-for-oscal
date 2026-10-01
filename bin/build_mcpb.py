#!/usr/bin/env python3
"""
Build an MCP Bundle (.mcpb) for the OSCAL MCP server.

Stages the bundle template in ``conf/mcpb`` together with the wheel built by
``hatch build``, fills in the version, wheel path, and tool list, locks
dependencies with uv, then validates and packs the bundle with the mcpb CLI.

The bundle uses the MCPB ``uv`` server type: the host application installs
Python and the dependencies listed in the bundle's pyproject.toml/uv.lock, so
no platform-specific packages are shipped inside the bundle.

Usage:
    hatch build && hatch run build-mcpb

Requires ``uv`` and ``npx`` (Node.js) on PATH. The resulting
``build/mcp-server-for-oscal-<version>.mcpb`` is written next to the wheel.
"""

from __future__ import annotations

import inspect
import json
import logging
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "conf" / "mcpb"
BUILD_DIR = REPO_ROOT / "build"
STAGING_DIR = BUILD_DIR / "mcpb"
MCPB_CLI = "@anthropic-ai/mcpb@2"


def find_wheel() -> Path:
    """Return the most recent wheel produced by ``hatch build``."""
    wheels = sorted(BUILD_DIR.glob("mcp_server_for_oscal-*.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        msg = f"No wheel found in {BUILD_DIR}; run `hatch build` first."
        raise SystemExit(msg)
    return wheels[-1]


def tool_list() -> list[dict[str, str]]:
    """Describe the tools registered by the server, for manifest.json."""
    from mcp_server_for_oscal.tools import get_tool_list

    tools = []
    for fn in get_tool_list():
        doc = inspect.getdoc(fn) or ""
        tools.append({"name": fn.__name__, "description": doc.split("\n\n")[0].strip()})
    tools.append({"name": "about", "description": "Get metadata about the server itself"})
    return tools


def run(*cmd: str, cwd: Path) -> None:
    logger.info("Running: %s", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)  # nosec B603


def main() -> None:
    wheel = find_wheel()
    # Wheel filenames are <name>-<version>-<python>-<abi>-<platform>.whl
    version = wheel.name.split("-")[1]
    logger.info("Building MCPB bundle for version %s from %s", version, wheel.name)

    npx = shutil.which("npx")
    uv = shutil.which("uv")
    if not npx or not uv:
        raise SystemExit("Both `npx` (Node.js) and `uv` are required on PATH.")

    shutil.rmtree(STAGING_DIR, ignore_errors=True)
    shutil.copytree(TEMPLATE_DIR, STAGING_DIR)
    (STAGING_DIR / "wheels").mkdir()
    shutil.copy2(wheel, STAGING_DIR / "wheels" / wheel.name)
    shutil.copy2(REPO_ROOT / "LICENSE", STAGING_DIR / "LICENSE")

    manifest_path = STAGING_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = version
    manifest["tools"] = tool_list()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    pyproject_path = STAGING_DIR / "pyproject.toml"
    pyproject = pyproject_path.read_text()
    pyproject = pyproject.replace('version = "0.0.0"', f'version = "{version}"')
    pyproject = pyproject.replace("WHEEL_FILENAME", wheel.name)
    pyproject_path.write_text(pyproject)

    # Pin all transitive dependencies so every install of this bundle resolves
    # the same versions; the bundle runs with `uv run --frozen`.
    run(uv, "lock", cwd=STAGING_DIR)

    output = BUILD_DIR / f"mcp-server-for-oscal-{version}.mcpb"
    run(npx, "-y", MCPB_CLI, "validate", str(manifest_path), cwd=STAGING_DIR)
    run(npx, "-y", MCPB_CLI, "pack", str(STAGING_DIR), str(output), cwd=BUILD_DIR)
    logger.info("Wrote %s", output)


if __name__ == "__main__":
    sys.exit(main())
