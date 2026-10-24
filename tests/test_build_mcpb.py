"""Tests for ``bin/build_mcpb.py``: the MCPB manifest tool list matches the live tools/list.

Validates: Requirements 10.1 (tool-context-optimization).
"""

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from mcp_server_for_oscal import main
from mcp_server_for_oscal.tools.descriptions import ABOUT_DESCRIPTION
from mcp_server_for_oscal.tools.tool_budget import summary_sentence

BUILD_MCPB_PATH = Path(__file__).resolve().parent.parent / "bin" / "build_mcpb.py"


@pytest.fixture(scope="module")
def build_mcpb() -> ModuleType:
    """Load ``bin/build_mcpb.py`` as a module; its ``main()`` is behind a ``__main__`` guard."""
    spec = importlib.util.spec_from_file_location("_build_mcpb_under_test", BUILD_MCPB_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def live_descriptions() -> dict[str, str]:
    """Map each tool name in the live tools/list to its description."""
    server = main.build_server()
    main._setup_tools(server)
    tools = asyncio.run(server.list_tools())
    return {t.name: t.description or "" for t in tools}


@pytest.mark.unit
class TestToolList:
    def test_names_match_live_tools(self, build_mcpb, live_descriptions):
        names = [entry["name"] for entry in build_mcpb.tool_list()]
        assert len(names) == len(set(names))
        assert set(names) == set(live_descriptions)

    def test_descriptions_are_live_summary_sentences(self, build_mcpb, live_descriptions):
        for entry in build_mcpb.tool_list():
            expected = summary_sentence(live_descriptions[entry["name"]])
            assert expected, f"{entry['name']} has an empty summary sentence"
            assert entry["description"] == expected, entry["name"]

    def test_about_entry(self, build_mcpb):
        about = [e for e in build_mcpb.tool_list() if e["name"] == "about"]
        assert about == [{"name": "about", "description": summary_sentence(ABOUT_DESCRIPTION)}]
