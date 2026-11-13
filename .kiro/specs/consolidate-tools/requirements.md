# Requirements Document

## Introduction

GitHub issue #40 asks to consolidate the per-model query and list tools into generic tools. Today `get_tool_list()` returns 45 tools, and the `tools/list` response has 46 entries because it also lists `about`. Of the 45 tools, 26 are per-model variants: 7 `query_<model>` tools, 7 `list_<model>s` tools, and 12 child-element listers. Together with the three Component Definition listers, these 29 tools cost 19,585 characters of the 34,938-character `tools/list` response, measured at commit 599a41d (Python 3.14.8, `mcp` 2.1.1). Issue #40 estimated this cost at about 29,000 characters; the measured figure is lower than the issue's estimate. The issue also reports that 22 of the per-model variants return nothing with the bundled store.

This feature replaces the 26 per-model variants and the three Component Definition listers with three generic tools that take the model type or child element type as a parameter: `list_documents`, `query_documents`, and `list_child_elements`. After the change the Tool_Registry holds 19 tools, and the `tools/list` response holds 20 entries with `about`. `get_child_element`, `query_component_definition`, `get_capability`, and `text_search_oscal` stay. The change is a hard break in version 0.8.0 (the latest tag is v0.7.1): the old tool names are removed with no aliases and no legacy flag. A release note with an old-to-new mapping table documents the break.

Decisions recorded for issue #40 (approach):

- Generic tools only. No conditional registration of per-model tools.
- Hard break in 0.8.0. No aliases, no compatibility flag.
- Component Definitions are in scope: `model_type` covers all 8 OSCAL document models, and `list_component_definitions`, `list_components`, and `list_capabilities` are removed.
- `list_child_elements` takes `element_type` only. Child element type names are unique across models, so the model type is implied.

Project constraints that apply to every task in this spec: run all Python through `hatch`, never prefix commands with `cd`, follow `.kiro/steering/tool-authoring.md` and `.kiro/steering/oscal-bindings.md`, and do not commit or push without approval (the current branch is `main`).

## Glossary

