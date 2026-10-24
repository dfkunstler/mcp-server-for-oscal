"""Budget_Tests for the tools/list context budget (Feature: tool-context-optimization).

Layout, one class per concern so later tasks can append classes:

- ``TestBudgetConstants`` / ``TestSplitSentences`` / ``TestOldToolNamePattern`` /
  ``TestViolationMessage``: unit tests on synthetic input (task 2.4).
- Property tests for the checkers (tasks 2.5-2.7).
- Live-registry tests against ``tools/list`` (task 9.1).
- Steering file sync tests (task 10.3).
"""

import asyncio
import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from mcp_server_for_oscal import main
from mcp_server_for_oscal.tools import get_tool_list, tool_budget
from mcp_server_for_oscal.tools.descriptions import ROUTING_GUIDE, SERVER_INSTRUCTIONS
from mcp_server_for_oscal.tools.tool_budget import (
    KIND_MISSING_PARAMETER_DESCRIPTION,
    KIND_PARAMETER_DESCRIPTION,
    KIND_TOOL_DESCRIPTION,
    KIND_TOTAL_SIZE,
    Violation,
    check_limits,
    check_total,
    split_sentences,
    summary_sentence,
    tools_list_size,
)

# Req 4.1: old tool names that must not appear in any description (word-bounded).
OLD_TOOL_NAMES = re.compile(r"\b(list_models|get_schema)\b")


# --- Unit tests: constants and helpers (task 2.4) --------------------------------------------


class TestBudgetConstants:
    """Req 1.1, 1.2, 1.8: the budget constants are the documented values."""

    def test_total_budget_at_most_70_percent_of_baseline(self):
        assert tool_budget.BASELINE_SIZE * 0.7 >= tool_budget.TOTAL_BUDGET

    def test_documented_values(self):
        assert tool_budget.BASELINE_SIZE == 55_685
        assert tool_budget.TOTAL_BUDGET == 38_979
        assert tool_budget.TOOL_DESCRIPTION_CAP == 1_200
        assert tool_budget.PARAMETER_DESCRIPTION_CAP == 250
        assert tool_budget.ROUTING_GUIDE_CAP == 3_000
        assert tool_budget.MAX_DESCRIPTION_SENTENCES == 3


class TestSplitSentences:
    """Abbreviations stay inside their sentence."""

    def test_abbreviations_do_not_split(self):
        text = "Return values, e.g. Strings, i.e. Text. Also lists, etc. And more. Use when needed."
        assert split_sentences(text) == [
            "Return values, e.g. Strings, i.e. Text.",
            "Also lists, etc. And more.",
            "Use when needed.",
        ]

    def test_uppercase_abbreviations_do_not_split(self):
        assert split_sentences("Get items, E.G. Controls. Done.") == [
            "Get items, E.G. Controls.",
            "Done.",
        ]

    def test_whitespace_collapsed_and_empty(self):
        assert split_sentences("List  things.\n   Use when\nneeded.") == [
            "List things.",
            "Use when needed.",
        ]
        assert split_sentences("   \n ") == []

    def test_summary_sentence(self):
        assert summary_sentence("Return a schema, e.g. JSON. More text.") == (
            "Return a schema, e.g. JSON."
        )
        assert summary_sentence("") == ""


class TestOldToolNamePattern:
    """Req 4.1: the old-name check does not match the current tool names."""

    def test_current_names_do_not_match(self):
        assert OLD_TOOL_NAMES.search("Call list_oscal_models or get_oscal_schema.") is None

    def test_old_names_match(self):
        assert OLD_TOOL_NAMES.search("use the list_models tool") is not None
        assert OLD_TOOL_NAMES.search("call get_schema first") is not None


class TestViolationMessage:
    """Req 1.7: a violation message names tool, parameter, measured length, and limit."""

    def test_parameter_violation_message(self):
        v = Violation("get_constraints", "target", KIND_PARAMETER_DESCRIPTION, 270, 250)
        assert v.message() == (
            "tool=get_constraints parameter=target: parameter-description length 270 > limit 250"
        )

    def test_tool_violation_message_has_no_parameter(self):
        v = Violation("query_catalog", None, KIND_TOOL_DESCRIPTION, 1_300, 1_200)
        assert v.message() == "tool=query_catalog: tool-description length 1300 > limit 1200"

    def test_detail_appended(self):
        v = Violation("t_x", "p", "missing-parameter-description", None, None, "blank")
        assert v.message() == "tool=t_x parameter=p: missing-parameter-description (blank)"


