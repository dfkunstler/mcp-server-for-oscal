"""
OSCAL MCP Server Tools

This package contains all the tool functions for the OSCAL MCP server.
"""

from collections.abc import Callable


def get_tool_list() -> list[Callable]:
    """Return the canonical list of OSCAL tool functions.

    Excludes the `about` tool (MCP-server-only).
    """
    from mcp_server_for_oscal.tools.find_references_to import find_references_to
    from mcp_server_for_oscal.tools.get_allowed_values import get_allowed_values
    from mcp_server_for_oscal.tools.get_constraints import get_constraints
    from mcp_server_for_oscal.tools.get_definition import get_definition
    from mcp_server_for_oscal.tools.get_metaschema_source import get_metaschema_source
    from mcp_server_for_oscal.tools.get_schema import get_oscal_schema
    from mcp_server_for_oscal.tools.list_models import list_oscal_models
    from mcp_server_for_oscal.tools.list_oscal_resources import list_oscal_resources
    from mcp_server_for_oscal.tools.query_component_definition import (
        get_capability,
        query_component_definition,
    )
    from mcp_server_for_oscal.tools.query_oscal_models import (
        get_child_element,
        list_child_elements,
        list_documents,
        query_documents,
        text_search_oscal,
    )
    from mcp_server_for_oscal.tools.validate_oscal_content import (
        validate_oscal_content,
        validate_oscal_file,
    )

    tools: list[Callable] = [
        list_oscal_models,
        get_oscal_schema,
        # Metaschema constraint tools (bundled data only; no AWS dependency)
        get_allowed_values,
        get_constraints,
        find_references_to,
        get_definition,
        get_metaschema_source,
        list_oscal_resources,
        query_component_definition,
        get_capability,
        validate_oscal_content,
        validate_oscal_file,
        # Generic document and child element tools (consolidate-tools, #40)
        list_documents,
        query_documents,
        list_child_elements,
        text_search_oscal,
        get_child_element,
    ]

    from mcp_server_for_oscal.tools.query_documentation import (
        get_oscal_documentation,
        query_oscal_documentation,
    )

    tools.extend([query_oscal_documentation, get_oscal_documentation])

    return tools
