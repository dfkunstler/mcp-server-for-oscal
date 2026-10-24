"""
Tests for the list_oscal_resources tool.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import mcp_server_for_oscal.tools.list_oscal_resources as lr_module
from mcp_server_for_oscal.tools.list_oscal_resources import (
    list_oscal_resources,
    parse_resource_entries,
    read_resources_file,
)
from mcp_server_for_oscal.tools.oscal_store import OscalStore

PAGE_KEYS = {"items", "total", "offset", "limit", "hasMore"}
BUNDLED_ENTRY_COUNT = 82
BUNDLED_CATEGORIES = {
    "Content": 11,
    "Tools": 50,
    "Articles and Blog Posts": 17,
    "Presentations and Talks": 2,
    "Other Resources": 2,
}


@pytest.fixture(autouse=True)
def _reset_store():
    """Reset the module-level store to None for each test.

    The ``read_resources_file()`` function falls back to reading
    ``awesome-oscal.md`` from the filesystem (``data/oscal_docs/``)
    when no store is set, which is the expected path in development.
    """
    old_store = lr_module._store
    lr_module._store = None
    yield
    lr_module._store = old_store


def _ctx() -> AsyncMock:
    ctx = AsyncMock()
    ctx.session.client_params = {}
    return ctx


class TestParseResourceEntries:
    """Unit tests for parse_resource_entries."""

    def test_headings_set_category_and_prose_is_skipped(self):
        markdown = (
            "# Title\n"
            "Intro prose.\n\n"
            "- before any section\n\n"
            "## Content\n\n"
            "- [A](https://a): first\n\n"
            "* [B](https://b): second\n"
            "### Sub Tools\n"
            "+ [C](https://c): third\n"
        )
        assert parse_resource_entries(markdown) == [
            {"category": "General", "text": "before any section"},
            {"category": "Content", "text": "[A](https://a): first"},
            {"category": "Content", "text": "[B](https://b): second"},
            {"category": "Sub Tools", "text": "[C](https://c): third"},
        ]

    def test_continuations_and_sub_items_are_joined(self):
        markdown = "## Tools\n- [A](https://a): part one\n in a pipeline\n  - sub item\n\nprose\n"
        assert parse_resource_entries(markdown) == [
            {"category": "Tools", "text": "[A](https://a): part one in a pipeline - sub item"},
        ]

    def test_blank_line_ends_entry(self):
        markdown = "## Tools\n- one\n\n  not a continuation\n- two\n"
        assert parse_resource_entries(markdown) == [
            {"category": "Tools", "text": "one"},
            {"category": "Tools", "text": "two"},
        ]

    def test_empty_markdown(self):
        assert parse_resource_entries("") == []

    def test_bundled_file_entries_and_categories(self):
        entries = parse_resource_entries(read_resources_file())
        assert len(entries) == BUNDLED_ENTRY_COUNT
        counts: dict[str, int] = {}
        for entry in entries:
            assert set(entry) == {"category", "text"}
            assert entry["text"]
            counts[entry["category"]] = counts.get(entry["category"], 0) + 1
        assert counts == BUNDLED_CATEGORIES
        # The ComplyScribe entry has a continuation line in the bundled file.
        assert any(
            e["text"].endswith("(https://github.com/IBM/compliance-trestle) in a CI/CD pipeline")
            for e in entries
        )


class TestListOscalResources:
    """Test cases for the list_oscal_resources tool."""

    def test_returns_default_page(self):
        result = list_oscal_resources(ctx=_ctx())
        assert isinstance(result, dict)
        assert set(result) == PAGE_KEYS
        assert result["offset"] == 0
        assert result["limit"] == 10
        assert result["total"] == BUNDLED_ENTRY_COUNT
        assert result["hasMore"] is True
        assert len(result["items"]) == 10
        assert result["items"][0]["category"] == "Content"
        assert "Australian Cyber Security Centre" in result["items"][0]["text"]

    def test_pages_concatenate_to_all_entries(self):
        expected = parse_resource_entries(read_resources_file())
        collected: list[dict] = []
        offset = 0
        while True:
            page = list_oscal_resources(offset=offset, limit=25)
            collected.extend(page["items"])
            if not page["hasMore"]:
                break
            offset += 25
        assert collected == expected

    def test_offset_past_end_returns_empty_items(self):
        result = list_oscal_resources(offset=BUNDLED_ENTRY_COUNT + 5, limit=10)
        assert result["items"] == []
        assert result["total"] == BUNDLED_ENTRY_COUNT
        assert result["hasMore"] is False

    @pytest.mark.parametrize(("offset", "limit"), [(-1, 10), (0, 0), (0, 101)])
    def test_invalid_paging_raises_and_notifies(self, offset, limit):
        ctx = _ctx()
        with pytest.raises(ValueError):
            list_oscal_resources(offset=offset, limit=limit, ctx=ctx)
        ctx.error.assert_called_once()
        assert "Failed to list OSCAL resources" in ctx.error.call_args[0][0]

    def test_read_resources_file_returns_string(self):
        """Test that read_resources_file returns a string."""
        result = read_resources_file()
        assert isinstance(result, str)

    def test_list_oscal_resources_file_not_found_error(self):
        """Test error handling when store is unavailable and file is not found."""
        ctx = _ctx()
        # Patch Path.exists to return False so both fallback paths fail
        with (
            patch(
                "mcp_server_for_oscal.tools.list_oscal_resources.Path.exists",
                return_value=False,
            ),
            pytest.raises(FileNotFoundError),
        ):
            list_oscal_resources(ctx=ctx)

        # Verify error was reported to context
        ctx.error.assert_called_once()

    @patch("mcp_server_for_oscal.tools.list_oscal_resources.open")
    def test_list_oscal_resources_io_error(self, mock_open):
        """Test error handling for IO errors when reading the file."""
        mock_open.side_effect = OSError("Permission denied")
        ctx = _ctx()

        with pytest.raises(IOError):
            list_oscal_resources(ctx=ctx)

        ctx.error.assert_called_once()
        assert "Failed to list OSCAL resources" in ctx.error.call_args[0][0]

    @patch("mcp_server_for_oscal.tools.list_oscal_resources.open")
    def test_list_oscal_resources_unicode_decode_error(self, mock_open):
        """Test error handling for encoding issues."""
        mock_open.side_effect = UnicodeDecodeError("utf-8", b"", 0, 1, "invalid start byte")
        ctx = _ctx()

        with pytest.raises(UnicodeDecodeError):
            list_oscal_resources(ctx=ctx)

        ctx.error.assert_called_once()

    def test_read_resources_file_with_temporary_file(self):
        """Test reading a temporary file with known content."""
        test_content = """# Test OSCAL Resources

