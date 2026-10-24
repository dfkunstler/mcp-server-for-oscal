"""Tests for the get_definition tool (Feature: metaschema-constraints).

Smoke tests from task 16.1, Property 20 from task 16.3, and the required examples from
task 16.4.
"""

import asyncio
import inspect
import itertools
import json
import re
import tempfile
from functools import cache
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from hypothesis.control import currently_in_test_context
from mcp.server.mcpserver import Context, MCPServer

from mcp_server_for_oscal.tools import get_definition as gd_module
from mcp_server_for_oscal.tools.get_definition import get_definition
from mcp_server_for_oscal.tools.metaschema_common import shown, suggest_definition_names
from mcp_server_for_oscal.tools.metaschema_index import (
    Definition,
    DefinitionKey,
    MetaschemaIndex,
    MetaschemaLoadError,
    ModelScope,
    get_index,
)
from mcp_server_for_oscal.tools.utils import (
    MAX_PAGE_LIMIT,
    RESPONSE_BUDGET,
    TRUNCATION_MARK,
    OSCALModelType,
)

from .test_get_allowed_values import _GONE_ENT, _PROFILE_BODY
from .test_metaschema_index import (
    CAT,
    PROF,
    _doctype,
    _ent_decl,
    _imports,
    _mod,
    _module,
    _write_bundle,
)

REQUIRED_KEYS = {"kind", "name", "scope", "source-file", "line"}
_MODEL_VALUES = [m.value for m in OSCALModelType]


def _all_matches(name: str, model: str | None = None, kind: str | None = None) -> list[dict]:
    """Walk every page of a result and return the matches in order."""
    items: list[dict] = []
    offset = 0
    while True:
        page = get_definition(name=name, model=model, kind=kind, offset=offset, limit=100)
        assert len(json.dumps(page)) <= RESPONSE_BUDGET
        items.extend(page["items"])
        if not page["hasMore"]:
            return items
        offset += len(page["items"])


def _has_null(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_has_null(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_null(v) for v in value)
    return False


@pytest.mark.usefixtures("reset_metaschema_index")
class TestSmoke:
    """Smoke tests against the bundled OSCAL 1.2.3 Metaschema."""

    def test_defined_component(self):
        matches = _all_matches("defined-component", "component-definition")
        assert len(matches) == 1
        match = matches[0]
        assert match["kind"] == "assembly"
        assert match["scope"] == "global"
        assert match["formal-name"] == "Component"
        assert match["source-file"] == "oscal_component_metaschema.xml"
        assert "parent-definition" not in match
        flags = {f["name"]: f for f in match["flags"]}
        assert flags["uuid"] == {
            "name": "uuid",
            "required": True,
            "inline": True,
            "datatype": "uuid",
        }
        assert flags["type"]["ref"] == "defined-component-type"
        assert flags["type"]["required"] is True
        children = {c["name"]: c for c in match["model"]}
        assert children["title"]["min-occurs"] == 1
        assert children["prop"] == {
            "name": "prop",
            "kind": "assembly",
            "min-occurs": 0,
            "max-occurs": "unbounded",
            "group-as": "props",
            "ref": "property",
        }
        assert any(c.get("constraint-id") for c in match["constraints"])
        assert not _has_null(match)

    def test_uuid_across_modules_is_paged_within_budget(self):
        matches = _all_matches("uuid")
        assert len(matches) > 10
        assert all(m["name"] == "uuid" for m in matches)
        locations = [(m["source-file"], m["line"]) for m in matches]
        assert locations == sorted(locations)
        assert len(set(locations)) == len(locations)
        assert all("parent-definition" in m for m in matches if m["scope"] == "local")
        assert not _has_null(matches)

    def test_kind_filter(self):
        assert {m["kind"] for m in _all_matches("uuid", kind="flag")} == {"flag"}

    def test_remarks_keep_blocks(self):
        matches = _all_matches("part", "catalog", "assembly")
        remarks = [m["remarks"] for m in matches if "remarks" in m]
        assert any("\n\n" in r and "\n- " in r for r in remarks)


@pytest.mark.usefixtures("reset_metaschema_index")
class TestErrors:
    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            ({"name": "uuid", "kind": "module"}, "Valid kinds: assembly, field, flag"),
            ({"name": "uuid", "model": "nope"}, "list_oscal_models"),
            ({"name": ""}, "Invalid definition name"),
            ({"name": "x" * 201}, "Invalid definition name"),
            ({"name": "defined-componnet"}, "defined-component"),
        ],
    )
    def test_errors_notify_and_raise(self, kwargs, fragment):
        with (
            patch.object(gd_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match=re.escape(fragment)),
        ):
            get_definition(**kwargs)
        notify.assert_called_once()

    def test_invalid_kind_does_not_read_index(self):
        with (
            patch.object(gd_module, "get_index") as get_index,
            pytest.raises(ValueError, match="Invalid kind"),
        ):
            get_definition(name="uuid", kind="module")
        get_index.assert_not_called()

    def test_wrong_kind_suggests_names_of_that_kind(self):
        with pytest.raises(ValueError, match="no Metaschema field definition") as err:
            get_definition(name="defined-component", kind="field")
        suggestions = str(err.value).split("Nearest definition names: ")[1].split(", ")
        assert "defined-component" not in suggestions
        assert 1 <= len(suggestions) <= 10