# --- Property tests: limit checker (task 2.5) ------------------------------------------------

_TOOL_CAP = tool_budget.TOOL_DESCRIPTION_CAP
_PARAM_CAP = tool_budget.PARAMETER_DESCRIPTION_CAP

_names = st.from_regex(r"[a-z]+(_[a-z]+){0,3}", fullmatch=True)


def _lengths_around(cap: int) -> st.SearchStrategy[int]:
    """Lengths at and next to a cap, plus random lengths on both sides of it."""
    return st.one_of(st.sampled_from([0, 1, cap - 1, cap, cap + 1]), st.integers(0, cap + 50))


# A parameter description spec: ("missing",), ("blank", n spaces), or ("text", length).
_param_specs = st.one_of(
    st.just(("missing", 0)),
    st.tuples(st.just("blank"), st.integers(0, 5)),
    st.tuples(st.just("text"), _lengths_around(_PARAM_CAP).filter(lambda n: n > 0)),
)
# A tool description spec: None (key absent) or a length.
_tool_desc_specs = st.one_of(st.none(), _lengths_around(_TOOL_CAP))
_tool_specs = st.tuples(_names, _tool_desc_specs, st.dictionaries(_names, _param_specs, max_size=4))


def _build_tool(spec: tuple) -> dict:
    """Build a synthetic tools/list entry from a (name, description spec, params spec) tuple."""
    name, desc_len, params = spec
    tool: dict = {"name": name, "inputSchema": {"type": "object", "properties": {}}}
    if desc_len is not None:
        tool["description"] = "x" * desc_len
    for param, (kind, n) in params.items():
        prop: dict = {"type": "string"}
        if kind == "blank":
            prop["description"] = " " * n
        elif kind == "text":
            prop["description"] = "y" * n
        tool["inputSchema"]["properties"][param] = prop
    return tool


def _expected_violations(specs: list[tuple]) -> list[tuple]:
    """Independent model of check_limits: (tool, parameter, kind, measured, limit) in order."""
    expected: list[tuple] = []
    for name, desc_len, params in specs:
        if (desc_len or 0) > _TOOL_CAP:
            expected.append((name, None, KIND_TOOL_DESCRIPTION, desc_len, _TOOL_CAP))
        for param, (kind, n) in params.items():
            if kind in {"missing", "blank"}:
                expected.append((name, param, KIND_MISSING_PARAMETER_DESCRIPTION, None, None))
            elif n > _PARAM_CAP:
                expected.append((name, param, KIND_PARAMETER_DESCRIPTION, n, _PARAM_CAP))
    return expected


class TestLimitCheckerProperty:
    """Feature: tool-context-optimization, Property 1: Limit checker reports exactly the
    over-limit descriptions.

    **Validates: Requirements 1.4, 1.5, 1.7, 3.2**
    """

    @settings(max_examples=100)
    @given(specs=st.lists(_tool_specs, max_size=6))
    def test_check_limits_reports_exactly_expected(self, specs):
        tools = [_build_tool(s) for s in specs]
        violations = check_limits(tools)

        actual = [(v.tool, v.parameter, v.kind, v.measured, v.limit) for v in violations]
        assert actual == _expected_violations(specs)

        for v in violations:
            msg = v.message()
            assert f"tool={v.tool}" in msg
            if v.parameter is not None:
                assert f"parameter={v.parameter}" in msg
            else:
                assert "parameter=" not in msg
            if v.kind != KIND_MISSING_PARAMETER_DESCRIPTION:
                assert f"length {v.measured} > limit {v.limit}" in msg

    @settings(max_examples=100)
    @given(specs=st.lists(_tool_specs, max_size=6), delta=st.integers(-5, 5))
    def test_check_total_reports_iff_over_budget(self, specs, delta):
        tools = [_build_tool(s) for s in specs]
        size = tools_list_size(tools)
        budget = max(size + delta, 0)
        violations = check_total(tools, budget)

        if size > budget:
            assert [(v.tool, v.parameter, v.kind, v.measured, v.limit) for v in violations] == [
                (tool_budget.ALL_TOOLS, None, KIND_TOTAL_SIZE, size, budget)
            ]
            assert f"length {size} > limit {budget}" in violations[0].message()
        else:
            assert violations == []