- **MCP_Server**: The FastMCP server built by `build_server()` in `src/mcp_server_for_oscal/main.py`, which registers every tool from the Tool_Registry plus `about`.
- **Agent**: The Strands agent in `src/mcp_server_for_oscal/oscal_agent.py`, which receives tool copies from `agent_tools()`.
- **Tool_Registry**: The function `get_tool_list()` in `src/mcp_server_for_oscal/tools/__init__.py`, the single list of tools that the MCP_Server, the Agent, and the MCPB manifest read.
- **OscalStore**: The SQLite-backed store in `tools/oscal_store.py`, exposing `list_documents`, `query`, `list_child_elements`, and `get_child_element`.
- **Model_Type**: One of the 8 OSCAL document model values of `OSCALModelType`: `catalog`, `profile`, `component-definition`, `system-security-plan`, `assessment-plan`, `assessment-results`, `plan-of-action-and-milestones`, `mapping-collection` (the value of `OSCALModelType.MAPPING`).
- **Element_Type**: One of the 14 child element type names in the values of `CHILD_ELEMENT_TYPES`: `component`, `capability`, `control`, `group`, `import`, `modify`, `control-implementation`, `system-component`, `task`, `activity`, `result`, `finding`, `poam-item`, `mapping`.
- **Query_Type**: One of the values of `VALID_QUERY_TYPES`: `all`, `by_uuid`, `by_title`.
- **List_Documents_Tool**: The new tool `list_documents(model_type, offset, limit)`.
- **Query_Documents_Tool**: The new tool `query_documents(model_type, query_type, query_value, offset, limit)`.
- **List_Child_Elements_Tool**: The new tool `list_child_elements(element_type, parent_doc_uuid, offset, limit)`.
- **Generic_Tools**: The List_Documents_Tool, the Query_Documents_Tool, and the List_Child_Elements_Tool.
- **Removed_Per_Model_Tools**: The 26 tools `query_catalog`, `query_profile`, `query_ssp`, `query_assessment_plan`, `query_assessment_results`, `query_poam`, `query_mapping_collection`, `list_catalogs`, `list_profiles`, `list_ssps`, `list_assessment_plans`, `list_assessment_results`, `list_poams`, `list_mapping_collections`, `list_catalog_controls`, `list_catalog_groups`, `list_profile_imports`, `list_profile_modify`, `list_ssp_control_implementations`, `list_ssp_system_components`, `list_assessment_plan_tasks`, `list_assessment_plan_activities`, `list_assessment_results_results`, `list_assessment_results_findings`, `list_poam_items`, `list_mapping_collection_mappings`.
- **Removed_CDef_Listers**: The 3 tools `list_component_definitions`, `list_components`, `list_capabilities`.
- **Removed_Tools**: The Removed_Per_Model_Tools and the Removed_CDef_Listers (29 tools).
- **Retained_Tools**: Every tool in the Tool_Registry that is not a Removed_Tool, including `get_child_element`, `query_component_definition`, `get_capability`, and `text_search_oscal`.
- **Valid_Page**: An `offset` that is an integer of 0 or more and a `limit` that is an integer from 1 to 100 inclusive, as checked by `utils.paginate`.
- **Page_Envelope**: A result dict with exactly the top-level keys `items`, `total`, `offset`, `limit`, and `hasMore`, where `hasMore` is true exactly when `offset + limit < total`.
- **Document_Summary**: An item of the List_Documents_Tool result, with keys `uuid`, `title`, `model_type`, `childCount`, and `sizeInBytes`.
- **Query_Item**: An item of the Query_Documents_Tool result, with keys `uuid`, `title`, `model_type`, `sizeInBytes`, and `childTypeCounts`.
- **Child_Summary**: An item of the List_Child_Elements_Tool result, with keys `id`, `title`, `element_type`, `description`, `parentDocumentTitle`, `parentDocumentUuid`, and `sizeInBytes`.
- **Client_Error_Notification**: A call to `try_notify_client_error(msg, ctx)` before the tool raises or returns an error.
- **Description_Shape**: The tool description rules in `.kiro/steering/tool-authoring.md`, checked by `description_shape_problems()` and `unknown_tool_refs()` in `tools/tool_budget.py`.
- **Budget_Module**: `src/mcp_server_for_oscal/tools/tool_budget.py`, which holds `BASELINE_SIZE`, `BEFORE_CONSOLIDATION_SIZE`, `BEFORE_CONSOLIDATION_TOOLS`, `AFTER_CONSOLIDATION_SIZE`, `AFTER_CONSOLIDATION_TOOLS`, `TOTAL_BUDGET`, the caps, and `tools_list_size()`.
- **Budget_Tests**: `tests/test_tool_budget.py`, which check the live `tools/list` response against the Budget_Module.
- **Tools_List_Size**: The value of `tools_list_size()` for the `tools` array of the MCP_Server `tools/list` response.
- **Before_Size**: The Tools_List_Size measured on the code before this feature: 34,938 characters in 46 entries (45 tools plus `about`), measured at HEAD 599a41d with Python 3.14.8 and `mcp` 2.1.1. The Budget_Module records it as `BEFORE_CONSOLIDATION_SIZE`. The constant `BASELINE_SIZE = 55_685` is a stale figure from before the 0.7.0 tool-context optimization and is not the Before_Size.
- **After_Size**: The Tools_List_Size measured after this feature, in 20 entries (19 tools plus `about`), with the same Python and `mcp` versions used for the Before_Size. The Budget_Module records it as `AFTER_CONSOLIDATION_SIZE`.
- **Routing_Guide**: The `ROUTING_GUIDE` string in `tools/descriptions.py`, sent in the server instructions.
- **Release_Note**: The 0.8.0 release note document stored in the repository.
- **Mapping_Table**: A table in the Release_Note with one row per Removed_Tool that gives the old tool name, the replacement tool, the replacement arguments, and any result key changes.
- **Sync_Targets**: The files that list or describe tools and must match the Tool_Registry: `bin/build_mcpb.py` with the `conf/mcpb/manifest.json` template, `src/mcp_server_for_oscal/tools/README.md`, `conf/powers/oscal/POWER.md`, `README.md`, `AGENTS.md`, `.kiro/steering/product.md`, `.kiro/steering/structure.md`, `.agents/summary/components.md`, `.agents/summary/interfaces.md`, `bin/eval_tool_selection_prompts.json`, `tests/test_tool_registry.py`, `tests/tools/test_query_oscal_models_child_elements.py`, and `tests/test_tool_param_parity.py`.
- **Test_Suite**: The command `hatch run tests` (mypy, pytest with coverage on Python 3.13 and 3.14, and bandit).

