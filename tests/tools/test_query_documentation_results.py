"""Property tests for Doc_Result_Items of query_oscal_documentation.

One class per Correctness Property of the search-fetch-pairs design. The strategies at the
top of the file generate Bedrock ``retrieve`` responses and local Documentation_Documents and
are shared by every class. No test touches the network: KB_Mode runs use a stubbed
``Session``.

Generated values that must never reach a tool result carry an uppercase marker (see
``LEAK_MARKERS``). Passage text and local documents use only lowercase letters and a little
punctuation, so a marker in a serialized item can only come from a leak.
"""

import json
import logging
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
)
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools import query_documentation
from mcp_server_for_oscal.tools.oscal_store import (
    ORIGIN_BUNDLED,
    ORIGIN_USER,
    Origin,
    OscalStore,
    doc_id,
)
from mcp_server_for_oscal.tools.query_documentation import (
    KB_FAILED_MESSAGE,
    KB_MAX_RESULTS,
    interleave,
    kb_result_items,
    merge_page,
    query_oscal_documentation,
)

_MODULE = "mcp_server_for_oscal.tools.query_documentation"

# Uppercase markers of values that must not appear in any Doc_Result_Item.
LEAK_MARKERS = ("META", "REQ", "TOK", "HOST", "DIR", "QRY", "ARN")
KB_ITEM_KEYS = frozenset({"title", "snippet"})
LOCAL_ITEM_KEYS = frozenset({"docId", "title", "snippet"})
LOCAL_SNIPPET_MAX = 200
# A word shared by every generated local document, so a query on it finds all of them.
SHARED_WORD = "zebrafish"

_PROPERTY_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
_STORE_SETTINGS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# ------------------------------------------------------------------
# Strategies
# ------------------------------------------------------------------

words = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=10)
# Lowercase words only: no digits, no uppercase, no ".", so no marker, score, or
# location value can appear in a passage by chance.
plain_text = st.lists(words, min_size=0, max_size=30).map(" ".join)

# Heading text: ordinary words or a heading that looks like a Storage_Location, which
# kb_title must reject. None of these contain "s3://" or a leak marker.
heading_text = st.one_of(
    st.lists(words, min_size=1, max_size=6).map(" ".join),
    words.map(lambda w: f"https://example.org/{w}"),
    words.map(lambda w: f"arn:aws:bedrock:us-east-1:kb/{w}"),
    words.map(lambda w: f"/home/{w}/notes"),
    words.map(lambda w: f"c:\\users\\{w}"),
)


@st.composite
def passage_text(draw) -> str:
    """Passage text with zero or more markdown headings, any amount of plain text."""
    parts = []
    for _ in range(draw(st.integers(0, 3))):
        if draw(st.booleans()):
            hashes = "#" * draw(st.integers(1, 6))
            parts.append(f"{hashes} {draw(heading_text)}")
        parts.append(draw(plain_text))
    return "\n".join(parts)


marker_id = st.integers(min_value=0, max_value=10**6)
# The last path segment (``STEM``) is the only part of a location a title may draw on (D6),
# and only as a humanized stem. Directories (``DIR``), buckets and hosts (``HOST``), query
# strings and fragments (``QRY``), and every part of an ARN (``ARN``) must never appear.
stem_segment = st.builds(lambda n, w: f"STEM{n}_{w}", marker_id, words)
dir_segment = st.builds(lambda n, w: f"DIR{n}-{w}", marker_id, words)
extension = st.sampled_from([".md", ".html", ".pdf", ".txt"])
key_path = st.builds(
    lambda dirs, stem, ext: "/".join([*dirs, stem]) + ext,
    st.lists(dir_segment, max_size=2),
    stem_segment,
    extension,
)
url_suffix = st.sampled_from(["", "?versionId=QRY1", "#QRY-anchor", "/"])
# A bucket or host with no path segment after it names a storage location, not a document.
host_only_suffix = st.sampled_from(["", "/", "/?versionId=QRY1", "#QRY-anchor"])
s3_uri = st.one_of(
    st.builds(lambda n, k: f"s3://HOST{n}-bucket/{k}", marker_id, key_path),
    st.builds(lambda n, q: f"s3://HOST{n}-bucket{q}", marker_id, host_only_suffix),
)
web_url = st.one_of(
    st.builds(
        lambda n, k, q: f"https://HOST{n}.example.com/{k}{q}", marker_id, key_path, url_suffix
    ),
    st.builds(lambda n, q: f"https://HOST{n}.example.com{q}", marker_id, host_only_suffix),
)
arn = st.one_of(
    st.builds(lambda n, m: f"arn:aws:s3:::ARN{n}-bucket/ARN{m}-key.md", marker_id, marker_id),
    st.builds(lambda n: f"arn:aws:bedrock:us-east-1:123456789012:ARN{n}", marker_id),
    st.builds(lambda n: f"ARN:AWS:s3:::ARN{n}/ARN-doc", marker_id),
)
# A customDocumentLocation ID is an ARN, a path-like key, or an opaque ID (with or without
# separators). An opaque ID is a whole KB location value, so it must not become the title.
custom_id = st.one_of(
    arn,
    key_path,
    st.builds(lambda n, w: f"STEM{n}_{w}", marker_id, words),
    st.builds(lambda n: f"STEM{n}", marker_id),
)

