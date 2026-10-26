"""Regression guards for the oscal-bindings 0.3.1 upgrade.

Feature: upgrade-oscal-bindings-0-3-1. Sections:

1. Source scan (Requirement 3.5): the Server validates OSCAL through ``model_validate(dict)``
   only. ``model_validate_json`` is banned because upstream cairn-proofs/oscal-bindings-python#6
   makes the JSON path silently drop keys spelled with Python field names, and the union
   ``parse_*`` helpers are banned because they bypass ``MODEL_MAP``.
2. Property tests (Properties 1-7 of the design).
3. Enum schema parity (Requirements 4.7, 4.9): closed and open enum value sets in the Bindings
   models match the bundled OSCAL JSON Schema (cairn-proofs/oscal-bindings-specs#9).
"""

import ast
import copy
import enum
import json
import tempfile
import types
import typing
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import regex
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from oscal_bindings import models as oscal_models
from pydantic import BaseModel, ValidationError

import mcp_server_for_oscal
from mcp_server_for_oscal.tools.oscal_store import CHILD_ELEMENT_TYPES, OscalStore
from mcp_server_for_oscal.tools.utils import MODEL_MAP, OSCALModelType
from mcp_server_for_oscal.tools.validate_oscal_content import validate_oscal_content

from .tools.test_oscal_store import _COMMON_METADATA, _make_mapping_collection

SRC_ROOT = Path(mcp_server_for_oscal.__file__).parent
BINDINGS_PACKAGE = "oscal_bindings"
BANNED_METHOD = "model_validate_json"
BANNED_PREFIX = "parse_"


# ---------------------------------------------------------------------------
# Section 1: Source scan (Requirement 3.5)
# ---------------------------------------------------------------------------


def _is_bindings_module(module: str | None) -> bool:
    return module is not None and (
        module == BINDINGS_PACKAGE or module.startswith(f"{BINDINGS_PACKAGE}.")
    )


def _root_name(node: ast.expr) -> str | None:
    """Return the base name of an attribute chain such as ``a.b.c`` (``a``)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def find_banned_bindings_usage(source: str, filename: str = "<string>") -> list[str]:
    """Return ``filename:line: reason`` entries for banned oscal-bindings usage in ``source``.

    Flags any ``.model_validate_json`` attribute, any ``parse_*`` name imported from
    ``oscal_bindings`` or a submodule, and any ``parse_*`` attribute reached through a name
    bound by an ``oscal_bindings`` import (``import oscal_bindings as ob; ob.parse_oscal()``).
    """
    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    bindings_names: set[str] = set()

    # First pass: collect names bound by oscal_bindings imports.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_bindings_module(alias.name):
                    # `import a.b` binds `a`; `import a.b as x` binds `x`.
                    bindings_names.add(alias.asname or alias.name.split(".")[0])
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and _is_bindings_module(node.module)
        ):
            for alias in node.names:
                if alias.name.startswith(BANNED_PREFIX):
                    violations.append(
                        f"{filename}:{node.lineno}: imports {alias.name} from {node.module}"
                    )
                bindings_names.add(alias.asname or alias.name)

    # Second pass: attribute access.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr == BANNED_METHOD:
            violations.append(f"{filename}:{node.lineno}: uses .{BANNED_METHOD}")
        elif node.attr.startswith(BANNED_PREFIX) and _root_name(node.value) in bindings_names:
            violations.append(f"{filename}:{node.lineno}: uses oscal_bindings {node.attr}")

    return violations


@pytest.mark.unit
class TestSourceScan:
    """The Server never uses ``model_validate_json`` or the Bindings ``parse_*`` helpers."""

    def test_src_has_no_banned_bindings_usage(self):
        files = sorted(SRC_ROOT.rglob("*.py"))
        assert files, f"no Python files found under {SRC_ROOT}"
        violations = [
            v
            for path in files
            for v in find_banned_bindings_usage(
                path.read_text(encoding="utf-8"), str(path.relative_to(SRC_ROOT))
            )
        ]
        assert violations == []

    @pytest.mark.parametrize(
        "source",
        [
            "Catalog.model_validate_json(text)",
            "f = MODEL_MAP[t].model_validate_json",
            "from oscal_bindings import parse_oscal",
            "from oscal_bindings.parsing import parse_oscal_file as p",
            "import oscal_bindings\noscal_bindings.parse_oscal(doc)",
            "import oscal_bindings.parsing as obp\nobp.parse_catalog(doc)",
            "from oscal_bindings import parsing\nparsing.parse_profile(doc)",
        ],
    )
    def test_scanner_detects_violation(self, source):
        assert find_banned_bindings_usage(source)

    @pytest.mark.parametrize(
        "source",
        [
            "MODEL_MAP[t].model_validate(json.loads(text))",
            "from oscal_bindings.models import Catalog",
            "from oscal_bindings import models\nmodels.Catalog.model_validate(doc)",
            "from argparse import parse_args\nimport urllib\nurllib.parse_qs('a=1')",
            "import json\njson.parse_thing(x)",
            "from .utils import parse_page",
        ],
    )
    def test_scanner_allows_clean_source(self, source):
        assert find_banned_bindings_usage(source) == []

    def test_scanner_reports_location(self):
        violations = find_banned_bindings_usage("x = 1\nCatalog.model_validate_json(s)", "a.py")
        assert violations == ["a.py:2: uses .model_validate_json"]


# ---------------------------------------------------------------------------
# Section 2: Property tests (Properties 1-6 of the design)
# ---------------------------------------------------------------------------
#
# ``oscal_documents()`` generates model-valid documents for all eight model types, each as
# ``(model_type, doc)`` with ``doc`` wrapped in its root key. Every identifier a Store child
# uses (UUIDs and catalog tokens) is unique within a document, as OSCAL requires and as the
# ``child_elements`` UNIQUE(uuid, parent_doc_id) key assumes. ``expected_children()`` is the
# oracle: it walks the raw dict, never the Bindings model, so it checks the Store's model walk
# independently.

_MAX_CHILDREN = 4
_MAX_GROUP_DEPTH = 3
_counts = st.integers(min_value=0, max_value=_MAX_CHILDREN)
_uuids = st.uuids(version=4).map(str)
_tokens = st.from_regex(r"[a-z][a-z0-9]{0,5}", fullmatch=True)
_texts = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=20,
)


def _fresh(draw: st.DrawFn, ids: st.SearchStrategy[str], used: set[str]) -> str:
    """Draw an identifier not yet in ``used`` and record it."""
    value = draw(ids.filter(lambda v: v not in used))
    used.add(value)
    return value


def _fresh_token(draw: st.DrawFn, used: set[str]) -> str:
    """Draw a catalog token that is unique by construction (no filter rejections)."""
    value = f"{draw(_tokens)}-{len(used)}"
    used.add(value)
    return value


def _wrap(model_type: OSCALModelType, body: dict, used: set[str], draw: st.DrawFn) -> dict:
    """Wrap ``body`` with a document UUID and metadata under the model's root key."""
    inner = {
        "uuid": _fresh(draw, _uuids, used),
        "metadata": {**_COMMON_METADATA, "title": draw(_texts)},
        **body,
    }
    return {model_type.value: inner}


