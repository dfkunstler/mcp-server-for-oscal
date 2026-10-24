"""Tests for the get_constraints tool (Feature: metaschema-constraints).

Smoke tests from task 12.1, the agreement property from task 12.3, and the item shape
property from task 12.4, and the required examples from task 12.5.
"""

import asyncio
import inspect
import json
import re
import tempfile
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import get_args
from unittest.mock import patch
from xml.sax.saxutils import quoteattr

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from mcp.server.mcpserver import Context, MCPServer

from mcp_server_for_oscal.tools import get_allowed_values as gav_module
from mcp_server_for_oscal.tools import get_constraints as gc_module
from mcp_server_for_oscal.tools.get_allowed_values import get_allowed_values
from mcp_server_for_oscal.tools.get_constraints import ConstraintType, get_constraints
from mcp_server_for_oscal.tools.metaschema_index import (
    BUNDLE_DIR,
    CONSTRAINT_TYPES,
    Constraint,
    Definition,
    DefinitionKey,
    MetaschemaIndex,
    MetaschemaLoadError,
    ModelScope,
    get_index,
)
from mcp_server_for_oscal.tools.metaschema_paths import (
    Condition,
    PathAlternative,
    compose_selector,
    normalize_target,
    parse_selector,
    validate_selector,
)
from mcp_server_for_oscal.tools.utils import MAX_PAGE_LIMIT, OSCALModelType, paginate

from .test_get_allowed_values import (
    _GONE_ENT,
    _PROFILE_BODY,
    _SYN_DEFS,
    _SYN_DOCTYPE,
    _SYN_ENT,
    _SYN_FLAG_TARGETS,
    _SYN_FLAGS,
    _SYN_TARGETS,
    _SYN_USE_NAMES,
    _SYN_X,
    _flag_slot_definitions,
    _real_selector_pools,
    _selector_totals,
    _signature_rows,
    _syn_module_body,
    _syn_selectors,
)
from .test_get_allowed_values import (
    _all_items as _all_value_items,
)
from .test_metaschema_index import (
    CAT,
    ENT,
    PROF,
    _doctype,
    _ent_decl,
    _imports,
    _mod,
    _module,
    _write_bundle,
)

COMMON_KEYS = {"type", "definition", "target", "level", "source-file", "line"}


def _all_items(
    model: str, target: str, constraint_types: list[ConstraintType] | None = None
) -> list[dict]:
    """Walk every page of a result and return the items in order."""
    items: list[dict] = []
    offset = 0
    while True:
        page = get_constraints(
            model=model,
            target=target,
            constraint_types=constraint_types,
            offset=offset,
            limit=100,
        )
        items.extend(page["items"])
        if not page["hasMore"]:
            return items
        offset += len(page["items"])


def _location(item: dict) -> tuple[str, int]:
    return item["source-file"], item["line"]


@pytest.mark.usefixtures("reset_metaschema_index")
class TestSmoke:
    """Smoke tests against the bundled OSCAL 1.2.3 Metaschema."""

    def test_bare_defined_component_lists_several_types(self):
        items = _all_items("component-definition", "defined-component")
        types = {item["type"] for item in items}
        assert {"allowed-values", "matches", "is-unique", "index-has-key"} <= types
        for item in items:
            assert set(item) >= COMMON_KEYS
            assert None not in item.values()
        assert [_location(i) for i in items] == sorted(_location(i) for i in items)

    def test_type_specific_keys(self):
        items = _all_items("component-definition", "defined-component")
        by_id = {item.get("constraint-id"): item for item in items}
        prop_name = by_id["oscal-component-prop-name"]
        assert prop_name["value-count"] > 0
        assert prop_name["allow-other"] is False
        assert prop_name["namespace"] == "http://csrc.nist.gov/ns/oscal"
        assert by_id["oscal-component-release-date-value-datatype"]["datatype"] == "date"
        assert by_id["oscal-unique-defined-component-responsible-role"]["key-fields"] == [
            "@role-id"
        ]
        assert by_id["oscal-service-component-link-rel-type"]["conditions"] == [
            {"step": "(.)", "predicate": "@type='service'"}
        ]

    def test_bare_name_walks_nested_local_definitions(self):
        """metadata's constraints include those of its local location Definition (8.3)."""
        items = _all_items("catalog", "metadata", ["has-cardinality"])
        assert items
        assert {item["definition"] for item in items} == {"location"}
        assert all("min-occurs" in item for item in items)

    def test_type_filter_is_subset_in_same_order(self):
        everything = _all_items("component-definition", "defined-component")
        only = ["matches", "is-unique"]
        filtered = _all_items("component-definition", "defined-component", only)
        assert filtered == [item for item in everything if item["type"] in only]
        assert _all_items("component-definition", "defined-component", []) == everything

    def test_path_selector_agrees_with_get_allowed_values(self):
        target = "defined-component/prop/@name"
        items = _all_items("component-definition", target, ["allowed-values"])
        values = get_allowed_values(model="component-definition", target=target, limit=100)
        assert sum(item["value-count"] for item in items) == values["total"]
        assert all(item["type"] == "allowed-values" for item in items)

    def test_flag_slot_match_names_flag_definition(self):
        items = _all_items("system-security-plan", "system-component/@type")
        assert items
        assert all(item["definition"] == "system-component" for item in items)
        assert {item["flag-definition"] for item in items} == {"system-component-type"}

    def test_no_match_returns_empty_page(self):
        page = get_constraints(model="component-definition", target="defined-component/nope")
        assert page == {"items": [], "total": 0, "offset": 0, "limit": 25, "hasMore": False}


