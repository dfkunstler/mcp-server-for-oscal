# OSCAL MCP Server Tools

This package contains all tool implementations for the OSCAL MCP server. Each tool is implemented as a Python module with the `@tool` decorator from the `strands` library, making them automatically discoverable by the MCP server (`MCPServer`).

Each tool's description is its docstring: a summary sentence, an optional "Use when" sentence, and an optional constraint, with no `Args:` or `Returns:` sections. Parameter descriptions live in the `inputSchema` (declared with `Annotated[T, ParamDoc("...")]`, shared aliases in `descriptions.py`). Closed value sets are `enum`s. The server `instructions` carry a Routing_Guide that maps common tasks to tools. Size limits are in `tool_budget.py` and enforced by `tests/test_tool_budget.py`.

## Available Tools

| Tool | Description |
|------|-------------|
| `list_oscal_models` | List all 8 OSCAL model types with metadata (layer, status, descriptions) |
| `get_oscal_schema` | Retrieve the JSON or XSD schema of one OSCAL model (`model_name` required) |
| `get_allowed_values` | Look up the values bundled OSCAL Metaschema `allowed-values` constraints permit at a target (e.g. `defined-component/prop/@name`), with per-value namespace and conditions (paginated) |
| `get_constraints` | List the bundled OSCAL Metaschema constraints (`allowed-values`, `matches`, `index`, `index-has-key`, `is-unique`, `has-cardinality`, `expect`) that apply at a target, with source file and line, optionally filtered by type (paginated) |
| `find_references_to` | Find the fields, flags, props, and links that bundled OSCAL Metaschema `index-has-key` constraints require to reference a given assembly by key (e.g. what refers to a `component` by UUID), optionally scoped to a model, with source file and line (paginated) |
| `get_definition` | Read a bundled OSCAL Metaschema definition (assembly, field, or flag) by name with its formal name, description, remarks, flags, model children, and constraint summaries, optionally scoped to a model or kind, with source file and line (paginated) |
| `get_metaschema_source` | Read a line range of a bundled OSCAL Metaschema source file verbatim, to check a source file and line reported by the other Metaschema tools; call without a file to list the bundled files (paged by line range) |
| `list_oscal_resources` | List curated OSCAL community resources as `{category, text}` entries (paginated) |
| `validate_oscal_content` | Validate OSCAL JSON content through a 4-level pipeline (well-formedness, JSON Schema, model, oscal-cli) |
| `validate_oscal_file` | Validate an OSCAL JSON file (local path, or remote URI when the operator enables it) through the same 4-level pipeline |
| `query_component_definition` | Return components (or a matching capability) from loaded Component Definitions by UUID, title, or type (paginated) |
| `list_component_definitions` | List loaded Component Definitions with summary metadata (paginated) |
| `list_components` | List loaded components with summary metadata (paginated) |
| `list_capabilities` | List loaded capabilities, for Component Definitions that contain them (paginated) |
| `get_capability` | Retrieve a single capability by UUID as OSCAL JSON (`uuid` required) |
| `query_oscal_documentation` | Search OSCAL documentation: Bedrock Knowledge Base when configured, else full-text search of bundled docs |
| `about` | Server metadata including version and supported OSCAL version |
| **Catalog tools** | |
| `query_catalog` | Query OSCAL Catalog documents by UUID or title, or list all, with `childTypeCounts` (paginated) |
| `list_catalogs` | List loaded OSCAL Catalogs with summary metadata (UUID, title, child count, size) |
| `list_catalog_controls` | List controls within OSCAL Catalog documents, optionally scoped by parent catalog UUID |
| `list_catalog_groups` | List control groups (families) within OSCAL Catalog documents, optionally scoped by parent catalog UUID |
| **SSP tools** | |
| `query_ssp` | Query OSCAL System Security Plan documents by UUID or title, or list all, with `childTypeCounts` (paginated) |
| `list_ssps` | List loaded OSCAL System Security Plans with summary metadata |
| `list_ssp_control_implementations` | List control-implementation elements within OSCAL SSP documents |
| `list_ssp_system_components` | List system-component elements (servers, services, software) within OSCAL SSP documents |
| **Profile tools** | |
| `query_profile` | Query OSCAL Profile documents by UUID or title, or list all, with `childTypeCounts` (paginated) |
| `list_profiles` | List loaded OSCAL Profiles with summary metadata |
| `list_profile_imports` | List import elements (catalog/profile references) within OSCAL Profile documents |
| `list_profile_modify` | List modify elements (control customisations) within OSCAL Profile documents |
| **Assessment Plan tools** | |
| `query_assessment_plan` | Query OSCAL Assessment Plan documents by UUID or title, or list all, with `childTypeCounts` (paginated) |
| `list_assessment_plans` | List loaded OSCAL Assessment Plans with summary metadata |
| `list_assessment_plan_tasks` | List task elements within OSCAL Assessment Plan documents |
| `list_assessment_plan_activities` | List activity elements (assessment methods/procedures) within OSCAL Assessment Plan documents |
| **Assessment Results tools** | |
| `query_assessment_results` | Query OSCAL Assessment Results documents by UUID or title, or list all, with `childTypeCounts` (paginated) |
| `list_assessment_results` | List loaded OSCAL Assessment Results with summary metadata |
| `list_assessment_results_results` | List result elements (assessment outcomes) within OSCAL Assessment Results documents |
| `list_assessment_results_findings` | List finding elements (control implementation determinations) within OSCAL Assessment Results documents |
| **POA&M tools** | |
| `query_poam` | Query OSCAL Plan of Action and Milestones (POA&M) documents by UUID or title, or list all, with `childTypeCounts` (paginated) |
| `list_poams` | List loaded OSCAL Plans of Action and Milestones with summary metadata |
| `list_poam_items` | List POA&M item elements (security issues and remediation plans) within OSCAL POA&M documents |
| **Mapping Collection tools** | |
| `query_mapping_collection` | Query OSCAL Mapping Collection documents by UUID or title, or list all, with `childTypeCounts` (paginated) |
| `list_mapping_collections` | List loaded OSCAL Mapping Collections with summary metadata |
| `list_mapping_collection_mappings` | List mapping elements (cross-framework control relationships) within OSCAL Mapping Collection documents |
| **Cross-cutting tools** | |
| `text_search_oscal` | Full-text search across all loaded OSCAL documents and child elements using SQLite FTS5, ranked by relevance |
| `get_child_element` | Retrieve a single child element by its identifier (UUID or token ID), with optional parent document scoping |

