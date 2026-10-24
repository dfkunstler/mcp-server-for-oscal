"""Tests for the find_references_to tool (Feature: metaschema-constraints).

Smoke tests from task 14.1, the Property 19 oracle test (task 14.3), and the required
example tests (task 14.4, ``TestRequiredExamples``).
"""

import asyncio
import functools
import inspect
import json
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from mcp.server.mcpserver import Context, MCPServer

from mcp_server_for_oscal.tools import find_references_to as fr_module
from mcp_server_for_oscal.tools.find_references_to import find_references_to
from mcp_server_for_oscal.tools.metaschema_index import (
    BUNDLE_DIR,
    Constraint,
    MetaschemaIndex,
    MetaschemaLoadError,
    ModelScope,
)
from mcp_server_for_oscal.tools.metaschema_paths import PathAlternative, normalize_target
from mcp_server_for_oscal.tools.utils import OSCALModelType, paginate

from .test_metaschema_index import CAT, PROF, _imports, _mod, _module, _write_bundle

ITEM_KEYS = {
    "index-name",
    "definition",
    "target",
    "key-fields",
    "source-file",
    "line",
    "index-definition",
    "index-target",
    "index-source-file",
    "index-line",
    "cross-document",
}


def _all_items(assembly: str, model: str | None = None) -> list[dict]:
    """Walk every page of a result and return the items in order."""
    items: list[dict] = []
    offset = 0
    while True:
        page = find_references_to(assembly=assembly, model=model, offset=offset, limit=100)
        items.extend(page["items"])
        if not page["hasMore"]:
            return items
        offset += len(page["items"])


def _order_key(item: dict) -> tuple[str, int, str]:
    return item["source-file"], item["line"], item["index-name"]


@pytest.mark.usefixtures("reset_metaschema_index")
class TestSmoke:
    """Smoke tests against the bundled OSCAL 1.2.3 Metaschema."""

    def test_ssp_component_references(self):
        items = _all_items("component", "system-security-plan")
        refs = {(i["index-name"], i["target"], tuple(i["key-fields"])) for i in items}
        assert (
            "index-system-implementation-component-uuid",
            "component/link[@rel='depends-on']",
            ("@href",),
        ) in refs
        assert (
            "index-system-implementation-component-uuid-service",
            "component/link[@rel='uses-service']",
            ("@href",),
        ) in refs
        for item in items:
            assert set(item) >= ITEM_KEYS
            assert None not in item.values()
        assert [_order_key(i) for i in items] == sorted(_order_key(i) for i in items)

    def test_conditions_reported_for_has_key_target(self):
        items = _all_items("component", "system-security-plan")
        depends_on = next(i for i in items if i["target"] == "component/link[@rel='depends-on']")
        assert depends_on["index-definition"] == "system-implementation"
        assert depends_on["cross-document"] is False
        assert {"step": "link", "predicate": "@rel='depends-on'"} in depends_on["conditions"]

    def test_definition_use_name_matches(self):
        # system-component is used as "component" in system-implementation's model.
        by_use_name = _all_items("system-component", "system-security-plan")
        by_instance = _all_items("component", "system-security-plan")
        assert by_use_name == by_instance

    def test_by_component_is_cross_document(self):
        items = _all_items("by-component", "system-security-plan")
        assert items
        assert all(i["index-name"] == "by-component-uuid" for i in items)
        assert all(i["cross-document"] is True for i in items)
        assert {"link[@rel='provided-by']"} <= {i["target"] for i in items}

    def test_index_resolved_across_modules(self):
        # The location index is declared in metadata; references live in other modules.
        items = _all_items("location", "system-security-plan")
        assert any(i["source-file"] != i["index-source-file"] for i in items)
        assert all(i["index-source-file"] == "oscal_metadata_metaschema.xml" for i in items)

    def test_model_filter_restricts_has_key_modules(self):
        everywhere = _all_items("component")
        cdef = _all_items("component", "component-definition")
        assert len(cdef) < len(everywhere)
        assert not any(i["source-file"] == "oscal_ssp_metaschema.xml" for i in cdef)

    def test_empty_and_unknown_assembly_return_empty_page(self):
        for assembly in ("", "no-such-assembly", "Component"):
            page = find_references_to(assembly=assembly)
            assert page["items"] == []
            assert page["total"] == 0
            assert page["hasMore"] is False


