# Implementation Plan: consolidate-tools

## Overview

Replace the 29 Removed_Tools with three Generic_Tools (`list_documents`, `query_documents`, `list_child_elements`) in a hard break released as 0.8.0 (GitHub issue #40). The language is Python 3.13+ (default dev environment 3.14). The store (`OscalStore`) does not change.

The order keeps the suite green where practical:

1. Freeze the reference oracle and measure the baselines (Before_Size, coverage) while the code is untouched.
2. Add the aliases and Generic_Tools next to the old tools, and test them against the store and the frozen oracle while the old tools still exist.
3. Remove the old tools and rewrite or delete their tests. The suite is red inside this group of tasks and green again at the checkpoint after it.
4. Rewrite the Routing_Guide, set the budget constants, write the Release_Note, sync the docs, and add the guard tests.
5. Run the full `hatch run tests`.

Rules for every task in this plan:

- Work on a feature branch created from issue #40 (for example with `gh issue develop 40`). Never work on or commit to `main`. The current branch is `main` at the time of writing, so create or check out the feature branch before task 1.1 edits anything.
- Run all Python through `hatch` (`hatch test`, `hatch run <script>`, `hatch run python ...`). Pass pytest flags after `--`. Never call `python`, `pytest`, `mypy`, `ruff`, or `bandit` directly.
- Never prefix a command with `cd`. Use the tool's working-directory parameter. Repeat this rule to any subagent.
- Follow `.kiro/steering/tool-authoring.md` and `.kiro/steering/oscal-bindings.md`.
- Run `hatch run tests` before any commit. Every commit message includes `#40` and says whether the code was tested successfully. Stage only files this feature changed. Do not commit or push without explicit user approval. The first commit on the feature branch is the spec files.

## Tasks

- [ ] 1. Preflight: baselines and frozen oracle
  - [ ] 1.1 Confirm the feature branch and record the baselines
    - Run `git branch --show-current`. If it prints `main`, stop and ask the user to approve creating the feature branch from issue #40.
    - Record the line coverage of `tools/query_oscal_models.py` and `tools/query_component_definition.py` from the report that `hatch run tests` writes to `private/docs/coverage` (the report cannot be recovered after the removal).
    - Measure Before_Size on a worktree of 599a41d with `hatch run python bin/export_tools_list.py <out.json>` (`PYTHONPATH=<worktree>/src`), field `toolsListChars`. Expect 34,938 characters in 46 entries (Python 3.14.8, `mcp` 2.1.1). Record the values and versions in `private/docs/consolidate-tools-baseline.md` (gitignored).
    - _Requirements: 12.1, 12.2, 16.3_

  - [ ] 1.2 Create `tests/removed_tools.py`
    - Define `Replacement(tool, args)` and `REMOVED_TOOLS: dict[str, Replacement]` with the 29 rows of the design's old-to-new mapping table.
    - Add `render_args()` (the fixed Mapping_Table cell format: `name="value"` joined by `, `) and the `GENERIC_TOOLS` and `RETAINED_TOOLS` name sets. Shared data only; no tests.
    - _Requirements: 8.1, 14.2, 15.2, 15.3_

  - [ ] 1.3 Create `tests/removed_tools_reference.py`
    - Copy verbatim from commit 599a41d (`git show 599a41d:<path>`): `old_query`, `old_list_documents`, `old_list_children` (the store call each removed tool made), and `old_list_component_definitions`, `old_list_components`, `old_list_capabilities`, `old_child_summary` (the removed helper bodies).
    - Each function takes a store. The module imports nothing from the removed tools, so it survives their deletion.
    - _Requirements: 6.1, 6.2, 6.3, 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ] 1.4 Check the frozen oracle against the live removed tools
    - Add a temporary test `tests/tools/test_removed_tools_oracle_check.py`. For every Removed_Tool, assert that calling the live tool equals the matching reference function over the `build_leak_store` fixture store, for several pages and `parent_doc_uuid` values.
    - Run it with `hatch test tests/tools/test_removed_tools_oracle_check.py` and fix the reference module until it passes. Task 4.2 deletes this file.
    - _Requirements: 6.1, 6.2, 6.3, 7.1, 7.2, 7.3, 7.4_

