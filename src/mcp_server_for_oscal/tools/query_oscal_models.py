"""
MCP tools for querying all OSCAL model types via the OscalStore.

Three generic tools take the model type or element type as an argument:
``list_documents`` and ``query_documents`` for documents, and
``list_child_elements`` for child elements. They are thin, validating wrappers
over the OscalStore singleton. A cross-model ``text_search_oscal`` tool
provides FTS5 full-text search, and ``get_child_element`` fetches one child
element by identifier.

Tool docstrings are the descriptions sent to clients, so they follow the
Description_Shape checked by ``tests/test_tool_budget.py``. Parameter
descriptions live in the ``Annotated`` types (see ``tools/descriptions.py``).
"""

import logging
from typing import Annotated, NoReturn

from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.tools.descriptions import (
    BudgetedLimit,
    ChildElementType,
    DocumentModelType,
    Limit,
    Offset,
    OptionalModelName,
    ParamDoc,
    ParentDocUuid,
    PerModelQueryType,
    QueryValue,
)
from mcp_server_for_oscal.tools.metaschema_common import check_page
from mcp_server_for_oscal.tools.oscal_store import (
    CHILD_ELEMENT_TYPES,
    VALID_QUERY_TYPES,
    OscalStore,
)
from mcp_server_for_oscal.tools.utils import OSCALModelType, shown, try_notify_client_error

logger = logging.getLogger(__name__)

# Module-level singleton — initialised lazily by ``init_store()``.
_store: OscalStore | None = None


def init_store(store: OscalStore) -> None:
    """Set the module-level OscalStore singleton.

    Called during server startup after the store has been initialised and
    directories have been scanned.
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


# ------------------------------------------------------------------
# Generic tools: one tool for every model type or element type
# ------------------------------------------------------------------

_MODEL_VALUES: tuple[str, ...] = tuple(m.value for m in OSCALModelType)
_ELEMENT_VALUES: tuple[str, ...] = tuple(t for types in CHILD_ELEMENT_TYPES.values() for t in types)


def _reject(msg: str, ctx: Context | None) -> NoReturn:
    """Notify the client of ``msg`` and raise it as a ``ValueError``."""
    try_notify_client_error(msg, ctx)
    raise ValueError(msg)


def _shown_value(value: object) -> str:
    """Return ``value`` cut for an error message; non-strings show their type name."""
    return shown(value) if isinstance(value, str) else type(value).__name__


def _check_model_type(value: object, ctx: Context | None) -> OSCALModelType:
    """Return the ``OSCALModelType`` for ``value`` or reject it, listing the valid values."""
    if isinstance(value, str) and value in _MODEL_VALUES:
        return OSCALModelType(value)
    _reject(
        f"Invalid model_type: '{_shown_value(value)}'. Valid values: {', '.join(_MODEL_VALUES)}",
        ctx,
    )


def _check_element_type(value: object, ctx: Context | None) -> str:
    """Return ``value`` if it is a child element type or reject it, listing the valid values."""
    if isinstance(value, str) and value in _ELEMENT_VALUES:
        return value
    _reject(
        f"Invalid element_type: '{_shown_value(value)}'. "
        f"Valid values: {', '.join(_ELEMENT_VALUES)}",
        ctx,
    )


def _check_page(offset: int, limit: int, ctx: Context | None) -> None:
    """Reject an out-of-range ``offset`` or ``limit``, notifying the client first."""
    try:
        check_page(offset, limit)
    except ValueError as err:
        try_notify_client_error(str(err), ctx)
        raise


def _child_summary(item: dict) -> dict:
    """Return the store item without ``raw_json`` and with ``sizeInBytes`` (0 when no JSON)."""
    raw_json = item.get("raw_json")
    summary = {k: v for k, v in item.items() if k != "raw_json"}
    summary["sizeInBytes"] = len(raw_json.encode("utf-8")) if raw_json is not None else 0
    return summary


@tool()
def list_documents(
    model_type: DocumentModelType,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List loaded OSCAL documents of one model type with UUID, title, child count, and size.
    Use when picking a `query_value` for query_documents. Results are paged."""
    model = _check_model_type(model_type, ctx)
    _check_page(offset, limit, ctx)
    return _get_store().list_documents(ctx=ctx, oscal_model_type=model, offset=offset, limit=limit)


@tool()
def query_documents(
    model_type: DocumentModelType,
    query_type: PerModelQueryType = "all",
    query_value: QueryValue = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """Return loaded OSCAL documents of one model type with metadata and `childTypeCounts`.
    For child elements, use list_child_elements, then get_child_element."""
    model = _check_model_type(model_type, ctx)
    _check_page(offset, limit, ctx)
    if query_type not in VALID_QUERY_TYPES:
        _reject(
            f"Invalid query_type: '{_shown_value(query_type)}'. "
            f"Valid types: {', '.join(VALID_QUERY_TYPES)}",
            ctx,
        )
    if query_type != "all" and not query_value:
        _reject(f"query_value is required for query_type '{query_type}'", ctx)
    return _get_store().query(
        ctx=ctx,
        oscal_model_type=model,
        query_type=query_type,
        query_value=query_value,
        offset=offset,
        limit=limit,
    )


@tool()
def list_child_elements(
    element_type: ChildElementType,
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List child elements (controls, components, findings, etc.) of one type with ID, title,
    parent document, and size. Get one's OSCAL JSON with get_child_element. Results are paged."""
    element = _check_element_type(element_type, ctx)
    _check_page(offset, limit, ctx)
    page = _get_store().list_child_elements(
        ctx=ctx,
        parent_doc_uuid=parent_doc_uuid,
        element_type=element,
        offset=offset,
        limit=limit,
        include_raw_json=True,
    )
    return {**page, "items": [_child_summary(item) for item in page["items"]]}


# ------------------------------------------------------------------
# Cross-model text search
# ------------------------------------------------------------------


@tool()
def text_search_oscal(
    query_text: Annotated[str, ParamDoc("Full-text search terms.")],
    oscal_model_type: OptionalModelName = None,
    offset: Offset = 0,
    limit: BudgetedLimit = 10,
    ctx: Context | None = None,
) -> dict:
    """Search loaded OSCAL documents and child elements, including control prose, ranked by
    relevance. Pass a child hit's entity ID and `parentDocumentUuid` to get_child_element."""
    try:
        check_page(offset, limit)
    except ValueError as err:
        try_notify_client_error(str(err), ctx)
        raise

    model_type_enum: OSCALModelType | None = None
    if oscal_model_type:
        try:
            model_type_enum = OSCALModelType(oscal_model_type)
        except ValueError:
            logger.warning(
                "Unknown oscal_model_type '%s'; searching all types",
                oscal_model_type,
            )

    return _get_store().text_search(
        query_text=query_text,
        oscal_model_type=model_type_enum,
        offset=offset,
        limit=limit,
    )


# ------------------------------------------------------------------
# Get child element by ID (cross-model)
# ------------------------------------------------------------------


@tool()
def get_child_element(
    element_id: Annotated[
        str,
        ParamDoc(
            "UUID or token ID (e.g. ac-1) of the child element. Token IDs are unique only "
            "within one document."
        ),
    ],
    parent_doc_uuid: ParentDocUuid = None,
    ctx: Context | None = None,  # noqa: ARG001 - injected by MCP; kept for API
) -> dict | None:
    """Get one child element by UUID or token ID, with its OSCAL JSON in `raw_json`. If the
    result is an `ambiguous_element_id` error, retry with parent_doc_uuid."""
    return _get_store().get_child_element(
        element_id=element_id,
        parent_doc_uuid=parent_doc_uuid,
    )
