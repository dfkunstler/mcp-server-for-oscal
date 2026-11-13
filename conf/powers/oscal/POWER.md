---
name: "oscal"
displayName: "OSCAL (Open Security Controls Assessment Language)"
description: "AI assistant tools for working with NIST's Open Security Controls Assessment Language (OSCAL) - validate content, access schemas, explore component definitions, query documentation, and discover community resources for GRC automation."
keywords: ["oscal", "security compliance", "GRC engineering", "compliance automation", "Component Definition", "Catalog", "Component", "Profile", "Mapping", "Assessment Plan", "Assessment Results", "POAM", "Control", "Finding", "Observation", "Risk", "attestation", "remediation", "back matter", "AP", "AR", "SAP", "SAR", "SSP" ]
author: "AWS Labs"
---

# OSCAL (Open Security Controls Assessment Language)

## Overview

This power provides AI assistants with comprehensive tools to work with NIST's Open Security Controls Assessment Language (OSCAL). OSCAL is a set of framework-agnostic, vendor-neutral, machine-readable schemas that describe the full life cycle of governance, risk, and compliance (GRC) artifacts, from controls to remediation plans.

The power enables AI assistants to provide accurate, authoritative guidance about OSCAL architecture, models, use cases, requirements, and implementation by accessing:
- All OSCAL model schemas (JSON and XSD)
- Multi-level OSCAL content validation (well-formedness, JSON Schema, OSCAL model, oscal-cli)
- Bundled AWS component definitions with capability and component navigation
- Community resources and tools
- Structured data about OSCAL's three-layer architecture

This addresses the common challenge where AI assistants alone produce inconsistent results related to OSCAL due to limited availability of examples in the public domain.

## What is OSCAL?

OSCAL (Open Security Controls Assessment Language) is a set of framework-agnostic, vendor-neutral, machine-readable schemas developed by NIST that describe the full life cycle of GRC (governance, risk, compliance) artifacts, from controls to remediation plans. OSCAL enables automation of GRC workflows by replacing digital paper (spreadsheets, PDFs, etc.) with a standard-based structured data format.

### OSCAL's Three-Layer Architecture

**Control Layer (3 models):**
- **Catalog** - Collections of security controls (e.g., NIST 800-53, ISO 27001)
- **Profile** - Tailored selections of controls from catalogs
- **Mapping Collection** - Relationships between different control frameworks

**Implementation Layer (2 models):**
- **Component Definition** - How components implement security controls
- **System Security Plan (SSP)** - How a system implements required controls

**Assessment Layer (3 models):**
- **Assessment Plan (SAP)** - Plans for assessing control implementation
- **Assessment Results (SAR)** - Results of control assessments
- **Plan of Action and Milestones (POA&M)** - Remediation plans for findings

## Available Tools

| Tool | Description |
|------|-------------|
| `list_oscal_models` | List all 8 OSCAL model types with metadata (layer, status, descriptions) |
| `get_oscal_schema` | Retrieve JSON or XSD schema for any OSCAL model |
| `get_allowed_values` | Look up the values bundled OSCAL Metaschema `allowed-values` constraints permit at a target (e.g. `defined-component/prop/@name`), with per-value namespace and conditions (paginated) |
| `get_constraints` | List the bundled OSCAL Metaschema constraints (`allowed-values`, `matches`, `index`, `index-has-key`, `is-unique`, `has-cardinality`, `expect`) that apply at a target, with source file and line, optionally filtered by type (paginated) |
| `find_references_to` | Find the fields, flags, props, and links that bundled OSCAL Metaschema `index-has-key` constraints require to reference a given assembly by key (e.g. what refers to a `component` by UUID), optionally scoped to a model, with source file and line (paginated) |
| `get_definition` | Read a bundled OSCAL Metaschema definition (assembly, field, or flag) by name with its formal name, description, remarks, flags, model children, and constraint summaries, optionally scoped to a model or kind, with source file and line (paginated) |
| `get_metaschema_source` | Read a line range of a bundled OSCAL Metaschema source file verbatim, to check a source file and line reported by the other Metaschema tools; call without a file to list the bundled files (paged by line range) |
| `list_oscal_resources` | Browse curated OSCAL community resources, tools, and educational content |
| `validate_oscal_content` | Validate OSCAL JSON content through a 4-level pipeline (well-formedness, JSON Schema, OSCAL model, oscal-cli) |
| `validate_oscal_file` | Validate an OSCAL JSON file (local or remote URI) through the same 4-level pipeline |
| `query_component_definition` | Query component definitions to find capabilities and components by UUID, title, or type |
| `get_capability` | Retrieve a single capability by UUID with full OSCAL representation |
| `query_oscal_documentation` | Search OSCAL documentation and return paged `{title, snippet}` passages; local pages carry a `docId`. Uses an AWS Bedrock Knowledge Base when `OSCAL_KB_ID` is set (merged with your own docs), else local full-text search |
| `get_oscal_documentation` | Read a documentation page by `docId` (from `query_oscal_documentation`) as a verbatim line range |
| `about` | Server metadata including version and supported OSCAL version |
| **Generic document tools (all 8 model types)** | |
| `list_documents` | List loaded OSCAL documents of one `model_type` with UUID, title, child count, and size (paginated) |
| `query_documents` | Query loaded OSCAL documents of one `model_type` by UUID or title, or list all, with `childTypeCounts` (paginated) |
| `list_child_elements` | List child elements (controls, components, findings, etc.) of one `element_type`, optionally scoped by parent document UUID (paginated) |
| **Cross-cutting tools** | |
| `text_search_oscal` | Full-text search across loaded OSCAL documents and child elements (including control prose and element IDs), ranked by relevance. Child hits carry `entity_id` and `parentDocumentUuid` for `get_child_element` |
| `get_child_element` | Retrieve a single child element by its identifier (UUID or token ID), with optional parent document scoping |

