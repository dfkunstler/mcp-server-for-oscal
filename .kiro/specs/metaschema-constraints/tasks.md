# Implementation Plan: Metaschema Constraints

## Overview

GitHub issue #20. Implementation follows the two slices in design.md. Phase A (Core, PR 1) bundles and verifies the OSCAL 1.2.3 Metaschema sources, builds the pure path module, the safe loader and index, the response-budget pager, and markup rendering, then ships `get_allowed_values` with registration, docs, and startup warm-up. Core is complete and shippable without any Phase B task. Phase B (Extended, PR 2+) adds `get_constraints`, `find_references_to`, `get_definition`, and `get_metaschema_source`. Each is one new module that reads the core index, and each can ship on its own.

Implementation language: Python 3.13+ (as in design.md).

Repo conventions that apply to every task:

- Run all Python through hatch: `hatch test <path>` for tests (pytest flags after `--`, e.g. `hatch test tests/tools/test_metaschema_paths.py -- -x`), `hatch run typing`, `hatch check fmt --fix`, `hatch check code --fix`, `hatch run python ...` for scripts. Never call `python`, `pytest`, `mypy`, `ruff`, or `bandit` directly.
- Never prefix commands with `cd`. Set the working directory through the tool's `cwd` parameter. Restate this rule and the hatch rule to any subagent.
- Tools return plain dicts with hyphenated keys and no nulls, never Pydantic models.
- Tool errors: build a client-safe message (RC-13), call `try_notify_client_error(msg, ctx)`, then raise.
- Tests that touch the `get_index()` singleton use the `reset_metaschema_index` fixture from `tests/conftest.py`.
- Commits happen only at checkpoints, on the feature branch, with `#20` in the message and a note on whether the tests passed. Never commit to `main`, and never push without explicit approval.

## Tasks

### Phase A: Core (PR 1, Requirements 1-7, plus 12 for get_allowed_values)

