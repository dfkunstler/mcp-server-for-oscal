# Implementation Plan: Replace compliance-trestle with oscal-bindings

## Overview

Swap `compliance-trestle` for `oscal-bindings==0.1.0` in four stages, following the design's migration order: a one-time differential check with both libraries installed, the code swap (single `MODEL_MAP`, store, validator, cdef tools, logging, dependencies, DB rebuild), test updates and new tests, then docs/steering and verification greps. Checkpoints run the full suite and commit to branch `26-replace-trestle-with-oscal-bindings`.

## Rules for executors (apply to every task)

- Run all Python through hatch: `hatch test`, `hatch run tests`, `hatch run typing`, `hatch check fmt --fix`, `hatch check code --fix`, `hatch run update`, `hatch run build-db`, `hatch run python private/...`. Never call `python`, `pytest`, `mypy`, `ruff`, or `bandit` directly. Put pytest flags after `--` (for example `hatch test -- -x -k model`).
- Never prefix commands with `cd`. Set the working directory with the tool's `cwd` parameter. Restate this rule and the hatch rule to any subagent.
- Work only on branch `26-replace-trestle-with-oscal-bindings`. Never commit to `main`. Never push; pushing requires explicit user approval.
- Stage only files created or modified for this feature, by name. Do not stage `.mcp.json` or other unrelated files.
- Commit messages reference `#26` and state whether `hatch run tests` passed.
- Do not modify historical specs under `.kiro/specs/` other than `replace-trestle-with-oscal-bindings` (Req 11.6).

## Tasks

- [x] 0. Commit the spec files
  - [x] 0.1 Make the spec files the first commit on the feature branch
    - The spec files already exist: `.kiro/specs/replace-trestle-with-oscal-bindings/{.config.kiro,requirements.md,design.md,tasks.md}`. Do not regenerate them
    - Confirm the current branch is `26-replace-trestle-with-oscal-bindings`
    - Run `hatch run tests` (baseline; code is unchanged)
    - Stage only the four spec files and commit, for example `Add spec for replacing trestle with oscal-bindings (#26); tests pass (baseline)`
    - _Requirements: tracking for all requirements_

- [x] 1. Step 0: Differential check with both libraries installed
  - [x] 1.1 Add `oscal-bindings==0.1.0` alongside `compliance-trestle`
    - Add `oscal-bindings==0.1.0` to runtime dependencies in `pyproject.toml`; keep `compliance-trestle` for now
    - Run `hatch run update` to re-lock `requirements.txt`
    - Confirm import with `hatch run python -c "import oscal_bindings; print(oscal_bindings.__oscal_schema_version__)"`
    - _Requirements: 1.1_
  - [x] 1.2 Write and run the throwaway differential script
    - Create `private/trestle_diff.py` (`private/` is gitignored; never stage it). Run with `hatch run python private/trestle_diff.py`
    - Inputs: every document in the bundled DB (`raw_json`, `model_type`; run `hatch run build-db` first if the DB is missing), `tests/fixtures/*.json`, and JSON and zip members under `data/`
    - For each document compare Trestle class vs Bindings class on:
      1. accept/reject (Req 7.1–7.3)
      2. `OscalStore._extract_child_elements(model_type, parsed_model)` output (`uuid`, `title`, `element_type`, `description`); it is an instance method taking a parsed model, so one throwaway store instance serves both libraries. Check for attribute-name drift (silently empty children) and value-type drift (`RootModel` `str()` as `root='...'`, `AnyUrl` normalization)
      3. Stored_Element_JSON: for every child element, the JSON each library produces with `model_dump_json(exclude_none=True, by_alias=True)` equals the element's JSON in the source document (datetime-equivalent) (Req 6.7, 6.9)
    - Normalize datetime-only differences in child `raw_json` before comparing (Req 8.1 accepts these)
    - _Requirements: 6.7, 6.9, 7.1, 7.2, 7.3, 7.5_
  - [x] 1.3 Record results, delete the script, and gate on differences
    - Fill in the "Differential check results" section of this file: document counts per source and model type, accept/reject mismatches, child-row mismatches, Stored_Element_JSON vs source mismatches, and how each was resolved
    - Delete `private/trestle_diff.py`
    - If any non-datetime difference remains, STOP. Report it to the user and wait for a decision (fix in the extractor or accept explicitly). Do not start task 3 until resolved
    - _Requirements: 6.7, 6.9, 7.1, 7.2, 7.3_

