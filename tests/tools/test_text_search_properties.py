"""Property tests for ``OscalStore.text_search`` (search-fetch-pairs tasks 9.4-9.8).

Property 7 uses its own store (every model type, documentation of both Origins).
Property 8 scans new catalogs into its own store in every example.

Property 5 also covers ``OscalStore.search_documentation`` (same ranking rules).

One class per design property. The pure normalization part of Property 3 lives in
``tests/tools/test_fts_query_properties.py``; this file covers the store-backed parts.
"""

import json
import random
import uuid as uuid_mod
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import NoReturn

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools.fts_query import STOP_WORDS, normalize_query
from mcp_server_for_oscal.tools.oscal_store import (
    ORIGIN_BUNDLED,
    ORIGIN_USER,
    TEXT_SEARCH_DESCRIPTION_CAP,
    OscalStore,
)
from mcp_server_for_oscal.tools.utils import (
    MODEL_MAP,
    RESPONSE_BUDGET,
    TRUNCATION_MARK,
    OSCALModelType,
)

from .test_fts_query_properties import _QUERIES
from .test_text_search import build_search_store, make_catalog, resolvability_documents

# ---------------------------------------------------------------------------
# Shared strategies and fixtures
# ---------------------------------------------------------------------------

MODEL_TYPES = st.one_of(st.none(), st.sampled_from(list(OSCALModelType)))
OFFSETS = st.integers(min_value=0, max_value=5)
LIMITS = st.integers(min_value=1, max_value=100)


def _like_fallback_forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    """Stand-in for ``OscalStore._like_search``: reaching it means MATCH raised."""
    msg = "text_search fell back to LIKE: the normalized FTS5 MATCH raised OperationalError"
    raise AssertionError(msg)


@pytest.fixture(scope="module")
def strict_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[OscalStore]:
    """The fixture search store, with the LIKE fallback replaced by a failing stub.

    Module-scoped so Hypothesis examples share one store; the stub is an instance
    attribute, so no other store is affected.
    """
    built = build_search_store(tmp_path_factory.mktemp("text-search-properties"))
    store = built.store
    try:
        store._like_search = _like_fallback_forbidden  # type: ignore[method-assign]
        yield store
    finally:
        store.close()


def _search(
    store: OscalStore,
    query_text: str,
    model_type: OSCALModelType | None,
    offset: int,
    limit: int,
) -> dict:
    return store.text_search(query_text, oscal_model_type=model_type, offset=offset, limit=limit)


# ---------------------------------------------------------------------------
# Property 3 (store part, text_search)
# ---------------------------------------------------------------------------


class TestNormalizedQueryRunsAgainstStore:
    """Feature: search-fetch-pairs, Property 3 (store part, text_search):
    Query normalization never yields FTS5 syntax.

    **Validates: Requirements 8.1, 8.2, 8.3, 8.8**
    """

    @settings(max_examples=200, deadline=None)
    @given(query_text=_QUERIES, model_type=MODEL_TYPES, offset=OFFSETS, limit=LIMITS)
    def test_text_search_never_raises_and_empty_terms_give_empty_page(
        self,
        strict_store: OscalStore,
        query_text: str,
        model_type: OSCALModelType | None,
        offset: int,
        limit: int,
    ) -> None:
        # Raises AssertionError via the stub if the MATCH raised OperationalError (8.1-8.3).
        page = _search(strict_store, query_text, model_type, offset, limit)

        assert isinstance(page["items"], list)
        assert page["total"] >= 0
        if not normalize_query(query_text):
            # Requirement 8.8: no Search_Terms means an empty page with total 0.
            assert page["items"] == []
            assert page["total"] == 0


# ---------------------------------------------------------------------------
# Property 5: per-term oracle, all-terms first
# ---------------------------------------------------------------------------

# Words absent from every fixture document, so some queries match only some terms.
_MISSING_WORDS = ("zzqxv", "qwfpgj-9")