# --------------------------------------------------------------------------------------
# Required examples (task 16.4)
# --------------------------------------------------------------------------------------

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_DEFINE_TAG = re.compile(
    r"<(/?)define-(?:assembly|field|flag)\b((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.DOTALL
)
_NAME_ATTR = re.compile(r"\bname=\"([^\"]*)\"")


def _source_definitions(text: str, name: str) -> list[tuple[int, str | None]]:
    """``(line, enclosing definition name)`` of every ``define-*`` named ``name`` in ``text``.

    A plain scan of the module source, independent of the index: comments are blanked
    (newlines kept, so line numbers hold), and a stack of open ``define-*`` tags gives the
    enclosing Definition.
    """
    text = _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)
    stack: list[str] = []
    found: list[tuple[int, str | None]] = []
    for tag in _DEFINE_TAG.finditer(text):
        closing, attrs = tag.group(1), tag.group(2)
        if closing:
            stack.pop()
            continue
        attr = _NAME_ATTR.search(attrs)
        assert attr is not None
        if attr.group(1) == name:
            found.append((text.count("\n", 0, tag.start()) + 1, stack[-1] if stack else None))
        if not attrs.rstrip().endswith("/"):
            stack.append(attr.group(1))
    assert stack == []
    return found


@pytest.mark.usefixtures("reset_metaschema_index")
class TestRequiredExamples:
    """Validates: Requirements 10.1, 10.5, 10.7, 10.8, 10.9"""

    def test_signature_and_defaults(self):
        """Req 10.1: parameters, types, and defaults in order."""
        params = inspect.signature(get_definition).parameters
        expected = [
            ("name", str, inspect.Parameter.empty),
            ("model", str | None, None),
            ("kind", str | None, None),
            ("offset", int, 0),
            ("limit", int, 10),
            ("ctx", Context | None, None),
        ]
        assert [(p.name, p.annotation, p.default) for p in params.values()] == expected

    def test_mcp_input_schema_hides_ctx(self):
        """The MCP server injects ``ctx``; callers see only the five documented arguments."""
        server = MCPServer("test")
        server.add_tool(get_definition)
        (tool_def,) = asyncio.run(server.list_tools())
        assert tool_def.name == "get_definition"
        schema = tool_def.input_schema
        assert list(schema["properties"]) == ["name", "model", "kind", "offset", "limit"]
        assert schema["required"] == ["name"]
        assert schema["properties"]["offset"]["default"] == 0
        assert schema["properties"]["limit"]["default"] == 10

    def test_uuid_across_modules_matches_bundle_source(self):
        """Req 10.5: every ``uuid`` Definition, by location, with its enclosing Definition."""
        index = get_index()
        expected: list[tuple[str, int, str | None]] = []
        for rel in index.source_files():
            if rel.endswith(".xml"):
                text = "".join(index.source_lines(rel))
                expected += [(rel, line, p) for line, p in _source_definitions(text, "uuid")]
        expected.sort(key=lambda e: (e[0], e[1]))

        matches = _all_matches("uuid")
        # The total equals both the source scan and a private load of the bundle.
        assert (
            len(matches) == len(expected) == len(_bundle_index().all_modules().definitions("uuid"))
        )
        assert len({m["source-file"] for m in matches}) > 1
        got = [(m["source-file"], m["line"], m.get("parent-definition")) for m in matches]
        assert got == expected
        for m in matches:
            assert (m["scope"] == "local") == ("parent-definition" in m)
            line = index.source_lines(m["source-file"])[m["line"] - 1]
            assert f"<define-{m['kind']} " in line
            assert ' name="uuid"' in line
        assert any(m["scope"] == "local" for m in matches)

    def test_uuid_default_page(self):
        """Req 10.1, 10.5: the default page is the first 10 matches of the full order."""
        everything = _all_matches("uuid")
        page = get_definition(name="uuid")
        assert set(page) == {"items", "total", "offset", "limit", "hasMore"}
        assert page["offset"] == 0
        assert page["limit"] == 10
        assert page["total"] == len(everything)
        assert page["hasMore"] is True
        assert page["items"] == everything[:10]

    @pytest.mark.parametrize(
        ("name", "model", "kind"),
        [
            ("defined-componnet", None, None),
            ("contorl", "catalog", None),
            ("uuid", "catalog", "field"),
            ("q" * 150, None, None),
        ],
    )
    def test_no_match_lists_nearest_names(self, name, model, kind):
        """Req 10.7: no match raises with the nearest names, in suggestion order, notified once."""
        index = _bundle_index()
        scope = _scope_of(index, model)
        names = scope.definition_names()
        if kind is not None:
            names = tuple(n for n in names if any(d.key.kind == kind for d in scope.definitions(n)))
        expected = suggest_definition_names(name, names)
        assert 1 <= len(expected) <= 10
        with (
            patch.object(gd_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match=r"^Unknown definition") as exc,
        ):
            get_definition(name=name, model=model, kind=kind)
        message = str(exc.value)
        notify.assert_called_once_with(message, None)
        assert message.split("Nearest definition names: ")[1].split(", ") == expected
        assert f"'{shown(name)}'" in message
        if len(name) > 80:
            assert name not in message
            assert f"'{name[:80]}...'" in message

    @pytest.mark.parametrize(
        ("length", "accepted"), [(0, False), (1, True), (200, True), (201, False)]
    )
    def test_name_length_boundaries(self, length, accepted):
        """Req 10.1, 10.7: 1 and 200 characters are valid names; 0 and 201 are not."""
        name = "z" * length
        with (
            patch.object(gd_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError) as exc,
        ):
            get_definition(name=name)
        message = str(exc.value)
        notify.assert_called_once_with(message, None)
        # No Definition is named "z..."; a valid length fails only for lack of a match.
        prefix = "Unknown definition" if accepted else "Invalid definition name: expected 1-200"
        assert message.startswith(prefix)
        assert "Nearest definition names: " in message

    def test_name_boundaries_match_existing_definitions(self):
        """Req 10.1: a name at a valid boundary length is looked up like any other."""
        names = _bundle_index().all_modules().definition_names()
        shortest = min(names, key=len)
        assert 1 <= len(shortest) <= 200
        assert _all_matches(shortest)

    @pytest.mark.parametrize("model", ["nope", "", "Catalog", "m" * 120])
    def test_invalid_model_names_value_and_list_oscal_models(self, model):
        """Req 10.8: the invalid model is named and list_oscal_models is pointed to."""
        with (
            patch.object(gd_module, "get_index") as get_index_mock,
            patch.object(gd_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match="list_oscal_models") as exc,
        ):
            get_definition(name="uuid", model=model)
        message = str(exc.value)
        assert f"Invalid model: '{shown(model)}'" in message
        notify.assert_called_once_with(message, None)
        get_index_mock.assert_not_called()

    @pytest.mark.parametrize("kind", ["module", "Assembly", "", "flags", 3])
    def test_invalid_kind_lists_valid_kinds(self, kind):
        """Req 10.9: the three valid kinds are listed; the index is not read."""
        with (
            patch.object(gd_module, "get_index") as get_index_mock,
            patch.object(gd_module, "try_notify_client_error") as notify,
            pytest.raises(ValueError, match=r"^Invalid kind") as exc,
        ):
            get_definition(name="uuid", kind=kind)
        message = str(exc.value)
        assert message.endswith("Valid kinds: assembly, field, flag")
        notify.assert_called_once_with(message, None)
        get_index_mock.assert_not_called()

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
            patch.object(gd_module, "get_index", return_value=index),
            patch.object(gd_module, "try_notify_client_error") as notify,
        ):
            for model in ("catalog", None):
                notify.reset_mock()
                with pytest.raises(MetaschemaLoadError) as exc:
                    get_definition(name="c", model=model)
                assert isinstance(exc.value, ValueError)
                message = str(exc.value)
                assert f'{common}: x (SYSTEM "{_GONE_ENT}")' in message
                assert str(tmp_path) not in message
                assert tmp_path.as_posix() not in message
                notify.assert_called_once_with(message, None)

            # A scope that does not reach the failing module still answers.
            notify.reset_mock()
            page = get_definition(name="p", model="profile")
            notify.assert_not_called()
        assert [(i["kind"], i["source-file"]) for i in page["items"]] == [("assembly", PROF)]