# --- Property tests: shape checker (task 2.6) ------------------------------------------------

# Lowercase words with no sentence punctuation; "ctx" is excluded so it is only injected.
_words = st.lists(
    st.from_regex(r"[a-z]{1,8}", fullmatch=True).filter(lambda w: w != "ctx"),
    min_size=1,
    max_size=8,
)
_summary_verbs = st.sampled_from(sorted(tool_budget.SUMMARY_VERBS))
# Capitalized first words that are not summary verbs (includes near misses like "Returns").
_non_verbs = st.sampled_from(["The", "This", "Tool", "Returns", "Lists", "Gets", "Use", "An"])
_section_names = st.sampled_from(["Args", "Arguments", "Parameters", "Returns", "Raises", "Yields"])


def _sentence(first: str, words: list[str]) -> str:
    return f"{first} {' '.join(words)}."


@st.composite
def _well_formed(draw) -> list[str]:
    """1-3 sentences: a summary, an optional "Use when" sentence, an optional paging sentence."""
    sentences = [_sentence(draw(_summary_verbs), draw(_words))]
    if draw(st.booleans()):
        sentences.append(_sentence("Use when", draw(_words)))
    if draw(st.booleans()):
        sentences.append("Results are paged.")
    return sentences


class TestShapeCheckerProperty:
    """Feature: tool-context-optimization, Property 2: Shape checker rejects forbidden
    structure.

    **Validates: Requirements 2.1, 2.2, 2.3, 2.4**
    """

    @settings(max_examples=100)
    @given(sentences=_well_formed(), sep=st.sampled_from([" ", "\n", "\n    "]))
    def test_well_formed_has_no_problems(self, sentences, sep):
        assert tool_budget.description_shape_problems(sep.join(sentences)) == []

    @settings(max_examples=100)
    @given(sentences=_well_formed(), section=_section_names, words=_words)
    def test_docstring_section_reported(self, sentences, section, words):
        text = " ".join(sentences) + f"\n\n{section}:\n    {' '.join(words)}"
        problems = tool_budget.description_shape_problems(text)
        assert any(f"docstring section {section}:" in p for p in problems)

    @settings(max_examples=100)
    @given(sentences=_well_formed(), data=st.data())
    def test_ctx_mention_reported(self, sentences, data):
        idx = data.draw(st.integers(0, len(sentences) - 1))
        sentences[idx] = sentences[idx].removesuffix(".") + " with ctx."
        problems = tool_budget.description_shape_problems(" ".join(sentences))
        assert "mentions ctx" in problems

    @settings(max_examples=100)
    @given(sentences=_well_formed(), extra=st.lists(_words, min_size=1, max_size=3))
    def test_too_many_sentences_reported(self, sentences, extra):
        # Pad to at least MAX + 1 sentences, so the result always has a 4th sentence.
        while len(sentences) + len(extra) <= tool_budget.MAX_DESCRIPTION_SENTENCES:
            extra.append(extra[0])
        sentences += [_sentence("Also", w) for w in extra]
        problems = tool_budget.description_shape_problems(" ".join(sentences))
        expected = (
            f"has {len(sentences)} sentences, more than {tool_budget.MAX_DESCRIPTION_SENTENCES}"
        )
        assert expected in problems

    @settings(max_examples=100)
    @given(sentences=_well_formed(), first=_non_verbs, words=_words)
    def test_non_verb_summary_reported(self, sentences, first, words):
        sentences[0] = _sentence(first, words)
        problems = tool_budget.description_shape_problems(" ".join(sentences))
        assert any(p.startswith(f"summary sentence starts with {first!r}") for p in problems)

    @settings(max_examples=100)
    @given(sentences=_well_formed(), words=_words)
    def test_use_without_when_reported(self, sentences, words):
        if words[0] == "when":
            words[0] = "this"
        sentences = [*sentences[:1], _sentence("Use", words), *sentences[1:2]]
        problems = tool_budget.description_shape_problems(" ".join(sentences))
        assert any("but not 'Use when'" in p for p in problems)