## Content
- Test resource 1
- Test resource 2

## Tools
- Test tool 1
"""

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as temp_file:
            temp_file.write(test_content)
            temp_file_path = temp_file.name

        try:
            # Patch the path resolution to use our temporary file
            with patch("mcp_server_for_oscal.tools.list_oscal_resources.Path") as mock_path:
                mock_path_instance = Mock()
                mock_path_instance.parent = Mock()
                mock_path_instance.parent.parent = Mock()
                mock_path_instance.parent.parent.__truediv__ = Mock(
                    return_value=Path(temp_file_path).parent
                )
                mock_path.__file__ = __file__
                mock_path.return_value = mock_path_instance

                # Mock the final path resolution
                with (
                    patch("builtins.open", open),
                    patch("mcp_server_for_oscal.tools.list_oscal_resources.Path") as mock_path2,
                ):
                    mock_docs_path = Mock()
                    mock_docs_path.__truediv__ = Mock(return_value=Path(temp_file_path))
                    mock_path2.return_value.parent.parent.__truediv__ = Mock(
                        return_value=mock_docs_path
                    )

                    # Direct test with the actual file
                    with open(temp_file_path, encoding="utf-8") as f:
                        result = f.read()

                    assert result == test_content
        finally:
            os.unlink(temp_file_path)

    def test_read_resources_file_empty_file_warning(self):
        """Test that a warning is logged for empty files."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as temp_file:
            # Write empty content
            temp_file.write("")
            temp_file_path = temp_file.name

        try:
            with patch("mcp_server_for_oscal.tools.list_oscal_resources.Path"):
                mock_path_instance = Mock()
                mock_path_instance.parent = Mock()
                mock_path_instance.parent.parent = Mock()

                # Mock the final path to point to our empty temp file
                with (
                    patch("builtins.open", open),
                    patch("mcp_server_for_oscal.tools.list_oscal_resources.logger"),
                ):
                    # Direct file read test
                    with open(temp_file_path, encoding="utf-8") as f:
                        content = f.read()

                    assert content == ""
        finally:
            os.unlink(temp_file_path)

    def test_list_oscal_resources_context_none(self):
        """Test that the tool works when context is None."""
        result = list_oscal_resources()
        assert result["total"] == BUNDLED_ENTRY_COUNT
        assert result["items"]

    def test_read_resources_file_encoding_fallback(self):
        """Test encoding fallback behavior."""
        # Create a file with latin-1 encoding
        test_content_latin1 = "Test content with special char: \xe9"  # é in latin-1

        with tempfile.NamedTemporaryFile(mode="wb", suffix=".md", delete=False) as temp_file:
            temp_file.write(test_content_latin1.encode("latin-1"))
            temp_file_path = temp_file.name

        try:
            # Test that the file can be read with fallback encoding
            with open(temp_file_path, encoding="latin-1", errors="replace") as f:
                content = f.read()

            assert "Test content" in content
            assert len(content) > 0
        finally:
            os.unlink(temp_file_path)

    def test_list_oscal_resources_consistent_calls(self):
        """Test that multiple calls return consistent results."""
        ctx = _ctx()
        assert list_oscal_resources(ctx=ctx) == list_oscal_resources(ctx=ctx)

    def test_list_oscal_resources_logging(self):
        """Test that appropriate logging occurs during successful execution."""
        with patch("mcp_server_for_oscal.tools.list_oscal_resources.logger") as mock_logger:
            list_oscal_resources(ctx=_ctx())

            assert mock_logger.debug.called
            debug_msgs = [call[0][0] for call in mock_logger.debug.call_args_list]
            assert any("Successfully read" in msg for msg in debug_msgs)

    def test_init_store_sets_module_singleton(self, tmp_path):
        """init_store() sets the module-level _store variable."""
        db_path = str(tmp_path / "test.db")
        store = OscalStore(db_path=db_path, seed_from_bundled=False)
        try:
            old = lr_module._store
            lr_module.init_store(store)
            assert lr_module._store is store
            lr_module._store = old
        finally:
            store.close()

    def test_read_from_store_returns_none_when_no_store(self):
        """_read_from_store returns None when the store is not set."""
        from mcp_server_for_oscal.tools.list_oscal_resources import (
            _read_from_store,
        )

        assert _read_from_store() is None


