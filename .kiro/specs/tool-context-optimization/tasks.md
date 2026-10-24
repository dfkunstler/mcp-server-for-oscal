# Implementation Plan: Tool Context Optimization

## Overview

Python 3.13+ implementation for GitHub issue #39 on branch `39-tighten-tool-descriptions`. The guardrails land first: the Budget_Module (`tools/tool_budget.py`), the shared descriptions module (`tools/descriptions.py`), and their checker tests. The checkers are pure functions and are tested with synthetic input, so they pass from the first commit. The live-registry Budget_Tests come in task 9, after the rewrites, the paging change, and `main.build_server()`. Before that point the live `tools/list` is still over budget. The Agent tool spec change (`oscal_agent.agent_tools`, task 7.5) is independent of the rewrites and gives the Agent tool copies without `ctx` and `$defs.Context`, so the parity test in task 9.2 can require exact property-set equality. The live-registry tests (task 9) and the steering file (task 10) are the long-term guardrail: they check every tool in `get_tool_list()`, including tools added later, so future tools cannot regress the budget unnoticed.

Project rules for every task:

- Run all Python through hatch: `hatch test -- <pytest flags>`, `hatch test tests/path::Test`, `hatch run tests`, `hatch run python ...`. Never call `python`, `pytest`, `mypy`, `ruff`, or `bandit` directly.
- Never prefix commands with `cd`. Use the tool's `cwd` parameter. Restate this rule and the hatch rule to any subagent.
- Never commit to `main` and never push without explicit user approval. Stage only the files of this feature, by name.
- Commit messages include `#39` and state the test result (for example "tested: hatch run tests passes" or "untested").
- Lint and format with `hatch check code --fix` and `hatch check fmt --fix`.

## Tasks

- [x] 1. Commit the spec files to the feature branch
  - [x] 1.1 Commit requirements.md, design.md, tasks.md, and .config.kiro
    - Confirm the current branch is `39-tighten-tool-descriptions` (`git branch --show-current`); stop if it is `main`
    - Wait for user approval of the spec before committing
    - Stage only `.kiro/specs/tool-context-optimization/{requirements.md,design.md,tasks.md,.config.kiro}` by name
    - Message, for example: `#39 Add tool-context-optimization spec (docs only, no code to test)`
    - _Requirements: 10.5_

