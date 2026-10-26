"""Tests for ``OscalStore.text_search`` (search-fetch-pairs, issue #45).

The ``SearchStore`` builder writes a catalog (control ``ac-2`` titled "Account
Management", whose title and prose never spell "AC-2"), a Component Definition, and a
markdown guide, and scans them into a fresh store. Later test classes reuse it.
"""

import json
import logging
import re
import sqlite3
import uuid as uuid_mod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools import query_component_definition as qcd
from mcp_server_for_oscal.tools import query_oscal_models as qom
from mcp_server_for_oscal.tools.descriptions import BUDGETED_LIMIT_DOC
from mcp_server_for_oscal.tools.oscal_store import (
    BUNDLED_DB_PATH,
    CHILD_ELEMENT_TYPES,
    OscalStore,
)
from mcp_server_for_oscal.tools.utils import MODEL_MAP, OSCALModelType

from ..fixture_store import load_sample_cdef

CATALOG_UUID = "c1d2e3f4-5678-4abc-8def-aabbccddeeff"
CONTROL_ID = "ac-2"
CONTROL_TITLE = "Account Management"
GUIDE_TITLE = "Profile Resolution Guide"

_METADATA = {
    "last-modified": "2024-01-01T00:00:00Z",
    "version": "1.0",
    "oscal-version": "1.0.4",
}


def make_catalog() -> dict:
    """Catalog with controls ``ac-1`` and ``ac-2``; no title or prose contains "AC-2"."""
    return {
        "catalog": {
            "uuid": CATALOG_UUID,
            "metadata": {**_METADATA, "title": "Search Fixture Catalog"},
            "controls": [
                {"id": "ac-1", "title": "Policy and Procedures"},
                {
                    "id": CONTROL_ID,
                    "title": CONTROL_TITLE,
                    "parts": [
                        {
                            "id": "ac-2_smt",
                            "name": "statement",
                            "prose": "Define and document the types of system accounts.",
                        }
                    ],
                },
            ],
        }
    }


GUIDE_MARKDOWN = f"# {GUIDE_TITLE}\n\nHow a profile resolves into a catalog.\n"


@dataclass
class SearchStore:
    """A fresh store holding the fixture catalog, the sample cdef, and a guide."""

    store: OscalStore
    cdef_uuid: str


def build_search_store(directory: Path) -> SearchStore:
    """Write the fixture documents under ``directory/docs`` and scan them into a store.

    The caller closes ``store``.
    """
    docs = directory / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    cdef = load_sample_cdef()
    (docs / "catalog.json").write_text(json.dumps(make_catalog()), encoding="utf-8")
    (docs / "cdef.json").write_text(json.dumps(cdef), encoding="utf-8")
    (docs / "guide.md").write_text(GUIDE_MARKDOWN, encoding="utf-8")
    store = OscalStore(db_path=str(directory / "store.db"), cache_size=10, seed_from_bundled=False)
    try:
        store.scan_directory(docs)
    except BaseException:
        store.close()  # Windows cannot delete an open SQLite file
        raise
    return SearchStore(store=store, cdef_uuid=cdef["component-definition"]["uuid"])


@pytest.fixture
def search_store(tmp_path: Path) -> Iterator[SearchStore]:
    built = build_search_store(tmp_path)
    yield built
    built.store.close()


