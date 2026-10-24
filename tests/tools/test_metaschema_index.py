"""Tests for tools/metaschema_index.py."""

import builtins
import contextlib
import hashlib
import json
import logging
import socket
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from xml.parsers.expat import ExpatError

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools import metaschema_index as mi
from mcp_server_for_oscal.tools.metaschema_index import (
    METASCHEMA_NS,
    BundlePath,
    _Refused,
    model_module,
    read_schema_versions,
    resolve_bundle_path,
)
from mcp_server_for_oscal.tools.utils import OSCALModelType

LEAKED = "9.9.9"


def _module(body: str, doctype: str = "") -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n{doctype}\n'
        f'<METASCHEMA xmlns="{METASCHEMA_NS}">\n{body}\n</METASCHEMA>\n'
    ).encode()


@pytest.fixture
def no_io(monkeypatch):
    """Fail the test if anything opens a file or socket during the parse."""
    opened: list[object] = []

    def deny_open(file, *args, **kwargs):
        opened.append(file)
        raise AssertionError(f"unexpected open: {file!r}")

    def deny_socket(*args, **kwargs):
        opened.append("socket")
        raise AssertionError("unexpected socket")

    monkeypatch.setattr(builtins, "open", deny_open)
    monkeypatch.setattr(socket, "socket", deny_socket)
    return opened


class TestReadSchemaVersions:
    """Validates: Requirements 3.4, 3.6"""

    def test_single_version(self):
        assert read_schema_versions(_module("<schema-version>1.2.3</schema-version>")) == ["1.2.3"]

    def test_multiple_versions_in_document_order_and_stripped(self):
        body = (
            "<schema-version>\n  1.2.3\n</schema-version>"
            "<define-assembly name='x'><schema-version>1.0.0</schema-version></define-assembly>"
        )
        assert read_schema_versions(_module(body)) == ["1.2.3", "1.0.0"]

    def test_none(self):
        assert read_schema_versions(_module("<short-name>x</short-name>")) == []

    def test_prefixed_namespace(self):
        data = (
            f'<m:METASCHEMA xmlns:m="{METASCHEMA_NS}">'
            "<m:schema-version>1.2.3</m:schema-version></m:METASCHEMA>"
        ).encode()
        assert read_schema_versions(data) == ["1.2.3"]

    def test_unnamespaced_accepted(self):
        data = b"<METASCHEMA><schema-version>1.2.3</schema-version></METASCHEMA>"
        assert read_schema_versions(data) == ["1.2.3"]

    def test_other_namespace_ignored(self):
        body = '<schema-version xmlns="urn:other">0.0.1</schema-version>'
        assert read_schema_versions(_module(body)) == []

    def test_text_split_by_entity_and_cdata_is_joined(self):
        body = "<schema-version>1.<![CDATA[2]]>&#46;3</schema-version>"
        assert read_schema_versions(_module(body)) == ["1.2.3"]

    def test_malformed_raises(self):
        with pytest.raises(ExpatError):
            read_schema_versions(b"<METASCHEMA><schema-version>1.2.3</METASCHEMA>")

    def test_real_bundle_shape(self):
        """Mirrors the upstream module header: DOCTYPE with external entities."""
        doctype = (
            "<!DOCTYPE METASCHEMA [\n"
            "<!ENTITY allowed-values-component-type SYSTEM "
            '"./shared-constraints/allowed-values-component-type.ent">\n]>'
        )
        body = (
            "<schema-name>OSCAL Component Definition Model</schema-name>\n"
            "<schema-version>1.2.3</schema-version>\n"
            "<allowed-values>&allowed-values-component-type;</allowed-values>"
        )
        assert read_schema_versions(_module(body, doctype)) == ["1.2.3"]

    def test_external_entities_are_not_opened(self, tmp_path: Path, no_io):
        ent = tmp_path / "leak.ent"
        ent.write_text(f"<schema-version>{LEAKED}</schema-version>", encoding="utf-8")
        doctype = (
            "<!DOCTYPE METASCHEMA [\n"
            f'<!ENTITY rel SYSTEM "leak.ent">\n'
            f'<!ENTITY abs SYSTEM "{ent.as_posix()}">\n'
            f'<!ENTITY url SYSTEM "{ent.as_uri()}">\n'
            '<!ENTITY net SYSTEM "http://example.invalid/x.ent">\n'
            "]>"
        )
        body = "<schema-version>1.2.3</schema-version>\n&rel;&abs;&url;&net;"
        assert read_schema_versions(_module(body, doctype)) == ["1.2.3"]
        assert no_io == []

    def test_external_dtd_is_not_opened(self, tmp_path: Path, no_io):
        dtd = tmp_path / "leak.dtd"
        dtd.write_text(f'<!ENTITY v "{LEAKED}">', encoding="utf-8")
        doctype = f'<!DOCTYPE METASCHEMA SYSTEM "{dtd.as_uri()}">'
        versions = read_schema_versions(_module("<schema-version>&v;</schema-version>", doctype))
        assert LEAKED not in "".join(versions)
        assert no_io == []


class TestModelModule:
    """Validates: Requirements 4.4 (module per model, derived from utils.schema_names)"""

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            (OSCALModelType.CATALOG, "oscal_catalog_metaschema.xml"),
            (OSCALModelType.PROFILE, "oscal_profile_metaschema.xml"),
            (OSCALModelType.COMPONENT_DEFINITION, "oscal_component_metaschema.xml"),
            (OSCALModelType.SYSTEM_SECURITY_PLAN, "oscal_ssp_metaschema.xml"),
            (OSCALModelType.ASSESSMENT_PLAN, "oscal_assessment-plan_metaschema.xml"),
            (OSCALModelType.ASSESSMENT_RESULTS, "oscal_assessment-results_metaschema.xml"),
            (OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES, "oscal_poam_metaschema.xml"),
            (OSCALModelType.MAPPING, "oscal_mapping_metaschema.xml"),
        ],
    )
    def test_model_module(self, model, expected):
        assert model_module(model) == expected

    def test_covers_every_model_type(self):
        assert len({model_module(m) for m in OSCALModelType}) == len(OSCALModelType)


class TestConstants:
    def test_limits_and_types(self):
        assert mi.MAX_FILE_BYTES == 2 * 1024 * 1024
        assert mi.MAX_DEPTH == 64
        assert mi.CONSTRAINT_TYPES == (
            "allowed-values",
            "matches",
            "index",
            "index-has-key",
            "is-unique",
            "has-cardinality",
            "expect",
        )
        assert mi.BUNDLE_DIR.name == "oscal_metaschema"
        assert mi.BUNDLE_DIR.parent.name == "mcp_server_for_oscal"

    def test_load_error_is_value_error(self):
        assert issubclass(mi.MetaschemaLoadError, ValueError)

    def test_records_are_frozen(self):
        loc = mi.SourceLocation("oscal_x_metaschema.xml", 3)
        with pytest.raises(AttributeError):
            loc.line = 4  # type: ignore[misc]
        err = mi.LoadError("oscal_x_metaschema.xml", "ent", "referenced file does not exist")
        with pytest.raises(AttributeError):
            err.reason = "x"  # type: ignore[misc]


@pytest.fixture
def bundle(tmp_path: Path) -> tuple[Path, frozenset[str]]:
    """A tiny bundle under ``tmp_path/b`` and a file outside it; returns (root, allowlist)."""
    root = tmp_path / "b"
    (root / "shared-constraints").mkdir(parents=True)
    (root / "oscal_a_metaschema.xml").write_text("<METASCHEMA/>", encoding="utf-8")
    (root / "shared-constraints" / "a.ent").write_text("<enum value='x'/>", encoding="utf-8")
    (root / "shared-constraints" / "unlisted.ent").write_text("", encoding="utf-8")
    (tmp_path / "outside.ent").write_text(LEAKED, encoding="utf-8")
    allowlist = frozenset(
        {"oscal_a_metaschema.xml", "shared-constraints/a.ent", "shared-constraints/gone.ent"}
    )
    return root, allowlist


@pytest.fixture
def guarded_bundle(bundle, no_io):
    """``bundle``, with ``open`` and sockets denied from here on."""
    return bundle


class TestResolveBundlePath:
    """Validates: Requirements 4.2, 4.6, 4.11"""

    @pytest.mark.parametrize(
        ("base", "ref", "rel"),
        [
            ("", "./shared-constraints/a.ent", "shared-constraints/a.ent"),
            ("", "shared-constraints/a.ent", "shared-constraints/a.ent"),
            ("", "oscal_a_metaschema.xml", "oscal_a_metaschema.xml"),
            ("shared-constraints", "../oscal_a_metaschema.xml", "oscal_a_metaschema.xml"),
            ("shared-constraints", "a.ent", "shared-constraints/a.ent"),
        ],
    )
    def test_accepts_listed_file(self, guarded_bundle, base, ref, rel):
        root, allowlist = guarded_bundle
        got = resolve_bundle_path(root / base, ref, root, allowlist)
        assert got == BundlePath((root / rel).resolve(), rel)
        assert got.path.is_file()

    @pytest.mark.parametrize(
        ("ref", "reason"),
        [
            ("http://example.invalid/x.ent", mi.REFUSED_SCHEME),
            ("file:///etc/passwd", mi.REFUSED_SCHEME),
            ("C:/Windows/win.ini", mi.REFUSED_SCHEME),
            ("c:x.ent", mi.REFUSED_SCHEME),
            ("/etc/passwd", mi.REFUSED_ABSOLUTE),
            ("\\Windows\\win.ini", mi.REFUSED_ABSOLUTE),
            ("\\\\server\\share\\x.ent", mi.REFUSED_ABSOLUTE),
            ("//server/share/x.ent", mi.REFUSED_ABSOLUTE),
            ("shared-constraints/a.ent\0", mi.REFUSED_NUL),
            ("../outside.ent", mi.REFUSED_OUTSIDE),
            ("shared-constraints/../../outside.ent", mi.REFUSED_OUTSIDE),
            ("shared-constraints/unlisted.ent", mi.REFUSED_UNLISTED),
            ("shared-constraints/gone.ent", mi.REFUSED_MISSING),
            ("shared-constraints/never.ent", mi.REFUSED_MISSING),
            ("shared-constraints", mi.REFUSED_MISSING),
        ],
    )
    def test_refuses(self, guarded_bundle, ref, reason):
        root, allowlist = guarded_bundle
        with pytest.raises(_Refused) as exc:
            resolve_bundle_path(root, ref, root, allowlist)
        assert exc.value.reason == reason
        assert str(exc.value) == reason

    def test_refuses_symlink_out_of_bundle(self, bundle, tmp_path: Path, no_io):
        root, allowlist = bundle
        link = root / "shared-constraints" / "a.ent"
        link.unlink()
        try:
            link.symlink_to(tmp_path / "outside.ent")
        except OSError:  # pragma: no cover - Windows without symlink privilege
            pytest.skip("symlinks not supported")
        with pytest.raises(_Refused) as exc:
            resolve_bundle_path(root, "shared-constraints/a.ent", root, allowlist)
        assert exc.value.reason == mi.REFUSED_OUTSIDE

    def test_root_is_resolved(self, bundle, tmp_path: Path):
        """A root given through a symlink still contains its own files."""
        root, allowlist = bundle
        alias = tmp_path / "alias"
        try:
            alias.symlink_to(root, target_is_directory=True)
        except OSError:  # pragma: no cover - Windows without symlink privilege
            pytest.skip("symlinks not supported")
        got = resolve_bundle_path(alias, "shared-constraints/a.ent", alias, allowlist)
        assert got.rel == "shared-constraints/a.ent"

    def test_reasons_are_client_safe(self, bundle, tmp_path: Path):
        root, allowlist = bundle
        for ref in ("../outside.ent", "shared-constraints/gone.ent", "/etc/passwd"):
            with pytest.raises(_Refused) as exc:
                resolve_bundle_path(root, ref, root, allowlist)
            assert str(tmp_path) not in exc.value.reason
            assert ref not in exc.value.reason