### Key Tool Parameters

**`get_oscal_schema`**
- `model_name` (string, default: `"complete"`) — OSCAL model name (e.g. `"catalog"`, `"profile"`, `"ssp"`, `"component-definition"`). Use `list_oscal_models` to discover valid names. The `"complete"` schema is very large — request specific models when possible.
- `schema_type` (string, default: `"json"`) — `"json"` for JSON Schema, `"xsd"` for XML Schema.

**`validate_oscal_content`**
- `content` (string, required) — OSCAL JSON content as a string.
- `model_type` (string, optional) — OSCAL model type (e.g. `"catalog"`, `"profile"`). Auto-detected from root key if omitted.

**`validate_oscal_file`**
- `file_uri` (string, required) — Local file path or remote URI. Remote URIs require `OSCAL_ALLOW_REMOTE_URIS=true`.
- `model_type` (string, optional) — Same as `validate_oscal_content`.

**`query_component_definition`**
- `query_type` (string, default: `"all"`) — `"all"`, `"by_uuid"`, `"by_title"`, or `"by_type"`.
- `query_value` (string, optional) — Value to search for. Required for `by_uuid`, `by_title`, `by_type`.
- `component_definition_filter` (string, optional) — UUID or title of a Component Definition to narrow scope.

**`get_capability`**
- `uuid` (string, required) — UUID of the capability. Use `list_child_elements` with `element_type="capability"` to discover UUIDs.

**`list_documents`**
- `model_type` (string, required) — One of the 8 OSCAL model types (e.g. `"catalog"`, `"component-definition"`, `"system-security-plan"`).
- `offset` (integer, default: `0`) — Zero-based pagination offset.
- `limit` (integer, default: `10`) — Maximum items per page (1–100).

**`query_documents`**
- `model_type` (string, required) — One of the 8 OSCAL model types, as for `list_documents`.
- `query_type` (string, default: `"all"`) — `"all"`, `"by_uuid"`, or `"by_title"`.
- `query_value` (string, optional) — Value to search for. Required for `by_uuid` and `by_title`.
- `offset` (integer, default: `0`) — Zero-based pagination offset.
- `limit` (integer, default: `10`) — Maximum items per page (1–100).

**`list_child_elements`**
- `element_type` (string, required) — One of `component`, `capability`, `control`, `group`, `import`, `modify`, `control-implementation`, `system-component`, `task`, `activity`, `result`, `finding`, `poam-item`, `mapping`. Each type belongs to one model (e.g. `control` to catalog).
- `parent_doc_uuid` (string, optional) — UUID of the parent document to scope the listing.
- `offset` (integer, default: `0`) — Zero-based pagination offset.
- `limit` (integer, default: `10`) — Maximum items per page (1–100).

**`text_search_oscal`**
- `query_text` (string, required) — The search text.
- `oscal_model_type` (string, optional) — Filter results to a specific OSCAL model type (e.g. `"catalog"`, `"system-security-plan"`). When omitted, all model types are searched.
- `offset` (integer, default: `0`) — Zero-based pagination offset.
- `limit` (integer, default: `10`) — Maximum items per page (1–100). A page may hold fewer to fit the response budget.