class TestIdentifierSearch:
    """Element_IDs and Document_UUIDs are searchable (D10).

    Requirements: 6.7, 14.2
    """

    def test_control_id_finds_control(self, search_store: SearchStore) -> None:
        result = search_store.store.text_search(CONTROL_ID, limit=100)

        hits = [i for i in result["items"] if i["entity_id"] == CONTROL_ID]
        assert hits == [
            {
                "entity_type": "child_element",
                "entity_id": CONTROL_ID,
                "title": CONTROL_TITLE,
                "description": hits[0]["description"],
                "model_type": "catalog",
                "parentDocumentUuid": CATALOG_UUID,
            }
        ]
        assert "ac-2" not in hits[0]["description"].casefold()

    @pytest.mark.parametrize("uuid_attr", ["catalog", "cdef"])
    def test_document_uuid_finds_document(self, search_store: SearchStore, uuid_attr: str) -> None:
        uuid = CATALOG_UUID if uuid_attr == "catalog" else search_store.cdef_uuid

        result = search_store.store.text_search(uuid, limit=100)

        documents = [i for i in result["items"] if i["entity_type"] == "document"]
        assert [d["entity_id"] for d in documents] == [uuid]
        assert "parentDocumentUuid" not in documents[0]

    def test_like_fallback_matches_element_id(
        self, search_store: SearchStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        search_store.store.text_search("warm-up")  # index pending documents first

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("forced")

        monkeypatch.setattr(search_store.store, "_fts_search", _raise)
        result = search_store.store.text_search(CONTROL_ID, limit=100)

        assert CONTROL_ID in [i["entity_id"] for i in result["items"]]

    def test_fts_rows_hold_element_ids(self, search_store: SearchStore) -> None:
        store = search_store.store
        store.text_search("warm-up")  # index pending documents
        conn = store._conn
        rows = conn.execute(
            "SELECT f.entity_type, f.element_id, "
            "COALESCE(ce.uuid, d.uuid) AS expected "
            "FROM fts_index f "
            "LEFT JOIN child_elements ce "
            " ON f.entity_type = 'child_element' AND ce.id = CAST(f.entity_id AS INTEGER) "
            "LEFT JOIN documents d "
            " ON f.entity_type = 'document' AND d.id = CAST(f.entity_id AS INTEGER)"
        ).fetchall()
        assert {r["entity_type"] for r in rows} == {"document", "child_element", "documentation"}
        for r in rows:
            expected = "" if r["entity_type"] == "documentation" else r["expected"]
            assert r["element_id"] == expected


class TestDocumentationSearchUnchanged:
    """Documentation search matches title and description only (D10).

    Requirements: 6.7
    """

    def test_title_term_finds_guide(self, search_store: SearchStore) -> None:
        result = search_store.store.search_documentation("Resolution")

        assert [i["title"] for i in result["items"]] == [GUIDE_TITLE]

    def test_identifiers_do_not_match_documentation(self, search_store: SearchStore) -> None:
        store = search_store.store
        store.text_search("warm-up")  # index OSCAL documents, so element_id is filled

        for query in (f'"{CATALOG_UUID}"', f'"{CONTROL_ID}"', "documentation"):
            assert store.search_documentation(query)["total"] == 0, query


# --------------------------------------------------------------------------------------
# Property 6: every hit resolves through the fetch tools
# --------------------------------------------------------------------------------------


def _uuid(n: int) -> str:
    """Deterministic version-4 UUID for fixture element ``n``."""
    return str(uuid_mod.UUID(int=0xA11CE000 + n, version=4))


def _metadata(title: str) -> dict:
    return {**_METADATA, "title": title}


def _grouped_catalog() -> dict:
    """Catalog whose controls sit in a group; ``ac-1`` also exists in ``make_catalog``."""
    return {
        "catalog": {
            "uuid": _uuid(1),
            "metadata": _metadata("Grouped Harbor Catalog"),
            "groups": [
                {
                    "id": "ac",
                    "title": "Access Control Family",
                    "controls": [
                        {
                            "id": "ac-1",
                            "title": "Lighthouse Policy",
                            "parts": [
                                {
                                    "id": "ac-1_gdn",
                                    "name": "guidance",
                                    "prose": "Review the lighthouse policy every quarter.",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    }


def _profile() -> dict:
    return {
        "profile": {
            "uuid": _uuid(2),
            "metadata": _metadata("Moderate Glacier Baseline"),
            "imports": [{"href": "https://example.com/glacier-catalog.json", "include-all": {}}],
            "modify": {"set-parameters": [{"param-id": "ac-1_prm_1", "values": ["quarterly"]}]},
        }
    }


def _ssp() -> dict:
    return {
        "system-security-plan": {
            "uuid": _uuid(3),
            "metadata": _metadata("Orchard System Security Plan"),
            "import-profile": {"href": "https://example.com/glacier-profile.json"},
            "system-characteristics": {
                "system-ids": [{"id": "orchard-001"}],
                "system-name": "Orchard",
                "description": "Orchard tracks fruit harvests.",
                "security-sensitivity-level": "moderate",
                "system-information": {
                    "information-types": [
                        {
                            "title": "Harvest Records",
                            "description": "Records of harvested fruit.",
                            "confidentiality-impact": {"base": "fips-199-moderate"},
                            "integrity-impact": {"base": "fips-199-moderate"},
                            "availability-impact": {"base": "fips-199-low"},
                        }
                    ]
                },
                "security-impact-level": {
                    "security-objective-confidentiality": "moderate",
                    "security-objective-integrity": "moderate",
                    "security-objective-availability": "low",
                },
                "status": {"state": "operational"},
                "authorization-boundary": {"description": "The orchard network."},
            },
            "system-implementation": {
                "users": [{"uuid": _uuid(30)}],
                "components": [
                    {
                        "uuid": _uuid(31),
                        "type": "this-system",
                        "title": "Orchard Pressroom Service",
                        "description": "Presses cider from tracked apples.",
                        "status": {"state": "operational"},
                    }
                ],
            },
            "control-implementation": {
                "description": "Orchard implements the glacier baseline.",
                "implemented-requirements": [{"uuid": _uuid(32), "control-id": "ac-1"}],
            },
        }
    }


def _assessment_plan() -> dict:
    return {
        "assessment-plan": {
            "uuid": _uuid(4),
            "metadata": _metadata("Meadow Assessment Plan"),
            "import-ssp": {"href": "https://example.com/orchard-ssp.json"},
            "local-definitions": {
                "activities": [
                    {
                        "uuid": _uuid(40),
                        "title": "Interview Beekeepers",
                        "description": "Ask beekeepers about hive access.",
                    }
                ]
            },
            "reviewed-controls": {"control-selections": [{"include-all": {}}]},
            "tasks": [
                {
                    "uuid": _uuid(41),
                    "type": "action",
                    "title": "Inspect Hives",
                    "description": "Walk the meadow and inspect every hive.",
                }
            ],
        }
    }


def _assessment_results() -> dict:
    return {
        "assessment-results": {
            "uuid": _uuid(5),
            "metadata": _metadata("Canyon Assessment Results"),
            "import-ap": {"href": "https://example.com/meadow-plan.json"},
            "results": [
                {
                    "uuid": _uuid(50),
                    "title": "Canyon Survey Result",
                    "description": "Survey of canyon trail markers.",
                    "start": "2024-01-02T00:00:00Z",
                    "reviewed-controls": {"control-selections": [{"include-all": {}}]},
                    "findings": [
                        {
                            "uuid": _uuid(51),
                            "title": "Faded Trail Marker",
                            "description": "A canyon trail marker has faded paint.",
                            "target": {
                                "type": "objective-id",
                                "target-id": "ac-1_obj",
                                "status": {"state": "not-satisfied"},
                            },
                        }
                    ],
                }
            ],
        }
    }


def _poam() -> dict:
    return {
        "plan-of-action-and-milestones": {
            "uuid": _uuid(6),
            "metadata": _metadata("Riverbank Remediation Plan"),
            "poam-items": [
                {
                    "uuid": _uuid(60),
                    "title": "Repair Riverbank Levee",
                    "description": "Reinforce the eroded levee section.",
                }
            ],
        }
    }


def _mapping_collection() -> dict:
    resource = {"type": "catalog", "href": "https://example.com/glacier-catalog.json"}
    return {
        "mapping-collection": {
            "uuid": _uuid(7),
            "metadata": _metadata("Compass Crosswalk Mappings"),
            "provenance": {
                "method": "human",
                "matching-rationale": "semantic",
                "status": "complete",
                "mapping-description": "Compass crosswalk between two catalogs.",
            },
            "mappings": [
                {
                    "uuid": _uuid(70),
                    "source-resource": resource,
                    "target-resource": resource,
                    "maps": [
                        {
                            "uuid": _uuid(71),
                            "relationship": "equivalent-to",
                            "sources": [{"type": "control", "id-ref": "ac-1"}],
                            "targets": [{"type": "control", "id-ref": "ac-2"}],
                        }
                    ],
                }
            ],
        }
    }


def resolvability_documents() -> dict[str, dict]:
    """Model-valid documents covering every ``OSCALModelType``, keyed by file stem."""
    return {
        "catalog": make_catalog(),
        "grouped-catalog": _grouped_catalog(),
        "profile": _profile(),
        "cdef": load_sample_cdef(),
        "ssp": _ssp(),
        "assessment-plan": _assessment_plan(),
        "assessment-results": _assessment_results(),
        "poam": _poam(),
        "mapping-collection": _mapping_collection(),
    }


# The per-model query tool for each model type other than Component Definition, which
# resolves a document through ``query_component_definition``'s filter instead.
PER_MODEL_QUERY: dict[OSCALModelType, Callable[..., dict]] = {
    OSCALModelType.CATALOG: qom.query_catalog,
    OSCALModelType.PROFILE: qom.query_profile,
    OSCALModelType.SYSTEM_SECURITY_PLAN: qom.query_ssp,
    OSCALModelType.ASSESSMENT_PLAN: qom.query_assessment_plan,
    OSCALModelType.ASSESSMENT_RESULTS: qom.query_assessment_results,
    OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES: qom.query_poam,
    OSCALModelType.MAPPING: qom.query_mapping_collection,
}


@dataclass
class ResolvabilityStore:
    """A store with documents of every model type, and query candidates drawn from it."""

    store: OscalStore
    candidates: list[str]


def _query_candidates(store: OscalStore) -> list[str]:
    """Indexed titles, description words, Element_IDs, and Document_UUIDs, deduplicated."""
    store.text_search("warm-up")  # index every pending document
    docs = store.list_documents(limit=100)["items"]
    children = store.list_child_elements(limit=100)["items"]
    # One page holds everything.
    assert len(docs) < 100
    assert len(children) < 100
    candidates = [d["uuid"] for d in docs] + [d["title"] for d in docs]
    for child in children:
        candidates += [child["id"], child["title"]]
        candidates += re.findall(r"\w{4,}", child.get("description") or "")
    return sorted({c for c in candidates if c})


@pytest.fixture(scope="module")
def resolvability_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ResolvabilityStore]:
    directory = tmp_path_factory.mktemp("resolvability")
    docs = directory / "docs"
    docs.mkdir()
    documents = resolvability_documents()
    for stem, doc in documents.items():
        (root_key,) = doc
        MODEL_MAP[OSCALModelType(root_key)].model_validate(doc[root_key])  # fixture is valid
        (docs / f"{stem}.json").write_text(json.dumps(doc), encoding="utf-8")
    store = OscalStore(db_path=str(directory / "store.db"), cache_size=20, seed_from_bundled=False)
    try:
        store.scan_directory(docs)
        listed = store.list_documents(limit=100)
        assert listed["total"] == len(documents)
        assert {d["model_type"] for d in listed["items"]} == {m.value for m in OSCALModelType}
        yield ResolvabilityStore(store=store, candidates=_query_candidates(store))
    finally:
        store.close()


def _all_hits(query_text: str) -> list[dict]:
    """Every ``text_search_oscal`` hit for ``query_text``, following ``hasMore``."""
    hits: list[dict] = []
    offset = 0
    while True:
        page = qom.text_search_oscal(query_text, offset=offset, limit=100)
        hits += page["items"]
        if not page["hasMore"]:
            assert len(hits) == page["total"]
            return hits
        offset += len(page["items"])


def _assert_child_resolves(hit: dict) -> None:
    child = qom.get_child_element(hit["entity_id"], hit["parentDocumentUuid"])
    assert child is not None, hit
    assert "error" not in child, (hit, child)
    assert child["id"] == hit["entity_id"]
    assert child["parentDocumentUuid"] == hit["parentDocumentUuid"]
    assert child["title"] == hit["title"]
    assert child["element_type"] in CHILD_ELEMENT_TYPES[OSCALModelType(hit["model_type"])]


def _assert_document_resolves(store: OscalStore, hit: dict) -> None:
    model_type = OSCALModelType(hit["model_type"])
    page = store.query(
        oscal_model_type=model_type, query_type="by_uuid", query_value=hit["entity_id"]
    )
    assert page["total"] == 1, hit
    assert [(i["uuid"], i["title"]) for i in page["items"]] == [(hit["entity_id"], hit["title"])]

    if model_type is OSCALModelType.COMPONENT_DEFINITION:
        scoped = qcd.query_component_definition(component_definition_filter=hit["entity_id"])
        assert scoped["component_definitions_searched"] == 1, hit
        listed = qcd.list_component_definitions(limit=100)["items"]
        assert (hit["entity_id"], hit["title"]) in [(i["uuid"], i["title"]) for i in listed]
        return
    page = PER_MODEL_QUERY[model_type](query_type="by_uuid", query_value=hit["entity_id"])
    assert page["total"] == 1, hit
    assert [(i["uuid"], i["title"]) for i in page["items"]] == [(hit["entity_id"], hit["title"])]


class TestHitsResolveProperty:
    """Feature: search-fetch-pairs, Property 6: Text search hits resolve through the fetch tools.

    For a store with documents of every model type and any query built from its indexed
    titles, description words, Element_IDs, and Document_UUIDs, every child hit passed to
    ``get_child_element(entity_id, parentDocumentUuid)`` returns that child, and every
    document hit passed to its model's query tool with ``by_uuid`` returns that document.

    **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
    """

    @given(data=st.data())
    @settings(max_examples=100, deadline=None)
    @pytest.mark.slow
    def test_every_hit_resolves(
        self, resolvability_store: ResolvabilityStore, data: st.DataObject
    ) -> None:
        terms = data.draw(
            st.lists(st.sampled_from(resolvability_store.candidates), min_size=1, max_size=3),
            label="terms",
        )
        query_text = " ".join(terms)
        store = resolvability_store.store
        saved_qom, saved_qcd = qom._store, qcd._oscal_store
        qom.init_store(store)
        qcd.init_store(store)
        try:
            hits = _all_hits(query_text)
            assert hits, query_text  # every candidate is indexed text, so something matches
            for hit in hits:
                if hit["entity_type"] == "child_element":
                    _assert_child_resolves(hit)
                else:
                    assert hit["entity_type"] == "document", hit
                    assert "parentDocumentUuid" not in hit
                    _assert_document_resolves(store, hit)
        finally:
            qom._store, qcd._oscal_store = saved_qom, saved_qcd

    def test_candidates_cover_every_model_type(
        self, resolvability_store: ResolvabilityStore
    ) -> None:
        """The candidate pool reaches a hit of every model type (guards the generator)."""
        saved = qom._store
        qom.init_store(resolvability_store.store)
        try:
            seen = {
                (h["model_type"], h["entity_type"])
                for c in resolvability_store.candidates
                for h in _all_hits(c)
            }
        finally:
            qom._store = saved
        for model_type in OSCALModelType:
            assert (model_type.value, "document") in seen, model_type
            assert (model_type.value, "child_element") in seen, model_type


# --------------------------------------------------------------------------------------
# Unit tests: freshness, normalization, fallback, control prose, by_title, bundled DB
# --------------------------------------------------------------------------------------

TERMS_CATALOG_UUID = "d2e3f4a5-6789-4bcd-9ef0-bbccddeeff00"
TERMS_CATALOG_TITLE = "Self-Hosted Lantern Catalog"
GUIDANCE_CONTROL_ID = "lt-1"
GUIDANCE_CONTROL_TITLE = "Lantern Inventory"
# Occurs only in the guidance part of control ``lt-1``.
GUIDANCE_PHRASE = "polish every brass wick holder"


def make_terms_catalog() -> dict:
    """Catalog with a hyphenated title whose control prose spells hyphenated OSCAL terms.

    Control ``lt-1`` holds ``back-matter`` and ``control-implementation`` in its statement
    and ``GUIDANCE_PHRASE`` only in its guidance.
    """
    return {
        "catalog": {
            "uuid": TERMS_CATALOG_UUID,
            "metadata": {**_METADATA, "title": TERMS_CATALOG_TITLE},
            "controls": [
                {
                    "id": GUIDANCE_CONTROL_ID,
                    "title": GUIDANCE_CONTROL_TITLE,
                    "parts": [
                        {
                            "id": "lt-1_smt",
                            "name": "statement",
                            "prose": (
                                "List each lantern in the back-matter and describe it "
                                "in the control-implementation after profile resolution."
                            ),
                        },
                        {
                            "id": "lt-1_gdn",
                            "name": "guidance",
                            "prose": f"Crews {GUIDANCE_PHRASE} before winter.",
                        },
                    ],
                }
            ],
        }
    }


def build_terms_store(directory: Path) -> SearchStore:
    """``build_search_store`` plus the ``make_terms_catalog`` document. Caller closes it."""
    docs = directory / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "terms-catalog.json").write_text(json.dumps(make_terms_catalog()), encoding="utf-8")
    return build_search_store(directory)


@pytest.fixture
def terms_store(tmp_path: Path) -> Iterator[SearchStore]:
    built = build_terms_store(tmp_path)
    yield built
    built.store.close()


def _forbid_like_fallback(store: OscalStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the LIKE fallback fail the test, so any caught FTS error is visible."""

    def _fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("FTS MATCH raised sqlite3.OperationalError; LIKE fallback was used")

    monkeypatch.setattr(store, "_like_search", _fail)


class TestTextSearchUnit:
    """Unit tests for ``OscalStore.text_search`` and ``text_search_oscal``.

    Requirements: 6.7, 7.3, 7.7, 8.2, 8.7, 8.9, 9.6
    """

    def test_corrupt_pending_document_is_skipped_with_warning(
        self, search_store: SearchStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        """7.3: a pending document that fails to index is logged by UUID; search goes on."""
        store = search_store.store
        conn = store._conn
        conn.execute(
            "UPDATE documents SET raw_json = ?, indexed = 0 WHERE uuid = ?",
            ('{"catalog": {not json', CATALOG_UUID),
        )
        conn.commit()

        with caplog.at_level(logging.WARNING):
            result = store.text_search("Sample", limit=100)

        assert CATALOG_UUID in caplog.text
        assert result["total"] > 0
        assert {i["model_type"] for i in result["items"]} == {"component-definition"}
        pending = conn.execute(
            "SELECT indexed FROM documents WHERE uuid = ?", (CATALOG_UUID,)
        ).fetchone()
        assert pending["indexed"] == 0  # retried on the next call

    def test_like_fallback_when_fts_raises(
        self,
        terms_store: SearchStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """8.9: a raising FTS query logs a warning and LIKE returns the same item shape."""
        store = terms_store.store
        expected = store.text_search("lantern inventory", limit=100)

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("forced")

        monkeypatch.setattr(store, "_fts_search", _raise)
        with caplog.at_level(logging.WARNING):
            result = store.text_search("lantern inventory", limit=100)

        assert "falling back to LIKE" in caplog.text
        assert result["total"] == expected["total"] > 0
        # The control matches both terms, so it comes first.
        first = result["items"][0]
        assert first == {
            "entity_type": "child_element",
            "entity_id": GUIDANCE_CONTROL_ID,
            "title": GUIDANCE_CONTROL_TITLE,
            "description": first["description"],
            "model_type": "catalog",
            "parentDocumentUuid": TERMS_CATALOG_UUID,
        }
        for got, want in zip(
            sorted(result["items"], key=lambda i: i["entity_id"]),
            sorted(expected["items"], key=lambda i: i["entity_id"]),
            strict=True,
        ):
            assert got.keys() == want.keys()

    @pytest.mark.parametrize(
        ("query", "expected_id"),
        [
            ("back-matter", GUIDANCE_CONTROL_ID),
            ("control-implementation", GUIDANCE_CONTROL_ID),
            ("ac-2", CONTROL_ID),
        ],
    )
    def test_hyphenated_tokens_match_without_operational_error(
        self,
        terms_store: SearchStore,
        monkeypatch: pytest.MonkeyPatch,
        query: str,
        expected_id: str,
    ) -> None:
        """8.2: hyphenated tokens are one phrase; FTS never raises."""
        store = terms_store.store
        _forbid_like_fallback(store, monkeypatch)

        result = store.text_search(query, limit=100)

        assert expected_id in [i["entity_id"] for i in result["items"]]

    def test_ac_2_hit_carries_parent_document_uuid(
        self, terms_store: SearchStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """6.7: ``ac-2`` returns fixture control ``ac-2`` with its parent Document_UUID."""
        store = terms_store.store
        _forbid_like_fallback(store, monkeypatch)

        result = store.text_search("ac-2", limit=100)

        hits = [i for i in result["items"] if i["entity_id"] == CONTROL_ID]
        assert [(h["entity_type"], h["parentDocumentUuid"]) for h in hits] == [
            ("child_element", CATALOG_UUID)
        ]

    def test_operator_characters_are_literal(
        self, terms_store: SearchStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """8.2/8.3: quotes, ``AND``, and an unbalanced parenthesis do not break FTS."""
        store = terms_store.store
        _forbid_like_fallback(store, monkeypatch)

        result = store.text_search('"profile resolution" AND (x', limit=100)

        assert GUIDANCE_CONTROL_ID in [i["entity_id"] for i in result["items"]]

    def test_guidance_only_phrase_finds_control(
        self, terms_store: SearchStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """9.6: a phrase only in a control's guidance finds that control."""
        store = terms_store.store
        _forbid_like_fallback(store, monkeypatch)

        result = store.text_search(GUIDANCE_PHRASE, limit=100)

        # Every term occurs only in the guidance, so the control is the top hit.
        top = result["items"][0]
        assert (top["entity_type"], top["entity_id"], top["parentDocumentUuid"]) == (
            "child_element",
            GUIDANCE_CONTROL_ID,
            TERMS_CATALOG_UUID,
        )
        assert GUIDANCE_PHRASE in top["description"]

    @pytest.mark.parametrize("title", [TERMS_CATALOG_TITLE, "self-hosted lantern"])
    def test_by_title_with_hyphenated_title(self, terms_store: SearchStore, title: str) -> None:
        """``by_title`` finds a hyphenated title, by exact match and through FTS."""
        store = terms_store.store
        store.text_search("warm-up")  # index pending documents, so the FTS phase has rows

        result = store.query(
            oscal_model_type=OSCALModelType.CATALOG, query_type="by_title", query_value=title
        )

        assert [i["uuid"] for i in result["items"]] == [TERMS_CATALOG_UUID]

    def test_text_search_oscal_limit_uses_budgeted_limit_text(self) -> None:
        """7.7: the ``limit`` schema carries the shared ``BudgetedLimit`` description."""
        schema = qom.text_search_oscal.tool_spec["inputSchema"]["json"]
        limit = schema["properties"]["limit"]

        assert limit["description"] == BUDGETED_LIMIT_DOC
        assert limit.get("default") == 10


@pytest.mark.skipif(
    not (BUNDLED_DB_PATH.exists() and OscalStore._verify_bundled_db()),
    reason="bundled DB or its hash manifest is missing or fails verification",
)
class TestBundledDocumentationSearch:
    """8.7: against the bundled DB, the question form returns a superset.

    Requirements: 8.7
    """

    def test_question_returns_superset_of_phrase(self) -> None:
        store = OscalStore(cache_size=5)
        try:
            assert store.db_mode == "bundled"
            question = store.search_documentation("how does profile resolution work", limit=1000)
            phrase = store.search_documentation("profile resolution", limit=1000)
        finally:
            store.close()

        assert phrase["items"]  # guard: the bundled docs discuss profile resolution
        assert not phrase["hasMore"]
        assert not question["hasMore"]
        assert {i["docId"] for i in question["items"]} >= {i["docId"] for i in phrase["items"]}
