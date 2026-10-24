"""Tests for Metaschema markup rendering (RC-11; Requirements 6.7, 6.8, 10.4).

Example tests plus Property 11 (markup renders to the documented Markdown).
"""

import re
from pathlib import Path
from urllib.parse import quote

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools import metaschema_index as mi
from mcp_server_for_oscal.tools.metaschema_index import Markup, MetaschemaIndex
from mcp_server_for_oscal.tools.utils import OSCALModelType


def el(tag: str, *children: "mi._Node | str", **attrs: str) -> mi._Node:
    """Build a markup node; ``href_`` style keys are not needed since attrs are plain names."""
    return mi._Node(tag, attrs, "test.xml", 1, list(children))


def plain(*children: "mi._Node | str") -> str:
    return Markup(el("description", *children)).plain()


def blocks(*children: "mi._Node | str") -> str:
    return Markup(el("remarks", *children)).blocks()


class TestInline:
    @pytest.mark.parametrize(
        ("tag", "expected"),
        [("code", "`x`"), ("q", '"x"'), ("em", "*x*"), ("strong", "**x**")],
    )
    def test_inline_element(self, tag, expected):
        assert plain("a ", el(tag, "x"), " b") == f"a {expected} b"

    def test_nested_inline(self):
        assert plain(el("strong", "a ", el("em", "b ", el("code", "c")))) == "**a *b `c`***"

    def test_other_elements_become_text(self):
        assert plain(el("i", "x"), " ", el("sub", "2"), el("unknown", "y")) == "x 2y"

    def test_wrapper_tag_is_not_rendered(self):
        # Markup may wrap a whole enum element; only its children are rendered.
        assert Markup(el("enum", "Some ", el("code", "v"), value="v")).plain() == "Some `v`"

    def test_empty_content(self):
        assert plain() == ""
        assert plain("  \n\t ") == ""
        assert plain(el("code")) == "``"


class TestLinks:
    @pytest.mark.parametrize(
        "href",
        [
            "http://example.com/a",
            "HTTPS://example.com/a?b=c#d",
            "#frag",
            "relative/path.html",
            "../up#x",
            "page?x=a:b",
            "dir/a:b",
        ],
    )
    def test_safe_href_is_linked(self, href):
        assert plain(el("a", "t", href=href)) == f"[t]({href})"

    @pytest.mark.parametrize(
        "href",
        [
            "//host/path",
            "javascript:alert(1)",
            "JavaScript:alert(1)",
            "data:text/html,x",
            "file:///etc/passwd",
            "mailto:a@example.com",
            "",
        ],
    )
    def test_unsafe_href_is_text_only(self, href):
        assert plain("see ", el("a", "t[x]", href=href)) == "see t[x]"

    def test_missing_href_is_text_only(self):
        assert plain(el("a", "t")) == "t"

    def test_link_text_is_escaped(self):
        assert plain(el("a", r"a[b]\c", href="#x")) == r"[a\[b\]\\c](#x)"

    def test_href_is_percent_encoded(self):
        assert plain(el("a", "t", href="a (b)<c>\td")) == "[t](a%20%28b%29%3Cc%3E%09d)"

    def test_nested_markup_in_link_text(self):
        assert plain(el("a", el("code", "x"), href="https://e.org")) == "[`x`](https://e.org)"

    def test_safe_href_direct(self):
        assert mi._safe_href(None) is None
        assert mi._safe_href("") is None
        assert mi._safe_href("//h") is None
        assert mi._safe_href("http://h/(x)") == "http://h/%28x%29"


class TestControlCharacters:
    def test_c0_removed_except_tab_and_newline(self):
        text = "".join(chr(i) for i in range(0x20)) + "x"
        assert mi._strip_c0(text) == "\t\nx"

    def test_c0_removed_before_whitespace_collapse(self):
        # The removed control joins its neighbours instead of acting as a separator.
        assert plain("a\x01b \x0b c\x7f") == "ab c\x7f"