class TestArgumentRejection:
    """Rejected arguments raise ValueError, notify the client, and never read the index."""

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"model": "nope", "target": "defined-component"}, "list_oscal_models"),
            ({"model": "catalog", "target": "a[b]"}, "target"),
            (
                {"model": "catalog", "target": "control", "constraint_types": ["expect", "bogus"]},
                "'bogus'",
            ),
            ({"model": "catalog", "target": "control", "constraint_types": "expect"}, "list"),
            ({"model": "catalog", "target": "control", "limit": 0}, "limit"),
            ({"model": "catalog", "target": "control", "offset": -1}, "offset"),
        ],
    )
    def test_rejected_without_index_access(self, kwargs, match):
        with (
            patch.object(gc_module, "get_index") as get_index,
            patch.object(gc_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match=match),
        ):
            get_constraints(**kwargs)
        get_index.assert_not_called()
        notify.assert_called_once()

    def test_invalid_type_lists_all_seven_names(self):
        with pytest.raises(ValueError) as exc:
            get_constraints(model="catalog", target="control", constraint_types=["nope", "nope"])
        message = str(exc.value)
        assert message.count("'nope'") == 1
        for name in CONSTRAINT_TYPES:
            assert name in message
        assert len(CONSTRAINT_TYPES) == 7

    @pytest.mark.usefixtures("reset_metaschema_index")
    def test_unknown_definition_suggests_names(self):
        with (
            patch.object(gc_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match="Unknown definition 'defined-componnet'") as exc,
        ):
            get_constraints(model="component-definition", target="defined-componnet")
        notify.assert_called_once()
        assert "defined-component" in str(exc.value).split("Nearest definition names: ")[1]


_EMPTY_PAGE = {"items": [], "total": 0, "offset": 0, "limit": 25, "hasMore": False}


def _paginate_error(offset: int, limit: int) -> str:
    """The message ``utils.paginate`` itself raises for ``offset`` and ``limit``."""
    with pytest.raises(ValueError) as exc:
        paginate([], offset, limit)
    return str(exc.value)


