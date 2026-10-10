"""
Tools for querying OSCAL Component Definition documents.

All tools read from the module-level ``OscalStore`` set by ``init_store()``;
they raise ``RuntimeError`` until a store has been initialised.

OSCAL Component Definitions follow a hierarchy:
  Component Definition  →  Capability  →  Component

A Component Definition is the top-level document. It contains Capabilities
(groupings that describe higher-level security functions) and Components
(leaf-level items such as services, software, or regions). Queries in this
module prioritize Capabilities over Components to reflect that hierarchy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from mcp.server.mcpserver import Context  # noqa: TC002 - MCP resolves ctx annotations at runtime
from oscal_bindings.models import ComponentDefinition
from strands import tool

from mcp_server_for_oscal.tools.utils import (
    OSCALModelType,
    paginate,
    safe_log_mcp,
    try_notify_client_error,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from mcp_server_for_oscal.tools.oscal_store import OscalStore

logger = logging.getLogger(__name__)


# OscalStore singleton backing every Component Definition tool. Set by
# init_store(); the tools raise RuntimeError while it is None.
_oscal_store: OscalStore | None = None


def init_store(store: OscalStore) -> None:
    """Set the module-level OscalStore singleton.

    The Component Definition tool wrappers read exclusively from this
    store and raise ``RuntimeError`` until it is set.

    Args:
        store: An initialized OscalStore instance.
    """
    global _oscal_store  # noqa: PLW0603
    _oscal_store = store


# ------------------------------------------------------------------
# OscalStore access and scope helpers
# ------------------------------------------------------------------

# Page size used when iterating child elements; large enough that most
# lookups finish in a single query, small enough to bound memory per page.
_CHILD_PAGE = 500

# Maximum number of title hits inspected when resolving a CDef_Filter by
# title. The store's exact phase returns only NOCASE-equal titles, so this
# only matters when it falls back to fuzzy (FTS/LIKE) matching.
_SCOPE_TITLE_LIMIT = 50


def _require_store() -> OscalStore:
    """Return the initialised OscalStore singleton.

    Raises:
        RuntimeError: If ``init_store()`` has not been called.
    """
    if _oscal_store is None:
        msg = (
            "OscalStore is not initialised; call init_store() before using "
            "Component Definition tools"
        )
        raise RuntimeError(msg)
    return _oscal_store


@dataclass(frozen=True)
class _Scope:
    """Which Component Definitions a query searches.

    Attributes:
        cdef_uuid: UUID of the single Component Definition in scope, or
            ``None`` for every Component Definition in the store.
        searched: Value reported as ``component_definitions_searched``.
    """

    cdef_uuid: str | None
    searched: int


def _resolve_scope(
    store: OscalStore,
    ctx: Context | None,
    cdef_filter: str | None,
    total_cdefs: int,
) -> _Scope | None:
    """Resolve an optional CDef_Filter to a query scope.

    The filter matches a Component Definition UUID exactly, or its title
    case-insensitively. Fuzzy (FTS) title hits from ``store.query`` are
    rejected so a partial filter never silently selects a document.

    Returns:
        A ``_Scope``, or ``None`` when a filter was given and matched nothing.
    """
    if not cdef_filter:
        return _Scope(None, total_cdefs)

    hit = store.query(
        ctx=ctx,
        oscal_model_type=OSCALModelType.COMPONENT_DEFINITION,
        query_type="by_uuid",
        query_value=cdef_filter,
        offset=0,
        limit=1,
    )
    if hit["items"]:
        logger.info("Filtered to Component Definition with UUID: %s", cdef_filter)
        return _Scope(hit["items"][0]["uuid"], 1)

    hit = store.query(
        ctx=ctx,
        oscal_model_type=OSCALModelType.COMPONENT_DEFINITION,
        query_type="by_title",
        query_value=cdef_filter,
        offset=0,
        limit=_SCOPE_TITLE_LIMIT,
    )
    wanted = cdef_filter.lower()
    for item in hit["items"]:
        if str(item.get("title") or "").lower() == wanted:
            logger.info("Filtered to Component Definition with title: %s", cdef_filter)
            return _Scope(item["uuid"], 1)

    msg = f"No Component Definition found with UUID or title matching: `{cdef_filter}`."
    logger.debug(msg)
    safe_log_mcp(
        msg + " Try again without a filter or lookup the filter value with "
        "the tool list_component_definitions.",
        ctx,
        "info",
    )
    return None


def _iter_children(
    store: OscalStore,
    element_type: str,
    scope: _Scope,
    *,
    element_id: str | None = None,
    title: str | None = None,
    include_raw_json: bool = False,
) -> Iterator[dict]:
    """Yield every matching child element summary, paging the store.

    Items are yielded in ``list_child_elements`` order (title NOCASE, uuid).
    The generator is lazy, so callers that stop early issue one query.
    """
    offset = 0
    while True:
        page = store.list_child_elements(
            element_type=element_type,
            parent_doc_uuid=scope.cdef_uuid,
            offset=offset,
            limit=_CHILD_PAGE,
            element_id=element_id,
            title=title,
            include_raw_json=include_raw_json,
        )
        yield from page["items"]
        if not page["hasMore"]:
            return
        offset += _CHILD_PAGE


def _first(items: Iterable[dict]) -> list[dict]:
    """Return a list holding the first item of *items*, or an empty list."""
    for item in items:
        return [item]
    return []


def _raw(store: OscalStore, child: dict) -> dict:
    """Return the child's stored OSCAL JSON as a dict.

    The result uses OSCAL (hyphenated) keys with nulls omitted.
    Uses ``child["raw_json"]`` when present. When it is ``None`` (the store
    could not serialize the child at index time), the child is materialized
    from its parent Component Definition and serialized the same way the
    store does (``exclude_none=True, by_alias=True``), so keys match.
    Returns an empty dict if the child cannot be found.
    """
    raw_json = child.get("raw_json")
    if raw_json is not None:
        loaded = json.loads(raw_json)
        return loaded if isinstance(loaded, dict) else {}

    model = store.get_parsed_model_by_uuid(child["parentDocumentUuid"])
    if not isinstance(model, ComponentDefinition):
        return {}
    pool: list[Any] = list(
        (model.capabilities if child.get("element_type") == "capability" else model.components)
        or []
    )
    for obj in pool:
        if str(obj.uuid) == child["id"]:
            loaded = json.loads(obj.model_dump_json(exclude_none=True, by_alias=True))
            return loaded if isinstance(loaded, dict) else {}
    logger.warning("Child %s missing from parent %s", child["id"], child["parentDocumentUuid"])
    return {}


def _component_type(raw: dict) -> str:
    """Return the component ``type`` from its raw OSCAL dict."""
    return str(raw.get("type", ""))


def _has_prop_value(raw: dict, value: str) -> bool:
    """Return True if any property in the raw OSCAL dict has *value*."""
    return any(isinstance(p, dict) and p.get("value") == value for p in raw.get("props") or [])


def _select_component_candidates(
    store: OscalStore,
    scope: _Scope,
    query_type: str,
    value: str | None,
) -> list[dict]:
    """Return child summaries of the components matching a query.

    ``by_uuid`` and ``by_title`` return at most one candidate. ``by_title``
    tries an exact, case-insensitive title match first, then falls back to
    the first component (in ``list_child_elements`` order) with a property
    whose value equals *value*. ``by_type`` is an exact string match on the
    component ``type``.

    Raises:
        ValueError: If *query_type* is not a supported query type.
    """
    if query_type == "all":
        return list(_iter_children(store, "component", scope, include_raw_json=True))
    if query_type == "by_uuid":
        return _first(
            _iter_children(store, "component", scope, element_id=value, include_raw_json=True)
        )
    if query_type == "by_title":
        hit = _first(_iter_children(store, "component", scope, title=value, include_raw_json=True))
        if hit or value is None:
            return hit
        return _first(
            c
            for c in _iter_children(store, "component", scope, include_raw_json=True)
            if _has_prop_value(_raw(store, c), value)
        )
    if query_type == "by_type":
        return [
            c
            for c in _iter_children(store, "component", scope, include_raw_json=True)
            if _component_type(_raw(store, c)) == value
        ]
    msg = f"Invalid query_type: {query_type}"
    raise ValueError(msg)


def _materialize_components(store: OscalStore, page: list[dict]) -> list[dict]:
    """Return the stored OSCAL JSON of each component candidate.

    Candidates whose JSON cannot be read (``_raw`` returns ``{}``) are
    skipped; ``_raw`` logs a warning for them.

    Returns:
        One OSCAL component dict (hyphenated keys, nulls omitted) per found
        candidate, in candidate order.
    """
    return [d for c in page if (d := _raw(store, c))]


def _find_capability(
    store: OscalStore,
    scope: _Scope,
    query_type: str,
    value: str,
) -> dict | None:
    """Find a capability by UUID (``by_uuid``) or exact title (otherwise).

    Searches every capability in *scope* (no result cap) and returns the
    first hit's stored OSCAL JSON (hyphenated keys, nulls omitted).

    Returns:
        The capability dict, or ``None`` if nothing matched or its JSON
        could not be read.
    """
    if query_type == "by_uuid":
        it = _iter_children(store, "capability", scope, element_id=value, include_raw_json=True)
    else:
        it = _iter_children(store, "capability", scope, title=value, include_raw_json=True)
    hit = _first(it)
    if not hit:
        return None
    return _raw(store, hit[0]) or None


def _query_component_definition(
    store: OscalStore,
    ctx: Context | None,
    component_definition_filter: str | None,
    query_type: str,
    query_value: str | None,
    offset: int = 0,
    limit: int = 10,
) -> dict[str, Any]:
    """Query Capabilities and Components on the OscalStore.

    For ``by_uuid`` and ``by_title`` a Capability in scope is returned first
    (Capability_Query_Response). Otherwise matching Components are paginated
    and only the page is materialized (Component_Query_Response).

    Raises:
        ValueError: If ``query_value`` is required and empty, the store has no
            Component Definitions, ``offset``/``limit`` are invalid, or
            ``query_type`` is unsupported.
    """
    if query_value:
        query_value = query_value.strip()

    if query_type in ("by_uuid", "by_title", "by_type") and not query_value:
        msg = f"query_value is required when query_type is '{query_type}'"
        try_notify_client_error(msg, ctx)
        raise ValueError(msg)

    total_cdefs = store.list_documents(
        ctx=ctx,
        oscal_model_type=OSCALModelType.COMPONENT_DEFINITION,
        offset=0,
        limit=1,
    )["total"]
    if total_cdefs == 0:
        msg = "No Component Definitions loaded"
        logger.warning(msg)
        try_notify_client_error(msg, ctx)
        raise ValueError(msg)

    # Validate offset/limit before any scans; raises ValueError like before.
    empty_page = paginate([], offset, limit)

    scope = _resolve_scope(store, ctx, component_definition_filter, total_cdefs)
    if scope is None:
        return {
            "components": [],
            "total_count": 0,
            "offset": empty_page["offset"],
            "limit": empty_page["limit"],
            "hasMore": empty_page["hasMore"],
            "query_type": query_type,
            "component_definitions_searched": 0,
            "filtered_by": component_definition_filter,
        }

    if query_type in ("by_uuid", "by_title") and query_value:
        try:
            cap = _find_capability(store, scope, query_type, query_value)
            if cap is not None:
                logger.debug("Returning capability %s", cap.get("uuid"))
                return {
                    "capability": cap,
                    "component_count": len(cap.get("incorporates-components", [])),
                    "offset": 0,
                    "limit": 1,
                    "total": 1,
                    "hasMore": False,
                    "query_type": query_type,
                    "component_definitions_searched": scope.searched,
                    "filtered_by": component_definition_filter,
                }
        except Exception:
            logger.exception("Failure while searching capabilities")

    candidates = _select_component_candidates(store, scope, query_type, query_value)
    page = paginate(candidates, offset, limit)
    return {
        "components": _materialize_components(store, page["items"]),
        "total_count": page["total"],
        "offset": page["offset"],
        "limit": page["limit"],
        "hasMore": page["hasMore"],
        "query_type": query_type,
        "component_definitions_searched": scope.searched,
        "filtered_by": component_definition_filter,
    }


def _child_summary(item: dict, name_key: str) -> dict:
    """Map a ``list_child_elements`` item to a list-tool summary item.

    ``sizeInBytes`` is the UTF-8 length of the child's ``raw_json``, or 0
    when the store could not serialize the child at index time.
    """
    raw_json = item.get("raw_json")
    return {
        "uuid": item["id"],
        name_key: item["title"],
        "parentComponentDefinitionTitle": item.get("parentDocumentTitle", ""),
        "parentComponentDefinitionUuid": item.get("parentDocumentUuid", ""),
        "sizeInBytes": len(raw_json.encode("utf-8")) if raw_json is not None else 0,
    }


def _child_page(
    store: OscalStore,
    ctx: Context | None,
    element_type: str,
    name_key: str,
    offset: int,
    limit: int,
) -> dict:
    """Return a Page_Response of child summaries of *element_type*."""
    result = store.list_child_elements(
        ctx=ctx,
        element_type=element_type,
        offset=offset,
        limit=limit,
        include_raw_json=True,
    )
    return {
        "items": [_child_summary(item, name_key) for item in result["items"]],
        "total": result["total"],
        "offset": result["offset"],
        "limit": result["limit"],
        "hasMore": result["hasMore"],
    }


def _list_components(
    store: OscalStore,
    ctx: Context | None,
    offset: int,
    limit: int,
) -> dict:
    """Return a Page_Response of Component summaries.

    Raises:
        RuntimeError: If the store contains no Components.
    """
    page = _child_page(store, ctx, "component", "title", offset, limit)
    if page["total"] == 0:
        msg = "No Components loaded"
        try_notify_client_error(msg, ctx)
        raise RuntimeError(msg)
    return page


def _list_capabilities(
    store: OscalStore,
    ctx: Context | None,
    offset: int,
    limit: int,
) -> dict:
    """Return a Page_Response of Capability summaries.

    Capabilities are optional in OSCAL, so an empty store yields an empty
    page rather than an error.
    """
    return _child_page(store, ctx, "capability", "name", offset, limit)


def _get_capability(store: OscalStore, uuid: str) -> dict | None:
    """Return the stored OSCAL JSON of the Capability with *uuid*, or ``None``.

    Searches every Capability in the store (no position cap). The dict uses
    OSCAL (hyphenated) keys with nulls omitted.
    """
    if not uuid:
        return None
    return _find_capability(store, _Scope(None, 0), "by_uuid", uuid)


def _list_component_definitions(
    store: OscalStore,
    ctx: Context | None,
    offset: int,
    limit: int,
) -> dict:
    """Return a Page_Response of Component Definition summaries.

    Raises:
        RuntimeError: If the store contains no Component Definitions.
    """
    result = store.list_documents(
        ctx=ctx,
        oscal_model_type=OSCALModelType.COMPONENT_DEFINITION,
        offset=offset,
        limit=limit,
    )
    if result["total"] == 0:
        msg = "No Component Definitions loaded"
        try_notify_client_error(msg, ctx)
        raise RuntimeError(msg)

    items = [
        {
            "uuid": item["uuid"],
            "title": item["title"],
            "componentCount": item.get("childCount", 0),
            "importedComponentDefinitionsCount": 0,
            "sizeInBytes": item.get("sizeInBytes", 0),
        }
        for item in result["items"]
    ]
    return {
        "items": items,
        "total": result["total"],
        "offset": result["offset"],
        "limit": result["limit"],
        "hasMore": result["hasMore"],
    }


# ------------------------------------------------------------------
# MCP tool wrappers (thin delegates to the singleton store)
# ------------------------------------------------------------------


@tool()
def query_component_definition(
    ctx: Context | None = None,
    component_definition_filter: str | None = None,
    query_type: Literal["all", "by_uuid", "by_title", "by_type"] = "all",
    query_value: str | None = None,
    return_format: Literal["raw"] = "raw",  # noqa: ARG001 - public API, single value
    offset: int = 0,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Query OSCAL Component Definition documents to find Capabilities and Components.

    OSCAL Component Definitions follow a hierarchy: a Component Definition contains
    Capabilities and Components. A Capability groups related Components and describes
    a higher-level security function. This tool reflects that hierarchy — when a
    query matches a Capability (by title or UUID), the Capability is returned
    directly, including its list of incorporated Components. Only when no matching
    Capability is found does the search fall through to individual Components.

    Prefer querying by Capability name/UUID when exploring what a Component
    Definition offers. Query by Component only when you need details about a
    specific service, software, region, or similar leaf-level element.

    Use the companion tools to discover valid query filters:
      - list_capabilities()  — lists all Capability UUIDs and names
      - list_components()    — lists all Component UUIDs and titles
      - list_component_definitions() — lists all Component Definition UUIDs and titles

    If you need details about the Component Definition schema, use the tool get_oscal_schema.

    Args:
        ctx: MCP server context (injected automatically by MCP server)
        component_definition_filter: Optional UUID or metadata.title of a Component
            Definition to narrow the search scope. Case-insensitive for titles.
            If omitted, all loaded Component Definitions are searched.
        query_type: Type of query to perform:
            - "all": Return all components in the definition(s). Intended for use
              with a component_definition_filter. Results may be large. For a
              lightweight summary, use list_components() instead.
            - "by_uuid": Find a Capability or Component by UUID (requires query_value).
              Capabilities are checked first.
            - "by_title": Find a Capability by name or a Component by title
              (requires query_value). Capabilities are checked first; if no
              Capability matches, Components are searched with a fallback to
              property-value matching.
            - "by_type": Filter Components by type (requires query_value).
              Does not apply to Capabilities.
        query_value: The value to search for. Required for by_uuid, by_title,
            and by_type queries.
        return_format: Response format. Currently only "raw" is supported, returning
            complete OSCAL objects as OSCAL JSON (hyphenated keys, nulls omitted).
        offset: Zero-based pagination offset (default 0).
        limit: Maximum items to return, 1-100 (default 10).

    Returns:
        dict: When a Capability matches, the response contains:
            - capability: Full OSCAL Capability object as OSCAL JSON
            - component_count: Number of entries in the Capability's
              ``incorporates-components`` list (0 if absent)
            - offset, limit, total, hasMore: Pagination metadata
              (always 0, 1, 1, False for single-capability results)
            - query_type, component_definitions_searched, filtered_by

        When Components are returned instead, the response contains:
            - components: Paginated list of OSCAL Component objects as OSCAL JSON
            - total_count: Total number of matching Components across all pages
            - offset, limit, hasMore: Pagination metadata
            - query_type, component_definitions_searched, filtered_by

    Raises:
        ValueError: If required query parameters are missing or no data is loaded.
    """
    store = _require_store()
    # return_format has a single supported value ("raw"); it is kept in the
    # signature for API compatibility and is not passed to the helper.
    return _query_component_definition(
        store,
        ctx,
        component_definition_filter,
        query_type,
        query_value,
        offset,
        limit,
    )


