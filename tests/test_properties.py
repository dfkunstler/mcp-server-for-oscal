"""
Property-based tests for OSCAL MCP Server correctness properties.

Uses Hypothesis to generate random inputs and verify universal
correctness properties of the server.

Feature: oscal-mcp-server
Feature: remove-legacy-cdef-store
"""

import re
import string
import tempfile
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from oscal_bindings.models import ComponentDefinition

from mcp_server_for_oscal.tools import query_component_definition as _qcd_module
from mcp_server_for_oscal.tools.oscal_store import OscalStore
from mcp_server_for_oscal.tools.query_component_definition import (
    get_capability,
    init_store,
    list_capabilities,
    list_components,
    query_component_definition,
)
from mcp_server_for_oscal.tools.utils import OSCALModelType, paginate
from mcp_server_for_oscal.tools.validate_oscal_content import _validate_json_schema

from .fixture_store import build_fixture_store, make_many_capabilities_cdef

# ------------------------------------------------------------------
# Shared strategies and helpers for remove-legacy-cdef-store properties
# ------------------------------------------------------------------

QueryType = Literal["all", "by_uuid", "by_title", "by_type"]

COMPONENT_TYPES = ("software", "service", "hardware", "policy")
PROP_NAMES = ("vendor", "region", "tier")
# Prop values are drawn from a small pool so several Components share one,
# and never collide with generated titles (titles always end in " <n>").
PROP_VALUES = ("alpha", "beta", "gamma", "delta")
# Token values for rich-mode nested fields (``role-id``, ``control-id``).
ROLE_IDS = ("provider", "customer", "shared")
CONTROL_IDS = ("ac-1", "ac-2", "au-2", "sc-7")

# ASCII letters/digits keep FTS tokenisation and NOCASE case-folding simple.
_WORD = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=8)