- [x] 2. Guardrails: shared descriptions module and Budget_Module with checker tests
  - [x] 2.1 Create `src/mcp_server_for_oscal/tools/descriptions.py`
    - `ParamDoc(str)` with `__slots__ = ()` and `__get_pydantic_json_schema__` that sets `description`
    - Shared text constants (`OFFSET_DOC`, `LIMIT_DOC`, `BUDGETED_LIMIT_DOC`, `PARENT_DOC_UUID_DOC`, `MODEL_NAME_DOC`, `OPTIONAL_MODEL_DOC`, `TARGET_SELECTOR_DOC`, `QUERY_TYPE_DOC`, `QUERY_VALUE_DOC`), each ≤ 250 characters
    - `Annotated` aliases as plain assignments, not PEP 695 `type` statements (`Offset`, `Limit`, `BudgetedLimit`, `ParentDocUuid`, `ModelName`, `OptionalModelName`, `TargetSelector`, `PerModelQueryType`, `QueryValue`)
    - `ABOUT_DESCRIPTION`. The Routing_Guide and `SERVER_INSTRUCTIONS` come in task 7
    - Module docstring explains why `ParamDoc` and not `Field` (strands-agents 1.59.0 `NotImplementedError`)
    - No import of `main`; no import-time side effects
    - _Requirements: 3.1, 3.2_

  - [x] 2.2 Write unit tests for `ParamDoc` in `tests/tools/test_descriptions.py`
    - A sample Strands `@tool` function with `ParamDoc` parameters: `MCPServer.add_tool` schema and `fn.tool_spec` both carry the same description per parameter
    - A `Literal` alias produces an `enum` in both registries
    - Direct Python calls with defaults still work (defaults are plain values, not `FieldInfo`)
    - Every shared `*_DOC` constant is ≤ `PARAMETER_DESCRIPTION_CAP`
    - _Requirements: 3.1, 3.4, 1.5_

  - [x] 2.3 Create `src/mcp_server_for_oscal/tools/tool_budget.py` (the Budget_Module)
    - Constants: `BASELINE_SIZE = 55_685`, `TOTAL_BUDGET = BASELINE_SIZE * 70 // 100`, `TOOL_DESCRIPTION_CAP = 1_200`, `PARAMETER_DESCRIPTION_CAP = 250`, `ROUTING_GUIDE_CAP = 3_000`, `MAX_DESCRIPTION_SENTENCES = 3`, `SUMMARY_VERBS`, `EXTRA_IDENTIFIERS`
    - `tools_list_size` (compact JSON, `ensure_ascii=False`), frozen dataclass `Violation` with `message()`
    - `check_limits`, `check_total`, `split_sentences`, `summary_sentence`, `description_shape_problems`, `known_identifiers`, `unknown_tool_refs` as specified in the design
    - Standard library imports only
    - _Requirements: 1.1, 1.2, 1.7, 2.1, 2.2, 2.3, 2.4, 2.6_

  - [x] 2.4 Write unit tests for the Budget_Module constants and helpers in `tests/test_tool_budget.py`
    - `TOTAL_BUDGET <= BASELINE_SIZE * 0.7`; the constants are the documented values
    - `split_sentences` keeps `e.g.`, `i.e.`, and `etc.` inside one sentence
    - `\b(list_models|get_schema)\b` does not match `list_oscal_models` or `get_oscal_schema`
    - Example `Violation.message()` text names tool, parameter, measured length, and limit
    - These tests use synthetic input only, so they pass now; the live-registry tests are added in task 9
    - _Requirements: 1.1, 1.2, 1.7, 1.8, 4.1_

  - [x] 2.5 Write property test for the limit checker
    - **Property 1: Limit checker reports exactly the over-limit descriptions**
    - **Validates: Requirements 1.4, 1.5, 1.7, 3.2**
    - In `tests/test_tool_budget.py`; Hypothesis, tagged `Feature: tool-context-optimization, Property 1`

  - [x] 2.6 Write property test for the shape checker
    - **Property 2: Shape checker rejects forbidden structure**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4**
    - In `tests/test_tool_budget.py`; tagged `Feature: tool-context-optimization, Property 2`

  - [x] 2.7 Write property test for unknown tool reference detection
    - **Property 3: Unknown tool references are detected exactly**
    - **Validates: Requirements 2.5, 2.6, 8.3, 8.4**
    - In `tests/test_tool_budget.py`; tagged `Feature: tool-context-optimization, Property 3`

- [x] 3. Store: Child_Type_Counts and the query_type guard
  - [x] 3.1 Update `src/mcp_server_for_oscal/tools/oscal_store.py`
    - Add `VALID_QUERY_TYPES = ("all", "by_uuid", "by_title")`; `query()` raises `ValueError` naming all three for any other value
    - Add `_child_type_counts(doc_id)` (static SQL, bound value, `GROUP BY element_type`)
    - `_build_query_items` returns `uuid`, `title`, `model_type`, `file_path`, `sizeInBytes`, `childTypeCounts`; remove `children`
    - Delete `_query_by_type`; confirm `query_component_definition` still only uses `by_uuid`/`by_title` on the store
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.7_

  - [x] 3.2 Update existing tests that assert the old store behavior
    - `tests/tools/test_oscal_store.py`: `children` → `childTypeCounts`; `by_type` → `ValueError`
    - `tests/test_properties.py`: replace the `by_type` tests
    - Search the rest of `tests/` for `children` and `by_type` on the 7 per-model tools and update them
    - _Requirements: 10.4_

  - [x] 3.3 Write property test for the query_type guard
    - **Property 4: Only the three per-model query types are accepted**
    - **Validates: Requirements 5.9, 6.1, 6.2**
    - In `tests/tools/test_oscal_store.py`; include `"by_type"` as an explicit `@example`

  - [x] 3.4 Write property test for child type counts
    - **Property 5: Query items carry exact child type counts**
    - **Validates: Requirements 6.3, 6.4, 6.5, 6.7**
    - In `tests/tools/test_oscal_store.py`; generated catalogs validated through `MODEL_MAP`, scanned into an ephemeral store under `tmp_path`

