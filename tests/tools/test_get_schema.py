"""
Tests for the get_schema tool.
"""

import inspect
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, mock_open, patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

import mcp_server_for_oscal.tools.get_schema as get_schema_module
from mcp_server_for_oscal.tools.get_schema import get_oscal_schema, open_schema_file
from mcp_server_for_oscal.tools.utils import OSCALModelType, schema_names

# Shared helpers for real-file tests (no json.load / file-content mocking).
SCHEMA_DIR = Path(get_schema_module.__file__).parent.parent / "oscal_schemas"
VALID_MODELS = [*(m.value for m in OSCALModelType), "complete"]
SCHEMA_TYPES = ["json", "xsd"]


def expected_text(name: str, schema_type: str) -> str:
    """Independent oracle: the bundled schema file's raw bytes decoded as UTF-8."""
    return (SCHEMA_DIR / f"{schema_names[name]}.{schema_type}").read_bytes().decode("utf-8")


@pytest.fixture
def handle_tracker():
    """Wrap the real ``open_schema_file`` and record every handle it returns.

    Holding the references means ``handle.closed`` reflects explicit closing by
    the tool, not garbage collection.
    """
    handles: list = []

    def _tracking_open(file_name: str):
        handle = open_schema_file(file_name)
        handles.append(handle)
        return handle

    with patch(
        "mcp_server_for_oscal.tools.get_schema.open_schema_file", side_effect=_tracking_open
    ) as mock_open_fn:
        yield handles, mock_open_fn
    for handle in handles:
        handle.close()


class TestGetSchemaBugCondition:
    """Property 1 (bug condition, GitHub #13): XSD requests return the bundled XSD text
    and schema file handles are always closed.

    The valid input domain is finite (9 models x 2 schema types), so it is covered
    exhaustively with parametrize rather than sampled.

    **Validates: Requirements 1.1, 1.2, 1.3, 2.1, 2.3**
    """

    @pytest.mark.parametrize("model_name", VALID_MODELS)
    def test_get_schema_xsd_returns_bundled_text(self, model_name):
        """XSD request returns the bundled XSD file contents exactly."""
        result = get_oscal_schema(None, model_name, "xsd")
        assert result == expected_text(model_name, "xsd")

    def test_get_schema_xsd_does_not_notify_client_error(self):
        """A valid XSD request sends no error notification to the client."""
        ctx = AsyncMock()
        ctx.session.client_params = {}
        result = get_oscal_schema(ctx, "catalog", "xsd")
        assert result == expected_text("catalog", "xsd")
        ctx.error.assert_not_called()

    @pytest.mark.parametrize("schema_type", SCHEMA_TYPES)
    @pytest.mark.parametrize("model_name", VALID_MODELS)
    def test_get_schema_handle_closed(self, handle_tracker, model_name, schema_type):
        """Exactly one schema handle is opened, and it is closed after the call."""
        handles, mock_open_fn = handle_tracker
        get_oscal_schema(None, model_name, schema_type)
        mock_open_fn.assert_called_once_with(f"{schema_names[model_name]}.{schema_type}")
        assert len(handles) == 1
        assert handles[0].closed

    def test_get_schema_handle_closed_on_json_parse_error(self, tmp_path):
        """The handle is closed even when JSON parsing fails."""
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json", encoding="utf-8")
        handles: list = []

        def _open_bad(_file_name: str):
            handle = bad_file.open(encoding="utf-8")
            handles.append(handle)
            return handle

        with patch("mcp_server_for_oscal.tools.get_schema.open_schema_file", side_effect=_open_bad):
            try:
                with pytest.raises(json.JSONDecodeError):
                    get_oscal_schema(None, "catalog", "json")
                assert len(handles) == 1
                assert handles[0].closed
            finally:
                for handle in handles:
                    handle.close()


def expected_json_output(name: str) -> str:
    """Independent oracle for JSON requests: reproduce the tool's exact output from raw bytes."""
    return json.dumps(json.loads(expected_text(name, "json")))