- [ ] 2. Add the Generic_Tools next to the old tools
  - [ ] 2.1 Add the shared aliases in `tools/descriptions.py`
    - Add `MODEL_TYPE_DOC`, `ELEMENT_TYPE_DOC` (each at most 250 characters), and plain-assignment aliases `DocumentModelType` (8 Model_Type literals) and `ChildElementType` (14 Element_Type literals), each `Annotated[Literal[...], ParamDoc(...)]`. Do not use a PEP 695 `type` statement or `pydantic.Field`.
    - Reuse `Offset`, `Limit`, `ParentDocUuid`, `PerModelQueryType`, and `QueryValue` unchanged.
    - _Requirements: 1.1, 2.1, 3.1, 10.3, 10.4_

  - [ ] 2.2 Implement the three tools in `tools/query_oscal_models.py`
    - Add `_reject`, `_check_model_type`, `_check_element_type`, `_check_page` (wrapping `metaschema_common.check_page`), and `_child_summary` (computes `sizeInBytes` from `include_raw_json=True`, drops `raw_json`).
    - Add `list_documents`, `query_documents`, and `list_child_elements` as Strands `@tool` functions with `ctx: Context | None = None` last. Check order: enum argument, page, tool-specific arguments, then `_get_store()`.
    - `query_documents` passes `query_value` through unchanged and raises `ValueError` naming the Query_Type when `by_uuid` or `by_title` has an empty or missing value. Descriptions follow the Description_Shape and stay at 180 characters or less.
    - Keep the old tools and helpers in place for now. Return plain dicts only.
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 2.2, 2.3, 2.4, 2.5, 2.6, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 10.1, 10.2, 10.3_

  - [ ] 2.3 Register the Generic_Tools and wire the test fixtures
    - Add the three tools to `get_tool_list()` in `tools/__init__.py`, in the order `list_documents`, `query_documents`, `list_child_elements`, with the old tools still registered.
    - In `tests/conftest.py` and the `installed_store` fixture of `tests/test_properties.py`, make sure `query_oscal_models._store` is reset and `query_oscal_models.init_store` is called (step 4 of the tool-authoring rules). Add only what is missing.
    - Add `CALLS` entries for the three tools to `tests/test_path_leak.py` (`TestCallsTable` fails when a registry tool has none). Temporarily extend the exact tool set and count in `tests/test_tool_registry.py` and adjust any other exact-count assertion that fails. Task 4 sets the final values.
    - Run `hatch run typing` and `hatch test` to confirm the suite is green.
    - _Requirements: 4.4, 4.6, 14.3_

  - [ ] 2.4 Create `tests/tools/test_generic_tools.py` with fixtures and schema tests
    - Add a module-scoped read-only fixture over `build_leak_store(tmp_path_factory...)`, plus one extra document with a multi-byte child title for Property 4.
    - Add example tests for: parameter order, defaults, aliases (`DocumentModelType` on both document tools), `ctx` last and hidden from the schema, and the `enum` of each parameter.
    - Add the enum sync tests: `model_type` enum equals the `OSCALModelType` keys of `CHILD_ELEMENT_TYPES` (5.1); `element_type` enum equals the union of its values (5.2); no Element_Type under two Model_Types (5.3); `query_type` enum equals `VALID_QUERY_TYPES` (5.4). Check once that removing one literal makes the test fail.
    - Add description tests: `description_shape_problems()` is empty, at most 180 characters, every parameter description at most 250 characters, `unknown_tool_refs()` is empty.
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 4.7, 5.1, 5.2, 5.3, 5.4, 10.1, 10.2, 10.3, 10.4, 10.5_

  - [ ] 2.5 Add unit tests for errors, empty results, and the uninitialised store
    - Out-of-range `offset` and `limit` raise `ValueError` and notify the client (patch `try_notify_client_error`).
    - Invalid `model_type`, `element_type`, and `query_type` passed directly from Python raise `ValueError` listing the valid values. `by_uuid` and `by_title` with `None` or `""` raise `ValueError` naming the Query_Type.
    - Each tool raises `RuntimeError` matching `init_store` when its store is unset, and raises `ValueError` first for invalid arguments even then.
    - Empty cases: a model with no documents, an unknown `parent_doc_uuid`, a parent with no child of the type, and an empty store (including `component-definition`, `component`, and `capability`) return an empty Page_Envelope with `total` 0 (7.5). A `raw_json` of `None` yields `sizeInBytes` 0.
    - Error messages and results hold no filesystem path.
    - _Requirements: 1.5, 2.6, 3.7, 3.8, 4.1, 4.2, 4.3, 4.4, 4.6, 7.5_

  - [ ] 2.6 Write property test for list_documents
    - **Property 1: list_documents equals the store**
    - Draw the Model_Type from `OSCALModelType`, `offset` from `integers(0, 30)`, `limit` from `integers(1, 100)`. Compare to `OscalStore.list_documents`; check the Page_Envelope keys, the Document_Summary keys, `model_type`, and `hasMore`. Include a second small store for an empty Model_Type. Use `deadline=None` and at least 100 examples.
    - **Validates: Requirements 1.3, 1.4, 1.5, 6.1**

  - [ ] 2.7 Write property test for query_documents
    - **Property 2: query_documents equals the store**
    - Draw `query_type` and `query_value` (existing UUID, existing title, case variant, partial title, unknown value, null for `all`) and a Valid_Page. Compare to `OscalStore.query` (or both raise `ValueError`); check the Query_Item keys and `model_type`.
    - **Validates: Requirements 2.4, 2.5, 6.2**

  - [ ] 2.8 Write property test for list_child_elements
    - **Property 3: list_child_elements equals the store, scoped and summarized**
    - Draw the Element_Type from the 14 values and `parent_doc_uuid` from `none()`, existing document UUIDs, a document of another model, and random UUID strings. Check `total`, `offset`, `limit`, `hasMore`, item order, the six shared fields, the exact Child_Summary keys, no `raw_json`, and the empty result for unknown parents.
    - **Validates: Requirements 3.4, 3.5, 3.6, 3.8, 3.9, 6.3**

  - [ ]* 2.9 Write property test for sizeInBytes
    - **Property 4: sizeInBytes is the UTF-8 length of the stored JSON**
    - For every Element_Type and every child in the store (including the multi-byte title), compare `sizeInBytes` to `len(raw_json.encode("utf-8"))` from the store, and to 0 when the store holds no `raw_json`.
    - **Validates: Requirements 3.7**

  - [ ]* 2.10 Write property test for paging
    - **Property 5: Paging a Generic_Tool visits every item once, in order**
    - For each Generic_Tool and any `limit` from 1 to 100, page with `offset` 0, `limit`, `2 * limit`, and so on until `hasMore` is false. The concatenation must equal one large call, with no repeats, ordered by (ASCII-lowercased title, identifier).
    - **Validates: Requirements 1.3, 3.4**

  - [ ]* 2.11 Write property test for page rejection
    - **Property 6: Out-of-range pages are rejected before the store is read**
    - Draw `offset` from `integers(max_value=-1)`, `limit` from `integers(max_value=0)` or `integers(min_value=101)`. For every Generic_Tool, assert the client notification and the `ValueError`, with the store singleton unset.
    - **Validates: Requirements 4.1**

  - [ ]* 2.12 Write property test for invalid enum arguments
    - **Property 7: Invalid enum arguments are rejected with the valid values**
    - Draw `text().filter(...)` outside the Model_Type, Element_Type, and Query_Type values. Assert the notification and a `ValueError` message that contains every valid value.
    - **Validates: Requirements 4.2, 4.3**

  - [ ]* 2.13 Write property test for a missing query_value
    - **Property 8: A missing query_value is rejected**
    - For any Model_Type, `query_type` in (`by_uuid`, `by_title`), and `query_value` of `None` or `""`, assert the notification and a `ValueError` that contains the `query_type`.
    - **Validates: Requirements 2.6**

  - [ ]* 2.14 Write property test for plain JSON results
    - **Property 9: Results are plain JSON**
    - For valid calls to every Generic_Tool, assert `result == json.loads(json.dumps(result))` and that no value is a Pydantic model instance.
    - **Validates: Requirements 4.5**

  - [ ] 2.15 Add equivalence tests for the Removed_Per_Model_Tools
    - Create `tests/tools/test_removed_tools_equivalence.py`. Parametrize over the 26 per-model rows of `REMOVED_TOOLS`. Call the replacement through the Mapping_Table arguments and compare to `old_query`, `old_list_documents`, and `old_list_children` from the frozen reference module.
    - Use property-based tests over `offset`, `limit`, and `parent_doc_uuid`. Require at least one fixture document for every Model_Type and Element_Type. For listers, compare `total`, `offset`, `limit`, `hasMore`, and that every old key and value appears in the new item.
    - **Property 1: list_documents equals the store**, **Property 2: query_documents equals the store**, **Property 3: list_child_elements equals the store, scoped and summarized** (oracle form)
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4**

  - [ ] 2.16 Add Component Definition equivalence tests
    - In the same test file, cover the three Removed_CDef_Listers against the fixtures of `tests/tools/test_query_component_definition.py`: key renames (`componentCount` to `childCount` equal to Components plus Capabilities, `uuid` to `id`, `name` to `title`, `parentComponentDefinition*` to `parentDocument*`), equal `total`, `hasMore`, and item order, plus empty Page_Envelope results where the old tools raised `RuntimeError`.
    - **Property 10: Component Definition listers map onto the Generic_Tools**
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.4, 7.5, 7.8**

  - [ ] 2.17 Add parameter parity tests for the Generic_Tools
    - In `tests/test_tool_param_parity.py`, assert that the MCP_Server `inputSchema` and the Agent tool spec have the same parameter names, required parameters, `enum` values, and parameter descriptions, and that the Agent spec has no `ctx`.
    - **Property 13: Server and Agent expose the same contract**
    - **Validates: Requirements 13.1, 13.2, 13.3**

