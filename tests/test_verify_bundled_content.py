"""Unit tests for verify_bundled_content and the directory-aware integrity messages.

Validates: Requirements 2.3, 2.4
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from mcp_server_for_oscal.tools.utils import (
    BUNDLED_CONTENT_DIRS,
    INTEGRITY_ERRORS,
    manifest_bytes,
    verify_bundled_content,
    verify_package_integrity,
    write_manifest,
)


def _write_manifest(directory: Path) -> None:
    """Write a hashes.json listing every regular file in ``directory`` (not recursive)."""
    file_hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.iterdir())
        if p.is_file() and p.name != "hashes.json"
    }
    write_manifest(directory, {"commit": "test", "file_hashes": file_hashes})


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A package dir with valid oscal_schemas/ and oscal_metaschema/shared-constraints/."""
    schemas = tmp_path / "oscal_schemas"
    schemas.mkdir()
    (schemas / "oscal_catalog_schema.json").write_text('{"$id": "x"}', encoding="utf-8")
    _write_manifest(schemas)

    meta = tmp_path / "oscal_metaschema"
    shared = meta / "shared-constraints"
    shared.mkdir(parents=True)
    (meta / "oscal_catalog_metaschema.xml").write_text("<METASCHEMA/>", encoding="utf-8")
    (meta / "README.md").write_text("tag v1.2.1\n", encoding="utf-8")
    (shared / "allowed-values.ent").write_text("<!-- ent -->", encoding="utf-8")
    _write_manifest(meta)
    _write_manifest(shared)
    return tmp_path


def test_bundled_content_dirs():
    assert BUNDLED_CONTENT_DIRS == ("oscal_schemas", "oscal_metaschema")


def test_integrity_errors_cover_json_decode_error():
    assert issubclass(json.JSONDecodeError, INTEGRITY_ERRORS)
    assert issubclass(FileNotFoundError, INTEGRITY_ERRORS)


def test_passes_on_valid_bundle(bundle: Path):
    verify_bundled_content(bundle)


@pytest.mark.parametrize(
    "rel_path",
    [
        "oscal_schemas/oscal_catalog_schema.json",
        "oscal_metaschema/oscal_catalog_metaschema.xml",
        "oscal_metaschema/shared-constraints/allowed-values.ent",
    ],
)
def test_changed_byte_fails_and_names_directory(bundle: Path, rel_path: str):
    target = bundle / rel_path
    data = bytearray(target.read_bytes())
    data[0] ^= 0x01
    target.write_bytes(bytes(data))

    with pytest.raises(RuntimeError, match="has been modified") as exc:
        verify_bundled_content(bundle)
    assert target.name in str(exc.value)
    assert str(target.parent) in str(exc.value)


def test_missing_listed_file_fails_and_names_directory(bundle: Path):
    target = bundle / "oscal_metaschema" / "shared-constraints" / "allowed-values.ent"
    target.unlink()

    with pytest.raises(
        RuntimeError, match=re.escape("File allowed-values.ent missing from package.")
    ) as exc:
        verify_bundled_content(bundle)
    assert str(target.parent) in str(exc.value)


def test_unlisted_file_fails(bundle: Path, caplog: pytest.LogCaptureFixture):
    shared = bundle / "oscal_metaschema" / "shared-constraints"
    (shared / "extra.ent").write_text("unlisted", encoding="utf-8")

    with pytest.raises(KeyError):
        verify_bundled_content(bundle)
    assert any("extra.ent" in r.message and str(shared) in r.message for r in caplog.records)


@pytest.mark.parametrize("subdir", ["oscal_metaschema", "oscal_metaschema/shared-constraints"])
def test_missing_manifest_fails(bundle: Path, subdir: str):
    (bundle / subdir / "hashes.json").unlink()

    with pytest.raises(INTEGRITY_ERRORS):
        verify_bundled_content(bundle)


def test_invalid_json_manifest_fails(bundle: Path):
    (bundle / "oscal_metaschema" / "hashes.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(INTEGRITY_ERRORS):
        verify_bundled_content(bundle)


def test_missing_bundled_directory_fails(bundle: Path):
    meta = bundle / "oscal_metaschema"
    for p in sorted(meta.rglob("*"), reverse=True):
        p.rmdir() if p.is_dir() else p.unlink()
    meta.rmdir()

    with pytest.raises(OSError):
        verify_bundled_content(bundle)


def test_verify_package_integrity_still_passes_on_single_dir(bundle: Path):
    verify_package_integrity(bundle / "oscal_schemas")


# ---------------------------------------------------------------------------
# Canonical manifest form (Req 2.4: a manifest must match bin/update_hashes.py output)
# ---------------------------------------------------------------------------


def test_manifest_bytes_format():
    state = {"commit": "", "file_hashes": {"b.ent": "1", "a.ent": "0"}}
    # 2-space indent, key order kept, LF, no trailing newline
    assert manifest_bytes(state) == (
        b'{\n  "commit": "",\n  "file_hashes": {\n    "b.ent": "1",\n    "a.ent": "0"\n  }\n}'
    )


def test_update_hashes_writes_canonical_manifest(tmp_path: Path):
    from bin.update_hashes import capture_file_state

    (tmp_path / "a.ent").write_text("x", encoding="utf-8")
    state, output_file = capture_file_state(str(tmp_path), str(tmp_path))
    assert Path(output_file).read_bytes() == manifest_bytes(state)
    verify_package_integrity(tmp_path)


@pytest.mark.parametrize(
    "rewrite",
    [
        pytest.param(lambda b: b.replace(b"  ", b"\t", 1), id="space-to-tab"),
        pytest.param(lambda b: b + b"\n", id="trailing-newline"),
        pytest.param(lambda b: b.replace(b"\n", b"\r\n"), id="crlf"),
        pytest.param(lambda b: json.dumps(json.loads(b)).encode(), id="compact"),
    ],
)
def test_whitespace_only_manifest_change_fails_and_names_manifest(
    bundle: Path, rewrite, caplog: pytest.LogCaptureFixture
):
    manifest = bundle / "oscal_metaschema" / "shared-constraints" / "hashes.json"
    original = manifest.read_bytes()
    changed = rewrite(original)
    assert changed != original
    assert json.loads(changed) == json.loads(original)  # same content, different bytes
    manifest.write_bytes(changed)

    with pytest.raises(RuntimeError, match="not in canonical form") as exc:
        verify_bundled_content(bundle)
    assert "hashes.json" in str(exc.value)
    assert str(manifest.parent) in str(exc.value)
    assert any(str(manifest.parent) in r.message for r in caplog.records)


def test_manifest_with_unexpected_key_fails(bundle: Path):
    directory = bundle / "oscal_schemas"
    state = json.loads((directory / "hashes.json").read_bytes())
    write_manifest(directory, {**state, "extra": ""})

    with pytest.raises(RuntimeError, match=r"unexpected keys \['extra'\]") as exc:
        verify_bundled_content(bundle)
    assert str(directory) in str(exc.value)


def test_rewritten_canonical_manifest_passes(bundle: Path):
    directory = bundle / "oscal_metaschema"
    write_manifest(directory, json.loads((directory / "hashes.json").read_bytes()))
    verify_bundled_content(bundle)
