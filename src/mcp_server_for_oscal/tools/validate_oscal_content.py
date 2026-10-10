"""
Tool for validating OSCAL content through a multi-level validation pipeline.
"""

import itertools
import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import regex
import requests
from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.config import config
from mcp_server_for_oscal.tools.utils import (
    MODEL_MAP,
    ROOT_KEY_TO_MODEL_TYPE,
    OSCALModelType,
    load_oscal_json_schema,
    try_notify_client_error,
)

logger = logging.getLogger(__name__)

MAX_ERRORS_PER_LEVEL = 20

# Levels that run after Level 1 (well-formedness); skipped together on early exit.
_POST_PARSE_LEVELS = ("json_schema", "model", "oscal_cli")


def _pattern_safe(validator: Any, patrn: str, instance: Any, schema: Any) -> Any:  # noqa: ARG001 - jsonschema keyword signature
    """Pattern keyword handler that uses the ``regex`` module for ECMA-262 compatibility.

    OSCAL JSON schemas use Unicode property escapes (e.g. ``\\p{L}``) which are
    valid in ECMA-262 / JSON Schema but unsupported by Python's stdlib ``re``.
    The third-party ``regex`` module handles these natively.
    """
    if not validator.is_type(instance, "string"):
        return
    try:
        if not regex.search(patrn, instance):
            from jsonschema import ValidationError

            yield ValidationError(f"{instance!r} does not match {patrn!r}")
    except regex.error:
        logger.debug("Skipping invalid JSON Schema pattern: %s", patrn)


def _build_safe_validator() -> Any:
    """Return a Draft7Validator subclass with a Python-safe ``pattern`` handler."""
    import jsonschema

    return jsonschema.validators.extend(
        jsonschema.Draft7Validator,
        {"pattern": _pattern_safe},
    )


def _make_level(
    level: str,
    *,
    valid: bool = True,
    errors: list[str] | None = None,
    warnings: list[str] | None = None,
    skipped: bool = False,
    skip_reason: str | None = None,
) -> dict[str, Any]:
    """Create a validation level result dict."""
    return {
        "level": level,
        "valid": valid,
        "errors": errors or [],
        "warnings": warnings or [],
        "skipped": skipped,
        "skip_reason": skip_reason,
    }


def _detect_model_type(data: dict) -> OSCALModelType | None:
    """Detect the OSCAL model type from a parsed JSON document's root keys."""
    for key in data:
        if key == "$schema":
            continue
        if key in ROOT_KEY_TO_MODEL_TYPE:
            return ROOT_KEY_TO_MODEL_TYPE[key]
    return None


def _validate_well_formedness(content: str) -> tuple[dict, dict | None]:
    """Level 1: Check that content is valid JSON and a dict.

    Returns:
        (level_result, parsed_data_or_None)
    """
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        return _make_level("well_formedness", valid=False, errors=[str(exc)]), None

    if not isinstance(data, dict):
        return _make_level(
            "well_formedness",
            valid=False,
            errors=[f"Expected a JSON object at root, got {type(data).__name__}"],
        ), None

    return _make_level("well_formedness"), data


def _validate_json_schema(data: dict, model_type: OSCALModelType) -> dict:
    """Level 2: Validate against the bundled NIST OSCAL JSON schema."""

    try:
        schema = load_oscal_json_schema(model_type)
    except Exception as exc:
        return _make_level(
            "json_schema",
            valid=False,
            errors=[f"Failed to load schema for {model_type}: {exc}"],
        )

    validator = _build_safe_validator()(schema)
    raw_errors = list(itertools.islice(validator.iter_errors(data), MAX_ERRORS_PER_LEVEL + 1))

    if not raw_errors:
        return _make_level("json_schema")

    errors = [e.message for e in raw_errors[:MAX_ERRORS_PER_LEVEL]]
    warnings = []
    if len(raw_errors) > MAX_ERRORS_PER_LEVEL:
        warnings.append(f"Showing first {MAX_ERRORS_PER_LEVEL} errors; more may exist")

    return _make_level("json_schema", valid=False, errors=errors, warnings=warnings)