# --------------------------------------------------------------------------------------
# Property 20: get_definition returns every same-named Definition within budget
# --------------------------------------------------------------------------------------


def _event(label: str) -> None:
    """Record a Hypothesis event; the helpers are also used by plain example tests."""
    if currently_in_test_context():
        event(label)


@cache
def _bundle_index() -> MetaschemaIndex:
    """A private load of the real bundle, independent of the ``get_index()`` singleton."""
    return MetaschemaIndex.load()


def _scope_of(index: MetaschemaIndex, model: str | None) -> ModelScope:
    return index.all_modules() if model is None else index.scope(OSCALModelType(model))


def _walk_pages(name: str, model: str | None, kind: str | None, limits: list[int]) -> list[dict]:
    """Walk every page with the given limits (cycled); check each page's envelope and budget."""
    items: list[dict] = []
    offset = 0
    total: int | None = None
    for limit in itertools.cycle(limits):
        page = get_definition(name=name, model=model, kind=kind, offset=offset, limit=limit)
        text = json.dumps(page)  # JSON-serializable
        assert len(text) <= RESPONSE_BUDGET
        assert not _has_null(page)
        assert set(page) == {"items", "total", "offset", "limit", "hasMore"}
        if total is None:
            total = page["total"]
        assert page["total"] == total
        assert page["offset"] == offset
        got = page["items"]
        assert 1 <= len(got) <= page["limit"] <= limit
        assert page["hasMore"] == (offset + len(got) < total)
        if page["hasMore"]:
            # A short page only when the budget forced it, and then limit reports its size.
            assert len(got) == limit or page["limit"] == len(got)
        if any(item.get("truncated") for item in got):
            assert len(got) == 1
        if len(got) < limit and page["hasMore"]:
            _event("budget-reduced page")
        items.extend(got)
        offset += len(got)
        if not page["hasMore"]:
            break
    assert len(items) == total
    return items


