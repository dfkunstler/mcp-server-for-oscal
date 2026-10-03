"""
Tests for the Component Definition MCP tools in ``query_component_definition``.

Every test runs against a real ``OscalStore`` (the ``fixture_store`` and
``many_capabilities_store`` fixtures in ``tests/conftest.py``) built from the
JSON fixtures in ``tests/fixtures``. Nothing on ``OscalStore`` is mocked.

Fixture contents (three Component Definitions, five Components, one Capability):

- ``sample_component_definition.json``: "Sample Component" (software)
- ``multi_component_definition.json``: "Database Service" (software, prop value
  "PostgreSQL Global Development Group"), "API Gateway" (service),
  "Hardware Security Module" (hardware)
- ``sample_component_definition_with_capabilities.json``: "Capability Component"
  (software) and the "Test Capability" capability
"""

import ast
import builtins
import contextlib
import importlib.util
import os
import sys
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from trestle.oscal.component import ComponentDefinition

from mcp_server_for_oscal.config import config
from mcp_server_for_oscal.tools import query_component_definition as qcd_module
from mcp_server_for_oscal.tools.query_component_definition import (
    get_capability,
    init_store,
    list_capabilities,
    list_component_definitions,
    list_components,
    query_component_definition,
)

from ..fixture_store import (
    CAPABILITIES_CDEF_FILE,
    SAMPLE_CDEF_FILE,
    VALID_CDEF_FILES,
    build_fixture_store,
    load_fixture_cdef,
)

# ---------------------------------------------------------------------------
# Fixture identifiers
# ---------------------------------------------------------------------------

SAMPLE_CDEF_UUID = "a1b2c3d4-5678-4abc-8def-123456789012"
MULTI_CDEF_UUID = "f1e2d3c4-1234-4abc-8def-111111111111"
CAPS_CDEF_UUID = "c1d2e3f4-5678-4abc-8def-aabbccddeeff"
MULTI_CDEF_TITLE = "Multi-Component Definition"

SAMPLE_COMPONENT_UUID = "b2c3d4e5-6789-4bcd-9efa-234567890123"
DATABASE_UUID = "c1111111-1111-4111-8111-111111111111"
API_GATEWAY_UUID = "c2222222-2222-4222-8222-222222222223"
HSM_UUID = "c3333333-3333-4333-8333-333333333333"
CAPABILITY_COMPONENT_UUID = "e1f2a3b4-5678-4abc-9def-aabbccddeeff"

CAPABILITY_UUID = "d1e2f3a4-5678-4abc-9def-112233445566"
CAPABILITY_NAME = "Test Capability"

PROP_FALLBACK_VALUE = "PostgreSQL Global Development Group"

TOTAL_CDEFS = 3
ALL_COMPONENT_UUIDS = {
    SAMPLE_COMPONENT_UUID,
    DATABASE_UUID,
    API_GATEWAY_UUID,
    HSM_UUID,
    CAPABILITY_COMPONENT_UUID,
}
MULTI_COMPONENT_UUIDS = {DATABASE_UUID, API_GATEWAY_UUID, HSM_UUID}

COMPONENT_QUERY_KEYS = {
    "components",
    "total_count",
    "offset",
    "limit",
    "hasMore",
    "query_type",
    "component_definitions_searched",
    "filtered_by",
}
CAPABILITY_QUERY_KEYS = {
    "capability",
    "component_count",
    "offset",
    "limit",
    "total",
    "hasMore",
    "query_type",
    "component_definitions_searched",
    "filtered_by",
}
PAGE_KEYS = {"items", "total", "offset", "limit", "hasMore"}
CHILD_PARENT_KEYS = {
    "uuid",
    "parentComponentDefinitionTitle",
    "parentComponentDefinitionUuid",
    "sizeInBytes",
}


# ---------------------------------------------------------------------------
# Helpers: expected values computed from the source fixtures via trestle
# ---------------------------------------------------------------------------


def _parse_fixture(filename: str) -> ComponentDefinition:
    data = load_fixture_cdef(filename)
    return ComponentDefinition.parse_obj(data["component-definition"])