@tool()
def list_component_definitions(
    ctx: Context | None = None, offset: int = 0, limit: int = 10
) -> dict:
    """List loaded Component Definitions with summary metadata.

    A Component Definition is the top-level OSCAL document that contains
    Capabilities and Components. Use this tool to discover available
    definitions and obtain UUIDs or titles for use as the
    component_definition_filter in query_component_definition().

    Args:
        ctx: MCP server context (injected automatically by MCP server)
        offset: Zero-based index of the first item to return (default 0).
        limit: Maximum number of items to return, 1-100 (default 10).

    Returns:
        dict: Page_Response with keys ``items``, ``total``, ``offset``,
            ``limit``, ``hasMore``. Each item in ``items`` has keys:
            uuid, title, componentCount, importedComponentDefinitionsCount,
            sizeInBytes.
    """
    store = _require_store()
    return _list_component_definitions(store, ctx, offset, limit)


@tool()
def list_components(ctx: Context | None = None, offset: int = 0, limit: int = 10) -> dict:
    """List loaded Components with summary metadata.

    Components are leaf-level elements within a Component Definition that
    represent individual services, software, regions, or similar items.
    A Component may belong to one or more Capabilities. Use this tool to
    discover Component UUIDs and titles for targeted queries via
    query_component_definition().

    Args:
        ctx: MCP server context (injected automatically by MCP server)
        offset: Zero-based index of the first item to return (default 0).
        limit: Maximum number of items to return, 1-100 (default 10).

    Returns:
        dict: Page_Response with keys ``items``, ``total``, ``offset``,
            ``limit``, ``hasMore``. Each item in ``items`` has keys:
            uuid, title, parentComponentDefinitionTitle,
            parentComponentDefinitionUuid, sizeInBytes.
    """
    store = _require_store()
    return _list_components(store, ctx, offset, limit)