class TestGetSchemaPreservation:
    """Property 2 (preservation, GitHub #13): JSON output and error contracts are unchanged
    for every input outside the bug condition.

    Valid JSON requests are covered exhaustively; invalid inputs are sampled with Hypothesis.
    Test names avoid "xsd" and "closed" so ``-k "xsd or closed"`` selects only Property 1.

    **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**
    """

    @pytest.mark.parametrize("model_name", VALID_MODELS)
    def test_json_output_identical(self, model_name):
        """JSON output is byte-identical to json.dumps(json.load(file)) and parses equal."""
        result = get_oscal_schema(None, model_name, "json")
        assert result == expected_json_output(model_name)
        assert json.loads(result) == json.loads(expected_text(model_name, "json"))

    def test_default_arguments_return_complete_json(self):
        """With no arguments, the tool returns the complete JSON schema."""
        assert get_oscal_schema() == expected_json_output("complete")

    @given(schema_type=st.text().filter(lambda s: s not in SCHEMA_TYPES))
    def test_invalid_schema_type_rejected_without_opening(self, schema_type):
        """Any schema_type other than json/xsd raises the exact ValueError and opens no file."""
        with patch("mcp_server_for_oscal.tools.get_schema.open_schema_file") as mock_open_fn:
            with pytest.raises(
                ValueError, match=f"^{re.escape(f'Invalid schema type: {schema_type}.')}$"
            ):
                get_oscal_schema(None, "catalog", schema_type)
            mock_open_fn.assert_not_called()

    @given(
        model_name=st.text().filter(lambda s: s not in VALID_MODELS),
        schema_type=st.sampled_from(SCHEMA_TYPES),
    )
    def test_invalid_model_rejected_without_opening(self, model_name, schema_type):
        """Any unknown model_name raises the existing ValueError and opens no file."""
        with patch("mcp_server_for_oscal.tools.get_schema.open_schema_file") as mock_open_fn:
            with pytest.raises(ValueError, match=r"^Invalid model: ") as exc_info:
                get_oscal_schema(None, model_name, schema_type)
            assert str(exc_info.value) == (
                f"Invalid model: {model_name}. "
                "Use the tool list_oscal_models to get valid model names."
            )
            mock_open_fn.assert_not_called()