@pytest.mark.usefixtures("reset_metaschema_index")
class TestRequiredExamples:
    """Validates: Requirements 8.1, 8.8, 8.9, 8.10, 8.11"""

    def test_signature_and_defaults(self):
        """Req 8.1: parameters, types, and defaults in order."""
        expected = [
            ("model", str, inspect.Parameter.empty),
            ("target", str, inspect.Parameter.empty),
            ("constraint_types", list[ConstraintType] | None, None),
            ("offset", int, 0),
            ("limit", int, 25),
            ("ctx", Context | None, None),
        ]
        assert _signature_rows(get_constraints) == expected

    def test_constraint_type_enum_matches_index(self):
        """The ``constraint_types`` enum is exactly the index's CONSTRAINT_TYPES, in order."""
        assert get_args(ConstraintType) == CONSTRAINT_TYPES
        server = MCPServer("test")
        server.add_tool(get_constraints)
        (tool_def,) = asyncio.run(server.list_tools())
        prop = tool_def.input_schema["properties"]["constraint_types"]
        (array,) = [s for s in prop["anyOf"] if s.get("type") == "array"]
        assert tuple(array["items"]["enum"]) == CONSTRAINT_TYPES

    def test_mcp_input_schema_hides_ctx(self):
        """The MCP server injects ``ctx``; callers see only the five documented arguments."""
        server = MCPServer("test")
        server.add_tool(get_constraints)
        (tool_def,) = asyncio.run(server.list_tools())
        assert tool_def.name == "get_constraints"
        schema = tool_def.input_schema
        assert list(schema["properties"]) == [
            "model",
            "target",
            "constraint_types",
            "offset",
            "limit",
        ]
        assert sorted(schema["required"]) == ["model", "target"]
        assert schema["properties"]["offset"]["default"] == 0
        assert schema["properties"]["limit"]["default"] == 25

    @pytest.mark.parametrize(
        ("model", "target", "types"),
        [
            # A bare name with a type filter none of its constraints has.
            ("catalog", "control", ["has-cardinality"]),
            ("catalog", "parameter", ["is-unique"]),
            # A path no constraint targets.
            ("component-definition", "defined-component/nope", ["allowed-values", "expect"]),
        ],
    )
    def test_existing_definition_without_match_is_empty_page(self, model, target, types):
        """Req 8.8: an existing Definition with no matching constraint gives an empty page."""
        with patch.object(gc_module, "try_notify_client_error") as notify:
            page = get_constraints(model=model, target=target, constraint_types=types)
        assert page == _EMPTY_PAGE
        notify.assert_not_called()
        # The same Definition does have constraints without the filter.
        assert get_constraints(model=model, target=target.split("/")[0])["total"] > 0

    def test_mixed_types_names_each_invalid_value_only(self):
        """Req 8.9: each invalid value is named once; the seven valid names are listed."""
        types = ["expect", "Matches", "index", 7, "bogus", "Matches"]
        with (
            patch.object(gc_module, "get_index") as get_index,
            patch.object(gc_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError) as exc,
        ):
            get_constraints(model="catalog", target="control", constraint_types=types)
        message = str(exc.value)
        notify.assert_called_once_with(message, None)
        get_index.assert_not_called()
        invalid_part, valid_part = message.split("Valid constraint types: ")
        assert invalid_part.count("'Matches'") == 1
        assert "'bogus'" in invalid_part
        assert "int" in invalid_part
        for valid in ("expect", "index"):
            assert f"'{valid}'" not in invalid_part
        assert valid_part.split(", ") == list(CONSTRAINT_TYPES)

    @pytest.mark.parametrize(
        ("offset", "limit"), [(-1, 25), (-5, 0), (0, 0), (0, -3), (0, MAX_PAGE_LIMIT + 1)]
    )
    def test_out_of_range_paging_raises_paginate_error(self, offset, limit):
        """Reqs 8.11, 12.2: the exact ValueError utils.paginate produces, no page."""
        with (
            patch.object(gc_module, "get_index") as get_index,
            patch.object(gc_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError) as exc,
        ):
            get_constraints(model="catalog", target="control", offset=offset, limit=limit)
        assert str(exc.value) == _paginate_error(offset, limit)
        notify.assert_called_once_with(str(exc.value), None)
        get_index.assert_not_called()

    def test_limit_bounds_are_accepted(self):
        """Req 12.1: limit 1 and MAX_PAGE_LIMIT are both valid."""
        one = get_constraints(model="component-definition", target="defined-component", limit=1)
        assert len(one["items"]) == 1
        assert one["hasMore"] is True
        full = get_constraints(
            model="component-definition", target="defined-component", limit=MAX_PAGE_LIMIT
        )
        assert full["total"] == one["total"]
        assert full["hasMore"] is False

    def test_offset_at_or_past_total_is_empty_with_full_total(self):
        """Req 12.7: offset >= total gives no items, the full total, and hasMore false."""
        total = get_constraints(model="component-definition", target="defined-component")["total"]
        assert total > 0
        for offset in (total, total + 1, total + 1000):
            page = get_constraints(
                model="component-definition", target="defined-component", offset=offset
            )
            assert page["items"] == []
            assert page["total"] == total
            assert page["offset"] == offset
            assert page["hasMore"] is False

    @pytest.mark.parametrize("model", ["nope", "", "Catalog"])
    def test_invalid_model_names_list_oscal_models(self, model):
        """Req 8.10: a non-Model_Name is rejected before the index is read."""
        with (
            patch.object(gc_module, "get_index") as get_index,
            patch.object(gc_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match="list_oscal_models") as exc,
        ):
            get_constraints(model=model, target="control")
        notify.assert_called_once_with(str(exc.value), None)
        get_index.assert_not_called()

    def test_load_error_is_client_safe_and_notified_once(self, tmp_path: Path):
        """A refused entity fails the scope with a client-safe MetaschemaLoadError."""
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
            patch.object(gc_module, "get_index", return_value=index),
            patch.object(gc_module, "try_notify_client_error") as notify,
        ):
            with pytest.raises(MetaschemaLoadError) as exc:
                get_constraints(model="catalog", target="c")
            assert isinstance(exc.value, ValueError)
            message = str(exc.value)
            assert f'{common}: x (SYSTEM "{_GONE_ENT}")' in message
            assert str(tmp_path) not in message
            assert tmp_path.as_posix() not in message
            notify.assert_called_once_with(message, None)

            # A scope that does not reach the failing module still answers.
            notify.reset_mock()
            page = get_constraints(model="profile", target="p")
            notify.assert_not_called()
        assert [i["type"] for i in page["items"]] == ["allowed-values"]
        assert page["items"][0]["source-file"] == PROF


# --------------------------------------------------------------------------------------
# Property 17: get_constraints agrees with get_allowed_values and filters by type
# --------------------------------------------------------------------------------------

_Location = tuple[str, int]


def _constraint_location(constraint: Constraint) -> _Location:
    return constraint.location.source_file, constraint.location.line


def _same_document_paths(constraint: Constraint) -> set[str]:
    return {a.path for a in constraint.alternatives if not a.cross_document and a.path is not None}


