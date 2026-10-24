#!/usr/bin/env python3
"""Update the bundled NIST OSCAL Metaschema sources from a pinned upstream commit.

Downloads the GitHub commit archive ``usnistgov/OSCAL/archive/<commit>.zip`` over HTTPS
with default certificate and host-name verification, checks it, and atomically replaces
the Metaschema bundle with byte-identical copies of
``src/metaschema/oscal_*_metaschema.xml`` and ``src/metaschema/shared-constraints/*.ent``.

Checks, in order (any failure prints ``ERROR: <step>: <detail>``, exits 1, and leaves the
bundle untouched):

1. ``--commit`` is exactly 40 lowercase hex characters (the URL is built from it only).
2. The download completes within 300 s total and is at most 50 MB.
3. The zip comment equals the commit (GitHub writes the commit SHA as the comment).
4. At least one module and one ``.ent`` file match the exact member names.
5. Every module declares exactly one ``<schema-version>`` equal to ``--version``.

The existing ``hashes.json`` files are carried over and are stale until
``hatch run rehash``. Only the stdlib and the stdlib-only
``mcp_server_for_oscal.tools.metaschema_index`` module are used.

Usage (normally via ``hatch run update-oscal-schemas``):
    hatch run python bin/update_oscal_metaschema.py --version 1.2.3 \\
        --commit e061961c7702afeabffdf5e59894d35e748dade1 \\
        --dest src/mcp_server_for_oscal/oscal_metaschema

Spec: .kiro/specs/metaschema-constraints (Requirement 3), GitHub issue #20.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import ssl
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Final
from xml.parsers.expat import ExpatError

from mcp_server_for_oscal.tools.metaschema_index import read_schema_versions

REPOSITORY: Final = "usnistgov/OSCAL"
ARCHIVE_URL_PREFIX: Final = f"https://github.com/{REPOSITORY}/archive/"
DOWNLOAD_DEADLINE_S: Final = 300.0
MAX_ARCHIVE_BYTES: Final = 50 * 1024 * 1024
_CHUNK: Final = 64 * 1024
_COMMIT_RE: Final = re.compile(r"[0-9a-f]{40}")

MODULE_GLOB: Final = "oscal_*_metaschema.xml"
ENT_DIR: Final = "shared-constraints"
ENT_GLOB: Final = "*.ent"
HASHES: Final = "hashes.json"
README: Final = "README.md"


class UpdateError(Exception):
    """A failed update step. ``step`` names the step for the error message."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


def check_commit(commit: str) -> None:
    """Raise ``UpdateError`` unless ``commit`` is exactly 40 lowercase hex characters."""
    if not _COMMIT_RE.fullmatch(commit):
        raise UpdateError("check commit", f"expected 40 lowercase hex characters, got {commit!r}")


def archive_url(commit: str) -> str:
    """Return the commit archive URL. Built from the validated commit only, never a tag."""
    check_commit(commit)
    return f"{ARCHIVE_URL_PREFIX}{commit}.zip"