- [ ] 3. Checkpoint - Generic_Tools are complete and equal to the oracle
  - Run `hatch run typing` and `hatch test`. The suite must be green with both the old and new tools registered. Ask the user if questions arise.

- [ ] 4. Remove the old tools and migrate their tests
  - [ ] 4.1 Remove the Removed_Tools from the code and the registry
    - In `tools/query_oscal_models.py`, delete the 26 per-model tools and the helpers `_query`, `_list_documents`, and `_list_children`. Keep `text_search_oscal` and `get_child_element`. Update the module docstring.
    - In `tools/query_component_definition.py`, delete `_child_summary`, `_child_page`, `_list_components`, `_list_capabilities`, `_list_component_definitions`, and the three `@tool` wrappers. Keep `query_component_definition` and `get_capability`.
    - Rewrite the `query_component_definition` description to name `list_child_elements` and `list_documents` (450 characters or less), change the `get_capability` `uuid` parameter description to name `list_child_elements` with element type `capability`, and change the info message in `_resolve_scope` to name `list_documents` with `model_type` `component-definition`.
    - Update `get_tool_list()` to the five `query_oscal_models` tools and two Component Definition tools, in the order of the design. The registry has 19 tools (20 with `about`).
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 9.1, 9.2, 9.3, 9.4, 9.5, 10.5_

  - [ ] 4.2 Delete the temporary oracle check
    - Delete `tests/tools/test_removed_tools_oracle_check.py`. From now on `tests/removed_tools_reference.py` is the only oracle.
    - _Requirements: 6.1, 6.2, 6.3_

  - [ ] 4.3 Rewrite `tests/tools/test_query_oscal_models.py`
    - Rewrite `TestCatalogTools`, the six empty-model classes, `TestPageResponseFormat`, `TestToolSignatures`, `TestToolDescriptions`, `TestMultiTypeIntegration`, and `TestPerModelItemsHaveNoFilePath` as `query_documents` and `list_documents` calls with `model_type`, one parametrized case per old per-model case. Re-key `_PER_MODEL_BODIES` by `OSCALModelType`.
    - Expect 5 tools in the module in `test_ctx_last_and_every_parameter_described`. Keep `TestInitStore`, `TestTextSearchOscal`, `TestTextSearchIntegration`, and `test_component_definition_query_results_unchanged`.
    - _Requirements: 9.5, 14.6_

  - [ ] 4.4 Rewrite `tests/tools/test_query_oscal_models_child_elements.py`
    - Rewrite `TestListCatalogControls`, `TestListCatalogGroups`, `TestListPoamItems`, and `TestListChildElementToolsEmptyResults` as `list_child_elements(element_type=...)` cases that also check `sizeInBytes`. Change `TestToolRegistration` to assert the module defines exactly the 5 tools and the registry has exactly 19. Keep `TestGetChildElement`.
    - _Requirements: 9.1, 14.3, 14.6_

  - [ ] 4.5 Rewrite `tests/tools/test_query_oscal_models_child_element_properties.py`
    - Rewrite the delegation, signature, and response-format properties over `element_type` drawn from the 14 values for the single tool.
    - _Requirements: 3.6, 14.6_

  - [ ] 4.6 Rewrite `tests/tools/test_query_oscal_models_child_element_properties_2.py`
    - Rewrite `TestStoreDependencyEnforcement` for the three Generic_Tools. Keep the `get_child_element` classes.
    - _Requirements: 4.4, 9.1, 14.6_

  - [ ] 4.7 Edit `tests/tools/test_query_component_definition.py`
    - Delete the tests of `list_component_definitions`, `list_components`, and `list_capabilities` and their three `WRAPPER_CALLS` entries. Check that `TestEmptyStores` scenarios are covered by task 2.5, then delete them here.
    - Add a test that the filter-miss info message names `list_documents`, one that the `query_component_definition` description names the Generic_Tools, and one that the `get_capability` `uuid` description names `list_child_elements`. Keep every other test as the regression suite for retained behavior.
    - _Requirements: 7.5, 9.2, 9.3, 9.4, 14.6_

  - [ ] 4.8 Rewrite the helper property in `tests/test_properties.py`
    - Rewrite "List helpers complete and correctly keyed" to page `list_child_elements` for `component` and `capability` and `list_documents` for `component-definition`, with the new key sets.
    - _Requirements: 7.1, 7.2, 7.3, 14.6_

  - [ ] 4.9 Replace the `CALLS` table in `tests/test_path_leak.py`
    - Replace the 29 removed entries with entries for `query_documents` and `list_documents` (every Model_Type) and `list_child_elements` (every Element_Type), reusing the `_query_model_calls` and `_child_list_calls` builders. `TestCallsTable` must still pass.
    - _Requirements: 4.6, 14.6_

  - [ ] 4.10 Rewrite `PER_MODEL_QUERY` in `tests/tools/test_text_search.py`
    - Build it with `functools.partial(qom.query_documents, model_type=...)` per Model_Type.
    - _Requirements: 9.5, 14.6_

  - [ ] 4.11 Set the final tool set in `tests/test_tool_registry.py`
    - Rewrite `BASE_TOOL_NAMES` (19 names) and the count assertion (19, or 20 with `about`). Prune `PRE_FEATURE_TOOLS` to the retained tools and change `test_snapshot_matches_registry_minus_new_tool` to `set(PRE) == (BASE - GENERIC - {"get_oscal_documentation"}) | {"about"}`. Keep the check that no retained tool lost a parameter.
    - _Requirements: 8.1, 9.1, 9.2, 9.3, 9.5, 14.3_

