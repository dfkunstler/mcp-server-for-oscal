"""
Property-based tests for the Generic_Tools and get_child_element (Properties 4–6).

Property 4: Store dependency enforcement (list_documents, query_documents, list_child_elements)
Property 5: Get tool — unambiguous lookup
Property 6: Get tool — ambiguity detection
"""

import json
from collections.abc import Callable
from functools import partial
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools import query_oscal_models
from mcp_server_for_oscal.tools.oscal_store import CHILD_ELEMENT_TYPES, OscalStore
from mcp_server_for_oscal.tools.utils import OSCALModelType

# ---------------------------------------------------------------------------
# Generic_Tool calls, one per Model_Type and Element_Type
# ---------------------------------------------------------------------------

_STORE_PATH = "mcp_server_for_oscal.tools.query_oscal_models._store"

# The Generic_Tools are also called with deliberately invalid arguments, so type them as Any.
_generic: Any = query_oscal_models

# One valid call per Generic_Tool and per Model_Type or Element_Type. The enum argument is
# valid in each case, so only the unset store can make the call fail.
GENERIC_CALLS: list[tuple[str, Callable[[], object]]] = [
    *(
        (f"{name}-{model.value}", partial(getattr(_generic, name), model_type=model.value))
        for name in ("list_documents", "query_documents")
        for model in OSCALModelType
    ),
    *(
        (f"list_child_elements-{element}", partial(_generic.list_child_elements, element))
        for types in CHILD_ELEMENT_TYPES.values()
        for element in types
    ),
]

# One call per Generic_Tool with an invalid argument.
INVALID_CALLS: list[tuple[str, Callable[[], object]]] = [
    ("list_documents", partial(_generic.list_documents, model_type="nope")),
    ("query_documents", partial(_generic.query_documents, model_type="nope")),
    ("list_child_elements", partial(_generic.list_child_elements, element_type="nope")),
    (
        "query_documents-query_type",
        partial(_generic.query_documents, model_type="catalog", query_type="nope"),
    ),
    (
        "query_documents-query_value",
        partial(_generic.query_documents, model_type="catalog", query_type="by_uuid"),
    ),
    ("list_documents-offset", partial(_generic.list_documents, "catalog", offset=-1)),
    ("list_child_elements-limit", partial(_generic.list_child_elements, "control", limit=0)),
]


# ---------------------------------------------------------------------------
# Property 4: Store dependency enforcement
# Validates: Requirements 4.4, 9.1
# ---------------------------------------------------------------------------


class TestStoreDependencyEnforcement:
    """Feature: consolidate-tools, Property 4: Store dependency enforcement"""

    @pytest.mark.parametrize(("_name", "call"), GENERIC_CALLS, ids=[n for n, _ in GENERIC_CALLS])
    def test_generic_tool_raises_when_store_is_none(self, _name, call):
        """Each Generic_Tool raises RuntimeError naming init_store when _store is None."""
        with patch(_STORE_PATH, None), pytest.raises(RuntimeError, match="init_store"):
            call()

    @pytest.mark.parametrize(("_name", "call"), INVALID_CALLS, ids=[n for n, _ in INVALID_CALLS])
    def test_invalid_arguments_rejected_before_store_is_read(self, _name, call):
        """An invalid argument raises ValueError, not RuntimeError, when _store is None."""
        with patch(_STORE_PATH, None), pytest.raises(ValueError):
            call()

    @settings(max_examples=25, deadline=None)
    @given(
        model_type=st.sampled_from([m.value for m in OSCALModelType]),
        offset=st.integers(min_value=0, max_value=30),
        limit=st.integers(min_value=1, max_value=100),
    )
    def test_valid_pages_reach_the_store_check(self, model_type, offset, limit):
        """Any valid model type and page passes argument checks, then fails on the unset store."""
        with patch(_STORE_PATH, None):
            with pytest.raises(RuntimeError, match="init_store"):
                query_oscal_models.list_documents(model_type, offset=offset, limit=limit)
            with pytest.raises(RuntimeError, match="init_store"):
                query_oscal_models.query_documents(model_type, offset=offset, limit=limit)

    def test_get_child_element_raises_when_store_is_none(self):
        """get_child_element raises RuntimeError when _store is None."""
        with patch("mcp_server_for_oscal.tools.query_oscal_models._store", None):
            with pytest.raises(RuntimeError):
                query_oscal_models.get_child_element(element_id="test-id")


