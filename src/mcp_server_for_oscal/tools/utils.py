"""
Shared utilities for OSCAL MCP tools.
"""

import asyncio
import hashlib
import json
import logging
import re
import warnings
from collections.abc import Mapping
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Final, Literal

import anyio.from_thread
from mcp import MCPDeprecationWarning
from mcp.server.mcpserver import Context
from oscal_bindings.models import (
    AssessmentPlan,
    AssessmentResults,
    Catalog,
    ComponentDefinition,
    MappingCollection,
    PlanOfActionAndMilestones,
    Profile,
    SystemSecurityPlan,
)
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Largest page size accepted by paginate().
MAX_PAGE_LIMIT = 100

# Strong references to fire-and-forget client log tasks; the event loop only
# holds weak references, so an unreferenced task can be garbage-collected
# before it runs.
_background_tasks: set[asyncio.Task] = set()

# MCP client logging is deprecated as of protocol 2026-07-28 (SEP-2577) but is
# still delivered to clients that opt in; server-side logging is done by callers.
warnings.filterwarnings(
    "ignore",
    message="The logging capability is deprecated",
    category=MCPDeprecationWarning,
)


class OSCALModelType(StrEnum):
    """Enumeration of OSCAL model types."""

    # These values are intended to match the root object name in the JSON schema
    CATALOG = "catalog"
    PROFILE = "profile"
    COMPONENT_DEFINITION = "component-definition"
    SYSTEM_SECURITY_PLAN = "system-security-plan"
    ASSESSMENT_PLAN = "assessment-plan"
    ASSESSMENT_RESULTS = "assessment-results"
    PLAN_OF_ACTION_AND_MILESTONES = "plan-of-action-and-milestones"
    MAPPING = "mapping-collection"


# Single source of OSCAL model classes, shared by OscalStore and the validator.
# A plain dict (not a read-only proxy) so tests can use unittest.mock.patch.dict.
# The bindings classes have no common base beyond pydantic's BaseModel.
MODEL_MAP: Final[dict[OSCALModelType, type[BaseModel]]] = {
    OSCALModelType.CATALOG: Catalog,
    OSCALModelType.PROFILE: Profile,
    OSCALModelType.COMPONENT_DEFINITION: ComponentDefinition,
    OSCALModelType.SYSTEM_SECURITY_PLAN: SystemSecurityPlan,
    OSCALModelType.ASSESSMENT_PLAN: AssessmentPlan,
    OSCALModelType.ASSESSMENT_RESULTS: AssessmentResults,
    OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES: PlanOfActionAndMilestones,
    OSCALModelType.MAPPING: MappingCollection,
}


schema_names = {
    OSCALModelType.ASSESSMENT_PLAN: "oscal_assessment-plan_schema",
    OSCALModelType.ASSESSMENT_RESULTS: "oscal_assessment-results_schema",
    OSCALModelType.CATALOG: "oscal_catalog_schema",
    OSCALModelType.COMPONENT_DEFINITION: "oscal_component_schema",
    OSCALModelType.MAPPING: "oscal_mapping_schema",
    OSCALModelType.PROFILE: "oscal_profile_schema",
    OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES: "oscal_poam_schema",
    OSCALModelType.SYSTEM_SECURITY_PLAN: "oscal_ssp_schema",
    "complete": "oscal_complete_schema",
}

# Maps the root JSON key in an OSCAL document to its model type.
# Used for auto-detecting the model type from document content.
ROOT_KEY_TO_MODEL_TYPE: dict[str, OSCALModelType] = {mt.value: mt for mt in OSCALModelType}


def load_oscal_json_schema(model_type: OSCALModelType) -> dict:
    """Load and parse a bundled OSCAL JSON schema by model type.

    Args:
        model_type: The OSCAL model type to load the schema for.

    Returns:
        The parsed JSON schema as a dict.
    """
    schema_base = schema_names[model_type]
    schema_path = Path(__file__).parent.parent / "oscal_schemas" / f"{schema_base}.json"
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


# Bundled schema "$id" values embed the OSCAL release, e.g.
# http://csrc.nist.gov/ns/oscal/1.2.3/oscal-catalog-schema.json
_SCHEMA_ID_VERSION_RE = re.compile(r"/(\d+\.\d+\.\d+)/oscal-[\w-]+-schema\.json$")


def oscal_version_from_schema(schema: dict) -> str:
    """Extract the OSCAL release version from a JSON schema's ``$id``.

    Raises:
        ValueError: If the ``$id`` does not contain a recognizable version.
    """
    schema_id = schema.get("$id", "")
    match = _SCHEMA_ID_VERSION_RE.search(schema_id)
    if not match:
        raise ValueError(f"No OSCAL version found in schema $id: {schema_id!r}")
    return match.group(1)