- [ ] 5. Checkpoint - Old tools are gone and the suite is green
  - Run `hatch run typing` and `hatch test`. Fix every failure before going on. (Tests that read the Routing_Guide, the budget, and the docs are rewritten in tasks 6 to 9; if they fail here, fix only what blocks this checkpoint.) Ask the user if questions arise.

- [ ] 6. Routing_Guide and budget
  - [ ] 6.1 Rewrite the Routing_Guide and its test
    - In `tools/descriptions.py`, change the three `ROUTING_GUIDE` bullets as in the design (documents, search, Component Definitions). Leave the other bullets and the Metaschema tip byte-identical. Keep the line `text_search_oscal, then get_child_element`.
    - In `tests/tools/test_descriptions.py`, replace `query_catalog` in the routing-guide tool list with `query_documents` and add `list_documents` and `list_child_elements`.
    - Check that the server instructions stay at 3,000 characters or less and that `unknown_tool_refs()` is empty.
    - _Requirements: 8.5, 10.5, 11.1, 11.2, 11.3, 11.4_

  - [ ] 6.2 Measure After_Size and update the Budget_Module and steering
    - Measure the After_Size with `hatch run python bin/export_tools_list.py <out.json>` (`toolsListChars`). Expect about 18,400 characters in 20 entries.
    - In `tools/tool_budget.py`, keep `BASELINE_SIZE` as history and add `BEFORE_CONSOLIDATION_SIZE = 34_938`, `BEFORE_CONSOLIDATION_TOOLS = 46`, `AFTER_CONSOLIDATION_SIZE`, and `AFTER_CONSOLIDATION_TOOLS = 20`, each with a comment that gives the commit (or "main at 599a41d plus the consolidate-tools working tree" plus the measurement command), Python 3.14.8, and `mcp` 2.1.1. Set `TOTAL_BUDGET = AFTER_CONSOLIDATION_SIZE * 110 // 100`.
    - In `.kiro/steering/tool-authoring.md`, update the Limits table to the new `TOTAL_BUDGET` and update the size-plan list to name `query_documents`, `list_documents`, `list_child_elements`, `get_child_element`, and `text_search_oscal` at 180 characters.
    - _Requirements: 12.1, 12.2, 12.3, 12.4_

  - [ ] 6.3 Rewrite `tests/test_tool_budget.py`
    - Test `BEFORE_CONSOLIDATION_SIZE`, `AFTER_CONSOLIDATION_SIZE`, and `After <= TOTAL_BUDGET <= 1.10 * After`; keep the `BASELINE_SIZE` test. Replace `PER_MODEL_QUERY_TOOLS` tests with `query_documents` ones, add the 180-character check for the Generic_Tools, rewrite `CDEF_TOOLS` and `test_cdef_main_route_and_capability_scope` for the new description text, and use `query_documents` with `{"model_type": "catalog", "query_type": "by_type"}` in `TestLiveCallPath`. Set `ROUTED_TOOLS` to the Generic_Tools.
    - Add a check that no Removed_Tool name is in the server instructions, and that the steering Limits table matches the constants (`TestSteeringSync`).
    - Run `hatch test tests/test_tool_budget.py` and confirm it passes against the live `tools/list` response.
    - _Requirements: 8.6, 10.2, 11.4, 12.2, 12.3, 12.4, 12.5_

