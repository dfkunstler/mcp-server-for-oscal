"""
Tests for the validate_oscal_content tool.
"""

import json
import os
import tempfile
import uuid as uuid_mod
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
from hypothesis import given, settings
from hypothesis import strategies as st

import mcp_server_for_oscal.tools.validate_oscal_content as vmod
from mcp_server_for_oscal.tools.utils import MODEL_MAP, OSCALModelType
from mcp_server_for_oscal.tools.validate_oscal_content import (
    MAX_ERRORS_PER_LEVEL,
    OSCAL_CLI_BINARY_PLACEHOLDER,
    OSCAL_CLI_DIR_PLACEHOLDER,
    OSCAL_CLI_INPUT_PLACEHOLDER,
    _detect_model_type,
    _validate_json_schema,
    _validate_model,
    _validate_oscal_cli,
    _validate_well_formedness,
    scrub_cli_output,
    validate_oscal_content,
    validate_oscal_file,
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
        assert result["errors"] == ["Failed to load the bundled schema for catalog"]


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
        assert result["errors"] == ["Failed to load the bundled schema for catalog"]


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
    def test_failure_output_scrubs_paths(self, mock_run, mock_which):
        """The temp file and oscal-cli paths in oscal-cli output become placeholders."""
        cli = "/usr/local/bin/oscal-cli"
        mock_which.return_value = cli

        def run(argv, **_kwargs):
            tmp = argv[2]
            stderr = f"{cli}: [ERROR] {Path(tmp).as_uri()} [1:2] bad\nmissing field in {tmp}"
            return MagicMock(returncode=1, stdout="", stderr=stderr)

        mock_run.side_effect = run
        result = _validate_oscal_cli('{"catalog": {}}', OSCALModelType.CATALOG)
        assert result["errors"] == [
            f"oscal-cli: [ERROR] {OSCAL_CLI_INPUT_PLACEHOLDER} [1:2] bad",
            f"missing field in {OSCAL_CLI_INPUT_PLACEHOLDER}",
        ]

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


# --- Property 17: oscal-cli output scrubbing --------------------------------------------------

# Path segments: no separators, no leading/trailing space (oscal-cli output is stripped).
_cli_segments = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_ ", min_size=1, max_size=8)
_cli_segments = _cli_segments.filter(lambda s: s == s.strip())
_cli_names = st.sampled_from(["oscal-cli", "oscal-cli.bat", "oscal-cli.exe"])
_posix_cli_paths = st.builds(
    lambda segs, name: "/" + "/".join([*segs, name]),
    st.lists(_cli_segments, max_size=4),
    _cli_names,
)
_windows_cli_paths = st.builds(
    lambda drive, segs, name: f"{drive}:\\" + "\\".join([*segs, name]),
    st.sampled_from("CDZ"),
    st.lists(_cli_segments, max_size=4),
    _cli_names,
)
_cli_paths = _posix_cli_paths | _windows_cli_paths
# Filler text has no path separators, colons, or uppercase, so it never forms or extends a path
# and never collides with the per-line markers.
_filler = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 .,;-=()[]'\"", max_size=20)

_TMP_FORM_KEYS = ("tmp-native", "tmp-posix", "tmp-real", "tmp-uri", "tmp-real-uri", "tmp-java")
_CLI_FORM_KEYS = ("cli", "cli-posix", "cli-real")
_DIR_FORM_KEYS = ("cli-dir", "cli-dir-posix")
_FORM_KEYS = _TMP_FORM_KEYS + _CLI_FORM_KEYS + _DIR_FORM_KEYS


def _java_uri(path: str) -> str:
    """Return the ``file:/x`` form Java's ``File.toURI`` prints."""
    posix = Path(path).as_posix()
    return "file:" + (posix if posix.startswith("/") else f"/{posix}")


def _cli_output_forms(tmp: str, cli: str) -> dict[str, tuple[str, str]]:
    """Map each form key to (rendered path, expected placeholder) for ``tmp`` and ``cli``."""
    real_tmp = os.path.realpath(tmp)
    forms = {
        "tmp-native": tmp,
        "tmp-posix": Path(tmp).as_posix(),
        "tmp-real": real_tmp,
        "tmp-uri": Path(tmp).as_uri(),
        "tmp-real-uri": Path(real_tmp).as_uri(),
        "tmp-java": _java_uri(real_tmp),
    }
    out = {key: (value, OSCAL_CLI_INPUT_PLACEHOLDER) for key, value in forms.items()}
    for key, value in (
        ("cli", cli),
        ("cli-posix", Path(cli).as_posix()),
        ("cli-real", os.path.realpath(cli)),
    ):
        out[key] = (value, OSCAL_CLI_BINARY_PLACEHOLDER)
    directory = Path(cli).parent
    # A filesystem-root directory is deliberately not replaced, so it is never embedded.
    if directory != Path(directory.anchor):
        out["cli-dir"] = (str(directory), OSCAL_CLI_DIR_PLACEHOLDER)
        out["cli-dir-posix"] = (directory.as_posix(), OSCAL_CLI_DIR_PLACEHOLDER)
    return out


# One output line: parts that are either ("text", filler) or ("form", key).
_line_parts = st.lists(
    st.one_of(
        st.tuples(st.just("text"), _filler),
        st.tuples(st.just("form"), st.sampled_from(_FORM_KEYS)),
    ),
    max_size=5,
)


def _render_line(parts, marker: str, forms: dict[str, tuple[str, str]], *, scrubbed: bool) -> str:
    """Join ``parts`` with spaces and end with ``marker``; ``scrubbed`` renders placeholders."""
    rendered = []
    for kind, value in parts:
        if kind == "text":
            rendered.append(value)
        elif value in forms:  # dir forms are absent when the dir is a filesystem root
            rendered.append(forms[value][1 if scrubbed else 0])
    rendered.append(marker)
    return " ".join(rendered)


def _leaked_forms(line: str, forms: dict[str, tuple[str, str]]) -> list[str]:
    """Return the path forms still present in ``line``."""
    return [path for path, _ in forms.values() if path in line]


class TestPropertyOscalCliOutputScrubbed:
    """Property 17: oscal-cli output never shows the temporary file path.

    Feature: search-fetch-pairs, Property 17
    **Validates: Requirements 4.4**
    """

    @settings(max_examples=100, deadline=None)
    @given(
        cli=_cli_paths,
        tmp_name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=1),
        lines=st.lists(_line_parts, min_size=1, max_size=5),
    )
    def test_scrub_cli_output_replaces_every_form(self, cli, tmp_name, lines):
        """Every embedded path form becomes its placeholder and the other text is unchanged."""
        tmp = os.path.join(tempfile.gettempdir(), f"tmp{tmp_name}.json")
        forms = _cli_output_forms(tmp, cli)
        for i, parts in enumerate(lines):
            marker = f"MARK{i}X"
            raw = _render_line(parts, marker, forms, scrubbed=False)
            scrubbed = scrub_cli_output(raw, tmp, cli)
            assert scrubbed == _render_line(parts, marker, forms, scrubbed=True)
            assert _leaked_forms(scrubbed, forms) == []

    @settings(max_examples=50, deadline=None)
    @given(
        cli=_cli_paths,
        lines=st.lists(_line_parts, min_size=1, max_size=MAX_ERRORS_PER_LEVEL * 2),
        channel=st.sampled_from(["stdout", "stderr"]),
    )
    def test_validate_oscal_cli_lines_never_show_paths(self, cli, lines, channel):
        """No error or warning line names the temp file or oscal-cli; text and limits hold."""
        seen_tmp: list[str] = []

        def run(argv, **_kwargs):
            tmp = argv[2]
            seen_tmp.append(tmp)
            forms = _cli_output_forms(tmp, cli)
            output = "\n".join(
                _render_line(parts, f"MARK{i}X", forms, scrubbed=False)
                for i, parts in enumerate(lines)
            )
            streams = {"stdout": "", "stderr": ""}
            streams[channel] = output
            return MagicMock(returncode=1, **streams)

        with (
            patch(
                "mcp_server_for_oscal.tools.validate_oscal_content.shutil.which", return_value=cli
            ),
            patch(
                "mcp_server_for_oscal.tools.validate_oscal_content.subprocess.run",
                side_effect=run,
            ),
        ):
            result = _validate_oscal_cli('{"catalog": {}}', OSCALModelType.CATALOG)

        assert len(seen_tmp) == 1
        forms = _cli_output_forms(seen_tmp[0], cli)
        assert result["valid"] is False
        for line in result["errors"] + result["warnings"]:
            assert _leaked_forms(line, forms) == [], line

        shown = min(len(lines), MAX_ERRORS_PER_LEVEL)
        assert len(result["errors"]) == shown
        for i, error in enumerate(result["errors"]):
            assert f"MARK{i}X" in error
            expected = _render_line(lines[i], f"MARK{i}X", forms, scrubbed=True)
            # The whole output is stripped once, so only the first line loses leading space.
            assert error == (expected.lstrip() if i == 0 else expected)
        if len(lines) > MAX_ERRORS_PER_LEVEL:
            assert result["warnings"] == [
                f"Showing {MAX_ERRORS_PER_LEVEL} of {len(lines)} error lines"
            ]
        else:
            assert result["warnings"] == []


