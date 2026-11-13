"""Tests for the Generic_Tools: ``list_documents``, ``query_documents``, ``list_child_elements``.

Feature: consolidate-tools (GitHub issue #40).

Layout, one section per concern so later tasks (2.5-2.14) can append classes:

- Module-level fixtures: ``generic_fixture`` (module-scoped, read-only store over
  ``build_leak_store`` plus one catalog with a multi-byte child title), ``installed_store``
  (installs it into ``query_oscal_models`` for one test), and ``server_tools`` (live
  ``tools/list`` entries by name).
- ``TestSignatures`` / ``TestSchemas``: parameter order, defaults, shared aliases, ``ctx`` last
  and hidden from the schema, and the ``enum`` of each parameter (task 2.4).
- ``TestEnumSync``: the schema enums match the store constants (Requirement 5).
- ``TestDescriptions``: Description_Shape, size caps, unknown tool references (Requirement 10).
- ``TestPageRejection`` .. ``TestNoFilesystemPath``: errors, empty results, the uninitialised
  store, and path hygiene (task 2.5).
"""

import asyncio
import inspect
import json
import re
import string
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock, patch

import pydantic
import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st
from mcp.server.mcpserver import Context

from mcp_server_for_oscal import main
from mcp_server_for_oscal.tools import get_tool_list, tool_budget
from mcp_server_for_oscal.tools import query_oscal_models as qom
from mcp_server_for_oscal.tools.descriptions import (
    ChildElementType,
    DocumentModelType,
    Limit,
    Offset,
    ParentDocUuid,
    PerModelQueryType,
    QueryValue,
)
from mcp_server_for_oscal.tools.oscal_store import (
    CHILD_ELEMENT_TYPES,
    ORIGIN_USER,
    VALID_QUERY_TYPES,
    OscalStore,
)
from mcp_server_for_oscal.tools.utils import OSCALModelType

from ..fixture_store import (
    LeakFixture,
    build_fixture_store,
    build_leak_store,
    load_valid_fixture_cdefs,
    make_many_capabilities_cdef,
)

GENERIC_TOOL_NAMES = ("list_documents", "query_documents", "list_child_elements")
GENERIC_TOOLS = {fn.__name__: fn for fn in get_tool_list() if fn.__name__ in GENERIC_TOOL_NAMES}