## Requirements

### Requirement 1: List documents of one model type

**User Story:** As an AI assistant, I want one tool that lists loaded OSCAL documents of any model type, so that I do not need a separate list tool per model.

#### Acceptance Criteria

1. THE List_Documents_Tool SHALL accept a required `model_type` parameter whose input schema is an `enum` of exactly the 8 Model_Type values.
2. THE List_Documents_Tool SHALL accept an `offset` parameter with default 0 that uses the shared `Offset` alias and a `limit` parameter with default 10 that uses the shared `Limit` alias.
3. WHEN the List_Documents_Tool is called with a Model_Type and a Valid_Page, THE List_Documents_Tool SHALL return a Page_Envelope of Document_Summary items for the documents of that Model_Type, ordered by title (case-insensitive) and then by UUID.
4. WHEN the List_Documents_Tool is called with a Model_Type and a Valid_Page, THE List_Documents_Tool SHALL return a result equal to the result of `OscalStore.list_documents` for the same Model_Type, offset, and limit.
5. WHEN no documents of the requested Model_Type are loaded, THE List_Documents_Tool SHALL return a Page_Envelope with an empty `items` list and `total` equal to 0.

### Requirement 2: Query documents of one model type

**User Story:** As an AI assistant, I want one tool that finds loaded OSCAL documents of any model type by UUID or title, so that I do not need a separate query tool per model.

#### Acceptance Criteria

1. THE Query_Documents_Tool SHALL accept a required `model_type` parameter whose input schema is an `enum` of exactly the 8 Model_Type values.
2. THE Query_Documents_Tool SHALL accept a `query_type` parameter with default `all` that uses the shared `PerModelQueryType` alias (or a renamed alias with the same `enum` of Query_Type values) and a `query_value` parameter with default null that uses the shared `QueryValue` alias.
3. THE Query_Documents_Tool SHALL accept an `offset` parameter with default 0 that uses the shared `Offset` alias and a `limit` parameter with default 10 that uses the shared `Limit` alias.
4. WHEN the Query_Documents_Tool is called with a Model_Type, a Query_Type, a `query_value` that the Query_Type allows, and a Valid_Page, THE Query_Documents_Tool SHALL return a result equal to the result of `OscalStore.query` for the same arguments.
5. WHEN the Query_Documents_Tool succeeds, THE Query_Documents_Tool SHALL return a Page_Envelope whose items are Query_Items of the requested Model_Type only.
6. IF the Query_Documents_Tool is called with `query_type` `by_uuid` or `by_title` and an empty or missing `query_value`, THEN THE Query_Documents_Tool SHALL send a Client_Error_Notification and raise a `ValueError` whose message names the Query_Type.

### Requirement 3: List child elements of one element type

**User Story:** As an AI assistant, I want one tool that lists child elements (controls, components, findings, and so on) of any type, so that I do not need a separate lister per child element type.

#### Acceptance Criteria

1. THE List_Child_Elements_Tool SHALL accept a required `element_type` parameter whose input schema is an `enum` of exactly the 14 Element_Type values.
2. THE List_Child_Elements_Tool SHALL accept an optional `parent_doc_uuid` parameter with default null that uses the shared `ParentDocUuid` alias.
3. THE List_Child_Elements_Tool SHALL accept an `offset` parameter with default 0 that uses the shared `Offset` alias and a `limit` parameter with default 10 that uses the shared `Limit` alias.
4. WHEN the List_Child_Elements_Tool is called with an Element_Type and a Valid_Page, THE List_Child_Elements_Tool SHALL return a Page_Envelope of Child_Summary items of that Element_Type across all loaded documents, ordered by title (case-insensitive) and then by identifier.
5. WHEN the List_Child_Elements_Tool is called with a `parent_doc_uuid`, THE List_Child_Elements_Tool SHALL return only Child_Summary items whose `parentDocumentUuid` equals that `parent_doc_uuid`.
6. WHEN the List_Child_Elements_Tool succeeds, THE List_Child_Elements_Tool SHALL return, for each Child_Summary, the same `id`, `title`, `element_type`, `description`, `parentDocumentTitle`, and `parentDocumentUuid` values that `OscalStore.list_child_elements` returns for the same arguments.
7. WHEN the List_Child_Elements_Tool succeeds, THE List_Child_Elements_Tool SHALL set `sizeInBytes` of each Child_Summary to the UTF-8 byte length of the child's stored OSCAL JSON, or to 0 when the store holds no serialized JSON for that child.
8. WHEN the requested `parent_doc_uuid` matches no loaded document, or the matched document contains no child of the requested Element_Type, THE List_Child_Elements_Tool SHALL return a Page_Envelope with an empty `items` list and `total` equal to 0.
9. THE List_Child_Elements_Tool SHALL return Child_Summary items without the child's full OSCAL JSON, so that `get_child_element` stays the way to fetch one child's OSCAL JSON.

