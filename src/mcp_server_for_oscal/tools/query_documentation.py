"""
Tool for searching OSCAL documentation: a Bedrock Knowledge Base when configured, plus the
local documentation in the OscalStore.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Final, TypeVar

from boto3 import Session
from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.config import config
from mcp_server_for_oscal.tools.descriptions import (
    BudgetedLimit,
    LineCount,
    Offset,
    ParamDoc,
    StartLine,
)
from mcp_server_for_oscal.tools.metaschema_common import check_page
from mcp_server_for_oscal.tools.oscal_store import ORIGIN_USER, OscalStore
from mcp_server_for_oscal.tools.utils import (
    budget_page,
    check_line_range,
    lines_within_budget,
    safe_log_mcp,
    try_notify_client_error,
)

logger = logging.getLogger(__name__)

# D5: always request the Bedrock maximum so every page of a query sees the same KB_Hits.
KB_MAX_RESULTS: Final = 100
KB_FAILED_MESSAGE: Final = "Knowledge Base search failed; showing local documentation results."
UNKNOWN_DOC_ID_MESSAGE: Final = (
    "Unknown documentation ID. Use a docId from a query_oscal_documentation result."
)
STORE_NOT_INITIALIZED: Final = {"error": "OscalStore has not been initialized"}

# Free-text key of a Doc_Result_Item that budget_page() may shorten.
_TRUNCATABLE: Final = ("snippet",)

# Maximum length of a KB_Hit title taken from a passage heading.
KB_TITLE_MAX: Final = 200

# KB location types and the key holding their URI, URL, or ID, in lookup order (D6).
_KB_LOCATION_KEYS: Final = (
    ("s3Location", "uri"),
    ("webLocation", "url"),
    ("confluenceLocation", "url"),
    ("salesforceLocation", "url"),
    ("sharePointLocation", "url"),
    ("customDocumentLocation", "id"),
)

# A markdown ATX heading: up to 3 spaces, 1-6 '#', whitespace, text, optional closing '#'s.
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$", re.MULTILINE)
# An absolute POSIX or Windows path (drive letter or UNC).
_ABSOLUTE_PATH = re.compile(r"^(?:[/\\]|[A-Za-z]:[/\\])")

T = TypeVar("T")

# Module-level singleton — initialised lazily by ``init_store()``.
_store: OscalStore | None = None


def init_store(store: OscalStore) -> None:
    """Set the module-level OscalStore singleton.

    Called during server startup after the store has been
    initialised and directories have been scanned.
    """
    global _store  # noqa: PLW0603
    _store = store


def _get_store() -> OscalStore:
    """Return the module-level store, raising if not yet initialised."""
    if _store is None:
        raise RuntimeError(
            "OscalStore has not been initialised. Call init_store() during server startup."
        )
    return _store


@tool
def query_oscal_documentation(
    query: Annotated[str, ParamDoc("Question or search terms about OSCAL.")],
    offset: Offset = 0,
    limit: BudgetedLimit = 10,
    ctx: Context | None = None,
) -> dict:
    """Search OSCAL documentation (Knowledge Base if configured, plus local docs) and return
    paged passages. Use when a schema lacks the answer; for a model property try
    get_oscal_schema. Pass a hit's `docId` to get_oscal_documentation for the full page."""
    try:
        check_page(offset, limit)
    except ValueError as err:
        try_notify_client_error(str(err), ctx)
        raise

    if not isinstance(query, str) or not query.strip():
        return budget_page([], 0, offset)

    # A blank or whitespace-only KB ID means no Knowledge Base is configured (#14).
    if not config.knowledge_base_id.strip():
        logger.info("Using local documentation search path")
        return query_local(query, offset, limit)

    logger.info("Using Knowledge Base search path (KB ID: %s)", config.knowledge_base_id)
    try:
        kb = query_kb(query)
    except Exception:
        # ARNs, locations, and request details stay in the log record (Requirement 4.3).
        logger.warning("Knowledge Base query failed", exc_info=True)
        safe_log_mcp(KB_FAILED_MESSAGE, ctx, "warning")
        return query_local(query, offset, limit)

    local_items: list[dict] = []
    local_total = 0
    if _store is not None:
        local = _store.search_documentation(query, 0, offset + limit, origins=(ORIGIN_USER,))
        local_items, local_total = local["items"], local["total"]
    page = merge_page(kb, local_items, local_total, offset, limit)
    return budget_page(page["items"], page["total"], offset, truncatable=_TRUNCATABLE)