# Literal expectations, written out so a change to the constants shows up in review.
MODEL_TYPE_VALUES = [
    "catalog",
    "profile",
    "component-definition",
    "system-security-plan",
    "assessment-plan",
    "assessment-results",
    "plan-of-action-and-milestones",
    "mapping-collection",
]
ELEMENT_TYPE_VALUES = [
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
QUERY_TYPE_VALUES = ["all", "by_uuid", "by_title"]

# Parameters (in order) of each Generic_Tool, with the shared alias each one uses.
EXPECTED_PARAMETERS: dict[str, list[tuple[str, Any]]] = {
    "list_documents": [
        ("model_type", DocumentModelType),
        ("offset", Offset),
        ("limit", Limit),
        ("ctx", None),
    ],
    "query_documents": [
        ("model_type", DocumentModelType),
        ("query_type", PerModelQueryType),
        ("query_value", QueryValue),
        ("offset", Offset),
        ("limit", Limit),
        ("ctx", None),
    ],
    "list_child_elements": [
        ("element_type", ChildElementType),
        ("parent_doc_uuid", ParentDocUuid),
        ("offset", Offset),
        ("limit", Limit),
        ("ctx", None),
    ],
}
# Defaults of the optional parameters; parameters not listed here are required.
EXPECTED_DEFAULTS: dict[str, dict[str, Any]] = {
    "list_documents": {"offset": 0, "limit": 10},
    "query_documents": {"query_type": "all", "query_value": None, "offset": 0, "limit": 10},
    "list_child_elements": {"parent_doc_uuid": None, "offset": 0, "limit": 10},
}
# The ``enum`` each parameter has in the input schema (absent for parameters not listed).
EXPECTED_ENUMS: dict[str, dict[str, list[str]]] = {
    "list_documents": {"model_type": MODEL_TYPE_VALUES},
    "query_documents": {"model_type": MODEL_TYPE_VALUES, "query_type": QUERY_TYPE_VALUES},
    "list_child_elements": {"element_type": ELEMENT_TYPE_VALUES},
}

# Document and child of the extra catalog (task 2.9 uses them for Property 4).
EXTRA_CATALOG_UUID = "5e1f0c2a-00a0-4a00-8000-000000000001"
MULTIBYTE_CONTROL_ID = "uni-1"
MULTIBYTE_CONTROL_TITLE = "コントロール – Zugriffsüberwachung 🔐"


# --- Fixtures --------------------------------------------------------------------------------


@dataclass(frozen=True)
class GenericFixture:
    """The leak fixture store plus the extra catalog with a multi-byte child title."""

    leak: LeakFixture
    extra_catalog_uuid: str
    multibyte_control_id: str
    multibyte_control_title: str

    @property
    def store(self) -> OscalStore:
        """The read-only store shared by every test of the module."""
        return self.leak.store


def _write_multibyte_catalog(directory: Path) -> None:
    """Write a model-valid catalog whose only control has a multi-byte title."""
    catalog = {
        "catalog": {
            "uuid": EXTRA_CATALOG_UUID,
            "metadata": {
                "title": "Ünïcödé Catalog",
                "last-modified": "2024-01-01T00:00:00Z",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
            "controls": [
                {
                    "id": MULTIBYTE_CONTROL_ID,
                    "title": MULTIBYTE_CONTROL_TITLE,
                    "parts": [
                        {
                            "id": f"{MULTIBYTE_CONTROL_ID}_smt",
                            "name": "statement",
                            "prose": "Überwachung der Zugänge – アクセス管理。",
                        }
                    ],
                }
            ],
        }
    }
    (directory / "multibyte_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture(scope="module")
def generic_fixture(tmp_path_factory: pytest.TempPathFactory) -> Iterator[GenericFixture]:
    """Read-only store: a document of every model, a child of every Element_Type, and one extra
    catalog with a multi-byte child title. Tests must not write to it."""
    leak = build_leak_store(tmp_path_factory.mktemp("generic-tools"))
    try:
        _write_multibyte_catalog(leak.user_docs_dir)
        leak.store.scan_directory(leak.user_docs_dir, origin=ORIGIN_USER)
        leak.store.text_search(leak.text_query)  # indexes pending documents (child elements)
        yield GenericFixture(
            leak=leak,
            extra_catalog_uuid=EXTRA_CATALOG_UUID,
            multibyte_control_id=MULTIBYTE_CONTROL_ID,
            multibyte_control_title=MULTIBYTE_CONTROL_TITLE,
        )
    finally:
        leak.store.close()


@pytest.fixture
def installed_store(reset_oscal_store: None, generic_fixture: GenericFixture) -> OscalStore:
    """Install the shared store into ``query_oscal_models`` for one test.

    ``reset_oscal_store`` (autouse, function-scoped) clears the singleton again afterwards.
    """
    qom.init_store(generic_fixture.store)
    return generic_fixture.store


@pytest.fixture(scope="module")
def server_tools() -> dict[str, dict[str, Any]]:
    """Live ``tools/list`` entries from a server built by the production path, by name."""
    server = main.build_server()
    main._setup_tools(server)
    tools = asyncio.run(server.list_tools())
    dumped = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]
    return {t["name"]: t for t in dumped}


def _properties(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    return entry["inputSchema"].get("properties", {})


def _enum(prop: Mapping[str, Any]) -> list[str] | None:
    """Return the string ``enum`` of a schema property (also inside ``anyOf``), or None."""
    if "enum" in prop:
        return list(prop["enum"])
    for branch in prop.get("anyOf", []):
        if "enum" in branch:
            return list(branch["enum"])
    return None


def _flat_element_types(mapping: Mapping[OSCALModelType, Sequence[str]]) -> list[str]:
    return [t for types in mapping.values() for t in types]


# --- Sync checks as pure functions (so the tests can be shown to fail on a gap) ---------------


def enum_gap(schema_enum: Sequence[str] | None, expected: Sequence[str]) -> str:
    """Return a message when ``schema_enum`` and ``expected`` differ as sets, else ``""``."""
    if schema_enum is None:
        return "no enum in the schema"
    missing = sorted(set(expected) - set(schema_enum))
    extra = sorted(set(schema_enum) - set(expected))
    if missing or extra or len(schema_enum) != len(set(schema_enum)):
        return (
            f"missing={missing} extra={extra} duplicates={len(schema_enum) - len(set(schema_enum))}"
        )
    return ""


def shared_element_types(mapping: Mapping[OSCALModelType, Sequence[str]]) -> dict[str, list[str]]:
    """Return each Element_Type name that occurs under more than one Model_Type."""
    owners: dict[str, list[str]] = {}
    for model_type, types in mapping.items():
        for element_type in types:
            owners.setdefault(element_type, []).append(model_type.value)
    return {name: models for name, models in owners.items() if len(models) > 1}


# --- Fixture sanity --------------------------------------------------------------------------


class TestGenericFixture:
    """The shared store holds what the later property tests rely on."""

    def test_every_model_type_has_a_document(self, generic_fixture: GenericFixture):
        for model_type in OSCALModelType:
            page = generic_fixture.store.list_documents(oscal_model_type=model_type, limit=100)
            assert page["total"] >= 1, model_type.value

    def test_every_element_type_has_a_child(self, generic_fixture: GenericFixture):
        for element_type in _flat_element_types(CHILD_ELEMENT_TYPES):
            page = generic_fixture.store.list_child_elements(element_type=element_type, limit=1)
            assert page["total"] >= 1, element_type

    def test_multibyte_child_is_indexed_with_non_ascii_json(self, generic_fixture: GenericFixture):
        page = generic_fixture.store.list_child_elements(
            parent_doc_uuid=generic_fixture.extra_catalog_uuid,
            element_type="control",
            include_raw_json=True,
        )
        assert [(i["id"], i["title"]) for i in page["items"]] == [
            (generic_fixture.multibyte_control_id, generic_fixture.multibyte_control_title)
        ]
        raw_json = page["items"][0]["raw_json"]
        assert len(raw_json.encode("utf-8")) > len(raw_json)

    def test_installed_store_serves_the_tools(self, installed_store: OscalStore):
        result = qom.list_documents(model_type="catalog")
        assert result["total"] >= 2  # the leak catalog and the multi-byte catalog


# --- Signatures (Requirements 1.1, 1.2, 2.2, 2.3, 3.2, 3.3, 4.7, 10.4) -----------------------


class TestSignatures:
    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_tool_is_registered(self, name: str):
        assert name in GENERIC_TOOLS

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_parameter_order(self, name: str):
        params = list(inspect.signature(GENERIC_TOOLS[name]).parameters)
        assert params == [p for p, _ in EXPECTED_PARAMETERS[name]]

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_defaults(self, name: str):
        sig = inspect.signature(GENERIC_TOOLS[name])
        defaults = EXPECTED_DEFAULTS[name]
        for param_name, param in sig.parameters.items():
            if param_name == "ctx":
                assert param.default is None
            elif param_name in defaults:
                assert param.default == defaults[param_name], param_name
            else:
                assert param.default is inspect.Parameter.empty, param_name

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_shared_aliases(self, name: str):
        sig = inspect.signature(GENERIC_TOOLS[name])
        for param_name, alias in EXPECTED_PARAMETERS[name]:
            if alias is not None:
                assert sig.parameters[param_name].annotation == alias, param_name

    def test_document_tools_share_one_model_type_alias(self):
        """Requirements 1.1, 2.1, 10.4: DocumentModelType on both document tools."""
        list_sig = inspect.signature(GENERIC_TOOLS["list_documents"])
        query_sig = inspect.signature(GENERIC_TOOLS["query_documents"])
        assert list_sig.parameters["model_type"].annotation is DocumentModelType
        assert query_sig.parameters["model_type"].annotation is DocumentModelType

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_ctx_is_last_and_optional_context(self, name: str):
        """Requirement 4.7."""
        sig = inspect.signature(GENERIC_TOOLS[name])
        assert list(sig.parameters)[-1] == "ctx"
        assert sig.parameters["ctx"].annotation == (Context | None)
        assert sig.parameters["ctx"].default is None


# --- Input schemas (Requirements 1.1, 2.1, 3.1, 4.7) -----------------------------------------


class TestSchemas:
    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_ctx_hidden_from_schema(self, name: str, server_tools: dict[str, dict[str, Any]]):
        expected = [p for p, _ in EXPECTED_PARAMETERS[name] if p != "ctx"]
        assert list(_properties(server_tools[name])) == expected

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_required_parameters(self, name: str, server_tools: dict[str, dict[str, Any]]):
        expected = [
            p
            for p, _ in EXPECTED_PARAMETERS[name]
            if p != "ctx" and p not in EXPECTED_DEFAULTS[name]
        ]
        assert server_tools[name]["inputSchema"].get("required", []) == expected

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_schema_defaults(self, name: str, server_tools: dict[str, dict[str, Any]]):
        for param_name, prop in _properties(server_tools[name]).items():
            if param_name in EXPECTED_DEFAULTS[name]:
                assert prop.get("default") == EXPECTED_DEFAULTS[name][param_name], param_name
            else:
                assert "default" not in prop, param_name

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_enum_of_each_parameter(self, name: str, server_tools: dict[str, dict[str, Any]]):
        """Requirements 1.1, 2.1, 3.1: exact enums; every other parameter has none."""
        for param_name, prop in _properties(server_tools[name]).items():
            expected = EXPECTED_ENUMS[name].get(param_name)
            assert _enum(prop) == expected, f"{name}.{param_name}"

    def test_model_type_enum_is_the_eight_model_types(self):
        assert len(MODEL_TYPE_VALUES) == 8
        assert [m.value for m in OSCALModelType] == MODEL_TYPE_VALUES

    def test_element_type_enum_is_the_fourteen_element_types(self):
        assert len(ELEMENT_TYPE_VALUES) == 14
        assert len(set(ELEMENT_TYPE_VALUES)) == 14


# --- Enum sync with the store (Requirement 5) ------------------------------------------------


class TestEnumSync:
    @pytest.mark.parametrize("name", ["list_documents", "query_documents"])
    def test_model_type_enum_matches_child_element_types_keys(
        self, name: str, server_tools: dict[str, dict[str, Any]]
    ):
        """Requirement 5.1."""
        schema_enum = _enum(_properties(server_tools[name])["model_type"])
        expected = [m.value for m in CHILD_ELEMENT_TYPES]
        assert enum_gap(schema_enum, expected) == ""

    def test_element_type_enum_matches_child_element_types_values(
        self, server_tools: dict[str, dict[str, Any]]
    ):
        """Requirement 5.2."""
        schema_enum = _enum(_properties(server_tools["list_child_elements"])["element_type"])
        assert enum_gap(schema_enum, _flat_element_types(CHILD_ELEMENT_TYPES)) == ""

    def test_no_element_type_under_two_model_types(self):
        """Requirement 5.3."""
        assert shared_element_types(CHILD_ELEMENT_TYPES) == {}

    def test_query_type_enum_matches_valid_query_types(
        self, server_tools: dict[str, dict[str, Any]]
    ):
        """Requirement 5.4."""
        schema_enum = _enum(_properties(server_tools["query_documents"])["query_type"])
        assert enum_gap(schema_enum, VALID_QUERY_TYPES) == ""

    def test_sync_checks_detect_a_gap(self):
        """The checks above fail when a literal is missing, extra, duplicated, or shared."""
        assert enum_gap(MODEL_TYPE_VALUES[1:], MODEL_TYPE_VALUES) != ""
        assert enum_gap([*ELEMENT_TYPE_VALUES, "extra"], ELEMENT_TYPE_VALUES) != ""
        assert enum_gap(QUERY_TYPE_VALUES[:-1], VALID_QUERY_TYPES) != ""
        assert enum_gap(None, QUERY_TYPE_VALUES) != ""
        shared = {**CHILD_ELEMENT_TYPES, OSCALModelType.PROFILE: ("import", "control")}
        assert shared_element_types(shared) == {"control": ["catalog", "profile"]}


# --- Descriptions (Requirement 10) -----------------------------------------------------------


class TestDescriptions:
    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_description_shape(self, name: str, server_tools: dict[str, dict[str, Any]]):
        """Requirement 10.1."""
        description = server_tools[name]["description"]
        assert tool_budget.description_shape_problems(description) == []

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_description_at_most_180_characters(
        self, name: str, server_tools: dict[str, dict[str, Any]]
    ):
        """Requirement 10.2: the docstring as clients receive it, whitespace collapsed."""
        description = server_tools[name]["description"]
        assert len(" ".join(description.split())) <= 180
        assert len(description) <= tool_budget.TOOL_DESCRIPTION_CAP

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_every_parameter_described_within_cap(
        self, name: str, server_tools: dict[str, dict[str, Any]]
    ):
        """Requirement 10.3."""
        for param_name, prop in _properties(server_tools[name]).items():
            description = prop.get("description", "")
            assert description.strip(), f"{name}.{param_name} has no description"
            assert len(description) <= tool_budget.PARAMETER_DESCRIPTION_CAP, f"{name}.{param_name}"

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_no_unknown_tool_references(self, name: str, server_tools: dict[str, dict[str, Any]]):
        """Requirement 10.5: descriptions name only tools, parameters, or enum values."""
        allowed = tool_budget.known_identifiers(list(server_tools.values()))
        texts = [server_tools[name]["description"]] + [
            prop.get("description", "") for prop in _properties(server_tools[name]).values()
        ]
        for text in texts:
            assert tool_budget.unknown_tool_refs(text, allowed) == [], text

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_description_does_not_name_ctx(
        self, name: str, server_tools: dict[str, dict[str, Any]]
    ):
        assert "ctx" not in server_tools[name]["description"]


# --- Errors, empty results, and the uninitialised store (task 2.5) ---------------------------

PAGE_KEYS = {"items", "total", "offset", "limit", "hasMore"}
# A syntactically valid call of each tool; tests override one argument at a time.
VALID_CALLS: dict[str, dict[str, Any]] = {
    "list_documents": {"model_type": "catalog"},
    "query_documents": {"model_type": "catalog"},
    "list_child_elements": {"element_type": "control"},
}
# (offset, limit) pairs outside the Valid_Page range, with the text of the paginate error.
BAD_PAGES = [
    (-1, 10, "offset must be non-negative"),
    (-5, 10, "offset must be non-negative"),
    (0, 0, "limit must be between 1 and 100"),
    (0, -1, "limit must be between 1 and 100"),
    (0, 101, "limit must be between 1 and 100"),
]
UNKNOWN_UUID = "5e1f0c2a-ffff-4fff-8fff-ffffffffffff"
NOTIFY_TARGET = "try_notify_client_error"


def _call(name: str, **overrides: Any) -> dict:
    """Call the Generic_Tool ``name`` from Python with ``VALID_CALLS[name]`` plus overrides."""
    kwargs = {**VALID_CALLS[name], **overrides}
    return getattr(qom, name)(**kwargs)


@pytest.fixture
def notify(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``try_notify_client_error`` where ``query_oscal_models`` looks it up."""
    mock = MagicMock()
    monkeypatch.setattr(qom, NOTIFY_TARGET, mock)
    return mock


def _assert_empty_page(result: dict, *, offset: int = 0, limit: int = 10) -> None:
    assert set(result) == PAGE_KEYS
    assert result["items"] == []
    assert result["total"] == 0
    assert result["hasMore"] is False
    assert result["offset"] == offset
    assert result["limit"] == limit


def _location_needles(fixture: GenericFixture) -> list[str]:
    """Strings that would show a storage location: the fixture's paths, temp, and home."""
    leak = fixture.leak
    return [
        str(leak.root),
        str(leak.db_path),
        str(leak.bundled_dir),
        str(leak.user_docs_dir),
        str(Path(tempfile.gettempdir())),
        str(Path.home()),
        "s3://",
        "arn:aws",
    ]


def _assert_no_location(text: str, needles: Sequence[str]) -> None:
    for needle in needles:
        assert needle not in text, f"{needle!r} found in {text[:200]!r}"


class TestPageRejection:
    """Requirement 4.1: bad pages notify the client and raise before the store is read."""

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    @pytest.mark.parametrize(("offset", "limit", "text"), BAD_PAGES)
    def test_bad_page_notifies_and_raises(
        self, name: str, offset: int, limit: int, text: str, notify: MagicMock
    ):
        ctx = object()
        with pytest.raises(ValueError, match=text):
            _call(name, offset=offset, limit=limit, ctx=ctx)
        notify.assert_called_once()
        message, passed_ctx = notify.call_args.args
        assert text in message
        assert passed_ctx is ctx

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_bad_page_raises_before_the_store_is_read(self, name: str, notify: MagicMock):
        """The store is unset (autouse reset), yet the error is a ValueError, not RuntimeError."""
        assert qom._store is None
        with pytest.raises(ValueError, match="limit must be between 1 and 100"):
            _call(name, limit=101)

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    @pytest.mark.parametrize(("offset", "limit"), [(0, 1), (0, 100), (3, 50)])
    def test_boundary_pages_are_accepted(
        self, name: str, offset: int, limit: int, installed_store: OscalStore, notify: MagicMock
    ):
        result = _call(name, offset=offset, limit=limit)
        assert result["offset"] == offset
        assert result["limit"] == limit
        notify.assert_not_called()


class TestInvalidEnumArguments:
    """Requirements 4.2, 4.3, 2.6: direct Python calls bypass the schema `enum`."""

    @pytest.mark.parametrize("name", ["list_documents", "query_documents"])
    @pytest.mark.parametrize("bad", ["", "Catalog", "catalogs", "ssp", "component_definition"])
    def test_invalid_model_type_lists_valid_values(self, name: str, bad: str, notify: MagicMock):
        ctx = object()
        with pytest.raises(ValueError, match="Invalid model_type") as excinfo:
            _call(name, model_type=bad, ctx=ctx)
        for value in MODEL_TYPE_VALUES:
            assert value in str(excinfo.value)
        notify.assert_called_once_with(str(excinfo.value), ctx)

    @pytest.mark.parametrize("name", ["list_documents", "query_documents"])
    @pytest.mark.parametrize("bad", [None, 7, ["catalog"]])
    def test_non_string_model_type_is_rejected_by_type_name(
        self, name: str, bad: object, notify: MagicMock
    ):
        with pytest.raises(ValueError, match="Invalid model_type") as excinfo:
            _call(name, model_type=bad)
        assert f"'{type(bad).__name__}'" in str(excinfo.value)
        notify.assert_called_once()

    @pytest.mark.parametrize("bad", ["", "Control", "controls", "catalog", "oscal-control"])
    def test_invalid_element_type_lists_valid_values(self, bad: str, notify: MagicMock):
        ctx = object()
        with pytest.raises(ValueError, match="Invalid element_type") as excinfo:
            _call("list_child_elements", element_type=bad, ctx=ctx)
        for value in ELEMENT_TYPE_VALUES:
            assert value in str(excinfo.value)
        notify.assert_called_once_with(str(excinfo.value), ctx)

    def test_non_string_element_type_is_rejected(self, notify: MagicMock):
        with pytest.raises(ValueError, match="Invalid element_type"):
            _call("list_child_elements", element_type=None)
        notify.assert_called_once()

    @pytest.mark.parametrize("bad", ["", "by_type", "ALL", "by-uuid", "none"])
    def test_invalid_query_type_lists_valid_values(self, bad: str, notify: MagicMock):
        ctx = object()
        with pytest.raises(ValueError, match="Invalid query_type") as excinfo:
            _call("query_documents", query_type=bad, query_value="x", ctx=ctx)
        for value in QUERY_TYPE_VALUES:
            assert value in str(excinfo.value)
        notify.assert_called_once_with(str(excinfo.value), ctx)

    @pytest.mark.parametrize("query_type", ["by_uuid", "by_title"])
    @pytest.mark.parametrize("missing", [None, ""])
    def test_missing_query_value_names_the_query_type(
        self, query_type: str, missing: str | None, notify: MagicMock
    ):
        ctx = object()
        with pytest.raises(ValueError, match="query_value is required") as excinfo:
            _call("query_documents", query_type=query_type, query_value=missing, ctx=ctx)
        assert query_type in str(excinfo.value)
        notify.assert_called_once_with(str(excinfo.value), ctx)

    def test_all_ignores_a_missing_query_value(
        self, installed_store: OscalStore, notify: MagicMock
    ):
        result = _call("query_documents", query_type="all", query_value=None)
        assert result["total"] >= 1
        notify.assert_not_called()

    def test_long_rejected_value_is_cut_in_the_message(self, notify: MagicMock):
        bad = "x" * 500
        with pytest.raises(ValueError, match="Invalid model_type") as excinfo:
            _call("list_documents", model_type=bad)
        assert bad not in str(excinfo.value)
        assert len(str(excinfo.value)) < 500

    def test_check_order_is_enum_then_page_then_query_value(self, notify: MagicMock):
        """With several problems at once, the first check in the fixed order wins."""
        with pytest.raises(ValueError, match="Invalid model_type"):
            _call("query_documents", model_type="nope", query_type="bogus", offset=-1)
        with pytest.raises(ValueError, match="offset must be non-negative"):
            _call("query_documents", query_type="bogus", offset=-1)
        with pytest.raises(ValueError, match="Invalid query_type"):
            _call("query_documents", query_type="bogus", query_value=None)


class TestUninitialisedStore:
    """Requirement 4.4: an unset singleton raises RuntimeError naming init_store()."""

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_unset_store_raises_runtime_error(self, name: str):
        assert qom._store is None
        with pytest.raises(RuntimeError, match=r"init_store\(\)"):
            _call(name)

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_runtime_error_is_not_notified(self, name: str, notify: MagicMock):
        with pytest.raises(RuntimeError):
            _call(name)
        notify.assert_not_called()

    @pytest.mark.parametrize(
        ("name", "overrides", "error"),
        [
            ("list_documents", {"model_type": "bogus"}, "Invalid model_type"),
            ("query_documents", {"model_type": "bogus"}, "Invalid model_type"),
            ("query_documents", {"query_type": "bogus"}, "Invalid query_type"),
            ("query_documents", {"query_type": "by_uuid"}, "query_value is required"),
            ("list_child_elements", {"element_type": "bogus"}, "Invalid element_type"),
            ("list_documents", {"offset": -1}, "offset must be non-negative"),
            ("query_documents", {"limit": 0}, "limit must be between 1 and 100"),
            ("list_child_elements", {"limit": 101}, "limit must be between 1 and 100"),
        ],
    )
    def test_invalid_arguments_raise_value_error_first(
        self, name: str, overrides: dict[str, Any], error: str
    ):
        assert qom._store is None
        with pytest.raises(ValueError, match=error):
            _call(name, **overrides)

    def test_message_names_the_function_not_a_location(self):
        with pytest.raises(RuntimeError) as excinfo:
            _call("list_documents")
        _assert_no_location(str(excinfo.value), [str(Path.home()), tempfile.gettempdir(), "s3://"])


class TestEmptyResults:
    """Requirements 1.5, 3.8, 7.5: no match is an empty Page_Envelope, not an error."""

    @pytest.fixture
    def cdef_only_store(self, tmp_path: Path) -> Iterator[OscalStore]:
        """A store holding Component Definitions only; every other model has no documents."""
        store = build_fixture_store(tmp_path, load_valid_fixture_cdefs())
        store.text_search("component")  # indexes child elements
        qom.init_store(store)
        yield store
        store.close()

    @pytest.fixture
    def empty_store(self, tmp_path: Path) -> Iterator[OscalStore]:
        """A store with no documents at all."""
        store = build_fixture_store(tmp_path, [])
        qom.init_store(store)
        yield store
        store.close()

    @pytest.mark.parametrize(
        "model_type", [m for m in MODEL_TYPE_VALUES if m != "component-definition"]
    )
    def test_model_with_no_documents_lists_empty(
        self, model_type: str, cdef_only_store: OscalStore
    ):
        _assert_empty_page(_call("list_documents", model_type=model_type))

    @pytest.mark.parametrize(
        "model_type", [m for m in MODEL_TYPE_VALUES if m != "component-definition"]
    )
    def test_model_with_no_documents_queries_empty(
        self, model_type: str, cdef_only_store: OscalStore
    ):
        _assert_empty_page(_call("query_documents", model_type=model_type))

    def test_model_with_documents_is_not_empty(self, cdef_only_store: OscalStore):
        """Guards the fixture: the empty results above are about the model, not the store."""
        assert _call("list_documents", model_type="component-definition")["total"] == 3
        assert _call("query_documents", model_type="component-definition")["total"] == 3

    @pytest.mark.parametrize("query_type", ["by_uuid", "by_title"])
    def test_query_with_no_match_is_empty(self, query_type: str, installed_store: OscalStore):
        result = _call(
            "query_documents", query_type=query_type, query_value=UNKNOWN_UUID, offset=0, limit=5
        )
        _assert_empty_page(result, limit=5)

    @pytest.mark.parametrize("element_type", ELEMENT_TYPE_VALUES)
    def test_unknown_parent_is_empty(self, element_type: str, installed_store: OscalStore):
        result = _call(
            "list_child_elements", element_type=element_type, parent_doc_uuid=UNKNOWN_UUID
        )
        _assert_empty_page(result)

    @pytest.mark.parametrize("parent_doc_uuid", ["", "not-a-uuid", "../etc/passwd"])
    def test_malformed_parent_is_empty(self, parent_doc_uuid: str, installed_store: OscalStore):
        _assert_empty_page(_call("list_child_elements", parent_doc_uuid=parent_doc_uuid))

    def test_parent_without_a_child_of_the_type_is_empty(
        self, installed_store: OscalStore, generic_fixture: GenericFixture
    ):
        """The catalog holds controls but no task (a task belongs to assessment plans)."""
        catalog = generic_fixture.leak.catalog_uuid
        assert _call("list_child_elements", parent_doc_uuid=catalog)["total"] >= 1
        result = _call("list_child_elements", element_type="task", parent_doc_uuid=catalog)
        _assert_empty_page(result)

    def test_parent_of_another_model_is_empty(
        self, installed_store: OscalStore, generic_fixture: GenericFixture
    ):
        profile = generic_fixture.leak.doc_uuids[OSCALModelType.PROFILE]
        _assert_empty_page(
            _call("list_child_elements", element_type="control", parent_doc_uuid=profile)
        )

    @pytest.mark.parametrize("model_type", MODEL_TYPE_VALUES)
    def test_empty_store_lists_no_documents(self, model_type: str, empty_store: OscalStore):
        """Includes component-definition, where list_component_definitions raised (7.5)."""
        _assert_empty_page(_call("list_documents", model_type=model_type))
        _assert_empty_page(_call("query_documents", model_type=model_type))

    @pytest.mark.parametrize("element_type", ELEMENT_TYPE_VALUES)
    def test_empty_store_lists_no_children(self, element_type: str, empty_store: OscalStore):
        """Includes component and capability, where list_components raised (7.5)."""
        _assert_empty_page(_call("list_child_elements", element_type=element_type))

    def test_empty_page_beyond_the_end_keeps_total(self, installed_store: OscalStore):
        """An offset past the last item gives no items but the real total (not an error)."""
        total = _call("list_documents", model_type="catalog")["total"]
        result = _call("list_documents", model_type="catalog", offset=total + 5)
        assert result["items"] == []
        assert result["total"] == total
        assert result["hasMore"] is False


class TestSizeInBytesWithoutJson:
    """Requirement 3.7: a child without stored JSON has ``sizeInBytes`` 0."""

    def test_none_raw_json_gives_zero(self):
        summary = qom._child_summary({"id": "c1", "title": "T", "raw_json": None})
        assert summary == {"id": "c1", "title": "T", "sizeInBytes": 0}

    def test_missing_raw_json_gives_zero(self):
        assert qom._child_summary({"id": "c1"}) == {"id": "c1", "sizeInBytes": 0}

    def test_empty_raw_json_gives_zero(self):
        assert qom._child_summary({"id": "c1", "raw_json": ""})["sizeInBytes"] == 0

    def test_raw_json_is_dropped_and_measured_in_utf8_bytes(self):
        summary = qom._child_summary({"id": "c1", "raw_json": '{"t": "Ü"}'})
        assert "raw_json" not in summary
        assert summary["sizeInBytes"] == len('{"t": "Ü"}'.encode()) == 11

    def test_tool_reports_zero_when_the_store_holds_no_json(
        self,
        installed_store: OscalStore,
        generic_fixture: GenericFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Through the tool: the store's page has ``raw_json`` None for every item."""
        real = installed_store.list_child_elements

        def without_json(**kwargs: Any) -> dict:
            page = real(**kwargs)
            return {**page, "items": [{**i, "raw_json": None} for i in page["items"]]}

        monkeypatch.setattr(installed_store, "list_child_elements", without_json)
        result = _call("list_child_elements", element_type="control", limit=100)
        assert result["items"]
        assert all(item["sizeInBytes"] == 0 for item in result["items"])
        assert all("raw_json" not in item for item in result["items"])


class TestNoFilesystemPath:
    """Requirement 4.6: error messages and results hold no filesystem path."""

    @pytest.mark.parametrize(
        ("name", "overrides"),
        [
            ("list_documents", {"model_type": "bogus"}),
            ("query_documents", {"model_type": "bogus"}),
            ("query_documents", {"query_type": "bogus"}),
            ("query_documents", {"query_type": "by_title"}),
            ("list_child_elements", {"element_type": "bogus"}),
            ("list_documents", {"offset": -1}),
            ("query_documents", {"limit": 0}),
            ("list_child_elements", {"limit": 101}),
        ],
    )
    def test_error_messages_hold_no_location(
        self,
        name: str,
        overrides: dict[str, Any],
        installed_store: OscalStore,
        generic_fixture: GenericFixture,
        notify: MagicMock,
    ):
        with pytest.raises(ValueError) as excinfo:
            _call(name, **overrides)
        needles = _location_needles(generic_fixture)
        _assert_no_location(str(excinfo.value), needles)
        for call in notify.call_args_list:
            _assert_no_location(call.args[0], needles)

    @pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
    def test_results_hold_no_location(
        self, name: str, installed_store: OscalStore, generic_fixture: GenericFixture
    ):
        needles = _location_needles(generic_fixture)
        calls: list[dict[str, Any]] = []
        if name == "list_child_elements":
            calls = [{"element_type": t, "limit": 100} for t in ELEMENT_TYPE_VALUES]
        else:
            calls = [{"model_type": m, "limit": 100} for m in MODEL_TYPE_VALUES]
        for overrides in calls:
            text = json.dumps(_call(name, **overrides), ensure_ascii=False)
            _assert_no_location(text, needles)

    def test_empty_results_hold_no_location(
        self, installed_store: OscalStore, generic_fixture: GenericFixture
    ):
        needles = _location_needles(generic_fixture)
        text = json.dumps(_call("list_child_elements", parent_doc_uuid=UNKNOWN_UUID))
        _assert_no_location(text, needles)


# --- Property tests (tasks 2.6-2.14) ---------------------------------------------------------

DOCUMENT_SUMMARY_KEYS = {"uuid", "title", "model_type", "childCount", "sizeInBytes"}


@pytest.fixture(scope="module")
def cdef_only_generic_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[OscalStore]:
    """A small read-only store holding Component Definitions only.

    Every other Model_Type has no documents here, so it covers the empty-result case.
    """
    store = build_fixture_store(
        tmp_path_factory.mktemp("generic-small"), load_valid_fixture_cdefs()
    )
    try:
        yield store
    finally:
        store.close()


class TestListDocumentsProperty:
    """Property 1: list_documents equals the store."""

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        model_type=st.sampled_from(list(OSCALModelType)),
        offset=st.integers(min_value=0, max_value=30),
        limit=st.integers(min_value=1, max_value=100),
        store_name=st.sampled_from(["full", "small"]),
    )
    def test_list_documents_equals_the_store(
        self,
        reset_oscal_store: None,
        generic_fixture: GenericFixture,
        cdef_only_generic_store: OscalStore,
        model_type: OSCALModelType,
        offset: int,
        limit: int,
        store_name: str,
    ):
        """**Property 1: list_documents equals the store**

        For any Model_Type and Valid_Page, over the full fixture store or the small store
        (empty for every Model_Type except component-definition), the tool result equals
        ``OscalStore.list_documents`` for the same arguments, has the Page_Envelope keys and
        Document_Summary keys, reports the drawn ``model_type`` on every item, and sets
        ``hasMore`` from ``offset + limit < total``.

        **Validates: Requirements 1.3, 1.4, 1.5, 6.1**
        """
        use_small_store = store_name == "small"
        store = cdef_only_generic_store if use_small_store else generic_fixture.store
        qom.init_store(store)

        expected = store.list_documents(oscal_model_type=model_type, offset=offset, limit=limit)
        result = qom.list_documents(model_type=model_type.value, offset=offset, limit=limit)

        event(f"total={'empty' if expected['total'] == 0 else 'non-empty'}")
        assert result == expected
        assert set(result) == PAGE_KEYS
        assert result["offset"] == offset
        assert result["limit"] == limit
        assert result["hasMore"] is (offset + limit < result["total"])
        assert len(result["items"]) <= limit
        for item in result["items"]:
            assert set(item) == DOCUMENT_SUMMARY_KEYS
            assert item["model_type"] == model_type.value
        if use_small_store and model_type is not OSCALModelType.COMPONENT_DEFINITION:
            assert result["total"] == 0
            assert result["items"] == []


QUERY_ITEM_KEYS = {"uuid", "title", "model_type", "sizeInBytes", "childTypeCounts"}
QUERY_VALUE_KINDS = ("uuid", "title", "case_variant", "partial_title", "unknown", "none")


def _all_documents(store: OscalStore) -> list[dict]:
    """Return every document summary (uuid, title) of every Model_Type in ``store``."""
    return [
        item
        for model_type in OSCALModelType
        for item in store.list_documents(oscal_model_type=model_type, limit=100)["items"]
    ]


def _query_value(kind: str, doc: dict | None) -> str | None:
    """Return the ``query_value`` of ``kind`` built from ``doc`` (None for an empty store)."""
    title = doc["title"] if doc else "Sample"
    if kind == "uuid":
        return doc["uuid"] if doc else UNKNOWN_UUID
    if kind == "title":
        return title
    if kind == "case_variant":
        return title.swapcase()
    if kind == "partial_title":
        return title[: max(1, len(title) // 2)]
    if kind == "unknown":
        return "no such document anywhere"
    return None


class TestQueryDocumentsProperty:
    """Property 2: query_documents equals the store."""

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        model_type=st.sampled_from(list(OSCALModelType)),
        query_type=st.sampled_from(QUERY_TYPE_VALUES),
        value_kind=st.sampled_from(QUERY_VALUE_KINDS),
        doc_index=st.integers(min_value=0, max_value=1000),
        offset=st.integers(min_value=0, max_value=30),
        limit=st.integers(min_value=1, max_value=100),
        store_name=st.sampled_from(["full", "small"]),
    )
    def test_query_documents_equals_the_store(
        self,
        reset_oscal_store: None,
        generic_fixture: GenericFixture,
        cdef_only_generic_store: OscalStore,
        model_type: OSCALModelType,
        query_type: Literal["all", "by_uuid", "by_title"],
        value_kind: str,
        doc_index: int,
        offset: int,
        limit: int,
        store_name: str,
    ):
        """**Property 2: query_documents equals the store**

        For any Model_Type, Query_Type, query value (an existing UUID, an existing title, a case
        variant, a partial title, an unknown value, or null), and Valid_Page, the tool result
        equals ``OscalStore.query`` for the same arguments, or both raise ``ValueError``. A
        result has the Page_Envelope keys and Query_Item keys, and every item's ``model_type``
        is the requested Model_Type.

        **Validates: Requirements 2.4, 2.5, 6.2**
        """
        store = cdef_only_generic_store if store_name == "small" else generic_fixture.store
        qom.init_store(store)
        docs = _all_documents(store)  # any model's document, so scoping to model_type is covered
        doc = docs[doc_index % len(docs)] if docs else None
        query_value = _query_value(value_kind, doc)

        try:
            expected = store.query(
                oscal_model_type=model_type,
                query_type=query_type,
                query_value=query_value,
                offset=offset,
                limit=limit,
            )
        except ValueError:
            event("outcome=ValueError")
            with pytest.raises(ValueError, match="query_"):
                qom.query_documents(
                    model_type=model_type.value,
                    query_type=query_type,
                    query_value=query_value,
                    offset=offset,
                    limit=limit,
                )
            return

        result = qom.query_documents(
            model_type=model_type.value,
            query_type=query_type,
            query_value=query_value,
            offset=offset,
            limit=limit,
        )

        event(f"outcome={'empty' if expected['total'] == 0 else 'non-empty'}")
        assert result == expected
        assert set(result) == PAGE_KEYS
        assert result["offset"] == offset
        assert result["limit"] == limit
        assert len(result["items"]) <= limit
        for item in result["items"]:
            assert set(item) == QUERY_ITEM_KEYS
            assert item["model_type"] == model_type.value


CHILD_SUMMARY_SHARED_KEYS = (
    "id",
    "title",
    "element_type",
    "description",
    "parentDocumentTitle",
    "parentDocumentUuid",
)
CHILD_SUMMARY_KEYS = {*CHILD_SUMMARY_SHARED_KEYS, "sizeInBytes"}
PARENT_KINDS = ("none", "existing", "other_model", "random")
# The Model_Type each Element_Type belongs to (an Element_Type is under exactly one model).
ELEMENT_OWNER: dict[str, OSCALModelType] = {
    element_type: model_type
    for model_type, element_types in CHILD_ELEMENT_TYPES.items()
    for element_type in element_types
}


def _model_documents(store: OscalStore) -> dict[OSCALModelType, list[str]]:
    """Return the Document_UUIDs of ``store`` per Model_Type."""
    return {
        model_type: [
            item["uuid"]
            for item in store.list_documents(oscal_model_type=model_type, limit=100)["items"]
        ]
        for model_type in OSCALModelType
    }


class TestListChildElementsProperty:
    """Property 3: list_child_elements equals the store, scoped and summarized."""

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        element_type=st.sampled_from(ELEMENT_TYPE_VALUES),
        parent_kind=st.sampled_from(PARENT_KINDS),
        doc_index=st.integers(min_value=0, max_value=1000),
        random_uuid=st.uuids().map(str),
        offset=st.integers(min_value=0, max_value=30),
        limit=st.integers(min_value=1, max_value=100),
    )
    def test_list_child_elements_equals_the_store(
        self,
        reset_oscal_store: None,
        generic_fixture: GenericFixture,
        element_type: Any,  # one of ELEMENT_TYPE_VALUES; the tool parameter is a Literal
        parent_kind: str,
        doc_index: int,
        random_uuid: str,
        offset: int,
        limit: int,
    ):
        """**Property 3: list_child_elements equals the store, scoped and summarized**

        For any Element_Type, ``parent_doc_uuid`` (none, an existing document, a document of
        another model, or a random UUID), and Valid_Page, the tool result has the same ``total``,
        ``offset``, ``limit``, ``hasMore``, and item order as ``OscalStore.list_child_elements``,
        and each item has the store's six shared fields plus ``sizeInBytes`` and no ``raw_json``.
        A parent that does not exist, or that belongs to another model, gives an empty result.

        **Validates: Requirements 3.4, 3.5, 3.6, 3.8, 3.9, 6.3**
        """
        store = generic_fixture.store
        qom.init_store(store)
        documents = _model_documents(store)
        owner = ELEMENT_OWNER[element_type]
        parent: str | None
        if parent_kind == "none":
            parent = None
        elif parent_kind == "existing":
            all_uuids = [u for uuids in documents.values() for u in uuids]
            parent = all_uuids[doc_index % len(all_uuids)]
        elif parent_kind == "other_model":
            other_uuids = [u for m, uuids in documents.items() if m is not owner for u in uuids]
            parent = other_uuids[doc_index % len(other_uuids)]
        else:
            parent = random_uuid

        expected = store.list_child_elements(
            parent_doc_uuid=parent, element_type=element_type, offset=offset, limit=limit
        )
        with_json = store.list_child_elements(
            parent_doc_uuid=parent,
            element_type=element_type,
            offset=offset,
            limit=limit,
            include_raw_json=True,
        )
        result = qom.list_child_elements(
            element_type=element_type, parent_doc_uuid=parent, offset=offset, limit=limit
        )

        event(f"parent={parent_kind}")
        event(f"total={'empty' if expected['total'] == 0 else 'non-empty'}")
        assert set(result) == PAGE_KEYS
        for key in ("total", "offset", "limit", "hasMore"):
            assert result[key] == expected[key], key
        assert result["offset"] == offset
        assert result["limit"] == limit
        assert result["hasMore"] is (offset + limit < result["total"])
        assert len(result["items"]) == len(expected["items"]) <= limit
        for item, store_item, json_item in zip(
            result["items"], expected["items"], with_json["items"], strict=True
        ):
            assert set(item) == CHILD_SUMMARY_KEYS
            assert "raw_json" not in item
            for key in CHILD_SUMMARY_SHARED_KEYS:
                assert item[key] == store_item[key], key
            assert item["element_type"] == element_type
            assert item["sizeInBytes"] == len(json_item["raw_json"].encode("utf-8"))
        assert [i["id"] for i in result["items"]] == [i["id"] for i in expected["items"]]

        if parent_kind in ("random", "other_model"):
            # A parent that is unknown, or of another model, has no child of this type.
            assert result["total"] == 0
            assert result["items"] == []
            assert result["hasMore"] is False
        if parent is not None:
            assert all(i["parentDocumentUuid"] == parent for i in result["items"])


class TestSizeInBytesProperty:
    """Property 4: sizeInBytes is the UTF-8 length of the stored JSON."""

    def test_sizeinbytes_is_the_utf8_length_of_the_stored_json(
        self, reset_oscal_store: None, generic_fixture: GenericFixture
    ):
        """**Property 4: sizeInBytes is the UTF-8 length of the stored JSON**

        For every Element_Type, Valid_Page, and parent (none, or the extra catalog), each item's
        ``sizeInBytes`` equals ``len(raw_json.encode("utf-8"))`` of the same child from the store
        (``include_raw_json=True``), or 0 when the store holds no ``raw_json``. The multi-byte
        child is hit, and its byte length is greater than its character length.

        **Validates: Requirements 3.7**
        """
        store = generic_fixture.store
        qom.init_store(store)
        multibyte_hits: list[tuple[int, int]] = []
        items_checked = 0

        # The multi-byte control is the only control of the extra catalog, so it is returned only
        # for (control, extra catalog, offset 0). Pin that draw so the hit does not depend on the
        # random draw (about 1 in 870 per example).
        @example(
            element_type="control",
            parent=generic_fixture.extra_catalog_uuid,
            offset=0,
            limit=100,
        )
        @example(
            element_type="control",
            parent=generic_fixture.extra_catalog_uuid,
            offset=0,
            limit=1,
        )
        @settings(max_examples=200, deadline=None)
        @given(
            element_type=st.sampled_from(ELEMENT_TYPE_VALUES),
            parent=st.sampled_from([None, generic_fixture.extra_catalog_uuid]),
            offset=st.integers(min_value=0, max_value=30),
            limit=st.integers(min_value=1, max_value=100),
        )
        def check(
            element_type: Any,  # one of ELEMENT_TYPE_VALUES; the tool parameter is a Literal
            parent: str | None,
            offset: int,
            limit: int,
        ) -> None:
            nonlocal items_checked
            stored = store.list_child_elements(
                parent_doc_uuid=parent,
                element_type=element_type,
                offset=offset,
                limit=limit,
                include_raw_json=True,
            )
            result = qom.list_child_elements(
                element_type=element_type, parent_doc_uuid=parent, offset=offset, limit=limit
            )
            event(f"items={'empty' if not result['items'] else 'non-empty'}")
            assert [i["id"] for i in result["items"]] == [i["id"] for i in stored["items"]]
            for item, stored_item in zip(result["items"], stored["items"], strict=True):
                raw_json = stored_item.get("raw_json")
                expected = len(raw_json.encode("utf-8")) if raw_json else 0
                assert item["sizeInBytes"] == expected, (element_type, item["id"])
                assert "raw_json" not in item
                items_checked += 1
                if item["id"] == generic_fixture.multibyte_control_id:
                    assert item["title"] == generic_fixture.multibyte_control_title
                    multibyte_hits.append((item["sizeInBytes"], len(raw_json)))

        check()

        assert items_checked > 0
        assert multibyte_hits, "the multi-byte child was never returned"
        for size_in_bytes, character_count in multibyte_hits:
            assert size_in_bytes > character_count

    def test_multibyte_child_byte_length_exceeds_character_length(
        self, installed_store: OscalStore, generic_fixture: GenericFixture
    ):
        """Pinned example: the multi-byte control is measured in bytes, not characters."""
        result = _call(
            "list_child_elements",
            element_type="control",
            parent_doc_uuid=generic_fixture.extra_catalog_uuid,
            limit=100,
        )
        stored = installed_store.list_child_elements(
            parent_doc_uuid=generic_fixture.extra_catalog_uuid,
            element_type="control",
            limit=100,
            include_raw_json=True,
        )
        [item] = result["items"]
        [stored_item] = stored["items"]
        raw_json = stored_item["raw_json"]
        assert item["id"] == generic_fixture.multibyte_control_id
        assert item["sizeInBytes"] == len(raw_json.encode("utf-8"))
        assert item["sizeInBytes"] > len(raw_json)


# Titles of the extra Component Definitions of the paging store. They differ only by case (or
# are close in order), so the (lowercased title, UUID) sort key and its UUID tie-break matter.
PAGING_DOC_TITLES = (
    "alpha def",
    "Alpha def",
    "ALPHA DEF",
    "Beta def",
    "beta Def",
    "Éclair def",
    "zeta def",
    "Zeta def",
)
PAGING_CAPABILITIES = 120  # more than the largest limit (100), so one limit needs two pages
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


@pytest.fixture(scope="module")
def paging_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[OscalStore]:
    """Read-only store with enough items to page: 12 Component Definitions (case-variant titles),
    more than 140 Capabilities (tied titles across documents), and Components."""
    big, _ = make_many_capabilities_cdef(PAGING_CAPABILITIES)
    extras = []
    for title in PAGING_DOC_TITLES:
        cdef, _ = make_many_capabilities_cdef(3)
        cdef["component-definition"]["metadata"]["title"] = title
        extras.append(cdef)
    store = build_fixture_store(
        tmp_path_factory.mktemp("generic-paging"), [big, *load_valid_fixture_cdefs(), *extras]
    )
    try:
        yield store
    finally:
        store.close()


def _ascii_lower(text: str | None) -> str:
    """Fold ASCII letters only, as SQLite ``COLLATE NOCASE`` does."""
    return (text or "").translate(_ASCII_LOWER)


def _walk_pages(fetch: Callable[[int, int], dict], limit: int) -> tuple[list[dict], int, int]:
    """Page with offset 0, ``limit``, ``2 * limit``, ... until ``hasMore`` is false.

    Returns ``(items, number of pages, total)``; checks the envelope of every page.
    """
    items: list[dict] = []
    pages = 0
    offset = 0
    first_total: int | None = None
    while True:
        page = fetch(offset, limit)
        pages += 1
        assert (page["offset"], page["limit"]) == (offset, limit)
        first_total = page["total"] if first_total is None else first_total
        assert page["total"] == first_total
        assert len(page["items"]) <= limit
        assert page["hasMore"] is (offset + limit < page["total"])
        items.extend(page["items"])
        if not page["hasMore"]:
            return items, pages, first_total
        offset += limit
        assert pages <= first_total, "paging does not terminate"


class TestPagingProperty:
    """Property 5: paging a Generic_Tool visits every item once, in order."""

    def test_paging_visits_every_item_once_in_order(
        self,
        reset_oscal_store: None,
        generic_fixture: GenericFixture,
        paging_store: OscalStore,
    ):
        """**Property 5: Paging a Generic_Tool visits every item once, in order**

        For each Generic_Tool (``query_documents`` with ``all``), a drawn Model_Type or
        Element_Type, and any ``limit`` from 1 to 100, calling the tool with ``offset`` 0,
        ``limit``, ``2 * limit``, and so on until ``hasMore`` is false returns items whose
        concatenation equals one large store call for the whole list (``limit`` = ``total``),
        has no repeats, and (for the two listers) is ordered by (ASCII-lowercased title,
        identifier). ``query_documents`` keeps the store's order, which the large-call equality
        checks. At least one example pages over more than one page for every tool, and one over
        more than 100 items.

        **Validates: Requirements 1.3, 3.4**
        """
        multi_page_tools: set[str] = set()
        largest_multi_page_total = 0
        examples_run = 0

        def check(
            tool: str, model_type: OSCALModelType, element_type: str, store_name: str, limit: int
        ) -> None:
            nonlocal largest_multi_page_total, examples_run
            examples_run += 1
            store = paging_store if store_name == "paging" else generic_fixture.store
            qom.init_store(store)
            fetch: Callable[[int, int], dict]
            reference: Callable[[int], list[dict]]
            if tool == "list_documents":

                def fetch(offset: int, page_limit: int) -> dict:
                    return qom.list_documents(
                        model_type=model_type.value, offset=offset, limit=page_limit
                    )

                def reference(total: int) -> list[dict]:
                    page = store.list_documents(oscal_model_type=model_type, limit=max(total, 1))
                    return page["items"]

            elif tool == "query_documents":

                def fetch(offset: int, page_limit: int) -> dict:
                    return qom.query_documents(
                        model_type=model_type.value,
                        query_type="all",
                        offset=offset,
                        limit=page_limit,
                    )

                def reference(total: int) -> list[dict]:
                    page = store.query(
                        oscal_model_type=model_type,
                        query_type="all",
                        offset=0,
                        limit=max(total, 1),
                    )
                    return page["items"]

            else:

                def fetch(offset: int, page_limit: int) -> dict:
                    return qom.list_child_elements(
                        element_type=element_type,  # type: ignore[arg-type]
                        offset=offset,
                        limit=page_limit,
                    )

                def reference(total: int) -> list[dict]:
                    page = store.list_child_elements(
                        element_type=element_type, offset=0, limit=max(total, 1)
                    )
                    return page["items"]

            items, pages, total = _walk_pages(fetch, limit)
            event(f"tool={tool}")
            event(f"pages={'one' if pages == 1 else 'several'}")
            if pages > 1:
                multi_page_tools.add(tool)
                largest_multi_page_total = max(largest_multi_page_total, total)

            # Completeness and equality with one large call (child items add sizeInBytes).
            assert len(items) == total
            seen = items
            if tool == "list_child_elements":
                seen = [{k: v for k, v in i.items() if k != "sizeInBytes"} for i in items]
            assert seen == reference(total)

            # No repeats, and the documented order.
            if tool == "list_child_elements":
                identities = [(i["parentDocumentUuid"], i["id"]) for i in items]
                sort_keys = [(_ascii_lower(i["title"]), i["id"]) for i in items]
            else:
                identities = [i["uuid"] for i in items]
                sort_keys = [(_ascii_lower(i["title"]), i["uuid"]) for i in items]
            assert len(set(identities)) == len(identities)
            if tool != "query_documents":
                # Requirements 1.3 and 3.4 fix the order of the two listers. ``query_documents``
                # (all) returns ``OscalStore.query`` order (insertion order, requirement 2.4), so
                # for it the order check is the equality with the large call above.
                assert sort_keys == sorted(sort_keys)

        @settings(max_examples=200, deadline=None)
        @example(
            tool="list_documents",
            model_type=OSCALModelType.COMPONENT_DEFINITION,
            element_type="capability",
            store_name="paging",
            limit=1,
        )
        @example(
            tool="query_documents",
            model_type=OSCALModelType.COMPONENT_DEFINITION,
            element_type="capability",
            store_name="paging",
            limit=3,
        )
        @example(
            tool="list_child_elements",
            model_type=OSCALModelType.COMPONENT_DEFINITION,
            element_type="capability",
            store_name="paging",
            limit=100,
        )
        @given(
            tool=st.sampled_from(GENERIC_TOOL_NAMES),
            model_type=st.sampled_from(list(OSCALModelType)),
            element_type=st.sampled_from(ELEMENT_TYPE_VALUES),
            store_name=st.sampled_from(["generic", "paging"]),
            limit=st.integers(min_value=1, max_value=100),
        )
        def run(
            tool: str,
            model_type: OSCALModelType,
            element_type: str,
            store_name: str,
            limit: int,
        ) -> None:
            check(tool, model_type, element_type, store_name, limit)

        run()

        assert examples_run >= 100
        assert multi_page_tools == set(GENERIC_TOOL_NAMES), "a tool was never paged over 2+ pages"
        assert largest_multi_page_total > PAGING_CAPABILITIES, (
            "no example paged a list longer than the Capability count"
        )


# Valid and invalid halves of a page. Each draw has at least one invalid half.
_BAD_OFFSETS = st.integers(max_value=-1)
_GOOD_OFFSETS = st.integers(min_value=0, max_value=1000)
_BAD_LIMITS = st.one_of(st.integers(max_value=0), st.integers(min_value=101))
_GOOD_LIMITS = st.integers(min_value=1, max_value=100)
_BAD_PAGES_STRATEGY = st.one_of(
    st.tuples(_BAD_OFFSETS, _BAD_LIMITS),  # both invalid
    st.tuples(_BAD_OFFSETS, _GOOD_LIMITS),  # invalid offset, valid limit
    st.tuples(_GOOD_OFFSETS, _BAD_LIMITS),  # valid offset, invalid limit
)


class TestPageRejectionProperty:
    """Property 6: out-of-range pages are rejected before the store is read."""

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(tool=st.sampled_from(GENERIC_TOOL_NAMES), page=_BAD_PAGES_STRATEGY)
    def test_out_of_range_pages_are_rejected_before_the_store_is_read(
        self,
        reset_oscal_store: None,
        tool: str,
        page: tuple[int, int],
    ):
        """**Property 6: Out-of-range pages are rejected before the store is read**

        For any Generic_Tool, otherwise valid arguments, and any ``offset`` below 0 or ``limit``
        below 1 or above 100 (also with only one of the two invalid), the tool notifies the
        client and raises the ``ValueError`` of ``utils.paginate`` for those values. The store
        singleton is unset, so a store read would raise ``RuntimeError`` instead.

        **Validates: Requirements 4.1**
        """
        offset, limit = page
        assert qom._store is None
        ctx = object()
        expected = (
            f"offset must be non-negative, got {offset}"
            if offset < 0
            else f"limit must be between 1 and 100, got {limit}"
        )
        event(f"invalid={'offset' if offset < 0 else 'limit'}")
        event(f"offset={'invalid' if offset < 0 else 'valid'}")
        event(f"limit={'valid' if 1 <= limit <= 100 else 'invalid'}")

        with patch.object(qom, NOTIFY_TARGET) as notify:
            with pytest.raises(ValueError, match=re.escape(expected)) as excinfo:
                _call(tool, offset=offset, limit=limit, ctx=ctx)

        assert str(excinfo.value) == expected
        notify.assert_called_once_with(expected, ctx)


def _text_outside(valid: Sequence[str]) -> st.SearchStrategy[str]:
    """Strings that are not in ``valid``: any text, plus long text that ``shown()`` cuts."""
    return st.one_of(
        st.text(),
        st.text(min_size=300, max_size=400),
    ).filter(lambda s: s not in valid)


class TestInvalidEnumArgumentsProperty:
    """Property 7: invalid enum arguments are rejected with the valid values."""

    @staticmethod
    def _assert_rejected(
        tool: str,
        arg: str,
        bad: str,
        valid: Sequence[str],
        overrides: Mapping[str, Any],
    ) -> None:
        ctx = object()
        with patch.object(qom, NOTIFY_TARGET) as notify:
            with pytest.raises(ValueError, match=f"Invalid {arg}") as excinfo:
                _call(tool, **{arg: bad, **overrides}, ctx=ctx)

        message = str(excinfo.value)
        notify.assert_called_once_with(message, ctx)
        for value in valid:
            assert value in message, f"{value!r} missing from {message!r}"
        if len(bad) > 80:
            # shown() cuts a long draw, so the whole draw never reaches the message.
            event("draw=long")
            assert bad not in message
            assert "..." in message
        else:
            event("draw=short")

    @settings(max_examples=100, deadline=None)
    @given(
        tool=st.sampled_from(["list_documents", "query_documents"]),
        bad=_text_outside(MODEL_TYPE_VALUES),
    )
    def test_invalid_model_type_is_rejected_with_every_model_type(self, tool: str, bad: str):
        """**Property 7: Invalid enum arguments are rejected with the valid values**

        For any string that is not a Model_Type, ``list_documents`` and ``query_documents``
        notify the client and raise a ``ValueError`` whose message contains every Model_Type.

        **Validates: Requirements 4.2, 4.3**
        """
        self._assert_rejected(tool, "model_type", bad, MODEL_TYPE_VALUES, {})

    @settings(max_examples=100, deadline=None)
    @given(bad=_text_outside(ELEMENT_TYPE_VALUES))
    def test_invalid_element_type_is_rejected_with_every_element_type(self, bad: str):
        """**Property 7: Invalid enum arguments are rejected with the valid values**

        For any string that is not an Element_Type, ``list_child_elements`` notifies the
        client and raises a ``ValueError`` whose message contains every Element_Type.

        **Validates: Requirements 4.2, 4.3**
        """
        self._assert_rejected("list_child_elements", "element_type", bad, ELEMENT_TYPE_VALUES, {})

    @settings(max_examples=100, deadline=None)
    @given(bad=_text_outside(QUERY_TYPE_VALUES))
    def test_invalid_query_type_is_rejected_with_every_query_type(self, bad: str):
        """**Property 7: Invalid enum arguments are rejected with the valid values**

        For any string that is not a Query_Type, ``query_documents`` notifies the client and
        raises a ``ValueError`` whose message contains every Query_Type.

        **Validates: Requirements 4.2, 4.3**
        """
        self._assert_rejected(
            "query_documents", "query_type", bad, QUERY_TYPE_VALUES, {"query_value": "x"}
        )


class TestMissingQueryValueProperty:
    """Property 8: a missing query_value is rejected."""

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        model_type=st.sampled_from(MODEL_TYPE_VALUES),
        query_type=st.sampled_from(["by_uuid", "by_title"]),
        query_value=st.sampled_from([None, ""]),
    )
    def test_missing_query_value_is_rejected_naming_the_query_type(
        self,
        reset_oscal_store: None,
        model_type: str,
        query_type: str,
        query_value: str | None,
    ):
        """**Property 8: A missing query_value is rejected**

        For any Model_Type, any ``query_type`` of ``by_uuid`` or ``by_title``, and a
        ``query_value`` of ``None`` or ``""``, ``query_documents`` notifies the client and
        raises a ``ValueError`` whose message contains the ``query_type``. The store
        singleton is unset, so a store read would raise ``RuntimeError`` instead.

        **Validates: Requirements 2.6**
        """
        assert qom._store is None
        ctx = object()
        event(f"query_type={query_type}")
        event(f"query_value={query_value!r}")

        with patch.object(qom, NOTIFY_TARGET) as notify:
            with pytest.raises(ValueError, match=re.escape(query_type)) as excinfo:
                _call(
                    "query_documents",
                    model_type=model_type,
                    query_type=query_type,
                    query_value=query_value,
                    ctx=ctx,
                )

        notify.assert_called_once_with(str(excinfo.value), ctx)


def _pydantic_models_in(value: Any, path: str = "result") -> list[str]:
    """Return the paths of every Pydantic model instance in ``value`` (walks dicts and lists)."""
    if isinstance(value, pydantic.BaseModel):
        return [path]
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            found.extend(_pydantic_models_in(item, f"{path}[{key!r}]"))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found.extend(_pydantic_models_in(item, f"{path}[{index}]"))
    return found


class TestPlainJsonResultsProperty:
    """Property 9: results are plain JSON."""

    def test_walker_finds_a_nested_pydantic_model(self):
        """The walker reports a model hidden in a dict and a list (it is not vacuous)."""

        class Sample(pydantic.BaseModel):
            name: str

        nested = {"items": [{"a": 1}, {"b": [Sample(name="x")]}]}
        assert _pydantic_models_in(nested) == ["result['items'][1]['b'][0]"]
        assert _pydantic_models_in({"items": [{"a": 1}]}) == []

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        tool=st.sampled_from(GENERIC_TOOL_NAMES),
        model_type=st.sampled_from(MODEL_TYPE_VALUES),
        element_type=st.sampled_from(ELEMENT_TYPE_VALUES),
        query_type=st.sampled_from(QUERY_TYPE_VALUES),
        value_kind=st.sampled_from(QUERY_VALUE_KINDS),
        parent_kind=st.sampled_from(["none", "existing", "random"]),
        doc_index=st.integers(min_value=0, max_value=1000),
        random_uuid=st.uuids().map(str),
        offset=st.integers(min_value=0, max_value=30),
        limit=st.integers(min_value=1, max_value=100),
    )
    def test_results_are_plain_json(
        self,
        reset_oscal_store: None,
        generic_fixture: GenericFixture,
        tool: str,
        model_type: Any,  # one of MODEL_TYPE_VALUES; the tool parameter is a Literal
        element_type: Any,  # one of ELEMENT_TYPE_VALUES; the tool parameter is a Literal
        query_type: Any,  # one of QUERY_TYPE_VALUES; the tool parameter is a Literal
        value_kind: str,
        parent_kind: str,
        doc_index: int,
        random_uuid: str,
        offset: int,
        limit: int,
    ):
        """**Property 9: Results are plain JSON**

        For a valid call to any Generic_Tool (any Model_Type or Element_Type, parent, Valid_Page,
        and query arguments) over the full fixture store, which includes the multi-byte child,
        the result survives ``json.loads(json.dumps(result))`` unchanged (so it holds only
        dicts, lists, strings, numbers, booleans, and null), and no value in it, at any depth,
        is a Pydantic model instance.

        **Validates: Requirements 4.5**
        """
        store = generic_fixture.store
        qom.init_store(store)
        docs = _all_documents(store)
        doc = docs[doc_index % len(docs)]
        event(f"tool={tool}")

        if tool == "list_documents":
            result = qom.list_documents(model_type=model_type, offset=offset, limit=limit)
        elif tool == "query_documents":
            # A valid call: by_uuid and by_title need a value; ``all`` takes any value or none.
            if query_type == "by_uuid":
                value = doc["uuid"] if value_kind != "unknown" else UNKNOWN_UUID
            elif query_type == "by_title":
                value = _query_value(value_kind if value_kind != "none" else "title", doc)
            else:
                value = _query_value(value_kind, doc)
            result = qom.query_documents(
                model_type=model_type,
                query_type=query_type,
                query_value=value,
                offset=offset,
                limit=limit,
            )
        else:
            parent = {"none": None, "existing": doc["uuid"], "random": random_uuid}[parent_kind]
            result = qom.list_child_elements(
                element_type=element_type, parent_doc_uuid=parent, offset=offset, limit=limit
            )

        event(f"items={'empty' if not result['items'] else 'non-empty'}")
        assert isinstance(result, dict)
        assert result == json.loads(json.dumps(result))
        assert _pydantic_models_in(result) == []

    def test_multibyte_child_result_is_plain_json(
        self, installed_store: OscalStore, generic_fixture: GenericFixture
    ):
        """Pinned example: the multi-byte child round-trips through JSON unchanged."""
        result = _call(
            "list_child_elements",
            element_type="control",
            parent_doc_uuid=generic_fixture.extra_catalog_uuid,
            limit=100,
        )
        assert [i["id"] for i in result["items"]] == [generic_fixture.multibyte_control_id]
        assert result == json.loads(json.dumps(result, ensure_ascii=False))
        assert _pydantic_models_in(result) == []
