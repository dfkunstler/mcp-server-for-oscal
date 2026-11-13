# Project Structure

```
mcp-server-for-oscal/
├── src/mcp_server_for_oscal/       # Main package
│   ├── __init__.py
│   ├── __main__.py                 # Module entry point
│   ├── main.py                     # MCP server setup, CLI arg parsing, startup integrity checks
│   ├── config.py                   # Config class — loads env vars, CLI overrides, singleton `config`
│   ├── oscal_agent.py              # Strands agent integration
│   ├── tools/                      # MCP tool implementations (a module holds one or more related tools)
│   │   ├── __init__.py             # get_tool_list(): the tool registry
│   │   ├── utils.py                # Shared: OSCALModelType enum, schema loading, hash verification
│   │   ├── get_schema.py
│   │   ├── list_models.py
│   │   ├── list_oscal_resources.py
│   │   ├── query_component_definition.py  # 2 tools: query_component_definition, get_capability
│   │   ├── query_documentation.py  # query_oscal_documentation, get_oscal_documentation
│   │   ├── query_oscal_models.py   # 5 tools: list_documents, query_documents, list_child_elements, text_search_oscal, get_child_element
│   │   ├── validate_oscal_content.py  # validate_oscal_content, validate_oscal_file
│   │   └── ...                     # Metaschema tools, store, and helpers; see AGENTS.md code map
│   ├── oscal_schemas/              # Bundled OSCAL JSON & XSD schemas + hashes.json manifest
│   ├── oscal_docs/                 # Bundled OSCAL documentation + hashes.json manifest
│   └── component_definitions/      # Bundled AWS component definitions (zip) + hashes.json
├── tests/                          # Test suite (mirrors src structure)
│   ├── conftest.py                 # Shared fixtures, pytest markers (unit, integration, slow)
│   ├── fixtures/                   # JSON test fixtures (sample/invalid component definitions)
│   ├── test_*.py                   # Top-level tests (config, main, utils, integration, properties)
│   └── tools/                      # Per-tool test files (test_get_schema.py, etc.)
├── bin/                            # Utility scripts (update_hashes.py, update-oscal-schemas.sh)
├── conf/
│   ├── agentcore/                  # Dockerfile for Bedrock AgentCore deployment
│   └── powers/oscal/               # Kiro Power config (POWER.md, mcp.json)
├── private/docs/                   # Generated reports (coverage, bandit) — not committed
├── pyproject.toml                  # Project metadata, dependencies, hatch config, tool settings
├── _version.py                     # Auto-generated version file (hatch-vcs)
└── requirements.txt                # Pinned dependencies for reproducible builds
```

## Conventions

- Each file under `tools/` holds one tool or a few related tools (for example `query_oscal_models.py` has five); group tools by subject, not one per file
- Tools are listed in `get_tool_list()` in `tools/__init__.py`; `main.py::_setup_tools()` registers that list via `mcp.add_tool()`
- Bundled content directories each contain a `hashes.json` manifest for integrity verification at startup
- Tests use `pytest` markers: `unit`, `integration`, `slow`; async tests auto-detected and marked with `pytest.mark.asyncio`
- Test fixtures are JSON files in `tests/fixtures/`
- The `private/` directory holds generated artifacts and is gitignored
