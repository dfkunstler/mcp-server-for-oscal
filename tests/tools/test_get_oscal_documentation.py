"""Tests for the ``get_oscal_documentation`` Doc_Fetch_Tool (spec: search-fetch-pairs).

One class per correctness property. The fixtures below write markdown Documentation_Documents
of both Origins into a fresh OscalStore and install it as the module-level ``_store`` of
``query_documentation``, restoring the previous store afterwards.
"""

import asyncio
import json
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st
from mcp.server.mcpserver import MCPServer

from mcp_server_for_oscal.tools import query_documentation
from mcp_server_for_oscal.tools.descriptions import LINE_COUNT_DOC, START_LINE_DOC
from mcp_server_for_oscal.tools.oscal_store import (
    ORIGIN_BUNDLED,
    ORIGIN_USER,
    ORIGINS,
    Origin,
    OscalStore,
    doc_id,
)
from mcp_server_for_oscal.tools.query_documentation import get_oscal_documentation
from mcp_server_for_oscal.tools.utils import (
    MAX_LINE_COUNT,
    RESPONSE_BUDGET,
    TRUNCATION_MARK,
    OSCALModelType,
)

# Keys every Doc_Fetch_Tool response has; ``truncated`` is added when one line was cut.
RESPONSE_KEYS = frozenset(
    {"docId", "title", "start-line", "end-line", "total-lines", "has-more", "text"}
)

_PROPERTY_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


# ------------------------------------------------------------------
# Fixtures and helpers (shared by every property class in this file)
# ------------------------------------------------------------------


@dataclass(frozen=True)
class StoredDocs:
    """A store holding markdown docs, keyed by ``(origin, relative name)`` → Doc_ID."""

    store: OscalStore
    ids: Mapping[tuple[Origin, str], str]


def stored_text(written: str) -> str:
    """The text the store keeps for a file written with ``written``.

    Markdown is read with universal newlines, so ``\\r\\n`` and a lone ``\\r`` become ``\\n``.
    """
    return written.replace("\r\n", "\n").replace("\r", "\n")


