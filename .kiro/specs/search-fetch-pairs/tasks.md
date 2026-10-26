# Implementation Plan: Search/Fetch Pairs

## Overview

GitHub issue #45. Follow-up issue for full-text fetch of KB hits: #47 (recorded by task 2.2).

The plan builds the pure query normalizer first, then the store changes that everything else depends on (Origin, Relative_Name, Doc_ID, schema upgrade, FTS cleanup, Control_Prose), then the search and fetch tools, then message hygiene, startup and build wiring, the Path_Leak_Test, and the sync targets. Each group ends at a checkpoint where `hatch run tests` passes and the work is committed to the feature branch. Existing tests that assert old shapes are updated inside the task that changes the shape, so the suite stays green at every checkpoint.

One ordering change from the issue's outline: the `utils` budget and line-range helpers (task 8) come before the text search changes (task 9), because `text_search` returns its page through `utils.budget_page()`.

Rules for every task:

- Run all Python through hatch: `hatch run tests` (full suite), `hatch test <path>::<test>` (targeted), `hatch run python <script>` (scripts). `hatch run python -c "..."` breaks on `{...}` (hatch context fields), so write probes as script files under `private/` and delete them afterwards.
- Pass pytest flags after `--` (`hatch test tests/tools/test_fts_query.py -- -x -k normalize`).
- Never prefix a shell command with `cd`. Use the tool's `cwd` parameter. Restate this rule and the hatch rule to any subagent.
- Stage only files created or changed for this feature, by name. Never `git add -A` or `git add .`.
- Never commit to `main`. Never push and never open a PR without explicit user approval.
- Every commit message references #45 and states whether `hatch run tests` passed.

## Tasks

- [x] 1. Git setup on a feature branch for #45
  - [x] 1.1 Check the current branch and create the feature branch from issue #45
    - Run `git status` and `git branch --show-current`. If uncommitted changes unrelated to this spec exist, stop and ask the user.
    - Create and check out the branch with `gh issue develop 45 --checkout` (base `main`). If `gh issue develop` is unavailable, use `git checkout -b 45-search-fetch-pairs main` and link the branch to #45 in a later comment.
    - Confirm `git branch --show-current` is not `main`.
    - _Requirements: 13.7_
  - [x] 1.2 Commit the spec files as the first commit on the feature branch
    - Run `hatch run tests` to record the baseline result (no source changes yet).
    - Stage only `.kiro/specs/search-fetch-pairs/requirements.md`, `design.md`, `tasks.md`, and `.config.kiro` by name.
    - Commit with a message such as `docs(spec): search/fetch pairs spec (#45); baseline hatch run tests <passed|failed>`. Do not push.
    - _Requirements: 13.7_

- [x] 2. Create the follow-up issue for full-text fetch of KB hits
  - [x] 2.1 Create the follow-up GitHub issue
    - Create it directly with `gh issue create --label enhancement --title "Full-text fetch for Knowledge Base documentation hits" --body-file <file>`; write the body to a file under `private/` (gitignored) and delete it afterwards. If the `enhancement` label does not exist, rerun without `--label`. No user review is needed.
    - The body must: reference #45; explain that after #45 KB_Hits from `query_oscal_documentation` are snippet-only (`title` and `snippet`, no `docId`) and cannot be passed to `get_oscal_documentation`; record the open question of whether a KB data source S3 object key can map to a bundled Relative_Name / Doc_ID (for example, the key minus a configured prefix, giving `doc_id("bundled", rel)`), or whether a future Origin such as `kb` (`doc_id("kb", <object key>)`, IDs only, resolved through the KB) is needed; and note design section 15 / Requirement 15.4 (the user's goal is full-text fetch of KB hits; the #45 design leaves room for it): a future KB `docId` is added through the existing `docId` key of Doc_Result_Items with no other shape change.
    - _Requirements: 15.1, 15.2, 15.4_
  - [x] 2.2 Link the follow-up issue from #45 and record its number
    - Run `gh issue comment 45 --body "Follow-up for full-text fetch of KB hits: #<new>"`.
    - Replace `#TBD` in this file's Overview with the new issue number.
    - _Requirements: 15.3_

- [x] 3. Record the baseline context budget
  - [x] 3.1 Measure `tools/list` and `SERVER_INSTRUCTIONS` before any code change
    - Write `private/measure_tools_list.py` that builds the server the way `tests/test_tool_budget.py::live_tools` does and prints the tool count, the compact JSON length of the `tools` array, the longest tool and parameter descriptions, and `len(SERVER_INSTRUCTIONS)`. Run it with `hatch run python private/measure_tools_list.py`. Keep the script until task 20.1.
    - Record the numbers in the Notes section of this file (design expects 45 tools, 33,941 characters, `SERVER_INSTRUCTIONS` 1,795).
    - _Requirements: 13.2_