# --------------------------------------------------------------------------------------
# Safe loader
# --------------------------------------------------------------------------------------

MOD = "oscal_t_metaschema.xml"
ENT = "shared-constraints/x.ent"


def _write_bundle(
    root: Path, files: dict[str, str | bytes], unlisted: frozenset[str] = frozenset()
) -> frozenset[str]:
    """Write ``files`` and both manifests (update_hashes.py format); return the allowlist."""
    (root / mi.SHARED_DIR).mkdir(parents=True, exist_ok=True)
    manifests: dict[str, dict[str, str]] = {"": {}, f"{mi.SHARED_DIR}/": {}}
    for rel, content in files.items():
        data = content.encode() if isinstance(content, str) else content
        (root / rel).write_bytes(data)
        if rel in unlisted:
            continue
        prefix, _, name = rel.rpartition("/")
        manifests[f"{prefix}/" if prefix else ""][name] = hashlib.sha256(data).hexdigest()
    for prefix, hashes in manifests.items():
        manifest = {"commit": "'0000000'", "file_hashes": hashes}
        (root / f"{prefix}{mi.MANIFEST}").write_text(json.dumps(manifest), encoding="utf-8")
    return mi._read_allowlist(root)


def _doctype(*decls: str) -> str:
    return "<!DOCTYPE METASCHEMA [\n" + "\n".join(decls) + "\n]>"


def _ent_decl(name: str = "x", system: str = "./shared-constraints/x.ent") -> str:
    return f'<!ENTITY {name} SYSTEM "{system}">'


def _ns_ent(body: str) -> str:
    """Entity file content: elements carry the namespace, as upstream .ent files do."""
    return body.replace("<enum", f'<enum xmlns="{METASCHEMA_NS}"')


def _walk(node: mi._Node):
    yield node
    for child in node.children:
        if isinstance(child, mi._Node):
            yield from _walk(child)


def _find(tree: mi._Node, tag: str) -> list[mi._Node]:
    return [n for n in _walk(tree) if n.tag == tag]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "bundle"
    path.mkdir()
    (tmp_path / "outside.ent").write_text(f"<enum value='{LEAKED}'/>", encoding="utf-8")
    return path


@pytest.fixture
def opened(monkeypatch) -> list[Path]:
    """Record every file the loader opens (and still open it); deny sockets."""
    paths: list[Path] = []
    real_open = builtins.open

    def spy_open(file, *args, **kwargs):
        paths.append(Path(file).resolve())
        return real_open(file, *args, **kwargs)

    def deny_socket(*args, **kwargs):
        raise AssertionError("unexpected socket")

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(socket, "socket", deny_socket)
    return paths


def _assert_only_allowlisted(opened: list[Path], root: Path, allowlist: frozenset[str]) -> None:
    rels = {p.relative_to(root.resolve()).as_posix() for p in opened}
    assert rels <= allowlist


def _load(root: Path, allowlist: frozenset[str], module: str = MOD) -> mi._Node | mi.LoadError:
    return mi._load_module(root, module, allowlist)


def _error(result: mi._Node | mi.LoadError) -> mi.LoadError:
    assert isinstance(result, mi.LoadError), result
    return result


def _tree(result: mi._Node | mi.LoadError) -> mi._Node:
    assert isinstance(result, mi._Node), result
    return result


class TestReadAllowlist:
    """Validates: Requirements 4.2 (Manifest_Allowlist, RC-5)"""

    def test_union_of_both_manifests(self, root: Path):
        allowlist = _write_bundle(
            root, {MOD: _module(""), ENT: "", "README.md": "", "shared-constraints/y.ent": ""}
        )
        assert allowlist == {MOD, ENT, "README.md", "shared-constraints/y.ent"}
        assert mi._module_files(allowlist) == (MOD,)

    def test_module_files_filter_and_sort(self):
        allowlist = {"oscal_b_metaschema.xml", "oscal_a_metaschema.xml", "README.md", ENT}
        assert mi._module_files(allowlist) == ("oscal_a_metaschema.xml", "oscal_b_metaschema.xml")

    def test_missing_sub_manifest(self, root: Path):
        _write_bundle(root, {MOD: _module("")})
        (root / mi.SHARED_DIR / mi.MANIFEST).unlink()
        with pytest.raises(_Refused) as exc:
            mi._read_allowlist(root)
        assert exc.value.subject == "shared-constraints/hashes.json"
        assert str(root) not in exc.value.reason

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            "[]",
            '{"commit": "x"}',
            '{"file_hashes": []}',
            '{"file_hashes": {"../a": 1}}',
        ],
    )
    def test_malformed_manifest(self, root: Path, content: str):
        _write_bundle(root, {MOD: _module("")})
        (root / mi.MANIFEST).write_text(content, encoding="utf-8")
        with pytest.raises(_Refused) as exc:
            mi._read_allowlist(root)
        assert (exc.value.subject, exc.value.reason) == ("hashes.json", mi.REFUSED_BAD_MANIFEST)


class TestLoadModule:
    """Validates: Requirements 4.1, 4.3, 4.5, 4.9"""

    def test_entity_expanded_in_place_with_per_file_lines(self, root: Path, opened):
        module = _module(
            "<allowed-values>\n&x;\n<enum value='z'/>\n</allowed-values>", _doctype(_ent_decl())
        )
        ent = _ns_ent("<!-- c -->\n<enum value='a'>A <code>c</code></enum>\n<enum value='b'/>")
        allowlist = _write_bundle(root, {MOD: module, ENT: ent})
        opened.clear()
        tree = _tree(_load(root, allowlist))

        assert tree.tag == "METASCHEMA"
        assert (tree.source_file, tree.line) == (MOD, 5)
        enums = _find(tree, "enum")
        assert [(e.attrs["value"], e.source_file, e.line) for e in enums] == [
            ("a", ENT, 2),
            ("b", ENT, 3),
            ("z", MOD, 8),
        ]
        a = enums[0]
        assert a.children[0] == "A "
        code = a.children[1]
        assert isinstance(code, mi._Node)
        assert (code.tag, code.children, code.source_file, code.line) == ("code", ["c"], ENT, 2)
        _assert_only_allowlisted(opened, root, allowlist)

    def test_foreign_namespace_skipped_and_ns_attrs_dropped(self, root: Path):
        body = (
            '<define-flag name="f" xml:lang="en"><x:a xmlns:x="urn:x"><b/>t</x:a>'
            "<formal-name>F</formal-name></define-flag>"
        )
        allowlist = _write_bundle(root, {MOD: _module(body)})
        flag = _find(_tree(_load(root, allowlist)), "define-flag")[0]
        assert dict(flag.attrs) == {"name": "f"}
        assert [c.tag for c in flag.children if isinstance(c, mi._Node)] == ["formal-name"]
        assert _find(flag, "b") == []

    def test_internal_entity_without_reference_is_expanded(self, root: Path):
        module = _module("<remarks>&i;</remarks>", _doctype('<!ENTITY i "inline">'))
        allowlist = _write_bundle(root, {MOD: module})
        remarks = _find(_tree(_load(root, allowlist)), "remarks")[0]
        assert remarks.children == ["inline"]

    def test_external_ref_from_entity_file_refused(self, root: Path, opened):
        module = _module(
            "<allowed-values>&x;</allowed-values>",
            _doctype(_ent_decl(), _ent_decl("y", "./shared-constraints/y.ent")),
        )
        files: dict[str, str | bytes] = {
            MOD: module,
            ENT: _ns_ent("<enum value='a'/>&y;"),
            "shared-constraints/y.ent": "",
        }
        allowlist = _write_bundle(root, files)
        err = _error(_load(root, allowlist))
        assert err == mi.LoadError(
            MOD, 'y (SYSTEM "./shared-constraints/y.ent")', mi.REFUSED_NESTED_EXTERNAL
        )
        assert (root / "shared-constraints/y.ent").resolve() not in opened

    @pytest.mark.parametrize(
        ("decl", "subject", "reason"),
        [
            ('<!ENTITY % pe "x">', "pe", mi.REFUSED_PARAM_ENTITY),
            ('<!ENTITY a "&b;"><!ENTITY b "x">', "a", mi.REFUSED_ENTITY_IN_ENTITY),
            ('<!ENTITY a "&#38;b;"><!ENTITY b "x">', "a", mi.REFUSED_ENTITY_IN_ENTITY),
            ('<!ENTITY lol "lol"><!ENTITY lol1 "&lol;&lol;">', "lol1", mi.REFUSED_ENTITY_IN_ENTITY),
            (
                '<!NOTATION n SYSTEM "n"><!ENTITY u SYSTEM "u" NDATA n>',
                "u",
                mi.REFUSED_UNPARSED_ENTITY,
            ),
        ],
    )
    def test_declarations_refused(self, root: Path, decl, subject, reason):
        allowlist = _write_bundle(root, {MOD: _module("<remarks/>", _doctype(decl))})
        assert _error(_load(root, allowlist)) == mi.LoadError(MOD, subject, reason)

    def test_undeclared_entity_with_external_dtd_refused_and_not_opened(self, root: Path, opened):
        doctype = '<!DOCTYPE METASCHEMA SYSTEM "x.dtd">'
        module = _module("<remarks>&nope;</remarks>", doctype)
        allowlist = _write_bundle(root, {MOD: module, "x.dtd": '<!ENTITY nope "x">'})
        assert _error(_load(root, allowlist)) == mi.LoadError(MOD, "nope", mi.REFUSED_UNDECLARED)
        assert (root / "x.dtd").resolve() not in opened

    def test_not_metaschema_root(self, root: Path):
        allowlist = _write_bundle(root, {MOD: '<METASCHEMA xmlns="urn:other"/>'})
        assert _error(_load(root, allowlist)) == mi.LoadError(MOD, MOD, mi.REFUSED_NOT_METASCHEMA)

    def test_unlisted_module_refused(self, root: Path, opened):
        allowlist = _write_bundle(root, {MOD: _module("")}, unlisted=frozenset({MOD}))
        opened.clear()
        assert _error(_load(root, allowlist)) == mi.LoadError(MOD, MOD, mi.REFUSED_UNLISTED)
        assert opened == []


class TestLoadModuleRefusedReferences:
    """Validates: Requirements 4.2, 4.6, 4.11"""

    @pytest.mark.parametrize(
        ("system", "reason"),
        [
            ("http://example.invalid/x.ent", mi.REFUSED_SCHEME),
            ("file:///etc/passwd", mi.REFUSED_SCHEME),
            ("/etc/passwd", mi.REFUSED_ABSOLUTE),
            ("../outside.ent", mi.REFUSED_OUTSIDE),
            ("./shared-constraints/unlisted.ent", mi.REFUSED_UNLISTED),
            ("./shared-constraints/gone.ent", mi.REFUSED_MISSING),
        ],
    )
    def test_refused_without_opening(self, root: Path, opened, system, reason):
        module = _module("<allowed-values>&x;</allowed-values>", _doctype(_ent_decl("x", system)))
        files: dict[str, str | bytes] = {
            MOD: module,
            "shared-constraints/unlisted.ent": _ns_ent("<enum value='u'/>"),
        }
        allowlist = _write_bundle(
            root, files, unlisted=frozenset({"shared-constraints/unlisted.ent"})
        )
        opened.clear()
        err = _error(_load(root, allowlist))
        assert err == mi.LoadError(MOD, f'x (SYSTEM "{system}")', reason)
        assert opened == [(root / MOD).resolve()]