# --- Property tests: unknown tool references (task 2.7) --------------------------------------

# Tool-name format: lowercase snake_case with at least one underscore (same as tool_budget._NAME).
_tool_format_names = st.from_regex(r"[a-z][a-z0-9]{0,6}(?:_[a-z0-9]{1,6}){1,3}", fullmatch=True)
_BARE_PREFIXES = ("list_", "get_", "query_", "validate_", "find_", "search_", "text_search_")
# Names that the bare-word pattern detects: a verb prefix followed by snake_case segments.
_verb_names = st.builds(
    lambda prefix, rest: prefix + rest,
    st.sampled_from(_BARE_PREFIXES),
    st.from_regex(r"[a-z0-9]{1,6}(?:_[a-z0-9]{1,6}){0,2}", fullmatch=True),
)
_ref_names = st.one_of(_tool_format_names, _verb_names)
# Filler prose: lowercase words with no underscore, backtick, or "(", so filler alone can never
# form a detectable reference, and a space always separates it from a placed name.
_filler = st.lists(st.from_regex(r"[a-z]{1,8}", fullmatch=True), max_size=3).map(" ".join)
_PLACEMENTS = ("backtick", "backtick_call", "call", "bare")


def _place(name: str, style: str) -> str:
    return {
        "backtick": f"`{name}`",
        "backtick_call": f"`{name}()`",
        "call": f"{name}(x)",
        "bare": name,
    }[style]


def _detectable(name: str, style: str) -> bool:
    """Independent model: backticked and call forms always count; bare only with a verb prefix."""
    return style != "bare" or name.startswith(_BARE_PREFIXES)


@st.composite
def _reference_texts(draw) -> tuple[str, frozenset[str], list[str]]:
    """Return (text, allowed set, expected unknown references) for mixed prose and names."""
    allowed = frozenset(draw(st.sets(_ref_names, max_size=6)))
    unknown = draw(st.sets(_ref_names.filter(lambda n: n not in allowed), max_size=6))
    pool = sorted(allowed | unknown)
    placed = draw(
        st.lists(st.tuples(st.sampled_from(pool), st.sampled_from(_PLACEMENTS)), max_size=10)
        if pool
        else st.just([])
    )
    parts = [draw(_filler)]
    for name, style in placed:
        parts.extend([_place(name, style), draw(st.sampled_from(["", ",", "."])), draw(_filler)])
    text = " ".join(p for p in parts if p)
    expected = sorted({n for n, s in placed if n in unknown and _detectable(n, s)})
    return text, allowed, expected


_schema_values = st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True)


@st.composite
def _tools_with_identifiers(draw) -> tuple[list[dict], set[str]]:
    """Synthetic tools/list entries plus the identifiers known_identifiers must collect."""
    tools: list[dict] = []
    expected: set[str] = set()
    for _ in range(draw(st.integers(0, 4))):
        name = draw(_tool_format_names)
        props: dict = {}
        expected.add(name)
        for prop_name in draw(st.sets(_schema_values, max_size=3)):
            values = draw(st.lists(_schema_values, max_size=3))
            shape = draw(st.sampled_from(["plain", "enum", "anyof", "const"]))
            if shape == "enum" and values:
                prop = {"type": "string", "enum": [*values, 1, None]}
                expected.update(values)
            elif shape == "anyof" and values:
                prop = {"anyOf": [{"type": "string", "enum": values}, {"type": "null"}]}
                expected.update(values)
            elif shape == "const" and values:
                prop = {"const": values[0]}
                expected.add(values[0])
            else:
                prop = {"type": "integer"}
            props[prop_name] = {**prop, "description": "Free text, not an identifier."}
            expected.add(prop_name)
        tools.append({"name": name, "inputSchema": {"type": "object", "properties": props}})
    return tools, expected | set(tool_budget.EXTRA_IDENTIFIERS)


