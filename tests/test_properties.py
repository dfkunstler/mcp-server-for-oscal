"""
Property-based tests for OSCAL MCP Server correctness properties.

Uses Hypothesis to generate random inputs and verify universal
correctness properties of the server.

Feature: oscal-mcp-server
Feature: remove-legacy-cdef-store
"""

import string
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from hypothesis import given, settings
from hypothesis import strategies as st
from trestle.oscal.component import ComponentDefinition

from mcp_server_for_oscal.tools import query_component_definition as _qcd_module
from mcp_server_for_oscal.tools.oscal_store import OscalStore
from mcp_server_for_oscal.tools.query_component_definition import (
    init_store,
    query_component_definition,
)

from .fixture_store import build_fixture_store

# ------------------------------------------------------------------
# Shared strategies and helpers for remove-legacy-cdef-store properties
# ------------------------------------------------------------------

QueryType = Literal["all", "by_uuid", "by_title", "by_type"]

COMPONENT_TYPES = ("software", "service", "hardware", "policy")
PROP_NAMES = ("vendor", "region", "tier")
# Prop values are drawn from a small pool so several Components share one,
# and never collide with generated titles (titles always end in " <n>").
PROP_VALUES = ("alpha", "beta", "gamma", "delta")

# ASCII letters/digits keep FTS tokenisation and NOCASE case-folding simple.
_WORD = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=8)


@st.composite
def cdef_stores(
    draw: st.DrawFn,
    min_cdefs: int = 1,
    max_cdefs: int = 4,
    max_components: int = 5,
    max_capabilities: int = 3,
) -> list[dict]:
    """Draw a list of Trestle-valid, wrapped Component Definition dicts.

    - 1-4 cdefs, each with 0-5 components and 0-3 capabilities.
    - Every UUID in the store (cdef, component, capability) is distinct.
    - Every cdef title, component title, and capability name is unique
      across the store, case-insensitively: each is ``"<word> <n>"`` with a
      store-wide counter ``n``. They contain only ASCII letters, digits, and
      single spaces, with no leading/trailing whitespace.
    - Component ``type`` comes from ``COMPONENT_TYPES``; components carry
      0-3 props whose values come from ``PROP_VALUES``.
    - Capabilities optionally incorporate components of the same cdef.
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
    uuid_pool = iter(
        draw(
            st.lists(
                st.uuids(version=4).map(str),
                min_size=n_uuids,
                max_size=n_uuids,
                unique=True,
            )
        )
    )
    counter = iter(range(10_000))

    def unique_title() -> str:
        return f"{draw(_WORD)} {next(counter)}"

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
                    st.tuples(
                        st.sampled_from(PROP_NAMES), st.sampled_from(PROP_VALUES)
                    ),
                    max_size=3,
                )
            )
            if props:
                comp["props"] = [{"name": n, "value": v} for n, v in props]
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
                        {"component-uuid": u, "description": "Incorporated"}
                        for u in incorporated
                    ]
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
    """Parse wrapped cdef dicts into Trestle models (validates the strategy)."""
    return [ComponentDefinition.parse_obj(c["component-definition"]) for c in cdefs]


def case_variant(draw: st.DrawFn, text: str) -> str:
    """Draw an arbitrary per-character letter-case variant of *text*."""
    flips = draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    return "".join(
        ch.swapcase() if f else ch for ch, f in zip(text, flips, strict=True)
    )


@contextmanager
def installed_store(cdefs: list[dict]) -> Iterator[OscalStore]:
    """Build a Fixture_Store from *cdefs* and install it as ``_oscal_store``.

    Runs per Hypothesis example (the autouse ``reset_oscal_store`` fixture is
    function-scoped, so it does not isolate examples). The store is closed,
    the temp directory removed, and the prior singleton restored on exit.
    """
    saved = _qcd_module._oscal_store  # noqa: SLF001
    with tempfile.TemporaryDirectory() as tmp:
        store = build_fixture_store(Path(tmp), cdefs)
        try:
            init_store(store)
            yield store
        finally:
            _qcd_module._oscal_store = saved  # noqa: SLF001
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
        each equal to the source ``DefinedComponent.dict(exclude_none=True)``.

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

        expected = {
            str(c.uuid): c.dict(exclude_none=True)
            for m in in_scope
            for c in m.components or []
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
        if query_type == "by_uuid":
            key = str(comp.uuid)  # UUID case preserved
        else:
            key = case_variant(data.draw, comp.title)
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
            assert resp["components"] == [comp.dict(exclude_none=True)]
            assert resp["total_count"] == 1