def _expected_components() -> dict[str, dict]:
    """Map component UUID -> ``DefinedComponent.dict(exclude_none=True)``."""
    out: dict[str, dict] = {}
    for filename in VALID_CDEF_FILES:
        for comp in _parse_fixture(filename).components or []:
            out[str(comp.uuid)] = comp.dict(exclude_none=True)
    return out


def _expected_capability():
    caps = _parse_fixture(CAPABILITIES_CDEF_FILE).capabilities or []
    return next(c for c in caps if str(c.uuid) == CAPABILITY_UUID)


def _component_uuids(result: dict[str, Any]) -> set[str]:
    return {str(c["uuid"]) for c in result["components"]}


def _assert_components_match_fixtures(result: dict[str, Any]) -> None:
    """Each returned component equals the trestle dict from its source (3.7)."""
    expected = _expected_components()
    for comp in result["components"]:
        assert comp == expected[str(comp["uuid"])]


# Filter values that select multi_component_definition.json: exact UUID and a
# case-altered title (CDef_Filter titles match case-insensitively).
MULTI_FILTERS = [
    pytest.param(MULTI_CDEF_UUID, id="filter-uuid"),
    pytest.param(MULTI_CDEF_TITLE.upper(), id="filter-title"),
]


# ---------------------------------------------------------------------------
# Wrappers against fixture_store (2.1, 7.2)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("fixture_store")
class TestWrappersAgainstFixtureStore:
    """Each of the five MCP tool wrappers delegates to the real store."""

    def test_query_component_definition(self):
        result = query_component_definition(ctx=None)
        assert set(result) == COMPONENT_QUERY_KEYS
        assert result["total_count"] == len(ALL_COMPONENT_UUIDS)

    def test_list_component_definitions(self):
        result = list_component_definitions(ctx=None)
        assert set(result) == PAGE_KEYS
        assert result["total"] == TOTAL_CDEFS
        assert result["hasMore"] is False
        assert {i["uuid"] for i in result["items"]} == {
            SAMPLE_CDEF_UUID,
            MULTI_CDEF_UUID,
            CAPS_CDEF_UUID,
        }

    def test_list_component_definitions_item_keys(self):
        """Items keep the existing summary keys (5.8)."""
        result = list_component_definitions(ctx=None)
        titles = {
            str(_parse_fixture(f).uuid): _parse_fixture(f).metadata.title
            for f in VALID_CDEF_FILES
        }
        for item in result["items"]:
            assert set(item) == {
                "uuid",
                "title",
                "componentCount",
                "importedComponentDefinitionsCount",
                "sizeInBytes",
            }
            assert item["title"] == titles[item["uuid"]]

    def test_list_components(self):
        result = list_components(ctx=None)
        assert set(result) == PAGE_KEYS
        assert result["total"] == len(ALL_COMPONENT_UUIDS)
        assert {i["uuid"] for i in result["items"]} == ALL_COMPONENT_UUIDS

    def test_list_capabilities(self):
        result = list_capabilities(ctx=None)
        assert set(result) == PAGE_KEYS
        assert result["total"] == 1
        assert [i["uuid"] for i in result["items"]] == [CAPABILITY_UUID]

    def test_get_capability(self):
        result = get_capability(ctx=None, uuid=CAPABILITY_UUID)
        assert result == _expected_capability().dict()


# ---------------------------------------------------------------------------
# list_components / list_capabilities item shape (5.1, 5.2)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("fixture_store")
class TestListChildElements:
    """Summary items carry the documented keys; ``uuid`` is the child's id."""

    def test_list_components_items(self):
        result = list_components(ctx=None)
        parents = {
            str(comp.uuid): (str(cd.uuid), cd.metadata.title, comp.title)
            for cd in (_parse_fixture(f) for f in VALID_CDEF_FILES)
            for comp in cd.components or []
        }
        for item in result["items"]:
            assert set(item) == CHILD_PARENT_KEYS | {"title"}
            parent_uuid, parent_title, title = parents[item["uuid"]]
            assert item["title"] == title
            assert item["parentComponentDefinitionUuid"] == parent_uuid
            assert item["parentComponentDefinitionTitle"] == parent_title
            assert item["sizeInBytes"] > 0

    def test_list_capabilities_items(self):
        result = list_capabilities(ctx=None)
        (item,) = result["items"]
        assert set(item) == CHILD_PARENT_KEYS | {"name"}
        assert item["uuid"] == CAPABILITY_UUID
        assert item["name"] == CAPABILITY_NAME
        assert item["parentComponentDefinitionUuid"] == CAPS_CDEF_UUID
        assert (
            item["parentComponentDefinitionTitle"]
            == _parse_fixture(CAPABILITIES_CDEF_FILE).metadata.title
        )
        assert item["sizeInBytes"] > 0