class TestLoadModuleLimits:
    """Validates: Requirements 4.10 (RC-10), 4.11"""

    def test_module_too_large(self, root: Path, monkeypatch):
        allowlist = _write_bundle(root, {MOD: _module("<remarks>" + "x" * 300 + "</remarks>")})
        monkeypatch.setattr(mi, "MAX_FILE_BYTES", 200)
        err = _error(_load(root, allowlist))
        assert err == mi.LoadError(MOD, MOD, "file is larger than the limit of 200 bytes")

    def test_entity_too_large(self, root: Path, monkeypatch):
        module = _module("<allowed-values>&x;</allowed-values>", _doctype(_ent_decl()))
        ent = _ns_ent("<enum value='a'/>" * 40)
        allowlist = _write_bundle(root, {MOD: module, ENT: ent})
        monkeypatch.setattr(mi, "MAX_FILE_BYTES", len(module))
        err = _error(_load(root, allowlist))
        assert err == mi.LoadError(
            MOD, ENT, f"file is larger than the limit of {len(module)} bytes"
        )

    def test_exactly_at_size_limit_loads(self, root: Path, monkeypatch):
        module = _module("")
        allowlist = _write_bundle(root, {MOD: module})
        monkeypatch.setattr(mi, "MAX_FILE_BYTES", len(module))
        assert isinstance(_load(root, allowlist), mi._Node)

    def test_depth_at_limit_loads_and_entity_counts_at_reference_point(
        self, root: Path, monkeypatch
    ):
        # METASCHEMA(1) > allowed-values(2) > enum(3) > code(4)
        module = _module("<allowed-values>&x;</allowed-values>", _doctype(_ent_decl()))
        allowlist = _write_bundle(
            root, {MOD: module, ENT: _ns_ent("<enum value='a'><code/></enum>")}
        )
        monkeypatch.setattr(mi, "MAX_DEPTH", 4)
        assert isinstance(_load(root, allowlist), mi._Node)
        monkeypatch.setattr(mi, "MAX_DEPTH", 3)
        err = _error(_load(root, allowlist))
        assert err == mi.LoadError(MOD, ENT, "element nesting exceeds the limit of 3")

    def test_depth_exceeded_in_module(self, root: Path, monkeypatch):
        allowlist = _write_bundle(root, {MOD: _module("<a><b><c/></b></a>")})
        monkeypatch.setattr(mi, "MAX_DEPTH", 3)
        assert _error(_load(root, allowlist)) == mi.LoadError(
            MOD, MOD, "element nesting exceeds the limit of 3"
        )


class TestLoadModuleErrors:
    """Validates: Requirements 4.11 (RC-13)"""

    def test_malformed_module(self, root: Path):
        allowlist = _write_bundle(root, {MOD: _module("<remarks></define-flag>")})
        err = _error(_load(root, allowlist))
        assert (err.module, err.subject) == (MOD, MOD)
        assert err.reason.startswith("mismatched tag: line 4, column ")

    def test_malformed_entity_file(self, root: Path):
        module = _module("<allowed-values>&x;</allowed-values>", _doctype(_ent_decl()))
        allowlist = _write_bundle(root, {MOD: module, ENT: "\n<enum value='a'>"})
        err = _error(_load(root, allowlist))
        assert (err.module, err.subject) == (MOD, ENT)
        assert ": line 2, column " in err.reason

    def test_os_error_uses_strerror_and_logs_original(self, root: Path, monkeypatch, caplog):
        allowlist = _write_bundle(root, {MOD: _module("")})
        target = (root / MOD).resolve()
        real_open = builtins.open

        def failing_open(file, *args, **kwargs):
            if Path(file).resolve() == target:
                raise PermissionError(13, "Permission denied", str(file))
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", failing_open)
        with caplog.at_level(logging.WARNING, logger=mi.__name__):
            err = _error(_load(root, allowlist))
        assert err == mi.LoadError(MOD, MOD, "Permission denied")
        record = caplog.records[-1]
        assert record.exc_info is not None
        assert str(target) in "".join(traceback.format_exception(*record.exc_info))

    def test_load_errors_are_client_safe(self, root: Path, tmp_path: Path, monkeypatch):
        module = _module(
            "<allowed-values>&a;&b;</allowed-values>",
            _doctype(_ent_decl("a", "../outside.ent"), _ent_decl("b")),
        )
        allowlist = _write_bundle(root, {MOD: module, ENT: "<bad"})
        errors = [_error(_load(root, allowlist))]
        # Second case: the entity file fails to parse.
        module2 = _module("<allowed-values>&b;</allowed-values>", _doctype(_ent_decl("b")))
        allowlist = _write_bundle(root, {MOD: module2, ENT: "<bad"})
        errors.append(_error(_load(root, allowlist)))
        monkeypatch.setattr(mi, "MAX_DEPTH", 1)
        errors.append(_error(_load(root, allowlist)))
        for err in errors:
            for text in (err.module, err.subject, err.reason):
                assert str(tmp_path) not in text
                assert "Error(" not in text
                assert "Traceback" not in text


@pytest.mark.skipif(not (mi.BUNDLE_DIR / mi.MANIFEST).is_file(), reason="bundle not populated")
class TestRealBundle:
    """Validates: Requirements 4.1, 4.5 against the bundled Metaschema sources."""

    def test_every_module_loads(self, opened):
        allowlist = mi._read_allowlist(mi.BUNDLE_DIR)
        modules = mi._module_files(allowlist)
        assert modules
        results = {m: mi._load_module(mi.BUNDLE_DIR, m, allowlist) for m in modules}
        assert {m: r for m, r in results.items() if isinstance(r, mi.LoadError)} == {}
        _assert_only_allowlisted(
            [p for p in opened if p.name != mi.MANIFEST], mi.BUNDLE_DIR, allowlist
        )

    def test_provided_by_located_in_entity_file(self):
        allowlist = mi._read_allowlist(mi.BUNDLE_DIR)
        tree = _tree(mi._load_module(mi.BUNDLE_DIR, "oscal_component_metaschema.xml", allowlist))
        hits = [e for e in _find(tree, "enum") if e.attrs.get("value") == "provided-by"]
        assert [(e.source_file, e.line) for e in hits] == [
            ("shared-constraints/allowed-values-component_component_service.ent", 1)
        ]


# --------------------------------------------------------------------------------------
# Extraction, import graph, ModelScope, MetaschemaIndex
# --------------------------------------------------------------------------------------

CAT = model_module(OSCALModelType.CATALOG)
PROF = model_module(OSCALModelType.PROFILE)

EXTRACT_BODY = """\
<define-assembly name="a">
  <formal-name>  The
    A </formal-name>
  <description>Desc <code>a</code></description>
  <define-flag name="id" required="yes"/>
  <flag ref="t"><use-name>type</use-name></flag>
  <flag ref="plain"/>
  <model>
    <define-field name="f" min-occurs="1" max-occurs="unbounded">
      <group-as name="fs"/>
      <define-flag name="g"/>
    </define-field>
    <assembly ref="b" max-occurs="unbounded"><group-as name="bs"/></assembly>
    <choice>
      <field ref="c"><use-name>see</use-name></field>
    </choice>
  </model>
  <constraint>
    <let var="v" expression="."/>
    <matches id="m1" target="@id" datatype="token" level="WARNING"/>
    <index name="idx" target="b|f"><key-field target="@id"/><key-field target="@x"/></index>
    <expect test="count(b) gt 0"><message>Need <q>b</q></message></expect>
  </constraint>
  <remarks><p>R</p></remarks>
</define-assembly>
<define-flag name="t">
  <constraint>
    <allowed-values allow-other="yes">
      <enum value="x">X <em>x</em></enum>
      <enum value="y" deprecated="1.1.0"/>
    </allowed-values>
  </constraint>
</define-flag>
<define-field name="f"/>"""


def _index(root: Path, files: dict[str, str | bytes]) -> mi.MetaschemaIndex:
    _write_bundle(root, files)
    return mi.MetaschemaIndex.load(root)


def _defs(scope: mi.ModelScope) -> list[mi.Definition]:
    return [d for n in scope.definition_names() for d in scope.definitions(n)]


class TestExtraction:
    """Validates: Requirements 4.5, 5.6"""

    @pytest.fixture
    def scope(self, root: Path) -> mi.ModelScope:
        index = _index(root, {CAT: _module(EXTRACT_BODY)})
        assert dict(index.load_errors) == {}
        return index.scope(OSCALModelType.CATALOG)

    def test_global_and_local_definitions_with_parent(self, scope: mi.ModelScope):
        assert scope.modules == (CAT,)
        assert scope.definition_names() == ("a", "f", "g", "id", "t")
        (a,) = scope.definitions("a")
        assert (a.scope, a.parent, a.key.kind, a.key.location) == (
            "global",
            None,
            "assembly",
            mi.SourceLocation(CAT, 4),
        )
        assert a.formal_name == "The A"
        assert a.description is not None
        assert a.description.node.tag == "description"
        assert a.remarks is not None
        assert a.remarks.node.tag == "remarks"
        local_f, global_f = scope.definitions("f")
        assert (local_f.scope, local_f.parent, local_f.group_as) == ("local", a.key, "fs")
        assert (global_f.scope, global_f.parent) == ("global", None)
        (g,) = scope.definitions("g")
        assert (g.scope, g.parent) == ("local", local_f.key)
        (flag_id,) = scope.definitions("id")
        assert a.nested == (flag_id.key, local_f.key)
        assert local_f.nested == (g.key,)
        assert scope.global_definition("field", "f") == (global_f,)
        assert scope.global_definition("flag", "g") == ()
        assert scope.definitions("missing") == ()

    def test_flag_slots(self, scope: mi.ModelScope):
        (a,) = scope.definitions("a")
        (flag_id,) = scope.definitions("id")
        assert a.flags == (
            mi.FlagSlot("id", flag_id.key, None, required=True),
            mi.FlagSlot("type", None, "t", required=False),
            mi.FlagSlot("plain", None, "plain", required=False),
        )

    def test_model_children(self, scope: mi.ModelScope):
        (a,) = scope.definitions("a")
        local_f = scope.definitions("f")[0]
        assert [
            (c.kind, c.effective_name, c.ref, c.inline, c.min_occurs, c.max_occurs, c.group_as)
            for c in a.model_children
        ] == [
            ("field", "f", None, local_f.key, 1, "unbounded", "fs"),
            ("assembly", "b", "b", None, 0, "unbounded", "bs"),
            ("field", "see", "c", None, 0, "1", None),
        ]
        assert [c.in_choice for c in a.model_children] == [False, False, True]

    def test_constraints(self, scope: mi.ModelScope):
        (a,) = scope.definitions("a")
        matches, index, expect = a.constraints
        assert (matches.type, matches.id, matches.level, matches.target) == (
            "matches",
            "m1",
            "WARNING",
            "@id",
        )
        assert dict(matches.attrs) == {"datatype": "token"}
        assert [alt.path for alt in matches.alternatives] == ["@id"]
        assert matches.definition_key == a.key
        assert matches.location == mi.SourceLocation(CAT, 23)
        assert (index.attrs["name"], index.key_fields) == ("idx", ("@id", "@x"))
        assert [alt.path for alt in index.alternatives] == ["b", "f"]
        assert (expect.level, expect.target, dict(expect.attrs)) == (
            "ERROR",
            ".",
            {"test": "count(b) gt 0"},
        )
        assert expect.message is not None
        assert expect.message.node.tag == "message"
        assert matches.message is None
        assert matches.enums == index.enums == ()

    def test_allowed_values_without_target(self, scope: mi.ModelScope):
        (t,) = scope.global_definition("flag", "t")
        (av,) = t.constraints
        assert (av.type, av.target, av.id, dict(av.attrs)) == (
            "allowed-values",
            ".",
            None,
            {"allow-other": "yes"},
        )
        assert [alt.path for alt in av.alternatives] == [""]
        x, y = av.enums
        assert (x.value, x.deprecated, x.location) == ("x", None, mi.SourceLocation(CAT, 32))
        assert x.description is not None
        assert x.description.node.attrs["value"] == "x"
        assert (y.value, y.deprecated, y.description) == ("y", "1.1.0", None)

    def test_enums_from_entity_in_document_order_with_per_file_location(self, root: Path):
        body = (
            '<define-flag name="t"><constraint>\n'
            '<allowed-values target="."><enum value="first"/>\n&x;\n'
            '<enum value="last"/></allowed-values>\n'
            "</constraint></define-flag>"
        )
        ent = _ns_ent("\n\n<enum value='e1'>E</enum>\n<enum value='e2'/>")
        index = _index(root, {CAT: _module(body, _doctype(_ent_decl())), ENT: ent})
        (t,) = index.scope(OSCALModelType.CATALOG).definitions("t")
        (av,) = t.constraints
        assert [(e.value, e.location.source_file, e.location.line) for e in av.enums] == [
            ("first", CAT, 7),
            ("e1", ENT, 3),
            ("e2", ENT, 4),
            ("last", CAT, 9),
        ]
        assert av.location == mi.SourceLocation(CAT, 7)

    def test_bad_min_occurs_is_a_load_error(self, root: Path):
        body = '<define-assembly name="a"><model><field ref="f" min-occurs="-1"/></model>'
        index = _index(root, {CAT: _module(body + "</define-assembly>")})
        assert index.load_errors[CAT] == (
            mi.LoadError(CAT, f"{CAT} line 4", mi.REFUSED_BAD_OCCURS),
        )


