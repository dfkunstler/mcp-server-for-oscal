"""Startup integrity checks for the bundled Metaschema sources (Req 2.3, 2.4, 2.5, 2.6).

Each test runs a real ``verify_bundled_content`` against a temp copy of the bundled
content (``oscal_schemas/`` + ``oscal_metaschema/``) with ``PACKAGE_DIR`` patched, so
the real bundle is never modified. The Metaschema index warm-up runs for real against the
copy; everything after it is mocked.
"""

import logging
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from mcp_server_for_oscal import main as main_module
from mcp_server_for_oscal import oscal_agent
from mcp_server_for_oscal.tools import metaschema_index as msi
from mcp_server_for_oscal.tools.utils import INTEGRITY_ERRORS, verify_bundled_content

SRC_PACKAGE_DIR = Path(main_module.__file__).parent
METASCHEMA = "oscal_metaschema"
SHARED = f"{METASCHEMA}/shared-constraints"
MODULE_FILE = f"{METASCHEMA}/oscal_catalog_metaschema.xml"
ENT_FILE = f"{SHARED}/allowed-values-component-type.ent"


@pytest.fixture
def package_dir(tmp_path: Path) -> Path:
    """Temp package dir holding copies of every bundled content directory."""
    pkg = tmp_path / "pkg"
    for name in ("oscal_schemas", METASCHEMA):
        shutil.copytree(SRC_PACKAGE_DIR / name, pkg / name)
    return pkg


def _flip_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))


# Each tamper returns (file name, directory) that the error log must name.
Tamper = Callable[[Path], tuple[str, Path]]


def _modify_module(pkg: Path) -> tuple[str, Path]:
    _flip_byte(pkg / MODULE_FILE)
    return Path(MODULE_FILE).name, pkg / METASCHEMA


def _modify_ent(pkg: Path) -> tuple[str, Path]:
    _flip_byte(pkg / ENT_FILE)
    return Path(ENT_FILE).name, pkg / SHARED


def _remove_listed_file(pkg: Path) -> tuple[str, Path]:
    (pkg / MODULE_FILE).unlink()
    return Path(MODULE_FILE).name, pkg / METASCHEMA


def _add_unlisted_file(pkg: Path) -> tuple[str, Path]:
    (pkg / SHARED / "extra.ent").write_text("<!-- not in manifest -->\n", encoding="utf-8")
    return "extra.ent", pkg / SHARED


def _remove_manifest(pkg: Path) -> tuple[str, Path]:
    (pkg / SHARED / "hashes.json").unlink()
    return "hashes.json", pkg / SHARED


def _malform_manifest(pkg: Path) -> tuple[str, Path]:
    (pkg / METASCHEMA / "hashes.json").write_text("{not valid json", encoding="utf-8")
    return "hashes.json", pkg / METASCHEMA


TAMPERS: dict[str, Tamper] = {
    "module-byte-changed": _modify_module,
    "ent-byte-changed": _modify_ent,
    "listed-file-missing": _remove_listed_file,
    "unlisted-extra-file": _add_unlisted_file,
    "manifest-missing": _remove_manifest,
    "manifest-malformed": _malform_manifest,
}


@contextmanager
def _real_warm(pkg: Path) -> Iterator[Mock]:
    """Record ``_warm_metaschema_index`` calls while running the real one against ``pkg``.

    The index singleton is reset for the duration (and restored afterwards), so the real
    ``get_index()`` parses the verified copy under ``pkg``, never the source bundle.
    """
    with (
        patch.object(msi, "_index", None),
        patch.object(msi, "BUNDLE_DIR", pkg / METASCHEMA),
        patch.object(
            main_module, "_warm_metaschema_index", wraps=main_module._warm_metaschema_index
        ) as mock_warm,
    ):
        yield mock_warm