- [ ] 7. Release note
  - [ ] 7.1 Write `docs/releases/v0.8.0.md`
    - Create the `docs/releases/` directory and the note: `# 0.8.0`, a "Breaking" label, a link to #40, and a statement that no aliases or compatibility flag exist.
    - Add the Mapping_Table with one row for each of the 29 Removed_Tools in the fixed cell format (backticked old name, backticked replacement, backticked arguments as `name="value"` joined by `, `), plus the result key renames for the three Component Definition listers (`componentCount` to `childCount`, `uuid` to `id`, `name` to `title`, `parentComponentDefinitionTitle` to `parentDocumentTitle`, `parentComponentDefinitionUuid` to `parentDocumentUuid`).
    - Add the behavior changes: `childCount` counts Components plus Capabilities; `importedComponentDefinitionsCount` was the literal 0 and has no replacement; empty-store listers return an empty page; `sizeInBytes`, `element_type`, and `description` added to child items; `model_type` added to Component Definition document items; `offset` and `limit` are now checked; `component-definition` is accepted by the document tools.
    - Add the Measurements section with Before_Size 34,938, the measured After_Size, 46 and 20 entries, Python 3.14.8, `mcp` 2.1.1, the measurement command, and the note that the requirement text quotes 55,685 from before 0.6.3. Add the publishing step: `gh release edit v0.8.0 --notes-file docs/releases/v0.8.0.md` after the draft exists (not run in this plan).
    - _Requirements: 7.6, 7.7, 12.1, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6_