- [x] 1. Bundle the Metaschema sources and add the update script
  - [x] 1.1 Add the `.gitattributes` entry for the bundle
    - Add `src/mcp_server_for_oscal/oscal_metaschema/** -text` after the `*` line, next to the `oscal_schemas` entry
    - _Requirements: 1.5_

  - [x] 1.2 Create `tools/metaschema_index.py` with `read_schema_versions(data: bytes) -> list[str]`
    - Use stdlib `pyexpat` only, with no entity expansion. Return every `<schema-version>` text in document order
    - The rest of the module is added in task 5; the update script (1.3) and the guard test (1.7) use this function
    - _Requirements: 3.4, 3.6_

  - [x] 1.3 Implement `bin/update_oscal_metaschema.py` (stdlib only)
    - Expose the importable functions `check_commit`, `download`, `select_members`, `check_versions`, `replace_bundle`, plus a CLI with `--version`, `--commit`, and `--dest`
    - Require `--commit` to be exactly 40 lowercase hex characters. Build the URL `https://github.com/usnistgov/OSCAL/archive/<commit>.zip` from the commit only, never from the tag
    - Download with `urllib.request.urlopen(..., context=ssl.create_default_context())`, a 300 s total deadline, and a 50 MB cap. Add `# nosec B310` with the reason (fixed `https://github.com/` prefix plus a validated commit)
    - Require the zip comment to equal the commit. Select only the exact members `OSCAL-<commit>/src/metaschema/oscal_*_metaschema.xml` and `.../shared-constraints/*.ent`, with no `extractall`. Require at least 1 module and at least 1 `.ent` file. Require every module to declare exactly one `<schema-version>` equal to `--version`
    - Write each file with `write_bytes` into a staging dir next to `dest`, byte-verbatim. Write `README.md` in the design format (`Source repository`, `Tag: v<version>`, `Commit: <sha>`, `Retrieved: <UTC date>`). Copy the existing `hashes.json` files if present; tolerate missing ones on the first population. Swap with `os.replace` (dest → backup, staging → dest, then delete backup). Remove the staging dir in `finally`
    - On any failure, print `ERROR: <step>: <detail>` to stderr, exit 1, and leave `dest` untouched. Print a reminder to run `hatch run rehash`
    - _Requirements: 1.4, 3.1, 3.2, 3.3, 3.4, 3.5_

  - [x] 1.4 Wire the metaschema step into `bin/update-oscal-schemas.sh`
    - Add `CURRENT_RELEASE_COMMIT="e061961c7702afeabffdf5e59894d35e748dade1"` next to `CURRENT_RELEASE_VERSION`, with the `gh api` dereference comment from the design
    - Call `python "$SCRIPT_DIR/update_oscal_metaschema.py" --version ... --commit ... --dest .../oscal_metaschema || exit 1` before the JSON/XSD download. The script runs under `hatch run update-oscal-schemas`, so `python` is the hatch env interpreter
    - _Requirements: 3.1, 3.2_

  - [x] 1.5 Extend the `rehash` script in `pyproject.toml`
    - Add `hatch run bin/update_hashes.py src/mcp_server_for_oscal/oscal_metaschema` and `git add` for `oscal_metaschema/hashes.json` and `oscal_metaschema/shared-constraints/hashes.json`
    - _Requirements: 2.1, 2.2_

  - [x] 1.6 Populate the bundle from the pinned commit (needs network once)
    - Run `hatch run update-oscal-schemas`, or the new script on its own: `hatch run python bin/update_oscal_metaschema.py --version 1.2.3 --commit e061961c7702afeabffdf5e59894d35e748dade1 --dest src/mcp_server_for_oscal/oscal_metaschema`. Then run `hatch run rehash`
    - Confirm the bundle holds 14 modules, 12 `.ent` files, `README.md`, and 2 manifests, and nothing else. Confirm that `oscal_schemas/` is unchanged if the full update script was used
    - The downloaded bytes and manifests are committed with the Core PR at checkpoint 3 or 11. CI never re-downloads them
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1_

  - [x] 1.7 Write the bundle guard tests in `tests/test_metaschema_bundle.py`
    - The file list is exactly the 14 modules, 12 `.ent` files, README, and 2 manifests. Every entity `SYSTEM` id and every `<import href>` resolves to a bundle file, and no unlisted file exists (1.7)
    - The README has `Tag: v<get_bundled_oscal_version()>` (3.7). `CURRENT_RELEASE_COMMIT` in `bin/update-oscal-schemas.sh` is 40 lowercase hex characters and equals the README `Commit:` line (3.8)
    - Every module declares exactly one `<schema-version>` equal to the bundled version, and the failure message names each offender and its version. Zero modules also fails (3.6)
    - Each manifest lists exactly the files in its directory (2.1). `verify_package_integrity` passes on the unmodified bundle (2.6). `rehash` lines are present in `pyproject.toml` (2.2). `git check-attr text` reports `unset` for a bundle file, skipped if `git` is unavailable (1.5)
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.7, 2.1, 2.2, 2.6, 3.6, 3.7, 3.8_

  - [x] 1.8 Write a property test for the update script in `tests/test_update_oscal_metaschema.py`
    - **Property 16: Metaschema update is all-or-nothing and byte-exact**
    - Fake archives built in memory, with `download` patched (no network)
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

  - [x] 1.9 Write example tests for the update script
    - The URL is built from the commit only. `urlopen` gets an `SSLContext` with `CERT_REQUIRED` and `check_hostname`. A different-SHA zip comment is rejected and the bundle is untouched. The CLI exits 1 and names the step for each failure kind
    - _Requirements: 3.1, 3.3_

- [x] 2. Verify the bundle at startup in both entry points
  - [x] 2.1 Add integrity helpers to `tools/utils.py`
    - Add `BUNDLED_CONTENT_DIRS = ("oscal_schemas", "oscal_metaschema")`, `INTEGRITY_ERRORS = (RuntimeError, KeyError, OSError, ValueError)`, and `verify_bundled_content(package_dir)`, which verifies every dir recursively
    - Add the directory to the `verify_package_integrity` mismatch and missing-file messages as a suffix, so existing substring tests still pass. Keep its exception types
    - _Requirements: 2.3, 2.4_

  - [x] 2.2 Call `verify_bundled_content` from `main.main()` and `oscal_agent.main()`
    - Add a `PACKAGE_DIR` module constant so tests can patch it. Catch `INTEGRITY_ERRORS`, call `logger.exception` naming the dir and file, and `raise SystemExit(2) from err`. Verification runs before store init and `_setup_tools()`
    - _Requirements: 2.3, 2.4, 2.5_

  - [x] 2.3 Write integrity example tests in `tests/test_metaschema_integrity.py`
    - Use a temp copy of the bundle with `PACKAGE_DIR` patched. Each of these gives exit 2 from both `main.main()` and `oscal_agent.main()`: one changed byte in a module, one changed byte in a `.ent` file, a missing listed file, an unlisted extra file in `shared-constraints/`, a missing manifest, and a malformed manifest. The unmodified copy passes. Verification runs before `_setup_tools`
    - _Requirements: 2.3, 2.4, 2.5, 2.6_

  - [x] 2.4 Write a property test for single-byte tampering
    - **Property 15: Any single-byte change fails startup verification**
    - **Validates: Requirements 2.4, 2.6**