def _vocabulary(store: OscalStore, entity_types: tuple[str, ...]) -> list[str]:
    """Return the distinct non-stop Search_Terms in the indexed text of ``entity_types``.

    Terms come from ``title``, ``description``, and ``element_id`` of the FTS rows, cut
    by ``normalize_query`` so each one is a single Search_Term.
    """
    store.text_search("warm-up")  # index pending OSCAL documents first
    rows = store._conn.execute(
        "SELECT entity_type, title, description, element_id FROM fts_index"
    ).fetchall()
    seen: dict[str, str] = {}
    for row in rows:
        if row["entity_type"] not in entity_types:
            continue
        for text in (row["title"], row["description"], row["element_id"]):
            for word in (text or "").split():
                terms = normalize_query(word)
                if len(terms) == 1 and terms[0].casefold() not in STOP_WORDS:
                    seen.setdefault(terms[0].casefold(), terms[0])
    return sorted(seen.values())


@pytest.fixture(scope="module")
def text_vocabulary(strict_store: OscalStore) -> list[str]:
    vocab = _vocabulary(strict_store, ("document", "child_element"))
    assert vocab, "fixture store has no indexed text"
    return vocab


@pytest.fixture(scope="module")
def doc_vocabulary(strict_store: OscalStore) -> list[str]:
    vocab = _vocabulary(strict_store, ("documentation",))
    assert vocab, "fixture store has no indexed documentation"
    return vocab


def _all_text_hits(store: OscalStore, query_text: str) -> list[tuple]:
    """Page through ``text_search`` with ``hasMore``; return hit keys in result order."""
    keys: list[tuple] = []
    offset = 0
    while True:
        page = store.text_search(query_text, offset=offset, limit=100)
        keys += [
            (i["entity_type"], i["model_type"], i["entity_id"], i.get("parentDocumentUuid"))
            for i in page["items"]
        ]
        if not page["hasMore"]:
            break
        assert page["items"], "hasMore with an empty page would loop forever"
        offset += len(page["items"])
    assert len(keys) == page["total"]
    return keys


def _all_doc_hits(store: OscalStore, query_text: str) -> list[tuple]:
    """Page through ``search_documentation`` with ``hasMore``; return Doc_IDs in order."""
    keys: list[tuple] = []
    offset = 0
    while True:
        page = store.search_documentation(query_text, offset=offset, limit=100)
        keys += [(i["docId"],) for i in page["items"]]
        if not page["hasMore"]:
            break
        assert page["items"], "hasMore with an empty page would loop forever"
        offset += len(page["items"])
    assert len(keys) == page["total"]
    return keys


def _assert_matches_oracle(hits_of: Callable[[str], list[tuple]], query_text: str) -> None:
    """Check Requirements 8.5 and 8.6 for ``query_text`` against single-term searches."""
    terms = normalize_query(query_text)
    hits = hits_of(query_text)
    assert len(hits) == len(set(hits)), f"duplicate hits for {query_text!r}: {hits}"

    per_term = [set(hits_of(term)) for term in terms]
    # Requirement 8.5: every entity that matches at least one Search_Term, and no other.
    assert set(hits) == set().union(*per_term)

    # Requirement 8.6: all-terms hits form a prefix; order within groups is FTS-internal.
    match_all = set.intersection(*per_term) if per_term else set()
    groups = [hit in match_all for hit in hits]
    assert groups == sorted(groups, reverse=True), (
        f"an only-some-terms hit precedes an all-terms hit for {terms!r}: {hits}"
    )


def _queries(vocabulary: list[str]) -> st.SearchStrategy[tuple[list[str], list[str]]]:
    """Draw (content words, stop words) with content words mostly from ``vocabulary``."""
    words = st.lists(
        st.one_of(st.sampled_from(vocabulary), st.sampled_from(_MISSING_WORDS)),
        min_size=1,
        max_size=4,
        unique_by=str.casefold,
    )
    stops = st.lists(st.sampled_from(sorted(STOP_WORDS)), max_size=4)
    return st.tuples(words, stops)


def _with_stop_words(words: list[str], stops: list[str], data: st.DataObject) -> str:
    """Interleave ``stops`` among ``words`` at drawn positions."""
    mixed = list(words)
    for stop in stops:
        mixed.insert(data.draw(st.integers(0, len(mixed)), label="stop position"), stop)
    return " ".join(mixed)


