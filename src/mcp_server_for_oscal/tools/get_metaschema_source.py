"""
Tool for reading a bounded line range of a bundled OSCAL Metaschema source file.

The raw fallback for the other Metaschema tools (Requirement 11, design OQ6): callers use it
to check a ``source-file``/``line`` citation or read context the query tools do not
summarize. ``file`` is looked up by exact match in ``MetaschemaIndex.source_files()`` (the
manifest allowlist) before anything is read, and lines come only from
``MetaschemaIndex.source_lines()``; this module never opens a file (Requirement 7.1).
"""

import logging
from typing import Annotated

from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.tools.descriptions import LineCount, ParamDoc, StartLine
from mcp_server_for_oscal.tools.metaschema_index import get_index
from mcp_server_for_oscal.tools.utils import (
    MAX_LINE_COUNT,  # noqa: F401 - re-exported for tests
    check_line_count,
    check_line_range,
    lines_within_budget,
    try_notify_client_error,
)

logger = logging.getLogger(__name__)


@tool()
def get_metaschema_source(
    file: Annotated[
        str | None,
        ParamDoc(
            'Bundle-relative path exactly as listed, e.g. "oscal_component_metaschema.xml". '
            "Omit to list the files."
        ),
    ] = None,
    start_line: StartLine = 1,
    line_count: LineCount = 200,
    ctx: Context | None = None,
) -> dict:
    """Read a line range of a bundled OSCAL Metaschema source file verbatim, or list the files
    with line counts when `file` is omitted. Use when checking a `source-file` and `line` from
    get_definition, get_allowed_values, get_constraints, or find_references_to. A response may
    hold fewer lines to fit the response budget; continue from `end-line` + 1 while
    `has-more` is true."""
    logger.debug(
        "get_metaschema_source(file: %s, start_line: %s, line_count: %s)",
        file,
        start_line,
        line_count,
    )
    try:
        index = get_index()
        files = index.source_files()
        if file is None:
            return {"files": [{"file": f, "line-count": len(index.source_lines(f))} for f in files]}

        # Exact-match allowlist lookup and line_count check before any read (11.3, 11.7).
        _check_file(file, files)
        check_line_count(line_count)
        lines = index.source_lines(file)
        check_line_range(start_line, line_count, len(lines), noun="file")
    except ValueError as err:  # includes MetaschemaLoadError, whose message is client-safe
        try_notify_client_error(str(err), ctx)
        raise

    return _within_budget(file, lines, start_line, line_count)


# --------------------------------------------------------------------------------------
# Argument checks (Requirements 11.3, 11.6)
# --------------------------------------------------------------------------------------


def _check_file(file: object, files: tuple[str, ...]) -> None:
    """Raise ``ValueError`` listing the available files unless ``file`` is one exactly."""
    if isinstance(file, str) and file in files:
        return
    # The rejected value is not echoed: it may be an absolute path, which no client-facing
    # message may contain (Requirement 4.11, RC-13).
    available = ", ".join(files)
    msg = (
        "Unknown OSCAL Metaschema source file. Use a bundle-relative path exactly as listed. "
        f"Available files: {available}"
    )
    raise ValueError(msg)


def _within_budget(file: str, lines: tuple[str, ...], start: int, line_count: int) -> dict:
    """The largest whole-line range from ``start`` that fits the response budget (11.4)."""
    return lines_within_budget({"file": file}, lines, start, line_count)