- [x] 3. Checkpoint: bundle and integrity
  - Run `hatch run tests`, `hatch check fmt`, and `hatch check code`. Ensure all tests pass, ask the user if questions arise.
  - Commit point on the feature branch, with `#20` and the test status in the message. Do not commit as part of task execution unless the user approves.

- [x] 4. Implement `tools/metaschema_paths.py` (pure, no I/O)
  - [x] 4.1 Implement `Condition`, `PathAlternative`, and `normalize_target`
    - Use a quote-, paren-, and bracket-aware scanner. Split on top-level `|`. Handle a `doc(...)` prefix (`cross_document`). Split on top-level `/` and keep `//`. Strip leading `.`/`(.)` self steps into Conditions. Split predicates on top-level `and`. A Namespace_Condition is a single-literal `has-oscal-namespace`/`has-namespace` conjunct with no top-level `or`, and the namespace comes from the last step that has one
    - Unbalanced input gives `path=None`. The function never raises. `None` is treated as `.`
    - _Requirements: 6.4, 6.5, 8.7, 9.5, 9.7_

  - [x] 4.2 Implement `validate_selector`, `parse_selector`, `compose_selector`, and `MAX_SELECTOR_LENGTH = 512`
    - Reject empty input, more than 512 characters, any whitespace, a leading or trailing `/`, `///`, and any of `[ ] ( ) |`. The message names the selector (truncated to 80 characters) and the rule broken
    - `parse_selector` keeps a leading `//` in the remainder. `compose_selector` is its inverse
    - _Requirements: 5.2, 5.3, 5.11_

  - [x] 4.3 Write a property test for target normalization in `tests/tools/test_metaschema_paths.py`
    - **Property 1: Target normalization recovers the structure it was rendered from**
    - **Validates: Requirements 6.4, 6.5, 8.7, 9.5, 9.7**

  - [x] 4.4 Write a property test for the selector round trip
    - **Property 2: Target_Selector compose/parse round trip**
    - **Validates: Requirements 5.2, 5.3**

  - [x] 4.5 Write parametrized example tests from the design's normalization table and the selector rejection rules
    - _Requirements: 5.11, 6.4, 6.5_

