"""
Unit tests for the child element tools in query_oscal_models.py.

Covers:
- list_child_elements(element_type=...) against real OscalStore data, including sizeInBytes
- get_child_element tool (UUID, token ID, ambiguity, not found)
- tool registration: the module's 5 tools and the registry's 19 (consolidate-tools, #40)
"""

import json

import pytest

from mcp_server_for_oscal.tools import query_oscal_models
from mcp_server_for_oscal.tools.descriptions import ChildElementType
from mcp_server_for_oscal.tools.oscal_store import OscalStore

# ---------------------------------------------------------------------------
# Helpers — minimal valid OSCAL documents
# ---------------------------------------------------------------------------


def _make_catalog_with_controls(
    uuid="d2e3f4a5-6789-4bcd-9ef0-bbccddeeff00",
    title="Security Controls Catalog",
):
    """Catalog with controls and a group."""
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


def _make_catalog_simple(
    uuid="c1d2e3f4-5678-4abc-8def-aabbccddeeff",
    title="Test Catalog",
):
    """Catalog with no controls or groups."""
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_type_store(tmp_path):
    """OscalStore populated with a catalog (with controls/groups) and a POA&M.

    Provides data for testing list child element tools and get_child_element.
    """
    db_path = str(tmp_path / "child_elements.db")
    s = OscalStore(db_path=db_path, cache_size=10, seed_from_bundled=False)

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    cat = _make_catalog_with_controls()
    (docs_dir / "catalog.json").write_text(json.dumps(cat))

    poam = _make_poam()
    (docs_dir / "poam.json").write_text(json.dumps(poam))

    s.scan_directory(docs_dir)

    query_oscal_models.init_store(s)
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Task 6.1: Unit tests for list child element tools
# ---------------------------------------------------------------------------

PAGE_RESPONSE_KEYS = {"items", "total", "offset", "limit", "hasMore"}
ITEM_REQUIRED_KEYS = {
    "id",
    "title",
    "element_type",
    "description",
    "parentDocumentTitle",
    "parentDocumentUuid",
    "sizeInBytes",
}


def _list(element_type: ChildElementType, **kwargs) -> dict:
    """Call list_child_elements for ``element_type`` with the test store installed."""
    return query_oscal_models.list_child_elements(element_type=element_type, ctx=None, **kwargs)


@pytest.mark.unit
class TestListCatalogControls:
    """list_child_elements(element_type="control") returns controls from the catalog."""

    def test_returns_controls(self, multi_type_store):
        result = _list("control")
        assert result["total"] == 2
        titles = {item["title"] for item in result["items"]}
        assert titles == {"Access Control Policy", "Account Management"}

    def test_response_has_page_response_keys(self, multi_type_store):
        result = _list("control")
        assert set(result.keys()) == PAGE_RESPONSE_KEYS

    def test_items_use_id_key_not_uuid(self, multi_type_store):
        result = _list("control")
        for item in result["items"]:
            assert "id" in item
            assert "uuid" not in item

    def test_items_include_all_required_keys(self, multi_type_store):
        result = _list("control")
        for item in result["items"]:
            assert set(item.keys()) == ITEM_REQUIRED_KEYS

    def test_items_have_no_raw_json(self, multi_type_store):
        result = _list("control")
        for item in result["items"]:
            assert "raw_json" not in item

    def test_items_have_correct_element_type(self, multi_type_store):
        result = _list("control")
        for item in result["items"]:
            assert item["element_type"] == "control"

    def test_items_have_parent_document_info(self, multi_type_store):
        result = _list("control")
        for item in result["items"]:
            assert item["parentDocumentTitle"] == "Security Controls Catalog"
            assert item["parentDocumentUuid"] == "d2e3f4a5-6789-4bcd-9ef0-bbccddeeff00"

    def test_size_in_bytes_is_utf8_length_of_stored_json(self, multi_type_store):
        for item in _list("control")["items"]:
            full = query_oscal_models.get_child_element(
                ctx=None,
                element_id=item["id"],
                parent_doc_uuid=item["parentDocumentUuid"],
            )
            assert item["sizeInBytes"] > 0
            assert item["sizeInBytes"] == len(full["raw_json"].encode("utf-8"))

    def test_parent_doc_uuid_scopes_the_listing(self, multi_type_store):
        scoped = _list("control", parent_doc_uuid="d2e3f4a5-6789-4bcd-9ef0-bbccddeeff00")
        assert scoped["total"] == 2
        other = _list("control", parent_doc_uuid="e5f6a7b8-9abc-4ef0-a1ab-567890123456")
        assert other["total"] == 0
        assert other["items"] == []