# ---------------------------------------------------------------------------
# query_component_definition matrix (3.1-3.9, 7.3)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("fixture_store")
class TestQueryMatrixNoFilter:
    """Component queries across every Component Definition in the store."""

    def _query(self, query_type: str, query_value: str | None = None) -> dict:
        result = query_component_definition(
            ctx=None,
            query_type=query_type,  # type: ignore[arg-type]
            query_value=query_value,
        )
        assert set(result) == COMPONENT_QUERY_KEYS
        assert result["query_type"] == query_type
        assert result["filtered_by"] is None
        assert result["component_definitions_searched"] == TOTAL_CDEFS
        _assert_components_match_fixtures(result)
        return result

    def test_all(self):
        result = self._query("all")
        assert _component_uuids(result) == ALL_COMPONENT_UUIDS
        assert result["total_count"] == len(ALL_COMPONENT_UUIDS)
        assert result["hasMore"] is False

    def test_by_uuid(self):
        result = self._query("by_uuid", API_GATEWAY_UUID)
        assert _component_uuids(result) == {API_GATEWAY_UUID}
        assert result["total_count"] == 1

    def test_by_title_case_insensitive(self):
        result = self._query("by_title", "database SERVICE")
        assert _component_uuids(result) == {DATABASE_UUID}
        assert result["total_count"] == 1

    def test_by_title_prop_value_fallback(self):
        result = self._query("by_title", PROP_FALLBACK_VALUE)
        assert _component_uuids(result) == {DATABASE_UUID}
        assert result["total_count"] == 1

    def test_by_type(self):
        result = self._query("by_type", "software")
        assert _component_uuids(result) == {
            SAMPLE_COMPONENT_UUID,
            DATABASE_UUID,
            CAPABILITY_COMPONENT_UUID,
        }
        assert result["total_count"] == 3


@pytest.mark.usefixtures("fixture_store")
@pytest.mark.parametrize("cdef_filter", MULTI_FILTERS)
class TestQueryMatrixWithFilter:
    """Component queries scoped to multi_component_definition.json."""

    def _query(
        self, cdef_filter: str, query_type: str, query_value: str | None = None
    ) -> dict:
        result = query_component_definition(
            ctx=None,
            component_definition_filter=cdef_filter,
            query_type=query_type,  # type: ignore[arg-type]
            query_value=query_value,
        )
        assert set(result) == COMPONENT_QUERY_KEYS
        assert result["query_type"] == query_type
        assert result["filtered_by"] == cdef_filter
        assert result["component_definitions_searched"] == 1
        _assert_components_match_fixtures(result)
        assert _component_uuids(result) <= MULTI_COMPONENT_UUIDS
        return result

    def test_all(self, cdef_filter):
        result = self._query(cdef_filter, "all")
        assert _component_uuids(result) == MULTI_COMPONENT_UUIDS
        assert result["total_count"] == len(MULTI_COMPONENT_UUIDS)

    def test_by_uuid(self, cdef_filter):
        result = self._query(cdef_filter, "by_uuid", HSM_UUID)
        assert _component_uuids(result) == {HSM_UUID}

    def test_by_uuid_outside_filter_is_empty(self, cdef_filter):
        result = self._query(cdef_filter, "by_uuid", SAMPLE_COMPONENT_UUID)
        assert result["components"] == []
        assert result["total_count"] == 0

    def test_by_title_case_insensitive(self, cdef_filter):
        result = self._query(cdef_filter, "by_title", "api gateway")
        assert _component_uuids(result) == {API_GATEWAY_UUID}

    def test_by_title_prop_value_fallback(self, cdef_filter):
        result = self._query(cdef_filter, "by_title", PROP_FALLBACK_VALUE)
        assert _component_uuids(result) == {DATABASE_UUID}

    def test_by_type(self, cdef_filter):
        result = self._query(cdef_filter, "by_type", "software")
        assert _component_uuids(result) == {DATABASE_UUID}
        assert result["total_count"] == 1