def _oracle(scope: ModelScope, name: str, kind: str | None) -> list[Definition]:
    """Independent scan of every Definition in the scope: same name and kind, by location."""
    found = [
        d
        for n in scope.definition_names()
        for d in scope.definitions(n)
        if d.key.name == name and (kind is None or d.key.kind == kind)
    ]
    return sorted(found, key=lambda d: (d.key.location.source_file, d.key.location.line))


def _first_global(scope: ModelScope, kind: str, name: str) -> Definition | None:
    """The first global Definition of ``kind`` named ``name`` in scope order (a ``ref``)."""
    return next(
        (d for d in scope.definitions(name) if d.scope == "global" and d.key.kind == kind), None
    )


def _ref_name(effective: str, ref: str, target: Definition | None) -> str:
    """A reference's instance name: its own use-name, else the target's use-name."""
    if effective == ref and target is not None and target.use_name:
        return target.use_name
    return effective


def _expected_flags(scope: ModelScope, by_key: dict[DefinitionKey, Definition], d: Definition):
    expected = []
    for slot in d.flags:
        flag: dict = {"name": slot.effective_name, "required": slot.required}
        target: Definition | None = None
        if slot.inline is not None:
            target = by_key[slot.inline]
            flag["inline"] = True
        elif slot.ref is not None:
            target = _first_global(scope, "flag", slot.ref)
            flag["name"] = _ref_name(slot.effective_name, slot.ref, target)
            flag["ref"] = slot.ref
        if target is not None:
            flag["datatype"] = target.as_type or "string"
        expected.append(flag)
    return expected