### Requirement 4: Shared validation, errors, and output rules for the Generic_Tools

**User Story:** As an AI assistant, I want the Generic_Tools to reject bad arguments with clear messages and return valid JSON, so that I can correct my call without guessing.

#### Acceptance Criteria

1. IF a Generic_Tool is called with an `offset` or `limit` that is not a Valid_Page, THEN THE Generic_Tool SHALL send a Client_Error_Notification and raise the `ValueError` that `utils.paginate` raises for those values, before the Generic_Tool reads the OscalStore.
2. IF the List_Documents_Tool or the Query_Documents_Tool is called directly from Python with a `model_type` that is not a Model_Type, THEN THE Generic_Tool SHALL send a Client_Error_Notification and raise a `ValueError` whose message lists the valid Model_Type values.
3. IF the List_Child_Elements_Tool is called directly from Python with an `element_type` that is not an Element_Type, THEN THE List_Child_Elements_Tool SHALL send a Client_Error_Notification and raise a `ValueError` whose message lists the valid Element_Type values.
4. IF a Generic_Tool is called before the OscalStore singleton of its module is initialized, THEN THE Generic_Tool SHALL raise a `RuntimeError` that names `init_store()`.
5. THE Generic_Tools SHALL return plain JSON-serializable dicts that contain no Pydantic model instances.
6. THE Generic_Tools SHALL return results and error messages that contain no filesystem path or storage location, as checked by `tests/test_path_leak.py`.
7. THE Generic_Tools SHALL declare `ctx: Context | None = None` as the last parameter.

### Requirement 5: Enum values match the store

**User Story:** As a maintainer, I want the `model_type` and `element_type` enums to stay in step with the store, so that adding a model or child type cannot silently leave a gap.

#### Acceptance Criteria

1. THE Test_Suite SHALL include a test that fails when the `model_type` enum of the List_Documents_Tool or the Query_Documents_Tool differs from the set of `OSCALModelType` keys of `CHILD_ELEMENT_TYPES`.
2. THE Test_Suite SHALL include a test that fails when the `element_type` enum of the List_Child_Elements_Tool differs from the union of the values of `CHILD_ELEMENT_TYPES`.
3. THE Test_Suite SHALL include a test that fails when an Element_Type name occurs under more than one Model_Type in `CHILD_ELEMENT_TYPES`.
4. THE Test_Suite SHALL include a test that fails when the `query_type` enum of the Query_Documents_Tool differs from `VALID_QUERY_TYPES`.

### Requirement 6: Equivalent functionality for the Removed_Per_Model_Tools

**User Story:** As an existing user of the per-model tools, I want every removed tool to have a documented replacement call that returns the same data, so that I lose no capability in 0.8.0.

#### Acceptance Criteria

1. FOR each removed `list_<model>s` tool and its Model_Type, THE List_Documents_Tool SHALL return, for the same `offset` and `limit`, a result equal to the result the removed tool returned (a Page_Envelope from `OscalStore.list_documents` for that Model_Type).
2. FOR each removed `query_<model>` tool and its Model_Type, THE Query_Documents_Tool SHALL return, for the same `query_type`, `query_value`, `offset`, and `limit`, a result equal to the result the removed tool returned (the result of `OscalStore.query` for that Model_Type).
3. FOR each removed child-element lister and its Element_Type, THE List_Child_Elements_Tool SHALL return, for the same `parent_doc_uuid`, `offset`, and `limit`, a Page_Envelope whose `total`, `offset`, `limit`, and `hasMore` equal the values the removed lister returned and whose items contain every key and value the removed lister returned.
4. THE Test_Suite SHALL cover criteria 6.1, 6.2, and 6.3 for every Removed_Per_Model_Tool with at least one fixture document of the matching Model_Type or Element_Type, and with property-based tests over `offset`, `limit`, and `parent_doc_uuid`.

