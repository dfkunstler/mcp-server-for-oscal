# Requirements Document

## Introduction

The MCP Server for OSCAL currently uses `compliance-trestle` (imported as `trestle`) for OSCAL Pydantic models. This feature replaces it with `oscal-bindings` (`oscal_bindings.models`), a typed binding generated for the same OSCAL release as the bundled schemas. The change is a library swap: accept/reject results and store caching and indexing behavior stay the same. Every tool that returns OSCAL model content returns valid OSCAL JSON.

Three intentional differences are accepted:

- Validation level 3 is renamed from `"trestle"` to `"model"` (client-visible breaking change).
- Parsed datetimes keep their original UTC offset instead of being normalized to UTC.
- `get_capability` and the component results of `query_component_definition` switch from snake_case Python field names with `null` values to OSCAL property names with unset fields omitted (client-visible breaking change). The previous `get_capability` output was kept snake_case for compatibility with the original tool; that compatibility is intentionally dropped.

The component definition tools currently re-parse the parent Component Definition and dump the element model, which is where the snake_case keys and `null` values come from. This feature fixes those tools at the source instead of wrapping their output: they return the element's stored OSCAL JSON (the child element `raw_json` the Store writes at index time), falling back to materializing the element from its parent only when `raw_json` is missing. Returning Pydantic models directly from tools is rejected because neither the MCP SDK nor Strands applies `exclude_none` to tool results, and Strands falls back to Python `repr` for models nested in dicts and uses snake_case field names for bare models.

The upstream enhancement cairn-proofs/oscal-bindings-python#5 (serialize to valid OSCAL by default) is tracked separately. This feature does not depend on it and stays on `oscal-bindings==0.1.0`. When that enhancement ships, a follow-up may bump the pin.

Tracked by GitHub issue #26 on branch `26-replace-trestle-with-oscal-bindings`.

## Glossary

- **Server**: The `mcp-server-for-oscal` package under `src/mcp_server_for_oscal/`, including `main.py`, `oscal_agent.py`, and `tools/`.
- **Bindings**: The `oscal-bindings` Python package, imported as `oscal_bindings`, whose `oscal_bindings.models` module provides typed Pydantic classes for OSCAL models.
- **Trestle**: The `compliance-trestle` Python package, imported as `trestle`, which this feature removes.
- **Model_Map**: The single mapping named `MODEL_MAP` in `tools/utils.py` from each `OSCALModelType` member to its Bindings model class.
- **Model_Class**: One of the Bindings classes `Catalog`, `Profile`, `ComponentDefinition`, `SystemSecurityPlan`, `AssessmentPlan`, `AssessmentResults`, `PlanOfActionAndMilestones`, `MappingCollection`.
- **Typed_Parse**: Parsing a document by calling `Model_Class.model_validate(root_data)`, where `root_data` is the object under the document's OSCAL root key.
- **Union_Parse**: Parsing through `oscal_bindings.parse_oscal()` or any `oscal_bindings` `parse_*` helper that validates against a union of all model types.
- **Validator**: The `validate_oscal_content` and `validate_oscal_file` tools in `tools/validate_oscal_content.py`.
- **Model_Level**: Validation level 3 of the Validator, reported with level name `"model"`.
- **Store**: `OscalStore` in `tools/oscal_store.py`, including its LRU parse cache and lazy child-element indexing.
- **Cdef_Tools**: The component definition tools in `tools/query_component_definition.py`.
- **OSCAL_JSON**: A JSON-compatible `dict[str, Any]` that uses OSCAL property names (hyphenated aliases such as `incorporates-components` and `control-implementations`), contains no key for any unset optional field and no `null` value, and holds only JSON-native values (UUIDs and datetimes as strings).
- **Stored_Element_JSON**: The OSCAL JSON of a child element as stored by the Store in the child element's `raw_json` column at index time.
- **Element_JSON_Reader**: The existing function `_raw` in `tools/query_component_definition.py`, which returns Stored_Element_JSON parsed to a dict when `raw_json` is present, and otherwise materializes the element from its parent Component Definition and serializes it with `model_dump_json(exclude_none=True, by_alias=True)`.
- **Model_Output_Tools**: Every MCP tool whose result contains OSCAL model content, including the capability query and component query paths of `query_component_definition`, `get_capability`, and the tools that return stored raw document or child-element JSON.
- **Upstream_Serialization_Enhancement**: The oscal-bindings issue cairn-proofs/oscal-bindings-python#5, which proposes that Bindings models serialize to valid OSCAL by default. Tracked separately; this feature does not depend on it.
- **Bundled_Schema_Version**: The OSCAL version returned by `get_bundled_oscal_version()`, derived from the bundled schema `$id` and equal to `CURRENT_RELEASE_VERSION` in `bin/update-oscal-schemas.sh` (currently `1.2.3`).
- **Test_Suite**: The full check run by `hatch run tests`: mypy, pytest on Python 3.11 and 3.12 with coverage, and bandit.
- **Code_Tree**: The directories `src/`, `bin/`, and `tests/`.
- **Project_Docs**: `AGENTS.md`, `.kiro/steering/tech.md`, `.kiro/steering/hatch.md`, `.kiro/steering/product.md`, `.agents/summary/*`, `src/mcp_server_for_oscal/tools/README.md`, `conf/powers/oscal/POWER.md`, `README.md`, and `CONTRIBUTING.md`.
- **Historical_Specs**: Existing spec directories under `.kiro/specs/` other than `replace-trestle-with-oscal-bindings`.

