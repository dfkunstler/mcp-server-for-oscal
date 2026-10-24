"""Tests for the get_metaschema_source tool (Feature: metaschema-constraints).

Smoke tests (task 18.1), Properties 21 and 22 (tasks 18.3, 18.4), and example tests for
listing mode, the signature, and range errors (task 18.5).
"""

import asyncio
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from mcp.server.mcpserver import Context, MCPServer

from mcp_server_for_oscal.tools import get_metaschema_source as gms_module
from mcp_server_for_oscal.tools.get_metaschema_source import MAX_LINE_COUNT, get_metaschema_source
from mcp_server_for_oscal.tools.metaschema_index import (
    BUNDLE_DIR,
    MetaschemaIndex,
    MetaschemaLoadError,
    get_index,
)
from mcp_server_for_oscal.tools.utils import RESPONSE_BUDGET, TRUNCATION_MARK

from .test_get_allowed_values import _signature_rows
from .test_metaschema_index import CAT, ENT, _module, _write_bundle

MODULE = "oscal_component_metaschema.xml"
ENT_FILE = "shared-constraints/allowed-values-component-type.ent"


def _bundle_lines(rel: str) -> list[str]:
    """The bundle file's lines, endings kept, read straight from disk for comparison."""
    return (BUNDLE_DIR / rel).read_bytes().decode("utf-8").splitlines(keepends=True)


def test_listing_mode_returns_all_sources_sorted_with_line_counts():
    result = get_metaschema_source(start_line=-5, line_count=0)  # ignored in listing mode
    files = result["files"]
    assert len(files) == 26
    names = [f["file"] for f in files]
    assert names == sorted(names)
    assert names == list(get_index().source_files())
    assert ENT_FILE in names
    for entry in files:
        assert set(entry) == {"file", "line-count"}
        assert entry["line-count"] == len(_bundle_lines(entry["file"]))
    assert len(json.dumps(result)) <= RESPONSE_BUDGET


@pytest.mark.parametrize("rel", [MODULE, ENT_FILE])
def test_reads_range_verbatim(rel):
    expected = _bundle_lines(rel)
    result = get_metaschema_source(file=rel, start_line=3, line_count=10)
    assert result == {
        "file": rel,
        "start-line": 3,
        "end-line": min(12, len(expected)),
        "total-lines": len(expected),
        "has-more": len(expected) > 12,
        "text": "".join(expected[2:12]),
    }


def test_range_is_clamped_at_end_of_file():
    total = len(_bundle_lines(MODULE))
    result = get_metaschema_source(file=MODULE, start_line=total, line_count=500)
    assert result["end-line"] == total
    assert result["has-more"] is False
    assert result["text"] == _bundle_lines(MODULE)[-1]


def test_large_range_is_cut_by_whole_lines_within_budget():
    lines = _bundle_lines("oscal_implementation-common_metaschema.xml")
    result = get_metaschema_source(
        file="oscal_implementation-common_metaschema.xml", start_line=1, line_count=500
    )
    assert len(json.dumps(result)) <= RESPONSE_BUDGET
    end = result["end-line"]
    assert end < 500
    assert result["has-more"] is True
    assert result["text"] == "".join(lines[:end])
    # One more line would not fit.
    bigger = dict(result, text="".join(lines[: end + 1]), **{"end-line": end + 1})
    assert len(json.dumps(bigger)) > RESPONSE_BUDGET


@pytest.mark.parametrize(
    "bad",
    [
        "hashes.json",
        "../hashes.json",
        "shared-constraints/hashes.json",
        str(BUNDLE_DIR / MODULE),
        "/etc/passwd",
        "OSCAL_COMPONENT_METASCHEMA.XML",
        "./" + MODULE,
        "shared-constraints\\allowed-values-component-type.ent",
        "https://example.com/" + MODULE,
        "",
    ],
)
def test_file_outside_allowlist_lists_available_files_without_reading(bad):
    index = MetaschemaIndex.load()
    with (
        patch.object(gms_module, "get_index", return_value=index),
        patch.object(MetaschemaIndex, "_read_lines") as read_spy,
        patch.object(gms_module, "try_notify_client_error") as notify,
        pytest.raises(ValueError, match="Available files: ") as exc,
    ):
        get_metaschema_source(file=bad)
    read_spy.assert_not_called()
    notify.assert_called_once()
    msg = str(exc.value)
    for rel in index.source_files():
        assert rel in msg
    assert str(BUNDLE_DIR) not in msg
    assert str(Path.home()) not in msg


