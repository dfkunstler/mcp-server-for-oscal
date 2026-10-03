# Requirements Document

## Introduction

`src/mcp_server_for_oscal/tools/query_component_definition.py` still contains the historical in-memory `ComponentDefinitionStore`, a module-level `_store` singleton populated at import time, and fallback branches in each MCP tool wrapper that delegate to it. Component Definitions are no longer bundled as raw files, so the legacy store is empty at runtime. As a result:

- `query_component_definition` raises "No Component Definitions loaded" for every component-level query, because the OscalStore delegation path falls back to `_store.query(...)` after the capability check.
- `OscalStore.list_child_elements` returns items keyed `"id"`, but `_oscal_store_list_components`, `_oscal_store_list_capabilities`, `_oscal_store_get_capability`, and `_oscal_store_find_capability` read `item["uuid"]`, which raises `KeyError` against a real store.
- Capability lookup helpers scan only the first 100 child elements, and reach into the private `OscalStore._conn` connection.

Existing tests miss these defects because they rely on mocks or on the legacy store. This feature removes the legacy store and its tests, re-implements component-level querying on `OscalStore`, fixes the key mismatch, adds a public `OscalStore` helper for document lookup by UUID, and adds tests that run against a real (non-mocked) `OscalStore` built from test fixtures. After the Legacy_Store is removed, the `component_definitions_dir` configuration option (environment variable `OSCAL_COMPONENT_DEFINITIONS_DIR`) has no runtime consumer. The option is retained for backward compatibility but is deprecated: the server logs a deprecation warning when the option is explicitly set, and documentation directs users to `OSCAL_DOCUMENTS_DIR` (`oscal_documents_dir`), which loads user-supplied OSCAL documents into the OscalStore.

## Glossary

- **CDef_Tools**: The module `mcp_server_for_oscal.tools.query_component_definition`, including the MCP tool wrappers `query_component_definition`, `list_component_definitions`, `list_components`, `list_capabilities`, and `get_capability`, and their private helper functions.
- **Legacy_Store**: The `ComponentDefinitionStore` class, the module-level `_store` singleton, the `_load_component_definitions_from_directory` alias, and the import-time `_store.load_from_directory()` call in CDef_Tools.
- **OscalStore**: The SQLite-backed store class in `mcp_server_for_oscal.tools.oscal_store`, injected into CDef_Tools via `init_store()`.
- **Store_Singleton**: The module-level `_oscal_store` variable in CDef_Tools set by `init_store()`.
- **Component_Definition**: An OSCAL `component-definition` document held in the OscalStore.
- **Component**: An OSCAL `DefinedComponent` contained in a Component_Definition.
- **Capability**: An OSCAL `Capability` contained in a Component_Definition.
- **CDef_Filter**: The optional `component_definition_filter` argument of `query_component_definition`, matched against a Component_Definition UUID or (case-insensitively) its `metadata.title`.
- **Component_Query_Response**: The existing response shape for component results: keys `components`, `total_count`, `offset`, `limit`, `hasMore`, `query_type`, `component_definitions_searched`, `filtered_by`.
- **Capability_Query_Response**: The existing response shape for a capability result: keys `capability`, `component_count`, `offset` (0), `limit` (1), `total` (1), `hasMore` (false), `query_type`, `component_definitions_searched`, `filtered_by`.
- **Page_Response**: The existing paginated list shape with keys `items`, `total`, `offset`, `limit`, `hasMore`.
- **Document_Lookup_Helper**: A public OscalStore method that resolves a document UUID to its internal document id or parsed trestle model.
- **Fixture_Store**: An OscalStore instance created in tests from JSON Component_Definition fixtures under `tests/fixtures/`, without mocking OscalStore methods.
- **Test_Suite**: The project test suite run by `hatch run tests` (mypy, pytest, coverage, bandit).
- **Config**: The `Config` class in `mcp_server_for_oscal.config`, including its module-level `config` instance.
- **Server**: The OSCAL MCP server process started via `mcp_server_for_oscal.main.main()`.
- **Deprecated_CDef_Dir_Setting**: The `component_definitions_dir` Config attribute, populated from the `OSCAL_COMPONENT_DEFINITIONS_DIR` environment variable (including values loaded from a `.env` file). No command-line argument exists for this setting.
- **Replacement_Setting**: The `oscal_documents_dir` Config attribute, populated from the `OSCAL_DOCUMENTS_DIR` environment variable, whose directory the Server scans into the OscalStore at startup.
- **Deprecation_Warning**: A log record at `WARNING` level stating that `OSCAL_COMPONENT_DEFINITIONS_DIR` is deprecated, has no effect, and will be removed in a future release, and naming `OSCAL_DOCUMENTS_DIR` as the replacement.
- **Setting_Docs**: The files `DEVELOPING.md`, `dotenv.example`, and `src/mcp_server_for_oscal/tools/README.md`, which are the documentation files referencing the Deprecated_CDef_Dir_Setting.