@contextmanager
def _patched_server_main(pkg: Path) -> Iterator[tuple[Callable[[], None], Mock]]:
    """Patch ``main.main()`` to verify ``pkg`` and mock every step after the index warm-up."""
    after = Mock()
    with (
        patch("sys.argv", ["main.py"]),
        patch.object(main_module, "PACKAGE_DIR", pkg),
        patch.object(main_module, "config") as mock_config,
        patch.object(main_module, "mcp") as mock_mcp,
        _real_warm(pkg) as mock_warm,
        patch.object(main_module, "_init_oscal_store") as mock_init,
        patch.object(main_module, "_setup_tools") as mock_setup,
        patch.object(main_module.logging, "basicConfig"),
    ):
        mock_config.log_level = "INFO"
        mock_config.transport = "stdio"
        after.attach_mock(mock_warm, "warm")
        after.attach_mock(mock_init, "init_store")
        after.attach_mock(mock_setup, "setup_tools")
        mock_mcp.run = after.run
        yield main_module.main, after


@pytest.fixture
def server_startup(package_dir):
    """Run ``main.main()`` with post-verification steps mocked; return (run, after-mock)."""
    with _patched_server_main(package_dir) as startup:
        yield startup


@pytest.fixture
def agent_startup(monkeypatch, package_dir):
    """Run ``oscal_agent.main()`` with post-verification steps mocked."""
    monkeypatch.setattr("sys.argv", ["agent"])
    monkeypatch.setattr(oscal_agent, "PACKAGE_DIR", package_dir)
    after = Mock()
    with (
        patch.object(oscal_agent, "config") as mock_config,
        _real_warm(package_dir) as mock_warm,
        patch.object(main_module, "_init_oscal_store") as mock_init,
        patch.object(oscal_agent, "_build_session_manager", return_value=(None, None)),
        patch.object(oscal_agent, "_build_conversation_manager", return_value=None),
        patch.object(oscal_agent, "create_oscal_agent", return_value=MagicMock()) as mock_create,
        patch.object(oscal_agent.logging, "basicConfig"),
        patch("builtins.input", side_effect=EOFError),
    ):
        mock_config.aws_profile = None
        mock_config.log_level = "INFO"
        mock_config.agent_max_tokens = 4096
        after.attach_mock(mock_warm, "warm")
        after.attach_mock(mock_init, "init_store")
        after.attach_mock(mock_create, "create_agent")
        yield oscal_agent.main, after


@pytest.fixture(params=["server", "agent"])
def startup(request):
    """Both entry points, each with its post-verification mocks."""
    return request.getfixturevalue(f"{request.param}_startup")


class TestMetaschemaIntegrityAtStartup:
    def test_unmodified_copy_passes(self, startup):
        run, after = startup
        run()  # no SystemExit
        names = [c[0] for c in after.mock_calls]
        # Warm-up runs after verification and before store and tool/agent setup
        assert names[:2] == ["warm", "init_store"]
        assert names[2] in {"setup_tools", "create_agent"}
        # The real warm-up parsed the verified copy into the singleton
        assert msi._index is not None
        assert dict(msi._index.load_errors) == {}

    @pytest.mark.parametrize("failure", ["bundle-unloadable", "module-missing"])
    def test_index_failures_never_stop_startup(self, startup, tmp_path, failure, caplog):
        """Warm-up failures are logged as warnings; startup continues (Req 4.8)."""
        run, after = startup
        bundle = tmp_path / "index-src"
        if failure == "bundle-unloadable":
            bundle.mkdir()  # no manifest: get_index() raises MetaschemaLoadError
        else:
            shutil.copytree(SRC_PACKAGE_DIR / METASCHEMA, bundle)
            (bundle / Path(MODULE_FILE).name).unlink()  # one per-module LoadError (or more)
        # Only the index reads this dir; verification still checks the intact package copy
        with patch.object(msi, "BUNDLE_DIR", bundle), caplog.at_level(logging.WARNING):
            run()  # no SystemExit
        names = [c[0] for c in after.mock_calls]
        assert names[:2] == ["warm", "init_store"]
        assert names[2] in {"setup_tools", "create_agent"}
        warnings = [
            r
            for r in caplog.records
            if r.name == main_module.__name__ and r.levelno == logging.WARNING
        ]
        if failure == "bundle-unloadable":
            assert len(warnings) == 1
            assert msi._index is None
        else:
            assert msi._index is not None
            assert Path(MODULE_FILE).name in msi._index.load_errors
            assert len(warnings) == len(msi._index.load_errors)

    @pytest.mark.parametrize("tamper", TAMPERS.values(), ids=TAMPERS.keys())
    def test_tampered_bundle_exits_2_before_setup(self, startup, package_dir, tamper, caplog):
        run, after = startup
        file_name, directory = tamper(package_dir)
        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc_info:
            run()
        assert exc_info.value.code == 2
        # Nothing after verification ran: no store, no tools, no agent, no serving
        assert after.mock_calls == []
        # The error log names the failing file and its directory (Req 2.4)
        assert file_name in caplog.text
        assert str(directory) in caplog.text