- [ ] 8. Sync targets and documentation
  - [ ] 8.1 Update the tool tables in `src/mcp_server_for_oscal/tools/README.md` and `conf/powers/oscal/POWER.md`
    - Replace the per-model query, list, and child-lister rows and the three Component Definition lister rows with three Generic_Tool rows. Name no Removed_Tool.
    - _Requirements: 14.2_

  - [ ] 8.2 Update `README.md` and `AGENTS.md`
    - In `README.md`, update the tool list if present and change the transcript example near line 262 from `list_components` to `list_child_elements`. Keep the first-line `mcp-name` comment.
    - In `AGENTS.md`, change the code map rows for `query_oscal_models.py` (five tools) and `query_component_definition.py` (two tools).
    - _Requirements: 14.2, 14.5_

  - [ ] 8.3 Update `.kiro/steering/product.md` and `.kiro/steering/structure.md`
    - Update the tool list and the convention text that says each tool lives in its own file (modules hold several tools). `tool-authoring.md` was updated in task 6.2.
    - _Requirements: 14.2_

  - [ ] 8.4 Update `.agents/summary/` files
    - Update the tool tables in `components.md` and `interfaces.md`, and the single mentions of `list_catalog_controls` and `query_catalog` in `index.md` and `workflows.md`. Leave `review_notes.md` unchanged.
    - _Requirements: 14.2_

  - [ ] 8.5 Migrate `bin/eval_tool_selection_prompts.json`
    - Replace each Removed_Tool name in `expect` and `ok` with its Mapping_Table replacement, drop duplicates, and keep order (for example `docs-catalogs` expects `["list_documents", "query_documents"]`, `cdef-all-components` expects `["list_child_elements"]`).
    - _Requirements: 14.4_