class TestErrors:
    def test_invalid_model_notifies_and_raises(self):
        with (
            patch.object(fr_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match="list_oscal_models") as excinfo,
        ):
            find_references_to(assembly="component", model="not-a-model")
        assert "not-a-model" in str(excinfo.value)
        notify.assert_called_once()

    def test_non_string_assembly_raises(self):
        with (
            patch.object(fr_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match="assembly"),
        ):
            find_references_to(assembly=None)  # type: ignore[arg-type]
        notify.assert_called_once()

    @pytest.mark.parametrize(("offset", "limit"), [(-1, 25), (0, 0), (0, 101)])
    def test_bad_paging_raises_before_index_access(self, offset, limit):
        with (
            patch.object(fr_module, "get_index") as get_index,
            pytest.raises(ValueError, match=r"offset|limit"),
        ):
            find_references_to(assembly="component", offset=offset, limit=limit)
        get_index.assert_not_called()


# --------------------------------------------------------------------------------------
# Property 19: find_references_to pairs every key reference with its index
# --------------------------------------------------------------------------------------
#
# The oracle scans ``MetaschemaIndex.all_modules()`` directly and does not use the tool's
# matching helpers. Its rules (Requirements 9.2-9.8, 9.11, plus the 14.1 resolutions):
# - names: ``assembly``, the ``use-name`` of any Definition named ``assembly``, and the
#   effective name of any model child whose ``ref`` is ``assembly``; none for "".
# - an ``index`` matches when the last step (text after the final ``/``) of any alternative's
#   Normalized_Path, cross-document alternatives included, is one of the names; a
#   parenthesized union step matches each member.
# - indexes come from every module; ``model`` only filters where ``index-has-key`` is declared.
# - order: has-key (source-file, line), index-name, index (source-file, line).


def _oracle_constraints(bundle: ModelScope) -> list[Constraint]:
    """Every Constraint in ``bundle``, each once, in scan order."""
    seen: set[int] = set()
    found: list[Constraint] = []
    for name in bundle.definition_names():
        for definition in bundle.definitions(name):
            for constraint in definition.constraints:
                if id(constraint) not in seen:
                    seen.add(id(constraint))
                    found.append(constraint)
    return found


def _oracle_names(bundle: ModelScope, assembly: str) -> set[str]:
    if assembly == "":
        return set()
    names = {assembly}
    for name in bundle.definition_names():
        for definition in bundle.definitions(name):
            if definition.key.name == assembly and definition.use_name:
                names.add(definition.use_name)
            names.update(c.effective_name for c in definition.model_children if c.ref == assembly)
    return names


_UNION_STEP = re.compile(r"\((?P<members>[^()]*)\)")


def _oracle_last_step(alternative: PathAlternative) -> set[str]:
    if alternative.path is None:
        return set()
    step = alternative.path.split("/")[-1]
    union = _UNION_STEP.fullmatch(step)
    members = union.group("members").split("|") if union else [step]
    return {m.strip() for m in members} - {""}


def _oracle_conditions(item: dict, alternatives: tuple[PathAlternative, ...]) -> None:
    """Requirement 9.5 via 6.4/6.5: one distinct namespace is reported and left out."""
    namespaces = {a.namespace for a in alternatives if a.namespace is not None}
    single = len(namespaces) == 1
    conditions: list[dict] = []
    for alt in alternatives:
        source = alt
        if not single and alt.namespace is not None:
            (source,) = normalize_target(alt.raw, extract_namespace=False)
        for c in source.conditions:
            entry = {"step": c.step, "predicate": c.predicate}
            if entry not in conditions:
                conditions.append(entry)
    if single:
        item["namespace"] = next(iter(namespaces))
    if conditions:
        item["conditions"] = conditions


def _oracle_key(item: dict) -> tuple:
    return (
        item["source-file"],
        item["line"],
        item["index-name"],
        item["index-source-file"],
        item["index-line"],
    )