- [x] 4. Checkpoint: guardrails and store
  - Run `hatch check code --fix`, `hatch check fmt --fix`, then `hatch run tests`
  - Ensure all tests pass, ask the user if questions arise
  - Commit only the files from tasks 2 and 3, for example: `#39 Add tool budget checks, ParamDoc, store childTypeCounts (tested: hatch run tests passes)`

- [x] 5. Rewrite tool signatures and descriptions per group
  - Every rewrite: Description_Shape docstring (summary sentence starting with a `SUMMARY_VERBS` word, optional "Use when" sentence, optional paging sentence; no `Args:`/`Returns:`; no `ctx`), `Annotated[..., ParamDoc(...)]` or a shared alias for each user-facing parameter, required identifying parameters first, `ctx: Context | None = None` last, tool names referenced only if they exist. Follow the design's size-plan targets per group
  - [x] 5.1 Schema and models tools: `get_schema.py`, `list_models.py`
    - `get_oscal_schema`: `model_name` required (accepts `"complete"`), `schema_type: Literal["json", "xsd"] = "json"`, `ctx` last; keep runtime checks for direct calls
    - Update `tests/tools/test_get_schema.py`: keyword calls in place of positional `ctx`-first calls, no default `model_name`, new docstring text
    - _Requirements: 2.1-2.4, 3.1, 4.1, 5.1, 5.2, 5.3, 5.7, 5.8_

  - [x] 5.2 Validation tools: `validate_oscal_content.py`
    - `validate_oscal_content` and `validate_oscal_file` rewritten; no `config.allow_remote_uris` in any text
    - `file_uri` description: remote URIs are rejected unless the server operator enables remote URIs
    - Update the matching tests under `tests/tools/` that assert docstring text or positional calls
    - _Requirements: 2.1-2.4, 3.1, 4.4, 4.5_

  - [x] 5.3 Component Definition tools: `query_component_definition.py`
    - `query_component_definition`, `list_component_definitions`, `list_components`, `list_capabilities`, `get_capability`
    - No capability-first workflow; main route is `list_components` and `query_component_definition`; `list_capabilities`/`get_capability` described as for Component Definitions that contain capabilities
    - `get_capability.uuid` required, `ctx` last; `query_component_definition` keeps its own `by_type`
    - Update `tests/tools/test_query_component_definition.py` (and related) for signature and text changes
    - _Requirements: 2.1-2.4, 3.1, 4.2, 4.3, 5.5, 5.7_

  - [x] 5.4 Per-model and search tools: `query_oscal_models.py`
    - 7 Per_Model_Query_Tools: `query_type: PerModelQueryType = "all"`, `query_value: QueryValue`, `offset: Offset`, `limit: Limit`, `ctx` last; descriptions mention `childTypeCounts` and direct to the matching `list_*` child tools and `get_child_element`
    - Document `list_*` tools, child `list_*` tools, `get_child_element` (`element_id` required), `text_search_oscal` (`query_text` required, `oscal_model_type: OptionalModelName`)
    - Update `tests/tools/test_query_oscal_models*.py` for signatures, `by_type` removal, and text
    - _Requirements: 2.1-2.4, 3.1, 5.6, 5.7, 6.1, 6.6_

  - [x] 5.5 Metaschema tools: `get_definition.py`, `get_allowed_values.py`, `get_constraints.py`, `find_references_to.py`, `get_metaschema_source.py`
    - Shared `ModelName`/`OptionalModelName`, `TargetSelector`, `Offset`, `BudgetedLimit`; `get_definition.kind: Literal["assembly", "field", "flag"] | None = None` (keep `_check_kind`)
    - Move the Target_Selector grammar into `TARGET_SELECTOR_DOC`; cross-tool advice (generic `property`/`link`) is moved to the Routing_Guide in task 7
    - Update `tests/tools/test_get_allowed_values.py` docstring tests to assert `TARGET_SELECTOR_DOC` in the `target` parameter description; keep the `test_*_input_schema` property-order tests passing
    - _Requirements: 2.1-2.4, 3.1, 5.4, 5.8_

  - [x] 5.6 Documentation tool: `query_documentation.py`
    - Rewrite `query_oscal_documentation` in the Description_Shape with `ParamDoc` parameters; update its tests for text changes
    - _Requirements: 2.1-2.4, 3.1_