class TestSearchMatchesPerTermOracle:
    """Feature: search-fetch-pairs, Property 5: Search results equal a per-term oracle,
    all-terms first.

    **Validates: Requirements 8.5, 8.6, 8.7**
    """

    @settings(max_examples=100, deadline=None)
    @given(data=st.data())
    def test_text_search_equals_union_of_single_term_searches(
        self, strict_store: OscalStore, text_vocabulary: list[str], data: st.DataObject
    ) -> None:
        words, stops = data.draw(_queries(text_vocabulary), label="query")
        query_text = " ".join(words)
        hits_of = partial(_all_text_hits, strict_store)

        _assert_matches_oracle(hits_of, query_text)

        # Requirement 8.7: adding Stop_Words never removes a hit (here: same terms, same hits).
        stop_query = _with_stop_words(words, stops, data)
        assert normalize_query(stop_query) == normalize_query(query_text)
        assert set(hits_of(stop_query)) >= set(hits_of(query_text))
        assert hits_of(stop_query) == hits_of(query_text)

    @settings(max_examples=50, deadline=None)
    @given(data=st.data())
    def test_search_documentation_equals_union_of_single_term_searches(
        self, strict_store: OscalStore, doc_vocabulary: list[str], data: st.DataObject
    ) -> None:
        words, stops = data.draw(_queries(doc_vocabulary), label="query")
        query_text = " ".join(words)
        hits_of = partial(_all_doc_hits, strict_store)

        _assert_matches_oracle(hits_of, query_text)

        stop_query = _with_stop_words(words, stops, data)
        assert set(hits_of(stop_query)) >= set(hits_of(query_text))


# ---------------------------------------------------------------------------
# Property 7: item shape and scope
# ---------------------------------------------------------------------------

_ITEM_KEYS = frozenset({"entity_type", "entity_id", "title", "description", "model_type"})

# Documentation that shares words with the OSCAL fixtures, so queries also match
# documentation rows (which text_search must leave out, Requirement 7.1).
_USER_GUIDE = "# Catalog Control Guide\n\nHow a profile selects catalog controls and accounts.\n"
_BUNDLED_GUIDE = "# Assessment Plan Notes\n\nSystem security plan and assessment results.\n"


@dataclass
class ShapeStore:
    """A store with every model type and documentation of both Origins, plus its oracle."""

    store: OscalStore
    vocabulary: list[str]
    documents: set[tuple[str, str, str]]  # (Document_UUID, model_type, title)
    children: set[tuple[str, str, str, str]]  # (Element_ID, parent UUID, model_type, title)
    row_ids: set[str]  # decimal documents.id and child_elements.id values


def _shape_oracle(store: OscalStore) -> tuple[set, set, set]:
    """Return the documents, children, and internal row ids of ``store`` after indexing."""
    conn = store._conn
    documents = {
        (r["uuid"], r["model_type"], r["title"])
        for r in conn.execute(
            "SELECT uuid, model_type, title FROM documents WHERE model_type != 'documentation'"
        )
    }
    children = {
        (r["uuid"], r["parent_uuid"], r["model_type"], r["title"])
        for r in conn.execute(
            "SELECT ce.uuid, ce.title, pd.uuid AS parent_uuid, pd.model_type "
            "FROM child_elements ce JOIN documents pd ON pd.id = ce.parent_doc_id"
        )
    }
    row_ids = {str(r["id"]) for r in conn.execute("SELECT id FROM documents")}
    row_ids |= {str(r["id"]) for r in conn.execute("SELECT id FROM child_elements")}
    return documents, children, row_ids


@pytest.fixture(scope="module")
def shape_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ShapeStore]:
    directory = tmp_path_factory.mktemp("text-search-shape")
    user_dir, bundled_dir = directory / "user", directory / "bundled"
    user_dir.mkdir()
    bundled_dir.mkdir()
    for stem, doc in resolvability_documents().items():
        (user_dir / f"{stem}.json").write_text(json.dumps(doc), encoding="utf-8")
    (user_dir / "guide.md").write_text(_USER_GUIDE, encoding="utf-8")
    (bundled_dir / "notes.md").write_text(_BUNDLED_GUIDE, encoding="utf-8")
    store = OscalStore(db_path=str(directory / "store.db"), cache_size=20, seed_from_bundled=False)
    try:
        store.scan_directory(user_dir, origin=ORIGIN_USER)
        store.scan_directory(bundled_dir, origin=ORIGIN_BUNDLED)
        store._like_search = _like_fallback_forbidden  # type: ignore[method-assign]
        vocabulary = _vocabulary(store, ("document", "child_element", "documentation"))
        documents, children, row_ids = _shape_oracle(store)
        assert {m for _, m, _ in documents} == {m.value for m in OSCALModelType}
        assert store.count_documents(origin=ORIGIN_BUNDLED) == 1
        yield ShapeStore(store, vocabulary, documents, children, row_ids)
    finally:
        store.close()