class TestUnknownToolRefsProperty:
    """Feature: tool-context-optimization, Property 3: Unknown tool references are detected
    exactly.

    **Validates: Requirements 2.5, 2.6, 8.3, 8.4**
    """

    @settings(max_examples=100)
    @given(case=_reference_texts())
    def test_reports_exactly_detectable_unknown_names(self, case):
        text, allowed, expected = case
        result = tool_budget.unknown_tool_refs(text, allowed)
        assert result == expected
        assert not set(result) & allowed

    @settings(max_examples=100)
    @given(words=_filler)
    def test_filler_prose_has_no_references(self, words):
        assert tool_budget.unknown_tool_refs(words, frozenset()) == []

    @settings(max_examples=100)
    @given(case=_tools_with_identifiers())
    def test_known_identifiers_collects_all_sources(self, case):
        tools, expected = case
        known = tool_budget.known_identifiers(tools)
        assert known == expected
        # Every collected name, referenced in any detectable form, is never reported.
        text = " ".join(f"`{n}` {n}( {n}" for n in sorted(expected))
        assert tool_budget.unknown_tool_refs(text, known) == []

    def test_examples(self):
        tools = [
            {
                "name": "query_catalog",
                "inputSchema": {
                    "properties": {
                        "query_type": {
                            "anyOf": [{"enum": ["all", "by_uuid"]}, {"type": "null"}],
                            "description": "x",
                        }
                    }
                },
            }
        ]
        known = tool_budget.known_identifiers(tools)
        assert {"query_catalog", "query_type", "by_uuid", "raw_json"} <= known
        text = "Use query_catalog with `by_uuid`; then list_models, `get_schema()`, or foo_bar(."
        assert tool_budget.unknown_tool_refs(text, known) == [
            "foo_bar",
            "get_schema",
            "list_models",
        ]


# --- Live-registry tests against tools/list (task 9.1) ---------------------------------------
#
# These tests iterate the live tool registration, never a hard-coded tool list, so a tool added
# to get_tool_list() later is checked against the caps, the shape, and the budget with no test
# change (Req 1.6). Only the content and schema-fact checks name specific tools.


@pytest.fixture(scope="module")
def live_server() -> MCPServer:
    """A fresh server built and registered through the production path (Req 1.6)."""
    server = main.build_server()
    main._setup_tools(server)
    return server


@pytest.fixture(scope="module")
def live_tools(live_server: MCPServer) -> list[dict]:
    """The wire shape of the live tools/list `tools` array."""
    tools = asyncio.run(live_server.list_tools())
    return [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]


def _by_name(tools: list[dict]) -> dict[str, dict]:
    return {t["name"]: t for t in tools}


def _props(tool: dict) -> dict:
    return tool.get("inputSchema", {}).get("properties", {})


def _enum_values(prop: dict) -> list:
    """The enum of a property, also when it is wrapped in anyOf (optional Literal)."""
    if "enum" in prop:
        return prop["enum"]
    for option in prop.get("anyOf", []):
        if "enum" in option:
            return option["enum"]
    return []


def _assert_no_violations(violations: list[Violation]) -> None:
    assert violations == [], "\n".join(v.message() for v in violations)


PER_MODEL_QUERY_TOOLS = (
    "query_catalog",
    "query_ssp",
    "query_profile",
    "query_assessment_plan",
    "query_assessment_results",
    "query_poam",
    "query_mapping_collection",
)
CDEF_TOOLS = (
    "query_component_definition",
    "list_component_definitions",
    "list_components",
    "list_capabilities",
    "get_capability",
)
CAPABILITY_FIRST_PHRASES = re.compile(
    r"capability-first|prefer querying by capability|start here", re.IGNORECASE
)


class TestLiveBudget:
    """Req 1.3-1.8, 3.2: the live tools/list fits the caps and the Total_Budget."""

    def test_names_match_registry(self, live_tools):
        expected = {fn.__name__ for fn in get_tool_list()} | {"about"}
        assert {t["name"] for t in live_tools} == expected

    def test_limits(self, live_tools):
        _assert_no_violations(check_limits(live_tools))

    def test_total_budget(self, live_tools):
        _assert_no_violations(check_total(live_tools))


