# Product Overview

MCP Server for OSCAL is a Model Context Protocol (MCP) server that gives AI assistants (Claude, Kiro, Claude Code, etc.) tools to work with NIST's Open Security Controls Assessment Language (OSCAL).

OSCAL is a set of framework-agnostic, vendor-neutral, machine-readable schemas from NIST that describe the full GRC (governance, risk, compliance) lifecycle — from controls to remediation plans. It replaces spreadsheets and PDFs with structured, automatable data.

## Purpose

Most LLMs produce inconsistent OSCAL output due to limited public examples. This MCP server solves that by providing tools that give AI agents accurate, authoritative OSCAL guidance covering architecture, models, use-cases, requirements, and implementation.

## Key Characteristics

- Runs locally with no external service dependencies by default (all OSCAL schemas and docs are bundled)
- Uses `stdio` transport by default; `streamable-http` is available but lacks auth
- Bundled content is integrity-verified at startup via SHA-256 hash manifests
- Published on PyPI as `mcp-server-for-oscal`
- Licensed under Apache-2.0

## Tools Provided

The live list is `get_tool_list()` in `tools/__init__.py` (19 tools) plus `about`, which only the server registers.

- `list_oscal_models` — list available OSCAL model types
- `get_oscal_schema` — retrieve JSON/XSD schemas for any OSCAL model
- `get_allowed_values`, `get_constraints`, `find_references_to`, `get_definition`, `get_metaschema_source` — look up Metaschema definitions, constraints, and references from the bundled modules
- `list_oscal_resources` — list bundled OSCAL resources
- `list_documents` — list the loaded documents of one model type (all eight models, including component definitions)
- `query_documents` — query the documents of one model type by UUID, title, type, or all
- `list_child_elements` — list child elements (controls, groups, components, capabilities, and so on) of one element type, optionally scoped to a parent document
- `get_child_element` — fetch one child element by ID
- `text_search_oscal` — full-text search across documents and child elements
- `query_component_definition` — query AWS component definitions by capability
- `get_capability` — fetch one capability by UUID
- `validate_oscal_content` / `validate_oscal_file` — validate OSCAL JSON against schemas and oscal-bindings models
- `query_oscal_documentation` — search OSCAL documentation (AWS Bedrock Knowledge Base when `OSCAL_KB_ID` is set, merged with user docs; otherwise local full-text search)
- `get_oscal_documentation` — read a documentation page by `docId` as a line range
- `about` — server metadata