def _constraints_by_location(scope: ModelScope) -> dict[_Location, Constraint]:
    """Every Constraint in ``scope`` by Source_Location, which identifies it (asserted)."""
    found: dict[_Location, Constraint] = {}
    for name in scope.definition_names():
        for definition in scope.definitions(name):
            for constraint in definition.constraints:
                prior = found.setdefault(_constraint_location(constraint), constraint)
                assert prior is constraint, f"two constraints at {_constraint_location(prior)}"
    return found


def _descendant_keys(scope: ModelScope, definitions: tuple[Definition, ...]) -> set[DefinitionKey]:
    """Keys of ``definitions`` and every local Definition nested in them, at any depth."""
    keys: set[DefinitionKey] = set()

    def visit(definition: Definition) -> None:
        if definition.key in keys:
            return
        keys.add(definition.key)
        for key in definition.nested:
            for nested in scope.definitions(key.name):
                if nested.key == key:
                    visit(nested)

    for definition in definitions:
        visit(definition)
    return keys


def _oracle_constraints(scope: ModelScope, selector: str) -> list[Constraint]:
    """Expected Constraints of every type for ``selector`` by a direct scan (8.2, 8.3, 8.5).

    A path selector matches as in Requirement 5 (a same-document alternative equal to the
    remainder, plus empty-path constraints on flag Definitions for ``@flag``). A bare name
    takes every Constraint declared on its Definitions and their nested local Definitions.
    """
    name, remainder = parse_selector(selector)
    definitions = scope.definitions(name)
    found: dict[int, Constraint] = {}
    if not remainder:
        keys = _descendant_keys(scope, definitions)
        for constraint in _constraints_by_location(scope).values():
            if constraint.definition_key in keys:
                found[id(constraint)] = constraint
    else:
        for definition in definitions:
            for constraint in definition.constraints:
                if remainder in _same_document_paths(constraint):
                    found[id(constraint)] = constraint
        if remainder.startswith("@") and "/" not in remainder:
            for definition in definitions:
                for flag_def in _flag_slot_definitions(scope, definition, remainder[1:]):
                    for constraint in flag_def.constraints:
                        if "" in _same_document_paths(constraint):
                            found[id(constraint)] = constraint
    return sorted(found.values(), key=_constraint_location)


def _value_key(item: dict, *, with_conditions: bool) -> tuple:
    """The fields an Allowed_Value_Item shares with its constraint's get_constraints item."""
    key: tuple = (
        item["target"],
        item["allow-other"],
        item.get("extensible"),
        item.get("constraint-id"),
        item.get("flag-definition"),
    )
    if with_conditions:
        key += (item.get("namespace"), item.get("conditions"))
    return key


def _check_agreement(
    scope: ModelScope,
    by_location: dict[_Location, Constraint],
    model: str,
    selector: str,
    types: list[ConstraintType],
) -> dict[str, int]:
    """Assert Property 17 for one selector and type filter; return counts for events.

    Path selectors: expanding each ``allowed-values`` item's enums, in item order, must give
    the get_allowed_values items exactly (zero-enum constraints contribute nothing, and their
    ``value-count`` is 0). Bare names: get_allowed_values covers only the returned
    constraints declared on a Definition of that name with a same-document empty path; the
    rest are on nested local Definitions or target another path (Requirement 8.3).
    """
    name, remainder = parse_selector(selector)
    definitions = scope.definitions(name)
    if not definitions:
        return {}
    everything = _all_items(model, selector)
    locations = [_location(item) for item in everything]
    # 8.2, 8.3, 8.5: the matched set, in Source_Location order.
    assert locations == [_constraint_location(c) for c in _oracle_constraints(scope, selector)]
    assert [item["type"] for item in everything] == [by_location[loc].type for loc in locations]

    # 8.4: a filter keeps the unfiltered order; an empty list is no filter.
    filtered = _all_items(model, selector, types)
    assert filtered == [item for item in everything if not types or item["type"] in types]

    allowed = [item for item in everything if item["type"] == "allowed-values"]
    assert _all_items(model, selector, ["allowed-values"]) == allowed
    selector_keys = {d.key for d in definitions}
    expected: list[tuple] = []
    counts = {"zero-enum": 0, "bare-extra": 0}
    for item in allowed:
        constraint = by_location[_location(item)]
        assert item["value-count"] == len(constraint.enums)
        counts["zero-enum"] += not constraint.enums
        if remainder or (
            constraint.definition_key in selector_keys and "" in _same_document_paths(constraint)
        ):
            shared = _value_key(item, with_conditions=bool(remainder))
            expected += [
                (e.value, e.location.source_file, e.location.line, *shared)
                for e in constraint.enums
            ]
        else:
            counts["bare-extra"] += 1
    got = [
        (v["value"], v["source-file"], v["line"], *_value_key(v, with_conditions=bool(remainder)))
        for v in _all_value_items(model, selector)
    ]
    assert got == expected, f"{model} {selector}"
    counts["items"] = len(everything)
    return counts


