"""Provenance tests for ``OscalStore`` ingestion (Origin, Relative_Name, Doc_ID).

One class per property of the search-fetch-pairs design:

- ``TestIngestionProvenanceProperty``: Property 1 (Origin and Relative_Name per row)
- ``TestDocIdStabilityProperty``: Property 2 (Doc_IDs depend only on Origin and
  Relative_Name)

Unit tests: ``TestScanDirectoryOrigin`` (Origin validation, ``count_documents``) and
``TestReindexFtsCleanup`` (FTS cleanup on re-index and on a changed uuid/Doc_ID).
"""

import io
import json
import shutil
import sqlite3
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools.oscal_store import (
    DOC_ID_NAMESPACE,
    ORIGIN_BUNDLED,
    ORIGIN_USER,
    ORIGINS,
    Origin,
    OscalStore,
    doc_id,
    relative_name,
    zip_member_name,
)

from ..fixture_store import load_sample_cdef

# Lowercase ASCII only: macOS file systems are case-insensitive, and Unicode names are
# Property 2's concern.
_SEGMENT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6)
_DIRS = st.lists(_SEGMENT, max_size=2)
_KINDS = ("json", "zip", "md")


@dataclass(frozen=True)
class _FileSpec:
    """One file in a generated tree: kind, parent directories, and stem."""

    kind: str
    dirs: tuple[str, ...]
    stem: str

    @property
    def rel(self) -> str:
        return "/".join((*self.dirs, f"{self.stem}.{self.kind}"))


_FILE_SPECS = st.builds(
    _FileSpec,
    kind=st.sampled_from(_KINDS),
    dirs=_DIRS.map(tuple),
    stem=_SEGMENT,
)
_TREES = st.lists(_FILE_SPECS, min_size=1, max_size=5, unique_by=lambda f: f.rel)
# Each scan: (index of the Scan_Root copy, Origin)
_SCANS = st.lists(
    st.tuples(st.integers(min_value=0, max_value=1), st.sampled_from(ORIGINS)),
    min_size=1,
    max_size=4,
)

_ZIP_MEMBERS = ("a.json", "nested/b.json")


def _cdef_json(key: str) -> str:
    """Return the sample cdef as JSON with a document UUID and title unique to ``key``."""
    cdef = load_sample_cdef()
    root = cdef["component-definition"]
    root["uuid"] = str(uuid5(NAMESPACE_URL, f"provenance-property-1:{key}"))
    root["metadata"]["title"] = f"Provenance {key}"
    return json.dumps(cdef)