@st.composite
def _component_definitions(draw: st.DrawFn) -> dict:
    used: set[str] = set()
    body: dict = {}
    if components := [
        {
            "uuid": _fresh(draw, _uuids, used),
            "type": draw(st.sampled_from(["software", "service", "hardware"])),
            "title": draw(_texts),
            "description": draw(_texts),
        }
        for _ in range(draw(_counts))
    ]:
        body["components"] = components
    if capabilities := [
        {"uuid": _fresh(draw, _uuids, used), "name": draw(_texts), "description": draw(_texts)}
        for _ in range(draw(_counts))
    ]:
        body["capabilities"] = capabilities
    return _wrap(OSCALModelType.COMPONENT_DEFINITION, body, used, draw)


def _controls(draw: st.DrawFn, used: set[str], min_size: int = 0) -> list[dict]:
    n = draw(st.integers(min_value=min_size, max_value=_MAX_CHILDREN))
    return [{"id": _fresh_token(draw, used), "title": draw(_texts)} for _ in range(n)]


def _group(draw: st.DrawFn, used: set[str], depth: int) -> dict:
    """One catalog group. Bindings 0.3.x splits groups into ``CatalogGroupWithControls`` and
    ``CatalogGroupWithGroups``, so a group holds controls or sub-groups, not both."""
    group: dict = {"id": _fresh_token(draw, used), "title": draw(_texts)}
    kinds = ["empty", "controls"] + (["groups"] if depth < _MAX_GROUP_DEPTH else [])
    match draw(st.sampled_from(kinds)):
        case "controls":
            group["controls"] = _controls(draw, used, min_size=1)
        case "groups":
            n = draw(st.integers(min_value=1, max_value=3))
            group["groups"] = [_group(draw, used, depth + 1) for _ in range(n)]
    return group


@st.composite
def _catalogs(draw: st.DrawFn) -> dict:
    used: set[str] = set()
    body: dict = {}
    if controls := _controls(draw, used):
        body["controls"] = controls
    if groups := [_group(draw, used, 1) for _ in range(draw(_counts))]:
        body["groups"] = groups
    return _wrap(OSCALModelType.CATALOG, body, used, draw)


@st.composite
def _profiles(draw: st.DrawFn) -> dict:
    used: set[str] = set()
    n_imports = draw(st.integers(min_value=1, max_value=_MAX_CHILDREN))
    body: dict = {
        "imports": [{"href": f"#import-{i}", "include-all": {}} for i in range(n_imports)]
    }
    if draw(st.booleans()):
        body["modify"] = {"set-parameters": [{"param-id": draw(_tokens)}]}
    return _wrap(OSCALModelType.PROFILE, body, used, draw)


@st.composite
def _system_security_plans(draw: st.DrawFn) -> dict:
    used: set[str] = set()
    n_components = draw(st.integers(min_value=1, max_value=_MAX_CHILDREN))
    body = {
        "import-profile": {"href": "#profile"},
        "system-characteristics": {
            "system-ids": [{"id": "system-1"}],
            "system-name": draw(_texts),
            "description": draw(_texts),
            "system-information": {
                "information-types": [{"title": draw(_texts), "description": draw(_texts)}]
            },
            "status": {"state": "operational"},
            "authorization-boundary": {"description": draw(_texts)},
        },
        "system-implementation": {
            "users": [{"uuid": _fresh(draw, _uuids, used)}],
            "components": [
                {
                    "uuid": _fresh(draw, _uuids, used),
                    "type": draw(st.sampled_from(["this-system", "software", "service"])),
                    "title": draw(_texts),
                    "description": draw(_texts),
                    "status": {"state": "operational"},
                }
                for _ in range(n_components)
            ],
        },
        "control-implementation": {
            "description": draw(_texts),
            "implemented-requirements": [
                {"uuid": _fresh(draw, _uuids, used), "control-id": "ac-1"}
            ],
        },
    }
    return _wrap(OSCALModelType.SYSTEM_SECURITY_PLAN, body, used, draw)


_REVIEWED_CONTROLS: dict = {"control-selections": [{"include-all": {}}]}


@st.composite
def _assessment_plans(draw: st.DrawFn) -> dict:
    used: set[str] = set()
    body: dict = {"import-ssp": {"href": "#ssp"}, "reviewed-controls": _REVIEWED_CONTROLS}
    if activities := [
        {"uuid": _fresh(draw, _uuids, used), "title": draw(_texts), "description": draw(_texts)}
        for _ in range(draw(_counts))
    ]:
        body["local-definitions"] = {"activities": activities}
    if tasks := [
        {
            "uuid": _fresh(draw, _uuids, used),
            "type": draw(st.sampled_from(["action", "milestone"])),
            "title": draw(_texts),
        }
        for _ in range(draw(_counts))
    ]:
        body["tasks"] = tasks
    return _wrap(OSCALModelType.ASSESSMENT_PLAN, body, used, draw)


def _finding(draw: st.DrawFn, used: set[str]) -> dict:
    return {
        "uuid": _fresh(draw, _uuids, used),
        "title": draw(_texts),
        "description": draw(_texts),
        "target": {
            "type": draw(st.sampled_from(["statement-id", "objective-id"])),
            "target-id": "ac-1_smt",
            "status": {"state": draw(st.sampled_from(["satisfied", "not-satisfied"]))},
        },
    }


@st.composite
def _assessment_results(draw: st.DrawFn) -> dict:
    used: set[str] = set()
    results = []
    for _ in range(draw(st.integers(min_value=1, max_value=3))):
        result: dict = {
            "uuid": _fresh(draw, _uuids, used),
            "title": draw(_texts),
            "description": draw(_texts),
            "start": "2024-01-01T00:00:00Z",
            "reviewed-controls": _REVIEWED_CONTROLS,
        }
        if findings := [_finding(draw, used) for _ in range(draw(_counts))]:
            result["findings"] = findings
        results.append(result)
    body = {"import-ap": {"href": "#ap"}, "results": results}
    return _wrap(OSCALModelType.ASSESSMENT_RESULTS, body, used, draw)


