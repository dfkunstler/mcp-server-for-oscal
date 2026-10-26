"""Guard tests for the per-model fixtures in ``tests/fixtures/models``.

The Path_Leak_Test scans these files into a store and calls every child lister with a
real ID, so each file must be model-valid, yield every child element type of its model,
and hold no string that looks like a storage location.

Requirements: 5.1
"""

import json
import re
from collections.abc import Iterator

import pytest

from mcp_server_for_oscal.tools.oscal_store import CHILD_ELEMENT_TYPES, OscalStore
from mcp_server_for_oscal.tools.utils import MODEL_MAP, OSCALModelType

from .fixture_store import MODEL_FIXTURES_DIR

# Absolute POSIX, home-relative, UNC, or Windows drive paths; parent-directory segments.
_PATH_LIKE = re.compile(r"^(?:/|~|\\\\|[A-Za-z]:[\\/])|(?:^|[\\/])\.\.(?:[\\/]|$)")
_FORBIDDEN_SUBSTRINGS = ("file:", "s3://", "arn:aws")
_ALLOWED_HREF_PREFIXES = ("#", "https://example.com/")


def _load(model_type: OSCALModelType) -> dict:
    return json.loads((MODEL_FIXTURES_DIR / f"{model_type.value}.json").read_text(encoding="utf-8"))


def _walk(value: object, key: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(owning key, string)`` for every string value and key, recursively.

    A dict key is yielded with an empty owning key, so only values are href-checked.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            yield "", k
            yield from _walk(v, k)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, key)
    elif isinstance(value, str):
        yield key, value


def test_one_fixture_per_model_type() -> None:
    names = sorted(p.name for p in MODEL_FIXTURES_DIR.iterdir())
    assert names == sorted(f"{m.value}.json" for m in OSCALModelType)


@pytest.mark.parametrize("model_type", list(OSCALModelType), ids=lambda m: m.value)
def test_fixture_is_model_valid(model_type: OSCALModelType) -> None:
    doc = _load(model_type)
    assert list(doc) == [model_type.value]
    MODEL_MAP[model_type].model_validate(doc[model_type.value])


@pytest.mark.parametrize("model_type", list(OSCALModelType), ids=lambda m: m.value)
def test_fixture_has_no_paths(model_type: OSCALModelType) -> None:
    for key, text in _walk(_load(model_type)):
        assert not _PATH_LIKE.search(text), (key, text)
        lowered = text.casefold()
        for needle in _FORBIDDEN_SUBSTRINGS:
            assert needle not in lowered, (key, text)
        if key == "href":
            assert text.startswith(_ALLOWED_HREF_PREFIXES), (key, text)


def test_catalog_has_nested_groups_and_prose() -> None:
    catalog = _load(OSCALModelType.CATALOG)["catalog"]
    inner = [g for outer in catalog["groups"] for g in outer.get("groups", [])]
    assert inner, "catalog needs a group nested inside a group"
    part_names = {
        part["name"]
        for group in inner
        for control in group.get("controls", [])
        for part in control.get("parts", [])
        if part.get("prose")
    }
    assert {"statement", "guidance"} <= part_names


@pytest.fixture(scope="module")
def models_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[OscalStore]:
    directory = tmp_path_factory.mktemp("fixture-models")
    store = OscalStore(db_path=str(directory / "store.db"), cache_size=10, seed_from_bundled=False)
    try:
        store.scan_directory(MODEL_FIXTURES_DIR)
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("model_type", list(OSCALModelType), ids=lambda m: m.value)
def test_fixture_yields_every_child_type(
    models_store: OscalStore, model_type: OSCALModelType
) -> None:
    doc_uuid = _load(model_type)[model_type.value]["uuid"]
    page = models_store.list_child_elements(parent_doc_uuid=doc_uuid, limit=100)
    assert not page["hasMore"]
    found = {item["element_type"] for item in page["items"]}
    assert found == set(CHILD_ELEMENT_TYPES[model_type]), found