def _imports(*hrefs: str) -> str:
    return "".join(f'<import href="{h}"/>' for h in hrefs)


def _mod(name: str) -> str:
    return f"oscal_{name}_metaschema.xml"


class TestImportGraph:
    """Validates: Requirements 4.4, 4.7"""

    def test_cycle_diamond_and_self_import_each_module_once(self, root: Path):
        x, y, z = _mod("x"), _mod("y"), _mod("z")
        files: dict[str, str | bytes] = {
            CAT: _module(_imports(x, y, x) + '<define-assembly name="cat"/>'),
            x: _module(_imports(z) + '<define-field name="shared"/>'),
            y: _module(_imports(z, y) + '<define-field name="shared"/>'),
            z: _module(_imports(CAT, x) + '<define-flag name="zf"/>'),
            PROF: _module(_imports(y)),
        }
        index = _index(root, files)
        assert dict(index.load_errors) == {}
        cat = index.scope(OSCALModelType.CATALOG)
        assert cat.modules == (CAT, x, y, z)
        assert [d.key.module for d in cat.definitions("shared")] == [x, y]
        assert cat.definition_names() == ("cat", "shared", "zf")
        assert index.scope(OSCALModelType.PROFILE).modules == (PROF, y, z, CAT, x)
        assert index.all_modules().modules == tuple(sorted(files))

    def test_missing_model_module_raises_for_that_model_only(self, root: Path):
        index = _index(root, {CAT: _module("")})
        assert index.scope(OSCALModelType.CATALOG).modules == (CAT,)
        with pytest.raises(mi.MetaschemaLoadError) as exc:
            index.scope(OSCALModelType.PROFILE)
        assert str(exc.value) == (
            f"OSCAL Metaschema for model 'profile' failed to load: {PROF}: {mi.REFUSED_NO_MODULE}"
        )

    def test_scope_accepts_model_value_string(self, root: Path):
        index = _index(root, {CAT: _module("")})
        assert index.scope("catalog").modules == (CAT,)  # type: ignore[arg-type]


class TestScopeErrors:
    """Validates: Requirements 4.6, 4.8, 4.10, 4.11"""

    def test_failing_imported_module_fails_only_scopes_that_reach_it(self, root: Path):
        bad = _mod("bad")
        files: dict[str, str | bytes] = {
            CAT: _module(_imports(bad) + '<define-flag name="c"/>'),
            bad: _module("<remarks></define-flag>"),
            PROF: _module('<define-flag name="p"/>'),
        }
        index = _index(root, files)
        assert list(index.load_errors) == [bad]
        (err,) = index.load_errors[bad]
        with pytest.raises(mi.MetaschemaLoadError) as exc:
            index.scope(OSCALModelType.CATALOG)
        assert str(exc.value) == (
            f"OSCAL Metaschema for model 'catalog' failed to load: {bad}: {err.reason}"
        )
        with pytest.raises(mi.MetaschemaLoadError, match="for the bundle failed to load"):
            index.all_modules()
        assert index.scope(OSCALModelType.PROFILE).definition_names() == ("p",)

    def test_entity_error_in_imported_module_names_module_and_entity(self, root: Path):
        common = _mod("common")
        files: dict[str, str | bytes] = {
            CAT: _module(_imports(common)),
            common: _module(
                "<allowed-values>&x;</allowed-values>",
                _doctype(_ent_decl("x", "./shared-constraints/gone.ent")),
            ),
        }
        index = _index(root, files)
        with pytest.raises(mi.MetaschemaLoadError) as exc:
            index.scope(OSCALModelType.CATALOG)
        assert str(exc.value) == (
            "OSCAL Metaschema for model 'catalog' failed to load: "
            f'{common}: x (SYSTEM "./shared-constraints/gone.ent"): {mi.REFUSED_MISSING}'
        )

    @pytest.mark.parametrize(
        ("href", "reason"),
        [
            (_mod("gone"), mi.REFUSED_MISSING),
            ("../outside.ent", mi.REFUSED_OUTSIDE),
            ("http://example.invalid/m.xml", mi.REFUSED_SCHEME),
            (ENT, mi.REFUSED_NOT_MODULE),
        ],
    )
    def test_unresolvable_import_is_error_of_importing_module(
        self, root: Path, opened, href, reason
    ):
        files: dict[str, str | bytes] = {CAT: _module(_imports(href)), ENT: "", PROF: _module("")}
        allowlist = _write_bundle(root, files)
        opened.clear()
        index = mi.MetaschemaIndex.load(root)
        assert dict(index.load_errors) == {CAT: (mi.LoadError(CAT, href, reason),)}
        with pytest.raises(mi.MetaschemaLoadError) as exc:
            index.scope(OSCALModelType.CATALOG)
        assert f"{CAT}: {href}: {reason}" in str(exc.value)
        assert index.scope(OSCALModelType.PROFILE).modules == (PROF,)
        _assert_only_allowlisted(
            [p for p in opened if p.name != mi.MANIFEST], root, allowlist - {ENT}
        )

    def test_import_without_href(self, root: Path):
        index = _index(root, {CAT: _module("<import/>")})
        assert index.load_errors[CAT] == (mi.LoadError(CAT, "import", mi.REFUSED_NO_HREF),)

    def test_messages_are_client_safe(self, root: Path, tmp_path: Path, monkeypatch):
        files: dict[str, str | bytes] = {
            CAT: _module(
                _imports("../outside.ent")
                + "<allowed-values>&x;</allowed-values>"
                + "<remarks>"
                + "x" * 300
                + "</remarks>",
                _doctype(_ent_decl("x", "../outside.ent")),
            ),
            PROF: _module("<a><b><c/></b></a>"),
        }
        index = _index(root, files)
        monkeypatch.setattr(mi, "MAX_DEPTH", 2)
        deep = mi.MetaschemaIndex.load(root)
        messages = []
        for idx in (index, deep):
            for model in (OSCALModelType.CATALOG, OSCALModelType.PROFILE):
                try:
                    idx.scope(model)
                except mi.MetaschemaLoadError as err:
                    messages.append(str(err))
        assert len(messages) == 3
        for message in messages:
            assert str(tmp_path) not in message
            assert "Traceback" not in message
            assert "Errno" not in message
            assert "Error(" not in message


class TestBundleLevelErrors:
    """Validates: Requirements 4.8, 4.11 (bundle-level load failure)"""

    @pytest.mark.parametrize(
        ("manifest", "content", "reason"),
        [
            (mi.MANIFEST, "not json", mi.REFUSED_BAD_MANIFEST),
            (f"{mi.SHARED_DIR}/{mi.MANIFEST}", '{"file_hashes": []}', mi.REFUSED_BAD_MANIFEST),
            (f"{mi.SHARED_DIR}/{mi.MANIFEST}", None, "No such file or directory"),
        ],
    )
    def test_bad_manifest_raises_naming_manifest(
        self, root: Path, tmp_path: Path, manifest, content, reason
    ):
        _write_bundle(root, {CAT: _module("")})
        if content is None:
            (root / manifest).unlink()
        else:
            (root / manifest).write_text(content, encoding="utf-8")
        with pytest.raises(mi.MetaschemaLoadError) as exc:
            mi.MetaschemaIndex.load(root)
        message = str(exc.value)
        assert f"oscal_metaschema/{manifest}: {reason}" in message
        assert "hatch run rehash" in message
        assert str(tmp_path) not in message
        assert isinstance(exc.value.__cause__, _Refused)


class TestSourceFiles:
    """Validates: Requirements 4.5 (citations), 11 (source access via the index)"""

    def test_source_files_and_lines(self, root: Path, opened):
        module = _module("<remarks>a\r\nb</remarks>")
        files: dict[str, str | bytes] = {
            PROF: module,
            CAT: _module(""),
            ENT: "one\ntwo\x0cstill two\u2028and\nlast",
            "README.md": "x",
        }
        index = _index(root, files)
        assert index.source_files() == (CAT, PROF, ENT)
        opened.clear()
        lines = index.source_lines(ENT)
        assert lines == ("one\n", "two\x0cstill two\u2028and\n", "last")
        assert index.source_lines(ENT) is lines
        assert opened == [(root / ENT).resolve()]
        assert "".join(index.source_lines(PROF)) == module.decode()
        assert index.source_lines(PROF)[3] == "<remarks>a\r\n"

    @pytest.mark.parametrize("rel", ["README.md", "hashes.json", "../outside.ent", "x" * 200])
    def test_non_source_refused_without_opening(self, root: Path, opened, rel):
        index = _index(root, {CAT: _module(""), "README.md": "x"})
        opened.clear()
        with pytest.raises(ValueError, match="is not an OSCAL Metaschema source file") as exc:
            index.source_lines(rel)
        assert not isinstance(exc.value, mi.MetaschemaLoadError)
        assert len(str(exc.value)) < 150
        assert opened == []

    def test_unreadable_source_raises_client_safe(self, root: Path, tmp_path: Path):
        index = _index(root, {CAT: _module(""), ENT: b"\xff\xfe"})
        with pytest.raises(mi.MetaschemaLoadError) as exc:
            index.source_lines(ENT)
        assert str(exc.value) == (
            f"OSCAL Metaschema source {ENT} cannot be read: {mi.REFUSED_UNDECODABLE}"
        )
        (root / CAT).unlink()
        with pytest.raises(mi.MetaschemaLoadError) as exc:
            index.source_lines(CAT)
        assert str(exc.value).endswith(mi.REFUSED_MISSING)
        assert str(tmp_path) not in str(exc.value)

    def test_records_are_immutable(self, root: Path):
        index = _index(root, {CAT: _module(EXTRACT_BODY)})
        with pytest.raises(TypeError):
            index.load_errors["x"] = ()  # type: ignore[index]
        scope = index.scope(OSCALModelType.CATALOG)
        (a,) = scope.definitions("a")
        assert isinstance(a.constraints, tuple)
        with pytest.raises(TypeError):
            a.constraints[0].attrs["x"] = "y"  # type: ignore[index]
        with pytest.raises(AttributeError):
            scope.modules = ()  # type: ignore[misc]


