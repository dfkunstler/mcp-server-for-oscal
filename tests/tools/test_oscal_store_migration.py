"""Tests for the pre-feature schema upgrade in ``OscalStore._migrate``.

Legacy DBs are built with the pre-feature DDL (copied literally from ``main``) and raw
``INSERT`` statements, never with ``OscalStore`` code, so the tests exercise the upgrade
of a DB that the old server wrote.
"""

import hashlib
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools.oscal_store import (
    ORIGIN_BUNDLED,
    ORIGINS,
    SCHEMA_VERSION,
    OscalStore,
    doc_id,
    legacy_provenance,
)

# ---------------------------------------------------------------------------
# Pre-feature DDL, copied literally from ``OscalStore._init_schema`` on ``main``
# (``git show main:src/mcp_server_for_oscal/tools/oscal_store.py``).
# ---------------------------------------------------------------------------

LEGACY_DDL: tuple[str, ...] = (
    """
                CREATE TABLE IF NOT EXISTS documents (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid         TEXT    NOT NULL UNIQUE,
                    title        TEXT    NOT NULL,
                    model_type   TEXT    NOT NULL,
                    file_path    TEXT    NOT NULL UNIQUE,
                    file_size    INTEGER NOT NULL,
                    file_mtime   REAL    NOT NULL,
                    content_hash TEXT,
                    raw_json     TEXT    NOT NULL,
                    indexed      INTEGER NOT NULL DEFAULT 0,
                    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
                )
            """,
    "CREATE INDEX IF NOT EXISTS idx_documents_uuid ON documents(uuid)",
    "CREATE INDEX IF NOT EXISTS idx_documents_title ON documents(title COLLATE NOCASE)",
    "CREATE INDEX IF NOT EXISTS idx_documents_model_type ON documents(model_type)",
    """
                CREATE TABLE IF NOT EXISTS child_elements (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid            TEXT    NOT NULL,
                    title           TEXT    NOT NULL,
                    element_type    TEXT    NOT NULL,
                    parent_doc_id   INTEGER NOT NULL
                                    REFERENCES documents(id) ON DELETE CASCADE,
                    description     TEXT,
                    raw_json        TEXT,
                    UNIQUE(uuid, parent_doc_id)
                )
            """,
    "CREATE INDEX IF NOT EXISTS idx_child_uuid ON child_elements(uuid)",
    "CREATE INDEX IF NOT EXISTS idx_child_title ON child_elements(title COLLATE NOCASE)",
    "CREATE INDEX IF NOT EXISTS idx_child_type ON child_elements(element_type)",
    "CREATE INDEX IF NOT EXISTS idx_child_parent ON child_elements(parent_doc_id)",
    """
                CREATE VIRTUAL TABLE IF NOT EXISTS fts_index USING fts5(
                    entity_type,
                    entity_id,
                    title,
                    description,
                    model_type
                )
            """,
)

# Version-1 DDL (early builds of this feature): the pre-feature tables plus the provenance
# columns and their index, with the five-column FTS table and ``user_version`` 1.
V1_DDL: tuple[str, ...] = (
    """
                CREATE TABLE IF NOT EXISTS documents (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid         TEXT    NOT NULL UNIQUE,
                    title        TEXT    NOT NULL,
                    model_type   TEXT    NOT NULL,
                    file_path    TEXT    NOT NULL UNIQUE,
                    file_size    INTEGER NOT NULL,
                    file_mtime   REAL    NOT NULL,
                    content_hash TEXT,
                    raw_json     TEXT    NOT NULL,
                    indexed      INTEGER NOT NULL DEFAULT 0,
                    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                    origin        TEXT NOT NULL DEFAULT '',
                    relative_name TEXT NOT NULL DEFAULT ''
                )
            """,
    *LEGACY_DDL[1:],
    "CREATE INDEX IF NOT EXISTS idx_documents_origin ON documents(origin, model_type)",
    "PRAGMA user_version = 1",
)

# Column names of the current (version 2) FTS table (D10).
FTS_COLUMNS_V2 = ["entity_type", "entity_id", "title", "description", "model_type", "element_id"]

# ---------------------------------------------------------------------------
# Legacy row model and generators
# ---------------------------------------------------------------------------