### Requirement 7: Equivalent functionality for the Removed_CDef_Listers

**User Story:** As an existing user of the Component Definition listers, I want the Generic_Tools to give me the same identifiers, names, parents, counts, and sizes, so that I can still pick a `component_definition_filter` or `query_value` for `query_component_definition`.

#### Acceptance Criteria

1. WHEN the List_Documents_Tool is called with `model_type` `component-definition`, THE List_Documents_Tool SHALL return, for each Component Definition that `list_component_definitions` returned, a Document_Summary whose `uuid`, `title`, and `sizeInBytes` equal the removed tool's `uuid`, `title`, and `sizeInBytes` and whose `childCount` equals the removed tool's `componentCount`, a value that counts the Component Definition's Components plus its Capabilities.
2. WHEN the List_Child_Elements_Tool is called with `element_type` `component`, THE List_Child_Elements_Tool SHALL return, for each Component that `list_components` returned, a Child_Summary whose `id`, `title`, `parentDocumentTitle`, `parentDocumentUuid`, and `sizeInBytes` equal the removed tool's `uuid`, `title`, `parentComponentDefinitionTitle`, `parentComponentDefinitionUuid`, and `sizeInBytes`.
3. WHEN the List_Child_Elements_Tool is called with `element_type` `capability`, THE List_Child_Elements_Tool SHALL return, for each Capability that `list_capabilities` returned, a Child_Summary whose `id`, `title`, `parentDocumentTitle`, `parentDocumentUuid`, and `sizeInBytes` equal the removed tool's `uuid`, `name`, `parentComponentDefinitionTitle`, `parentComponentDefinitionUuid`, and `sizeInBytes`.
4. THE List_Documents_Tool and the List_Child_Elements_Tool SHALL return Component Definition, Component, and Capability items in the same order and with the same `total` and `hasMore` values as the Removed_CDef_Listers for the same `offset` and `limit`.
5. WHEN no Component Definitions or no Components are loaded, THE Generic_Tools SHALL return an empty Page_Envelope in the cases where `list_component_definitions` and `list_components` raised `RuntimeError`.
6. THE Release_Note SHALL state the behavior change of criterion 7.5.
7. THE Release_Note SHALL state that the removed `list_component_definitions` set `importedComponentDefinitionsCount` to the literal 0 for every item regardless of the document's `import-component-definitions`, so the key carried no information, and that the key has no replacement.
8. THE Test_Suite SHALL cover criteria 7.1 through 7.5 against the Component Definition fixtures used by `tests/tools/test_query_component_definition.py`.

### Requirement 8: Removal of the old tool names

**User Story:** As a maintainer, I want the Removed_Tools gone from every registry and client surface, so that the `tools/list` savings are real and no client sees two ways to do one thing.

#### Acceptance Criteria

1. THE Tool_Registry SHALL contain the Generic_Tools and the Retained_Tools and SHALL contain none of the Removed_Tools.
2. THE MCP_Server `tools/list` response SHALL contain none of the Removed_Tool names.
3. THE Agent tool list from `agent_tools()` SHALL contain none of the Removed_Tool names.
4. THE package modules SHALL define no public function, alias, or compatibility flag under a Removed_Tool name.
5. THE tool descriptions, parameter descriptions, server instructions, and client-facing messages SHALL contain none of the Removed_Tool names.
6. THE Test_Suite SHALL include a test that fails when any Removed_Tool name appears in the MCP_Server `tools/list` response or in the server instructions.

### Requirement 9: Retained tools keep their behavior

**User Story:** As an existing user, I want the tools that stay to behave as before, so that only the documented break affects me.

#### Acceptance Criteria

