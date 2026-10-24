"""Unit tests for ParamDoc and the shared parameter text in tools/descriptions.py.

Feature: tool-context-optimization. Validates: Requirements 1.5, 3.1, 3.4.
"""

import asyncio
from typing import Annotated, Any

import pytest
from mcp.server.mcpserver import Context, MCPServer
from strands import tool

from mcp_server_for_oscal.tools import descriptions
from mcp_server_for_oscal.tools.descriptions import (
    LIMIT_DOC,
    OFFSET_DOC,
    PARENT_DOC_UUID_DOC,
    QUERY_TYPE_DOC,
    Limit,
    Offset,
    ParamDoc,
    ParentDocUuid,
    PerModelQueryType,
)
from mcp_server_for_oscal.tools.tool_budget import PARAMETER_DESCRIPTION_CAP

NAME_DOC = "Name of the thing to look up."


@tool()
def sample_tool(
    name: Annotated[str, ParamDoc(NAME_DOC)],
    query_type: PerModelQueryType = "all",
    parent_doc_uuid: ParentDocUuid = None,
    offset: Offset = 0,
    limit: Limit = 10,
    ctx: Context | None = None,
) -> dict:
    """Return the arguments the tool received."""
    return {
        "name": name,
        "query_type": query_type,
        "parent_doc_uuid": parent_doc_uuid,
        "offset": offset,
        "limit": limit,
        "ctx": ctx,
    }


EXPECTED_DOCS = {
    "name": NAME_DOC,
    "query_type": QUERY_TYPE_DOC,
    "parent_doc_uuid": PARENT_DOC_UUID_DOC,
    "offset": OFFSET_DOC,
    "limit": LIMIT_DOC,
}


def _server_properties() -> dict[str, Any]:
    server = MCPServer("test")  # fresh instance, never main.mcp
    server.add_tool(sample_tool)
    (tool_def,) = asyncio.run(server.list_tools())
    return tool_def.input_schema["properties"]


def _agent_properties() -> dict[str, Any]:
    return sample_tool.tool_spec["inputSchema"]["json"]["properties"]


class TestParamDocInBothRegistries:
    def test_paramdoc_is_a_str(self) -> None:
        doc = ParamDoc(NAME_DOC)
        assert isinstance(doc, str)
        assert doc == NAME_DOC

    def test_server_schema_has_each_description(self) -> None:
        props = _server_properties()
        for param, doc in EXPECTED_DOCS.items():
            assert props[param].get("description") == doc, param

    def test_agent_spec_has_each_description(self) -> None:
        props = _agent_properties()
        for param, doc in EXPECTED_DOCS.items():
            assert props[param].get("description") == doc, param

    def test_server_and_agent_descriptions_match(self) -> None:
        server_props = _server_properties()
        agent_props = _agent_properties()
        for param in EXPECTED_DOCS:
            assert server_props[param]["description"] == agent_props[param]["description"]

    def test_server_schema_excludes_ctx(self) -> None:
        assert "ctx" not in _server_properties()


class TestLiteralAlias:
    @pytest.mark.parametrize("props_fn", [_server_properties, _agent_properties])
    def test_literal_alias_produces_enum(self, props_fn) -> None:
        prop = props_fn()["query_type"]
        assert prop["enum"] == ["all", "by_uuid", "by_title"]
        assert prop["description"] == QUERY_TYPE_DOC


class TestDirectCalls:
    def test_defaults_are_plain_values(self) -> None:
        result = sample_tool("x")
        assert result == {
            "name": "x",
            "query_type": "all",
            "parent_doc_uuid": None,
            "offset": 0,
            "limit": 10,
            "ctx": None,
        }

    def test_explicit_arguments_pass_through(self) -> None:
        result = sample_tool("x", query_type="by_uuid", offset=5, limit=3)
        assert result["query_type"] == "by_uuid"
        assert result["offset"] == 5
        assert result["limit"] == 3


SHARED_DOCS = {
    name: value
    for name, value in vars(descriptions).items()
    if name.endswith("_DOC") and isinstance(value, str)
}


class TestSharedDocLengths:
    def test_shared_docs_found(self) -> None:
        assert {"OFFSET_DOC", "LIMIT_DOC", "TARGET_SELECTOR_DOC"} <= SHARED_DOCS.keys()

    @pytest.mark.parametrize("name", sorted(SHARED_DOCS))
    def test_shared_doc_within_cap(self, name: str) -> None:
        value = SHARED_DOCS[name]
        assert value.strip(), name
        assert len(value) <= PARAMETER_DESCRIPTION_CAP, (
            f"{name} length {len(value)} > limit {PARAMETER_DESCRIPTION_CAP}"
        )