class TestLiveShape:
    """Req 2.1-2.6, 3.3, 4.1, 8.3, 8.4: descriptions follow the Description_Shape and name only
    tools that exist."""

    def test_description_shape(self, live_tools):
        problems = [
            f"tool={t['name']}: {p}"
            for t in live_tools
            for p in tool_budget.description_shape_problems(t.get("description") or "")
        ]
        assert problems == [], "\n".join(problems)

    def test_no_ctx_in_input_schema(self, live_tools):
        offenders = [t["name"] for t in live_tools if "ctx" in _props(t)]
        assert offenders == []

    def test_no_old_tool_names(self, live_tools):
        offenders = [
            f"tool={t['name']}: {m.group(0)}"
            for t in live_tools
            for m in OLD_TOOL_NAMES.finditer(t.get("description") or "")
        ]
        assert offenders == [], "\n".join(offenders)

    def test_no_unknown_tool_refs(self, live_tools, live_server):
        known = tool_budget.known_identifiers(live_tools)
        texts = [(t["name"], t.get("description") or "") for t in live_tools]
        texts.append(("instructions", live_server.instructions or ""))
        problems = [
            f"{where}: unknown name {name}"
            for where, text in texts
            for name in tool_budget.unknown_tool_refs(text, known)
        ]
        assert problems == [], "\n".join(problems)


class TestLiveContent:
    """Req 4.2-4.5, 6.6: specific descriptions carry the corrected guidance."""

    def test_cdef_no_capability_first(self, live_tools):
        tools = _by_name(live_tools)
        for name in CDEF_TOOLS:
            assert CAPABILITY_FIRST_PHRASES.search(tools[name]["description"]) is None, name

    def test_cdef_main_route_and_capability_scope(self, live_tools):
        tools = _by_name(live_tools)
        assert "list_components" in tools["query_component_definition"]["description"]
        for name in ("list_capabilities", "get_capability"):
            assert "contain capabilities" in " ".join(tools[name]["description"].split()), name

    def test_validate_file_remote_uri_text(self, live_tools):
        tool = _by_name(live_tools)["validate_oscal_file"]
        texts = [tool["description"], *(p.get("description", "") for p in _props(tool).values())]
        assert not any("allow_remote_uris" in t for t in texts)
        file_uri = _props(tool)["file_uri"]["description"]
        assert "Remote URIs are rejected" in file_uri
        assert "server operator enables" in file_uri

    @pytest.mark.parametrize("name", PER_MODEL_QUERY_TOOLS)
    def test_per_model_query_points_to_children(self, live_tools, name):
        description = _by_name(live_tools)[name]["description"]
        assert "childTypeCounts" in description
        assert "get_child_element" in description


class TestLiveSchemaFacts:
    """Req 5.1-5.8, 6.1, 7.1: required parameters, enums, and paging parameters."""

    @pytest.mark.parametrize(
        ("tool", "param"),
        [
            ("get_oscal_schema", "model_name"),
            ("get_capability", "uuid"),
            ("text_search_oscal", "query_text"),
            ("get_child_element", "element_id"),
        ],
    )
    def test_required(self, live_tools, tool, param):
        assert param in _by_name(live_tools)[tool]["inputSchema"].get("required", [])

    def test_enums(self, live_tools):
        tools = _by_name(live_tools)
        assert _enum_values(_props(tools["get_oscal_schema"])["schema_type"]) == ["json", "xsd"]
        assert {"assembly", "field", "flag"} <= set(
            _enum_values(_props(tools["get_definition"])["kind"])
        )

    @pytest.mark.parametrize("name", PER_MODEL_QUERY_TOOLS)
    def test_per_model_query_type_enum(self, live_tools, name):
        prop = _props(_by_name(live_tools)[name])["query_type"]
        assert _enum_values(prop) == ["all", "by_uuid", "by_title"]

    def test_list_oscal_resources_paging(self, live_tools):
        assert {"offset", "limit"} <= set(_props(_by_name(live_tools)["list_oscal_resources"]))