# Scan roots as the old server stored them (absolute, some Windows-style). Bundled roots
# contain a build marker (D4); user roots do not.
DOC_ROOTS: tuple[str, ...] = (
    "/Users/dev/repo/data/oscal_docs/",
    "/opt/venv/lib/python3.13/site-packages/mcp_server_for_oscal/oscal_docs/",
    "C:\\work\\repo\\data\\oscal_docs\\",
    "/home/alice/docs/",
    "/home/bob/notes/",
    "C:\\Users\\carol\\docs\\",
)
MODEL_ROOTS: tuple[str, ...] = (
    "/Users/dev/repo/data/component_definitions/",
    "/opt/venv/lib/python3.13/site-packages/mcp_server_for_oscal/component_definitions/",
    "/home/alice/oscal/",
    "/home/alice/oscal/aws.zip/",
    "C:\\Users\\carol\\oscal\\",
)
# A small vocabulary so that Relative_Names (and user basenames) collide often.
SEGMENTS: tuple[str, ...] = ("a", "guide", "café", "models")
STEMS: tuple[str, ...] = ("intro", "ssp", "readme")
MODEL_TYPES: tuple[str, ...] = (
    "catalog",
    "component-definition",
    "profile",
    "system-security-plan",
    "mapping-collection",
)
ORPHAN_ID_BASE = 100_000  # above every generated row id


@dataclass
class LegacyChild:
    uuid: str
    element_type: str
    fts_copies: int  # FTS rows pointing at this child (duplicates allowed)
    row_id: int = 0


@dataclass
class LegacyDoc:
    model_type: str
    file_path: str
    uuid: str
    indexed: int
    has_fts: bool
    children: list[LegacyChild] = field(default_factory=list)
    row_id: int = 0


@dataclass
class LegacyDb:
    docs: list[LegacyDoc]
    orphans: list[tuple[str, int]]  # (entity_type, offset above ORPHAN_ID_BASE)


def _join(root: str, parts: list[str]) -> str:
    sep = "\\" if "\\" in root else "/"
    return root + sep.join(parts)


@st.composite
def legacy_dbs(draw) -> LegacyDb:
    """Generate a pre-feature DB: docs of every kind, children, FTS rows, and orphans."""
    n_docs = draw(st.integers(min_value=0, max_value=12))
    docs: list[LegacyDoc] = []
    seen_paths: set[str] = set()
    model_counter = 0
    for _ in range(n_docs):
        is_doc = draw(st.booleans())
        segs = draw(st.lists(st.sampled_from(SEGMENTS), max_size=2))
        stem = draw(st.sampled_from(STEMS))
        if is_doc:
            root = draw(st.sampled_from(DOC_ROOTS))
            file_path = _join(root, [*segs, f"{stem}.md"])
            model_type = "documentation"
            # Pre-feature Doc_ID: UUID v5 of the absolute path.
            uuid = str(uuid5(NAMESPACE_URL, file_path))
        else:
            root = draw(st.sampled_from(MODEL_ROOTS))
            file_path = _join(root, [*segs, f"{stem}.json"])
            model_type = draw(st.sampled_from(MODEL_TYPES))
            model_counter += 1
            uuid = str(uuid5(NAMESPACE_URL, f"oscal-content-{model_counter}"))
        if file_path in seen_paths:  # file_path is UNIQUE in the legacy schema
            continue
        seen_paths.add(file_path)
        children: list[LegacyChild] = []
        if not is_doc:
            for i in range(draw(st.integers(min_value=0, max_value=3))):
                children.append(
                    LegacyChild(
                        uuid=f"el-{i}",
                        element_type=draw(st.sampled_from(("control", "group", "component"))),
                        fts_copies=draw(st.integers(min_value=0, max_value=2)),
                    )
                )
        docs.append(
            LegacyDoc(
                model_type=model_type,
                file_path=file_path,
                uuid=uuid,
                indexed=draw(st.sampled_from((0, 1))) if is_doc else 1,
                has_fts=draw(st.booleans()),
                children=children,
            )
        )
    orphans = draw(
        st.lists(
            st.tuples(
                st.sampled_from(("child_element", "document", "documentation")),
                st.integers(min_value=0, max_value=50),
            ),
            max_size=6,
        )
    )
    return LegacyDb(docs=docs, orphans=orphans)