location = st.one_of(
    st.builds(lambda v: {"type": "S3", "s3Location": {"uri": v}}, s3_uri),
    st.builds(lambda v: {"type": "WEB", "webLocation": {"url": v}}, web_url),
    st.builds(lambda v: {"type": "CONFLUENCE", "confluenceLocation": {"url": v}}, web_url),
    st.builds(lambda v: {"type": "SALESFORCE", "salesforceLocation": {"url": v}}, web_url),
    st.builds(lambda v: {"type": "SHAREPOINT", "sharePointLocation": {"url": v}}, web_url),
    st.builds(
        lambda v: {"type": "CUSTOM", "customDocumentLocation": {"id": v}},
        custom_id,
    ),
)
metadata_value = st.one_of(
    st.builds(lambda n: f"META{n}", marker_id),
    s3_uri,
    st.lists(st.builds(lambda n: f"META{n}", marker_id), max_size=3),
)
metadata = st.dictionaries(
    st.sampled_from(
        ["x-amz-bedrock-kb-source-uri", "x-amz-bedrock-kb-chunk-id", "title", "author", "METAkey"]
    ),
    metadata_value,
    max_size=4,
)
score = st.floats(min_value=0.0, max_value=1.0, allow_nan=False)


@st.composite
def retrieval_result(draw) -> dict:
    """One well-formed KB_Hit; ``location``, ``metadata``, and ``score`` may be absent."""
    result: dict = {"content": {"text": draw(passage_text()), "type": "TEXT"}}
    if draw(st.integers(0, 4)):
        result["location"] = draw(location)
    if draw(st.booleans()):
        result["metadata"] = draw(metadata)
    if draw(st.booleans()):
        result["score"] = draw(score)
    return result


malformed_result = st.one_of(
    st.none(),
    st.builds(lambda n: f"META{n}", marker_id),
    st.builds(lambda loc: {"location": loc}, location),
    st.builds(lambda loc: {"content": None, "location": loc}, location),
    st.builds(lambda n, loc: {"content": {"text": n}, "location": loc}, marker_id, location),
    st.builds(lambda md: {"content": "not a mapping", "metadata": md}, metadata),
)


@st.composite
def retrieve_response(draw) -> dict:
    """A Bedrock ``retrieve`` response with well-formed and malformed results."""
    results = draw(
        st.lists(
            st.one_of(retrieval_result(), retrieval_result(), malformed_result),
            max_size=12,
        )
    )
    response: dict = {
        "retrievalResults": results[:KB_MAX_RESULTS],
        "ResponseMetadata": {
            "RequestId": f"REQ{draw(marker_id)}",
            "HTTPStatusCode": 200,
            "HTTPHeaders": {"x-amzn-requestid": f"REQ{draw(marker_id)}"},
            "RetryAttempts": 0,
        },
    }
    if draw(st.booleans()):
        response["nextToken"] = f"TOK{draw(marker_id)}"
    if draw(st.booleans()):
        response["guardrailAction"] = "NONE"
    return response


# Local Documentation_Documents: lowercase title words and a body that always contains
# SHARED_WORD, sometimes far longer than the 200-character snippet limit.
local_document = st.tuples(
    st.lists(words, min_size=1, max_size=5).map(" ".join),
    st.lists(words, min_size=0, max_size=400).map(" ".join),
)
local_documents = st.lists(local_document, min_size=1, max_size=5)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def location_values(response: Mapping) -> list[str]:
    """Every location URI, URL, or ID string in ``response``."""
    values: list[str] = []
    for result in response.get("retrievalResults", []):
        if not isinstance(result, Mapping) or not isinstance(result.get("location"), Mapping):
            continue
        for key, loc in result["location"].items():
            if key != "type" and isinstance(loc, Mapping):
                values.extend(v for v in loc.values() if isinstance(v, str))
    return values


