"""Tests for the get_allowed_values tool (Feature: metaschema-constraints).

Smoke tests from task 9.1 and required example tests from task 9.2. The property tests
(tasks 9.3-9.10) extend this module.
"""

import asyncio
import inspect
import json
import re
import tempfile
from functools import cache
from pathlib import Path
from typing import Annotated, get_args, get_origin
from unittest.mock import patch
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

import pytest
from hypothesis import HealthCheck, assume, event, example, given, settings
from hypothesis import strategies as st
from mcp.server.mcpserver import Context, MCPServer

from mcp_server_for_oscal.tools import get_allowed_values as gav_module
from mcp_server_for_oscal.tools.descriptions import ROUTING_GUIDE, TARGET_SELECTOR_DOC, ParamDoc
from mcp_server_for_oscal.tools.get_allowed_values import (
    _bounded_levenshtein,
    get_allowed_values,
    suggest_definition_names,
)
from mcp_server_for_oscal.tools.metaschema_index import (
    Constraint,
    Definition,
    EnumValue,
    MetaschemaIndex,
    MetaschemaLoadError,
    ModelScope,
    get_index,
)
from mcp_server_for_oscal.tools.metaschema_paths import (
    PathAlternative,
    compose_selector,
    validate_selector,
)
from mcp_server_for_oscal.tools.tool_budget import summary_sentence
from mcp_server_for_oscal.tools.utils import (
    MAX_PAGE_LIMIT,
    RESPONSE_BUDGET,
    OSCALModelType,
    paginate,
)

from .test_metaschema_index import (
    CAT,
    ENT,
    PROF,
    _doctype,
    _ent_decl,
    _imports,
    _line_of,
    _mod,
    _module,
    _ns_ent,
    _write_bundle,
)

OSCAL_NS = "http://csrc.nist.gov/ns/oscal"
SERVICE_ENT = "shared-constraints/allowed-values-component_component_service.ent"
SERVICE_PREDICATE = "@type='service'"
SERVICE_CONDITION = {"step": "(.)", "predicate": SERVICE_PREDICATE}
PAGE_KEYS = {"items", "total", "offset", "limit", "hasMore"}
REQUIRED_ITEM_KEYS = {
    "value",
    "description",
    "allow-other",
    "definition",
    "target",
    "level",
    "source-file",
    "line",
}


def _all_items(model: str, target: str) -> list[dict]:
    """Walk every page of a result and return the items in order."""
    items: list[dict] = []
    offset = 0
    while True:
        page = get_allowed_values(model=model, target=target, offset=offset, limit=100)
        items.extend(page["items"])
        if not page["hasMore"]:
            return items
        offset += len(page["items"])


def _assert_line_has_enum(item: dict) -> None:
    """The item's source-file/line points at the bundle's ``<enum ... value="...">`` line.

    ``.ent`` files declare ``xmlns`` on each ``<enum>`` before ``value``, so the attributes are
    matched separately.
    """
    line = get_index().source_lines(item["source-file"])[item["line"] - 1]
    assert "<enum " in line
    # The XML-escaped form of the value (Property 6), as written in a double-quoted attribute.
    assert f' value="{xml_escape(item["value"], {'"': "&quot;"})}"' in line


@pytest.mark.usefixtures("reset_metaschema_index")
class TestRequiredExamples:
    """Required example tests against the bundled OSCAL 1.2.3 Metaschema (Req 7.6, 7.7).

    Not skipped or marked, so a failure fails ``hatch run tests`` (Req 7.10).
    """

    def test_defined_component_prop_name_has_asset_type(self):
        """Req 7.6: asset-type is allowed on defined-component props in the OSCAL namespace."""
        page = get_allowed_values(
            model="component-definition", target="defined-component/prop/@name"
        )
        assert set(page) >= PAGE_KEYS
        items = _all_items("component-definition", "defined-component/prop/@name")
        for item in items:
            assert set(item) >= REQUIRED_ITEM_KEYS
            assert item["definition"] == "defined-component"
            assert None not in item.values()
        asset = [i for i in items if i["value"] == "asset-type" and i.get("namespace") == OSCAL_NS]
        assert asset, "asset-type with the OSCAL namespace missing"
        for item in asset:
            _assert_line_has_enum(item)

    def test_ssp_system_component_type_has_this_system(self):
        """Req 7.7: this-system is allowed for SSP system-component/@type."""
        items = _all_items("system-security-plan", "system-component/@type")
        this_system = [i for i in items if i["value"] == "this-system"]
        assert this_system
        # Matched through the referenced global flag definition (RC-2).
        assert all(i["flag-definition"] == "system-component-type" for i in this_system)
        for item in this_system:
            _assert_line_has_enum(item)

    def test_defined_component_type_excludes_this_system(self):
        """Req 7.7: this-system is not allowed for cdef defined-component/@type."""
        items = _all_items("component-definition", "defined-component/@type")
        assert items
        assert "this-system" not in {i["value"] for i in items}
        assert {i["flag-definition"] for i in items} == {"defined-component-type"}

    def test_link_rel_service_values_are_conditional(self):
        """Req 7.7: provided-by/used-by carry the @type='service' condition; uses-network not."""
        items = _all_items("component-definition", "defined-component/link/@rel")
        for value in ("provided-by", "used-by"):
            matches = [i for i in items if i["value"] == value]
            assert matches, f"{value} missing"
            for item in matches:
                assert item["source-file"] == SERVICE_ENT
                assert SERVICE_CONDITION in item["conditions"]
                _assert_line_has_enum(item)
        uses_network = [i for i in items if i["value"] == "uses-network"]
        assert uses_network
        assert any(
            all(c["predicate"] != SERVICE_PREDICATE for c in i.get("conditions", []))
            for i in uses_network
        )


@pytest.mark.usefixtures("reset_metaschema_index")
class TestBundledExamples:
    """Smoke checks against the bundled OSCAL 1.2.3 Metaschema."""

    def test_items_sorted_by_source_location(self):
        # Constraint order cannot be read from items, but within one source file the enum
        # lines of a single constraint must ascend.
        items = _all_items("component-definition", "defined-component/link/@rel")
        assert items
        by_file: dict[tuple[str, str], list[int]] = {}
        for item in items:
            by_file.setdefault((item["source-file"], item["target"]), []).append(item["line"])
        for lines in by_file.values():
            assert lines == sorted(lines)

    def test_no_match_returns_suggested_targets(self):
        page = get_allowed_values(model="component-definition", target="defined-component/nope")
        assert page["items"] == []
        assert page["total"] == 0
        assert page["hasMore"] is False
        suggested = page["suggested-targets"]
        assert suggested == sorted(set(suggested))
        assert 0 < len(suggested) <= 20
        assert "defined-component/@type" in suggested
        for selector in suggested:
            assert selector.startswith("defined-component")
            assert get_allowed_values(model="component-definition", target=selector)["total"]


def _bundle_selectors(index: MetaschemaIndex) -> list[tuple[str, str]]:
    """Every ``(model, selector)`` pair Requirement 7.8 names, deduplicated, in stable order.

    Per model scope: ``compose_selector(name, path)`` for every non-cross-document path
    alternative of every ``allowed-values`` Constraint on every Definition, and
    ``<name>/@<flag>`` for every Flag_Slot's effective name. Selectors that fail
    ``validate_selector`` (e.g. over-long) are left out, since the tool rejects them.
    """
    pairs: dict[tuple[str, str], None] = {}
    for model in OSCALModelType:
        scope = index.scope(model)
        for name in scope.definition_names():
            for definition in scope.definitions(name):
                candidates = [
                    compose_selector(name, alt.path)
                    for constraint in definition.constraints
                    if constraint.type == "allowed-values"
                    for alt in constraint.alternatives
                    if not alt.cross_document and alt.path is not None
                ]
                candidates += [f"{name}/@{slot.effective_name}" for slot in definition.flags]
                for selector in candidates:
                    try:
                        validate_selector(selector)
                    except ValueError:
                        continue
                    pairs[model.value, selector] = None
    return list(pairs)


@pytest.fixture(scope="module")
def bundle_selectors() -> list[tuple[str, str]]:
    """The Requirement 7.8 selector set, built once per module from the real bundle."""
    return _bundle_selectors(MetaschemaIndex.load())


