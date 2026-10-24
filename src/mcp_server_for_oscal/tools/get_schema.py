"""
Tool for retrieving OSCAL schemas.
"""

import json
import logging
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from strands import tool

from mcp_server_for_oscal.tools.descriptions import ParamDoc
from mcp_server_for_oscal.tools.utils import (
    OSCALModelType,
    schema_names,
    try_notify_client_error,
)

logger = logging.getLogger(__name__)


@tool
def get_oscal_schema(
    model_name: Annotated[
        str,
        ParamDoc(
            'OSCAL model name from list_oscal_models, or "complete" for all models (very large).'
        ),
    ],
    schema_type: Annotated[
        Literal["json", "xsd"],
        ParamDoc('"json" returns the JSON schema as JSON text; "xsd" returns the XML Schema.'),
    ] = "json",
    ctx: Context | None = None,
) -> str:
    """Return the JSON schema or XML Schema (XSD) of one OSCAL model as text.

    Use when you need the structure of an OSCAL model: its properties, types, and required
    fields.
    """
    logger.debug(
        "get_oscal_model_schema(model_name: %s, syntax: %s, session client params: %s)",
        model_name,
        schema_type,
        ctx.session.client_params if ctx else None,
    )

    if schema_type not in ["json", "xsd"]:
        msg = f"Invalid schema type: {schema_type}."
        try_notify_client_error(msg, ctx)
        raise ValueError(msg)

    if model_name not in OSCALModelType.__members__.values() and model_name != "complete":
        msg = (
            f"Invalid model: {model_name}. Use the tool list_oscal_models to get valid model names."
        )
        try_notify_client_error(msg, ctx)
        raise ValueError(msg)

    schema_file_name = f"{schema_names.get(model_name)}.{schema_type}"

    try:
        # Context manager guarantees the handle closes on success and on error.
        with open_schema_file(schema_file_name) as schema_file:
            if schema_type == "xsd":
                # XSD is XML: return the bundled text as-is (never JSON-parse it).
                schema_text = schema_file.read()
            else:
                schema_text = json.dumps(json.load(schema_file))
    except Exception:
        msg = f"failed to open schema {schema_file_name}"
        logger.exception(msg)
        try_notify_client_error(msg, ctx)
        raise

    return schema_text


def open_schema_file(file_name: str) -> Any:
    """Open a schema file from the OSCAL schemas directory."""
    # Get the directory of this file and navigate to oscal_schemas relative to it
    current_file_dir = Path(__file__).parent
    schema_path = current_file_dir.parent.joinpath("oscal_schemas")

    try:
        return open(schema_path.joinpath(file_name.lstrip("./\\")), encoding="utf-8")
    except Exception:
        msg = f"failed to open file {file_name}"
        logger.exception(msg)
        raise