def leaked_strings(response: Mapping) -> list[str]:
    """Location, metadata, score, and response-metadata values that must not leak."""
    values = location_values(response)
    for result in response.get("retrievalResults", []):
        if not isinstance(result, Mapping):
            continue
        for value in (result.get("metadata") or {}).values():
            values.extend(value if isinstance(value, list) else [value])
        if isinstance(result.get("score"), float):
            values.append(json.dumps(result["score"]))
    meta = response.get("ResponseMetadata", {})
    values.append(meta.get("RequestId", ""))
    if "nextToken" in response:
        values.append(response["nextToken"])
    return [v for v in values if v]


def passage_texts(response: Mapping) -> list[str]:
    """The ``content.text`` of each well-formed result, in KB order."""
    texts = []
    for result in response.get("retrievalResults", []):
        content = result.get("content") if isinstance(result, Mapping) else None
        text = content.get("text") if isinstance(content, Mapping) else None
        if isinstance(text, str):
            texts.append(text)
    return texts


def assert_safe_title(title: str) -> None:
    """A KB title is ``""`` or free of every Storage_Location shape (Requirement 10.4)."""
    assert isinstance(title, str)
    if title:
        assert "://" not in title
        assert not title.casefold().startswith("arn:")
        assert not title.startswith(("/", "\\"))
        assert not (len(title) > 2 and title[1] == ":" and title[2] in "/\\")


def assert_no_leak(serialized: str, response: Mapping) -> None:
    """No location, metadata, score, or response-metadata value appears in ``serialized``."""
    assert "s3://" not in serialized
    for marker in LEAK_MARKERS:
        assert marker not in serialized, f"{marker!r} leaked into {serialized!r}"
    for value in leaked_strings(response):
        assert value not in serialized, f"{value!r} leaked into {serialized!r}"