class TestPlain:
    def test_whitespace_collapsed_and_trimmed(self):
        assert plain("\n  a \t\n b  ", el("code", " c  d "), "  ") == "a b ` c d `"

    def test_blocks_flatten_in_plain(self):
        assert plain(el("p", "one"), el("p", "two")) == "onetwo"
        assert plain(el("p", "one"), "\n", el("p", "two")) == "one two"


class TestBlocks:
    def test_paragraphs_separated(self):
        assert blocks("\n ", el("p", " one\n two "), "\n ", el("p", "three"), "\n") == (
            "one two\n\nthree"
        )

    def test_lists(self):
        ul = el("ul", "\n", el("li", "a ", el("em", "b")), "\n", el("li", "c"), "\n")
        ol = el("ol", el("li", "d"))
        assert blocks(el("p", "intro"), ul, ol) == "intro\n\n- a *b*\n- c\n\n- d"

    def test_nested_list_and_paragraph_in_item(self):
        li = el("li", el("p", "a"), el("p", "b"), el("ul", el("li", "c")))
        assert blocks(el("ul", li)) == "- a b\n- c"

    def test_headings_and_bare_inline_text(self):
        assert blocks("lead ", el("code", "x"), el("h2", "Head"), el("p", "body")) == (
            "lead `x`\n\nHead\n\nbody"
        )

    def test_empty(self):
        assert blocks() == ""
        assert blocks(el("p", "  "), el("ul")) == ""


@pytest.fixture(scope="module")
def index() -> MetaschemaIndex:
    return MetaschemaIndex.load()


class TestBundle:
    def test_defined_component_description(self, index):
        (definition,) = index.scope(OSCALModelType.COMPONENT_DEFINITION).definitions(
            "defined-component"
        )
        assert definition.description is not None
        text = definition.description.plain()
        assert text
        assert "\n" not in text
        assert "  " not in text

    def test_part_remarks_keep_list_items(self, index):
        parts = index.scope(OSCALModelType.CATALOG).definitions("part")
        remarks = [d.remarks.blocks() for d in parts if d.remarks is not None]
        assert any("\n- Referencing a part by id to tailor" in r and "\n\n" in r for r in remarks)
        assert all("<" not in r for r in remarks)

    def test_bundle_links_and_code_render(self, index):
        rendered = [
            d.description.plain()
            for name in index.all_modules().definition_names()
            for d in index.all_modules().definitions(name)
            if d.description is not None
        ]
        assert any("`" in text for text in rendered)
        assert all("\n" not in text and text == text.strip() for text in rendered)


# --------------------------------------------------------------------------------------
# Property 11: Markup renders to the documented Markdown (Requirements 6.7, 6.8, 10.4)
#
# Trees are generated as plain tuples ``(tag, href | None, children)``, serialized to XML,
# parsed with the module loader, and rendered with ``Markup``. An independent reference
# renderer implements the documented rules (design "Markup rendering", RC-11) directly on
# the generated tuples.
# --------------------------------------------------------------------------------------

type Tree = str | tuple[str, str | None, list[Tree]]

_MARKDOWN = {"code": ("`", "`"), "q": ('"', '"'), "em": ("*", "*"), "strong": ("**", "**")}
_OTHER_TAGS = ("i", "sub", "unknown", "p")  # rendered as their text content in plain()
_INLINE_TAGS = (*_MARKDOWN, "a", "i", "sub", "unknown")  # no block tags (for blocks())

# Text: Markdown and link specials, XML specials, whitespace runs (CR arrives as &#13;).
_TEXT = st.text(alphabet="ab Z[]()\\`*&<>\"'é \t\n\r\u00a0", max_size=12)
_HREF_PREFIXES = (
    "http://",
    "HTTPS://",
    "Http:",
    "#",
    "",
    "./",
    "../",
    "//",
    "javascript:",
    "JavaScript:",
    "data:",
    "file:///",
    "mailto:",
    "vbscript:",
)
_HREFS = st.one_of(
    st.none(),
    st.builds(
        str.__add__,
        st.sampled_from(_HREF_PREFIXES),
        st.text(alphabet="ab/?#:.()<>% \t\n\r\u00a0é", max_size=10),
    ),
)


