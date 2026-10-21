# Implementation Plan: Upgrade oscal-bindings to 0.3.1

## Overview

Set up the issue and feature branch, capture a pre-upgrade baseline, bump and re-lock the dependency, audit and fix source and tests for 0.3.x, add regression guard tests, update the guidance docs, rebuild the bundled DB if needed, and commit.

Rules for every task:
- Run all Python through hatch only (`hatch test`, `hatch run ...`, `hatch run python ...`). Put pytest flags after `--`. Never call `python`, `pytest`, `mypy`, `ruff`, or `bandit` directly.
- Never prefix shell commands with `cd`. Use the tool's working-directory (`cwd`) parameter. Restate this to any subagent.
- Stage only files changed for this feature. Never commit to `main`. Never push without explicit user approval.
- Commit messages include the issue number (`#<n>`) and state whether tests passed.

## Tasks

- [x] 1. Issue, branch, and spec commit
  - [x] 1.1 Find or create the GitHub issue and feature branch (requires user approval)
    - Run `gh issue list --search "oscal-bindings"` (and `--state all`) to find a related issue
    - If none exists, draft an issue (title like "Upgrade oscal-bindings to 0.3.1", body summarizing 0.2.0/0.3.0 changes, upstream #5 resolved, #6 caveat) and ask the user to approve before `gh issue create`
    - After approval, create and check out the branch with `gh issue develop <n> --checkout`
    - Confirm `git branch --show-current` is not `main`
    - _Requirements: 1.1_
  - [x] 1.2 Commit the spec files as the first commit on the feature branch
    - Stage only `.kiro/specs/upgrade-oscal-bindings-0-3-1/` (`.config.kiro`, `requirements.md`, `design.md`, `tasks.md`)
    - Message: `Add spec for oscal-bindings 0.3.1 upgrade (#<n>) [spec only, no code tested]`
    - _Requirements: 1.1_

- [x] 2. Capture pre-upgrade baseline on 0.1.0
  - [x] 2.1 Record child extraction and DB baseline
    - With `hatch run python -c` (or a temp script outside `src/` and `tests/`), scan `tests/fixtures/` and `data/` with `OscalStore`, and write sorted (model_type, doc uuid, element_type, uuid, title) tuples plus per-model-type document counts to a temp file outside the repo
    - Record `hatch run python -c "import oscal_bindings; print(oscal_bindings.__version__, oscal_bindings.__oscal_schema_version__)"`
    - _Requirements: 4.1, 7.2_

- [x] 3. Bump and re-lock the dependency
  - [x] 3.1 Pin `oscal-bindings==0.3.1` and re-lock
    - Edit `pyproject.toml` line 14 to `"oscal-bindings==0.3.1"`
    - Run `hatch run update`; review the `requirements.txt` diff; if anything beyond oscal-bindings and its required transitive bumps (e.g. pydantic, pydantic-core) changed, show the diff to the user before continuing
    - If the `default` env does not pick up the new pin, run `hatch env remove default` once
    - Confirm the installed version with `hatch run python -c "import oscal_bindings; print(oscal_bindings.__version__, oscal_bindings.__oscal_schema_version__)"` (expect 0.3.1 / 1.2.3)
    - _Requirements: 1.1, 1.2, 1.3, 2.1_

- [x] 4. Audit and fix source for 0.3.x
  - [x] 4.1 Audit `src/` against the design checklist and fix breakage
    - Run `hatch run typing` and `hatch test -- -x -q` to surface failures
    - Apply the design's audit checklist (renamed classes, repurposed names, enum-as-str, merge combine/flat, mappings shape, union variant order, `get_supported_element_types`) to `tools/utils.py`, `tools/oscal_store.py`, `tools/validate_oscal_content.py`, `tools/query_oscal_models.py`, `tools/query_component_definition.py`
    - Fix only what 0.3.1 breaks: update renamed imports; compare closed-enum fields by `.value`
    - Keep explicit `by_alias=True, exclude_none=True` kwargs; leave the direct `ComponentDefinition` import in `query_component_definition.py` (note it as a follow-up)
    - Confirm no `model_validate_json` or `parse_*` usage in `src/`
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_
  - [ ]* 4.2 Write AST source-scan test
    - In `tests/test_oscal_bindings_upgrade.py`, assert no `model_validate_json` attribute call and no `parse_*` import from `oscal_bindings` anywhere under `src/mcp_server_for_oscal/`
    - _Requirements: 3.5_

- [x] 5. Fix tests and Hypothesis strategies
  - [x] 5.1 Update test imports, strategies, and brittle assertions
    - Fix any test import of a renamed Bindings name
    - Introspect 0.3.1 `model_fields` for closed-enum (Enum subclass) fields and cross-check every strategy that builds OSCAL docs (`tests/test_oscal_models.py` `_BUILDERS`/`_MUTATIONS`, `tests/test_properties.py`, fixture builders in `conftest.py`/`fixture_store.py`)
    - Replace free-text values for closed-enum fields with `st.sampled_from([m.value for m in <Enum>])` or valid literals, except in tests that target invalid input
    - Change tests asserting exact Bindings error text to assert level validity and field location
    - Update `CatalogGroupWithControls`/`CatalogGroupWithGroups` docstring mentions only if names changed
    - _Requirements: 5.4, 5.5, 4.6, 4.7_

- [x] 6. Checkpoint - Ensure all tests pass
  - Run `hatch run tests`; compare extraction tuples and document counts against the task 2.1 baseline. Ensure all tests pass, ask the user if questions arise.
  - _Requirements: 2.2, 4.1, 5.1_

- [ ] 7. Regression guard property tests
  - [ ]* 7.1 Write property test for child extraction completeness
    - **Property 1: Child extraction is complete**
    - **Validates: Requirements 4.1, 4.4, 4.5**
  - [ ]* 7.2 Write property test for child raw_json validity and round trip
    - **Property 2: Child raw_json is valid OSCAL and round-trips**
    - **Validates: Requirements 4.2, 4.3**
  - [ ]* 7.3 Write property test for Validator acceptance
    - **Property 3: Validator accepts model-valid documents**
    - **Validates: Requirements 4.6, 3.4**
  - [ ]* 7.4 Write property test for out-of-enum rejection
    - **Property 4: Out-of-enum values are rejected**
    - **Validates: Requirements 4.7**
  - [ ]* 7.5 Write property test for Python field name rejection
    - **Property 5: Python field names are rejected on the dict path**
    - **Validates: Requirements 4.8, 3.4**
  - [ ]* 7.6 Write property test for default vs explicit serialization
    - **Property 6: Default serialization equals explicit-flag serialization**
    - **Validates: Requirements 6.1, 6.5**

- [x] 8. Update guidance documentation
  - [x] 8.1 Revise `.kiro/steering/oscal-bindings.md`
    - Section 2: validate with `model_validate(dict)`; note upstream #6 (pydantic 2.13.x JSON path drops Python-field-name keys)
    - Section 3: keep catalog group class names, mention `CatalogGroup` alias and `MappingCollectionMappings`; closed enums hold Enum members, compare with `.value`
    - Section 4: models serialize to valid OSCAL by default since 0.2.0; explicit kwargs optional and harmless; keep "prefer stored `raw_json`" and "don't return Pydantic models from tools" with the updated rationale (MCP SDK/Strands serialization paths, Strands `repr` fallback outside Pydantic serialization)
    - Implementation Workflow: mark cairn-proofs/oscal-bindings-python#5 resolved in 0.2.0
    - Anti-patterns and Example: update per design
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_
  - [x] 8.2 Align `AGENTS.md` and related summaries
    - Update `AGENTS.md` line 101 to match section 4
    - Update matching serialization lines in `.agents/summary/*.md` and `src/mcp_server_for_oscal/tools/README.md` if present; change nothing else
    - _Requirements: 6.8_

- [x] 9. Rebuild bundled DB if needed
  - [x] 9.1 Run `hatch run build-db` when extraction code or `data/` parsing results changed
    - Compare per-model-type document counts with the task 2.1 baseline; if any document dropped (e.g. closed-enum rejection), stop and report the files to the user
    - The DB and its manifest are gitignored; do not stage them
    - Delete the task 2.1 temp baseline file afterwards
    - _Requirements: 7.1, 7.2_

- [x] 10. Final checkpoint and commit
  - [x] 10.1 Run quality gates and commit
    - Run `hatch run tests`, `hatch check fmt`, `hatch check code` (use `--fix` only for formatting/lint fixes, then re-run)
    - Stage only feature files (`pyproject.toml`, `requirements.txt`, changed `src/` and `tests/` files, `tests/test_oscal_bindings_upgrade.py`, guidance docs, `tasks.md`)
    - Commit on the feature branch: `Upgrade oscal-bindings to 0.3.1 (#<n>) [hatch run tests passing]` (state actual test status)
    - Do not push; ask the user for approval before any push or PR
    - _Requirements: 5.1, 5.2, 5.3_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster upgrade; 4.2 and 7.x guard against future regressions, especially upstream #6
- Task 2.1 must run before task 3.1 so the baseline reflects 0.1.0
- Keeping explicit serialization kwargs and the direct `ComponentDefinition` import is deliberate; see design Decisions

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2"] },
    { "id": 2, "tasks": ["2.1"] },
    { "id": 3, "tasks": ["3.1"] },
    { "id": 4, "tasks": ["4.1", "8.1"] },
    { "id": 5, "tasks": ["5.1", "8.2"] },
    { "id": 6, "tasks": ["4.2"] },
    { "id": 7, "tasks": ["7.1"] },
    { "id": 8, "tasks": ["7.2"] },
    { "id": 9, "tasks": ["7.3"] },
    { "id": 10, "tasks": ["7.4"] },
    { "id": 11, "tasks": ["7.5"] },
    { "id": 12, "tasks": ["7.6"] },
    { "id": 13, "tasks": ["9.1"] },
    { "id": 14, "tasks": ["10.1"] }
  ]
}
```