- [x] 6. Paging for `list_oscal_resources`
  - [x] 6.1 Update `src/mcp_server_for_oscal/tools/list_oscal_resources.py`
    - Add `parse_resource_entries(markdown)` per the design (headings level 2-6 set `category`, default "General"; top-level `-`/`*`/`+` items; continuations joined with single spaces; blank line ends an entry)
    - `list_oscal_resources(offset: Offset = 0, limit: Limit = 10, ctx=None) -> dict` returns `utils.paginate(entries, offset, limit)`; errors go through `try_notify_client_error` and are raised
    - Description_Shape docstring
    - Update `tests/tools/test_list_oscal_resources.py` for the `dict` page return; check the bundled file yields 82 entries in 5 categories
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 2.1-2.4, 3.1_

  - [x] 6.2 Write property test for resource markdown parsing
    - **Property 6: Resource markdown parsing round trip**
    - **Validates: Requirements 7.2**
    - In `tests/tools/test_list_oscal_resources.py`

  - [x] 6.3 Write property test for resource pages
    - **Property 7: Resource pages match a slice of the entries**
    - **Validates: Requirements 7.2, 7.3, 7.4**
    - Patch `read_resources_file` to return rendered markdown

  - [x] 6.4 Write property test for invalid paging arguments
    - **Property 8: Invalid paging arguments are rejected**
    - **Validates: Requirements 7.5**