def _shape_queries(vocabulary: list[str]) -> st.SearchStrategy[str]:
    """Queries of 1-3 vocabulary words, or arbitrary text from ``_QUERIES``."""
    words = st.lists(st.sampled_from(vocabulary), min_size=1, max_size=3).map(" ".join)
    return st.one_of(words, words, _QUERIES)


def _assert_item_shape(item: dict, shape: ShapeStore, model_type: OSCALModelType | None) -> None:
    entity_type = item["entity_type"]
    # Requirement 7.1: documentation is never a text_search hit.
    assert entity_type in {"document", "child_element"}, item
    assert item["model_type"] != "documentation", item
    # Requirement 7.6: the model filter scopes every item.
    if model_type is not None:
        assert item["model_type"] == model_type.value, item

    # Requirement 6.5: exact key set, parentDocumentUuid iff child.
    expected_keys = _ITEM_KEYS | (
        {"parentDocumentUuid"} if entity_type == "child_element" else set()
    )
    assert set(item) == expected_keys, item

    # Requirement 6.6: entity_id is the element's own identifier, never a row id.
    if entity_type == "document":
        key = (item["entity_id"], item["model_type"], item["title"])
        assert key in shape.documents, item
        own_ids = {d[0] for d in shape.documents}
    else:
        child_key = (
            item["entity_id"],
            item["parentDocumentUuid"],
            item["model_type"],
            item["title"],
        )
        assert child_key in shape.children, item
        own_ids = {c[0] for c in shape.children}
    if item["entity_id"] in shape.row_ids:
        assert item["entity_id"] in own_ids, item

    # Requirement 7.4 bound, as the item shape documents it.
    assert len(item["description"]) <= TEXT_SEARCH_DESCRIPTION_CAP + len(TRUNCATION_MARK), item


class TestTextSearchItemShapeAndScope:
    """Feature: search-fetch-pairs, Property 7: Text search items have the documented
    shape and scope.

    **Validates: Requirements 6.5, 6.6, 7.1, 7.6**
    """

    @settings(max_examples=100, deadline=None)
    @given(data=st.data(), model_type=MODEL_TYPES, offset=OFFSETS, limit=LIMITS)
    def test_items_have_documented_shape_and_scope(
        self,
        shape_store: ShapeStore,
        data: st.DataObject,
        model_type: OSCALModelType | None,
        offset: int,
        limit: int,
    ) -> None:
        query_text = data.draw(_shape_queries(shape_store.vocabulary), label="query")
        page = _search(shape_store.store, query_text, model_type, offset, limit)

        assert len(page["items"]) <= page["total"]
        for item in page["items"]:
            _assert_item_shape(item, shape_store, model_type)

    def test_vocabulary_reaches_documentation_and_every_model_type(
        self, shape_store: ShapeStore
    ) -> None:
        """Guards the generator: vocabulary words hit every model type, and documentation."""
        store = shape_store.store
        seen = {
            (i["model_type"], i["entity_type"])
            for word in shape_store.vocabulary
            for i in store.text_search(word, limit=100)["items"]
        }
        for model_type in OSCALModelType:
            assert (model_type.value, "document") in seen, model_type
            assert (model_type.value, "child_element") in seen, model_type
        # Both guides share words with the OSCAL fixtures, so documentation rows match too.
        for word in ("catalog", "assessment"):
            assert store.search_documentation(word)["total"] >= 1, word
            assert store.text_search(word)["total"] >= 1, word


# ---------------------------------------------------------------------------
# Property 8: search freshness
# ---------------------------------------------------------------------------