def _elements(depth: int, tags: tuple[str, ...]) -> st.SearchStrategy[Tree]:
    children = _children(depth - 1, tags) if depth > 1 else st.lists(_TEXT, max_size=2)
    return st.builds(
        lambda tag, href, kids: (tag, href if tag == "a" else None, kids),
        st.sampled_from(tags),
        _HREFS,
        children,
    )


def _children(depth: int, tags: tuple[str, ...]) -> st.SearchStrategy[list[Tree]]:
    return st.lists(st.one_of(_TEXT, _elements(depth, tags)), max_size=4)


_PLAIN_TREES = _children(3, (*_MARKDOWN, "a", *_OTHER_TAGS))
_ITEMS = _children(3, _INLINE_TAGS)
_WS = st.text(alphabet=" \t\n", max_size=3)


@st.composite
def _block_trees(draw: st.DrawFn) -> list[Tree]:
    """``p`` and ``ul``/``ol`` of ``li`` blocks, separated by whitespace-only text."""
    out: list[Tree] = []
    for _ in range(draw(st.integers(0, 4))):
        out.append(draw(_WS))
        tag = draw(st.sampled_from(("p", "ul", "ol")))
        if tag == "p":
            out.append(("p", None, draw(_ITEMS)))
        else:
            items = draw(st.lists(_ITEMS, max_size=3))
            kids: list[Tree] = []
            for item in items:
                kids += [draw(_WS), ("li", None, item)]
            out.append((tag, None, kids))
    out.append(draw(_WS))
    return out


# --- XML serialization and loader parsing ---------------------------------------------


def _xml_text(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#13;")
    )


def _xml_attr(value: str) -> str:
    # Character references keep tab, newline, and CR through attribute-value normalization.
    out = _xml_text(value).replace('"', "&quot;")
    return out.replace("\t", "&#9;").replace("\n", "&#10;")


def _to_xml(part: Tree) -> str:
    if isinstance(part, str):
        return _xml_text(part)
    tag, href, kids = part
    attr = "" if href is None else f' href="{_xml_attr(href)}"'
    return f"<{tag}{attr}>{''.join(map(_to_xml, kids))}</{tag}>"


def _loaded(wrapper: str, children: list[Tree]) -> Markup:
    """Serialize ``children`` under ``wrapper``, parse with the loader, return its Markup."""
    xml = (
        f'<METASCHEMA xmlns="{mi.METASCHEMA_NS}">'
        f"<{wrapper}>{''.join(map(_to_xml, children))}</{wrapper}></METASCHEMA>"
    )
    builder = mi._TreeBuilder(Path(), mi.BundlePath(Path("test.xml"), "test.xml"), frozenset())
    (node,) = builder.parse(xml.encode()).children
    assert isinstance(node, mi._Node)
    return Markup(node)


# --- Reference renderer ---------------------------------------------------------------


def _ref_c0(text: str) -> str:
    return "".join(c for c in text if ord(c) >= 0x20 or c in "\t\n")


def _ref_href(href: str | None) -> str | None:
    """Documented link rule: http(s), fragment, or scheme-less relative not starting ``//``."""
    if not href:
        return None
    if not href.startswith("#"):
        if href.startswith("//"):
            return None
        first = re.match(r"[^/?#]*", href).group()  # type: ignore[union-attr]
        if ":" in first and first.split(":", 1)[0].lower() not in {"http", "https"}:
            return None
    # ( ) < > and everything at or below U+0020 are percent-encoded. Other whitespace too,
    # so that whitespace collapse cannot split the link target.
    return "".join(
        quote(c, safe="") if c in "()<>" or ord(c) <= 0x20 or c.isspace() else c for c in href
    )