@st.composite
def _poams(draw: st.DrawFn) -> dict:
    used: set[str] = set()
    # poam-items is required (min 1) in OSCAL 1.2.3.
    n_items = draw(st.integers(min_value=1, max_value=_MAX_CHILDREN))
    items = [
        {"uuid": _fresh(draw, _uuids, used), "title": draw(_texts), "description": draw(_texts)}
        for _ in range(n_items)
    ]
    body = {"poam-items": items}
    return _wrap(OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES, body, used, draw)


@st.composite
def _mapping_collections(draw: st.DrawFn) -> dict:
    n = draw(st.integers(min_value=1, max_value=6))
    return _make_mapping_collection(draw(st.lists(_uuids, min_size=n, max_size=n, unique=True)))


_DOCUMENT_STRATEGIES: dict[OSCALModelType, st.SearchStrategy[dict]] = {
    OSCALModelType.COMPONENT_DEFINITION: _component_definitions(),
    OSCALModelType.CATALOG: _catalogs(),
    OSCALModelType.PROFILE: _profiles(),
    OSCALModelType.SYSTEM_SECURITY_PLAN: _system_security_plans(),
    OSCALModelType.ASSESSMENT_PLAN: _assessment_plans(),
    OSCALModelType.ASSESSMENT_RESULTS: _assessment_results(),
    OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES: _poams(),
    OSCALModelType.MAPPING: _mapping_collections(),
}


def oscal_documents() -> st.SearchStrategy[tuple[OSCALModelType, dict]]:
    """Generate ``(model_type, doc)`` for a model-valid document of any of the 8 model types."""
    return st.one_of(
        *(st.tuples(st.just(t), strategy) for t, strategy in _DOCUMENT_STRATEGIES.items())
    )


def _controls_in_groups(groups: list[dict]) -> Iterator[str]:
    for group in groups:
        yield from (c["id"] for c in group.get("controls", []))
        yield from _controls_in_groups(group.get("groups", []))


def expected_children(model_type: OSCALModelType, doc: dict) -> set[tuple[str, str]]:
    """Return the (element_type, id) pairs the Store should extract, walking the raw dict.

    Follows the Store's documented contract: catalog groups are top-level only, while
    controls are collected at the catalog root and at every group depth; profile imports
    are ``import-<index>``; singleton children use their element type as the id.
    """
    inner = doc[model_type.value]
    match model_type:
        case OSCALModelType.COMPONENT_DEFINITION:
            pairs = [("component", c["uuid"]) for c in inner.get("components", [])]
            pairs += [("capability", c["uuid"]) for c in inner.get("capabilities", [])]
        case OSCALModelType.CATALOG:
            groups = inner.get("groups", [])
            pairs = [("control", c["id"]) for c in inner.get("controls", [])]
            pairs += [("group", g["id"]) for g in groups]
            pairs += [("control", cid) for cid in _controls_in_groups(groups)]
        case OSCALModelType.PROFILE:
            pairs = [("import", f"import-{i}") for i in range(len(inner["imports"]))]
            if "modify" in inner:
                pairs.append(("modify", "modify"))
        case OSCALModelType.SYSTEM_SECURITY_PLAN:
            pairs = [("control-implementation", "control-implementation")]
            pairs += [
                ("system-component", c["uuid"])
                for c in inner["system-implementation"]["components"]
            ]
        case OSCALModelType.ASSESSMENT_PLAN:
            pairs = [("task", t["uuid"]) for t in inner.get("tasks", [])]
            activities = inner.get("local-definitions", {}).get("activities", [])
            pairs += [("activity", a["uuid"]) for a in activities]
        case OSCALModelType.ASSESSMENT_RESULTS:
            pairs = []
            for result in inner["results"]:
                pairs.append(("result", result["uuid"]))
                pairs += [("finding", f["uuid"]) for f in result.get("findings", [])]
        case OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES:
            pairs = [("poam-item", p["uuid"]) for p in inner["poam-items"]]
        case OSCALModelType.MAPPING:
            pairs = [("mapping", m["uuid"]) for m in inner["mappings"]]
    assert len(pairs) == len(set(pairs)), "generator produced duplicate child ids"
    return set(pairs)


@contextmanager
def ingested_store(doc: dict) -> Iterator[OscalStore]:
    """Yield a fresh, non-seeded ``OscalStore`` that has scanned exactly ``doc``."""
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp) / "docs"
        docs_dir.mkdir()
        (docs_dir / "doc.json").write_text(json.dumps(doc), encoding="utf-8")
        store = OscalStore(
            db_path=str(Path(tmp) / "store.db"), cache_size=4, seed_from_bundled=False
        )
        try:
            assert store.scan_directory(docs_dir) == 1, "document was not ingested"
            yield store
        finally:
            # Close before the temp dir is removed (Windows cannot delete an open SQLite file).
            store.close()


def list_all_children(store: OscalStore, doc_uuid: str) -> list[dict]:
    """Return every child element of ``doc_uuid`` through the public Store API."""
    page = store.list_child_elements(parent_doc_uuid=doc_uuid, limit=10_000, include_raw_json=True)
    assert not page["hasMore"]
    return page["items"]


def _python_only_field_names() -> frozenset[str]:
    """Return Bindings Python field names that differ from their alias and are no alias anywhere.

    Such a name (e.g. ``control_id``) is never a valid OSCAL key, so it must not appear in
    serialized output.
    """
    python_names: set[str] = set()
    aliases: set[str] = set()
    for obj in vars(oscal_models).values():
        if not (isinstance(obj, type) and issubclass(obj, BaseModel)):
            continue
        for name, field in obj.model_fields.items():
            alias = field.alias or name
            aliases.add(alias)
            if alias != name:
                python_names.add(name)
    return frozenset(python_names - aliases)


PYTHON_ONLY_FIELD_NAMES = _python_only_field_names()


