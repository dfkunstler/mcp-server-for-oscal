"""Helpers for building real ``OscalStore`` instances from Component Definitions.

Tests use these helpers instead of mocking ``OscalStore``: each helper writes
real OSCAL JSON to disk and ingests it with ``OscalStore.scan_directory``.
"""

import json
import uuid
from collections.abc import Iterable
from pathlib import Path

from mcp_server_for_oscal.tools.oscal_store import OscalStore

FIXTURES_DIR = Path(__file__).parent / "fixtures"

SAMPLE_CDEF_FILE = "sample_component_definition.json"
MULTI_CDEF_FILE = "multi_component_definition.json"
CAPABILITIES_CDEF_FILE = "sample_component_definition_with_capabilities.json"
VALID_CDEF_FILES = (SAMPLE_CDEF_FILE, MULTI_CDEF_FILE, CAPABILITIES_CDEF_FILE)

MANY_CAPABILITIES_NAME_PREFIX = "Capability "


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