def _expected_children(scope: ModelScope, d: Definition) -> list[dict]:
    expected = []
    for c in d.model_children:
        child: dict = {
            "name": c.effective_name,
            "kind": c.kind,
            "min-occurs": c.min_occurs,
            "max-occurs": c.max_occurs,
        }
        if c.ref is not None:
            child["name"] = _ref_name(c.effective_name, c.ref, _first_global(scope, c.kind, c.ref))
        if c.group_as:
            child["group-as"] = c.group_as
        if c.ref is not None:
            child["ref"] = c.ref
        elif c.inline is not None:
            child["inline"] = True
        if c.in_choice:
            child["in-choice"] = True
        expected.append(child)
    return expected


def _check_match(
    scope: ModelScope, by_key: dict[DefinitionKey, Definition], d: Definition, item: dict
) -> None:
    """One match against its Definition: key set, values, and remarks truncation."""
    expected: dict = {"kind": d.key.kind, "name": d.key.name, "scope": d.scope}
    if d.parent is not None:
        expected["parent-definition"] = d.parent.name
    if d.formal_name:
        expected["formal-name"] = d.formal_name
    if d.description is not None and d.description.plain():
        expected["description"] = d.description.plain()
    if d.use_name:
        expected["use-name"] = d.use_name
    if d.group_as:
        expected["group-as"] = d.group_as
    if d.flags:
        expected["flags"] = _expected_flags(scope, by_key, d)
    if d.model_children:
        expected["model"] = _expected_children(scope, d)
    if d.constraints:
        expected["constraints"] = [
            {"type": c.type, "target": c.target} | ({"constraint-id": c.id} if c.id else {})
            for c in d.constraints
        ]
    full = d.remarks.blocks() if d.remarks is not None else ""
    if full:
        expected["remarks"] = full
    expected["source-file"] = d.key.location.source_file
    expected["line"] = d.key.location.line

    assert (d.scope == "local") == (d.parent is not None)
    if item.get("truncated"):
        assert item["truncated"] is True
        _event("truncated remarks")
        remarks = item["remarks"]
        assert remarks.endswith(TRUNCATION_MARK)
        assert len(remarks) - len(TRUNCATION_MARK) < len(full)
        assert full.startswith(remarks[: -len(TRUNCATION_MARK)])
        expected["remarks"] = remarks
        expected["truncated"] = True
    assert set(item) == set(expected)
    assert set(item) >= REQUIRED_KEYS
    assert item == expected
    for child in item.get("model", ()):
        assert type(child["min-occurs"]) is int
        assert isinstance(child["max-occurs"], str)
    for flag in item.get("flags", ()):
        assert type(flag["required"]) is bool