@cache
def get_bundled_oscal_version() -> str:
    """Return the OSCAL version of the bundled schemas.

    Read from the schemas themselves so it can't drift from what is shipped.
    """
    return oscal_version_from_schema(load_oscal_json_schema(OSCALModelType.CATALOG))


def try_notify_client_error(msg: str, ctx: Context | None) -> None:
    safe_log_mcp(msg, ctx, "error")


def safe_log_mcp(
    msg: str, ctx: Context | None, level: Literal["debug", "info", "warning", "error"]
) -> None:
    if not ctx or not msg:
        return
    log_fn = getattr(ctx, level)
    try:
        loop = asyncio.get_running_loop()
        # Already in async context - can't use asyncio.run()
        task = loop.create_task(log_fn(msg))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return
    except RuntimeError:
        pass
    try:
        # Sync tool running on an MCPServer worker thread - hand the coroutine
        # back to the server's event loop, which owns the session streams.
        anyio.from_thread.run(log_fn, msg)
    except RuntimeError:
        # Not in async context - safe to use asyncio.run()
        asyncio.run(log_fn(msg))


# Hash manifest written by bin/update_hashes.py and read by verify_package_integrity.
MANIFEST_NAME: Final = "hashes.json"
# Top-level keys a manifest may contain; any other key fails verification.
MANIFEST_KEYS: Final = frozenset({"commit", "file_hashes"})


def manifest_bytes(state: Mapping[str, object]) -> bytes:
    """Return the canonical serialization of a ``hashes.json`` manifest.

    The single definition of the manifest format: ``bin/update_hashes.py`` writes it and
    ``verify_package_integrity`` requires a manifest's raw bytes to equal it, so
    formatting-only edits (whitespace, escapes) fail verification. Two-space indent,
    key order as given, ASCII-escaped, UTF-8, LF line endings, no trailing newline.
    """
    return json.dumps(state, indent=2).encode("utf-8")


def write_manifest(directory: Path, state: Mapping[str, object]) -> Path:
    """Write ``state`` as the canonical ``hashes.json`` in ``directory``; return its path.

    Written as bytes so line endings are LF on every OS.
    """
    path = directory / MANIFEST_NAME
    path.write_bytes(manifest_bytes(state))
    return path


def _load_manifest(directory: Path) -> dict:
    """Read, parse, and check the form of ``directory/hashes.json``.

    Raises:
        OSError: The manifest is missing or unreadable.
        ValueError: The manifest is not UTF-8 or not valid JSON (``json.JSONDecodeError``
            is kept as the type for invalid JSON; tests/test_file_integrity.py relies on it).
        RuntimeError: The manifest is not a JSON object, has keys other than
            ``MANIFEST_KEYS``, or is not byte-identical to ``manifest_bytes`` of its content.
    """
    try:
        raw = directory.joinpath(MANIFEST_NAME).read_bytes()
    except OSError:
        # Missing or unreadable manifest: name it and its directory in plain text (the
        # OSError message shows the path as a repr, escaped on Windows). Keep the type.
        logger.exception(f"Missing or unreadable {MANIFEST_NAME} in directory {directory}")
        raise
    try:
        state = json.loads(raw.decode("utf-8"))
    except ValueError:
        # Malformed manifest: log file and directory, keep the original error type.
        logger.exception(f"Malformed manifest {MANIFEST_NAME} in directory {directory}")
        raise

    if not isinstance(state, dict):
        msg = f"Manifest {MANIFEST_NAME} in {directory} is not a JSON object"
        logger.error(msg)
        raise RuntimeError(msg)  # noqa: TRY004 - all manifest failures are RuntimeError
    unexpected = sorted(set(state) - MANIFEST_KEYS)
    if unexpected:
        msg = f"Manifest {MANIFEST_NAME} in {directory} has unexpected keys {unexpected}"
        logger.error(msg)
        raise RuntimeError(msg)
    # Byte-for-byte check: the JSON parse alone accepts whitespace and escape changes.
    if raw != manifest_bytes(state):
        msg = (
            f"Manifest {MANIFEST_NAME} in {directory} is not in canonical form "
            "(regenerate it with bin/update_hashes.py)"
        )
        logger.error(msg)
        raise RuntimeError(msg)
    return state


