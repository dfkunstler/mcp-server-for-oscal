"""Detect tests that leak files into ``./MagicMock/``.

A bare ``MagicMock`` used as a filesystem path is ``os.PathLike`` and expands to
``MagicMock/<mock-name>/<id>`` relative to the working directory (issue #17). These helpers
compare a cheap fingerprint of that directory before and after a test. They are pure
functions of a root directory so ``tests/test_leak_guard.py`` can exercise them on
``tmp_path``. Nothing here deletes anything.

The fingerprint holds the modification times of ``MagicMock/`` and its immediate
subdirectories. A directory's mtime changes when an entry is added or removed in it, so a
leak is detected without listing the files inside (a stale checkout holds thousands).
"""

import os
from pathlib import Path

import pytest

LEAK_DIR_NAME = "MagicMock"

CLEANUP_HINT = (
    "Remove ./MagicMock/ (gitignored) and patch main._init_oscal_store or set "
    "config.oscal_store_db_path to a tmp_path string."
)

# (exists, mtime_ns of MagicMock/, sorted (subdirectory name, mtime_ns) pairs)
Fingerprint = tuple[bool, int | None, tuple[tuple[str, int], ...]]


def fingerprint(root: Path) -> Fingerprint:
    """Return the state of ``root / "MagicMock"``: existence, mtime, and subdirectory mtimes.

    Costs one ``stat`` and one ``scandir`` of the immediate children. It never lists the
    files inside the subdirectories. A directory that disappears while it is scanned counts
    as absent.
    """
    leak_dir = Path(root) / LEAK_DIR_NAME
    try:
        mtime = leak_dir.stat().st_mtime_ns
        with os.scandir(leak_dir) as entries:
            children = tuple(
                sorted(
                    (entry.name, entry.stat().st_mtime_ns)
                    for entry in entries
                    if entry.is_dir(follow_symlinks=False)
                )
            )
    except (FileNotFoundError, NotADirectoryError):
        return (False, None, ())
    return (True, mtime, children)


def describe_change(before: Fingerprint, after: Fingerprint) -> str | None:
    """Return a failure message when the fingerprints differ, else ``None``."""
    if before == after:
        return None
    if not before[0] and after[0]:
        what = f"test created ./{LEAK_DIR_NAME}/ (a mock object was used as a filesystem path)"
    elif before[0] and not after[0]:
        what = f"test removed ./{LEAK_DIR_NAME}/"
    else:
        old, new = dict(before[2]), dict(after[2])
        changed = sorted(name for name in old.keys() | new.keys() if old.get(name) != new.get(name))
        if changed:
            names = ", ".join(f"./{LEAK_DIR_NAME}/{name}/" for name in changed)
            what = f"test modified {names}"
        else:
            what = f"test modified ./{LEAK_DIR_NAME}/"
    return f"{what}. {CLEANUP_HINT}"


def fail_on_change(before: Fingerprint, after: Fingerprint) -> None:
    """Fail the current test (without a traceback) when the fingerprints differ."""
    message = describe_change(before, after)
    if message:
        pytest.fail(message, pytrace=False)