## Requirements

### Requirement 1: Remove the legacy in-memory store

**User Story:** As a maintainer, I want the unused in-memory Component Definition store removed, so that there is a single code path for Component Definition queries.

#### Acceptance Criteria

1. THE CDef_Tools module SHALL contain no definition of the `ComponentDefinitionStore` class.
2. THE CDef_Tools module SHALL contain no module-level `_store` singleton and no `_load_component_definitions_from_directory` alias.
3. WHEN the CDef_Tools module is imported, THE CDef_Tools module SHALL perform no filesystem reads of Component_Definition content.
4. THE CDef_Tools module SHALL contain only imports that are referenced by remaining CDef_Tools code.
5. THE Config class SHALL continue to accept the `allow_remote_uris` and `request_timeout` settings, and SHALL continue to accept the Deprecated_CDef_Dir_Setting as specified in Requirement 8.

### Requirement 2: Require an initialised OscalStore in MCP tool wrappers

**User Story:** As a developer, I want the MCP tool wrappers to depend only on the OscalStore, so that misconfiguration fails clearly instead of silently querying an empty store.

#### Acceptance Criteria

1. WHEN any of the five CDef_Tools MCP tool wrappers is invoked while the Store_Singleton is set, THE CDef_Tools wrapper SHALL delegate to the OscalStore-backed implementation.
2. IF any of the five CDef_Tools MCP tool wrappers is invoked while the Store_Singleton is unset, THEN THE CDef_Tools wrapper SHALL raise a `RuntimeError` whose message states that the OscalStore is not initialised.
3. THE CDef_Tools module SHALL keep the `init_store(store)` function signature used by `main._init_oscal_store()`.

### Requirement 3: Component-level querying on the OscalStore

**User Story:** As an AI assistant user, I want `query_component_definition` to return Components from the OscalStore, so that component queries work against the shipped database.

#### Acceptance Criteria

1. WHEN `query_component_definition` is called with `query_type` "all" and no CDef_Filter, THE CDef_Tools SHALL return a Component_Query_Response containing every Component across all Component_Definitions in the OscalStore, subject to pagination.
2. WHEN `query_component_definition` is called with `query_type` "all" and a CDef_Filter that matches a Component_Definition, THE CDef_Tools SHALL return a Component_Query_Response containing only the Components of the matched Component_Definition, subject to pagination.
3. WHEN `query_component_definition` is called with `query_type` "by_uuid" and no Capability matches the `query_value`, THE CDef_Tools SHALL return a Component_Query_Response containing the Component whose UUID equals `query_value` within the searched Component_Definitions.
4. WHEN `query_component_definition` is called with `query_type` "by_title" and no Capability matches the `query_value`, THE CDef_Tools SHALL return a Component_Query_Response containing the Component whose title equals `query_value` case-insensitively within the searched Component_Definitions.
5. WHEN `query_component_definition` is called with `query_type` "by_title" and neither a Capability nor a Component title matches the `query_value`, THE CDef_Tools SHALL return a Component_Query_Response containing the first Component, within the searched Component_Definitions, having a property whose value equals `query_value`.
6. WHEN `query_component_definition` is called with `query_type` "by_type", THE CDef_Tools SHALL return a Component_Query_Response containing every Component whose `type` equals `query_value` within the searched Component_Definitions, subject to pagination.
7. THE CDef_Tools SHALL represent each returned Component as the trestle `DefinedComponent` serialised with `dict(exclude_none=True)`.
8. WHEN no Component matches a "by_uuid", "by_title", or "by_type" query, THE CDef_Tools SHALL return a Component_Query_Response with an empty `components` list and `total_count` 0.
9. THE CDef_Tools SHALL set `component_definitions_searched` to the number of Component_Definitions in scope for the query (1 when a CDef_Filter matches, otherwise the total number of Component_Definitions in the OscalStore).
10. WHEN `query_component_definition` is called with a CDef_Filter, THE CDef_Tools SHALL parse only the Component_Definition matched by the CDef_Filter to produce Component results.
11. WHEN `query_component_definition` is called with `query_type` "by_uuid" or "by_title" and no CDef_Filter, THE CDef_Tools SHALL parse only the Component_Definitions that contain a matching Component candidate to produce Component results.
12. THE CDef_Tools SHALL apply `offset` and `limit` to `components` and report `total_count` and `hasMore` consistently with the existing `paginate` helper.

