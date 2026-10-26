"""
Tests for the centralized tool registry in tools/__init__.py.

Covers unit tests (task 1.3) and property-based tests (task 1.2)
for the get_tool_list() function.

Feature: oscal-agent-production
"""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal import main
from mcp_server_for_oscal.config import config
from mcp_server_for_oscal.tools import get_tool_list
from mcp_server_for_oscal.tools.list_models import list_oscal_models
from mcp_server_for_oscal.tools.utils import MODEL_MAP, OSCALModelType

# All tools that should always be present (query_oscal_documentation is
# unconditionally registered regardless of knowledge_base_id config).
BASE_TOOL_NAMES = {
    "list_oscal_models",
    "get_oscal_schema",
    # Metaschema constraint tools (unconditionally registered, no AWS dependency)
    "get_allowed_values",
    "get_constraints",
    "find_references_to",
    "get_definition",
    "get_metaschema_source",
    "list_oscal_resources",
    "query_component_definition",
    "list_component_definitions",
    "list_components",
    "list_capabilities",
    "get_capability",
    "validate_oscal_content",
    "validate_oscal_file",
    # New OSCAL model tools
    "query_catalog",
    "list_catalogs",
    "query_ssp",
    "list_ssps",
    "query_profile",
    "list_profiles",
    "query_assessment_plan",
    "list_assessment_plans",
    "query_assessment_results",
    "list_assessment_results",
    "query_poam",
    "list_poams",
    "query_mapping_collection",
    "list_mapping_collections",
    "text_search_oscal",
    # Child element tools
    "list_catalog_controls",
    "list_catalog_groups",
    "list_ssp_control_implementations",
    "list_ssp_system_components",
    "list_profile_imports",
    "list_profile_modify",
    "list_assessment_plan_tasks",
    "list_assessment_plan_activities",
    "list_assessment_results_results",
    "list_assessment_results_findings",
    "list_poam_items",
    "list_mapping_collection_mappings",
    "get_child_element",
    # Documentation tools (unconditionally registered)
    "query_oscal_documentation",
    "get_oscal_documentation",
}


class TestGetToolListUnit:
    """Unit tests for get_tool_list() — task 1.3."""

    def test_returns_all_base_tools(self):
        """get_tool_list() returns all base tools including query_oscal_documentation."""
        tools = get_tool_list()
        tool_names = {t.__name__ for t in tools}
        assert tool_names == BASE_TOOL_NAMES
        # 45 here; the server adds `about` for 46 (get_oscal_documentation joined in #45).
        assert len(tools) == len(BASE_TOOL_NAMES) == 45

    def test_excludes_about(self):
        """get_tool_list() never includes the 'about' tool."""
        tools = get_tool_list()
        tool_names = {t.__name__ for t in tools}
        assert "about" not in tool_names

    def test_includes_query_oscal_documentation_when_kb_id_set(self):
        """get_tool_list() includes query_oscal_documentation when KB ID is set."""
        with patch.object(config, "knowledge_base_id", "my-kb-id"):
            tools = get_tool_list()
            tool_names = {t.__name__ for t in tools}
            assert "query_oscal_documentation" in tool_names

    def test_includes_query_oscal_documentation_when_kb_id_empty(self):
        """get_tool_list() includes query_oscal_documentation even when KB ID is empty."""
        with patch.object(config, "knowledge_base_id", ""):
            tools = get_tool_list()
            tool_names = {t.__name__ for t in tools}
            assert "query_oscal_documentation" in tool_names


