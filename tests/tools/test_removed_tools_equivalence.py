"""Equivalence of the Generic_Tools and the Removed_Tools (consolidate-tools, #40).

For each replaced tool this module calls the Generic_Tool through the Mapping_Table arguments
(``Replacement.kwargs()``) and compares the result to the frozen reference module
(``tests/removed_tools_reference.py``), which holds the logic of the removed tools. The store is
the Fixture_Store of ``build_leak_store``: it holds a document of every Model_Type and a child of
every Element_Type, and the tests assert that, so no row passes on empty results.

Sections:

- Per-model rows (the 26 Removed_Per_Model_Tools): Properties 1, 2, and 3 in oracle form.
- Component Definition rows (the three Removed_CDef_Listers ``list_component_definitions``,
  ``list_components``, ``list_capabilities``): key renames, item order, and empty pages, over
  the Component Definition fixtures of ``test_query_component_definition.py`` (the
  ``fixture_store`` and ``many_capabilities_store`` fixtures) and the Fixture_Store. Property 10.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools import query_component_definition as qcd
from mcp_server_for_oscal.tools import query_oscal_models as qom
from mcp_server_for_oscal.tools.oscal_store import CHILD_ELEMENT_TYPES, OscalStore
from mcp_server_for_oscal.tools.utils import OSCALModelType

from ..fixture_store import LeakFixture, build_fixture_store, build_leak_store, load_sample_cdef
from ..removed_tools import REMOVED_TOOLS, Replacement
from ..removed_tools_reference import (
    old_child_summary,
    old_list_capabilities,
    old_list_children,
    old_list_component_definitions,
    old_list_components,
    old_list_documents,
    old_query,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pytestmark = pytest.mark.unit

UNKNOWN_UUID = "00000000-0000-4000-8000-000000000000"

# The three Removed_CDef_Listers are covered by the Component Definition section below.
_CDEF_ROWS = frozenset({"list_component_definitions", "list_components", "list_capabilities"})

PER_MODEL_ROWS = [name for name in REMOVED_TOOLS if name not in _CDEF_ROWS]
QUERY_ROWS = [n for n in PER_MODEL_ROWS if REMOVED_TOOLS[n].tool == "query_documents"]
LIST_DOC_ROWS = [n for n in PER_MODEL_ROWS if REMOVED_TOOLS[n].tool == "list_documents"]
CHILD_ROWS = [n for n in PER_MODEL_ROWS if REMOVED_TOOLS[n].tool == "list_child_elements"]

# Valid_Page values. Small values hit the few fixture items; large ones hit the bounds.
OFFSETS = st.one_of(st.integers(0, 3), st.integers(0, 30))
LIMITS = st.one_of(st.integers(1, 3), st.integers(1, 100))

# Each test uses the module-scoped Fixture_Store through a function-scoped fixture that installs
# it into the store singletons; the installation is identical for every Hypothesis example.
PROPERTY_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def test_per_model_rows_are_the_26_removed_per_model_tools() -> None:
    assert len(PER_MODEL_ROWS) == 26
    assert len(QUERY_ROWS) == 7
    assert len(LIST_DOC_ROWS) == 7
    assert len(CHILD_ROWS) == 12
    assert sorted(PER_MODEL_ROWS) == sorted(QUERY_ROWS + LIST_DOC_ROWS + CHILD_ROWS)


# ------------------------------------------------------------------
# Fixtures and helpers
# ------------------------------------------------------------------


@pytest.fixture(scope="module")
def leak(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeakFixture]:
    fixture = build_leak_store(tmp_path_factory.mktemp("equivalence"))
    try:
        yield fixture
    finally:
        fixture.store.close()


@pytest.fixture
def wired(leak: LeakFixture) -> Iterator[LeakFixture]:
    """Install the Fixture_Store in both tool modules, restoring the singletons after."""
    saved_qom, saved_qcd = qom._store, qcd._oscal_store
    qom.init_store(leak.store)
    qcd.init_store(leak.store)
    try:
        yield leak
    finally:
        qom._store, qcd._oscal_store = saved_qom, saved_qcd


def _tool(replacement: Replacement) -> Callable[..., dict]:
    """Return the Generic_Tool that ``replacement`` names."""
    tool: Callable[..., dict] = getattr(qom, replacement.tool)
    return tool


def _model(replacement: Replacement) -> OSCALModelType:
    return OSCALModelType(replacement.kwargs()["model_type"])


def _element_type(replacement: Replacement) -> str:
    return replacement.kwargs()["element_type"]


def _doc_title(store: OscalStore, model_type: OSCALModelType, doc_uuid: str) -> str:
    return store.query(oscal_model_type=model_type, query_type="by_uuid", query_value=doc_uuid)[
        "items"
    ][0]["title"]


def _all_parents(leak: LeakFixture) -> list[str | None]:
    """Every `parent_doc_uuid` worth trying: no scope, every document, and an unknown UUID."""
    return [
        None,
        *leak.doc_uuids.values(),
        *leak.bundled_cdef_uuids,
        *leak.user_cdef_uuids,
        UNKNOWN_UUID,
    ]


def _assert_child_page_matches(new: dict, old: dict) -> None:
    """Check the Page_Envelope fields and that each new item holds every old key and value."""
    for key in ("total", "offset", "limit", "hasMore"):
        assert new[key] == old[key], key
    assert set(new) == set(old)
    assert len(new["items"]) == len(old["items"])
    for new_item, old_item in zip(new["items"], old["items"], strict=True):
        assert new_item.items() >= old_item.items()
        assert set(new_item) - set(old_item) == {"sizeInBytes"}
        assert "raw_json" not in new_item
        assert isinstance(new_item["sizeInBytes"], int)
        assert new_item["sizeInBytes"] >= 0


# ------------------------------------------------------------------
# Fixture coverage: every Model_Type and Element_Type has a fixture
# ------------------------------------------------------------------


def test_fixture_store_covers_every_model_type_and_element_type(wired: LeakFixture) -> None:
    for model_type in OSCALModelType:
        assert old_list_documents(wired.store, model_type, 0, 10)["total"] >= 1, model_type
    element_types = {t for types in CHILD_ELEMENT_TYPES.values() for t in types}
    for element_type in element_types:
        assert old_list_children(wired.store, element_type, None, 0, 10)["total"] >= 1, element_type
    covered = {_model(REMOVED_TOOLS[n]) for n in QUERY_ROWS + LIST_DOC_ROWS}
    covered_children = {_element_type(REMOVED_TOOLS[n]) for n in CHILD_ROWS}
    assert covered == set(OSCALModelType) - {OSCALModelType.COMPONENT_DEFINITION}
    assert covered_children <= element_types


# ------------------------------------------------------------------
# Property 1: list_documents equals the store (oracle form)
# ------------------------------------------------------------------


@pytest.mark.parametrize("name", LIST_DOC_ROWS)
def test_list_documents_matches_oracle_with_documents(name: str, wired: LeakFixture) -> None:
    """The fixture holds a document of this Model_Type, and the first page equals the oracle."""
    replacement = REMOVED_TOOLS[name]
    old = old_list_documents(wired.store, _model(replacement), 0, 100)
    assert old["total"] >= 1
    assert _tool(replacement)(**replacement.kwargs(), offset=0, limit=100) == old


@pytest.mark.parametrize("name", LIST_DOC_ROWS)
@PROPERTY_SETTINGS
@given(offset=OFFSETS, limit=LIMITS)
def test_property_list_documents_equals_oracle(
    name: str, wired: LeakFixture, offset: int, limit: int
) -> None:
    """**Property 1: list_documents equals the store** (oracle form).

    **Validates: Requirements 6.1, 6.4**
    """
    replacement = REMOVED_TOOLS[name]
    new = _tool(replacement)(**replacement.kwargs(), offset=offset, limit=limit)
    assert new == old_list_documents(wired.store, _model(replacement), offset, limit)
    assert new["offset"] == offset
    assert new["limit"] == limit


# ------------------------------------------------------------------
# Property 2: query_documents equals the store (oracle form)
# ------------------------------------------------------------------

_QUERY_KINDS = (
    "all",
    "all-with-value",
    "uuid",
    "uuid-upper",
    "uuid-unknown",
    "title",
    "title-upper",
    "title-prefix",
    "title-miss",
    "random",
)


def _query_args(
    kind: str, text: str, wired: LeakFixture, model_type: OSCALModelType
) -> tuple[str, str | None]:
    """Resolve a drawn kind to a (query_type, query_value) pair for ``model_type``."""
    doc_uuid = wired.doc_uuids[model_type]
    title = _doc_title(wired.store, model_type, doc_uuid)
    pairs: dict[str, tuple[str, str | None]] = {
        "all": ("all", None),
        "all-with-value": ("all", text),
        "uuid": ("by_uuid", doc_uuid),
        "uuid-upper": ("by_uuid", doc_uuid.upper()),
        "uuid-unknown": ("by_uuid", UNKNOWN_UUID),
        "title": ("by_title", title),
        "title-upper": ("by_title", title.upper()),
        "title-prefix": ("by_title", title[:3]),
        "title-miss": ("by_title", "no such title"),
        "random": ("by_title", text or "x"),
    }
    return pairs[kind]


@pytest.mark.parametrize("name", QUERY_ROWS)
def test_query_documents_finds_the_fixture_document(name: str, wired: LeakFixture) -> None:
    """The fixture holds a document of this Model_Type, found by UUID, and equal to the oracle."""
    replacement = REMOVED_TOOLS[name]
    model_type = _model(replacement)
    query_type, query_value = _query_args("uuid", "", wired, model_type)
    new = _tool(replacement)(
        **replacement.kwargs(), query_type=query_type, query_value=query_value, offset=0, limit=10
    )
    assert new["total"] == 1
    assert new == old_query(wired.store, model_type, query_type, query_value, 0, 10)
    # The defaults equal an unpaged "all" query.
    assert _tool(replacement)(**replacement.kwargs()) == old_query(
        wired.store, model_type, "all", None, 0, 10
    )


@pytest.mark.parametrize("name", QUERY_ROWS)
@PROPERTY_SETTINGS
@given(
    kind=st.sampled_from(_QUERY_KINDS),
    text=st.text(max_size=20),
    offset=OFFSETS,
    limit=LIMITS,
)
def test_property_query_documents_equals_oracle(
    name: str, wired: LeakFixture, kind: str, text: str, offset: int, limit: int
) -> None:
    """**Property 2: query_documents equals the store** (oracle form).

    **Validates: Requirements 6.2, 6.4**
    """
    replacement = REMOVED_TOOLS[name]
    model_type = _model(replacement)
    query_type, query_value = _query_args(kind, text, wired, model_type)
    new = _tool(replacement)(
        **replacement.kwargs(),
        query_type=query_type,
        query_value=query_value,
        offset=offset,
        limit=limit,
    )
    assert new == old_query(wired.store, model_type, query_type, query_value, offset, limit)


# ------------------------------------------------------------------
# Property 3: list_child_elements equals the store, scoped and summarized (oracle form)
# ------------------------------------------------------------------


@pytest.mark.parametrize("name", CHILD_ROWS)
def test_list_child_elements_finds_the_fixture_children(name: str, wired: LeakFixture) -> None:
    """The fixture holds a child of this Element_Type, and the page equals the oracle."""
    replacement = REMOVED_TOOLS[name]
    element_type = _element_type(replacement)
    old = old_list_children(wired.store, element_type, None, 0, 100)
    assert old["items"], f"fixture store has no {element_type} child"
    new = _tool(replacement)(**replacement.kwargs(), offset=0, limit=100)
    _assert_child_page_matches(new, old)
    # The defaults equal an unscoped first page of 10.
    _assert_child_page_matches(
        _tool(replacement)(**replacement.kwargs()),
        old_list_children(wired.store, element_type, None, 0, 10),
    )


@pytest.mark.parametrize("name", CHILD_ROWS)
@PROPERTY_SETTINGS
@given(data=st.data(), offset=OFFSETS, limit=LIMITS)
def test_property_list_child_elements_equals_oracle(
    name: str, wired: LeakFixture, data: st.DataObject, offset: int, limit: int
) -> None:
    """**Property 3: list_child_elements equals the store, scoped and summarized** (oracle form).

    **Validates: Requirements 6.3, 6.4**
    """
    replacement = REMOVED_TOOLS[name]
    parent = data.draw(
        st.one_of(
            st.sampled_from(_all_parents(wired)),
            st.uuids().map(str),
        ),
        label="parent_doc_uuid",
    )
    new = _tool(replacement)(
        **replacement.kwargs(), parent_doc_uuid=parent, offset=offset, limit=limit
    )
    old = old_list_children(wired.store, _element_type(replacement), parent, offset, limit)
    _assert_child_page_matches(new, old)


@pytest.mark.parametrize("name", CHILD_ROWS)
def test_list_child_elements_scopes_to_the_parent_document(name: str, wired: LeakFixture) -> None:
    """A parent of another model gives an empty page, and an unknown parent too."""
    replacement = REMOVED_TOOLS[name]
    element_type = _element_type(replacement)
    owner = next(m for m, types in CHILD_ELEMENT_TYPES.items() if element_type in types)
    other = next(m for m in OSCALModelType if m is not owner)
    tool: Any = _tool(replacement)
    scoped = tool(**replacement.kwargs(), parent_doc_uuid=wired.doc_uuids[owner], limit=100)
    assert scoped["total"] >= 1
    assert {item["parentDocumentUuid"] for item in scoped["items"]} == {wired.doc_uuids[owner]}
    for parent in (wired.doc_uuids[other], UNKNOWN_UUID):
        empty = tool(**replacement.kwargs(), parent_doc_uuid=parent, limit=100)
        assert empty["items"] == []
        assert empty["total"] == 0
        assert empty["hasMore"] is False


# ==================================================================
# Component Definition rows: the three Removed_CDef_Listers
# ==================================================================

# Reference function per Removed_CDef_Lister. All take (store, ctx, offset, limit).
CDEF_REFERENCES: dict[str, Callable[..., dict]] = {
    "list_component_definitions": old_list_component_definitions,
    "list_components": old_list_components,
    "list_capabilities": old_list_capabilities,
}
CDEF_ROWS = list(CDEF_REFERENCES)
# The old item key that held the child's name (the new item always uses `title`).
OLD_NAME_KEY = {"list_components": "title", "list_capabilities": "name"}
CHILD_ELEMENT_OF = {"list_components": "component", "list_capabilities": "capability"}

DOCUMENT_ITEM_KEYS = {"uuid", "title", "model_type", "childCount", "sizeInBytes"}
CHILD_ITEM_KEYS = {
    "id",
    "title",
    "element_type",
    "description",
    "parentDocumentTitle",
    "parentDocumentUuid",
    "sizeInBytes",
}
PAGE_KEYS = {"items", "total", "offset", "limit", "hasMore"}
EMPTY_PAGE = {"items": [], "total": 0, "offset": 0, "limit": 10, "hasMore": False}


@pytest.fixture(params=["fixture_store", "many_capabilities_store", "leak_store"])
def cdef_store(request: pytest.FixtureRequest) -> OscalStore:
    """A store with Component Definitions, installed in both tool modules.

    ``fixture_store`` (3 definitions, 5 components, 1 capability) and
    ``many_capabilities_store`` (1 definition, 1 component, 120 capabilities) are the fixtures of
    ``test_query_component_definition.py``. ``leak_store`` is the Fixture_Store (5 definitions
    from two Origins).
    """
    if request.param == "leak_store":
        leak: LeakFixture = request.getfixturevalue("wired")
        return leak.store
    value = request.getfixturevalue(request.param)
    return value.store if request.param == "many_capabilities_store" else value


@pytest.fixture
def empty_store(tmp_path: Path) -> Iterator[OscalStore]:
    """An empty store installed in both tool modules."""
    store = OscalStore(db_path=str(tmp_path / "empty.db"), cache_size=10, seed_from_bundled=False)
    saved_qom, saved_qcd = qom._store, qcd._oscal_store
    qom.init_store(store)
    qcd.init_store(store)
    try:
        yield store
    finally:
        qom._store, qcd._oscal_store = saved_qom, saved_qcd
        store.close()


def _cdef_tool(name: str) -> Callable[..., dict]:
    """Return the Generic_Tool call for a Removed_CDef_Lister, with its Mapping_Table arguments."""
    replacement = REMOVED_TOOLS[name]
    tool = _tool(replacement)

    def call(**page: int) -> dict:
        return tool(**replacement.kwargs(), **page)

    return call


def _component_plus_capability_count(store: OscalStore, parent_uuid: str) -> int:
    return sum(
        store.list_child_elements(parent_doc_uuid=parent_uuid, element_type=t, limit=1)["total"]
        for t in ("component", "capability")
    )


def _assert_cdef_page_equivalent(name: str, store: OscalStore, new: dict, old: dict) -> None:
    """Check Page_Envelope equality, item order, and the key renames for one lister."""
    assert set(new) == set(old) == PAGE_KEYS
    for key in ("total", "offset", "limit", "hasMore"):
        assert new[key] == old[key], key
    assert len(new["items"]) == len(old["items"])
    id_key = "uuid" if name == "list_component_definitions" else "id"
    assert [i[id_key] for i in new["items"]] == [i["uuid"] for i in old["items"]]  # item order
    for new_item, old_item in zip(new["items"], old["items"], strict=True):
        if name == "list_component_definitions":
            assert set(new_item) == DOCUMENT_ITEM_KEYS
            assert new_item["model_type"] == "component-definition"
            assert old_item["importedComponentDefinitionsCount"] == 0  # removed, no replacement
            assert "importedComponentDefinitionsCount" not in new_item
            assert new_item["childCount"] == old_item["componentCount"]
            assert new_item["childCount"] == _component_plus_capability_count(
                store, new_item["uuid"]
            )
            renamed = {
                "uuid": new_item["uuid"],
                "title": new_item["title"],
                "componentCount": new_item["childCount"],
                "importedComponentDefinitionsCount": old_item["importedComponentDefinitionsCount"],
                "sizeInBytes": new_item["sizeInBytes"],
            }
        else:
            assert set(new_item) == CHILD_ITEM_KEYS
            assert new_item["element_type"] == CHILD_ELEMENT_OF[name]
            renamed = {
                "uuid": new_item["id"],
                OLD_NAME_KEY[name]: new_item["title"],
                "parentComponentDefinitionTitle": new_item["parentDocumentTitle"],
                "parentComponentDefinitionUuid": new_item["parentDocumentUuid"],
                "sizeInBytes": new_item["sizeInBytes"],
            }
        assert renamed == old_item
        assert isinstance(new_item["sizeInBytes"], int)


def test_cdef_rows_are_the_three_removed_cdef_listers() -> None:
    assert set(CDEF_ROWS) == _CDEF_ROWS
    assert [REMOVED_TOOLS[n].tool for n in CDEF_ROWS] == [
        "list_documents",
        "list_child_elements",
        "list_child_elements",
    ]


# ------------------------------------------------------------------
# Key renames, totals, hasMore, item order, sizeInBytes
# ------------------------------------------------------------------


@pytest.mark.parametrize("name", CDEF_ROWS)
def test_cdef_lister_matches_oracle_with_renamed_keys(name: str, cdef_store: OscalStore) -> None:
    """Full first page, a default page, and a one-item page equal the oracle after the renames."""
    reference = CDEF_REFERENCES[name]
    call = _cdef_tool(name)
    old_all = reference(cdef_store, None, 0, 100)
    assert old_all["total"] >= 1, "fixture store has nothing to list"
    _assert_cdef_page_equivalent(name, cdef_store, call(offset=0, limit=100), old_all)
    _assert_cdef_page_equivalent(name, cdef_store, call(), reference(cdef_store, None, 0, 10))
    _assert_cdef_page_equivalent(
        name, cdef_store, call(offset=0, limit=1), reference(cdef_store, None, 0, 1)
    )


def test_cdef_fixture_stores_have_the_documented_contents(cdef_store: OscalStore) -> None:
    """Every fixture store holds a definition and a component; capabilities exist somewhere."""
    assert old_list_component_definitions(cdef_store, None, 0, 100)["total"] >= 1
    assert old_list_components(cdef_store, None, 0, 100)["total"] >= 1
    assert old_list_capabilities(cdef_store, None, 0, 100)["total"] >= 1


@pytest.mark.parametrize("name", ["list_components", "list_capabilities"])
def test_old_child_summary_maps_store_items_onto_the_new_items(
    name: str, cdef_store: OscalStore
) -> None:
    """The frozen `old_child_summary` of each store item equals the renamed new item."""
    element_type = CHILD_ELEMENT_OF[name]
    store_items = cdef_store.list_child_elements(
        element_type=element_type, limit=100, include_raw_json=True
    )["items"]
    new_items = _cdef_tool(name)(offset=0, limit=100)["items"]
    assert len(store_items) == len(new_items) >= 1
    for store_item, new_item in zip(store_items, new_items, strict=True):
        old_item = old_child_summary(store_item, OLD_NAME_KEY[name])
        assert old_item["uuid"] == new_item["id"]
        assert old_item[OLD_NAME_KEY[name]] == new_item["title"]
        assert old_item["parentComponentDefinitionTitle"] == new_item["parentDocumentTitle"]
        assert old_item["parentComponentDefinitionUuid"] == new_item["parentDocumentUuid"]
        assert old_item["sizeInBytes"] == new_item["sizeInBytes"] > 0


# ------------------------------------------------------------------
# Empty results (7.5)
# ------------------------------------------------------------------


@pytest.mark.parametrize("name", ["list_component_definitions", "list_components"])
def test_empty_store_old_listers_raised_and_generic_tools_return_an_empty_page(
    name: str, empty_store: OscalStore
) -> None:
    with pytest.raises(RuntimeError, match="No "):
        CDEF_REFERENCES[name](empty_store, None, 0, 10)
    assert _cdef_tool(name)() == EMPTY_PAGE


def test_empty_store_list_capabilities_equals_the_oracle(empty_store: OscalStore) -> None:
    """The old capability lister already returned an empty page, and the new tool equals it."""
    assert old_list_capabilities(empty_store, None, 0, 10) == EMPTY_PAGE
    assert _cdef_tool("list_capabilities")() == EMPTY_PAGE


def test_definition_without_components_old_list_components_raised(tmp_path: Path) -> None:
    """A definition with no Components or Capabilities: `list_components` raised, now empty."""
    cdef = load_sample_cdef()
    cdef["component-definition"].pop("components")
    store = build_fixture_store(tmp_path, [cdef])
    saved_qom, saved_qcd = qom._store, qcd._oscal_store
    qom.init_store(store)
    qcd.init_store(store)
    try:
        with pytest.raises(RuntimeError, match="No Components"):
            old_list_components(store, None, 0, 10)
        assert _cdef_tool("list_components")() == EMPTY_PAGE
        assert _cdef_tool("list_capabilities")() == EMPTY_PAGE
        docs = _cdef_tool("list_component_definitions")()
        assert docs["total"] == 1
        assert docs["items"][0]["childCount"] == 0
        _assert_cdef_page_equivalent(
            "list_component_definitions",
            store,
            docs,
            old_list_component_definitions(store, None, 0, 10),
        )
    finally:
        qom._store, qcd._oscal_store = saved_qom, saved_qcd
        store.close()


# ------------------------------------------------------------------
# Property 10: Component Definition listers map onto the Generic_Tools
# ------------------------------------------------------------------


@pytest.mark.parametrize("name", CDEF_ROWS)
@PROPERTY_SETTINGS
@given(offset=OFFSETS, limit=LIMITS)
def test_property_cdef_listers_map_onto_generic_tools(
    name: str, cdef_store: OscalStore, offset: int, limit: int
) -> None:
    """**Property 10: Component Definition listers map onto the Generic_Tools**.

    For any Valid_Page, the Generic_Tool named by the Mapping_Table equals the frozen lister after
    the key renames: same `total`, `offset`, `limit`, `hasMore`, item order, and `sizeInBytes`.

    **Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5, 7.8**
    """
    old = CDEF_REFERENCES[name](cdef_store, None, offset, limit)
    new = _cdef_tool(name)(offset=offset, limit=limit)
    _assert_cdef_page_equivalent(name, cdef_store, new, old)
