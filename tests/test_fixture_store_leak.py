"""Sanity checks for ``build_leak_store``, the Path_Leak_Test Fixture_Store (Requirement 5.1)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_server_for_oscal.tools.oscal_store import (
    CHILD_ELEMENT_TYPES,
    ORIGIN_BUNDLED,
    ORIGIN_USER,
)
from mcp_server_for_oscal.tools.utils import OSCALModelType

from .fixture_store import VALID_CDEF_FILES, LeakFixture, build_leak_store

pytestmark = pytest.mark.unit


@pytest.fixture
def leak(tmp_path: Path) -> Iterator[LeakFixture]:
    fixture = build_leak_store(tmp_path)
    try:
        yield fixture
    finally:
        fixture.store.close()


def _origin_of(fixture: LeakFixture, doc_uuid: str) -> str:
    row = fixture.store._conn.execute(
        "SELECT origin FROM documents WHERE uuid = ?", (doc_uuid,)
    ).fetchone()
    assert row is not None, doc_uuid
    return row["origin"]


def test_layout(leak: LeakFixture) -> None:
    assert leak.db_path == leak.root / "db" / "store.db"
    assert leak.db_path.is_file()
    assert leak.bundled_dir == leak.root / "bundled"
    assert leak.user_docs_dir == leak.root / "user-docs"
    assert leak.valid_json_file.is_file()
    assert leak.valid_json_file.parent == leak.user_docs_dir


def test_origins(leak: LeakFixture) -> None:
    # 8 model fixtures + 1 zip cdef + awesome list + overview.
    assert leak.store.count_documents(origin=ORIGIN_BUNDLED) == len(OSCALModelType) + 3
    # 3 cdef fixtures + user guide + overview.
    assert leak.store.count_documents(origin=ORIGIN_USER) == len(VALID_CDEF_FILES) + 2
    for doc_uuid in (*leak.doc_uuids.values(), *leak.bundled_cdef_uuids):
        assert _origin_of(leak, doc_uuid) == ORIGIN_BUNDLED
    for doc_uuid in leak.user_cdef_uuids:
        assert _origin_of(leak, doc_uuid) == ORIGIN_USER
    for doc_id in (leak.bundled_overview_doc_id, leak.awesome_doc_id):
        assert _origin_of(leak, doc_id) == ORIGIN_BUNDLED
    for doc_id in (leak.user_overview_doc_id, leak.user_guide_doc_id):
        assert _origin_of(leak, doc_id) == ORIGIN_USER


def test_overview_doc_ids_differ_per_origin(leak: LeakFixture) -> None:
    """Requirement 2.3: same Relative_Name, different Origins, different Doc_IDs."""
    assert leak.bundled_overview_doc_id != leak.user_overview_doc_id
    bundled = leak.store.get_documentation(leak.bundled_overview_doc_id)
    user = leak.store.get_documentation(leak.user_overview_doc_id)
    assert bundled is not None
    assert user is not None
    assert bundled.lines != user.lines


def test_document_ids_resolve(leak: LeakFixture) -> None:
    assert set(leak.doc_uuids) == set(OSCALModelType)
    for model_type, doc_uuid in leak.doc_uuids.items():
        page = leak.store.query(
            oscal_model_type=model_type, query_type="by_uuid", query_value=doc_uuid
        )
        assert [item["uuid"] for item in page["items"]] == [doc_uuid]
    for doc_uuid in (*leak.bundled_cdef_uuids, *leak.user_cdef_uuids):
        page = leak.store.query(
            oscal_model_type=OSCALModelType.COMPONENT_DEFINITION,
            query_type="by_uuid",
            query_value=doc_uuid,
        )
        assert [item["uuid"] for item in page["items"]] == [doc_uuid]
    assert len({*leak.bundled_cdef_uuids, *leak.user_cdef_uuids}) == 5


def test_child_ids_resolve(leak: LeakFixture) -> None:
    expected_types = {t for types in CHILD_ELEMENT_TYPES.values() for t in types}
    assert set(leak.children) == expected_types
    for element_type, ref in leak.children.items():
        child = leak.store.get_child_element(ref.element_id, ref.parent_doc_uuid)
        assert child is not None, element_type
        assert child["element_type"] == element_type
    control = leak.store.get_child_element(leak.control.element_id, leak.control.parent_doc_uuid)
    assert control is not None
    assert control["title"] == "Account Management"

    cdef_uuid = leak.doc_uuids[OSCALModelType.COMPONENT_DEFINITION]
    component = leak.store.get_child_element(leak.component_uuid, cdef_uuid)
    assert component is not None
    assert component["element_type"] == "component"
    assert component["title"] == leak.component_title
    capability = leak.store.get_child_element(leak.capability_uuid, cdef_uuid)
    assert capability is not None
    assert capability["element_type"] == "capability"
    assert capability["title"] == leak.capability_name


def test_doc_ids_resolve(leak: LeakFixture) -> None:
    for doc_id in (
        leak.bundled_overview_doc_id,
        leak.user_overview_doc_id,
        leak.user_guide_doc_id,
        leak.awesome_doc_id,
    ):
        assert leak.store.get_documentation(doc_id) is not None, doc_id


def test_queries_hit(leak: LeakFixture) -> None:
    hits = leak.store.text_search(leak.text_query)["items"]
    assert any(
        h["entity_id"] == "ac-2" and h.get("parentDocumentUuid") == leak.catalog_uuid for h in hits
    )
    doc_hits = {h["docId"] for h in leak.store.search_documentation(leak.doc_query)["items"]}
    assert {leak.bundled_overview_doc_id, leak.user_overview_doc_id} <= doc_hits


def test_all_documents_indexed(leak: LeakFixture) -> None:
    row = leak.store._conn.execute(
        "SELECT COUNT(*) AS cnt FROM documents WHERE model_type != 'documentation' AND indexed = 0"
    ).fetchone()
    assert row["cnt"] == 0
