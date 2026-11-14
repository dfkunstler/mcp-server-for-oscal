"""Detect tests that leak files into ``./MagicMock/``.

A bare ``MagicMock`` used as a filesystem path is ``os.PathLike`` and expands to
``MagicMock/<mock-name>/<id>`` relative to the working directory (issue #17). These helpers
compare a cheap fingerprint of that directory before and after a test. They are pure
functions of a root directory so ``tests/test_leak_guard.py`` can exercise them on
``tmp_path``. Nothing here deletes anything.

The fingerprint holds entry names: the names in ``MagicMock/`` and, for each immediate
subdirectory, the names in that subdirectory. It deliberately does not use directory
modification times. NTFS updates a directory's timestamp lazily, so on Windows a file added
to an existing directory can leave its mtime unchanged. That is the case that matters: after
a first leak, later leaks land as new files in an existing ``MagicMock/<name>/``. Comparing
names also detects a removed file replaced by a different file (same entry count).
"""

import os
from pathlib import Path

import pytest

LEAK_DIR_NAME = "MagicMock"

CLEANUP_HINT = (
    "Remove ./MagicMock/ (gitignored) and patch main._init_oscal_store or set "
    "config.oscal_store_db_path to a tmp_path string."
)

# (exists, sorted entry names of MagicMock/ or None,
#  sorted (subdirectory name, sorted entry names of that subdirectory) pairs)
Fingerprint = tuple[bool, tuple[str, ...] | None, tuple[tuple[str, tuple[str, ...]], ...]]


def _scan(path: Path) -> tuple[list[str], list[str]]:
    """Return (all entry names, subdirectory names) of ``path``. Symlinked directories are
    not subdirectories."""
    names: list[str] = []
    subdirs: list[str] = []
    with os.scandir(path) as entries:
        for entry in entries:
            names.append(entry.name)
            if entry.is_dir(follow_symlinks=False):
                subdirs.append(entry.name)
    return sorted(names), sorted(subdirs)


def fingerprint(root: Path) -> Fingerprint:
    """Return the state of ``root / "MagicMock"``: existence and entry names.

    Costs one ``scandir`` of ``MagicMock/`` plus one per immediate subdirectory, and only
    when ``MagicMock/`` exists. It is not recursive. A directory that disappears while it is
    scanned counts as absent (a vanished subdirectory is skipped).
    """
    leak_dir = Path(root) / LEAK_DIR_NAME
    try:
        names, subdirs = _scan(leak_dir)
    except (FileNotFoundError, NotADirectoryError):
        return (False, None, ())
    children: list[tuple[str, tuple[str, ...]]] = []
    for sub in subdirs:
        try:
            children.append((sub, tuple(_scan(leak_dir / sub)[0])))
        except (FileNotFoundError, NotADirectoryError):
            continue
    return (True, tuple(names), tuple(children))


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