- [x] 5. Implement the safe loader and index in `tools/metaschema_index.py`
  - [x] 5.1 Add the data model, constants, and path resolution
    - Add the frozen slotted dataclasses `SourceLocation`, `EnumValue`, `Constraint`, `FlagSlot`, `DefinitionKey`, `Definition`, `ModelChild`, and `LoadError` (client-safe fields only)
    - Add `MetaschemaLoadError(ValueError)`, `BUNDLE_DIR`, `MAX_FILE_BYTES = 2 * 1024 * 1024`, `MAX_DEPTH = 64`, `CONSTRAINT_TYPES`, and `model_module(model)` derived from `utils.schema_names`
    - `resolve_bundle_path(base_dir, ref, root, allowlist)` refuses, without opening anything: URL schemes and drive letters, leading `/` or `\`, UNC paths, NUL, paths that escape `root.resolve()` after `..` and symlinks are resolved, paths not in the manifest allowlist, and missing files
    - _Requirements: 4.2, 4.6, 4.11_

  - [x] 5.2 Implement the pyexpat loader
    - The allowlist comes from both manifests. Read each file bounded to `MAX_FILE_BYTES + 1` bytes. Use `ParserCreate(namespace_separator=" ")` with `SetParamEntityParsing(NEVER)`
    - In `EntityDeclHandler`, refuse parameter entities and internal values that contain `&`. In `ExternalEntityRefHandler`, refuse references from inside a `.ent` file, otherwise resolve through `resolve_bundle_path` and parse with `ExternalEntityParserCreate`, re-binding handlers per sub-parser so lines count per file
    - Keep a shared depth counter that refuses nesting above `MAX_DEPTH`. Build a `_Node` tree with `(source_file, line)` per element, keeping markup children
    - Per module, catch `_Refused`, `ExpatError`, `OSError` (use `strerror` only), and `UnicodeDecodeError`, and record a `LoadError` with bundle-relative names. Log the original exception with `exc_info=True`. Open no network connection
    - _Requirements: 4.1, 4.2, 4.3, 4.5, 4.6, 4.9, 4.10, 4.11_

  - [x] 5.3 Implement extraction, the import graph, and `ModelScope`
    - Extract global and local Definitions with `parent`, flag slots (inline, or `ref` with effective names), Constraints of `CONSTRAINT_TYPES` with `normalize_target`, attrs, key fields, and message, and enums in document order after expansion, each with its own Source_Location
    - Resolve imports through `resolve_bundle_path`. Compute each scope by BFS with a visited set, each module once, and precompute the scopes for all 8 `OSCALModelType`s and `all_modules()`
    - `MetaschemaIndex.load(root)` never raises for per-module errors. Failed modules are excluded, and every scope that reaches one carries its errors, so `scope()` raises `MetaschemaLoadError` with a client-safe message. A missing or malformed manifest raises `MetaschemaLoadError` naming the bundle-relative manifest
    - Add `definitions(name)`, `definition_names()`, `global_definition(kind, name)`, `source_files()`, `source_lines(rel)`, and `load_errors` (`MappingProxyType`)
    - _Requirements: 4.4, 4.5, 4.7, 4.8, 4.10, 4.11, 5.6_

  - [x] 5.4 Add the thread-safe `get_index()` singleton and the `reset_metaschema_index` fixture in `tests/conftest.py`
    - Use double-checked locking. If `load` fails, leave the singleton `None` so the next call retries
    - _Requirements: 4.8, 5.13_

  - [x] 5.5 Write a property test for entity expansion in `tests/tools/test_metaschema_index.py`
    - **Property 12: Entities expand in place with their own Source_Locations**
    - **Validates: Requirements 4.1, 4.5**

  - [x] 5.6 Write a property test for path resolution
    - **Property 13: Path resolution never escapes the allowlist** (use an `open` spy)
    - **Validates: Requirements 4.2, 4.6**

  - [x] 5.7 Write a property test for Model_Scope
    - **Property 14: Model_Scope is the import closure, each module once**
    - **Validates: Requirements 4.4, 4.7**

  - [x] 5.8 Write a property test for loader limits
    - **Property 23: Loader size and depth limits** (lower `MAX_FILE_BYTES` and `MAX_DEPTH` in the test)
    - **Validates: Requirements 4.8, 4.10**

  - [x] 5.9 Write a property test for error disclosure
    - **Property 24: Client-facing errors never disclose host paths**
    - **Validates: Requirements 4.6, 4.8, 4.11, 5.13**

  - [x] 5.10 Write loader example tests
    - Billion-laughs and parameter-entity modules give a `LoadError` (4.3). A missing entity file names its path (4.6). Patching `socket.socket` to raise has no effect on `load` (4.3). A 2,097,153-byte entity file and a 65-level module each give a `LoadError` naming the file and the limit (4.10). A load error affects only the scopes that reach it (4.8). 8 threads calling `get_index()` get one object from one parse
    - _Requirements: 4.3, 4.6, 4.8, 4.10_

- [x] 6. Add the response-budget pager
  - [x] 6.1 Implement `RESPONSE_BUDGET = 20_000` and `paginate_within_budget` in `tools/utils.py`
    - Call `paginate` first (its `ValueError` propagates), then merge `extra`. Size is `len(json.dumps(page))`. Binary-search the longest fitting prefix and set `limit=k` and `hasMore`. If one item alone is too large, shorten its `truncatable` fields at a code-point boundary with `…`, set `truncated: true`, and keep `source-file` and `line`
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.7_

  - [x] 6.2 Write a property test for the pager in `tests/test_utils.py`
    - **Property 10: Budget pager returns the longest fitting prefix**
    - **Validates: Requirements 12.3, 12.4, 12.5**

- [x] 7. Implement markup rendering
  - [x] 7.1 Implement `Markup.plain()`, `Markup.blocks()`, `_safe_href`, and the C0 filter in `tools/metaschema_index.py`
    - Remove C0 control characters except tab and newline. Render `code`/`q`/`em`/`strong` as Markdown. Render `a` as `[text](href)` only for `http`/`https`, `#fragment`, or a scheme-less relative href that does not start with `//`, with text escaped (`\ [ ]`) and the href percent-encoded (`( ) < >` and ≤ U+0020); otherwise emit the text only. Other elements become their text content
    - `plain` collapses whitespace and trims it. `blocks` separates paragraphs with `\n\n` and starts list items with `- `
    - _Requirements: 6.7, 6.8, 10.4_

  - [x] 7.2 Write a property test for markup rendering, plus examples showing that `javascript:` and `//host` links render as text only
    - **Property 11: Markup renders to the documented Markdown**
    - **Validates: Requirements 6.7, 6.8, 10.4**

