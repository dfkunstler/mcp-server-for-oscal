"""
MCP tools for querying all OSCAL model types via the OscalStore.

Each OSCAL model type gets a query/list tool pair that delegates to the
OscalStore singleton, plus list tools for each model's child element types.
A cross-model ``text_search_oscal`` tool provides FTS5 full-text search, and
``get_child_element`` fetches one child element by identifier.

Tool docstrings are the descriptions sent to clients, so they follow the
Description_Shape checked by ``tests/test_tool_budget.py``. Parameter
descriptions live in the ``Annotated`` types (see ``tools/descriptions.py``).
"""

import logging
from typing import Annotated

from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.tools.descriptions import (
    Limit,
    Offset,
    OptionalModelName,
    ParamDoc,
    ParentDocUuid,
    PerModelQueryType,
    QueryValue,
)
from mcp_server_for_oscal.tools.oscal_store import OscalStore
from mcp_server_for_oscal.tools.utils import OSCALModelType

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


def _query(
    model_type: OSCALModelType,
    query_type: str,
    query_value: str | None,
    offset: int,
    limit: int,
    ctx: Context | None,
) -> dict:
    return _get_store().query(
        ctx=ctx,
        oscal_model_type=model_type,
        query_type=query_type,
        query_value=query_value,
        offset=offset,
        limit=limit,
    )


def _list_documents(
    model_type: OSCALModelType, offset: int, limit: int, ctx: Context | None
) -> dict:
    return _get_store().list_documents(
        ctx=ctx, oscal_model_type=model_type, offset=offset, limit=limit
    )


def _list_children(
    element_type: str,
    parent_doc_uuid: str | None,
    offset: int,
    limit: int,
    ctx: Context | None,
) -> dict:
    return _get_store().list_child_elements(
        ctx=ctx,
        parent_doc_uuid=parent_doc_uuid,
        element_type=element_type,
        offset=offset,
        limit=limit,
    )


# ------------------------------------------------------------------
# Catalog tools
# ------------------------------------------------------------------