**`query_oscal_documentation`**
- `query` (string, required) — Question or search terms about OSCAL.
- `offset` (integer, default: `0`) — Zero-based pagination offset.
- `limit` (integer, default: `10`) — Maximum items per page (1–100). A page may hold fewer to fit the response budget.

**`get_oscal_documentation`**
- `doc_id` (string, required) — `docId` from a `query_oscal_documentation` result.
- `start_line` (integer, default: `1`) — 1-based first line to return.
- `line_count` (integer, default: `200`) — Maximum lines to return (1–500). Continue from `end-line` + 1 while `has-more` is true.

**`get_child_element`**
- `element_id` (string, required) — UUID or token ID of the child element (e.g. `"ac-1"` for a catalog control).
- `parent_doc_uuid` (string, optional) — UUID of the parent document to narrow the search.

### Tool Usage Examples

**Get a specific OSCAL schema:**
```
get_oscal_schema(model_name="catalog", schema_type="json")
```

**Validate OSCAL content inline:**
```
validate_oscal_content(content='{"catalog": {...}}')
```

**Validate a local OSCAL file:**
```
validate_oscal_file(file_uri="/path/to/my-ssp.json")
```

**Find a component by title:**
```
query_component_definition(query_type="by_title", query_value="Amazon S3")
```

**Explore component definitions top-down:**
```
list_documents(model_type="component-definition")  →  list_child_elements(element_type="capability")  →  get_capability(uuid="...")
```

**List all loaded catalogs:**
```
list_documents(model_type="catalog")
```

**Query a specific catalog by title:**
```
query_documents(model_type="catalog", query_type="by_title", query_value="NIST SP 800-53")
```

**List controls in a specific catalog:**
```
list_child_elements(element_type="control", parent_doc_uuid="<catalog-uuid>")
```

**Full-text search across all OSCAL documents:**
```
text_search_oscal(query_text="access control", oscal_model_type="catalog")
```

**Search documentation, then read a full page:**
```
query_oscal_documentation(query="profile resolution")  →  get_oscal_documentation(doc_id="<docId>")
```

**Retrieve a specific child element:**
```
get_child_element(element_id="ac-1", parent_doc_uuid="<catalog-uuid>")
```

**Explore OSCAL documents top-down:**
```
list_documents(model_type="catalog")  →  list_child_elements(element_type="control", parent_doc_uuid="...")  →  get_child_element(element_id="ac-1")
```

## Onboarding

### Prerequisites