class TestValidateOscalFileMessages:
    """search-fetch-pairs Requirements 4.1, 4.6: ``validate_oscal_file`` failures name no
    path or URI in the raised message or the client notification. No network is used."""

    CANNOT_READ = "Cannot read the OSCAL file named by file_uri."
    URI = "https://internal.example/secret/bucket-name/ssp.json"

    @staticmethod
    def _sent(ctx) -> list[str]:
        return [c.args[0] for c in ctx.error.call_args_list]

    @pytest.mark.parametrize(
        "as_uri",
        [
            pytest.param(
                False,
                marks=pytest.mark.skipif(os.name == "nt", reason="drive letter parses as scheme"),
            ),
            True,
        ],
    )
    def test_missing_local_file(self, tmp_path, mock_context, as_uri):
        missing = tmp_path / "nope" / "missing.json"
        file_uri = missing.as_uri() if as_uri else str(missing)
        with pytest.raises(ValueError) as exc_info:
            validate_oscal_file(file_uri, ctx=mock_context)
        assert str(exc_info.value) == self.CANNOT_READ
        assert self._sent(mock_context) == [self.CANNOT_READ]
        for text in (str(exc_info.value), *self._sent(mock_context)):
            assert str(tmp_path) not in text
            assert tmp_path.as_posix() not in text
            assert "missing.json" not in text

    def test_directory_path(self, tmp_path, mock_context):
        with pytest.raises(ValueError) as exc_info:
            validate_oscal_file(tmp_path.as_uri(), ctx=mock_context)
        assert str(exc_info.value) == "URI must point to a file"
        assert tmp_path.as_posix() not in str(exc_info.value)
        assert all(tmp_path.as_posix() not in m for m in self._sent(mock_context))

    def test_remote_disabled(self, mock_context):
        with (
            patch.object(vmod.config, "allow_remote_uris", False),
            patch.object(vmod.requests, "get") as get,
            pytest.raises(ValueError) as exc_info,
        ):
            validate_oscal_file(self.URI, ctx=mock_context)
        get.assert_not_called()
        assert "not enabled" in str(exc_info.value)
        self._assert_no_uri(exc_info.value, mock_context)

    def test_timeout(self, mock_context):
        with (
            patch.object(vmod.config, "allow_remote_uris", True),
            patch.object(vmod.requests, "get", side_effect=requests.Timeout(self.URI)),
            pytest.raises(ValueError) as exc_info,
        ):
            validate_oscal_file(self.URI, ctx=mock_context)
        assert "timeout" in str(exc_info.value).lower()
        self._assert_no_uri(exc_info.value, mock_context)

    def test_http_error_includes_status(self, mock_context):
        response = MagicMock(status_code=403, url=self.URI)
        response.raise_for_status.side_effect = requests.HTTPError(
            f"403 Client Error: Forbidden for url: {self.URI}", response=response
        )
        with (
            patch.object(vmod.config, "allow_remote_uris", True),
            patch.object(vmod.requests, "get", return_value=response),
            pytest.raises(ValueError) as exc_info,
        ):
            validate_oscal_file(self.URI, ctx=mock_context)
        assert str(exc_info.value) == "Failed to fetch remote OSCAL file (HTTP status 403)"
        self._assert_no_uri(exc_info.value, mock_context)

    def _assert_no_uri(self, exc: Exception, ctx) -> None:
        sent = self._sent(ctx)
        assert sent == [str(exc)]
        for text in (str(exc), *sent):
            assert "internal.example" not in text
            assert "secret" not in text
            assert "https://" not in text