@pytest.fixture(scope="module")
def index() -> mi.MetaschemaIndex:
    """The real bundle, loaded once per module."""
    return mi.MetaschemaIndex.load()


@pytest.mark.skipif(not (mi.BUNDLE_DIR / mi.MANIFEST).is_file(), reason="bundle not populated")
class TestRealBundleIndex:
    """Validates: Requirements 4.4, 4.5, 4.7, 5.6 against the bundled Metaschema sources."""

    def test_all_scopes_load(self, index: mi.MetaschemaIndex):
        assert dict(index.load_errors) == {}
        for model in OSCALModelType:
            scope = index.scope(model)
            assert scope.modules[0] == model_module(model)
            assert len(set(scope.modules)) == len(scope.modules)
            assert "oscal_metadata_metaschema.xml" in scope.modules
        assert len(index.all_modules().modules) == 14
        assert len(index.source_files()) == 26

    def test_definition_counts(self, index: mi.MetaschemaIndex):
        # The design notes 500 Definitions / 266 names; the parsed trees hold 498 / 265
        # (every define-* element in the Metaschema namespace, comments excluded).
        everything = index.all_modules()
        assert len(_defs(everything)) == 498
        assert len(everything.definition_names()) == 265
        locations = {d.key.location for d in _defs(everything)}
        assert len(locations) == 498

    def test_defined_component_type_flag_ref(self, index: mi.MetaschemaIndex):
        scope = index.scope(OSCALModelType.COMPONENT_DEFINITION)
        (dc,) = scope.definitions("defined-component")
        assert dc.scope == "global"
        (slot,) = [f for f in dc.flags if f.effective_name == "type"]
        assert (slot.ref, slot.inline, slot.required) == ("defined-component-type", None, True)
        (flag,) = scope.global_definition("flag", "defined-component-type")
        (av,) = [c for c in flag.constraints if c.type == "allowed-values"]
        assert av.id == "oscal-component-type"
        assert [alt.path for alt in av.alternatives] == [""]
        assert "service" in [e.value for e in av.enums]

    def test_enum_locations_point_at_their_value(self, index: mi.MetaschemaIndex):
        everything = index.all_modules()
        enums = [
            e
            for d in _defs(everything)
            for c in d.constraints
            for e in c.enums  # allowed-values only
        ]
        assert len(enums) == 914
        assert sum(e.location.source_file.endswith(".ent") for e in enums) == 275
        for e in enums:
            line = index.source_lines(e.location.source_file)[e.location.line - 1]
            assert "<enum" in line


# --------------------------------------------------------------------------------------
# get_index() singleton
# --------------------------------------------------------------------------------------


@pytest.mark.usefixtures("reset_metaschema_index")
class TestGetIndex:
    """Validates: Requirements 4.8, 5.13 (process-wide, thread-safe, retry after failure)."""

    @pytest.fixture
    def counted_load(self, monkeypatch, root: Path) -> list[Path]:
        """Point ``BUNDLE_DIR`` at a tmp bundle and record every ``load`` call's root.

        The wrapper calls the real ``load`` after a short sleep, which widens the window in
        which concurrent first callers would race if the lock were missing.
        """
        calls: list[Path] = []
        real_load = mi.MetaschemaIndex.load.__func__  # type: ignore[attr-defined]

        def wrapped(cls, root_: Path = mi.BUNDLE_DIR) -> mi.MetaschemaIndex:
            calls.append(root_)
            time.sleep(0.05)
            return real_load(cls, root_)

        monkeypatch.setattr(mi.MetaschemaIndex, "load", classmethod(wrapped))
        monkeypatch.setattr(mi, "BUNDLE_DIR", root)
        return calls

    def test_same_object_on_repeated_calls(self, root: Path, counted_load: list[Path]):
        _write_bundle(root, {})
        first = mi.get_index()
        assert isinstance(first, mi.MetaschemaIndex)
        assert mi.get_index() is first
        assert mi._index is first
        # BUNDLE_DIR is read at call time, and the bundle is parsed once.
        assert counted_load == [root]

    def test_failure_leaves_none_and_next_call_retries(self, root: Path, counted_load: list[Path]):
        # No manifest yet: a bundle-level failure, raised by the real loader.
        with pytest.raises(mi.MetaschemaLoadError):
            mi.get_index()
        assert mi._index is None
        _write_bundle(root, {})
        index = mi.get_index()
        assert mi._index is index
        assert mi.get_index() is index
        assert counted_load == [root, root]

    def test_concurrent_first_calls_share_one_load(self, root: Path, counted_load: list[Path]):
        _write_bundle(root, {})
        n = 8
        barrier = threading.Barrier(n)

        def call() -> mi.MetaschemaIndex:
            barrier.wait()
            return mi.get_index()

        with ThreadPoolExecutor(max_workers=n) as pool:
            results = list(pool.map(lambda _: call(), range(n)))
        assert len({id(r) for r in results}) == 1
        assert results[0] is mi._index
        assert counted_load == [root]

    def test_fixture_resets_singleton(self):
        assert mi._index is None


# --------------------------------------------------------------------------------------
# Loader examples (task 5.10)
# --------------------------------------------------------------------------------------

_BILLION_LAUGHS = (
    '<!ENTITY lol "lol">',
    *(f'<!ENTITY lol{i} "{f"&lol{i - 1 or ''};" * 10}">' for i in range(1, 10)),
)


def _scope_error(index: mi.MetaschemaIndex, model: OSCALModelType) -> str:
    with pytest.raises(mi.MetaschemaLoadError) as exc:
        index.scope(model)
    return str(exc.value)


def _padded_entity(size: int) -> bytes:
    """A well-formed entity file of exactly ``size`` bytes: one enum plus a comment."""
    head = _ns_ent("<enum value='a'/>").encode() + b"<!--"
    tail = b"-->"
    return head + b"p" * (size - len(head) - len(tail)) + tail


def _nested_module(levels: int) -> bytes:
    """A module whose elements nest ``levels`` deep, ``METASCHEMA`` included."""
    inner = levels - 1
    return _module("<a>" * inner + "</a>" * inner)


class TestLoaderExamples:
    """Loader examples through ``MetaschemaIndex.load``, with the real limits.

    Validates: Requirements 4.3, 4.6, 4.8, 4.10

    Bullet -> tests (new tests in this class, existing ones elsewhere in the file):

    - Billion laughs and parameter entities give a ``LoadError`` (4.3):
      ``test_billion_laughs_and_parameter_entity_fail_their_scope_only``; per-module
      ``TestLoadModule.test_declarations_refused``.
    - A missing entity file names its path (4.6):
      ``test_missing_entity_file_named_as_written``;
      ``TestLoadModuleRefusedReferences.test_refused_without_opening`` (``gone.ent``) and
      ``TestScopeErrors.test_entity_error_in_imported_module_names_module_and_entity``.
    - ``socket.socket`` raising has no effect on ``load`` (4.3):
      ``test_socket_denied_tmp_bundle``, ``test_socket_denied_real_bundle``; the ``opened``
      and ``no_io`` fixtures also deny sockets in the loader tests that use them.
    - A 2,097,153-byte entity file and a 65-level module each give a ``LoadError`` naming
      the file and the limit (4.10): ``test_entity_file_over_real_size_limit``,
      ``test_module_over_real_depth_limit`` (both check that the limit itself still
      loads); lowered-limit cases in ``TestLoadModuleLimits`` and Property 23.
    - A load error affects only the scopes that reach it (4.8): every test here checks
      the unaffected profile scope;
      ``TestScopeErrors.test_failing_imported_module_fails_only_scopes_that_reach_it``.
    - 8 threads calling ``get_index()`` get one object from one parse:
      ``TestGetIndex.test_concurrent_first_calls_share_one_load``.
    """

    @pytest.fixture
    def sockets(self, monkeypatch) -> list[tuple]:
        """Make ``socket.socket`` raise; record every attempt."""
        calls: list[tuple] = []

        def deny(*args, **kwargs):
            calls.append(args)
            raise OSError("network access denied by test")

        monkeypatch.setattr(socket, "socket", deny)
        return calls

    @pytest.mark.parametrize(
        ("decls", "subject", "reason"),
        [
            (_BILLION_LAUGHS, "lol1", mi.REFUSED_ENTITY_IN_ENTITY),
            (('<!ENTITY % pe "x">',), "pe", mi.REFUSED_PARAM_ENTITY),
            (
                ('<!ENTITY % pe SYSTEM "./shared-constraints/x.ent">',),
                "pe",
                mi.REFUSED_PARAM_ENTITY,
            ),
        ],
    )
    def test_billion_laughs_and_parameter_entity_fail_their_scope_only(
        self, root: Path, decls, subject, reason
    ):
        files: dict[str, str | bytes] = {
            CAT: _module(
                "<remarks>&lol9;</remarks>" if subject == "lol1" else "", _doctype(*decls)
            ),
            PROF: _module('<define-flag name="p"/>'),
            ENT: "",
        }
        index = _index(root, files)
        assert dict(index.load_errors) == {CAT: (mi.LoadError(CAT, subject, reason),)}
        assert f"{CAT}: {subject}: {reason}" in _scope_error(index, OSCALModelType.CATALOG)
        assert index.scope(OSCALModelType.PROFILE).definition_names() == ("p",)

    def test_missing_entity_file_named_as_written(self, root: Path):
        written = "shared-constraints/./never-there.ent"
        files: dict[str, str | bytes] = {
            CAT: _module("<allowed-values>&x;</allowed-values>", _doctype(_ent_decl("x", written))),
            PROF: _module(""),
        }
        index = _index(root, files)
        assert dict(index.load_errors) == {
            CAT: (mi.LoadError(CAT, f'x (SYSTEM "{written}")', mi.REFUSED_MISSING),)
        }
        message = _scope_error(index, OSCALModelType.CATALOG)
        assert f'{CAT}: x (SYSTEM "{written}"): {mi.REFUSED_MISSING}' in message
        assert index.scope(OSCALModelType.PROFILE).modules == (PROF,)

    def test_socket_denied_tmp_bundle(self, root: Path, sockets: list[tuple]):
        # Network references are refused by scheme, before any lookup is attempted.
        url = "http://example.invalid/x.ent"
        files: dict[str, str | bytes] = {
            CAT: _module(EXTRACT_BODY),
            PROF: _module("<allowed-values>&n;</allowed-values>", _doctype(_ent_decl("n", url))),
        }
        _write_bundle(root, files)
        denied = mi.MetaschemaIndex.load(root)
        assert sockets == []
        assert dict(denied.load_errors) == {
            PROF: (mi.LoadError(PROF, f'n (SYSTEM "{url}")', mi.REFUSED_SCHEME),)
        }
        assert denied.scope(OSCALModelType.CATALOG).definition_names() == (
            "a",
            "f",
            "g",
            "id",
            "t",
        )

    @pytest.mark.skipif(not (mi.BUNDLE_DIR / mi.MANIFEST).is_file(), reason="bundle not populated")
    def test_socket_denied_real_bundle(self, index: mi.MetaschemaIndex, sockets: list[tuple]):
        denied = mi.MetaschemaIndex.load()
        assert sockets == []
        assert dict(denied.load_errors) == dict(index.load_errors) == {}
        assert denied.source_files() == index.source_files()
        for model in OSCALModelType:
            assert denied.scope(model).modules == index.scope(model).modules
            assert denied.scope(model).definition_names() == index.scope(model).definition_names()

    def test_entity_file_over_real_size_limit(self, root: Path):
        limit = mi.MAX_FILE_BYTES
        assert limit == 2_097_152
        body = '<define-flag name="t"><constraint><allowed-values>&x;</allowed-values></constraint>'
        module = _module(body + "</define-flag>", _doctype(_ent_decl()))
        files: dict[str, str | bytes] = {CAT: module, PROF: _module(""), ENT: _padded_entity(limit)}
        assert len(files[ENT]) == limit
        at_limit = _index(root, files)
        assert dict(at_limit.load_errors) == {}
        (t,) = at_limit.scope(OSCALModelType.CATALOG).definitions("t")
        assert [e.value for c in t.constraints for e in c.enums] == ["a"]

        files[ENT] = _padded_entity(limit + 1)
        assert len(files[ENT]) == 2_097_153
        over = _index(root, files)
        reason = "file is larger than the limit of 2097152 bytes"
        assert dict(over.load_errors) == {CAT: (mi.LoadError(CAT, ENT, reason),)}
        assert f"{CAT}: {ENT}: {reason}" in _scope_error(over, OSCALModelType.CATALOG)
        assert over.scope(OSCALModelType.PROFILE).modules == (PROF,)

    def test_module_over_real_depth_limit(self, root: Path):
        limit = mi.MAX_DEPTH
        assert limit == 64
        at_limit = _index(root, {CAT: _nested_module(limit), PROF: _module("")})
        assert dict(at_limit.load_errors) == {}

        over = _index(root, {CAT: _nested_module(limit + 1), PROF: _module("")})
        reason = "element nesting exceeds the limit of 64"
        assert dict(over.load_errors) == {CAT: (mi.LoadError(CAT, CAT, reason),)}
        assert f"{CAT}: {reason}" in _scope_error(over, OSCALModelType.CATALOG)
        assert over.scope(OSCALModelType.PROFILE).modules == (PROF,)