# Every file in the Metaschema bundle: 14 modules, 12 .ent files, README.md, 2 manifests.
BUNDLE_FILES: tuple[str, ...] = tuple(
    sorted(
        p.relative_to(SRC_PACKAGE_DIR).as_posix()
        for p in (SRC_PACKAGE_DIR / METASCHEMA).rglob("*")
        if p.is_file()
    )
)


@pytest.fixture(scope="module")
def shared_package_dir(tmp_path_factory) -> Path:
    """One temp copy of the bundled content, shared by every Hypothesis example.

    Each example modifies one file and restores its bytes afterwards, so the copy is
    unmodified between examples. Module scope keeps Hypothesis away from per-example
    function-scoped fixtures.
    """
    pkg = tmp_path_factory.mktemp("pkg")
    for name in ("oscal_schemas", METASCHEMA):
        shutil.copytree(SRC_PACKAGE_DIR / name, pkg / name)
    return pkg


@st.composite
def single_byte_changes(draw) -> tuple[str, int, int]:
    """A bundle file, a byte offset in it, and a replacement byte != the original."""
    rel = draw(st.sampled_from(BUNDLE_FILES))
    data = (SRC_PACKAGE_DIR / rel).read_bytes()
    offset = draw(st.integers(min_value=0, max_value=len(data) - 1))
    replacement = draw(st.integers(min_value=0, max_value=255).filter(lambda b: b != data[offset]))
    return rel, offset, replacement


class TestSingleByteTamperingProperty:
    """Property 15: Any single-byte change fails startup verification.

    **Validates: Requirements 2.4, 2.6**
    """

    def test_bundle_has_expected_files(self):
        assert len(BUNDLE_FILES) == 29  # 14 modules + 12 .ent + README.md + 2 manifests

    @given(change=single_byte_changes())
    # Indentation space -> tab in a manifest: the JSON still parses to the same object
    @example(change=(f"{METASCHEMA}/hashes.json", 2, 0x09))
    # "commit" key -> "Commit": still canonical JSON, so only the key-set check catches it
    @example(change=(f"{METASCHEMA}/hashes.json", 5, ord("C")))
    def test_any_single_byte_change_fails_verification(self, shared_package_dir, change):
        rel, offset, replacement = change
        target = shared_package_dir / rel
        original = target.read_bytes()
        tampered = bytearray(original)
        tampered[offset] = replacement
        target.write_bytes(bytes(tampered))
        try:
            with pytest.raises(INTEGRITY_ERRORS):
                verify_bundled_content(shared_package_dir)
            with (
                _patched_server_main(shared_package_dir) as (run, after),
                pytest.raises(SystemExit) as exc_info,
            ):
                run()
            assert exc_info.value.code == 2
            assert after.mock_calls == []
        finally:
            target.write_bytes(original)