def _write_tree(root: Path, tree: list[_FileSpec]) -> dict[str, str]:
    """Write ``tree`` under ``root``; return ``{stored file_path: expected Relative_Name}``.

    Each JSON file and zip member gets its own document UUID (keyed by its Relative_Name),
    so copies of the tree under two roots hold the same documents.
    """
    expected: dict[str, str] = {}
    for spec in tree:
        path = root.joinpath(*spec.dirs, f"{spec.stem}.{spec.kind}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if spec.kind == "json":
            path.write_text(_cdef_json(spec.rel), encoding="utf-8")
            expected[str(path)] = spec.rel
        elif spec.kind == "md":
            path.write_text(f"# Doc {spec.rel}\n\nBody of {spec.rel}.\n", encoding="utf-8")
            expected[str(path)] = spec.rel
        else:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                for member in _ZIP_MEMBERS:
                    zf.writestr(member, _cdef_json(f"{spec.rel}/{member}"))
            path.write_bytes(buf.getvalue())
            for member in _ZIP_MEMBERS:
                expected[path.joinpath(member).as_posix()] = f"{spec.rel}/{member}"
    return expected


def _rows(db_path: Path) -> list[sqlite3.Row]:
    """Read every ``documents`` row through a separate connection to the store's DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT file_path, origin, relative_name FROM documents").fetchall()
    finally:
        conn.close()


class TestIngestionProvenanceProperty:
    """Feature: search-fetch-pairs, Property 1: Every ingested row records the scan's
    Origin and Relative_Name.

    *For any* generated tree of JSON, zip, and markdown files and any sequence of
    ``scan_directory`` calls (each with a random Origin and, for repeated files, possibly
    a different Scan_Root location), after the calls every ``documents`` row has Origin in
    {``bundled``, ``user``}, and each row's Origin and Relative_Name equal those of the
    most recent call that saw its file, including calls that found the content hash
    unchanged. Selecting by Origin returns the same rows after the source files are moved
    or deleted.

    **Validates: Requirements 1.1, 1.2, 1.5, 1.6**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(tree=_TREES, scans=_SCANS)
    def test_rows_record_latest_scan_provenance(
        self, tree: list[_FileSpec], scans: list[tuple[int, Origin]]
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            roots = [base / "root0" / "docs", base / "elsewhere" / "root1"]
            expected_by_root = [_write_tree(root, tree) for root in roots]
            db_path = base / "db" / "store.db"
            db_path.parent.mkdir()
            store = OscalStore(db_path=str(db_path), cache_size=4, seed_from_bundled=False)
            try:
                for root_idx, origin in scans:
                    store.scan_directory(roots[root_idx], origin=origin)

                rows = _rows(db_path)
                # Later scans of a root overwrite earlier ones
                last_origin = dict(scans)

                # 1.1: exactly one recorded Origin per row
                assert rows
                assert all(r["origin"] in ORIGINS for r in rows)

                # 1.2 / 1.5: each row carries the provenance of the latest scan of its file
                for r in rows:
                    owners = [i for i, exp in enumerate(expected_by_root) if r["file_path"] in exp]
                    assert len(owners) == 1, r["file_path"]
                    root_idx = owners[0]
                    assert root_idx in last_origin, r["file_path"]
                    assert r["origin"] == last_origin[root_idx], r["file_path"]
                    assert r["relative_name"] == expected_by_root[root_idx][r["file_path"]]

                # Every file seen by the final scan has a row at its path
                final_paths = set(expected_by_root[scans[-1][0]])
                assert final_paths <= {r["file_path"] for r in rows}

                # 1.6: selection by the recorded Origin survives moving and deleting sources
                by_origin = {o: sum(r["origin"] == o for r in rows) for o in ORIGINS}
                assert {o: store.count_documents(origin=o) for o in ORIGINS} == by_origin
                shutil.move(base / "root0", base / "moved")
                shutil.rmtree(base / "elsewhere")
                assert {o: store.count_documents(origin=o) for o in ORIGINS} == by_origin
                assert sorted(map(tuple, _rows(db_path))) == sorted(map(tuple, rows))
            finally:
                store.close()


# Property 2 strategies. Characters with distinct NFC and NFD forms, plus lowercase ASCII
# (lowercase only: macOS file systems are case-insensitive).
_FS_SEGMENT = st.text(alphabet="abcxyz019éüñåçöΩ", min_size=1, max_size=6)
# Pure-function segments: any Unicode except separators, surrogates, controls, '.'
# (so no "." or ".." parts), and characters a Windows path parser treats specially.
_PURE_SEGMENT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc"),
        blacklist_characters='/\\.:<>"|?*',
    ),
    min_size=1,
    max_size=8,
)
_NormForm = Literal["NFC", "NFD"]
_NORM_FORMS: st.SearchStrategy[_NormForm] = st.sampled_from(("NFC", "NFD"))


def _expected_doc_id(origin: str, rel: str) -> str:
    """Return the design's Doc_ID formula, computed independently of ``doc_id``."""
    return str(uuid5(DOC_ID_NAMESPACE, f"{origin}:{unicodedata.normalize('NFC', rel)}"))


