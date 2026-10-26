"""
Tests for the main module.
"""

import hashlib
import importlib
import json
import logging
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid5

import pytest

from mcp_server_for_oscal import main as main_module
from mcp_server_for_oscal.config import config as real_config
from mcp_server_for_oscal.main import _warm_metaschema_index, main
from mcp_server_for_oscal.tools.oscal_store import ORIGIN_USER, OscalStore, doc_id
from tests.tools.test_oscal_store_migration import LegacyDb, LegacyDoc, build_legacy_db


class TestMain:
    """Test cases for the main function and module."""

    @pytest.fixture(autouse=True)
    def skip_integrity_check(self):
        """These tests cover CLI and transport handling, not bundle integrity.

        Startup integrity is covered by TestStartupIntegrity below and
        tests/test_file_integrity_integration.py.
        """
        with patch("mcp_server_for_oscal.main.verify_bundled_content"):
            yield

    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration object."""
        config = Mock()
        config.aws_profile = "default"
        config.log_level = "INFO"
        return config

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = Mock()
        mcp.run = Mock()
        return mcp

    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    @patch("sys.argv", ["main.py"])
    def test_main_default_arguments(self, mock_logging_config, mock_config, mock_mcp):
        """Test main function with default arguments."""
        # Setup mocks
        mock_config.aws_profile = "default"
        mock_config.log_level = "INFO"
        mock_config.transport = "stdio"

        # Execute test
        main()

        # Verify configuration update was called with None values (defaults)
        mock_config.update_from_args.assert_called_once_with(
            bedrock_model_id=None,
            knowledge_base_id=None,
            log_level="INFO",
            transport="stdio",
        )

        # Verify transport validation was called
        mock_config.validate_transport.assert_called_once()

        # Verify logging configuration
        mock_logging_config.assert_called_once_with(level="INFO")

        # Verify MCP server was started with configured transport
        mock_mcp.run.assert_called_once_with(transport="stdio")

    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    @patch(
        "sys.argv",
        [
            "main.py",
            "--aws-profile",
            "test-profile",
            "--log-level",
            "DEBUG",
            "--bedrock-model-id",
            "test-model",
            "--knowledge-base-id",
            "test-kb",
        ],
    )
    def test_main_with_arguments(self, mock_logging_config, mock_config, mock_mcp):
        """Test main function with command line arguments."""
        # Setup mocks
        mock_config.aws_profile = "test-profile"
        mock_config.log_level = "DEBUG"
        mock_config.transport = "stdio"

        # Execute test
        main()

        # Verify configuration update was called with provided values
        mock_config.update_from_args.assert_called_once_with(
            bedrock_model_id="test-model",
            knowledge_base_id="test-kb",
            log_level="DEBUG",
            transport="stdio",
        )

        # Verify transport validation was called
        mock_config.validate_transport.assert_called_once()

        # Verify logging configuration
        mock_logging_config.assert_called_once_with(level="DEBUG")

        # Verify MCP server was started with configured transport
        mock_mcp.run.assert_called_once_with(transport="stdio")

    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    @patch("mcp_server_for_oscal.main.logger")
    @patch("sys.argv", ["main.py"])
    def test_main_mcp_server_error(self, mock_logger, mock_logging_config, mock_config, mock_mcp):
        """Test main function when MCP server fails to start."""
        # Setup mocks
        mock_config.aws_profile = "default"
        mock_config.log_level = "INFO"
        mock_config.transport = "stdio"

        # Make MCP server fail
        mock_mcp.run.side_effect = Exception("Server failed to start")

        # Execute test and verify exception is raised
        with pytest.raises(Exception, match="Server failed to start"):
            main()

        # Verify error was logged with transport info
        mock_logger.exception.assert_called_once()
        error_call_args = mock_logger.exception.call_args[0][0]
        assert "Error running MCP server" in error_call_args

    @patch("mcp_server_for_oscal.main.logging.getLogger")
    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    @patch("sys.argv", ["main.py", "--log-level", "DEBUG"])
    def test_main_logger_configuration(
        self, mock_logging_config, mock_config, mock_mcp, mock_get_logger
    ):
        """Test that all loggers are configured with the specified log level."""
        # Setup mocks
        mock_config.aws_profile = "default"
        mock_config.log_level = "DEBUG"
        mock_config.transport = "stdio"

        mock_strands_logger = Mock()
        mock_mcp_logger = Mock()
        mock_main_logger = Mock()

        def get_logger_side_effect(name):
            if name == "strands":
                return mock_strands_logger
            if name == "mcp":
                return mock_mcp_logger
            if name == "mcp_server_for_oscal.main":
                return mock_main_logger
            return Mock()

        mock_get_logger.side_effect = get_logger_side_effect

        # Execute test
        main()

        # Verify all loggers were configured
        mock_strands_logger.setLevel.assert_called_once_with("DEBUG")
        mock_mcp_logger.setLevel.assert_called_once_with("DEBUG")
        mock_main_logger.setLevel.assert_called_once_with("DEBUG")

    @patch("mcp_server_for_oscal.main.argparse.ArgumentParser")
    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    def test_main_argument_parser_setup(
        self, mock_logging_config, mock_config, mock_mcp, mock_parser_class
    ):
        """Test that argument parser is set up correctly."""
        # Setup mocks
        mock_parser = Mock()
        mock_args = Mock()
        mock_args.aws_profile = "test-profile"
        mock_args.log_level = "INFO"
        mock_args.bedrock_model_id = None
        mock_args.knowledge_base_id = None
        mock_args.transport = "stdio"

        mock_parser.parse_args.return_value = mock_args
        mock_parser_class.return_value = mock_parser

        mock_config.aws_profile = "test-profile"
        mock_config.log_level = "INFO"
        mock_config.transport = "stdio"

        # Execute test
        main()

        # Verify parser was created with correct description
        mock_parser_class.assert_called_once_with(description="OSCAL MCP Server")

        # Verify all expected arguments were added
        add_argument_calls = mock_parser.add_argument.call_args_list
        argument_names = [call[0][0] for call in add_argument_calls]

        expected_arguments = [
            "--aws-profile",
            "--log-level",
            "--bedrock-model-id",
            "--knowledge-base-id",
            "--transport",
        ]

        for expected_arg in expected_arguments:
            assert expected_arg in argument_names, f"Argument {expected_arg} not found"

    @patch("sys.argv", ["main.py", "--help"])
    def test_main_help_argument(self):
        """Test that help argument works (will cause SystemExit)."""
        with pytest.raises(SystemExit):
            main()

    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    @patch("sys.argv", ["main.py", "--log-level", "INVALID"])
    def test_main_with_invalid_log_level(self, mock_logging_config, mock_config, mock_mcp):
        """Test main function with invalid log level (should still work)."""
        # Setup mocks
        mock_config.aws_profile = "default"
        mock_config.log_level = "INVALID"
        mock_config.transport = "stdio"

        # Execute test (should not raise exception)
        main()

        # Verify configuration was updated with invalid log level
        mock_config.update_from_args.assert_called_once_with(
            bedrock_model_id=None,
            knowledge_base_id=None,
            log_level="INVALID",
            transport="stdio",
        )

        # Verify logging was configured with invalid level
        mock_logging_config.assert_called_once_with(level="INVALID")

    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    @patch("sys.argv", ["main.py", "--transport", "streamable-http"])
    def test_main_with_streamable_http_transport(self, mock_logging_config, mock_config, mock_mcp):
        """Test main function with streamable-http transport."""
        # Setup mocks
        mock_config.aws_profile = "default"
        mock_config.log_level = "INFO"
        mock_config.transport = "streamable-http"
        mock_config.host = "0.0.0.0"  # nosec B104 # mocked server, never binds
        mock_config.stateless_http = True

        # Execute test
        main()

        # Verify configuration update was called with streamable-http transport
        mock_config.update_from_args.assert_called_once_with(
            bedrock_model_id=None,
            knowledge_base_id=None,
            log_level="INFO",
            transport="streamable-http",
        )

        # Verify transport validation was called
        mock_config.validate_transport.assert_called_once()

        # Verify MCP server was started with streamable-http transport and that
        # transport settings are passed to run() (MCPServer v2 API)
        mock_mcp.run.assert_called_once_with(
            transport="streamable-http",
            host="0.0.0.0",  # nosec B104 # mocked server, never binds
            stateless_http=True,
        )

    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    @patch("mcp_server_for_oscal.main.logger")
    @patch("sys.argv", ["main.py", "--transport", "invalid-transport"])
    def test_main_with_invalid_transport(
        self, mock_logger, mock_logging_config, mock_config, mock_mcp
    ):
        """Test main function with invalid transport type."""
        # Setup mocks
        mock_config.aws_profile = "default"
        mock_config.log_level = "INFO"
        mock_config.transport = "invalid-transport"

        # Make validate_transport raise ValueError
        mock_config.validate_transport.side_effect = ValueError(
            "Invalid transport type: invalid-transport. Valid options are: stdio, streamable-http"
        )

        # Execute test and verify SystemExit is raised
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

        # Verify error was logged
        mock_logger.exception.assert_called_once()
        error_call_args = mock_logger.exception.call_args[0]
        assert "Transport configuration error" in error_call_args[0]

    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    @patch("mcp_server_for_oscal.main.logger")
    @patch("sys.argv", ["main.py"])
    def test_main_logs_transport_on_startup(
        self, mock_logger, mock_logging_config, mock_config, mock_mcp
    ):
        """Test that transport method is logged during startup."""
        # Setup mocks
        mock_config.aws_profile = "default"
        mock_config.log_level = "INFO"
        mock_config.transport = "stdio"

        # Execute test
        main()

        # Verify transport was logged during startup
        mock_logger.info.assert_called()
        info_calls = [call[0] for call in mock_logger.info.call_args_list]
        transport_logged = any("transport" in str(call).lower() for call in info_calls)
        assert transport_logged, "Transport method should be logged during startup"

    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    @patch("sys.argv", ["main.py", "--transport", ""])
    def test_main_with_empty_transport(self, mock_logging_config, mock_config, mock_mcp):
        """Test main function with empty transport string."""
        # Setup mocks
        mock_config.aws_profile = "default"
        mock_config.log_level = "INFO"
        mock_config.transport = ""

        # Make validate_transport raise ValueError for empty string
        mock_config.validate_transport.side_effect = ValueError(
            "Invalid transport type: . Valid options are: stdio, streamable-http"
        )

        # Execute test and verify SystemExit is raised
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

        # Verify configuration was updated with empty transport
        mock_config.update_from_args.assert_called_once_with(
            bedrock_model_id=None,
            knowledge_base_id=None,
            log_level="INFO",
            transport="",
        )

    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    @patch("mcp_server_for_oscal.main.logger")
    @patch("sys.argv", ["main.py", "--transport", "STDIO"])
    def test_main_with_uppercase_transport(
        self, mock_logger, mock_logging_config, mock_config, mock_mcp
    ):
        """Test main function with uppercase transport (should fail validation)."""
        # Setup mocks
        mock_config.aws_profile = "default"
        mock_config.log_level = "INFO"
        mock_config.transport = "STDIO"

        # Make validate_transport raise ValueError for uppercase
        mock_config.validate_transport.side_effect = ValueError(
            "Invalid transport type: STDIO. Valid options are: stdio, streamable-http"
        )

        # Execute test and verify SystemExit is raised
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1

        # Verify error was logged
        mock_logger.exception.assert_called_once()
        error_call_args = mock_logger.exception.call_args[0]
        assert "Transport configuration error" in error_call_args[0]

    @patch("mcp_server_for_oscal.main.mcp")
    @patch("mcp_server_for_oscal.main.config")
    @patch("mcp_server_for_oscal.main.logging.basicConfig")
    @patch("mcp_server_for_oscal.main.logger")
    @patch("sys.argv", ["main.py"])
    def test_main_logs_specific_transport_method(
        self, mock_logger, mock_logging_config, mock_config, mock_mcp
    ):
        """Test that the specific transport method is logged with correct format."""
        # Setup mocks
        mock_config.aws_profile = "default"
        mock_config.log_level = "INFO"
        mock_config.transport = "streamable-http"

        # Execute test
        main()

        # Verify specific transport was logged
        mock_logger.info.assert_called()
        info_calls = mock_logger.info.call_args_list

        # Look for the specific log message about transport
        transport_log_found = False
        for call in info_calls:
            if len(call[0]) >= 2 and "Starting MCP Server `%s` v%s with transport:" in call[0][0]:
                assert call[0][3] == "streamable-http"
                transport_log_found = True
                break

        assert transport_log_found, "Specific transport method should be logged during startup"


_CDEF_ENV = "OSCAL_COMPONENT_DEFINITIONS_DIR"
_MAIN_LOGGER = "mcp_server_for_oscal.main"


class TestDeprecatedSettings:
    """Startup deprecation warning for OSCAL_COMPONENT_DEFINITIONS_DIR (Req 8)."""

    @pytest.fixture
    def startup_mocks(self, monkeypatch):
        """Patch main()'s side effects and isolate the real config singleton.

        update_from_args mutates the module-level config, so pin the attributes
        it may touch; monkeypatch restores them after the test.
        """
        monkeypatch.setattr(real_config, "transport", "stdio")
        monkeypatch.setattr(real_config, "log_level", "INFO")
        monkeypatch.setattr(real_config, "bedrock_model_id", real_config.bedrock_model_id)
        monkeypatch.setattr(real_config, "knowledge_base_id", real_config.knowledge_base_id)
        monkeypatch.setattr("sys.argv", ["main.py"])
        with (
            patch("mcp_server_for_oscal.main.mcp") as mock_mcp,
            patch("mcp_server_for_oscal.main.verify_bundled_content"),
            patch("mcp_server_for_oscal.main._init_oscal_store") as mock_init,
            patch("mcp_server_for_oscal.main._setup_tools") as mock_setup,
            patch("mcp_server_for_oscal.main.logging.basicConfig"),
        ):
            yield {"mcp": mock_mcp, "init": mock_init, "setup": mock_setup}

    @staticmethod
    def _cdef_warnings(caplog):
        return [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and _CDEF_ENV in r.getMessage()
        ]

    @pytest.mark.usefixtures("startup_mocks")
    @pytest.mark.parametrize("value", ["/custom/comp_defs", ""])
    def test_warns_once_when_cdef_dir_set(self, monkeypatch, caplog, value):
        monkeypatch.setenv(_CDEF_ENV, value)
        with caplog.at_level(logging.WARNING, logger=_MAIN_LOGGER):
            main()
        records = self._cdef_warnings(caplog)
        assert len(records) == 1
        assert "OSCAL_DOCUMENTS_DIR" in records[0].getMessage()

    def test_warns_once_with_streamable_http(self, startup_mocks, monkeypatch, caplog):
        monkeypatch.setenv(_CDEF_ENV, "/custom/comp_defs")
        monkeypatch.setattr("sys.argv", ["main.py", "--transport", "streamable-http"])
        with caplog.at_level(logging.WARNING, logger=_MAIN_LOGGER):
            main()
        records = self._cdef_warnings(caplog)
        assert len(records) == 1
        assert "OSCAL_DOCUMENTS_DIR" in records[0].getMessage()
        assert startup_mocks["mcp"].run.call_args.kwargs["transport"] == ("streamable-http")

    @pytest.mark.usefixtures("startup_mocks")
    def test_no_warning_when_cdef_dir_unset(self, monkeypatch, caplog):
        monkeypatch.delenv(_CDEF_ENV, raising=False)
        with caplog.at_level(logging.WARNING, logger=_MAIN_LOGGER):
            main()
        assert self._cdef_warnings(caplog) == []

    def test_cdef_dir_has_no_effect_on_startup(self, startup_mocks, monkeypatch, caplog):
        mock_init, mock_setup = startup_mocks["init"], startup_mocks["setup"]

        monkeypatch.setenv(_CDEF_ENV, "/custom/comp_defs")
        with caplog.at_level(logging.WARNING, logger=_MAIN_LOGGER):
            main()
        init_set, setup_set = mock_init.call_args_list, mock_setup.call_args_list
        mock_init.reset_mock()
        mock_setup.reset_mock()

        monkeypatch.delenv(_CDEF_ENV, raising=False)
        with caplog.at_level(logging.WARNING, logger=_MAIN_LOGGER):
            main()

        assert init_set == mock_init.call_args_list
        assert setup_set == mock_setup.call_args_list
        assert len(init_set) == 1
        assert len(setup_set) == 1

    def test_cdef_dir_not_read_outside_config(self):
        src = Path(__file__).parent.parent / "src" / "mcp_server_for_oscal"
        attr_files, env_files = set(), set()
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "component_definitions_dir" in text:
                attr_files.add(path.name)
            if _CDEF_ENV in text:
                env_files.add(path.name)
        assert attr_files == {"config.py"}
        assert env_files <= {"config.py", "main.py"}
        assert "config.py" in env_files


class TestStartupIntegrity:
    """main() verifies bundled content first and exits 2 on failure (Req 2.3, 2.4, 2.5)."""

    @pytest.fixture
    def startup(self, monkeypatch):
        """Patch the side effects after verification; attach them to one parent mock
        so tests can assert call order."""
        monkeypatch.setattr("sys.argv", ["main.py"])
        order = Mock()
        with (
            patch("mcp_server_for_oscal.main.config") as mock_config,
            patch("mcp_server_for_oscal.main.mcp") as mock_mcp,
            patch("mcp_server_for_oscal.main._warm_metaschema_index") as mock_warm,
            patch("mcp_server_for_oscal.main._init_oscal_store") as mock_init,
            patch("mcp_server_for_oscal.main._setup_tools") as mock_setup,
            patch("mcp_server_for_oscal.main.logging.basicConfig"),
        ):
            mock_config.log_level = "INFO"
            mock_config.transport = "stdio"
            order.attach_mock(mock_warm, "warm")
            order.attach_mock(mock_init, "init_store")
            order.attach_mock(mock_setup, "setup_tools")
            mock_mcp.run = order.run
            yield order

    def test_exit_2_when_bundle_missing(self, startup, monkeypatch, tmp_path):
        # tmp_path has no bundled content dirs, so the real check fails
        monkeypatch.setattr("mcp_server_for_oscal.main.PACKAGE_DIR", tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 2
        assert startup.mock_calls == []

    def test_verification_runs_before_store_and_tools(self, startup):
        with patch("mcp_server_for_oscal.main.verify_bundled_content") as mock_verify:
            startup.attach_mock(mock_verify, "verify")
            main()
        names = [c[0] for c in startup.mock_calls]
        assert names[:4] == ["verify", "warm", "init_store", "setup_tools"]


class TestWarmMetaschemaIndex:
    """_warm_metaschema_index() loads the index at startup and never exits (Req 2.3, 4.8)."""

    @staticmethod
    def _warnings(caplog) -> list[logging.LogRecord]:
        return [
            r
            for r in caplog.records
            if r.name == "mcp_server_for_oscal.main" and r.levelno == logging.WARNING
        ]

    def test_loads_bundled_index_without_warnings(self, reset_metaschema_index, caplog):
        from mcp_server_for_oscal.tools import metaschema_index as msi

        with caplog.at_level(logging.WARNING):
            _warm_metaschema_index()
        assert msi._index is not None
        assert self._warnings(caplog) == []

    def test_bundle_failure_logs_warning_and_returns(
        self, reset_metaschema_index, monkeypatch, tmp_path, caplog
    ):
        from mcp_server_for_oscal.tools import metaschema_index as msi

        # An empty dir has no manifest, so the real get_index() raises MetaschemaLoadError
        monkeypatch.setattr(msi, "BUNDLE_DIR", tmp_path)
        with caplog.at_level(logging.WARNING):
            _warm_metaschema_index()  # no exception, no SystemExit
        warnings = self._warnings(caplog)
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None
        assert isinstance(warnings[0].exc_info[1], msi.MetaschemaLoadError)
        assert msi._index is None  # next call retries

    def test_one_warning_per_module_with_load_errors(
        self, reset_metaschema_index, monkeypatch, tmp_path, caplog
    ):
        from mcp_server_for_oscal.tools import metaschema_index as msi

        bundle = tmp_path / "oscal_metaschema"
        shutil.copytree(msi.BUNDLE_DIR, bundle)
        # Listed in the manifest but missing: a per-module LoadError, not a bundle failure
        (bundle / "oscal_catalog_metaschema.xml").unlink()
        monkeypatch.setattr(msi, "BUNDLE_DIR", bundle)
        with caplog.at_level(logging.WARNING):
            _warm_metaschema_index()
        load_errors = msi.get_index().load_errors
        assert "oscal_catalog_metaschema.xml" in load_errors
        warnings = self._warnings(caplog)
        assert len(warnings) == len(load_errors)
        assert {w.args[0] for w in warnings} == set(load_errors)
        # Client-safe LoadError text only: no absolute paths in the log line
        assert all(str(tmp_path) not in w.getMessage() for w in warnings)


class TestBuildServer:
    """build_server() and _setup_tools(server) (tool-context-optimization Req 8.1)."""

    def test_build_server_returns_fresh_server_with_instructions(self):
        from mcp_server_for_oscal import main as main_module
        from mcp_server_for_oscal.tools.descriptions import SERVER_INSTRUCTIONS

        server = main_module.build_server()
        assert server is not main_module.mcp
        assert server is not main_module.build_server()
        assert server.instructions == SERVER_INSTRUCTIONS
        assert server.name == real_config.server_name

    def test_setup_tools_registers_on_given_server_only(self):
        import asyncio

        from mcp_server_for_oscal import main as main_module
        from mcp_server_for_oscal.tools import get_tool_list
        from mcp_server_for_oscal.tools.descriptions import ABOUT_DESCRIPTION

        before = {t.name for t in asyncio.run(main_module.mcp.list_tools())}
        server = main_module.build_server()
        main_module._setup_tools(server)

        tools = {t.name: t for t in asyncio.run(server.list_tools())}
        assert set(tools) == {fn.__name__ for fn in get_tool_list()} | {"about"}
        assert tools["about"].description == ABOUT_DESCRIPTION
        assert {t.name for t in asyncio.run(main_module.mcp.list_tools())} == before


# Module singletons that `_install_store` sets: (module path, attribute name)
_STORE_SINGLETONS: tuple[tuple[str, str], ...] = (
    ("mcp_server_for_oscal.tools.list_oscal_resources", "_store"),
    ("mcp_server_for_oscal.tools.query_component_definition", "_oscal_store"),
    ("mcp_server_for_oscal.tools.query_documentation", "_store"),
    ("mcp_server_for_oscal.tools.query_oscal_models", "_store"),
)


def _singletons() -> list:
    """Return the current value of every store singleton, in `_STORE_SINGLETONS` order."""
    return [getattr(importlib.import_module(m), attr) for m, attr in _STORE_SINGLETONS]


class TestStartupStoreWiring:
    """`_install_store` and `_init_oscal_store` (search-fetch-pairs Req 1.4, 1.7, 14.4)."""

    @pytest.fixture(autouse=True)
    def restore_singletons(self):
        """Restore every store singleton and `_user_documents_loaded`; close new stores."""
        modules = [(importlib.import_module(m), attr) for m, attr in _STORE_SINGLETONS]
        saved = [getattr(mod, attr) for mod, attr in modules]
        saved_count = main_module._user_documents_loaded
        yield
        installed = [getattr(mod, attr) for mod, attr in modules]
        for (mod, attr), value in zip(modules, saved, strict=True):
            setattr(mod, attr, value)
        main_module._user_documents_loaded = saved_count
        closed: set[int] = set()
        for store in installed:
            if store is not None and all(store is not s for s in saved) and id(store) not in closed:
                closed.add(id(store))
                store.close()

    @staticmethod
    def _about() -> dict:
        server = main_module.build_server()  # fresh instance, never main.mcp
        main_module._setup_tools(server)
        tool = server._tool_manager.get_tool("about")
        assert tool is not None
        return tool.fn()

    @staticmethod
    def _init(documents_dir: Path, db_path: Path) -> None:
        with patch("mcp_server_for_oscal.main.config") as mock_config:
            mock_config.oscal_store_db_path = str(db_path)
            mock_config.oscal_store_cache_size = 10
            mock_config.oscal_documents_dir = str(documents_dir)
            main_module._init_oscal_store()

    @staticmethod
    def _doc_rows(db_path: Path) -> dict[str, tuple[str, str, str]]:
        """Map documentation `file_path` to `(uuid, origin, relative_name)`."""
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT file_path, uuid, origin, relative_name FROM documents "
                "WHERE model_type = 'documentation'"
            ).fetchall()
        finally:
            conn.close()
        return {fp: (uuid, origin, rel) for fp, uuid, origin, rel in rows}

    @staticmethod
    def _assert_about_path_free(about: dict, *paths: Path) -> None:
        text = json.dumps(about)
        for p in paths:
            assert str(p) not in text
            assert p.as_posix() not in text

    def test_install_store_sets_every_module_singleton(self, tmp_path):
        store = OscalStore(db_path=str(tmp_path / "store.db"), seed_from_bundled=False)
        main_module._install_store(store)

        assert all(s is store for s in _singletons())

    def test_init_records_user_rows_and_about_count(self, tmp_path):
        doc_dir = tmp_path / "docs"
        (doc_dir / "guide").mkdir(parents=True)
        (doc_dir / "one.md").write_text("# One\n\nText.", encoding="utf-8")
        (doc_dir / "guide" / "two.md").write_text("# Two\n\nText.", encoding="utf-8")
        db_path = tmp_path / "store.db"
        sqlite3.connect(db_path).close()  # existing file: no bundled seeding

        self._init(doc_dir, db_path)

        store = _singletons()[0]
        assert isinstance(store, OscalStore)
        assert all(s is store for s in _singletons())
        rows = self._doc_rows(db_path)
        assert sorted(rows.values()) == sorted(
            (doc_id(ORIGIN_USER, rel), ORIGIN_USER, rel) for rel in ("one.md", "guide/two.md")
        )
        assert main_module._user_documents_loaded == 2
        about = self._about()
        assert about["user-documents-loaded"] == 2
        self._assert_about_path_free(about, doc_dir, db_path, tmp_path)

    def test_pre_feature_persistent_db_gets_user_origin_and_doc_ids(self, tmp_path):
        doc_dir = tmp_path / "docs"
        (doc_dir / "guide").mkdir(parents=True)
        (doc_dir / "nested" / "deep").mkdir(parents=True)
        unchanged = doc_dir / "guide" / "intro.md"
        changed = doc_dir / "nested" / "deep" / "notes.md"
        unchanged.write_text("# Intro\n\nUnchanged body.", encoding="utf-8")
        changed.write_text("# Notes\n\nEdited since the old scan.", encoding="utf-8")

        # Pre-feature (user_version 0) rows: absolute-path uuid5 Doc_IDs, no Origin column
        spec = LegacyDb(
            docs=[
                LegacyDoc(
                    model_type="documentation",
                    file_path=str(p),
                    uuid=str(uuid5(NAMESPACE_URL, str(p))),
                    indexed=1,
                    has_fts=True,
                )
                for p in (unchanged, changed)
            ],
            orphans=[],
        )
        db_path = tmp_path / "persistent.db"
        build_legacy_db(db_path, spec)
        # The unchanged file gets its real hash, so the scan takes the unchanged-row path;
        # the other row keeps the stale hash and is re-ingested
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE documents SET content_hash = ? WHERE file_path = ?",
                (hashlib.sha256(unchanged.read_bytes()).hexdigest(), str(unchanged)),
            )
            conn.commit()
        finally:
            conn.close()

        self._init(doc_dir, db_path)

        # Relative_Names are relative to the documents dir, not the basename the
        # pre-feature upgrade infers
        expected = {str(unchanged): "guide/intro.md", str(changed): "nested/deep/notes.md"}
        rows = self._doc_rows(db_path)
        assert set(rows) == set(expected)
        for file_path, rel in expected.items():
            assert rows[file_path] == (doc_id(ORIGIN_USER, rel), ORIGIN_USER, rel)
        store = _singletons()[2]  # query_documentation
        assert store.get_documentation(doc_id(ORIGIN_USER, "guide/intro.md")) is not None
        assert main_module._user_documents_loaded == 2
        about = self._about()
        assert about["user-documents-loaded"] == 2
        self._assert_about_path_free(about, doc_dir, db_path, tmp_path)