@pytest.mark.usefixtures("fixture_store")
class TestQueryCapabilityFirst:
    """``by_uuid`` / ``by_title`` return a Capability before any Component."""

    @pytest.mark.parametrize(
        ("query_type", "query_value"),
        [("by_uuid", CAPABILITY_UUID), ("by_title", CAPABILITY_NAME)],
    )
    @pytest.mark.parametrize("cdef_filter", [None, CAPS_CDEF_UUID])
    def test_capability_response(self, query_type, query_value, cdef_filter):
        result = query_component_definition(
            ctx=None,
            component_definition_filter=cdef_filter,
            query_type=query_type,
            query_value=query_value,
        )
        cap = _expected_capability()
        assert set(result) == CAPABILITY_QUERY_KEYS
        assert result["capability"] == cap.oscal_dict()
        assert result["component_count"] == len(cap.incorporates_components or [])
        assert (result["offset"], result["limit"], result["total"]) == (0, 1, 1)
        assert result["hasMore"] is False
        assert result["query_type"] == query_type
        assert result["filtered_by"] == cdef_filter
        expected_searched = 1 if cdef_filter else TOTAL_CDEFS
        assert result["component_definitions_searched"] == expected_searched


# ---------------------------------------------------------------------------
# Error and edge cases (2.2, 4.3-4.6, 5.3-5.7, 7.4, 7.5)
# ---------------------------------------------------------------------------

NO_COMPONENTS_CDEF_UUID = "0a0a0a0a-0000-4000-8000-000000000001"