@pytest.mark.usefixtures("reset_metaschema_index")
class TestCitationProperty:
    """Property 6: every citation points at its value.

    Requirement 7.8 quantifies over every bundle selector, so the check is exhaustive
    rather than sampled. Not skipped or marked, so a failure fails ``hatch run tests``.

    **Validates: Requirements 4.5, 6.12, 7.8**
    """

    def test_every_citation_points_at_its_value(self, bundle_selectors):
        index = get_index()
        sources = set(index.source_files())
        checked = 0
        selectors_with_items = 0
        for model, selector in bundle_selectors:
            items = _all_items(model, selector)
            selectors_with_items += bool(items)
            for item in items:
                context = f"{model} {selector}: {item['value']!r}"
                assert item["source-file"] in sources, context
                lines = index.source_lines(item["source-file"])
                assert 1 <= item["line"] <= len(lines), context
                _assert_line_has_enum(item)
                checked += 1
        # Guard against a vacuous pass (e.g. an empty selector set or index).
        assert selectors_with_items > 100
        assert checked > 1000

    def test_selector_set_covers_flag_and_path_forms(self, bundle_selectors):
        selectors = {s for _, s in bundle_selectors}
        assert {m for m, _ in bundle_selectors} == {m.value for m in OSCALModelType}
        assert "system-component/@type" in selectors  # flag-slot form (RC-2)
        assert "defined-component/prop/@name" in selectors  # constraint-path form
        assert "defined-component/link/@rel" in selectors


@cache
def _selector_totals() -> dict[tuple[str, str], int]:
    """``total`` for each Requirement 7.8 selector that returns items, built once per process.

    Hypothesis strategies cannot take pytest fixtures, so this mirrors ``bundle_selectors``.
    Uses a private ``MetaschemaIndex.load()`` so it does not depend on the ``get_index()``
    singleton's state.
    """
    index = MetaschemaIndex.load()
    totals: dict[tuple[str, str], int] = {}
    with patch.object(gav_module, "get_index", return_value=index):
        for model, selector in _bundle_selectors(index):
            total = get_allowed_values(model=model, target=selector, limit=1)["total"]
            if total:
                totals[model, selector] = total
    return totals


# Results this large are the only ones whose 100-item pages exceed the budget, and they
# are a few dozen of ~500 selectors, so they get their own share of the examples.
_LARGE_RESULT = 40


def _paging_selectors() -> st.SearchStrategy[tuple[str, str]]:
    totals = _selector_totals()
    large = [pair for pair, total in totals.items() if total >= _LARGE_RESULT]
    return st.one_of(st.sampled_from(list(totals)), st.sampled_from(large))


@cache
def _full_result(model: str, target: str) -> tuple[str, ...]:
    """The ``limit=100`` walk from offset 0, as serialized items (cached per selector)."""
    return tuple(json.dumps(item, sort_keys=True) for item in _all_items(model, target))


@pytest.mark.usefixtures("reset_metaschema_index")
class TestPagingProperty:
    """Property 9: paging walks the full result exactly once.

    Selectors are sampled from the Requirement 7.8 set (only those with items; empty
    results are covered by ``test_no_match_returns_suggested_targets``), with extra weight
    on large results so budget-reduced pages occur. The reference is the ``limit=100`` walk
    from offset 0, so the check also covers agreement across limits.

    **Validates: Requirements 5.12, 7.9, 12.3, 12.6, 12.7**
    """

    @settings(
        max_examples=100,
        deadline=None,
        # The index singleton is deliberately shared across examples (read-only).
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(
        pair=st.deferred(_paging_selectors),
        limit=st.one_of(st.integers(1, MAX_PAGE_LIMIT), st.integers(60, MAX_PAGE_LIMIT)),
        data=st.data(),
    )
    def test_paging_walks_full_result_exactly_once(self, pair, limit, data):
        model, target = pair
        full = _full_result(model, target)
        total = len(full)
        assert total == _selector_totals()[pair]
        start = data.draw(st.integers(0, total + 3), label="offset")

        walked: list[str] = []
        offset = start
        reduced = False
        while True:
            page = get_allowed_values(model=model, target=target, offset=offset, limit=limit)
            context = f"{model} {target} offset={offset} limit={limit}"
            assert set(page) >= PAGE_KEYS, context
            assert len(json.dumps(page)) <= RESPONSE_BUDGET, context  # 12.3
            assert page["total"] == total, context
            assert page["offset"] == offset, context
            items = page["items"]
            if offset >= total:  # 12.7
                assert items == [], context
                assert page["hasMore"] is False, context
                break
            assert items, context  # 12.6: every page progresses
            assert len(items) <= limit, context
            if page["limit"] != limit:  # 12.4: reduced limit reports the items returned
                assert page["limit"] == len(items) < limit, context
                reduced = True
            assert page["hasMore"] == (offset + len(items) < total), context
            walked.extend(json.dumps(item, sort_keys=True) for item in items)
            if not page["hasMore"]:
                break
            offset += len(items)

        assert walked == list(full[start:])
        # Each item has a distinct (constraint, enum) location, so equality with a
        # duplicate-free reference rules out repeats and omissions.
        assert len(set(full)) == total
        event(f"budget-reduced={reduced}")
        event(f"past-end={start >= total}")


class TestArgumentRejection:
    """Invalid arguments raise ValueError before the index is read."""

    @pytest.mark.parametrize(
        ("model", "target", "match"),
        [
            pytest.param("not-a-model", "defined-component", "list_oscal_models", id="model"),
            pytest.param("component-definition", "", "must not be empty", id="empty"),
            pytest.param("component-definition", "a/[x]", "predicates", id="predicate"),
            pytest.param("component-definition", "/a", "start with", id="leading-slash"),
            pytest.param("component-definition", "a///b", "///", id="triple-slash"),
            pytest.param("component-definition", "a b", "whitespace", id="whitespace"),
        ],
    )
    def test_rejected_without_index_access(self, model, target, match):
        with (
            patch.object(gav_module, "get_index") as get_index,
            patch.object(gav_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match=match),
        ):
            get_allowed_values(model=model, target=target)
        get_index.assert_not_called()
        notify.assert_called_once()

    @pytest.mark.parametrize(("offset", "limit"), [(-1, 25), (0, 0), (0, 101)])
    def test_bad_page_arguments_raise_paginate_error(self, offset, limit):
        with (
            patch.object(gav_module, "get_index") as get_index,
            pytest.raises(ValueError, match=r"offset|limit"),
        ):
            get_allowed_values(
                model="component-definition",
                target="defined-component",
                offset=offset,
                limit=limit,
            )
        get_index.assert_not_called()


_MODEL_VALUES = sorted(m.value for m in OSCALModelType)
_SHOWN_CHARS = 80  # messages name the model or selector up to this many characters


def _near_miss_models() -> st.SearchStrategy[str]:
    """Strings one small edit away from a model name: case, padding, separators, length."""
    edits = [
        str.upper,
        str.title,
        lambda m: m[0].upper() + m[1:],
        lambda m: f" {m}",
        lambda m: f"{m} ",
        lambda m: f"{m}\n",
        lambda m: m.replace("-", "_"),
        lambda m: m.replace("-", ""),
        lambda m: m[:-1],
        lambda m: f"{m}s",
        lambda m: f"oscal-{m}",
    ]
    return st.builds(
        lambda m, edit: edit(m), st.sampled_from(_MODEL_VALUES), st.sampled_from(edits)
    )


_invalid_models = st.one_of(_near_miss_models(), st.text(max_size=200)).filter(
    lambda m: m not in _MODEL_VALUES
)

_name = st.from_regex(r"[a-z][a-z0-9-]{0,24}", fullmatch=True)
_step = st.one_of(_name, _name.map(lambda n: f"@{n}"))
_valid_selectors = st.one_of(
    st.sampled_from(
        ["defined-component", "defined-component/prop/@name", "part//part/@name", "x/@y"]
    ),
    st.builds(
        lambda head, rest: head + "".join(sep + step for sep, step in rest),
        _name,
        st.lists(st.tuples(st.sampled_from(["/", "//"]), _step), max_size=6),
    ),
)
# Every character ``str.isspace`` accepts (about 30), so draws never need a filter.
_whitespace = st.sampled_from([chr(c) for c in range(0x110000) if chr(c).isspace()])


@st.composite
def _invalid_selector(draw) -> tuple[str, str]:
    """A Target_Selector that breaks exactly one rule, and a phrase from that rule's message.

    Each construction breaks only its own rule, or one that ``validate_selector`` checks
    earlier, so the expected rule is the one reported.
    """
    base = draw(_valid_selectors)
    rule = draw(st.sampled_from(["empty", "long", "space", "lead", "trail", "triple", "forbidden"]))
    if rule == "empty":
        return "", "must not be empty"
    if rule == "long":  # length is checked right after emptiness
        # Valid selectors are under 200 characters, so the padding always crosses 512.
        return base + "a" * draw(st.integers(513 - len(base), 900 - len(base))), "at most 512"
    if rule == "space":  # whitespace is checked before the '/' rules
        pos = draw(st.integers(0, len(base)))
        return base[:pos] + draw(_whitespace) + base[pos:], "whitespace"
    if rule == "lead":
        return "/" + base, "must not start with '/'"
    if rule == "trail":
        return base + "/", "must not end with '/'"
    if rule == "triple":
        return base + "///" + draw(_valid_selectors), "'///'"
    # A non-'/' character inserted anywhere cannot create a leading/trailing '/' or '///'.
    pos = draw(st.integers(0, len(base)))
    return base[:pos] + draw(st.sampled_from("[]()|")) + base[pos:], "predicates or unions"


_valid_offsets = st.integers(0, 10_000)
_valid_limits = st.integers(1, MAX_PAGE_LIMIT)
_invalid_offsets = st.integers(max_value=-1)
_invalid_limits = st.one_of(st.integers(max_value=0), st.integers(min_value=MAX_PAGE_LIMIT + 1))


def _call_rejected(model: str, target: str, offset: int, limit: int) -> tuple[str, list[str]]:
    """Call the tool with the index unavailable; return the error and the notified messages."""
    with (
        patch.object(gav_module, "get_index") as index,
        patch.object(gav_module, "try_notify_client_error") as notify,
        pytest.raises(ValueError) as exc,
    ):
        get_allowed_values(model=model, target=target, offset=offset, limit=limit)
    index.assert_not_called()
    return str(exc.value), [c.args[0] for c in notify.call_args_list]


def _assert_names_value(message: str, value: str) -> None:
    """The message names ``value``, truncated to its first 80 characters."""
    if len(value) <= _SHOWN_CHARS:
        assert f"'{value}'" in message
    else:
        assert f"'{value[:_SHOWN_CHARS]}...'" in message
        assert value not in message


class TestArgumentRejectionProperty:
    """Property 3: invalid arguments are rejected before the index is read.

    The other arguments are drawn valid or invalid, which also checks the design's order:
    the model is checked first, then the selector, then paging (all before ``get_index``).

    **Validates: Requirements 5.9, 5.11, 5.12, 12.2**
    """

    @settings(max_examples=200, deadline=None)
    @given(
        model=_invalid_models,
        target=st.one_of(_valid_selectors, _invalid_selector().map(lambda p: p[0])),
        offset=st.one_of(_valid_offsets, _invalid_offsets),
        limit=st.one_of(_valid_limits, _invalid_limits),
    )
    def test_invalid_model_rejected_first(self, model, target, offset, limit):
        message, notified = _call_rejected(model, target, offset, limit)
        assert message.startswith("Invalid model: ")
        assert "list_oscal_models" in message
        _assert_names_value(message, model)
        assert notified == [message]

    @settings(max_examples=200, deadline=None)
    @given(
        model=st.sampled_from(_MODEL_VALUES),
        selector=_invalid_selector(),
        offset=st.one_of(_valid_offsets, _invalid_offsets),
        limit=st.one_of(_valid_limits, _invalid_limits),
    )
    def test_invalid_selector_rejected_before_paging(self, model, selector, offset, limit):
        target, rule = selector
        message, notified = _call_rejected(model, target, offset, limit)
        assert message.startswith("Invalid target selector ")
        assert rule in message
        _assert_names_value(message, target)
        assert notified == [message]
        event(f"rule={rule}")

    @settings(max_examples=200, deadline=None)
    @given(
        model=st.sampled_from(_MODEL_VALUES),
        target=_valid_selectors,
        page=st.one_of(
            st.tuples(_invalid_offsets, st.one_of(_valid_limits, _invalid_limits)),
            st.tuples(_valid_offsets, _invalid_limits),
        ),
    )
    def test_invalid_page_raises_paginate_error(self, model, target, page):
        offset, limit = page
        validate_selector(target)  # the strategy only yields valid selectors
        with pytest.raises(ValueError) as expected:
            paginate([], offset, limit)
        message, notified = _call_rejected(model, target, offset, limit)
        assert message == str(expected.value)
        # The design only requires notification for model and selector errors; any
        # notification sent must carry the raised message.
        assert set(notified) <= {message}


@pytest.mark.usefixtures("reset_metaschema_index")
class TestUnknownDefinition:
    def test_unknown_definition_suggests_names(self):
        with (
            patch.object(gav_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match="Unknown definition 'defined-componnet'") as exc,
        ):
            get_allowed_values(model="component-definition", target="defined-componnet/@type")
        notify.assert_called_once()
        assert "defined-component" in str(exc.value).split("Nearest definition names: ")[1]

    def test_long_segment_is_truncated_in_message(self):
        segment = "x" * 200
        with pytest.raises(ValueError, match="Unknown definition") as exc:
            get_allowed_values(model="component-definition", target=segment)
        assert "x" * 81 not in str(exc.value)


class TestSuggestionHelpers:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [("", "", 0), ("abc", "", 3), ("kitten", "sitting", 3), ("same", "same", 0)],
    )
    def test_bounded_levenshtein_exact_within_cutoff(self, a, b, expected):
        assert _bounded_levenshtein(a, b, 10) == expected

    def test_bounded_levenshtein_reports_over_cutoff(self):
        assert _bounded_levenshtein("kitten", "sitting", 2) == 3
        assert _bounded_levenshtein("a", "abcdef", 2) == 3

    def test_suggestions_ordered_by_distance_then_name(self):
        names = ("bb", "ab", "abc", "zzzz", "AB")
        # "ab" vs "AB" both distance 0 case-insensitively; ties sort by name ("AB" < "ab").
        assert suggest_definition_names("ab", names)[:3] == ["AB", "ab", "abc"]

    def test_suggestions_capped_at_ten(self):
        names = tuple(f"name-{i:02d}" for i in range(30))
        assert len(suggest_definition_names("name", names)) == 10