# Lowercase ASCII letters only: one Search_Term each, valid in titles, prose, and IDs.
_LETTERS = "abcdefghijklmnopqrstuvwxyz"
# Every generated token starts with this prefix, so none is a stop word or fixture word.
_WORD_PREFIX = "zq"
# Where a catalog carries its unique token: document title, control title, or prose.
_PLACEMENTS = ("document-title", "control-title", "control-prose")


def _letters(n: int, width: int = 4) -> str:
    """Encode ``0 <= n < 26**width`` as ``width`` lowercase letters (base 26).

    Fixed width keeps ``prefix + serial + stem`` unique for distinct serials.
    """
    out = ""
    for _ in range(width):
        n, digit = divmod(n, 26)
        out = _LETTERS[digit] + out
    return out


@dataclass
class FreshCatalog:
    """A catalog written for one example, with its unique token and expected hit."""

    uuid: str
    control_id: str
    token: str
    placement: str
    document: dict


def _fresh_catalog(serial: str, stem: str, placement: str) -> FreshCatalog:
    """Return ``make_catalog()`` with a new UUID, control IDs, and a token at ``placement``.

    ``serial`` is unique per catalog, so the token and identifiers never repeat in the store.
    """
    token = f"{_WORD_PREFIX}{serial}{stem}"
    document = make_catalog()
    catalog = document["catalog"]
    catalog["uuid"] = str(uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, f"freshness/{serial}"))
    catalog["metadata"]["title"] = f"Freshness Catalog {serial}"
    controls = catalog["controls"]
    controls[0]["id"] = f"fa-{serial}-1"
    control = controls[1]
    control["id"] = f"fa-{serial}-2"
    control["parts"][0]["id"] = f"fa-{serial}-2_smt"
    if placement == "document-title":
        catalog["metadata"]["title"] = f"Freshness Catalog {token}"
    elif placement == "control-title":
        control["title"] = f"Account {token} Management"
    else:
        control["parts"][0]["prose"] = f"Define and document {token} system accounts."
    return FreshCatalog(catalog["uuid"], control["id"], token, placement, document)


@dataclass
class FreshnessStore:
    """A module store that each example scans new catalogs into, plus a serial counter."""

    store: OscalStore
    root: Path
    serial: int = 0

    def next_serial(self) -> str:
        self.serial += 1
        return _letters(self.serial)


@pytest.fixture(scope="module")
def freshness_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FreshnessStore]:
    directory = tmp_path_factory.mktemp("text-search-freshness")
    store = OscalStore(db_path=str(directory / "store.db"), cache_size=10, seed_from_bundled=False)
    try:
        yield FreshnessStore(store, directory)
    finally:
        store.close()


def _indexed_flags(store: OscalStore, uuids: list[str]) -> dict[str, int]:
    marks = ",".join("?" * len(uuids))
    rows = store._conn.execute(
        f"SELECT uuid, indexed FROM documents WHERE uuid IN ({marks})",  # noqa: S608 - placeholders only
        uuids,
    ).fetchall()
    return {r["uuid"]: r["indexed"] for r in rows}


def _expected_hit(catalog: FreshCatalog) -> tuple[str, str, str | None]:
    if catalog.placement == "document-title":
        return ("document", catalog.uuid, None)
    return ("child_element", catalog.control_id, catalog.uuid)