@contextmanager
def local_store(documents: list[tuple[str, str]]) -> Iterator[OscalStore]:
    """A fresh store with ``documents`` split across the bundled and user Origins."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bundled, user = root / "bundled", root / "user"
        bundled.mkdir()
        user.mkdir()
        for i, (title, body) in enumerate(documents):
            target = bundled if i % 2 else user
            (target / f"doc-{i}.md").write_text(
                f"# {title}\n\n{SHARED_WORD} {body}\n", encoding="utf-8"
            )
        store = OscalStore(db_path=str(root / "docs.db"), seed_from_bundled=False)
        try:
            store.scan_directory(bundled, origin=ORIGIN_BUNDLED)
            store.scan_directory(user, origin=ORIGIN_USER)
            yield store
        finally:
            store.close()


def stub_session(session_class: Mock, response: object) -> None:
    """Wire ``Session().client().retrieve`` to return ``response``."""
    session_class.return_value.client.return_value.retrieve.return_value = response


# ------------------------------------------------------------------
# Property 11
# ------------------------------------------------------------------


class TestDocResultItemShape:
    """Feature: search-fetch-pairs, Property 11: Doc_Result_Items have one shape and carry
    no KB location data.

    **Validates: Requirements 3.3, 3.4, 10.2, 10.3, 10.4, 10.5**
    """

    @_PROPERTY_SETTINGS
    @given(response=retrieve_response())
    @example(
        response={
            "retrievalResults": [
                {
                    "content": {"text": "", "type": "TEXT"},
                    "location": {"type": "CUSTOM", "customDocumentLocation": {"id": "STEM0"}},
                }
            ]
        }
    )
    def test_kb_items_are_title_and_passage_only(self, response):
        items = kb_result_items(response)

        assert [item["snippet"] for item in items] == passage_texts(response)
        for item in items:
            assert set(item) == KB_ITEM_KEYS
            assert_safe_title(item["title"])
            assert_no_leak(json.dumps(item), response)

    @_STORE_SETTINGS
    @given(documents=local_documents)
    def test_local_items_are_doc_id_title_snippet(self, documents):
        with local_store(documents) as store:
            page = store.search_documentation(SHARED_WORD, 0, 100)

        assert page["total"] == len(documents)
        for item in page["items"]:
            assert set(item) == LOCAL_ITEM_KEYS
            assert all(isinstance(item[key], str) for key in LOCAL_ITEM_KEYS)
            assert item["docId"]
            assert len(item["snippet"]) <= LOCAL_SNIPPET_MAX

    @_STORE_SETTINGS
    @given(response=retrieve_response(), documents=local_documents)
    def test_kb_mode_page_carries_no_kb_location_data(self, response, documents):
        with (
            local_store(documents) as store,
            patch.object(query_documentation, "_store", store),
            patch(f"{_MODULE}.config") as cfg,
            patch(f"{_MODULE}.Session") as session_class,
        ):
            cfg.aws_profile = None
            cfg.knowledge_base_id = "test-kb-id"
            stub_session(session_class, response)
            page = query_oscal_documentation(SHARED_WORD, offset=0, limit=100)

        kb_items = [item for item in page["items"] if "docId" not in item]
        local_items = [item for item in page["items"] if "docId" in item]
        assert [item["snippet"] for item in kb_items] == passage_texts(response)
        for item in kb_items:
            assert set(item) == KB_ITEM_KEYS
            assert_safe_title(item["title"])
        for item in local_items:
            assert set(item) == LOCAL_ITEM_KEYS
            assert len(item["snippet"]) <= LOCAL_SNIPPET_MAX
        assert_no_leak(json.dumps(page), response)


# ------------------------------------------------------------------
# Property 12
# ------------------------------------------------------------------

# Tagged stand-ins for KB_Hits and Local_Hits: each item says which list it came from and
# its position there, so order and alternation can be read back from any merged sequence.
kb_tags = st.integers(0, KB_MAX_RESULTS).map(lambda n: [{"kb": i} for i in range(n)])
local_tags = st.integers(0, 150).map(lambda n: [{"local": i} for i in range(n)])
page_limit = st.integers(1, 100)

# KB passages for the full tool: a unique lowercase tag first (so each KB_Hit is told apart
# from every other), then filler that is sometimes long enough for budget_page() to trim a
# page. No passage alone exceeds RESPONSE_BUDGET, so budget_page() never truncates one.
kb_passage_length = st.one_of(st.integers(0, 200), st.integers(2_000, 9_000))
kb_passage_lengths = st.lists(kb_passage_length, max_size=40)
many_local_documents = st.lists(local_document, min_size=0, max_size=14)
_MAX_PAGES = 1_000


def kb_response(lengths: list[int]) -> dict:
    """A ``retrieve`` response with one KB_Hit per entry of ``lengths``, in that order."""
    return {
        "retrievalResults": [
            {"content": {"text": f"kbhit{i} " + "w" * n, "type": "TEXT"}}
            for i, n in enumerate(lengths)
        ]
    }


def walk_pages(query: str, limit: int) -> list[dict]:
    """Every item of ``query_oscal_documentation`` from offset 0 while ``hasMore``."""
    items: list[dict] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        page = query_oscal_documentation(query, offset=offset, limit=limit)
        assert page["offset"] == offset
        assert len(page["items"]) <= limit
        items.extend(page["items"])
        if not page["hasMore"]:
            return items
        # A page that asks for more must make progress, or the walk would never end.
        assert page["items"], f"empty page with hasMore at offset {offset}"
        offset += len(page["items"])
    raise AssertionError("page walk did not terminate")


def item_key(item: Mapping) -> str:
    """The identity of a Doc_Result_Item: its Doc_ID, else its KB passage."""
    return item["docId"] if "docId" in item else item["snippet"]


class TestMergedPaging:
    """Feature: search-fetch-pairs, Property 12: Merged pages walk the interleaved sequence
    exactly once.

    **Validates: Requirements 11.3, 11.4, 11.5, 11.6**
    """

    @_PROPERTY_SETTINGS
    @given(kb=kb_tags, local=local_tags)
    def test_interleave_keeps_order_and_alternates_then_appends(self, kb, local):
        merged = interleave(kb, local)

        # 11.3: each list keeps its own order, and nothing is added or lost.
        assert [item for item in merged if "kb" in item] == kb
        assert [item for item in merged if "local" in item] == local
        # 11.4: strict alternation, KB_Hit first, while both lists have items...
        shared = min(len(kb), len(local))
        for i in range(shared):
            assert merged[2 * i] == kb[i]
            assert merged[2 * i + 1] == local[i]
        # ...then the rest of the longer list.
        assert merged[2 * shared :] == kb[shared:] + local[shared:]

    @_PROPERTY_SETTINGS
    @given(
        kb=kb_tags,
        local=local_tags,
        offset=st.integers(0, KB_MAX_RESULTS + 160),
        limit=page_limit,
    )
    def test_merge_page_is_a_slice_of_the_interleaving(self, kb, local, offset, limit):
        end = offset + limit
        page = merge_page(kb, local[:end], len(local), offset, limit)

        total = len(kb) + len(local)
        assert page["items"] == interleave(kb, local)[offset:end]
        assert page["total"] == total
        assert page["offset"] == offset
        assert page["limit"] == limit
        assert page["hasMore"] == (end < total)

    @_PROPERTY_SETTINGS
    @given(kb=kb_tags, local=local_tags, limit=page_limit)
    def test_merge_page_walk_reproduces_the_interleaving(self, kb, local, limit):
        walked: list[dict] = []
        offset = 0
        while True:
            # Each page sees only the local prefix the tool would fetch for it.
            page = merge_page(kb, local[: offset + limit], len(local), offset, limit)
            walked.extend(page["items"])
            if not page["hasMore"]:
                break
            offset += len(page["items"])

        assert walked == interleave(kb, local)

    @_STORE_SETTINGS
    @given(lengths=kb_passage_lengths, documents=many_local_documents, limit=page_limit)
    @example(lengths=[9_000] * 6, documents=[("alpha", "beta")] * 4, limit=100)
    def test_kb_mode_walk_yields_interleaved_kb_and_user_hits_once(self, lengths, documents, limit):
        response = kb_response(lengths)
        with (
            local_store(documents) as store,
            patch.object(query_documentation, "_store", store),
            patch(f"{_MODULE}.config") as cfg,
            patch(f"{_MODULE}.Session") as session_class,
        ):
            cfg.aws_profile = None
            cfg.knowledge_base_id = "test-kb-id"
            stub_session(session_class, response)

            walked = walk_pages(SHARED_WORD, limit)

            user_hits = store.search_documentation(
                SHARED_WORD, 0, len(documents) + 1, origins=(ORIGIN_USER,)
            )["items"]
            bundled_ids = {
                item["docId"]
                for item in store.search_documentation(
                    SHARED_WORD, 0, len(documents) + 1, origins=(ORIGIN_BUNDLED,)
                )["items"]
            }
            retrieve = session_class.return_value.client.return_value.retrieve
            calls = retrieve.call_args_list

        # local_store puts even-indexed documents in the user Origin.
        assert len(user_hits) == len(documents[::2])
        expected = interleave(kb_result_items(response), user_hits)
        keys = [item_key(item) for item in walked]
        # 11.3, 11.4, 11.6: the walk is the interleaved sequence, no repeats, no gaps.
        assert keys == [item_key(item) for item in expected]
        assert len(set(keys)) == len(keys)
        assert walked == expected
        # 11.2 (context): bundled documents never appear in KB_Mode.
        assert not bundled_ids & {item["docId"] for item in walked if "docId" in item}
        # 11.5 (D5): every page requests the fixed Bedrock maximum of KB_Hits.
        assert calls
        for call in calls:
            config_arg = call.kwargs["retrievalConfiguration"]
            assert config_arg["vectorSearchConfiguration"]["numberOfResults"] == KB_MAX_RESULTS


# ------------------------------------------------------------------
# Property 13
# ------------------------------------------------------------------

# A token no generated word can contain, held by one user document only (Requirement 11.9).
UNIQUE_WORD = "quokkaunique"
# Words that can never match SHARED_WORD or UNIQUE_WORD, even as a prefix or substring.
other_words = words.filter(lambda w: SHARED_WORD not in w and UNIQUE_WORD not in w)
# (matches the query, title, body). Non-matching documents never hold SHARED_WORD.
origin_document = st.tuples(
    st.booleans(),
    st.lists(other_words, min_size=1, max_size=4).map(" ".join),
    st.lists(other_words, min_size=0, max_size=40).map(" ".join),
)
origin_documents = st.lists(origin_document, min_size=0, max_size=6)
kb_failure = st.sampled_from(
    [
        ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Retrieve"),
        RuntimeError("knowledge base unavailable"),
        Exception("boom"),
    ]
)


@contextmanager
def origin_store(
    bundled_docs: list[tuple[bool, str, str]],
    user_docs: list[tuple[bool, str, str]],
    *,
    unique_user_doc: bool = False,
) -> Iterator[tuple[OscalStore, set[str], set[str], str]]:
    """A store with ``bundled_docs`` and ``user_docs`` under their Origins.

    Yields the store, the Doc_IDs of the matching bundled and user documents, and the
    Doc_ID of the user document holding ``UNIQUE_WORD`` (``""`` unless requested).
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        matching: dict[str, set[str]] = {ORIGIN_BUNDLED: set(), ORIGIN_USER: set()}
        groups: tuple[tuple[Origin, str, list[tuple[bool, str, str]]], ...] = (
            (ORIGIN_BUNDLED, "b", bundled_docs),
            (ORIGIN_USER, "u", user_docs),
        )
        for origin, prefix, docs in groups:
            (root / origin).mkdir()
            for i, (matches, title, body) in enumerate(docs):
                name = f"{prefix}-{i}.md"
                lead = f"{SHARED_WORD} " if matches else ""
                (root / origin / name).write_text(f"# {title}\n\n{lead}{body}\n", encoding="utf-8")
                if matches:
                    matching[origin].add(doc_id(origin, name))
        unique_id = ""
        if unique_user_doc:
            name = "unique.md"
            (root / ORIGIN_USER / name).write_text(
                f"# my notes\n\n{UNIQUE_WORD} user content\n", encoding="utf-8"
            )
            unique_id = doc_id(ORIGIN_USER, name)
        store = OscalStore(db_path=str(root / "docs.db"), seed_from_bundled=False)
        try:
            store.scan_directory(root / ORIGIN_BUNDLED, origin=ORIGIN_BUNDLED)
            store.scan_directory(root / ORIGIN_USER, origin=ORIGIN_USER)
            yield store, matching[ORIGIN_BUNDLED], matching[ORIGIN_USER], unique_id
        finally:
            store.close()