- [x] 8. Checkpoint: paths, index, pager, markup
  - Run `hatch run tests`, `hatch check fmt`, and `hatch check code`. Ensure all tests pass, ask the user if questions arise.
  - Commit point (`#20`, test status in the message). Do not commit without approval.

- [x] 9. Implement the `get_allowed_values` tool
  - [x] 9.1 Create `tools/get_allowed_values.py`
    - Use a Strands `@tool` with the signature `(model, target, offset=0, limit=25, ctx=None) -> dict`. The docstring's first paragraph says "OSCAL Metaschema" and includes example questions. It documents the Target_Selector syntax with at least 2 examples and advises callers to also query `property/@name` and `link/@rel`
    - Validate `model` against `OSCALModelType`, pointing to `list_oscal_models`. Then `validate_selector`/`parse_selector`, both before any index access
    - Get `get_index().scope(...)`. If the definition is unknown, suggest up to 10 names using a bounded case-insensitive Levenshtein (segment capped at 64 characters, message segment at 80, banded with early exit, heap of the 10 best, ties broken by name)
    - Match `allowed-values` Constraints on every same-named Definition by non-cross-document `path == remainder`. For `@<flag>`, also follow inline and `ref` flag slots to the flag Definition with `path == ""`. Deduplicate by constraint
    - Build Allowed_Value_Items per the design key table (`flag-definition`, `namespace`/`conditions` across the matched alternatives, enum Source_Location). Sort by `(constraint source-file, constraint line, enum order)`
    - If nothing matches, return `paginate([], ...)` plus `suggested-targets` (0-20, distinct, sorted, each productive). Otherwise return `paginate_within_budget(items, offset, limit, truncatable=("description",))`
    - Every error goes through `try_notify_client_error` and is then raised. Return a plain dict
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 5.11, 5.12, 5.13, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.9, 6.10, 6.11, 6.12, 6.13, 7.1, 7.4, 12.1, 12.4, 12.5_

  - [x] 9.2 Write the required example tests in `tests/tools/test_get_allowed_values.py`
    - `component-definition`, `defined-component/prop/@name` returns `asset-type` with namespace `http://csrc.nist.gov/ns/oscal` (7.6)
    - The SSP `system-component/@type` returns `this-system`. The cdef `defined-component/@type` does not return `this-system`. The cdef `defined-component/link/@rel` returns `provided-by` and `used-by` from `shared-constraints/allowed-values-component_component_service.ent`, each with the `@type='service'` condition, and at least one `uses-network` item without that condition (7.7)
    - _Requirements: 7.6, 7.7, 7.10_

  - [x] 9.3 Write the required citation property test
    - **Property 6: Every citation points at its value** (over every bundle selector, including `<name>/@<flag>` selectors)
    - **Validates: Requirements 4.5, 6.12, 7.8**

  - [x] 9.4 Write the required paging property test
    - **Property 9: Paging walks the full result exactly once**
    - **Validates: Requirements 5.12, 7.9, 12.3, 12.6, 12.7**

  - [x] 9.5 Write a property test for argument rejection
    - **Property 3: Invalid arguments are rejected before the index is read**
    - **Validates: Requirements 5.9, 5.11, 5.12, 12.2**

  - [x] 9.6 Write a property test for matching
    - **Property 4: get_allowed_values equals a brute-force oracle**
    - **Validates: Requirements 5.2, 5.3, 5.4, 5.5, 5.6, 5.7**

  - [x] 9.7 Write a property test for item shape
    - **Property 5: Allowed_Value_Item shape**
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.6, 6.9, 6.10, 6.11**

  - [x] 9.8 Write a property test for suggested targets
    - **Property 7: Suggested targets are valid and productive**
    - **Validates: Requirements 5.8**

  - [x] 9.9 Write a property test for definition suggestions
    - **Property 8: Unknown-definition suggestions are the nearest names** (naive Levenshtein oracle)
    - **Validates: Requirements 5.10**

  - [x] 9.10 Write further example tests
    - `property/@name` returns `marking`. Check the signature and defaults (5.1) and the docstring rules (7.4). A patched index with a load error raises `MetaschemaLoadError` naming the bundle-relative file (5.13)
    - _Requirements: 5.1, 5.13, 7.4_

