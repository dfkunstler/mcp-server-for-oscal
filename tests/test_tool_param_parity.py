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