def _no_components_cdef() -> dict:
    """A minimal Trestle-valid Component Definition with no components."""
    return {
        "component-definition": {
            "uuid": NO_COMPONENTS_CDEF_UUID,
            "metadata": {
                "title": "No Components Definition",
                "last-modified": "2024-01-01T00:00:00+00:00",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
        }
    }


@pytest.fixture
def store_factory(tmp_path):
    """Build and install a real store from cdef dicts; closes it afterward."""
    stores = []

    def _build(cdefs: list[dict]):
        store = build_fixture_store(tmp_path, cdefs)
        init_store(store)
        stores.append(store)
        return store

    yield _build
    for store in stores:
        store.close()


WRAPPER_CALLS = [
    pytest.param(lambda: query_component_definition(ctx=None), id="query"),
    pytest.param(lambda: list_component_definitions(ctx=None), id="list_cdefs"),
    pytest.param(lambda: list_components(ctx=None), id="list_components"),
    pytest.param(lambda: list_capabilities(ctx=None), id="list_capabilities"),
    pytest.param(
        lambda: get_capability(ctx=None, uuid=CAPABILITY_UUID), id="get_capability"
    ),
]


class TestUninitialisedStore:
    """Every wrapper raises RuntimeError before ``init_store`` (2.2)."""

    @pytest.mark.parametrize("call", WRAPPER_CALLS)
    def test_raises_runtime_error(self, call):
        # The autouse ``reset_oscal_store`` fixture leaves the singleton unset.
        with pytest.raises(RuntimeError, match="init_store"):
            call()


@pytest.mark.usefixtures("fixture_store")
class TestQueryErrorsAndEdgeCases:
    """Validation, filter misses and query_value normalisation (4.3-4.6)."""

    @pytest.mark.parametrize("query_type", ["by_uuid", "by_title", "by_type"])
    @pytest.mark.parametrize("query_value", [None, "", "   \t "])
    def test_missing_query_value(self, query_type, query_value):
        with pytest.raises(ValueError, match="query_value is required"):
            query_component_definition(
                ctx=None, query_type=query_type, query_value=query_value
            )

    @pytest.mark.parametrize(
        "cdef_filter", ["no-such-definition", "Multi", "Component"]
    )
    def test_unmatched_or_fuzzy_filter_is_empty(self, cdef_filter):
        """Unknown and partial (fuzzy-only) titles never select a cdef."""
        result = query_component_definition(
            ctx=None, component_definition_filter=cdef_filter
        )
        assert set(result) == COMPONENT_QUERY_KEYS
        assert result["components"] == []
        assert result["total_count"] == 0
        assert result["hasMore"] is False
        assert result["component_definitions_searched"] == 0
        assert result["filtered_by"] == cdef_filter

    @pytest.mark.parametrize(
        ("query_type", "query_value", "expected"),
        [
            ("by_uuid", f"  {HSM_UUID}\n", {HSM_UUID}),
            ("by_title", "  API Gateway  ", {API_GATEWAY_UUID}),
            ("by_type", " hardware ", {HSM_UUID}),
        ],
    )
    def test_whitespace_padded_query_value_matches(
        self, query_type, query_value, expected
    ):
        result = query_component_definition(
            ctx=None, query_type=query_type, query_value=query_value
        )
        assert _component_uuids(result) == expected

    def test_whitespace_padded_capability_name_matches(self):
        result = query_component_definition(
            ctx=None, query_type="by_title", query_value=f"  {CAPABILITY_NAME} "
        )
        assert result["capability"] == _expected_capability().oscal_dict()


class TestEmptyStores:
    """Stores with no cdefs, no components or no capabilities (4.4, 5.3-5.7)."""

    def test_query_on_empty_store(self, store_factory):
        store_factory([])
        with pytest.raises(ValueError, match="No Component Definitions loaded"):
            query_component_definition(ctx=None)

    def test_list_component_definitions_on_empty_store(self, store_factory):
        store_factory([])
        with pytest.raises(RuntimeError, match="No Component Definitions loaded"):
            list_component_definitions(ctx=None)

    def test_list_capabilities_without_capabilities(self, store_factory):
        store_factory([load_fixture_cdef(SAMPLE_CDEF_FILE)])
        result = list_capabilities(ctx=None)
        assert result == {
            "items": [],
            "total": 0,
            "offset": 0,
            "limit": 10,
            "hasMore": False,
        }

    def test_list_components_without_components(self, store_factory):
        store_factory([_no_components_cdef()])
        assert list_component_definitions(ctx=None)["total"] == 1
        with pytest.raises(RuntimeError, match="No Components loaded"):
            list_components(ctx=None)


class TestManyCapabilities:
    """Capability lookups are not capped at 100 (5.4-5.6, 7.4, 7.5)."""

    def test_get_capability_beyond_position_100(self, many_capabilities_store):
        target = many_capabilities_store.target
        result = get_capability(ctx=None, uuid=target["uuid"])
        assert result is not None
        assert str(result["uuid"]) == target["uuid"]
        assert result["name"] == target["name"]

    @pytest.mark.parametrize(
        ("query_type", "key"), [("by_uuid", "uuid"), ("by_title", "name")]
    )
    def test_query_finds_capability_beyond_position_100(
        self, many_capabilities_store, query_type, key
    ):
        target = many_capabilities_store.target
        result = query_component_definition(
            ctx=None, query_type=query_type, query_value=target[key]
        )
        assert set(result) == CAPABILITY_QUERY_KEYS
        # oscal_dict() wraps the Capability under its "capability" alias.
        cap = result["capability"]["capability"]
        assert str(cap["uuid"]) == target["uuid"]
        assert cap["name"] == target["name"]

    @pytest.mark.parametrize(
        "uuid", ["00000000-0000-4000-8000-000000000000", ""], ids=["unknown", "empty"]
    )
    @pytest.mark.usefixtures("many_capabilities_store")
    def test_get_capability_not_found(self, uuid):
        assert get_capability(ctx=None, uuid=uuid) is None


# ---------------------------------------------------------------------------
# Parse scope: only the needed Component Definitions are parsed (3.10, 3.11)
# ---------------------------------------------------------------------------


@pytest.fixture
def parse_spy(fixture_store):
    """Spy on ``get_parsed_model_by_uuid`` of the installed real store."""
    with patch.object(
        fixture_store,
        "get_parsed_model_by_uuid",
        wraps=fixture_store.get_parsed_model_by_uuid,
    ) as spy:
        yield spy


def _parsed_uuids(spy) -> list[str]:
    return [c.args[0] if c.args else c.kwargs["doc_uuid"] for c in spy.call_args_list]


FILTERED_COMPONENT_QUERIES = [
    pytest.param("all", None, MULTI_COMPONENT_UUIDS, id="all"),
    pytest.param("by_uuid", HSM_UUID, {HSM_UUID}, id="by_uuid"),
    pytest.param("by_title", "API Gateway", {API_GATEWAY_UUID}, id="by_title"),
    pytest.param("by_type", "software", {DATABASE_UUID}, id="by_type"),
    pytest.param("by_title", PROP_FALLBACK_VALUE, {DATABASE_UUID}, id="prop-fallback"),
]


class TestParseScope:
    """Component results parse only the Component Definitions they need."""

    @pytest.mark.parametrize("cdef_filter", MULTI_FILTERS)
    @pytest.mark.parametrize(
        ("query_type", "query_value", "expected"), FILTERED_COMPONENT_QUERIES
    )
    def test_filtered_query_parses_only_matched_cdef(
        self, parse_spy, cdef_filter, query_type, query_value, expected
    ):
        """With a CDef_Filter only the matched cdef is parsed (3.10)."""
        result = query_component_definition(
            ctx=None,
            component_definition_filter=cdef_filter,
            query_type=query_type,
            query_value=query_value,
        )
        assert _component_uuids(result) == expected
        assert _parsed_uuids(parse_spy) == [MULTI_CDEF_UUID]

    @pytest.mark.parametrize(
        ("query_type", "query_value", "expected_uuid", "parent_uuid"),
        [
            pytest.param(
                "by_uuid", HSM_UUID, HSM_UUID, MULTI_CDEF_UUID, id="by_uuid-multi"
            ),
            pytest.param(
                "by_uuid",
                SAMPLE_COMPONENT_UUID,
                SAMPLE_COMPONENT_UUID,
                SAMPLE_CDEF_UUID,
                id="by_uuid-sample",
            ),
            pytest.param(
                "by_title",
                "api gateway",
                API_GATEWAY_UUID,
                MULTI_CDEF_UUID,
                id="by_title",
            ),
            pytest.param(
                "by_title",
                PROP_FALLBACK_VALUE,
                DATABASE_UUID,
                MULTI_CDEF_UUID,
                id="prop-fallback",
            ),
        ],
    )
    def test_unfiltered_lookup_parses_only_candidate_parent(
        self, parse_spy, query_type, query_value, expected_uuid, parent_uuid
    ):
        """Without a filter only the hit's parent cdef is parsed (3.11)."""
        result = query_component_definition(
            ctx=None, query_type=query_type, query_value=query_value
        )
        assert _component_uuids(result) == {expected_uuid}
        assert _parsed_uuids(parse_spy) == [parent_uuid]

    def test_unfiltered_lookup_miss_parses_nothing(self, parse_spy):
        result = query_component_definition(
            ctx=None, query_type="by_uuid", query_value="no-such-uuid"
        )
        assert result["components"] == []
        assert _parsed_uuids(parse_spy) == []

    @pytest.mark.parametrize(
        ("query_type", "query_value"),
        [("by_uuid", CAPABILITY_UUID), ("by_title", CAPABILITY_NAME)],
    )
    @pytest.mark.parametrize("cdef_filter", [None, CAPS_CDEF_UUID])
    def test_capability_lookup_parses_only_its_parent(
        self, parse_spy, query_type, query_value, cdef_filter
    ):
        result = query_component_definition(
            ctx=None,
            component_definition_filter=cdef_filter,
            query_type=query_type,
            query_value=query_value,
        )
        assert set(result) == CAPABILITY_QUERY_KEYS
        assert _parsed_uuids(parse_spy) == [CAPS_CDEF_UUID]

    def test_get_capability_parses_only_its_parent(self, parse_spy):
        assert get_capability(ctx=None, uuid=CAPABILITY_UUID) is not None
        assert _parsed_uuids(parse_spy) == [CAPS_CDEF_UUID]


# ---------------------------------------------------------------------------
# Structural smoke tests (1.1-1.3, 6.3)
# ---------------------------------------------------------------------------

_CDEF_TOOLS_PATH = Path(qcd_module.__file__)
_LEGACY_NAMES = (
    "ComponentDefinitionStore",
    "_store",
    "_load_component_definitions_from_directory",
)
# Names under which CDef_Tools refers to an OscalStore instance.
_STORE_NAMES = {"_oscal_store", "store"}


def _cdef_tools_tree() -> ast.Module:
    return ast.parse(_CDEF_TOOLS_PATH.read_text(encoding="utf-8"))


class TestLegacyStoreRemoved:
    """The legacy in-memory store is gone (1.1, 1.2)."""

    @pytest.mark.parametrize("name", _LEGACY_NAMES)
    def test_module_has_no_legacy_attribute(self, name):
        assert not hasattr(qcd_module, name)

    def test_source_has_no_legacy_names(self):
        tree = _cdef_tools_tree()
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        }
        assert names.isdisjoint(_LEGACY_NAMES)

    def test_source_does_not_reference_cdef_dir_setting(self):
        assert "component_definitions_dir" not in _CDEF_TOOLS_PATH.read_text(
            encoding="utf-8"
        )