@tool()
def query_catalog(
    query_type: PerModelQueryType = "all",
    query_value: QueryValue = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """Return loaded OSCAL Catalogs with metadata and `childTypeCounts`. For controls and groups,
    use list_catalog_controls or list_catalog_groups, then get_child_element."""
    return _query(OSCALModelType.CATALOG, query_type, query_value, offset, limit, ctx)


@tool()
def list_catalogs(
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List loaded OSCAL Catalogs with UUID, title, child count, and size. Use when picking a
    `query_value` for query_catalog. Results are paged."""
    return _list_documents(OSCALModelType.CATALOG, offset, limit, ctx)


# ------------------------------------------------------------------
# SSP tools
# ------------------------------------------------------------------


@tool()
def query_ssp(
    query_type: PerModelQueryType = "all",
    query_value: QueryValue = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """Return loaded OSCAL SSPs with metadata and `childTypeCounts`. For child elements, use
    list_ssp_control_implementations or list_ssp_system_components, then get_child_element."""
    return _query(OSCALModelType.SYSTEM_SECURITY_PLAN, query_type, query_value, offset, limit, ctx)


@tool()
def list_ssps(
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List loaded OSCAL System Security Plans with UUID, title, child count, and size. Use when
    picking a `query_value` for query_ssp. Results are paged."""
    return _list_documents(OSCALModelType.SYSTEM_SECURITY_PLAN, offset, limit, ctx)


# ------------------------------------------------------------------
# Profile tools
# ------------------------------------------------------------------


@tool()
def query_profile(
    query_type: PerModelQueryType = "all",
    query_value: QueryValue = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """Return loaded OSCAL Profiles with metadata and `childTypeCounts`. For child elements, use
    list_profile_imports or list_profile_modify, then get_child_element."""
    return _query(OSCALModelType.PROFILE, query_type, query_value, offset, limit, ctx)


@tool()
def list_profiles(
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List loaded OSCAL Profiles with UUID, title, child count, and size. Use when picking a
    `query_value` for query_profile. Results are paged."""
    return _list_documents(OSCALModelType.PROFILE, offset, limit, ctx)


# ------------------------------------------------------------------
# Assessment Plan tools
# ------------------------------------------------------------------


@tool()
def query_assessment_plan(
    query_type: PerModelQueryType = "all",
    query_value: QueryValue = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """Return loaded OSCAL Assessment Plans with `childTypeCounts`. For child elements, use list_assessment_plan_tasks or list_assessment_plan_activities, then
    get_child_element."""
    return _query(OSCALModelType.ASSESSMENT_PLAN, query_type, query_value, offset, limit, ctx)


@tool()
def list_assessment_plans(
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List loaded OSCAL Assessment Plans with UUID, title, child count, and size. Use when
    picking a `query_value` for query_assessment_plan. Results are paged."""
    return _list_documents(OSCALModelType.ASSESSMENT_PLAN, offset, limit, ctx)


# ------------------------------------------------------------------
# Assessment Results tools
# ------------------------------------------------------------------


@tool()
def query_assessment_results(
    query_type: PerModelQueryType = "all",
    query_value: QueryValue = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """Return loaded OSCAL Assessment Results with `childTypeCounts`. For child elements, use list_assessment_results_results or list_assessment_results_findings, then
    get_child_element."""
    return _query(OSCALModelType.ASSESSMENT_RESULTS, query_type, query_value, offset, limit, ctx)


@tool()
def list_assessment_results(
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List loaded OSCAL Assessment Results with UUID, title, child count, and size. Use when
    picking a `query_value` for query_assessment_results. Results are paged."""
    return _list_documents(OSCALModelType.ASSESSMENT_RESULTS, offset, limit, ctx)


# ------------------------------------------------------------------
# POA&M tools
# ------------------------------------------------------------------


@tool()
def query_poam(
    query_type: PerModelQueryType = "all",
    query_value: QueryValue = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """Return loaded OSCAL Plans of Action and Milestones with metadata and `childTypeCounts`.
    For POA&M items, use list_poam_items, then get_child_element."""
    return _query(
        OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES, query_type, query_value, offset, limit, ctx
    )


@tool()
def list_poams(
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List loaded OSCAL Plans of Action and Milestones with UUID, title, child count, and size.
    Use when picking a `query_value` for query_poam. Results are paged."""
    return _list_documents(OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES, offset, limit, ctx)


# ------------------------------------------------------------------
# Mapping Collection tools
# ------------------------------------------------------------------


@tool()
def query_mapping_collection(
    query_type: PerModelQueryType = "all",
    query_value: QueryValue = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """Return loaded OSCAL Mapping Collections with metadata and `childTypeCounts`. For
    mappings, use list_mapping_collection_mappings, then get_child_element."""
    return _query(OSCALModelType.MAPPING, query_type, query_value, offset, limit, ctx)


@tool()
def list_mapping_collections(
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List loaded OSCAL Mapping Collections with UUID, title, child count, and size. Use when
    picking a `query_value` for query_mapping_collection. Results are paged."""
    return _list_documents(OSCALModelType.MAPPING, offset, limit, ctx)


# ------------------------------------------------------------------
# Cross-model text search
# ------------------------------------------------------------------


@tool()
def text_search_oscal(
    query_text: Annotated[str, ParamDoc("Full-text search terms.")],
    oscal_model_type: OptionalModelName = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,  # noqa: ARG001 - injected by MCP; kept for API
) -> dict:
    """Search the text of loaded OSCAL documents and child elements, ranked by relevance. Each
    item has entity type, ID, title, description, and model type. Results are paged."""
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
# Child element tools
# ------------------------------------------------------------------


@tool()
def list_catalog_controls(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List controls in loaded OSCAL Catalogs with token ID (e.g. ac-1), title, and parent
    document. Get a control's OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("control", parent_doc_uuid, offset, limit, ctx)


@tool()
def list_catalog_groups(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List control groups (families) in loaded OSCAL Catalogs with token ID, title, and parent
    document. Get a group's OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("group", parent_doc_uuid, offset, limit, ctx)


@tool()
def list_ssp_control_implementations(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List the control-implementation element of loaded OSCAL SSPs, one per SSP. Get
    its OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("control-implementation", parent_doc_uuid, offset, limit, ctx)


@tool()
def list_ssp_system_components(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List system components in loaded OSCAL SSPs with UUID, title, and parent document. Get
    a component's OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("system-component", parent_doc_uuid, offset, limit, ctx)


@tool()
def list_profile_imports(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List imports in loaded OSCAL Profiles, with positional IDs (import-0, import-1). Get
    an import's OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("import", parent_doc_uuid, offset, limit, ctx)


@tool()
def list_profile_modify(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List the modify element (control tailoring) of loaded OSCAL Profiles, one per profile.
    Get its OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("modify", parent_doc_uuid, offset, limit, ctx)


@tool()
def list_assessment_plan_tasks(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List tasks in loaded OSCAL Assessment Plans with UUID, title, and parent document. Get
    a task's OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("task", parent_doc_uuid, offset, limit, ctx)


@tool()
def list_assessment_plan_activities(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List activities in loaded OSCAL Assessment Plans with UUID, title, and parent document.
    Get an activity's OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("activity", parent_doc_uuid, offset, limit, ctx)


@tool()
def list_assessment_results_results(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List results in loaded OSCAL Assessment Results with UUID, title, and parent document.
    Get a result's OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("result", parent_doc_uuid, offset, limit, ctx)


@tool()
def list_assessment_results_findings(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List findings in loaded OSCAL Assessment Results with UUID, title, and parent document.
    Get a finding's OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("finding", parent_doc_uuid, offset, limit, ctx)


@tool()
def list_poam_items(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List POA&M items in loaded OSCAL POA&Ms with ID, title, and parent document. Get
    an item's OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("poam-item", parent_doc_uuid, offset, limit, ctx)


@tool()
def list_mapping_collection_mappings(
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """List mappings in loaded OSCAL Mapping Collections with UUID, title, and parent document.
    Get a mapping's OSCAL JSON with get_child_element. Results are paged."""
    return _list_children("mapping", parent_doc_uuid, offset, limit, ctx)


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