def local_doc_ids(items: list[dict]) -> list[str]:
    """The Doc_IDs of the Local_Hits in ``items``, in order."""
    return [item["docId"] for item in items if "docId" in item]


class TestOriginScopePerMode:
    """Feature: search-fetch-pairs, Property 13: Each mode searches the right Origins.

    **Validates: Requirements 11.1, 11.2, 11.7, 11.9**
    """

    @_STORE_SETTINGS
    @given(
        bundled_docs=origin_documents,
        user_docs=origin_documents,
        kb_id=st.sampled_from(["", "   "]),
        limit=page_limit,
    )
    def test_local_mode_searches_both_origins(self, bundled_docs, user_docs, kb_id, limit):
        with (
            origin_store(bundled_docs, user_docs) as (store, bundled_ids, user_ids, _),
            patch.object(query_documentation, "_store", store),
            patch(f"{_MODULE}.config") as cfg,
            patch(f"{_MODULE}.Session") as session_class,
        ):
            cfg.aws_profile = None
            cfg.knowledge_base_id = kb_id
            walked = walk_pages(SHARED_WORD, limit)

        ids = local_doc_ids(walked)
        # 11.7: every item is a Local_Hit, and they are the matches of both Origins.
        assert len(ids) == len(walked)
        assert len(set(ids)) == len(ids)
        assert set(ids) == bundled_ids | user_ids
        session_class.assert_not_called()

    @_STORE_SETTINGS
    @given(
        bundled_docs=origin_documents,
        user_docs=origin_documents,
        lengths=st.lists(st.integers(0, 200), max_size=8),
        limit=page_limit,
    )
    def test_kb_mode_searches_kb_and_user_origin_only(
        self, bundled_docs, user_docs, lengths, limit
    ):
        response = kb_response(lengths)
        with (
            origin_store(bundled_docs, user_docs) as (store, bundled_ids, user_ids, _),
            patch.object(query_documentation, "_store", store),
            patch(f"{_MODULE}.config") as cfg,
            patch(f"{_MODULE}.Session") as session_class,
        ):
            cfg.aws_profile = None
            cfg.knowledge_base_id = "test-kb-id"
            stub_session(session_class, response)
            walked = walk_pages(SHARED_WORD, limit)

        ids = local_doc_ids(walked)
        # 11.1: the KB_Hits are the KB passages, and the Local_Hits the matching user docs.
        assert [item for item in walked if "docId" not in item] == kb_result_items(response)
        assert len(set(ids)) == len(ids)
        assert set(ids) == user_ids
        # 11.2: no bundled document appears in KB_Mode.
        assert not set(ids) & bundled_ids

    @_STORE_SETTINGS
    @given(
        bundled_docs=origin_documents,
        user_docs=origin_documents,
        failure=kb_failure,
        limit=page_limit,
    )
    def test_kb_failure_falls_back_to_both_origins(self, bundled_docs, user_docs, failure, limit):
        with (
            origin_store(bundled_docs, user_docs) as (store, bundled_ids, user_ids, _),
            patch.object(query_documentation, "_store", store),
            patch(f"{_MODULE}.config") as cfg,
            patch(f"{_MODULE}.Session") as session_class,
        ):
            cfg.aws_profile = None
            cfg.knowledge_base_id = "test-kb-id"
            session_class.return_value.client.return_value.retrieve.side_effect = failure
            walked = walk_pages(SHARED_WORD, limit)

        ids = local_doc_ids(walked)
        # 11.8 (context): a failed KB call gives Local_Mode results over both Origins.
        assert len(ids) == len(walked)
        assert len(set(ids)) == len(ids)
        assert set(ids) == bundled_ids | user_ids

    @_STORE_SETTINGS
    @given(
        bundled_docs=origin_documents,
        user_docs=origin_documents,
        kb_id=st.sampled_from(["", "test-kb-id"]),
        lengths=st.lists(st.integers(0, 200), max_size=8),
    )
    def test_user_doc_with_unique_token_is_found_in_both_modes(
        self, bundled_docs, user_docs, kb_id, lengths
    ):
        with (
            origin_store(bundled_docs, user_docs, unique_user_doc=True) as (
                store,
                _,
                _,
                unique_id,
            ),
            patch.object(query_documentation, "_store", store),
            patch(f"{_MODULE}.config") as cfg,
            patch(f"{_MODULE}.Session") as session_class,
        ):
            cfg.aws_profile = None
            cfg.knowledge_base_id = kb_id
            stub_session(session_class, kb_response(lengths))
            walked = walk_pages(UNIQUE_WORD, 10)

        # 11.9: the user document is the only Local_Hit, in Local_Mode and KB_Mode.
        assert local_doc_ids(walked) == [unique_id]