- [x] 7. Routing_Guide, `main.build_server`, and Agent tool specs without `ctx`
  - [x] 7.1 Add `ROUTING_GUIDE` and `SERVER_INSTRUCTIONS` to `tools/descriptions.py`
    - Intro keeps the strings `tests/test_integration.py` asserts ("OSCAL MCP server", "Open Security Controls Assessment Language", "NIST", "security", "controls")
    - Guide names a tool per task in Req 8.2, including `query_oscal_documentation`, plus the generic `property`/`link` Metaschema tip; ≤ `ROUTING_GUIDE_CAP`
    - _Requirements: 8.1, 8.2, 8.3, 8.5_

  - [x] 7.2 Add `build_server()` and parameterize `_setup_tools` in `src/mcp_server_for_oscal/main.py`
    - `build_server()` returns `MCPServer(..., instructions=SERVER_INSTRUCTIONS)`; `mcp = build_server()`
    - `_setup_tools(server: MCPServer | None = None)`; `about` uses `ABOUT_DESCRIPTION`
    - Existing patches of `main._setup_tools` and `main.mcp` keep working; update `tests/test_main.py` / `tests/test_integration.py` only if they assert the old instructions text
    - _Requirements: 8.1_

  - [x] 7.3 Update `tests/tools/test_get_allowed_values.py` to assert the property/link advice in `ROUTING_GUIDE`
    - Replaces the old first-paragraph docstring assertion (supersedes metaschema-constraints Req 7.4)
    - _Requirements: 10.4_

  - [x] 7.4 Add an `instructions` assertion to `tests/test_integration_stdio_smoke.py`
    - The initialize result's `instructions` contains `list_oscal_models`
    - _Requirements: 8.1_

  - [x] 7.5 Add `strip_mcp_context`, `agent_tool`, and `agent_tools` to `src/mcp_server_for_oscal/oscal_agent.py`
    - Constants `MCP_CONTEXT_PARAM = "ctx"` and `MCP_CONTEXT_DEF = "Context"`
    - `strip_mcp_context(spec)`: deep copy; remove `ctx` from `properties` and `required`; remove `$defs.Context`; remove `$defs` when it is then empty; leave every other key unchanged; never mutate the input
    - `agent_tool(tool)`: for a `DecoratedFunctionTool`, `copy.copy` the tool and set `clone.tool_spec = strip_mcp_context(tool.tool_spec)` through the public setter; return any other object (plain callables, mocks) unchanged
    - `agent_tools(tools)`: map `agent_tool`, keep order
    - `create_oscal_agent()`: call `tools = agent_tools(tools)` right after the `tools` default is resolved, and pass that list to `Agent(...)`
    - Public Strands API only (`tool_spec` getter and setter, `copy.copy`); no `_metadata` or `_tool_func` access; no change to tool signatures or the shared module-level tool objects
    - Existing mock-based tests in `tests/test_oscal_agent.py` must still pass
    - _Requirements: 3.4, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7_

  - [x] 7.6 Add Agent tool spec tests to `tests/test_oscal_agent.py`
    - Per-tool specs: for each tool in `agent_tools(get_tool_list())`, the input schema has no `ctx` property, no `ctx` in `required`, no `$defs.Context`, no empty `$defs`, and no `"#/$defs/Context"` reference anywhere in the serialized spec
    - Real Agent registry: `create_oscal_agent()` with `boto3.Session` patched to `boto3.Session(region_name="us-east-1")` (real `BedrockModel` and `Agent`; construction makes no network call); `agent.tool_registry.get_all_tools_config()` has no `ctx` or `Context` in any input schema
    - Invocation: on that Agent, `agent.tool.get_oscal_schema(model_name="catalog", record_direct_tool_call=False)` returns `status == "success"`; a second test runs `agent_tool(fn).stream(...)` with input that has no `ctx` on a tool whose function is wrapped by a spy, and asserts the function received `ctx=None`
    - Originals unchanged: after `agent_tools()` runs, every tool that declares `ctx` still has it in `inspect.signature` and in the original `fn.tool_spec`; non-`DecoratedFunctionTool` items pass through as the same object
    - Server unchanged: the live `tools/list` from `main.build_server()` and `main._setup_tools(server)` is equal before and after `create_oscal_agent()` runs in the same process
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8_

  - [x] 7.7 Write property test for `strip_mcp_context`
    - **Property 9: Stripping the MCP context removes exactly ctx and its definition**
    - **Validates: Requirements 11.1, 11.2, 11.3, 11.4**
    - In `tests/test_oscal_agent.py`; Hypothesis, tagged `Feature: tool-context-optimization, Property 9`
    - Generators per the design: property names from `st.from_regex(r"[a-z][a-z_]{0,10}")` plus optional `ctx`, `required` subsets with optional `"ctx"`, optional `$defs` with optional `Context` and zero or more other entries, extra top-level keys
    - Assert removals, preserved order of `required`, unchanged other keys, unmutated input, and idempotence

- [-] 8. Checkpoint: rewrites, paging, routing
  - Run `hatch check code --fix`, `hatch check fmt --fix`, then `hatch run tests`
  - Ensure all tests pass, ask the user if questions arise
  - Commit only the files from tasks 5-7, for example: `#39 Rewrite tool descriptions, page list_oscal_resources, add routing guide, strip ctx from Agent specs (tested: hatch run tests passes)`

