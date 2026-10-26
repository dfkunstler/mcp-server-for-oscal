"""Helpers for building real ``OscalStore`` instances from Component Definitions.

Tests use these helpers instead of mocking ``OscalStore``: each helper writes
real OSCAL JSON to disk and ingests it with ``OscalStore.scan_directory``.
"""

import json
import shutil
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from mcp_server_for_oscal.tools.oscal_store import (
    CHILD_ELEMENT_TYPES,
    ORIGIN_BUNDLED,
    ORIGIN_USER,
    OscalStore,
    doc_id,
)
from mcp_server_for_oscal.tools.utils import OSCALModelType

FIXTURES_DIR = Path(__file__).parent / "fixtures"
# One minimal, model-valid document per OSCALModelType, named ``<model type value>.json``.
MODEL_FIXTURES_DIR = FIXTURES_DIR / "models"

SAMPLE_CDEF_FILE = "sample_component_definition.json"
MULTI_CDEF_FILE = "multi_component_definition.json"
CAPABILITIES_CDEF_FILE = "sample_component_definition_with_capabilities.json"
VALID_CDEF_FILES = (SAMPLE_CDEF_FILE, MULTI_CDEF_FILE, CAPABILITIES_CDEF_FILE)

MANY_CAPABILITIES_NAME_PREFIX = "Capability "

# --- Path_Leak_Test fixture (Requirement 5) -------------------------------------------------

# Relative_Names of the documentation files in the leak fixture.
LEAK_AWESOME_REL = "awesome-oscal.md"
LEAK_OVERVIEW_REL = "concepts/overview.md"
LEAK_USER_GUIDE_REL = "notes/user-guide.md"
LEAK_CDEFS_ZIP = "cdefs.zip"
LEAK_ZIP_MEMBER = "bundled_component_definition.json"
# UUIDs of the cdef inside ``cdefs.zip``: the sample cdef with fixed UUIDs, so they differ
# from the user copy of the same fixture (``documents.uuid`` is UNIQUE across Origins).
LEAK_ZIP_CDEF_UUID = "5e1f0c2a-0009-4a00-8000-000000000001"
LEAK_ZIP_COMPONENT_UUID = "5e1f0c2a-0009-4a00-8000-000000000002"

# Query words that hit text_search_oscal (catalog ac-2 guidance) and
# query_oscal_documentation (both overview documents).
LEAK_TEXT_QUERY = "account guidance"
LEAK_DOC_QUERY = "overview"

_LEAK_AWESOME_MD = """# Awesome OSCAL

## Tools

- [OSCAL CLI](https://github.com/usnistgov/oscal-cli) - Command line tool for OSCAL content.
- [Leak Fixture Viewer](https://example.org/viewer) - Renders OSCAL documents.

## Content

- [NIST SP 800-53 in OSCAL](https://github.com/usnistgov/oscal-content) - Official catalogs.
"""

_LEAK_BUNDLED_OVERVIEW_MD = """# OSCAL Overview

The bundled overview introduces the OSCAL layers: control, implementation, and assessment.
"""

_LEAK_USER_OVERVIEW_MD = """# Team Overview

This user overview explains how our team maintains OSCAL component definitions.
"""

_LEAK_USER_GUIDE_MD = """# User Guide

Steps for loading component definitions into the server and searching them.
"""


@dataclass(frozen=True)
class ChildRef:
    """One Child_Element: its Element_ID and the Document_UUID of its parent document."""

    element_id: str
    parent_doc_uuid: str


@dataclass(frozen=True)
class LeakFixture:
    """IDs and locations of the Path_Leak_Test Fixture_Store (built by ``build_leak_store``).

    Every ID resolves in ``store``. The location fields (``root``, ``db_path``,
    ``bundled_dir``, ``user_docs_dir``) are the needles the test searches results for.
    """

    root: Path
    db_path: Path
    bundled_dir: Path
    user_docs_dir: Path
    store: OscalStore
    # Document_UUID of the bundled model fixture per OSCALModelType.
    doc_uuids: Mapping[OSCALModelType, str]
    # Component Definition Document_UUIDs per Origin.
    bundled_cdef_uuids: tuple[str, ...]
    user_cdef_uuids: tuple[str, ...]
    # Component and Capability from the bundled component-definition model fixture.
    component_uuid: str
    component_title: str
    capability_uuid: str
    capability_name: str
    # One Child_Element per child element type (keys of CHILD_ELEMENT_TYPES values).
    children: Mapping[str, ChildRef]
    # Doc_IDs of the Documentation_Documents.
    bundled_overview_doc_id: str
    user_overview_doc_id: str
    user_guide_doc_id: str
    awesome_doc_id: str
    # A model-valid OSCAL JSON file under ``user_docs_dir`` (for validate_oscal_file).
    valid_json_file: Path
    text_query: str = LEAK_TEXT_QUERY
    doc_query: str = LEAK_DOC_QUERY

    @property
    def catalog_uuid(self) -> str:
        """Document_UUID of the bundled catalog."""
        return self.doc_uuids[OSCALModelType.CATALOG]

    @property
    def control(self) -> ChildRef:
        """The catalog control ``ac-2`` (Account Management)."""
        return ChildRef("ac-2", self.catalog_uuid)

    @property
    def bundled_doc_id(self) -> str:
        """A bundled Doc_ID (the bundled ``concepts/overview.md``)."""
        return self.bundled_overview_doc_id

    @property
    def user_doc_id(self) -> str:
        """A user Doc_ID (``notes/user-guide.md``)."""
        return self.user_guide_doc_id