- [x] 10. Register the tool, update docs, and warm the index at startup
  - [x] 10.1 Add `get_allowed_values` to `get_tool_list()` in `tools/__init__.py`, with no AWS condition, and to `BASE_TOOL_NAMES` and the counts in `tests/test_tool_registry.py`
    - _Requirements: 7.2, 7.3, 7.5_

  - [x] 10.2 Update the docs in the same change
    - Add a row to the tool table in `src/mcp_server_for_oscal/tools/README.md`. Add the tool to the tool list in `README.md` and to the tool table in `conf/powers/oscal/POWER.md`
    - In `AGENTS.md`, add code-map rows for `metaschema_index.py`, `metaschema_paths.py`, `get_allowed_values.py`, and `oscal_metaschema/`, and update the notes for the `rehash` and `update-oscal-schemas` scripts (`CURRENT_RELEASE_COMMIT`)
    - _Requirements: 7.5_

  - [x] 10.3 Add `_warm_metaschema_index()` to `main.main()` and `oscal_agent.main()`
    - Call it after verification and before `_init_oscal_store()` and `_setup_tools()`. It calls `get_index()`, logs a warning on exception, and logs one warning per module with load errors. It never exits
    - _Requirements: 2.3, 4.8_

  - [x] 10.4 Extend `tests/test_integration_stdio_smoke.py`
    - `list_tools` includes `get_allowed_values`. `call_tool("get_allowed_values", {"model": "component-definition", "target": "defined-component/prop/@name"})` returns `asset-type`
    - In the CI `mcpb` job this runs from the installed wheel, which also verifies wheel inclusion and LF-exact checkout
    - _Requirements: 1.5, 1.6, 7.2_

  - [x] 10.5 Write `tests/test_metaschema_docs.py`
    - Each registered Metaschema_Tool appears in `tools/README.md`, `README.md`, `AGENTS.md`, and `POWER.md`. An AST scan checks that tool modules import `metaschema_index` and do not call `open`, `read_text`, or `read_bytes`
    - _Requirements: 7.1, 7.5_

- [x] 11. Final Core checkpoint (PR 1 ready)
  - Run `hatch run tests` (typing, tests on 3.13 and 3.14 with coverage, bandit), `hatch check fmt`, and `hatch check code`. Ensure all tests pass, ask the user if questions arise.
  - Commit point (`#20`, test status, updated `tasks.md`). Pushing and opening the PR need explicit user approval.

### Phase B: Extended (PR 2+, each tool ships on its own)

Each Extended tool reads only `get_index()` and `metaschema_paths`. Add small read-only accessors to `metaschema_index.py` only when needed. Registering a tool means adding it to `get_tool_list()` and `BASE_TOOL_NAMES` and adding rows in `tools/README.md`, `README.md`, `AGENTS.md`, and `POWER.md` in the same change (Requirement 7.5).