# --------------------------------------------------------------------------------------
# Property 12: entity expansion
# --------------------------------------------------------------------------------------

# Filler written before an enum or entity reference: blank lines and comments (each on its
# own line) shift the start tag to a random line of its file.
_FILLER = st.lists(st.sampled_from(["", "<!-- c -->", "  "]), max_size=3)


@st.composite
def _entity_bundle(draw: st.DrawFn) -> tuple[list[list[tuple]], list[tuple[list[str], list]]]:
    """Draw constraints (lists of items) and entity files (leading filler, enum fillers).

    An item is ``("enum", filler)`` for an inline enum or ``("ent", k, filler)`` for a
    reference to entity file ``k``. Each entity file is referenced exactly once, from a
    randomly chosen constraint, at a random position among that constraint's inline enums.
    """
    n_constraints = draw(st.integers(1, 5))
    n_ents = draw(st.integers(1, 4))
    ents = [(draw(_FILLER), draw(st.lists(_FILLER, min_size=1, max_size=5))) for _ in range(n_ents)]
    constraints: list[list[tuple]] = [
        [("enum", f) for f in draw(st.lists(_FILLER, max_size=4))] for _ in range(n_constraints)
    ]
    for k in range(n_ents):
        items = constraints[draw(st.integers(0, n_constraints - 1))]
        items.insert(draw(st.integers(0, len(items))), ("ent", k, draw(_FILLER)))
    return constraints, ents


def _line_of(text: str, needle: str) -> int:
    """1-based line of the single occurrence of ``needle`` in ``text``."""
    assert text.count(needle) == 1, needle
    return text[: text.index(needle)].count("\n") + 1


class TestEntityExpansionProperty:
    """Property 12: Entities expand in place with their own Source_Locations.

    **Validates: Requirements 4.1, 4.5**
    """

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(_entity_bundle())
    def test_entities_expand_in_place_with_own_locations(
        self, drawn: tuple[list[list[tuple]], list[tuple[list[str], list]]]
    ):
        constraints, ents = drawn
        files: dict[str, str | bytes] = {}
        # Expected per constraint: (value, file holding the start tag) in inlined order.
        expected: list[list[tuple[str, str]]] = []

        ent_values: list[list[str]] = []
        for k, (leading, enum_fillers) in enumerate(ents):
            rel = f"{mi.SHARED_DIR}/e{k}.ent"
            lines = list(leading)
            values = []
            for i, filler in enumerate(enum_fillers):
                value = f"e{k}-{i}"
                lines += [*filler, f'<enum value="{value}">V</enum>']
                values.append(value)
            files[rel] = _ns_ent("\n".join(lines))
            ent_values.append(values)

        body = ['<define-flag name="t">', "<constraint>"]
        n_inline = 0
        for c, items in enumerate(constraints):
            body.append(f'<allowed-values target="." id="av{c}">')
            seq: list[tuple[str, str]] = []
            for item in items:
                body += item[-1]
                if item[0] == "enum":
                    value = f"m-{n_inline}"
                    n_inline += 1
                    body.append(f'<enum value="{value}"/>')
                    seq.append((value, CAT))
                else:
                    k = item[1]
                    body.append(f"&e{k};")
                    seq += [(v, f"{mi.SHARED_DIR}/e{k}.ent") for v in ent_values[k]]
            body.append("</allowed-values>")
            expected.append(seq)
        body += ["</constraint>", "</define-flag>"]
        doctype = _doctype(
            *(_ent_decl(f"e{k}", f"./{mi.SHARED_DIR}/e{k}.ent") for k in range(len(ents)))
        )
        files[CAT] = _module("\n".join(body), doctype)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = _index(root, files)
            assert dict(index.load_errors) == {}
            (t,) = index.scope(OSCALModelType.CATALOG).definitions("t")

        assert [av.id for av in t.constraints] == [f"av{c}" for c in range(len(constraints))]
        for av, seq in zip(t.constraints, expected, strict=True):
            assert [e.value for e in av.enums] == [v for v, _ in seq]
            for enum, (value, rel) in zip(av.enums, seq, strict=True):
                text = files[rel]
                text = text.decode() if isinstance(text, bytes) else text
                line = _line_of(text, f'value="{value}"')
                assert enum.location == mi.SourceLocation(rel, line), value


# --------------------------------------------------------------------------------------
# Property 13: path resolution
# --------------------------------------------------------------------------------------

# Placeholders replaced with paths into the per-example temporary directory.
_ABS_OUTSIDE = "<abs-outside>"
_URI_OUTSIDE = "<uri-outside>"
_ABS_INSIDE = "<abs-inside>"

# Path segments: navigation, listed / unlisted / listed-but-missing / never-written names,
# symlinks (in-bundle, out-of-bundle file and directory, loop), and the outside file's name.
_REF_SEGMENTS = st.sampled_from(
    [
        "..",
        "..",
        ".",
        "",
        mi.SHARED_DIR,
        mi.SHARED_DIR,
        mi.SHARED_DIR,
        "b",
        "b",
        MOD,
        "x.ent",
        "u.ent",
        "gone.ent",
        "never.ent",
        "in.ent",
        "out.ent",
        "up",
        "loop",
        "outside.ent",
        mi.MANIFEST,
    ]
)
_RELATIVE_PREFIXES = st.sampled_from(["", "./", "../", ".\\", "..\\"])
_ROOTED_PREFIXES = st.sampled_from(
    [
        "/",
        "\\",
        "//srv/share/",
        "\\\\srv\\share\\",
        "C:",
        "c:/",
        "C:\\",
        "file://",
        "file:///",
        "http://example.invalid/",
        _ABS_OUTSIDE,
        _URI_OUTSIDE,
        _ABS_INSIDE,
    ]
)


@st.composite
def _near_valid_refs(draw: st.DrawFn) -> str:
    """References close to a real bundle file, so accepted and listed-symlink cases occur."""
    via = draw(
        st.sampled_from(["", "./", "b/../", f"{mi.SHARED_DIR}/../", f"{mi.SHARED_DIR}/up/b/"])
    )
    folder = draw(st.sampled_from([f"{mi.SHARED_DIR}/", ""]))
    name = draw(st.sampled_from(["x.ent", "in.ent", "out.ent", "gone.ent", "u.ent", MOD]))
    return via + folder + name


@st.composite
def _grammar_refs(draw: st.DrawFn) -> str:
    """A reference string: a prefix, segments joined by ``/`` or ``\\``, an optional NUL."""
    segments = draw(st.lists(_REF_SEGMENTS, min_size=1, max_size=5))
    # Mostly relative references, where ``..`` and symlinks decide containment.
    rooted = draw(st.integers(0, 3)) == 0
    ref = draw(_ROOTED_PREFIXES if rooted else _RELATIVE_PREFIXES)
    for i, segment in enumerate(segments):
        ref += (draw(st.sampled_from(["/", "/", "\\"])) if i else "") + segment
    if draw(st.integers(0, 19)) == 0:
        pos = draw(st.integers(0, len(ref)))
        ref = ref[:pos] + "\0" + ref[pos:]
    return ref


_refs = st.one_of(_near_valid_refs(), _grammar_refs())

_REFUSED_REASONS = frozenset(
    {
        mi.REFUSED_NUL,
        mi.REFUSED_SCHEME,
        mi.REFUSED_ABSOLUTE,
        mi.REFUSED_UNRESOLVABLE,
        mi.REFUSED_OUTSIDE,
        mi.REFUSED_MISSING,
        mi.REFUSED_UNLISTED,
    }
)


def _path_bundle(tmp: Path) -> tuple[Path, frozenset[str]]:
    """Write a bundle under ``tmp/b`` plus ``tmp/outside.ent``; return (root, allowlist).

    Listed: the module ``MOD``, ``x.ent``, ``gone.ent`` (deleted after listing), and the symlinks
    ``in.ent`` (to ``x.ent``) and ``out.ent`` (to the outside file). ``u.ent`` is unlisted.
    Symlinks ``up`` (outside directory) and ``loop`` are not files. Symlinks are skipped
    where the platform does not allow them; references to them are then missing files.
    """
    root = tmp / "b"
    root.mkdir()
    (tmp / "outside.ent").write_text(_ns_ent(f"<enum value='{LEAKED}'/>"), encoding="utf-8")
    shared = f"{mi.SHARED_DIR}/"
    files: dict[str, str | bytes] = {
        MOD: "placeholder",  # rewritten per example with the reference under test
        f"{shared}x.ent": _ns_ent("<enum value='x'/>"),
        f"{shared}u.ent": _ns_ent("<enum value='u'/>"),
        f"{shared}gone.ent": _ns_ent("<enum value='g'/>"),
        f"{shared}in.ent": "placeholder",
        f"{shared}out.ent": "placeholder",
    }
    allowlist = _write_bundle(root, files, unlisted=frozenset({f"{shared}u.ent"}))
    (root / shared / "gone.ent").unlink()
    links = {
        "in.ent": (Path("x.ent"), False),
        "out.ent": (tmp / "outside.ent", False),
        "up": (tmp, True),
        "loop": (Path("loop"), False),
    }
    for name, (target, is_dir) in links.items():
        link = root / shared / name
        link.unlink(missing_ok=True)
        with contextlib.suppress(OSError):  # Windows without symlink privilege
            link.symlink_to(target, target_is_directory=is_dir)
    return root, allowlist


def _expand(ref: str, tmp: Path) -> str:
    """Replace the placeholders with absolute paths (and a URI) into ``tmp``."""
    return (
        ref.replace(_ABS_OUTSIDE, f"{tmp}/")
        .replace(_URI_OUTSIDE, f"{tmp.as_uri()}/")
        .replace(_ABS_INSIDE, f"{tmp / 'b'}/")
    )


