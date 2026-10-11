# Implementation Plan

## Ground rules (apply to every task)

- Run all Python through hatch: `hatch test`, `hatch run tests`, `hatch run typing`, `hatch check fmt --fix`, `hatch check code --fix`. Never call `python`, `pytest`, `mypy`, `ruff`, or `bandit` directly.
- Pass pytest flags after `--`, e.g. `hatch test tests/tools/test_get_schema.py -- -k xsd -x`.
- Never prefix shell commands with `cd`; use the tool's `cwd` parameter. Restate this and the hatch rule to any subagent.
- Work only on the feature branch for issue #13. Never commit to `main`.
- Run `hatch run tests` before each commit. Commit messages include `#13` and state whether tests passed.
- Stage only files changed for this fix, by name. Never push without explicit user approval.

- [-] 0. Create the feature branch and commit the spec
  - Confirm the current branch is `main` and the working tree has no unrelated staged changes (`git status`)
  - Create and check out a branch linked to the issue: `gh issue develop 13 --checkout` (or `git checkout -b fix/13-xsd-schema-retrieval` if linking fails)
  - Run `hatch run tests` (baseline; record the result)
  - Stage only `.kiro/specs/xsd-schema-retrieval-fix/{.config.kiro,bugfix.md,design.md,tasks.md}`
  - Commit: `docs(spec): add XSD schema retrieval bugfix spec (#13) - tests passed|failed`
  - Do not push

- [~] 1. Write bug condition exploration tests
  - **Property 1: Bug Condition** - XSD requests return bundled XSD text, and schema file handles are closed
  - **CRITICAL**: These tests MUST FAIL on unfixed code. Failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: The tests encode the expected behavior and will validate the fix once they pass
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: The valid domain is finite (9 models × 2 types), so cover it exhaustively with `pytest.mark.parametrize` instead of Hypothesis sampling (Windows CI caps examples at 10)
  - Add shared helpers to `tests/tools/test_get_schema.py`:
    - `SCHEMA_DIR = Path(get_schema_module.__file__).parent.parent / "oscal_schemas"`
    - `VALID_MODELS = [*OSCALModelType, "complete"]` (use `.value` strings)
    - `expected_text(name, type)` reading raw bytes and decoding UTF-8 (independent oracle; catches newline/encoding drift)
    - Handle-tracking fixture: patch `open_schema_file` with `side_effect` calling the real function, recording each returned handle
  - Bug condition: `isBugCondition(input) = model_name IN VALID_MODELS AND schema_type == "xsd"`; `isHandleLeakCondition(input) = model_name IN VALID_MODELS AND schema_type IN {"json","xsd"}`
  - Tests (no `json.load` mocking):
    - XSD, parametrized over `VALID_MODELS` (includes `complete`, `system-security-plan`, `plan-of-action-and-milestones`): `get_oscal_schema(None, m, "xsd") == expected_text(m, "xsd")`
    - XSD with mock `ctx`: `ctx.error` not called for a valid request
    - Handle closed, parametrized over `VALID_MODELS × {"json","xsd"}`: exactly one handle opened and `handle.closed` after the call (XSD cases can't reach this assertion pre-fix; that's fine)
    - Handle closed on JSON parse error: tracker returns a real handle to a `tmp_path` file containing `not json`; `json.JSONDecodeError` raised and handle closed
  - Run on UNFIXED code: `hatch test tests/tools/test_get_schema.py -- -k "xsd or closed"`
  - **EXPECTED OUTCOME**: Tests FAIL. Expected counterexamples: `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` for all 9 XSD cases plus a "failed to open schema ..." client notification; `handle.closed is False` after JSON success and parse error
  - If failures differ (e.g., wrong file name), revisit the root-cause hypothesis in design.md before continuing
  - Record the counterexamples in this task when marking it complete
  - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.3_