def _doc_ids(db_path: Path) -> list[str]:
    """Read the Doc_ID of every documentation row through a separate connection."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT uuid FROM documents WHERE model_type = 'documentation'")
        return [r[0] for r in rows.fetchall()]
    finally:
        conn.close()


class TestDocIdStabilityProperty:
    """Feature: search-fetch-pairs, Property 2: Doc_IDs depend only on Origin and
    Relative_Name.

    *For any* Origin, relative path (random Unicode segments in NFC or NFD form), and two
    different Scan_Root locations, ingesting the markdown file under either root yields
    the same Doc_ID, equal to ``uuid5(DOC_ID_NAMESPACE, origin + ":" + NFC(relative
    path))``. *For any* Relative_Name, the ``bundled`` and ``user`` Doc_IDs differ.

    **Validates: Requirements 2.1, 2.2, 2.3**
    """

    @settings(max_examples=100, deadline=None)
    @given(
        origin=st.sampled_from(ORIGINS),
        segments=st.lists(_PURE_SEGMENT, min_size=1, max_size=4),
        roots=st.lists(st.lists(_PURE_SEGMENT, max_size=3), min_size=2, max_size=2),
        form=_NORM_FORMS,
    )
    def test_relative_name_and_doc_id_are_root_and_form_independent(
        self, origin: Origin, segments: list[str], roots: list[list[str]], form: _NormForm
    ) -> None:
        expected_rel = unicodedata.normalize("NFC", "/".join(segments))
        written = [unicodedata.normalize(form, s) for s in segments]
        anchor = Path.cwd()
        rels = []
        for root_segs in roots:
            root = anchor.joinpath("scan-root", *root_segs)
            rels.append(relative_name(root.joinpath(*written), root))

        # 2.2: same Relative_Name (NFC, '/'-joined) wherever the Scan_Root is
        assert rels == [expected_rel, expected_rel]
        # 2.1: the Doc_ID is the UUID v5 of Origin and Relative_Name, path-free
        assert doc_id(origin, rels[0]) == _expected_doc_id(origin, expected_rel)
        assert doc_id(origin, rels[0]) == doc_id(origin, rels[1])
        # 2.3: same Relative_Name, different Origins -> different Doc_IDs
        assert doc_id(ORIGIN_BUNDLED, expected_rel) != doc_id(ORIGIN_USER, expected_rel)

    @settings(max_examples=100, deadline=None)
    @given(
        origin=st.sampled_from(ORIGINS),
        zip_segments=st.lists(_PURE_SEGMENT, min_size=1, max_size=3),
        member_segments=st.lists(_PURE_SEGMENT, min_size=1, max_size=3),
        form=_NORM_FORMS,
    )
    def test_zip_member_name_is_separator_and_form_independent(
        self,
        origin: Origin,
        zip_segments: list[str],
        member_segments: list[str],
        form: _NormForm,
    ) -> None:
        zip_rel = unicodedata.normalize("NFC", "/".join(zip_segments))
        member = [unicodedata.normalize(form, s) for s in member_segments]
        posix = zip_member_name(zip_rel, "/".join(member))
        windows = zip_member_name(zip_rel, "\\".join(member))

        expected = unicodedata.normalize("NFC", "/".join((*zip_segments, *member_segments)))
        assert posix == windows == expected
        assert doc_id(origin, posix) == doc_id(origin, windows)
        assert doc_id(origin, posix) == _expected_doc_id(origin, expected)

    @settings(
        max_examples=50,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        origin=st.sampled_from(ORIGINS),
        segments=st.lists(_FS_SEGMENT, min_size=1, max_size=3),
        form=_NORM_FORMS,
    )
    def test_ingested_doc_id_ignores_scan_root_location(
        self, origin: Origin, segments: list[str], form: _NormForm
    ) -> None:
        rel = "/".join(segments) + ".md"
        expected = _expected_doc_id(origin, rel)
        written = [unicodedata.normalize(form, s) for s in segments]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            roots = [base / "root0" / "docs", base / "elsewhere" / "deeper" / "root1"]
            ingested = []
            for i, root in enumerate(roots):
                md = root.joinpath(*written[:-1], f"{written[-1]}.md")
                md.parent.mkdir(parents=True, exist_ok=True)
                md.write_text("# Doc\n\nBody.\n", encoding="utf-8")
                db_path = base / f"db{i}" / "store.db"
                db_path.parent.mkdir()
                store = OscalStore(db_path=str(db_path), cache_size=4, seed_from_bundled=False)
                try:
                    store.scan_directory(root, origin=origin)
                finally:
                    store.close()
                ingested.append(_doc_ids(db_path))

        # 2.1 / 2.2: one Doc_ID per root, equal across roots and to the formula
        assert ingested == [[expected], [expected]]
        # The Doc_ID does not encode either absolute root
        other = ORIGIN_USER if origin == ORIGIN_BUNDLED else ORIGIN_BUNDLED
        assert expected != _expected_doc_id(other, rel)
        assert all(expected != _expected_doc_id(origin, str(r / rel)) for r in roots)


# ---------------------------------------------------------------------------
# Unit tests (task 5.7)
# ---------------------------------------------------------------------------


def _open_store(tmp_path: Path) -> OscalStore:
    db_path = tmp_path / "db" / "store.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return OscalStore(db_path=str(db_path), cache_size=4, seed_from_bundled=False)


def _fts_keys(store: OscalStore) -> list[tuple[str, int]]:
    """Return ``(entity_type, entity_id)`` of every FTS row."""
    rows = store._conn.execute("SELECT entity_type, entity_id FROM fts_index").fetchall()
    return [(r["entity_type"], int(r["entity_id"])) for r in rows]


def _assert_fts_clean(store: OscalStore) -> None:
    """Assert no FTS row is duplicated or points at a missing child element or document."""
    keys = _fts_keys(store)
    assert len(keys) == len(set(keys)), keys
    child_ids = {r["id"] for r in store._conn.execute("SELECT id FROM child_elements")}
    doc_ids = {r["id"] for r in store._conn.execute("SELECT id FROM documents")}
    for entity_type, entity_id in keys:
        assert entity_id in (child_ids if entity_type == "child_element" else doc_ids)


def _write_cdef(path: Path, key: str) -> str:
    """Write the sample cdef with a document UUID derived from ``key``; return the UUID."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_cdef_json(key), encoding="utf-8")
    return json.loads(path.read_text(encoding="utf-8"))["component-definition"]["uuid"]