def v1_spec(spec: LegacyDb) -> LegacyDb:
    """Return ``spec`` as a version-1 DB holds it: documentation rows keyed by Doc_ID.

    Documentation rows whose Doc_ID repeats an earlier row's are dropped (the version-1
    upgrade or scan never left two rows with one Doc_ID).
    """
    docs: list[LegacyDoc] = []
    seen: set[str] = set()
    for d in spec.docs:
        if d.model_type == "documentation":
            target = doc_id(*legacy_provenance(d.file_path))
            if target in seen:
                continue
            seen.add(target)
            d.uuid = target
        docs.append(d)
    return LegacyDb(docs=docs, orphans=spec.orphans)


def build_legacy_db(path: Path, spec: LegacyDb, version: int = 0) -> None:
    """Write ``spec`` to ``path`` with the version-0 (or version-1) DDL and raw INSERTs.

    Version-1 rows carry Origin and Relative_Name from ``legacy_provenance``; pass a
    spec from ``v1_spec`` so documentation UUIDs are Doc_IDs.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        for stmt in V1_DDL if version == 1 else LEGACY_DDL:
            conn.execute(stmt)
        for d in spec.docs:
            cur = conn.execute(
                "INSERT INTO documents (uuid, title, model_type, file_path, file_size, "
                "file_mtime, content_hash, raw_json, indexed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (d.uuid, "Title", d.model_type, d.file_path, 2, 1.0, "h", "{}", d.indexed),
            )
            d.row_id = cur.lastrowid or 0
            if version == 1:
                conn.execute(
                    "UPDATE documents SET origin = ?, relative_name = ? WHERE id = ?",
                    (*legacy_provenance(d.file_path), d.row_id),
                )
            fts_type = "documentation" if d.model_type == "documentation" else "document"
            if d.has_fts:
                conn.execute(
                    "INSERT INTO fts_index (entity_type, entity_id, title, description, "
                    "model_type) VALUES (?, ?, ?, ?, ?)",
                    (fts_type, str(d.row_id), "Title", "body", d.model_type),
                )
            for c in d.children:
                cur = conn.execute(
                    "INSERT INTO child_elements (uuid, title, element_type, parent_doc_id, "
                    "description, raw_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (c.uuid, "Child", c.element_type, d.row_id, "desc", "{}"),
                )
                c.row_id = cur.lastrowid or 0
                for _ in range(c.fts_copies):
                    conn.execute(
                        "INSERT INTO fts_index (entity_type, entity_id, title, description, "
                        "model_type) VALUES (?, ?, ?, ?, ?)",
                        ("child_element", str(c.row_id), "Child", "desc", d.model_type),
                    )
        for entity_type, offset in spec.orphans:
            conn.execute(
                "INSERT INTO fts_index (entity_type, entity_id, title, description, model_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (entity_type, str(ORPHAN_ID_BASE + offset), "Gone", "", "x"),
            )
        conn.commit()
    finally:
        conn.close()


def dump_db(path: Path) -> tuple:
    """Return a full, ordered dump of the schema, tables, and ``user_version``."""
    conn = sqlite3.connect(path)
    try:
        return (
            conn.execute("PRAGMA user_version").fetchone()[0],
            conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(),
            conn.execute("SELECT * FROM documents ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM child_elements ORDER BY id").fetchall(),
            conn.execute("SELECT rowid, * FROM fts_index ORDER BY rowid").fetchall(),
        )
    finally:
        conn.close()


def fts_columns(conn: sqlite3.Connection) -> list[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(fts_index)")]


def expected_element_ids(spec: LegacyDb) -> dict[tuple[str, str], str]:
    """Map ``(entity_type, entity_id)`` of live entities to their expected ``element_id``."""
    expected: dict[tuple[str, str], str] = {}
    for d in spec.docs:
        if d.model_type == "documentation":
            expected["documentation", str(d.row_id)] = ""
        else:
            expected["document", str(d.row_id)] = d.uuid
        for c in d.children:
            expected["child_element", str(c.row_id)] = c.uuid
    return expected


def open_and_close(path: Path) -> None:
    store = OscalStore(db_path=str(path), seed_from_bundled=False)
    store.close()


def surviving_doc_ids(spec: LegacyDb) -> set[int]:
    """Row ids expected to survive: all OSCAL docs, and the lowest id per Doc_ID."""
    keep: set[int] = set()
    seen_targets: set[str] = set()
    for d in sorted(spec.docs, key=lambda x: x.row_id):
        if d.model_type != "documentation":
            keep.add(d.row_id)
            continue
        target = doc_id(*legacy_provenance(d.file_path))
        if target not in seen_targets:
            seen_targets.add(target)
            keep.add(d.row_id)
    return keep


# ---------------------------------------------------------------------------
# Property 19
# ---------------------------------------------------------------------------


class TestPreFeatureUpgradeProperty:
    """Property-based tests for the in-place upgrade of pre-feature DBs."""

    @given(spec=legacy_dbs())
    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_pre_feature_dbs_upgrade_in_place_once(self, spec: LegacyDb) -> None:
        """Feature: search-fetch-pairs, Property 19: Pre-feature DBs upgrade in place, once.

        **Validates: Requirements 2.4, 14.2, 14.3**
        """
        with tempfile.TemporaryDirectory(prefix="oscal_migration_") as tmp:
            db_path = Path(tmp) / "legacy.db"
            build_legacy_db(db_path, spec)
            assert dump_db(db_path)[0] == 0  # pre-feature DBs have user_version 0

            open_and_close(db_path)

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
                rows = {r["id"]: r for r in conn.execute("SELECT * FROM documents")}
                by_id = {d.row_id: d for d in spec.docs}

                # Duplicated Doc_IDs keep the lowest row id; nothing else is removed.
                assert set(rows) == surviving_doc_ids(spec)

                for row_id, row in rows.items():
                    legacy = by_id[row_id]
                    origin, rel = legacy_provenance(legacy.file_path)
                    assert row["origin"] in ORIGINS
                    assert (row["origin"], row["relative_name"]) == (origin, rel)
                    assert row["file_path"] == legacy.file_path
                    if legacy.model_type == "documentation":
                        assert row["uuid"] == doc_id(origin, rel)
                        assert row["indexed"] == legacy.indexed
                    else:
                        assert row["uuid"] == legacy.uuid
                        # Catalogs are re-extracted on next access (14.3); others keep state.
                        expected = 0 if legacy.model_type == "catalog" else legacy.indexed
                        assert row["indexed"] == expected

                doc_uuids = [r["uuid"] for r in rows.values() if r["model_type"] == "documentation"]
                assert len(doc_uuids) == len(set(doc_uuids))

                # Child rows of surviving documents are unchanged.
                child_ids = {r["id"] for r in conn.execute("SELECT id FROM child_elements")}
                expected_children = {
                    c.row_id for d in spec.docs if d.row_id in rows for c in d.children
                }
                assert child_ids == expected_children

                # No orphan or duplicate FTS rows; each live entity that had FTS rows
                # keeps exactly one.
                fts = [
                    (r["entity_type"], int(r["entity_id"]))
                    for r in conn.execute("SELECT entity_type, entity_id FROM fts_index")
                ]
                for entity_type, entity_id in fts:
                    if entity_type == "child_element":
                        assert entity_id in child_ids
                    else:
                        assert entity_id in rows
                assert len(fts) == len(set(fts))
                expected_fts = sorted(
                    [
                        (
                            "documentation" if d.model_type == "documentation" else "document",
                            d.row_id,
                        )
                        for d in spec.docs
                        if d.has_fts and d.row_id in rows
                    ]
                    + [
                        ("child_element", c.row_id)
                        for d in spec.docs
                        if d.row_id in rows
                        for c in d.children
                        if c.fts_copies > 0
                    ]
                )
                assert sorted(fts) == expected_fts

                # The FTS table has the element_id column, filled from the joins (D10).
                assert fts_columns(conn) == FTS_COLUMNS_V2
                element_ids = expected_element_ids(spec)
                for r in conn.execute("SELECT entity_type, entity_id, element_id FROM fts_index"):
                    assert r["element_id"] == element_ids[r["entity_type"], r["entity_id"]]
            finally:
                conn.close()

            # A second open changes nothing.
            before = dump_db(db_path)
            open_and_close(db_path)
            assert dump_db(db_path) == before

    @given(spec=legacy_dbs())
    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_version_1_dbs_rebuild_fts_with_element_id(self, spec: LegacyDb) -> None:
        """Feature: search-fetch-pairs, Property 19: Version-1 DBs upgrade in place, once.

        **Validates: Requirements 14.2, 14.6**
        """
        spec = v1_spec(spec)
        with tempfile.TemporaryDirectory(prefix="oscal_migration_") as tmp:
            db_path = Path(tmp) / "v1.db"
            build_legacy_db(db_path, spec, version=1)
            before = dump_db(db_path)
            assert before[0] == 1

            open_and_close(db_path)

            after = dump_db(db_path)
            assert after[0] == SCHEMA_VERSION
            # documents and child_elements are unchanged.
            assert after[2:4] == before[2:4]
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                assert fts_columns(conn) == FTS_COLUMNS_V2
                rows = conn.execute("SELECT rowid, * FROM fts_index ORDER BY rowid").fetchall()
            finally:
                conn.close()
            # Same rows (rowid and the five old columns), plus element_id from the joins;
            # orphans and documentation rows get ''.
            assert [tuple(r)[:6] for r in rows] == before[4]
            element_ids = expected_element_ids(spec)
            for r in rows:
                assert r["element_id"] == element_ids.get((r["entity_type"], r["entity_id"]), "")

            open_and_close(db_path)
            assert dump_db(db_path) == after


# ---------------------------------------------------------------------------
# Unit tests (task 5.7)
# ---------------------------------------------------------------------------

UPGRADE_FAILED_MESSAGE = "Failed to upgrade the OSCAL store schema"


class TestLegacyProvenance:
    """Examples of ``legacy_provenance`` (D4).

    Requirements: 14.2
    """

    @pytest.mark.parametrize(
        ("file_path", "expected"),
        [
            # Each bundled build marker
            ("/Users/dev/repo/data/oscal_docs/guide/intro.md", ("bundled", "guide/intro.md")),
            ("/Users/dev/repo/data/component_definitions/c.json", ("bundled", "c.json")),
            (
                "/opt/venv/site-packages/mcp_server_for_oscal/oscal_docs/a/b.md",
                ("bundled", "a/b.md"),
            ),
            (
                "/opt/venv/site-packages/mcp_server_for_oscal/component_definitions/x.json",
                ("bundled", "x.json"),
            ),
            # A relative path still matches a marker
            ("mcp_server_for_oscal/oscal_docs/z.md", ("bundled", "z.md")),
            # The last marker wins
            ("/r/data/oscal_docs/x/data/component_definitions/c.json", ("bundled", "c.json")),
            # Bundled zip members keep the zip in the Relative_Name
            (
                "/Users/dev/repo/data/component_definitions/aws.zip/nested/b.json",
                ("bundled", "aws.zip/nested/b.json"),
            ),
            # Windows separators
            ("C:\\work\\repo\\data\\oscal_docs\\guide\\intro.md", ("bundled", "guide/intro.md")),
            ("C:\\Users\\carol\\docs\\sub\\intro.md", ("user", "intro.md")),
            ("C:\\Users\\carol\\oscal\\aws.zip/nested/b.json", ("user", "aws.zip/nested/b.json")),
            # User files: basename; user zip members: <zip basename>/<member>
            ("/home/alice/docs/sub/readme.md", ("user", "readme.md")),
            ("/home/alice/oscal/aws.zip/nested/b.json", ("user", "aws.zip/nested/b.json")),
            # A marker must be a whole path segment
            ("/repo/mydata/oscal_docs/x.md", ("user", "x.md")),
            # NFC normalization
            ("/home/alice/docs/cafe\u0301.md", ("user", "caf\u00e9.md")),
        ],
    )
    def test_examples(self, file_path: str, expected: tuple[str, str]) -> None:
        assert legacy_provenance(file_path) == expected


class TestMigrationUnit:
    """Unit tests for ``OscalStore._migrate``.

    Requirements: 2.4, 4.2, 14.2, 14.3
    """

    def test_failed_upgrade_raises_fixed_message_and_leaves_db_unchanged(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "secret-location" / "legacy.db"
        db_path.parent.mkdir()
        doc = LegacyDoc(
            model_type="documentation",
            file_path="/home/alice/docs/intro.md",
            uuid=str(uuid5(NAMESPACE_URL, "/home/alice/docs/intro.md")),
            indexed=1,
            has_fts=True,
        )
        build_legacy_db(db_path, LegacyDb(docs=[doc], orphans=[("document", 1)]))
        # A real sqlite3.Error during the backfill UPDATE
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TRIGGER fail_upgrade BEFORE UPDATE ON documents "
            "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
        )
        conn.commit()
        conn.close()
        before = dump_db(db_path)

        with pytest.raises(RuntimeError) as exc_info:
            OscalStore(db_path=str(db_path), seed_from_bundled=False)

        assert str(exc_info.value) == UPGRADE_FAILED_MESSAGE
        assert str(tmp_path) not in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, sqlite3.Error)
        after = dump_db(db_path)
        assert after[0] == 0  # user_version
        assert after == before

    def test_reopening_upgraded_db_changes_nothing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy.db"
        docs = [
            LegacyDoc(
                model_type="documentation",
                file_path="/Users/dev/repo/data/oscal_docs/intro.md",
                uuid=str(uuid5(NAMESPACE_URL, "/Users/dev/repo/data/oscal_docs/intro.md")),
                indexed=1,
                has_fts=True,
            ),
            LegacyDoc(
                model_type="catalog",
                file_path="/home/alice/oscal/cat.json",
                uuid="cat-uuid",
                indexed=1,
                has_fts=True,
                children=[LegacyChild(uuid="ac-1", element_type="control", fts_copies=2)],
            ),
        ]
        build_legacy_db(db_path, LegacyDb(docs=docs, orphans=[("child_element", 3)]))
        open_and_close(db_path)
        upgraded = dump_db(db_path)
        assert upgraded[0] == SCHEMA_VERSION
        for _ in range(2):
            open_and_close(db_path)
            assert dump_db(db_path) == upgraded

    def test_version_1_db_upgrades_to_current_with_element_id(self, tmp_path: Path) -> None:
        db_path = tmp_path / "v1.db"
        spec = v1_spec(
            LegacyDb(
                docs=[
                    LegacyDoc(
                        model_type="documentation",
                        file_path="/Users/dev/repo/data/oscal_docs/intro.md",
                        uuid="",
                        indexed=1,
                        has_fts=True,
                    ),
                    LegacyDoc(
                        model_type="catalog",
                        file_path="/home/alice/oscal/cat.json",
                        uuid="cat-uuid",
                        indexed=1,
                        has_fts=True,
                        children=[LegacyChild(uuid="ac-2", element_type="control", fts_copies=1)],
                    ),
                ],
                orphans=[],
            )
        )
        build_legacy_db(db_path, spec, version=1)
        before = dump_db(db_path)

        open_and_close(db_path)

        after = dump_db(db_path)
        assert after[0] == SCHEMA_VERSION
        assert after[2:4] == before[2:4]  # documents and child_elements unchanged
        conn = sqlite3.connect(db_path)
        try:
            assert fts_columns(conn) == FTS_COLUMNS_V2
            fts = conn.execute(
                "SELECT entity_type, element_id FROM fts_index ORDER BY rowid"
            ).fetchall()
        finally:
            conn.close()
        assert fts == [("documentation", ""), ("document", "cat-uuid"), ("child_element", "ac-2")]

    def test_failed_version_1_upgrade_rolls_back(self, tmp_path: Path) -> None:
        db_path = tmp_path / "v1.db"
        cdef = LegacyDoc(
            model_type="component-definition",
            file_path="/home/alice/oscal/cdef.json",
            uuid="cdef-uuid",
            indexed=1,
            has_fts=True,
        )
        build_legacy_db(db_path, LegacyDb(docs=[cdef], orphans=[]), version=1)
        # A table with the rebuild's target name makes the rebuild fail with sqlite3.Error.
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE fts_index_v2 (x)")
        conn.commit()
        conn.close()
        before = dump_db(db_path)

        with pytest.raises(RuntimeError) as exc_info:
            OscalStore(db_path=str(db_path), seed_from_bundled=False)

        assert str(exc_info.value) == UPGRADE_FAILED_MESSAGE
        assert dump_db(db_path) == before

    def test_populated_current_db_reopen_changes_nothing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "current.db"
        build_legacy_db(
            db_path,
            LegacyDb(
                docs=[
                    LegacyDoc(
                        model_type="profile",
                        file_path="/home/alice/oscal/p.json",
                        uuid="p-uuid",
                        indexed=1,
                        has_fts=True,
                    )
                ],
                orphans=[],
            ),
        )
        open_and_close(db_path)  # upgrade to the current version
        current = dump_db(db_path)
        assert current[0] == SCHEMA_VERSION
        open_and_close(db_path)
        assert dump_db(db_path) == current

    def test_fresh_db_reopen_changes_nothing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fresh.db"
        open_and_close(db_path)
        fresh = dump_db(db_path)
        assert fresh[0] == SCHEMA_VERSION
        open_and_close(db_path)
        assert dump_db(db_path) == fresh

    def test_duplicate_fts_rows_keep_lowest_rowid(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy.db"
        cdef = LegacyDoc(
            model_type="component-definition",
            file_path="/home/alice/oscal/cdef.json",
            uuid="cdef-uuid",
            indexed=1,
            has_fts=True,
            children=[
                LegacyChild(uuid="c-1", element_type="component", fts_copies=3),
                LegacyChild(uuid="c-2", element_type="component", fts_copies=1),
            ],
        )
        build_legacy_db(db_path, LegacyDb(docs=[cdef], orphans=[]))
        # A second FTS row for the document itself
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO fts_index (entity_type, entity_id, title, description, model_type) "
            "VALUES ('document', ?, 'Title', 'later copy', 'component-definition')",
            (str(cdef.row_id),),
        )
        conn.commit()
        before = conn.execute(
            "SELECT entity_type, entity_id, MIN(rowid) FROM fts_index "
            "GROUP BY entity_type, entity_id"
        ).fetchall()
        conn.close()

        open_and_close(db_path)

        conn = sqlite3.connect(db_path)
        try:
            after = conn.execute(
                "SELECT entity_type, entity_id, rowid FROM fts_index ORDER BY rowid"
            ).fetchall()
            descriptions = conn.execute(
                "SELECT description FROM fts_index WHERE entity_type = 'document'"
            ).fetchall()
        finally:
            conn.close()
        assert sorted(after) == sorted(before)
        assert len(after) == 3  # document, c-1, c-2
        assert descriptions == [("body",)]  # the first (lowest rowid) copy is kept

    @pytest.mark.parametrize("stored_hash", ["current", "stale"])
    def test_legacy_markdown_row_is_rekeyed_on_rescan(
        self, tmp_path: Path, stored_hash: str
    ) -> None:
        """Unchanged content refreshes the row in place; changed content replaces it."""
        hash_matches = stored_hash == "current"
        root = tmp_path / "docs"
        md = root / "guide" / "intro.md"
        md.parent.mkdir(parents=True)
        md.write_text("# Intro\n\nBody.\n", encoding="utf-8")
        path_uuid = str(uuid5(NAMESPACE_URL, str(md)))
        db_path = tmp_path / "legacy.db"
        doc = LegacyDoc(
            model_type="documentation",
            file_path=str(md),
            uuid=path_uuid,
            indexed=1,
            has_fts=True,
        )
        build_legacy_db(db_path, LegacyDb(docs=[doc], orphans=[]))
        if hash_matches:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "UPDATE documents SET content_hash = ? WHERE id = ?",
                (hashlib.sha256(md.read_bytes()).hexdigest(), doc.row_id),
            )
            conn.commit()
            conn.close()

        store = OscalStore(db_path=str(db_path), seed_from_bundled=False)
        try:
            # The upgrade infers user + basename; the rescan gives the exact values
            store.scan_directory(root, origin=ORIGIN_BUNDLED)
            rows = store._conn.execute(
                "SELECT id, uuid, origin, relative_name FROM documents"
            ).fetchall()
            fts = store._conn.execute("SELECT entity_type, entity_id FROM fts_index").fetchall()
        finally:
            store.close()

        expected_uuid = doc_id(ORIGIN_BUNDLED, "guide/intro.md")
        assert [(r["uuid"], r["origin"], r["relative_name"]) for r in rows] == [
            (expected_uuid, ORIGIN_BUNDLED, "guide/intro.md")
        ]
        assert expected_uuid != path_uuid
        if hash_matches:
            assert rows[0]["id"] == doc.row_id  # refreshed in place
        assert [tuple(r) for r in fts] == [("documentation", str(rows[0]["id"]))]