@tool
def get_oscal_documentation(
    doc_id: Annotated[str, ParamDoc("docId from a query_oscal_documentation result.")],
    start_line: StartLine = 1,
    line_count: LineCount = 200,
    ctx: Context | None = None,
) -> dict:
    """Read an OSCAL documentation page by `docId` as a verbatim line range. Use when a
    query_oscal_documentation snippet is not enough. A response may hold fewer lines to fit
    the response budget; continue from `end-line` + 1 while `has-more` is true."""
    store = _get_store()
    try:
        # Exact-match Doc_ID lookup before any text is read (12.4); an OSCAL Document_UUID
        # never matches (12.6). The rejected value is never echoed (4.5, 12.5).
        doc = store.get_documentation(doc_id) if isinstance(doc_id, str) else None
        if doc is None:
            raise ValueError(UNKNOWN_DOC_ID_MESSAGE)
        # Checks line_count (1-500) first, then start_line against the document (12.7).
        check_line_range(start_line, line_count, len(doc.lines), noun="document")
    except ValueError as err:
        try_notify_client_error(str(err), ctx)
        raise
    return lines_within_budget(
        {"docId": doc.doc_id, "title": doc.title}, doc.lines, start_line, line_count
    )


def query_local(query: str, offset: int, limit: int) -> dict:
    """Local_Mode: search documentation of both Origins and fit the page to the budget."""
    if _store is None:
        return dict(STORE_NOT_INITIALIZED)
    page = _store.search_documentation(query, offset, limit)
    return budget_page(page["items"], page["total"], offset, truncatable=_TRUNCATABLE)


def query_kb(query: str) -> list[dict]:
    """Return the Doc_Result_Items (``title``, ``snippet``) of a Knowledge Base search."""
    return kb_result_items(_retrieve(query))


def _retrieve(query: str) -> Mapping:
    """Call Bedrock ``retrieve`` for ``query`` with ``KB_MAX_RESULTS`` results."""
    aws_profile = config.aws_profile
    if aws_profile:
        logger.debug("Using AWS profile: %s", aws_profile)
        session = Session(profile_name=aws_profile)
    else:
        logger.debug("Using default AWS profile or environment credentials")
        session = Session()
    response = session.client("bedrock-agent-runtime").retrieve(
        knowledgeBaseId=config.knowledge_base_id,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": KB_MAX_RESULTS}},
    )
    results = response.get("retrievalResults") if isinstance(response, Mapping) else None
    logger.debug(
        "Knowledge Base returned %d results", len(results) if isinstance(results, list) else 0
    )
    return response


# ------------------------------------------------------------------
# Pure helpers (no boto3, no store)
# ------------------------------------------------------------------


def kb_result_items(response: object) -> list[dict]:
    """Return ``[{title, snippet}]`` for each ``retrievalResults`` entry with a string
    ``content.text``, in KB order.

    Reads only ``content.text`` and ``location`` (through ``kb_title``); never ``metadata``,
    ``score``, or ``ResponseMetadata`` (Requirement 3.4).
    """
    if not isinstance(response, Mapping):
        return []
    results = response.get("retrievalResults")
    if not isinstance(results, list):
        return []
    items: list[dict] = []
    for result in results:
        if not isinstance(result, Mapping):
            continue
        content = result.get("content")
        text = content.get("text") if isinstance(content, Mapping) else None
        if isinstance(text, str):
            items.append({"title": kb_title(result), "snippet": text})
    return items


