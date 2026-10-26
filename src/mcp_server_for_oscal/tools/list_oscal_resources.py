"""
Tool for listing OSCAL community resources.
"""

import logging
import re
from pathlib import Path

from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.tools.descriptions import Limit, Offset
from mcp_server_for_oscal.tools.oscal_store import ORIGIN_BUNDLED, OscalStore
from mcp_server_for_oscal.tools.utils import paginate, try_notify_client_error

logger = logging.getLogger(__name__)

# Module-level singleton — initialised lazily by ``init_store()``.
_store: OscalStore | None = None


def init_store(store: OscalStore) -> None:
    """Set the module-level OscalStore singleton.

    Called during server startup after the store has been
    initialised and directories have been scanned.
    """
    global _store  # noqa: PLW0603
    _store = store


DEFAULT_CATEGORY = "General"

# Client-facing error text; it names the failed operation and no path (Requirement 4.1).
LIST_RESOURCES_ERROR = "Failed to list OSCAL resources"

# Relative_Name of the awesome-oscal list under the bundled ``data/oscal_docs`` Scan_Root.
# Legacy rows get the same value from ``legacy_provenance`` (suffix after ``/data/oscal_docs/``).
AWESOME_OSCAL_RELATIVE_NAME = "awesome-oscal.md"

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_TOP_LEVEL_ITEM = re.compile(r"^[-*+]\s+")


def parse_resource_entries(markdown: str) -> list[dict[str, str]]:
    """Split awesome-oscal markdown into one entry per top-level list item.

    A heading of level 2-6 sets the category; entries before any such heading get
    "General". A line matching ``^[-*+]\\s+`` starts an entry. Following non-blank lines that
    are not headings or top-level items (indented continuations and sub-items) are joined to
    it with single spaces. A blank line ends the entry. Other prose is skipped.
    """
    entries: list[dict[str, str]] = []
    category = DEFAULT_CATEGORY
    parts: list[str] | None = None  # parts of the entry being collected, if any
    entry_category = category

    def flush() -> None:
        nonlocal parts
        if parts:
            entries.append({"category": entry_category, "text": " ".join(parts)})
        parts = None

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            if len(heading.group(1)) >= 2:  # noqa: PLR2004 - level 1 is the document title
                category = heading.group(2) or DEFAULT_CATEGORY
            continue
        item = _TOP_LEVEL_ITEM.match(line)
        if item:
            flush()
            entry_category = category
            parts = [line[item.end() :].strip()]
        elif parts is not None:
            parts.append(stripped)
        # else: prose outside any entry is skipped
    flush()
    return entries


@tool
def list_oscal_resources(offset: Offset = 0, limit: Limit = 10, ctx: Context | None = None) -> dict:
    """List OSCAL community resources (tools, content, articles, talks) from the awesome-oscal
    list, each with `category` and markdown `text`. Results are paged."""
    logger.debug(
        "list_oscal_resources(offset=%s, limit=%s) called with session client params: %s",
        offset,
        limit,
        ctx.session.client_params if ctx else None,
    )

    try:
        markdown = read_resources_file()
    except Exception as err:
        # Read errors (FileNotFoundError, OSError, UnicodeDecodeError) carry the file path in
        # their text, so the caller gets fixed text and the detail stays in the log
        # (Requirements 4.1, 4.6).
        logger.exception("Failed to read the OSCAL resources list")
        try_notify_client_error(LIST_RESOURCES_ERROR, ctx)
        raise RuntimeError(LIST_RESOURCES_ERROR) from err

    try:
        entries = parse_resource_entries(markdown)
        logger.debug("Successfully read OSCAL resources file: %d entries", len(entries))
        return paginate(entries, offset, limit)
    except Exception:
        # paginate's ValueError names only the offending argument, so it is re-raised as is.
        logger.exception(LIST_RESOURCES_ERROR)
        try_notify_client_error(LIST_RESOURCES_ERROR, ctx)
        raise


def _read_from_store() -> str | None:
    """Try to read awesome-oscal content from the bundled OscalStore DB.

    Selects by the recorded Origin and Relative_Name (Requirement 1.6), so a user file named
    ``awesome-oscal.md`` never shadows the bundled list. Returns the content string, or None
    if the store is not initialised or the document is not found.
    """
    if _store is None:
        return None

    try:
        row = _store._conn.execute(  # noqa: SLF001 - OscalStore has no public raw-query API
            "SELECT raw_json FROM documents "
            "WHERE model_type = 'documentation' AND origin = ? AND relative_name = ?",
            (ORIGIN_BUNDLED, AWESOME_OSCAL_RELATIVE_NAME),
        ).fetchone()
        if row and row["raw_json"]:
            return row["raw_json"]
    except Exception:
        logger.debug("Failed to read awesome-oscal from OscalStore", exc_info=True)

    return None


def read_resources_file() -> str:
    """
    Read the awesome-oscal.md content.

    First attempts to read from the bundled OscalStore database (runtime
    path). Falls back to reading the file from the ``oscal_docs/``
    directory relative to this package (development path).

    Returns:
        str: The complete content of the awesome-oscal.md file

    Raises:
        FileNotFoundError: If the awesome-oscal.md content cannot be found
        IOError: If there are issues reading the file
        UnicodeDecodeError: If there are encoding issues with the file
    """
    # Primary path: read from the bundled DB
    content = _read_from_store()
    if content is not None:
        logger.debug("Read OSCAL resources from bundled OscalStore DB")
        if not content.strip():
            logger.warning("OSCAL resources content from DB is empty")
        return content

    # Fallback: read from filesystem (development / build-time)
    current_file_dir = Path(__file__).parent
    # Try repo-root data/ directory first (post-move location)
    repo_root = current_file_dir.parent.parent.parent
    resources_file_path = repo_root / "data" / "oscal_docs" / "awesome-oscal.md"
    if not resources_file_path.exists():
        # Legacy path (pre-move, inside package)
        resources_file_path = current_file_dir.parent / "oscal_docs" / "awesome-oscal.md"

    logger.debug("Reading OSCAL resources from: %s", resources_file_path)

    try:
        # Read with explicit UTF-8 encoding and error handling
        with open(resources_file_path, encoding="utf-8", errors="strict") as file:
            content = file.read()

        if not content.strip():
            logger.warning("OSCAL resources file is empty")

        return content
    except FileNotFoundError:
        logger.error("OSCAL resources file not found at: %s", resources_file_path)
        raise
    except UnicodeDecodeError as e:
        logger.error("Encoding error reading file %s: %s", resources_file_path, e)
        # Try with different encoding as fallback
        try:
            with open(resources_file_path, encoding="latin-1", errors="replace") as file:
                content = file.read()
            logger.warning("Successfully read file with latin-1 encoding fallback")
            return content
        except Exception as fallback_error:
            logger.error("Fallback encoding also failed: %s", fallback_error)
            raise e from fallback_error  # Raise the original UnicodeDecodeError
    except OSError as e:
        logger.error("IO error reading file %s: %s", resources_file_path, e)
        raise