class TestPathResolutionProperty:
    """Property 13: Path resolution never escapes the allowlist.

    **Validates: Requirements 4.2, 4.6**
    """

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(ref=_refs, base_rel=st.sampled_from(["", mi.SHARED_DIR]))
    def test_resolution_and_load_stay_in_allowlist(self, ref: str, base_rel: str):
        from_shared = bool(base_rel)
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            root, allowlist = _path_bundle(tmp)
            ref = _expand(ref, tmp)
            root_resolved = root.resolve()
            body = (
                '<define-flag name="t"><constraint><allowed-values target=".">'
                "&x;</allowed-values></constraint></define-flag>"
            )
            (root / MOD).write_bytes(_module(body, _doctype(_ent_decl("x", ref))))

            # 1. resolve_bundle_path: an allowlisted existing file inside root, or _Refused.
            base = root / base_rel
            refusal: _Refused | None = None
            try:
                got = resolve_bundle_path(base, ref, root, allowlist)
            except _Refused as err:
                refusal = err
                event(err.reason)
                assert err.reason in _REFUSED_REASONS
            else:
                event("accepted")
                assert got.path.is_relative_to(root_resolved)
                assert got.rel in allowlist
                assert got.path == root_resolved / got.rel
                assert got.path.is_file()

            # 2. The same reference as an entity SYSTEM id: only allowlisted files opened.
            opened: list[Path] = []
            real_open = builtins.open

            def spy_open(file, *args, **kwargs):
                opened.append(Path(file).resolve())
                return real_open(file, *args, **kwargs)

            def deny_socket(*args, **kwargs):
                raise AssertionError("unexpected socket")

            with (
                mock.patch.object(builtins, "open", spy_open),
                mock.patch.object(socket, "socket", deny_socket),
            ):
                result = mi._load_module(root, MOD, allowlist)

            for path in opened:
                assert path.is_relative_to(root_resolved), path
                assert path.relative_to(root_resolved).as_posix() in allowlist, path

            if from_shared:
                return  # entities resolve against the module directory (root), not shared/
            if refusal is not None and "\0" not in ref:
                # A refused reference names the module, the entity, and the reason.
                assert result == mi.LoadError(MOD, f'x (SYSTEM "{ref}")', refusal.reason)
                assert opened == [(root / MOD).resolve()]
            elif refusal is not None:
                # NUL is not well-formed XML: the module itself fails before the entity.
                assert isinstance(result, mi.LoadError)
                assert result.module == MOD
                assert opened == [(root / MOD).resolve()]
            elif got.rel != MOD:  # a module entity-including itself fails to parse; skip it
                assert isinstance(result, mi._Node), result
                assert opened == [(root / MOD).resolve(), got.path]


_MODEL_MODULES = {model_module(m): m for m in OSCALModelType}
_MODULE_POOL = (*sorted(_MODEL_MODULES), *(_mod(f"extra{i}") for i in range(4)))


@st.composite
def _import_graphs(draw: st.DrawFn) -> dict[str, list[str]]:
    """An import graph over 1-12 modules: each module's imports in document order.

    Targets are drawn from the present modules only, so self-imports, cycles, diamonds, and
    duplicate imports occur, but every import names a module (no load errors).
    """
    present = draw(
        st.lists(st.sampled_from(_MODULE_POOL), min_size=1, max_size=len(_MODULE_POOL), unique=True)
    )
    targets = st.sampled_from(present)
    return {m: draw(st.lists(targets, max_size=5)) for m in present}