# ------------------------------------------------------------------
# Property 18
# ------------------------------------------------------------------

# Sensitive values carry uppercase markers; local documents hold only lowercase words and
# Doc_IDs are lowercase UUIDs, so a marker in any client-facing text can only be a leak.
account_id = st.integers(min_value=10**11, max_value=10**12 - 1).map(str)
kb_id_value = st.builds(lambda n: f"KBID{n}", marker_id)
request_id = st.builds(lambda n: f"REQ{n}-request", marker_id)
sensitive_s3 = st.builds(lambda n, k: f"s3://HOST{n}-bucket/{k}", marker_id, key_path)
sensitive_path = st.one_of(
    st.builds(lambda n, w: f"/Users/DIR{n}/{w}/notes.md", marker_id, words),
    st.builds(lambda n, w: f"C:\\DIR{n}\\{w}\\notes.md", marker_id, words),
)
endpoint_url = st.builds(
    lambda n: f"https://HOST{n}.bedrock-agent-runtime.us-east-1.amazonaws.com", marker_id
)


@st.composite
def sensitive_values(draw) -> dict[str, str]:
    """One of each sensitive value; the ARN embeds the account and KB IDs."""
    account = draw(account_id)
    kb = draw(kb_id_value)
    return {
        "account": account,
        "kb": kb,
        "request": draw(request_id),
        "s3": draw(sensitive_s3),
        "path": draw(sensitive_path),
        "arn": f"arn:aws:bedrock:us-east-1:{account}:knowledge-base/{kb}",
        "endpoint": draw(endpoint_url),
    }