- [ ] 9. Guard tests for removal and the Release_Note
  - [ ] 9.1 Create `tests/test_removed_tools.py` with the removal and eval tests
    - Parametrize over the 29 Removed_Tools: the name is absent from `get_tool_list()`, the live `tools/list`, `agent_tools()`, the attributes of `query_oscal_models`, `query_component_definition`, the `tools` package and `main`, every tool and parameter description, the server instructions, and every Sync_Targets file (excluding the Release_Note, this test, and `.agents/summary/review_notes.md`). The registry equals the Generic_Tools plus the Retained_Tools.
    - Check that every name in `bin/eval_tool_selection_prompts.json` is a registry tool or `about`, and that `conf/mcpb/manifest.json` generation by `bin/build_mcpb.py` lists exactly the registry tools (run `hatch test tests/test_build_mcpb.py` too).
    - **Property 11: Removed names appear nowhere**
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 14.1, 14.2, 14.4**

  - [ ] 9.2 Add the Mapping_Table and Release_Note tests
    - In the same file, parse the Mapping_Table of `docs/releases/v0.8.0.md`: 29 rows, each old name a Removed_Tool exactly once, each replacement a registry tool, each argument string equal to `render_args(REMOVED_TOOLS[old])`, and each argument value a member of the replacement's schema `enum`.
    - Check that the note contains `0.8.0`, `breaking`, `#40`, the five key renames, the no-alias statement, the behavior changes (including `importedComponentDefinitionsCount` and Components plus Capabilities), and the recorded sizes and versions that match the Budget_Module constants.
    - **Property 12: The Mapping_Table is exactly the removal set**
    - **Validates: Requirements 7.6, 7.7, 12.1, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7**