- [ ] 9. Live-registry Budget_Tests, MCPB, README, and test sync
  - [~] 9.1 Add the live-registry Budget_Tests to `tests/test_tool_budget.py`
    - Module-scoped `live_tools` fixture: `main.build_server()`, `main._setup_tools(server)`, `asyncio.run(server.list_tools())`, `model_dump(by_alias=True, exclude_none=True, mode="json")`
    - Names equal `{fn.__name__ for fn in get_tool_list()} | {"about"}`; `check_limits == []`; `check_total == []`
    - Iterate the live registry, never a hard-coded tool list, so tools added later are checked with no test change
    - Shape: `description_shape_problems == []` per tool; no `ctx` in any `inputSchema`; no `list_models`/`get_schema`; `unknown_tool_refs` empty for each description and for `instructions`
    - Content checks for Component Definition text, `validate_oscal_file`, and per-model query descriptions
    - Schema facts: `required` arrays, `enum` values, per-model `query_type` enum, `list_oscal_resources` `offset`/`limit`
    - MCP call path: `schema_type="yaml"`, `kind="module"`, `query_type="by_type"` raise `ToolError` listing valid values; `get_oscal_schema(model_name="complete")` succeeds
    - Routing_Guide: `instructions` contains `ROUTING_GUIDE`, names each Req 8.2 tool, length ≤ `ROUTING_GUIDE_CAP`
    - If any limit fails, trim the offending descriptions until it passes
    - _Requirements: 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.1-2.6, 3.2, 3.3, 4.1-4.5, 5.1-5.9, 6.1, 6.2, 6.6, 7.1, 8.1-8.5_

  - [~] 9.2 Add the Server/Agent parameter parity test in `tests/test_tool_param_parity.py`
    - Parametrize over `get_tool_list()`; the Agent_Tool_Spec is `oscal_agent.agent_tool(fn).tool_spec`, the same function `create_oscal_agent()` uses
    - The Agent property name set equals the server `inputSchema.properties` key set exactly, with no exception for `ctx`
    - For each property `p`, `agent_tool(fn).tool_spec["inputSchema"]["json"]["properties"][p]["description"]` equals the server description
    - A separate test asserts that `create_oscal_agent()` (with `boto3.Session`, `BedrockModel`, and `Agent` patched, as the existing tests do) passes `Agent` the specs from `agent_tools`, so the parity test checks what the Agent really gets
    - _Requirements: 3.4, 3.5, 3.6_

  - [~] 9.3 Update `bin/build_mcpb.py` and add its test
    - `tool_list()` uses `summary_sentence(inspect.getdoc(fn) or "")` and `summary_sentence(ABOUT_DESCRIPTION)`
    - New test `tests/test_build_mcpb.py` loads `bin/build_mcpb.py` with `importlib.util.spec_from_file_location` and compares each entry with `summary_sentence` of the live description
    - The repo template `conf/mcpb/manifest.json` keeps `"tools": []`; no regeneration needed
    - _Requirements: 10.1_

  - [~] 9.4 Update `src/mcp_server_for_oscal/tools/README.md`
    - New descriptions, removed per-model `by_type`, `childTypeCounts` result shape, required parameters, `list_oscal_resources` paging
    - _Requirements: 10.2_

  - [~] 9.5 Confirm `tests/test_tool_registry.py` and `tests/test_query_oscal_models_child_elements.py` pass unchanged
    - Same tool names and count; do not edit the expected sets
    - _Requirements: 10.3_