- [~] 2. Write preservation property tests (BEFORE implementing the fix)
  - **Property 2: Preservation** - JSON output and error contracts unchanged
  - **IMPORTANT**: Follow observation-first methodology. Observe unfixed behavior for inputs where `isBugCondition` is false, then encode it
  - Observe on unfixed code: `get_oscal_schema(None, "catalog", "json")` equals `json.dumps(json.loads(raw_bytes))`; `get_oscal_schema()` returns the `complete` JSON; `schema_type="JSON"` raises `ValueError("Invalid schema type: JSON.")`; `model_name="Catalog"` raises `ValueError` matching `"Invalid model: "`
  - Tests in `tests/tools/test_get_schema.py`:
    - JSON output identity, parametrized over `VALID_MODELS`: result is byte-identical to `json.dumps(json.loads(raw))` and parses equal to the file
    - Default arguments: `get_oscal_schema()` equals the expected `complete` JSON output
    - Hypothesis: `st.text().filter(lambda s: s not in {"json", "xsd"})` for `schema_type` → `ValueError` with exact message `"Invalid schema type: <value>."`, `open_schema_file` not called
    - Hypothesis: `st.text().filter(lambda s: s not in VALID_MODELS)` × `st.sampled_from(["json", "xsd"])` → `ValueError` matching `"Invalid model: "`, `open_schema_file` not called
    - Use `ctx=None` and create mocks inside the test body (no function-scoped fixtures in Hypothesis tests)
  - Keep existing `test_get_schema_file_not_found` assertions (JSON open failure → "failed to open schema <file>", original exception re-raised)
  - Do not include handle-closed assertions here; those belong to Property 1
  - Run on UNFIXED code: `hatch test tests/tools/test_get_schema.py`
  - **EXPECTED OUTCOME**: Preservation tests PASS (baseline confirmed)
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [ ] 3. Fix XSD schema retrieval and file handle leak

  - [~] 3.1 Implement the fix in `src/mcp_server_for_oscal/tools/get_schema.py`
    - Replace the `try` body with a `with open_schema_file(schema_file_name) as schema_file:` block
    - Inside it, branch: `xsd` → `schema_text = schema_file.read()`; otherwise `schema_text = json.dumps(json.load(schema_file))`
    - Keep the `except Exception` block unchanged (log, `try_notify_client_error`, re-raise); `return schema_text` after the `try` (avoids ruff `TRY300`)
    - Keep validation order, error messages, the entry `logger.debug`, and `open_schema_file` unchanged
    - Docstring: update the `schema_type` arg line (`json` default returns JSON schema; `xsd` returns XSD; other values rejected) and `Returns` (JSON string for `json`, XML (XSD) text for `xsd`). Leave the first paragraph untouched (used by the MCPB manifest)
    - _Bug_Condition: isBugCondition(input) where model_name ∈ VALID_MODELS AND schema_type = "xsd"; isHandleLeakCondition(input) where schema_type ∈ {"json","xsd"}_
    - _Expected_Behavior: XSD result equals raw bundled file text decoded UTF-8; every opened handle is closed on return or raise (Properties 1, 2 in design)_
    - _Preservation: JSON output byte-identical, defaults, validation errors, and open/parse error reporting unchanged (Property 3 in design)_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

  - [~] 3.2 Update existing tests broken by the `with` statement and remove bug-hiding mocks
    - `tests/tools/test_get_schema.py`:
      - Delete `test_get_schema_success_xsd_schema` (superseded by task 1 tests)
      - Convert `test_get_schema_success_default_params`, `_catalog_model`, `_ssp_model`, `_poam_model`, `test_get_schema_logging`, `test_get_schema_all_valid_models` to read real files via a pass-through wrapper (`side_effect=open_schema_file`), keeping `assert_called_once_with(<file name>)`; drop `json.load` mocks and the `json.load` call assertion in `_default_params`
      - `test_get_schema_json_parse_error`: use `mock_open(read_data="not json")` or a `tmp_path` handle; keep the `JSONDecodeError` re-raise and client message assertions
      - Add XSD open-failure test: `FileNotFoundError` with `schema_type="xsd"` → `ctx.error` called with `"failed to open schema oscal_catalog_schema.xsd"` and exception re-raised
      - Add docstring test: `Returns` mentions both JSON and XSD/XML, using the same docstring access the tool-registry tests use (Property 4)
      - Keep the `open_schema_file` helper tests unchanged
    - `tests/test_integration.py`:
      - `test_get_schema_tool_integration`: remove `open_schema_file` and `json.load` mocks; assert real catalog JSON (has `"$schema"`); add an XSD counterpart asserting the text equals the file (starts with `<xs:schema`) and no client error
      - `test_schema_file_integration`: switch to pass-through wrapper and loop over `schema_type` in `{"json","xsd"}`, keeping `assert_called_with(expected_filename)`
    - `tests/test_integration_stdio_smoke.py`: add one `get_oscal_schema` XSD call only if it's a one-line addition to existing tool calls; otherwise skip and note why
    - _Requirements: 2.1, 2.2, 2.4, 3.5, 3.6_

  - [~] 3.3 Update docs and known-bug references
    - `src/mcp_server_for_oscal/tools/README.md`: `get_oscal_schema` Returns → "JSON string for `json`; raw XSD (XML) text for `xsd`"
    - `.agents/summary/review_notes.md`: mark B1 (#13) resolved
    - `.agents/summary/components.md`: remove "XSD path is broken (always `json.load`)"
    - `AGENTS.md`: drop "XSD schema retrieval #13" from the known-bugs bullet
    - _Requirements: 2.4_

  - [~] 3.4 Verify bug condition exploration tests now pass
    - **Property 1: Expected Behavior** - XSD requests return bundled XSD text, and schema file handles are closed
    - **IMPORTANT**: Re-run the SAME tests from task 1. Do NOT write new tests
    - Run: `hatch test tests/tools/test_get_schema.py -- -k "xsd or closed"`
    - **EXPECTED OUTCOME**: Tests PASS (bug fixed)
    - _Requirements: 2.1, 2.2, 2.3_

  - [~] 3.5 Verify preservation tests still pass
    - **Property 2: Preservation** - JSON output and error contracts unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2. Do NOT write new tests
    - Run: `hatch test tests/tools/test_get_schema.py tests/test_integration.py`
    - **EXPECTED OUTCOME**: Tests PASS (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6_

- [~] 4. Checkpoint - Ensure all tests pass, then commit
  - `hatch check fmt --fix` and `hatch check code --fix`; resolve any findings with targeted `# noqa: RULE - reason` only if justified
  - `hatch run typing`
  - `hatch run tests` (mypy, pytest on 3.13 + 3.14 with coverage, bandit). All must pass
  - Confirm `git branch --show-current` is the #13 feature branch, not `main`
  - Stage only files changed for this fix, by name: `src/mcp_server_for_oscal/tools/get_schema.py`, `src/mcp_server_for_oscal/tools/README.md`, `tests/tools/test_get_schema.py`, `tests/test_integration.py`, `tests/test_integration_stdio_smoke.py` (if changed), `.agents/summary/review_notes.md`, `.agents/summary/components.md`, `AGENTS.md`, `.kiro/specs/xsd-schema-retrieval-fix/tasks.md`
  - Commit: `fix(get_schema): return raw XSD text and close schema file handles (#13) - tests passed`
  - Do not push. Ask the user before pushing or opening a PR
  - Ask the user if questions arise