class TestScanDirectoryOrigin:
    """``scan_directory`` Origin validation and ``count_documents``.

    Requirements: 1.2
    """

    def test_unknown_origin_raises_before_reading_files(self, tmp_path: Path) -> None:
        root = tmp_path / "docs"
        root.mkdir()
        (root / "intro.md").write_text("# Intro\n\nBody.\n", encoding="utf-8")
        store = _open_store(tmp_path)
        try:
            for bad in ("kb", "", "Bundled", "user "):
                with pytest.raises(ValueError, match="Unknown origin"):
                    store.scan_directory(root, origin=bad)  # type: ignore[arg-type]
            # Rejected even when the directory does not exist (checked first)
            with pytest.raises(ValueError, match="Unknown origin"):
                store.scan_directory(tmp_path / "missing", origin="kb")  # type: ignore[arg-type]
            assert store._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        finally:
            store.close()

    def test_count_documents_by_origin(self, tmp_path: Path) -> None:
        bundled_root = tmp_path / "bundled"
        user_root = tmp_path / "user"
        _write_cdef(bundled_root / "a.json", "count:a")
        (bundled_root / "guide").mkdir()
        (bundled_root / "guide" / "intro.md").write_text("# Intro\n\nBody.\n", encoding="utf-8")
        _write_tree(user_root, [_FileSpec("zip", (), "pack"), _FileSpec("md", ("n",), "note")])
        store = _open_store(tmp_path)
        try:
            assert store.count_documents(origin=ORIGIN_BUNDLED) == 0
            assert store.count_documents(origin=ORIGIN_USER) == 0
            store.scan_directory(bundled_root, origin=ORIGIN_BUNDLED)
            store.scan_directory(user_root)  # default Origin is user
            assert store.count_documents(origin=ORIGIN_BUNDLED) == 2
            # Two zip members and one markdown file
            assert store.count_documents(origin=ORIGIN_USER) == 3
            # A rescan under the other Origin moves the rows
            store.scan_directory(user_root, origin=ORIGIN_BUNDLED)
            assert store.count_documents(origin=ORIGIN_BUNDLED) == 5
            assert store.count_documents(origin=ORIGIN_USER) == 0
        finally:
            store.close()