def _bad_json_paths(value: object, path: str = "$") -> Iterator[str]:
    """Yield the path of every ``null`` value and every Python-only key in ``value``."""
    if value is None:
        yield f"{path}: null"
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in PYTHON_ONLY_FIELD_NAMES:
                yield f"{path}.{key}: Python field name"
            yield from _bad_json_paths(child, f"{path}.{key}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _bad_json_paths(child, f"{path}[{i}]")


def _root(value: Any) -> list:
    """Unwrap an optional ``RootModel`` list wrapper to a plain list."""
    return list(getattr(value, "root", value) or [])


def _groups_controls(groups: list) -> Iterator[tuple[tuple[str, str], BaseModel]]:
    for group in groups:
        # Group union variants: only CatalogGroupWithControls has `controls`.
        for ctrl in getattr(group, "controls", None) or []:
            yield ("control", ctrl.id), ctrl
        yield from _groups_controls(getattr(group, "groups", None) or [])


def child_objects(model_type: OSCALModelType, model: BaseModel) -> dict[tuple[str, str], BaseModel]:
    """Map each expected child's (element_type, id) to its parsed Bindings object.

    Walks the parsed ``MODEL_MAP`` model along the same paths the Store extracts, so each
    object is the concrete one the Store serialized (e.g. the catalog group union variant).
    """
    m: Any = model
    pairs: list[tuple[tuple[str, str], BaseModel]] = []
    match model_type:
        case OSCALModelType.COMPONENT_DEFINITION:
            pairs += [(("component", str(c.uuid)), c) for c in _root(m.components)]
            pairs += [(("capability", str(c.uuid)), c) for c in _root(m.capabilities)]
        case OSCALModelType.CATALOG:
            groups = _root(m.groups)
            pairs += [(("control", c.id), c) for c in _root(m.controls)]
            pairs += [(("group", g.id), g) for g in groups]
            pairs += list(_groups_controls(groups))
        case OSCALModelType.PROFILE:
            pairs += [(("import", f"import-{i}"), x) for i, x in enumerate(m.imports)]
            if m.modify is not None:
                pairs.append((("modify", "modify"), m.modify))
        case OSCALModelType.SYSTEM_SECURITY_PLAN:
            pairs.append(
                (("control-implementation", "control-implementation"), m.control_implementation)
            )
            pairs += [
                (("system-component", str(c.uuid)), c)
                for c in _root(m.system_implementation.components)
            ]
        case OSCALModelType.ASSESSMENT_PLAN:
            pairs += [(("task", str(t.uuid)), t) for t in _root(m.tasks)]
            if m.local_definitions is not None:
                pairs += [
                    (("activity", str(a.uuid)), a) for a in _root(m.local_definitions.activities)
                ]
        case OSCALModelType.ASSESSMENT_RESULTS:
            for result in _root(m.results):
                pairs.append((("result", str(result.uuid)), result))
                pairs += [(("finding", str(f.uuid)), f) for f in _root(result.findings)]
        case OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES:
            pairs += [(("poam-item", str(p.uuid)), p) for p in _root(m.poam_items)]
        case OSCALModelType.MAPPING:
            pairs += [(("mapping", str(x.uuid)), x) for x in _root(m.mappings)]
    return dict(pairs)


def child_classes(model_type: OSCALModelType, model: BaseModel) -> dict[tuple[str, str], type]:
    """Map each expected child's (element_type, id) to the Bindings class of its object."""
    return {key: type(obj) for key, obj in child_objects(model_type, model).items()}


def validator_levels(model_type: OSCALModelType, doc: dict) -> dict[str, dict]:
    """Run the public Validator on ``doc`` as JSON text and return its levels keyed by name.

    oscal-cli is hidden from ``PATH`` lookup so level 4 is always skipped and the result does
    not depend on the local environment. Asserts the reported model type matches.
    """
    with patch("mcp_server_for_oscal.tools.validate_oscal_content.shutil.which", return_value=None):
        result = validate_oscal_content(json.dumps(doc), model_type.value, ctx=None)
    assert result["model_type"] == model_type.value
    return {level["level"]: level for level in result["levels"]}


def validator_model_level(model_type: OSCALModelType, doc: dict) -> dict:
    """Return the Validator's model level (level 3) result for ``doc``.

    Asserts JSON parsing (level 1) succeeded and the model level ran, so a ``valid`` of
    ``False`` always means the Bindings model rejected the document.
    """
    levels = validator_levels(model_type, doc)
    assert levels["well_formedness"]["valid"], levels["well_formedness"]["errors"]
    model_level = levels["model"]
    assert not model_level["skipped"], model_level["skip_reason"]
    return model_level


# Closed-enum sites in the generated documents, as (owner class, Python field name). Component
# and task `type` are open (`str | Enum`) in Bindings 0.3.1, so they are not listed.
_CLOSED_ENUM_FIELDS: dict[str, tuple[type[BaseModel], str]] = {
    "ssp-status-state": (oscal_models.Status, "state"),
    "component-status-state": (oscal_models.SystemComponentStatus, "state"),
    "finding-target-type": (oscal_models.FindingTarget, "type"),
    "finding-status-state": (oscal_models.FindingTargetStatus, "state"),
}


def _closed_enum(site: str) -> type[enum.Enum]:
    """Return the Enum class of a closed-enum site, read from the Bindings field annotation.

    Fails if the annotation is not a bare ``Enum`` subclass (for example ``str | Enum``,
    which accepts any string and so is not closed).
    """
    owner, field_name = _CLOSED_ENUM_FIELDS[site]
    annotation = owner.model_fields[field_name].annotation
    assert isinstance(annotation, type), f"{site}: {annotation!r} is not a closed enum"
    assert issubclass(annotation, enum.Enum), f"{site}: {annotation!r} is not an Enum"
    return annotation


EnumSite = tuple[tuple[str | int, ...], type[enum.Enum]]


def closed_enum_sites(model_type: OSCALModelType, doc: dict) -> list[EnumSite]:
    """Return (JSON path, Enum class) for every closed-enum value in a generated document.

    Paths start at the root key and use hyphenated OSCAL keys and list indices.
    """
    root = model_type.value
    inner = doc[root]
    sites: list[EnumSite] = []
    match model_type:
        case OSCALModelType.SYSTEM_SECURITY_PLAN:
            sites.append(
                (
                    (root, "system-characteristics", "status", "state"),
                    _closed_enum("ssp-status-state"),
                )
            )
            for i, _ in enumerate(inner["system-implementation"]["components"]):
                path = (root, "system-implementation", "components", i, "status", "state")
                sites.append((path, _closed_enum("component-status-state")))
        case OSCALModelType.ASSESSMENT_RESULTS:
            for i, result in enumerate(inner["results"]):
                for j, _ in enumerate(result.get("findings", [])):
                    target = (root, "results", i, "findings", j, "target")
                    sites.append(((*target, "type"), _closed_enum("finding-target-type")))
                    sites.append(
                        ((*target, "status", "state"), _closed_enum("finding-status-state"))
                    )
    return sites


def _set_path(doc: dict, path: tuple[str | int, ...], value: object) -> None:
    node: Any = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


@st.composite
def out_of_enum_mutations(draw: st.DrawFn) -> tuple[OSCALModelType, dict, dict, EnumSite, str]:
    """Draw ``(model_type, doc, mutated, site, bad_value)`` for a document with an enum site.

    ``mutated`` is a deep copy of ``doc`` with one closed-enum value replaced by a string that
    is not a member of that enum.
    """
    model_type = draw(
        st.sampled_from([OSCALModelType.SYSTEM_SECURITY_PLAN, OSCALModelType.ASSESSMENT_RESULTS])
    )
    doc = draw(_DOCUMENT_STRATEGIES[model_type])
    sites = closed_enum_sites(model_type, doc)
    assume(sites)  # assessment results may have no findings
    site = draw(st.sampled_from(sites))
    members = {m.value for m in site[1]}
    bad_value = draw(st.text(max_size=30).filter(lambda s: s not in members))
    mutated = copy.deepcopy(doc)
    _set_path(mutated, site[0], bad_value)
    return model_type, doc, mutated, site, bad_value


def _alias_python_names() -> dict[str, frozenset[str]]:
    """Map each Bindings alias to the Python field names that differ from it, across models."""
    names: dict[str, set[str]] = {}
    for obj in vars(oscal_models).values():
        if not (isinstance(obj, type) and issubclass(obj, BaseModel)):
            continue
        for name, field in obj.model_fields.items():
            if field.alias and field.alias != name:
                names.setdefault(field.alias, set()).add(name)
    return {alias: frozenset(pythons) for alias, pythons in names.items()}


ALIAS_PYTHON_NAMES = _alias_python_names()

KeyPath = tuple[str | int, ...]


def python_name_candidates(value: object, path: KeyPath = ()) -> Iterator[tuple[KeyPath, str]]:
    """Yield (path to key, Python field name) for every hyphenated key with a distinct Python name.

    A key qualifies when it is a Bindings alias and its underscore spelling is the Python field
    name for that alias, so renaming it spells the key the way Python code would.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            python_name = key.replace("-", "_")
            if python_name != key and python_name in ALIAS_PYTHON_NAMES.get(key, ()):
                yield (*path, key), python_name
            yield from python_name_candidates(child, (*path, key))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from python_name_candidates(child, (*path, i))


def _rename_key(doc: dict, path: KeyPath, new_key: str) -> None:
    node: Any = doc
    for key in path[:-1]:
        node = node[key]
    node[new_key] = node.pop(path[-1])


@st.composite
def python_field_name_mutations(
    draw: st.DrawFn,
) -> tuple[OSCALModelType, dict, dict, KeyPath, str]:
    """Draw ``(model_type, doc, mutated, key_path, python_name)``.

    ``mutated`` is a deep copy of ``doc`` with one hyphenated key renamed to its Python field
    name. Paths start at the root key.
    """
    model_type, doc = draw(oscal_documents())
    candidates = list(python_name_candidates(doc))
    assume(candidates)  # metadata `last-modified`/`oscal-version` make this always true
    key_path, python_name = draw(st.sampled_from(candidates))
    mutated = copy.deepcopy(doc)
    _rename_key(mutated, key_path, python_name)
    return model_type, doc, mutated, key_path, python_name


def store_ingest_result(doc: dict) -> tuple[int, int]:
    """Scan ``doc`` into a fresh, non-seeded Store; return (scan count, stored document total)."""
    with tempfile.TemporaryDirectory() as tmp:
        docs_dir = Path(tmp) / "docs"
        docs_dir.mkdir()
        (docs_dir / "doc.json").write_text(json.dumps(doc), encoding="utf-8")
        store = OscalStore(
            db_path=str(Path(tmp) / "store.db"), cache_size=4, seed_from_bundled=False
        )
        try:
            count = store.scan_directory(docs_dir)
            total = store.list_documents(limit=1)["total"]
        finally:
            # Close before the temp dir is removed (Windows cannot delete an open SQLite file).
            store.close()
    return count, total


# Open-enum sites in the generated documents, as (owner class, Python field name, Store child
# element type). Each field is `Annotated[str, pattern] | Enum` in Bindings 0.3.1: the listed
# values are suggestions and any value of the base datatype is valid.
_OPEN_ENUM_FIELDS: dict[OSCALModelType, tuple[type[BaseModel], str, str]] = {
    OSCALModelType.COMPONENT_DEFINITION: (oscal_models.DefinedComponent, "type", "component"),
    OSCALModelType.SYSTEM_SECURITY_PLAN: (oscal_models.SystemComponent, "type", "system-component"),
    OSCALModelType.ASSESSMENT_PLAN: (oscal_models.Task, "type", "task"),
}

# Base-datatype patterns of the open sites, as emitted by Bindings 0.3.1 (pydantic-core matches
# them with Rust regex semantics, where `\p{L}`/`\p{N}` are Unicode categories).
STRING_PATTERN = r"^\S(.*\S)?$"
NCNAME_PATTERN = r"^(\p{L}|_)(\p{L}|\p{N}|[.\-_])*$"

# Letters and digits from early, long-stable Unicode blocks so Python's `unicodedata` and the
# Rust regex Unicode tables agree on every drawn character's category.
_MAX_CODEPOINT = 0x2FFF
_letters = st.characters(categories=["L"], max_codepoint=_MAX_CODEPOINT)
_ncname_chars = st.one_of(
    _letters,
    st.characters(categories=["N"], max_codepoint=_MAX_CODEPOINT),
    st.sampled_from(".-_"),
)
_ncnames = st.builds(
    lambda first, rest: first + "".join(rest),
    st.one_of(_letters, st.just("_")),
    st.lists(_ncname_chars, max_size=20),
)
# Non-empty string with no leading or trailing whitespace; inner spaces are allowed.
_visible = st.characters(categories=("L", "N", "P", "S"), max_codepoint=_MAX_CODEPOINT)
_non_empty_strings = st.one_of(
    _visible,
    st.builds(
        lambda first, middle, last: first + "".join(middle) + last,
        _visible,
        st.lists(st.one_of(_visible, st.just(" ")), max_size=20),
        _visible,
    ),
)
_BASE_DATATYPE_VALUES: dict[str, st.SearchStrategy[str]] = {
    STRING_PATTERN: _non_empty_strings,
    NCNAME_PATTERN: _ncnames,
}


def _open_enum(model_type: OSCALModelType) -> tuple[str, type[enum.Enum]]:
    """Return (base-datatype pattern, Enum class) of an open-enum site's Bindings field.

    Fails if the annotation is not exactly one pattern-constrained ``str`` branch plus one
    ``Enum`` branch, so the test fails if upstream closes the enum or changes its datatype.
    """
    owner, field_name, _ = _OPEN_ENUM_FIELDS[model_type]
    annotation = owner.model_fields[field_name].annotation
    branches = typing.get_args(annotation)
    enums = [b for b in branches if isinstance(b, type) and issubclass(b, enum.Enum)]
    strs = [
        b
        for b in branches
        if typing.get_origin(b) is typing.Annotated and typing.get_args(b)[0] is str
    ]
    assert len(enums) == 1, f"{owner.__name__}.{field_name}: no Enum branch in {annotation!r}"
    assert len(strs) == 1, f"{owner.__name__}.{field_name}: not open (no str branch)"
    patterns = [
        meta.pattern
        for info in typing.get_args(strs[0])[1:]
        for meta in getattr(info, "metadata", [])
        if getattr(meta, "pattern", None)
    ]
    assert len(patterns) == 1, f"{owner.__name__}.{field_name}: patterns {patterns!r}"
    assert patterns[0] in _BASE_DATATYPE_VALUES, f"unknown base datatype {patterns[0]!r}"
    return patterns[0], enums[0]


def open_enum_paths(model_type: OSCALModelType, doc: dict) -> list[tuple[KeyPath, str]]:
    """Return (JSON path to the open-enum value, owning child's uuid) for each open site."""
    root = model_type.value
    inner = doc[root]
    match model_type:
        case OSCALModelType.COMPONENT_DEFINITION:
            parents: tuple[str, ...] = (root, "components")
            items = inner.get("components", [])
        case OSCALModelType.SYSTEM_SECURITY_PLAN:
            parents = (root, "system-implementation", "components")
            items = inner["system-implementation"]["components"]
        case OSCALModelType.ASSESSMENT_PLAN:
            parents = (root, "tasks")
            items = inner.get("tasks", [])
        case _:
            items = []
    return [((*parents, i, "type"), item["uuid"]) for i, item in enumerate(items)]


@st.composite
def off_list_open_enum_mutations(
    draw: st.DrawFn,
) -> tuple[OSCALModelType, dict, dict, KeyPath, str, str]:
    """Draw ``(model_type, doc, mutated, path, child_uuid, value)``.

    ``mutated`` is a deep copy of ``doc`` with one open-enum value replaced by a value of the
    field's base datatype that is not a listed member of the field's Enum.
    """
    model_type = draw(st.sampled_from(list(_OPEN_ENUM_FIELDS)))
    doc = draw(_DOCUMENT_STRATEGIES[model_type])
    paths = open_enum_paths(model_type, doc)
    assume(paths)  # component definitions and assessment plans may have no open site
    path, child_uuid = draw(st.sampled_from(paths))
    pattern, enum_cls = _open_enum(model_type)
    members = {m.value for m in enum_cls}
    value = draw(_BASE_DATATYPE_VALUES[pattern].filter(lambda v: v not in members))
    mutated = copy.deepcopy(doc)
    _set_path(mutated, path, value)
    return model_type, doc, mutated, path, child_uuid, value


@pytest.mark.unit
class TestBindingsUpgradeProperties:
    """Properties 1-7 of the upgrade-oscal-bindings-0-3-1 design."""

    @given(generated=oscal_documents())
    @settings(max_examples=100, deadline=None)
    def test_child_extraction_is_complete(self, generated):
        """Feature: upgrade-oscal-bindings-0-3-1, Property 1: Child extraction is complete.

        For any generated valid document of any model type, including catalogs with nested
        groups and mapping collections with any number of mappings, the (element_type, id)
        pairs the Store extracts equal the pairs computed by walking the raw input dict.

        **Validates: Requirements 4.1, 4.4, 4.5**
        """
        model_type, doc = generated
        inner = doc[model_type.value]
        # Guard the generator: the document must be model-valid under the Bindings class.
        MODEL_MAP[model_type].model_validate(inner)
        expected = expected_children(model_type, doc)

        with ingested_store(doc) as store:
            items = list_all_children(store, inner["uuid"])

        extracted = [(item["element_type"], item["id"]) for item in items]
        assert len(extracted) == len(set(extracted)), f"duplicate children: {extracted}"
        assert set(extracted) == expected
        assert {element_type for element_type, _ in extracted} <= set(
            CHILD_ELEMENT_TYPES[model_type]
        )

    @given(generated=oscal_documents())
    @settings(max_examples=100, deadline=None)
    def test_child_raw_json_is_valid_oscal_and_round_trips(self, generated):
        """Feature: upgrade-oscal-bindings-0-3-1, Property 2: Child raw_json is valid OSCAL
        and round-trips.

        For any generated valid document, every extracted child's ``raw_json`` parses as a
        JSON object with no ``null`` values and no Python-only field names, and validating it
        with the child's Bindings class then serializing it yields an equal object.

        **Validates: Requirements 4.2, 4.3**
        """
        model_type, doc = generated
        inner = doc[model_type.value]
        classes = child_classes(model_type, MODEL_MAP[model_type].model_validate(inner))

        with ingested_store(doc) as store:
            items = list_all_children(store, inner["uuid"])

        assert {(item["element_type"], item["id"]) for item in items} == set(classes)
        for item in items:
            key = (item["element_type"], item["id"])
            assert item["raw_json"] is not None, f"{key}: raw_json missing"
            obj = json.loads(item["raw_json"])
            assert isinstance(obj, dict), f"{key}: raw_json is not an object"
            assert list(_bad_json_paths(obj)) == [], f"{key}: invalid OSCAL JSON"
            round_tripped = classes[key].model_validate(obj).model_dump(mode="json")
            assert round_tripped == obj, f"{key}: round trip changed the object"

    @given(generated=oscal_documents())
    @settings(max_examples=100, deadline=None)
    def test_validator_accepts_model_valid_documents(self, generated):
        """Feature: upgrade-oscal-bindings-0-3-1, Property 3: Validator accepts model-valid
        documents.

        For any generated document that the ``MODEL_MAP`` class accepts, the Validator reports
        the model level as valid. Only the model level is asserted; the JSON Schema level may
        differ (patterns, formats) and is not part of this property.

        **Validates: Requirements 4.6, 3.4**
        """
        model_type, doc = generated
        # Precondition: the MODEL_MAP class accepts the document.
        MODEL_MAP[model_type].model_validate(doc[model_type.value])

        model_level = validator_model_level(model_type, doc)
        assert model_level["valid"], model_level["errors"]
        assert model_level["errors"] == []

    @given(generated=out_of_enum_mutations())
    @settings(max_examples=100, deadline=None)
    def test_out_of_enum_values_are_rejected(self, generated):
        """Feature: upgrade-oscal-bindings-0-3-1, Property 4: Out-of-enum values are rejected.

        For any generated valid document containing a closed-enum field, and any string that
        is not a member of that enum, replacing the field value with that string makes the
        Validator report the model level as invalid.

        **Validates: Requirements 4.7**
        """
        model_type, doc, mutated, (path, enum_cls), bad_value = generated
        # Precondition: the unmodified document is model-valid.
        MODEL_MAP[model_type].model_validate(doc[model_type.value])
        assert bad_value not in {m.value for m in enum_cls}

        model_level = validator_model_level(model_type, mutated)
        assert model_level["valid"] is False, f"{path} = {bad_value!r} was accepted"
        assert model_level["errors"], "model level is invalid but reported no errors"
        field_name = str(path[-1])
        assert any(field_name in line for line in model_level["errors"]), model_level["errors"]

    @given(generated=python_field_name_mutations())
    @settings(max_examples=100, deadline=None)
    def test_python_field_names_are_rejected_on_the_dict_path(self, generated):
        """Feature: upgrade-oscal-bindings-0-3-1, Property 5: Python field names are rejected
        on the dict path.

        For any generated valid document and any hyphenated key whose Python field name
        differs from its alias, renaming that key to the Python field name makes both
        ``OscalStore`` model validation and the Validator model level reject the document.
        Guards against upstream cairn-proofs/oscal-bindings-python#6 reaching the Server.

        **Validates: Requirements 4.8, 3.4**
        """
        model_type, doc, mutated, key_path, python_name = generated
        # Precondition: the unmodified document is model-valid and ingested by the Store.
        MODEL_MAP[model_type].model_validate(doc[model_type.value])
        assert store_ingest_result(doc) == (1, 1)

        # Store: the mutated document is skipped at model validation and never stored.
        assert store_ingest_result(mutated) == (0, 0), f"{key_path} -> {python_name} stored"

        # Validator: the model level reports the document invalid, with errors.
        model_level = validator_model_level(model_type, mutated)
        assert model_level["valid"] is False, f"{key_path} -> {python_name} was accepted"
        assert model_level["errors"], "model level is invalid but reported no errors"

    @given(generated=oscal_documents())
    @settings(max_examples=100, deadline=None)
    def test_default_serialization_equals_explicit_flags(self, generated):
        """Feature: upgrade-oscal-bindings-0-3-1, Property 6: Default serialization equals
        explicit-flag serialization.

        For any generated valid document, ``model_dump_json()`` with default arguments equals
        ``model_dump_json(by_alias=True, exclude_none=True)`` for the top-level model and for
        every extracted child object. The Python-mode ``model_dump()`` is checked the same way.

        **Validates: Requirements 6.1, 6.5**
        """
        model_type, doc = generated
        model = MODEL_MAP[model_type].model_validate(doc[model_type.value])
        objects: dict[Any, BaseModel] = {("<root>", model_type.value): model}
        objects |= child_objects(model_type, model)

        for key, obj in objects.items():
            explicit_json = obj.model_dump_json(by_alias=True, exclude_none=True)
            assert obj.model_dump_json() == explicit_json, f"{key}: JSON serialization differs"
            explicit_py = obj.model_dump(by_alias=True, exclude_none=True)
            assert obj.model_dump() == explicit_py, f"{key}: Python serialization differs"

    @given(generated=off_list_open_enum_mutations())
    @settings(max_examples=100, deadline=None)
    def test_open_enums_accept_off_list_values(self, generated):
        """Feature: upgrade-oscal-bindings-0-3-1, Property 7: Open enums accept off-list values.

        For any generated valid document with an open-enum field (component ``type`` in
        component definitions and SSPs, assessment plan task ``type``), and any value of the
        field's base datatype that is not a listed value, setting the field to that value keeps
        the Validator model level valid and the Store ingests the document with the value
        unchanged. Contract: cairn-proofs/oscal-bindings-specs#9.

        **Validates: Requirements 4.9**
        """
        model_type, doc, mutated, path, child_uuid, value = generated
        pattern, enum_cls = _open_enum(model_type)
        # Preconditions: the unmodified document is model-valid, and the value is an off-list
        # value of the base datatype (checked with the `regex` module's `\p{...}` support).
        MODEL_MAP[model_type].model_validate(doc[model_type.value])
        assert value not in {m.value for m in enum_cls}
        assert regex.fullmatch(pattern, value), f"{value!r} does not match {pattern!r}"

        model_level = validator_model_level(model_type, mutated)
        assert model_level["valid"] is True, f"{path} = {value!r}: {model_level['errors']}"
        assert model_level["errors"] == []

        assert store_ingest_result(mutated) == (1, 1), f"{path} = {value!r} was not stored"
        element_type = _OPEN_ENUM_FIELDS[model_type][2]
        with ingested_store(mutated) as store:
            items = list_all_children(store, mutated[model_type.value]["uuid"])
        [child] = [i for i in items if (i["element_type"], i["id"]) == (element_type, child_uuid)]
        assert json.loads(child["raw_json"])["type"] == value


# ---------------------------------------------------------------------------
# Section 3: Closed and open enum schema parity (Requirements 4.7, 4.9)
# ---------------------------------------------------------------------------
#
# Contract: cairn-proofs/oscal-bindings-specs#9 (Python fix cairn-proofs/oscal-bindings-python#21,
# included in 0.3.1). The bundled JSON Schema is the oracle: a closed enum is an `allOf` with an
# `enum` member (only listed values are valid); an open enum is an `anyOf` with an `enum` member
# (any value of the base datatype is valid; the listed values are suggestions).

BUNDLED_SCHEMA_PATH = SRC_ROOT / "oscal_schemas" / "oscal_complete_schema.json"

# Pinned so an OSCAL schema bump forces a review of this test.
EXPECTED_SCHEMA_CLOSED_ENUMS = 26
EXPECTED_SCHEMA_OPEN_ENUMS = 27
# Bindings 0.3.1 field counts. Some schema sites split across union classes (for example
# `MetadataPartyWithAddresses`/`MetadataPartyWithLocationUuids`), and some schema sites share
# one field definition, so these need not equal the schema site counts.
EXPECTED_BINDINGS_CLOSED_FIELDS = 26
EXPECTED_BINDINGS_OPEN_FIELDS = 27

ValueSet = frozenset[str]


def schema_enum_sets(
    schema: object,
) -> tuple[list[tuple[str, ValueSet]], list[tuple[str, ValueSet]]]:
    """Return ``(closed, open)`` lists of (JSON pointer, value set) for every enum site."""
    closed: list[tuple[str, ValueSet]] = []
    open_: list[tuple[str, ValueSet]] = []

    def walk(node: object, pointer: str) -> None:
        if isinstance(node, dict):
            for keyword, sink in (("allOf", closed), ("anyOf", open_)):
                members = node.get(keyword)
                if isinstance(members, list):
                    sink.extend(
                        (pointer, frozenset(m["enum"]))
                        for m in members
                        if isinstance(m, dict) and "enum" in m
                    )
            for key, child in node.items():
                walk(child, f"{pointer}/{key}")
        elif isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, f"{pointer}/{i}")

    walk(schema, "#")
    return closed, open_


def _annotation_branches(annotation: object) -> list[object]:
    """Flatten an annotation into its union branches, unwrapping ``Annotated`` and ``list``."""
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        return _annotation_branches(typing.get_args(annotation)[0])
    if origin in {typing.Union, types.UnionType}:
        return [b for arg in typing.get_args(annotation) for b in _annotation_branches(arg)]
    if origin is list:
        return _annotation_branches(typing.get_args(annotation)[0])
    return [annotation]


FieldEnum = tuple[str, ValueSet]  # ("Class.field", value set)


def bindings_enum_fields() -> tuple[list[FieldEnum], list[FieldEnum]]:
    """Return ``(closed, open)`` Bindings enum fields across ``oscal_bindings.models``.

    ``None`` branches are ignored. A field is closed when every remaining branch is an ``Enum``
    (no ``str`` or other base-datatype branch). A field is open when it has an ``Enum`` branch
    plus a base-datatype branch: ``str`` for token/string enums, ``AnyUrl`` for URI enums.
    """
    closed: list[FieldEnum] = []
    open_: list[FieldEnum] = []
    for name, cls in sorted(vars(oscal_models).items()):
        if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
            continue
        for field_name, field in cls.model_fields.items():
            branches = [b for b in _annotation_branches(field.annotation) if b is not type(None)]
            enums = [b for b in branches if isinstance(b, type) and issubclass(b, enum.Enum)]
            if not enums:
                continue
            values = frozenset(m.value for e in enums for m in e)
            (closed if len(enums) == len(branches) else open_).append(
                (f"{name}.{field_name}", values)
            )
    return closed, open_


@pytest.fixture(scope="module")
def schema_sets() -> tuple[list[tuple[str, ValueSet]], list[tuple[str, ValueSet]]]:
    return schema_enum_sets(json.loads(BUNDLED_SCHEMA_PATH.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def bindings_sets() -> tuple[list[FieldEnum], list[FieldEnum]]:
    return bindings_enum_fields()


@pytest.mark.unit
class TestEnumSchemaParity:
    """Closed and open enum value sets in Bindings 0.3.1 match the bundled OSCAL JSON Schema.

    Model-level checks only: ``model_validate`` raises pydantic ``ValidationError``. The
    contract's ``parse-error`` (``OscalParseError``) comes only from the banned ``parse_*``
    helpers, so it is never asserted here.

    **Validates: Requirements 4.7, 4.9**
    """

    def test_schema_enum_counts(self, schema_sets):
        closed, open_ = schema_sets
        assert len(closed) == EXPECTED_SCHEMA_CLOSED_ENUMS
        assert len(open_) == EXPECTED_SCHEMA_OPEN_ENUMS

    def test_bindings_enum_field_counts(self, bindings_sets):
        closed, open_ = bindings_sets
        assert len(closed) == EXPECTED_BINDINGS_CLOSED_FIELDS, [n for n, _ in closed]
        assert len(open_) == EXPECTED_BINDINGS_OPEN_FIELDS, [n for n, _ in open_]

    def test_every_schema_closed_enum_is_bound_closed(self, schema_sets, bindings_sets):
        bound = {values for _, values in bindings_sets[0]}
        missing = [(ptr, sorted(v)) for ptr, v in schema_sets[0] if v not in bound]
        assert missing == [], f"schema closed enums with no closed Bindings field: {missing}"

    def test_every_bindings_closed_field_matches_a_schema_closed_enum(
        self, schema_sets, bindings_sets
    ):
        schema_closed = {values for _, values in schema_sets[0]}
        extra = [(name, sorted(v)) for name, v in bindings_sets[0] if v not in schema_closed]
        assert extra == [], f"closed Bindings fields with no schema closed enum: {extra}"

    def test_no_schema_open_enum_is_bound_closed(self, schema_sets, bindings_sets):
        schema_closed = {values for _, values in schema_sets[0]}
        schema_open = {values for _, values in schema_sets[1]}
        # A value set listed as both closed and open in the schema would make a closed field
        # with that set ambiguous. Bundled OSCAL 1.2.3 has no such overlap; if a schema bump
        # adds one, review the affected sites by pointer before relaxing this check.
        assert schema_closed & schema_open == set()
        wrongly_closed = [name for name, v in bindings_sets[0] if v in schema_open]
        assert wrongly_closed == [], f"open schema enums bound as closed: {wrongly_closed}"

    def test_every_bindings_open_field_matches_a_schema_open_enum(self, schema_sets, bindings_sets):
        schema_open = {values for _, values in schema_sets[1]}
        extra = [(name, sorted(v)) for name, v in bindings_sets[1] if v not in schema_open]
        assert extra == [], f"open Bindings fields with no schema open enum: {extra}"

    @pytest.mark.parametrize(
        ("cls", "data"),
        [
            (oscal_models.ParameterSelection, {"how-many": "seven"}),
            (oscal_models.PortRange, {"transport": "SCTP"}),
        ],
        ids=["parameter-selection-how-many", "port-range-transport"],
    )
    def test_closed_enum_rejects_off_list_value(self, cls, data):
        with pytest.raises(ValidationError):
            cls.model_validate(data)

    @pytest.mark.parametrize(
        ("cls", "data"),
        [
            (oscal_models.Link, {"href": "#x", "rel": "custom-relation"}),
            (oscal_models.Hash, {"algorithm": "BLAKE3", "value": "00ff"}),
        ],
        ids=["link-rel", "hash-algorithm"],
    )
    def test_open_enum_accepts_off_list_value(self, cls, data):
        model = cls.model_validate(data)
        assert model.model_dump(mode="json") == data
