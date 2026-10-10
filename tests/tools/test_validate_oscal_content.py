"""
Tests for the validate_oscal_content tool.
"""

import json
import uuid as uuid_mod
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools.utils import MODEL_MAP, OSCALModelType
from mcp_server_for_oscal.tools.validate_oscal_content import (
    MAX_ERRORS_PER_LEVEL,
    _detect_model_type,
    _validate_json_schema,
    _validate_model,
    _validate_oscal_cli,
    _validate_well_formedness,
    validate_oscal_content,
)

from .test_oscal_store import _VALID_DOC_BUILDERS


@pytest.fixture
def mock_context():
    """Create a mock MCP context for testing."""
    context = AsyncMock()
    context.error = AsyncMock()
    context.session = AsyncMock()
    context.session.client_params = {}
    return context


@pytest.fixture
def valid_catalog_json():
    """Minimal valid OSCAL catalog JSON string."""
    return json.dumps(
        {
            "catalog": {
                "uuid": "12345678-1234-4123-8123-123456789abc",
                "metadata": {
                    "title": "Test Catalog",
                    "last-modified": "2024-01-01T00:00:00Z",
                    "version": "1.0",
                    "oscal-version": "1.1.2",
                },
            }
        }
    )


class TestDetectModelType:
    """Test auto-detection of OSCAL model type from root keys."""

    def test_detect_catalog(self):
        assert _detect_model_type({"catalog": {}}) == OSCALModelType.CATALOG

    def test_detect_profile(self):
        assert _detect_model_type({"profile": {}}) == OSCALModelType.PROFILE

    def test_detect_component_definition(self):
        assert (
            _detect_model_type({"component-definition": {}}) == OSCALModelType.COMPONENT_DEFINITION
        )

    def test_detect_ssp(self):
        assert (
            _detect_model_type({"system-security-plan": {}}) == OSCALModelType.SYSTEM_SECURITY_PLAN
        )

    def test_detect_assessment_plan(self):
        assert _detect_model_type({"assessment-plan": {}}) == OSCALModelType.ASSESSMENT_PLAN

    def test_detect_assessment_results(self):
        assert _detect_model_type({"assessment-results": {}}) == OSCALModelType.ASSESSMENT_RESULTS

    def test_detect_poam(self):
        assert (
            _detect_model_type({"plan-of-action-and-milestones": {}})
            == OSCALModelType.PLAN_OF_ACTION_AND_MILESTONES
        )

    def test_detect_mapping(self):
        assert _detect_model_type({"mapping-collection": {}}) == OSCALModelType.MAPPING

    def test_schema_key_ignored(self):
        """$schema key should be skipped during detection."""
        data = {"$schema": "http://example.com/schema.json", "catalog": {}}
        assert _detect_model_type(data) == OSCALModelType.CATALOG

    def test_unknown_root_key(self):
        assert _detect_model_type({"unknown": {}}) is None

    def test_empty_dict(self):
        assert _detect_model_type({}) is None

    def test_only_schema_key(self):
        assert _detect_model_type({"$schema": "http://example.com"}) is None


class TestValidateWellFormedness:
    """Test Level 1: well-formedness checks."""

    def test_valid_json_object(self):
        result, data = _validate_well_formedness('{"catalog": {}}')
        assert result["valid"] is True
        assert result["level"] == "well_formedness"
        assert result["errors"] == []
        assert data == {"catalog": {}}

    def test_invalid_json(self):
        result, data = _validate_well_formedness("{not valid json}")
        assert result["valid"] is False
        assert len(result["errors"]) == 1
        assert data is None

    def test_json_array(self):
        result, data = _validate_well_formedness("[1, 2, 3]")
        assert result["valid"] is False
        assert "list" in result["errors"][0]
        assert data is None

    def test_json_string(self):
        result, data = _validate_well_formedness('"just a string"')
        assert result["valid"] is False
        assert data is None

    def test_empty_string(self):
        result, data = _validate_well_formedness("")
        assert result["valid"] is False
        assert data is None

    def test_json_null(self):
        result, data = _validate_well_formedness("null")
        assert result["valid"] is False
        assert data is None

    def test_nested_valid_object(self):
        result, data = _validate_well_formedness('{"a": {"b": [1, 2]}}')
        assert result["valid"] is True
        assert data == {"a": {"b": [1, 2]}}