class TestProperty1UnconditionalToolInclusion:
    """
    Property 1: Unconditional tool inclusion

    For any string value of knowledge_base_id (empty or non-empty),
    the tool list returned by get_tool_list() SHALL always contain
    query_oscal_documentation and all base tools.

    **Validates: Requirements 1.2, 1.3, 4.1**
    """

    @settings(max_examples=100)
    @given(kb_id=st.text(min_size=1, max_size=200).filter(lambda s: s.strip()))
    def test_non_empty_kb_id_includes_doc_tool(self, kb_id):
        """
        Feature: oscal-agent-production, Property 1: Unconditional tool inclusion

        When knowledge_base_id is a non-empty string, query_oscal_documentation
        must be present and all base tools must also be present.
        """
        with patch.object(config, "knowledge_base_id", kb_id):
            tools = get_tool_list()
            tool_names = {t.__name__ for t in tools}

            assert "query_oscal_documentation" in tool_names
            assert BASE_TOOL_NAMES.issubset(tool_names)

    @settings(max_examples=100)
    @given(kb_id=st.just(""))
    def test_empty_kb_id_still_includes_doc_tool(self, kb_id):
        """
        Feature: oscal-agent-production, Property 1: Unconditional tool inclusion

        When knowledge_base_id is empty, query_oscal_documentation must
        still be present, along with all base tools.
        """
        with patch.object(config, "knowledge_base_id", kb_id):
            tools = get_tool_list()
            tool_names = {t.__name__ for t in tools}

            assert "query_oscal_documentation" in tool_names
            assert BASE_TOOL_NAMES.issubset(tool_names)


# ---------------------------------------------------------------------------
# Feature: search-fetch-pairs (#45), Requirement 13
# ---------------------------------------------------------------------------

# Frozen snapshot of the live tools/list before search-fetch-pairs: tool name -> parameter
# names. Captured by building the server (build_server + _setup_tools) from a worktree of
# main at commit 25f949d75e2f068b6b04022a8ec9e13547da4de7. Do not regenerate it from the
# current code; it records what clients could rely on before the feature.
_PAGED = frozenset({"offset", "limit"})
_QUERY = frozenset({"query_type", "query_value"}) | _PAGED
_CHILD = frozenset({"parent_doc_uuid"}) | _PAGED
PRE_FEATURE_TOOLS: dict[str, frozenset[str]] = {
    "about": frozenset(),
    "list_oscal_models": frozenset(),
    "get_oscal_schema": frozenset({"model_name", "schema_type"}),
    "get_allowed_values": frozenset({"model", "target"}) | _PAGED,
    "get_constraints": frozenset({"model", "target", "constraint_types"}) | _PAGED,
    "find_references_to": frozenset({"assembly", "model"}) | _PAGED,
    "get_definition": frozenset({"name", "model", "kind"}) | _PAGED,
    "get_metaschema_source": frozenset({"file", "start_line", "line_count"}),
    "list_oscal_resources": _PAGED,
    "query_component_definition": frozenset(
        {"component_definition_filter", "query_type", "query_value", "return_format"}
    )
    | _PAGED,
    "list_component_definitions": _PAGED,
    "list_components": _PAGED,
    "list_capabilities": _PAGED,
    "get_capability": frozenset({"uuid"}),
    "validate_oscal_content": frozenset({"content", "model_type"}),
    "validate_oscal_file": frozenset({"file_uri", "model_type"}),
    "query_catalog": _QUERY,
    "list_catalogs": _PAGED,
    "query_ssp": _QUERY,
    "list_ssps": _PAGED,
    "query_profile": _QUERY,
    "list_profiles": _PAGED,
    "query_assessment_plan": _QUERY,
    "list_assessment_plans": _PAGED,
    "query_assessment_results": _QUERY,
    "list_assessment_results": _PAGED,
    "query_poam": _QUERY,
    "list_poams": _PAGED,
    "query_mapping_collection": _QUERY,
    "list_mapping_collections": _PAGED,
    "text_search_oscal": frozenset({"query_text", "oscal_model_type"}) | _PAGED,
    "list_catalog_controls": _CHILD,
    "list_catalog_groups": _CHILD,
    "list_ssp_control_implementations": _CHILD,
    "list_ssp_system_components": _CHILD,
    "list_profile_imports": _CHILD,
    "list_profile_modify": _CHILD,
    "list_assessment_plan_tasks": _CHILD,
    "list_assessment_plan_activities": _CHILD,
    "list_assessment_results_results": _CHILD,
    "list_assessment_results_findings": _CHILD,
    "list_poam_items": _CHILD,
    "list_mapping_collection_mappings": _CHILD,
    "get_child_element": frozenset({"element_id", "parent_doc_uuid"}),
    "query_oscal_documentation": frozenset({"query"}),
}

# Model-name parameters are described with this shared prefix (tools/descriptions.py).
_MODEL_NAME_DOC_PREFIX = "OSCAL model name from list_oscal_models"


