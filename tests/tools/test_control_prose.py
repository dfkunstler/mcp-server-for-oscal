"""Property test for Control_Prose in catalog control descriptions.

Feature: search-fetch-pairs, Property 10: Control descriptions equal Control_Prose at
every level.

For any generated catalog (random nesting of ``CatalogGroupWithControls`` and
``CatalogGroupWithGroups``, controls with random ``statement``, ``guidance``, and other
parts, with nested parts and missing prose), after indexing each extracted control's
``child_elements.description`` and FTS ``description`` equal an oracle that walks the
JSON dict.

**Validates: Requirements 9.1, 9.2, 9.3, 9.4**
"""

import itertools
import json
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools.oscal_store import OscalStore
from mcp_server_for_oscal.tools.utils import MODEL_MAP, OSCALModelType

_CATALOG_UUID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
_METADATA = {
    "title": "Prose Property Catalog",
    "last-modified": "2024-01-01T00:00:00Z",
    "version": "1.0",
    "oscal-version": "1.1.3",
}

# Prose names first so they are drawn often; the rest are ordinary OSCAL part names.
_PART_NAMES = ("statement", "guidance", "item", "objective", "assessment-method", "overview")

# Letters, digits, punctuation, and whitespace (including a non-breaking space and
# non-ASCII letters) so that stripping and blank prose are exercised.
_PROSE = st.one_of(
    st.none(),
    st.just(""),
    st.text(alphabet=" \t\n\u00a0abcXYZ019.,{}é中", max_size=20),
)


# ---------------------------------------------------------------------------
# Strategies (plain dicts; IDs are assigned afterwards so they are unique)
# ---------------------------------------------------------------------------


def _parts(depth: int) -> st.SearchStrategy[list[dict]]:
    """Return a strategy for a list of parts nested up to ``depth`` more levels."""

    @st.composite
    def part(draw) -> dict:
        p: dict = {"name": draw(st.sampled_from(_PART_NAMES))}
        prose = draw(_PROSE)
        if prose is not None:
            p["prose"] = prose
        if depth > 0:
            children = draw(_parts(depth - 1))
            if children:
                p["parts"] = children
        return p

    return st.lists(part(), max_size=3)


@st.composite
def _control(draw, *, allow_enhancements: bool = True) -> dict:
    ctrl: dict = {"title": "Control"}
    parts = draw(_parts(2))
    if parts:
        ctrl["parts"] = parts
    # Control enhancements are not extracted; their prose must not leak into the parent.
    if allow_enhancements:
        enhancements = draw(st.lists(_control(allow_enhancements=False), max_size=1))
        if enhancements:
            ctrl["controls"] = enhancements
    return ctrl


def _group(depth: int) -> st.SearchStrategy[dict]:
    """Return a strategy for a group: controls (leaf) or nested groups, never both."""

    @st.composite
    def group(draw) -> dict:
        grp: dict = {"title": "Group"}
        if depth > 0 and draw(st.booleans()):
            grp["groups"] = draw(st.lists(_group(depth - 1), min_size=1, max_size=2))
        else:
            controls = draw(st.lists(_control(), max_size=3))
            if controls:
                grp["controls"] = controls
        return grp

    return group()


@st.composite
def catalogs(draw) -> dict:
    """Generate a model-valid catalog dict with unique control and group IDs."""
    body: dict = {"uuid": _CATALOG_UUID, "metadata": dict(_METADATA)}
    controls = draw(st.lists(_control(), max_size=3))
    if controls:
        body["controls"] = controls
    groups = draw(st.lists(_group(2), max_size=3))
    if groups:
        body["groups"] = groups
    _assign_ids(body, itertools.count(1))
    return {"catalog": body}


def _assign_ids(node: dict, counter: itertools.count) -> None:
    """Give every control and group a unique token ID, in document order."""
    for ctrl in node.get("controls", []):
        ctrl["id"] = f"ctrl-{next(counter)}"
        _assign_ids(ctrl, counter)
    for grp in node.get("groups", []):
        grp["id"] = f"grp-{next(counter)}"
        _assign_ids(grp, counter)


# ---------------------------------------------------------------------------
# Oracle: walks the JSON dict, not the models
# ---------------------------------------------------------------------------


def _oracle_prose(control: dict) -> str:
    """Return the Control_Prose of a control dict per the Requirement 9 definition."""
    out: list[str] = []

    def walk(part: dict) -> None:
        text = (part.get("prose") or "").strip()
        if text:
            out.append(text)
        for child in part.get("parts", []):
            walk(child)

    for part in control.get("parts", []):
        if part["name"] in {"statement", "guidance"}:
            walk(part)
    return "\n\n".join(out)


def _expected(catalog: dict) -> dict[str, str]:
    """Map each control the store extracts (top level and in groups) to its prose."""
    body = catalog["catalog"]
    expected = {c["id"]: _oracle_prose(c) for c in body.get("controls", [])}
    stack = list(body.get("groups", []))
    while stack:
        grp = stack.pop()
        expected.update({c["id"]: _oracle_prose(c) for c in grp.get("controls", [])})
        stack.extend(grp.get("groups", []))
    return expected


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(catalog=catalogs())
def test_control_descriptions_equal_control_prose(catalog):
    """Feature: search-fetch-pairs, Property 10: Control descriptions equal Control_Prose.

    **Validates: Requirements 9.1, 9.2, 9.3, 9.4**
    """
    # The generated catalog is model-valid, so the store ingests it.
    MODEL_MAP[OSCALModelType.CATALOG].model_validate(catalog["catalog"])
    expected = _expected(catalog)

    with tempfile.TemporaryDirectory() as tmp:
        docs = Path(tmp) / "docs"
        docs.mkdir()
        (docs / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        store = OscalStore(
            db_path=str(Path(tmp) / "store.db"), cache_size=4, seed_from_bundled=False
        )
        try:
            store.scan_directory(docs)
            # Listing children (public API) indexes the document on first access.
            page = store.list_child_elements(
                parent_doc_uuid=_CATALOG_UUID, element_type="control", limit=1000
            )
            listed = {item["id"]: item["description"] for item in page["items"]}
            assert page["total"] == len(expected)
            assert listed == expected

            # The FTS row of each control carries the same description.
            rows = store._conn.execute(
                "SELECT ce.uuid, f.description FROM child_elements ce "
                "JOIN documents d ON ce.parent_doc_id = d.id "
                "JOIN fts_index f ON f.entity_type = 'child_element' "
                "AND f.entity_id = CAST(ce.id AS TEXT) "
                "WHERE d.uuid = ? AND ce.element_type = 'control'",
                (_CATALOG_UUID,),
            ).fetchall()
            assert {r["uuid"]: r["description"] for r in rows} == expected
        finally:
            store.close()