def _check_against_oracle(
    scope: ModelScope, name: str, model: str | None, kind: str | None, limits: list[int]
) -> list[dict]:
    items = _walk_pages(name, model, kind, limits)
    oracle = _oracle(scope, name, kind)
    locations = [(i["source-file"], i["line"]) for i in items]
    assert locations == [(d.key.location.source_file, d.key.location.line) for d in oracle]
    assert len(set(locations)) == len(locations)
    by_key = {d.key: d for n in scope.definition_names() for d in scope.definitions(n)}
    for d, item in zip(oracle, items, strict=True):
        _check_match(scope, by_key, d, item)
    return items


@st.composite
def _real_queries(draw: st.DrawFn) -> tuple[str, str | None, str | None]:
    """(name, model, kind) from the real bundle, weighted toward names with many Definitions."""
    model = draw(st.sampled_from([None, *_MODEL_VALUES]))
    scope = _scope_of(_bundle_index(), model)
    names = scope.definition_names()
    common = [n for n in names if len(scope.definitions(n)) >= 5]
    names_st = st.sampled_from(names)
    name = draw(st.sampled_from(common) | names_st if common else names_st)
    kinds = sorted({d.key.kind for d in scope.definitions(name)})
    kind = draw(st.sampled_from([None, *kinds]))
    return name, model, kind


_LIMITS = st.lists(
    st.one_of(st.integers(1, 10), st.integers(1, MAX_PAGE_LIMIT)), min_size=1, max_size=4
)

# Synthetic bundles: many same-named ("x") global and local Definitions with long remarks.
_WORD = st.text(alphabet="abé", min_size=1, max_size=8)
_BLOCK = st.tuples(
    st.sampled_from(["p", "ul"]), _WORD, st.one_of(st.integers(1, 50), st.integers(400, 2500))
)
_REMARKS = st.lists(_BLOCK, max_size=3)


def _remarks_xml(blocks: list[tuple[str, str, int]]) -> str:
    if not blocks:
        return ""
    parts = []
    for tag, word, reps in blocks:
        text = " ".join([word] * reps)
        if tag == "p":
            parts.append(f"<p>{text}</p>")
        else:
            parts.append(f"<ul><li>{text}</li><li><em>{word}</em></li></ul>")
    return "<remarks>\n" + "\n".join(parts) + "\n</remarks>"


@st.composite
def _syn_module(draw: st.DrawFn, prefix: str, min_holders: int = 0) -> str:
    """A module body: holder assemblies with local field/flag ``x``, plus optional globals."""
    lines = []
    for i in range(draw(st.integers(min_holders, 8))):
        lines += [
            f'<define-assembly name="{prefix}h{i}">',
            f'<define-flag name="x" required="yes" as-type="token">{_remarks_xml(draw(_REMARKS))}',
            "</define-flag>",
            '<flag ref="x"/>',
            "<model>",
            '<define-field name="x" max-occurs="unbounded">',
            '<group-as name="xs"/>',
            "<description>Local <code>x</code></description>",
            _remarks_xml(draw(_REMARKS)),
            "</define-field>",
            '<choice><assembly ref="x"/></choice>',
            "</model>",
            "</define-assembly>",
        ]
    if draw(st.booleans()):
        lines += [
            '<define-assembly name="x">',
            "<formal-name>Global X</formal-name>",
            '<constraint><expect id="e1" target="." test="true()"/></constraint>',
            _remarks_xml(draw(_REMARKS)),
            "</define-assembly>",
        ]
    if draw(st.booleans()):
        lines.append('<define-flag name="x" as-type="uuid"/>')
    return "\n".join(lines)


@st.composite
def _syn_bundles(draw: st.DrawFn) -> dict[str, str | bytes]:
    extras = [_mod(f"s{i}") for i in range(draw(st.integers(0, 2)))]
    imported = draw(st.lists(st.sampled_from(extras), unique=True)) if extras else []
    files: dict[str, str | bytes] = {
        CAT: _module(_imports(*imported) + "\n" + draw(_syn_module("c", min_holders=1)))
    }
    for i, rel in enumerate(extras):
        files[rel] = _module(draw(_syn_module(f"s{i}")))
    return files