@pytest.mark.unit
class TestListCatalogGroups:
    """list_child_elements(element_type="group") returns groups from the catalog."""

    def test_returns_groups(self, multi_type_store):
        result = _list("group")
        assert result["total"] == 1
        assert result["items"][0]["title"] == "Access Control"
        assert result["items"][0]["id"] == "ac"

    def test_items_have_correct_element_type(self, multi_type_store):
        result = _list("group")
        for item in result["items"]:
            assert item["element_type"] == "group"

    def test_items_include_all_required_keys(self, multi_type_store):
        for item in _list("group")["items"]:
            assert set(item.keys()) == ITEM_REQUIRED_KEYS
            assert item["sizeInBytes"] > 0


@pytest.mark.unit
class TestListPoamItems:
    """list_child_elements(element_type="poam-item") returns POA&M items."""

    def test_returns_poam_items(self, multi_type_store):
        result = _list("poam-item")
        assert result["total"] == 1
        assert result["items"][0]["title"] == "Fix vulnerability"

    def test_items_have_correct_element_type(self, multi_type_store):
        result = _list("poam-item")
        for item in result["items"]:
            assert item["element_type"] == "poam-item"

    def test_items_include_all_required_keys(self, multi_type_store):
        result = _list("poam-item")
        for item in result["items"]:
            assert set(item.keys()) == ITEM_REQUIRED_KEYS
            assert item["sizeInBytes"] > 0


@pytest.mark.unit
class TestListChildElementToolsEmptyResults:
    """list_child_elements returns an empty Page_Response when no matching data exists."""

    def test_ssp_control_implementations_empty(self, multi_type_store):
        result = _list("control-implementation")
        assert result["total"] == 0
        assert result["items"] == []
        assert set(result.keys()) == PAGE_RESPONSE_KEYS

    @pytest.mark.parametrize(
        "element_type",
        [
            "system-component",
            "import",
            "modify",
            "task",
            "activity",
            "result",
            "finding",
            "mapping",
            "component",
            "capability",
        ],
    )
    def test_element_type_without_documents_is_empty(self, multi_type_store, element_type):
        result = _list(element_type)
        assert result["total"] == 0
        assert result["items"] == []
        assert result["hasMore"] is False
        assert set(result.keys()) == PAGE_RESPONSE_KEYS


# ---------------------------------------------------------------------------
# Task 6.2: Unit tests for get_child_element tool
# ---------------------------------------------------------------------------