@tool()
def list_capabilities(ctx: Context | None = None, offset: int = 0, limit: int = 10) -> dict:
    """List loaded Capabilities with summary metadata.

    Capabilities sit above Components but are optional in the OSCAL hierarchy.
    Each Capability groups related Components and describes a collection
    or higher-level offering. Start here when exploring what a Component
    Definition provides — then drill into individual Components as needed.

    Use the returned UUIDs or names as query_value in
    query_component_definition() to retrieve full Capability details.

    Args:
        ctx: MCP server context (injected automatically by MCP server)
        offset: Zero-based index of the first item to return (default 0).
        limit: Maximum number of items to return, 1-100 (default 10).

    Returns:
        dict: Page_Response with keys ``items``, ``total``, ``offset``,
            ``limit``, ``hasMore``. Each item in ``items`` has keys:
            uuid, name, parentComponentDefinitionTitle,
            parentComponentDefinitionUuid, sizeInBytes.
    """
    store = _require_store()
    return _list_capabilities(store, ctx, offset, limit)


@tool()
def get_capability(
    ctx: Context | None = None,  # noqa: ARG001 - injected by MCP; kept for API
    uuid: str = "",
) -> dict | None:
    """Retrieve a single Capability by UUID, returning its full OSCAL representation.

    A Capability groups related Components and may include control
    implementations, description, and a list of incorporated Components.
    Use list_capabilities() to discover available UUIDs.

    Args:
        ctx: MCP server context (injected automatically by MCP server)
        uuid: UUID of the Capability to retrieve.

    Returns:
        dict | None: Full OSCAL Capability object as OSCAL JSON (hyphenated
            keys, nulls omitted), or None if the UUID is not found.
    """
    store = _require_store()
    return _get_capability(store, uuid)