### Requirement 4: Preserve capability-first query semantics and error behaviour

**User Story:** As an AI assistant user, I want existing query behaviour preserved, so that prompts and clients relying on the current response formats keep working.

#### Acceptance Criteria

1. WHEN `query_component_definition` is called with `query_type` "by_uuid" or "by_title" and a Capability in scope matches `query_value`, THE CDef_Tools SHALL return a Capability_Query_Response for that Capability.
2. WHERE a CDef_Filter is provided, THE CDef_Tools SHALL match Capabilities only within the Component_Definition matched by the CDef_Filter.
3. IF `query_type` is "by_uuid", "by_title", or "by_type" and `query_value` is empty or missing, THEN THE CDef_Tools SHALL raise a `ValueError` stating that `query_value` is required for that `query_type`.
4. IF the OscalStore contains zero Component_Definitions, THEN THE CDef_Tools `query_component_definition` SHALL raise a `ValueError` with the message "No Component Definitions loaded".
5. IF a CDef_Filter matches no Component_Definition by UUID or case-insensitive title, THEN THE CDef_Tools SHALL return a Component_Query_Response with an empty `components` list, `total_count` 0, and `component_definitions_searched` 0.
6. WHEN `query_value` contains leading or trailing whitespace, THE CDef_Tools SHALL strip the whitespace before matching.

### Requirement 5: Correct list and capability helpers

**User Story:** As an AI assistant user, I want `list_components`, `list_capabilities`, and `get_capability` to work against the real OscalStore, so that discovery tools return data instead of errors.

#### Acceptance Criteria

1. WHEN `list_components` is called, THE CDef_Tools SHALL return a Page_Response whose items each contain `uuid`, `title`, `parentComponentDefinitionTitle`, `parentComponentDefinitionUuid`, and `sizeInBytes`, with `uuid` populated from the OscalStore child element identifier.
2. WHEN `list_capabilities` is called, THE CDef_Tools SHALL return a Page_Response whose items each contain `uuid`, `name`, `parentComponentDefinitionTitle`, `parentComponentDefinitionUuid`, and `sizeInBytes`, with `uuid` populated from the OscalStore child element identifier.
3. WHEN `get_capability` is called with the UUID of a Capability in the OscalStore, THE CDef_Tools SHALL return the full Capability as a dict, regardless of the Capability's position among all Capabilities in the OscalStore.
4. WHEN `get_capability` is called with an empty UUID or a UUID that matches no Capability, THE CDef_Tools SHALL return `None`.
5. WHEN a Capability lookup is performed by `query_component_definition`, THE CDef_Tools SHALL locate a matching Capability regardless of the Capability's position among all Capabilities in the OscalStore.
6. IF `list_components` is called while the OscalStore contains zero Components, THEN THE CDef_Tools SHALL raise a `RuntimeError` with the message "No Components loaded".
7. WHEN `list_capabilities` is called while the OscalStore contains zero Capabilities, THE CDef_Tools SHALL return a Page_Response with an empty `items` list and `total` 0.
8. WHEN `list_component_definitions` is called, THE CDef_Tools SHALL return a Page_Response with the existing item keys `uuid`, `title`, `componentCount`, `importedComponentDefinitionsCount`, and `sizeInBytes`.