# ---------------------------------------------------------------------------
# Property 5: Get tool — unambiguous lookup
# Validates: Requirements 11.1, 11.2, 11.5, 11.6
# ---------------------------------------------------------------------------


class TestGetToolUnambiguousLookup:
    """Feature: query-oscal-models, Property 5: Get tool unambiguous lookup"""

    @settings(max_examples=100)
    @given(
        element_id=st.text(min_size=1, max_size=50),
        parent_doc_uuid=st.one_of(st.none(), st.uuids().map(str)),
    )
    def test_get_child_element_delegates_correctly(self, element_id, parent_doc_uuid):
        """get_child_element passes element_id and parent_doc_uuid to the store."""
        mock_store = MagicMock(spec=OscalStore)
        mock_store.get_child_element.return_value = None

        with patch(
            "mcp_server_for_oscal.tools.query_oscal_models._get_store",
            return_value=mock_store,
        ):
            result = query_oscal_models.get_child_element(
                element_id=element_id,
                parent_doc_uuid=parent_doc_uuid,
            )

        mock_store.get_child_element.assert_called_once_with(
            element_id=element_id,
            parent_doc_uuid=parent_doc_uuid,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Helpers — minimal catalogs with overlapping control IDs
# ---------------------------------------------------------------------------


def _make_catalog(uuid, title, control_ids):
    """Build a minimal catalog with the given control IDs."""
    return {
        "catalog": {
            "uuid": uuid,
            "metadata": {
                "title": title,
                "last-modified": "2024-01-01T00:00:00Z",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
            "controls": [{"id": cid, "title": f"Control {cid}"} for cid in control_ids],
        }
    }


# ---------------------------------------------------------------------------
# Property 6: Get tool — ambiguity detection
# Validates: Requirements 11.3, 11.4
# ---------------------------------------------------------------------------


class TestGetToolAmbiguityDetection:
    """Feature: query-oscal-models, Property 6: Get tool ambiguity detection"""

    @pytest.fixture
    def ambiguous_store(self, tmp_path):
        """Store with two catalogs that both contain control 'ac-1'."""
        db_path = str(tmp_path / "ambiguous.db")
        s = OscalStore(db_path=db_path, cache_size=10, seed_from_bundled=False)

        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()

        cat1_uuid = "aaaa1111-1111-4111-8111-111111111111"
        cat2_uuid = "bbbb2222-2222-4222-8222-222222222222"

        cat1 = _make_catalog(cat1_uuid, "Catalog A", ["ac-1", "ac-2"])
        cat2 = _make_catalog(cat2_uuid, "Catalog B", ["ac-1", "sc-1"])

        (docs_dir / "cat1.json").write_text(json.dumps(cat1))
        (docs_dir / "cat2.json").write_text(json.dumps(cat2))

        s.scan_directory(docs_dir)

        query_oscal_models.init_store(s)
        yield s, cat1_uuid, cat2_uuid
        s.close()

    def test_ambiguous_element_returns_error_dict(self, ambiguous_store):
        """Calling get_child_element without parent on an overlapping ID
        returns an ambiguity error listing both parent UUIDs."""
        _store, cat1_uuid, cat2_uuid = ambiguous_store

        result = query_oscal_models.get_child_element(element_id="ac-1")

        assert result is not None
        assert result["error"] == "ambiguous_element_id"
        assert result["element_id"] == "ac-1"
        assert set(result["matching_documents"]) == {cat1_uuid, cat2_uuid}

    def test_unique_element_returns_element(self, ambiguous_store):
        """Calling get_child_element without parent on a unique ID
        returns the element (not an error)."""
        _store, _cat1_uuid, cat2_uuid = ambiguous_store

        result = query_oscal_models.get_child_element(element_id="sc-1")

        assert result is not None
        assert "error" not in result
        assert result["id"] == "sc-1"
        assert result["parentDocumentUuid"] == cat2_uuid
        assert "raw_json" in result

    def test_disambiguated_with_parent_returns_element(self, ambiguous_store):
        """Providing parent_doc_uuid resolves ambiguity for overlapping IDs."""
        _store, cat1_uuid, _cat2_uuid = ambiguous_store

        result = query_oscal_models.get_child_element(element_id="ac-1", parent_doc_uuid=cat1_uuid)

        assert result is not None
        assert "error" not in result
        assert result["id"] == "ac-1"
        assert result["parentDocumentUuid"] == cat1_uuid
        assert "raw_json" in result