## Requirements

### Requirement 1: Dependency swap

**User Story:** As a maintainer, I want the project to depend on oscal-bindings instead of compliance-trestle, so that the OSCAL models match the bundled schema release.

#### Acceptance Criteria

1. THE Server SHALL declare `oscal-bindings==0.1.0` as a runtime dependency in `pyproject.toml`.
2. THE Server SHALL omit `compliance-trestle` from the dependencies declared in `pyproject.toml`.
3. WHEN the maintainer runs `hatch run update`, THE Server SHALL produce a `requirements.txt` that pins `oscal-bindings==0.1.0` and contains no `compliance-trestle` entry.
4. THE Code_Tree SHALL contain no import of the `trestle` package.

### Requirement 2: Single model map

**User Story:** As a developer, I want one model map shared by the store and the validator, so that model support cannot drift between the two.

#### Acceptance Criteria

1. THE Server SHALL define Model_Map in `tools/utils.py` alongside `OSCALModelType`.
2. THE Model_Map SHALL map each of the eight `OSCALModelType` members (catalog, profile, component-definition, system-security-plan, assessment-plan, assessment-results, plan-of-action-and-milestones, mapping-collection) directly to the corresponding Model_Class object.
3. THE Model_Map SHALL reference Model_Class objects imported from `oscal_bindings.models`, with no `importlib`-based lookup.
4. THE Store SHALL obtain Model_Class objects from the Model_Map imported from `tools/utils.py`.
5. THE Validator SHALL obtain Model_Class objects from the Model_Map imported from `tools/utils.py`.
6. THE Code_Tree SHALL contain no definition of `TRESTLE_MODEL_MAP` or `_TRESTLE_MODEL_MAP`.

### Requirement 3: Typed parsing

**User Story:** As a developer, I want documents parsed with the specific typed class, so that parsing is fast and errors refer to a single model.

#### Acceptance Criteria

1. WHEN the Store parses or validates a document, THE Store SHALL use Typed_Parse with the Model_Class from Model_Map.
2. WHEN the Validator runs Model_Level, THE Validator SHALL use Typed_Parse with the Model_Class from Model_Map.
3. THE Code_Tree SHALL contain no call to Union_Parse.

### Requirement 4: Model validation level rename

**User Story:** As an MCP client author, I want validation level 3 named for what it checks rather than the library behind it, so that the result format stays stable across library changes.

#### Acceptance Criteria

