"""
Property-based tests for the ``list_child_elements`` tool in query_oscal_models.py.

Feature: query-oscal-models, migrated by consolidate-tools (GitHub issue #40). The 12 per-model
child listers were replaced by one tool whose ``element_type`` argument takes the 14 Element_Type
values, so each property is stated over those values.
"""

import inspect
import json
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools import query_oscal_models
from mcp_server_for_oscal.tools.descriptions import (
    ChildElementType,
    Limit,
    Offset,
    ParentDocUuid,
)
from mcp_server_for_oscal.tools.oscal_store import CHILD_ELEMENT_TYPES, OscalStore

from ..fixture_store import build_leak_store

# ---------------------------------------------------------------------------
# The 14 Element_Type values
# ---------------------------------------------------------------------------

ELEMENT_TYPES: list[str] = [
    "component",
    "capability",
    "control",
    "group",
    "import",
    "modify",
    "control-implementation",
    "system-component",
    "task",
    "activity",
    "result",
    "finding",
    "poam-item",
    "mapping",
]

TOOL = query_oscal_models.list_child_elements


def test_element_types_match_the_store_constants():
    """The literal list above equals the store's Element_Types (14 values, none repeated)."""
    from_store = [t for types in CHILD_ELEMENT_TYPES.values() for t in types]
    assert len(ELEMENT_TYPES) == 14
    assert sorted(ELEMENT_TYPES) == sorted(from_store)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def fixture_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[OscalStore]:
    """Read-only store with a document of every model type and a child of every Element_Type."""
    leak = build_leak_store(tmp_path_factory.mktemp("child-element-properties"))
    try:
        leak.store.text_search(leak.text_query)  # indexes pending documents (child elements)
        yield leak.store
    finally:
        leak.store.close()


@pytest.fixture
def installed_store(reset_oscal_store: None, fixture_store: OscalStore) -> OscalStore:
    """Install the shared store into ``query_oscal_models`` for one test.

    ``reset_oscal_store`` (autouse, function-scoped) clears the singleton again afterwards.
    """
    query_oscal_models.init_store(fixture_store)
    return fixture_store


# ---------------------------------------------------------------------------
# Property 1: Correct delegation
# Tag: Feature: query-oscal-models, Property 1: Correct delegation
# Validates: Requirements 3.6 (consolidate-tools)
# ---------------------------------------------------------------------------


class TestProperty1CorrectDelegation:
    """``list_child_elements`` delegates to ``OscalStore.list_child_elements`` with the given
    element_type and passes every other parameter through unchanged.

    **Validates: Requirements 3.6**
    """

    @settings(max_examples=100, deadline=None)
    @given(
        element_type=st.sampled_from(ELEMENT_TYPES),
        parent_doc_uuid=st.one_of(st.none(), st.uuids().map(str)),
        offset=st.integers(min_value=0, max_value=1000),
        limit=st.integers(min_value=1, max_value=100),
    )
    def test_delegates_with_given_element_type(self, element_type, parent_doc_uuid, offset, limit):
        sentinel = {"items": [], "total": 0, "offset": offset, "limit": limit, "hasMore": False}
        with patch.object(OscalStore, "list_child_elements", return_value=sentinel) as mock_lce:
            saved = query_oscal_models._store
            try:
                query_oscal_models._store = OscalStore.__new__(OscalStore)
                result = TOOL(
                    element_type=element_type,
                    parent_doc_uuid=parent_doc_uuid,
                    offset=offset,
                    limit=limit,
                    ctx=None,
                )
            finally:
                query_oscal_models._store = saved

            # The tool asks the store for raw_json so it can compute sizeInBytes.
            mock_lce.assert_called_once_with(
                ctx=None,
                parent_doc_uuid=parent_doc_uuid,
                element_type=element_type,
                offset=offset,
                limit=limit,
                include_raw_json=True,
            )
            assert result == sentinel

    @settings(max_examples=50, deadline=None)
    @given(
        element_type=st.sampled_from(ELEMENT_TYPES),
        raw_jsons=st.lists(st.one_of(st.none(), st.text(max_size=20)), max_size=5),
    )
    def test_items_are_the_store_items_without_raw_json(self, element_type, raw_jsons):
        items = [
            {"id": f"id-{i}", "title": f"T{i}", "element_type": element_type, "raw_json": raw}
            for i, raw in enumerate(raw_jsons)
        ]
        page = {"items": items, "total": len(items), "offset": 0, "limit": 10, "hasMore": False}
        with patch.object(OscalStore, "list_child_elements", return_value=page):
            saved = query_oscal_models._store
            try:
                query_oscal_models._store = OscalStore.__new__(OscalStore)
                result = TOOL(element_type=element_type, ctx=None)
            finally:
                query_oscal_models._store = saved

        assert {k: v for k, v in result.items() if k != "items"} == {
            k: v for k, v in page.items() if k != "items"
        }
        assert [{k: v for k, v in i.items() if k != "sizeInBytes"} for i in result["items"]] == [
            {k: v for k, v in i.items() if k != "raw_json"} for i in items
        ]
        assert all("raw_json" not in i for i in result["items"])


