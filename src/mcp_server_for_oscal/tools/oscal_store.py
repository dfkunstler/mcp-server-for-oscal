"""
SQLite-backed store for all OSCAL model types.

Replaces the in-memory ComponentDefinitionStore with a scalable store
that supports all eight OSCAL model types, lazy loading, FTS5 search,
pagination, and three database modes (bundled, persistent, ephemeral).
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

if TYPE_CHECKING:
    from collections.abc import Sequence

from mcp_server_for_oscal.config import config
from mcp_server_for_oscal.tools import fts_query
from mcp_server_for_oscal.tools.fts_query import TITLE_COLUMNS, match_expressions, normalize_query
from mcp_server_for_oscal.tools.utils import (
    MODEL_MAP,
    ROOT_KEY_TO_MODEL_TYPE,
    TRUNCATION_MARK,
    OSCALModelType,
    budget_page,
)

logger = logging.getLogger(__name__)

# Path to the pre-built DB shipped with the package
BUNDLED_DB_PATH = Path(__file__).parent.parent / "oscal_store.db"

# Path to the package-level hashes.json manifest (written by build_oscal_db.py)
BUNDLED_HASHES_PATH = Path(__file__).parent.parent / "hashes.json"

# Origin: the provenance recorded on every documents row at ingestion. ``bundled`` is
# content scanned into the bundled DB by bin/build_oscal_db.py; ``user`` is content
# scanned from OSCAL_DOCUMENTS_DIR (or any other caller of scan_directory).
Origin = Literal["bundled", "user"]
ORIGIN_BUNDLED: Final = "bundled"
ORIGIN_USER: Final = "user"
ORIGINS: Final[tuple[Origin, ...]] = (ORIGIN_BUNDLED, ORIGIN_USER)

# Column definition shared by the fresh CREATE TABLE and ALTER TABLE for ``origin`` and
# ``relative_name``. No CHECK: SQLite verifies an added CHECK against existing rows, and
# '' would fail it; scan_directory validates Origin in Python instead.
_PROVENANCE_COLUMN: Final = "TEXT NOT NULL DEFAULT ''"

# Namespace of Doc_IDs (UUID v5 of "<origin>:<relative name>"), fixed so IDs are stable
# across machines and releases.
DOC_ID_NAMESPACE: Final = uuid5(
    NAMESPACE_URL, "https://github.com/dfkunstler/mcp-server-for-oscal/doc-id"
)


def relative_name(path: Path, root: Path) -> str:
    """Return the '/'-joined parts of ``path`` relative to ``root``, NFC-normalized.

    NFC normalization makes the name independent of how the filesystem encodes
    Unicode file names (macOS HFS+ stores NFD), and joining the parts with '/'
    makes it independent of the OS path separator.
    """
    return unicodedata.normalize("NFC", "/".join(path.relative_to(root).parts))


def zip_member_name(zip_relative_name: str, member: str) -> str:
    """Return the Relative_Name of a zip member: the zip's Relative_Name, '/', the member."""
    return unicodedata.normalize("NFC", f"{zip_relative_name}/{member.replace(chr(92), '/')}")


def doc_id(origin: Origin, rel: str) -> str:
    """Return the Doc_ID for a documentation file: UUID v5 of ``"<origin>:<rel>"``.

    ``origin`` never contains ':', so the name is unambiguous, and the same
    Relative_Name under different Origins gives different Doc_IDs.
    """
    return str(uuid5(DOC_ID_NAMESPACE, f"{origin}:{rel}"))


# Schema version recorded in ``PRAGMA user_version``. Version 0 is the pre-feature schema.
# Version 1 adds the ``origin`` and ``relative_name`` columns, Doc_IDs per Origin and
# Relative_Name, and catalog Control_Prose. Version 2 adds the searchable ``element_id``
# column to ``fts_index`` (D10).
SCHEMA_VERSION: Final = 2

# DDL of the current ``fts_index`` table; ``{table}`` is a fixed table name. ``element_id``
# is last, so the column indexes of the older five columns (``snippet(fts_index, 3, ...)``
# = ``description``) stay valid (D10).
_FTS_DDL: Final = (
    "CREATE VIRTUAL TABLE {table} USING fts5("
    "entity_type, entity_id, title, description, model_type, element_id)"
)

# Path markers of the bundled build inputs (D4). Only the pre-feature migration uses them;
# runtime selection reads the ``origin`` column.
_BUNDLED_MARKERS: Final = (
    "/data/component_definitions/",
    "/data/oscal_docs/",
    "mcp_server_for_oscal/component_definitions/",
    "mcp_server_for_oscal/oscal_docs/",
)


def legacy_provenance(file_path: str) -> tuple[Origin, str]:
    """Return the inferred ``(origin, relative_name)`` of a pre-feature ``file_path``.

    A path containing a bundled build marker is ``bundled`` with the suffix after the
    last marker as its Relative_Name. Any other path is ``user`` with its basename, or
    ``<zip basename>/<member>`` for a zip member. The next scan of the file replaces
    these best-effort values with exact ones.
    """
    norm = "/" + file_path.replace("\\", "/")
    best_end = -1
    for marker in _BUNDLED_MARKERS:
        pos = norm.rfind(marker)
        if pos >= 0:
            best_end = max(best_end, pos + len(marker))
    if best_end >= 0 and best_end < len(norm):
        return ORIGIN_BUNDLED, unicodedata.normalize("NFC", norm[best_end:])
    zip_pos = norm.find(".zip/")
    if zip_pos >= 0:
        zip_base = norm[:zip_pos].rsplit("/", 1)[-1] + ".zip"
        member = norm[zip_pos + len(".zip/") :]
        return ORIGIN_USER, unicodedata.normalize("NFC", f"{zip_base}/{member}")
    return ORIGIN_USER, unicodedata.normalize("NFC", norm.rsplit("/", 1)[-1])


# Query types accepted by OscalStore.query (the per-model query tools expose the same set).
VALID_QUERY_TYPES: tuple[str, ...] = ("all", "by_uuid", "by_title")

# Maps each OSCAL model type to the child element types it can contain.
CHILD_ELEMENT_TYPES: dict[OSCALModelType, tuple[str, ...]] = {
    OSCALModelType.COMPONENT_DEFINITION: ("component", "capability"),
    OSCALModelType.CATALOG: ("control", "group"),
    OSCALModelType.PROFILE: ("import", "modify"),
    OSCALModelType.SYSTEM_SECURITY_PLAN: (
        "control-implementation",
        "system-component",
    ),
    OSCALModelType.ASSESSMENT_PLAN: ("task", "activity"),
    OSCALModelType.ASSESSMENT_RESULTS: ("result", "finding"),
    OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES: ("poam-item",),
    OSCALModelType.MAPPING: ("mapping",),
}

# Names of the catalog control parts whose prose (and nested parts' prose) forms the
# Control_Prose stored as the control's description (Requirement 9).
PROSE_PART_NAMES: Final = frozenset({"statement", "guidance"})

# Maximum length of a text_search hit's description before TRUNCATION_MARK (Requirement 7.4).
TEXT_SEARCH_DESCRIPTION_CAP: Final = 500

# FROM/JOIN/WHERE of text_search over the ``hits`` CTE: resolves each FTS row to its
# Element_ID or Document_UUID (D1) and drops orphan FTS rows. Fixed SQL, no caller text.
_TEXT_SEARCH_JOIN: Final = """
    FROM hits h
    LEFT JOIN child_elements ce
           ON h.entity_type = 'child_element' AND ce.id = CAST(h.entity_id AS INTEGER)
    LEFT JOIN documents pd ON pd.id = ce.parent_doc_id
    LEFT JOIN documents d
           ON h.entity_type = 'document' AND d.id = CAST(h.entity_id AS INTEGER)
    WHERE COALESCE(ce.id, d.id) IS NOT NULL
"""

# Maximum length of a documentation search snippet (Requirement 10.3).
DOC_SNIPPET_CAP: Final = 200

# FROM/JOIN of search_documentation over the ``hits`` CTE: resolves each FTS row to its
# Doc_ID and drops orphan FTS rows. The caller appends the bound Origin filter
# (Requirements 11.2, 11.7). Fixed SQL, no caller text.
_DOC_SEARCH_JOIN: Final = """
    FROM hits h
    JOIN documents d
      ON d.id = CAST(h.entity_id AS INTEGER) AND d.model_type = 'documentation'
"""


@dataclass(frozen=True)
class DocumentationText:
    """A Documentation_Document's Doc_ID, title, and text lines (with line endings)."""

    doc_id: str
    title: str
    lines: tuple[str, ...]


def control_prose(control: object) -> str:
    """Return the Control_Prose of a catalog control.

    Works on the oscal-bindings ``Control`` model (from
    ``MODEL_MAP[OSCALModelType.CATALOG]``): the prose of its ``statement`` and
    ``guidance`` parts and of their nested parts, in document order, joined by blank
    lines. Returns ``""`` when the control has no such part or none has prose.
    Parameter insertions (``{{ insert: param, ... }}``) are kept as written.
    """
    out: list[str] = []
    for part in _as_list(getattr(control, "parts", None)):
        if _token(getattr(part, "name", None)) in PROSE_PART_NAMES:
            _collect_prose(part, out)
    return "\n\n".join(out)


def _collect_prose(part: object, out: list[str]) -> None:
    """Append the stripped prose of ``part`` and then of its nested parts, depth first."""
    prose = getattr(part, "prose", None)
    text = str(getattr(prose, "root", prose)).strip() if prose is not None else ""
    if text:
        out.append(text)
    for child in _as_list(getattr(part, "parts", None)):
        _collect_prose(child, out)


def _as_list(value: object) -> list:
    """Return ``value`` as a list, unwrapping a ``RootModel`` wrapper; ``None`` → ``[]``."""
    value = getattr(value, "root", value)
    return list(value) if value else []  # type: ignore[call-overload]


def _token(value: object) -> object:
    """Return the OSCAL value of a closed-enum ``Enum`` member, or ``value`` unchanged."""
    return getattr(value, "value", value)


