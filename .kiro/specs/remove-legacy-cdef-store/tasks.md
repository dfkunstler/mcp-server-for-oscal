# Implementation Plan: remove-legacy-cdef-store

## Overview

Make `OscalStore` the only backend for the Component Definition tools. Steps: add two public `OscalStore` capabilities (`get_parsed_model_by_uuid`, keyword-only filters on `list_child_elements`), build a real Fixture_Store test helper, rewrite the CDef_Tools helpers around `_require_store` / `_resolve_scope` / `_iter_children`, delete the legacy `ComponentDefinitionStore` and its fallbacks, deprecate `OSCAL_COMPONENT_DEFINITIONS_DIR` with a one-time startup warning, then replace the legacy and mock-based tests with tests against a real store. Language: Python 3.11+ (hatch, pytest, hypothesis, ruff, mypy). Shell commands run from the workspace root via the `cwd` parameter, never with a `cd` prefix. Tracked by GitHub issue [#8](https://github.com/dfkunstler/mcp-server-for-oscal/issues/8) in `dfkunstler/mcp-server-for-oscal`.

## Tasks

- [ ] 1. Set up branch and spec commit
  - [-] 1.1 Create the feature branch from the existing GitHub issue and commit the spec
    - Confirm the current branch with `git branch --show-current` (expected: `main`). Never commit to `main`.
    - The issue already exists; do not search for, draft, or create one. No default repo is set, so pass `-R dfkunstler/mcp-server-for-oscal` to every `gh` call and do not run `gh repo set-default`
    - Create and check out the branch: `gh issue develop 8 -R dfkunstler/mcp-server-for-oscal --checkout --name 8-remove-legacy-cdef-store`
    - Stage only `.kiro/specs/remove-legacy-cdef-store/` files (`.config.kiro`, `requirements.md`, `design.md`, `tasks.md`) and commit: `docs: add remove-legacy-cdef-store spec (#8)`
    - Do not push without explicit user approval
    - _Requirements: none (project git-strategy)_

- [ ] 2. Add public OscalStore lookup and filtering APIs
  - [~] 2.1 Implement `OscalStore.get_parsed_model_by_uuid(doc_uuid)`
    - In `src/mcp_server_for_oscal/tools/oscal_store.py`, resolve `documents.uuid` to `id` with a bound parameter and delegate to `get_parsed_model()` so the existing LRU cache is used
    - Return `None` for an empty UUID or no matching row
    - _Requirements: 6.1, 6.2_

  - [~] 2.2 Add keyword-only `element_id`, `title`, `include_raw_json` to `OscalStore.list_child_elements`
    - `element_id` → `ce.uuid = ?`; `title` → `ce.title = ? COLLATE NOCASE`; `include_raw_json=True` adds `"raw_json"` to each item
    - Keep positional parameters and defaults unchanged so existing `query_oscal_models` callers are untouched; use bound parameters only (the `# nosec B608` f-string interpolates only the fixed clause list)
    - Ordering stays `title COLLATE NOCASE, uuid`
    - _Requirements: 5.3, 5.5, 6.3_

  - [~] 2.3 Write unit tests for the new OscalStore APIs
    - In `tests/tools/test_oscal_store.py`: `get_parsed_model_by_uuid` with a known UUID (returns a `ComponentDefinition`), an unknown UUID and an empty string (both `None`)
    - `list_child_elements` examples: `element_id` filter, case-insensitive `title` filter, combination with `parent_doc_uuid` and `element_type`, `include_raw_json` present only when requested, default call output unchanged
    - _Requirements: 6.1, 6.2, 7.6_

- [ ] 3. Build Fixture_Store test infrastructure
  - [~] 3.1 Create `tests/fixture_store.py`
    - `build_fixture_store(directory, cdefs) -> OscalStore`: write each cdef dict as JSON under `directory/docs`, create `OscalStore(db_path=..., cache_size=10, seed_from_bundled=False)`, call `scan_directory`
    - `make_many_capabilities_cdef(n=120)`: one Trestle-valid cdef whose capability names sort so the target sits beyond position 100
    - Helpers to load the three valid JSON fixtures in `tests/fixtures/` (`sample_component_definition.json`, `multi_component_definition.json`, `sample_component_definition_with_capabilities.json`)
    - No mocking of OscalStore methods
    - _Requirements: 7.2, 7.4_

  - [~] 3.2 Move the autouse reset fixture to `tests/conftest.py` and add store fixtures
    - Move `reset_oscal_store` from `tests/tools/conftest.py` and `tests/test_properties.py` into `tests/conftest.py`; rewrite its docstring to say it isolates the `_oscal_store` singleton, and that unset-store tests rely on it being `None`
    - Add `fixture_store` (builds from the three fixtures, calls `init_store`, yields, closes) and `many_capabilities_store` fixtures using `tests/fixture_store.py`
    - Leave the legacy Property 16 tests in `tests/test_properties.py` in place for now (they are removed in task 7.4)
    - _Requirements: 7.2, 7.4, 7.5_

- [~] 4. Checkpoint - OscalStore APIs and test infrastructure
  - Run `hatch run tests` and `hatch fmt`; ensure all tests pass, ask the user if questions arise.
  - Commit only the files changed in tasks 2–3 plus the updated `tasks.md`: `feat: add OscalStore uuid lookup and child filters, fixture store helpers (#8) - tests passing`

- [ ] 5. Rewrite CDef_Tools on OscalStore
  - [~] 5.1 Add store access and scope helpers in `src/mcp_server_for_oscal/tools/query_component_definition.py`
    - `_require_store()` raising `RuntimeError("OscalStore is not initialised; call init_store() before using Component Definition tools")`; keep `init_store(store)` signature
    - Frozen dataclass `_Scope(cdef_uuid, searched)`; `_resolve_scope` using `store.query` by_uuid then by_title with an exact case-insensitive title check (reject FTS-only hits), returning `None` and logging the existing guidance message when unmatched
    - `_iter_children` paging `list_child_elements` with `_CHILD_PAGE = 500`; `_first`, `_raw` (falls back to parent materialization when `raw_json` is `None`), `_component_type`, `_has_prop_value`
    - _Requirements: 2.2, 2.3, 4.2, 4.5, 6.3_

  - [~] 5.2 Implement candidate selection, materialization, and capability lookup
    - `_select_component_candidates` for `all`, `by_uuid` (`element_id`), `by_title` (`title`, then prop-value fallback over `raw_json`), `by_type` (`raw_json` type check); `ValueError` for invalid `query_type`
    - `_materialize_components`: parse each distinct parent once via `get_parsed_model_by_uuid`, return `DefinedComponent.dict(exclude_none=True)`, `logger.warning` and skip candidates missing from the parent
    - `_find_capability(store, scope, query_type, value)` via `element_type="capability"` + `element_id`/`title`, materialized from the parent; no `_conn` access and no 100-item cap
    - _Requirements: 3.3, 3.4, 3.5, 3.6, 3.7, 3.10, 3.11, 5.5, 6.3_

  - [~] 5.3 Implement `_query_component_definition(store, ctx, filter, query_type, query_value, offset, limit)`
    - Strip `query_value`; `ValueError` when required and empty; `ValueError("No Component Definitions loaded")` when the store has zero cdefs (both with `try_notify_client_error`)
    - Empty Component_Query_Response with `component_definitions_searched = 0` when the filter matches nothing
    - Capability-first for `by_uuid`/`by_title` returning the Capability_Query_Response (`oscal_dict()`, `component_count`, offset 0, limit 1, total 1, hasMore false); log exceptions and fall through
    - `paginate` candidates, materialize only the page, return Component_Query_Response with `component_definitions_searched = scope.searched`
    - _Requirements: 3.1, 3.2, 3.8, 3.9, 3.12, 4.1, 4.3, 4.4, 4.5, 4.6_

  - [~] 5.4 Implement list and capability helpers
    - `_list_components`: `include_raw_json=True`, `uuid = item["id"]`, parent title/UUID keys, `sizeInBytes` from `raw_json` UTF-8 length (0 if `None`); `RuntimeError("No Components loaded")` when total is 0
    - `_list_capabilities`: `uuid = item["id"]`, `name = item["title"]`, empty Page_Response when none
    - `_get_capability(store, uuid)`: `None` for empty UUID or no match, otherwise `cap.dict()`
    - `_list_component_definitions(store, ctx, offset, limit)`: existing logic, explicit `store` argument
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.6, 5.7, 5.8_

  - [~] 5.5 Rewire the five `@tool()` wrappers and remove the legacy store
    - Each wrapper keeps its signature and docstring, calls `store = _require_store()`, and delegates to its helper
    - Delete `ComponentDefinitionStore`, `_store`, `_load_component_definitions_from_directory`, the import-time `load_from_directory()` call, all `else` fallbacks, the old `_oscal_store_*` helpers, and `# pragma: no cover` guards that no longer apply
    - Remove now-unused imports (`zipfile`, `Path`, `urlparse`, `requests`, `cast`, `config`, `json` if unused); confirm with `hatch fmt` / ruff F401
    - Do not touch `config.py` here (`component_definitions_dir`, `allow_remote_uris`, `request_timeout` retained; the deprecation comment is task 6.3)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2_

- [ ] 6. Update startup messaging and deprecate `OSCAL_COMPONENT_DEFINITIONS_DIR`
  - [~] 6.1 Update `main.py::_init_oscal_store` docstring and warning text
    - Remove references to falling back to the legacy store; state that the Component Definition tools raise until a store is initialised
    - Adjust any assertion on the old message in `tests/test_main.py`
    - _Requirements: 2.2_

  - [~] 6.2 Add the deprecation warning to `main.py`
    - Add `import os`, module constant `_DEPRECATED_CDEF_DIR_ENV = "OSCAL_COMPONENT_DEFINITIONS_DIR"`, and `_warn_deprecated_settings()` as in the design: keyed on `_DEPRECATED_CDEF_DIR_ENV in os.environ` (any value, including `""`), never on `config.component_definitions_dir`; one `logger.warning` naming the setting, stating it has no effect, and naming `OSCAL_DOCUMENTS_DIR`
    - Call it exactly once in `main()`, immediately after the `# reConfigure logging` block and before `config.validate_transport()`, so `stdio` and `streamable-http` share the single call
    - Do not call it from `Config.__init__` or `_init_oscal_store()`
    - _Requirements: 8.2, 8.3, 8.4_

  - [~] 6.3 Mark the setting deprecated in `config.py` and the setting docs
    - `config.py`: replace the `component_definitions_dir` comment with the design's `DEPRECATED` comment; keep the attribute and its `os.getenv("OSCAL_COMPONENT_DEFINITIONS_DIR", "component_definitions")` default unchanged
    - `DEVELOPING.md` (env var table, ~line 48): description becomes "**Deprecated**, no effect. Use `OSCAL_DOCUMENTS_DIR`."; keep the row
    - `dotenv.example` (~line 31): replace the commented assignment with `# OSCAL_COMPONENT_DEFINITIONS_DIR is deprecated and has no effect; use OSCAL_DOCUMENTS_DIR.`
    - `src/mcp_server_for_oscal/tools/README.md` (~line 373): bullet becomes "`component_definitions_dir`: deprecated, no effect; use `oscal_documents_dir` (`OSCAL_DOCUMENTS_DIR`)"
    - _Requirements: 1.5, 8.1, 8.5_

  - [~] 6.4 Write `TestDeprecatedSettings` in `tests/test_main.py`
    - Required (satisfies 8.6, 8.7). Each test patches `mcp_server_for_oscal.main.mcp`, `verify_package_integrity`, `_init_oscal_store`, `_setup_tools`, `logging.basicConfig`, sets `sys.argv = ["main.py"]`, and uses `caplog.at_level(logging.WARNING, logger="mcp_server_for_oscal.main")`; control the env with `monkeypatch.setenv` / `monkeypatch.delenv(..., raising=False)` (a developer `.env` may already have injected the variable)
    - `test_warns_once_when_cdef_dir_set`, parametrized over `"/custom/comp_defs"` and `""`: exactly one `WARNING` record mentioning `OSCAL_COMPONENT_DEFINITIONS_DIR`, and it mentions `OSCAL_DOCUMENTS_DIR`
    - `test_warns_once_with_streamable_http`: same assertion with `--transport streamable-http`
    - `test_no_warning_when_cdef_dir_unset`: zero such records
    - `test_cdef_dir_has_no_effect_on_startup`: run `main()` with the variable set and unset; `_init_oscal_store` and `_setup_tools` mocks have identical `call_args_list`
    - `test_cdef_dir_not_read_outside_config`: scan `src/mcp_server_for_oscal/**/*.py`; `component_definitions_dir` appears only in `config.py`, `OSCAL_COMPONENT_DEFINITIONS_DIR` only in `config.py` and `main.py` (depends on task 5.5 having removed the CDef_Tools reference)
    - Keep `tests/test_config.py::test_component_definitions_dir_still_works` unchanged; confirm it still passes
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.6, 8.7, 8.8_

- [ ] 7. Replace CDef_Tools tests with real-store tests
  - [~] 7.1 Rewrite `tests/tools/test_query_component_definition.py`: wrapper and query matrix tests
    - Remove all legacy-store and mock-delegation tests
    - Each of the five wrappers against `fixture_store`; `list_component_definitions` item keys
    - `query_component_definition` × {`all`, `by_uuid`, `by_title`, `by_title` prop-value fallback (`"PostgreSQL Global Development Group"`), `by_type`} × {no filter, filter}
    - `list_components` / `list_capabilities` item keys and `uuid` populated from the child element id
    - _Requirements: 2.1, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 5.1, 5.2, 5.8, 7.2, 7.3_

  - [~] 7.2 Add error and edge-case tests to `tests/tools/test_query_component_definition.py`
    - Parametrized over the five wrappers: `RuntimeError` when `_oscal_store is None`
    - `ValueError` for missing and whitespace-only `query_value`; empty store → "No Component Definitions loaded"; unmatched filter → empty response with `component_definitions_searched` 0; fuzzy-only title filter does not match; whitespace-padded `query_value` still matches
    - `many_capabilities_store` (>100 capabilities): `get_capability` and `query_component_definition` `by_uuid` and `by_title` find the capability beyond position 100; unknown UUID and empty UUID return `None`
    - `list_capabilities` empty page on a store without capabilities; `list_components` `RuntimeError("No Components loaded")` on a store whose only cdef has no components
    - _Requirements: 2.2, 4.3, 4.4, 4.5, 4.6, 5.3, 5.4, 5.5, 5.6, 5.7, 7.4, 7.5_

  - [~] 7.3 Add parse-scope and structural smoke tests to `tests/tools/test_query_component_definition.py`
    - Spy with `wraps=store.get_parsed_model_by_uuid`: filtered queries only parse the matched cdef; unfiltered `by_uuid`/`by_title` only parse candidate parents
    - Module has no `ComponentDefinitionStore`, `_store`, `_load_component_definitions_from_directory`
    - Reloading the module under patched `Path.iterdir`/`open` reads nothing from `config.component_definitions_dir`
    - AST check: no `_oscal_store._*` or `store._*` attribute access in CDef_Tools
    - _Requirements: 1.1, 1.2, 1.3, 3.10, 3.11, 6.3_

  - [~] 7.4 Remove legacy tests from `tests/test_properties.py`
    - Delete the `_store` import and `TestProperty16ComponentDefinitionFilterScoping` (all `_store._reset()` / `load_from_directory` usage)
    - Grep the test suite for `ComponentDefinitionStore`, `_store._reset`, `load_from_directory`, `_load_component_definitions_from_directory` and the legacy `_store` import from CDef_Tools; none may remain
    - _Requirements: 7.1_

- [~] 8. Checkpoint - CDef_Tools rewrite and example tests
  - Run `hatch run tests` and `hatch fmt`; ensure all tests pass, ask the user if questions arise.
  - Commit only the files changed in tasks 5–7 plus the updated `tasks.md`: `refactor: remove legacy ComponentDefinitionStore, query cdefs via OscalStore (#8) - tests passing`

- [ ] 9. Property-based tests against a Fixture_Store
  - Add a Hypothesis strategy in `tests/test_properties.py` for Trestle-valid stores (1–4 cdefs, 0–5 components, 0–3 capabilities each, `type` from a small set); build each example in a `tempfile.TemporaryDirectory()` with `build_fixture_store`; `@settings(max_examples=100, deadline=None)`; tag docstrings `Feature: remove-legacy-cdef-store, Property N: <title>`

  - [~] 9.1 Write property test for scope completeness and fidelity of `all`
    - **Property 1: Scope completeness and fidelity for `all`**
    - Required: replaces legacy `test_filter_by_uuid_scopes_to_single_cdef` / `test_filter_by_title_scopes_to_single_cdef`
    - **Validates: Requirements 3.1, 3.2, 3.7, 3.9, 7.7**

  - [~] 9.2 Write property test for lookup by UUID and title
    - **Property 2: Lookup by UUID and title respects scope, case, and whitespace**
    - Required: replaces legacy `test_filter_scoping_with_by_uuid_query`
    - **Validates: Requirements 3.3, 3.4, 3.8, 4.6, 7.7**

  - [~] 9.3 Write property test for property-value fallback
    - **Property 3: Property-value fallback**
    - **Validates: Requirements 3.5, 3.8**

  - [~] 9.4 Write property test for `by_type`
    - **Property 4: `by_type` matches a reference model**
    - **Validates: Requirements 3.6, 3.8**

  - [~] 9.5 Write property test for pagination
    - **Property 5: Pagination agrees with `paginate`**
    - **Validates: Requirements 3.12**

  - [~] 9.6 Write property test for capability-first scoping
    - **Property 6: Capability-first and capability scoping**
    - **Validates: Requirements 4.1, 4.2, 5.5, 7.7**

  - [~] 9.7 Write property test for list helpers
    - **Property 7: List helpers are complete and correctly keyed**
    - **Validates: Requirements 5.1, 5.2**

  - [~] 9.8 Write property test for `get_capability`
    - **Property 8: `get_capability` is position-independent**
    - **Validates: Requirements 5.3, 5.4**

- [~] 10. Final checkpoint - Full suite, format, commit
  - Run `hatch run tests` (mypy, pytest, coverage, bandit) and `hatch fmt`; ensure zero failures, zero mypy errors, no new bandit findings, and a clean tree after formatting. Ask the user if questions arise.
  - Commit only the files changed in task 9 plus the updated `tasks.md`: `test: add Fixture_Store property tests for cdef tools (#8) - tests passing`
  - Do not push or open a PR without explicit user approval.
  - _Requirements: 1.4, 7.7, 7.8_

## Notes

- Tasks marked with `*` (9.3–9.8) are optional property-based tests and can be skipped for a faster MVP. Example-based tests in 2.3 and 7.1–7.3 are required; they cover Requirement 7, including the >100-capability lookup (7.4) and the unset-store `RuntimeError` (7.5). Property tests 9.1 and 9.2, plus the strategy setup in task 9, are required because they satisfy Requirement 7.7 (CDef_Filter scoping), replacing the legacy tests deleted in 7.4.
- Task 1.1 uses the existing issue #8 in `dfkunstler/mcp-server-for-oscal`; always pass `-R dfkunstler/mcp-server-for-oscal` to `gh` and do not set a default repo. Never commit to `main`; never push without explicit approval.
- Tasks 5.1–5.5 all edit `query_component_definition.py` and run sequentially. Existing CDef_Tools tests are expected to fail between 5.5 and 7.4, so there is no checkpoint in that span.
- Tasks 6.1 and 6.2 both edit `main.py` and sit in different waves. Task 6.4 (required) runs after 5.5 because its source scan expects CDef_Tools to no longer reference `component_definitions_dir`. Requirement 8 has no property test by design; 6.4 covers it with examples.
- Run shell commands with the `cwd` parameter, never with a `cd` prefix.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["2.2", "3.2"] },
    { "id": 3, "tasks": ["2.3", "5.1", "6.1"] },
    { "id": 4, "tasks": ["5.2", "6.2", "6.3"] },
    { "id": 5, "tasks": ["5.3"] },
    { "id": 6, "tasks": ["5.4"] },
    { "id": 7, "tasks": ["5.5"] },
    { "id": 8, "tasks": ["7.1", "7.4", "6.4"] },
    { "id": 9, "tasks": ["7.2", "9.1"] },
    { "id": 10, "tasks": ["7.3", "9.2"] },
    { "id": 11, "tasks": ["9.3"] },
    { "id": 12, "tasks": ["9.4"] },
    { "id": 13, "tasks": ["9.5"] },
    { "id": 14, "tasks": ["9.6"] },
    { "id": 15, "tasks": ["9.7"] },
    { "id": 16, "tasks": ["9.8"] }
  ]
}
```