### 1. List OSCAL Models
**Tool**: `list_oscal_models`

Returns metadata about all available OSCAL model types including:
- Model descriptions and purposes
- OSCAL layer (Control, Implementation, Assessment)
- Formal and short names
- Release status (all currently GA)

Covers all 8 OSCAL models: Catalog, Profile, Mapping, Component Definition, System Security Plan, Assessment Plan, Assessment Results, and Plan of Action & Milestones. The model names are the valid values for `model_name`, `model`, and `model_type` parameters of the other tools.

**Parameters**: None

**Returns**: Dictionary mapping model names to their metadata

---

### 2. Get OSCAL Schema
**Tool**: `get_oscal_schema`

Retrieves JSON or XSD schemas for OSCAL models. OSCAL schemas are self-documenting, making this the primary tool for understanding model structure, properties, and requirements.

**Parameters**:
- `model_name` (str, required): OSCAL model name from `list_oscal_models`, or `"complete"` for all models (very large)
- `schema_type` (`"json"` | `"xsd"`, default `"json"`): Other values are rejected by input validation

**Returns**: JSON string for `json`; raw XSD (XML) text for `xsd`

---

### 3. List OSCAL Community Resources
**Tool**: `list_oscal_resources`

Lists the curated OSCAL community resources from [Awesome OSCAL](https://github.com/oscal-club/awesome-oscal): tools and software, educational content, example documents, articles, talks, and libraries.

**Parameters**:
- `offset` (int, default=0), `limit` (int, default=10, 1–100): Pagination

**Returns**: The standard paginated response (see [Common Return Format](#common-return-format)). Each item is one top-level list entry from the awesome-oscal markdown:
- `category`: The section heading the entry belongs to (`"General"` before the first heading)
- `text`: The entry's markdown, with continuation lines joined by single spaces

An `offset` past the end returns empty `items` with the correct `total`. A negative `offset` or a `limit` outside 1–100 raises an error.

---

### 4. Query Component Definitions
**Tool**: `query_component_definition`

Returns components (services, software, regions, etc.) and their control implementations from loaded Component Definitions. The main route is `list_component_definitions` and `list_components` to find UUIDs and titles, then `query_component_definition`. `by_uuid` and `by_title` queries return a matching capability first, if one exists; use `list_capabilities` and `get_capability` only for Component Definitions that contain capabilities.

**Parameters**:
- `component_definition_filter` (str, optional): UUID or exact title (case-insensitive) of one Component Definition to search
- `query_type` (`"all"` | `"by_uuid"` | `"by_title"` | `"by_type"`, default `"all"`): `by_title` matches the title, then property values; `by_type` filters components by their type (this tool only; the per-model `query_*` tools have no `by_type`)
- `query_value` (str, optional): UUID, title, or component type to match. Required unless `query_type` is `"all"`
- `return_format` (`"raw"`, default `"raw"`): Only OSCAL JSON is supported
- `offset` (int, default=0), `limit` (int, default=10, 1–100): Pagination

**Returns**: When a capability matches, a dictionary with:
- `capability`: The capability as OSCAL JSON
- `component_count`: Number of entries in the capability's `incorporates-components` list (0 if absent)
- `offset`, `limit`, `total`, `hasMore`: Pagination metadata (always 0, 1, 1, false)
- `query_type`, `component_definitions_searched`, `filtered_by`

Otherwise, a dictionary with:
- `components`: Page of matching components as OSCAL JSON
- `total_count`: Total number of matching components across all pages
- `offset`, `limit`, `hasMore`: Pagination metadata
- `query_type`, `component_definitions_searched`, `filtered_by`

Capabilities and components are returned as the element's stored OSCAL JSON: hyphenated OSCAL property names (for example `incorporates-components`, `control-implementations`) with unset fields omitted rather than set to `null`. The output can be embedded in a component definition and validated as-is.

**Features**:
- Loads Component Definitions from local directory (including zip files)
- Supports remote URI loading when `OSCAL_ALLOW_REMOTE_URIS=true`
- Looks up capabilities and components through the OSCAL Store index by UUID, title, and type
- Can search by component properties

---

### 5. List Component Definitions
**Tool**: `list_component_definitions`

Returns a page of loaded Component Definitions. Use it to pick a `component_definition_filter` for `query_component_definition`.

**Parameters**: `offset` (int, default=0), `limit` (int, default=10, 1–100)

**Returns**: The standard paginated response. Each item contains:
- `uuid`: Component Definition UUID
- `title`: Component Definition title
- `componentCount`: Number of components defined
- `importedComponentDefinitionsCount`: Number of imported Component Definitions
- `sizeInBytes`: Size of the source document

---

### 6. List Components
**Tool**: `list_components`

Returns a page of loaded components across all Component Definitions. Use it to pick a `query_value` for `query_component_definition`.

**Parameters**: `offset` (int, default=0), `limit` (int, default=10, 1–100)

**Returns**: The standard paginated response. Each item contains:
- `uuid`: Component UUID
- `title`: Component title
- `parentComponentDefinitionTitle`: Title of parent Component Definition
- `parentComponentDefinitionUuid`: UUID of parent Component Definition
- `sizeInBytes`: Size of the component's OSCAL JSON

---

### 7. Query OSCAL Documentation
**Tool**: `query_oscal_documentation`

Searches OSCAL documentation for questions about concepts, best practices, and implementation guidance that the model schemas do not answer. When a Knowledge Base ID is configured, it queries Amazon Bedrock Knowledge Base; otherwise (or if the Bedrock query fails) it runs a full-text search over the bundled OSCAL documentation.

**Parameters**:
- `query` (str, required): Question or search terms about OSCAL

**Returns**: Bedrock retrieve results, or the matching passages from the local search

**Bedrock configuration** (optional):
- `OSCAL_KB_ID` environment variable
- AWS credentials configured (via profile or environment)
- Optional: Set `OSCAL_AWS_PROFILE` to use specific AWS profile

---

### 8. Validate OSCAL Content
**Tool**: `validate_oscal_content`

Validates OSCAL JSON content through a multi-level pipeline:

| Level | What it checks | Implementation |
|-------|---------------|----------------|
| 1. Well-formedness | Valid JSON, is a dict | `json.loads()` |
| 2. JSON Schema | Conforms to NIST OSCAL schema | `jsonschema.Draft7Validator` with bundled schemas |
| 3. Model | Semantic checks via OSCAL Pydantic models | `model_validate()` on the class from `MODEL_MAP` (`oscal_bindings.models`) |
| 4. oscal-cli | Full NIST validation | `subprocess.run()` if on PATH |

Each entry in `levels` carries the level name: `well_formedness`, `json_schema`, `model`, or `oscal_cli`.

**Parameters**:
- `content` (str, required): OSCAL JSON document as a string
- `model_type` (str, optional): OSCAL model name (e.g. "catalog", "profile"). Auto-detected from the root key if omitted.

**Returns**: Dictionary with:
- `valid`: Overall validity (true only if all non-skipped levels pass)
- `model_type`: Detected or provided model type
- `levels`: Per-level results with `valid`, `errors`, `warnings`, `skipped`, and `skip_reason`

**Key behaviors**:
- If Level 1 fails, Levels 2-4 are skipped
- If `oscal-cli` is not installed, Level 4 is gracefully skipped
- The `model` level runs for all 8 OSCAL model types, including `mapping-collection`
- If the model class cannot be loaded, the `model` level reports an error starting with `Failed to load OSCAL model class`
- Errors capped at 20 per level

---

### 9. Validate OSCAL File
**Tool**: `validate_oscal_file`

Validates an OSCAL JSON file through the same multi-level pipeline as `validate_oscal_content`.

**Parameters**:
- `file_uri` (str, required): Path or URI of an OSCAL JSON file. Remote URIs are rejected unless the server operator enables remote URIs.
- `model_type` (str, optional): OSCAL model name (e.g. "catalog", "profile"). Auto-detected from the root key if omitted.

**Returns**: Same structured validation results as `validate_oscal_content`

**Key behaviors**:
- Supports local file paths and `file://` URIs
- Remote URIs (http/https) are blocked by default; the operator enables them with `OSCAL_ALLOW_REMOTE_URIS=true`
- Remote requests respect `OSCAL_REQUEST_TIMEOUT` (default 30s)
- Delegates to `validate_oscal_content` after reading the file

---

### 10. List Capabilities
**Tool**: `list_capabilities`

Returns a page of capabilities across all loaded Component Definitions. Capabilities are optional in OSCAL, so the list is empty when no loaded Component Definition contains any.

**Parameters**: `offset` (int, default=0), `limit` (int, default=10, 1–100)

**Returns**: The standard paginated response. Each item contains:
- `uuid`: Capability UUID
- `name`: Capability name
- `parentComponentDefinitionTitle`: Title of parent Component Definition
- `parentComponentDefinitionUuid`: UUID of parent Component Definition
- `sizeInBytes`: Size of the capability's OSCAL JSON

---

### 11. Get Capability
**Tool**: `get_capability`

Retrieves a single capability by UUID with its full OSCAL representation. Use it only for Component Definitions that contain capabilities.

**Parameters**:
- `uuid` (str, required): UUID of the capability, from `list_capabilities`

**Returns**: The capability's stored OSCAL JSON (hyphenated OSCAL property names, unset fields omitted), or null if not found

---

### 12. About
**Tool**: `about`

Returns metadata about the MCP server itself.

**Parameters**: None

**Returns**: Dictionary containing:
- `version`: Server version
- `keywords`: Server keywords
- `oscal-version`: Supported OSCAL version (currently 1.2.1)
- `user-documents-loaded`: Number of documents in the store from `OSCAL_DOCUMENTS_DIR`, counting files unchanged since an earlier scan (0 when unset or the directory is missing)

---

## Query & List Tools (OSCAL Store)

The OSCAL Store provides a SQLite-backed index of all loaded OSCAL documents and their child elements. Every OSCAL model type has a pair of tools:

- **`query_<model>`**: retrieve document metadata and per-type child element counts, filtered by UUID or title
- **`list_<model>`**: return summary metadata (UUID, title, model type, child count, size)

Child element tools (`list_<model>_<element>`) list elements within a specific parent document type. Two cross-cutting tools, `text_search_oscal` and `get_child_element`, operate across all model types. To read a document's child elements, use the matching child list tool, then `get_child_element`.

### Common Parameters for `query_*` Tools

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query_type` | `"all"` \| `"by_uuid"` \| `"by_title"` | `"all"` | `by_title` matches the exact title, then falls back to full-text search. Other values (including `"by_type"`) are rejected |
| `query_value` | str | `None` | UUID or title to match (required for `by_uuid` and `by_title`) |
| `offset` | int | `0` | Zero-based pagination offset |
| `limit` | int | `10` | Maximum items to return, 1–100 |

Each `query_*` item contains `uuid`, `title`, `model_type`, `file_path`, `sizeInBytes`, and `childTypeCounts`: a map from child element type to its count in that document (for example `{"control": 1007, "group": 20}`). Types with no elements are omitted, so a document without child elements has `{}`. Child elements are not returned inline.

### Common Parameters for `list_*` Tools

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `offset` | int | `0` | Zero-based pagination offset |
| `limit` | int | `10` | Maximum items to return, 1–100 |

### Common Return Format

All `query_*` and `list_*` tools (and `list_oscal_resources`) return a paginated response dict:

| Key | Type | Description |
|-----|------|-------------|
| `items` | list | Page of result objects |
| `total` | int | Total number of matching items |
| `offset` | int | Current pagination offset |
| `limit` | int | Requested page size |
| `hasMore` | bool | Whether additional pages exist beyond the current page |

### Child Element List Tools

Tools like `list_catalog_controls`, `list_ssp_system_components`, `list_poam_items`, etc. list child elements within a specific parent document type. In addition to `offset` and `limit`, they accept:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `parent_doc_uuid` | str | `None` | UUID of the parent document to scope the listing. When omitted, child elements across all documents of that type are returned. |

Each returned item contains: `id`, `title`, `element_type`, `description`, `parentDocumentTitle`, `parentDocumentUuid`.

### `text_search_oscal`

Full-text search across all loaded OSCAL documents and child elements using SQLite FTS5. Results are ranked by relevance.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query_text` | str | _(required)_ | Full-text search terms |
| `oscal_model_type` | str | `None` | Filter results to a specific OSCAL model type (e.g. `"catalog"`, `"system-security-plan"`). When omitted, all model types are searched. |
| `offset` | int | `0` | Zero-based pagination offset |
| `limit` | int | `10` | Maximum items to return, 1–100 |

Returns the standard paginated response. Each item contains: `entity_type`, `entity_id`, `title`, `description`, `model_type`.

### `get_child_element`

Retrieve a single child element by its identifier (UUID or token ID), with optional parent document scoping.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `element_id` | str | _(required)_ | UUID or token ID of the child element (e.g. `"ac-1"` for a catalog control, or a UUID for tasks, findings, etc.). Token IDs are unique only within one document. |
| `parent_doc_uuid` | str | `None` | UUID of the parent document to narrow the search. Recommended for token-based IDs that may not be globally unique. |

Returns a dict with keys: `id`, `title`, `element_type`, `description`, `parentDocumentTitle`, `parentDocumentUuid`, `raw_json`, or `None` if not found. If `element_id` is ambiguous across documents and `parent_doc_uuid` is omitted, returns an error dict with `error: "ambiguous_element_id"` and a list of matching parent document UUIDs.

---

## Implementation Details

### Tool Registration
Tools are registered in `main.py` using the MCP Python SDK's `MCPServer` (formerly `FastMCP`). Tool functions are collected by `get_tool_list()` in `tools/__init__.py`, which gathers the original tools, query/list tools from `query_oscal_models.py`, and `query_oscal_documentation` (always included). The `about` tool is registered separately in `main.py` as an MCP-server-only tool, with its description from `descriptions.ABOUT_DESCRIPTION`. The server `instructions` (`descriptions.SERVER_INSTRUCTIONS`) end with the Routing_Guide.

Every tool that takes the MCP context declares `ctx: Context | None = None` as its last parameter. The MCP SDK hides it from the `inputSchema` by type, and `oscal_agent.agent_tools()` gives the Strands Agent copies of the tools with `ctx` removed from their specs, so both expose the same parameters.

### Dependencies
- **strands**: Provides the `@tool` decorator for tool definitions
- **MCPServer** (MCP Python SDK v2): MCP server framework
- **oscal-bindings**: OSCAL Pydantic models (`oscal_bindings.models`), used for model validation and document parsing
- **OscalStore**: SQLite-backed content indexing and full-text search (`oscal_store.py`)
- **boto3**: AWS SDK (for documentation queries)
- **requests**: HTTP client (for remote Component Definition loading)
- **jsonschema**: JSON Schema validation (transitive dependency)

### Utilities
The `utils.py` module provides shared functionality:
- `OSCALModelType`: Enum of OSCAL model types
- `MODEL_MAP`: Mapping of each `OSCALModelType` to its `oscal_bindings.models` class. This is the single source of OSCAL model classes; the validator (`validate_oscal_content.py`) and the store (`oscal_store.py`) both read it
- `schema_names`: Mapping of model names to schema file names
- `ROOT_KEY_TO_MODEL_TYPE`: Reverse mapping from JSON root keys to model types
- `load_oscal_json_schema()`: Load bundled OSCAL JSON schemas
- `paginate()`: Build the standard paginated response from a list
- `try_notify_client_error()`: Helper for error notifications
- `verify_package_integrity()`: Package integrity verification

The `descriptions.py` module holds the shared tool text: `ParamDoc`, the reusable `Annotated` parameter aliases (`Offset`, `Limit`, `ParentDocUuid`, `TargetSelector`, and others), `ABOUT_DESCRIPTION`, and the Routing_Guide. The `tool_budget.py` module holds the description size limits and the pure checks the budget tests run.

The `query_oscal_models.py` module provides the query/list tool implementations for all OSCAL model types, including per-model `query_*`/`list_*` pairs, child element list tools, `text_search_oscal`, and `get_child_element`.

### Configuration
Tools respect configuration from `config.py`, including:
- `component_definitions_dir`: deprecated, no effect; use `oscal_documents_dir` (`OSCAL_DOCUMENTS_DIR`)
- `allow_remote_uris`: Enable/disable remote URI loading
- `request_timeout`: Timeout for remote requests
- `knowledge_base_id`: Bedrock Knowledge Base ID
- `aws_profile`: AWS profile for Bedrock queries
- `log_level`: Logging level
- `oscal_documents_dir`: Directory containing user OSCAL JSON files
- `oscal_store_db_path`: Path to persistent SQLite database
- `oscal_store_cache_size`: Maximum parsed documents in LRU cache