1. WHEN the Validator reports results, THE Validator SHALL report level 3 with the level name `"model"`.
2. IF a prior level failure or an unresolved model type causes the Validator to skip remaining levels, THEN THE Validator SHALL report the skipped level 3 entry with the level name `"model"`.
3. THE Validator SHALL describe level 3 in the `validate_oscal_content` and `validate_oscal_file` docstrings as model validation using OSCAL Pydantic models, without naming Trestle.
4. THE Server SHALL name the level 3 function in `validate_oscal_content.py` `_validate_model`.
5. IF loading the Model_Class for level 3 fails, THEN THE Validator SHALL report an error message beginning with `"Failed to load OSCAL model class"`.
6. THE `src/mcp_server_for_oscal/tools/README.md` SHALL document level 3 with the level name `"model"`.

### Requirement 5: Library-neutral naming in the Server

**User Story:** As a maintainer, I want identifiers, messages, and docstrings that do not name the model library, so that a future library change does not require another rename.

#### Acceptance Criteria

1. THE Store SHALL name its document validation method `_validate_with_model`.
2. THE Store SHALL describe parsed documents in docstrings and log messages as "parsed OSCAL model" instead of "Parsed Trestle model".
3. THE Server SHALL use error, log, and docstring text in the Store and Validator that refers to OSCAL models without naming Trestle or Bindings.
4. THE Server SHALL contain no `logging.getLogger("trestle")` call in `main.py` or `oscal_agent.py`.
5. WHERE the Bindings package emits log records under a named logger, THE Server SHALL set that logger's level to `config.log_level` at each site in `main.py` and `oscal_agent.py` that previously configured the `"trestle"` logger.

### Requirement 6: Valid OSCAL tool output

**User Story:** As an MCP client user, I want every tool that returns OSCAL model content to return valid OSCAL JSON, so that the output can be used directly as OSCAL without reshaping.

#### Acceptance Criteria

1. THE Cdef_Tools SHALL import every OSCAL model class they use from `oscal_bindings.models`.
2. WHEN the capability query path of `query_component_definition` returns a capability, THE Cdef_Tools SHALL return the dict produced by the Element_JSON_Reader for that capability.
3. WHEN the component query path of `query_component_definition` returns components, THE Cdef_Tools SHALL return the dict produced by the Element_JSON_Reader for each component.
4. WHEN `get_capability` finds the requested capability, THE Cdef_Tools SHALL return the dict produced by the Element_JSON_Reader for that capability.
5. WHEN the capability query path of `query_component_definition` returns a capability, THE Cdef_Tools SHALL set `component_count` to the length of the `incorporates-components` list in the returned capability dict, or 0 when that key is absent.
6. THE Cdef_Tools SHALL contain no call to `oscal_dict()` and no `_wrap_capability` function.
7. WHEN the capability query path of `query_component_definition`, the component query path of `query_component_definition`, or `get_capability` returns model content, THE Cdef_Tools SHALL return that content as OSCAL_JSON whose values equal the source document's OSCAL JSON for the same element, with equivalent datetime representations treated as equal.
8. THE Model_Output_Tools SHALL return OSCAL model content as OSCAL_JSON.
9. WHEN the Store serializes a child element to Stored_Element_JSON, THE Store SHALL pass `by_alias=True` and `exclude_none=True` explicitly to `model_dump_json`.
10. WHEN the Element_JSON_Reader materializes an element from its parent Component Definition, THE Element_JSON_Reader SHALL pass `by_alias=True` and `exclude_none=True` explicitly to `model_dump_json`.
11. THE Model_Output_Tools SHALL return plain JSON-compatible dicts, with no Pydantic model instance in any tool result.
12. WHEN a component returned by the component query path of `query_component_definition` is embedded in a minimal component-definition document, THE Validator's level 2 JSON Schema check SHALL report the document as valid against the bundled OSCAL JSON schema.
13. WHEN a capability returned by the capability query path of `query_component_definition` or by `get_capability` is embedded in a minimal component-definition document, THE Validator's level 2 JSON Schema check SHALL report the document as valid against the bundled OSCAL JSON schema.
14. THE Test_Suite SHALL include a test asserting that the model content returned by the capability query path, the component query path, and `get_capability` contains no `null` value at any depth.
15. THE Test_Suite SHALL include a test asserting that the model content returned by the capability query path, the component query path, and `get_capability` contains no snake_case key at any depth where the OSCAL property name is hyphenated (for example, no `incorporates_components`, `control_implementations`, or `implemented_requirements`).