def _write_leak_bundled(bundled: Path) -> None:
    """Write the bundled Scan_Root: model fixtures, ``cdefs.zip``, and markdown."""
    bundled.mkdir(parents=True)
    for model_type in OSCALModelType:
        name = f"{model_type.value}.json"
        shutil.copyfile(MODEL_FIXTURES_DIR / name, bundled / name)
    zip_cdef = load_sample_cdef()
    zip_cdef["component-definition"]["uuid"] = LEAK_ZIP_CDEF_UUID
    zip_cdef["component-definition"]["components"][0]["uuid"] = LEAK_ZIP_COMPONENT_UUID
    with zipfile.ZipFile(bundled / LEAK_CDEFS_ZIP, "w") as zf:
        zf.writestr(LEAK_ZIP_MEMBER, json.dumps(zip_cdef))
    (bundled / LEAK_AWESOME_REL).write_text(_LEAK_AWESOME_MD, encoding="utf-8")
    (bundled / LEAK_OVERVIEW_REL).parent.mkdir(parents=True)
    (bundled / LEAK_OVERVIEW_REL).write_text(_LEAK_BUNDLED_OVERVIEW_MD, encoding="utf-8")


def _write_leak_user_docs(user_docs: Path) -> None:
    """Write the user Scan_Root: the three valid cdef fixtures and markdown."""
    user_docs.mkdir(parents=True)
    for name in VALID_CDEF_FILES:
        shutil.copyfile(FIXTURES_DIR / name, user_docs / name)
    for rel, text in (
        (LEAK_USER_GUIDE_REL, _LEAK_USER_GUIDE_MD),
        (LEAK_OVERVIEW_REL, _LEAK_USER_OVERVIEW_MD),
    ):
        (user_docs / rel).parent.mkdir(parents=True, exist_ok=True)
        (user_docs / rel).write_text(text, encoding="utf-8")


def _first_child(store: OscalStore, parent_uuid: str, element_type: str) -> ChildRef:
    """Return the first Child_Element of ``element_type`` in ``parent_uuid``."""
    page = store.list_child_elements(
        parent_doc_uuid=parent_uuid, element_type=element_type, limit=1
    )
    if not page["items"]:
        msg = f"leak fixture has no {element_type!r} child in {parent_uuid}"
        raise AssertionError(msg)
    return ChildRef(page["items"][0]["id"], parent_uuid)


def _model_fixture(model_type: OSCALModelType) -> dict:
    """Return the root object of the model fixture for ``model_type``."""
    path = MODEL_FIXTURES_DIR / f"{model_type.value}.json"
    return json.loads(path.read_text(encoding="utf-8"))[model_type.value]


def _collect_leak_ids(store: OscalStore) -> dict:
    """Collect the IDs the Path_Leak_Test CALLS table needs from the fixture files."""
    doc_uuids = {mt: _model_fixture(mt)["uuid"] for mt in OSCALModelType}
    cdef = _model_fixture(OSCALModelType.COMPONENT_DEFINITION)
    children = {
        element_type: _first_child(store, doc_uuids[model_type], element_type)
        for model_type, element_types in CHILD_ELEMENT_TYPES.items()
        for element_type in element_types
    }
    return {
        "doc_uuids": doc_uuids,
        "bundled_cdef_uuids": (doc_uuids[OSCALModelType.COMPONENT_DEFINITION], LEAK_ZIP_CDEF_UUID),
        "user_cdef_uuids": tuple(
            load_fixture_cdef(name)["component-definition"]["uuid"] for name in VALID_CDEF_FILES
        ),
        "component_uuid": cdef["components"][0]["uuid"],
        "component_title": cdef["components"][0]["title"],
        "capability_uuid": cdef["capabilities"][0]["uuid"],
        "capability_name": cdef["capabilities"][0]["name"],
        "children": children,
        "bundled_overview_doc_id": doc_id(ORIGIN_BUNDLED, LEAK_OVERVIEW_REL),
        "user_overview_doc_id": doc_id(ORIGIN_USER, LEAK_OVERVIEW_REL),
        "user_guide_doc_id": doc_id(ORIGIN_USER, LEAK_USER_GUIDE_REL),
        "awesome_doc_id": doc_id(ORIGIN_BUNDLED, LEAK_AWESOME_REL),
    }