# --------------------------------------------------------------------------------------
# Property 4: get_allowed_values equals a brute-force oracle
# --------------------------------------------------------------------------------------

# (value, source-file, line, flag-definition) of one Allowed_Value_Item.
_OracleItem = tuple[str, str, int, str | None]


def _split_selector(selector: str) -> tuple[str, str]:
    """``(first segment, remainder)``: a single leading ``/`` is dropped, ``//`` is kept (5.2)."""
    name, _, rest = selector.partition("/")
    return name, ("/" + rest if rest.startswith("/") else rest)


def _allowed_values_on(definition: Definition, path: str) -> list[Constraint]:
    """``allowed-values`` Constraints on ``definition`` with a same-document ``path`` alternative."""
    return [
        c
        for c in definition.constraints
        if c.type == "allowed-values"
        and any(not alt.cross_document and alt.path == path for alt in c.alternatives)
    ]


def _flag_slot_definitions(
    scope: ModelScope, definition: Definition, flag: str
) -> list[Definition]:
    """Flag Definitions behind ``definition``'s Flag_Slots named ``flag`` (5.4, RC-2)."""
    found: list[Definition] = []
    for slot in definition.flags:
        if slot.effective_name != flag:
            continue
        if slot.inline is not None:
            found += [d for d in scope.definitions(slot.inline.name) if d.key == slot.inline]
        elif slot.ref is not None:
            found += scope.global_definition("flag", slot.ref)
    return found


# (constraint, flag-definition, matched same-document alternatives) of one matched Constraint.
_OracleMatch = tuple[Constraint, str | None, tuple[PathAlternative, ...]]


def _oracle_matches(scope: ModelScope, selector: str) -> list[_OracleMatch] | None:
    """Matched constraints for ``selector`` by a direct scan of ``scope``; None for an unknown name.

    Walks every Definition named by the first segment (global and local, 5.6), its matching
    constraints (5.2, 5.3), and for an ``@flag`` remainder the flag Definitions behind its
    Flag_Slots (5.4). Deduplicates by constraint identity (5.5) and sorts by the constraint's
    Source_Location (5.7).
    """
    name, remainder = _split_selector(selector)
    definitions = scope.definitions(name)
    if not definitions:
        return None
    # constraint id -> (constraint, flag-definition, path it matched through)
    matched: dict[int, tuple[Constraint, str | None, str]] = {}
    for definition in definitions:
        for constraint in _allowed_values_on(definition, remainder):
            matched.setdefault(id(constraint), (constraint, None, remainder))
    if remainder.startswith("@") and "/" not in remainder:
        for definition in definitions:
            for flag_def in _flag_slot_definitions(scope, definition, remainder[1:]):
                for constraint in _allowed_values_on(flag_def, ""):
                    prior = matched.setdefault(id(constraint), (constraint, flag_def.key.name, ""))
                    # A constraint reached both directly and through a flag slot would make
                    # ``flag-definition`` ambiguous; the requirements do not cover it.
                    assert prior[1] == flag_def.key.name, f"{selector}: direct and flag match"
    ordered = sorted(
        matched.values(), key=lambda m: (m[0].location.source_file, m[0].location.line)
    )
    keys = [(c.location.source_file, c.location.line) for c, _, _ in ordered]
    # 5.7 leaves the order of two constraints at one Source_Location open.
    assert len(set(keys)) == len(keys), f"{selector}: constraints share a Source_Location"
    return [
        (c, flag_def_name, tuple(a for a in c.alternatives if not a.cross_document and a.path == p))
        for c, flag_def_name, p in ordered
    ]


def _oracle(scope: ModelScope, selector: str) -> list[_OracleItem] | None:
    """Expected items for ``selector``: each matched constraint's enums in document order (5.7)."""
    matches = _oracle_matches(scope, selector)
    if matches is None:
        return None
    return [
        (enum.value, enum.location.source_file, enum.location.line, flag_def_name)
        for constraint, flag_def_name, _ in matches
        for enum in constraint.enums
    ]