- [x] 12. Implement the `get_constraints` tool
  - [x] 12.1 Create `tools/get_constraints.py`
    - Signature: `(model, target, constraint_types=None, offset=0, limit=25, ctx=None)`. Generalize the core matcher to `CONSTRAINT_TYPES`. A bare definition name walks the Definition and all nested local Definitions. Apply the type filter and reject invalid types with the 7 valid names. Order by constraint Source_Location
    - Items carry the common keys plus type-specific keys (`value-count`, `key-fields`, `min-occurs`, ...) and `namespace`/`conditions`. Use `paginate_within_budget(truncatable=("message", "test"))` and the same model and definition errors as `get_allowed_values`
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9, 8.10, 8.11, 12.1, 12.4, 12.5_

  - [x] 12.2 Register `get_constraints` and update the docs and registry test
    - _Requirements: 7.2, 7.3, 7.4, 7.5_

  - [x] 12.3 Write a property test for agreement with `get_allowed_values` in `tests/tools/test_get_constraints.py`
    - **Property 17: get_constraints agrees with get_allowed_values and filters by type**
    - **Validates: Requirements 8.2, 8.3, 8.4, 8.5**

  - [x] 12.4 Write a property test for item shape
    - **Property 18: get_constraints item shape**
    - **Validates: Requirements 8.6, 8.7**

  - [x] 12.5 Write example tests for signature, empty result, invalid types, and paging errors
    - _Requirements: 8.1, 8.8, 8.9, 8.10, 8.11_

- [x] 13. Checkpoint: get_constraints
  - Run `hatch run tests`, `hatch check fmt`, and `hatch check code`. Ensure all tests pass, ask the user if questions arise. Commit point (`#20`); do not commit without approval.

- [x] 14. Implement the `find_references_to` tool
  - [x] 14.1 Create `tools/find_references_to.py`
    - Signature: `(assembly, model=None, offset=0, limit=25, ctx=None)`. Index `index` Constraints by name across `all_modules()`. Match on the last step or a `use-name`. Pair each match with `index-has-key` by `name`, filtered by the `model` scope when given
    - Items carry `index-*` keys, `key-fields`, `namespace`/`conditions`, and `cross-document`. Order by `(index-has-key source-file, line, index-name)`
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10, 9.11, 12.1, 12.4_

  - [x] 14.2 Register `find_references_to` and update the docs and registry test
    - _Requirements: 7.2, 7.3, 7.4, 7.5_

  - [x] 14.3 Write a property test for reference pairing in `tests/tools/test_find_references_to.py`
    - **Property 19: find_references_to pairs every key reference with its index**
    - **Validates: Requirements 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.11**

  - [x] 14.4 Write example tests
    - SSP `component` includes the `component/link[@rel='depends-on']` and `component/link[@rel='uses-service']` `@href` references; SSP `by-component` includes the cross-document `by-component-uuid` reference from `link[@rel='provided-by']` `@href` (the bundled 1.2.3 Metaschema has no `index-has-key` on `by-component` `@component-uuid`). Also test an invalid model, paging errors, and an empty `assembly`
    - _Requirements: 9.1, 9.8, 9.9, 9.10_

- [x] 15. Checkpoint: find_references_to
  - Run `hatch run tests`, `hatch check fmt`, and `hatch check code`. Ensure all tests pass, ask the user if questions arise. Commit point (`#20`); do not commit without approval.

- [x] 16. Implement the `get_definition` tool
  - [x] 16.1 Create `tools/get_definition.py`
    - Signature: `(model=None, name, kind=None, offset=0, limit=10, ctx=None)`. Matches carry `kind`, `name`, `scope`, `formal-name`, `description` (`plain`), `remarks` (`blocks`), `use-name`/`group-as`, `flags`, `model` children, a `constraints` summary, `parent-definition` for local matches, and the Source_Location, with no nulls. Order by Source_Location
    - Use `paginate_within_budget(truncatable=("remarks",))`. Errors: name length or no match (up to 10 nearest names), invalid model, invalid kind
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 12.1, 12.4, 12.5_

  - [x] 16.2 Register `get_definition` and update the docs and registry test
    - _Requirements: 7.2, 7.3, 7.4, 7.5_

  - [x] 16.3 Write a property test for same-named Definitions in `tests/tools/test_get_definition.py`
    - **Property 20: get_definition returns every same-named Definition within budget**
    - **Validates: Requirements 10.2, 10.3, 10.5, 10.6**

  - [x] 16.4 Write example tests for `uuid` across modules and for the error cases
    - _Requirements: 10.5, 10.7, 10.8, 10.9_