def _reachable(start: str, graph: dict[str, list[str]]) -> set[str]:
    """Oracle: the modules reachable from ``start`` (iterative DFS, order-free)."""
    seen = {start}
    stack = [start]
    while stack:
        for nxt in graph[stack.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _bfs_order(start: str, graph: dict[str, list[str]]) -> list[str]:
    """Oracle: BFS from ``start`` following imports in document order (design: scope order)."""
    order = [start]
    frontier = [start]
    while frontier:
        nxt_frontier = []
        for module in frontier:
            for target in graph[module]:
                if target not in order:
                    order.append(target)
                    nxt_frontier.append(target)
        frontier = nxt_frontier
    return order


def _def_name(module: str) -> str:
    return "d-" + module.removeprefix("oscal_").removesuffix("_metaschema.xml")


class TestModelScopeProperty:
    """Property 14: Model_Scope is the import closure, each module once.

    **Validates: Requirements 4.4, 4.7**
    """

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(_import_graphs())
    def test_scope_is_import_closure_each_module_once(self, graph: dict[str, list[str]]):
        event(f"modules={len(graph)}")
        if any(m in targets for m, targets in graph.items()):
            event("self-import")
        if any(len(set(t)) < len(t) for t in graph.values()):
            event("duplicate import")
        files: dict[str, str | bytes] = {
            m: _module(_imports(*targets) + f'<define-flag name="{_def_name(m)}"/>')
            for m, targets in graph.items()
        }
        with tempfile.TemporaryDirectory() as tmp:
            index = _index(Path(tmp), files)

            assert dict(index.load_errors) == {}
            for module, model in _MODEL_MODULES.items():
                if module not in graph:
                    with pytest.raises(mi.MetaschemaLoadError, match=mi.REFUSED_NO_MODULE):
                        index.scope(model)
                    continue
                scope = index.scope(model)
                modules = scope.modules
                assert len(modules) == len(set(modules))  # each module once
                assert set(modules) == _reachable(module, graph)  # exactly the closure
                assert list(modules) == _bfs_order(module, graph)  # BFS scope order
                assert modules[0] == module
                # Visible Definitions are exactly the closure's, each from its own module.
                assert scope.definition_names() == tuple(sorted(_def_name(m) for m in modules))
                for m in modules:
                    assert [d.key.module for d in scope.definitions(_def_name(m))] == [m]

            everything = index.all_modules()
            assert everything.modules == tuple(sorted(graph))
            assert everything.definition_names() == tuple(sorted(_def_name(m) for m in graph))


# --------------------------------------------------------------------------------------
# Property 23: Loader size and depth limits
# --------------------------------------------------------------------------------------

SSP = model_module(OSCALModelType.SYSTEM_SECURITY_PLAN)
_LIM_PAD = 600  # keeps generated files larger than the manifests and the unrelated module


def _ns_chain(depth: int) -> str:
    """``depth`` (>= 1) nested ``<a>`` elements, the outermost declaring the namespace."""
    return f'<a xmlns="{METASCHEMA_NS}">' + "<a>" * (depth - 1) + "</a>" * depth


@st.composite
def _limit_bundles(draw):
    """A module with 0-2 entity files referenced at random depths, padded to random sizes.

    Returns ``(module_depth, refs, ent_depths, pads)``: ``refs[i]`` is the number of module
    elements (below ``METASCHEMA``) enclosing entity ``i``'s reference, sorted so document
    order is entity order; ``pads`` are extra bytes for the module, then each entity.
    """
    n_ents = draw(st.integers(0, 2))
    module_depth = draw(st.integers(0, 40))
    refs = sorted(draw(st.lists(st.integers(0, module_depth), min_size=n_ents, max_size=n_ents)))
    ent_depths = draw(st.lists(st.integers(1, 39), min_size=n_ents, max_size=n_ents))
    pads = draw(st.lists(st.integers(0, 400), min_size=n_ents + 1, max_size=n_ents + 1))
    return module_depth, refs, ent_depths, pads


def _ent_rel(i: int) -> str:
    return f"{mi.SHARED_DIR}/e{i}.ent"


def _limit_module(module_depth: int, refs: list[int], pad: int) -> bytes:
    """Module text: a chain of ``module_depth`` elements, entity ``i`` referenced at ``refs[i]``."""
    body = ""
    for level in range(module_depth, -1, -1):  # build inside out
        here = "".join(f"&e{i};" for i, r in enumerate(refs) if r == level)
        body = here + body if level == 0 else "<a>" + here + body + "</a>"
    decls = [_ent_decl(f"e{i}", f"./{_ent_rel(i)}") for i in range(len(refs))]
    comment = f"<!--{'x' * (_LIM_PAD + pad)}-->"
    return _module(body + comment, _doctype(*decls) if decls else "")


def _first_limit_hit(
    lim: str,
    sizes: dict[str, int],
    module_depth: int,
    refs: list[int],
    ent_depths: list[int],
    max_bytes: int,
    max_depth: int,
) -> tuple[str, str] | None:
    """Simulate loading ``lim`` in document order; return ``(file, reason)`` of the first hit."""

    def read(rel: str) -> tuple[str, str] | None:
        return (rel, mi._too_large(max_bytes)) if sizes[rel] > max_bytes else None

    if hit := read(lim):
        return hit
    for level in range(module_depth + 1):  # level 0 is METASCHEMA itself
        if 1 + level > max_depth:
            return lim, mi._too_deep(max_depth)
        for i, r in enumerate(refs):
            if r != level:
                continue
            if hit := read(_ent_rel(i)):
                return hit
            if 1 + level + ent_depths[i] > max_depth:
                return _ent_rel(i), mi._too_deep(max_depth)
    return None


class TestLoaderLimitsProperty:
    """Property 23: Loader size and depth limits.

    **Validates: Requirements 4.8, 4.10**
    """

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(bundle=_limit_bundles(), via_import=st.booleans(), data=st.data())
    def test_limits_fail_exactly_the_offending_module(
        self, *, bundle, via_import: bool, data: st.DataObject
    ):
        module_depth, refs, ent_depths, pads = bundle
        # The module under test is the catalog module itself, or a module that both the
        # catalog and SSP modules import. The profile module is unrelated.
        lim = _mod("lim") if via_import else CAT
        files: dict[str, str | bytes] = {
            lim: _limit_module(module_depth, refs, pads[0]),
            PROF: _module('<define-flag name="p"/>'),
        }
        for i, d in enumerate(ent_depths):
            padding = f"<!--{'x' * (_LIM_PAD + pads[i + 1])}-->"
            files[_ent_rel(i)] = _ns_chain(d) + padding
        if via_import:
            files[CAT] = _module(_imports(lim))
            files[SSP] = _module(_imports(lim))
        sizes = {rel: len(c.encode() if isinstance(c, str) else c) for rel, c in files.items()}
        combined = max(
            [1 + module_depth, *(1 + r + d for r, d in zip(refs, ent_depths, strict=True))]
        )
        generated = [sizes[lim], *(sizes[_ent_rel(i)] for i in range(len(refs)))]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_bundle(root, files)
            # Floors keep the manifests and the unrelated modules within the limits.
            floor_bytes = max(
                sizes[PROF],
                *(sizes[m] for m in (CAT, SSP) if via_import),
                *((root / p / mi.MANIFEST).stat().st_size for p in ("", mi.SHARED_DIR)),
            )
            # Half the time near one file's size (boundaries), else above every size so
            # the depth limit decides.
            near = st.sampled_from(generated).flatmap(lambda s: st.integers(s - 2, s + 2))
            above = st.integers(max(generated), max(generated) + 64)
            max_bytes = max(floor_bytes, data.draw(st.one_of(near, above), "max_bytes"))
            max_depth = data.draw(st.integers(max(2, combined - 3), combined + 3), "max_depth")

            with (
                mock.patch.object(mi, "MAX_FILE_BYTES", max_bytes),
                mock.patch.object(mi, "MAX_DEPTH", max_depth),
            ):
                index = mi.MetaschemaIndex.load(root)

            expect_ok = all(s <= max_bytes for s in generated) and combined <= max_depth
            hit = _first_limit_hit(lim, sizes, module_depth, refs, ent_depths, max_bytes, max_depth)
            assert (hit is None) == expect_ok
            assert index.scope(OSCALModelType.PROFILE).modules == (PROF,)
            reaching = [OSCALModelType.CATALOG]
            if via_import:
                reaching.append(OSCALModelType.SYSTEM_SECURITY_PLAN)

            if expect_ok:
                event("loads")
                assert dict(index.load_errors) == {}
                for model in reaching:
                    assert lim in index.scope(model).modules
                return

            assert hit is not None
            subject, reason = hit
            event(f"refused: {'module' if subject == lim else 'entity'} {reason.split()[0]}")
            assert dict(index.load_errors) == {lim: (mi.LoadError(lim, subject, reason),)}
            for model in reaching:
                with pytest.raises(mi.MetaschemaLoadError) as exc:
                    index.scope(model)
                assert f"{lim}: " in str(exc.value)
                assert subject in str(exc.value)
                assert reason in str(exc.value)
            with pytest.raises(mi.MetaschemaLoadError):
                index.all_modules()


# --------------------------------------------------------------------------------------
# Property 24: Client-facing errors never disclose host paths
# --------------------------------------------------------------------------------------

_LIM = _mod("lim")
_UNLISTED = f"{mi.SHARED_DIR}/u.ent"
_DISC_PAD = f"<!--{'p' * 600}-->"  # keeps the faulty files larger than every manifest
_DISC_BODY = (
    '<define-flag name="t"><constraint><allowed-values target=".">'
    "&x;</allowed-values></constraint></define-flag>"
)
_DEEP = "<a><b><c><d/></c></b></a>"  # METASCHEMA plus 4 levels


def _outside_refs() -> list[str]:
    """Refused references. Absolute ones point at the system temp dir or ``/etc``, never
    under the per-example directory: a SYSTEM id or href is echoed as written (RC-13), so
    it may not carry the marker without making the property vacuous."""
    outside = Path(tempfile.gettempdir()) / "outside.ent"
    return [
        "http://example.invalid/x.ent",
        "https://example.invalid/x.ent",
        "file:///etc/passwd",
        outside.as_uri(),
        str(outside),
        "/etc/passwd",
        "C:\\Windows\\x.ent",
        "\\\\srv\\share\\x.ent",
        "../outside.ent",
        "../../../../../etc/passwd",
        f"./{_UNLISTED}",
        f"./{mi.SHARED_DIR}/missing.ent",
        f"./{mi.SHARED_DIR}/../../outside.ent",
    ]


_OS_ERRORS = st.sampled_from(["permission", "io", "isdir", "message-only"])


def _os_error(kind: str, file: object) -> OSError:
    """An OSError as the OS raises it: the message or ``filename`` embeds the absolute path."""
    if kind == "permission":
        return PermissionError(13, "Permission denied", str(file))
    if kind == "io":
        return OSError(5, "Input/output error", str(file))
    if kind == "isdir":
        return IsADirectoryError(21, "Is a directory", str(file))
    return OSError(f"[Errno 13] Permission denied: '{file}'")  # no strerror


@st.composite
def _disclosure_faults(draw: st.DrawFn) -> dict:
    """One injected failure: its kind and parameters, plus which module carries it."""
    kind = draw(
        st.sampled_from(
            [
                "system",
                "param",
                "nested",
                "entity-in-entity",
                "unparsed",
                "undeclared",
                "malformed",
                "size",
                "depth",
                "unreadable",
                "manifest",
                "import",
                "occurs",
            ]
        )
    )
    fault: dict = {"kind": kind, "via_import": draw(st.booleans())}
    refs = st.sampled_from(_outside_refs())
    if kind in {"system", "nested"}:
        fault["ref"] = draw(refs)
    elif kind == "undeclared":
        fault["external_dtd"] = draw(st.booleans())
    elif kind == "malformed":
        fault["file"] = draw(st.sampled_from(["module", "entity"]))
        fault["content"] = draw(
            st.sampled_from(
                [
                    "<remarks></define-flag>",
                    "<enum value='a'>",
                    "<a b='1' b='2'/>",
                    "\x00",
                    b"\xff\xfe\xff<",  # undecodable (entity file only; see _disclosure_files)
                ]
            )
        )
    elif kind == "size":
        fault["file"] = draw(st.sampled_from(["module", "entity"]))
    elif kind == "depth":
        fault["max_depth"] = draw(st.integers(1, 4))
    elif kind == "unreadable":
        fault["file"] = draw(st.sampled_from(["module", "entity", "manifest", "shared-manifest"]))
        fault["error"] = draw(_OS_ERRORS)
    elif kind == "manifest":
        fault["file"] = draw(st.sampled_from([mi.MANIFEST, f"{mi.SHARED_DIR}/{mi.MANIFEST}"]))
        fault["content"] = draw(
            st.sampled_from(
                [
                    None,  # deleted
                    b"not json",
                    b"{}",
                    b'{"file_hashes": []}',
                    b'{"file_hashes": {"../x": "0"}}',
                    b"\xff\xfe",
                ]
            )
        )
    elif kind == "import":
        fault["href"] = draw(st.one_of(refs, st.sampled_from([None, ENT, "", MOD])))
    return fault


def _disclosure_files(fault: dict) -> tuple[str, dict[str, str | bytes]]:
    """The bundle for ``fault``: returns (faulty module, files)."""
    kind = fault["kind"]
    faulty = _LIM if fault["via_import"] else CAT
    decls = [_ent_decl("x")]
    body = _DISC_BODY
    ent = _ns_ent("<enum value='a'/>")
    doctype_prefix = "<!DOCTYPE METASCHEMA ["
    if kind == "system":
        decls = [_ent_decl("x", fault["ref"])]
    elif kind == "param":
        decls.insert(0, f'<!ENTITY % p SYSTEM "./{ENT}">')
    elif kind == "nested":
        decls.append(_ent_decl("y", fault["ref"]))
        ent = _ns_ent("<enum value='a'/>&y;")
    elif kind == "entity-in-entity":
        decls.append('<!ENTITY a "&#38;x;">')
    elif kind == "unparsed":
        decls += ['<!NOTATION n SYSTEM "n">', '<!ENTITY u SYSTEM "u.bin" NDATA n>']
    elif kind == "undeclared":
        body += "<remarks>&zz;</remarks>"
        if fault["external_dtd"]:
            doctype_prefix = '<!DOCTYPE METASCHEMA SYSTEM "x.dtd" ['
    elif kind == "depth":
        body += _DEEP
    elif kind == "import":
        href = fault["href"]
        body = ("<import/>" if href is None else _imports(href)) + body
    elif kind == "occurs":
        body += '<define-assembly name="z"><model><field ref="f" min-occurs="x"/></model>'
        body += "</define-assembly>"
    doctype = doctype_prefix + "\n" + "\n".join(decls) + "\n]>"
    module = _module(body + _DISC_PAD, doctype)
    ent_bytes = (ent + _DISC_PAD).encode()
    if kind == "malformed":
        content = fault["content"]
        if isinstance(content, bytes):
            ent_bytes = content + _DISC_PAD.encode()  # an undecodable module is the same case
        elif fault["file"] == "module":
            module = _module(content + _DISC_PAD, doctype)
        else:
            ent_bytes = (content + _DISC_PAD).encode()
    files: dict[str, str | bytes] = {
        faulty: module,
        ENT: ent_bytes,
        _UNLISTED: _ns_ent("<enum value='u'/>"),
        PROF: _module('<define-flag name="p"/>'),
    }
    if fault["via_import"]:
        files[CAT] = _module(_imports(_LIM))
    return faulty, files


def _client_messages(root: Path) -> tuple[list[str], list[mi.LoadError], str | None]:
    """Every Client_Facing_Message the index and ``get_allowed_values`` produce for ``root``.

    Returns (messages, load errors, bundle-level load message or None).
    """
    from mcp_server_for_oscal.tools import get_allowed_values as gav

    messages: list[str] = []
    errors: list[mi.LoadError] = []
    bundle_error: str | None = None
    try:
        index = mi.MetaschemaIndex.load(root)
    except mi.MetaschemaLoadError as err:
        bundle_error = str(err)
        messages.append(bundle_error)
    else:
        errors = [e for errs in index.load_errors.values() for e in errs]
        messages += [text for e in errors for text in (e.module, e.subject, e.reason)]
        scopes = [lambda m=m: index.scope(m) for m in OSCALModelType]
        for get_scope in [*scopes, index.all_modules]:
            try:
                get_scope()
            except mi.MetaschemaLoadError as err:
                messages.append(str(err))
        for rel in index.source_files():
            try:
                index.source_lines(rel)
            except mi.MetaschemaLoadError as err:
                messages.append(str(err))

    with (
        mock.patch.object(gav, "get_index", side_effect=lambda: mi.MetaschemaIndex.load(root)),
        mock.patch.object(gav, "try_notify_client_error") as notify,
    ):
        for model in OSCALModelType:
            try:
                gav.get_allowed_values(model=model.value, target="t")
            except ValueError as err:  # MetaschemaLoadError is a ValueError
                messages.append(str(err))
    messages += [c.args[0] if c.args else c.kwargs["msg"] for c in notify.call_args_list]
    return messages, errors, bundle_error


_FORBIDDEN_TEXT = ("Traceback", "Errno", "Error(", " object at 0x", "Path(")
_BUNDLE_PREFIX = "OSCAL Metaschema for the bundle failed to load"


class TestErrorDisclosureProperty:
    """Property 24: Client-facing errors never disclose host paths.

    **Validates: Requirements 4.6, 4.8, 4.11, 5.13**
    """

    @settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(fault=_disclosure_faults())
    def test_client_messages_carry_no_host_path(self, fault: dict):
        kind = fault["kind"]
        event(kind)
        faulty, files = _disclosure_files(fault)
        with tempfile.TemporaryDirectory(prefix="HOSTPATH-") as tmp_name:
            tmp = Path(tmp_name)
            marker = tmp.name
            root = tmp / "bundle"
            root.mkdir()
            (tmp / "outside.ent").write_text(_ns_ent(f"<enum value='{LEAKED}'/>"), "utf-8")
            _write_bundle(root, files, unlisted=frozenset({_UNLISTED}))

            patches: list[contextlib.AbstractContextManager] = []
            if kind == "manifest":
                path = root / fault["file"]
                if fault["content"] is None:
                    path.unlink()
                else:
                    path.write_bytes(fault["content"])
            elif kind == "size":
                rel = faulty if fault["file"] == "module" else ENT
                limit = (root / rel).stat().st_size - 1
                patches.append(mock.patch.object(mi, "MAX_FILE_BYTES", limit))
            elif kind == "depth":
                patches.append(mock.patch.object(mi, "MAX_DEPTH", fault["max_depth"]))
            elif kind == "unreadable":
                rel = {
                    "module": faulty,
                    "entity": ENT,
                    "manifest": mi.MANIFEST,
                    "shared-manifest": f"{mi.SHARED_DIR}/{mi.MANIFEST}",
                }[fault["file"]]
                target = (root / rel).resolve()
                real_open = builtins.open

                def failing_open(file, *args, _target=target, **kwargs):
                    if isinstance(file, str | Path) and Path(file).resolve() == _target:
                        raise _os_error(fault["error"], file)
                    return real_open(file, *args, **kwargs)

                patches.append(mock.patch.object(builtins, "open", failing_open))

            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                messages, errors, bundle_error = _client_messages(root)

            forbidden = {str(tmp), str(tmp.resolve()), tmp.as_uri(), tmp.resolve().as_uri()}
            forbidden.add(marker)
            assert messages
            for message in messages:
                for text in (*forbidden, *_FORBIDDEN_TEXT):
                    assert text not in message, (text, message)

            # Each failure is named by its bundle-relative file.
            if bundle_error is not None:
                event("bundle-level")
                assert f"{mi.BUNDLE_DIR.name}/" in bundle_error
            else:
                assert errors, fault
                failing = {e.module for e in errors}
                assert faulty in failing, (fault, errors)
                bundle = [m for m in messages if m.startswith(_BUNDLE_PREFIX)]
                assert bundle
                assert all(f"{e.module}: " in m for m in bundle for e in errors), bundle