def detail_text(values: Mapping[str, str]) -> str:
    """An error message that names every sensitive value."""
    return (
        f"User: arn:aws:iam::{values['account']}:user/ARN-dev is not authorized on "
        f"{values['arn']} (KB {values['kb']}, request {values['request']}); "
        f"source {values['s3']} cached at {values['path']}"
    )


def build_failure(kind: str, values: Mapping[str, str]) -> Exception:
    """A boto3/botocore or generic failure whose message or attributes hold ``values``."""
    text = detail_text(values)
    if kind == "client-error":
        return ClientError(
            {
                "Error": {"Code": "AccessDeniedException", "Message": text},
                "ResponseMetadata": {
                    "RequestId": values["request"],
                    "HostId": "",
                    "HTTPStatusCode": 403,
                    "HTTPHeaders": {
                        "x-amzn-requestid": values["request"],
                        "x-amzn-errortype": f"AccessDeniedException:{values['arn']}",
                    },
                    "RetryAttempts": 0,
                },
            },
            "Retrieve",
        )
    if kind == "no-credentials":
        return NoCredentialsError()
    if kind == "endpoint":
        return EndpointConnectionError(endpoint_url=f"{values['endpoint']}/{values['kb']}")
    if kind == "param-validation":
        return ParamValidationError(report=text)
    if kind == "runtime":
        return RuntimeError(text)
    return Exception(text)