- [-] 2. Checkpoint: differential check complete
  - Run `hatch run tests`; ensure all tests pass, ask the user if questions arise
  - Stage `pyproject.toml`, `requirements.txt`, and `tasks.md`; commit, for example `Add oscal-bindings alongside trestle and record differential check (#26); tests pass`

- [ ] 3. Step 1: Swap the library
  - [~] 3.1 Add `MODEL_MAP` to `tools/utils.py`
    - Import the eight classes from `oscal_bindings.models` and define `MODEL_MAP: Final[dict[OSCALModelType, type[BaseModel]]]` directly below `OSCALModelType`, as a plain `dict`
    - Check whether `oscal_bindings` exports a common model base class; use it as the value type if so, otherwise `pydantic.BaseModel`
    - No `importlib` lookup
    - _Requirements: 2.1, 2.2, 2.3_
  - [~] 3.2 Update `tools/oscal_store.py`
    - Remove `import importlib` and `TRESTLE_MODEL_MAP`; import `MODEL_MAP` from `tools.utils`
    - Rewrite `_do_parse` to use `MODEL_MAP` and `model_class.model_validate(root_data)`; new missing-class message `"No OSCAL model class for type ..."`; drop the class-load `RuntimeError` path
    - Rename `_validate_with_trestle` to `_validate_with_model` (update all 3 call sites); missing-class branch rejects with a warning (fail-closed); failure warning reads "OSCAL model validation failed"
    - Reword `cache_size`, `get_parsed_model*`, section comments, and log text to "parsed OSCAL model" with no library name. Leave LRU cache and `_ensure_indexed` logic unchanged
    - Apply any extractor fixes decided in task 1.3
    - Extractor fix from task 1.3: in the MAPPING branch of `_extract_child_elements`, add `mappings = getattr(mappings, "root", mappings)` before the list check, so the `Mappings` `RootModel` (array form) is unwrapped instead of raising `AttributeError` on `.uuid` (Req 7.3, 7.5)
    - `_child_dict` keeps the explicit `model_dump_json(exclude_none=True, by_alias=True)` arguments (Req 6.9)
    - _Requirements: 2.4, 2.6, 3.1, 5.1, 5.2, 5.3, 6.9, 7.3, 7.4, 7.5_
  - [~] 3.3 Update `tools/validate_oscal_content.py`
    - Remove `import importlib` and `_TRESTLE_MODEL_MAP`; import `MODEL_MAP` from `tools.utils`
    - Rename `_validate_trestle` to `_validate_model`; use `MODEL_MAP[model_type]` with `KeyError` → invalid level, error `"Failed to load OSCAL model class for '<type>': ..."`; call `model_cls.model_validate(inner)`; keep the 20-line error cap
    - Add `_POST_PARSE_LEVELS = ("json_schema", "model", "oscal_cli")` and use it in all three skip loops
    - Level 3 docstring line in both tools becomes `3. Model - Semantic checks via OSCAL Pydantic models`; pipeline comment `# -- Level 3: Model --`
    - _Requirements: 2.5, 2.6, 3.2, 4.1, 4.2, 4.3, 4.4, 4.5, 5.3, 7.1, 7.2_
  - [~] 3.4 Update `tools/query_component_definition.py`
    - `_find_capability` returns `dict | None`: look up via `_iter_children(..., include_raw_json=True)` and return `_raw(store, hit)`; return `None` when there is no hit or the result is `{}`
    - Capability query response uses that dict; `component_count = len(cap.get("incorporates-components", []))`
    - `_materialize_components` returns `[d for c in page if (d := _raw(store, c))]`
    - `_get_capability` returns the `_find_capability` dict directly; drop the compatibility docstring
    - `_raw` fallback keeps explicit `exclude_none=True, by_alias=True`
    - Import only `ComponentDefinition` from `oscal_bindings.models` (drop `Capability`/`DefinedComponent` if unused). No `oscal_dict()` and no `_wrap_capability`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.10, 6.11_
  - [~] 3.5 Remove the Trestle logger configuration and resolve Req 5.5
    - Remove `logging.getLogger("trestle").setLevel(config.log_level)` at `main.py` (2 sites) and `oscal_agent.py` (1 site)
    - Run `grep -rnE "getLogger|logging\.(debug|info|warning|error|exception)" "$(hatch env find default)"/lib/python3.12/site-packages/oscal_bindings` (path verified for this machine's hatch virtual env; if it differs, locate the package with `hatch run python -c "import oscal_bindings, os; print(os.path.dirname(oscal_bindings.__file__))"`)
    - If it finds a named logger, add `logging.getLogger("oscal_bindings").setLevel(config.log_level)` at the three former sites; otherwise add nothing
    - Record the grep output and decision in the "Req 5.5 logging check" section of this file
    - _Requirements: 5.4, 5.5_
  - [~] 3.6 Remove `compliance-trestle` and re-lock
    - Remove `compliance-trestle` from `pyproject.toml`; keep `oscal-bindings==0.1.0`
    - Run `hatch run update`; confirm `requirements.txt` pins `oscal-bindings==0.1.0` and has no `compliance-trestle` entry
    - Review the `requirements.txt` diff for packages dropped with Trestle that our code imports directly; add any such package to `pyproject.toml` explicitly
    - _Requirements: 1.1, 1.2, 1.3_
  - [~] 3.7 Rebuild the bundled DB
    - Run `hatch run build-db` so child `raw_json` reflects the new serialization (offset-preserving datetimes)
    - The DB and its manifest are gitignored build artifacts; do not stage them
    - _Requirements: 7.5, 8.1_

- [ ] 4. Update and add tests
  - [~] 4.1 Rewrite `TestValidateTrestle` as `TestValidateModel` in `tests/tools/test_validate_oscal_content.py`
    - Drop `importlib` patches; use `patch.dict(MODEL_MAP, {OSCALModelType.CATALOG: mock_cls})` from `mcp_server_for_oscal.tools.utils`
    - Valid: `mock_cls.model_validate` returns normally → `valid is True`, `level == "model"`
    - Parse error: `side_effect = Exception("a\nb\nc")` → `valid is False`, errors split into lines
    - Load failure: `patch.dict(MODEL_MAP, {}, clear=True)` → `errors[0].startswith("Failed to load OSCAL model class")`
    - Keep the mapping-collection test, renaming only the function under test
    - _Requirements: 2.5, 4.1, 4.4, 4.5, 10.2_
  - [~] 4.2 Update pipeline tests in `tests/tools/test_validate_oscal_content.py`
    - Patch targets change to `..._validate_model`; mocked returns use `"level": "model"`
    - Every level-name assertion expects `"model"`, including skipped-level cases
    - _Requirements: 4.1, 4.2, 10.2_
  - [~] 4.3 Update cdef expectations in `tests/tools/test_query_component_definition.py`
    - Update tests that expect snake_case / `model_dump` output from `get_capability`, the component query, or the capability query to expect the source OSCAL element dicts; reword the `oscal_dict()` comment
    - Switch model imports to `oscal_bindings.models`
    - _Requirements: 6.2, 6.3, 6.4, 10.1_
  - [~] 4.4 Update `tests/test_properties.py`
    - Import `ComponentDefinition` from `oscal_bindings.models`
    - Update tests that expect snake_case / `model_dump` output from `get_capability`, the component query, or the capability query to expect the source OSCAL element dicts
    - Docstrings: "Trestle-valid" → "model-valid", "Trestle models" → "OSCAL models"
    - _Requirements: 6.2, 6.3, 6.4, 10.1, 10.4_
  - [~] 4.5 Update remaining test imports and docstrings
    - In all other test files (for example `tests/tools/test_oscal_store.py`, `tests/fixture_store.py`, `tests/tools/test_nested_catalog_controls_bug.py`), change `from trestle.oscal.<x> import Y` to `from oscal_bindings.models import Y`; update `_validate_with_trestle` references to `_validate_with_model`
    - In `test_nested_catalog_controls_bug.py`, `Group2` → `CatalogGroupWithControls`, `Group1` → `CatalogGroupWithGroups`, and drop "Trestle" from docstrings
    - Replace any other `trestle` mention in tests with library-neutral wording
    - Regression test from task 1.3 in `tests/tools/test_oscal_store.py`: a mapping-collection with `mappings` as an array of two or more mappings indexes without error and yields one `mapping` child row per entry (Req 7.3, 7.5)
    - _Requirements: 7.3, 7.5, 10.1, 10.3, 10.4_
  - [~] 4.6 Remove UTC-normalization expectations
    - Run `hatch test` and inspect datetime-related failures
    - Grep `tests/` for `+00:00`, `timezone.utc`, `astimezone`, `utcoffset`, `tzinfo`, and `Z"`; change any assertion that expects UTC normalization to expect the source offset. Leave input fixtures alone
    - _Requirements: 8.1, 8.2_
  - [~] 4.7 Add the schema version guard and `about` check in `tests/test_utils.py`
    - Add `test_matches_oscal_bindings_schema_version` to `TestBundledOscalVersion`: `oscal_bindings.__oscal_schema_version__ == get_bundled_oscal_version()`
    - Extend the about-tool test to assert its key set contains no bindings-version field
    - _Requirements: 9.1, 9.2_
  - [~] 4.8 Add `MODEL_MAP` completeness tests in `tests/test_utils.py`
    - `set(MODEL_MAP) == set(OSCALModelType)`
    - Each value `is` the expected class from `oscal_bindings.models` (all eight)
    - _Requirements: 2.1, 2.2, 2.3_
  - [~] 4.14 Add cdef output example tests in `tests/tools/test_query_component_definition.py`
    - `component_count` with and without `incorporates-components`
    - `_raw` fallback with `raw_json` None yields hyphenated keys and no nulls
    - Capability query, component query, and `get_capability` results embedded in a minimal component-definition pass the level-2 JSON Schema check (`_validate_json_schema`)
    - Results contain no null at any depth and no snake_case keys where OSCAL uses hyphens
    - _Requirements: 6.5, 6.8, 6.10, 6.12, 6.13, 6.14, 6.15_
  - [~] 4.9 Write property test: typed parse returns the mapped class
    - **Property 1: Typed parse returns the mapped class**
    - New `tests/test_oscal_models.py`; reuse `_VALID_DOC_BUILDERS` from `tests/tools/test_oscal_store.py` (keyed by root-key string: catalog, component-definition, plan-of-action-and-milestones; map with `OSCALModelType(key)`) and existing cdef/catalog strategies; `type(OscalStore._do_parse(json.dumps(doc), t.value)) is MODEL_MAP[t]`
    - `@settings(max_examples=100)`; tag `# Feature: oscal-bindings migration (#26), Property 1: Typed parse returns the mapped class` (do not use the spec directory name in tags; it contains "trestle", which Req 10.4 forbids under `tests/`)
    - **Validates: Requirements 3.1, 2.4**
  - [~] 4.10 Write property test: store and validator agree with the acceptance oracle
    - **Property 2: Store and validator agree with the acceptance oracle**
    - In `tests/test_oscal_models.py`; generate a valid document, optionally apply one mutation (unknown root key, unknown `metadata` key, delete `uuid` or `metadata`); assert `_validate_with_model(d, t) == _validate_model(d, t)["valid"]` and both equal "no mutation applied"
    - `@settings(max_examples=100)`; tag comment as above with Property 2
    - **Validates: Requirements 7.1, 7.2, 7.3, 3.2, 2.5**
  - [~] 4.11 Write property test: datetime offsets are preserved
    - **Property 5: Datetime offsets are preserved**
    - In `tests/test_oscal_models.py`; offsets −14:00..+14:00 at minute granularity plus naive timestamps in a cdef's `metadata.last-modified`; parsed `metadata.last_modified.utcoffset()` equals the input offset
    - `@settings(max_examples=100)`; tag comment with Property 5
    - **Validates: Requirements 8.1**
  - [~] 4.12 Write property test: cdef tool output is the source element's OSCAL JSON
    - **Property 3: Cdef tool output is the source element's OSCAL JSON**
    - In `tests/test_properties.py`; reuse the cdef strategy; for any capability or component, the capability query, component query, and `get_capability` results each equal the element's source JSON dict (datetime-equivalent), contain no nulls and no snake_case keys, and pass level-2 JSON Schema validation when embedded in a minimal component-definition. The source dict is the library-independent oracle
    - `@settings(max_examples=100)`; tag comment with Property 3
    - **Validates: Requirements 6.2, 6.3, 6.4, 6.5, 6.7, 6.11, 6.12, 6.13, 6.14, 6.15**
  - [~] 4.13 Write property test: level names are stable
    - **Property 4: Level names are stable**
    - In `tests/tools/test_validate_oscal_content.py`; inputs: valid OSCAL JSON, mutated OSCAL JSON, objects with arbitrary root keys, non-object JSON, non-JSON text; `levels` names are `["well_formedness", "json_schema", "model", "oscal_cli"]`
    - `@settings(max_examples=100)`; tag comment with Property 4
    - **Validates: Requirements 4.1, 4.2, 10.2**

- [~] 5. Checkpoint: swap and tests complete
  - Run `hatch check fmt --fix`, `hatch check code --fix`, `hatch run typing`, then `hatch run tests` (Python 3.11 and 3.12, coverage, bandit); ensure all tests pass, ask the user if questions arise
  - Stage only the changed source, test, `pyproject.toml`, `requirements.txt`, and `tasks.md` files by name; commit, for example `Replace compliance-trestle with oscal-bindings (#26); tests pass`
  - _Requirements: 10.5_

- [ ] 6. Update docs and steering
  - [~] 6.1 Replace the trestle steering file
    - `git mv .kiro/steering/compliance-trestle.md .kiro/steering/oscal-bindings.md`, then rewrite: use `oscal_bindings.models` typed classes with `Model.model_validate(root_data)`; never use `parse_oscal()` or other union `parse_*` helpers; get classes from `MODEL_MAP` in `tools/utils.py`; models forbid extra fields; datetimes keep their offset; serialize with `model_dump`/`model_dump_json(by_alias=True, exclude_none=True)`; keep `oscal_bindings.__oscal_schema_version__` in step with bundled schemas (guard test enforces it)
    - _Requirements: 11.1, 11.2_
  - [~] 6.2 Update `AGENTS.md`
    - Rules table points to `oscal-bindings.md`; remove the "model map is duplicated" gotcha; describe `MODEL_MAP` in `tools/utils.py` as the single source of model classes; validation levels read "JSON → JSON Schema → model → oscal-cli"; schema bumps also bump `oscal-bindings`
    - _Requirements: 11.3, 11.4, 11.5_
  - [~] 6.3 Update `src/mcp_server_for_oscal/tools/README.md`
    - Level 3 named `"model"` in the tools table, level table, and notes; remove the "mapping-collection skips Level 3" note; list `oscal-bindings` as the model library; describe `MODEL_MAP` as the single source
    - This file is under `src/`, so it must contain no "trestle" string at all, not even a historical "replaces Trestle" note (Req 10.4)
    - _Requirements: 4.6, 10.4, 11.3, 11.5_
  - [~] 6.4 Update remaining project docs and steering
    - `.kiro/steering/tech.md`, `.agents/summary/*`, `conf/powers/oscal/POWER.md`, `README.md`, `CONTRIBUTING.md`: replace Trestle with oscal-bindings as the model library
    - `.kiro/steering/hatch.md` (one-off snippet example) and `.kiro/steering/product.md` (validator line): remove the Trestle references
    - Historical mentions ("replaced compliance-trestle") are allowed here, outside `src/`; current-dependency references are not
    - _Requirements: 11.3_

- [ ] 7. Step 2: Verification greps
  - [~] 7.1 Run the verification greps and record results
    - `grep -rIil trestle src bin tests pyproject.toml requirements.txt` → expect no output (Req 1.2–1.4, 2.6, 5.4, 10.4)
    - `grep -rnE "importlib|parse_oscal|from oscal_bindings import parse" src/mcp_server_for_oscal/tools/oscal_store.py src/mcp_server_for_oscal/tools/validate_oscal_content.py` → expect no output (Req 2.3, 3.3)
    - `grep -rnE "parse_oscal|from oscal_bindings import parse" src bin tests` → expect no output (Req 3.3)
    - `grep -rnE "Group1|Group2" tests` → expect no output (Req 10.3)
    - `grep -rnE "oscal_dict|_wrap_capability" src tests` → expect no output (Req 6.6)
    - `grep -rIil trestle AGENTS.md README.md CONTRIBUTING.md .agents/summary .kiro/steering conf/powers/oscal src/mcp_server_for_oscal/tools/README.md` → expect no current-dependency references (Req 11.3)
    - `git diff --stat main -- .kiro/specs ':!.kiro/specs/replace-trestle-with-oscal-bindings'` → expect empty (Req 11.6)
    - Fix any hits, then record each command's outcome in the "Verification grep results" section of this file
    - _Requirements: 1.2, 1.3, 1.4, 2.3, 2.6, 3.3, 5.4, 6.6, 10.3, 10.4, 11.3, 11.6_

- [~] 8. Final checkpoint
  - Run `hatch check fmt --fix`, `hatch check code --fix`, then `hatch run tests`; ensure all tests pass, ask the user if questions arise
  - Stage only the docs, steering, and `tasks.md` changes for this feature by name; commit, for example `Update docs and steering for oscal-bindings (#26); tests pass`
  - Do not push. Report to the user that the branch is ready and ask whether to push and open a PR

## Differential check results

Run with both libraries installed (oscal-bindings 0.1.0, schema 1.2.3); script deleted after the run.

| Source | Model type: documents |
|---|---|
| Bundled DB | catalog: 1, component-definition: 230 |
| `data/` zip | catalog: 1, component-definition: 230 |
| `tests/fixtures` | component-definition: 4 (`malformed_component_definition.json` is invalid JSON by design, not counted) |
| NIST oscal-content (`-min` duplicates skipped) | catalog: 17, profile: 9, system-security-plan: 5, assessment-plan: 2, assessment-results: 2, plan-of-action-and-milestones: 1, component-definition: 2 |
| Synthetic | mapping-collection: 2 (`mappings` as array and as single object) |

- Accept/reject: 0 mismatches. 505 documents accepted by both; `invalid_component_definition.json` rejected by both. Mutation probe (unknown root key, unknown `metadata` key, delete `uuid`, delete `metadata`): 505 × 4 = 2,020 mutants, all rejected by both.
- Child rows: identical counts and values for every type (component 1,729, capability 1, group 361, control 2,817, import 9, modify 5, control-implementation 5, system-component 14, task 2, activity 2, result 2, finding 2, poam-item 2) except mapping-collection with `mappings` as an array: Bindings returns a `Mappings` `RootModel`, and the extractor's `.uuid` access raises `AttributeError` outside the `try` in `_ensure_indexed` (Trestle 2 mapping rows, Bindings 1). The single-object form works. Static attribute audit: every extractor attribute path exists in both libraries; no `RootModel`/`AnyUrl` value drift found.
- Stored_Element_JSON vs source: Bindings 4,952/4,952 exact. Trestle 4,951 exact plus 2 datetime-only (assessment-results `observations[].collected`/`expires` normalized to UTC; accepted under Req 8.1). No non-datetime differences between libraries.
- Resolution (user decision): fix the mapping extractor in task 3.2 (unwrap `RootModel` before the list check) and add a regression test in task 4.5 (Req 7.3, 7.5). No other differences remain; the gate is cleared.
- Coverage caveats: mapping-collection coverage is synthetic only; capability coverage is a single fixture element.

## Req 5.5 logging check

_To be filled in by task 3.5._

## Verification grep results

_To be filled in by task 7.1._

## Notes

- Tasks marked with `*` are optional property tests and can be skipped for a faster MVP. Tasks 4.1, 4.7, 4.8, and 4.14 (validator rewrite, version guard, `MODEL_MAP` completeness, cdef output examples) are required
- Cdef tool output changes from snake_case `model_dump` keys to the source OSCAL element JSON (hyphenated keys). This is an accepted breaking change
- cairn-proofs/oscal-bindings-python#5 is tracked separately and is not a dependency of this feature
- Task 1.3 is a hard gate: any non-datetime difference needs a user decision before the swap
- Removing `compliance-trestle` in task 3.6 breaks test imports until tasks 4.3–4.5 land; the full suite is expected to pass only at checkpoint 5
- Existing LRU-cache and lazy-indexing tests stay unchanged and must pass (Req 7.4, 7.5)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["0.1"] },
    { "id": 1, "tasks": ["1.1"] },
    { "id": 2, "tasks": ["1.2"] },
    { "id": 3, "tasks": ["1.3"] },
    { "id": 4, "tasks": ["3.1"] },
    { "id": 5, "tasks": ["3.2", "3.3", "3.4", "3.5"] },
    { "id": 6, "tasks": ["3.6"] },
    { "id": 7, "tasks": ["3.7", "4.1", "4.3", "4.4", "4.5", "4.7"] },
    { "id": 8, "tasks": ["4.2", "4.8", "4.9", "4.14"] },
    { "id": 9, "tasks": ["4.6"] },
    { "id": 10, "tasks": ["4.10", "4.12", "4.13"] },
    { "id": 11, "tasks": ["4.11"] },
    { "id": 12, "tasks": ["6.1", "6.2", "6.3", "6.4"] },
    { "id": 13, "tasks": ["7.1"] }
  ]
}
```