def test_rejected_file_is_not_echoed():
    with pytest.raises(ValueError, match="Available files: ") as exc:
        get_metaschema_source(file="secret-" + "x" * 1000)
    assert "secret-" not in str(exc.value)


@pytest.mark.parametrize("line_count", [0, -1, 501, True, "10"])
def test_line_count_out_of_range(line_count):
    with (
        patch.object(gms_module, "try_notify_client_error") as notify,
        pytest.raises(ValueError, match="Valid range: 1 to 500"),
    ):
        get_metaschema_source(file=MODULE, line_count=line_count)
    notify.assert_called_once()


def test_start_line_out_of_range():
    total = len(_bundle_lines(MODULE))
    for start_line in (0, -3, total + 1, None):
        with (
            patch.object(gms_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match=f"Valid range for this file: 1 to {total}$"),
        ):
            get_metaschema_source(file=MODULE, start_line=start_line)
        notify.assert_called_once()


def test_single_over_budget_line_is_truncated():
    """Unreachable with the bundled sources (longest line < 1,000 chars); keeps paging alive."""
    long_line = "x" * (RESPONSE_BUDGET + 100) + "\n"
    lines = ("first\n", long_line, "last\n")
    result = gms_module._within_budget("f.xml", lines, 2, 2)
    assert result["start-line"] == result["end-line"] == 2
    assert result["has-more"] is True
    assert result["truncated"] is True
    assert result["text"].endswith("…")
    assert len(json.dumps(result)) <= RESPONSE_BUDGET


# --------------------------------------------------------------------------------------
# Property 21: get_metaschema_source pages reconstruct the file (task 18.3)
# --------------------------------------------------------------------------------------


def _oracle_lines(data: bytes) -> list[str]:
    """Lines split on LF, CRLF, and CR with endings kept, independently of the tool.

    ``io.StringIO(newline="")`` ends lines exactly where expat does and translates nothing,
    unlike ``str.splitlines`` (which also splits on form feed, U+2028, ...).
    """
    return io.StringIO(data.decode("utf-8"), newline="").readlines()


def _check_page(page: dict, file: str, lines: list[str], start: int, line_count: int) -> None:
    """Per-response invariants of Requirements 11.2 and 11.4."""
    total = len(lines)
    end = page["end-line"]
    assert len(json.dumps(page)) <= RESPONSE_BUDGET
    assert page["file"] == file
    assert page["start-line"] == start
    assert page["total-lines"] == total
    assert start <= end <= total
    assert end - start + 1 <= line_count
    assert page["has-more"] is (end < total)
    if page.get("truncated"):
        # Over-budget single line: shortened, marked, and still advances by one line.
        event("over-budget line truncated")
        assert end == start
        assert page["text"].endswith(TRUNCATION_MARK)
        assert lines[start - 1].startswith(page["text"].removesuffix(TRUNCATION_MARK))
        assert len(json.dumps(dict(page, text=lines[start - 1]))) > RESPONSE_BUDGET
        return
    assert "truncated" not in page
    assert page["text"] == "".join(lines[start - 1 : end])
    if end < min(start + line_count - 1, total):
        # Cut for the budget: the largest whole-line range, so one more line would not fit.
        event("page cut for budget")
        bigger = dict(
            page,
            text=page["text"] + lines[end],
            **{"end-line": end + 1, "has-more": end + 1 < total},
        )
        assert len(json.dumps(bigger)) > RESPONSE_BUDGET


def _walk_pages(file: str, lines: list[str], start: int, line_count: int) -> list[dict]:
    """Follow ``has-more`` from ``start`` (next start = end-line + 1), checking each page."""
    pages: list[dict] = []
    next_start = start
    while True:
        page = get_metaschema_source(file=file, start_line=next_start, line_count=line_count)
        _check_page(page, file, lines, next_start, line_count)
        pages.append(page)
        if not page["has-more"]:
            return pages
        next_start = page["end-line"] + 1
        assert len(pages) <= len(lines)  # every page advances at least one line


