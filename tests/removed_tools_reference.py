"""Frozen reference for the 29 tools removed by consolidate-tools (#40).

Each function holds the logic of a removed tool, copied from commit 599a41d
(``tools/query_oscal_models.py`` and ``tools/query_component_definition.py``)
with only the names changed. It is the test oracle for the Generic_Tools after
the removed tools are deleted.

The module imports nothing from the removed tools. Do not edit the bodies to
follow later changes to the store or the Generic_Tools: a change here would
hide a behavior change.

Per-model tools (26) made one store call each:

- ``old_query``: ``query_<model>`` (7 tools), was ``_query`` in ``query_oscal_models``.
- ``old_list_documents``: ``list_<model>s`` (6 tools), was ``_list_documents``.
- ``old_list_children``: the 12 per-model child listers, was ``_list_children``.

Component Definition tools (3) were helpers in ``query_component_definition``:

- ``old_list_component_definitions``, ``old_list_components``, ``old_list_capabilities``,
  with ``old_child_page`` and ``old_child_summary`` (shared helpers).

``old_child_summary`` maps one store item and takes no store.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp_server_for_oscal.tools.utils import OSCALModelType, try_notify_client_error

if TYPE_CHECKING:
    from mcp.server.mcpserver import Context

    from mcp_server_for_oscal.tools.oscal_store import OscalStore


# ------------------------------------------------------------------
# Per-model tools (from tools/query_oscal_models.py at 599a41d)
# ------------------------------------------------------------------


def old_query(
    store: OscalStore,
    model_type: OSCALModelType,
    query_type: str,
    query_value: str | None,
    offset: int,
    limit: int,
    ctx: Context | None = None,
) -> dict:
    return store.query(
        ctx=ctx,
        oscal_model_type=model_type,
        query_type=query_type,
        query_value=query_value,
        offset=offset,
        limit=limit,
    )


def old_list_documents(
    store: OscalStore,
    model_type: OSCALModelType,
    offset: int,
    limit: int,
    ctx: Context | None = None,
) -> dict:
    return store.list_documents(ctx=ctx, oscal_model_type=model_type, offset=offset, limit=limit)


def old_list_children(
    store: OscalStore,
    element_type: str,
    parent_doc_uuid: str | None,
    offset: int,
    limit: int,
    ctx: Context | None = None,
) -> dict:
    return store.list_child_elements(
        ctx=ctx,
        parent_doc_uuid=parent_doc_uuid,
        element_type=element_type,
        offset=offset,
        limit=limit,
    )


# ------------------------------------------------------------------
# Component Definition tools (from tools/query_component_definition.py at 599a41d)
# ------------------------------------------------------------------


def old_child_summary(item: dict, name_key: str) -> dict:
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


def old_child_page(
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
        "items": [old_child_summary(item, name_key) for item in result["items"]],
        "total": result["total"],
        "offset": result["offset"],
        "limit": result["limit"],
        "hasMore": result["hasMore"],
    }


def old_list_components(
    store: OscalStore,
    ctx: Context | None,
    offset: int,
    limit: int,
) -> dict:
    """Return a Page_Response of Component summaries.

    Raises:
        RuntimeError: If the store contains no Components.
    """
    page = old_child_page(store, ctx, "component", "title", offset, limit)
    if page["total"] == 0:
        msg = "No Components loaded"
        try_notify_client_error(msg, ctx)
        raise RuntimeError(msg)
    return page


def old_list_capabilities(
    store: OscalStore,
    ctx: Context | None,
    offset: int,
    limit: int,
) -> dict:
    """Return a Page_Response of Capability summaries.

    Capabilities are optional in OSCAL, so an empty store yields an empty
    page rather than an error.
    """
    return old_child_page(store, ctx, "capability", "name", offset, limit)


def old_list_component_definitions(
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