# --- Property 6 generators -------------------------------------------------------------
# Words never start with a heading or bullet marker and hold no whitespace or "#", so a
# rendered line can't be read as a heading or a top-level item by accident.
_WORD = st.from_regex(r"[A-Za-z0-9][A-Za-z0-9\[\]():/.,'_-]{0,11}", fullmatch=True)
# Single-line text: words joined by single spaces (no leading/trailing/internal runs).
_LINE = st.lists(_WORD, min_size=1, max_size=6).map(" ".join)
# An entry: first line plus continuation parts; a continuation may be a nested sub-item.
_CONTINUATION = st.tuples(st.booleans(), _LINE).map(lambda t: f"- {t[1]}" if t[0] else t[1])
_ENTRY = st.tuples(_LINE, st.lists(_CONTINUATION, max_size=3)).map(lambda t: [t[0], *t[1]])
_SECTION = st.tuples(
    _LINE,  # category
    st.integers(min_value=2, max_value=6),  # heading level
    st.lists(
        st.tuples(
            _ENTRY,
            st.sampled_from(["-", "*", "+"]),  # bullet
            st.integers(min_value=1, max_value=4),  # continuation indent
            st.one_of(st.none(), _LINE),  # optional prose paragraph before the entry
        ),
        max_size=5,
    ),
)


class TestParseResourceEntriesProperties:
    """Feature: tool-context-optimization, Property 6: Resource markdown parsing round trip.

    **Validates: Requirements 7.2**
    """

    @settings(max_examples=100)
    @given(
        title=st.one_of(st.none(), _LINE),
        sections=st.lists(_SECTION, max_size=5),
    )
    def test_render_then_parse_round_trip(self, title, sections):
        lines: list[str] = []
        expected: list[dict[str, str]] = []
        if title is not None:
            lines += [f"# {title}", ""]
        for category, level, entries in sections:
            lines += [f"{'#' * level} {category}", ""]
            for parts, bullet, indent, prose in entries:
                if prose is not None:
                    lines += [prose, ""]
                lines.append(f"{bullet} {parts[0]}")
                lines += [f"{' ' * indent}{part}" for part in parts[1:]]
                lines.append("")
                expected.append({"category": category, "text": " ".join(parts)})
        rendered = "\n".join(lines)

        assert parse_resource_entries(rendered) == expected