class TestNewDocumentsSearchableWithoutOtherCalls:
    """Feature: search-fetch-pairs, Property 8: Newly scanned documents are searchable
    without other calls.

    **Validates: Requirements 7.2**
    """

    @settings(max_examples=25, deadline=None)
    @given(
        catalogs=st.lists(
            st.tuples(
                st.text(alphabet=_LETTERS, min_size=1, max_size=8),
                st.sampled_from(_PLACEMENTS),
            ),
            min_size=1,
            max_size=3,
        ),
    )
    def test_text_search_finds_just_scanned_documents(
        self, freshness_store: FreshnessStore, catalogs: list[tuple[str, str]]
    ) -> None:
        store = freshness_store.store
        batch = freshness_store.root / f"batch-{freshness_store.next_serial()}"
        batch.mkdir()
        fresh = [
            _fresh_catalog(freshness_store.next_serial(), stem, placement)
            for stem, placement in catalogs
        ]
        for i, catalog in enumerate(fresh):
            (batch / f"catalog-{i}.json").write_text(json.dumps(catalog.document), "utf-8")

        assert store.scan_directory(batch) == len(fresh)
        uuids = [c.uuid for c in fresh]
        assert _indexed_flags(store, uuids) == dict.fromkeys(uuids, 0)

        for catalog in fresh:
            assert normalize_query(catalog.token) == (catalog.token,)
            # First call touching the new documents: no list/get/query call precedes it.
            page = store.text_search(catalog.token, limit=100)
            hits = {
                (i["entity_type"], i["entity_id"], i.get("parentDocumentUuid"))
                for i in page["items"]
            }
            assert _expected_hit(catalog) in hits, (catalog.token, page)

        # Requirement 7.2: text_search indexed every pending document it searched.
        assert _indexed_flags(store, uuids) == dict.fromkeys(uuids, 1)


# ---------------------------------------------------------------------------
# Property 9: budgeted paging
# ---------------------------------------------------------------------------

# Shared by every control (in its statement) and every catalog title, so one query hits
# them all; 160 controls with up-to-501-character descriptions overflow a page of 100.
_COMMON_WORD = "zephyrine"
# Carried by every third control only, so two-term queries mix all-terms and some-terms hits.
_RARE_WORD = "quillwort"
_BUDGET_QUERIES = (_COMMON_WORD, f"{_COMMON_WORD} {_RARE_WORD}", f"{_RARE_WORD} accounts")
# Filler vocabulary; the quote and non-ASCII words grow json.dumps output (escapes).
_FILLER = (
    "define", "document", "accounts", "review", "privileged", "audit", "boundary",
    "naïve", "“quoted”", 'say "hi"', "règle", "system", "organization", "frequency",
)  # fmt: skip
_BUDGET_CATALOGS = 4
_CONTROLS_PER_CATALOG = 40


def _prose(rng: random.Random, length: int, *, lead: str = "") -> str:
    """Return about ``length`` characters of filler prose that starts with ``lead``."""
    words = [lead] if lead else []
    size = len(lead)
    while size < length:
        word = rng.choice(_FILLER)
        words.append(word)
        size += len(word) + 1
    return " ".join(words)


