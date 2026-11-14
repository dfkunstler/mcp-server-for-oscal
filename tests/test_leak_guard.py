"""Tests for ``tests/leak_guard.py`` (issue #17), run against temporary roots."""

import shutil
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from . import leak_guard
from .leak_guard import LEAK_DIR_NAME, describe_change, fail_on_change, fingerprint


def _make_stale(root: Path, files: int = 50) -> Path:
    """Create ``MagicMock/x/`` holding ``files`` files."""
    sub = root / LEAK_DIR_NAME / "x"
    sub.mkdir(parents=True)
    for i in range(files):
        (sub / f"{i}").write_bytes(b"")
    return sub


class TestFingerprint:
    def test_absent_directory_is_stable(self, tmp_path):
        before = fingerprint(tmp_path)
        assert before == (False, None, ())
        assert describe_change(before, fingerprint(tmp_path)) is None

    def test_creating_leak_changes_fingerprint(self, tmp_path):
        before = fingerprint(tmp_path)
        (tmp_path / LEAK_DIR_NAME / "x").mkdir(parents=True)
        (tmp_path / LEAK_DIR_NAME / "x" / "file").write_bytes(b"")
        message = describe_change(before, fingerprint(tmp_path))
        assert message is not None
        assert LEAK_DIR_NAME in message
        assert "created" in message

    def test_stale_directory_unchanged_across_noop(self, tmp_path):
        _make_stale(tmp_path)
        before = fingerprint(tmp_path)
        assert before[0] is True
        assert describe_change(before, fingerprint(tmp_path)) is None

    def test_adding_file_to_existing_subdirectory_is_detected(self, tmp_path):
        sub = _make_stale(tmp_path)
        before = fingerprint(tmp_path)
        (sub / "new-file").write_bytes(b"")
        message = describe_change(before, fingerprint(tmp_path))
        assert message is not None
        assert f"{LEAK_DIR_NAME}/x/" in message
        assert "modified" in message

    def test_adding_subdirectory_is_detected(self, tmp_path):
        _make_stale(tmp_path)
        before = fingerprint(tmp_path)
        (tmp_path / LEAK_DIR_NAME / "y").mkdir()
        message = describe_change(before, fingerprint(tmp_path))
        assert message is not None
        assert LEAK_DIR_NAME in message

    def test_removing_directory_is_detected(self, tmp_path):
        _make_stale(tmp_path)
        before = fingerprint(tmp_path)
        shutil.rmtree(tmp_path / LEAK_DIR_NAME)
        message = describe_change(before, fingerprint(tmp_path))
        assert message is not None
        assert LEAK_DIR_NAME in message
        assert "removed" in message

    def test_files_directly_in_leak_dir_are_not_listed(self, tmp_path):
        """Only subdirectories are fingerprinted individually."""
        _make_stale(tmp_path)
        (tmp_path / LEAK_DIR_NAME / "loose-file").write_bytes(b"")
        assert [name for name, _ in fingerprint(tmp_path)[2]] == ["x"]

    def test_replacing_file_in_subdirectory_with_same_count_is_detected(self, tmp_path):
        sub = _make_stale(tmp_path, files=3)
        before = fingerprint(tmp_path)
        (sub / "0").unlink()
        (sub / "other").write_bytes(b"")
        message = describe_change(before, fingerprint(tmp_path))
        assert message is not None
        assert f"{LEAK_DIR_NAME}/x/" in message
        assert "modified" in message

    def test_fingerprint_does_not_modify_or_delete(self, tmp_path):
        sub = _make_stale(tmp_path, files=3)
        fingerprint(tmp_path)
        assert sorted(p.name for p in sub.iterdir()) == ["0", "1", "2"]

    def test_describe_change_equal_is_none(self, tmp_path):
        _make_stale(tmp_path)
        state = fingerprint(tmp_path)
        assert describe_change(state, state) is None

    def test_message_ends_with_cleanup_hint(self, tmp_path):
        before = fingerprint(tmp_path)
        (tmp_path / LEAK_DIR_NAME).mkdir()
        message = describe_change(before, fingerprint(tmp_path))
        assert message is not None
        assert message.endswith(leak_guard.CLEANUP_HINT)
        assert "_init_oscal_store" in message
        assert "tmp_path" in message


class TestFailOnChange:
    def test_changed_fingerprint_fails_with_message_naming_magicmock(self, tmp_path):
        before = fingerprint(tmp_path)
        (tmp_path / LEAK_DIR_NAME / "x").mkdir(parents=True)
        with pytest.raises(pytest.fail.Exception, match=LEAK_DIR_NAME):
            fail_on_change(before, fingerprint(tmp_path))

    def test_unchanged_fingerprint_passes(self, tmp_path):
        _make_stale(tmp_path)
        state = fingerprint(tmp_path)
        fail_on_change(state, fingerprint(tmp_path))


# Property 3: Detection. Operations on a temp root under MagicMock/<name>/.
_names = st.sampled_from(["a", "b", "c"])
_files = st.sampled_from(["f1", "f2", "f3"])
_ops = st.lists(
    st.tuples(st.sampled_from(["add", "remove"]), _names, _files),
    max_size=8,
)


class TestDetectionProperty:
    @settings(max_examples=50, deadline=None)
    @given(initial=st.booleans(), ops=_ops)
    def test_describe_change_is_none_exactly_when_fingerprints_equal(self, initial, ops):
        """**Validates: Requirements 2.3, 1.3**

        ``describe_change`` is ``None`` exactly when the fingerprints are equal, and any
        operation that adds or removes an entry makes them differ.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if initial:
                _make_stale(root, files=3)
            before = fingerprint(root)

            effective = False
            for op, name, fname in ops:
                target = root / LEAK_DIR_NAME / name / fname
                if op == "add":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists():
                        target.write_bytes(b"")
                        effective = True
                elif target.exists():
                    target.unlink()
                    effective = True

            after = fingerprint(root)
            message = describe_change(before, after)

            assert (message is None) == (before == after)
            if effective:
                assert message is not None
                assert LEAK_DIR_NAME in message