failure_kind = st.sampled_from(
    ["client-error", "no-credentials", "endpoint", "param-validation", "runtime", "exception"]
)
# Where the failure is raised: Session(), Session().client(), or client.retrieve().
failure_site = st.sampled_from(["session", "client", "retrieve"])


def raise_at(session_class: Mock, site: str, failure: Exception) -> None:
    """Make the stubbed boto3 call at ``site`` raise ``failure``."""
    if site == "session":
        session_class.side_effect = failure
    elif site == "client":
        session_class.return_value.client.side_effect = failure
    else:
        session_class.return_value.client.return_value.retrieve.side_effect = failure


def client_strings(ctx: Mock) -> list[str]:
    """Every string argument of every call made on ``ctx`` (all Client_Facing_Messages)."""
    strings: list[str] = []
    for call in ctx.mock_calls:
        _, args, kwargs = call
        strings.extend(a for a in (*args, *kwargs.values()) if isinstance(a, str))
    return strings


class _Records(logging.Handler):
    """Collect log records of one logger, without the function-scoped caplog fixture."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def captured_logs(name: str) -> Iterator[list[logging.LogRecord]]:
    """Records emitted on logger ``name`` while the block runs."""
    handler = _Records()
    target = logging.getLogger(name)
    target.addHandler(handler)
    try:
        yield handler.records
    finally:
        target.removeHandler(handler)


class TestKbFailureMessages:
    """Feature: search-fetch-pairs, Property 18: Knowledge Base failures emit no location
    or ARN.

    **Validates: Requirements 4.3, 11.8**
    """

    @_STORE_SETTINGS
    @given(
        bundled_docs=origin_documents,
        user_docs=origin_documents,
        values=sensitive_values(),
        kind=failure_kind,
        site=failure_site,
        profile=st.sampled_from([None, "PROFILE-dev"]),
    )
    def test_kb_failure_sends_only_the_fixed_warning(
        self, bundled_docs, user_docs, values, kind, site, profile
    ):
        failure = build_failure(kind, values)
        ctx = AsyncMock()
        ctx.session = Mock()
        ctx.session.client_params = {}
        raised = ""
        with (
            origin_store(bundled_docs, user_docs) as (store, bundled_ids, user_ids, _),
            patch.object(query_documentation, "_store", store),
            patch(f"{_MODULE}.config") as cfg,
            patch(f"{_MODULE}.Session") as session_class,
            captured_logs(_MODULE) as records,
        ):
            cfg.aws_profile = profile
            cfg.knowledge_base_id = values["kb"]
            raise_at(session_class, site, failure)
            try:
                result = query_oscal_documentation(SHARED_WORD, offset=0, limit=100, ctx=ctx)
            except Exception as err:  # recorded so its text is checked, then failed below
                raised = f"{type(err).__name__}: {err}"
                result = {}
            expected = query_documentation.query_local(SHARED_WORD, 0, 100)

        # 4.3: nothing the client sees names a location, ARN, ID, path, or request detail.
        messages = client_strings(ctx)
        outgoing = [*messages, raised, *(str(v) for k, v in result.items() if k == "error")]
        for text in outgoing:
            assert "s3://" not in text
            assert "arn:" not in text.casefold()
            for value in values.values():
                assert value not in text, f"{value!r} leaked into {text!r}"
        assert not raised, f"KB failure escaped to the caller: {raised}"

        # The only Client_Facing_Message is the fixed warning, sent once.
        assert messages == [KB_FAILED_MESSAGE]
        ctx.warning.assert_awaited_once_with(KB_FAILED_MESSAGE)
        for level in ("error", "info", "debug"):
            getattr(ctx, level).assert_not_called()

        # 11.8: the result is the Local_Mode page over both Origins.
        assert result == expected
        ids = local_doc_ids(result["items"])
        assert len(ids) == len(result["items"])
        assert set(ids) == bundled_ids | user_ids

        # 4.6: the log record keeps the details, including the original exception.
        failed = [r for r in records if r.getMessage() == "Knowledge Base query failed"]
        assert len(failed) == 1
        assert failed[0].levelno == logging.WARNING
        assert failed[0].exc_info is not None
        assert failed[0].exc_info[1] is failure