class TestReindexFtsCleanup:
    """Re-indexing and re-ingesting leave no duplicate or orphan FTS rows.

    Requirements: 14.3
    """

    def test_reindexing_leaves_one_fts_row_per_entity(self, tmp_path: Path) -> None:
        root = tmp_path / "docs"
        cdef_path = root / "cdef.json"
        uuid = _write_cdef(cdef_path, "reindex")
        store = _open_store(tmp_path)
        try:
            store.scan_directory(root)
            row_id = store._conn.execute(
                "SELECT id FROM documents WHERE uuid = ?", (uuid,)
            ).fetchone()["id"]
            for _ in range(3):
                store._conn.execute("UPDATE documents SET indexed = 0 WHERE id = ?", (row_id,))
                store._conn.commit()
                store._ensure_indexed(row_id)
                _assert_fts_clean(store)
            n_children = store._conn.execute("SELECT COUNT(*) FROM child_elements").fetchone()[0]
            assert n_children > 0
            assert len(_fts_keys(store)) == n_children + 1  # children plus the document

            # Changed content under the same content UUID: re-ingest, then re-index
            cdef = json.loads(cdef_path.read_text(encoding="utf-8"))
            cdef["component-definition"]["metadata"]["title"] = "Retitled"
            cdef_path.write_text(json.dumps(cdef), encoding="utf-8")
            assert store.scan_directory(root) == 1
            store._ensure_indexed(row_id)
            _assert_fts_clean(store)
            assert ("document", row_id) in _fts_keys(store)
        finally:
            store.close()

    @pytest.mark.parametrize("kind", ["json", "md"])
    def test_changed_doc_id_replaces_row_at_same_file_path(self, tmp_path: Path, kind: str) -> None:
        """A file whose uuid/Doc_ID changes on rescan keeps exactly one row at its path."""
        root = tmp_path / "docs"
        path = root / f"file.{kind}"
        root.mkdir()
        if kind == "json":
            _write_cdef(path, "conflict:v1")
        else:
            path.write_text("# V1\n\nFirst body.\n", encoding="utf-8")
        store = _open_store(tmp_path)
        try:
            store.scan_directory(root, origin=ORIGIN_USER)
            old = store._conn.execute(
                "SELECT id, uuid FROM documents WHERE file_path = ?", (str(path),)
            ).fetchone()
            store._ensure_indexed(old["id"])
            old_child_ids = [
                r["id"]
                for r in store._conn.execute(
                    "SELECT id FROM child_elements WHERE parent_doc_id = ?", (old["id"],)
                )
            ]
            if kind == "json":
                assert old_child_ids
                new_uuid = _write_cdef(path, "conflict:v2")
            else:
                path.write_text("# V2\n\nSecond body.\n", encoding="utf-8")
                new_uuid = doc_id(ORIGIN_BUNDLED, "file.md")

            # Different content and a different Origin
            assert store.scan_directory(root, origin=ORIGIN_BUNDLED) == 1

            rows = store._conn.execute(
                "SELECT id, uuid, origin FROM documents WHERE file_path = ?", (str(path),)
            ).fetchall()
            assert [(r["uuid"], r["origin"]) for r in rows] == [(new_uuid, ORIGIN_BUNDLED)]
            assert new_uuid != old["uuid"]
            assert store._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
            stale_children = store._conn.execute(
                "SELECT COUNT(*) FROM child_elements WHERE parent_doc_id = ?", (old["id"],)
            ).fetchone()[0]
            assert stale_children == 0
            keys = _fts_keys(store)
            assert all(k[1] != old["id"] for k in keys if k[0] != "child_element")
            assert all(("child_element", cid) not in keys for cid in old_child_ids)
            _assert_fts_clean(store)
        finally:
            store.close()