class TestGetSchema:
    """Test cases for the get_oscal_schema tool."""

    @pytest.fixture
    def mock_context(self):
        """Create a mock MCP context."""
        context = AsyncMock()
        context.error = AsyncMock()
        context.session = AsyncMock()
        context.session.client_params = {}
        return context

    # Pass-through wrapper: the real open_schema_file reads the bundled file, while the
    # mock still records which file name the tool asked for.
    @patch("mcp_server_for_oscal.tools.get_schema.open_schema_file", side_effect=open_schema_file)
    def test_get_schema_success_default_params(self, mock_open_schema_file, mock_context):
        """Test successful schema retrieval with default parameters."""
        result = get_oscal_schema(mock_context)

        assert result == expected_json_output("complete")
        mock_open_schema_file.assert_called_once_with("oscal_complete_schema.json")
        mock_context.error.assert_not_called()

    @patch("mcp_server_for_oscal.tools.get_schema.open_schema_file", side_effect=open_schema_file)
    def test_get_schema_success_catalog_model(self, mock_open_schema_file, mock_context):
        """Test successful schema retrieval for catalog model."""
        result = get_oscal_schema(mock_context, model_name="catalog", schema_type="json")

        assert result == expected_json_output("catalog")
        mock_open_schema_file.assert_called_once_with("oscal_catalog_schema.json")

    @patch("mcp_server_for_oscal.tools.get_schema.open_schema_file", side_effect=open_schema_file)
    def test_get_schema_success_ssp_model(self, mock_open_schema_file, mock_context):
        """Test successful schema retrieval for system-security-plan model (mapped to ssp)."""
        result = get_oscal_schema(
            mock_context, model_name="system-security-plan", schema_type="json"
        )

        assert result == expected_json_output("system-security-plan")
        mock_open_schema_file.assert_called_once_with("oscal_ssp_schema.json")

    @patch("mcp_server_for_oscal.tools.get_schema.open_schema_file", side_effect=open_schema_file)
    def test_get_schema_success_poam_model(self, mock_open_schema_file, mock_context):
        """Test successful schema retrieval for plan-of-action-and-milestones model (mapped to poam)."""
        result = get_oscal_schema(
            mock_context, model_name="plan-of-action-and-milestones", schema_type="json"
        )

        assert result == expected_json_output("plan-of-action-and-milestones")
        mock_open_schema_file.assert_called_once_with("oscal_poam_schema.json")

    def test_get_schema_invalid_schema_type(self, mock_context):
        """Test error handling for invalid schema type."""
        # Execute test and verify exception
        with pytest.raises(ValueError, match="Invalid schema type: invalid"):
            get_oscal_schema(mock_context, model_name="catalog", schema_type="invalid")

        # Verify error context call
        mock_context.error.assert_called_once_with("Invalid schema type: invalid.")

    def test_get_schema_invalid_model_name(self, mock_context):
        """Test error handling for invalid model name."""
        # Execute test and verify exception
        with pytest.raises(ValueError, match="Invalid model: invalid-model"):
            get_oscal_schema(mock_context, model_name="invalid-model", schema_type="json")

        # Verify error context call
        mock_context.error.assert_called_once()
        error_call_args = mock_context.error.call_args[0][0]
        assert "Invalid model: invalid-model" in error_call_args
        assert "Use the tool list_oscal_models to get valid model names" in error_call_args

    @patch("mcp_server_for_oscal.tools.get_schema.open_schema_file")
    def test_get_schema_file_not_found(self, mock_open_schema_file, mock_context):
        """Test error handling when schema file is not found."""
        # Setup mocks
        mock_open_schema_file.side_effect = FileNotFoundError("File not found")

        # Execute test and verify exception
        with pytest.raises(FileNotFoundError):
            get_oscal_schema(mock_context, model_name="catalog", schema_type="json")

        # Verify error handling
        mock_context.error.assert_called_once_with(
            "failed to open schema oscal_catalog_schema.json"
        )

    @patch("mcp_server_for_oscal.tools.get_schema.open_schema_file")
    def test_get_schema_file_not_found_xsd(self, mock_open_schema_file, mock_context):
        """An XSD open failure reports the file name to the client and re-raises."""
        mock_open_schema_file.side_effect = FileNotFoundError("File not found")

        with pytest.raises(FileNotFoundError):
            get_oscal_schema(mock_context, model_name="catalog", schema_type="xsd")

        mock_open_schema_file.assert_called_once_with("oscal_catalog_schema.xsd")
        mock_context.error.assert_called_once_with("failed to open schema oscal_catalog_schema.xsd")

    @patch("mcp_server_for_oscal.tools.get_schema.open_schema_file")
    def test_get_schema_json_parse_error(self, mock_open_schema_file, mock_context):
        """Test error handling when JSON parsing fails."""
        # mock_open returns a handle that supports the context-manager protocol.
        mock_open_schema_file.side_effect = mock_open(read_data="not json")

        with pytest.raises(json.JSONDecodeError):
            get_oscal_schema(mock_context, model_name="catalog", schema_type="json")

        mock_open_schema_file.assert_called_once_with("oscal_catalog_schema.json")
        mock_context.error.assert_called_once_with(
            "failed to open schema oscal_catalog_schema.json"
        )

    def test_get_schema_docstring_documents_both_formats(self):
        """The Returns section describes JSON and XSD output; the summary is unchanged.

        Uses ``inspect.getdoc``, the same access ``bin/build_mcpb.py`` uses for the
        MCPB manifest tool description.
        """
        doc = inspect.getdoc(get_oscal_schema) or ""
        assert doc.split("\n\n")[0].startswith(
            "A tool that returns the schema for specified OSCAL model."
        )
        returns = doc.split("Returns:", 1)[1]
        assert "JSON" in returns
        assert "XSD" in returns
        assert "XML" in returns

    @patch("mcp_server_for_oscal.tools.get_schema.logger")
    def test_get_schema_logging(self, mock_logger, mock_context):
        """Test that appropriate logging occurs."""
        with patch(
            "mcp_server_for_oscal.tools.get_schema.open_schema_file",
            side_effect=open_schema_file,
        ) as mock_open_schema_file:
            get_oscal_schema(mock_context, model_name="catalog", schema_type="json")

            mock_open_schema_file.assert_called_once_with("oscal_catalog_schema.json")

            # Verify logging
            mock_logger.debug.assert_called_once()
            debug_call_args = mock_logger.debug.call_args[0]
            assert "get_oscal_model_schema" in debug_call_args[0]
            assert "catalog" in debug_call_args[1]
            assert "json" in debug_call_args[2]

    def test_open_schema_file_success(self):
        """Test successful schema file opening."""
        with patch("builtins.open", mock_open(read_data='{"test": "data"}')) as mock_file:
            result = open_schema_file("schema.json")

            # Verify file was opened correctly (the path will be a PosixPath object)
            assert mock_file.called
            call_args = mock_file.call_args[0][0]
            assert Path(call_args).parts[-2:] == ("oscal_schemas", "schema.json")
            assert result is not None

    def test_open_schema_file_with_path_cleaning(self):
        """Test schema file opening with path cleaning."""
        with patch("builtins.open", mock_open(read_data='{"test": "data"}')) as mock_file:
            # Test with various path prefixes that should be stripped
            test_files = [
                "./schema.json",
                ".\\schema.json",
                "/schema.json",
                "\\schema.json",
            ]

            for test_file in test_files:
                mock_file.reset_mock()
                open_schema_file(test_file)

                # Verify the file path ends with the cleaned filename
                call_args = mock_file.call_args[0][0]
                assert Path(call_args).parts[-2:] == ("oscal_schemas", "schema.json")

    def test_open_schema_file_not_found(self):
        """Test error handling when schema file is not found."""
        with patch("builtins.open", side_effect=FileNotFoundError("File not found")):
            with pytest.raises(FileNotFoundError):
                open_schema_file("nonexistent.json")

    @patch("mcp_server_for_oscal.tools.get_schema.open_schema_file", side_effect=open_schema_file)
    def test_get_schema_all_valid_models(self, mock_open_schema_file, mock_context):
        """Test schema retrieval for all valid OSCAL model types."""
        for model in OSCALModelType:
            mock_open_schema_file.reset_mock()

            result = get_oscal_schema(mock_context, model_name=model, schema_type="json")

            assert result == expected_json_output(model.value)

            expected_filename = f"{schema_names.get(model)}.json"
            mock_open_schema_file.assert_called_once_with(expected_filename)
