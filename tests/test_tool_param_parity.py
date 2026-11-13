"""Server/Agent parameter parity for every tool in get_tool_list().

Feature: tool-context-optimization (Req 3.4, 3.5, 3.6).

The MCP server builds its inputSchema from the function signature (pydantic, `ctx` excluded by
its `Context` type). The Agent uses the Strands tool spec, with `ctx` stripped by
`oscal_agent.agent_tool`. Both must expose the same property names and descriptions.
"""

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from mcp_server_for_oscal import main, oscal_agent
from mcp_server_for_oscal.tools import get_tool_list
from mcp_server_for_oscal.tools.oscal_store import CHILD_ELEMENT_TYPES
from mcp_server_for_oscal.tools.utils import OSCALModelType

TOOLS = get_tool_list()


@pytest.fixture(scope="module")
def server_tools() -> dict[str, dict[str, Any]]:
    """Live tools/list entries from a fresh server built by the production path, by name."""
    server = main.build_server()
    main._setup_tools(server)
    tools = asyncio.run(server.list_tools())
    dumped = [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in tools]
    return {t["name"]: t for t in dumped}


def _agent_properties(fn: Any) -> dict[str, Any]:
    return oscal_agent.agent_tool(fn).tool_spec["inputSchema"]["json"].get("properties", {})