- [x] 17. Checkpoint: get_definition
  - Run `hatch run tests`, `hatch check fmt`, and `hatch check code`. Ensure all tests pass, ask the user if questions arise. Commit point (`#20`); do not commit without approval.

- [x] 18. Implement the `get_metaschema_source` tool
  - [x] 18.1 Create `tools/get_metaschema_source.py`
    - Signature: `(file=None, start_line=1, line_count=200, ctx=None)`. Look up `file` by exact match in `source_files()` before any read. Read only through `source_lines()`
    - Return `file`, `start-line`, `end-line`, `total-lines`, `has-more`, and verbatim `text`, cut by whole lines to fit `RESPONSE_BUDGET`. Listing mode returns `files` with `line-count`, sorted
    - Errors: a file not in the allowlist lists the available files, plus range errors for `start_line` and `line_count`
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 12.3_

  - [x] 18.2 Register `get_metaschema_source` and update the docs and registry test
    - _Requirements: 7.2, 7.3, 7.4, 7.5_

  - [x] 18.3 Write a property test for page reconstruction in `tests/tools/test_get_metaschema_source.py`
    - **Property 21: get_metaschema_source pages reconstruct the file**
    - **Validates: Requirements 11.2, 11.4**

  - [x] 18.4 Write a property test for allowlist rejection
    - **Property 22: get_metaschema_source rejects everything outside the allowlist** (use an `open` spy)
    - **Validates: Requirements 11.3, 11.6, 11.7**

  - [x] 18.5 Write example tests for listing mode and range errors
    - _Requirements: 11.5, 11.6, 11.7_

- [x] 19. Checkpoint: get_metaschema_source
  - Run `hatch run tests`, `hatch check fmt`, and `hatch check code`. Ensure all tests pass, ask the user if questions arise. Commit point (`#20`); do not commit without approval.

## Notes

- Tasks marked with `*` are optional and supplementary. The required test tasks (1.7, 2.3, 9.2, 9.3, 9.4, 10.4) cover tests that the requirements mandate (1.7, 2.6, 3.6-3.8, 7.6-7.10) or that the design relies on to verify wheel inclusion (1.6).
- Task 1.6 is the only task that needs network access. The downloaded bundle and manifests are committed, so CI never re-downloads them.
- Property numbers follow design.md. Properties 23 and 24 are core properties numbered after the extended ones.
- Phase A has no dependency on Phase B. Each Phase B tool (tasks 12, 14, 16, 18) can ship as its own PR.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.5", "2.1", "4.1"] },
    { "id": 1, "tasks": ["1.3", "2.2", "4.2", "6.1"] },
    { "id": 2, "tasks": ["1.4", "1.8", "4.3", "5.1", "6.2"] },
    { "id": 3, "tasks": ["1.6", "1.9", "4.4", "5.2"] },
    { "id": 4, "tasks": ["1.7", "2.3", "4.5", "5.3"] },
    { "id": 5, "tasks": ["2.4", "5.4"] },
    { "id": 6, "tasks": ["5.5", "7.1"] },
    { "id": 7, "tasks": ["5.6", "9.1"] },
    { "id": 8, "tasks": ["5.7", "9.2", "10.1"] },
    { "id": 9, "tasks": ["5.8", "9.3", "10.2", "10.3"] },
    { "id": 10, "tasks": ["5.9", "9.4", "10.4"] },
    { "id": 11, "tasks": ["5.10", "9.5", "10.5"] },
    { "id": 12, "tasks": ["7.2", "9.6"] },
    { "id": 13, "tasks": ["9.7"] },
    { "id": 14, "tasks": ["9.8"] },
    { "id": 15, "tasks": ["9.9"] },
    { "id": 16, "tasks": ["9.10"] },
    { "id": 17, "tasks": ["12.1"] },
    { "id": 18, "tasks": ["12.2", "14.1"] },
    { "id": 19, "tasks": ["12.3", "14.2", "16.1"] },
    { "id": 20, "tasks": ["12.4", "14.3", "16.2", "18.1"] },
    { "id": 21, "tasks": ["12.5", "14.4", "16.3", "18.2"] },
    { "id": 22, "tasks": ["16.4", "18.3"] },
    { "id": 23, "tasks": ["18.4"] },
    { "id": 24, "tasks": ["18.5"] }
  ]
}
```