@cache
def _all_type_selectors() -> tuple[tuple[str, str], ...]:
    """Real-bundle ``(model, selector)`` pairs from every path alternative of every type."""
    index = MetaschemaIndex.load()
    pairs: dict[tuple[str, str], None] = {}
    for model in OSCALModelType:
        scope = index.scope(model)
        for name in scope.definition_names():
            for definition in scope.definitions(name):
                for constraint in definition.constraints:
                    for path in _same_document_paths(constraint):
                        selector = compose_selector(name, path)
                        try:
                            validate_selector(selector)
                        except ValueError:
                            continue
                        pairs[model.value, selector] = None
    return tuple(pairs)


def _real_pairs() -> st.SearchStrategy[tuple[str, str]]:
    selectors, bare = _real_selector_pools()
    return st.one_of(
        st.sampled_from(list(_selector_totals())),
        st.sampled_from(selectors),
        st.sampled_from(bare),
        st.sampled_from(_all_type_selectors()),
    )


_type_filters = st.lists(st.sampled_from(CONSTRAINT_TYPES), max_size=len(CONSTRAINT_TYPES) + 1)


@pytest.mark.usefixtures("reset_metaschema_index")
class TestAgreementProperty:
    """Property 17: get_constraints agrees with get_allowed_values and filters by type.

    **Validates: Requirements 8.2, 8.3, 8.4, 8.5**
    """

    @settings(
        max_examples=200,
        deadline=None,
        # The index singleton is deliberately shared across examples (read-only).
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(pair=st.deferred(_real_pairs), types=_type_filters)
    def test_real_bundle_agrees(self, pair, types):
        model, selector = pair
        scope = get_index().scope(OSCALModelType(model))
        counts = _check_agreement(scope, _real_by_location(scope), model, selector, types)
        event(f"mode={'path' if parse_selector(selector)[1] else 'bare'}")
        event(f"items>0={counts.get('items', 0) > 0}")
        event(f"bare-extra>0={counts.get('bare-extra', 0) > 0}")

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(cat=_syn_module_body, x=_syn_module_body, types=_type_filters)
    def test_synthetic_bundles_agree(self, cat, x, types):
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
            by_location = _constraints_by_location(scope)
            totals = {"items": 0, "zero-enum": 0, "bare-extra": 0}
            with (
                patch.object(gav_module, "get_index", return_value=index),
                patch.object(gc_module, "get_index", return_value=index),
            ):
                for selector in _syn_selectors(scope):
                    counts = _check_agreement(scope, by_location, "catalog", selector, types)
                    for key, value in counts.items():
                        totals[key] += value
        for key, value in totals.items():
            event(f"{key}>0={value > 0}")


_by_location_cache: dict[int, tuple[ModelScope, dict[_Location, Constraint]]] = {}


def _real_by_location(scope: ModelScope) -> dict[_Location, Constraint]:
    """``_constraints_by_location`` for a real-bundle scope, computed once per scope object."""
    cached = _by_location_cache.get(id(scope))
    if cached is None or cached[0] is not scope:
        if len(_by_location_cache) >= len(OSCALModelType):  # a new index: drop the old one
            _by_location_cache.clear()
        cached = _by_location_cache[id(scope)] = (scope, _constraints_by_location(scope))
    return cached[1]


# --------------------------------------------------------------------------------------
# Property 18: get_constraints item shape
# --------------------------------------------------------------------------------------

# Type-specific attributes copied verbatim when declared (Requirement 8.6). Written from the
# requirement, not from the tool's own table. ``allow-other``, ``value-count``,
# ``key-fields``, and ``message`` are checked separately.
_DECLARED_ATTRS: dict[str, tuple[str, ...]] = {
    "allowed-values": ("extensible",),
    "matches": ("datatype", "regex"),
    "index": ("name",),
    "index-has-key": ("name",),
    "is-unique": (),
    "has-cardinality": ("min-occurs", "max-occurs"),
    "expect": ("test",),
}
_KEY_FIELD_TYPES = frozenset({"index", "index-has-key", "is-unique"})
_DOCUMENTED_KEYS = (
    COMMON_KEYS
    | {"constraint-id", "namespace", "conditions", "flag-definition", "truncated"}
    | {"allow-other", "value-count", "key-fields", "message"}
    | {attr for attrs in _DECLARED_ATTRS.values() for attr in attrs}
)
_HYPHENATED = re.compile(r"[a-z]+(?:-[a-z]+)*")
_TRUNCATABLE = ("message", "test")

# XML attribute values: literal whitespace becomes a space (XML 1.0 section 3.3.3), then
# character and predefined entity references are replaced.
_ATTR_RE = re.compile(r"""\s+([\w.:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_REF_RE = re.compile(r"&(?:#x([0-9a-fA-F]+)|#([0-9]+)|(lt|gt|amp|quot|apos));")
_NAMED_REFS = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}


def _attr_value(raw: str) -> str:
    normalized = re.sub(r"[\t\n\r]", " ", raw)
    assert "&" not in _REF_RE.sub("", normalized), f"unexpected entity in {raw!r}"

    def replace(m: re.Match[str]) -> str:
        if m[1]:
            return chr(int(m[1], 16))
        if m[2]:
            return chr(int(m[2]))
        return _NAMED_REFS[m[3]]

    return _REF_RE.sub(replace, normalized)


def _start_tag_attrs(text: str, line: int, tag: str) -> dict[str, str]:
    """No-namespace attributes of the ``<tag ...>`` start tag that begins on ``line``.

    An oracle independent of the index's parser: it reads the source text at the cited
    Source_Location, so it also checks the ``target`` and ``level`` defaults (8.6).
    """
    lines = text.split("\n")
    start = sum(len(text_line) + 1 for text_line in lines[: line - 1])
    end = start + len(lines[line - 1]) + 1
    opening = re.compile(rf"<(?:[\w.-]+:)?{re.escape(tag)}(?=[\s/>])").search(text, start, end)
    assert opening is not None, f"no <{tag}> start tag on line {line}"
    attrs: dict[str, str] = {}
    pos = opening.end()
    while attr := _ATTR_RE.match(text, pos):
        if ":" not in attr[1]:  # namespaced attributes are not Constraint attributes
            attrs[attr[1]] = _attr_value(attr[2] if attr[2] is not None else attr[3])
        pos = attr.end()
    assert re.match(r"\s*/?>", text[pos:]), f"unparsed start tag text on line {line}"
    return attrs


@cache
def _real_source(source_file: str) -> str:
    return (BUNDLE_DIR / source_file).read_text(encoding="utf-8")


def _expected_namespace_and_conditions(
    alternatives: list[PathAlternative],
) -> tuple[str | None, list[dict[str, str]]]:
    """``namespace`` and ``conditions`` for the matched alternatives (6.4, 6.5, RC-4).

    Exactly one distinct namespace: report it, and the conditions without its
    Namespace_Conditions. Otherwise: no namespace, and every Namespace_Condition is reported
    as a Condition (each alternative re-normalized without namespace extraction).
    """
    namespaces = {alt.namespace for alt in alternatives if alt.namespace is not None}
    namespace = next(iter(namespaces)) if len(namespaces) == 1 else None
    conditions: list[Condition] = []
    for alt in alternatives:
        source = alt
        if namespace is None:
            (source,) = normalize_target(alt.raw, extract_namespace=False)
        conditions += [c for c in source.conditions if c not in conditions]
    return namespace, [{"step": c.step, "predicate": c.predicate} for c in conditions]


def _assert_no_none(value: object, where: str) -> None:
    assert value is not None, f"None at {where}"
    if isinstance(value, dict):
        for key, child in value.items():
            _assert_no_none(child, f"{where}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _assert_no_none(child, f"{where}[{i}]")


def _assert_value_types(item: dict) -> None:
    for key, value in item.items():
        if key in {"allow-other", "truncated"}:
            assert value is True or value is False, key
        elif key in {"line", "value-count"}:
            assert type(value) is int, key
        elif key == "key-fields":
            assert value, key
            assert all(isinstance(v, str) for v in value), key
        elif key == "conditions":
            assert value, key
            for condition in value:
                assert set(condition) == {"step", "predicate"}
                assert all(isinstance(v, str) for v in condition.values())
        else:
            assert isinstance(value, str), key
    if "truncated" in item:
        assert item["truncated"] is True


def _expected_item(
    item: dict,
    constraint: Constraint,
    selector_name: str,
    alternatives: list[PathAlternative],
    flag_definition: str | None,
    source: Callable[[str], str],
) -> dict:
    """The item Requirements 8.6 and 8.7 prescribe for ``constraint``."""
    src = _start_tag_attrs(source(item["source-file"]), item["line"], constraint.type)
    expected: dict = {
        "type": constraint.type,
        "definition": selector_name if flag_definition else constraint.definition_key.name,
        "target": src.get("target", "."),
        "level": src.get("level", "ERROR"),
        "source-file": constraint.location.source_file,
        "line": constraint.location.line,
    }
    if "id" in src:
        expected["constraint-id"] = src["id"]
    if constraint.type == "allowed-values":
        expected["allow-other"] = src.get("allow-other") == "yes"
        expected["value-count"] = len(constraint.enums)
    expected.update({a: src[a] for a in _DECLARED_ATTRS[constraint.type] if a in src})
    if constraint.type in _KEY_FIELD_TYPES and constraint.key_fields:
        expected["key-fields"] = list(constraint.key_fields)
    if constraint.type == "expect" and constraint.message is not None:
        expected["message"] = constraint.message.plain()
    namespace, conditions = _expected_namespace_and_conditions(alternatives)
    if namespace is not None:
        expected["namespace"] = namespace
    if conditions:
        expected["conditions"] = conditions
    if flag_definition:
        expected["flag-definition"] = flag_definition
    return expected


def _check_item_shapes(
    scope: ModelScope,
    by_location: dict[_Location, Constraint],
    source: Callable[[str], str],
    model: str,
    selector: str,
) -> set[str]:
    """Assert Property 18 for every item of ``selector``; return labels for events.

    The matched alternatives are recomputed here: in path mode, the same-document
    alternatives whose path is the remainder (or, through a Flag_Slot, the empty path); in
    bare-name mode, every alternative (the target is not filtered).
    """
    name, remainder = parse_selector(selector)
    definitions = scope.definitions(name)
    selector_keys = {d.key for d in definitions}
    flag_keys: set[DefinitionKey] = set()
    if remainder.startswith("@") and "/" not in remainder:
        flag_keys = {
            flag_def.key
            for definition in definitions
            for flag_def in _flag_slot_definitions(scope, definition, remainder[1:])
        }
    labels: set[str] = set()
    for item in _all_items(model, selector):
        _assert_no_none(item, "item")
        json.dumps(item, allow_nan=False)
        assert set(item) <= _DOCUMENTED_KEYS, set(item) - _DOCUMENTED_KEYS
        assert all(_HYPHENATED.fullmatch(key) for key in item), sorted(item)
        _assert_value_types(item)

        constraint = by_location[_location(item)]
        flag_definition = None
        if remainder:
            same_document = [a for a in constraint.alternatives if not a.cross_document]
            paths = {a.path for a in same_document}
            direct = constraint.definition_key in selector_keys and remainder in paths
            via_flag = constraint.definition_key in flag_keys and "" in paths
            assert direct != via_flag, f"{selector}: ambiguous match {_location(item)}"
            path = remainder if direct else ""
            alternatives = [a for a in same_document if a.path == path]
            if via_flag:
                flag_definition = constraint.definition_key.name
        else:
            alternatives = list(constraint.alternatives)
        expected = _expected_item(item, constraint, name, alternatives, flag_definition, source)

        got = dict(item)
        if got.pop("truncated", False):
            # paginate_within_budget shortened free text to fit the Response_Budget.
            for key in _TRUNCATABLE:
                if key in got and got[key] != expected.get(key):
                    assert got[key].endswith("…"), key
                    assert expected[key].startswith(got[key][: -len("…")]), key
                    got[key] = expected[key]
            labels.add("truncated")
        assert got == expected, f"{model} {selector}"
        labels.add(f"type={constraint.type}")
        labels.update(f"key={key}" for key in set(item) - COMMON_KEYS)
    return labels


@st.composite
def _shape_attrs(draw: st.DrawFn, ctype: str, targets: tuple[str | None, ...]) -> str:
    """Start-tag attributes: common ones, and each type-specific one declared or not."""
    pools: dict[str, tuple[str, ...]] = {
        "id": ("c-1", "c-2"),
        "level": ("WARNING", "CRITICAL"),
        **_SHAPE_TYPE_ATTRS[ctype],
    }
    drawn = {"target": draw(st.sampled_from(targets))}
    drawn |= {attr: draw(st.sampled_from((None, *values))) for attr, values in pools.items()}
    declared = {attr: value for attr, value in drawn.items() if value is not None}
    names = draw(st.permutations(list(declared)))
    return "".join(f" {name}={quoteattr(declared[name])}" for name in names)


# Values include characters that need escaping, to check verbatim reporting.
_SHAPE_TYPE_ATTRS: dict[str, dict[str, tuple[str, ...]]] = {
    "allowed-values": {"allow-other": ("yes", "no"), "extensible": ("model", "external", "none")},
    "matches": {"datatype": ("token", "date"), "regex": ("[a-z]+", "a<b & 'c'\t\"d\"")},
    "index": {"name": ("idx-a", "idx b")},
    "index-has-key": {"name": ("idx-a",)},
    "is-unique": {},
    "has-cardinality": {"min-occurs": ("0", "1"), "max-occurs": ("1", "unbounded")},
    "expect": {"test": ("count(x) > 1", "@a = 'b' and @c != \"d\"")},
}
_SHAPE_KEY_FIELDS = ("@uuid", "@id", "prop[@name='x']/@value")
_SHAPE_MESSAGES = ("plain text", "Must <code>x</code>\n   be <em>set</em>.")
_SHAPE_NS_TARGETS = (
    "(.)[has-oscal-namespace('http://ns/a') and @f='x']/@g",
    "b[has-oscal-namespace('http://ns/a')]/@f|b/@f",
    "b[has-namespace('http://ns/b')]/@f|b[has-oscal-namespace('http://ns/a')]/@f",
    "@f[has-oscal-namespace(('u1', 'u2'))]",
)
_SHAPE_TARGETS = (*_SYN_TARGETS, *_SHAPE_NS_TARGETS)
_SHAPE_FLAG_TARGETS = (
    *_SYN_FLAG_TARGETS,
    "(.)[has-oscal-namespace('http://ns/a')]",
    ".[has-namespace('http://ns/b')]|(.)[has-oscal-namespace('http://ns/a')]",
)


@st.composite
def _shape_constraint(draw: st.DrawFn, targets: tuple[str | None, ...]) -> str:
    """One constraint of any of the seven types; its start tag is on a line of its own."""
    ctype = draw(st.sampled_from(CONSTRAINT_TYPES))
    children: list[str] = []
    if ctype == "allowed-values":
        enums = ['<enum value="v1"/>', '<enum value="v2"/>', "&e;"]
        children += draw(st.lists(st.sampled_from(enums), max_size=2))
    if ctype in _KEY_FIELD_TYPES:
        fields = draw(st.lists(st.sampled_from(_SHAPE_KEY_FIELDS), max_size=2))
        children += [f"<key-field target={quoteattr(f)}/>" for f in fields]
    if ctype == "expect" and (message := draw(st.none() | st.sampled_from(_SHAPE_MESSAGES))):
        children.append(f"<message>{message}</message>")
    return "\n".join([f"<{ctype}{draw(_shape_attrs(ctype, targets))}>", *children, f"</{ctype}>"])


@st.composite
def _shape_constraints(draw: st.DrawFn, targets: tuple[str | None, ...]) -> str:
    parts = draw(st.lists(_shape_constraint(targets), max_size=4))
    return "\n".join(["<constraint>", *parts, "</constraint>"]) if parts else ""


@st.composite
def _shape_flag_slot(draw: st.DrawFn) -> str:
    use = draw(st.none() | st.sampled_from(_SYN_USE_NAMES))
    use_name = "" if use is None else f"<use-name>{use}</use-name>"
    name = draw(st.sampled_from(_SYN_FLAGS))
    if draw(st.booleans()):
        constraints = draw(_shape_constraints(_SHAPE_FLAG_TARGETS))
        return f'<define-flag name="{name}">{use_name}\n{constraints}\n</define-flag>'
    return f'<flag ref="{name}">{use_name}</flag>'


@st.composite
def _shape_definition(draw: st.DrawFn, *, local: bool = False) -> str:
    kind = draw(st.sampled_from(["assembly", "field"]))
    parts = [f'<define-{kind} name="{draw(st.sampled_from(_SYN_DEFS))}">']
    parts += draw(st.lists(_shape_flag_slot(), max_size=2))
    if kind == "assembly" and not local:
        children = draw(st.lists(_shape_definition(local=True), max_size=2))
        if children:
            parts += ["<model>", *children, "</model>"]
    parts += [draw(_shape_constraints(_SHAPE_TARGETS)), f"</define-{kind}>"]
    return "\n".join(parts)


@st.composite
def _shape_global_flag(draw: st.DrawFn) -> str:
    name = draw(st.sampled_from(_SYN_FLAGS))
    constraints = draw(_shape_constraints(_SHAPE_FLAG_TARGETS))
    return f'<define-flag name="{name}">\n{constraints}\n</define-flag>'


_shape_module_body = st.lists(
    st.one_of(_shape_definition(), _shape_global_flag()), min_size=1, max_size=4
).map("\n".join)


@pytest.mark.usefixtures("reset_metaschema_index")
class TestItemShapeProperty:
    """Property 18: get_constraints item shape.

    Every item has the common keys, exactly the type-specific keys whose source attribute
    or child is declared (values verbatim from the source text at the item's
    Source_Location), ``namespace``/``conditions`` per Property 1's rules for the matched
    alternatives, no ``None`` values, and only documented hyphenated keys.

    **Validates: Requirements 8.6, 8.7**
    """

    @settings(
        max_examples=200,
        deadline=None,
        # The index singleton is deliberately shared across examples (read-only).
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    @given(pair=st.deferred(_real_pairs))
    def test_real_bundle_item_shape(self, pair):
        model, selector = pair
        scope = get_index().scope(OSCALModelType(model))
        labels = _check_item_shapes(scope, _real_by_location(scope), _real_source, model, selector)
        event(f"mode={'path' if parse_selector(selector)[1] else 'bare'}")
        for label in sorted(labels):
            event(label)

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(cat=_shape_module_body, x=_shape_module_body)
    def test_synthetic_bundles_item_shape(self, cat, x):
        files: dict[str, str | bytes] = {
            CAT: _module(_imports(_SYN_X) + "\n" + cat, _SYN_DOCTYPE),
            _SYN_X: _module(x, _SYN_DOCTYPE),
            ENT: _SYN_ENT,
        }
        labels: set[str] = set()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_bundle(root, files)
            index = MetaschemaIndex.load(root)
            assert dict(index.load_errors) == {}
            scope = index.scope(OSCALModelType.CATALOG)
            by_location = _constraints_by_location(scope)

            def source(source_file: str) -> str:
                return (root / source_file).read_text(encoding="utf-8")

            with patch.object(gc_module, "get_index", return_value=index):
                for selector in _syn_selectors(scope):
                    if scope.definitions(parse_selector(selector)[0]):
                        labels |= _check_item_shapes(
                            scope, by_location, source, "catalog", selector
                        )
        for label in sorted(labels):
            event(label)