def _ref_inline(parts: list[Tree]) -> str:
    out = ""
    for part in parts:
        if isinstance(part, str):
            out += _ref_c0(part)  # C0 removed before any whitespace handling
            continue
        tag, href, kids = part
        text = _ref_inline(kids)
        safe = _ref_href(href) if tag == "a" else None
        if tag in _MARKDOWN:
            prefix, suffix = _MARKDOWN[tag]
            out += prefix + text + suffix
        elif safe is not None:
            escaped = text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            out += f"[{escaped}]({safe})"
        else:
            out += text
    return out


def _ref_collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _ref_plain(parts: list[Tree]) -> str:
    return _ref_collapse(_ref_inline(parts))


def _ref_blocks(parts: list[Tree]) -> str:
    rendered: list[str] = []
    for part in parts:
        if isinstance(part, str):
            continue  # whitespace-only separators
        tag, _, kids = part
        if tag == "p":
            rendered.append(_ref_plain(kids))
        else:
            items = [_ref_plain(li[2]) for li in kids if not isinstance(li, str)]
            rendered.append("\n".join(f"- {item}" for item in items if item))
    return "\n\n".join(block for block in rendered if block)


def _hrefs(parts: list[Tree]) -> list[str | None]:
    found: list[str | None] = []
    for part in parts:
        if not isinstance(part, str):
            if part[0] == "a":
                found.append(part[1])
            found += _hrefs(part[2])
    return found


class TestMarkupProperty:
    """Feature: metaschema-constraints, Property 11: Markup renders to the documented Markdown.

    **Validates: Requirements 6.7, 6.8, 10.4**
    """

    @settings(max_examples=200, deadline=None)
    @given(children=_PLAIN_TREES)
    def test_plain_matches_reference(self, children):
        rendered = _loaded("description", children).plain()
        assert rendered == _ref_plain(children)
        assert rendered == rendered.strip()
        assert not any(c < " " for c in rendered)
        assert "  " not in rendered

    @settings(max_examples=200, deadline=None)
    @given(children=_block_trees())
    def test_blocks_match_reference(self, children):
        rendered = _loaded("remarks", children).blocks()
        assert rendered == _ref_blocks(children)
        assert not any(c < " " and c != "\n" for c in rendered)
        assert "\n\n\n" not in rendered

    @settings(max_examples=200, deadline=None)
    @given(href=_HREFS)
    def test_safe_href_matches_reference(self, href):
        safe = mi._safe_href(href)
        assert safe == _ref_href(href)
        if safe is not None:
            assert not any(c in "()<>" or c <= " " or c.isspace() for c in safe)

    @settings(max_examples=200, deadline=None)
    @given(children=_PLAIN_TREES)
    def test_unsafe_links_are_text_only(self, children):
        """With every link unsafe, plain() equals the rendering with ``a`` as a plain span."""

        def unlink(parts: list[Tree]) -> list[Tree]:
            return [
                p
                if isinstance(p, str)
                else ("unknown" if p[0] == "a" else p[0], None, unlink(p[2]))
                for p in parts
            ]

        if all(_ref_href(h) is None for h in _hrefs(children)):
            assert _loaded("description", children).plain() == _ref_plain(unlink(children))

    @settings(max_examples=200, deadline=None)
    @given(text=st.text())
    def test_c0_filter(self, text):
        filtered = mi._strip_c0(text)
        assert not any(c < " " and c not in "\t\n" for c in filtered)
        assert filtered == _ref_c0(text)


class TestLoadedLinks:
    """RC-11 examples through the loader: ``javascript:`` and ``//host`` links are text only."""

    @pytest.mark.parametrize("href", ["javascript:alert(1)", "//evil.example/x"])
    def test_unsafe_link_is_text_only(self, href):
        markup = _loaded("description", ["see ", ("a", href, ["here"]), "."])
        assert markup.plain() == "see here."

    def test_safe_link_is_linked(self):
        markup = _loaded("description", [("a", "https://e.org/a b", ["t"])])
        assert markup.plain() == "[t](https://e.org/a%20b)"
