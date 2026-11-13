"""
Tools for querying OSCAL Component Definition documents.

``query_component_definition`` and ``get_capability`` read from the
module-level ``OscalStore`` set by ``init_store()``; they raise
``RuntimeError`` until a store has been initialised. Listing Component
Definitions, Components, and Capabilities is done by the generic tools
``list_documents`` and ``list_child_elements`` in ``query_oscal_models``.

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
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.server.mcpserver import Context  # noqa: TC002 - MCP resolves ctx annotations at runtime
from oscal_bindings.models import ComponentDefinition
from strands import tool

# Runtime imports: the MCP SDK and Strands resolve these annotations at runtime.
from mcp_server_for_oscal.tools.descriptions import Limit, Offset, ParamDoc
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
        msg + " Try again without a filter or look up the filter value with "
        "the tool list_documents (model_type component-definition).",
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


def _get_capability(store: OscalStore, uuid: str) -> dict | None:
    """Return the stored OSCAL JSON of the Capability with *uuid*, or ``None``.

    Searches every Capability in the store (no position cap). The dict uses
    OSCAL (hyphenated) keys with nulls omitted.
    """
    if not uuid:
        return None
    return _find_capability(store, _Scope(None, 0), "by_uuid", uuid)


# ------------------------------------------------------------------
# MCP tool wrappers (thin delegates to the singleton store)
# ------------------------------------------------------------------


@tool()
def query_component_definition(
    component_definition_filter: Annotated[
        str | None,
        ParamDoc(
            "UUID or exact title (case-insensitive) of one Component Definition to search. "
            "Omit to search all."
        ),
    ] = None,
    query_type: Annotated[
        Literal["all", "by_uuid", "by_title", "by_type"],
        ParamDoc(
            '"all" (paged, large without a filter), "by_uuid", "by_title" (title, then '
            'property value), or "by_type" (Component type). by_uuid and by_title return a '
            "matching Capability first."
        ),
    ] = "all",
    query_value: Annotated[
        str | None,
        ParamDoc('UUID, title, or Component type to match. Required unless query_type is "all".'),
    ] = None,
    return_format: Annotated[  # noqa: ARG001 - public API, single value
        Literal["raw"], ParamDoc('Response format. Only "raw" (OSCAL JSON) is supported.')
    ] = "raw",
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Return OSCAL Components from loaded Component Definitions as OSCAL JSON, or one
    Capability when a by_uuid or by_title query matches one. Use when you need component
    details: call it directly with by_title or by_type when you know the name or type, else
    look them up with list_child_elements (component or capability) or list_documents
    (component-definition), and fetch a Capability with get_capability. Results are paged.
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
def get_capability(
    uuid: Annotated[
        str,
        ParamDoc("UUID of the Capability, from list_child_elements with element_type capability."),
    ],
    ctx: Context | None = None,  # noqa: ARG001 - injected by MCP; kept for API
) -> dict | None:
    """Get one Capability by UUID as OSCAL JSON, or null when no Capability has that UUID. Use
    when working with Component Definitions that contain capabilities.
    """
    store = _require_store()
    return _get_capability(store, uuid)