- **uv package manager** for Python ([Installation instructions](https://docs.astral.sh/uv/getting-started/installation/))
- **Python 3.13 or higher** ([Install with uv](https://docs.astral.sh/uv/guides/install-python/))

### Installation

The OSCAL MCP server is distributed as a Python package and runs via `uvx` (no local installation required).

**Verify Prerequisites:**
```bash
# Check uv installation
uv --version

# Check Python version
python --version
```

### Configuration

Add the OSCAL MCP server to your AI assistant's MCP configuration:

Use the provided `mcp.json` file.

#### Optional Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OSCAL_KB_ID` | AWS Bedrock Knowledge Base ID used by `query_oscal_documentation` | _(empty — local documentation search)_ |
| `BEDROCK_MODEL_ID` | Bedrock model for agent features | `us.anthropic.claude-sonnet-4-20250514-v1:0` |
| `AWS_PROFILE` | AWS profile for Bedrock access | _(default credentials)_ |
| `AWS_REGION` | AWS region | _(SDK default)_ |
| `LOG_LEVEL` | Server log level | `INFO` |
| `OSCAL_MCP_TRANSPORT` | Transport protocol (`stdio` or `streamable-http`) | `stdio` |
| `OSCAL_ALLOW_REMOTE_URIS` | Allow `validate_oscal_file` to fetch remote URIs | `false` |

### Verification

After configuration, restart your AI assistant and verify the OSCAL tools are available:

```
> How many GA OSCAL models are there?
```

Expected response should list the 8 GA OSCAL models across the three layers.

## Common Workflows

### Workflow 1: Explore OSCAL Models and Architecture

**Goal:** Understand OSCAL's structure and available models

**Steps:**
1. **List all OSCAL models:**
   ```
   List all available OSCAL models with their status and descriptions
   ```

2. **Get detailed schema for a specific model:**
   ```
   Get the JSON schema for the OSCAL Catalog model
   ```

3. **Understand model relationships:**
   ```
   Explain how the OSCAL Profile model relates to the Catalog model
   ```

### Workflow 2: Validate OSCAL Content

**Goal:** Check whether OSCAL JSON is correct and conformant

**Steps:**
1. **Validate inline content:**
   ```
   Validate this OSCAL JSON content: { "catalog": { ... } }
   ```

2. **Validate a file on disk:**
   ```
   Validate the OSCAL file at /path/to/my-ssp.json
   ```

3. **Interpret results:** The validation pipeline returns per-level results:
   - Level 1: Well-formedness (valid JSON object?)
   - Level 2: JSON Schema conformance (matches NIST schema?)
   - Level 3: Model checks (oscal-bindings Pydantic model validation, reported as `model`)
   - Level 4: oscal-cli validation (if installed)

### Workflow 3: Explore Component Definitions

**Goal:** Navigate bundled AWS component definitions to understand control implementations

**Steps:**
1. **List available component definitions:**
   ```
   List all loaded OSCAL component definitions
   ```

2. **Browse capabilities within a definition:**
   ```
   List all capabilities in the component definitions
   ```

3. **Drill into a specific capability:**
   ```
   Show me the details of capability <uuid>
   ```

4. **Query by component title or type:**
   ```
   Find the component definition for Amazon S3
   ```

### Workflow 4: Community Resources Discovery

**Goal:** Find OSCAL tools, content, and educational materials

**Steps:**
1. **Browse available resources:**
   ```
   Show me available OSCAL community resources
   ```

2. **Filter by category:**
   ```
   What OSCAL tools are available for validation?
   ```

3. **Find educational content:**
   ```
   Are there any OSCAL tutorials or presentations available?
   ```

## Troubleshooting

### MCP Server Connection Issues

**Problem:** OSCAL MCP server won't start or connect

**Solutions:**
1. **Verify uv installation:**
   ```bash
   uv --version
   ```
   If not installed, follow [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)

2. **Test server manually:**
   ```bash
   uvx mcp-server-for-oscal@latest
   ```

3. **Check Python version:**
   ```bash
   python --version
   ```
   Ensure Python 3.13 or higher is installed

4. **Restart AI assistant** after configuration changes

5. **Check MCP configuration syntax** — ensure JSON is valid

### Tool Execution Errors

**Error:** "Tool not found" or "Permission denied"
**Solution:**
1. Verify MCP configuration is correct
2. For Kiro: Add tools to `autoApprove` list in configuration
3. Restart AI assistant

**Error:** "Schema not found for model X"
**Solution:**
1. Use `list_oscal_models` to see valid model names
2. Use exact model names: catalog, profile, ssp, component-definition, etc.

**Error:** "Unknown documentation ID. Use a docId from a query_oscal_documentation result."
**Cause:** `get_oscal_documentation` only accepts a `docId` returned by `query_oscal_documentation`; OSCAL document UUIDs and Knowledge Base hits (which have no `docId`) don't work
**Solution:** Run `query_oscal_documentation` and pass a hit's `docId`. For OSCAL documents and child elements, use `text_search_oscal` and `get_child_element` instead

### Package Installation Issues

**Problem:** uvx can't find or install mcp-server-for-oscal
**Solution:**
1. Check internet connection
2. Try updating uv: `uv self update`
3. Clear uv cache: `uv cache clean`

## Best Practices

- **Start with `list_oscal_models`** to understand the full OSCAL architecture before diving into specific models
- **Use `get_oscal_schema`** to understand exact data structures when implementing OSCAL documents
- **Use `validate_oscal_content`** to verify generated OSCAL JSON before saving or submitting
- **Explore component definitions top-down**: `list_documents` (`model_type="component-definition"`) → `list_child_elements` (`element_type="capability"`) → `get_capability` → `query_component_definition` for details
- **Prefer capabilities** over components when exploring what a component definition offers (`list_child_elements` with `element_type="capability"` rather than `"component"`) — capabilities group related components
- **Leverage community resources** via `list_oscal_resources` to find existing tools rather than building from scratch
- **Request the `complete` schema only as a last resort** — it's very large and may overflow context windows

---

**Package:** `mcp-server-for-oscal`
**MCP Server:** oscal
**GitHub:** https://github.com/dfkunstler/mcp-server-for-oscal