# A synthetic module with one line over the response budget, which no bundled file has.
_LONG_LINE = "<remarks>" + "y" * (RESPONSE_BUDGET + 500) + "</remarks>\n"
_SYNTHETIC_FILES: dict[str, bytes] = {
    CAT: _module("<!-- a -->\n<!-- b -->\r\n" + _LONG_LINE + "<!-- c -->\r<!-- d -->"),
    ENT: b"<!-- e -->\n" * 3,
}


@pytest.fixture(scope="module")
def synthetic_index(tmp_path_factory) -> MetaschemaIndex:
    root = tmp_path_factory.mktemp("bundle")
    files: dict[str, str | bytes] = dict(_SYNTHETIC_FILES)
    _write_bundle(root, files)
    return MetaschemaIndex.load(root)


class TestPageReconstructionProperty:
    """Property 21 (Feature: metaschema-constraints).

    **Validates: Requirements 11.2, 11.4**
    """

    @given(data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_pages_reconstruct_bundled_file(self, data):
        file = data.draw(st.sampled_from(get_index().source_files()), label="file")
        raw = (BUNDLE_DIR / file).read_bytes()
        lines = _oracle_lines(raw)
        start = data.draw(st.integers(1, len(lines)), label="start_line")
        line_count = data.draw(st.integers(1, MAX_LINE_COUNT), label="line_count")

        pages = _walk_pages(file, lines, start, line_count)

        assert not any(p.get("truncated") for p in pages)  # no bundled line is over budget
        assert pages[-1]["end-line"] == len(lines)
        text = "".join(p["text"] for p in pages)
        assert text == "".join(lines[start - 1 :])
        if start == 1:
            assert text.encode("utf-8") == raw  # byte-exact, line endings included

    @given(data=st.data())
    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_over_budget_line_is_truncated_and_paging_progresses(self, data, synthetic_index):
        assert set(synthetic_index.source_files()) == set(_SYNTHETIC_FILES)
        file = data.draw(st.sampled_from(sorted(_SYNTHETIC_FILES)), label="file")
        raw = _SYNTHETIC_FILES[file]
        lines = _oracle_lines(raw)
        long_no = lines.index(_LONG_LINE) + 1 if _LONG_LINE in lines else None
        start = data.draw(st.integers(1, len(lines)), label="start_line")
        line_count = data.draw(st.integers(1, MAX_LINE_COUNT), label="line_count")

        with patch.object(gms_module, "get_index", return_value=synthetic_index):
            pages = _walk_pages(file, lines, start, line_count)

        truncated = [p["start-line"] for p in pages if p.get("truncated")]
        assert truncated == ([long_no] if long_no is not None and start <= long_no else [])
        assert pages[-1]["end-line"] == len(lines)
        # Substituting the full long line for its truncated page reconstructs the rest exactly.
        text = "".join(
            lines[p["start-line"] - 1] if p.get("truncated") else p["text"] for p in pages
        )
        assert text == "".join(lines[start - 1 :])
        if start == 1:
            assert text.encode("utf-8") == raw


# --------------------------------------------------------------------------------------
# Property 22: get_metaschema_source rejects everything outside the allowlist (task 18.4)
# --------------------------------------------------------------------------------------

_SOURCES: tuple[str, ...] = get_index().source_files()

# Files in the bundle (or a manifest) that are not Metaschema_Sources.
_NOT_SOURCES = ("hashes.json", "shared-constraints/hashes.json", "README.md")

# Visually similar code points for characters that occur in every source file name.
_LOOKALIKES = {
    "o": ("\u043e", "\u03bf", "\u0585"),  # Cyrillic o, Greek omicron, Armenian oh
    "a": ("\u0430", "\u0251"),  # Cyrillic a, Latin alpha
    "e": ("\u0435",),  # Cyrillic ie
    ".": ("\uff0e", "\u2024"),  # fullwidth full stop, one dot leader
    "_": ("\uff3f",),  # fullwidth low line
    "-": ("\u2010", "\u2011", "\u2212"),  # hyphen, non-breaking hyphen, minus sign
    "/": ("\u2215", "\uff0f"),  # division slash, fullwidth solidus
}


@st.composite
def _case_variant(draw, name: str) -> str:
    flips = draw(st.lists(st.booleans(), min_size=len(name), max_size=len(name)))
    return "".join(c.swapcase() if f else c for c, f in zip(name, flips, strict=True))


@st.composite
def _percent_encoded(draw, name: str) -> str:
    i = draw(st.integers(0, len(name) - 1))
    encoded = "".join(f"%{b:02X}" for b in name[i].encode("utf-8"))
    return name[:i] + encoded + name[i + 1 :]


@st.composite
def _lookalike(draw, name: str) -> str:
    positions = [i for i, c in enumerate(name) if c in _LOOKALIKES]
    i = draw(st.sampled_from(positions))
    return name[:i] + draw(st.sampled_from(_LOOKALIKES[name[i]])) + name[i + 1 :]


def _near_misses(name: str) -> st.SearchStrategy[str]:
    """Variants of a real source (or non-source bundle file) name that are not exact."""
    abs_path = BUNDLE_DIR / name
    return st.one_of(
        _case_variant(name),
        st.sampled_from(("./", "../", "/", "//", "./shared-constraints/../", "metaschema/")).map(
            lambda p: p + name
        ),
        st.just(name.replace("/", "\\")),
        st.just("\\" + name),
        st.sampled_from((" ", "\t", "\n", "/", "\\", "\x00", "#", "?x=1", ".bak")).map(
            lambda s: name + s
        ),
        st.sampled_from((" ", "\t", "\ufeff", "\u200b")).map(lambda s: s + name),
        _percent_encoded(name),
        _lookalike(name),
        st.just(str(abs_path)),
        st.just(abs_path.as_uri()),
        st.just("file://" + str(abs_path)),
        st.just("https://example.com/" + name),
    )


_rejected_files = st.one_of(
    st.sampled_from(_SOURCES + _NOT_SOURCES).flatmap(_near_misses),
    st.sampled_from(_NOT_SOURCES),
    st.text(max_size=60),
    st.just(""),
).filter(lambda f: f not in _SOURCES)

# The fixed rejection message: identical for every rejected value, so nothing is echoed.
_REJECT_MSG = (
    "Unknown OSCAL Metaschema source file. Use a bundle-relative path exactly as listed. "
    f"Available files: {', '.join(_SOURCES)}"
)

# Invalid non-int values shared by the start_line and line_count strategies.
_non_ints = st.one_of(
    st.booleans(),
    st.none(),
    st.floats(allow_nan=True),
    st.text(max_size=10),
    st.integers().map(str),
    st.lists(st.integers(), max_size=2),
)


@pytest.fixture(scope="module")
def fresh_index() -> MetaschemaIndex:
    """A fresh index whose manifests and modules are loaded but whose line cache is empty.

    Loaded before any ``open`` spy is installed, so every ``open`` the spy records happened
    inside the tool call under test.
    """
    return MetaschemaIndex.load()


class _OpenSpy:
    """Records the path of every ``open``/``io.open`` call and delegates to the real one."""

    def __init__(self) -> None:
        self.paths: list[Path] = []
        self._real = io.open

    def __call__(self, file, *args, **kwargs):
        self.paths.append(Path(file))
        return self._real(file, *args, **kwargs)


def _call_spied(index: MetaschemaIndex, **kwargs) -> tuple[ValueError, _OpenSpy, str]:
    """Call the tool on ``index`` with ``open`` spied; return the error, spy, and notice."""
    spy = _OpenSpy()
    with (
        patch.object(gms_module, "get_index", return_value=index),
        patch("builtins.open", spy),
        patch("io.open", spy),
        patch.object(gms_module, "try_notify_client_error") as notify,
        pytest.raises(ValueError) as exc,
    ):
        get_metaschema_source(**kwargs)
    notify.assert_called_once()
    notice = notify.call_args.args[0]
    assert notice == str(exc.value)
    return exc.value, spy, notice


def _assert_client_safe(msg: str) -> None:
    assert str(BUNDLE_DIR) not in msg
    assert str(Path.home()) not in msg
    assert BUNDLE_DIR.as_uri() not in msg


class TestAllowlistRejectionProperty:
    """Property 22 (Feature: metaschema-constraints).

    **Validates: Requirements 11.3, 11.6, 11.7**
    """

    @given(file=_rejected_files, start_line=st.integers(), line_count=st.integers())
    @settings(max_examples=200, deadline=None)
    def test_file_outside_allowlist_is_rejected_without_opening(
        self, fresh_index, file, start_line, line_count
    ):
        event("contains a source name" if any(s in file for s in _SOURCES) else "other")
        err, spy, _ = _call_spied(
            fresh_index, file=file, start_line=start_line, line_count=line_count
        )
        assert spy.paths == []
        msg = str(err)
        # Same message for every rejected value: lists the files and echoes nothing.
        assert msg == _REJECT_MSG
        for rel in _SOURCES:
            assert rel in msg
        _assert_client_safe(msg)

    @given(
        file=st.sampled_from(_SOURCES),
        start_line=st.one_of(st.integers(), _non_ints),
        line_count=st.one_of(
            st.integers(max_value=0), st.integers(min_value=MAX_LINE_COUNT + 1), _non_ints
        ),
    )
    @settings(max_examples=200, deadline=None)
    def test_invalid_line_count_is_rejected_before_reading(
        self, fresh_index, file, start_line, line_count
    ):
        err, spy, _ = _call_spied(
            fresh_index, file=file, start_line=start_line, line_count=line_count
        )
        assert spy.paths == []
        assert fresh_index._lines == {}  # nothing read, so nothing cached
        msg = str(err)
        assert msg.startswith("Invalid line_count: ")
        assert msg.endswith(f"Valid range: 1 to {MAX_LINE_COUNT}")
        _assert_client_safe(msg)

    @given(data=st.data())
    @settings(max_examples=100, deadline=None)  # each example loads a fresh index (~35 ms)
    def test_invalid_start_line_states_valid_range(self, data):
        # A fresh index per example, so every example exercises the uncached read.
        index = MetaschemaIndex.load()
        file = data.draw(st.sampled_from(_SOURCES), label="file")
        total = len(_oracle_lines((BUNDLE_DIR / file).read_bytes()))
        start_line = data.draw(
            st.one_of(st.integers(max_value=0), st.integers(min_value=total + 1), _non_ints),
            label="start_line",
        )
        line_count = data.draw(st.integers(1, MAX_LINE_COUNT), label="line_count")

        err, spy, _ = _call_spied(index, file=file, start_line=start_line, line_count=line_count)

        # Only the requested, allowlisted file is read, once, to learn its line count.
        assert [p.resolve() for p in spy.paths] == [(BUNDLE_DIR / file).resolve()]
        msg = str(err)
        assert msg.startswith("Invalid start_line: ")
        assert msg.endswith(f"Valid range for this file: 1 to {total}")
        _assert_client_safe(msg)


# --------------------------------------------------------------------------------------
# Example tests: listing mode, signature, and range errors (task 18.5)
# --------------------------------------------------------------------------------------


def _expect_error(index: MetaschemaIndex | None, exc_type: type[ValueError], **kwargs) -> str:
    """Call the tool, expect ``exc_type``, check one notice with the same message; return it."""
    with (
        patch.object(gms_module, "get_index", return_value=index or get_index()),
        patch.object(gms_module, "try_notify_client_error") as notify,
        pytest.raises(exc_type) as exc,
    ):
        get_metaschema_source(**kwargs)
    msg = str(exc.value)
    notify.assert_called_once()
    assert notify.call_args.args[0] == msg
    return msg


class TestRequiredExamples:
    """Validates: Requirements 11.1, 11.5, 11.6, 11.7"""

    def test_signature_and_defaults(self):
        """Req 11.1: file=None, start_line=1, line_count=200, ctx=None, with their types."""
        expected = [
            ("file", str | None, None),
            ("start_line", int, 1),
            ("line_count", int, 200),
            ("ctx", Context | None, None),
        ]
        assert _signature_rows(get_metaschema_source) == expected

    def test_mcp_input_schema_hides_ctx(self):
        """The MCP server injects ``ctx``; callers see three optional arguments."""
        server = MCPServer("test")
        server.add_tool(get_metaschema_source)
        (tool_def,) = asyncio.run(server.list_tools())
        schema = tool_def.input_schema
        assert list(schema["properties"]) == ["file", "start_line", "line_count"]
        assert schema.get("required", []) == []
        assert schema["properties"]["start_line"]["default"] == 1
        assert schema["properties"]["line_count"]["default"] == 200

    def test_listing_mode_shape_and_independent_line_counts(self):
        """Req 11.5: only ``files``; one entry per allowlisted file; counts match the files."""
        result = get_metaschema_source()
        assert list(result) == ["files"]
        expected = [
            {"file": rel, "line-count": len(_oracle_lines((BUNDLE_DIR / rel).read_bytes()))}
            for rel in sorted(get_index().source_files())
        ]
        assert result["files"] == expected

    @pytest.mark.parametrize(
        ("start_line", "line_count"),
        [(1, 200), (0, 0), (10**9, 501), (-1, -1), (None, None), ("x", 2.5)],
    )
    def test_listing_mode_ignores_start_line_and_line_count(self, start_line, line_count):
        """Req 11.5: range arguments, valid or not, do not change or fail listing mode."""
        with patch.object(gms_module, "try_notify_client_error") as notify:
            result = get_metaschema_source(start_line=start_line, line_count=line_count)
        notify.assert_not_called()
        assert result == get_metaschema_source()

    @pytest.mark.parametrize("rel", [MODULE, ENT_FILE])
    @pytest.mark.parametrize("line_count", [0, MAX_LINE_COUNT + 1])
    def test_line_count_boundary_rejected(self, rel, line_count):
        """Req 11.7: 0 and 501 are rejected with the exact range message."""
        msg = _expect_error(None, ValueError, file=rel, line_count=line_count)
        assert msg == f"Invalid line_count: {line_count}. Valid range: 1 to 500"

    @pytest.mark.parametrize("rel", [MODULE, ENT_FILE])
    @pytest.mark.parametrize("line_count", [1, MAX_LINE_COUNT])
    def test_line_count_boundary_accepted(self, rel, line_count):
        """Req 11.7: 1 and 500 are valid."""
        lines = _oracle_lines((BUNDLE_DIR / rel).read_bytes())
        with patch.object(gms_module, "try_notify_client_error") as notify:
            result = get_metaschema_source(file=rel, line_count=line_count)
        notify.assert_not_called()
        assert result["start-line"] == 1
        assert 1 <= result["end-line"] <= min(line_count, len(lines))

    @pytest.mark.parametrize("rel", [MODULE, ENT_FILE])
    def test_start_line_boundaries(self, rel):
        """Req 11.6: 0 and total+1 are rejected with the file's range; 1 and total work."""
        total = len(_oracle_lines((BUNDLE_DIR / rel).read_bytes()))
        for bad in (0, total + 1):
            msg = _expect_error(None, ValueError, file=rel, start_line=bad)
            assert msg == f"Invalid start_line: {bad}. Valid range for this file: 1 to {total}"
        for good in (1, total):
            with patch.object(gms_module, "try_notify_client_error") as notify:
                result = get_metaschema_source(file=rel, start_line=good, line_count=1)
            notify.assert_not_called()
            assert result["start-line"] == result["end-line"] == good
            assert result["has-more"] is (good < total)

    def test_unreadable_source_raises_client_safe_load_error(self, tmp_path: Path):
        """A listed file that becomes undecodable fails reads and listing with a safe error."""
        _write_bundle(tmp_path, dict(_SYNTHETIC_FILES))
        index = MetaschemaIndex.load(tmp_path)
        (tmp_path / ENT).write_bytes(b"\xff\xfe not utf-8\n")

        for kwargs in ({"file": ENT}, {}):
            msg = _expect_error(index, MetaschemaLoadError, **kwargs)
            assert msg.startswith(f"OSCAL Metaschema source {ENT} cannot be read: ")
            assert str(tmp_path) not in msg
            assert tmp_path.as_posix() not in msg
            _assert_client_safe(msg)

        # Other files in the same index still read.
        with patch.object(gms_module, "get_index", return_value=index):
            assert get_metaschema_source(file=CAT)["start-line"] == 1