1. THE `get_child_element` tool SHALL keep its name, parameters, parameter descriptions, and results unchanged.
2. THE `query_component_definition` tool SHALL keep its name, parameters, and results unchanged, and its description SHALL name the Generic_Tools instead of the Removed_CDef_Listers.
3. THE `get_capability` tool SHALL keep its name, parameters, and results unchanged, and its `uuid` parameter description SHALL name the List_Child_Elements_Tool instead of `list_capabilities`.
4. WHEN `query_component_definition` is called with a `component_definition_filter` that matches no Component Definition, THE `query_component_definition` tool SHALL send an info message that names the List_Documents_Tool instead of `list_component_definitions`.
5. THE `text_search_oscal` tool SHALL keep its name, parameters, and results unchanged.

### Requirement 10: Description shape and size caps

**User Story:** As a maintainer, I want the Generic_Tools to follow the tool authoring rules, so that their descriptions stay short and accurate for an LLM caller.

#### Acceptance Criteria

1. THE Generic_Tools SHALL each have a description that passes `description_shape_problems()` with no problems: at most 3 sentences, a summary sentence that starts with a verb in `SUMMARY_VERBS`, no docstring sections, and no mention of `ctx`.
2. THE Generic_Tools SHALL each have a description of at most 180 characters, the size-plan target for per-model `query_*`, document `list_*`, and child `list_*` tools.
3. THE Generic_Tools SHALL describe every parameter with `Annotated[T, ParamDoc(...)]`, with each parameter description of at most `PARAMETER_DESCRIPTION_CAP` (250) characters.
4. WHERE a parameter occurs in more than one tool, THE Generic_Tools SHALL use a shared alias from `tools/descriptions.py` for that parameter, including new plain-assignment aliases for the Model_Type enum and the Element_Type enum.
5. THE tool descriptions and server instructions SHALL reference only tool names present in the Tool_Registry, as checked by `unknown_tool_refs()`.

### Requirement 11: Routing_Guide rewrite

**User Story:** As an AI assistant, I want the server instructions to route me to the Generic_Tools, so that I pick the right tool for loaded documents and child elements.

#### Acceptance Criteria

1. THE Routing_Guide SHALL route listing loaded OSCAL documents to the List_Documents_Tool, finding one document to the Query_Documents_Tool, and listing child elements to the List_Child_Elements_Tool followed by `get_child_element`.
2. THE Routing_Guide SHALL route document hits of `text_search_oscal` to the Query_Documents_Tool with `query_type` `by_uuid`.
3. THE Routing_Guide SHALL route Component Definition work to `query_component_definition`, with the List_Documents_Tool (`component-definition`) and the List_Child_Elements_Tool (`component`, `capability`) for discovery, and `get_capability` for one Capability.
4. THE server instructions (intro plus Routing_Guide) SHALL be at most `ROUTING_GUIDE_CAP` (3,000) characters.

### Requirement 12: tools/list measurement and budget update

**User Story:** As a maintainer, I want the `tools/list` size measured before and after the change and the budget tightened, so that the savings are recorded and kept.

#### Acceptance Criteria

1. THE Release_Note SHALL record the Before_Size (34,938), the After_Size, the `tools/list` entry count before and after (46 and 20), the Tool_Registry tool count before and after (45 and 19), and the Python and `mcp` versions used for both measurements (Python 3.14.8 and `mcp` 2.1.1 for the Before_Size).
2. THE Budget_Module SHALL define `BEFORE_CONSOLIDATION_SIZE` (34,938) and `BEFORE_CONSOLIDATION_TOOLS` (46) for the Before_Size, and `AFTER_CONSOLIDATION_SIZE` and `AFTER_CONSOLIDATION_TOOLS` (20) for the After_Size, each pair with a comment that gives the commit, the Python version, and the `mcp` version of the measurement, and SHALL keep `BASELINE_SIZE` unchanged as a historical constant.
3. THE Budget_Module SHALL set `TOTAL_BUDGET` to a value no less than `AFTER_CONSOLIDATION_SIZE` and no more than 110% of `AFTER_CONSOLIDATION_SIZE`.
4. WHEN the Budget_Module limits change, THE Limits table in `.kiro/steering/tool-authoring.md` SHALL show the same values in the same change.
5. WHEN the Budget_Tests run, THE Budget_Tests SHALL pass against the live `tools/list` response with the updated limits.

### Requirement 13: Server and Agent parameter parity