def _budget_catalog(index: int, rng: random.Random) -> dict:
    """Return a model-valid catalog of controls with 300-3,000-character prose."""
    controls = []
    for n in range(_CONTROLS_PER_CATALOG):
        control_id = f"bp{index}-{n}"
        lead = f"{_COMMON_WORD} {_RARE_WORD}" if n % 3 == 0 else _COMMON_WORD
        parts = [
            {
                "id": f"{control_id}_smt",
                "name": "statement",
                "prose": _prose(rng, rng.randint(300, 3_000), lead=lead),
            }
        ]
        if rng.random() < 0.5:
            parts.append(
                {
                    "id": f"{control_id}_gdn",
                    "name": "guidance",
                    "prose": _prose(rng, rng.randint(300, 3_000)),
                }
            )
        controls.append({"id": control_id, "title": f"Budget Control {n}", "parts": parts})
    document = {
        "catalog": {
            "uuid": str(uuid_mod.uuid5(uuid_mod.NAMESPACE_URL, f"budget-paging/{index}")),
            "metadata": {
                "title": f"{_COMMON_WORD} Budget Catalog {index}",
                "last-modified": "2024-01-01T00:00:00Z",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
            "controls": controls,
        }
    }
    root_key = OSCALModelType.CATALOG.value
    MODEL_MAP[OSCALModelType.CATALOG].model_validate(document[root_key])
    return document


@dataclass
class BudgetStore:
    """A store of long-prose catalogs plus the stored description of every FTS entity."""

    store: OscalStore
    stored: dict[tuple[str, str], str]  # (entity_type, Element_ID or UUID) -> description


@pytest.fixture(scope="module")
def budget_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[BudgetStore]:
    directory = tmp_path_factory.mktemp("text-search-budget")
    docs = directory / "docs"
    docs.mkdir()
    rng = random.Random(9)  # noqa: S311 - deterministic test data, not crypto
    for i in range(_BUDGET_CATALOGS):
        (docs / f"catalog-{i}.json").write_text(json.dumps(_budget_catalog(i, rng)), "utf-8")
    store = OscalStore(db_path=str(directory / "store.db"), cache_size=10, seed_from_bundled=False)
    try:
        assert store.scan_directory(docs) == _BUDGET_CATALOGS
        store._like_search = _like_fallback_forbidden  # type: ignore[method-assign]
        store.text_search(_COMMON_WORD)  # index every catalog
        rows = store._conn.execute(
            "SELECT entity_type, element_id, description FROM fts_index"
        ).fetchall()
        stored = {(r["entity_type"], r["element_id"]): r["description"] or "" for r in rows}
        yield BudgetStore(store, stored)
    finally:
        store.close()


def _hit_key(item: dict) -> tuple:
    return (
        item["entity_type"],
        item["model_type"],
        item["entity_id"],
        item.get("parentDocumentUuid"),
    )


def _unbounded_hits(store: OscalStore, query_text: str) -> list[tuple]:
    """Return every hit key in result order from one unsliced, unbudgeted query."""
    terms = normalize_query(query_text)
    _, total = store._fts_search(terms, None, 0, 1)
    rows, _ = store._fts_search(terms, None, 0, total)
    return [_hit_key(store._text_search_item(row)) for row in rows]


def _assert_description(item: dict, stored: dict[tuple[str, str], str]) -> None:
    """Requirement 7.4: the cap, and the exact truncated form of a longer description."""
    full = stored[(item["entity_type"], item["entity_id"])]
    assert len(item["description"]) <= TEXT_SEARCH_DESCRIPTION_CAP + len(TRUNCATION_MARK)
    if len(full) > TEXT_SEARCH_DESCRIPTION_CAP:
        assert item["description"] == full[:TEXT_SEARCH_DESCRIPTION_CAP] + TRUNCATION_MARK
    else:
        assert item["description"] == full


class TestTextSearchPagesFitBudget:
    """Feature: search-fetch-pairs, Property 9: Text search pages fit the budget and
    cover every hit once.

    **Validates: Requirements 7.4, 7.5**
    """

    @settings(max_examples=50, deadline=None)
    @given(limit=LIMITS, query_text=st.sampled_from(_BUDGET_QUERIES))
    def test_paging_fits_budget_and_visits_every_hit_once(
        self, budget_store: BudgetStore, limit: int, query_text: str
    ) -> None:
        store = budget_store.store
        walked: list[tuple] = []
        offset = 0
        while True:
            page = store.text_search(query_text, offset=offset, limit=limit)
            # Requirement 7.5: the serialized page fits; limit reports the items returned.
            assert len(json.dumps(page)) <= RESPONSE_BUDGET, (offset, limit)
            assert page["limit"] == len(page["items"])
            assert page["offset"] == offset
            assert len(page["items"]) <= limit
            for item in page["items"]:
                _assert_description(item, budget_store.stored)
            walked += [_hit_key(item) for item in page["items"]]
            if not page["hasMore"]:
                break
            assert page["items"], "hasMore with an empty page would loop forever"
            offset += len(page["items"])

        expected = _unbounded_hits(store, query_text)
        assert walked == expected  # no duplicates, no gaps, same order
        assert len(set(walked)) == len(walked)
        assert len(walked) == page["total"]

    def test_fixture_overflows_a_full_page(self, budget_store: BudgetStore) -> None:
        """Guards the generator: long descriptions exist and 100 hits exceed the budget."""
        store = budget_store.store
        assert any(len(d) > TEXT_SEARCH_DESCRIPTION_CAP for d in budget_store.stored.values())
        assert any(len(d) <= TEXT_SEARCH_DESCRIPTION_CAP for d in budget_store.stored.values())
        page = store.text_search(_COMMON_WORD, limit=100)
        assert page["total"] > 100
        assert 0 < page["limit"] < 100
        assert page["hasMore"]
        rows, _ = store._fts_search(normalize_query(_COMMON_WORD), None, 0, 100)
        unbudgeted = [store._text_search_item(row) for row in rows]
        assert len(json.dumps({"items": unbudgeted})) > RESPONSE_BUDGET