### Requirement 6: Public OscalStore document lookup

**User Story:** As a maintainer, I want CDef_Tools to use only public OscalStore APIs, so that the store's internal schema can change without breaking the tools.

#### Acceptance Criteria

1. THE OscalStore SHALL provide a public Document_Lookup_Helper that accepts a document UUID and returns the parsed trestle model for that document.
2. IF the Document_Lookup_Helper is called with a UUID that matches no document, THEN THE OscalStore SHALL return `None`.
3. THE CDef_Tools module SHALL access the OscalStore exclusively through public (non-underscore-prefixed) OscalStore attributes and methods.

### Requirement 7: Tests against a real OscalStore

**User Story:** As a maintainer, I want tests that exercise CDef_Tools against a real OscalStore, so that integration defects like the key mismatch are caught.

#### Acceptance Criteria

1. THE Test_Suite SHALL contain no references to `ComponentDefinitionStore`, `_store`, `_store._reset`, `load_from_directory`, or `_load_component_definitions_from_directory` from CDef_Tools.
2. THE Test_Suite SHALL include tests that call each of the five CDef_Tools MCP tool wrappers against a Fixture_Store.
3. THE Test_Suite SHALL include Fixture_Store tests for `query_component_definition` covering `query_type` values "all", "by_uuid", "by_title" (including property-value fallback), and "by_type", each with and without a CDef_Filter.
4. THE Test_Suite SHALL include a Fixture_Store test in which a Capability is retrieved by `get_capability` and by `query_component_definition` while more than 100 Capabilities exist in the Fixture_Store.
5. THE Test_Suite SHALL include a test verifying that each CDef_Tools MCP tool wrapper raises `RuntimeError` while the Store_Singleton is unset.
6. THE Test_Suite SHALL include tests for the Document_Lookup_Helper covering a known UUID and an unknown UUID.
7. THE Test_Suite SHALL include property-based tests in `tests/test_properties.py`, run against Fixture_Stores, verifying that a CDef_Filter given as a Component_Definition UUID or case-insensitive title scopes `query_component_definition` results for `query_type` "all", "by_uuid", and "by_title" to the matched Component_Definition, replacing the legacy CDef_Filter scoping property tests.
8. WHEN `hatch run tests` is executed after the change, THE Test_Suite SHALL complete with zero failures, zero mypy errors, and zero new bandit findings.

### Requirement 8: Deprecate the component definitions directory setting

**User Story:** As a user with `OSCAL_COMPONENT_DEFINITIONS_DIR` in an existing configuration, I want the server to keep starting and to tell me the setting is obsolete, so that I can migrate to `OSCAL_DOCUMENTS_DIR` without a broken deployment.

#### Acceptance Criteria

1. WHEN the `OSCAL_COMPONENT_DEFINITIONS_DIR` environment variable is set, THE Config SHALL load without raising an error and SHALL expose the value as the `component_definitions_dir` attribute.
2. WHEN the Server starts while the `OSCAL_COMPONENT_DEFINITIONS_DIR` environment variable is set, THE Server SHALL emit exactly one Deprecation_Warning.
3. WHEN the Server starts while the `OSCAL_COMPONENT_DEFINITIONS_DIR` environment variable is unset, THE Server SHALL emit zero Deprecation_Warnings.
4. THE Server SHALL produce identical OscalStore contents and identical MCP tool registrations regardless of the value of the Deprecated_CDef_Dir_Setting.
5. THE Setting_Docs SHALL mark the Deprecated_CDef_Dir_Setting as deprecated, state that the setting has no effect, and name the Replacement_Setting (`OSCAL_DOCUMENTS_DIR`) as the replacement.
6. THE Test_Suite SHALL include a test verifying that exactly one Deprecation_Warning is emitted at Server startup when `OSCAL_COMPONENT_DEFINITIONS_DIR` is set.
7. THE Test_Suite SHALL include a test verifying that zero Deprecation_Warnings are emitted at Server startup when `OSCAL_COMPONENT_DEFINITIONS_DIR` is unset.
8. THE Test_Suite SHALL retain a test verifying that Config exposes the value of `OSCAL_COMPONENT_DEFINITIONS_DIR` as the `component_definitions_dir` attribute.