class TestValidateJsonSchema:
    """Test Level 2: JSON Schema validation."""

    @patch("mcp_server_for_oscal.tools.validate_oscal_content.load_oscal_json_schema")
    def test_valid_document_passes(self, mock_load_schema):
        """A document conforming to the schema passes validation."""
        mock_load_schema.return_value = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"catalog": {"type": "object"}},
            "required": ["catalog"],
        }
        result = _validate_json_schema({"catalog": {}}, OSCALModelType.CATALOG)
        assert result["valid"] is True
        assert result["errors"] == []

    @patch("mcp_server_for_oscal.tools.validate_oscal_content.load_oscal_json_schema")
    def test_missing_required_field(self, mock_load_schema):
        """Missing required fields are caught."""
        mock_load_schema.return_value = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": ["catalog"],
        }
        result = _validate_json_schema({}, OSCALModelType.CATALOG)
        assert result["valid"] is False
        assert len(result["errors"]) >= 1

    @patch("mcp_server_for_oscal.tools.validate_oscal_content.load_oscal_json_schema")
    def test_error_cap(self, mock_load_schema):
        """Errors are capped at MAX_ERRORS_PER_LEVEL."""
        # Schema that requires many properties
        required = [f"field_{i}" for i in range(30)]
        mock_load_schema.return_value = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "required": required,
        }
        result = _validate_json_schema({}, OSCALModelType.CATALOG)
        assert result["valid"] is False
        assert len(result["errors"]) == MAX_ERRORS_PER_LEVEL
        assert len(result["warnings"]) >= 1
        assert "more may exist" in result["warnings"][0]

    @patch("mcp_server_for_oscal.tools.validate_oscal_content.load_oscal_json_schema")
    def test_schema_load_failure(self, mock_load_schema):
        """Graceful handling when schema cannot be loaded."""
        mock_load_schema.side_effect = FileNotFoundError("not found")
        result = _validate_json_schema({"catalog": {}}, OSCALModelType.CATALOG)
        assert result["valid"] is False
        assert "Failed to load schema" in result["errors"][0]


class TestValidateModel:
    """Test Level 3: OSCAL Pydantic model validation."""

    def test_valid_model_passes(self):
        """A successful model_validate call yields a valid 'model' level."""
        mock_cls = MagicMock()
        with patch.dict(MODEL_MAP, {OSCALModelType.CATALOG: mock_cls}):
            result = _validate_model({"catalog": {"uuid": "test"}}, OSCALModelType.CATALOG)
        assert result["valid"] is True
        assert result["level"] == "model"
        # The root key is unwrapped before validation
        mock_cls.model_validate.assert_called_once_with({"uuid": "test"})

    def test_mapping_collection_validated(self):
        """mapping-collection should be validated via the model class (not skipped)."""
        result = _validate_model({"mapping-collection": {}}, OSCALModelType.MAPPING)
        # Empty mapping-collection fails model validation (missing required fields)
        assert result["skipped"] is False
        assert result["valid"] is False

    def test_parse_error(self):
        """A model_validate exception is captured and split into error lines."""
        mock_cls = MagicMock()
        mock_cls.model_validate.side_effect = Exception("a\nb\nc")
        with patch.dict(MODEL_MAP, {OSCALModelType.CATALOG: mock_cls}):
            result = _validate_model({"catalog": {"bad": "data"}}, OSCALModelType.CATALOG)
        assert result["valid"] is False
        assert result["level"] == "model"
        assert result["errors"] == ["a", "b", "c"]

    def test_load_failure(self):
        """A model type missing from MODEL_MAP is reported as a load failure."""
        with patch.dict(MODEL_MAP, {}, clear=True):
            result = _validate_model({"catalog": {}}, OSCALModelType.CATALOG)
        assert result["valid"] is False
        assert result["level"] == "model"
        assert result["errors"][0].startswith("Failed to load OSCAL model class")


class TestValidateOscalCli:
    """Test Level 4: oscal-cli validation."""

    @patch("mcp_server_for_oscal.tools.validate_oscal_content.shutil.which")
    def test_not_installed_skipped(self, mock_which):
        """When oscal-cli is not found, Level 4 is skipped."""
        mock_which.return_value = None
        result = _validate_oscal_cli('{"catalog": {}}', OSCALModelType.CATALOG)
        assert result["skipped"] is True
        assert "not found" in result["skip_reason"]

    @patch("mcp_server_for_oscal.tools.validate_oscal_content.shutil.which")
    @patch("mcp_server_for_oscal.tools.validate_oscal_content.subprocess.run")
    def test_success(self, mock_run, mock_which):
        """Successful oscal-cli validation."""
        mock_which.return_value = "/usr/local/bin/oscal-cli"
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = _validate_oscal_cli('{"catalog": {}}', OSCALModelType.CATALOG)
        assert result["valid"] is True

    @patch("mcp_server_for_oscal.tools.validate_oscal_content.shutil.which")
    @patch("mcp_server_for_oscal.tools.validate_oscal_content.subprocess.run")
    def test_failure(self, mock_run, mock_which):
        """Failed oscal-cli validation returns errors."""
        mock_which.return_value = "/usr/local/bin/oscal-cli"
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="Error: missing required field"
        )
        result = _validate_oscal_cli('{"catalog": {}}', OSCALModelType.CATALOG)
        assert result["valid"] is False
        assert "missing required field" in result["errors"][0]

    @patch("mcp_server_for_oscal.tools.validate_oscal_content.shutil.which")
    @patch("mcp_server_for_oscal.tools.validate_oscal_content.subprocess.run")
    def test_timeout(self, mock_run, mock_which):
        """Timeout is handled gracefully."""
        mock_which.return_value = "/usr/local/bin/oscal-cli"
        mock_run.side_effect = __import__("subprocess").TimeoutExpired(  # nosec B404 # mock only
            cmd="oscal-cli", timeout=60
        )
        result = _validate_oscal_cli('{"catalog": {}}', OSCALModelType.CATALOG)
        assert result["valid"] is False
        assert "timed out" in result["errors"][0]