@contextmanager
def doc_store(docs: Mapping[tuple[Origin, str], str]) -> Iterator[StoredDocs]:
    """A fresh store with each ``(origin, relative name) → text`` written as a markdown file.

    Files are written byte-for-byte (no newline translation) under one directory per Origin
    and scanned with that Origin. The store is installed as ``query_documentation._store``
    for the duration of the context and the previous store is restored afterwards.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dirs = {origin: root / origin for origin in ORIGINS}
        for directory in dirs.values():
            directory.mkdir()
        for (origin, rel), text in docs.items():
            path = dirs[origin] / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
        store = OscalStore(db_path=str(root / "docs.db"), cache_size=4, seed_from_bundled=False)
        saved = query_documentation._store
        try:
            for origin, directory in dirs.items():
                store.scan_directory(directory, origin=origin)
            query_documentation.init_store(store)
            yield StoredDocs(store, {key: doc_id(*key) for key in docs})
        finally:
            query_documentation._store = saved
            store.close()  # Windows cannot delete an open SQLite file


def page_through(doc: str, line_count: int) -> list[dict]:
    """Every response of paging ``doc`` from line 1 with next start = ``end-line`` + 1."""
    pages = [get_oscal_documentation(doc, start_line=1, line_count=line_count)]
    while pages[-1]["has-more"]:
        assert len(pages) <= pages[-1]["total-lines"], "paging did not advance"
        nxt = pages[-1]["end-line"] + 1
        pages.append(get_oscal_documentation(doc, start_line=nxt, line_count=line_count))
    return pages


def json_size(value: object) -> int:
    """Serialized size as measured for the Response_Budget."""
    return len(json.dumps(value))


# ------------------------------------------------------------------
# Strategies
# ------------------------------------------------------------------

# Line content: any non-surrogate code point except NUL and the CR/LF line endings, plus
# explicit non-ASCII, astral, and other str.splitlines() boundaries (\x0b, \x0c, \x1c-\x1e,
# \x85, \u2028, \u2029), which split a stored line without being CR/LF.
_FRAGMENT_CHARS = st.one_of(
    st.characters(blacklist_categories=["Cs"], blacklist_characters="\x00\r\n"),
    st.sampled_from(["é", "€", "中", "𝔘", "😀", "\x0b", "\x0c", "\x1c", "\x85", "\u2028"]),
)
# Mostly short lines; some medium lines push whole pages over budget, and the last entry
# makes a line that alone exceeds RESPONSE_BUDGET (each fragment character serializes to
# at least one character).
_REPEATS = [1, 1, 1, 2, 5, 40, 400, 3000, RESPONSE_BUDGET + 1]
_ENDINGS = ["\n", "\n", "\r\n", "\r", ""]


@st.composite
def markdown_lines(draw: st.DrawFn) -> str:
    """One line: a short fragment repeated a random number of times, plus a line ending."""
    fragment = draw(st.text(_FRAGMENT_CHARS, min_size=1, max_size=4))
    return fragment * draw(st.sampled_from(_REPEATS)) + draw(st.sampled_from(_ENDINGS))


@st.composite
def markdown_documents(draw: st.DrawFn) -> str:
    """A markdown text the store ingests (not blank), optionally starting with a heading."""
    heading = draw(st.sampled_from(["", "# Guide\n", "## Ünïcode 😀 title\r\n"]))
    text = heading + "".join(draw(st.lists(markdown_lines(), min_size=1, max_size=25)))
    # The store skips blank files; give whitespace-only draws one visible character.
    return text if text.strip() else text + "x"


# ------------------------------------------------------------------
# Property 14
# ------------------------------------------------------------------


class TestPagingReconstructsDocument:
    """Feature: search-fetch-pairs, Property 14: Paging a document reconstructs its text
    within budget.

    **Validates: Requirements 12.3, 12.8, 12.9, 12.10**
    """

    @_PROPERTY_SETTINGS
    @given(
        text=markdown_documents(),
        origin=st.sampled_from(ORIGINS),
        line_count=st.integers(min_value=1, max_value=MAX_LINE_COUNT),
    )
    @example(text="# A\r\nb\rc\n", origin=ORIGIN_BUNDLED, line_count=1)
    @example(text="x" * (RESPONSE_BUDGET + 1) + "\nend", origin=ORIGIN_USER, line_count=500)
    @example(text="😀" * RESPONSE_BUDGET + "\r\n" + "é\n" * 3, origin=ORIGIN_USER, line_count=2)
    @example(text=("m" * 1000 + "\n") * 45, origin=ORIGIN_BUNDLED, line_count=200)
    def test_paging_reconstructs_text_within_budget(
        self, text: str, origin: Origin, line_count: int
    ) -> None:
        other: Origin = ORIGIN_USER if origin == ORIGIN_BUNDLED else ORIGIN_BUNDLED
        # A decoy with the same Relative_Name in the other Origin has a different Doc_ID.
        docs: dict[tuple[Origin, str], str] = {
            (origin, "guide.md"): text,
            (other, "guide.md"): "# Decoy\n\ndecoy text\n",
        }
        with doc_store(docs) as stored:
            target = stored.ids[(origin, "guide.md")]
            expected = stored_text(text)
            lines = expected.splitlines(keepends=True)
            stored_doc = stored.store.get_documentation(target)
            assert stored_doc is not None
            assert "".join(stored_doc.lines) == expected

            pages = page_through(target, line_count)

        next_start = 1
        any_truncated = False
        for page in pages:
            assert json_size(page) <= RESPONSE_BUDGET
            assert set(page) - RESPONSE_KEYS <= {"truncated"}
            assert set(page) >= RESPONSE_KEYS
            assert page["docId"] == target
            assert page["title"] == stored_doc.title
            assert page["total-lines"] == len(lines)

            start, end = page["start-line"], page["end-line"]
            assert start == next_start, "a line was skipped or repeated"
            assert start <= end <= min(start + line_count - 1, len(lines))
            assert page["has-more"] == (end < len(lines))

            if page.get("truncated"):
                any_truncated = True
                assert page["truncated"] is True
                assert end == start, "a truncated response holds exactly one line"
                line = lines[start - 1]
                assert page["text"].endswith(TRUNCATION_MARK)
                kept = page["text"][: -len(TRUNCATION_MARK)]
                assert len(kept) < len(line)
                assert line.startswith(kept)
                whole = {k: v for k, v in page.items() if k != "truncated"} | {"text": line}
                assert json_size(whole) > RESPONSE_BUDGET, "a line that fits was truncated"
            else:
                assert page["text"] == "".join(lines[start - 1 : end])
                if end < min(start + line_count - 1, len(lines)):
                    # Fewer lines than requested only when one more would exceed the budget.
                    longer = page | {
                        "end-line": end + 1,
                        "has-more": end + 1 < len(lines),
                        "text": page["text"] + lines[end],
                    }
                    assert json_size(longer) > RESPONSE_BUDGET
            next_start = end + 1

        event("truncated line" if any_truncated else "no truncation")
        event("multi-page" if len(pages) > 1 else "single page")
        assert next_start == len(lines) + 1, "paging stopped before the last line"
        assert pages[-1]["has-more"] is False
        if not any_truncated:
            assert "".join(page["text"] for page in pages) == expected


# ------------------------------------------------------------------
# Property 15
# ------------------------------------------------------------------

# Two Documentation_Documents (one per Origin) and one OSCAL catalog in the same store. The
# catalog is written into the user Scan_Root, so the scan ingests it as an OSCAL document
# with its Document_UUID, not as documentation.
_CATALOG_UUID = "c1d2e3f4-5678-4abc-8def-aabbccddeeff"
_CATALOG_JSON = json.dumps(
    {
        "catalog": {
            "uuid": _CATALOG_UUID,
            "metadata": {
                "title": "Fetch Fixture Catalog",
                "last-modified": "2024-01-01T00:00:00Z",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
        }
    }
)
_DOC_KEYS: tuple[tuple[Origin, str], ...] = (
    (ORIGIN_BUNDLED, "guide.md"),
    (ORIGIN_USER, "notes/local.md"),
)
_UNKNOWN_ID_DOCS: Mapping[tuple[Origin, str], str] = {
    _DOC_KEYS[0]: "# Bundled Guide\n\nbundled text\n",
    _DOC_KEYS[1]: "# Local Notes\n\nuser text\n",
    (ORIGIN_USER, "catalog.json"): _CATALOG_JSON,
}
# Doc_IDs are uuid5 of Origin and Relative_Name, so they are known before the scan.
_STORED_DOC_IDS = frozenset(doc_id(*key) for key in _DOC_KEYS)

_HEX = "0123456789abcdef"
_PATH_SEGMENT = st.text(
    st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="-_."),
    min_size=1,
    max_size=12,
)


@st.composite
def _near_miss_doc_ids(draw: st.DrawFn) -> str:
    """A stored Doc_ID with one character changed, its case changed, or whitespace around it."""
    stored = draw(st.sampled_from(sorted(_STORED_DOC_IDS)))
    kind = draw(st.sampled_from(["char", "case", "space"]))
    if kind == "case":
        return stored.upper()
    if kind == "space":
        before = draw(st.sampled_from(["", " ", "\t", "\n"]))
        after = draw(st.sampled_from(["", " ", "\r\n", "\u00a0"]))
        return before + stored + after if before or after else stored + " "
    i = draw(st.integers(min_value=0, max_value=len(stored) - 1))
    new = draw(st.sampled_from([c for c in _HEX + "-gZ " if c != stored[i]]))
    return stored[:i] + new + stored[i + 1 :]


@st.composite
def _absolute_paths(draw: st.DrawFn) -> str:
    """POSIX and Windows absolute paths and s3:// URIs ending in a markdown file name."""
    parts = draw(st.lists(_PATH_SEGMENT, min_size=1, max_size=4))
    style = draw(st.sampled_from(["posix", "windows", "unc", "s3"]))
    if style == "posix":
        return "/" + "/".join(parts) + ".md"
    if style == "windows":
        return draw(st.sampled_from("CDZ")) + ":\\" + "\\".join(parts) + ".md"
    if style == "unc":
        return "\\\\server\\" + "\\".join(parts) + ".md"
    return "s3://" + "/".join(parts) + "/doc.md"