# ---------------------------------------------------------------------------
# Property 2: Consistent interface signature
# Tag: Feature: query-oscal-models, Property 2: Consistent interface signature
# Validates: Requirements 3.6 (consolidate-tools)
# ---------------------------------------------------------------------------


class TestProperty2ConsistentInterfaceSignature:
    """Verify the tool has the expected parameter names, types, and defaults using
    inspect.signature().

    **Validates: Requirements 3.6**
    """

    def test_signature_params(self):
        sig = inspect.signature(TOOL)
        assert list(sig.parameters) == ["element_type", "parent_doc_uuid", "offset", "limit", "ctx"]

        # element_type: ChildElementType, required (identifies what to list)
        p_element = sig.parameters["element_type"]
        assert p_element.default is inspect.Parameter.empty
        assert p_element.annotation == ChildElementType

        # ctx: Context | None = None, last
        assert sig.parameters["ctx"].default is None

        # parent_doc_uuid: ParentDocUuid = None
        p_parent = sig.parameters["parent_doc_uuid"]
        assert p_parent.default is None
        assert p_parent.annotation == ParentDocUuid

        # offset: Offset = 0
        p_offset = sig.parameters["offset"]
        assert p_offset.default == 0
        assert p_offset.annotation == Offset

        # limit: Limit = 10
        p_limit = sig.parameters["limit"]
        assert p_limit.default == 10
        assert p_limit.annotation == Limit

    def test_has_docstring(self):
        assert TOOL.__doc__ is not None
        assert len(TOOL.__doc__.strip()) > 0


# ---------------------------------------------------------------------------
# Property 3: Response format contract
# Tag: Feature: query-oscal-models, Property 3: Response format contract
# Validates: Requirements 3.6 (consolidate-tools)
# ---------------------------------------------------------------------------

EXPECTED_PAGE_KEYS = {"items", "total", "offset", "limit", "hasMore"}
EXPECTED_ITEM_KEYS = {
    "id",
    "title",
    "element_type",
    "description",
    "parentDocumentTitle",
    "parentDocumentUuid",
    "sizeInBytes",
}


def _assert_response_format(result: dict, element_type: str) -> None:
    """Check the envelope, the item keys, and that the result is plain JSON."""
    assert set(result) == EXPECTED_PAGE_KEYS
    assert isinstance(result["items"], list)
    assert isinstance(result["total"], int)
    assert isinstance(result["offset"], int)
    assert isinstance(result["limit"], int)
    assert isinstance(result["hasMore"], bool)
    assert result["hasMore"] == (result["offset"] + result["limit"] < result["total"])
    assert len(result["items"]) <= result["limit"]
    for item in result["items"]:
        assert set(item) == EXPECTED_ITEM_KEYS
        assert item["element_type"] == element_type
        assert isinstance(item["id"], str)
        assert isinstance(item["parentDocumentUuid"], str)
        assert isinstance(item["sizeInBytes"], int)
        assert item["sizeInBytes"] > 0
    assert result == json.loads(json.dumps(result))


class TestProperty3ResponseFormatContract:
    """Call the tool against a real OscalStore with sample data and verify the response
    envelope and item keys for every Element_Type.

    **Validates: Requirements 3.6**
    """

    @pytest.mark.parametrize("element_type", ELEMENT_TYPES)
    def test_response_keys(self, installed_store, element_type):
        result = TOOL(element_type=element_type, ctx=None)

        _assert_response_format(result, element_type)
        # The fixture store holds a child of every Element_Type, so the item checks ran.
        assert result["total"] >= 1
        assert result["items"]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        element_type=st.sampled_from(ELEMENT_TYPES),
        offset=st.integers(min_value=0, max_value=5),
        limit=st.integers(min_value=1, max_value=100),
    )
    def test_response_keys_for_any_page(self, installed_store, element_type, offset, limit):
        result = TOOL(element_type=element_type, offset=offset, limit=limit, ctx=None)

        _assert_response_format(result, element_type)
        assert result["offset"] == offset
        assert result["limit"] == limit