class TestValidateOscalContentEndToEnd:
    """End-to-end tests for the full validation pipeline."""

    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_oscal_cli")
    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_model")
    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_json_schema")
    def test_valid_catalog_auto_detect(
        self, mock_schema, mock_model, mock_cli, mock_context, valid_catalog_json
    ):
        """Full pipeline with auto-detected model type."""
        mock_schema.return_value = {
            "level": "json_schema",
            "valid": True,
            "errors": [],
            "warnings": [],
            "skipped": False,
            "skip_reason": None,
        }
        mock_model.return_value = {
            "level": "model",
            "valid": True,
            "errors": [],
            "warnings": [],
            "skipped": False,
            "skip_reason": None,
        }
        mock_cli.return_value = {
            "level": "oscal_cli",
            "valid": True,
            "errors": [],
            "warnings": [],
            "skipped": True,
            "skip_reason": "oscal-cli not found in PATH",
        }

        result = validate_oscal_content(valid_catalog_json, ctx=mock_context)

        assert result["valid"] is True
        assert result["model_type"] == "catalog"
        assert len(result["levels"]) == 4

    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_oscal_cli")
    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_model")
    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_json_schema")
    def test_model_type_override(
        self, mock_schema, mock_model, mock_cli, mock_context, valid_catalog_json
    ):
        """Explicit model_type overrides auto-detection."""
        mock_schema.return_value = {
            "level": "json_schema",
            "valid": True,
            "errors": [],
            "warnings": [],
            "skipped": False,
            "skip_reason": None,
        }
        mock_model.return_value = {
            "level": "model",
            "valid": True,
            "errors": [],
            "warnings": [],
            "skipped": False,
            "skip_reason": None,
        }
        mock_cli.return_value = {
            "level": "oscal_cli",
            "valid": True,
            "errors": [],
            "warnings": [],
            "skipped": True,
            "skip_reason": "oscal-cli not found in PATH",
        }

        result = validate_oscal_content(valid_catalog_json, model_type="profile", ctx=mock_context)

        assert result["model_type"] == "profile"
        # Schema validation called with profile type, not catalog
        mock_schema.assert_called_once()
        call_args = mock_schema.call_args[0]
        assert call_args[1] == OSCALModelType.PROFILE

    def test_invalid_json_skips_levels_2_3_4(self, mock_context):
        """If JSON is invalid, levels 2-4 are skipped."""
        result = validate_oscal_content("{invalid json}", ctx=mock_context)

        assert result["valid"] is False
        assert result["levels"][0]["level"] == "well_formedness"
        assert result["levels"][0]["valid"] is False

        for lvl in result["levels"][1:]:
            assert lvl["skipped"] is True
            assert "well-formedness" in lvl["skip_reason"]

    def test_invalid_model_type(self, mock_context):
        """Invalid model_type parameter returns error with 4 levels."""
        result = validate_oscal_content(
            '{"catalog": {}}', model_type="not-a-model", ctx=mock_context
        )
        assert result["valid"] is False
        assert "error" in result
        assert len(result["levels"]) == 4
        for lvl in result["levels"][1:]:
            assert lvl["skipped"] is True
            assert "invalid model_type" in lvl["skip_reason"]

    def test_undetectable_model_type(self, mock_context):
        """Unknown root key fails model type detection with 4 levels."""
        result = validate_oscal_content('{"unknown": {}}', ctx=mock_context)
        assert result["valid"] is False
        assert "error" in result
        assert len(result["levels"]) == 4
        for lvl in result["levels"][1:]:
            assert lvl["skipped"] is True
            assert "undetectable model type" in lvl["skip_reason"]

    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_oscal_cli")
    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_model")
    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_json_schema")
    def test_overall_validity_false_when_any_level_fails(
        self, mock_schema, mock_model, mock_cli, mock_context, valid_catalog_json
    ):
        """Overall valid is False if any non-skipped level fails."""
        mock_schema.return_value = {
            "level": "json_schema",
            "valid": False,
            "errors": ["bad"],
            "warnings": [],
            "skipped": False,
            "skip_reason": None,
        }
        mock_model.return_value = {
            "level": "model",
            "valid": True,
            "errors": [],
            "warnings": [],
            "skipped": False,
            "skip_reason": None,
        }
        mock_cli.return_value = {
            "level": "oscal_cli",
            "valid": True,
            "errors": [],
            "warnings": [],
            "skipped": True,
            "skip_reason": "not found",
        }

        result = validate_oscal_content(valid_catalog_json, ctx=mock_context)
        assert result["valid"] is False

    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_oscal_cli")
    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_model")
    @patch("mcp_server_for_oscal.tools.validate_oscal_content._validate_json_schema")
    def test_skipped_levels_dont_affect_validity(
        self, mock_schema, mock_model, mock_cli, mock_context, valid_catalog_json
    ):
        """Skipped levels don't make the overall result invalid."""
        mock_schema.return_value = {
            "level": "json_schema",
            "valid": True,
            "errors": [],
            "warnings": [],
            "skipped": False,
            "skip_reason": None,
        }
        mock_model.return_value = {
            "level": "model",
            "valid": True,
            "errors": [],
            "warnings": [],
            "skipped": True,
            "skip_reason": "no model",
        }
        mock_cli.return_value = {
            "level": "oscal_cli",
            "valid": True,
            "errors": [],
            "warnings": [],
            "skipped": True,
            "skip_reason": "not found",
        }

        result = validate_oscal_content(valid_catalog_json, ctx=mock_context)
        assert result["valid"] is True

    def test_empty_content(self, mock_context):
        """Empty string fails well-formedness."""
        result = validate_oscal_content("", ctx=mock_context)
        assert result["valid"] is False
        assert result["levels"][0]["valid"] is False

    def test_none_content(self, mock_context):
        """None content produces well-formedness failure, not a crash."""
        result = validate_oscal_content(None, ctx=mock_context)
        assert result["valid"] is False
        assert result["levels"][0]["level"] == "well_formedness"
        assert result["levels"][0]["valid"] is False
        assert len(result["levels"]) == 4