def _oracle(index: MetaschemaIndex, assembly: str, model: str | None) -> list[dict]:
    """Brute-force expected items, sorted by the Requirement 9.11 key (stable)."""
    bundle = index.all_modules()
    modules = None if model is None else set(index.scope(OSCALModelType(model)).modules)
    names = _oracle_names(bundle, assembly)
    constraints = _oracle_constraints(bundle)
    matched = [
        c
        for c in constraints
        if c.type == "index"
        and "name" in c.attrs
        and any(_oracle_last_step(a) & names for a in c.alternatives)
    ]
    items = []
    for has_key in constraints:
        if has_key.type != "index-has-key" or "name" not in has_key.attrs:
            continue
        if modules is not None and has_key.definition_key.module not in modules:
            continue
        for idx in matched:
            if idx.attrs["name"] != has_key.attrs["name"]:
                continue
            item = {
                "index-name": idx.attrs["name"],
                "definition": has_key.definition_key.name,
                "target": has_key.target,
                "key-fields": list(has_key.key_fields),
                "source-file": has_key.location.source_file,
                "line": has_key.location.line,
                "index-definition": idx.definition_key.name,
                "index-target": idx.target,
                "index-source-file": idx.location.source_file,
                "index-line": idx.location.line,
                "cross-document": any(a.raw.lstrip().startswith("doc(") for a in idx.alternatives),
            }
            _oracle_conditions(item, has_key.alternatives)
            items.append(item)
    return sorted(items, key=_oracle_key)


def _walk(assembly: str, model: str | None, limit: int) -> list[dict]:
    """Every item of the result, walking pages of ``limit``; checks the page invariants."""
    items: list[dict] = []
    offset = 0
    total = None
    while True:
        page = find_references_to(assembly=assembly, model=model, offset=offset, limit=limit)
        total = page["total"] if total is None else total
        assert page["total"] == total
        assert page["offset"] == offset
        items.extend(page["items"])
        if not page["hasMore"]:
            break
        assert page["items"]
        offset += len(page["items"])
    assert len(items) == total
    return items


def _check(index: MetaschemaIndex, assembly: str, model: str | None, limit: int) -> list[dict]:
    """Compare the walked tool result with the oracle; return the items."""
    expected = _oracle(index, assembly, model)
    with patch.object(fr_module, "get_index", return_value=index):
        got = _walk(assembly, model, limit)

    # Same multiset of items (9.2-9.7), in Requirement 9.11 order. Items whose order keys tie
    # (one entity line included by two modules) may appear in either order.
    def dump(item: dict) -> str:
        return json.dumps(item, sort_keys=True)

    assert sorted(map(dump, got)) == sorted(map(dump, expected))
    assert [_oracle_key(i) for i in got] == [_oracle_key(i) for i in expected]
    for item in got:
        assert None not in item.values()
        assert isinstance(item["key-fields"], list)
    if not got:
        assert expected == []  # 9.8
    return got


@functools.cache
def _real_index() -> MetaschemaIndex:
    """A private load of the real bundle, independent of the ``get_index()`` singleton."""
    return MetaschemaIndex.load()