def verify_package_integrity(directory: Path) -> None:
    """Verify all files in a package directory match their expected SHA-256 hashes.

    This function provides critical security functionality by validating that bundled
    OSCAL schemas and documentation files haven't been tampered with. It reads a
    hashes.json manifest file and verifies that all listed files exist and have
    matching SHA-256 hashes.

    This implements the file integrity verification requirements specified in:
    .kiro/specs/file-integrity-testing/requirements.md - Requirements 1-6

    The function performs comprehensive validation:
    1. Parses the hashes.json manifest and requires it to be byte-identical to the
       canonical form (``manifest_bytes``) written by bin/update_hashes.py, with only
       the keys in ``MANIFEST_KEYS``
    2. Verifies all files listed in the manifest exist
    3. Computes SHA-256 hashes for all files and compares with expected values
    4. Detects orphaned files (present but not in manifest)
    5. Provides detailed logging and error reporting

    Args:
        directory (Path): Path to the package directory containing files and hashes.json.
                         The directory must contain a valid hashes.json file with the format:
                         {
                           "commit": "git_commit_hash",
                           "file_hashes": {
                             "filename1": "sha256_hash1",
                             "filename2": "sha256_hash2"
                           }
                         }

    Returns:
        None: Function completes silently on success

    Raises:
        RuntimeError: hashes.json is not a JSON object, has unexpected top-level keys,
            or is not in canonical form; a listed file is missing; or a file's hash
            doesn't match.
        KeyError: hashes.json has no 'file_hashes' section, or a file in the directory
            isn't listed in the manifest.
        OSError: hashes.json or the directory is missing, or a file can't be read.
        ValueError: hashes.json is not UTF-8 or not valid JSON (json.JSONDecodeError).

    Example:
        >>> from pathlib import Path
        >>> verify_package_integrity(Path("src/mcp_server_for_oscal/oscal_schemas"))
        # Completes silently if all files are valid

        >>> verify_package_integrity(Path("tampered_directory"))
        RuntimeError: File schema.json has been modified; expected hash abc123 != def456

    Security Notes:
        - Uses SHA-256 for cryptographic hash verification
        - Reads files in binary mode to ensure accurate hash computation
        - Fails fast on first integrity violation to prevent use of compromised data
        - Logs all verification activities for security monitoring
        - Designed to be called during server startup to prevent use of tampered files

    Integration:
        This function is typically called during MCP server startup to verify the
        integrity of bundled OSCAL schemas and documentation. The hashes.json files
        are generated at build time using the bin/update_hashes.py script.

        Server startup integration (main.py):
        >>> verify_package_integrity(Path("src/mcp_server_for_oscal/oscal_schemas"))
        >>> verify_package_integrity(Path("src/mcp_server_for_oscal/oscal_docs"))
    """

    logger.info(f"Verifying contents of package {directory.name}")
    state = _load_manifest(directory)

    # Confirm that all files listed in hashes.json actually exist
    for file_path in state["file_hashes"]:
        sfp = directory.joinpath(file_path)
        if not sfp.exists():
            raise RuntimeError(f"File {file_path} missing from package. Directory: {directory}")
        logger.debug("File %s exists", sfp)

    # Validate that all files in directory are unmodified
    for fn in directory.iterdir():
        if fn.is_dir():
            verify_package_integrity(fn)
            continue
        # Skip the hash file itself, which is not included in hashes.json
        if not fn.is_file() or fn.name == "hashes.json":
            continue
        with open(fn, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
            try:
                if h != state["file_hashes"][fn.name]:
                    raise RuntimeError(
                        f"File {fn.name} has been modified; expected hash "
                        f"{state['file_hashes'][fn.name]} != {h}. Directory: {directory}"
                    )
            except KeyError:
                # Unlisted file: log file and directory, keep the KeyError type
                # (tests/test_file_integrity.py relies on it).
                logger.exception(f"Unexpected file {fn.name} in directory {directory}")
                raise
            logger.debug("Hash for file %s matches", fn.name)


# Bundled content directories (relative to the package dir) verified at startup.
BUNDLED_CONTENT_DIRS: Final = ("oscal_schemas", "oscal_metaschema")

# Exceptions verify_package_integrity / verify_bundled_content can raise on a failed
# check: RuntimeError (hash mismatch, missing file), KeyError (unlisted file, manifest
# without file_hashes), OSError (missing manifest or directory, unreadable file),
# ValueError (manifest is not valid JSON; json.JSONDecodeError subclasses it).
INTEGRITY_ERRORS: Final = (RuntimeError, KeyError, OSError, ValueError)


def verify_bundled_content(package_dir: Path) -> None:
    """Verify every bundled content directory under ``package_dir``.

    Calls ``verify_package_integrity`` on each entry of ``BUNDLED_CONTENT_DIRS``, which
    recurses into subdirectories (e.g. ``oscal_metaschema/shared-constraints/``). A
    missing directory fails verification. Raises whatever ``verify_package_integrity``
    raises; all such failures are instances of ``INTEGRITY_ERRORS``.
    """
    for name in BUNDLED_CONTENT_DIRS:
        verify_package_integrity(package_dir / name)


def paginate(
    items: list[dict],
    offset: int = 0,
    limit: int = 10,
) -> dict:
    """Slice a list into a single page and return a Page_Response envelope.

    Args:
        items: The full, ordered list of result dicts.
        offset: Zero-based index of the first item to return.
        limit: Maximum number of items to return (1-100).

    Returns:
        dict with keys: items, total, offset, limit, hasMore.

    Raises:
        ValueError: If offset < 0 or limit < 1 or limit > 100.
    """
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE_LIMIT}, got {limit}")

    total = len(items)
    sliced = items[offset : offset + limit]
    has_more = offset + limit < total

    return {
        "items": sliced,
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": has_more,
    }