def _check_against_oracle(scope: ModelScope, model: str, selector: str) -> list[_OracleItem]:
    """Assert the tool's full result for ``selector`` equals the oracle; return it."""
    expected = _oracle(scope, selector)
    if expected is None:
        with pytest.raises(ValueError, match="Unknown definition"):
            get_allowed_values(model=model, target=selector)
        return []
    first = _all_items(model, selector)
    assert _all_items(model, selector) == first, f"{model} {selector}: not deterministic"
    name, _ = _split_selector(selector)
    assert {item["definition"] for item in first} <= {name}
    got = [
        (item["value"], item["source-file"], item["line"], item.get("flag-definition"))
        for item in first
    ]
    assert got == expected, f"{model} {selector}"
    return got


@cache
def _real_selector_pools() -> tuple[tuple[tuple[str, str], ...], ...]:
    """Real-bundle ``(model, selector)`` pools: 7.8 selectors and bare definition names."""
    index = MetaschemaIndex.load()
    bare = []
    for model in OSCALModelType:
        for name in index.scope(model).definition_names():
            try:
                validate_selector(name)
            except ValueError:
                continue
            bare.append((model.value, name))
    return tuple(_bundle_selectors(index)), tuple(bare)


def _real_selectors() -> st.SearchStrategy[tuple[str, str]]:
    selectors, bare = _real_selector_pools()
    with_items = list(_selector_totals())
    # Real definition names with random remainders, which almost never match.
    random_remainder = st.builds(
        lambda pair, rest: (pair[0], pair[1] + "".join(sep + step for sep, step in rest)),
        st.sampled_from(bare),
        st.lists(st.tuples(st.sampled_from(["/", "//"]), _step), min_size=1, max_size=4),
    )
    return st.one_of(
        st.sampled_from(with_items),
        st.sampled_from(selectors),
        st.sampled_from(bare),
        random_remainder,
    )


# Synthetic bundles: a catalog module importing a second module, both declaring definitions
# from small name pools so that same-named global and local definitions, inline and
# referenced flags with use-name, and unions with several matching alternatives all occur.
_SYN_DEFS = ("a", "b")
_SYN_FLAGS = ("f", "g")  # disjoint from _SYN_DEFS, see the ambiguity note in _oracle
_SYN_USE_NAMES = ("f", "g", "h")
_SYN_TARGETS = (
    None,
    ".",
    ".|(.)",
    "@f",
    "@g",
    "@h",
    "@f|@f",
    "(.)[@f='x']/@g",
    "b",
    "b/@f",
    "b[@x='1']/@f|b/@f",
    "a//b/@g",
    ".//b/@f",
    "doc('o.xml')/@f",
    "@g|doc('o.xml')/@g",
)
_SYN_REMAINDERS = ("", "@f", "@g", "@h", "@x", "b", "b/@f", "b/@g", "//b/@f", "a//b/@g")
_SYN_X = _mod("x")
_SYN_DOCTYPE = _doctype(_ent_decl("e"))
_SYN_ENT = _ns_ent("\n<enum value='e1'/>\n<enum value='e2'/>\n")


# Flag Definitions match only through an empty path (5.4), so their targets favor it.
_SYN_FLAG_TARGETS = (None, ".", ".|(.)", "(.)[.='x']", "doc('o.xml')", "@f")


@st.composite
def _syn_allowed_values(draw: st.DrawFn, targets: tuple[str | None, ...]) -> str:
    target = draw(st.sampled_from(targets))
    attr = "" if target is None else f" target={quoteattr(target)}"
    enums = draw(
        st.lists(st.sampled_from(['<enum value="v1"/>', '<enum value="v2"/>', "&e;"]), max_size=3)
    )
    return "\n".join([f"<allowed-values{attr}>", *enums, "</allowed-values>"])


@st.composite
def _syn_constraints(draw: st.DrawFn, targets: tuple[str | None, ...] = _SYN_TARGETS) -> str:
    other = st.just('<matches target="@f" datatype="token"/>')  # not allowed-values
    parts = draw(st.lists(st.one_of(_syn_allowed_values(targets), other), max_size=3))
    return "\n".join(["<constraint>", *parts, "</constraint>"]) if parts else ""


@st.composite
def _syn_flag_slot(draw: st.DrawFn) -> str:
    use = draw(st.none() | st.sampled_from(_SYN_USE_NAMES))
    use_name = "" if use is None else f"<use-name>{use}</use-name>"
    name = draw(st.sampled_from(_SYN_FLAGS))
    if draw(st.booleans()):
        constraints = draw(_syn_constraints(_SYN_FLAG_TARGETS))
        return f'<define-flag name="{name}">{use_name}\n{constraints}\n</define-flag>'
    return f'<flag ref="{name}">{use_name}</flag>'


@st.composite
def _syn_definition(draw: st.DrawFn, *, local: bool = False) -> str:
    kind = draw(st.sampled_from(["assembly", "field"]))
    parts = [f'<define-{kind} name="{draw(st.sampled_from(_SYN_DEFS))}">']
    parts += draw(st.lists(_syn_flag_slot(), max_size=3))
    if kind == "assembly" and not local:
        children = draw(st.lists(_syn_definition(local=True), max_size=2))
        if children:
            parts += ["<model>", *children, "</model>"]
    parts += [draw(_syn_constraints()), f"</define-{kind}>"]
    return "\n".join(parts)


@st.composite
def _syn_global_flag(draw: st.DrawFn) -> str:
    name = draw(st.sampled_from(_SYN_FLAGS))
    return (
        f'<define-flag name="{name}">\n{draw(_syn_constraints(_SYN_FLAG_TARGETS))}\n</define-flag>'
    )


_syn_module_body = st.lists(st.one_of(_syn_definition(), _syn_global_flag()), max_size=4).map(
    "\n".join
)


def _syn_selectors(scope: ModelScope) -> list[str]:
    """Every pool name with every pool remainder and every path alternative in the scope."""
    remainders = set(_SYN_REMAINDERS)
    for name in scope.definition_names():
        for definition in scope.definitions(name):
            for constraint in definition.constraints:
                remainders.update(a.path for a in constraint.alternatives if a.path is not None)
    selectors = set()
    for name in (*_SYN_DEFS, *_SYN_FLAGS, "zz"):
        for remainder in remainders:
            selector = compose_selector(name, remainder)
            try:
                validate_selector(selector)
            except ValueError:
                continue
            selectors.add(selector)
    return sorted(selectors)