def build_leak_store(root: Path) -> LeakFixture:
    """Build the Path_Leak_Test Fixture_Store under ``root`` (Requirements 5.1, 2.3).

    Layout::

        <root>/bundled/    one model fixture per OSCALModelType, cdefs.zip, awesome-oscal.md,
                           concepts/overview.md                  -> origin "bundled"
        <root>/user-docs/  the three valid cdef fixtures, notes/user-guide.md,
                           concepts/overview.md                  -> origin "user"
        <root>/db/store.db

    Every OSCAL document is indexed (child elements and FTS rows) before returning. The
    caller must call ``fixture.store.close()``.
    """
    bundled = root / "bundled"
    user_docs = root / "user-docs"
    db_path = root / "db" / "store.db"
    _write_leak_bundled(bundled)
    _write_leak_user_docs(user_docs)
    db_path.parent.mkdir(parents=True)
    store = OscalStore(db_path=str(db_path), cache_size=10, seed_from_bundled=False)
    try:
        store.scan_directory(bundled, origin=ORIGIN_BUNDLED)
        store.scan_directory(user_docs, origin=ORIGIN_USER)
        # text_search indexes every pending OSCAL document first (child elements, FTS rows).
        store.text_search(LEAK_TEXT_QUERY)
        ids = _collect_leak_ids(store)
    except BaseException:
        # Close before re-raising so the caller's temp dir can be removed (Windows).
        store.close()
        raise
    return LeakFixture(
        root=root,
        db_path=db_path,
        bundled_dir=bundled,
        user_docs_dir=user_docs,
        store=store,
        valid_json_file=user_docs / SAMPLE_CDEF_FILE,
        **ids,
    )


def build_fixture_store(directory: Path, cdefs: Iterable[dict]) -> OscalStore:
    """Write each cdef dict as JSON under ``directory/docs`` and scan into a fresh store.

    Args:
        directory: Writable directory (e.g. ``tmp_path``) that holds the docs
            and the SQLite database.
        cdefs: Component Definition dicts, each wrapped in a top-level
            ``"component-definition"`` key.

    Returns:
        An ``OscalStore`` that does not seed from the bundled database. The
        caller is responsible for calling ``close()``.
    """
    docs = directory / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    for i, cdef in enumerate(cdefs):
        (docs / f"cdef_{i}.json").write_text(json.dumps(cdef), encoding="utf-8")
    store = OscalStore(
        db_path=str(directory / "store.db"),
        cache_size=10,
        seed_from_bundled=False,
    )
    try:
        store.scan_directory(docs)
    except BaseException:
        # Close before re-raising so the caller's temp dir can be removed
        # (Windows cannot delete an open SQLite file).
        store.close()
        raise
    return store


def load_fixture_cdef(filename: str) -> dict:
    """Load a JSON fixture from ``tests/fixtures`` as a dict."""
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


def load_sample_cdef() -> dict:
    """Return ``sample_component_definition.json``."""
    return load_fixture_cdef(SAMPLE_CDEF_FILE)


def load_multi_cdef() -> dict:
    """Return ``multi_component_definition.json``."""
    return load_fixture_cdef(MULTI_CDEF_FILE)


def load_capabilities_cdef() -> dict:
    """Return ``sample_component_definition_with_capabilities.json``."""
    return load_fixture_cdef(CAPABILITIES_CDEF_FILE)


def load_valid_fixture_cdefs() -> list[dict]:
    """Return the three valid Component Definition fixtures, in a stable order."""
    return [load_fixture_cdef(name) for name in VALID_CDEF_FILES]


def make_many_capabilities_cdef(n: int = 120) -> tuple[dict, dict]:
    """Build one model-valid Component Definition with ``n`` capabilities.

    Capability names are zero-padded (``"Capability 000"`` ... ``"Capability
    119"``), so name order matches list order. The target is the last
    capability, which sits at index ``n - 1``. With the default ``n=120`` that
    is beyond position 100, the old default page limit.

    Args:
        n: Number of capabilities to generate (must be >= 1).

    Returns:
        ``(cdef, target)`` where ``cdef`` is the wrapped Component Definition
        dict and ``target`` is the target capability dict (``uuid``, ``name``,
        ``description``).
    """
    if n < 1:
        msg = "n must be >= 1"
        raise ValueError(msg)
    width = max(3, len(str(n - 1)))
    capabilities = [
        {
            "uuid": str(uuid.uuid4()),
            "name": f"{MANY_CAPABILITIES_NAME_PREFIX}{i:0{width}d}",
            "description": f"Generated capability number {i}",
        }
        for i in range(n)
    ]
    cdef = {
        "component-definition": {
            "uuid": str(uuid.uuid4()),
            "metadata": {
                "title": "Many Capabilities Definition",
                "last-modified": "2024-01-01T00:00:00+00:00",
                "version": "1.0",
                "oscal-version": "1.0.4",
            },
            "components": [
                {
                    "uuid": str(uuid.uuid4()),
                    "type": "software",
                    "title": "Many Capabilities Component",
                    "description": "Component in the many-capabilities fixture",
                }
            ],
            "capabilities": capabilities,
        }
    }
    return cdef, capabilities[-1]