# Structured strings never overlap the fixed message, so the non-echo check is meaningful.
_STRUCTURED_UNKNOWN_IDS = st.one_of(
    _near_miss_doc_ids(),
    _absolute_paths(),
    st.just(_CATALOG_UUID),
    st.just(_CATALOG_UUID.upper()),
    st.uuids().map(str),
    st.integers(min_value=1_000, max_value=100_000).map(lambda n: "x" * n),
)
_UNKNOWN_STR_IDS = st.one_of(_STRUCTURED_UNKNOWN_IDS, st.text(max_size=80)).filter(
    lambda value: value not in _STORED_DOC_IDS
)
_NON_STR_IDS = st.one_of(
    st.none(),
    st.integers(),
    st.lists(st.sampled_from(sorted(_STORED_DOC_IDS)), max_size=2),
)


def _ctx() -> AsyncMock:
    """MCP context double; its logging methods are awaitable like the real Context."""
    context = AsyncMock()
    context.session = Mock()
    context.session.client_params = {}
    return context


class TestUnknownDocIdFixedError:
    """Feature: search-fetch-pairs, Property 15: Unknown Doc_IDs get one fixed, non-echoing
    error.

    **Validates: Requirements 4.5, 12.5, 12.6**
    """

    def test_stored_doc_ids_are_accepted(self) -> None:
        """Guard against a vacuous property: the stored Doc_IDs and the catalog exist."""
        with doc_store(_UNKNOWN_ID_DOCS) as stored:
            for key in _DOC_KEYS:
                page = get_oscal_documentation(stored.ids[key])
                assert page["docId"] == stored.ids[key]
            catalogs = stored.store.list_documents(oscal_model_type=OSCALModelType.CATALOG)
            assert _CATALOG_UUID in json.dumps(catalogs["items"])
            assert stored.store.get_documentation(_CATALOG_UUID) is None

    @_PROPERTY_SETTINGS
    @given(value=st.one_of(_UNKNOWN_STR_IDS, _NON_STR_IDS))
    @example(value=_CATALOG_UUID)
    @example(value="/Users/someone/docs/guide.md")
    @example(value="C:\\Users\\someone\\docs\\guide.md")
    @example(value="s3://leak-bucket/oscal/doc.md")
    @example(value=None)
    @example(value=42)
    @example(value=[next(iter(_STORED_DOC_IDS))])
    @example(value=" " + next(iter(_STORED_DOC_IDS)))
    @example(value="")
    def test_unknown_doc_id_gets_fixed_message(self, value: object) -> None:
        ctx = _ctx()
        with doc_store(_UNKNOWN_ID_DOCS) as stored:
            real_lookup = stored.store.get_documentation
            found: list[object] = []

            def recording_lookup(key: str) -> object:
                result = real_lookup(key)
                found.append(result)
                return result

            with (
                patch.object(
                    stored.store, "get_documentation", side_effect=recording_lookup
                ) as spy,
                pytest.raises(ValueError) as raised,
            ):
                get_oscal_documentation(value, ctx=ctx)  # type: ignore[arg-type]

        message = str(raised.value)
        assert message == query_documentation.UNKNOWN_DOC_ID_MESSAGE
        assert "query_oscal_documentation" in message
        if isinstance(value, str) and len(value.strip()) > 3:
            # The fixed text cannot echo the value; a generated value that is already part
            # of the fixed text (e.g. "Unknown") is a coincidence, not an echo.
            assert value not in message or value in query_documentation.UNKNOWN_DOC_ID_MESSAGE

        # The same fixed text is the only Client_Facing_Message.
        assert ctx.error.await_args_list == [call(query_documentation.UNKNOWN_DOC_ID_MESSAGE)]
        for level in ("warning", "info", "debug"):
            getattr(ctx, level).assert_not_awaited()

        # No document text is read: the lookup is skipped for non-str values and finds
        # nothing for unknown strings.
        if isinstance(value, str):
            spy.assert_called_once_with(value)
            assert found == [None]
        else:
            spy.assert_not_called()
        event(type(value).__name__)

    @_PROPERTY_SETTINGS
    @given(value=_STRUCTURED_UNKNOWN_IDS)
    def test_structured_unknown_ids_are_not_echoed(self, value: str) -> None:
        """Paths, URIs, UUIDs, and near-miss Doc_IDs never appear in the error text."""
        with doc_store(_UNKNOWN_ID_DOCS), pytest.raises(ValueError) as raised:
            get_oscal_documentation(value)
        message = str(raised.value)
        assert message == query_documentation.UNKNOWN_DOC_ID_MESSAGE
        assert value not in message
        assert value.strip() not in message