class TestLiveCallPath:
    """Req 5.2, 5.9, 6.2: invalid Literal values fail SDK validation before the tool runs."""

    @pytest.mark.parametrize(
        ("name", "args", "valid"),
        [
            ("get_oscal_schema", {"model_name": "catalog", "schema_type": "yaml"}, ("json", "xsd")),
            ("get_definition", {"name": "uuid", "kind": "module"}, ("assembly", "field", "flag")),
            ("query_catalog", {"query_type": "by_type"}, ("all", "by_uuid", "by_title")),
        ],
    )
    def test_invalid_literal_lists_valid_values(self, live_server, name, args, valid):
        # query_catalog runs without an initialized store here, so the error can only come
        # from argument validation, which runs before the function body.
        with pytest.raises(ToolError) as exc_info:
            asyncio.run(live_server.call_tool(name, args))
        message = str(exc_info.value)
        for value in valid:
            assert f"'{value}'" in message, (value, message)

    def test_complete_schema_accepted(self, live_server):
        # get_oscal_schema reads ctx.session, which needs a real request, so this call goes
        # through an in-memory client rather than MCPServer.call_tool.
        async def call() -> CallToolResult:
            async with Client(live_server) as client:
                return await client.call_tool("get_oscal_schema", {"model_name": "complete"})

        result = asyncio.run(call())
        assert not result.is_error, result.content
        assert result.content


class TestRoutingGuide:
    """Req 8.1-8.5: instructions hold the Routing_Guide, which names a tool per task."""

    ROUTED_TOOLS = (
        "list_oscal_models",
        "get_oscal_schema",
        "get_definition",
        "get_constraints",
        "get_allowed_values",
        "query_catalog",
        "text_search_oscal",
        "query_component_definition",
        "validate_oscal_content",
        "list_oscal_resources",
        "query_oscal_documentation",
    )

    def test_instructions_contain_guide(self, live_server):
        assert ROUTING_GUIDE in (live_server.instructions or "")

    @pytest.mark.parametrize("name", ROUTED_TOOLS)
    def test_guide_names_tool(self, name):
        assert re.search(rf"\b{name}\b", ROUTING_GUIDE), name

    def test_guide_within_cap(self):
        assert len(SERVER_INSTRUCTIONS) <= tool_budget.ROUTING_GUIDE_CAP


# --- Steering and AGENTS.md sync tests (task 10.3) -------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
STEERING_PATH = ".kiro/steering/tool-authoring.md"
AGENTS_PATH = "AGENTS.md"
WIRING_HEADING = "## How a tool is wired"
MAX_WIRING_LINES = 5


def _read_repo_file(relative: str) -> str:
    """Read a file from the repo checkout; a missing file fails the test (no silent skip)."""
    path = REPO_ROOT / relative
    assert path.is_file(), f"{relative} is missing from the repo checkout at {REPO_ROOT}"
    return path.read_text(encoding="utf-8")


def _front_matter(text: str) -> str:
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "front matter block delimited by --- lines is missing"
    return match.group(1)


def _section_body(text: str, heading: str) -> str:
    lines = text.splitlines()
    assert heading in lines, f"heading {heading!r} not found"
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


class TestSteeringSync:
    """Req 1.9, 9.1, 9.4, 9.5: steering and AGENTS.md stay in sync with the Budget_Module."""

    @pytest.fixture(scope="class")
    def steering(self) -> str:
        return _read_repo_file(STEERING_PATH)

    @pytest.fixture(scope="class")
    def wiring_section(self) -> str:
        return _section_body(_read_repo_file(AGENTS_PATH), WIRING_HEADING)

    def test_steering_always_included(self, steering):
        assert re.search(r"^inclusion:\s*always\s*$", _front_matter(steering), re.MULTILINE)

    @pytest.mark.parametrize(
        "name",
        ["TOTAL_BUDGET", "TOOL_DESCRIPTION_CAP", "PARAMETER_DESCRIPTION_CAP", "ROUTING_GUIDE_CAP"],
    )
    def test_steering_states_limit(self, steering, name):
        value = f"{getattr(tool_budget, name):,}"
        assert value in steering, f"{STEERING_PATH} does not state {name} = {value}"

    @pytest.mark.parametrize(
        "path", ["src/mcp_server_for_oscal/tools/tool_budget.py", "tests/test_tool_budget.py"]
    )
    def test_steering_names_source_files(self, steering, path):
        assert path in steering

    def test_wiring_section_is_short(self, wiring_section):
        content = [
            line
            for line in wiring_section.splitlines()
            if line.strip() and not re.fullmatch(r"\s*<!--.*-->\s*", line)
        ]
        assert len(content) <= MAX_WIRING_LINES, content

    def test_wiring_section_links_steering(self, wiring_section):
        assert re.search(rf"\]\((?:\./)?{re.escape(STEERING_PATH)}\)", wiring_section)