class TestPublicStoreApiOnly:
    """CDef_Tools touches the OscalStore only via public attributes (6.3)."""

    def test_no_private_store_attribute_access(self):
        offenders = [
            f"{node.value.id}.{node.attr} (line {node.lineno})"
            for node in ast.walk(_cdef_tools_tree())
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in _STORE_NAMES
            and node.attr.startswith("_")
        ]
        assert offenders == []


class TestImportReadsNoCdefContent:
    """Importing CDef_Tools reads nothing from the cdef directory (1.3).

    A fresh copy of the module is executed under a throwaway name with the
    filesystem entry points patched to record every path they receive. The
    real ``mcp_server_for_oscal.tools.query_component_definition`` module is
    never reloaded, so module identity seen by other tests is unchanged.
    """

    def test_import_performs_no_cdef_reads(self, tmp_path, monkeypatch):
        cdef_dir = tmp_path / "component_definitions"
        cdef_dir.mkdir()
        (cdef_dir / "cdef.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(config, "component_definitions_dir", str(cdef_dir))

        seen: list[str] = []

        def recorder(real, path_arg: int = 0):
            def _wrapped(*args, **kwargs):
                if len(args) > path_arg:
                    seen.append(str(args[path_arg]))
                return real(*args, **kwargs)

            return _wrapped

        patches = [
            patch.object(Path, name, recorder(getattr(Path, name)))
            for name in (
                "iterdir",
                "glob",
                "rglob",
                "exists",
                "is_dir",
                "is_file",
                "open",
                "read_text",
                "read_bytes",
            )
        ]
        patches += [
            patch("builtins.open", recorder(builtins.open)),
            patch("os.listdir", recorder(os.listdir)),
            patch("os.scandir", recorder(os.scandir)),
            patch("os.walk", recorder(os.walk)),
            patch("zipfile.ZipFile", recorder(zipfile.ZipFile)),
        ]

        name = "_cdef_tools_import_probe"
        spec = importlib.util.spec_from_file_location(name, _CDEF_TOOLS_PATH)
        assert spec is not None
        assert spec.loader is not None
        fresh = importlib.util.module_from_spec(spec)
        sys.modules[name] = fresh
        try:
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                spec.loader.exec_module(fresh)
                import_seen = list(seen)
                # Positive control: the patches do record a cdef_dir read.
                list(cdef_dir.iterdir())
                assert str(cdef_dir) in seen[len(import_seen) :]
        finally:
            sys.modules.pop(name, None)

        # The probe really executed the module and left the real one intact.
        assert fresh.init_store is not qcd_module.init_store
        assert sys.modules[qcd_module.__name__] is qcd_module
        offenders = [
            p for p in import_seen if str(cdef_dir) in p or "component_definitions" in p
        ]
        assert offenders == []