**User Story:** As a maintainer, I want the MCP_Server and the Agent to expose the same parameters for the Generic_Tools, so that both callers see one contract.

#### Acceptance Criteria

1. THE MCP_Server and the Agent SHALL expose the same tool names for the Generic_Tools.
2. THE MCP_Server and the Agent SHALL expose, for each Generic_Tool, the same parameter names, the same required parameters, the same `enum` values, and the same parameter descriptions, as checked by `tests/test_tool_param_parity.py`.
3. THE Agent tool specs SHALL omit the `ctx` parameter of the Generic_Tools.

### Requirement 14: Sync targets and documentation

**User Story:** As a user or contributor, I want every tool listing in the repository to match the Tool_Registry, so that docs, bundles, and tests describe the tools that exist.

#### Acceptance Criteria

1. WHEN `bin/build_mcpb.py` builds the MCPB manifest, THE generated `conf/mcpb/manifest.json` tool list SHALL contain exactly the Tool_Registry tools with their summary sentences.
2. THE Sync_Targets SHALL list the Generic_Tools and SHALL contain none of the Removed_Tool names, except the Release_Note Mapping_Table and tests that assert a Removed_Tool name is absent.
3. THE `tests/test_tool_registry.py` and `tests/tools/test_query_oscal_models_child_elements.py` tests SHALL assert the exact tool set and tool count of the Tool_Registry after this feature.
4. THE `bin/eval_tool_selection_prompts.json` prompts SHALL expect the Generic_Tools wherever the prompts expected a Removed_Tool.
5. THE `AGENTS.md` code map SHALL describe `tools/query_oscal_models.py` and `tools/query_component_definition.py` with the tools each module defines after this feature.
6. WHEN tests that exercised a Removed_Tool are rewritten, THE Test_Suite SHALL exercise the same scenario through the replacement call given in the Mapping_Table.

### Requirement 15: 0.8.0 release note with mapping table

**User Story:** As an existing user upgrading from 0.7.x, I want a release note that maps every removed tool to its replacement, so that I can update my prompts and integrations in one pass.

#### Acceptance Criteria

1. THE Release_Note SHALL identify the release as version 0.8.0, state that the change is breaking, and reference GitHub issue #40.
2. THE Release_Note SHALL contain a Mapping_Table with exactly one row for each of the 29 Removed_Tools.
3. THE Mapping_Table SHALL give, in each row, the Removed_Tool name, the replacement Generic_Tool, and the replacement arguments (`model_type`, `element_type`, `query_type`, `parent_doc_uuid` as applicable).
4. THE Mapping_Table SHALL give the result key renames for the Removed_CDef_Listers: `componentCount` to `childCount` (the value counts Components plus Capabilities), `uuid` to `id`, `name` to `title`, `parentComponentDefinitionTitle` to `parentDocumentTitle`, and `parentComponentDefinitionUuid` to `parentDocumentUuid`.
5. THE Release_Note SHALL state that no aliases or compatibility flag exist for the Removed_Tools.
6. THE Release_Note SHALL list the behavior changes from criteria 7.5 and 7.7, the added `sizeInBytes` key of Child_Summary items, and the page validation of criterion 4.1, and SHALL state that `componentCount` (renamed `childCount`) counts Components plus Capabilities. The page validation item SHALL state that the Generic_Tools now reject an `offset` below 0 or a `limit` below 1 or above 100 with an error, whereas the removed per-model tools passed those values to SQLite unchecked (so that `limit=-1` returned every row and `limit=0` returned none).
7. THE Test_Suite SHALL include a test that fails when the Mapping_Table rows differ from the set of Removed_Tools or name a replacement tool that is not in the Tool_Registry.

### Requirement 16: Test suite passes

**User Story:** As a maintainer, I want the full test suite to pass, so that the feature meets the issue #40 acceptance criteria.

#### Acceptance Criteria

1. WHEN the feature is complete, THE Test_Suite SHALL pass on Python 3.13 and Python 3.14.
2. WHEN the feature is complete, THE changed files SHALL pass `hatch check fmt` and `hatch check code` with no errors.
3. THE Test_Suite SHALL keep line coverage of `tools/query_oscal_models.py` and `tools/query_component_definition.py` at or above the coverage measured before this feature.
