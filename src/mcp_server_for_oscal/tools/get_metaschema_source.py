"""
Tool for reading a bounded line range of a bundled OSCAL Metaschema source file.

The raw fallback for the other Metaschema tools (Requirement 11, design OQ6): callers use it
to check a ``source-file``/``line`` citation or read context the query tools do not
summarize. ``file`` is looked up by exact match in ``MetaschemaIndex.source_files()`` (the
manifest allowlist) before anything is read, and lines come only from
``MetaschemaIndex.source_lines()``; this module never opens a file (Requirement 7.1).
"""

import json
import logging
from typing import Annotated

from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.tools.descriptions import ParamDoc
from mcp_server_for_oscal.tools.metaschema_common import shown
from mcp_server_for_oscal.tools.metaschema_index import get_index
from mcp_server_for_oscal.tools.utils import (
    RESPONSE_BUDGET,
    TRUNCATION_MARK,
    try_notify_client_error,
)

logger = logging.getLogger(__name__)

MAX_LINE_COUNT = 500


@tool()
def get_metaschema_source(
    file: Annotated[
        str | None,
        ParamDoc(
            'Bundle-relative path exactly as listed, e.g. "oscal_component_metaschema.xml". '
            "Omit to list the files."
        ),
    ] = None,
    start_line: Annotated[
        int, ParamDoc("1-based first line to return. Ignored when file is omitted.")
    ] = 1,
    line_count: Annotated[
        int, ParamDoc("Maximum lines to return, 1-500. Ignored when file is omitted.")
    ] = 200,
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

        # Exact-match allowlist lookup and range checks before any read (11.3, 11.7).
        _check_file(file, files)
        _check_line_count(line_count)
        lines = index.source_lines(file)
        _check_start_line(start_line, len(lines))
    except ValueError as err:  # includes MetaschemaLoadError, whose message is client-safe
        try_notify_client_error(str(err), ctx)
        raise

    return _within_budget(file, lines, start_line, line_count)


# --------------------------------------------------------------------------------------
# Argument checks (Requirements 11.3, 11.6, 11.7)
# --------------------------------------------------------------------------------------


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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


def _check_line_count(line_count: object) -> None:
    if not _is_int(line_count) or not 1 <= line_count <= MAX_LINE_COUNT:  # type: ignore[operator]
        msg = f"Invalid line_count: {shown(repr(line_count))}. Valid range: 1 to {MAX_LINE_COUNT}"
        raise ValueError(msg)


def _check_start_line(start_line: object, total: int) -> None:
    if not _is_int(start_line) or not 1 <= start_line <= total:  # type: ignore[operator]
        msg = (
            f"Invalid start_line: {shown(repr(start_line))}. Valid range for this file: "
            f"1 to {total}"
        )
        raise ValueError(msg)


# --------------------------------------------------------------------------------------
# Response budget (Requirements 11.4, 12.3)
# --------------------------------------------------------------------------------------


def _response(file: str, lines: tuple[str, ...], start: int, count: int) -> dict:
    end = start + count - 1
    return {
        "file": file,
        "start-line": start,
        "end-line": end,
        "total-lines": len(lines),
        "has-more": end < len(lines),
        "text": "".join(lines[start - 1 : end]),
    }


def _size(response: dict) -> int:
    # Same measure as utils.paginate_within_budget.
    return len(json.dumps(response))


def _within_budget(file: str, lines: tuple[str, ...], start: int, line_count: int) -> dict:
    """The largest whole-line range from ``start`` that fits ``RESPONSE_BUDGET`` (11.4).

    At least one line is always returned so that paging progresses. If that one line alone
    is over budget (no bundled line comes close), its text is shortened at a code-point
    boundary, marked with ``TRUNCATION_MARK``, and the response gets ``truncated: true``.
    """
    count = min(line_count, len(lines) - start + 1)
    response = _response(file, lines, start, count)
    if _size(response) <= RESPONSE_BUDGET:
        return response

    # Binary search the largest k in [1, count) whose response fits.
    lo, hi = 1, count - 1
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if _size(_response(file, lines, start, mid)) <= RESPONSE_BUDGET:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best:
        return _response(file, lines, start, best)

    response = _response(file, lines, start, 1)
    response["truncated"] = True
    full = response["text"]
    lo, hi = 0, len(full) - 1
    best_len = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        response["text"] = full[:mid] + TRUNCATION_MARK
        if _size(response) <= RESPONSE_BUDGET:
            best_len, lo = mid, mid + 1
        else:
            hi = mid - 1
    response["text"] = full[:best_len] + TRUNCATION_MARK
    return response