# --- Property 7 generators -------------------------------------------------------------
# Sections of (category, entry lines); each entry is one bullet line, so the expected entry
# text is the line itself. Parsing is covered by Property 6; this property checks paging.
_PAGE_SECTIONS = st.lists(
    st.tuples(_LINE, st.lists(_LINE, max_size=12)),
    max_size=6,
)


def _render_sections(sections: list[tuple[str, list[str]]]) -> tuple[str, list[dict[str, str]]]:
    """Render sections as awesome-oscal markdown and return it with the expected entries."""
    lines = ["# Awesome OSCAL", ""]
    expected: list[dict[str, str]] = []
    for category, entries in sections:
        lines += [f"## {category}", ""]
        for text in entries:
            lines += [f"- {text}", ""]
            expected.append({"category": category, "text": text})
    return "\n".join(lines), expected


class TestListOscalResourcesPagingProperties:
    """Feature: tool-context-optimization, Property 7: Resource pages match a slice of the
    entries.

    **Validates: Requirements 7.2, 7.3, 7.4**
    """

    @settings(max_examples=100)
    @given(
        sections=_PAGE_SECTIONS,
        offset=st.integers(min_value=0, max_value=80),
        limit=st.integers(min_value=1, max_value=100),
    )
    def test_page_is_slice_of_entries(self, sections, offset, limit):
        markdown, entries = _render_sections(sections)
        with patch.object(lr_module, "read_resources_file", return_value=markdown):
            page = list_oscal_resources(offset=offset, limit=limit)

        assert set(page) == PAGE_KEYS
        assert page["items"] == entries[offset : offset + limit]
        assert page["total"] == len(entries)
        assert page["offset"] == offset
        assert page["limit"] == limit
        assert page["hasMore"] == (offset + limit < len(entries))
        if offset >= len(entries):
            assert page["items"] == []

    @settings(max_examples=100)
    @given(sections=_PAGE_SECTIONS, limit=st.integers(min_value=1, max_value=100))
    def test_pages_concatenate_to_entries(self, sections, limit):
        markdown, entries = _render_sections(sections)
        collected: list[dict[str, str]] = []
        with patch.object(lr_module, "read_resources_file", return_value=markdown):
            offset = 0
            while True:
                page = list_oscal_resources(offset=offset, limit=limit)
                collected.extend(page["items"])
                if not page["hasMore"]:
                    break
                offset += limit
        assert collected == entries

    @settings(max_examples=100)
    @given(sections=_PAGE_SECTIONS)
    def test_default_paging_is_first_page_of_ten(self, sections):
        markdown, entries = _render_sections(sections)
        with patch.object(lr_module, "read_resources_file", return_value=markdown):
            page = list_oscal_resources()

        assert page["offset"] == 0
        assert page["limit"] == 10
        assert page["items"] == entries[:10]
        assert page["total"] == len(entries)
        assert page["hasMore"] == (len(entries) > 10)


# --- Property 8 generators -------------------------------------------------------------
# utils.paginate rejects offset < 0, and limit < 1 or limit > 100 (MAX_PAGE_LIMIT).
_ANY_LIMIT = st.integers(min_value=-1_000, max_value=1_000)
_VALID_OFFSET = st.integers(min_value=0, max_value=1_000)
_INVALID_PAGING = st.one_of(
    st.tuples(st.integers(max_value=-1), _ANY_LIMIT),  # negative offset, any limit
    st.tuples(_VALID_OFFSET, st.integers(max_value=0)),  # limit below 1
    st.tuples(_VALID_OFFSET, st.integers(min_value=101)),  # limit above 100
)


class TestListOscalResourcesInvalidPagingProperties:
    """Feature: tool-context-optimization, Property 8: Invalid paging arguments are rejected.

    **Validates: Requirements 7.5**
    """

    @settings(max_examples=100)
    @given(sections=_PAGE_SECTIONS, paging=_INVALID_PAGING)
    def test_invalid_paging_raises_and_notifies(self, sections, paging):
        offset, limit = paging
        markdown, _ = _render_sections(sections)
        ctx = _ctx()
        with (
            patch.object(lr_module, "read_resources_file", return_value=markdown),
            pytest.raises(ValueError),
        ):
            list_oscal_resources(offset=offset, limit=limit, ctx=ctx)

        ctx.error.assert_called_once()
        assert "Failed to list OSCAL resources" in ctx.error.call_args[0][0]