def _check_synthetic(
    files: dict[str, str | bytes], model: str | None, kind: str | None, limits: list[int]
) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        _write_bundle(Path(tmp), files)
        index = MetaschemaIndex.load(Path(tmp))
        assert dict(index.load_errors) == {}
        scope = _scope_of(index, model)
        if kind is not None and not _oracle(scope, "x", kind):
            kind = None  # no "x" of that kind here; every bundle has local field and flag x
        with patch.object(gd_module, "get_index", return_value=index):
            return _check_against_oracle(scope, "x", model, kind, limits)


class TestSameNamedDefinitionsProperty:
    """Property 20: get_definition returns every same-named Definition within budget.

    For any name and kind filter, walking the pages returns exactly the Definitions with that
    name and kind in the searched modules, ordered by Source_Location, each local one with
    ``parent-definition``, no ``None`` values, every response within the Response_Budget, and
    ``truncated: true`` exactly on matches whose ``remarks`` were shortened.

    **Validates: Requirements 10.2, 10.3, 10.5, 10.6**
    """

    @settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(query=_real_queries(), limits=_LIMITS)
    def test_real_bundle_matches_oracle(self, query, limits):
        name, model, kind = query
        index = _bundle_index()
        with patch.object(gd_module, "get_index", return_value=index):
            items = _check_against_oracle(_scope_of(index, model), name, model, kind, limits)
        event(f"matches>10={len(items) > 10}")
        event(f"local={any(i['scope'] == 'local' for i in items)}")

    @pytest.mark.parametrize("name", ["uuid", "description", "title", "remarks", "property"])
    def test_real_heavy_names_all_modules(self, name):
        index = _bundle_index()
        with patch.object(gd_module, "get_index", return_value=index):
            items = _check_against_oracle(index.all_modules(), name, None, None, [100])
        assert len(items) >= 1

    @settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        files=_syn_bundles(),
        model=st.sampled_from([None, "catalog"]),
        kind=st.sampled_from([None, "assembly", "field", "flag"]),
        limits=_LIMITS,
    )
    def test_synthetic_bundles_match_oracle(self, files, model, kind, limits):
        items = _check_synthetic(files, model, kind, limits)
        event(f"truncated={any(i.get('truncated') for i in items)}")

    def test_synthetic_forces_budget_reduction_and_truncation(self):
        long_remarks = [("p", "é", 25_000), ("ul", "ab", 10)]
        mid_remarks = [("p", "word", 600), ("ul", "ab", 2)]
        body = "\n".join(
            [
                '<define-assembly name="x">',
                _remarks_xml(long_remarks),
                "</define-assembly>",
                *(
                    f'<define-assembly name="h{i}"><model>\n<define-field name="x">\n'
                    f"{_remarks_xml(mid_remarks)}\n</define-field>\n</model></define-assembly>"
                    for i in range(30)
                ),
            ]
        )
        files: dict[str, str | bytes] = {CAT: _module(body)}
        items = _check_synthetic(files, None, None, [100])
        assert len(items) == 31  # 1 global + 30 local
        assert items[0]["truncated"] is True
        assert items[0]["remarks"].startswith("é é")
        assert not any(i.get("truncated") for i in items[1:])
        assert all(i["parent-definition"] == f"h{n}" for n, i in enumerate(items[1:]))
        assert all("\n\n- ab ab\n- " in i["remarks"] for i in items[1:])
        with tempfile.TemporaryDirectory() as tmp:
            _write_bundle(Path(tmp), files)
            index = MetaschemaIndex.load(Path(tmp))
            with patch.object(gd_module, "get_index", return_value=index):
                page = get_definition(name="x", offset=1, limit=30)
        assert 1 <= page["limit"] == len(page["items"]) < 30
        assert page["hasMore"] is True