def kb_title(result: Mapping) -> str:
    """Return a title for one KB_Hit that contains no Storage_Location (D6).

    The first markdown heading of ``content.text`` (at most ``KB_TITLE_MAX`` characters),
    else the humanized stem of the last path segment of the location URI, URL, or ID,
    else ``""``. A location gives a stem only when it is path-like: it contains a ``/`` or
    ``\\`` separator and the last segment is not the whole value (for a URI or URL, a segment
    after the host). An opaque ID, a bare bucket or host, and an ARN give no stem. A candidate
    that contains ``://``, starts with ``arn:``, or is an absolute path is rejected
    (Requirements 3.4, 10.4, 10.5).
    """
    content = result.get("content")
    text = content.get("text") if isinstance(content, Mapping) else None
    if isinstance(text, str):
        match = _HEADING.search(text)
        if match:
            heading = match.group(1).strip()[:KB_TITLE_MAX].strip()
            if _safe_title(heading):
                return heading
    stem = _location_stem(result.get("location"))
    return stem if _safe_title(stem) else ""


def _location_stem(location: object) -> str:
    """Humanized file stem of the first known location value, or ``""``."""
    if not isinstance(location, Mapping):
        return ""
    for loc_key, value_key in _KB_LOCATION_KEYS:
        loc = location.get(loc_key)
        value = loc.get(value_key) if isinstance(loc, Mapping) else None
        if isinstance(value, str) and value.strip():
            # An ARN names an AWS resource, not a document; no part of it is a title.
            if value.strip().casefold().startswith("arn:"):
                return ""
            return _humanize_stem(value)
    return ""


def _humanize_stem(value: str) -> str:
    """Last path segment of ``value`` without query, fragment, or extension; ``-``/``_`` → space.

    Returns ``""`` unless ``value`` is path-like: a URI or URL must have a path segment after
    its host (an ``s3://bucket/`` bucket is a storage location, not a document), and any other
    value must contain a ``/`` or ``\\`` separator before its last segment. So the stem is
    never the whole location value (D6).
    """
    path = value.strip().split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
    if "://" in path:
        # Drop the scheme and the host (bucket or authority); only the path may name a document.
        path = "/" + path.split("://", 1)[1].partition("/")[2]
    path = path.rstrip("/")
    if "/" not in path:
        # An opaque ID (no separator) is a whole location value, so it gives no title.
        return ""
    segment = path.rsplit("/", 1)[-1]
    stem = segment.rsplit(".", 1)[0] if "." in segment.lstrip(".") else segment
    return " ".join(re.sub(r"[-_]+", " ", stem).split())


def _safe_title(candidate: str) -> bool:
    """True if ``candidate`` is non-empty and does not look like a Storage_Location."""
    return bool(
        candidate
        and "://" not in candidate
        and not candidate.casefold().startswith("arn:")
        and not _ABSOLUTE_PATH.match(candidate)
    )


def interleave(a: Sequence[T], b: Sequence[T]) -> list[T]:
    """Return ``a[0], b[0], a[1], b[1], ...``, then the rest of the longer list (11.4)."""
    out: list[T] = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            out.append(a[i])
        if i < len(b):
            out.append(b[i])
    return out


def merge_page(
    kb: Sequence[dict],
    local_prefix: Sequence[dict],
    local_total: int,
    offset: int,
    limit: int,
) -> dict:
    """Return the page ``[offset, offset + limit)`` of ``interleave(kb, local)``.

    Requires ``len(local_prefix) >= min(local_total, offset + limit)``: the first ``n``
    items of an interleaving depend only on the first ``n`` items of each list, so the
    page is exact. ``total`` is ``len(kb) + local_total``.
    """
    end = offset + limit
    merged = interleave(kb[:end], local_prefix[:end])
    total = len(kb) + local_total
    return {
        "items": merged[offset:end],
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": end < total,
    }