- [ ] 10. Steering file and `AGENTS.md`
  - [~] 10.1 Create `.kiro/steering/tool-authoring.md`
    - Front matter `inclusion: always`
    - The six wiring steps from today's `AGENTS.md` "How a tool is wired", updated for the new rules
    - Description_Shape with `SUMMARY_VERBS` and the 3-sentence maximum
    - Parameter rules: `Annotated[T, ParamDoc(...)]` and shared aliases in `tools/descriptions.py`, why not `Field` (strands-agents 1.59.0), no `ctx` docs and `ctx` last, keep the name `ctx` and the type `Context | None = None` (the MCP SDK hides it by type, `oscal_agent.agent_tools()` hides it from the Agent by name), `Literal` for closed sets, identifying parameters required, paging with `utils.paginate` or `paginate_within_budget`
    - Limits: Total_Budget 38,979, Tool_Description_Cap 1,200, Parameter_Description_Cap 250, `ROUTING_GUIDE_CAP` 3,000, size-plan targets; name `src/mcp_server_for_oscal/tools/tool_budget.py` as source of truth and `tests/test_tool_budget.py` as the Budget_Tests
    - Sync_Targets: `bin/build_mcpb.py` (`conf/mcpb/manifest.json`), `tools/README.md`, `tests/test_tool_registry.py`, Routing_Guide in `tools/descriptions.py`
    - _Requirements: 1.9, 9.1, 9.2, 9.3, 9.5_

  - [~] 10.2 Update `AGENTS.md`
    - "How a tool is wired" becomes ≤ 5 lines plus a link to `.kiro/steering/tool-authoring.md`
    - Add Code map rows for `tools/descriptions.py` and `tools/tool_budget.py`; the `oscal_agent.py` row mentions `agent_tools()`
    - _Requirements: 9.4_

  - [~] 10.3 Add steering and `AGENTS.md` sync tests to `tests/test_tool_budget.py`
    - Steering file has `inclusion: always`, the formatted values of `TOTAL_BUDGET`, `TOOL_DESCRIPTION_CAP`, `PARAMETER_DESCRIPTION_CAP` (formatted from the Budget_Module, not hard-coded), and names both files
    - `AGENTS.md` section body ≤ 5 non-blank lines and links to the steering file
    - _Requirements: 1.9, 9.1, 9.4, 9.5_

- [~] 11. Final checkpoint: measure and run the full suite
  - Measure the live `tools/list` size with `hatch run python -c "..."` using `main.build_server()`, `_setup_tools`, and `tool_budget.tools_list_size`; report it against `TOTAL_BUDGET` (38,979) and `BASELINE_SIZE` (55,685), and report the `instructions` length against `ROUTING_GUIDE_CAP`
  - Run `hatch check code`, `hatch check fmt`, then `hatch run tests` (typing, 3.13 + 3.14 with coverage, bandit)
  - Ensure all tests pass, ask the user if questions arise
  - Commit only the files from tasks 9-10 (and `tasks.md` status), for example: `#39 Add live budget tests, sync MCPB/README, tool-authoring steering (tested: hatch run tests passes)`
  - Do not push; ask the user before opening a PR
  - _Requirements: 1.3, 10.5_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. The guardrail checker tests (2.2, 2.4-2.7), the existing-test updates (3.2, 5.x, 6.1, 7.3), the Agent spec tests (7.6), and the live-registry, parity, and MCPB tests (9.1-9.3, 10.3) are not optional: they are the enforcement this feature adds.
- The Agent tool spec change (7.5) does not depend on the description rewrites and runs early. Its tests (7.6) need `main.build_server()` from 7.2, and the parity test (9.2) depends on `agent_tool` from 7.5.
- The checker tests in task 2 run on synthetic input and pass from the first commit. The live `tools/list` is over budget until tasks 5-7 land, so the live-registry tests are added in task 9.
- Requirement 3.1 uses `ParamDoc` instead of `pydantic.Field` (accepted deviation; see design "Requirement deviations").
- Tool names and count stay the same (renames and removals are issue #40).

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "2.3", "3.1", "7.5"] },
    { "id": 2, "tasks": ["2.2", "2.4", "3.2", "7.7"] },
    { "id": 3, "tasks": ["2.5", "3.3"] },
    { "id": 4, "tasks": ["2.6", "3.4"] },
    { "id": 5, "tasks": ["2.7", "5.1", "5.2", "5.3", "5.4", "5.5", "5.6", "6.1"] },
    { "id": 6, "tasks": ["7.1", "6.2"] },
    { "id": 7, "tasks": ["7.2", "7.3", "6.3"] },
    { "id": 8, "tasks": ["7.4", "6.4", "7.6"] },
    { "id": 9, "tasks": ["9.1", "9.2", "9.3", "9.4", "9.5"] },
    { "id": 10, "tasks": ["10.1", "10.2"] },
    { "id": 11, "tasks": ["10.3"] }
  ]
}
```