def _server_properties(server_tools: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    assert name in server_tools, f"{name} missing from server tools/list"
    return server_tools[name]["inputSchema"].get("properties", {})


@pytest.mark.parametrize("fn", TOOLS, ids=[fn.__name__ for fn in TOOLS])
class TestServerAgentParity:
    def test_property_names_match(self, fn, server_tools):
        """Req 3.4, 3.6: same property set on both sides, no exception for ctx."""
        agent_names = set(_agent_properties(fn))
        server_names = set(_server_properties(server_tools, fn.__name__))
        assert agent_names == server_names, (
            f"{fn.__name__}: agent-only={sorted(agent_names - server_names)} "
            f"server-only={sorted(server_names - agent_names)}"
        )

    def test_property_descriptions_match(self, fn, server_tools):
        """Req 3.5: each property has the same top-level description on both sides."""
        agent_props = _agent_properties(fn)
        server_props = _server_properties(server_tools, fn.__name__)
        for name, server_prop in server_props.items():
            assert "description" in server_prop, f"{fn.__name__}.{name}: no server description"
            assert "description" in agent_props[name], f"{fn.__name__}.{name}: no agent description"
            assert agent_props[name]["description"] == server_prop["description"], (
                f"{fn.__name__}.{name}: agent={agent_props[name]['description']!r} "
                f"server={server_prop['description']!r}"
            )


class TestCreateAgentUsesAgentTools:
    @patch("mcp_server_for_oscal.oscal_agent.Agent")
    @patch("mcp_server_for_oscal.oscal_agent.BedrockModel")
    @patch("mcp_server_for_oscal.oscal_agent.boto3.Session")
    def test_agent_receives_stripped_copies(
        self, mock_session_cls, mock_bedrock_cls, mock_agent_cls
    ):
        """create_oscal_agent() passes Agent the agent_tool copies, so parity covers them."""
        oscal_agent.create_oscal_agent()

        given = mock_agent_cls.call_args.kwargs["tools"]
        assert [t.tool_name for t in given] == [fn.__name__ for fn in TOOLS]
        for passed, original in zip(given, TOOLS, strict=True):
            assert passed is not original
            assert passed.tool_spec == oscal_agent.agent_tool(original).tool_spec
            schema = passed.tool_spec["inputSchema"]["json"]
            assert "ctx" not in schema.get("properties", {})
            assert "ctx" not in schema.get("required", [])
            assert "Context" not in schema.get("$defs", {})


# ---------------------------------------------------------------------------
# Generic_Tools (consolidate-tools, Property 13, Req 13.1, 13.2, 13.3)
# ---------------------------------------------------------------------------

GENERIC_TOOL_NAMES = ("list_documents", "query_documents", "list_child_elements")

# Parameter that identifies the target of each Generic_Tool (required, no default).
GENERIC_REQUIRED: dict[str, list[str]] = {
    "list_documents": ["model_type"],
    "query_documents": ["model_type"],
    "list_child_elements": ["element_type"],
}

_MODEL_ENUM = [m.value for m in OSCALModelType]
_ELEMENT_ENUM = [t for types in CHILD_ELEMENT_TYPES.values() for t in types]
_QUERY_TYPE_ENUM = ["all", "by_uuid", "by_title"]

# Expected enum-valued parameters per Generic_Tool.
GENERIC_ENUMS: dict[str, dict[str, list[str]]] = {
    "list_documents": {"model_type": _MODEL_ENUM},
    "query_documents": {"model_type": _MODEL_ENUM, "query_type": _QUERY_TYPE_ENUM},
    "list_child_elements": {"element_type": _ELEMENT_ENUM},
}


def _agent_schemas() -> dict[str, dict[str, Any]]:
    """Agent inputSchema JSON by tool name, built through `oscal_agent.agent_tools()`."""
    return {t.tool_name: t.tool_spec["inputSchema"]["json"] for t in oscal_agent.agent_tools(TOOLS)}


def _enum_values(schema: dict[str, Any], prop: dict[str, Any]) -> list[str] | None:
    """Return the `enum` values of a property, following `$ref` and `anyOf`, or None."""
    if "enum" in prop:
        return list(prop["enum"])
    ref = prop.get("$ref")
    if isinstance(ref, str):
        return _enum_values(schema, schema.get("$defs", {}).get(ref.rsplit("/", 1)[-1], {}))
    for branch in prop.get("anyOf", []):
        found = _enum_values(schema, branch)
        if found is not None:
            return found
    return None


def _required(schema: dict[str, Any]) -> list[str]:
    return sorted(schema.get("required", []))


@pytest.mark.parametrize("name", GENERIC_TOOL_NAMES)
class TestGenericToolParity:
    """Property 13: the Server and the Agent expose the same contract for each Generic_Tool."""

    def test_tool_name_on_both_sides(self, name, server_tools):
        """Req 13.1: the same tool name is on the server and on the agent."""
        assert name in server_tools
        assert name in _agent_schemas()

    def test_property_names_match(self, name, server_tools):
        """Req 13.2: same parameter names, and the agent has no `ctx`."""
        server_schema = server_tools[name]["inputSchema"]
        agent_schema = _agent_schemas()[name]
        assert set(agent_schema.get("properties", {})) == set(server_schema.get("properties", {}))
        assert "ctx" not in server_schema.get("properties", {})

    def test_required_parameters_match(self, name, server_tools):
        """Req 13.2: same required parameters; model_type or element_type, nothing else."""
        server_schema = server_tools[name]["inputSchema"]
        agent_schema = _agent_schemas()[name]
        assert _required(agent_schema) == _required(server_schema) == GENERIC_REQUIRED[name]

    def test_enum_values_match(self, name, server_tools):
        """Req 13.2: same `enum` values on every parameter, equal to the expected closed sets."""
        server_schema = server_tools[name]["inputSchema"]
        agent_schema = _agent_schemas()[name]
        server_props = server_schema.get("properties", {})
        agent_props = agent_schema.get("properties", {})
        found: dict[str, list[str]] = {}
        for param, server_prop in server_props.items():
            server_enum = _enum_values(server_schema, server_prop)
            agent_enum = _enum_values(agent_schema, agent_props[param])
            assert agent_enum == server_enum, f"{name}.{param}: enum differs"
            if server_enum is not None:
                found[param] = server_enum
        assert found == GENERIC_ENUMS[name]

    def test_property_descriptions_match(self, name, server_tools):
        """Req 13.2: same non-empty description for every parameter."""
        server_props = server_tools[name]["inputSchema"].get("properties", {})
        agent_props = _agent_schemas()[name].get("properties", {})
        assert server_props
        for param, server_prop in server_props.items():
            description = server_prop.get("description")
            assert description, f"{name}.{param}: no server description"
            assert agent_props[param].get("description") == description, f"{name}.{param}"

    def test_agent_spec_has_no_ctx(self, name):
        """Req 13.3: `ctx` and the `Context` definition are absent from the agent spec."""
        schema = _agent_schemas()[name]
        assert "ctx" not in schema.get("properties", {})
        assert "ctx" not in schema.get("required", [])
        assert "Context" not in schema.get("$defs", {})