- [ ] 10. Final verification
  - [ ] 10.1 Format, lint, and coverage
    - Run `hatch check fmt --fix` and `hatch check code --fix` (the `check` commands without `--fix` must then report no errors). Run `hatch run typing`.
    - Compare the line coverage of `tools/query_oscal_models.py` and `tools/query_component_definition.py` in the new report to the baseline from task 1.1. Add tests for any uncovered error path of the new wrappers.
    - _Requirements: 16.2, 16.3_

  - [ ] 10.2 Run the full test suite
    - Run `hatch run tests` (mypy, pytest with coverage on Python 3.13 and 3.14, bandit). It must pass. Do not commit or push; ask the user for approval first, and include `#40` in any commit message, staging only this feature's files. Ask the user if questions arise.
    - _Requirements: 16.1, 16.2, 16.3_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. They are Properties 4 to 9. Requirements 3.7, 4.1 to 4.5, and 2.6 stay covered by the required unit tests in tasks 2.4 and 2.5.
- Properties 1 to 3, 10, 11, 12, and 13 are required because requirements 5, 6.4, 7.8, 8.6, 13.2, and 15.7 demand tests for them.
- The design corrects the requirement text: Before_Size is 34,938 characters in 46 entries (measured at 599a41d), not `BASELINE_SIZE` 55,685.
- `.github/` cannot be read from the design session, so the release-note publishing step is documented as a manual `gh release edit` and is not automated here.
- Between tasks 4.1 and 4.11 the suite is red by design (tests still call removed tools). Checkpoint 5 restores green.
- Every command runs through `hatch`, with no `cd` prefix.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3"] },
    { "id": 1, "tasks": ["1.4", "2.1"] },
    { "id": 2, "tasks": ["2.2"] },
    { "id": 3, "tasks": ["2.3"] },
    { "id": 4, "tasks": ["2.4", "2.15", "2.17"] },
    { "id": 5, "tasks": ["2.5", "2.16"] },
    { "id": 6, "tasks": ["2.6"] },
    { "id": 7, "tasks": ["2.7"] },
    { "id": 8, "tasks": ["2.8"] },
    { "id": 9, "tasks": ["2.9"] },
    { "id": 10, "tasks": ["2.10"] },
    { "id": 11, "tasks": ["2.11"] },
    { "id": 12, "tasks": ["2.12"] },
    { "id": 13, "tasks": ["2.13"] },
    { "id": 14, "tasks": ["2.14"] },
    { "id": 15, "tasks": ["4.1", "4.2"] },
    { "id": 16, "tasks": ["4.3", "4.4", "4.5", "4.6", "4.7", "4.8", "4.9", "4.10", "4.11"] },
    { "id": 17, "tasks": ["6.1"] },
    { "id": 18, "tasks": ["6.2"] },
    { "id": 19, "tasks": ["6.3"] },
    { "id": 20, "tasks": ["7.1"] },
    { "id": 21, "tasks": ["8.1", "8.2", "8.3", "8.4", "8.5"] },
    { "id": 22, "tasks": ["9.1"] },
    { "id": 23, "tasks": ["9.2"] },
    { "id": 24, "tasks": ["10.1"] },
    { "id": 25, "tasks": ["10.2"] }
  ]
}
```