# Feature: oscal-bindings migration (#26), Property 4: Level names are stable
_EXPECTED_LEVEL_NAMES = ["well_formedness", "json_schema", "model", "oscal_cli"]

_titles = st.text(
    alphabet=st.characters(categories=("L", "N", "Zs")), min_size=1, max_size=40
).filter(lambda s: s.strip())


def _apply_mutation(doc: dict, mutation: str) -> dict:
    """Apply one structural mutation to a valid OSCAL document."""
    root_key = next(iter(doc))
    body = doc[root_key]
    if mutation == "unknown-root-key":
        body["unexpected-field"] = "x"
    elif mutation == "unknown-metadata-key":
        body["metadata"]["unexpected-field"] = "x"
    elif mutation == "drop-uuid":
        body.pop("uuid", None)
    elif mutation == "drop-metadata":
        body.pop("metadata", None)
    return doc


@st.composite
def _oscal_json_docs(draw: st.DrawFn) -> str:
    """Valid OSCAL JSON, optionally with one mutation applied."""
    root_key = draw(st.sampled_from(sorted(_VALID_DOC_BUILDERS)))
    doc = _VALID_DOC_BUILDERS[root_key](str(uuid_mod.uuid4()), draw(_titles))
    mutation = draw(
        st.sampled_from(
            ["none", "unknown-root-key", "unknown-metadata-key", "drop-uuid", "drop-metadata"]
        )
    )
    return json.dumps(_apply_mutation(doc, mutation))


_json_scalars = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text()

_validation_inputs = st.one_of(
    _oscal_json_docs(),
    # JSON objects with arbitrary root keys (mostly undetectable model types)
    st.dictionaries(st.text(max_size=20), _json_scalars, max_size=4).map(json.dumps),
    # Non-object JSON
    st.one_of(_json_scalars, st.lists(_json_scalars, max_size=4)).map(json.dumps),
    # Arbitrary text, usually not JSON
    st.text(max_size=200),
)


class TestPropertyLevelNamesStable:
    """Property 4: Level names are stable.

    **Validates: Requirements 4.1, 4.2, 10.2**
    """

    @settings(max_examples=100, deadline=None)
    @given(content=_validation_inputs)
    def test_level_names_are_stable(self, content):
        """Every input yields the four levels, in order, whether or not later levels run."""
        # Hide oscal-cli so the real level-4 function runs and skips instead of shelling out.
        with patch(
            "mcp_server_for_oscal.tools.validate_oscal_content.shutil.which", return_value=None
        ):
            result = validate_oscal_content(content, ctx=None)

        assert [lvl["level"] for lvl in result["levels"]] == _EXPECTED_LEVEL_NAMES