@pytest.fixture
def ambiguous_store(tmp_path):
    """OscalStore with two catalogs that both contain control 'ac-1'.

    Used to test ambiguity detection in get_child_element.
    """
    db_path = str(tmp_path / "ambiguous.db")
    s = OscalStore(db_path=db_path, cache_size=10, seed_from_bundled=False)

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    cat1 = {
        "catalog": {
            "uuid": "aaaa1111-1111-4111-8111-111111111111",
            "metadata": {
                "title": "Catalog Alpha",
                "last-modified": "2024-01-01T00:00:00Z",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
            "controls": [
                {"id": "ac-1", "title": "Access Control Policy Alpha"},
            ],
        }
    }
    (docs_dir / "cat_alpha.json").write_text(json.dumps(cat1))

    cat2 = {
        "catalog": {
            "uuid": "bbbb2222-2222-4222-8222-222222222222",
            "metadata": {
                "title": "Catalog Beta",
                "last-modified": "2024-01-01T00:00:00Z",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
            "controls": [
                {"id": "ac-1", "title": "Access Control Policy Beta"},
            ],
        }
    }
    (docs_dir / "cat_beta.json").write_text(json.dumps(cat2))

    s.scan_directory(docs_dir)

    query_oscal_models.init_store(s)
    yield s
    s.close()


@pytest.mark.unit
class TestGetChildElement:
    """Tests for the get_child_element tool function."""

    def test_get_by_uuid_with_parent(self, multi_type_store):
        """Get a POA&M item by UUID with parent_doc_uuid."""
        result = query_oscal_models.get_child_element(
            ctx=None,
            element_id="f6a7b8c9-abcd-4f01-a2ab-678901234567",
            parent_doc_uuid="e5f6a7b8-9abc-4ef0-a1ab-567890123456",
        )
        assert result is not None
        assert result["id"] == "f6a7b8c9-abcd-4f01-a2ab-678901234567"
        assert result["title"] == "Fix vulnerability"
        assert result["element_type"] == "poam-item"

    def test_get_by_token_id_with_parent(self, multi_type_store):
        """Get a catalog control by token ID with parent_doc_uuid."""
        result = query_oscal_models.get_child_element(
            ctx=None,
            element_id="ac-1",
            parent_doc_uuid="d2e3f4a5-6789-4bcd-9ef0-bbccddeeff00",
        )
        assert result is not None
        assert result["id"] == "ac-1"
        assert result["title"] == "Access Control Policy"
        assert result["parentDocumentUuid"] == "d2e3f4a5-6789-4bcd-9ef0-bbccddeeff00"

    def test_get_by_token_id_without_parent_unique(self, multi_type_store):
        """Get by token ID without parent — unique match returns element."""
        result = query_oscal_models.get_child_element(
            ctx=None,
            element_id="ac-1",
        )
        assert result is not None
        assert result["id"] == "ac-1"
        assert result["title"] == "Access Control Policy"

    def test_get_by_token_id_without_parent_ambiguous(self, ambiguous_store):
        """Get by token ID without parent — ambiguous returns error dict."""
        result = query_oscal_models.get_child_element(
            ctx=None,
            element_id="ac-1",
        )
        assert result is not None
        assert result["error"] == "ambiguous_element_id"
        assert result["element_id"] == "ac-1"
        assert len(result["matching_documents"]) == 2
        assert "aaaa1111-1111-4111-8111-111111111111" in result["matching_documents"]
        assert "bbbb2222-2222-4222-8222-222222222222" in result["matching_documents"]

    def test_get_not_found_returns_none(self, multi_type_store):
        """get_child_element returns None for nonexistent element_id."""
        result = query_oscal_models.get_child_element(
            ctx=None,
            element_id="nonexistent-id-12345",
        )
        assert result is None

    def test_response_includes_raw_json(self, multi_type_store):
        """get_child_element response includes raw_json key."""
        result = query_oscal_models.get_child_element(
            ctx=None,
            element_id="ac-1",
            parent_doc_uuid="d2e3f4a5-6789-4bcd-9ef0-bbccddeeff00",
        )
        assert result is not None
        assert "raw_json" in result
        assert result["raw_json"] is not None


# ---------------------------------------------------------------------------
# Task 6.3: Unit test for tool registration
# ---------------------------------------------------------------------------

GENERIC_TOOL_NAMES = {"list_documents", "query_documents", "list_child_elements"}
MODULE_TOOL_NAMES = GENERIC_TOOL_NAMES | {"text_search_oscal", "get_child_element"}
REGISTRY_TOOL_COUNT = 19  # 20 with the server's `about`


@pytest.mark.unit
class TestToolRegistration:
    """Verify the module defines exactly 5 tools and the registry exactly 19."""

    def test_module_defines_exactly_the_five_tools(self):
        defined = {
            name
            for name, obj in vars(query_oscal_models).items()
            if hasattr(obj, "tool_spec")
            and getattr(obj, "__module__", None) == query_oscal_models.__name__
        }
        assert defined == MODULE_TOOL_NAMES

    def test_all_module_tools_registered(self):
        from mcp_server_for_oscal.tools import get_tool_list

        tool_names = [fn.__name__ for fn in get_tool_list()]
        for name in sorted(MODULE_TOOL_NAMES):
            assert name in tool_names, f"{name} not found in get_tool_list()"

    def test_registry_has_exactly_19_tools(self):
        from mcp_server_for_oscal.tools import get_tool_list

        tool_list = get_tool_list()
        tool_names = {fn.__name__ for fn in tool_list}
        registered_here = {
            fn.__name__ for fn in tool_list if fn.__module__ == query_oscal_models.__name__
        }
        assert registered_here == MODULE_TOOL_NAMES
        assert len(tool_list) == len(tool_names) == REGISTRY_TOOL_COUNT