- [x] 4. Query normalization module
  - [x] 4.1 Implement `tools/fts_query.py`
    - Pure module, no SQLite import: `STOP_WORDS`, `MAX_SEARCH_TERMS = 16`, `SEARCH_COLUMNS = "{title description}"`, `_TERM`, `normalize_query`, `phrase`, `match_expressions`, `like_pattern`, as in design section 1.
    - Add example tests in `tests/tools/test_fts_query.py`: `back-matter`, `ac-2`, `ac-2.1`, `urn:x`, `NEAR(a b)`, `"profile resolution" AND (x`, all-stop-word queries, the `MAX_SEARCH_TERMS` cap, `like_pattern` escaping, non-`str` input raises `ValueError`.
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.8_
  - [x]* 4.2 Write property test for normalization output (pure part)
    - **Property 3: Query normalization never yields FTS5 syntax** (pure part: every term matches `_TERM` exactly; both `match_expressions` run against an in-memory FTS5 table with the store's DDL without `sqlite3.OperationalError`)
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.8**
  - [x]* 4.3 Write property test for stop-word handling
    - **Property 4: Stop words are dropped only when another word remains**
    - **Validates: Requirements 8.4**

- [x] 5. Store provenance, schema upgrade, and FTS cleanup
  - [x] 5.1 Add Origin, Relative_Name, and Doc_ID to ingestion
    - In `tools/oscal_store.py`: `Origin`, `ORIGIN_BUNDLED`, `ORIGIN_USER`, `ORIGINS`, `DOC_ID_NAMESPACE`, `relative_name()`, `zip_member_name()`, `doc_id()` (design section 3).
    - New `documents` columns `origin` and `relative_name` (`TEXT NOT NULL DEFAULT ''`) and `idx_documents_origin` in the fresh `CREATE TABLE`.
    - `scan_directory(directory, model_type_filter=None, *, origin=ORIGIN_USER)`: reject an unknown Origin with `ValueError` before reading files; pass `root` and `origin` to the JSON, zip, and markdown processors.
    - Replace `_file_unchanged` with `_unchanged_row`; add `_refresh_provenance` (Doc_ID collision: warn and update Origin and Relative_Name only); `_upsert_document` writes `origin` and `relative_name`; `_process_markdown_file` uses `doc_id(origin, rel)`. OSCAL documents keep their content `uuid`.
    - Add `count_documents(*, origin)`. Keep `count_documents_under` until task 15.1 removes its last caller.
    - Update tests in `tests/tools/test_oscal_store.py` that assert uuid5-of-path documentation IDs.
    - _Requirements: 1.1, 1.2, 1.5, 1.6, 2.1, 2.2, 2.3, 2.5_
  - [x] 5.2 Implement the in-place schema upgrade
    - `SCHEMA_VERSION = 1`; `_init_schema()` calls `_migrate()`: `PRAGMA user_version` check, `BEGIN IMMEDIATE`, re-check, `_add_column_if_missing` (via `PRAGMA table_info`), `_backfill_provenance` with pure `legacy_provenance(file_path)` (D4 markers, zip members, `\` separators), `_rekey_documentation` (duplicate Doc_IDs keep the lower `id` and delete the other row and its FTS row), `indexed = 0` for catalogs, `_delete_orphan_fts_rows`, then `user_version = 1`. On `sqlite3.Error`: rollback, log with the DB path, raise `RuntimeError("Failed to upgrade the OSCAL store schema")`.
    - Opening an already-upgraded DB (including the bundled DB) performs no writes.
    - _Requirements: 2.4, 14.2, 14.3, 4.2_
  - [x] 5.3 Delete a document's FTS rows in `_ensure_indexed` before reinserting
    - In the existing transaction, delete the `child_element` FTS rows of the children about to be deleted and the document's own `document` FTS row (design section 4, D3). Store `child.get("description") or ""` in FTS.
    - _Requirements: 7.2, 14.3_
  - [x]* 5.4 Write property test for ingestion provenance
    - **Property 1: Every ingested row records the scan's Origin and Relative_Name**
    - File `tests/tools/test_oscal_store_provenance.py`
    - **Validates: Requirements 1.1, 1.2, 1.5, 1.6**
  - [x]* 5.5 Write property test for Doc_ID stability
    - **Property 2: Doc_IDs depend only on Origin and Relative_Name**
    - File `tests/tools/test_oscal_store_provenance.py`
    - **Validates: Requirements 2.1, 2.2, 2.3**
  - [x]* 5.6 Write property test for the schema upgrade
    - **Property 19: Pre-feature DBs upgrade in place, once**
    - File `tests/tools/test_oscal_store_migration.py`; build legacy DBs with the pre-feature DDL and raw `INSERT`s, not `OscalStore` code.
    - **Validates: Requirements 2.4, 14.2, 14.3**
  - [x] 5.7 Write unit tests for provenance and migration
    - `scan_directory` rejects an unknown Origin; `count_documents(origin=...)`; a legacy markdown row with a path-derived UUID is re-keyed on rescan; `legacy_provenance` examples for each marker, zip members, and Windows separators; re-indexing a document leaves no duplicate or orphan FTS rows; a failed upgrade raises the fixed `RuntimeError` text.
    - Files `tests/tools/test_oscal_store_provenance.py` and `tests/tools/test_oscal_store_migration.py`.
    - _Requirements: 1.2, 2.4, 4.2, 14.2, 14.3_

- [x] 6. Catalog Control_Prose
  - [x] 6.1 Extract Control_Prose into control descriptions
    - `PROSE_PART_NAMES`, `control_prose`, `_collect_prose`, `_as_list`, `_token` in `tools/oscal_store.py` (design section 4), working on the oscal-bindings `Control`/`Part` models from `MODEL_MAP[OSCALModelType.CATALOG]`.
    - Set `description=control_prose(ctrl)` for top-level controls and in `_extract_controls_from_groups` at every level, for both group classes.
    - Update existing tests that assert title-only control descriptions.
    - _Requirements: 9.1, 9.2, 9.3, 9.4_
  - [x]* 6.2 Write property test for Control_Prose
    - **Property 10: Control descriptions equal Control_Prose at every level**
    - File `tests/tools/test_control_prose.py`; the oracle walks the JSON dict, not the models.
    - **Validates: Requirements 9.1, 9.2, 9.3, 9.4**

- [x] 7. Checkpoint: store foundation
  - Run `hatch run tests`. Ensure all tests pass, ask the user if questions arise.
  - Commit the files changed in tasks 4-6 (and this file) by name, for example `feat(store): FTS query normalization, provenance, schema upgrade, control prose (#45); hatch run tests passed`.

- [x] 8. Shared response-budget and line-range helpers in `utils`
  - [x] 8.1 Add `utils.budget_page()` and make `paginate_within_budget` use it
    - `budget_page(items, total, offset, *, budget=RESPONSE_BUDGET, truncatable=("description",), extra=None)`: longest fitting prefix, `limit` = items returned, `hasMore` recomputed, at least one item (truncated if needed). `paginate_within_budget` slices and then calls it; its output is unchanged.
    - _Requirements: 7.5_
  - [x] 8.2 Move the line-range helpers to `utils` and refactor `get_metaschema_source`
    - `MAX_LINE_COUNT = 500`, `check_line_range(start_line, line_count, total, *, noun="file")`, `lines_within_budget(base, lines, start, line_count, *, budget=RESPONSE_BUDGET)` from `get_metaschema_source._check_line_count`, `_check_start_line`, `_within_budget`, `_response`.
    - Add `START_LINE_DOC`, `LINE_COUNT_DOC`, `StartLine`, `LineCount` to `tools/descriptions.py`; switch `get_metaschema_source` to them (parameter names unchanged).
    - `get_metaschema_source` output stays byte-identical; its existing tests run unchanged.
    - _Requirements: 12.7, 12.8, 12.9, 13.1, 13.3_
  - [x] 8.3 Write unit tests for the new helpers
    - `budget_page`: fits budget, `hasMore`, single over-budget item truncated, `extra` keys. `check_line_range` messages name the parameter and range with `noun`. `lines_within_budget`: whole lines, one over-budget line cut at a code-point boundary with `TRUNCATION_MARK` and `truncated: true`.
    - Files `tests/test_utils.py` and `tests/test_paginate_within_budget.py`. Run the existing `get_metaschema_source` tests as the regression check.
    - _Requirements: 7.5, 12.7, 12.8, 12.9_

- [x] 9. Text search: resolvable, scoped, ranked, bounded
  - [x] 9.1 Rewrite `OscalStore.text_search`
    - `normalize_query`; empty terms → empty Page_Response with no SQL. `_index_pending_oscal_documents()` (warn with the Document_UUID and continue on failure). The ranked query of design section 5: `entity_type IN ('document', 'child_element')`, optional `model_type`, join to `child_elements`/`documents` for Element_ID, Document_UUID, and `parentDocumentUuid`, orphans dropped, `ORDER BY all_terms DESC, rank, rowid`, `total` by `COUNT(*)`. Values bound; `# nosec B608` on composed fixed fragments.
    - Items: `entity_type`, `entity_id`, `title`, `description` (500 characters + `TRUNCATION_MARK`), `model_type`, `parentDocumentUuid` on child hits only; no row ids. Return through `budget_page(..., truncatable=("description",))`.
    - LIKE fallback on `sqlite3.OperationalError`: warn with the normalized terms; ORed `LIKE ... ESCAPE '\'` per term; order by terms matched, title, rowid; same item shape.
    - `_query_by_title_fts`: normalized ANDed `{title}` phrases, `entity_type`/`model_type` as SQL filters.
    - Update tests in `tests/tools/test_oscal_store.py` and `tests/test_integration.py` that assert row-id `entity_id` values or documentation hits.
    - _Requirements: 6.1, 6.2, 6.5, 6.6, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 8.1, 8.2, 8.3, 8.5, 8.6, 8.8, 8.9_
  - [x] 9.2 Update the `text_search_oscal` tool
    - `limit: BudgetedLimit = 10` (name and range unchanged) and the new description from design section 14 (≤ 180 characters, Description_Shape).
    - _Requirements: 7.7, 13.1, 13.2, 13.3_
  - [x] 9.3 Write the resolvability property test (required)
    - **Property 6: Text search hits resolve through the fetch tools**
    - File `tests/tools/test_text_search.py`; docs of every `OSCALModelType`; child hits through `get_child_element(entity_id, parentDocumentUuid)`, document hits through the per-model query `by_uuid`.
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4**
  - [x]* 9.4 Write property test for normalization against the store (text search part)
    - **Property 3: Query normalization never yields FTS5 syntax** (store part for `text_search`: no `OperationalError`; empty page with `total` 0 exactly when the term tuple is empty)
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.8**
  - [x]* 9.5 Write property test for ranking against a per-term oracle
    - **Property 5: Search results equal a per-term oracle, all-terms first**
    - **Validates: Requirements 8.5, 8.6, 8.7**
  - [x]* 9.6 Write property test for item shape and scope
    - **Property 7: Text search items have the documented shape and scope**
    - **Validates: Requirements 6.5, 6.6, 7.1, 7.6**
  - [x]* 9.7 Write property test for search freshness
    - **Property 8: Newly scanned documents are searchable without other calls**
    - **Validates: Requirements 7.2**
  - [x]* 9.8 Write property test for text search paging within budget
    - **Property 9: Text search pages fit the budget and cover every hit once**
    - **Validates: Requirements 7.4, 7.5**
  - [x] 9.9 Write unit tests for text search
    - Pending document with corrupt `raw_json` → warning and search continues (7.3); LIKE fallback with the FTS query patched to raise (8.9); `back-matter`, `ac-2`, `control-implementation`, `"profile resolution" AND (x` return results without `OperationalError` (8.2), and `ac-2` returns fixture control `ac-2` as a hit with its `parentDocumentUuid` (6.7, needs 9.10); a phrase only in a fixture control's guidance finds that control (9.6); `by_title` with a hyphenated title finds the document; against the bundled DB when present (skip otherwise), "how does profile resolution work" ⊇ "profile resolution" (8.7); the `text_search_oscal` `limit` schema uses the `BudgetedLimit` text (7.7).
    - File `tests/tools/test_text_search.py`.
    - _Requirements: 7.3, 7.7, 8.2, 8.7, 8.9, 9.6_
  - [x] 9.10 Index Element_IDs in FTS for ID search
    - Runs before 9.3-9.9 and 11.1 (dependency graph), which touch the same store and search code. Design D10, sections 1, 2, 4, 5.
    - FTS schema: append `element_id` as the last column of `fts_index` in `_init_schema` (fresh DBs). In `tools/fts_query.py`, `SEARCH_COLUMNS = "{title description element_id}"` and add `DOC_SEARCH_COLUMNS = "{title description}"`; `search_documentation` (and task 11.1's rewrite) passes `DOC_SEARCH_COLUMNS`. The `text_search` LIKE fallback also checks `element_id`.
    - `_ensure_indexed` writes `element_id`: the Element_ID for `child_element` rows, the Document_UUID for the `document` row. The markdown indexer writes `''` for `documentation` rows. Items still take identifiers from the D1 joins.
    - Migration: `SCHEMA_VERSION = 2`. `_migrate` reads the version once inside `BEGIN IMMEDIATE`: version 0 runs the existing v1 steps and then the FTS rebuild; version 1 runs only `_rebuild_fts_with_element_id` (create `fts_index_v2`, copy rows with their `rowid` and `element_id` from joins to `child_elements`/`documents`, `''` for documentation, drop `fts_index`, rename); the rebuild is skipped when `PRAGMA table_info(fts_index)` already lists `element_id`. A DB at version 2 is not written to. Failures keep the fixed `RuntimeError` text and roll back.
    - Update tests that embed the FTS DDL or assert `user_version == 1`: `tests/tools/test_fts_query_properties.py` `_FTS_DDL` → six-column DDL; `tests/tools/test_oscal_store_migration.py` keeps `LEGACY_DDL` as the v0 DDL, adds a v1 DDL, compares with `SCHEMA_VERSION`, and extends the Property 19 test to check `element_id` values after upgrade; any other literal `1` or five-column assertion (task 16.2 will expect `SCHEMA_VERSION`).
    - Unit tests: `ac-2` finds fixture control `ac-2` (title "Account Management", no "AC-2" in title or prose) with `entity_id` `ac-2` and the catalog's `parentDocumentUuid`; a Document_UUID search finds that document hit; reopening a v2 DB leaves a full DB dump unchanged; a v1 DB upgrades to 2 with `element_id` filled and `documents`/`child_elements` unchanged; documentation search results are unchanged by the new column. Files `tests/tools/test_text_search.py` and `tests/tools/test_oscal_store_migration.py`.
    - _Requirements: 6.7, 14.2, 14.3, 14.6_

- [x] 10. Remove `file_path` from per-model query results
  - [x] 10.1 Stop selecting and returning `file_path`
    - `_query_by_uuid`, `_query_by_title`, `_query_by_title_fts`, `_query_all`, and `_build_query_items` in `tools/oscal_store.py` (design section 7). Update existing tests that expect `file_path`.
    - _Requirements: 3.1, 3.2_
  - [x] 10.2 Write unit tests for per-model items
    - Every `query_<model>` item lacks `file_path`; `query_component_definition` results are unchanged.
    - File `tests/tools/test_query_oscal_models.py`.
    - _Requirements: 3.2_

- [x] 11. Documentation search: store, KB normalization, merge, and paging
  - [x] 11.1 Rewrite `OscalStore.search_documentation` and add `get_documentation`
    - `search_documentation(query_text, offset=0, limit=10, *, origins=ORIGINS)`: same normalization and two-group ordering over `entity_type = 'documentation'`, joined to `documents` filtered by `origin IN (...)`; items `{"docId", "title", "snippet"}` with `snippet(...)` cut to 200 characters (first 200 description characters for title-only matches); LIKE fallback with `_extract_like_snippet`; no `source` key; no internal `limit` cap.
    - `DocumentationText` dataclass and `get_documentation(doc_id)` (exact `uuid` match with `model_type = 'documentation'`).
    - _Requirements: 3.3, 8.1, 8.5, 8.6, 10.3, 11.2, 11.7, 12.4, 12.6_
  - [x] 11.2 Rewrite `query_oscal_documentation`
    - In `tools/query_documentation.py`: `KB_MAX_RESULTS`, `KB_FAILED_MESSAGE`, `UNKNOWN_DOC_ID_MESSAGE`; signature `query`, `offset: Offset = 0`, `limit: BudgetedLimit = 10`, `ctx` last; new description (design section 8).
    - Flow: `check_page` (notify and raise on error); blank query → empty page with no KB call; Local_Mode over both Origins; KB_Mode with `numberOfResults=KB_MAX_RESULTS`, `kb_result_items`, user-only local prefix of `offset + limit`, `merge_page`; KB failure → warning log with `exc_info`, `KB_FAILED_MESSAGE` as a warning, Local_Mode over both Origins; every page through `budget_page(..., truncatable=("snippet",))`.
    - Pure helpers `kb_result_items`, `kb_title` (D6), `interleave`, `merge_page`. Remove `query_kb`'s raw response and its path-echoing error.
    - Update `tests/tools/test_query_documentation.py`, `tests/tools/test_local_doc_search.py`, and `tests/conftest.py` users of `sample_bedrock_response` for the new shape.
    - _Requirements: 3.1, 3.3, 3.4, 4.3, 10.1, 10.2, 10.4, 10.5, 10.6, 10.7, 11.1, 11.3, 11.4, 11.5, 11.6, 11.8, 11.9, 13.1, 15.4_
  - [x]* 11.3 Write property test for Doc_Result_Item shape
    - **Property 11: Doc_Result_Items have one shape and carry no KB location data**
    - File `tests/tools/test_query_documentation_results.py`
    - **Validates: Requirements 3.3, 3.4, 10.2, 10.3, 10.4, 10.5**
  - [x]* 11.4 Write property test for merged paging
    - **Property 12: Merged pages walk the interleaved sequence exactly once**
    - File `tests/tools/test_query_documentation_results.py`
    - **Validates: Requirements 11.3, 11.4, 11.5, 11.6**
  - [x]* 11.5 Write property test for Origin scope per mode
    - **Property 13: Each mode searches the right Origins**
    - File `tests/tools/test_query_documentation_results.py`; stubbed `Session`, no network.
    - **Validates: Requirements 11.1, 11.2, 11.7, 11.9**
  - [x]* 11.6 Write property test for KB failure messages
    - **Property 18: Knowledge Base failures emit no location or ARN**
    - File `tests/tools/test_query_documentation_results.py`
    - **Validates: Requirements 4.3, 11.8**
  - [x]* 11.7 Write property test for normalization against the store (documentation part)
    - **Property 3: Query normalization never yields FTS5 syntax** (store part for `search_documentation`)
    - File `tests/tools/test_local_doc_search.py`
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.8**
  - [x] 11.8 Write unit tests for `query_oscal_documentation`
    - Blank query returns an empty page with no KB call (10.7); KB request uses `numberOfResults=100` (11.5); `offset`/`limit` schema uses the shared `Offset`/`BudgetedLimit` descriptions (10.1); out-of-range `offset`/`limit` messages name the parameter and range (10.6); a user document with a unique token is returned in both modes (11.9).
    - File `tests/tools/test_query_documentation.py`.
    - _Requirements: 10.1, 10.6, 10.7, 11.5, 11.9_

- [x] 12. Documentation fetch tool
  - [x] 12.1 Implement and register `get_oscal_documentation`
    - In `tools/query_documentation.py` (D9, shares `_store`): `doc_id`, `start_line: StartLine = 1`, `line_count: LineCount = 200`, `ctx` last; description from design section 8 (≤ 250 characters). Unknown or non-`str` `doc_id` → notify and raise `ValueError(UNKNOWN_DOC_ID_MESSAGE)` with no echo; then `check_line_range(..., noun="document")`; then `lines_within_budget({"docId", "title"}, ...)`.
    - Add it to `get_tool_list()` and update the exact tool sets and counts (45 → 46) in `tests/test_tool_registry.py` and `tests/tools/test_query_oscal_models_child_elements.py` so the suite stays green.
    - _Requirements: 4.5, 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8, 12.9, 12.10, 13.1, 13.2_
  - [x]* 12.2 Write property test for document paging
    - **Property 14: Paging a document reconstructs its text within budget**
    - File `tests/tools/test_get_oscal_documentation.py`
    - **Validates: Requirements 12.3, 12.8, 12.9, 12.10**
  - [x]* 12.3 Write property test for unknown Doc_IDs
    - **Property 15: Unknown Doc_IDs get one fixed, non-echoing error**
    - File `tests/tools/test_get_oscal_documentation.py`
    - **Validates: Requirements 4.5, 12.5, 12.6**
  - [x]* 12.4 Write property test for out-of-range paging arguments
    - **Property 16: Out-of-range paging arguments name the parameter and range**
    - File `tests/tools/test_get_oscal_documentation.py`; covers both `query_oscal_documentation` and `get_oscal_documentation`.
    - **Validates: Requirements 10.6, 12.7**
  - [x] 12.5 Write unit tests for `get_oscal_documentation`
    - Fetch a `bundled` and a `user` document by Doc_ID; an OSCAL Document_UUID gets the unknown-ID error; the response keys match design ("docId", "title", "start-line", "end-line", "total-lines", "has-more", "text"); store not initialized raises the fixed `RuntimeError`.
    - File `tests/tools/test_get_oscal_documentation.py`.
    - _Requirements: 12.3, 12.5, 12.6_

- [x] 13. Checkpoint: search and fetch pairs
  - Run `hatch run tests`. Ensure all tests pass, ask the user if questions arise.
  - Commit the files changed in tasks 8-12 (and this file) by name, for example `feat(tools): resolvable text search, documentation search paging, get_oscal_documentation (#45); hatch run tests passed`.

- [x] 14. Client-facing message hygiene
  - [x] 14.1 Add the redactor to `try_notify_client_error`
    - `utils.redact_storage_locations(text)` (D8): `s3://` URIs, `arn:aws…` ARNs, home, temp, and package directories (native, POSIX, realpath) → `<redacted>`. `try_notify_client_error` calls `safe_log_mcp(redact_storage_locations(msg), ctx, "error")`.
    - _Requirements: 4.1, 4.6_
  - [x] 14.2 Remove paths from `validate_oscal_content` messages
    - `OSCAL_CLI_INPUT_PLACEHOLDER = "<input.json>"` and `scrub_cli_output(text, tmp_path, cli_path)` applied to every oscal-cli error and warning line and to the execution-error text. `validate_oscal_file`: catch `OSError` on local reads (`ValueError("Cannot read the OSCAL file named by file_uri.")`, detail logged); drop the echoed URI from remote-disabled and timeout messages; `RequestException` → fixed text plus status code. Schema and model load failures → `f"Failed to load the bundled schema for {model_type}"`, detail logged.
    - Update existing assertions on old message text in `tests/tools/test_validate_oscal_content.py`.
    - _Requirements: 4.1, 4.2, 4.4, 4.6_
  - [x]* 14.3 Write property test for oscal-cli output scrubbing
    - **Property 17: oscal-cli output never shows the temporary file path**
    - File `tests/tools/test_validate_oscal_content.py`; `shutil.which` and `subprocess.run` patched.
    - **Validates: Requirements 4.4**
  - [x] 14.4 Fix `load_external_component_definition` provenance and messages
    - Local zips scanned with `origin=ORIGIN_USER` and `root=zip_path.parent`; remote documents get `origin=ORIGIN_USER` and the URL path's last segment as Relative_Name; client-facing messages use fixed text without `source` or the exception string.
    - _Requirements: 1.1, 4.1, 4.2_
  - [x] 14.5 Look up the awesome-oscal list by Origin and Relative_Name
    - `list_oscal_resources._read_from_store` selects `origin = 'bundled' AND relative_name = 'awesome-oscal.md'`; the error message becomes `"Failed to list OSCAL resources"` with detail logged.
    - _Requirements: 1.6, 4.1_
  - [x] 14.6 Write unit tests for message hygiene
    - `redact_storage_locations` examples (home, temp, package dir, `s3://`, ARN) in `tests/test_utils.py`; `validate_oscal_file` missing local file, remote disabled, and timeout messages contain no path or URI in `tests/tools/test_validate_oscal_content.py`; a `user` `awesome-oscal.md` does not shadow the bundled list in `tests/tools/test_list_oscal_resources.py`.
    - _Requirements: 4.1, 4.2, 4.4, 1.6_

- [x] 15. Server startup wiring
  - [x] 15.1 Update `main.py`
    - Add `_install_store(store)` (calls `init_store` on `list_oscal_resources`, `query_component_definition`, `query_documentation`, `query_oscal_models`) and use it in `_init_oscal_store`. Scan `OSCAL_DOCUMENTS_DIR` with `origin=ORIGIN_USER`; `_user_documents_loaded = store.count_documents(origin=ORIGIN_USER)`. Remove `OscalStore.count_documents_under` and its tests (`tests/tools/test_oscal_store.py::TestCountDocumentsUnder`); keep `tests/tools/test_local_doc_search.py::TestAboutUserDocumentsLoaded` passing.
    - _Requirements: 1.4, 1.6, 1.7, 14.4_
  - [x] 15.2 Write unit tests for startup wiring
    - `_init_oscal_store` with a temp `OSCAL_DOCUMENTS_DIR` records `user` rows and the `about` count; a persistent pre-feature DB with `OSCAL_DOCUMENTS_DIR` set gets `user` Origins and Requirement 2 Doc_IDs; `_install_store` sets every module singleton. Patch `mcp_server_for_oscal.main.config`, not env vars.
    - File `tests/test_main.py`.
    - _Requirements: 1.4, 1.7, 14.4_

- [x] 16. Bundled DB build
  - [x] 16.1 Record Origin `bundled` in `bin/build_oscal_db.py`
    - Both scans pass `origin=ORIGIN_BUNDLED`; `build_db` stats add `origins` and `prose_controls`.
    - _Requirements: 1.3, 9.5, 14.1_
  - [x] 16.2 Extend `tests/test_build_oscal_db.py`
    - A build from fixture directories (catalog with prose, cdefs, two markdown files) has only `bundled` Origins, Requirement 2 Doc_IDs, Control_Prose, and `user_version` 1.
    - _Requirements: 1.3, 9.5, 14.1, 14.5_
  - [x] 16.3 Rebuild the bundled DB and refresh manifests
    - Run `hatch run build-db`. Check with `git status` whether any tracked `hashes.json` changed; if so run `hatch run rehash` (it stages manifests with `git add`; unstage anything unrelated to this feature). The bundled `oscal_store.db` and its `hashes.json` are gitignored and are not committed.
    - Re-run `hatch test tests/tools/test_text_search.py` so the bundled-DB test for Requirement 8.7 runs against the new DB.
    - _Requirements: 9.5, 14.1_

- [x] 17. Checkpoint: hygiene and wiring
  - Run `hatch run tests`. Ensure all tests pass, ask the user if questions arise.
  - Commit the files changed in tasks 14-16 (and this file) by name, for example `fix(tools): path-free messages, origin-aware startup and build (#45); hatch run tests passed`.

- [x] 18. Path_Leak_Test
  - [x] 18.1 Add minimal model fixtures
    - `tests/fixtures/models/<model>.json`, one per `OSCALModelType`: model-valid, minimal, no paths in content; the catalog has nested groups and statement and guidance prose.
    - _Requirements: 5.1_
  - [x] 18.2 Add `build_leak_store(root)` to `tests/fixture_store.py`
    - Builds `<tmp>/bundled/` (model fixtures, `cdefs.zip`, `awesome-oscal.md`, `concepts/overview.md`, scanned with `origin="bundled"`) and `<tmp>/user-docs/` (the three valid cdef fixtures, `notes/user-guide.md`, `concepts/overview.md`, scanned with `origin="user"`) into `<tmp>/db/store.db`; returns a `LeakFixture` with the IDs the CALLS table needs.
    - _Requirements: 5.1, 2.3_
  - [x] 18.3 Write `tests/test_path_leak.py` (required)
    - Guard: one model fixture per `OSCALModelType`. Install the store with `main._install_store`; restore singletons afterwards. Call through `main.build_server()` + `main._setup_tools(server)` and an in-memory `Client(server)`; capture Client_Facing_Messages by patching every module binding of `safe_log_mcp`/`try_notify_client_error`.
    - `Call` dataclass and `CALLS` table with one entry per registered tool plus `about`, including `unknown` calls for identifying parameters and `kb="stub"`/`kb="fail"` calls for `query_oscal_documentation` (stub hits with `s3://leak-bucket/...` locations, metadata, and an ARN).
    - Tests: coverage (`set(CALLS)` equals live `tools/list` names; failure lists missing and stale tools); table shape (every tool has an `ok` call, every tool with an identifying parameter has an `unknown` call); no leaks over every `(tool, call)` against all needles (tmp root, DB path, both content dirs in native, POSIX, realpath, and JSON-escaped forms, `Path.home()` when longer than 3 characters, the package dir, `tempfile.gettempdir()`, `s3://`, `arn:aws`, stub location and metadata values), naming tool, call, and needle kind; Metaschema `source-file`/`file` values are members of `get_index().source_files()`.
    - _Requirements: 3.1, 3.4, 3.5, 4.1, 4.3, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

- [x] 19. Sync targets and documentation
  - [x] 19.1 Update the Routing_Guide
    - `ROUTING_GUIDE` in `tools/descriptions.py`: the two lines from design section 14 (`text_search_oscal` → `get_child_element` / `query_*` by_uuid; `query_oscal_documentation` → `get_oscal_documentation`), within `ROUTING_GUIDE_CAP`.
    - _Requirements: 13.5_
  - [x] 19.2 Finish the registry tests
    - In `tests/test_tool_registry.py`: a frozen snapshot of every pre-feature tool name and parameter is still present (13.3); `"documentation"` is not in `OSCALModelType`, `MODEL_MAP`, or any model-name enum (13.4); the Routing_Guide names both fetch steps (13.5). Confirm `tests/tools/test_query_oscal_models_child_elements.py` and `tests/test_tool_param_parity.py` pass unchanged for the new tool.
    - _Requirements: 13.1, 13.3, 13.4, 13.5_
  - [x] 19.3 Update `src/mcp_server_for_oscal/tools/README.md`
    - Add `get_oscal_documentation`; describe the new `text_search_oscal` and `query_oscal_documentation` result shapes, the removed `file_path`, and Origin / Doc_ID.
    - _Requirements: 13.6_
  - [x] 19.4 Update `README.md`, `AGENTS.md`, and related docs
    - `README.md` tool list and result shapes; `AGENTS.md` code map (`tools/fts_query.py`, `get_oscal_documentation` in `query_documentation.py`) and OscalStore essentials (Origin, Relative_Name, Doc_ID, `user_version`); `.agents/summary/interfaces.md` and `conf/powers/oscal/POWER.md` where they list tools or shapes. Keep the README first-line `mcp-name` comment.
    - _Requirements: 13.6_
  - [x] 19.5 Verify `bin/build_mcpb.py` and parity
    - `bin/build_mcpb.py` needs no code change (it reads `get_tool_list()` summaries); run `hatch test tests/test_build_mcpb.py tests/test_tool_param_parity.py tests/test_tool_budget.py` and fix any failure.
    - _Requirements: 13.1, 13.2, 13.6_

- [x] 20. Context budget after the change
  - [x] 20.1 Measure and record the final budget
    - Re-run `hatch run python private/measure_tools_list.py`. Record before/after numbers in the Notes section (design estimate ≈ 35,300 of 38,979; `SERVER_INSTRUCTIONS` ≈ 1,950 of 3,000). If over a cap, shorten descriptions or reuse aliases rather than raise a limit. Delete the script afterwards.
    - _Requirements: 13.2_

- [x] 21. Final checkpoint
  - Run `hatch run tests`. Ensure all tests pass, ask the user if questions arise.
  - Commit the files changed in tasks 18-20 (and this file) by name, for example `test(tools): Path_Leak_Test over every registered tool; docs and sync targets (#45); hatch run tests passed`.

- [x] 22. Report on #45
  - [x] 22.1 Comment on #45 with a short summary of what landed
    - `gh issue comment 45 --body-file <file>` (file under `private/`, deleted afterwards): the two pairs, the new tool, the changed shapes, Origin/Doc_ID and the schema upgrade, path hygiene and the Path_Leak_Test, the budget numbers, and the follow-up issue number.
    - Do not push the branch and do not open a PR. Ask the user for explicit approval before either.
    - _Requirements: 13.6, 15.3_

## Notes

- Tasks marked with `*` are optional property-based tests and can be skipped for a faster MVP. Property 6 (resolvability, 9.3) and the Path_Leak_Test (18.3) are required. Unit-test sub-tasks are required because they hold the only checks for several criteria.
- Property tests run at least 100 examples (`OSCAL_TEST_MAX_EXAMPLES` caps them on Windows CI) and carry the docstring tag `Feature: search-fetch-pairs, Property N: <title>`.
- Store-backed tests build real `OscalStore` instances in `tmp_path` with `seed_from_bundled=False`.
- Budget baseline (task 3.1, measured at 9cc4668, #44 included, with `hatch run python private/measure_tools_list.py`): 45 tools; `tools/list` 33,941 of 38,979 characters (headroom 5,038); longest tool description `get_constraints` 442 (cap 1,200; next `query_component_definition` 429, `find_references_to` 416); longest parameter description `get_constraints.target` 197 (cap 250); `SERVER_INSTRUCTIONS` 1,795 of 3,000. Matches the design.md section 14 baseline, so the final estimate (≈ 35,300; `SERVER_INSTRUCTIONS` ≈ 1,950) stands.
- Budget after (task 20.1, measured on ab70952 plus the uncommitted task 18–19 working tree, same script): 46 tools (+1, `get_oscal_documentation`); `tools/list` 34,938 of 38,979 characters (+997; headroom 4,041, down from 5,038; 362 under the ≈ 35,300 estimate); longest tool description still `get_constraints` 442 (cap 1,200; next `query_component_definition` 429, `find_references_to` 416, `get_metaschema_source` 369, `get_allowed_values` 362); longest parameter description still `get_constraints.target` 197 (cap 250); `SERVER_INSTRUCTIONS` 2,005 of 3,000 (+210; 55 over the ≈ 1,950 estimate, within cap). All caps hold, so no description was shortened and no limit was changed. `private/measure_tools_list.py` deleted.
- Task 9.10 was added during implementation (design D10, Requirement 6.7): task 9.1 showed that `text_search` cannot find a control by its ID. It also covers design Property 20 through its example tests. Schema version 2 supersedes the `user_version` 1 mentioned in earlier tasks (5.2, 16.2); those tasks compare with `SCHEMA_VERSION`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["2.1", "3.1"] },
    { "id": 3, "tasks": ["2.2"] },
    { "id": 4, "tasks": ["4.1"] },
    { "id": 5, "tasks": ["4.2", "5.1"] },
    { "id": 6, "tasks": ["4.3", "5.2"] },
    { "id": 7, "tasks": ["5.3"] },
    { "id": 8, "tasks": ["5.4", "6.1"] },
    { "id": 9, "tasks": ["5.5", "5.6", "6.2"] },
    { "id": 10, "tasks": ["5.7"] },
    { "id": 11, "tasks": ["8.1"] },
    { "id": 12, "tasks": ["8.2"] },
    { "id": 13, "tasks": ["8.3", "9.1"] },
    { "id": 14, "tasks": ["9.2", "10.1"] },
    { "id": 15, "tasks": ["9.10"] },
    { "id": 16, "tasks": ["9.3", "10.2", "11.1"] },
    { "id": 17, "tasks": ["9.4", "11.2"] },
    { "id": 18, "tasks": ["9.5", "11.3", "11.7", "12.1"] },
    { "id": 19, "tasks": ["9.6", "11.4", "12.2"] },
    { "id": 20, "tasks": ["9.7", "11.5", "12.3"] },
    { "id": 21, "tasks": ["9.8", "11.6", "12.4"] },
    { "id": 22, "tasks": ["9.9", "11.8", "12.5"] },
    { "id": 23, "tasks": ["14.1", "14.2", "14.4", "14.5"] },
    { "id": 24, "tasks": ["14.3", "15.1", "16.1"] },
    { "id": 25, "tasks": ["14.6", "15.2", "16.2"] },
    { "id": 26, "tasks": ["16.3"] },
    { "id": 27, "tasks": ["18.1", "18.2"] },
    { "id": 28, "tasks": ["18.3"] },
    { "id": 29, "tasks": ["19.1", "19.3", "19.4"] },
    { "id": 30, "tasks": ["19.2", "19.5"] },
    { "id": 31, "tasks": ["20.1"] },
    { "id": 32, "tasks": ["22.1"] }
  ]
}
```