def _validate_model(data: dict, model_type: OSCALModelType) -> dict:
    """Level 3: Semantic validation using OSCAL Pydantic models."""
    try:
        model_cls = MODEL_MAP[model_type]
    except KeyError as exc:
        return _make_level(
            "model",
            valid=False,
            errors=[f"Failed to load OSCAL model class for '{model_type}': {exc}"],
        )

    # The OSCAL doc has a root key wrapping the actual model data
    inner = data.get(model_type.value, data)

    try:
        model_cls.model_validate(inner)
    except Exception as exc:
        error_str = str(exc)
        # Pydantic ValidationError can be very long; truncate per-error lines
        error_lines = error_str.split("\n")
        errors = error_lines[:MAX_ERRORS_PER_LEVEL]
        warnings = []
        if len(error_lines) > MAX_ERRORS_PER_LEVEL:
            warnings.append(f"Showing {MAX_ERRORS_PER_LEVEL} of {len(error_lines)} error lines")
        return _make_level("model", valid=False, errors=errors, warnings=warnings)

    return _make_level("model")


def _validate_oscal_cli(content: str, model_type: OSCALModelType) -> dict:  # noqa: ARG001 - same signature as other levels
    """Level 4: Full NIST validation via oscal-cli if available."""
    oscal_cli = shutil.which("oscal-cli")
    if oscal_cli is None:
        return _make_level(
            "oscal_cli",
            skipped=True,
            skip_reason="oscal-cli not found in PATH",
        )

    tmp_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".json", delete=False
        ) as tmp_file:
            tmp_file.write(content)

        result = subprocess.run(
            [oscal_cli, "validate", tmp_file.name],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0:
            return _make_level("oscal_cli")

        stderr = result.stderr.strip()
        stdout = result.stdout.strip()
        output = stderr or stdout or "Validation failed with no output"
        error_lines = output.split("\n")
        errors = error_lines[:MAX_ERRORS_PER_LEVEL]
        warnings = []
        if len(error_lines) > MAX_ERRORS_PER_LEVEL:
            warnings.append(f"Showing {MAX_ERRORS_PER_LEVEL} of {len(error_lines)} error lines")
        return _make_level("oscal_cli", valid=False, errors=errors, warnings=warnings)

    except subprocess.TimeoutExpired:
        return _make_level(
            "oscal_cli",
            valid=False,
            errors=["oscal-cli validation timed out after 60 seconds"],
        )
    except Exception as exc:
        return _make_level(
            "oscal_cli",
            valid=False,
            errors=[f"oscal-cli execution error: {exc}"],
        )
    finally:
        if tmp_file is not None:
            Path(tmp_file.name).unlink(missing_ok=True)


@tool
def validate_oscal_file(
    file_uri: str,
    model_type: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Validate OSCAL JSON file through a multi-level validation pipeline.

    Runs up to four validation levels in sequence:
      1. Well-formedness - Is it valid JSON and a JSON object?
      2. JSON Schema - Does it conform to the NIST OSCAL JSON schema?
      3. Model - Semantic checks via OSCAL Pydantic models
      4. oscal-cli - Full NIST validation if oscal-cli is installed

    If Level 1 fails, Levels 2-4 are skipped. If oscal-cli is not installed,
    Level 4 is gracefully skipped. The overall result is valid only when all
    non-skipped levels pass.

    Args:
        ctx: MCP server context (injected automatically by MCP server)
        file_uri: URI of the file to be validated. This can be local or remote but remote URI will fail unless `config.allow_remote_uris == True`.
        model_type: Optional OSCAL model type (e.g. "catalog", "profile").
            If omitted, the model type is auto-detected from the root key.

    Returns:
        dict: Structured validation results with per-level detail
    """
    uri = urlparse(file_uri)

    if uri.scheme in ("", "file"):
        lf = Path(uri.path)
        if lf.is_dir():
            raise ValueError("URI must point to a file")

        try:
            with open(lf, encoding="utf-8") as oftv:
                return validate_oscal_content(oftv.read(), model_type, ctx=ctx)
        except Exception:
            logger.exception("OSCAL validation error.")
            raise

    # Remote URI
    if not config.allow_remote_uris:
        msg = (
            f"Remote URI loading is not enabled. "
            f"Set OSCAL_ALLOW_REMOTE_URIS=true to enable. Source: {file_uri}"
        )
        logger.error(msg)
        try_notify_client_error(msg, ctx)
        raise ValueError(msg)

    try:
        response = requests.get(file_uri, timeout=config.request_timeout)
        response.raise_for_status()
        return validate_oscal_content(response.text, model_type, ctx=ctx)
    except requests.Timeout as e:
        msg = f"Request timeout while fetching remote URI (timeout={config.request_timeout}s): {file_uri}"
        logger.exception(msg)
        try_notify_client_error(msg, ctx)
        raise ValueError(msg) from e
    except requests.RequestException as e:
        msg = f"Failed to fetch remote OSCAL file: {e}"
        logger.exception(msg)
        try_notify_client_error(msg, ctx)
        raise ValueError(msg) from e


@tool
def validate_oscal_content(
    content: str,
    model_type: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """
    Validate OSCAL JSON content through a multi-level validation pipeline.

    Runs up to four validation levels in sequence:
      1. Well-formedness - Is it valid JSON and a JSON object?
      2. JSON Schema - Does it conform to the NIST OSCAL JSON schema?
      3. Model - Semantic checks via OSCAL Pydantic models
      4. oscal-cli - Full NIST validation if oscal-cli is installed

    If Level 1 fails, Levels 2-4 are skipped. If oscal-cli is not installed,
    Level 4 is gracefully skipped. The overall result is valid only when all
    non-skipped levels pass.

    Args:
        ctx: MCP server context (injected automatically by MCP server)
        content: OSCAL JSON content as a string
        model_type: Optional OSCAL model type (e.g. "catalog", "profile").
            If omitted, the model type is auto-detected from the root key.

    Returns:
        dict: Structured validation results with per-level detail
    """
    content_length = len(content) if isinstance(content, str) else None
    logger.debug(
        "validate_oscal_content(model_type=%s, content_length=%s)", model_type, content_length
    )

    levels: list[dict] = []

    # -- Level 1: Well-formedness --
    wf_result, parsed = _validate_well_formedness(content)
    levels.append(wf_result)

    if parsed is None:
        # JSON is not parseable; skip remaining levels
        for lvl in _POST_PARSE_LEVELS:
            levels.append(
                _make_level(lvl, skipped=True, skip_reason="Skipped due to well-formedness failure")
            )
        return {"valid": False, "model_type": model_type, "levels": levels}

    # -- Detect or validate model type --
    detected = _detect_model_type(parsed)

    if model_type is not None:
        # Validate provided model_type
        try:
            resolved_type = OSCALModelType(model_type)
        except ValueError:
            msg = f"Invalid model_type: '{model_type}'. Use list_oscal_models to see valid types."
            try_notify_client_error(msg, ctx)
            for lvl in _POST_PARSE_LEVELS:
                levels.append(
                    _make_level(
                        lvl,
                        skipped=True,
                        skip_reason=f"Skipped due to invalid model_type: '{model_type}'",
                    )
                )
            return {"valid": False, "model_type": model_type, "levels": levels, "error": msg}
    elif detected is not None:
        resolved_type = detected
    else:
        root_keys = [k for k in parsed if k != "$schema"]
        msg = f"Cannot detect OSCAL model type from root keys: {root_keys}"
        try_notify_client_error(msg, ctx)
        for lvl in _POST_PARSE_LEVELS:
            levels.append(
                _make_level(lvl, skipped=True, skip_reason="Skipped due to undetectable model type")
            )
        return {"valid": False, "model_type": None, "levels": levels, "error": msg}

    model_type_str = resolved_type.value

    # -- Level 2: JSON Schema --
    levels.append(_validate_json_schema(parsed, resolved_type))

    # -- Level 3: Model --
    levels.append(_validate_model(parsed, resolved_type))

    # -- Level 4: oscal-cli --
    levels.append(_validate_oscal_cli(content, resolved_type))

    # -- Overall validity --
    overall_valid = all(lvl["valid"] for lvl in levels if not lvl["skipped"])

    return {"valid": overall_valid, "model_type": model_type_str, "levels": levels}