# Maximum serialized size, in characters, of a Metaschema tool response.
RESPONSE_BUDGET: Final = 20_000

# Suffix appended to a free-text value shortened by paginate_within_budget().
TRUNCATION_MARK: Final = "…"

_PAGE_KEYS: Final = frozenset({"items", "total", "offset", "limit", "hasMore"})


def _json_size(value: object) -> int:
    """Return the serialized size used for the Response_Budget.

    Default separators and ``ensure_ascii=True`` give a length at least as long as
    the compact or UTF-8 encodings the MCP SDK and Strands produce.
    """
    return len(json.dumps(value))


def paginate_within_budget(
    items: list[dict],
    offset: int,
    limit: int,
    *,
    budget: int = RESPONSE_BUDGET,
    truncatable: tuple[str, ...] = ("description",),
    extra: Mapping[str, object] | None = None,
) -> dict:
    """Paginate like ``paginate`` but keep the serialized page within ``budget``.

    If the requested page is too large, the longest fitting prefix is returned with
    ``limit`` set to the number of items returned and ``hasMore`` recomputed. If the
    single item at ``offset`` alone is too large, its ``truncatable`` string values
    are shortened (longest first, cut at a code-point boundary, suffixed with ``…``)
    and the item gets ``truncated: true``. Other keys, including ``source-file`` and
    ``line``, are kept unchanged.

    Args:
        items: The full, ordered list of result dicts.
        offset: Zero-based index of the first item to return.
        limit: Maximum number of items to return (1-100).
        budget: Maximum ``len(json.dumps(page))``.
        truncatable: Keys of free-text values that may be shortened.
        extra: Additional top-level keys merged into the page (envelope keys win).

    Returns:
        A Page_Response dict: items, total, offset, limit, hasMore, plus ``extra``.

    Raises:
        ValueError: From ``paginate`` if offset or limit is out of range.
        RuntimeError: If one item cannot fit even with every truncatable value
            shortened to the truncation mark (an item shape the caller must avoid).
    """
    page = paginate(items, offset, limit)
    extras = {k: v for k, v in (extra or {}).items() if k not in _PAGE_KEYS}
    page.update(extras)
    if _json_size(page) <= budget or not page["items"]:
        return page

    total = page["total"]
    window: list[dict] = page["items"]

    def build(page_items: list[dict]) -> dict:
        k = len(page_items)
        return {
            "items": page_items,
            "total": total,
            "offset": offset,
            "limit": k,
            "hasMore": offset + k < total,
            **extras,
        }

    # Largest k in [1, len(window)) whose page fits; size grows with k.
    lo, hi = 0, len(window) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _json_size(build(window[:mid])) <= budget:
            lo = mid
        else:
            hi = mid - 1
    if lo >= 1:
        return build(window[:lo])

    # The single item at offset is too large on its own: shorten its free text.
    item = dict(window[0])
    item["truncated"] = True
    fields = sorted(
        (key for key in truncatable if isinstance(item.get(key), str)),
        key=lambda key: len(item[key]),
        reverse=True,
    )
    for key in fields:
        candidate = build([item])
        if _json_size(candidate) <= budget:
            return candidate
        original: str = window[0][key]
        # Longest prefix length in [0, len(original)) that fits with the mark.
        lo, hi = 0, len(original) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            item[key] = original[:mid] + TRUNCATION_MARK
            if _json_size(build([item])) <= budget:
                lo = mid
            else:
                hi = mid - 1
        shortened = original[:lo] + TRUNCATION_MARK
        # Very short values can serialize longer with the mark; keep those as is.
        item[key] = shortened if _json_size(shortened) < _json_size(original) else original

    candidate = build([item])
    if _json_size(candidate) <= budget:
        return candidate
    msg = (
        f"item at offset {offset} exceeds the response budget of {budget} characters "
        f"even with {', '.join(truncatable) or 'no fields'} shortened"
    )
    raise RuntimeError(msg)