@pytest.fixture(scope="module")
def live_server() -> Any:
    """A fresh server built by the production path, with every tool registered."""
    server = main.build_server()
    main._setup_tools(server)
    return server


@pytest.fixture(scope="module")
def live_tools(live_server: Any) -> dict[str, dict[str, Any]]:
    """Live tools/list entries by tool name."""
    tools = asyncio.run(live_server.list_tools())
    dumped = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]
    return {t["name"]: t for t in dumped}


def _properties(tool: dict[str, Any]) -> dict[str, Any]:
    return tool["inputSchema"].get("properties", {})


def _enums(schema: Any) -> list[list[Any]]:
    """Every `enum` list anywhere in a JSON schema fragment (anyOf, items, $defs, ...)."""
    if isinstance(schema, dict):
        found = [schema["enum"]] if isinstance(schema.get("enum"), list) else []
        for value in schema.values():
            found.extend(_enums(value))
        return found
    if isinstance(schema, list):
        return [e for item in schema for e in _enums(item)]
    return []


class TestExistingToolsKept:
    """Validates: search-fetch-pairs Requirement 13.3."""

    def test_snapshot_matches_registry_minus_new_tool(self):
        # Guards the snapshot itself: the only tool added since main is get_oscal_documentation.
        assert set(PRE_FEATURE_TOOLS) == (BASE_TOOL_NAMES - {"get_oscal_documentation"}) | {"about"}

    @pytest.mark.parametrize("name", sorted(PRE_FEATURE_TOOLS))
    def test_tool_and_params_still_present(self, live_tools, name):
        assert name in live_tools, f"{name} was removed"
        current = set(_properties(live_tools[name]))
        missing = PRE_FEATURE_TOOLS[name] - current
        assert not missing, f"{name} lost parameters {sorted(missing)}"


class TestDocumentationIsNotAModel:
    """Validates: search-fetch-pairs Requirement 13.4."""

    def test_not_in_oscal_model_type(self):
        assert "documentation" not in {m.value for m in OSCALModelType}

    def test_not_in_model_map(self):
        assert "documentation" not in {getattr(k, "value", k) for k in MODEL_MAP}

    def test_not_listed_by_list_oscal_models(self):
        # list_oscal_models is the source every model-name parameter points callers to.
        assert "documentation" not in {getattr(k, "value", k) for k in list_oscal_models()}

    def test_no_model_name_parameter_accepts_documentation(self, live_tools):
        model_params = {
            (name, param): schema
            for name, tool in live_tools.items()
            for param, schema in _properties(tool).items()
            if schema.get("description", "").startswith(_MODEL_NAME_DOC_PREFIX)
        }
        # Not vacuous: the known model-name parameters are all found.
        assert {
            ("get_oscal_schema", "model_name"),
            ("get_allowed_values", "model"),
            ("get_constraints", "model"),
            ("find_references_to", "model"),
            ("get_definition", "model"),
            ("validate_oscal_content", "model_type"),
            ("validate_oscal_file", "model_type"),
            ("text_search_oscal", "oscal_model_type"),
        } <= set(model_params)
        for key, schema in model_params.items():
            for enum in _enums(schema):
                assert "documentation" not in enum, key

    def test_no_enum_anywhere_offers_documentation(self, live_tools):
        for name, tool in live_tools.items():
            for enum in _enums(tool["inputSchema"]):
                assert "documentation" not in enum, name


class TestRoutingGuideFetchSteps:
    """Validates: search-fetch-pairs Requirement 13.5 on the live server instructions.

    The line-level check on ROUTING_GUIDE itself is
    tests/tools/test_descriptions.py::TestServerInstructions::test_guide_names_fetch_step_after_search;
    this only confirms the served instructions carry both pairs.
    """

    @pytest.mark.parametrize(
        ("search_tool", "fetch_tool"),
        [
            ("query_oscal_documentation", "get_oscal_documentation"),
            ("text_search_oscal", "get_child_element"),
        ],
    )
    def test_instructions_name_fetch_step(self, live_server, search_tool, fetch_tool):
        assert f"{search_tool}, then {fetch_tool}" in (live_server.instructions or "")