# ------------------------------------------------------------------
# Property 16
# ------------------------------------------------------------------

_PAGING_DOC_KEY: tuple[Origin, str] = (ORIGIN_USER, "paging/guide.md")
_SEARCH_DOCS: Mapping[tuple[Origin, str], str] = {
    (ORIGIN_BUNDLED, "search.md"): "# Search Guide\n\npagingtoken text\n",
    _PAGING_DOC_KEY: "# Local Paging\n\npagingtoken notes\n",
}
_SEARCH_MODES = ("local", "kb")

_BAD_OFFSETS = st.integers(max_value=-1)
_BAD_LIMITS = st.one_of(st.integers(max_value=0), st.integers(min_value=101))
_VALID_OFFSETS = st.integers(min_value=0, max_value=1_000)
_VALID_LIMITS = st.integers(min_value=1, max_value=100)

# Non-int values a JSON client can send. None of them contains a path separator, so a
# separator in a message could only come from the server.
_NON_INTS = st.one_of(
    st.booleans(),
    st.none(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.integers(min_value=-5, max_value=600).map(str),
    st.lists(st.integers(min_value=1, max_value=5), max_size=2),
)
_BAD_LINE_COUNTS = st.one_of(
    st.integers(max_value=0), st.integers(min_value=MAX_LINE_COUNT + 1), _NON_INTS
)


def _paging_text(total: int) -> str:
    """A markdown document of exactly ``total`` lines."""
    return "# Paging Doc\n" + "".join(f"line {i}\n" for i in range(2, total + 1))


@contextmanager
def _search_mode(mode: str) -> Iterator[Mock]:
    """Local_Mode (blank KB ID) or KB_Mode with a patched boto3 ``Session``; yields it."""
    fake_config = Mock()
    fake_config.knowledge_base_id = "" if mode == "local" else "KBTEST1234"
    fake_config.aws_profile = None
    with (
        patch.object(query_documentation, "config", fake_config),
        patch.object(query_documentation, "Session") as session,
    ):
        yield session


def _assert_no_path(message: str) -> None:
    """The message holds no filesystem path: no separator and no temp directory."""
    assert "/" not in message
    assert "\\" not in message
    assert tempfile.gettempdir() not in message


class TestOutOfRangePagingArguments:
    """Feature: search-fetch-pairs, Property 16: Out-of-range paging arguments name the
    parameter and range.

    **Validates: Requirements 10.6, 12.7**
    """

    # -- query_oscal_documentation ------------------------------------------------

    @_PROPERTY_SETTINGS
    @given(
        mode=st.sampled_from(_SEARCH_MODES),
        page=st.one_of(
            st.tuples(st.just("offset"), _BAD_OFFSETS, _VALID_LIMITS),
            st.tuples(st.just("limit"), _VALID_OFFSETS, _BAD_LIMITS),
        ),
    )
    @example(mode="local", page=("offset", -1, 10))
    @example(mode="kb", page=("limit", 0, 0))
    @example(mode="local", page=("limit", 0, 101))
    def test_search_rejects_out_of_range_page(self, mode: str, page: tuple[str, int, int]) -> None:
        bad, off, lim = page
        ctx = _ctx()
        with doc_store(_SEARCH_DOCS) as stored:
            real_search = stored.store.search_documentation
            with (
                _search_mode(mode) as session,
                patch.object(stored.store, "search_documentation", wraps=real_search) as search_spy,
                pytest.raises(ValueError) as raised,
            ):
                query_documentation.query_oscal_documentation(
                    "pagingtoken", offset=off, limit=lim, ctx=ctx
                )
            session.assert_not_called()
            search_spy.assert_not_called()

        message = str(raised.value)
        if bad == "offset":
            assert message == f"offset must be non-negative, got {off}"
        else:
            assert message == f"limit must be between 1 and 100, got {lim}"
        _assert_no_path(message)
        assert ctx.error.await_args_list == [call(message)]
        event(f"{mode}: bad {bad}")

    @pytest.mark.parametrize("mode", _SEARCH_MODES)
    @pytest.mark.parametrize(("offset", "limit"), [(0, 1), (0, 100), (1, 1), (5, 100)])
    def test_search_accepts_boundary_page(self, mode: str, offset: int, limit: int) -> None:
        ctx = _ctx()
        with doc_store(_SEARCH_DOCS), _search_mode(mode) as session:
            retrieve = session.return_value.client.return_value.retrieve
            retrieve.return_value = {"retrievalResults": []}
            page = query_documentation.query_oscal_documentation(
                "pagingtoken", offset=offset, limit=limit, ctx=ctx
            )
        # A budgeted page reports the number of items it holds as its limit.
        assert page["offset"] == offset
        assert page["limit"] == len(page["items"]) <= limit
        assert page["total"] >= 1
        ctx.error.assert_not_awaited()

    # -- get_oscal_documentation --------------------------------------------------

    @_PROPERTY_SETTINGS
    @given(
        total=st.integers(min_value=1, max_value=40),
        line_count=_BAD_LINE_COUNTS,
        start_line=st.one_of(st.integers(min_value=-5, max_value=60), _NON_INTS),
    )
    @example(total=3, line_count=0, start_line=1)
    @example(total=3, line_count=MAX_LINE_COUNT + 1, start_line=1)
    @example(total=3, line_count=True, start_line=1)
    @example(total=3, line_count=0, start_line=99)
    def test_fetch_rejects_bad_line_count_first(
        self, total: int, line_count: object, start_line: object
    ) -> None:
        """A bad ``line_count`` is reported, even when ``start_line`` is also bad."""
        ctx = _ctx()
        with doc_store({_PAGING_DOC_KEY: _paging_text(total)}) as stored:
            target = stored.ids[_PAGING_DOC_KEY]
            with pytest.raises(ValueError) as raised:
                get_oscal_documentation(
                    target,
                    start_line=start_line,  # type: ignore[arg-type]
                    line_count=line_count,  # type: ignore[arg-type]
                    ctx=ctx,
                )
        message = str(raised.value)
        assert message.startswith("Invalid line_count: ")
        assert message.endswith(f"Valid range: 1 to {MAX_LINE_COUNT}")
        assert "start_line" not in message
        _assert_no_path(message)
        assert ctx.error.await_args_list == [call(message)]
        start_ok = type(start_line) is int and 1 <= start_line <= total
        event("start_line also bad" if not start_ok else "start_line valid")
        event(type(line_count).__name__)

    @_PROPERTY_SETTINGS
    @given(
        data=st.data(),
        total=st.integers(min_value=1, max_value=40),
        line_count=st.integers(min_value=1, max_value=MAX_LINE_COUNT),
    )
    def test_fetch_rejects_bad_start_line(
        self, data: st.DataObject, total: int, line_count: int
    ) -> None:
        start_line = data.draw(
            st.one_of(
                st.integers(max_value=0),
                st.integers(min_value=total + 1),
                st.just(total + 1),
                _NON_INTS,
            ),
            label="start_line",
        )
        ctx = _ctx()
        with doc_store({_PAGING_DOC_KEY: _paging_text(total)}) as stored:
            target = stored.ids[_PAGING_DOC_KEY]
            stored_doc = stored.store.get_documentation(target)
            assert stored_doc is not None
            assert len(stored_doc.lines) == total
            with pytest.raises(ValueError) as raised:
                get_oscal_documentation(
                    target,
                    start_line=start_line,  # type: ignore[arg-type]
                    line_count=line_count,
                    ctx=ctx,
                )
        message = str(raised.value)
        assert message.startswith("Invalid start_line: ")
        assert message.endswith(f"Valid range for this document: 1 to {total}")
        assert "line_count" not in message
        _assert_no_path(message)
        assert ctx.error.await_args_list == [call(message)]
        event(type(start_line).__name__)

    @pytest.mark.parametrize("total", [1, 7])
    @pytest.mark.parametrize("line_count", [1, MAX_LINE_COUNT])
    def test_fetch_accepts_boundary_values(self, total: int, line_count: int) -> None:
        ctx = _ctx()
        with doc_store({_PAGING_DOC_KEY: _paging_text(total)}) as stored:
            target = stored.ids[_PAGING_DOC_KEY]
            for start_line in (1, total):
                page = get_oscal_documentation(
                    target, start_line=start_line, line_count=line_count, ctx=ctx
                )
                assert page["start-line"] == start_line
                assert page["total-lines"] == total
                assert page["end-line"] == min(start_line + line_count - 1, total)
        ctx.error.assert_not_awaited()


# ------------------------------------------------------------------
# Unit tests
# ------------------------------------------------------------------

_RESPONSE_KEY_ORDER = [
    "docId",
    "title",
    "start-line",
    "end-line",
    "total-lines",
    "has-more",
    "text",
]


class TestGetOscalDocumentationUnit:
    """Unit tests for ``get_oscal_documentation`` (task 12.5).

    Complements the minimal checks in ``test_query_documentation.py::TestGetOscalDocumentation``
    with exact responses, paging, the OSCAL Document_UUID case, and the MCP schema.

    **Validates: Requirements 12.3, 12.5, 12.6**
    """

    @pytest.mark.parametrize(
        ("key", "title", "text"),
        [
            (_DOC_KEYS[0], "Bundled Guide", "# Bundled Guide\n\nbundled text\n"),
            (_DOC_KEYS[1], "Local Notes", "# Local Notes\n\nuser text\n"),
        ],
        ids=["bundled", "user"],
    )
    def test_exact_response_for_each_origin(
        self, key: tuple[Origin, str], title: str, text: str
    ) -> None:
        """Requirement 12.3: exact dict, key order, docId echoed, title from the heading."""
        with doc_store(_UNKNOWN_ID_DOCS) as stored:
            requested = stored.ids[key]
            result = get_oscal_documentation(requested)
        assert result == {
            "docId": requested,
            "title": title,
            "start-line": 1,
            "end-line": 3,
            "total-lines": 3,
            "has-more": False,
            "text": text,
        }
        assert list(result) == _RESPONSE_KEY_ORDER
        assert result["docId"] == doc_id(*key)

    def test_second_page_via_start_line(self) -> None:
        """Requirement 12.3: a later page holds exactly the requested verbatim lines."""
        text = _paging_text(5)
        with doc_store({_PAGING_DOC_KEY: text}) as stored:
            target = stored.ids[_PAGING_DOC_KEY]
            first = get_oscal_documentation(target, start_line=1, line_count=2)
            second = get_oscal_documentation(target, start_line=first["end-line"] + 1, line_count=2)
            last = get_oscal_documentation(target, start_line=5, line_count=2)
        lines = text.splitlines(keepends=True)
        assert first["text"] == "".join(lines[0:2])
        assert first["has-more"] is True
        assert second == {
            "docId": target,
            "title": "Paging Doc",
            "start-line": 3,
            "end-line": 4,
            "total-lines": 5,
            "has-more": True,
            "text": "line 3\nline 4\n",
        }
        assert list(second) == _RESPONSE_KEY_ORDER
        assert last["start-line"] == last["end-line"] == 5
        assert last["has-more"] is False
        assert last["text"] == "line 5\n"

    def test_oscal_document_uuid_gets_unknown_id_error(self) -> None:
        """Requirement 12.6: a stored catalog's UUID is not a Doc_ID."""
        ctx = _ctx()
        with doc_store(_UNKNOWN_ID_DOCS), pytest.raises(ValueError) as raised:
            get_oscal_documentation(_CATALOG_UUID, ctx=ctx)
        assert str(raised.value) == query_documentation.UNKNOWN_DOC_ID_MESSAGE
        assert _CATALOG_UUID not in str(raised.value)
        assert ctx.error.await_args_list == [call(query_documentation.UNKNOWN_DOC_ID_MESSAGE)]

    def test_unknown_id_message_text(self) -> None:
        """Requirement 12.5: states the ID is unknown and names the Doc_ID source tool."""
        assert query_documentation.UNKNOWN_DOC_ID_MESSAGE == (
            "Unknown documentation ID. Use a docId from a query_oscal_documentation result."
        )

    def test_store_not_initialized_message_has_no_path(self) -> None:
        saved = query_documentation._store
        query_documentation._store = None
        try:
            with pytest.raises(RuntimeError) as raised:
                get_oscal_documentation(doc_id(ORIGIN_BUNDLED, "guide.md"))
        finally:
            query_documentation._store = saved
        message = str(raised.value)
        assert message == (
            "OscalStore has not been initialised. Call init_store() during server startup."
        )
        _assert_no_path(message)

    def test_mcp_server_schema(self) -> None:
        """Requirement 12.2: doc_id required; shared StartLine/LineCount docs; no ctx."""
        server = MCPServer("test")  # fresh instance, never main.mcp
        server.add_tool(get_oscal_documentation)
        (mcp_tool,) = asyncio.run(server.list_tools())
        assert mcp_tool.name == "get_oscal_documentation"
        schema = mcp_tool.input_schema
        props = schema["properties"]
        assert schema["required"] == ["doc_id"]
        assert set(props) == {"doc_id", "start_line", "line_count"}
        assert props["doc_id"]["description"] == "docId from a query_oscal_documentation result."
        assert props["start_line"]["default"] == 1
        assert props["line_count"]["default"] == 200
        assert props["start_line"]["description"] == START_LINE_DOC
        assert props["line_count"]["description"] == LINE_COUNT_DOC
        assert "ctx" not in props