@functools.cache
def _real_assembly_pools() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(index last steps, definition names + use-names + model-child effective names)."""
    bundle = _real_index().all_modules()
    steps: set[str] = set()
    names: set[str] = set(bundle.definition_names())
    for name in bundle.definition_names():
        for definition in bundle.definitions(name):
            if definition.use_name:
                names.add(definition.use_name)
            names.update(c.effective_name for c in definition.model_children)
            for constraint in definition.constraints:
                if constraint.type == "index":
                    for alt in constraint.alternatives:
                        steps.update(_oracle_last_step(alt))
    return tuple(sorted(steps)), tuple(sorted(names))


@st.composite
def _real_assemblies(draw: st.DrawFn) -> str:
    steps, names = _real_assembly_pools()
    return draw(
        st.one_of(
            st.sampled_from(steps),
            st.sampled_from(steps),
            st.sampled_from(names),
            st.sampled_from(names).map(str.upper),
            st.just(""),
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", max_size=20),
        )
    )


_MODELS = st.one_of(st.none(), st.sampled_from([m.value for m in OSCALModelType]))
_LIMITS = st.one_of(st.integers(1, 5), st.integers(1, 100))

# Synthetic bundles: catalog imports X; profile stands alone. Small name pools make matches,
# unions, shared index names, and unrelated same-named constraints frequent.
_SYN_X = _mod("x")
_SYN_NAMES = ("a", "b", "comp", "part")
_SYN_INDEX_NAMES = ("i1", "i2", "i3")
_SYN_PREDICATES = ("", "[@rel='r']", "[has-oscal-namespace('http://n/a')]", "[@type='t']")

_syn_name = st.sampled_from(_SYN_NAMES)
_syn_simple_step = st.builds(lambda n, p: n + p, _syn_name, st.sampled_from(_SYN_PREDICATES))
_syn_union_step = st.lists(_syn_name, min_size=2, max_size=3, unique=True).map(
    lambda ms: "(" + "|".join(ms) + ")"
)


@st.composite
def _syn_alternative(draw: st.DrawFn) -> str:
    steps = draw(st.lists(_syn_simple_step, min_size=0, max_size=2))
    steps.append(draw(st.one_of(_syn_simple_step, _syn_union_step)))
    seps = [draw(st.sampled_from(["/", "//"])) for _ in steps[1:]]
    path = steps[0] + "".join(s + step for s, step in zip(seps, steps[1:], strict=True))
    prefix = draw(st.sampled_from(["", "(.)/", ".//", "doc(link[@rel='r']/@href)/"]))
    return prefix + path


@st.composite
def _syn_constraint(draw: st.DrawFn) -> str:
    kind = draw(st.sampled_from(["index", "index-has-key", "index-has-key", "is-unique"]))
    name = draw(st.sampled_from(_SYN_INDEX_NAMES))
    target = "|".join(draw(st.lists(_syn_alternative(), min_size=1, max_size=3)))
    keys = draw(st.lists(st.sampled_from(["@uuid", "@id", "@href"]), max_size=2))
    key_xml = "".join(f'<key-field target="{k}"/>' for k in keys)
    return f'<{kind} name="{name}" target="{target}">{key_xml}</{kind}>'


@st.composite
def _syn_definition(draw: st.DrawFn) -> str:
    name = draw(_syn_name)
    use_name = draw(st.one_of(st.none(), _syn_name))
    children = []
    for ref, child_use in draw(
        st.lists(st.tuples(_syn_name, st.one_of(st.none(), _syn_name)), max_size=2)
    ):
        use_xml = f"<use-name>{child_use}</use-name>" if child_use else ""
        children.append(f'<assembly ref="{ref}">{use_xml}</assembly>')
    constraints = draw(st.lists(_syn_constraint(), max_size=4))
    # One element per line, so every Constraint has a distinct Source_Location.
    return "\n".join(
        [
            f'<define-assembly name="{name}">',
            f"<use-name>{use_name}</use-name>" if use_name else "",
            "<model>" + "".join(children) + "</model>",
            "<constraint>",
            *constraints,
            "</constraint>",
            "</define-assembly>",
        ]
    )


_syn_module_body = st.lists(_syn_definition(), max_size=3).map("\n".join)


class TestReferencePairingProperty:
    """Property 19: find_references_to pairs every key reference with its index.

    **Validates: Requirements 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.11**
    """

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(assembly=_real_assemblies(), model=_MODELS, limit=_LIMITS)
    def test_real_bundle_matches_oracle(self, assembly, model, limit):
        got = _check(_real_index(), assembly, model, limit)
        event(f"items={'yes' if got else 'no'}")
        event(f"cross-document={any(i['cross-document'] for i in got)}")
        event(f"cross-module={any(i['source-file'] != i['index-source-file'] for i in got)}")

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        cat=_syn_module_body,
        x=_syn_module_body,
        prof=_syn_module_body,
        assembly=st.one_of(
            _syn_name, _syn_name, st.just(""), st.text(alphabet="abcomp", max_size=4)
        ),
        model=st.sampled_from([None, OSCALModelType.CATALOG.value, OSCALModelType.PROFILE.value]),
        limit=_LIMITS,
    )
    def test_synthetic_bundles_match_oracle(self, cat, x, prof, assembly, model, limit):
        files: dict[str, str | bytes] = {
            CAT: _module(_imports(_SYN_X) + "\n" + cat),
            _SYN_X: _module(x),
            PROF: _module(prof),
        }
        with tempfile.TemporaryDirectory() as tmp:
            _write_bundle(Path(tmp), files)
            index = MetaschemaIndex.load(Path(tmp))
            assert dict(index.load_errors) == {}
            got = _check(index, assembly, model, limit)
        event(f"items={'yes' if got else 'no'}")
        event(f"cross-document={any(i['cross-document'] for i in got)}")
        event(f"cross-module={any(i['source-file'] != i['index-source-file'] for i in got)}")
        event(f"union={any('(' in i['index-target'].split('/')[-1] for i in got)}")


# --------------------------------------------------------------------------------------
# Required examples (task 14.4)
# --------------------------------------------------------------------------------------
#
# The bundled OSCAL 1.2.3 Metaschema has no ``index-has-key`` on ``by-component``
# ``@component-uuid`` (only ``is-unique`` constraints use it). The SSP ``component`` example
# therefore checks the ``depends-on`` / ``uses-service`` link references that do exist, and the
# ``by-component`` example checks the ``provided-by`` link reference to the cross-document
# ``by-component-uuid`` index. Line numbers are checked against the bundled file text.

_SSP = "oscal_ssp_metaschema.xml"

# (index-has-key target, index-name, has-key line, index-target, index line)
_SSP_COMPONENT_REFS = [
    (
        "component/link[@rel='depends-on']",
        "index-system-implementation-component-uuid",
        613,
        "component",
        610,
    ),
    (
        "component/link[@rel='uses-service']",
        "index-system-implementation-component-uuid-service",
        636,
        "component[@type='service']",
        633,
    ),
]


@functools.cache
def _bundle_lines(source_file: str) -> tuple[str, ...]:
    return tuple((BUNDLE_DIR / source_file).read_text(encoding="utf-8").splitlines())


def _assert_declared_at(source_file: str, line: int, element: str, name: str, target: str):
    """The bundled ``source_file`` declares ``<element name=... target=...>`` on ``line``."""
    text = _bundle_lines(source_file)[line - 1]
    assert f"<{element} " in text, text
    assert f'name="{name}"' in text, text
    assert f'target="{target}"' in text, text


@pytest.mark.usefixtures("reset_metaschema_index")
class TestRequiredExamples:
    """Validates: Requirements 9.1, 9.8, 9.9, 9.10"""

    def test_signature_and_defaults(self):
        """Req 9.1: assembly, model=None, offset=0, limit=25, ctx=None, with their types."""
        params = inspect.signature(find_references_to).parameters
        expected = [
            ("assembly", str, inspect.Parameter.empty),
            ("model", str | None, None),
            ("offset", int, 0),
            ("limit", int, 25),
            ("ctx", Context | None, None),
        ]
        assert [(p.name, p.annotation, p.default) for p in params.values()] == expected

    def test_mcp_input_schema_hides_ctx(self):
        """The MCP server injects ``ctx``; callers see only the four documented arguments."""
        server = MCPServer("test")
        server.add_tool(find_references_to)
        (tool_def,) = asyncio.run(server.list_tools())
        assert tool_def.name == "find_references_to"
        schema = tool_def.input_schema
        assert list(schema["properties"]) == ["assembly", "model", "offset", "limit"]
        assert schema["required"] == ["assembly"]
        assert schema["properties"]["offset"]["default"] == 0
        assert schema["properties"]["limit"]["default"] == 25

    @pytest.mark.parametrize(
        ("has_key_target", "index_name", "line", "index_target", "index_line"),
        _SSP_COMPONENT_REFS,
    )
    def test_ssp_component_link_reference_exact(
        self, has_key_target, index_name, line, index_target, index_line
    ):
        """Req 9.1: SSP ``component`` returns its link references with exact values."""
        items = _all_items("component", "system-security-plan")
        (item,) = [i for i in items if i["target"] == has_key_target]
        assert item == {
            "index-name": index_name,
            "definition": "system-implementation",
            "target": has_key_target,
            "key-fields": ["@href"],
            "source-file": _SSP,
            "line": line,
            "index-definition": "system-implementation",
            "index-target": index_target,
            "index-source-file": _SSP,
            "index-line": index_line,
            "cross-document": False,
            "conditions": [{"step": "link", "predicate": has_key_target.split("[", 1)[1][:-1]}],
        }
        _assert_declared_at(_SSP, line, "index-has-key", index_name, has_key_target)
        _assert_declared_at(_SSP, index_line, "index", index_name, index_target)

    def test_ssp_by_component_provided_by_reference_exact(self):
        """Req 9.1: ``by-component`` is referenced by ``provided-by`` links (cross-document)."""
        items = _all_items("by-component", "system-security-plan")
        (item,) = [i for i in items if i["target"] == "link[@rel='provided-by']"]
        index_target = (
            "control-implementation/implemented-requirement//by-component|"
            "doc(system-implementation/leveraged-authorization/link[@rel='system-security-plan']"
            "/@href)/system-security-plan/control-implementation/implemented-requirement"
            "//by-component"
        )
        assert item["index-name"] == "by-component-uuid"
        assert item["definition"] == "by-component"
        assert item["key-fields"] == ["@href"]
        assert (item["source-file"], item["line"]) == (_SSP, 1047)
        assert item["index-definition"] == "system-security-plan"
        assert item["index-target"] == index_target
        assert (item["index-source-file"], item["index-line"]) == (_SSP, 49)
        assert item["cross-document"] is True
        _assert_declared_at(
            _SSP, 1047, "index-has-key", "by-component-uuid", "link[@rel='provided-by']"
        )
        _assert_declared_at(_SSP, 49, "index", "by-component-uuid", index_target)

    def test_empty_assembly_returns_exact_empty_page(self):
        """Req 9.8: an empty ``assembly`` returns an empty Page_Response."""
        for model in (None, "system-security-plan"):
            page = find_references_to(assembly="", model=model)
            assert page == {"items": [], "total": 0, "offset": 0, "limit": 25, "hasMore": False}

    def test_offset_past_end_returns_empty_with_total(self):
        total = find_references_to(assembly="component", model="system-security-plan")["total"]
        assert total >= len(_SSP_COMPONENT_REFS)
        for offset in (total, total + 7):
            page = find_references_to(
                assembly="component", model="system-security-plan", offset=offset
            )
            assert page["items"] == []
            assert page["total"] == total
            assert page["offset"] == offset
            assert page["hasMore"] is False

    def test_invalid_model_message(self):
        """Req 9.9: the message names the invalid model and points to list_oscal_models."""
        with (
            patch.object(fr_module, "get_index") as get_index,
            patch.object(fr_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match="list_oscal_models") as excinfo,
        ):
            find_references_to(assembly="component", model="SSP")
        message = str(excinfo.value)
        assert "'SSP'" in message
        notify.assert_called_once()
        assert notify.call_args.args[0] == message
        get_index.assert_not_called()

    @pytest.mark.parametrize(("offset", "limit"), [(-1, 25), (0, 0), (0, 101), (-5, 0)])
    def test_paging_error_equals_paginate_message(self, offset, limit):
        """Req 9.10: the ValueError is the one ``utils.paginate`` produces."""
        with pytest.raises(ValueError) as expected:
            paginate([], offset, limit)
        with (
            patch.object(fr_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError) as excinfo,
        ):
            find_references_to(assembly="component", offset=offset, limit=limit)
        assert str(excinfo.value) == str(expected.value)
        notify.assert_called_once()

    def test_load_error_notifies_once_and_raises(self):
        error = MetaschemaLoadError("OSCAL Metaschema bundle cannot be loaded: test")
        with (
            patch.object(fr_module, "get_index", side_effect=error),
            patch.object(fr_module, "try_notify_client_error") as notify,
            pytest.raises(MetaschemaLoadError) as excinfo,
        ):
            find_references_to(assembly="component")
        assert excinfo.value is error
        notify.assert_called_once()
        assert notify.call_args.args[0] == str(error)