class OscalStore:
    """SQLite-backed store for all OSCAL model types."""

    def __init__(
        self,
        db_path: str | None = None,
        cache_size: int = 100,
        *,
        seed_from_bundled: bool = True,
    ) -> None:
        """Initialize the store, resolving database mode.

        Resolution order:
        1. If db_path is set and file exists -> open persistent DB
        2. If db_path is set and file missing -> copy bundled DB (if exists)
           or create new
        3. If db_path is None and bundled DB exists -> copy to temp,
           verify integrity
        4. If db_path is None and no bundled DB -> create ephemeral in temp dir

        Args:
            db_path: Path to SQLite database file. None = auto-resolve.
                     When None, falls back to config.oscal_store_db_path if set.
            cache_size: Max number of parsed OSCAL models to cache.
            seed_from_bundled: If True (default), seed from the bundled DB
                     when db_path does not exist. If False, always start
                     with an empty database.

        Raises:
            RuntimeError: If the database cannot be created or opened.
        """
        self._cache_size = cache_size
        self._seed_from_bundled = seed_from_bundled
        self._db_mode: str = ""  # "bundled", "persistent", or "ephemeral"
        self._temp_dir: tempfile.TemporaryDirectory | None = None
        self._cached_parse = self._build_cached_parse()

        resolved_path = self._resolve_db_path(db_path)
        self._db_path: str = resolved_path

        # sqlite3 connections may only be used by the thread that created
        # them, and MCP servers run sync tools on worker threads, so each
        # thread gets its own connection to the same database file.
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._closed = False

        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error as exc:
            raise RuntimeError(f"Cannot open SQLite database at {resolved_path}: {exc}") from exc

        self._init_schema()
        logger.info(
            "OscalStore initialized in %s mode at %s",
            self._db_mode,
            resolved_path,
        )

    # ------------------------------------------------------------------
    # Database mode resolution
    # ------------------------------------------------------------------

    def _resolve_db_path(self, db_path: str | None) -> str:
        """Resolve the database file path based on configuration.

        Returns the resolved path string and sets ``self._db_mode``.
        """
        # Use explicit argument first, then fall back to config
        effective_path = db_path
        if effective_path is None:
            cfg_path = getattr(config, "oscal_store_db_path", "")
            if cfg_path:
                effective_path = cfg_path

        if effective_path:
            return self._resolve_persistent(effective_path)
        return self._resolve_auto()

    def _resolve_persistent(self, db_path: str) -> str:
        """Resolve when an explicit DB path is provided."""
        p = Path(db_path)
        if p.exists():
            self._db_mode = "persistent"
            logger.info("Opening existing persistent DB at %s", db_path)
            return db_path

        # DB file doesn't exist yet — seed from bundled if available and valid
        if self._seed_from_bundled and BUNDLED_DB_PATH.exists() and self._verify_bundled_db():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(BUNDLED_DB_PATH, p)
            except OSError:
                logger.warning(
                    "Failed to copy bundled DB to %s; creating fresh DB",
                    db_path,
                )
            else:
                # Verify the copy's integrity
                expected_hash = self._get_expected_db_hash()
                if expected_hash:
                    self._verify_file_hash(p, expected_hash)
                self._db_mode = "persistent"
                logger.info("Seeded persistent DB from bundled DB at %s", db_path)
                return db_path

        # Create a new empty persistent DB
        p.parent.mkdir(parents=True, exist_ok=True)
        self._db_mode = "persistent"
        logger.info("Creating new persistent DB at %s", db_path)
        return db_path

    def _resolve_auto(self) -> str:
        """Resolve when no explicit DB path is configured."""
        if BUNDLED_DB_PATH.exists():
            if self._verify_bundled_db():
                return self._copy_bundled_to_temp()
            logger.warning("Bundled DB failed integrity check; falling back to ephemeral DB")
            return self._create_ephemeral()

        # No bundled DB — create ephemeral
        return self._create_ephemeral()

    def _copy_bundled_to_temp(self) -> str:
        """Copy the bundled DB to a temporary directory."""
        self._temp_dir = tempfile.TemporaryDirectory(prefix="oscal_store_")
        dest = Path(self._temp_dir.name) / "oscal_store.db"
        try:
            shutil.copy2(BUNDLED_DB_PATH, dest)
        except OSError:
            logger.warning("Failed to copy bundled DB; falling back to ephemeral")
            return self._create_ephemeral()

        expected_hash = self._get_expected_db_hash()
        if expected_hash:
            self._verify_file_hash(dest, expected_hash)
        self._db_mode = "bundled"
        logger.info("Copied bundled DB to temp location %s", dest)
        return str(dest)

    def _create_ephemeral(self) -> str:
        """Create an ephemeral DB in a temporary directory."""
        if self._temp_dir is None:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="oscal_store_")
        dest = Path(self._temp_dir.name) / "oscal_store.db"
        self._db_mode = "ephemeral"
        logger.info("Creating ephemeral DB at %s", dest)
        return str(dest)

    @staticmethod
    def _get_expected_db_hash() -> str | None:
        """Read the expected SHA-256 hash for oscal_store.db from hashes.json.

        Returns:
            The expected hex digest string, or None if hashes.json is
            missing, malformed, or lacks an entry for oscal_store.db.
        """
        if not BUNDLED_HASHES_PATH.exists():
            logger.warning(
                "hashes.json not found at %s; cannot verify DB",
                BUNDLED_HASHES_PATH,
            )
            return None

        try:
            manifest = json.loads(BUNDLED_HASHES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read hashes.json: %s", exc)
            return None

        expected = manifest.get("file_hashes", {}).get("oscal_store.db")
        if not expected:
            logger.warning("No hash entry for oscal_store.db in hashes.json")
        return expected

    @staticmethod
    def _verify_file_hash(file_path: Path, expected_hash: str) -> None:
        """Verify a file's SHA-256 matches the expected hash.

        Args:
            file_path: Path to the file to verify.
            expected_hash: Expected SHA-256 hex digest.

        Raises:
            RuntimeError: If the hash does not match.
        """
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)

        actual_hash = h.hexdigest()
        if actual_hash != expected_hash:
            file_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"Post-copy integrity check failed for {file_path}: "
                f"expected {expected_hash}, got {actual_hash}"
            )

    @staticmethod
    def _verify_bundled_db() -> bool:
        """Verify the bundled DB SHA-256 against the hash in hashes.json.

        Reuses the same integrity verification pattern as
        ``verify_package_integrity`` in ``utils.py``, but operates on a
        single file rather than a whole directory.

        Returns:
            True if the bundled DB exists and its hash matches the
            recorded value in hashes.json.  False if the DB is missing,
            hashes.json is missing/malformed, or the hash does not match.
        """
        if not BUNDLED_DB_PATH.exists():
            return False

        expected_hash = OscalStore._get_expected_db_hash()
        if not expected_hash:
            return False

        h = hashlib.sha256()
        try:
            with open(BUNDLED_DB_PATH, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
        except OSError as exc:
            logger.warning("Failed to read bundled DB for hashing: %s", exc)
            return False

        actual_hash = h.hexdigest()
        if actual_hash != expected_hash:
            logger.warning(
                "Bundled DB integrity check failed: expected %s, got %s",
                expected_hash,
                actual_hash,
            )
            return False

        logger.info("Bundled DB integrity verified")
        return True

    # ------------------------------------------------------------------
    # Schema initialization
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Create all required tables, indexes, and FTS virtual table.

        Uses ``CREATE … IF NOT EXISTS`` so it is safe to call on an
        existing database.
        """
        cur = self._conn.cursor()
        try:
            # -- documents table --
            cur.execute(f"""
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
                    origin        {_PROVENANCE_COLUMN},
                    relative_name {_PROVENANCE_COLUMN}
                )
            """)  # nosec B608 - constant column definition, no caller input

            # -- migration: add content_hash for existing DBs --
            with contextlib.suppress(sqlite3.OperationalError):  # column already exists
                cur.execute("ALTER TABLE documents ADD COLUMN content_hash TEXT")

            cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_uuid ON documents(uuid)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_title ON documents(title COLLATE NOCASE)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_model_type ON documents(model_type)"
            )

            # -- child_elements table --
            cur.execute("""
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
            """)

            cur.execute("CREATE INDEX IF NOT EXISTS idx_child_uuid ON child_elements(uuid)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_child_title ON child_elements(title COLLATE NOCASE)"
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_child_type ON child_elements(element_type)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_child_parent ON child_elements(parent_doc_id)"
            )

            # -- FTS5 virtual table (element_id last, D10) --
            cur.execute(_FTS_DDL.format(table="IF NOT EXISTS fts_index"))

            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise RuntimeError(f"Failed to initialize database schema: {exc}") from exc

        # Pre-feature DBs lack the provenance columns, so the index on them is created
        # only after the upgrade. On an upgraded DB both steps perform no writes.
        self._migrate()
        try:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_origin ON documents(origin, model_type)"
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise RuntimeError(f"Failed to initialize database schema: {exc}") from exc

    # ------------------------------------------------------------------
    # Schema upgrade (pre-feature DB -> SCHEMA_VERSION)
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        """Upgrade a DB older than ``SCHEMA_VERSION`` in place, in one transaction.

        Version 0 (pre-feature): adds the provenance columns, infers Origin and
        Relative_Name for rows that lack them (D4), re-keys documentation rows to
        Doc_IDs, marks catalogs for re-extraction (Control_Prose), and removes orphan
        and duplicate FTS rows. Versions 0 and 1: rebuild ``fts_index`` with the
        ``element_id`` column (D10). Then sets ``user_version``. ``BEGIN IMMEDIATE``
        and the version read inside it serialize two processes upgrading one
        persistent DB. A DB already at ``SCHEMA_VERSION`` is not written to.

        Raises:
            RuntimeError: If the upgrade fails; the DB is left unchanged.
        """
        conn = self._conn
        if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version >= SCHEMA_VERSION:
                conn.rollback()  # another process upgraded the DB meanwhile
                return
            if version < 1:
                self._add_column_if_missing("origin", _PROVENANCE_COLUMN)
                self._add_column_if_missing("relative_name", _PROVENANCE_COLUMN)
                self._backfill_provenance()
                self._rekey_documentation()
                conn.execute("UPDATE documents SET indexed = 0 WHERE model_type = 'catalog'")
                self._delete_orphan_fts_rows()
                self._delete_duplicate_fts_rows()
            self._rebuild_fts_with_element_id()
            conn.execute(
                f"PRAGMA user_version = {int(SCHEMA_VERSION)}"  # nosec B608 - constant int
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            logger.exception("Schema upgrade failed for %s", self._db_path)
            raise RuntimeError("Failed to upgrade the OSCAL store schema") from exc
        logger.info("Upgraded OSCAL store schema to version %d", SCHEMA_VERSION)

    def _add_column_if_missing(self, column: str, definition: str) -> None:
        """Add ``column`` to ``documents`` unless ``PRAGMA table_info`` already lists it.

        ``column`` and ``definition`` are module constants, never caller input.
        """
        existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(documents)")}
        if column not in existing:
            self._conn.execute(
                f"ALTER TABLE documents ADD COLUMN {column} {definition}"  # nosec B608 - constants
            )

    def _backfill_provenance(self) -> None:
        """Set Origin and Relative_Name of rows with no Origin from ``legacy_provenance``."""
        rows = self._conn.execute("SELECT id, file_path FROM documents WHERE origin = ''")
        updates = [(*legacy_provenance(r["file_path"]), r["id"]) for r in rows.fetchall()]
        self._conn.executemany(
            "UPDATE documents SET origin = ?, relative_name = ? WHERE id = ?", updates
        )

    def _rekey_documentation(self) -> None:
        """Set each documentation row's ``uuid`` to ``doc_id(origin, relative_name)``.

        When several rows map to one Doc_ID, the row with the lowest ``id`` keeps it
        and the others are deleted with their FTS rows. Changed rows first get a
        unique placeholder, so no intermediate state collides with a target Doc_ID.
        """
        rows = self._conn.execute(
            "SELECT id, uuid, origin, relative_name FROM documents "
            "WHERE model_type = 'documentation' ORDER BY id"
        ).fetchall()
        keepers: dict[str, int] = {}
        duplicates: list[int] = []
        changes: list[tuple[str, int]] = []
        for r in rows:
            target = doc_id(r["origin"], r["relative_name"])
            if target in keepers:
                duplicates.append(r["id"])
                continue
            keepers[target] = r["id"]
            if r["uuid"] != target:
                changes.append((target, r["id"]))
        for row_id in duplicates:
            self._delete_document_row(row_id)
        self._conn.executemany(
            "UPDATE documents SET uuid = 'rekey:' || id WHERE id = ?",
            [(row_id,) for _, row_id in changes],
        )
        self._conn.executemany("UPDATE documents SET uuid = ? WHERE id = ?", changes)

    def _delete_orphan_fts_rows(self) -> None:
        """Delete FTS rows whose ``entity_id`` matches no child element or document row."""
        self._conn.execute(
            "DELETE FROM fts_index WHERE "
            "(entity_type = 'child_element' "
            " AND CAST(entity_id AS INTEGER) NOT IN (SELECT id FROM child_elements)) "
            "OR (entity_type IN ('document', 'documentation') "
            " AND CAST(entity_id AS INTEGER) NOT IN (SELECT id FROM documents))"
        )

    def _delete_duplicate_fts_rows(self) -> None:
        """Delete all but the lowest-rowid FTS row of each ``(entity_type, entity_id)``.

        Old re-indexes inserted a new row for an entity without deleting the old one (D3).
        """
        self._conn.execute(
            "DELETE FROM fts_index WHERE rowid NOT IN "
            "(SELECT MIN(rowid) FROM fts_index GROUP BY entity_type, entity_id)"
        )

    def _rebuild_fts_with_element_id(self) -> None:
        """Rebuild a five-column ``fts_index`` with the ``element_id`` column (D10).

        FTS5 tables do not support ``ADD COLUMN``, so every row is copied with its
        ``rowid`` into a new table, with ``element_id`` from the joined Element_ID or
        Document_UUID (``''`` for documentation and unresolved rows); then the old
        table is dropped and the new one renamed. Does nothing when ``fts_index``
        already has ``element_id``. Runs in the caller's transaction (no commit).
        """
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(fts_index)")}
        if "element_id" in columns:
            return
        self._conn.execute(_FTS_DDL.format(table="fts_index_v2"))
        self._conn.execute(
            "INSERT INTO fts_index_v2 "
            "(rowid, entity_type, entity_id, title, description, model_type, element_id) "
            "SELECT f.rowid, f.entity_type, f.entity_id, f.title, f.description, "
            "f.model_type, "
            "CASE f.entity_type "
            " WHEN 'child_element' THEN COALESCE(ce.uuid, '') "
            " WHEN 'document' THEN COALESCE(d.uuid, '') "
            " ELSE '' END "
            "FROM fts_index f "
            "LEFT JOIN child_elements ce "
            " ON f.entity_type = 'child_element' AND ce.id = CAST(f.entity_id AS INTEGER) "
            "LEFT JOIN documents d "
            " ON f.entity_type = 'document' AND d.id = CAST(f.entity_id AS INTEGER)"
        )
        self._conn.execute("DROP TABLE fts_index")
        self._conn.execute("ALTER TABLE fts_index_v2 RENAME TO fts_index")

    def _delete_document_row(self, row_id: int) -> None:
        """Delete a documents row, its child elements, and all their FTS rows.

        FTS rows have no foreign key, so they are deleted explicitly; child elements
        go by ``ON DELETE CASCADE``. Runs in the caller's transaction (no commit).
        """
        self._delete_index_rows(row_id, ("document", "documentation"))
        self._conn.execute("DELETE FROM child_elements WHERE parent_doc_id = ?", (row_id,))
        self._conn.execute("DELETE FROM documents WHERE id = ?", (row_id,))

    def _delete_index_rows(self, doc_id: int, doc_entity_types: tuple[str, ...]) -> None:
        """Delete the FTS rows of a document's child elements and its own FTS rows.

        *doc_entity_types* names the document-level FTS entity types to delete
        (``entity_id`` is the documents row id). Runs in the caller's transaction
        (no commit); child element rows themselves are left to the caller.
        """
        self._conn.execute(
            "DELETE FROM fts_index WHERE entity_type = 'child_element' AND entity_id IN "
            "(SELECT CAST(id AS TEXT) FROM child_elements WHERE parent_doc_id = ?)",
            (doc_id,),
        )
        placeholders = ", ".join("?" * len(doc_entity_types))
        self._conn.execute(
            f"DELETE FROM fts_index WHERE entity_type IN ({placeholders}) "  # nosec B608 - fixed "?" placeholders only; values are bound
            "AND entity_id = ?",
            (*doc_entity_types, str(doc_id)),
        )

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    @property
    def db_mode(self) -> str:
        """Return the resolved database mode."""
        return self._db_mode

    @property
    def db_path(self) -> str:
        """Return the resolved database file path."""
        return self._db_path

    @property
    def _conn(self) -> sqlite3.Connection:
        """Return the calling thread's connection, opening it on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
            # Each connection is only used by its own thread; the check is
            # disabled so close() can shut them all down from one thread.
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    def close(self) -> None:
        """Close all database connections and clean up temp resources."""
        with self._conns_lock:
            self._closed = True
            conns, self._conns = self._conns, []
        for conn in conns:
            conn.close()
        self._local = threading.local()
        if self._temp_dir:
            self._temp_dir.cleanup()
            self._temp_dir = None

    # ------------------------------------------------------------------
    # LRU-cached model parsing
    # ------------------------------------------------------------------

    def get_parsed_model(self, doc_id: int) -> object:
        """Get a fully parsed OSCAL model for a document, using LRU cache.

        Fetches ``raw_json`` and ``model_type`` from SQLite, then delegates
        to an instance-level LRU-cached parser so repeated accesses for the
        same ``doc_id`` are served from cache.

        The cache key includes ``doc_id`` and ``raw_json``, so when a file
        is re-ingested with new content the stale entry is naturally
        bypassed.

        Args:
            doc_id: The integer primary key of the document row.

        Returns:
            A parsed OSCAL Pydantic model instance.

        Raises:
            ValueError: If no document exists for *doc_id* or the model
                type has no OSCAL model class.
            RuntimeError: If parsing fails.
        """
        row = self._conn.execute(
            "SELECT raw_json, model_type FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No document found with id {doc_id}")

        raw_json: str = row["raw_json"]
        model_type_str: str = row["model_type"]

        return self._cached_parse(doc_id, raw_json, model_type_str)

    def get_parsed_model_by_uuid(self, doc_uuid: str) -> object | None:
        """Get a fully parsed OSCAL model for the document with *doc_uuid*.

        Resolves ``documents.uuid`` to the row id and delegates to
        :meth:`get_parsed_model`, so the same LRU cache is used.

        Args:
            doc_uuid: The OSCAL document UUID (``documents.uuid``).

        Returns:
            A parsed OSCAL Pydantic model instance, or ``None`` when
            *doc_uuid* is empty or matches no document.

        Raises:
            ValueError: If the model type has no OSCAL model class.
            RuntimeError: If parsing fails.
        """
        if not doc_uuid:
            return None
        row = self._conn.execute(
            "SELECT id FROM documents WHERE uuid = ?",
            (doc_uuid,),
        ).fetchone()
        if row is None:
            return None
        return self.get_parsed_model(row["id"])

    @staticmethod
    def _do_parse(raw_json: str, model_type_str: str) -> object:
        """Parse *raw_json* into the parsed OSCAL model for *model_type_str*.

        This is the actual parsing logic, separated from caching so it
        can be tested independently.
        """
        try:
            model_type = OSCALModelType(model_type_str)
        except ValueError as exc:
            raise ValueError(f"Unknown model type '{model_type_str}'") from exc

        model_class = MODEL_MAP.get(model_type)
        if model_class is None:
            raise ValueError(f"No OSCAL model class for type '{model_type_str}'")

        data = json.loads(raw_json)
        root_data = data.get(model_type.value, data)

        try:
            return model_class.model_validate(root_data)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to parse document as {model_class.__name__}: {exc}"
            ) from exc

    def _build_cached_parse(self) -> functools._lru_cache_wrapper:
        """Build an LRU-cached wrapper around ``_do_parse``.

        The wrapper signature is ``(doc_id, raw_json, model_type_str)``
        so that *doc_id* + *raw_json* together form the effective cache
        key.  When a document is re-ingested with new content the
        *raw_json* argument changes, causing a cache miss.
        """

        @functools.lru_cache(maxsize=self._cache_size)
        def _cached_parse(doc_id: int, raw_json: str, model_type_str: str) -> object:  # noqa: ARG001 - doc_id is part of the cache key
            return OscalStore._do_parse(raw_json, model_type_str)

        return _cached_parse

    # ------------------------------------------------------------------
    # Lazy indexing
    # ------------------------------------------------------------------

    def _ensure_indexed(self, doc_id: int) -> None:
        """Ensure a document's child elements are indexed.

        Checks the ``indexed`` flag on the document row.  If already 1,
        returns immediately (no-op).  Otherwise, parses the document via
        the LRU cache, extracts child elements, inserts them into the
        ``child_elements`` and ``fts_index`` tables, and sets
        ``indexed=1``.

        Args:
            doc_id: The integer primary key of the document row.

        Raises:
            ValueError: If no document exists for *doc_id*.
            RuntimeError: If parsing or extraction fails.
        """
        row = self._conn.execute(
            "SELECT indexed, model_type, title, uuid FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"No document found with id {doc_id}")

        if row["indexed"]:
            return

        model_type_str = row["model_type"]
        try:
            model_type = OSCALModelType(model_type_str)
        except ValueError:
            logger.warning(
                "Unknown model type '%s' for doc %d; skipping indexing",
                model_type_str,
                doc_id,
            )
            return

        # Parse the full OSCAL model (uses LRU cache)
        parsed_model = self.get_parsed_model(doc_id)

        # Extract child elements
        children = self._extract_child_elements(model_type, parsed_model)

        try:
            cur = self._conn.cursor()

            # Remove stale FTS rows (children and the document itself, D3), then the
            # stale child elements, so re-indexing leaves no orphan or duplicate rows
            self._delete_index_rows(doc_id, ("document",))
            cur.execute(
                "DELETE FROM child_elements WHERE parent_doc_id = ?",
                (doc_id,),
            )

            for child in children:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO child_elements
                        (uuid, title, element_type, parent_doc_id,
                         description, raw_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        child["uuid"],
                        child["title"],
                        child["element_type"],
                        doc_id,
                        child.get("description"),
                        child.get("raw_json"),
                    ),
                )

                # Insert FTS entry for the child element
                child_id = cur.lastrowid
                cur.execute(
                    """
                    INSERT INTO fts_index
                        (entity_type, entity_id, title, description, model_type, element_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "child_element",
                        str(child_id),
                        child["title"],
                        child.get("description") or "",
                        model_type_str,
                        child["uuid"],
                    ),
                )

            # Insert FTS entry for the document itself
            cur.execute(
                """
                INSERT INTO fts_index
                    (entity_type, entity_id, title, description, model_type, element_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "document",
                    str(doc_id),
                    row["title"],
                    "",
                    model_type_str,
                    row["uuid"],
                ),
            )

            # Mark as indexed
            cur.execute(
                "UPDATE documents SET indexed = 1 WHERE id = ?",
                (doc_id,),
            )

            self._conn.commit()
            logger.debug(
                "Indexed doc %d (%s): %d child elements",
                doc_id,
                model_type_str,
                len(children),
            )
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise RuntimeError(f"Failed to index document {doc_id}: {exc}") from exc

    def _extract_child_elements(
        self,
        model_type: OSCALModelType,
        parsed_model: object,
    ) -> list[dict]:
        """Extract child element metadata from a parsed OSCAL model.

        Each returned dict has keys: uuid, title, element_type,
        description (optional), raw_json (optional).

        Args:
            model_type: The OSCAL model type of the document.
            parsed_model: A parsed OSCAL Pydantic model instance.

        Returns:
            A list of child element dicts.
        """
        children: list[dict] = []

        if model_type == OSCALModelType.COMPONENT_DEFINITION:
            # components
            for comp in getattr(parsed_model, "components", None) or []:
                children.append(
                    self._child_dict(
                        uuid=str(comp.uuid),
                        title=str(comp.title),
                        element_type="component",
                        description=str(comp.description) if comp.description else None,
                        obj=comp,
                    )
                )
            # capabilities
            for cap in getattr(parsed_model, "capabilities", None) or []:
                children.append(
                    self._child_dict(
                        uuid=str(cap.uuid),
                        title=str(cap.name),
                        element_type="capability",
                        description=str(cap.description) if cap.description else None,
                        obj=cap,
                    )
                )

        elif model_type == OSCALModelType.CATALOG:
            # controls (top-level)
            for ctrl in getattr(parsed_model, "controls", None) or []:
                children.append(
                    self._child_dict(
                        uuid=str(ctrl.id),
                        title=str(ctrl.title),
                        element_type="control",
                        description=control_prose(ctrl),
                        obj=ctrl,
                    )
                )
            # groups
            for grp in getattr(parsed_model, "groups", None) or []:
                children.append(
                    self._child_dict(
                        uuid=str(grp.id),
                        title=str(grp.title),
                        element_type="group",
                        description=None,
                        obj=grp,
                    )
                )
            # controls nested inside groups (recursive)
            self._extract_controls_from_groups(
                getattr(parsed_model, "groups", None) or [], children
            )

        elif model_type == OSCALModelType.PROFILE:
            # imports
            for idx, imp in enumerate(getattr(parsed_model, "imports", None) or []):
                href = str(getattr(imp, "href", ""))
                children.append(
                    self._child_dict(
                        uuid=f"import-{idx}",
                        title=href or f"import-{idx}",
                        element_type="import",
                        description=href,
                        obj=imp,
                    )
                )
            # modify
            modify = getattr(parsed_model, "modify", None)
            if modify is not None:
                children.append(
                    self._child_dict(
                        uuid="modify",
                        title="modify",
                        element_type="modify",
                        description=None,
                        obj=modify,
                    )
                )

        elif model_type == OSCALModelType.SYSTEM_SECURITY_PLAN:
            # control-implementation
            ctrl_impl = getattr(parsed_model, "control_implementation", None)
            if ctrl_impl is not None:
                desc = str(ctrl_impl.description) if ctrl_impl.description else None
                children.append(
                    self._child_dict(
                        uuid="control-implementation",
                        title="control-implementation",
                        element_type="control-implementation",
                        description=desc,
                        obj=ctrl_impl,
                    )
                )
            # system-components from system_implementation
            sys_impl = getattr(parsed_model, "system_implementation", None)
            if sys_impl is not None:
                for comp in getattr(sys_impl, "components", None) or []:
                    children.append(
                        self._child_dict(
                            uuid=str(comp.uuid),
                            title=str(comp.title),
                            element_type="system-component",
                            description=(str(comp.description) if comp.description else None),
                            obj=comp,
                        )
                    )

        elif model_type == OSCALModelType.ASSESSMENT_PLAN:
            # tasks
            for task in getattr(parsed_model, "tasks", None) or []:
                children.append(
                    self._child_dict(
                        uuid=str(task.uuid),
                        title=str(task.title),
                        element_type="task",
                        description=(str(task.description) if task.description else None),
                        obj=task,
                    )
                )
            # activities from local_definitions
            local_defs = getattr(parsed_model, "local_definitions", None)
            if local_defs is not None:
                for act in getattr(local_defs, "activities", None) or []:
                    children.append(
                        self._child_dict(
                            uuid=str(act.uuid),
                            title=str(act.title) if act.title else str(act.uuid),
                            element_type="activity",
                            description=(str(act.description) if act.description else None),
                            obj=act,
                        )
                    )

        elif model_type == OSCALModelType.ASSESSMENT_RESULTS:
            # results
            for result in getattr(parsed_model, "results", None) or []:
                children.append(
                    self._child_dict(
                        uuid=str(result.uuid),
                        title=str(result.title),
                        element_type="result",
                        description=(str(result.description) if result.description else None),
                        obj=result,
                    )
                )
                # findings within each result
                for finding in getattr(result, "findings", None) or []:
                    children.append(
                        self._child_dict(
                            uuid=str(finding.uuid),
                            title=str(finding.title),
                            element_type="finding",
                            description=(str(finding.description) if finding.description else None),
                            obj=finding,
                        )
                    )

        elif model_type == OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES:
            # poam-items
            for item in getattr(parsed_model, "poam_items", None) or []:
                children.append(
                    self._child_dict(
                        uuid=str(item.uuid),
                        title=str(item.title),
                        element_type="poam-item",
                        description=(str(item.description) if item.description else None),
                        obj=item,
                    )
                )

        elif model_type == OSCALModelType.MAPPING:
            # mappings
            mappings = getattr(parsed_model, "mappings", None)
            if mappings is not None:
                # Unwrap the Mappings RootModel (array form) to its list
                mappings = getattr(mappings, "root", mappings)
                # mappings can be a single Mapping or a list
                if not isinstance(mappings, list):
                    mappings = [mappings]
                for m in mappings:
                    # matching_rationale is a closed enum (Enum member) since
                    # oscal-bindings 0.3.0; use .value so the title stays the
                    # OSCAL token (e.g. "semantic"), not "MappingMatchingRationale.semantic"
                    rationale = getattr(m, "matching_rationale", None)
                    title = rationale.value if rationale is not None else str(m.uuid)
                    children.append(
                        self._child_dict(
                            uuid=str(m.uuid),
                            title=title,
                            element_type="mapping",
                            description=None,
                            obj=m,
                        )
                    )

        return children

    def _extract_controls_from_groups(
        self,
        groups: list,
        children: list[dict],
    ) -> None:
        """Recursively extract controls nested inside catalog groups.

        Walks the group hierarchy and appends a child dict for every
        control found at any nesting depth, with its Control_Prose as the
        description. Handles both ``CatalogGroupWithControls`` and
        ``CatalogGroupWithGroups``. Groups themselves are NOT added here —
        they are already extracted at the top level.

        Args:
            groups: A list of parsed group objects to traverse.
            children: The accumulator list to append child dicts to.
        """
        for grp in groups:
            for ctrl in getattr(grp, "controls", None) or []:
                children.append(
                    self._child_dict(
                        uuid=str(ctrl.id),
                        title=str(ctrl.title),
                        element_type="control",
                        description=control_prose(ctrl),
                        obj=ctrl,
                    )
                )
            self._extract_controls_from_groups(getattr(grp, "groups", None) or [], children)

    @staticmethod
    def _child_dict(
        uuid: str,
        title: str,
        element_type: str,
        description: str | None,
        obj: object,
    ) -> dict:
        """Build a child element dict, serializing the object to JSON."""
        raw_json: str | None = None
        try:
            json_method = getattr(obj, "model_dump_json", None)
            if json_method is not None:
                raw_json = json_method(exclude_none=True, by_alias=True)
        except Exception:
            logger.debug("Could not serialize %s child to JSON", element_type)
        return {
            "uuid": uuid,
            "title": title,
            "element_type": element_type,
            "description": description,
            "raw_json": raw_json,
        }

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def query(
        self,
        ctx: object | None = None,  # noqa: ARG002 - kept for API parity with MCP tools
        oscal_model_type: OSCALModelType | None = None,
        query_type: str = "all",
        query_value: str | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> dict:
        """Unified query across all OSCAL model types.

        Args:
            ctx: MCP server context (unused, kept for interface compat).
            oscal_model_type: Scope to a specific model type, or None for all.
            query_type: ``"all"`` (paginated scan), ``"by_uuid"`` (index
                lookup), or ``"by_title"`` (case-insensitive exact then FTS
                fallback).
            query_value: Required for by_uuid and by_title.
            offset: Zero-based pagination offset.
            limit: Maximum number of items to return.

        Returns:
            Page_Response dict with keys: items, total, offset, limit, hasMore.
            Each item has uuid, title, model_type, sizeInBytes, and
            childTypeCounts. Storage paths are never returned.

        Raises:
            ValueError: If query_type is not one of VALID_QUERY_TYPES, or if
                query_value is missing when required.
        """
        if query_type not in VALID_QUERY_TYPES:
            raise ValueError(
                f"Invalid query_type {query_type!r}; valid types: {', '.join(VALID_QUERY_TYPES)}"
            )
        if query_type in ("by_uuid", "by_title") and not query_value:
            raise ValueError(f"query_value is required for query_type '{query_type}'")

        if query_type == "by_uuid":
            # query_value validated above; cast for type checker
            return self._query_by_uuid(query_value, oscal_model_type, offset, limit)  # type: ignore[arg-type]
        if query_type == "by_title":
            return self._query_by_title(query_value, oscal_model_type, offset, limit)  # type: ignore[arg-type]
        # "all" — paginated scan
        return self._query_all(oscal_model_type, offset, limit)

    def _query_by_uuid(
        self,
        uuid_value: str,
        oscal_model_type: OSCALModelType | None,
        offset: int,
        limit: int,
    ) -> dict:
        """Direct index lookup on documents.uuid."""
        where = "WHERE d.uuid = ?"
        params: list = [uuid_value]
        if oscal_model_type is not None:
            where += " AND d.model_type = ?"
            params.append(oscal_model_type.value)

        total = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM documents d {where}",  # nosec B608
            params,
        ).fetchone()["cnt"]

        rows = self._conn.execute(
            f"SELECT d.id, d.uuid, d.title, d.model_type, d.file_size "  # nosec B608
            f"FROM documents d {where} ORDER BY d.id LIMIT ? OFFSET ?",  # nosec B608
            [*params, limit, offset],
        ).fetchall()

        items = self._build_query_items(rows)
        return self._page_response(items, total, offset, limit)

    def _query_by_title(
        self,
        title_value: str,
        oscal_model_type: OSCALModelType | None,
        offset: int,
        limit: int,
    ) -> dict:
        """Case-insensitive exact match first, FTS fallback."""
        # Phase 1: exact case-insensitive match
        where = "WHERE d.title = ? COLLATE NOCASE"
        params: list = [title_value]
        if oscal_model_type is not None:
            where += " AND d.model_type = ?"
            params.append(oscal_model_type.value)

        total = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM documents d {where}",  # nosec B608
            params,
        ).fetchone()["cnt"]

        if total > 0:
            rows = self._conn.execute(
                f"SELECT d.id, d.uuid, d.title, d.model_type, d.file_size "  # nosec B608
                f"FROM documents d {where} ORDER BY d.id LIMIT ? OFFSET ?",  # nosec B608
                [*params, limit, offset],
            ).fetchall()
            items = self._build_query_items(rows)
            return self._page_response(items, total, offset, limit)

        # Phase 2: FTS fallback
        return self._query_by_title_fts(title_value, oscal_model_type, offset, limit)

    def _query_by_title_fts(
        self,
        title_value: str,
        oscal_model_type: OSCALModelType | None,
        offset: int,
        limit: int,
    ) -> dict:
        """FTS fallback for title search.

        The title is normalized into Search_Terms that are ANDed as quoted ``{title}``
        phrases, so hyphenated titles and FTS5 operator characters match literally.
        ``entity_type`` and ``model_type`` are SQL filters, not part of the MATCH string.
        """
        terms = normalize_query(title_value)
        if not terms:
            return self._page_response([], 0, offset, limit)
        _match_any, match_all = match_expressions(terms, columns=TITLE_COLUMNS)

        fts_sql = (
            "SELECT DISTINCT d.id AS doc_id FROM fts_index "
            "JOIN documents d ON d.id = CAST(fts_index.entity_id AS INTEGER) "
            "WHERE fts_index MATCH ? AND fts_index.entity_type = 'document'"
        )
        fts_params: list = [match_all]
        if oscal_model_type is not None:
            fts_sql += " AND fts_index.model_type = ?"
            fts_params.append(oscal_model_type.value)

        fts_sql += " ORDER BY d.id"

        fts_rows = self._conn.execute(fts_sql, fts_params).fetchall()
        doc_ids = [r["doc_id"] for r in fts_rows]

        if not doc_ids:
            return self._page_response([], 0, offset, limit)

        total = len(doc_ids)
        paged_ids = doc_ids[offset : offset + limit]
        if not paged_ids:
            return self._page_response([], total, offset, limit)

        placeholders = ",".join("?" for _ in paged_ids)
        rows = self._conn.execute(
            f"SELECT d.id, d.uuid, d.title, d.model_type, d.file_size "  # nosec B608
            f"FROM documents d WHERE d.id IN ({placeholders}) ORDER BY d.id",  # nosec B608
            paged_ids,
        ).fetchall()

        items = self._build_query_items(rows)
        return self._page_response(items, total, offset, limit)

    def _query_all(
        self,
        oscal_model_type: OSCALModelType | None,
        offset: int,
        limit: int,
    ) -> dict:
        """Paginated scan of all documents."""
        where = ""
        params: list = []
        if oscal_model_type is not None:
            where = "WHERE d.model_type = ?"
            params.append(oscal_model_type.value)

        total = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM documents d {where}",  # nosec B608
            params,
        ).fetchone()["cnt"]

        rows = self._conn.execute(
            f"SELECT d.id, d.uuid, d.title, d.model_type, d.file_size "  # nosec B608
            f"FROM documents d {where} ORDER BY d.id LIMIT ? OFFSET ?",  # nosec B608
            [*params, limit, offset],
        ).fetchall()

        items = self._build_query_items(rows)
        return self._page_response(items, total, offset, limit)

    def _child_type_counts(self, doc_id: int) -> dict[str, int]:
        """Map each child element type of one document to its count (empty when none)."""
        rows = self._conn.execute(
            "SELECT element_type, COUNT(*) AS cnt FROM child_elements "
            "WHERE parent_doc_id = ? GROUP BY element_type ORDER BY element_type",
            (doc_id,),
        ).fetchall()
        return {r["element_type"]: r["cnt"] for r in rows}

    def _build_query_items(self, rows: list) -> list[dict]:
        """Build item dicts from document rows, triggering _ensure_indexed.

        Each item carries ``childTypeCounts`` (child element type -> count) instead of
        inline child summaries; callers fetch children with the child-element tools.
        """
        items = []
        for row in rows:
            doc_id = row["id"]
            self._ensure_indexed(doc_id)
            items.append(
                {
                    "uuid": row["uuid"],
                    "title": row["title"],
                    "model_type": row["model_type"],
                    "sizeInBytes": row["file_size"],
                    "childTypeCounts": self._child_type_counts(doc_id),
                }
            )
        return items

    @staticmethod
    def _page_response(items: list[dict], total: int, offset: int, limit: int) -> dict:
        """Build a Page_Response dict."""
        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "hasMore": offset + limit < total,
        }

    # ------------------------------------------------------------------
    # Model type detection
    # ------------------------------------------------------------------

    def _detect_model_type(self, file_path: Path) -> OSCALModelType | None:
        """Read only the first root key from a JSON file to detect model type.

        Opens the file, parses the JSON, and checks if the first (or any)
        root key maps to a known OSCAL model type via ROOT_KEY_TO_MODEL_TYPE.

        Args:
            file_path: Path to the JSON file.

        Returns:
            The detected OSCALModelType, or None if no known root key found.
        """
        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            # UnicodeDecodeError is not a JSONDecodeError; without it a single
            # non-UTF-8 file aborts the whole scan_directory() call.
            logger.debug("Cannot read/parse %s: %s", file_path, exc)
            return None

        if not isinstance(data, dict):
            return None

        for key in data:
            if key in ROOT_KEY_TO_MODEL_TYPE:
                return ROOT_KEY_TO_MODEL_TYPE[key]
        return None

    def _detect_model_type_from_data(self, data: dict) -> OSCALModelType | None:
        """Detect model type from already-parsed JSON data.

        Args:
            data: Parsed JSON dict.

        Returns:
            The detected OSCALModelType, or None if no known root key found.
        """
        if not isinstance(data, dict):
            return None
        for key in data:
            if key in ROOT_KEY_TO_MODEL_TYPE:
                return ROOT_KEY_TO_MODEL_TYPE[key]
        return None

    # ------------------------------------------------------------------
    # OSCAL model validation
    # ------------------------------------------------------------------

    def _validate_with_model(self, data: dict, model_type: OSCALModelType) -> bool:
        """Validate document data with the OSCAL model class for *model_type*.

        Args:
            data: The full parsed JSON dict (with root key).
            model_type: The detected OSCAL model type.

        Returns:
            True if validation succeeds, False otherwise (including when no
            model class is registered for *model_type*).
        """
        model_class = MODEL_MAP.get(model_type)
        if model_class is None:
            # Fail closed: MODEL_MAP covers every OSCALModelType, so this is
            # unreachable unless the map is incomplete.
            logger.warning("No OSCAL model class for %s document", model_type.value)
            return False

        try:
            model_class.model_validate(data.get(model_type.value, data))
        except Exception as exc:
            logger.warning(
                "OSCAL model validation failed for %s document: %s",
                model_type.value,
                exc,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Directory scanning
    # ------------------------------------------------------------------

    def scan_directory(
        self,
        directory: Path,
        model_type_filter: OSCALModelType | None = None,
        *,
        origin: Origin = ORIGIN_USER,
    ) -> int:
        """Scan a directory for OSCAL JSON files and markdown documentation.

        Walks the directory tree for ``.json``, ``.zip``, and ``.md`` files.
        For each JSON/ZIP file, extracts metadata (UUID, title, model_type,
        file_path, file_size, file_mtime, raw_json) and UPSERTs into the
        ``documents`` table with ``indexed=0``.  For each markdown file,
        indexes the content as documentation into the FTS index. Every row
        the call ingests records ``origin`` and its Relative_Name under
        ``directory``.

        Change detection: files whose SHA-256 content hash matches the stored
        value are not re-ingested, but their Origin and Relative_Name (and, for
        documentation, Doc_ID) are refreshed to this call's values.

        Args:
            directory: Root directory to scan (the Scan_Root).
            model_type_filter: When set, only ingest documents of this type.
            origin: Origin recorded on every row this call ingests.

        Returns:
            Number of new or updated files ingested.

        Raises:
            ValueError: If ``origin`` is not one of ``ORIGINS``.
        """
        if origin not in ORIGINS:
            raise ValueError(f"Unknown origin {origin!r}; expected one of {ORIGINS}")

        if not directory.exists():
            logger.info("Directory does not exist, skipping: %s", directory)
            return 0

        if not directory.is_dir():
            logger.info("Path is not a directory, skipping: %s", directory)
            return 0

        count = 0

        # Process .json files
        json_files = list(directory.rglob("**/*.json"))
        for json_file in json_files:
            if json_file.name == "hashes.json":
                continue
            result = self._process_json_file(json_file, model_type_filter, directory, origin)
            if result:
                count += 1

        # Process .zip files
        zip_files = list(directory.rglob("**/*.zip"))
        for zip_file in zip_files:
            count += self._process_zip_file(zip_file, model_type_filter, directory, origin)

        # Process .md files
        md_files = list(directory.rglob("**/*.md"))
        for md_file in md_files:
            if self._process_markdown_file(md_file, directory, origin):
                count += 1

        if count == 0:
            logger.info("No new or updated OSCAL documents found in %s", directory)
        else:
            logger.info(
                "Ingested %d new/updated document(s) from %s",
                count,
                directory,
            )

        return count

    def count_documents_under(self, directory: Path) -> int:
        """Return the number of stored documents whose source file lies under ``directory``.

        Unlike the return value of :meth:`scan_directory`, this includes documents
        left unchanged since an earlier scan (persistent DB mode). Matches both the
        native path form (``.json``/``.md`` files) and the POSIX form (zip members).
        """
        # Trailing separator so "/a/docs" doesn't match "/a/docs2/..."
        prefixes = sorted({f"{directory}{os.sep}", f"{directory.as_posix()}/"})
        clauses = " OR ".join("substr(file_path, 1, ?) = ?" for _ in prefixes)
        params: list[object] = []
        for prefix in prefixes:
            params.extend((len(prefix), prefix))
        row = self._conn.execute(
            f"SELECT COUNT(*) AS cnt FROM documents WHERE {clauses}",  # nosec B608
            params,
        ).fetchone()
        return int(row["cnt"])

    def count_documents(self, *, origin: Origin) -> int:
        """Return the number of stored documents whose recorded Origin is ``origin``.

        Selects by the ``origin`` column, so the count does not depend on where the
        source files are now (or whether they still exist).
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM documents WHERE origin = ?",
            (origin,),
        ).fetchone()
        return int(row["cnt"])

    def _unchanged_row(self, file_path: str, content_hash: str) -> sqlite3.Row | None:
        """Return the stored row for ``file_path`` when its content hash is unchanged.

        The row holds ``id``, ``uuid``, ``model_type``, ``origin``, and
        ``relative_name``. Returns ``None`` when no row exists, the hash differs,
        or the stored ``content_hash`` is NULL (pre-migration rows), so the file
        is re-ingested and the hash gets populated.
        """
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT id, uuid, model_type, origin, relative_name, content_hash "
            "FROM documents WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if row is None or row["content_hash"] is None or row["content_hash"] != content_hash:
            return None
        return row

    def _refresh_provenance(self, row: sqlite3.Row, origin: Origin, rel: str) -> None:
        """Set an unchanged row's Origin and Relative_Name (and Doc_ID) to this scan's values.

        Documentation rows are re-keyed to ``doc_id(origin, rel)``; OSCAL documents
        keep their content UUID. If another row already holds the new Doc_ID, log a
        warning and update only Origin and Relative_Name. The FTS ``documentation``
        row is keyed by ``documents.id``, so a Doc_ID change needs no FTS update.
        """
        new_uuid = doc_id(origin, rel) if row["model_type"] == "documentation" else row["uuid"]
        if (row["origin"], row["relative_name"], row["uuid"]) == (origin, rel, new_uuid):
            return
        try:
            self._conn.execute(
                "UPDATE documents SET origin = ?, relative_name = ?, uuid = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (origin, rel, new_uuid, row["id"]),
            )
        except sqlite3.IntegrityError:
            self._conn.rollback()
            logger.warning(
                "Doc_ID %s for %s:%s is held by another document; keeping Doc_ID %s",
                new_uuid,
                origin,
                rel,
                row["uuid"],
            )
        except sqlite3.Error as exc:
            self._conn.rollback()
            logger.error("Failed to refresh provenance of document %s: %s", row["uuid"], exc)
            return
        else:
            self._conn.commit()
            return
        try:
            self._conn.execute(
                "UPDATE documents SET origin = ?, relative_name = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (origin, rel, row["id"]),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            logger.error("Failed to refresh provenance of document %s: %s", row["uuid"], exc)

    def _extract_metadata(self, data: dict, model_type: OSCALModelType) -> tuple[str, str] | None:
        """Extract UUID and title from parsed OSCAL JSON data.

        Args:
            data: Full parsed JSON dict (with root key).
            model_type: The detected model type.

        Returns:
            (uuid, title) tuple, or None if extraction fails.
        """
        root_data = data.get(model_type.value, data)
        uuid = root_data.get("uuid")
        title = None
        metadata = root_data.get("metadata")
        if isinstance(metadata, dict):
            title = metadata.get("title")

        if not uuid or not title:
            return None
        return (str(uuid), str(title))

    def _upsert_document(
        self,
        uuid: str,
        title: str,
        model_type: str,
        file_path: str,
        file_size: int,
        file_mtime: float,
        raw_json: str,
        content_hash: str = "",
        *,
        origin: Origin,
        relative_name: str,
    ) -> bool:
        """UPSERT a document row into the documents table.

        Writes ``origin`` and ``relative_name`` on insert and on ``uuid`` conflict.
        A row for the same ``file_path`` under a different ``uuid`` (the content UUID
        or Doc_ID changed) is deleted first, with its child elements and FTS rows, so
        the ``file_path`` uniqueness constraint cannot reject the new row.
        Returns True on success, False on failure.
        """
        try:
            stale = self._conn.execute(
                "SELECT id FROM documents WHERE file_path = ? AND uuid != ?",
                (file_path, uuid),
            ).fetchone()
            if stale is not None:
                self._delete_document_row(stale["id"])
            self._conn.execute(
                """
                INSERT INTO documents
                    (uuid, title, model_type, file_path, file_size,
                     file_mtime, raw_json, content_hash, origin, relative_name,
                     indexed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))
                ON CONFLICT(uuid) DO UPDATE SET
                    title = excluded.title,
                    model_type = excluded.model_type,
                    file_path = excluded.file_path,
                    file_size = excluded.file_size,
                    file_mtime = excluded.file_mtime,
                    raw_json = excluded.raw_json,
                    content_hash = excluded.content_hash,
                    origin = excluded.origin,
                    relative_name = excluded.relative_name,
                    indexed = 0,
                    updated_at = datetime('now')
                """,
                (
                    uuid,
                    title,
                    model_type,
                    file_path,
                    file_size,
                    file_mtime,
                    raw_json,
                    content_hash,
                    origin,
                    relative_name,
                ),
            )
            self._conn.commit()
            return True
        except sqlite3.Error as exc:
            logger.error("Failed to upsert document %s: %s", uuid, exc)
            self._conn.rollback()
            return False

    def _process_json_file(
        self,
        json_file: Path,
        model_type_filter: OSCALModelType | None,
        root: Path,
        origin: Origin,
    ) -> bool:
        """Process a single JSON file for ingestion.

        ``root`` is the Scan_Root; ``origin`` and the file's Relative_Name under
        ``root`` are recorded on the row.

        Returns True if the file was ingested (new or updated).
        """
        file_path_str = str(json_file)
        rel = relative_name(json_file, root)
        try:
            stat = json_file.stat()
        except OSError as exc:
            logger.debug("Cannot stat %s: %s", json_file, exc)
            return False

        file_size = stat.st_size
        file_mtime = stat.st_mtime

        # Read file content in binary mode and compute SHA-256 hash
        try:
            with open(json_file, "rb") as f:
                raw_bytes = f.read()
        except OSError as exc:
            logger.debug("Cannot read %s: %s", json_file, exc)
            return False

        content_hash = hashlib.sha256(raw_bytes).hexdigest()

        # Change detection using content hash; unchanged rows still get this scan's provenance
        unchanged = self._unchanged_row(file_path_str, content_hash)
        if unchanged is not None:
            self._refresh_provenance(unchanged, origin, rel)
            return False

        # Detect model type
        model_type = self._detect_model_type(json_file)
        if model_type is None:
            logger.debug("No OSCAL root key found in %s", json_file)
            return False

        # Apply filter
        if model_type_filter is not None and model_type != model_type_filter:
            return False

        # Parse JSON from already-read bytes
        try:
            raw_json = raw_bytes.decode("utf-8")
            data = json.loads(raw_json)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug("Cannot parse %s: %s", json_file, exc)
            return False

        # Validate with the OSCAL model
        if not self._validate_with_model(data, model_type):
            logger.warning("Skipping invalid document: %s", json_file)
            return False

        # Extract metadata
        meta = self._extract_metadata(data, model_type)
        if meta is None:
            logger.debug("Cannot extract UUID/title from %s", json_file)
            return False

        uuid, title = meta
        return self._upsert_document(
            uuid,
            title,
            model_type.value,
            file_path_str,
            file_size,
            file_mtime,
            raw_json,
            content_hash=content_hash,
            origin=origin,
            relative_name=rel,
        )

    def _process_zip_file(
        self,
        zip_path: Path,
        model_type_filter: OSCALModelType | None,
        root: Path,
        origin: Origin,
    ) -> int:
        """Process a zip file containing OSCAL JSON documents.

        ``root`` is the Scan_Root; each member's Relative_Name is the zip's
        Relative_Name under ``root``, '/', and the member name.

        Returns the number of documents ingested from the zip.
        """
        count = 0
        zip_rel = relative_name(zip_path, root)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for inner_name in zf.namelist():
                    if not inner_name.endswith(".json"):
                        continue
                    # Use zip_path/inner_name as the canonical file_path
                    file_path_str = zip_path.joinpath(inner_name).as_posix()

                    # For zip entries, use the zip file's stat for mtime/size
                    try:
                        zip_stat = zip_path.stat()
                    except OSError:
                        continue

                    try:
                        with zf.open(inner_name) as inner_f:
                            raw_bytes = inner_f.read()
                    except (KeyError, OSError) as exc:
                        logger.debug(
                            "Cannot read %s from %s: %s",
                            inner_name,
                            zip_path,
                            exc,
                        )
                        continue

                    raw_json = raw_bytes.decode("utf-8", errors="replace")
                    file_size = len(raw_bytes)
                    file_mtime = zip_stat.st_mtime

                    # Compute SHA-256 hash of inner file content
                    content_hash = hashlib.sha256(raw_bytes).hexdigest()

                    rel = zip_member_name(zip_rel, inner_name)

                    # Change detection using content hash; unchanged rows still get
                    # this scan's provenance
                    unchanged = self._unchanged_row(file_path_str, content_hash)
                    if unchanged is not None:
                        self._refresh_provenance(unchanged, origin, rel)
                        continue

                    try:
                        data = json.loads(raw_json)
                    except json.JSONDecodeError as exc:
                        logger.debug(
                            "Invalid JSON in %s/%s: %s",
                            zip_path,
                            inner_name,
                            exc,
                        )
                        continue

                    model_type = self._detect_model_type_from_data(data)
                    if model_type is None:
                        continue

                    if model_type_filter is not None and model_type != model_type_filter:
                        continue

                    # Validate with the OSCAL model
                    if not self._validate_with_model(data, model_type):
                        logger.warning(
                            "Skipping invalid document in zip: %s/%s",
                            zip_path,
                            inner_name,
                        )
                        continue

                    meta = self._extract_metadata(data, model_type)
                    if meta is None:
                        continue

                    uuid, title = meta
                    if self._upsert_document(
                        uuid,
                        title,
                        model_type.value,
                        file_path_str,
                        file_size,
                        file_mtime,
                        raw_json,
                        content_hash=content_hash,
                        origin=origin,
                        relative_name=rel,
                    ):
                        count += 1

        except zipfile.BadZipFile as exc:
            logger.debug("Bad zip file %s: %s", zip_path, exc)
        except OSError as exc:
            logger.debug("Cannot open zip %s: %s", zip_path, exc)

        return count

    def _process_markdown_file(self, md_file: Path, root: Path, origin: Origin) -> bool:
        """Process a single markdown file for documentation indexing.

        Reads the file as UTF-8, derives a title from the first markdown
        heading (or falls back to the filename), sets the Doc_ID to
        ``doc_id(origin, <Relative_Name under root>)``, and inserts the
        document and FTS index rows.

        Returns True if the file was ingested (new or updated).
        """
        file_path_str = str(md_file)
        rel = relative_name(md_file, root)

        # Read file content
        try:
            content = md_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Cannot read markdown file %s: %s", md_file, exc)
            return False

        # Skip empty files
        if not content.strip():
            logger.warning("Skipping empty markdown file: %s", md_file)
            return False

        # Derive title from first markdown heading or filename fallback
        heading_match = re.search(r"^#{1,6}\s+(.+)", content, re.MULTILINE)
        if heading_match:
            title = heading_match.group(1).strip()
        else:
            title = md_file.stem.replace("-", " ").replace("_", " ")

        # Doc_ID: path-free and machine-independent (Origin + Relative_Name)
        doc_uuid = doc_id(origin, rel)

        # Compute SHA-256 content hash (binary read, matching utils.py approach)
        try:
            raw_bytes = md_file.read_bytes()
        except OSError as exc:
            logger.warning("Cannot read %s for hashing: %s", md_file, exc)
            return False

        content_hash = hashlib.sha256(raw_bytes).hexdigest()

        # File stat for mtime/size (stored for metadata)
        try:
            stat = md_file.stat()
        except OSError as exc:
            logger.warning("Cannot stat %s: %s", md_file, exc)
            return False

        file_size = stat.st_size
        file_mtime = stat.st_mtime

        # Change detection using content hash; unchanged rows still get this scan's
        # provenance and Doc_ID
        unchanged = self._unchanged_row(file_path_str, content_hash)
        if unchanged is not None:
            self._refresh_provenance(unchanged, origin, rel)
            return False

        # Upsert document row
        if not self._upsert_document(
            doc_uuid,
            title,
            "documentation",
            file_path_str,
            file_size,
            file_mtime,
            content,
            content_hash=content_hash,
            origin=origin,
            relative_name=rel,
        ):
            return False

        # Get the document row id for the FTS index entry
        row = self._conn.execute(
            "SELECT id FROM documents WHERE uuid = ?",
            (doc_uuid,),
        ).fetchone()
        if row is None:
            return False

        row_id = row["id"]

        # Insert into FTS index with entity_type="documentation"
        try:
            # Remove any stale FTS entries for this document
            self._conn.execute(
                "DELETE FROM fts_index WHERE entity_type = 'documentation' AND entity_id = ?",
                (str(row_id),),
            )
            self._conn.execute(
                """
                INSERT INTO fts_index
                    (entity_type, entity_id, title, description, model_type, element_id)
                VALUES (?, ?, ?, ?, ?, '')
                """,
                (
                    "documentation",
                    str(row_id),
                    title,
                    content,
                    "documentation",
                ),
            )
            # Mark as indexed since documentation has no child elements
            self._conn.execute(
                "UPDATE documents SET indexed = 1 WHERE id = ?",
                (row_id,),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to index documentation %s: %s", md_file, exc)
            self._conn.rollback()
            return False

        return True

    # ------------------------------------------------------------------
    # List / query API
    # ------------------------------------------------------------------

    def list_documents(
        self,
        ctx: object | None = None,  # noqa: ARG002 - kept for API parity with MCP tools
        oscal_model_type: OSCALModelType | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> dict:
        """List document summaries (UUID, title, model_type, child_count, sizeInBytes).

        Reads directly from SQLite — no full document loading.
        Triggers ``_ensure_indexed()`` for documents in the result page so
        that ``child_count`` is accurate.

        Args:
            ctx: Optional MCP context (unused, kept for interface consistency).
            oscal_model_type: Filter to a specific model type, or None for all.
            offset: Pagination offset (0-based).
            limit: Maximum number of items to return.

        Returns:
            Page_Response dict with keys: items, total, offset, limit, hasMore.
        """
        # Build WHERE clause
        where_clauses: list[str] = []
        params: list = []
        if oscal_model_type is not None:
            where_clauses.append("d.model_type = ?")
            params.append(oscal_model_type.value)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        # Get total count
        total_row = self._conn.execute(
            f"SELECT COUNT(*) as cnt FROM documents d {where_sql}",  # nosec B608
            params,
        ).fetchone()
        total = total_row["cnt"]

        # Get the page of documents
        page_params = [*params, limit, offset]
        rows = self._conn.execute(
            f"""
            SELECT d.id, d.uuid, d.title, d.model_type, d.file_size, d.indexed
            FROM documents d
            {where_sql}
            ORDER BY d.title COLLATE NOCASE, d.uuid
            LIMIT ? OFFSET ?
            """,  # nosec B608
            page_params,
        ).fetchall()

        # Ensure indexing for documents in this page so child_count is accurate
        for row in rows:
            if not row["indexed"]:
                try:
                    self._ensure_indexed(row["id"])
                except Exception:
                    logger.warning(
                        "Failed to index doc %d for list_documents",
                        row["id"],
                    )

        # Build items with child_count via subquery per document
        items: list[dict] = []
        for row in rows:
            child_count_row = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM child_elements WHERE parent_doc_id = ?",
                (row["id"],),
            ).fetchone()
            items.append(
                {
                    "uuid": row["uuid"],
                    "title": row["title"],
                    "model_type": row["model_type"],
                    "childCount": child_count_row["cnt"],
                    "sizeInBytes": row["file_size"],
                }
            )

        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "hasMore": offset + limit < total,
        }

    def text_search(
        self,
        query_text: str,
        oscal_model_type: OSCALModelType | None = None,
        offset: int = 0,
        limit: int = 10,
    ) -> dict:
        """Ranked full-text search across OSCAL documents and child elements.

        The query is normalized into Search_Terms (``fts_query.normalize_query``) that are
        matched as quoted, column-scoped FTS5 phrases. Entities that match any term are
        returned; those that match every term come first, each group ordered by FTS5
        rank. Documentation is excluded. OSCAL documents not yet indexed are indexed
        first. If the FTS5 ``MATCH`` still raises ``sqlite3.OperationalError``, a
        ``LIKE`` search over the same terms is used instead.

        Args:
            query_text: The search string.
            oscal_model_type: Scope results to a specific model type, or
                None for all types.
            offset: Pagination offset (0-based).
            limit: Maximum number of items to return.

        Returns:
            Page_Response dict with keys: items, total, offset, limit, hasMore, where
            ``limit`` is the number of items returned (the page may be shortened to fit
            ``RESPONSE_BUDGET``). Each item contains: entity_type, entity_id (the
            Element_ID or Document_UUID), title, description (at most 500 characters
            plus ``TRUNCATION_MARK``), model_type, and ``parentDocumentUuid`` on child
            element hits.

        Raises:
            ValueError: If ``query_text`` is not a string.
        """
        terms = normalize_query(query_text)
        if not terms:
            return budget_page([], 0, offset)

        self._index_pending_oscal_documents()

        try:
            rows, total = self._fts_search(terms, oscal_model_type, offset, limit)
        except sqlite3.OperationalError:
            logger.warning("FTS MATCH failed for search terms %r; falling back to LIKE", terms)
            rows, total = self._like_search(terms, oscal_model_type, offset, limit)

        items = [self._text_search_item(row) for row in rows]
        return budget_page(items, total, offset, truncatable=("description",))

    def _index_pending_oscal_documents(self) -> None:
        """Index every OSCAL document with ``indexed=0`` (Requirements 7.2, 7.3).

        A document that fails to index is logged by Document_UUID and skipped; it stays
        ``indexed=0`` and is retried on the next call.
        """
        pending = self._conn.execute(
            "SELECT id, uuid FROM documents WHERE indexed = 0 AND model_type != 'documentation'"
        ).fetchall()
        for row in pending:
            try:
                self._ensure_indexed(row["id"])
            except Exception:
                logger.warning("Failed to index document %s for text_search", row["uuid"])

    @staticmethod
    def _text_search_scope(oscal_model_type: OSCALModelType | None) -> tuple[str, list]:
        """Return the fixed ``fts_index`` scope filter and its bound values."""
        sql = "fts_index.entity_type IN ('document', 'child_element')"
        params: list = []
        if oscal_model_type is not None:
            sql += " AND fts_index.model_type = ?"
            params.append(oscal_model_type.value)
        return sql, params

    def _fts_search(
        self,
        terms: tuple[str, ...],
        oscal_model_type: OSCALModelType | None,
        offset: int,
        limit: int,
    ) -> tuple[list[sqlite3.Row], int]:
        """Run the ranked FTS5 query for ``terms``; return one page of rows and the total.

        May raise ``sqlite3.OperationalError``.
        """
        match_any, match_all = match_expressions(terms)
        scope_sql, scope_params = self._text_search_scope(oscal_model_type)
        hits_params: list = [match_any, *scope_params]
        # Fixed fragments only; values are bound.
        hits_cte = (
            "WITH hits AS ("  # nosec B608
            "SELECT fts_index.rowid AS rid, fts_index.entity_type AS entity_type, "
            "fts_index.entity_id AS entity_id, fts_index.title AS title, "
            "fts_index.description AS description, fts_index.model_type AS model_type, "
            "rank AS r "
            f"FROM fts_index WHERE fts_index MATCH ? AND {scope_sql})"
        )

        total = self._conn.execute(
            f"{hits_cte} SELECT COUNT(*) AS cnt {_TEXT_SEARCH_JOIN}",  # nosec B608
            hits_params,
        ).fetchone()["cnt"]

        # With one term every hit matches all terms, so the subquery is skipped.
        all_terms_params: list = []
        all_terms_sql = "1"
        if len(terms) > 1:
            all_terms_sql = "(h.rid IN (SELECT rowid FROM fts_index WHERE fts_index MATCH ?))"
            all_terms_params = [match_all]

        rows = self._conn.execute(
            f"{hits_cte} "  # nosec B608
            "SELECT h.entity_type, h.title, h.description, h.model_type, "
            "COALESCE(ce.uuid, d.uuid) AS element_id, pd.uuid AS parent_uuid, "
            f"{all_terms_sql} AS all_terms "
            f"{_TEXT_SEARCH_JOIN} "
            "ORDER BY all_terms DESC, h.r, h.rid LIMIT ? OFFSET ?",
            [*hits_params, *all_terms_params, limit, offset],
        ).fetchall()
        return rows, total

    def _like_search(
        self,
        terms: tuple[str, ...],
        oscal_model_type: OSCALModelType | None,
        offset: int,
        limit: int,
    ) -> tuple[list[sqlite3.Row], int]:
        """Case-insensitive ``LIKE`` fallback for ``terms`` (Requirement 8.9).

        Matches entities whose title, description, or Element_ID column (D10) contains
        any term, ordered by the number of terms matched (descending), then title, then
        FTS row id.
        """
        term_sql = (
            "(fts_index.title LIKE ? ESCAPE '\\' OR fts_index.description LIKE ? ESCAPE '\\' "
            "OR fts_index.element_id LIKE ? ESCAPE '\\')"
        )
        term_params: list = []
        for term in terms:
            pattern = fts_query.like_pattern(term)
            term_params += [pattern, pattern, pattern]
        matched_sql = " + ".join(f"(CASE WHEN {term_sql} THEN 1 ELSE 0 END)" for _ in terms)
        any_sql = " OR ".join(term_sql for _ in terms)
        scope_sql, scope_params = self._text_search_scope(oscal_model_type)

        # Fixed fragments only (one per term); values are bound.
        hits_cte = (
            "WITH hits AS ("  # nosec B608
            "SELECT fts_index.rowid AS rid, fts_index.entity_type AS entity_type, "
            "fts_index.entity_id AS entity_id, fts_index.title AS title, "
            "fts_index.description AS description, fts_index.model_type AS model_type, "
            f"({matched_sql}) AS matched "
            f"FROM fts_index WHERE ({any_sql}) AND {scope_sql})"
        )
        hits_params = [*term_params, *term_params, *scope_params]

        total = self._conn.execute(
            f"{hits_cte} SELECT COUNT(*) AS cnt {_TEXT_SEARCH_JOIN}",  # nosec B608
            hits_params,
        ).fetchone()["cnt"]

        rows = self._conn.execute(
            f"{hits_cte} "  # nosec B608
            "SELECT h.entity_type, h.title, h.description, h.model_type, "
            "COALESCE(ce.uuid, d.uuid) AS element_id, pd.uuid AS parent_uuid "
            f"{_TEXT_SEARCH_JOIN} "
            "ORDER BY h.matched DESC, h.title, h.rid LIMIT ? OFFSET ?",
            [*hits_params, limit, offset],
        ).fetchall()
        return rows, total

    @staticmethod
    def _text_search_item(row: sqlite3.Row) -> dict:
        """Build one text_search item from a joined hit row (no internal row ids)."""
        description = row["description"] or ""
        if len(description) > TEXT_SEARCH_DESCRIPTION_CAP:
            description = description[:TEXT_SEARCH_DESCRIPTION_CAP] + TRUNCATION_MARK
        item = {
            "entity_type": row["entity_type"],
            "entity_id": row["element_id"],
            "title": row["title"] or "",
            "description": description,
            "model_type": row["model_type"],
        }
        if row["entity_type"] == "child_element":
            item["parentDocumentUuid"] = row["parent_uuid"]
        return item

    def search_documentation(
        self,
        query_text: str,
        offset: int = 0,
        limit: int = 10,
        *,
        origins: Sequence[Origin] = ORIGINS,
    ) -> dict:
        """Ranked full-text search over Documentation_Documents of the given Origins.

        The query is normalized into Search_Terms (``fts_query.normalize_query``) that are
        matched as quoted FTS5 phrases scoped to ``DOC_SEARCH_COLUMNS`` (title and
        description, D10). Documents that match any term are returned; those that match
        every term come first, each group ordered by FTS5 rank. If the FTS5 ``MATCH``
        raises ``sqlite3.OperationalError``, a ``LIKE`` search over the same terms is used
        instead. There is no internal cap on ``limit``: the tool validates it, and KB_Mode
        needs ``offset + limit`` local hits.

        Args:
            query_text: The search string.
            offset: Pagination offset (0-based).
            limit: Maximum number of items to return.
            origins: Origins of the Documentation_Documents to search.

        Returns:
            Page_Response dict with keys: items, total, offset, limit, hasMore. Each item
            contains ``docId`` (the Doc_ID), ``title``, and ``snippet`` (at most 200
            characters of matched content).

        Raises:
            ValueError: If ``query_text`` is not a string.
        """
        terms = normalize_query(query_text)
        if not terms or not origins:
            return self._page_response([], 0, offset, limit)

        try:
            rows, total = self._doc_fts_search(terms, origins, offset, limit)
        except sqlite3.OperationalError:
            logger.warning(
                "FTS MATCH failed for documentation search terms %r; falling back to LIKE",
                terms,
            )
            rows, total = self._doc_like_search(terms, origins, offset, limit)
            items = [self._doc_like_item(row, terms) for row in rows]
        else:
            items = [self._doc_fts_item(row) for row in rows]
        return self._page_response(items, total, offset, limit)

    @staticmethod
    def _doc_origin_filter(origins: Sequence[Origin]) -> str:
        """Return the fixed ``d.origin IN (?, ...)`` filter with one placeholder per Origin."""
        placeholders = ", ".join("?" for _ in origins)
        return f"AND d.origin IN ({placeholders})"

    def _doc_fts_search(
        self,
        terms: tuple[str, ...],
        origins: Sequence[Origin],
        offset: int,
        limit: int,
    ) -> tuple[list[sqlite3.Row], int]:
        """Run the ranked FTS5 documentation query; return one page of rows and the total.

        May raise ``sqlite3.OperationalError``.
        """
        match_any, match_all = match_expressions(terms, columns=fts_query.DOC_SEARCH_COLUMNS)
        origin_sql = self._doc_origin_filter(origins)
        # Fixed fragments only; values are bound.
        hits_cte = (
            "WITH hits AS ("
            "SELECT fts_index.rowid AS rid, fts_index.entity_id AS entity_id, "
            "fts_index.title AS title, fts_index.description AS description, "
            "snippet(fts_index, 3, '', '', '...', 40) AS snippet, rank AS r "
            "FROM fts_index WHERE fts_index MATCH ? "
            "AND fts_index.entity_type = 'documentation')"
        )
        hits_params: list = [match_any, *origins]

        total = self._conn.execute(
            f"{hits_cte} SELECT COUNT(*) AS cnt {_DOC_SEARCH_JOIN} {origin_sql}",  # nosec B608
            hits_params,
        ).fetchone()["cnt"]

        # With one term every hit matches all terms, so the subquery is skipped.
        all_terms_params: list = []
        all_terms_sql = "1"
        if len(terms) > 1:
            all_terms_sql = "(h.rid IN (SELECT rowid FROM fts_index WHERE fts_index MATCH ?))"
            all_terms_params = [match_all]

        rows = self._conn.execute(
            f"{hits_cte} "  # nosec B608
            "SELECT d.uuid AS doc_id, h.title, h.description, h.snippet, "
            f"{all_terms_sql} AS all_terms "
            f"{_DOC_SEARCH_JOIN} {origin_sql} "
            "ORDER BY all_terms DESC, h.r, h.rid LIMIT ? OFFSET ?",
            [match_any, *all_terms_params, *origins, limit, offset],
        ).fetchall()
        return rows, total

    def _doc_like_search(
        self,
        terms: tuple[str, ...],
        origins: Sequence[Origin],
        offset: int,
        limit: int,
    ) -> tuple[list[sqlite3.Row], int]:
        """Case-insensitive ``LIKE`` fallback for documentation search (Requirement 8.9).

        Matches Documentation_Documents whose title or description contains any term,
        ordered by the number of terms matched (descending), then title, then FTS row id.
        """
        term_sql = (
            "(fts_index.title LIKE ? ESCAPE '\\' OR fts_index.description LIKE ? ESCAPE '\\')"
        )
        term_params: list = []
        for term in terms:
            pattern = fts_query.like_pattern(term)
            term_params += [pattern, pattern]
        matched_sql = " + ".join(f"(CASE WHEN {term_sql} THEN 1 ELSE 0 END)" for _ in terms)
        any_sql = " OR ".join(term_sql for _ in terms)
        origin_sql = self._doc_origin_filter(origins)

        # Fixed fragments only (one per term); values are bound.
        hits_cte = (
            "WITH hits AS ("  # nosec B608
            "SELECT fts_index.rowid AS rid, fts_index.entity_id AS entity_id, "
            "fts_index.title AS title, fts_index.description AS description, "
            f"({matched_sql}) AS matched "
            f"FROM fts_index WHERE ({any_sql}) AND fts_index.entity_type = 'documentation')"
        )
        hits_params = [*term_params, *term_params, *origins]

        total = self._conn.execute(
            f"{hits_cte} SELECT COUNT(*) AS cnt {_DOC_SEARCH_JOIN} {origin_sql}",  # nosec B608
            hits_params,
        ).fetchone()["cnt"]

        rows = self._conn.execute(
            f"{hits_cte} "  # nosec B608
            "SELECT d.uuid AS doc_id, h.title, h.description "
            f"{_DOC_SEARCH_JOIN} {origin_sql} "
            "ORDER BY h.matched DESC, h.title, h.rid LIMIT ? OFFSET ?",
            [*hits_params, limit, offset],
        ).fetchall()
        return rows, total

    @staticmethod
    def _doc_fts_item(row: sqlite3.Row) -> dict:
        """Build one Doc_Result_Item from an FTS hit row (Requirement 10.3).

        A title-only match has an empty description snippet; its snippet is then the start
        of the description.
        """
        snippet = row["snippet"] or row["description"] or ""
        return {
            "docId": row["doc_id"],
            "title": row["title"] or "",
            "snippet": snippet[:DOC_SNIPPET_CAP],
        }

    def _doc_like_item(self, row: sqlite3.Row, terms: tuple[str, ...]) -> dict:
        """Build one Doc_Result_Item from a LIKE hit row, snippeted at the first matched term."""
        description = row["description"] or ""
        lowered = description.lower()
        first = next((t.lower() for t in terms if t.lower() in lowered), terms[0].lower())
        return {
            "docId": row["doc_id"],
            "title": row["title"] or "",
            "snippet": self._extract_like_snippet(description, first, DOC_SNIPPET_CAP),
        }

    def get_documentation(self, doc_id: str) -> DocumentationText | None:
        """Return the Documentation_Document whose Doc_ID equals ``doc_id``, or None.

        The lookup is an exact match on the stored Doc_ID and the ``documentation`` model
        type, so an OSCAL Document_UUID never matches (Requirements 12.4, 12.6). ``lines``
        holds the stored markdown text split with line endings kept.
        """
        row = self._conn.execute(
            "SELECT uuid, title, raw_json FROM documents "
            "WHERE uuid = ? AND model_type = 'documentation'",
            (doc_id,),
        ).fetchone()
        if row is None:
            return None
        text = row["raw_json"] or ""
        return DocumentationText(
            doc_id=row["uuid"],
            title=row["title"] or "",
            lines=tuple(text.splitlines(keepends=True)),
        )

    @staticmethod
    def _extract_like_snippet(text: str, query_lower: str, max_len: int = 200) -> str:
        """Extract a snippet around the first case-insensitive match."""
        if not text:
            return ""
        pos = text.lower().find(query_lower)
        if pos == -1:
            # No match in description; return start of text
            return text[:max_len]
        # Centre the snippet around the match
        half = max_len // 2
        start = max(0, pos - half)
        end = min(len(text), start + max_len)
        # Adjust start if we're near the end
        if end - start < max_len:
            start = max(0, end - max_len)
        snippet = text[start:end]
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        return snippet[:max_len]

    def list_child_elements(
        self,
        ctx: object | None = None,  # noqa: ARG002 - kept for API parity with MCP tools
        parent_doc_uuid: str | None = None,
        element_type: str | None = None,
        offset: int = 0,
        limit: int = 10,
        *,
        element_id: str | None = None,
        title: str | None = None,
        include_raw_json: bool = False,
    ) -> dict:
        """List child element summaries with parent document info.

        Triggers ``_ensure_indexed()`` for relevant parent documents so
        that child elements are available.

        Results are ordered by ``title`` (case-insensitive), then ``uuid``.
        All filters are combined with AND and bound as SQL parameters.

        Args:
            ctx: Optional MCP context (unused, kept for interface consistency).
            parent_doc_uuid: Filter to children of a specific parent document.
            element_type: Filter to a specific child element type.
            offset: Pagination offset (0-based).
            limit: Maximum number of items to return.
            element_id: Keyword-only. Exact match on the child's identifier
                (``ce.uuid``; a UUID or token ID).
            title: Keyword-only. Exact, case-insensitive match on the
                child's title (``COLLATE NOCASE``).
            include_raw_json: Keyword-only. When True, each item also carries
                a ``"raw_json"`` key holding the child's serialized JSON
                string (may be ``None`` if serialization failed at index
                time).

        Returns:
            Page_Response dict with keys: items, total, offset, limit, hasMore.
            Each item has keys ``id``, ``title``, ``element_type``,
            ``description``, ``parentDocumentTitle``, ``parentDocumentUuid``,
            plus ``raw_json`` when ``include_raw_json`` is True.
        """
        # Ensure indexing for relevant parent documents
        if parent_doc_uuid is not None:
            doc_row = self._conn.execute(
                "SELECT id, indexed FROM documents WHERE uuid = ?",
                (parent_doc_uuid,),
            ).fetchone()
            if doc_row is not None and not doc_row["indexed"]:
                try:
                    self._ensure_indexed(doc_row["id"])
                except Exception:
                    logger.warning(
                        "Failed to index doc %s for list_child_elements",
                        parent_doc_uuid,
                    )
        else:
            # Ensure all unindexed documents are indexed
            unindexed = self._conn.execute("SELECT id FROM documents WHERE indexed = 0").fetchall()
            for row in unindexed:
                try:
                    self._ensure_indexed(row["id"])
                except Exception:
                    logger.warning(
                        "Failed to index doc %d for list_child_elements",
                        row["id"],
                    )

        # Build WHERE clause
        where_clauses: list[str] = []
        params: list = []
        if parent_doc_uuid is not None:
            where_clauses.append("d.uuid = ?")
            params.append(parent_doc_uuid)
        if element_type is not None:
            where_clauses.append("ce.element_type = ?")
            params.append(element_type)
        if element_id is not None:
            where_clauses.append("ce.uuid = ?")
            params.append(element_id)
        if title is not None:
            where_clauses.append("ce.title = ? COLLATE NOCASE")
            params.append(title)

        # Only fixed column text is interpolated; values stay bound params.
        raw_json_col = ", ce.raw_json" if include_raw_json else ""

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        # Get total count
        total_row = self._conn.execute(
            f"""
            SELECT COUNT(*) as cnt
            FROM child_elements ce
            JOIN documents d ON ce.parent_doc_id = d.id
            {where_sql}
            """,  # nosec B608
            params,
        ).fetchone()
        total = total_row["cnt"]

        # Get the page
        page_params = [*params, limit, offset]
        rows = self._conn.execute(
            f"""
            SELECT ce.uuid, ce.title, ce.element_type, ce.description,
                   d.title AS parent_title, d.uuid AS parent_uuid{raw_json_col}
            FROM child_elements ce
            JOIN documents d ON ce.parent_doc_id = d.id
            {where_sql}
            ORDER BY ce.title COLLATE NOCASE, ce.uuid
            LIMIT ? OFFSET ?
            """,  # nosec B608
            page_params,
        ).fetchall()

        items: list[dict] = []
        for row in rows:
            item = {
                "id": row["uuid"],
                "title": row["title"],
                "element_type": row["element_type"],
                "description": row["description"],
                "parentDocumentTitle": row["parent_title"],
                "parentDocumentUuid": row["parent_uuid"],
            }
            if include_raw_json:
                item["raw_json"] = row["raw_json"]
            items.append(item)

        return {
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "hasMore": offset + limit < total,
        }

    def get_child_element(
        self,
        element_id: str,
        parent_doc_uuid: str | None = None,
    ) -> dict | None:
        """Retrieve a single child element by its identifier.

        Triggers ``_ensure_indexed()`` for relevant parent documents so
        that child elements are available.

        For elements with token-based IDs (catalog controls/groups),
        ``parent_doc_uuid`` should be provided to disambiguate since
        token IDs are only unique within their containing document.

        For elements with UUIDs, ``parent_doc_uuid`` is optional.

        Args:
            element_id: The element identifier (UUID or token ID).
            parent_doc_uuid: Optional parent document UUID to scope the
                lookup using the composite key ``(uuid, parent_doc_id)``.

        Returns:
            Full element dict including ``raw_json``, or ``None`` if not
            found.  Returns an error dict with
            ``error: "ambiguous_element_id"`` if the element_id matches
            multiple documents and ``parent_doc_uuid`` was not provided.
        """
        if not element_id:
            return None

        # Ensure indexing for relevant parent documents
        if parent_doc_uuid is not None:
            doc_row = self._conn.execute(
                "SELECT id, indexed FROM documents WHERE uuid = ?",
                (parent_doc_uuid,),
            ).fetchone()
            if doc_row is not None and not doc_row["indexed"]:
                try:
                    self._ensure_indexed(doc_row["id"])
                except Exception:
                    logger.warning(
                        "Failed to index doc %s for get_child_element",
                        parent_doc_uuid,
                    )
        else:
            # Ensure all unindexed documents are indexed
            unindexed = self._conn.execute("SELECT id FROM documents WHERE indexed = 0").fetchall()
            for row in unindexed:
                try:
                    self._ensure_indexed(row["id"])
                except Exception:
                    logger.warning(
                        "Failed to index doc %d for get_child_element",
                        row["id"],
                    )

        # Query child elements
        if parent_doc_uuid is not None:
            # Composite key lookup: (uuid, parent_doc_id) via JOIN
            row = self._conn.execute(
                """
                SELECT ce.uuid, ce.title, ce.element_type, ce.description,
                       ce.raw_json,
                       d.title AS parent_title, d.uuid AS parent_uuid
                FROM child_elements ce
                JOIN documents d ON ce.parent_doc_id = d.id
                WHERE ce.uuid = ? AND d.uuid = ?
                """,
                (element_id, parent_doc_uuid),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": row["uuid"],
                "title": row["title"],
                "element_type": row["element_type"],
                "description": row["description"],
                "parentDocumentTitle": row["parent_title"],
                "parentDocumentUuid": row["parent_uuid"],
                "raw_json": row["raw_json"],
            }

        # No parent_doc_uuid — query by uuid alone
        rows = self._conn.execute(
            """
            SELECT ce.uuid, ce.title, ce.element_type, ce.description,
                   ce.raw_json,
                   d.title AS parent_title, d.uuid AS parent_uuid
            FROM child_elements ce
            JOIN documents d ON ce.parent_doc_id = d.id
            WHERE ce.uuid = ?
            """,
            (element_id,),
        ).fetchall()

        if len(rows) == 0:
            return None

        if len(rows) == 1:
            row = rows[0]
            return {
                "id": row["uuid"],
                "title": row["title"],
                "element_type": row["element_type"],
                "description": row["description"],
                "parentDocumentTitle": row["parent_title"],
                "parentDocumentUuid": row["parent_uuid"],
                "raw_json": row["raw_json"],
            }

        # Multiple matches — ambiguous element_id
        matching_docs = [r["parent_uuid"] for r in rows]
        return {
            "error": "ambiguous_element_id",
            "message": (
                f"Element ID '{element_id}' matches elements in "
                f"{len(matching_docs)} documents. Provide "
                f"parent_doc_uuid to disambiguate."
            ),
            "element_id": element_id,
            "matching_documents": matching_docs,
        }

    # ------------------------------------------------------------------
    # Backward-compatible external loading
    # ------------------------------------------------------------------

    def load_external_component_definition(
        self,
        source: str,
        ctx: object | None = None,
    ) -> None:
        """Load an OSCAL Component Definition from a local zip or remote URI.

        Backward-compatible method that mirrors the legacy
        ``ComponentDefinitionStore.load_external_component_definition``
        behavior.

        For local zip files, extracts and ingests all JSON entries.
        For remote URIs, fetches the JSON, validates, and ingests.

        Args:
            source: URI to the Component Definition JSON file or zip archive.
            ctx: Optional MCP context for error reporting.

        Raises:
            ValueError: If remote URIs are not allowed, validation fails,
                or the source is a directory.
        """
        import requests as _requests

        from mcp_server_for_oscal.tools.utils import try_notify_client_error

        def _notify(msg: str) -> None:
            try_notify_client_error(msg, ctx)  # type: ignore[arg-type]

        uri = urlparse(source)

        if uri.scheme in ("", "file"):
            lf = Path(source)
            if lf.is_dir():
                raise ValueError("URI must point to a zip file or JSON component definition")
            if lf.is_file() and lf.name.endswith("zip"):
                self._process_zip_file(
                    lf, OSCALModelType.COMPONENT_DEFINITION, lf.parent, ORIGIN_USER
                )
            return

        if not config.allow_remote_uris:
            msg = (
                f"Remote URI loading is not enabled. "
                f"Set OSCAL_ALLOW_REMOTE_URIS=true to enable. Source: {source}"
            )
            logger.error(msg)
            _notify(msg)
            raise ValueError(msg)

        logger.debug("Fetching remote Component Definition from: %s", source)

        try:
            response = _requests.get(source, timeout=config.request_timeout)
            response.raise_for_status()

            data = response.json()
            if "component-definition" not in data:
                data = {"component-definition": data}

            model_type = self._detect_model_type_from_data(data)
            if model_type != OSCALModelType.COMPONENT_DEFINITION:
                raise ValueError("Remote document is not a Component Definition")

            if not self._validate_with_model(data, model_type):
                raise ValueError("Remote Component Definition failed validation")

            meta = self._extract_metadata(data, model_type)
            if meta is None:
                raise ValueError("Cannot extract UUID/title from remote document")

            uuid_val, title = meta
            raw_json = json.dumps(data)
            content_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
            self._upsert_document(
                uuid_val,
                title,
                model_type.value,
                source,
                len(raw_json),
                0.0,
                raw_json,
                content_hash=content_hash,
                origin=ORIGIN_USER,
                relative_name=uri.path.rsplit("/", 1)[-1],
            )
            logger.info(
                "Successfully loaded remote component definition from: %s",
                source,
            )

        except _requests.Timeout as e:
            msg = (
                f"Request timeout while fetching remote URI "
                f"(timeout={config.request_timeout}s): {source}"
            )
            logger.exception(msg)
            _notify(msg)
            raise ValueError(msg) from e

        except _requests.RequestException as e:
            msg = f"Failed to fetch remote Component Definition: {e}"
            logger.exception(msg)
            _notify(msg)
            raise ValueError(msg) from e

        except json.JSONDecodeError as e:
            msg = f"Failed to parse remote Component Definition JSON: {e}"
            logger.exception(msg)
            _notify(msg)
            raise ValueError(msg) from e

        except ValueError:
            raise

        except Exception as e:
            msg = f"Failed to load or validate remote Component Definition: {e}"
            logger.exception(msg)
            _notify(msg)
            raise ValueError(msg) from e