### Requirement 7: Preserved behavior

**User Story:** As a user, I want validation and querying to behave as before, so that the library swap is invisible apart from the documented changes.

The criteria below cover acceptance, rejection, caching, and indexing. Tool output format is governed by Requirement 6 and datetime offsets by Requirement 8; where they differ from the previous implementation, Requirements 6 and 8 take precedence.

#### Acceptance Criteria

1. WHEN the Validator receives a document that the previous implementation accepted at level 3, THE Validator SHALL accept the document at Model_Level.
2. WHEN the Validator receives a document that the previous implementation rejected at level 3, THE Validator SHALL reject the document at Model_Level.
3. WHEN the Store scans a document, THE Store SHALL accept or reject the document with the same result as the previous implementation.
4. THE Store SHALL cache parsed OSCAL models in its LRU cache with the same cache size and eviction behavior as the previous implementation.
5. THE Store SHALL extract child elements and FTS rows on first access to a document, as the previous implementation did.

### Requirement 8: Datetime offset handling

**User Story:** As a maintainer, I want the accepted datetime behavior change captured in tests, so that the suite reflects how Bindings parses timestamps.

#### Acceptance Criteria

1. WHEN the Server parses an OSCAL datetime that carries a UTC offset, THE Server SHALL preserve the original offset in the parsed value.
2. THE Test_Suite SHALL contain no assertion that expects parsed datetimes to be normalized to UTC.

### Requirement 9: Schema version guard

**User Story:** As a maintainer, I want a test that fails when the bindings and the bundled schemas target different OSCAL releases, so that schema updates cannot silently desynchronize the models.

#### Acceptance Criteria

1. THE Test_Suite SHALL include a test asserting that `oscal_bindings.__oscal_schema_version__` equals Bundled_Schema_Version.
2. THE Server SHALL leave the `about` tool output unchanged, with no Bindings version field.

### Requirement 10: Test updates

**User Story:** As a maintainer, I want the tests to use the new library and terms, so that the suite verifies the shipped behavior.

#### Acceptance Criteria

1. THE Test_Suite SHALL import OSCAL model classes from `oscal_bindings.models`.
2. THE Test_Suite SHALL expect the level 3 name `"model"` wherever the Validator's level names are asserted.
3. THE Test_Suite SHALL refer to catalog group classes as `CatalogGroupWithGroups` and `CatalogGroupWithControls` in docstrings that previously named `Group1` and `Group2`.
4. THE Code_Tree SHALL contain no occurrence of the string `trestle`, matched case-insensitively.
5. WHEN the maintainer runs `hatch run tests`, THE Test_Suite SHALL pass on Python 3.11 and 3.12, including mypy, coverage, and bandit.

### Requirement 11: Documentation and steering

**User Story:** As a contributor or AI agent, I want the docs and steering rules to describe oscal-bindings, so that new work uses the correct library.

#### Acceptance Criteria

1. THE repository SHALL contain an oscal-bindings guideline at `.kiro/steering/oscal-bindings.md` that directs contributors to use `oscal_bindings.models` typed classes with `model_validate` and to avoid Union_Parse.
2. THE repository SHALL omit `.kiro/steering/compliance-trestle.md`.
3. THE Project_Docs SHALL describe `oscal-bindings` as the OSCAL model library and contain no reference to Trestle as a current dependency.
4. THE `AGENTS.md` SHALL omit the note that the model map is duplicated in `oscal_store.py` and `validate_oscal_content.py`.
5. THE `AGENTS.md` and `src/mcp_server_for_oscal/tools/README.md` SHALL describe Model_Map in `tools/utils.py` as the single source of OSCAL model classes.
6. THE Historical_Specs SHALL remain unmodified.
