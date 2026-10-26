"""
Tests for the new OSCAL model type MCP tools in query_oscal_models.py.
"""

import inspect
import json

import pytest

from mcp_server_for_oscal.tools import query_oscal_models, tool_budget
from mcp_server_for_oscal.tools.descriptions import (
    BudgetedLimit,
    Limit,
    Offset,
    OptionalModelName,
    ParentDocUuid,
    PerModelQueryType,
    QueryValue,
)
from mcp_server_for_oscal.tools.oscal_store import OscalStore
from mcp_server_for_oscal.tools.utils import OSCALModelType

# ---------------------------------------------------------------------------
# Helpers — minimal valid OSCAL documents
# ---------------------------------------------------------------------------


def _make_catalog(uuid="c1d2e3f4-5678-4abc-8def-aabbccddeeff", title="Test Catalog"):
    return {
        "catalog": {
            "uuid": uuid,
            "metadata": {
                "title": title,
                "last-modified": "2024-01-01T00:00:00Z",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
        }
    }


def _make_component_definition(
    uuid="a1b2c3d4-5678-4abc-8def-123456789012",
    title="Test Component Definition",
):
    return {
        "component-definition": {
            "uuid": uuid,
            "metadata": {
                "title": title,
                "last-modified": "2024-01-01T00:00:00Z",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
            "components": [
                {
                    "uuid": "b2c3d4e5-6789-4bcd-9efa-234567890123",
                    "type": "software",
                    "title": "Sample Component",
                    "description": "A sample component for testing",
                }
            ],
        }
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """Create an OscalStore with an ephemeral DB in tmp_path."""
    db_path = str(tmp_path / "test.db")
    s = OscalStore(db_path=db_path, cache_size=10, seed_from_bundled=False)
    yield s
    s.close()


@pytest.fixture
def populated_store(store, tmp_path):
    """Store with a catalog and a component-definition ingested."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    cat = _make_catalog()
    (docs_dir / "catalog.json").write_text(json.dumps(cat))

    cdef = _make_component_definition()
    (docs_dir / "cdef.json").write_text(json.dumps(cdef))

    store.scan_directory(docs_dir)

    # Wire the module-level singleton so tool functions work
    query_oscal_models.init_store(store)
    return store


# ---------------------------------------------------------------------------
# init_store / _get_store
# ---------------------------------------------------------------------------


class TestInitStore:
    def test_get_store_raises_before_init(self):
        """_get_store raises RuntimeError when no store is set."""
        saved = query_oscal_models._store
        try:
            query_oscal_models._store = None
            with pytest.raises(RuntimeError, match="not been initialised"):
                query_oscal_models._get_store()
        finally:
            query_oscal_models._store = saved

    def test_init_store_sets_singleton(self, store):
        query_oscal_models.init_store(store)
        assert query_oscal_models._get_store() is store


# ---------------------------------------------------------------------------
# Catalog tools
# ---------------------------------------------------------------------------


class TestCatalogTools:
    def test_list_catalogs_returns_page_response(self, populated_store):
        result = query_oscal_models.list_catalogs(ctx=None)
        assert "items" in result
        assert "total" in result
        assert result["total"] == 1
        assert result["items"][0]["title"] == "Test Catalog"

    def test_query_catalog_all(self, populated_store):
        result = query_oscal_models.query_catalog(ctx=None, query_type="all")
        assert result["total"] == 1

    def test_query_catalog_by_uuid(self, populated_store):
        result = query_oscal_models.query_catalog(
            ctx=None,
            query_type="by_uuid",
            query_value="c1d2e3f4-5678-4abc-8def-aabbccddeeff",
        )
        assert result["total"] == 1
        assert result["items"][0]["uuid"] == "c1d2e3f4-5678-4abc-8def-aabbccddeeff"

    def test_query_catalog_by_title(self, populated_store):
        result = query_oscal_models.query_catalog(
            ctx=None,
            query_type="by_title",
            query_value="Test Catalog",
        )
        assert result["total"] == 1

    def test_list_catalogs_empty(self, store):
        """list_catalogs returns empty when no catalogs are loaded."""
        query_oscal_models.init_store(store)
        result = query_oscal_models.list_catalogs(ctx=None)
        assert result["total"] == 0
        assert result["items"] == []

    def test_query_catalog_does_not_return_cdef(self, populated_store):
        """Catalog query should not return component-definition docs."""
        result = query_oscal_models.query_catalog(ctx=None, query_type="all")
        for item in result["items"]:
            assert item["model_type"] == "catalog"


# ---------------------------------------------------------------------------
# SSP tools
# ---------------------------------------------------------------------------


class TestSSPTools:
    def test_list_ssps_empty(self, populated_store):
        result = query_oscal_models.list_ssps(ctx=None)
        assert result["total"] == 0

    def test_query_ssp_all_empty(self, populated_store):
        result = query_oscal_models.query_ssp(ctx=None, query_type="all")
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Profile tools
# ---------------------------------------------------------------------------


class TestProfileTools:
    def test_list_profiles_empty(self, populated_store):
        result = query_oscal_models.list_profiles(ctx=None)
        assert result["total"] == 0

    def test_query_profile_all_empty(self, populated_store):
        result = query_oscal_models.query_profile(ctx=None, query_type="all")
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Assessment Plan tools
# ---------------------------------------------------------------------------


class TestAssessmentPlanTools:
    def test_list_assessment_plans_empty(self, populated_store):
        result = query_oscal_models.list_assessment_plans(ctx=None)
        assert result["total"] == 0

    def test_query_assessment_plan_all_empty(self, populated_store):
        result = query_oscal_models.query_assessment_plan(ctx=None, query_type="all")
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Assessment Results tools
# ---------------------------------------------------------------------------


class TestAssessmentResultsTools:
    def test_list_assessment_results_empty(self, populated_store):
        result = query_oscal_models.list_assessment_results(ctx=None)
        assert result["total"] == 0

    def test_query_assessment_results_all_empty(self, populated_store):
        result = query_oscal_models.query_assessment_results(ctx=None, query_type="all")
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# POA&M tools
# ---------------------------------------------------------------------------


class TestPOAMTools:
    def test_list_poams_empty(self, populated_store):
        result = query_oscal_models.list_poams(ctx=None)
        assert result["total"] == 0

    def test_query_poam_all_empty(self, populated_store):
        result = query_oscal_models.query_poam(ctx=None, query_type="all")
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Mapping Collection tools
# ---------------------------------------------------------------------------


class TestMappingCollectionTools:
    def test_list_mapping_collections_empty(self, populated_store):
        result = query_oscal_models.list_mapping_collections(ctx=None)
        assert result["total"] == 0

    def test_query_mapping_collection_all_empty(self, populated_store):
        result = query_oscal_models.query_mapping_collection(ctx=None, query_type="all")
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# text_search_oscal
# ---------------------------------------------------------------------------


class TestTextSearchOscal:
    def test_text_search_empty_query(self, populated_store):
        result = query_oscal_models.text_search_oscal(ctx=None, query_text="")
        assert result["total"] == 0

    def test_text_search_finds_catalog(self, populated_store):
        """After indexing, the catalog title should be searchable."""
        # Trigger indexing by querying the catalog
        query_oscal_models.query_catalog(ctx=None, query_type="all")
        result = query_oscal_models.text_search_oscal(ctx=None, query_text="Test Catalog")
        assert result["total"] >= 1

    def test_text_search_scoped_to_model_type(self, populated_store):
        """Scoping to catalog should exclude component-definition results."""
        # Trigger indexing for both
        query_oscal_models.query_catalog(ctx=None, query_type="all")
        populated_store.query(
            oscal_model_type=OSCALModelType.COMPONENT_DEFINITION,
            query_type="all",
        )
        result = query_oscal_models.text_search_oscal(
            ctx=None,
            query_text="Test",
            oscal_model_type="catalog",
        )
        for item in result["items"]:
            assert item["model_type"] == "catalog"

    def test_text_search_invalid_model_type_searches_all(self, populated_store):
        """An invalid model type string falls back to searching all types."""
        query_oscal_models.query_catalog(ctx=None, query_type="all")
        result = query_oscal_models.text_search_oscal(
            ctx=None,
            query_text="Test",
            oscal_model_type="not-a-real-type",
        )
        # Should not raise; returns results across all types
        assert "items" in result

    @pytest.mark.parametrize(
        ("offset", "limit", "param"),
        [(-1, 10, "offset"), (0, 0, "limit"), (0, 101, "limit")],
    )
    def test_text_search_rejects_out_of_range_page(self, populated_store, offset, limit, param):
        """Out-of-range offset/limit raise ValueError naming the parameter (BudgetedLimit 1-100)."""
        with pytest.raises(ValueError, match=param):
            query_oscal_models.text_search_oscal(
                ctx=None, query_text="Test", offset=offset, limit=limit
            )

    @pytest.mark.parametrize("limit", [1, 100])
    def test_text_search_accepts_limit_bounds(self, populated_store, limit):
        query_oscal_models.query_catalog(ctx=None, query_type="all")
        result = query_oscal_models.text_search_oscal(ctx=None, query_text="Test", limit=limit)
        assert len(result["items"]) <= limit


# ---------------------------------------------------------------------------
# Page_Response structure
# ---------------------------------------------------------------------------


class TestPageResponseFormat:
    """All tools should return the standard Page_Response envelope."""

    @pytest.mark.parametrize(
        "tool_fn",
        [
            query_oscal_models.list_catalogs,
            query_oscal_models.list_ssps,
            query_oscal_models.list_profiles,
            query_oscal_models.list_assessment_plans,
            query_oscal_models.list_assessment_results,
            query_oscal_models.list_poams,
            query_oscal_models.list_mapping_collections,
        ],
    )
    def test_list_tools_return_page_response_keys(self, populated_store, tool_fn):
        result = tool_fn(ctx=None)
        assert set(result.keys()) == {"items", "total", "offset", "limit", "hasMore"}

    @pytest.mark.parametrize(
        "tool_fn",
        [
            query_oscal_models.query_catalog,
            query_oscal_models.query_ssp,
            query_oscal_models.query_profile,
            query_oscal_models.query_assessment_plan,
            query_oscal_models.query_assessment_results,
            query_oscal_models.query_poam,
            query_oscal_models.query_mapping_collection,
        ],
    )
    def test_query_tools_return_page_response_keys(self, populated_store, tool_fn):
        result = tool_fn(ctx=None, query_type="all")
        assert set(result.keys()) == {"items", "total", "offset", "limit", "hasMore"}


# ---------------------------------------------------------------------------
# Signatures, schemas, and descriptions (tool-context-optimization, task 5.4)
# ---------------------------------------------------------------------------

# Per_Model_Query_Tool -> child list tools its description must name
QUERY_TOOLS_AND_CHILD_TOOLS = [
    (query_oscal_models.query_catalog, ["list_catalog_controls", "list_catalog_groups"]),
    (
        query_oscal_models.query_ssp,
        ["list_ssp_control_implementations", "list_ssp_system_components"],
    ),
    (query_oscal_models.query_profile, ["list_profile_imports", "list_profile_modify"]),
    (
        query_oscal_models.query_assessment_plan,
        ["list_assessment_plan_tasks", "list_assessment_plan_activities"],
    ),
    (
        query_oscal_models.query_assessment_results,
        ["list_assessment_results_results", "list_assessment_results_findings"],
    ),
    (query_oscal_models.query_poam, ["list_poam_items"]),
    (query_oscal_models.query_mapping_collection, ["list_mapping_collection_mappings"]),
]
QUERY_TOOLS = [fn for fn, _ in QUERY_TOOLS_AND_CHILD_TOOLS]


def _module_tools() -> list:
    from mcp_server_for_oscal.tools import get_tool_list

    return [fn for fn in get_tool_list() if fn.__module__ == query_oscal_models.__name__]


class TestToolSignatures:
    @pytest.mark.parametrize("tool_fn", QUERY_TOOLS, ids=lambda fn: fn.__name__)
    def test_query_tool_signature(self, tool_fn):
        sig = inspect.signature(tool_fn)
        assert list(sig.parameters) == ["query_type", "query_value", "offset", "limit", "ctx"]
        assert sig.parameters["query_type"].annotation == PerModelQueryType
        assert sig.parameters["query_type"].default == "all"
        assert sig.parameters["query_value"].annotation == QueryValue
        assert sig.parameters["offset"].annotation == Offset
        assert sig.parameters["limit"].annotation == Limit
        assert sig.parameters["limit"].default == 10

    @pytest.mark.parametrize("tool_fn", QUERY_TOOLS, ids=lambda fn: fn.__name__)
    def test_query_type_enum_excludes_by_type(self, tool_fn):
        schema = tool_fn.tool_spec["inputSchema"]["json"]
        assert schema["properties"]["query_type"]["enum"] == ["all", "by_uuid", "by_title"]

    def test_text_search_signature(self):
        sig = inspect.signature(query_oscal_models.text_search_oscal)
        assert next(iter(sig.parameters)) == "query_text"
        assert list(sig.parameters)[-1] == "ctx"
        assert sig.parameters["query_text"].default is inspect.Parameter.empty
        assert sig.parameters["oscal_model_type"].annotation == OptionalModelName
        assert sig.parameters["offset"].annotation == Offset
        assert sig.parameters["offset"].default == 0
        # Requirement 7.7: shared BudgetedLimit alias, name and default unchanged.
        assert sig.parameters["limit"].annotation == BudgetedLimit
        assert sig.parameters["limit"].default == 10
        assert "query_text" in query_oscal_models.text_search_oscal.tool_spec["inputSchema"][
            "json"
        ].get("required", [])

    def test_get_child_element_signature(self):
        sig = inspect.signature(query_oscal_models.get_child_element)
        assert list(sig.parameters) == ["element_id", "parent_doc_uuid", "ctx"]
        assert sig.parameters["element_id"].default is inspect.Parameter.empty
        assert sig.parameters["parent_doc_uuid"].annotation == ParentDocUuid

    def test_ctx_last_and_every_parameter_described(self):
        tools = _module_tools()
        assert len(tools) == 28
        for fn in tools:
            params = list(inspect.signature(fn).parameters)
            assert params[-1] == "ctx", fn.__name__
            props = fn.tool_spec["inputSchema"]["json"]["properties"]
            for name in params[:-1]:
                desc = props[name].get("description", "")
                assert desc.strip(), f"{fn.__name__}.{name}"
                assert len(desc) <= tool_budget.PARAMETER_DESCRIPTION_CAP


class TestToolDescriptions:
    def test_descriptions_fit_shape_and_size_target(self):
        for fn in _module_tools():
            doc = inspect.getdoc(fn) or ""
            assert tool_budget.description_shape_problems(doc) == [], fn.__name__
            assert len(" ".join(doc.split())) <= 180, fn.__name__

    @pytest.mark.parametrize(
        ("tool_fn", "child_tools"),
        QUERY_TOOLS_AND_CHILD_TOOLS,
        ids=[fn.tool_name for fn in QUERY_TOOLS],
    )
    def test_query_descriptions_direct_to_child_tools(self, tool_fn, child_tools):
        doc = inspect.getdoc(tool_fn) or ""
        assert "`childTypeCounts`" in doc
        assert "get_child_element" in doc
        for name in child_tools:
            assert name in doc


# ---------------------------------------------------------------------------
# Integration helpers — additional OSCAL document fixtures
# ---------------------------------------------------------------------------


def _make_poam(
    uuid="e5f6a7b8-9abc-4ef0-a1ab-567890123456",
    title="Test POA&M",
):
    """Minimal valid POA&M with poam-items."""
    return {
        "plan-of-action-and-milestones": {
            "uuid": uuid,
            "metadata": {
                "title": title,
                "last-modified": "2024-01-01T00:00:00Z",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
            "poam-items": [
                {
                    "uuid": "f6a7b8c9-abcd-4f01-a2ab-678901234567",
                    "title": "Fix vulnerability",
                    "description": "Remediate CVE-2024-0001",
                },
            ],
        }
    }


def _make_catalog_with_controls(
    uuid="d2e3f4a5-6789-4bcd-9ef0-bbccddeeff00",
    title="Security Controls Catalog",
):
    """Catalog with controls and a group for richer search content."""
    return {
        "catalog": {
            "uuid": uuid,
            "metadata": {
                "title": title,
                "last-modified": "2024-01-01T00:00:00Z",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
            "controls": [
                {"id": "ac-1", "title": "Access Control Policy"},
                {"id": "ac-2", "title": "Account Management"},
            ],
            "groups": [
                {"id": "ac", "title": "Access Control"},
            ],
        }
    }


# ---------------------------------------------------------------------------
# Integration fixture — multi-type store
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_type_store(tmp_path):
    """OscalStore populated with catalogs, component-definitions, and POA&Ms.

    Provides a richer dataset for integration-level tests that exercise
    the full flow across multiple document types.
    """
    db_path = str(tmp_path / "integration.db")
    s = OscalStore(db_path=db_path, cache_size=10, seed_from_bundled=False)

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    # Two catalogs
    cat1 = _make_catalog(
        uuid="c1d2e3f4-5678-4abc-8def-aabbccddeeff",
        title="Test Catalog",
    )
    (docs_dir / "catalog1.json").write_text(json.dumps(cat1))

    cat2 = _make_catalog_with_controls(
        uuid="d2e3f4a5-6789-4bcd-9ef0-bbccddeeff00",
        title="Security Controls Catalog",
    )
    (docs_dir / "catalog2.json").write_text(json.dumps(cat2))

    # One component-definition
    cdef = _make_component_definition(
        uuid="a1b2c3d4-5678-4abc-8def-123456789012",
        title="Test Component Definition",
    )
    (docs_dir / "cdef.json").write_text(json.dumps(cdef))

    # One POA&M
    poam = _make_poam(
        uuid="e5f6a7b8-9abc-4ef0-a1ab-567890123456",
        title="Test POA&M",
    )
    (docs_dir / "poam.json").write_text(json.dumps(poam))

    s.scan_directory(docs_dir)

    # Wire the module-level singleton
    query_oscal_models.init_store(s)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Integration tests — multi-type queries
# ---------------------------------------------------------------------------


class TestMultiTypeIntegration:
    """Integration tests exercising the full flow with multiple doc types."""

    # -- Catalog tools with real data --

    def test_list_catalogs_returns_both(self, multi_type_store):
        """list_catalogs returns exactly the two ingested catalogs."""
        result = query_oscal_models.list_catalogs(ctx=None)
        assert result["total"] == 2
        titles = {item["title"] for item in result["items"]}
        assert titles == {"Test Catalog", "Security Controls Catalog"}

    def test_query_catalog_by_uuid(self, multi_type_store):
        """query_catalog by_uuid returns the correct catalog."""
        result = query_oscal_models.query_catalog(
            ctx=None,
            query_type="by_uuid",
            query_value="d2e3f4a5-6789-4bcd-9ef0-bbccddeeff00",
        )
        assert result["total"] == 1
        assert result["items"][0]["title"] == "Security Controls Catalog"

    def test_query_catalog_by_title(self, multi_type_store):
        """query_catalog by_title finds the right catalog."""
        result = query_oscal_models.query_catalog(
            ctx=None,
            query_type="by_title",
            query_value="Test Catalog",
        )
        assert result["total"] == 1
        assert result["items"][0]["uuid"] == "c1d2e3f4-5678-4abc-8def-aabbccddeeff"

    def test_query_catalog_all_excludes_other_types(self, multi_type_store):
        """query_catalog all returns only catalogs, not cdefs or poams."""
        result = query_oscal_models.query_catalog(ctx=None, query_type="all")
        assert result["total"] == 2
        for item in result["items"]:
            assert item["model_type"] == "catalog"

    # -- POA&M tools with real data --

    def test_list_poams_returns_one(self, multi_type_store):
        """list_poams returns the single ingested POA&M."""
        result = query_oscal_models.list_poams(ctx=None)
        assert result["total"] == 1
        assert result["items"][0]["title"] == "Test POA&M"

    def test_query_poam_by_uuid(self, multi_type_store):
        """query_poam by_uuid returns the correct POA&M."""
        result = query_oscal_models.query_poam(
            ctx=None,
            query_type="by_uuid",
            query_value="e5f6a7b8-9abc-4ef0-a1ab-567890123456",
        )
        assert result["total"] == 1
        assert result["items"][0]["model_type"] == "plan-of-action-and-milestones"

    # -- Empty-type queries return empty results --

    def test_query_ssp_empty_with_multi_type_store(self, multi_type_store):
        """SSP queries return empty when no SSPs are loaded."""
        result = query_oscal_models.query_ssp(ctx=None, query_type="all")
        assert result["total"] == 0
        assert result["items"] == []

    def test_list_ssps_empty_with_multi_type_store(self, multi_type_store):
        """list_ssps returns empty when no SSPs are loaded."""
        result = query_oscal_models.list_ssps(ctx=None)
        assert result["total"] == 0
        assert result["items"] == []

    def test_list_profiles_empty_with_multi_type_store(self, multi_type_store):
        """list_profiles returns empty when no profiles are loaded."""
        result = query_oscal_models.list_profiles(ctx=None)
        assert result["total"] == 0

    def test_list_assessment_plans_empty_with_multi_type_store(self, multi_type_store):
        """list_assessment_plans returns empty when none loaded."""
        result = query_oscal_models.list_assessment_plans(ctx=None)
        assert result["total"] == 0

    def test_list_assessment_results_empty_with_multi_type_store(self, multi_type_store):
        """list_assessment_results returns empty when none loaded."""
        result = query_oscal_models.list_assessment_results(ctx=None)
        assert result["total"] == 0

    def test_list_mapping_collections_empty_with_multi_type_store(self, multi_type_store):
        """list_mapping_collections returns empty when none loaded."""
        result = query_oscal_models.list_mapping_collections(ctx=None)
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Integration tests — cross-model text search
# ---------------------------------------------------------------------------


class TestTextSearchIntegration:
    """Integration tests for text_search_oscal across multiple model types."""

    def test_text_search_finds_results_across_types(self, multi_type_store):
        """text_search_oscal returns results from multiple model types."""
        # Trigger indexing for all document types
        query_oscal_models.query_catalog(ctx=None, query_type="all")
        multi_type_store.query(
            oscal_model_type=OSCALModelType.COMPONENT_DEFINITION,
            query_type="all",
        )
        query_oscal_models.query_poam(ctx=None, query_type="all")

        # "Test" appears in titles across catalogs, cdef, and poam
        result = query_oscal_models.text_search_oscal(ctx=None, query_text="Test")
        assert result["total"] >= 3
        model_types = {item["model_type"] for item in result["items"]}
        # Should have results from at least two different model types
        assert len(model_types) >= 2

    def test_text_search_scoped_to_catalog(self, multi_type_store):
        """text_search_oscal scoped to catalog excludes other types."""
        # Trigger indexing
        query_oscal_models.query_catalog(ctx=None, query_type="all")
        multi_type_store.query(
            oscal_model_type=OSCALModelType.COMPONENT_DEFINITION,
            query_type="all",
        )
        query_oscal_models.query_poam(ctx=None, query_type="all")

        result = query_oscal_models.text_search_oscal(
            ctx=None,
            query_text="Test",
            oscal_model_type="catalog",
        )
        assert result["total"] >= 1
        for item in result["items"]:
            assert item["model_type"] == "catalog"

    def test_text_search_scoped_to_poam(self, multi_type_store):
        """text_search_oscal scoped to POA&M returns only POA&M results."""
        # Trigger indexing
        query_oscal_models.query_poam(ctx=None, query_type="all")

        result = query_oscal_models.text_search_oscal(
            ctx=None,
            query_text="vulnerability",
            oscal_model_type="plan-of-action-and-milestones",
        )
        assert result["total"] >= 1
        for item in result["items"]:
            assert item["model_type"] == "plan-of-action-and-milestones"

    def test_text_search_scoped_to_component_definition(self, multi_type_store):
        """text_search_oscal scoped to component-definition filters correctly."""
        # Trigger indexing
        multi_type_store.query(
            oscal_model_type=OSCALModelType.COMPONENT_DEFINITION,
            query_type="all",
        )

        result = query_oscal_models.text_search_oscal(
            ctx=None,
            query_text="Sample",
            oscal_model_type="component-definition",
        )
        assert result["total"] >= 1
        for item in result["items"]:
            assert item["model_type"] == "component-definition"

    def test_text_search_child_elements_indexed(self, multi_type_store):
        """text_search finds child element content (e.g. control titles)."""
        # Trigger indexing for the catalog with controls
        query_oscal_models.query_catalog(ctx=None, query_type="all")

        result = query_oscal_models.text_search_oscal(ctx=None, query_text="Access Control")
        assert result["total"] >= 1
        # At least one result should be a child_element from the catalog
        entity_types = {item["entity_type"] for item in result["items"]}
        assert "child_element" in entity_types

    def test_text_search_no_match_returns_empty(self, multi_type_store):
        """text_search with a term that matches nothing returns empty."""
        query_oscal_models.query_catalog(ctx=None, query_type="all")

        result = query_oscal_models.text_search_oscal(
            ctx=None, query_text="zzzznonexistenttermzzzz"
        )
        assert result["total"] == 0
        assert result["items"] == []


# ---------------------------------------------------------------------------
# Per-model query items carry no file paths (search-fetch-pairs, task 10.2)
# ---------------------------------------------------------------------------

_METADATA = {
    "title": "",
    "last-modified": "2024-01-01T00:00:00Z",
    "version": "1.0",
    "oscal-version": "1.0.4",
}


def _minimal_doc(root_key: str, uuid: str, title: str, body: dict) -> dict:
    return {root_key: {"uuid": uuid, "metadata": {**_METADATA, "title": title}, **body}}


_SELECTION: dict[str, object] = {"control-selections": [{"include-all": {}}]}
_RESOURCE = {"type": "catalog", "href": "https://example.com/catalog.json"}

# query tool -> (root key, document body without uuid/metadata) for one valid document per model
_PER_MODEL_BODIES = {
    query_oscal_models.query_catalog: ("catalog", {}),
    query_oscal_models.query_profile: (
        "profile",
        {"imports": [{"href": "#catalog", "include-all": {}}]},
    ),
    query_oscal_models.query_ssp: (
        "system-security-plan",
        {
            "import-profile": {"href": "#profile"},
            "system-characteristics": {
                "system-ids": [{"id": "sys-1"}],
                "system-name": "Test System",
                "description": "Test system",
                "system-information": {
                    "information-types": [{"title": "Info", "description": "Info type"}]
                },
                "status": {"state": "operational"},
                "authorization-boundary": {"description": "Boundary"},
            },
            "system-implementation": {
                "users": [{"uuid": "11111111-1111-4111-8111-111111111111"}],
                "components": [
                    {
                        "uuid": "22222222-2222-4222-8222-222222222222",
                        "type": "this-system",
                        "title": "This System",
                        "description": "The system",
                        "status": {"state": "operational"},
                    }
                ],
            },
            "control-implementation": {
                "description": "Implementation",
                "implemented-requirements": [
                    {"uuid": "33333333-3333-4333-8333-333333333333", "control-id": "ac-1"}
                ],
            },
        },
    ),
    query_oscal_models.query_assessment_plan: (
        "assessment-plan",
        {"import-ssp": {"href": "#ssp"}, "reviewed-controls": _SELECTION},
    ),
    query_oscal_models.query_assessment_results: (
        "assessment-results",
        {
            "import-ap": {"href": "#ap"},
            "results": [
                {
                    "uuid": "44444444-4444-4444-8444-444444444444",
                    "title": "Result",
                    "description": "Result",
                    "start": "2024-01-01T00:00:00Z",
                    "reviewed-controls": _SELECTION,
                }
            ],
        },
    ),
    query_oscal_models.query_poam: (
        "plan-of-action-and-milestones",
        {
            "poam-items": [
                {
                    "uuid": "55555555-5555-4555-8555-555555555555",
                    "title": "Fix",
                    "description": "Fix it",
                }
            ]
        },
    ),
    query_oscal_models.query_mapping_collection: (
        "mapping-collection",
        {
            "provenance": {
                "method": "human",
                "matching-rationale": "semantic",
                "status": "complete",
                "mapping-description": "Test provenance",
            },
            "mappings": [
                {
                    "uuid": "66666666-6666-4666-8666-666666666666",
                    "source-resource": _RESOURCE,
                    "target-resource": _RESOURCE,
                    "maps": [
                        {
                            "uuid": "77777777-7777-4777-8777-777777777777",
                            "relationship": "equivalent-to",
                            "sources": [{"type": "control", "id-ref": "ac-1"}],
                            "targets": [{"type": "control", "id-ref": "ac-1"}],
                        }
                    ],
                }
            ],
        },
    ),
}

_ITEM_KEYS = {"uuid", "title", "model_type", "sizeInBytes", "childTypeCounts"}


def _doc_uuid(index: int) -> str:
    return f"{index + 1:08x}-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _doc_title(root_key: str) -> str:
    return f"Pathless {root_key} document"


def _strings(value):
    """Yield every string (keys and values) inside a JSON-like value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


@pytest.fixture
def all_models_store(tmp_path):
    """Store with one valid document per per-model query tool, plus a Component Definition.

    Yields ``(store, docs_dir, db_path)`` with the store installed for both tool modules.
    """
    from mcp_server_for_oscal.tools import query_component_definition

    db_path = tmp_path / "all-models.db"
    s = OscalStore(db_path=str(db_path), cache_size=10, seed_from_bundled=False)
    docs_dir = tmp_path / "scan-root"
    docs_dir.mkdir()
    for i, (root_key, body) in enumerate(_PER_MODEL_BODIES.values()):
        doc = _minimal_doc(root_key, _doc_uuid(i), _doc_title(root_key), body)
        (docs_dir / f"{root_key}.json").write_text(json.dumps(doc))
    (docs_dir / "cdef.json").write_text(json.dumps(_make_component_definition()))
    s.scan_directory(docs_dir)
    query_oscal_models.init_store(s)
    query_component_definition.init_store(s)
    yield s, docs_dir, db_path
    s.close()


def _assert_pathless(result, docs_dir, db_path):
    forbidden = [str(docs_dir), str(db_path), docs_dir.as_posix(), str(docs_dir.parent)]
    for text in _strings(result):
        for path in forbidden:
            assert path not in text, f"{path!r} leaked in {text!r}"


class TestPerModelItemsHaveNoFilePath:
    """Requirement 3.2: per-model query items have no ``file_path`` and no store paths."""

    @pytest.mark.parametrize("tool_fn", list(_PER_MODEL_BODIES), ids=lambda fn: fn.__name__)
    @pytest.mark.parametrize("query_type", ["all", "by_uuid", "by_title"])
    def test_items_have_exact_keys_and_no_paths(self, all_models_store, tool_fn, query_type):
        _, docs_dir, db_path = all_models_store
        index = list(_PER_MODEL_BODIES).index(tool_fn)
        root_key = _PER_MODEL_BODIES[tool_fn][0]
        query_value = {
            "all": None,
            "by_uuid": _doc_uuid(index),
            "by_title": _doc_title(root_key),
        }[query_type]

        result = tool_fn(query_type=query_type, query_value=query_value, ctx=None)

        assert len(result["items"]) == 1, result
        item = result["items"][0]
        assert set(item) == _ITEM_KEYS
        assert item["uuid"] == _doc_uuid(index)
        assert item["model_type"] == root_key
        _assert_pathless(result, docs_dir, db_path)

    def test_component_definition_query_results_unchanged(self, all_models_store):
        """``query_component_definition`` keeps its response shape; components are OSCAL JSON."""
        from mcp_server_for_oscal.tools.query_component_definition import (
            query_component_definition,
        )

        _, docs_dir, db_path = all_models_store
        cdef = _make_component_definition()["component-definition"]

        result = query_component_definition(
            component_definition_filter=cdef["uuid"], query_type="all", ctx=None
        )

        assert set(result) == {
            "components",
            "total_count",
            "offset",
            "limit",
            "hasMore",
            "query_type",
            "component_definitions_searched",
            "filtered_by",
        }
        assert result["total_count"] == 1
        assert result["component_definitions_searched"] == 1
        assert result["filtered_by"] == cdef["uuid"]
        assert result["components"] == cdef["components"]
        _assert_pathless(result, docs_dir, db_path)