def download(
    commit: str,
    *,
    deadline_s: float = DOWNLOAD_DEADLINE_S,
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> bytes:
    """Download the commit archive and return its bytes.

    Uses a default TLS context (certificate chain and host name verified). The deadline
    covers the whole transfer, not each socket operation, and the size cap is enforced
    while reading.
    """
    url = archive_url(commit)
    context = ssl.create_default_context()
    start = time.monotonic()
    try:
        # S310/B310 suppressed: the URL is the fixed https://github.com/ prefix plus a
        # commit validated as 40 lowercase hex, so scheme and host cannot come from input.
        with urllib.request.urlopen(url, context=context, timeout=deadline_s) as resp:  # noqa: S310  # nosec B310
            length = resp.headers.get("Content-Length")
            if length is not None and length.isdigit() and int(length) > max_bytes:
                raise UpdateError("download", f"archive is {length} bytes, cap is {max_bytes}")
            buf = bytearray()
            while True:
                remaining = deadline_s - (time.monotonic() - start)
                if remaining <= 0:
                    raise UpdateError("download", f"did not complete within {deadline_s:g} s")
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > max_bytes:
                    raise UpdateError("download", f"archive exceeds the {max_bytes}-byte cap")
    except UpdateError:
        raise
    except TimeoutError as e:
        raise UpdateError("download", f"did not complete within {deadline_s:g} s ({e})") from e
    except (OSError, ValueError) as e:  # URLError and HTTPError are OSError subclasses
        raise UpdateError("download", f"{url}: {e}") from e
    if time.monotonic() - start > deadline_s:
        raise UpdateError("download", f"did not complete within {deadline_s:g} s")
    return bytes(buf)


def select_members(archive: bytes, commit: str) -> dict[str, bytes]:
    """Check the archive and return the Metaschema sources it holds.

    Returns a mapping from bundle-relative path (``oscal_x_metaschema.xml`` or
    ``shared-constraints/x.ent``) to the member's exact bytes. Members are selected by
    exact name only; nothing is extracted to disk, so member paths cannot escape.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive))
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        raise UpdateError("open archive", str(e)) from e

    with zf:
        comment = zf.comment
        if comment != commit.encode("ascii"):
            raise UpdateError(
                "check zip comment", f"expected {commit!r}, got {comment.decode('latin-1')!r}"
            )

        base = f"OSCAL-{commit}/src/metaschema/"
        selected: dict[str, bytes] = {}
        try:
            for info in zf.infolist():
                name = info.filename
                if info.is_dir() or not name.startswith(base):
                    continue
                rel = name[len(base) :]
                head, _, tail = rel.rpartition("/")
                if (head == "" and fnmatchcase(tail, MODULE_GLOB)) or (
                    head == ENT_DIR and "/" not in tail and fnmatchcase(tail, ENT_GLOB)
                ):
                    pass
                else:
                    continue
                if rel in selected:
                    raise UpdateError("select members", f"duplicate archive member {name!r}")
                selected[rel] = zf.read(info)
        except UpdateError:
            raise
        except (zipfile.BadZipFile, OSError, ValueError, EOFError) as e:
            raise UpdateError("extract", str(e)) from e

    modules = sum(1 for rel in selected if "/" not in rel)
    ents = len(selected) - modules
    if modules == 0:
        raise UpdateError("select members", f"no {base}{MODULE_GLOB} in the archive")
    if ents == 0:
        raise UpdateError("select members", f"no {base}{ENT_DIR}/{ENT_GLOB} in the archive")
    return selected


def check_versions(files: dict[str, bytes], version: str) -> None:
    """Require every module to declare exactly one ``<schema-version>`` equal to ``version``.

    ``files`` is the mapping from ``select_members``; ``.ent`` entries are skipped.
    The error names each offending module and the versions it declares.
    """
    problems: list[str] = []
    for rel in sorted(files):
        if "/" in rel:
            continue
        try:
            declared = read_schema_versions(files[rel])
        except ExpatError as e:
            problems.append(f"{rel}: not well-formed XML ({e})")
            continue
        if declared != [version]:
            problems.append(f"{rel} declares {declared!r}")
    if problems:
        raise UpdateError(
            "check schema-version", f"expected exactly [{version!r}]; " + "; ".join(problems)
        )


def render_readme(version: str, commit: str, retrieved: str) -> str:
    """Return the bundle README text (design: Bundle README.md format)."""
    return (
        "# OSCAL Metaschema sources\n"
        "\n"
        f"Source repository: {REPOSITORY}\n"
        f"Tag: v{version}\n"
        f"Commit: {commit}\n"
        f"Retrieved: {retrieved}\n"
        "\n"
        "Byte-identical copies of `src/metaschema/oscal_*_metaschema.xml` and\n"
        "`src/metaschema/shared-constraints/*.ent` at the tag. Do not edit; run\n"
        "`hatch run update-oscal-schemas`, then `hatch run rehash`.\n"
    )


def _write_staging(
    staging: Path, dest: Path, files: dict[str, bytes], version: str, commit: str
) -> None:
    (staging / ENT_DIR).mkdir()
    for rel, data in files.items():
        (staging / rel).write_bytes(data)
    retrieved = datetime.now(UTC).date().isoformat()
    (staging / README).write_bytes(render_readme(version, commit, retrieved).encode("utf-8"))
    # Carry the old manifests over (stale until `hatch run rehash`); absent on first run.
    for rel in (HASHES, f"{ENT_DIR}/{HASHES}"):
        old = dest / rel
        if old.is_file():
            (staging / rel).write_bytes(old.read_bytes())


def _swap(staging: Path, dest: Path) -> None:
    backup: Path | None = None
    if dest.exists():
        backup = Path(tempfile.mkdtemp(prefix=f".{dest.name}.backup-", dir=dest.parent))
        backup.rmdir()  # only reserve a unique name; os.replace needs it absent
        os.replace(dest, backup)
    try:
        os.replace(staging, dest)
    except OSError:
        if backup is not None:
            os.replace(backup, dest)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)


def replace_bundle(version: str, commit: str, dest: Path) -> None:
    """Run every step and atomically replace ``dest`` with the checked sources.

    Raises ``UpdateError`` naming the failed step. On any failure ``dest`` is left as it
    was: all checks run before anything is written, files are written to a staging dir
    next to ``dest`` (removed in ``finally``), and ``dest`` is swapped in with
    ``os.replace``.
    """
    check_commit(commit)
    archive = download(commit)
    files = select_members(archive, commit)
    check_versions(files, version)

    dest = Path(dest)
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{dest.name}.staging-", dir=dest.parent))
    except OSError as e:
        raise UpdateError("write staging", str(e)) from e
    try:
        try:
            _write_staging(staging, dest, files, version, commit)
        except OSError as e:
            raise UpdateError("write staging", str(e)) from e
        try:
            _swap(staging, dest)
        except OSError as e:
            raise UpdateError("replace bundle", str(e)) from e
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--version", required=True, help="OSCAL release, e.g. 1.2.3")
    parser.add_argument(
        "--commit", required=True, help="40-hex commit that tag v<version> points to"
    )
    parser.add_argument("--dest", required=True, type=Path, help="Metaschema bundle directory")
    args = parser.parse_args(argv)

    try:
        replace_bundle(args.version, args.commit, args.dest)
    except UpdateError as e:
        print(f"ERROR: {e.step}: {e.detail}", file=sys.stderr)  # noqa: T201 - CLI output
        return 1
    print(f"Updated {args.dest} to OSCAL v{args.version} ({args.commit}).")  # noqa: T201 - CLI output
    print("Run `hatch run rehash` to regenerate the bundle hashes.json files.")  # noqa: T201 - CLI output
    return 0


if __name__ == "__main__":
    sys.exit(main())
