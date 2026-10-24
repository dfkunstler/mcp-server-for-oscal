# Requirements Document

## Introduction

The OSCAL MCP Server advertises 40+ tools. The `tools/list` response is 55,685 characters, and every MCP client loads it into the model context on connect. Several descriptions are also wrong or misleading: they name tools that do not exist (`list_models`, `get_schema`), recommend a capability-first workflow although the bundled content has 0 capabilities, make the huge `complete` schema the default for `get_oscal_schema`, document internal config (`config.allow_remote_uris`), and repeat the `ctx` parameter docs in every tool.

This feature (GitHub issue #39, branch `39-tighten-tool-descriptions`) makes the tool surface smaller and more accurate:

- Per-tool and per-parameter character caps, plus a total `tools/list` budget at least 30% under the baseline. Tests enforce all three against the live `tools/list` response.
- A fixed description shape, and parameter descriptions declared in the parameter annotation with `Annotated[..., ParamDoc(...)]`. `pydantic.Field` inside `Annotated` is not used, because Strands `@tool` (strands-agents 1.59.0) rejects it at import.
- Required parameters and `Literal` enums where the current signatures accept invalid input.
- Removal of the `by_type` query type from the 7 per-model `query_*` tools. Those tools return counts per child element type in place of inline child summaries.
- A server `instructions` routing guide, and paging for `list_oscal_resources`.
- Removal of `ctx` from the Agent tool specifications. Strands `@tool` (strands-agents 1.59.0 through 1.62.0) treats only its own `tool_context` parameter as special, so each Agent tool specification exposes `ctx` as a normal optional property with a `$defs.Context` block, on all tools. The Agent's model sees `ctx` and can try to pass a value. The MCP SDK (`mcp` 2.1.1) already excludes `ctx` from the `inputSchema`.
- A new always-included steering file, `.kiro/steering/tool-authoring.md`, that holds the full authoring rules. The "How a tool is wired" section of `AGENTS.md` becomes a short summary that links to it.

The budgets also act as a guardrail for future tools. The Budget_Tests read the live tool registration, so a tool added to `get_tool_list()` later is checked against the caps, the Description_Shape, and the Total_Budget with no test change, and a regression fails the test suite. The Steering_File tells contributors the rules before they write the tool.

The design adds a few items that follow from these requirements but are not separate acceptance criteria: a 3,000-character cap on the Routing_Guide (`ROUTING_GUIDE_CAP`), a required `element_id` for `get_child_element`, `ctx` as the last parameter of every tool, a `query_type` guard in `OscalStore.query` for direct Python calls, and a shared `ABOUT_DESCRIPTION` constant. See "Requirement deviations" in design.md.

Out of scope: tool renames and removals (issue #40). All tool names in `get_tool_list()` stay the same.

## Glossary

- **Server**: The MCP Server for OSCAL (`mcp_server_for_oscal.main`), including its `MCPServer` instance and the tools from `get_tool_list()` plus `about`.
- **Agent**: The Strands agent created by `oscal_agent.create_oscal_agent()`. The Agent loads the same tool functions through the Strands `@tool` decorator.
- **Agent_Tool_Spec**: The tool specification (name, description, and JSON input schema) that the Agent sends to its model for one tool loaded from `get_tool_list()`, after `create_oscal_agent()` loads the tool.
- **Tool_List_Response**: The JSON result of an MCP `tools/list` request to a running Server. This is the list of tools with `name`, `description`, and `inputSchema`.
- **Tool_Entry**: One element of the Tool_List_Response.
- **Tool_Description**: The `description` value of one Tool_Entry. The tool function docstring is the source.
- **Parameter_Description**: The `description` value of one property in the `inputSchema` of one Tool_Entry.
- **Baseline_Size**: The character count of the compact JSON serialization of the Tool_List_Response `tools` array on `main` at the start of this feature (commit `c973674`): 55,685 characters (measured). The Budget_Module constant is the source of truth.
- **Total_Budget**: The maximum allowed character count of the compact JSON serialization of the Tool_List_Response `tools` array: 38,979 characters (70% of Baseline_Size, rounded down).
- **Tool_Description_Cap**: The maximum allowed length of one Tool_Description: 1,200 characters.
- **Parameter_Description_Cap**: The maximum allowed length of one Parameter_Description: 250 characters.
- **Budget_Module**: The single source of truth for Baseline_Size, Total_Budget, Tool_Description_Cap, and Parameter_Description_Cap. The budget tests import the values from the Budget_Module.
- **Budget_Tests**: The pytest tests that start or construct the Server, get the Tool_List_Response, and check it against the caps and the Total_Budget.
- **Description_Shape**: The fixed structure of a Tool_Description: (1) one summary sentence that states what the tool returns; (2) optionally, one "Use when" sentence that names the case for this tool and the alternative tool for other cases; (3) optionally, one sentence about paging or the size of the response. A Tool_Description contains no `Args:` or `Returns:` section, because parameter descriptions are in the `inputSchema`.
- **Per_Model_Query_Tools**: The 7 tools `query_catalog`, `query_ssp`, `query_profile`, `query_assessment_plan`, `query_assessment_results`, `query_poam`, and `query_mapping_collection`.
- **Child_Type_Counts**: An object in one query result item that maps each child element type of the document (for example `control`, `group`) to the number of child elements of that type.
- **Routing_Guide**: The text of the Server `instructions` field that tells the client which tool to use for each main task.
- **Steering_File**: The file `.kiro/steering/tool-authoring.md`, with front matter `inclusion: always`.
- **Sync_Targets**: The artifacts that must agree with the tool set and the tool descriptions: the MCPB manifest built by `bin/build_mcpb.py` (`conf/mcpb/manifest.json`), `src/mcp_server_for_oscal/tools/README.md`, and `tests/test_tool_registry.py`.

## Requirements

### Requirement 1: Context budget enforced by tests

**User Story:** As an AI client operator, I want the tool list to use less context, so that more of the model context stays available for the user's OSCAL work.

#### Acceptance Criteria

1. THE Budget_Module SHALL define Baseline_Size, Total_Budget, Tool_Description_Cap, and Parameter_Description_Cap as named constants.
2. THE Budget_Module SHALL define Total_Budget as a value that is at most 70% of Baseline_Size.
3. THE Server SHALL return a Tool_List_Response whose compact JSON serialization of the `tools` array has a length less than or equal to Total_Budget.
4. THE Server SHALL return each Tool_Description with a length less than or equal to Tool_Description_Cap.
5. THE Server SHALL return each Parameter_Description with a length less than or equal to Parameter_Description_Cap.
6. WHEN the Budget_Tests run, THE Budget_Tests SHALL get the Tool_List_Response from the live Server tool registration, not from the tool docstrings directly, so that each tool in `get_tool_list()`, including a tool added after this feature, is checked without a change to the Budget_Tests.
7. IF a Tool_Description, a Parameter_Description, or the total size exceeds its limit, THEN THE Budget_Tests SHALL fail with a message that names the tool, the parameter (when applicable), the measured length, and the limit.
8. THE Budget_Tests SHALL import the limit values from the Budget_Module.
9. THE Steering_File SHALL state the same values for Total_Budget, Tool_Description_Cap, and Parameter_Description_Cap as the Budget_Module, and SHALL name the Budget_Module as the source of truth.

### Requirement 2: Description shape

**User Story:** As an LLM calling the tools, I want each tool description to have the same short structure, so that I can pick the correct tool quickly.

#### Acceptance Criteria

1. THE Server SHALL return each Tool_Description in the Description_Shape.
2. THE Server SHALL return each Tool_Description with a first sentence that states what the tool returns.
3. THE Server SHALL return each Tool_Description without an `Args:` section and without a `Returns:` section.
4. THE Server SHALL return each Tool_Description without a description of the `ctx` parameter.
5. WHEN a Tool_Description names another tool, THE Server SHALL use a tool name that is present in the Tool_List_Response.
6. IF a Tool_Description contains a backticked identifier or a `name(` call pattern in tool-name format that is not present in the Tool_List_Response, THEN THE Budget_Tests SHALL fail with a message that names the tool and the unknown name.

### Requirement 3: Parameter descriptions in the input schema

**User Story:** As an LLM calling the tools, I want each parameter to have its own description in the input schema, so that I can fill in arguments without reading a long docstring.

#### Acceptance Criteria

1. THE tool functions SHALL declare each user-facing parameter description in the parameter annotation with `Annotated[<type>, ParamDoc(...)]`, not in the docstring, where `ParamDoc` is the shared parameter-description marker in `tools/descriptions.py` that both the MCP SDK and the Strands `@tool` decorator read.
2. THE Server SHALL return a Parameter_Description for each user-facing parameter of each Tool_Entry.
3. THE Server SHALL exclude the `ctx` parameter from each `inputSchema`.
4. WHEN the Agent loads a tool function, THE Agent SHALL expose an Agent_Tool_Spec whose set of input schema property names is equal to the set of property names in the Server `inputSchema` of the same tool.
5. WHEN the Agent loads a tool function, THE Agent SHALL expose, for each property, a description in the Agent_Tool_Spec that is equal to the Parameter_Description of the same property in the Server `inputSchema`.
6. THE test suite SHALL include a test that compares the property name sets and the parameter descriptions of the Server `inputSchema` and the Agent_Tool_Spec for each tool in `get_tool_list()`, with no exception for `ctx`.

### Requirement 4: Correct tool references and guidance

**User Story:** As an LLM calling the tools, I want the descriptions to be accurate, so that I do not call tools that do not exist or follow a workflow that returns no data.

#### Acceptance Criteria

1. THE Server SHALL return Tool_Descriptions that refer to `list_oscal_models` and `get_oscal_schema` by those names, and that contain no reference to `list_models` or `get_schema` as tool names.
2. THE Server SHALL return Component Definition Tool_Descriptions that contain no recommendation of a capability-first workflow.
3. THE Server SHALL return Component Definition Tool_Descriptions that name `list_components` and `query_component_definition` as the main route, and that describe `list_capabilities` and `get_capability` as tools for Component Definitions that contain capabilities.
4. THE Server SHALL return a `validate_oscal_file` Tool_Description and Parameter_Descriptions that contain no reference to `config.allow_remote_uris`.
5. THE Server SHALL return a `validate_oscal_file` Parameter_Description for `file_uri` that states that remote URIs are rejected unless the Server operator enables remote URIs.

### Requirement 5: Safe defaults and strict parameter types

**User Story:** As an LLM calling the tools, I want the parameter types to reject invalid values and the defaults to be small, so that a call with default arguments does not overflow my context.

#### Acceptance Criteria

1. THE `get_oscal_schema` tool SHALL require the `model_name` parameter.
2. THE `get_oscal_schema` tool SHALL accept `complete` as a value of `model_name`.
3. THE `get_oscal_schema` tool SHALL declare `schema_type` as `Literal["json", "xsd"]`.
4. THE `get_definition` tool SHALL declare `kind` as `Literal["assembly", "field", "flag"] | None`.
5. THE `get_capability` tool SHALL require the `uuid` parameter.
6. THE `text_search_oscal` tool SHALL require the `query_text` parameter.
7. THE Server SHALL return an `inputSchema` that lists `model_name` for `get_oscal_schema`, `uuid` for `get_capability`, and `query_text` for `text_search_oscal` in the `required` array.
8. THE Server SHALL return an `inputSchema` with an `enum` for `get_oscal_schema.schema_type` and for `get_definition.kind`.
9. IF a caller passes an invalid value for a `Literal` parameter, THEN THE Server SHALL return a validation error that lists the valid values.

### Requirement 6: Per-model query tools

**User Story:** As an LLM calling the tools, I want the per-model query tools to return small results without a redundant query type, so that one query does not fill my context with child element data.

#### Acceptance Criteria

1. THE Per_Model_Query_Tools SHALL declare `query_type` as `Literal["all", "by_uuid", "by_title"]`.
2. IF a caller passes `by_type` as `query_type` to a Per_Model_Query_Tool, THEN THE Per_Model_Query_Tool SHALL return a validation error that lists the valid query types.
3. THE Per_Model_Query_Tools SHALL return each result item with Child_Type_Counts.
4. THE Per_Model_Query_Tools SHALL return each result item without a list of inline child element summaries.
5. THE Per_Model_Query_Tools SHALL return each result item with the `uuid`, `title`, `model_type`, `file_path`, and `sizeInBytes` values that the tools return before this feature.
6. THE Per_Model_Query_Tools SHALL return Tool_Descriptions that direct the caller to the matching `list_*` child element tools and `get_child_element` for child element data.
7. WHEN a document has no child elements, THE Per_Model_Query_Tools SHALL return an empty Child_Type_Counts object for that document.

### Requirement 7: Paging for list_oscal_resources

**User Story:** As an LLM calling the tools, I want `list_oscal_resources` to return pages, so that the full awesome-oscal list does not enter my context in one response.

#### Acceptance Criteria

1. THE `list_oscal_resources` tool SHALL accept `offset` and `limit` parameters.
2. WHEN a caller calls `list_oscal_resources`, THE `list_oscal_resources` tool SHALL return one page of resources with the `total`, `offset`, and `limit` values, in the paging format of the other list tools.
3. WHEN a caller calls `list_oscal_resources` without `offset` and `limit`, THE `list_oscal_resources` tool SHALL return the first page with the default page size of the other list tools.
4. IF a caller passes an `offset` greater than or equal to `total`, THEN THE `list_oscal_resources` tool SHALL return an empty page with the correct `total`.
5. IF a caller passes a negative `offset` or a `limit` less than 1, THEN THE `list_oscal_resources` tool SHALL return a validation error.

### Requirement 8: Server routing guide

**User Story:** As an AI client, I want the server instructions to tell me which tool to use for each task, so that the tool descriptions do not have to repeat cross-tool guidance.

#### Acceptance Criteria

1. THE Server SHALL include a Routing_Guide in the `instructions` field of the MCP initialize result.
2. THE Routing_Guide SHALL name the tool to use for each of these tasks: list OSCAL models, get a schema, look up Metaschema definitions and constraints, query loaded OSCAL documents, search loaded OSCAL documents, query Component Definitions, validate OSCAL content, and list OSCAL community resources.
3. THE Routing_Guide SHALL refer only to tool names that are present in the Tool_List_Response.
4. IF the Routing_Guide names a tool that is not present in the Tool_List_Response, THEN THE test suite SHALL fail with a message that names the unknown tool.
5. WHERE the Server registers `query_oscal_documentation`, THE Routing_Guide SHALL name `query_oscal_documentation` for OSCAL documentation questions.

### Requirement 9: Tool authoring steering and AGENTS.md

**User Story:** As a contributor (human or AI agent), I want one place that holds the tool authoring rules, so that new tools follow the same budgets and shape and do not undo the savings of this feature.

#### Acceptance Criteria

1. THE repository SHALL contain the Steering_File with front matter `inclusion: always`.
2. THE Steering_File SHALL contain the full tool wiring steps that the "How a tool is wired" section of `AGENTS.md` contains before this feature.
3. THE Steering_File SHALL contain the Description_Shape, the `Annotated[..., ParamDoc(...)]` parameter rule (with the reason `Field` is not used), the rule to exclude `ctx` docs, the rule to use `Literal` types for closed value sets, the rule to make identifying parameters required, the paging rule for list-shaped results, the Total_Budget, the Tool_Description_Cap, the Parameter_Description_Cap, and the list of Sync_Targets.
4. THE "How a tool is wired" section of `AGENTS.md` SHALL contain a summary of at most 5 lines and a link to the Steering_File.
5. THE Steering_File SHALL name the Budget_Tests file and the Budget_Module file.

### Requirement 10: Sync targets

**User Story:** As a maintainer, I want the manifest, README, and registry tests to match the live tools, so that clients and contributors see the same tool surface.

#### Acceptance Criteria

1. WHEN `bin/build_mcpb.py` builds the MCPB manifest, THE MCPB manifest SHALL contain, for each tool, a description equal to the first sentence or paragraph of the new Tool_Description.
2. THE `src/mcp_server_for_oscal/tools/README.md` file SHALL describe each tool and parameter in agreement with the new Tool_Descriptions, the removed `by_type` query type, the Child_Type_Counts result shape, the required parameters, and the `list_oscal_resources` paging parameters.
3. THE `tests/test_tool_registry.py` tests SHALL pass with the same tool names and tool count as before this feature.
4. THE tests that assert the Per_Model_Query_Tools result shape or the `by_type` query type SHALL assert the new behavior.
5. WHEN `hatch run tests` runs, THE test suite SHALL pass, including typing, all tests on Python 3.13 and 3.14, and the bandit scan.

### Requirement 11: No ctx parameter in the Agent tool specifications

**User Story:** As an operator of the Strands agent, I want the Agent tool specifications to omit the internal `ctx` parameter, so that the Agent's model uses less context and does not try to pass a value for `ctx`.

#### Acceptance Criteria

1. WHEN `create_oscal_agent()` loads the tools from `get_tool_list()`, THE Agent SHALL expose each Agent_Tool_Spec without a `ctx` property in the input schema.
2. WHEN `create_oscal_agent()` loads the tools from `get_tool_list()`, THE Agent SHALL expose each Agent_Tool_Spec with a `required` array that does not contain `ctx`.
3. WHEN `create_oscal_agent()` loads the tools from `get_tool_list()`, THE Agent SHALL expose each Agent_Tool_Spec without a `Context` entry in `$defs`.
4. IF removing the `Context` entry leaves the `$defs` object of an Agent_Tool_Spec empty, THEN THE Agent SHALL expose that Agent_Tool_Spec without a `$defs` key.
5. WHEN the Agent invokes a tool from `get_tool_list()`, THE tool function SHALL run and return its result with `ctx` equal to `None` or with no `ctx` argument passed.
6. Each tool function in `get_tool_list()` that declares `ctx` SHALL keep `ctx: Context | None = None` in its signature.
7. THE Server SHALL return a Tool_List_Response whose Tool_Entry values for the tools in `get_tool_list()` are equal to the Tool_Entry values that the Server returns without the Agent_Tool_Spec change of this requirement.
8. THE test suite SHALL include a test that checks criteria 1 to 4 for each tool in `get_tool_list()` and a test that invokes at least one tool through the Agent tool interface without a `ctx` argument.