@pytest.mark.usefixtures("reset_metaschema_index")
class TestMatchingOracleProperty:
    """Property 4: get_allowed_values equals a brute-force oracle.

    The oracle scans the Model_Scope's Definitions, Constraints, and Flag_Slots directly and
    is compared with every page of the tool's result, in order; two calls must agree.

    **Validates: Requirements 5.2, 5.3, 5.4, 5.5, 5.6, 5.7**
    """

    @settings(
        max_examples=300,
        deadline=None,
        # The index singleton is deliberately shared across examples (read-only).
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(pair=st.deferred(_real_selectors))
    def test_real_bundle_matches_oracle(self, pair):
        model, selector = pair
        got = _check_against_oracle(get_index().scope(OSCALModelType(model)), model, selector)
        event(f"items={'yes' if got else 'no'}")
        event(f"flag-definition={any(fd is not None for *_, fd in got)}")

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(cat=_syn_module_body, x=_syn_module_body)
    def test_synthetic_bundles_match_oracle(self, cat, x):
        files: dict[str, str | bytes] = {
            CAT: _module(_imports(_SYN_X) + "\n" + cat, _SYN_DOCTYPE),
            _SYN_X: _module(x, _SYN_DOCTYPE),
            ENT: _SYN_ENT,
        }
        with tempfile.TemporaryDirectory() as tmp:
            _write_bundle(Path(tmp), files)
            index = MetaschemaIndex.load(Path(tmp))
            assert dict(index.load_errors) == {}
            scope = index.scope(OSCALModelType.CATALOG)
            matched = flagged = 0
            with patch.object(gav_module, "get_index", return_value=index):
                for selector in _syn_selectors(scope):
                    got = _check_against_oracle(scope, "catalog", selector)
                    matched += bool(got)
                    flagged += any(fd is not None for *_, fd in got)
        event(f"selectors-with-items>0={matched > 0}")
        event(f"flag-matches>0={flagged > 0}")
        event(f"same-name-defs={any(len(scope.definitions(n)) > 1 for n in _SYN_DEFS)}")


_EXAMPLE_BODY = """\
<define-assembly name="a">
<flag ref="g"><use-name>type</use-name></flag>
<define-flag name="f">
<constraint>
<allowed-values>
<enum value="inline"/>
</allowed-values>
</constraint>
</define-flag>
<model>
<define-assembly name="a">
<constraint>
<allowed-values target="b/@f|b[@x='1']/@f">
<enum value="local"/>
</allowed-values>
</constraint>
</define-assembly>
</model>
<constraint>
<allowed-values target="b/@f">
<enum value="global"/>
</allowed-values>
<allowed-values>
<enum value="self"/>
</allowed-values>
<allowed-values target="b//c/@f">
<enum value="desc"/>
</allowed-values>
<allowed-values target="doc('o.xml')/@type">
<enum value="cross"/>
</allowed-values>
</constraint>
</define-assembly>
<define-flag name="g">
<constraint>
<allowed-values target=".">
<enum value="ref"/>
</allowed-values>
</constraint>
</define-flag>"""


class TestMatchingOracleExamples:
    """Fixed expectations that also check the oracle itself (Requirements 5.2-5.7)."""

    @pytest.mark.parametrize(
        ("selector", "expected"),
        [
            # Local before global (Source_Location order); the union matches once (5.5, 5.6).
            pytest.param("a/b/@f", [("local", None), ("global", None)], id="same-name-union"),
            pytest.param("a", [("self", None)], id="bare"),  # no target attribute (5.3)
            pytest.param("a/b//c/@f", [("desc", None)], id="descendant"),  # RC-3
            pytest.param("a/@type", [("ref", "g")], id="flag-ref-use-name"),  # RC-2
            pytest.param("a/@f", [("inline", "f")], id="inline-flag"),
            pytest.param("a/@g", [], id="ref-hidden-by-use-name"),
        ],
    )
    def test_example(self, selector, expected):
        text = _module(_EXAMPLE_BODY).decode()
        with tempfile.TemporaryDirectory() as tmp:
            _write_bundle(Path(tmp), {CAT: text})
            index = MetaschemaIndex.load(Path(tmp))
            scope = index.scope(OSCALModelType.CATALOG)
            with patch.object(gav_module, "get_index", return_value=index):
                got = _check_against_oracle(scope, "catalog", selector)
        assert got == [
            (value, CAT, _line_of(text, f'value="{value}"'), flag_def)
            for value, flag_def in expected
        ]


# --------------------------------------------------------------------------------------
# Property 5: Allowed_Value_Item shape
# --------------------------------------------------------------------------------------

_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFORMATIONAL", "DEBUG")
_HYPHENATED_KEY = re.compile(r"[a-z]+(?:-[a-z]+)*")


def _assert_plain_json(value: object, where: str) -> None:
    """No None anywhere; every dict key is a hyphenated lowercase string (6.9)."""
    assert value is not None, f"{where}: null"
    if isinstance(value, dict):
        for key, inner in value.items():
            assert isinstance(key, str), f"{where}: key {key!r}"
            assert _HYPHENATED_KEY.fullmatch(key), f"{where}: key {key!r}"
            _assert_plain_json(inner, f"{where}.{key}")
    elif isinstance(value, list):
        for idx, inner in enumerate(value):
            _assert_plain_json(inner, f"{where}[{idx}]")
    else:
        assert isinstance(value, (str, int, bool)), f"{where}: {type(value).__name__}"


def _check_item_shape(
    item: dict,
    definition_name: str,
    match: _OracleMatch,
    enum: EnumValue,
) -> None:
    """``item`` against the Constraint and EnumValue it came from (Property 5)."""
    constraint, flag_def, alternatives = match
    where = f"{definition_name} {item.get('value')!r}"
    namespaces = {alt.namespace for alt in alternatives if alt.namespace is not None}
    optional = {
        "extensible": "extensible" in constraint.attrs,
        "namespace": len(namespaces) == 1,  # 6.4
        # 6.5; with several namespaces each Namespace_Condition is a Condition (6.4).
        "conditions": any(alt.conditions for alt in alternatives) or len(namespaces) > 1,
        "deprecated": enum.deprecated is not None,
        "constraint-id": constraint.id is not None,
        "flag-definition": flag_def is not None,
    }
    assert set(item) == REQUIRED_ITEM_KEYS | {k for k, on in optional.items() if on}, where
    _assert_plain_json(item, where)
    assert json.loads(json.dumps(item)) == item, where

    for key in ("value", "description", "definition", "target", "level", "source-file"):
        assert isinstance(item[key], str), f"{where}: {key}"
    # 6.1, 6.7, 6.8, 6.12
    assert item["value"] == enum.value, where
    expected_description = enum.description.plain() if enum.description is not None else ""
    assert item["description"] == expected_description, where
    assert item["definition"] == definition_name, where
    assert item["target"] == constraint.target, where  # "." when absent, by the index
    assert item["source-file"] == enum.location.source_file, where
    assert type(item["line"]) is int, where  # not bool
    assert item["line"] == enum.location.line >= 1, where
    # 6.2: a real bool, true only for "yes".
    assert item["allow-other"] is (constraint.attrs.get("allow-other") == "yes"), where
    # 6.10
    assert item["level"] == constraint.level, where
    assert item["level"] in _LEVELS, where
    # 6.3, 6.6, 6.11, 6.13: verbatim when present.
    if optional["extensible"]:
        assert item["extensible"] == constraint.attrs["extensible"], where
    if optional["deprecated"]:
        assert item["deprecated"] == enum.deprecated, where
    if optional["constraint-id"]:
        assert item["constraint-id"] == constraint.id, where
    if optional["flag-definition"]:
        assert item["flag-definition"] == flag_def, where
    if optional["namespace"]:
        assert item["namespace"] == next(iter(namespaces)), where
    for condition in item.get("conditions", []):
        assert set(condition) == {"step", "predicate"}, where
        assert all(isinstance(v, str) for v in condition.values()), where


def _check_shapes(scope: ModelScope, model: str, selector: str) -> list[dict]:
    """Check every item of ``selector`` against its oracle-matched source; return the items."""
    matches = _oracle_matches(scope, selector)
    if matches is None:
        return []
    items = _all_items(model, selector)
    sources = [(match, enum) for match in matches for enum in match[0].enums]
    assert len(items) == len(sources), f"{model} {selector}"
    name, _ = _split_selector(selector)
    for item, (match, enum) in zip(items, sources, strict=True):
        assert (item["source-file"], item["line"]) == (
            enum.location.source_file,
            enum.location.line,
        ), f"{model} {selector}"
        _check_item_shape(item, name, match, enum)
    return items


# Synthetic shape bundles: definition "a" with an inline flag "f". Every allowed-values
# constraint and enum gets generated attributes, and each enum a unique value, so items are
# checked against what was written rather than against the index.
# (target attribute, selector, namespace expected, conditions expected)
_SHAPE_DEF_TARGETS = (
    (None, "a", False, False),
    (".", "a", False, False),
    ("(.)[.='x']", "a", False, True),
    ("@f", "a/@f", False, False),
    ("(.)[@t='s']/@f", "a/@f", False, True),
    ("@f[has-oscal-namespace('http://n1')]", "a/@f", True, False),
    ("@f[has-oscal-namespace('http://n1') and @x='1']", "a/@f", True, True),
    ("@f[has-oscal-namespace('http://n1')]|@f", "a/@f", True, False),
    (
        "@f[has-oscal-namespace('http://n1')]|@f[has-oscal-namespace('http://n2')]",
        "a/@f",
        False,
        True,
    ),
)
_SHAPE_FLAG_TARGETS = (
    (None, "a/@f", False, False),
    (".", "a/@f", False, False),
    ("(.)[.='x']", "a/@f", False, True),
)
# (enum content, expected plain description) for 6.7 / 6.8.
_SHAPE_DESCRIPTIONS = (
    ("", ""),
    ("plain text", "plain text"),
    ("  a\n   <em>b</em> ", "a *b*"),
    ("<code>x</code> and <strong>s</strong>", "`x` and **s**"),
    ("say <q>hi</q>", 'say "hi"'),
)
_shape_allow_other = st.sampled_from([None, "yes", "no"])
_shape_extensible = st.sampled_from([None, "model", "external", "none"])
_shape_level = st.sampled_from([None, *_LEVELS])


@st.composite
def _shape_bundle(draw: st.DrawFn) -> tuple[str, dict[str, dict]]:
    """Module body for definition "a" and the expected item fields keyed by enum value."""
    expected: dict[str, dict] = {}
    counter = iter(range(1_000))

    def allowed_values(targets: tuple, flag_def: str | None) -> list[str]:
        out = []
        for _ in range(draw(st.integers(0, 3))):
            k = next(counter)
            target, selector, ns, conds = draw(st.sampled_from(targets))
            attrs = {
                "target": target,
                "allow-other": draw(_shape_allow_other),
                "extensible": draw(_shape_extensible),
                "level": draw(_shape_level),
                "id": draw(st.sampled_from([None, f"c-{k}"])),
            }
            attr_text = "".join(f" {n}={quoteattr(v)}" for n, v in attrs.items() if v is not None)
            lines = [f"<allowed-values{attr_text}>"]
            for j in range(draw(st.integers(1, 2))):
                value = f"v{k}-{j}"
                deprecated = draw(st.sampled_from([None, "1.0.0"]))
                markup, plain = draw(st.sampled_from(_SHAPE_DESCRIPTIONS))
                dep = "" if deprecated is None else f' deprecated="{deprecated}"'
                head = f'<enum value="{value}"{dep}'
                lines.append(f"{head}/>" if not markup else f"{head}>{markup}</enum>")
                expected[value] = {
                    "selector": selector,
                    "attrs": attrs,
                    "deprecated": deprecated,
                    "description": plain,
                    "namespace": ns,
                    "conditions": conds,
                    "flag-definition": flag_def,
                }
            lines.append("</allowed-values>")
            out += lines
        return ["<constraint>", *out, "</constraint>"] if out else []

    flag = allowed_values(_SHAPE_FLAG_TARGETS, "f")
    own = allowed_values(_SHAPE_DEF_TARGETS, None)
    body = "\n".join(
        [
            '<define-assembly name="a">',
            '<define-flag name="f">',
            *flag,
            "</define-flag>",
            *own,
            "</define-assembly>",
        ]
    )
    return body, expected


def _check_against_written(item: dict, spec: dict, text: str) -> None:
    """``item`` against the attributes generated for its constraint and enum."""
    attrs = spec["attrs"]
    where = item["value"]
    assert item["target"] == (attrs["target"] if attrs["target"] is not None else "."), where
    assert item["allow-other"] is (attrs["allow-other"] == "yes"), where
    assert item.get("extensible") == attrs["extensible"], where
    assert item["level"] == (attrs["level"] or "ERROR"), where
    assert item.get("constraint-id") == attrs["id"], where
    assert item.get("deprecated") == spec["deprecated"], where
    assert item["description"] == spec["description"], where
    assert item.get("flag-definition") == spec["flag-definition"], where
    assert ("namespace" in item) is spec["namespace"], where
    assert ("conditions" in item) is spec["conditions"], where
    assert item["definition"] == "a", where
    assert item["source-file"] == CAT, where
    assert item["line"] == _line_of(text, f'value="{item["value"]}"'), where


@pytest.mark.usefixtures("reset_metaschema_index")
class TestItemShapeProperty:
    """Property 5: Allowed_Value_Item shape.

    Each item is checked against the Constraint and EnumValue the oracle matched for it: the
    exact key set, value types, verbatim attributes, defaults, and plain JSON with no nulls.
    Synthetic bundles additionally check against the attributes that were written.

    **Validates: Requirements 6.1, 6.2, 6.3, 6.6, 6.9, 6.10, 6.11**
    """

    @settings(
        max_examples=200,
        deadline=None,
        # The index singleton is deliberately shared across examples (read-only).
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(pair=st.deferred(_real_selectors))
    def test_real_bundle_item_shape(self, pair):
        model, selector = pair
        items = _check_shapes(get_index().scope(OSCALModelType(model)), model, selector)
        event(f"items={'yes' if items else 'no'}")
        for key in ("extensible", "namespace", "conditions", "constraint-id", "flag-definition"):
            event(f"{key}={any(key in item for item in items)}")

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(bundle=_shape_bundle())
    def test_synthetic_item_shape(self, bundle):
        body, expected = bundle
        text = _module(body).decode()
        with tempfile.TemporaryDirectory() as tmp:
            _write_bundle(Path(tmp), {CAT: text})
            index = MetaschemaIndex.load(Path(tmp))
            assert dict(index.load_errors) == {}
            scope = index.scope(OSCALModelType.CATALOG)
            with patch.object(gav_module, "get_index", return_value=index):
                for selector in ("a", "a/@f"):
                    items = _check_shapes(scope, "catalog", selector)
                    want = sorted(v for v, spec in expected.items() if spec["selector"] == selector)
                    assert sorted(item["value"] for item in items) == want, selector
                    for item in items:
                        _check_against_written(item, expected[item["value"]], text)
        event(f"items={len(expected) > 0}")


# --------------------------------------------------------------------------------------
# Property 7: suggested targets are valid and productive
# --------------------------------------------------------------------------------------

_MAX_SUGGESTED = 20  # Requirement 5.8
_NO_MATCH = "/@zz-no-match"


def _is_valid(selector: str) -> bool:
    try:
        validate_selector(selector)
    except ValueError:
        return False
    return True


def _expected_suggestions(scope: ModelScope, name: str) -> list[str]:
    """5.8 bounds the list at 20 without saying which; the tool keeps the first 20 ascending."""
    return _productive_candidates(scope, name)[:_MAX_SUGGESTED]


def _productive_candidates(scope: ModelScope, name: str) -> list[str]:
    """The 5.8 candidate set for ``name``, built from its rule text, filtered by the oracle.

    Candidates: every non-cross-document path alternative of every ``allowed-values``
    Constraint on a Definition named ``name`` (``<name>``, ``<name><path>`` for ``//``,
    else ``<name>/<path>``), and ``<name>/@<flag>`` for every Flag_Slot whose flag
    Definition declares an ``allowed-values`` Constraint with an empty path. Kept when the
    selector is valid and the oracle finds at least one item. Sorted, uncapped.
    """
    candidates: set[str] = set()
    for definition in scope.definitions(name):
        for constraint in definition.constraints:
            if constraint.type != "allowed-values":
                continue
            for alt in constraint.alternatives:
                if alt.cross_document or alt.path is None:
                    continue
                path = alt.path
                if path == "":
                    candidates.add(name)
                elif path.startswith("//"):
                    candidates.add(name + path)
                else:
                    candidates.add(f"{name}/{path}")
        for slot in definition.flags:
            flag = slot.effective_name
            if any(
                _allowed_values_on(flag_def, "")
                for flag_def in _flag_slot_definitions(scope, definition, flag)
            ):
                candidates.add(f"{name}/@{flag}")
    return sorted(s for s in candidates if _is_valid(s) and _oracle(scope, s))


def _check_suggestions(scope: ModelScope, model: str, target: str) -> list[str]:
    """Assert Property 7 for a ``target`` that matches nothing; return ``suggested-targets``."""
    page = get_allowed_values(model=model, target=target)
    assert page["items"] == [], target
    assert page["total"] == 0, target
    assert page["hasMore"] is False, target
    suggested = page["suggested-targets"]
    assert isinstance(suggested, list), target
    assert all(isinstance(s, str) for s in suggested), target
    assert suggested == sorted(set(suggested)), target
    assert len(suggested) <= _MAX_SUGGESTED, target
    name, _ = _split_selector(target)
    for selector in suggested:
        where = f"{model} {target} -> {selector}"
        validate_selector(selector)
        assert selector == name or selector.startswith(f"{name}/"), where  # "/" or "//"
        total = get_allowed_values(model=model, target=selector, limit=1)["total"]
        expected = _oracle(scope, selector)
        assert expected is not None, where
        assert total == len(expected) >= 1, where
    assert suggested == _expected_suggestions(scope, name), f"{model} {target}"
    return suggested


def _no_match_targets() -> st.SearchStrategy[tuple[str, str]]:
    """Real ``(model, target)`` pairs: a bare name with no, a fixed, or a random remainder."""
    _, bare = _real_selector_pools()
    remainder = st.one_of(
        st.just(""),
        st.just(_NO_MATCH),
        st.lists(st.tuples(st.sampled_from(["/", "//"]), _step), min_size=1, max_size=3).map(
            lambda rest: "".join(sep + step for sep, step in rest)
        ),
    )
    return st.builds(lambda pair, rest: (pair[0], pair[1] + rest), st.sampled_from(bare), remainder)


# Synthetic suggestion bundles: definition "a" with many inline and referenced flags and many
# constraint paths, so candidate sets often exceed 20. Flag-definition targets: the first three
# have an empty path (productive through a Flag_Slot when they have enums); the rest do not.
_SUG_FLAG_TARGETS = (None, ".", "(.)[.='x']", "@x", "doc('o.xml')")
# Small and large counts, so both sides of the 20 cap occur often.
_sug_count = st.one_of(st.integers(0, 6), st.integers(15, 30))


def _sug_allowed_values(target: str | None, enums: int) -> str:
    attr = "" if target is None else f" target={quoteattr(target)}"
    values = "".join(f'<enum value="v{i}"/>' for i in range(enums))
    return f"<constraint><allowed-values{attr}>{values}</allowed-values></constraint>"


@st.composite
def _sug_flag_constraint(draw: st.DrawFn) -> str:
    if not draw(st.booleans()):
        return ""
    target = draw(st.sampled_from(_SUG_FLAG_TARGETS))
    return _sug_allowed_values(target, draw(st.integers(0, 2)))


@st.composite
def _sug_def_targets(draw: st.DrawFn) -> list[str]:
    """Own-constraint targets on "a": child, descendant, flag, self, union, and cross-doc."""
    forms = [
        lambda i: f"b{i}/@x",
        lambda i: f".//c{i}",
        lambda i: f"@f{i}",
        lambda i: f"@q{i}",
        lambda i: f"b{i}|d{i}/@y",
        lambda i: f"(.)[@t='s']/e{i}",
        lambda i: f"doc('o.xml')/@k{i}",
        lambda _: ".",
    ]
    size = draw(_sug_count)
    pick = st.tuples(st.sampled_from(forms), st.integers(0, 30))
    picks = draw(st.lists(pick, min_size=size, max_size=size))
    return [form(i) for form, i in picks]


@st.composite
def _suggestion_bundle(draw: st.DrawFn) -> str:
    """Module body: global and local "a", global flags "g<i>", and an unconstrained "z"."""
    inline = draw(_sug_count)
    refs = draw(
        st.lists(
            st.tuples(st.integers(0, 10), st.booleans()), max_size=10, unique_by=lambda r: r[0]
        )
    )
    parts = ['<define-assembly name="a">']
    parts += [
        f'<define-flag name="f{i}">{draw(_sug_flag_constraint())}</define-flag>'
        for i in range(inline)
    ]
    parts += [
        f'<flag ref="g{i}">' + (f"<use-name>u{i}</use-name>" if use else "") + "</flag>"
        for i, use in refs
    ]
    if draw(st.booleans()):
        local = [_sug_allowed_values(t, draw(st.integers(0, 2))) for t in draw(_sug_def_targets())]
        parts += ['<model><define-assembly name="a">', *local, "</define-assembly></model>"]
    parts += [_sug_allowed_values(t, draw(st.integers(0, 2))) for t in draw(_sug_def_targets())]
    parts.append("</define-assembly>")
    parts += [
        f'<define-flag name="g{i}">{draw(_sug_flag_constraint())}</define-flag>' for i in range(11)
    ]
    parts.append('<define-field name="z"/>')
    return "\n".join(parts)


@pytest.mark.usefixtures("reset_metaschema_index")
class TestSuggestedTargetsProperty:
    """Property 7: suggested targets are valid and productive.

    For a real definition name and a remainder that matches nothing, the response is an empty
    page with 0-20 distinct, ascending ``suggested-targets``, each a valid selector on that
    name that returns items (checked against the oracle). The list must also equal the 5.8
    candidate set computed independently (its first 20 in ascending order when larger).

    **Validates: Requirements 5.8**
    """

    @settings(
        max_examples=200,
        deadline=None,
        # The index singleton is deliberately shared across examples (read-only).
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.function_scoped_fixture,
            HealthCheck.filter_too_much,
        ],
    )
    @given(pair=st.deferred(_no_match_targets))
    def test_real_bundle_suggestions(self, pair):
        model, target = pair
        scope = get_index().scope(OSCALModelType(model))
        assume(_is_valid(target) and _oracle(scope, target) == [])
        suggested = _check_suggestions(scope, model, target)
        event(f"suggestions={'0' if not suggested else '20' if len(suggested) == 20 else '1-19'}")

    def test_every_real_definition_name(self):
        """Deterministic pass: every definition name in every model with ``/@zz-no-match``."""
        index = get_index()
        checked = with_suggestions = capped = 0
        for model in OSCALModelType:
            scope = index.scope(model)
            for name in scope.definition_names():
                target = name + _NO_MATCH
                if not _is_valid(target):
                    continue
                assert _oracle(scope, target) == [], f"{model.value} {target}"
                suggested = _check_suggestions(scope, model.value, target)
                checked += 1
                with_suggestions += bool(suggested)
                capped += len(suggested) == _MAX_SUGGESTED
        # Guard against a vacuous pass.
        assert checked > 500
        assert with_suggestions > 50

    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(body=_suggestion_bundle())
    def test_synthetic_suggestions(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            _write_bundle(Path(tmp), {CAT: _module(body)})
            index = MetaschemaIndex.load(Path(tmp))
            assert dict(index.load_errors) == {}
            scope = index.scope(OSCALModelType.CATALOG)
            with patch.object(gav_module, "get_index", return_value=index):
                suggested = _check_suggestions(scope, "catalog", "a" + _NO_MATCH)
                assert _check_suggestions(scope, "catalog", "z" + _NO_MATCH) == []
                full = len(_productive_candidates(scope, "a"))
        event(f"candidates>20={full > _MAX_SUGGESTED}")
        event(f"suggestions={'0' if not suggested else '20' if len(suggested) == 20 else '1-19'}")

    def test_synthetic_cap_keeps_first_twenty(self):
        """30 productive candidates: the first 20 in ascending order are returned."""
        flags = "".join(
            f'<define-flag name="f{i:02}">{_sug_allowed_values(None, 1)}</define-flag>'
            for i in range(25)
        )
        own = "".join(_sug_allowed_values(f"b{i}/@x", 1) for i in range(5))
        body = f'<define-assembly name="a">{flags}{own}</define-assembly>'
        with tempfile.TemporaryDirectory() as tmp:
            _write_bundle(Path(tmp), {CAT: _module(body)})
            index = MetaschemaIndex.load(Path(tmp))
            scope = index.scope(OSCALModelType.CATALOG)
            with patch.object(gav_module, "get_index", return_value=index):
                suggested = _check_suggestions(scope, "catalog", "a/@nope")
        everything = sorted([f"a/@f{i:02}" for i in range(25)] + [f"a/b{i}/@x" for i in range(5)])
        assert suggested == everything[:20]


# --------------------------------------------------------------------------------------
# Property 8: unknown-definition suggestions are the nearest names
# --------------------------------------------------------------------------------------

_SUGGEST_CHARS = 64  # RC-12: distances use the segment's first 64 characters
_MAX_NAMES = 10  # Requirement 5.10
_NEAREST = "Nearest definition names: "


def _naive_levenshtein(a: str, b: str) -> int:
    """Full-matrix Levenshtein distance (unit insert, delete, substitute)."""
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            rows[i][j] = min(
                rows[i - 1][j] + 1,
                rows[i][j - 1] + 1,
                rows[i - 1][j - 1] + (a[i - 1] != b[j - 1]),
            )
    return rows[len(a)][len(b)]


def _oracle_suggestions(segment: str, names: tuple[str, ...]) -> list[str]:
    """First 10 of ``names`` by (case-insensitive distance to ``segment[:64]``, name)."""
    query = segment[:_SUGGEST_CHARS].lower()
    return sorted(names, key=lambda n: (_naive_levenshtein(query, n.lower()), n))[:_MAX_NAMES]


@cache
def _bundle_index() -> MetaschemaIndex:
    """A private load of the real bundle, independent of the ``get_index()`` singleton."""
    return MetaschemaIndex.load()


@cache
def _scope_names() -> dict[str, tuple[str, ...]]:
    """``definition_names()`` per model value of the real bundle."""
    return {m.value: _bundle_index().scope(m).definition_names() for m in OSCALModelType}


# Characters a selector segment may contain: no whitespace, '/', predicates, or unions.
_segment_char = st.characters(exclude_categories=["Cs"]).filter(
    lambda c: not c.isspace() and c not in "/[]()|"
)


def _near_miss_segment(name: str, data: st.DataObject) -> str:
    """A real definition name with a few edits, case changes, or repetition past 64 chars."""
    chars = list(name)
    for _ in range(data.draw(st.integers(0, 3), label="edits")):
        pos = data.draw(st.integers(0, len(chars)), label="pos")
        op = data.draw(st.sampled_from(["insert", "delete", "replace"]), label="op")
        ch = data.draw(st.sampled_from("abcdefghijklmnopqrstuvwxyz-_ABC0"), label="ch")
        if op == "insert":
            chars.insert(pos, ch)
        elif chars and pos < len(chars):
            if op == "delete":
                del chars[pos]
            else:
                chars[pos] = ch
    text = "".join(chars) or "x"
    case = data.draw(st.sampled_from(["keep", "upper", "title", "swapcase"]), label="case")
    if case != "keep":
        text = getattr(text, case)()
    if data.draw(st.booleans(), label="long"):
        filler = data.draw(st.text(_segment_char, min_size=1, max_size=20), label="filler")
        text = (text + filler) * data.draw(st.integers(2, 40), label="repeat")
    return text[:512]


class TestUnknownDefinitionSuggestionProperty:
    """Property 8: unknown-definition suggestions are the nearest names.

    A naive full-matrix Levenshtein oracle is compared with the banded, early-exit
    implementation: directly, through ``suggest_definition_names``, and end to end through
    the tool against each real model scope. Includes the 64-character cap (RC-12).

    **Validates: Requirements 5.10**
    """

    @settings(max_examples=300, deadline=None)
    @given(
        a=st.one_of(st.text(max_size=100), st.text("abAB-", max_size=30)),
        b=st.one_of(st.text(max_size=100), st.text("abAB-", max_size=30)),
        cutoff=st.integers(0, 120),
    )
    def test_bounded_levenshtein_equals_capped_naive(self, a, b, cutoff):
        assert _bounded_levenshtein(a, b, cutoff) == min(_naive_levenshtein(a, b), cutoff + 1)

    @settings(max_examples=300, deadline=None)
    @given(
        segment=st.one_of(
            st.text(min_size=1, max_size=100),
            st.text("abcAB-", min_size=1, max_size=100),
        ),
        names=st.lists(
            st.one_of(st.text(min_size=1, max_size=40), st.text("abcAB-", min_size=1, max_size=12)),
            min_size=1,
            max_size=40,
            unique=True,  # definition_names() is distinct
        ).map(lambda ns: tuple(sorted(ns))),
    )
    # "İ".lower() is two characters: the distance can exceed len(name) (found while writing).
    @example(segment="abc", names=("İİ",))
    def test_suggestions_equal_naive_oracle(self, segment, names):
        assert suggest_definition_names(segment, names) == _oracle_suggestions(segment, names)
        event(f"segment>64={len(segment) > _SUGGEST_CHARS}")

    def test_real_names_are_message_safe(self):
        """Names are joined by ", " in the message, so none may contain it."""
        for model, names in _scope_names().items():
            assert names, model
            assert names == tuple(sorted(set(names))), model
            assert all(", " not in n and n for n in names), model

    @settings(
        max_examples=60,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )
    @given(model=st.sampled_from(_MODEL_VALUES), data=st.data())
    def test_tool_lists_oracle_names(self, model, data):
        names = _scope_names()[model]
        if data.draw(st.booleans(), label="near-miss"):
            segment = _near_miss_segment(data.draw(st.sampled_from(names), label="name"), data)
        else:
            segment = data.draw(st.text(_segment_char, min_size=1, max_size=512), label="text")
        remainder = data.draw(st.sampled_from(["", "/@type", "//part/@name"]), label="remainder")
        segment = segment[: 512 - len(remainder)]
        assume(segment not in names)
        target = segment + remainder
        validate_selector(target)

        expected = _oracle_suggestions(segment, names)
        assert 1 <= len(expected) <= _MAX_NAMES
        assert _tool_suggestions(model, target, segment) == expected
        if len(segment) > _SUGGEST_CHARS and segment[:_SUGGEST_CHARS] not in names:
            # The cap: a long segment lists the same names as its first 64 characters.
            head = segment[:_SUGGEST_CHARS]
            assert _tool_suggestions(model, head, head) == expected
        event(f"segment>64={len(segment) > _SUGGEST_CHARS}")
        event(f"segment>80={len(segment) > _SHOWN_CHARS}")


def _tool_suggestions(model: str, target: str, segment: str) -> list[str]:
    """Names the tool's unknown-definition error lists; checks the message and notification."""
    with (
        patch.object(gav_module, "get_index", return_value=_bundle_index()),
        patch.object(gav_module, "try_notify_client_error") as notify,
        pytest.raises(ValueError) as exc,
    ):
        get_allowed_values(model=model, target=target)
    message = str(exc.value)
    assert [c.args[0] for c in notify.call_args_list] == [message]
    shown = segment if len(segment) <= _SHOWN_CHARS else segment[:_SHOWN_CHARS] + "..."
    assert message.startswith(f"Unknown definition '{shown}': "), message
    _, sep, listed = message.partition(_NEAREST)
    assert sep, message
    return listed.split(", ")


# --------------------------------------------------------------------------------------
# Further example tests (task 9.10): property/@name, signature, docstring, load errors
# --------------------------------------------------------------------------------------

_GONE_ENT = "./shared-constraints/gone.ent"
_PROFILE_BODY = """\
<define-assembly name="p">
<constraint>
<allowed-values>
<enum value="ok"/>
</allowed-values>
</constraint>
</define-assembly>"""


def _signature_rows(fn) -> list[tuple[str, object, object]]:
    """``(name, base type, default)`` per parameter, with ``Annotated`` metadata removed.

    Every parameter except ``ctx`` must carry exactly one non-blank ``ParamDoc``, the
    description both the MCP inputSchema and the Strands tool spec read.
    """
    rows = []
    for p in inspect.signature(fn).parameters.values():
        annotation = p.annotation
        if p.name == "ctx":
            assert get_origin(annotation) is not Annotated
        else:
            assert get_origin(annotation) is Annotated, p.name
            docs = [m for m in get_args(annotation)[1:] if isinstance(m, ParamDoc)]
            assert len(docs) == 1, p.name
            assert docs[0].strip(), p.name
            annotation = get_args(annotation)[0]
        rows.append((p.name, annotation, p.default))
    return rows


@pytest.mark.usefixtures("reset_metaschema_index")
class TestFurtherExamples:
    """Validates: Requirements 5.1, 5.13, 7.4"""

    def test_property_name_returns_marking(self):
        items = _all_items("component-definition", "property/@name")
        marking = [i for i in items if i["value"] == "marking"]
        assert marking, "marking missing from property/@name"
        for item in marking:
            assert item["definition"] == "property"
            _assert_line_has_enum(item)

    def test_signature_and_defaults(self):
        """Req 5.1: model, target, offset=0, limit=25, ctx=None, with their types."""
        expected = [
            ("model", str, inspect.Parameter.empty),
            ("target", str, inspect.Parameter.empty),
            ("offset", int, 0),
            ("limit", int, 25),
            ("ctx", Context | None, None),
        ]
        assert _signature_rows(get_allowed_values) == expected

    def test_mcp_input_schema_hides_ctx(self):
        """The MCP server injects ``ctx``; callers see only the four documented arguments."""
        server = MCPServer("test")
        server.add_tool(get_allowed_values)
        (tool_def,) = asyncio.run(server.list_tools())
        schema = tool_def.input_schema
        assert list(schema["properties"]) == ["model", "target", "offset", "limit"]
        assert sorted(schema["required"]) == ["model", "target"]
        assert schema["properties"]["offset"]["default"] == 0
        assert schema["properties"]["limit"]["default"] == 25

    def test_summary_sentence_names_allowed_values_source(self):
        """The summary sentence says what the tool returns and where the values come from.

        Supersedes metaschema-constraints Req 7.4 (example questions in the first paragraph);
        tool-context-optimization Req 2 limits descriptions to three sentences.
        """
        summary = summary_sentence(inspect.getdoc(get_allowed_values) or "")
        assert summary.startswith("Return ")
        assert "OSCAL Metaschema" in summary
        assert "`allowed-values`" in summary

    def test_target_parameter_documents_target_selector_syntax(self):
        """Req 7.4: the ``target`` description is TARGET_SELECTOR_DOC, with valid examples."""
        server = MCPServer("test")
        server.add_tool(get_allowed_values)
        (tool_def,) = asyncio.run(server.list_tools())
        description = tool_def.input_schema["properties"]["target"]["description"]
        assert description == TARGET_SELECTOR_DOC
        examples: list[str] = []
        for quoted in re.findall(r'"([^"]+)"', description):
            try:
                validate_selector(quoted)
            except ValueError:
                continue
            examples.append(quoted)
        assert [e for e in examples if "/" in e], examples

    def test_routing_guide_advises_generic_property_and_link(self):
        """The Routing_Guide advises querying the generic property and link definitions.

        Supersedes metaschema-constraints Req 7.4 (advice in the tool docstring);
        tool-context-optimization Req 10.4 moves it to the server-level ROUTING_GUIDE.
        """
        assert '"property"' in ROUTING_GUIDE
        assert '"link"' in ROUTING_GUIDE
        assert '"property/@name"' in ROUTING_GUIDE
        assert '"link/@rel"' in ROUTING_GUIDE
        docstring = inspect.getdoc(get_allowed_values) or ""
        assert "property/@name" not in docstring
        assert "link/@rel" not in docstring

    def test_load_error_names_bundle_relative_source(self, tmp_path: Path):
        """Req 5.13: a refused entity fails the scope with a client-safe MetaschemaLoadError."""
        common = _mod("common")
        files: dict[str, str | bytes] = {
            CAT: _module(_imports(common) + '<define-flag name="c"/>'),
            common: _module(
                "<allowed-values>&x;</allowed-values>", _doctype(_ent_decl("x", _GONE_ENT))
            ),
            PROF: _module(_PROFILE_BODY),
        }
        _write_bundle(tmp_path, files)
        index = MetaschemaIndex.load(tmp_path)
        assert list(index.load_errors) == [common]
        with (
            patch.object(gav_module, "get_index", return_value=index),
            patch.object(gav_module, "try_notify_client_error") as notify,
        ):
            with pytest.raises(MetaschemaLoadError) as exc:
                get_allowed_values(model="catalog", target="c")
            assert isinstance(exc.value, ValueError)
            message = str(exc.value)
            assert f'{common}: x (SYSTEM "{_GONE_ENT}")' in message
            assert str(tmp_path) not in message
            assert tmp_path.as_posix() not in message
            notify.assert_called_once()
            assert notify.call_args.args[0] == message

            # A scope that does not reach the failing module still answers.
            notify.reset_mock()
            page = get_allowed_values(model="profile", target="p")
            notify.assert_not_called()
        assert [i["value"] for i in page["items"]] == ["ok"]
        assert page["items"][0]["source-file"] == PROF