@st.composite
def cdef_stores(
    draw: st.DrawFn,
    min_cdefs: int = 1,
    max_cdefs: int = 4,
    max_components: int = 5,
    max_capabilities: int = 3,
    *,
    rich: bool = False,
) -> list[dict]:
    """Draw a list of model-valid, wrapped Component Definition dicts.

    - 1-4 cdefs, each with 0-5 components and 0-3 capabilities.
    - Every UUID in the store (cdef, component, capability) is distinct.
    - Every cdef title, component title, and capability name is unique
      across the store, case-insensitively: each is ``"<word> <n>"`` with a
      store-wide counter ``n``. They contain only ASCII letters, digits, and
      single spaces, with no leading/trailing whitespace.
    - Component ``type`` comes from ``COMPONENT_TYPES``; components carry
      0-3 props whose values come from ``PROP_VALUES``.
    - Capabilities optionally incorporate components of the same cdef.
    - With ``rich=True``, components and capabilities may also carry
      hyphenated nested fields (``responsible-roles``/``role-id`` and
      ``control-implementations``/``implemented-requirements``/``control-id``).
      Nested UUIDs are distinct from every other UUID in the store. With the
      default ``rich=False`` the draws are exactly those of the plain strategy.
    """
    n_cdefs = draw(st.integers(min_cdefs, max_cdefs))
    shapes = [
        (
            draw(st.integers(0, max_components)),
            draw(st.integers(0, max_capabilities)),
        )
        for _ in range(n_cdefs)
    ]
    n_uuids = n_cdefs + sum(c + k for c, k in shapes)
    pool_list = draw(
        st.lists(
            st.uuids(version=4).map(str),
            min_size=n_uuids,
            max_size=n_uuids,
            unique=True,
        )
    )
    uuid_pool = iter(pool_list)
    counter = iter(range(10_000))

    def unique_title() -> str:
        return f"{draw(_WORD)} {next(counter)}"

    # Nested UUIDs for rich mode are drawn lazily and kept store-unique.
    used_uuids: set[str] = set(pool_list)

    def nested_uuid() -> str:
        u = draw(st.uuids(version=4).map(str).filter(lambda x: x not in used_uuids))
        used_uuids.add(u)
        return u

    def draw_roles() -> list[dict]:
        roles = draw(st.lists(st.sampled_from(ROLE_IDS), unique=True, max_size=2))
        return [{"role-id": r} for r in roles]

    def add_rich_fields(element: dict) -> None:
        """Optionally add hyphenated nested fields to a component/capability."""
        if not rich:
            return
        roles = draw_roles()
        if roles and "type" in element:  # responsible-roles: components only
            element["responsible-roles"] = roles
        n_ci = draw(st.integers(0, 2))
        if not n_ci:
            return
        element["control-implementations"] = []
        for _ in range(n_ci):
            reqs = []
            for control_id in draw(
                st.lists(st.sampled_from(CONTROL_IDS), unique=True, min_size=1, max_size=3)
            ):
                req: dict = {
                    "uuid": nested_uuid(),
                    "control-id": control_id,
                    "description": "Generated requirement",
                }
                req_roles = draw_roles()
                if req_roles:
                    req["responsible-roles"] = req_roles
                reqs.append(req)
            element["control-implementations"].append(
                {
                    "uuid": nested_uuid(),
                    "source": "https://example.com/catalog.json",
                    "description": "Generated control implementation",
                    "implemented-requirements": reqs,
                }
            )

    cdefs: list[dict] = []
    for n_comp, n_cap in shapes:
        cdef_uuid = next(uuid_pool)
        components = []
        for _ in range(n_comp):
            comp: dict = {
                "uuid": next(uuid_pool),
                "type": draw(st.sampled_from(COMPONENT_TYPES)),
                "title": unique_title(),
                "description": "Generated component",
            }
            props = draw(
                st.lists(
                    st.tuples(st.sampled_from(PROP_NAMES), st.sampled_from(PROP_VALUES)),
                    max_size=3,
                )
            )
            if props:
                comp["props"] = [{"name": n, "value": v} for n, v in props]
            add_rich_fields(comp)
            components.append(comp)
        capabilities = []
        for _ in range(n_cap):
            cap: dict = {
                "uuid": next(uuid_pool),
                "name": unique_title(),
                "description": "Generated capability",
            }
            if components:
                incorporated = draw(
                    st.lists(
                        st.sampled_from([c["uuid"] for c in components]),
                        unique=True,
                        max_size=len(components),
                    )
                )
                if incorporated:
                    cap["incorporates-components"] = [
                        {"component-uuid": u, "description": "Incorporated"} for u in incorporated
                    ]
            add_rich_fields(cap)
            capabilities.append(cap)
        body: dict = {
            "uuid": cdef_uuid,
            "metadata": {
                "title": unique_title(),
                "last-modified": "2024-01-01T00:00:00+00:00",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
        }
        if components:
            body["components"] = components
        if capabilities:
            body["capabilities"] = capabilities
        cdefs.append({"component-definition": body})
    return cdefs


def parse_cdefs(cdefs: list[dict]) -> list[ComponentDefinition]:
    """Parse wrapped cdef dicts into OSCAL models (validates the strategy)."""
    return [ComponentDefinition.model_validate(c["component-definition"]) for c in cdefs]


def raw_by_uuid(cdefs: list[dict], key: Literal["components", "capabilities"]) -> dict[str, dict]:
    """Map UUID to the source OSCAL element dict under *key* across all *cdefs*.

    These raw dicts are the library-independent oracle for tool output.
    """
    return {e["uuid"]: e for c in cdefs for e in c["component-definition"].get(key, [])}


def case_variant(draw: st.DrawFn, text: str) -> str:
    """Draw an arbitrary per-character letter-case variant of *text*."""
    flips = draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    return "".join(ch.swapcase() if f else ch for ch, f in zip(text, flips, strict=True))


@contextmanager
def installed_store(cdefs: list[dict]) -> Iterator[OscalStore]:
    """Build a Fixture_Store from *cdefs* and install it as ``_oscal_store``.

    Runs per Hypothesis example (the autouse ``reset_oscal_store`` fixture is
    function-scoped, so it does not isolate examples). The store is closed,
    the temp directory removed, and the prior singleton restored on exit.
    """
    saved = _qcd_module._oscal_store
    with tempfile.TemporaryDirectory() as tmp:
        store = build_fixture_store(Path(tmp), cdefs)
        try:
            init_store(store)
            yield store
        finally:
            _qcd_module._oscal_store = saved
            store.close()


def collect_pages(
    limit: int,
    query_type: QueryType = "all",
    query_value: str | None = None,
    component_definition_filter: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """Page ``query_component_definition`` until ``hasMore`` is false.

    Returns:
        ``(components, responses)``: concatenated components and every page.
    """
    components: list[dict] = []
    responses: list[dict] = []
    offset = 0
    while True:
        resp = query_component_definition(
            ctx=None,
            component_definition_filter=component_definition_filter,
            query_type=query_type,
            query_value=query_value,
            offset=offset,
            limit=limit,
        )
        responses.append(resp)
        components.extend(resp["components"])
        if not resp["hasMore"]:
            return components, responses
        offset += limit
        assert len(responses) <= 1000, "pagination did not terminate"


def collect_list_pages(list_fn: Callable[..., dict], limit: int) -> tuple[list[dict], list[dict]]:
    """Page a ``list_*`` helper until ``hasMore`` is false.

    Returns:
        ``(items, responses)``: concatenated items and every page.
    """
    items: list[dict] = []
    responses: list[dict] = []
    offset = 0
    while True:
        resp = list_fn(ctx=None, offset=offset, limit=limit)
        responses.append(resp)
        items.extend(resp["items"])
        if not resp["hasMore"]:
            return items, responses
        offset += limit
        assert len(responses) <= 1000, "pagination did not terminate"


# ------------------------------------------------------------------
# Feature: remove-legacy-cdef-store
# ------------------------------------------------------------------


class TestRemoveLegacyCdefStoreProperties:
    """Properties from the remove-legacy-cdef-store design."""

    @given(cdefs=cdef_stores(), data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_scope_completeness_and_fidelity_for_all(self, cdefs, data):
        """Feature: remove-legacy-cdef-store, Property 1: Scope completeness
        and fidelity for `all`.

        Paging ``query_type="all"`` yields exactly the in-scope Components,
        each equal to its source OSCAL element dict.

        **Validates: Requirements 3.1, 3.2, 3.7, 3.9, 7.7**
        """
        models = parse_cdefs(cdefs)
        mode = data.draw(st.sampled_from(["none", "uuid", "title"]), label="mode")
        if mode == "none":
            cdef_filter = None
            in_scope = models
        else:
            target = data.draw(st.sampled_from(models), label="target")
            in_scope = [target]
            cdef_filter = (
                str(target.uuid)
                if mode == "uuid"
                else case_variant(data.draw, target.metadata.title)
            )
        limit = data.draw(st.integers(1, 10), label="limit")

        raw_components = raw_by_uuid(cdefs, "components")
        expected = {
            str(c.uuid): raw_components[str(c.uuid)] for m in in_scope for c in m.components or []
        }

        with installed_store(cdefs):
            components, responses = collect_pages(
                limit,
                component_definition_filter=cdef_filter,
                query_type="all",
            )

        got = {str(c["uuid"]): c for c in components}
        assert len(got) == len(components), "duplicate components across pages"
        assert got == expected
        expected_searched = len(models) if cdef_filter is None else 1
        for resp in responses:
            assert resp["total_count"] == len(expected)
            assert resp["component_definitions_searched"] == expected_searched
            assert resp["filtered_by"] == cdef_filter
            assert resp["query_type"] == "all"

    @given(
        cdefs=cdef_stores().filter(
            lambda cs: any(c["component-definition"].get("components") for c in cs)
        ),
        data=st.data(),
    )
    @settings(max_examples=100, deadline=None)
    def test_lookup_by_uuid_and_title_respects_scope_case_whitespace(self, cdefs, data):
        """Feature: remove-legacy-cdef-store, Property 2: Lookup by UUID and
        title respects scope, case, and whitespace.

        A padded (and, for titles, case-varied) key finds exactly its
        Component when unscoped or scoped to its own cdef, and nothing when
        scoped to a different cdef.

        **Validates: Requirements 3.3, 3.4, 3.8, 4.6, 7.7**
        """
        models = parse_cdefs(cdefs)
        owners = [m for m in models if m.components]
        cdef_a = data.draw(st.sampled_from(owners), label="cdef_a")
        comp = data.draw(st.sampled_from(cdef_a.components), label="component")

        query_type = data.draw(st.sampled_from(["by_uuid", "by_title"]), label="qt")
        # UUID case preserved; titles get a random case variant
        key = str(comp.uuid) if query_type == "by_uuid" else case_variant(data.draw, comp.title)
        pad = st.text(alphabet=" \t\n", max_size=3)
        query_value = data.draw(pad, label="lpad") + key + data.draw(pad, label="rpad")

        others = [m for m in models if m.uuid != cdef_a.uuid]
        scopes = ["none", "a_uuid", "a_title"] + (["other"] if others else [])
        scope = data.draw(st.sampled_from(scopes), label="scope")
        if scope == "none":
            cdef_filter = None
        elif scope == "a_uuid":
            cdef_filter = str(cdef_a.uuid)
        elif scope == "a_title":
            cdef_filter = case_variant(data.draw, cdef_a.metadata.title)
        else:
            cdef_filter = str(data.draw(st.sampled_from(others), label="cdef_s").uuid)

        with installed_store(cdefs):
            resp = query_component_definition(
                ctx=None,
                component_definition_filter=cdef_filter,
                query_type=query_type,
                query_value=query_value,
            )

        assert "capability" not in resp
        assert resp["query_type"] == query_type
        assert resp["filtered_by"] == cdef_filter
        if scope == "other":
            assert resp["components"] == []
            assert resp["total_count"] == 0
        else:
            assert resp["components"] == [raw_by_uuid(cdefs, "components")[str(comp.uuid)]]
            assert resp["total_count"] == 1

    @given(cdefs=cdef_stores(), data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_property_value_fallback(self, cdefs, data):
        """Feature: remove-legacy-cdef-store, Property 3: Property-value
        fallback.

        A ``by_title`` value that is no in-scope Capability name or Component
        title, but is a prop value of some in-scope Component, returns exactly
        one in-scope Component carrying that prop value; otherwise nothing.

        **Validates: Requirements 3.5, 3.8**
        """
        models = parse_cdefs(cdefs)
        mode = data.draw(st.sampled_from(["none", "uuid"]), label="mode")
        if mode == "none":
            cdef_filter = None
            in_scope = models
        else:
            target = data.draw(st.sampled_from(models), label="target")
            cdef_filter = str(target.uuid)
            in_scope = [target]

        # PROP_VALUES never equal a title or capability name (those always
        # end in " <n>"), so the fallback path is the only way to match.
        value = data.draw(st.sampled_from(PROP_VALUES), label="value")
        pad = st.text(alphabet=" \t\n", max_size=3)
        query_value = data.draw(pad, label="lpad") + value + data.draw(pad, label="rpad")

        raw_components = raw_by_uuid(cdefs, "components")
        candidates = {
            str(c.uuid): raw_components[str(c.uuid)]
            for m in in_scope
            for c in m.components or []
            if any(p.value == value for p in c.props or [])
        }

        with installed_store(cdefs):
            resp = query_component_definition(
                ctx=None,
                component_definition_filter=cdef_filter,
                query_type="by_title",
                query_value=query_value,
            )

        assert "capability" not in resp
        assert resp["query_type"] == "by_title"
        assert resp["filtered_by"] == cdef_filter
        if candidates:
            assert len(resp["components"]) == 1
            assert resp["total_count"] == 1
            got = resp["components"][0]
            assert candidates.get(str(got["uuid"])) == got
        else:
            assert resp["components"] == []
            assert resp["total_count"] == 0

    @given(cdefs=cdef_stores(), data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_by_type_matches_reference_model(self, cdefs, data):
        """Feature: remove-legacy-cdef-store, Property 4: by_type matches a
        reference model.

        Paging ``query_type="by_type"`` with any type string (including one
        absent from the store) yields exactly the in-scope Components whose
        source ``type`` equals it, each equal to its source OSCAL element dict.

        **Validates: Requirements 3.6, 3.8**
        """
        models = parse_cdefs(cdefs)
        mode = data.draw(st.sampled_from(["none", "uuid", "title"]), label="mode")
        if mode == "none":
            cdef_filter = None
            in_scope_idx = list(range(len(models)))
        else:
            idx = data.draw(st.integers(0, len(models) - 1), label="target")
            in_scope_idx = [idx]
            target = models[idx]
            cdef_filter = (
                str(target.uuid)
                if mode == "uuid"
                else case_variant(data.draw, target.metadata.title)
            )
        type_value = data.draw(st.sampled_from((*COMPONENT_TYPES, "interconnection")), label="type")
        limit = data.draw(st.integers(1, 10), label="limit")

        # Reference model: computed from the raw source dicts, independent of
        # the store's indexing.
        expected_uuids = {
            comp["uuid"]
            for i in in_scope_idx
            for comp in cdefs[i]["component-definition"].get("components", [])
            if comp["type"] == type_value
        }
        raw_components = raw_by_uuid(cdefs, "components")
        expected = {
            str(c.uuid): raw_components[str(c.uuid)]
            for i in in_scope_idx
            for c in models[i].components or []
            if str(c.uuid) in expected_uuids
        }
        assert set(expected) == expected_uuids

        with installed_store(cdefs):
            components, responses = collect_pages(
                limit,
                query_type="by_type",
                query_value=type_value,
                component_definition_filter=cdef_filter,
            )

        got = {str(c["uuid"]): c for c in components}
        assert len(got) == len(components), "duplicate components across pages"
        assert set(got) == expected_uuids
        assert got == expected
        for resp in responses:
            assert resp["query_type"] == "by_type"
            assert resp["filtered_by"] == cdef_filter
            assert resp["total_count"] == len(expected_uuids)

    @given(cdefs=cdef_stores(), data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_pagination_agrees_with_paginate(self, cdefs, data):
        """Feature: remove-legacy-cdef-store, Property 5: Pagination agrees
        with paginate.

        A single call at any ``offset``/``limit`` returns the same
        ``components``, ``total_count``, ``offset``, ``limit``, and
        ``hasMore`` as ``paginate`` applied to the full result list (the same
        query paged at ``limit=100`` and concatenated).

        **Validates: Requirements 3.12**
        """
        models = parse_cdefs(cdefs)
        components = [c for m in models for c in m.components or []]
        query_type: QueryType = data.draw(
            st.sampled_from(["all", "by_type", "by_uuid", "by_title"]), label="qt"
        )
        query_value: str | None
        if query_type == "all":
            query_value = None
        elif query_type == "by_type":
            query_value = data.draw(
                st.sampled_from((*COMPONENT_TYPES, "interconnection")), label="type"
            )
        else:
            # Component keys, prop values (by_title fallback), or a miss.
            # Capability UUIDs/names are distinct from all of these.
            keys: list[str] = ["no-such-key"]
            if query_type == "by_uuid":
                keys += [str(c.uuid) for c in components]
            else:
                keys += [c.title for c in components] + list(PROP_VALUES)
            query_value = data.draw(st.sampled_from(keys), label="value")
        mode = data.draw(st.sampled_from(["none", "uuid"]), label="mode")
        cdef_filter = (
            None if mode == "none" else str(data.draw(st.sampled_from(models), label="target").uuid)
        )
        # Stores hold at most 20 Components, so offsets up to 30 cover
        # in-range, boundary, and beyond-the-end pages.
        offset = data.draw(st.integers(0, 30), label="offset")
        limit = data.draw(st.integers(1, 100), label="limit")

        with installed_store(cdefs):
            full, _ = collect_pages(
                100,
                query_type=query_type,
                query_value=query_value,
                component_definition_filter=cdef_filter,
            )
            resp = query_component_definition(
                ctx=None,
                component_definition_filter=cdef_filter,
                query_type=query_type,
                query_value=query_value,
                offset=offset,
                limit=limit,
            )

        assert "capability" not in resp
        expected = paginate(full, offset, limit)
        assert resp["components"] == expected["items"]
        assert resp["total_count"] == expected["total"]
        assert resp["offset"] == expected["offset"]
        assert resp["limit"] == expected["limit"]
        assert resp["hasMore"] == expected["hasMore"]

    @given(
        cdefs=cdef_stores().filter(
            lambda cs: any(c["component-definition"].get("capabilities") for c in cs)
        ),
        data=st.data(),
    )
    @settings(max_examples=100, deadline=None)
    def test_capability_first_and_capability_scoping(self, cdefs, data):
        """Feature: remove-legacy-cdef-store, Property 6: Capability-first and
        capability scoping.

        ``by_uuid`` with a Capability's UUID, or ``by_title`` with any case
        variant of its name, returns the Capability_Query_Response for it when
        unscoped or scoped to its own cdef. Scoped to a different cdef, the
        response is an empty Component_Query_Response instead.

        **Validates: Requirements 4.1, 4.2, 5.5, 7.7**
        """
        models = parse_cdefs(cdefs)
        owners = [m for m in models if m.capabilities]
        cdef_a = data.draw(st.sampled_from(owners), label="cdef_a")
        cap = data.draw(st.sampled_from(cdef_a.capabilities), label="capability")

        query_type = data.draw(st.sampled_from(["by_uuid", "by_title"]), label="qt")
        query_value = (
            str(cap.uuid) if query_type == "by_uuid" else case_variant(data.draw, cap.name)
        )

        others = [m for m in models if m.uuid != cdef_a.uuid]
        scopes = ["none", "a_uuid", "a_title"] + (["other"] if others else [])
        scope = data.draw(st.sampled_from(scopes), label="scope")
        if scope == "none":
            cdef_filter = None
        elif scope == "a_uuid":
            cdef_filter = str(cdef_a.uuid)
        elif scope == "a_title":
            cdef_filter = case_variant(data.draw, cdef_a.metadata.title)
        else:
            cdef_filter = str(data.draw(st.sampled_from(others), label="cdef_s").uuid)

        with installed_store(cdefs):
            resp = query_component_definition(
                ctx=None,
                component_definition_filter=cdef_filter,
                query_type=query_type,
                query_value=query_value,
            )

        assert resp["query_type"] == query_type
        assert resp["filtered_by"] == cdef_filter
        assert resp["component_definitions_searched"] == (len(models) if cdef_filter is None else 1)
        if scope == "other":
            # Capability keys never collide with Component keys or prop
            # values, so the component fallback finds nothing either.
            assert "capability" not in resp
            assert "components" in resp
            assert resp["components"] == []
            assert resp["total_count"] == 0
        else:
            assert "components" not in resp
            raw_cap = raw_by_uuid(cdefs, "capabilities")[str(cap.uuid)]
            got = resp["capability"]
            assert got["uuid"] == raw_cap["uuid"]
            assert got["name"] == raw_cap["name"]
            assert got == raw_cap
            assert resp["component_count"] == len(raw_cap.get("incorporates-components", []))
            assert resp["offset"] == 0
            assert resp["limit"] == 1
            assert resp["total"] == 1
            assert resp["hasMore"] is False

    @given(cdefs=cdef_stores(), data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_list_helpers_complete_and_correctly_keyed(self, cdefs, data):
        """Feature: remove-legacy-cdef-store, Property 7: List helpers are
        complete and correctly keyed.

        Paging ``list_components`` (resp. ``list_capabilities``) yields items
        with exactly the required keys, and the multiset of
        ``(uuid, parentComponentDefinitionUuid)`` equals the multiset of
        ``(component.uuid, cdef.uuid)`` (resp. capabilities) in the source.

        **Validates: Requirements 5.1, 5.2**
        """
        models = parse_cdefs(cdefs)
        limit = data.draw(st.integers(1, 10), label="limit")

        component_keys = {
            "uuid",
            "title",
            "parentComponentDefinitionTitle",
            "parentComponentDefinitionUuid",
            "sizeInBytes",
        }
        capability_keys = {
            "uuid",
            "name",
            "parentComponentDefinitionTitle",
            "parentComponentDefinitionUuid",
            "sizeInBytes",
        }

        expected_components = Counter(
            (str(c.uuid), c.title, str(m.uuid), m.metadata.title)
            for m in models
            for c in m.components or []
        )
        expected_capabilities = Counter(
            (str(k.uuid), k.name, str(m.uuid), m.metadata.title)
            for m in models
            for k in m.capabilities or []
        )

        with installed_store(cdefs):
            if expected_components:
                comp_items, comp_pages = collect_list_pages(list_components, limit)
            else:
                with pytest.raises(RuntimeError, match="No Components loaded"):
                    list_components(ctx=None, offset=0, limit=limit)
                comp_items, comp_pages = [], []
            cap_items, cap_pages = collect_list_pages(list_capabilities, limit)

        # Every page respects the requested limit and reports the true total.
        for pages, expected in (
            (comp_pages, expected_components),
            (cap_pages, expected_capabilities),
        ):
            for page in pages:
                assert len(page["items"]) <= limit
                assert page["total"] == sum(expected.values())

        for item in comp_items:
            assert set(item) == component_keys
        for item in cap_items:
            assert set(item) == capability_keys

        # Multiset of (uuid, parent uuid) matches the source exactly.
        assert Counter(
            (i["uuid"], i["parentComponentDefinitionUuid"]) for i in comp_items
        ) == Counter((u, p) for (u, _, p, _) in expected_components.elements())
        assert Counter(
            (i["uuid"], i["parentComponentDefinitionUuid"]) for i in cap_items
        ) == Counter((u, p) for (u, _, p, _) in expected_capabilities.elements())

        # Titles/names and parent titles match the source too.
        assert (
            Counter(
                (
                    i["uuid"],
                    i["title"],
                    i["parentComponentDefinitionUuid"],
                    i["parentComponentDefinitionTitle"],
                )
                for i in comp_items
            )
            == expected_components
        )
        assert (
            Counter(
                (
                    i["uuid"],
                    i["name"],
                    i["parentComponentDefinitionUuid"],
                    i["parentComponentDefinitionTitle"],
                )
                for i in cap_items
            )
            == expected_capabilities
        )

    @given(cdefs=cdef_stores(), data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_get_capability_is_position_independent(self, cdefs, data):
        """Feature: remove-legacy-cdef-store, Property 8: get_capability is
        position-independent.

        For every Capability ``k`` in the store, including stores with more
        than 100 Capabilities, ``get_capability(uuid=k.uuid)`` equals
        its source OSCAL element dict; any UUID not belonging to a Capability
        returns ``None``.

        **Validates: Requirements 5.3, 5.4**
        """
        # Mix in a >100-capability cdef on some examples so positions past
        # the old default page limit are exercised without slowing every run.
        if data.draw(st.booleans(), label="large"):
            n = data.draw(st.integers(101, 130), label="n_large_capabilities")
            large_cdef, _ = make_many_capabilities_cdef(n)
            cdefs = [*cdefs, large_cdef]
        models = parse_cdefs(cdefs)

        capabilities = [k for m in models for k in m.capabilities or []]
        capability_uuids = {str(k.uuid) for k in capabilities}
        non_capability_uuids = [str(m.uuid) for m in models] + [
            str(c.uuid) for m in models for c in m.components or []
        ]
        all_uuids = capability_uuids | set(non_capability_uuids)
        fresh = data.draw(
            st.uuids(version=4).map(str).filter(lambda u: u not in all_uuids),
            label="fresh_uuid",
        )
        non_capability_uuids += [fresh, ""]

        raw_caps = raw_by_uuid(cdefs, "capabilities")
        assert set(raw_caps) == capability_uuids

        with installed_store(cdefs):
            for k in capabilities:
                assert get_capability(ctx=None, uuid=str(k.uuid)) == raw_caps[str(k.uuid)]
            for u in non_capability_uuids:
                assert get_capability(ctx=None, uuid=u) is None


# ------------------------------------------------------------------
# Feature: oscal-bindings migration (#26)
# ------------------------------------------------------------------

_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def datetime_normalized(value: object) -> object:
    """Return *value* with OSCAL date-time strings replaced by ``datetime``s.

    Lets equality treat equivalent datetime spellings (``Z`` vs ``+00:00``,
    trailing fractional zeros) as equal.
    """
    if isinstance(value, dict):
        return {k: datetime_normalized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [datetime_normalized(v) for v in value]
    if isinstance(value, str) and _DATETIME_RE.match(value):
        return datetime.fromisoformat(value)
    return value


def assert_plain_oscal_json(value: object, path: str = "$") -> None:
    """Assert *value* is plain JSON with no nulls and no snake_case keys."""
    if isinstance(value, dict):
        for k, v in value.items():
            assert isinstance(k, str), f"non-string key at {path}"
            assert "_" not in k, f"snake_case key {k!r} at {path}"
            assert_plain_oscal_json(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            assert_plain_oscal_json(v, f"{path}[{i}]")
    else:
        assert value is not None, f"null value at {path}"
        assert isinstance(value, str | int | float | bool), (
            f"non-JSON value {type(value).__name__} at {path}"
        )


def minimal_cdef(key: str, elements: list[dict]) -> dict:
    """Embed *elements* under *key* in a minimal component-definition document."""
    return {
        "component-definition": {
            "uuid": str(uuid.uuid4()),
            "metadata": {
                "title": "t",
                "last-modified": "2024-01-01T00:00:00+00:00",
                "version": "1",
                "oscal-version": "1.2.3",
            },
            key: elements,
        }
    }


class TestOscalBindingsMigrationProperties:
    """Properties from the oscal-bindings migration design."""

    # Feature: oscal-bindings migration (#26), Property 3: Cdef tool output is the source element's OSCAL JSON
    @given(cdefs=cdef_stores(rich=True), data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_cdef_tool_output_is_source_oscal_json(self, cdefs, data):
        """Property 3: Cdef tool output is the source element's OSCAL JSON.

        For every component and capability, the component query, capability
        query, and ``get_capability`` results equal the source element dict
        (datetime-equivalent), contain no nulls or snake_case keys, and pass
        level-2 JSON Schema validation inside a minimal component-definition.
        The source dict is the library-independent oracle.

        **Validates: Requirements 6.2, 6.3, 6.4, 6.5, 6.7, 6.11, 6.12, 6.13,
        6.14, 6.15**
        """
        parse_cdefs(cdefs)  # the generated documents are model-valid
        raw_components = raw_by_uuid(cdefs, "components")
        raw_caps = raw_by_uuid(cdefs, "capabilities")

        query_caps: list[dict] = []
        got_caps: list[dict] = []
        with installed_store(cdefs):
            components, _ = collect_pages(100, query_type="all")
            for cap_uuid, raw_cap in raw_caps.items():
                query_type = data.draw(st.sampled_from(["by_uuid", "by_title"]), label="qt")
                resp = query_component_definition(
                    ctx=None,
                    query_type=query_type,
                    query_value=cap_uuid if query_type == "by_uuid" else raw_cap["name"],
                )
                assert "components" not in resp
                query_caps.append(resp["capability"])
                assert resp["component_count"] == len(raw_cap.get("incorporates-components", []))
                got = get_capability(ctx=None, uuid=cap_uuid)
                assert got is not None
                got_caps.append(got)

        # Component query path (Req 6.3, 6.7)
        assert {c["uuid"] for c in components} == set(raw_components)
        for comp in components:
            assert datetime_normalized(comp) == datetime_normalized(raw_components[comp["uuid"]])
        # Capability query path and get_capability (Req 6.2, 6.4, 6.7)
        for got in (*query_caps, *got_caps):
            assert datetime_normalized(got) == datetime_normalized(raw_caps[got["uuid"]])

        # Plain JSON, no nulls, no snake_case keys (Req 6.11, 6.14, 6.15)
        for element in (*components, *query_caps, *got_caps):
            assert_plain_oscal_json(element)

        # Level-2 JSON Schema validity, one wrapper per collection (Req 6.12, 6.13)
        for key, elements in (
            ("components", components),
            ("capabilities", query_caps),
            ("capabilities", got_caps),
        ):
            if not elements:
                continue
            level = _validate_json_schema(
                minimal_cdef(key, elements), OSCALModelType.COMPONENT_DEFINITION
            )
            assert level["valid"], level["errors"]
